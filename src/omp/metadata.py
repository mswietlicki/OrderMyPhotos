"""What OrderMyPhotos writes into a kept photo's file, and the writing (design.md S9 "Metadata goes in once per
file", P67): the title, the description, the tags, the people and animals with their regions, and the turn, in
one exiftool write per file.

- `plan()` makes one entry per kept photo, for the bundle's `metadata.jsonl` (plan.py).
- `write()` puts an entry into a copy of the photo and checks the copy: a failed write never touches the
  original. A test set's apply (apply.py) writes its photos in place this way. The library's photos are
  written on Mimir's host by `mimir-onedrive apply`, which uploads each once (S9).

The fields, so that Windows, OneDrive, Immich and digiKam agree (design.md S9's table):
- **title:** XMP-dc:Title and EXIF:XPTitle, the caption's Polish title;
- **description:** XMP-dc:Description, EXIF:ImageDescription and IPTC:Caption-Abstract, Polish then English;
- **flat keywords:** XMP-dc:Subject and IPTC:Keywords: each tag's Polish label, and the people's and animals'
  names. Never XPKeywords;
- **hierarchical keywords:** XMP-lr:HierarchicalSubject ("Places|Mountains", "People|Anna Kowalska") and
  XMP-digiKam:TagsList ("Places/Mountains", "People/Anna Kowalska"), the vocabulary's English paths;
- **regions:** MWG RegionInfo, a Face region per named face and a Pet region per named animal. They're in the
  stored image's coordinates, as MWG has it (before the EXIF Orientation is applied); the boxes were found on
  upright previews, so they're turned back. A turn written as the Orientation then leaves them right;
- **the turn** (P36): the EXIF Orientation;
- **provenance:** XMP-xmp:CreatorTool, and the omp namespace (exiftool.config) with what was written.

What the user wrote stays (design.md "Provenance of metadata"):
- a title or description of the user's (the inventory's provenance "user") is kept; one that v1 wrote, or that
  this module wrote and nobody changed since (WrittenTitle), is replaced;
- keywords: the user's stay and ours are added. v1's are dropped, and so are the ones this module added
  before (WrittenKeywords), on a second write;
- regions of someone else's are kept, and ours aren't written.

Only the stdlib, and no import from the package: Mimir's host loads this file by its path (mimir-onedrive).
"""

import json
import os
import re
import subprocess
import tempfile
import threading
from collections import defaultdict
from pathlib import Path

VERSION = "1"
TOOL = "OrderMyPhotos v2"
CONFIG = Path(__file__).with_name("exiftool.config")
WRITABLE = {".jpg", ".jpeg", ".mpo", ".heic", ".png", ".webp", ".tif", ".tiff"}  # not GIF, not raw (CR2)
IPTC = {".jpg", ".jpeg", ".mpo", ".tif", ".tiff"}  # the formats IPTC belongs in
TURN = [1, 6, 3, 8]  # EXIF Orientation of an unmirrored photo, a quarter turn clockwise at each step
# camera junk that isn't a description (as inventory.JUNK_DESCRIPTIONS; Immich reads ImageDescription first)
JUNK = re.compile(r"^\s*(OLYMPUS DIGITAL CAMERA|SONY DSC|DCIM.*|default|Description|\s*)\s*$", re.I)
# what a photo carries inside that a write must keep: Google's and Samsung's motion photo video, an MPO's
# other images
EMBEDDED = ("MotionPhotoVideo", "EmbeddedVideoFile", "MPImage2", "MPImage3")


# --- the plan's entries ---

def stored_area(box, orientation) -> dict:
    """A box [x0, y0, x1, y1], normalized on the upright photo, as an MWG area (centre, width and height,
    normalized) on the stored pixels: the upright photo is the stored one with its EXIF Orientation applied."""
    o = orientation if orientation in range(1, 9) else 1  # nonsense values: the previews took the file as it is
    back = {1: lambda x, y: (x, y), 2: lambda x, y: (1 - x, y), 3: lambda x, y: (1 - x, 1 - y),
            4: lambda x, y: (x, 1 - y), 5: lambda x, y: (y, x), 6: lambda x, y: (y, 1 - x),
            7: lambda x, y: (1 - y, 1 - x), 8: lambda x, y: (1 - y, x)}[o]
    x0, y0, x1, y1 = (min(1.0, max(0.0, float(v))) for v in box)
    (a, b), (c, d) = back(x0, y0), back(x1, y1)
    u0, u1, v0, v1 = min(a, c), max(a, c), min(b, d), max(b, d)
    return {"X": round((u0 + u1) / 2, 4), "Y": round((v0 + v1) / 2, 4), "W": round(u1 - u0, 4),
            "H": round(v1 - v0, 4), "Unit": "normalized"}


def clean(name) -> str:
    """A name as a keyword: one line, no hierarchy separators."""
    return re.sub(r"\s+", " ", re.sub(r"[|/\\]", "-", str(name or ""))).strip()


def prior_of(meta: dict):
    """Who wrote the title, description and keywords the inventory found (photo.meta): "v1", "v2", "user"
    (anything else, P4 keeps it) or None (nothing there)."""
    prov = meta.get("provenance")
    return "v1" if prov == "v1" else "v2" if prov == "v2" else "user" if meta.get("existing") else None


def turned(orientation: int, degrees: int):
    """The Orientation after a clockwise turn, None for a mirrored photo (left as it is) or no turn."""
    if not degrees % 360 or orientation not in TURN:
        return None
    return TURN[(TURN.index(orientation) + degrees % 360 // 90) % 4]


def plan(con, kept: list[dict]) -> list[dict]:
    """The entries for `metadata.jsonl`, one per kept photo with something to write. `kept`: dicts with `id`,
    `path` (where the photo is now), `dst` (where the plan puts it), `turn` (degrees clockwise) and `tags`
    (the detectors' [path, Polish label] pairs, e.g. nudity)."""
    captions = {}
    for i, data in con.execute("SELECT id, data FROM caption ORDER BY rowid"):  # the newest model's last
        captions[i] = json.loads(data)
    photo = {r[0]: (r[1], json.loads(r[2] or "{}")) for r in con.execute("SELECT id, orientation, meta FROM photo")}
    people, pets = defaultdict(list), defaultdict(list)
    try:
        rows = con.execute("SELECT f.id, f.box, p.name, p.short, p.owner FROM face f JOIN person p ON p.pid=f.person"
                           " WHERE p.name IS NOT NULL OR p.short IS NOT NULL OR p.owner=1").fetchall()
        owner = next((clean(r[2] or r[3]) for r in rows if r[4] and (r[2] or r[3])), "")  # the owner's clusters
        for i, box, name, short, is_owner in rows:
            n = clean(name or short) or (owner if is_owner else "")
            if n:
                people[i].append((n, json.loads(box)))
    except Exception:  # noqa: BLE001 - no people stage yet
        pass
    try:
        for i, box, name, short, kind in con.execute(
                "SELECT m.id, m.box, p.name, p.short, p.kind FROM pet_member m JOIN pet p ON p.pid=m.pid"
                " WHERE p.name IS NOT NULL OR p.short IS NOT NULL"):
            pets[i].append((clean(name or short), kind or "animal", json.loads(box)))
    except Exception:  # noqa: BLE001 - no pets yet
        pass
    out = []
    for k in kept:
        if Path(k["dst"]).suffix.lower() not in WRITABLE:
            continue
        o, meta = photo.get(k["id"], (1, {}))
        o = o if o in range(1, 9) else 1
        c = captions.get(k["id"], {})
        desc = "\n\n".join(t for t in (str(c.get(f) or "").strip() for f in ("description_pl", "description_en")) if t)
        tags = [[t["path"], t.get("pl") or t["path"].rsplit("/", 1)[-1]] for t in c.get("v2_tags", [])]
        tags += [list(t) for t in k.get("tags", []) if list(t) not in tags]
        # the turn as the Orientation it gives: written twice, the photo isn't turned twice. Not for HEIC, which
        # viewers turn by its own `irot` box: the EXIF Orientation would turn it twice, or not at all
        turn = 0 if Path(k["dst"]).suffix.lower() == ".heic" else int(k.get("turn") or 0)
        e = {"path": k["dst"], "from": k["path"], "id": k["id"], "prior": prior_of(meta), "orientation": o,
             "target": turned(o, turn), "title": str(c.get("title_pl") or "").strip(),
             "description": desc, "tags": tags,
             "people": [{"name": n, "area": stored_area(b, o)} for n, b in people.get(k["id"], [])],
             "pets": [{"name": n, "kind": kind, "area": stored_area(b, o)} for n, kind, b in pets.get(k["id"], [])]}
        if e["target"] or e["title"] or e["description"] or e["tags"] or e["people"] or e["pets"]:
            out.append(e)
    return out


# --- exiftool ---

class ExifTool:
    """One exiftool kept running (-stay_open): a command takes milliseconds instead of a start's ~0.3 s.
    One per thread: commands are answered in order."""

    def __init__(self, exe: str = "exiftool"):
        self.p = subprocess.Popen([exe, "-config", str(CONFIG), "-stay_open", "True", "-@", "-"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.n = 0
        self.lock = threading.Lock()

    def run(self, *args: str) -> tuple[str, str]:
        """stdout and stderr of one command."""
        with self.lock:
            self.n += 1
            tag = f"{{ready{self.n}}}"
            cmd = [*args, "-charset", "filename=UTF8", "-echo4", tag, f"-execute{self.n}"]
            self.p.stdin.write(("\n".join(cmd) + "\n").encode("utf-8"))  # an argument a line: none has a newline
            self.p.stdin.flush()
            out = self._until(self.p.stdout, tag)
            err = self._until(self.p.stderr, tag)
            return out, err

    @staticmethod
    def _until(stream, tag: str) -> str:
        lines = []
        while True:
            line = stream.readline()
            if not line:
                raise RuntimeError("exiftool stopped")
            s = line.decode("utf-8", "replace")
            if s.rstrip("\r\n").endswith(tag):
                lines.append(s.rstrip("\r\n")[: -len(tag)])
                return "".join(lines)
            lines.append(s)

    def read(self, path: Path) -> dict:
        """What write() needs to know of a file, by group:name, numbers as numbers."""
        out, err = self.run("-j", "-G1", "-n", "-struct",
                            "-FileType", "-Orientation", "-ImageWidth", "-ImageHeight", "-ImageDataHash",
                            "-XMP-dc:Title", "-XPTitle", "-XMP-dc:Description", "-EXIF:ImageDescription",
                            "-IPTC:Caption-Abstract", "-XMP-dc:Subject", "-IPTC:Keywords", "-XPKeywords",
                            "-XMP-lr:HierarchicalSubject", "-XMP-digiKam:TagsList", "-XMP-mwg-rs:RegionInfo",
                            "-XMP-xmp:CreatorTool", "-XMP-omp:all", *[f"-{v}" for v in EMBEDDED], str(path))
        try:
            return json.loads(out)[0]
        except (ValueError, IndexError) as x:
            raise RuntimeError(f"exiftool read {path}: {err.strip() or x}") from None

    def close(self):
        if self.p.poll() is None:
            self.p.stdin.write(b"-stay_open\nFalse\n")
            self.p.stdin.flush()
            self.p.wait(timeout=30)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _list(v, sep: str | None = None) -> list[str]:
    """A list tag as exiftool's JSON gives it: a list, or one item alone (a keyword may hold a comma). `sep`
    for a tag that keeps its items in one string (XPKeywords: ";")."""
    if v in (None, ""):
        return []
    if isinstance(v, list):
        return [str(x) for x in v]
    return [s.strip() for s in str(v).split(sep) if s.strip()] if sep else [str(v)]


def _by_name(tags: dict, name: str):
    return next((v for k, v in tags.items() if k.rsplit(":", 1)[-1] == name), None)


def _orientation(tags: dict) -> int:
    """The EXIF Orientation, 1 when there's none or it's nonsense (as the previews took it)."""
    v = tags.get("IFD0:Orientation")
    return v if isinstance(v, int) and v in range(1, 9) else 1


def _first(tags: dict, *names) -> str:
    for n in names:
        v = tags.get(n)
        if v not in (None, ""):
            return str(v)
    return ""


def _embedded(tags: dict) -> dict[str, int]:
    """The sizes of what the file carries inside (EMBEDDED), by name."""
    out = {}
    for k, x in tags.items():
        if k.rsplit(":", 1)[-1] in EMBEDDED and isinstance(x, str):  # "(Binary data 1234567 bytes, use -b ...)"
            m = re.search(r"(\d+) bytes", x)
            if m:
                out[k.rsplit(":", 1)[-1]] = int(m.group(1))
    return out


def _uniq(xs) -> list[str]:
    seen, out = set(), []
    for x in xs:
        if x and x.lower() not in seen:
            seen.add(x.lower())
            out.append(x)
    return out


def changes(tags: dict, e: dict, run: str = "") -> tuple[dict, list[str], dict]:
    """What to write into a file whose tags `read()` gave: (the JSON exiftool imports, the extra arguments,
    a note of what was written and kept)."""
    ext = "." + str(tags.get("File:FileType", "")).lower()
    ours_v2 = TOOL in _first(tags, "XMP-xmp:CreatorTool") or "XMP-omp:Version" in tags
    prior = "v2" if ours_v2 else e.get("prior")
    new, extra, note = {"SourceFile": "*"}, [], {"wrote": [], "kept": []}

    def keep_or_write(field, current, ours, written_key, same):
        """The user's value stays; v1's, ours unchanged since, or none, is replaced (when we have one and it
        isn't there already: a photo written again unchanged isn't written, nor uploaded, again)."""
        if not ours or same:
            return False
        if current and prior == "v2" and current != tags.get(written_key, ""):
            note["kept"].append(field + " (changed since we wrote it)")
            return False
        if current and prior == "user":
            note["kept"].append(field)
            return False
        note["wrote"].append(field)
        return True

    title, t = _first(tags, "XMP-dc:Title", "IFD0:XPTitle"), e.get("title")
    same = bool(t) and _first(tags, "XMP-dc:Title") == t and _first(tags, "IFD0:XPTitle") == t
    if keep_or_write("title", title, t, "XMP-omp:WrittenTitle", same):
        new |= {"XMP-dc:Title": e["title"], "IFD0:XPTitle": e["title"], "XMP-omp:WrittenTitle": e["title"]}
    desc, d = _first(tags, "XMP-dc:Description", "IFD0:ImageDescription", "IPTC:Caption-Abstract"), e.get("description")
    if JUNK.match(desc):
        desc = ""
    same = (bool(d) and _first(tags, "XMP-dc:Description") == d and _first(tags, "IFD0:ImageDescription") == d
            and (ext not in IPTC or _first(tags, "IPTC:Caption-Abstract") == d[:2000]))
    if keep_or_write("description", desc, d, "XMP-omp:WrittenDescription", same):
        new |= {"XMP-dc:Description": e["description"], "IFD0:ImageDescription": e["description"],
                "XMP-omp:WrittenDescription": e["description"]}
        if ext in IPTC:
            new["IPTC:Caption-Abstract"] = e["description"][:2000]
    elif not desc and JUNK.match(_first(tags, "IFD0:ImageDescription") or "x"):
        extra.append("-IFD0:ImageDescription=")  # camera junk, nothing instead: cleared when the file is written

    # keywords: what's there, less v1's (all of them) or ours from before, plus ours now
    flat = _uniq(_list(tags.get("XMP-dc:Subject")) + _list(tags.get("IPTC:Keywords")) + _list(tags.get("IFD0:XPKeywords"), ";"))
    hier = _uniq([h.replace("|", "/") for h in _list(tags.get("XMP-lr:HierarchicalSubject"))] + _list(tags.get("XMP-digiKam:TagsList")))
    was_f = was_h = set()
    if prior == "v1":
        flat, hier = [], []
    elif prior == "v2":
        was_f = {x.lower() for x in _list(tags.get("XMP-omp:WrittenKeywords"))}
        was_h = {x.lower() for x in _list(tags.get("XMP-omp:WrittenHierarchical"))}
        flat, hier = [x for x in flat if x.lower() not in was_f], [x for x in hier if x.lower() not in was_h]
    names = [p["name"] for p in e.get("people", [])] + [p["name"] for p in e.get("pets", [])]
    ours_f = _uniq([pl for _, pl in e.get("tags", [])] + names)
    ours_h = _uniq([path for path, _ in e.get("tags", [])] + [f"People/{p['name']}" for p in e.get("people", [])]
                   + [f"Pets/{p['name']}" for p in e.get("pets", [])])
    kept_user = bool(flat or hier) and prior == "user"
    dropped = prior == "v2" and bool(was_f or was_h)  # ours from before, to take out
    if ours_f or ours_h or dropped:
        flat, hier = _uniq(flat + ours_f), _uniq(hier + ours_h)
        kw = {"XMP-dc:Subject": flat, "XMP-lr:HierarchicalSubject": [h.replace("/", "|") for h in hier],
              "XMP-digiKam:TagsList": hier}
        if ext in IPTC:
            kw["IPTC:Keywords"] = [k.encode("utf-8")[:64].decode("utf-8", "ignore") for k in flat]
        if any(_list(tags.get(k)) != v for k, v in kw.items()):  # not there already
            new |= kw | {"XMP-omp:WrittenKeywords": ours_f, "XMP-omp:WrittenHierarchical": ours_h}
            if ext in IPTC:
                new["IPTC:CodedCharacterSet"] = "UTF8"
            note["wrote"].append(f"{len(ours_f)} keywords")
    if kept_user:
        note["kept"].append("keywords")

    # regions: ours, unless the file has regions that aren't
    regions = [{"Area": p["area"], "Name": p["name"], "Type": "Face"} for p in e.get("people", [])]
    regions += [{"Area": p["area"], "Name": p["name"], "Type": "Pet"} for p in e.get("pets", [])]
    theirs = tags.get("XMP-mwg-rs:RegionInfo") and not (prior == "v2" and tags.get("XMP-omp:WrittenRegions"))
    have = (tags.get("XMP-mwg-rs:RegionInfo") or {}).get("RegionList") or []
    if theirs:
        if regions:
            note["kept"].append("regions")
    elif regions and [(r.get("Name"), r.get("Type"), {k: round(float(v), 4) if k != "Unit" else v for k, v in (r.get("Area") or {}).items()})
                      for r in have] == [(r["Name"], r["Type"], r["Area"]) for r in regions]:
        pass  # there already
    elif regions:
        w, h = tags.get("File:ImageWidth") or tags.get("ExifIFD:ExifImageWidth"), tags.get("File:ImageHeight")
        w = w or next((v for k, v in tags.items() if k.endswith(":ImageWidth") and isinstance(v, int)), None)
        h = h or next((v for k, v in tags.items() if k.endswith(":ImageHeight") and isinstance(v, int)), None)
        info = {"RegionList": regions}
        if w and h:
            info["AppliedToDimensions"] = {"W": int(w), "H": int(h), "Unit": "pixel"}
        new |= {"XMP-mwg-rs:RegionInfo": info, "XMP-omp:WrittenRegions": len(regions)}
        note["wrote"].append(f"{len(regions)} regions")
    elif prior == "v2" and tags.get("XMP-omp:WrittenRegions"):
        extra.append("-XMP-mwg-rs:RegionInfo=")  # ours from before, nobody to name now
        new["XMP-omp:WrittenRegions"] = 0
        note["wrote"].append("regions removed")

    # the turn: to the Orientation the plan gives, from the one the run saw
    if e.get("target"):
        now = _orientation(tags)
        if now == e.get("orientation", 1) and now != e["target"]:
            extra.append(f"-IFD0:Orientation#={e['target']}")
            note["wrote"].append("turn")
            note["orientation"] = [now, e["target"]]
        elif now != e["target"]:
            note["kept"].append("orientation (changed since the run)")

    if note["wrote"]:
        new |= {"XMP-xmp:CreatorTool": TOOL, "XMP-omp:Version": VERSION}
        if run:
            new["XMP-omp:Run"] = run
    return new, extra, note


def write(et: ExifTool, src: Path, dst: Path, e: dict, run: str = "") -> dict:
    """Entry `e` written into a new file `dst`, made from `src`; `dst` is checked against `src` before this
    returns. Raises (leaving no `dst`) when the write or the check fails. Returns the note of what was written
    ({"wrote": [...], "kept": [...]}); nothing to write: {"wrote": []} and no `dst`."""
    if Path(src).suffix.lower() not in WRITABLE:
        return {"wrote": [], "kept": [], "skipped": "not a format we write"}
    before = et.read(src)
    if (e.get("people") or e.get("pets")) and _orientation(before) not in (e.get("orientation", 1), e.get("target")):
        e = e | {"people": [], "pets": []}  # the boxes were turned back for another orientation
    new, extra, note = changes(before, e, run)
    if not note["wrote"]:
        return note
    dst = Path(dst)
    dst.unlink(missing_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump([new], f, ensure_ascii=False)
    try:
        out, err = et.run("-m", "-q", "-q", f"-j={f.name}", *extra, "-o", str(dst), str(src))
    finally:
        os.unlink(f.name)
    if not dst.is_file():
        raise RuntimeError(f"exiftool wrote nothing: {err.strip() or out.strip()}")
    try:
        after = et.read(dst)
        problems = check(before, after, new, note)
        if problems:
            raise RuntimeError("; ".join(problems))
    except Exception:
        dst.unlink(missing_ok=True)
        raise
    st = os.stat(src)
    os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))  # the file's own dates stay (exiftool -o makes it new)
    return note


def check(before: dict, after: dict, new: dict, note: dict) -> list[str]:
    """Why the written file isn't right: the pixels, a motion photo's video, or a field not as written."""
    problems = []
    hb, ha = _by_name(before, "ImageDataHash"), _by_name(after, "ImageDataHash")
    if hb and hb != ha:
        problems.append("the image data changed")
    if _embedded(before) != _embedded(after):
        problems.append(f"what it carries inside changed ({_embedded(before)} -> {_embedded(after)})")
    for k in ("XMP-dc:Title", "XMP-dc:Description"):
        if k in new and str(after.get(k, "")) != new[k]:
            problems.append(f"{k} not as written")
    if "XMP-dc:Subject" in new and [x.lower() for x in _list(after.get("XMP-dc:Subject"))] != [x.lower() for x in new["XMP-dc:Subject"]]:
        problems.append("XMP-dc:Subject not as written")
    if "XMP-mwg-rs:RegionInfo" in new:
        got = (after.get("XMP-mwg-rs:RegionInfo") or {}).get("RegionList") or []
        if [r.get("Name") for r in got] != [r["Name"] for r in new["XMP-mwg-rs:RegionInfo"]["RegionList"]]:
            problems.append("the regions not as written")
    if note.get("orientation") and _orientation(after) != note["orientation"][1]:
        problems.append("the orientation not as written")
    return problems
