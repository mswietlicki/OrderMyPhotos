"""S6: the review bundle, a static folder at the root of the library (design.md S6, decisions.md P21).

  <root>/OrderMyPhotos/
    index.html             every album: ready or still processing, labelled, trusted
    albums/<album>.html    one review page per album that's ready
    t/<id>.jpg             512 px thumbnails, the only copies of the photos
    p/<id>.jpg             1536 px previews, only for formats a browser can't show
    library.json, README.txt
The open view shows the original through its relative path. Nothing is loaded from the internet, so the
folder works on any computer the storage is connected to. Labels live in the browser (localStorage, per
run and album) until they're exported; `omp labels --file …` imports them, and imported labels seed the
pages of later runs.
"""

import hashlib
import html
import json
import re
from zoneinfo import ZoneInfo
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from PIL import Image

from . import __version__
from .config import Config

THUMB_PX = 512
BROWSER_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"}
README = """OrderMyPhotos review
====================

Open index.html in a browser (Chrome or Edge recommended). Nothing is installed and nothing is loaded
from the internet: the pages, thumbnails and decisions are all in this folder, and the photos are shown
from their own folders next to it.

For each album you can review the proposals (keep / archive / bad, fix the bursts) or trust the
automatic picks. Export the decisions and put the file in decisions/ (or give it to whoever runs the
pipeline). Nothing is moved or deleted by opening these pages.
"""


def album_slug(album: str, taken: set[str]) -> str:
    name = re.sub(r'[\\/:*?"<>|#%]+', "_", PurePosixPath(album).name or "root").strip(" .") or "album"
    if name in taken:
        name = f"{name} ({hashlib.sha1(album.encode()).hexdigest()[:6]})"
    taken.add(name)
    return name


def user_labels(con) -> dict[str, dict]:
    """The latest imported label of each kind per photo, keyed by (album, file name)."""
    names = {}
    for r in con.execute("SELECT id, path, album FROM file ORDER BY path"):
        names.setdefault(r["id"], (r["album"], PurePosixPath(r["path"]).name))
    out = defaultdict(dict)
    for r in con.execute("SELECT id, kind, value FROM label ORDER BY lid"):
        if r["id"] in names:
            out[names[r["id"]]][r["kind"]] = r["value"]
    for v in out.values():
        v["confirmed"] = True
        v.setdefault("group", "")
    return dict(out)


def thumb(src: Path, dst: Path):
    if dst.exists():
        return
    img = Image.open(src)
    img.thumbnail((THUMB_PX, THUMB_PX), Image.Resampling.LANCZOS)
    img.save(dst, "JPEG", quality=82)


def run(cfg: Config, con, run_id: str, out: str | None):
    bundle = Path(out) if out else cfg.bundle
    for sub in ("albums", "t", "p", "decisions"):
        (bundle / sub).mkdir(parents=True, exist_ok=True)

    files = {}
    for r in con.execute("SELECT id, path, v1_archived, album FROM file WHERE media='photo' ORDER BY path"):
        files.setdefault(r["id"], dict(r))
    album_files = defaultdict(set)
    for r in con.execute("SELECT id, album FROM file WHERE media='photo' AND policy IN ('cull', 'describe')"):
        album_files[r["album"]].add(r["id"])  # unsorted folders are reviewed as proposals (S11), not as one page
    photos = {r["id"]: dict(r) for r in con.execute("SELECT id, taken_local, taken_ts FROM photo")}
    dec = {r["id"]: dict(r) for r in con.execute("SELECT * FROM decision WHERE run=?", (run_id,))}
    caps = {r["id"]: json.loads(r["data"]) for r in con.execute("SELECT id, data FROM caption")}
    try:
        ocr = {r["id"]: dict(r) for r in con.execute("SELECT * FROM text")}
    except Exception:  # noqa: BLE001 - no text stage yet
        ocr = {}
    groups = {}
    for g in con.execute("SELECT gid, kind, cohesion, data FROM grp WHERE run=?", (run_id,)):
        groups[g["gid"]] = {"kind": g["kind"], "cohesion": g["cohesion"], **json.loads(g["data"] or "{}"),
                            "members": [m[0] for m in con.execute("SELECT id FROM grp_member WHERE gid=? ORDER BY ord",
                                                                  (g["gid"],))]}
    event_of = {i: g["index"] for g in groups.values() if g["kind"] == "event" for i in g["members"]}
    v1 = {}
    for r in con.execute("SELECT album, first_file, kept, archived FROM v1_burst"):
        for f in json.loads(r["kept"]):
            v1[(r["album"], f)] = "kept"
        for f in json.loads(r["archived"]):
            v1[(r["album"], f)] = "archived"
    seeded = user_labels(con)
    timings = [dict(r) for r in con.execute("SELECT stage, items, round(seconds, 1) AS s, device FROM timing"
                                            " WHERE rowid IN (SELECT MAX(rowid) FROM timing GROUP BY stage) ORDER BY rowid")]

    def item(id_):
        f, d, p = files[id_], dec[id_], photos[id_]
        name = PurePosixPath(f["path"]).name
        thumb(cfg.preview(id_), bundle / "t" / f"{id_}.jpg")
        if PurePosixPath(name).suffix.lower() in BROWSER_EXT:
            src = "../../" + quote(f["path"])  # the original, next to the bundle
        else:
            prev = bundle / "p" / f"{id_}.jpg"
            if not prev.exists():
                prev.write_bytes(cfg.preview(id_).read_bytes())
            src = f"../p/{id_}.jpg"
        feats = json.loads(d["features"])
        c = caps.get(id_)
        tx = ocr.get(id_)
        if tx and tx["mostly_text"] and tx["text"]:
            # The text file that goes next to the photo when the plan is applied; the bundle keeps a copy.
            out = bundle / "text" / (f["path"].rsplit(".", 1)[0] + ".txt")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(tx["text"].rstrip() + "\n", encoding="utf-8")
            if tx.get("blocks"):  # the layout, with each block's position on the page
                out.with_suffix(".ocr.json").write_text(tx["blocks"], encoding="utf-8")
        return {
            "id": id_, "name": name, "src": src, "time": p["taken_local"][11:19], "date": p["taken_local"][:10],
            "ts": p["taken_ts"], "event": event_of.get(id_, 0), "group": f"B{d['gid']}" if d["gid"] is not None else "",
            "verdict": d["verdict"], "tier": d["tier"], "rank": d["rank"], "score": round(d["score"], 3),
            "reasons": json.loads(d["reasons"]), "floor": json.loads(d["floor"]) if d["floor"] else None,
            "protected": d["protected"],
            "f": {k: feats.get(k) for k in ("sharp", "rel_sharp", "quality", "aesthetics", "motion", "faces", "smile", "purpose",
                                           "memory_p", "bt", "p_keep", "clip_hi", "maybe", "p_extra", "rotate", "place",
                                           "text_page", "docset", "ocr_needed")},
            "ocr": tx["text"] if tx and tx["mostly_text"] and tx["text"] else None,
            "v1": v1.get((f["album"], name)), "v1_archived": bool(f["v1_archived"]),
            "cap": None if not c else {k: c.get(k) for k in ("title_pl", "title_en", "description_pl", "description_en",
                                                             "text_in_image", "purpose", "category", "names")}
                   | {"tags": [t["path"] for t in c.get("v2_tags", [])]},
        }

    template = (Path(__file__).parent / "report.html").read_text(encoding="utf-8")
    # S11 proposals for unsorted photos. Those that join an album with a page of its own are reviewed on that
    # page, with its bursts (P33); the rest get a review unit each, below.
    try:
        props = [dict(r) for r in con.execute("SELECT * FROM proposal WHERE run=? ORDER BY start", (run_id,))]
    except Exception:  # noqa: BLE001 - no albums stage yet
        props = []
    tz = ZoneInfo(cfg.tz)
    placed = {r[0]: r[1] for r in con.execute("SELECT id, value FROM signal WHERE kind='placed_at'")}
    from .albums import NOT_THEMES, THEME_ROOT
    from .library import library_for

    lib = library_for(cfg, con)
    known_albums = set(lib.albums) if lib else set()
    known_folders = ({m.group() for f in lib.folders if (m := THEME_ROOT.match(f))} if lib else set()) - NOT_THEMES

    def base_of(target):
        return (target or "").removesuffix(" PRIV").removesuffix("/Dokumenty")
    joining, joined_pids = defaultdict(dict), set()
    for pr in props:
        base = (pr["target"] or "").removesuffix(" PRIV").removesuffix("/Dokumenty")
        if pr["action"] == "add" and base in album_files:
            pdata = json.loads(pr["data"] or "{}")
            for (i,) in con.execute("SELECT id FROM proposal_member WHERE pid=?", (pr["pid"],)):
                if i in dec:
                    joining[base][i] = {"from": str(PurePosixPath(files[i]["path"]).parent), "why": pdata.get("why"),
                                        "target": pr["target"], "unsure": (pdata.get("unsure") or {}).get(i)}
            joined_pids.add(pr["pid"])
    nude = {r[0] for r in con.execute("SELECT id FROM signal WHERE kind='nudity' AND value >= 1")}  # never captioned
    taken, rows = set(), []
    for album in sorted(album_files):
        ids = album_files[album] | set(joining[album])
        decided = [i for i in ids if i in dec]
        uncaptioned = [i for i in decided if dec[i]["verdict"] == "keep" and i not in caps and i not in nude]
        ready = len(decided) == len(ids) and not uncaptioned
        slug = album_slug(album, taken)
        row = {"album": album, "slug": slug, "photos": len(ids), "ready": ready, "done": len(decided) - len(uncaptioned)}
        if ready:
            items = []
            for i in ids:
                x = item(i)
                if i in joining[album]:  # named by its path: it lives in another folder, and names may repeat
                    x |= {"name": files[i]["path"], "joins": joining[album][i]}
                    if i in placed:  # sent later: shown at the moment of the album photo it looks most like
                        at = datetime.fromtimestamp(placed[i], tz)
                        x["joins"]["sent"] = x["date"]
                        x |= {"ts": placed[i], "date": at.strftime("%Y-%m-%d"), "time": at.strftime("%H:%M:%S")}
                items.append(x)
            items.sort(key=lambda x: (x["ts"], x["name"]))
            gids = {x["group"] for x in items if x["group"]}
            bursts = {f"B{gid}": {"cohesion": g["cohesion"], "span": g.get("span_s"), "min": g.get("min_margin")}
                      for gid, g in groups.items() if g["kind"] == "burst" and f"B{gid}" in gids}
            summary = con.execute("SELECT text FROM album_context WHERE album=? AND source LIKE 'v2 summary%'",
                                  (album,)).fetchone()
            data = {"run": run_id, "album": album, "base": "../", "generated": datetime.now().isoformat(timespec="seconds"),
                    "items": items, "bursts": bursts, "summary": json.loads(summary[0]) if summary else None,
                    "timings": timings,
                    "user_labels": {x["name"]: seeded[(album, x["name"])] for x in items if (album, x["name"]) in seeded}
                    | {x["name"]: seeded[k] for x in items if x.get("joins")
                       and (k := (files[x["id"]]["album"], PurePosixPath(x["name"]).name)) in seeded}}
            ulab = data["user_labels"]
            for x in items:  # a joining photo shows in the burst the reviewer already labelled, under their name for it
                if x.get("joins") and x["group"]:
                    theirs = Counter(ulab[y["name"]]["group"] for y in items if y["group"] == x["group"]
                                     and ulab.get(y["name"], {}).get("group"))
                    if theirs:
                        x["group"] = theirs.most_common(1)[0][0]
            (bundle / "albums" / f"{slug}.html").write_text(
                template.replace("/*DATA*/null", json.dumps(data, ensure_ascii=False).replace("</", "<\\/")),
                encoding="utf-8")
            row |= {"bursts": len(bursts), "keep": sum(1 for x in items if x["verdict"] == "keep"),
                    "review": sum(1 for x in items if x["tier"] == "review"), "start": items[0]["date"],
                    "joining": len(joining[album]), "unsure": sum(1 for j in joining[album].values() if j.get("unsure")),
                    "cover": next((x["id"] for x in items if x["verdict"] == "keep"), items[0]["id"]),
                    "proposals": [[x["name"], x["verdict"], x["group"]] for x in items], "seeded": data["user_labels"]}
        rows.append(row)

    # S11's other proposals, one page per album or folder that receives photos (P35): its incoming photos,
    # each marked with where it comes from. A new album's page lets the reviewer accept or rename it.
    by_target = defaultdict(list)
    for pr in props:
        if pr["pid"] in joined_pids or pr["action"] == "relocate":
            continue
        kind = {"copy": "copies", "create": "new"}.get(pr["action"], "into")
        by_target[(kind, pr["target"] if kind == "new" else base_of(pr["target"]))].append(pr)
    for (kind, target), prs in sorted(by_target.items(), key=lambda kv: min(pr["start"] or 0 for pr in kv[1])):
        members = {}
        for pr in prs:
            pdata = json.loads(pr["data"] or "{}")
            for (i,) in con.execute("SELECT id FROM proposal_member WHERE pid=?", (pr["pid"],)):
                if i in dec:
                    members[i] = {"from": str(PurePosixPath(files[i]["path"]).parent),
                                  "why": (pdata.get("reasons") or {}).get(i) or pdata.get("why"), "target": pr["target"],
                                  "unsure": (pdata.get("unsure") or {}).get(i)}
        if not members:
            continue
        pr0, pdata = prs[0], json.loads(prs[0]["data"] or "{}")
        day = datetime.fromtimestamp(min(pr["start"] for pr in prs), tz).strftime("%Y-%m-%d")
        name = PurePosixPath(target).name
        exists = target in album_files or target in known_albums
        title = {"copies": f"Copies of photos in {name}", "new": name, "into": name}[kind]
        key = {"copies": f"copies:{target}", "new": f"new:{target}", "into": target}[kind]
        slug = album_slug(f"{kind} {name}", taken)
        items = []
        for i in members:
            x = item(i)
            x |= {"name": files[i]["path"], "joins": members[i]}  # named by its path: photos from many folders
            items.append(x)
        items.sort(key=lambda x: (x["ts"], x["name"]))
        gids = {x["group"] for x in items if x["group"]}
        bursts = {f"B{gid}": {"cohesion": g["cohesion"], "span": g.get("span_s"), "min": g.get("min_margin")}
                  for gid, g in groups.items() if g["kind"] == "burst" and f"B{gid}" in gids}
        user = {x["name"]: seeded[k] for x in items if (k := (files[x["id"]]["album"], PurePosixPath(x["name"]).name)) in seeded}
        sources = Counter(m["from"] for m in members.values())
        subtitle = {"new": f"new album · {day}", "copies": "copies to archive",
                    "into": ("existing album" if exists else "new folder" if pdata.get("new_folder") else "folder")
                    + " · " + ", ".join(f"{n} from {PurePosixPath(s).name}" for s, n in sources.most_common(3))}[kind]
        data = {"run": run_id, "album": key, "base": "../", "generated": datetime.now().isoformat(timespec="seconds"),
                "items": items, "bursts": bursts, "summary": None, "timings": timings, "user_labels": user,
                "proposal": {"action": kind, "target": target, "name": pr0["name"], "title": title,
                             "why": pdata.get("why"), "alternatives": pdata.get("alternatives", []),
                             "days": pdata.get("days", []), "places": pdata.get("places", []), "away": pdata.get("away"),
                             "exists": exists, "new_folder": bool(pdata.get("new_folder")),
                             "sources": [[s, n] for s, n in sources.most_common()]}}
        (bundle / "albums" / f"{slug}.html").write_text(
            template.replace("/*DATA*/null", json.dumps(data, ensure_ascii=False).replace("</", "<\\/")), encoding="utf-8")
        rows.append({"album": key, "slug": slug, "photos": len(items), "ready": True, "kind": "proposal",
                     "action": kind, "title": title, "subtitle": subtitle,
                     "unsure": sum(1 for m in members.values() if m.get("unsure")),
                     "bursts": len(bursts), "keep": sum(1 for x in items if x["verdict"] == "keep"),
                     "review": sum(1 for x in items if x["tier"] == "review"), "start": day,
                     "cover": next((x["id"] for x in items if x["verdict"] == "keep"), items[0]["id"]),
                     "proposals": [[x["name"], x["verdict"], x["group"]] for x in items], "seeded": user})
    # Every album and folder a photo can be moved to with `m`: the library's, and the new ones proposed here.
    targets = {a: "album" for a in known_albums} | {f: "folder" for f in known_folders}
    targets |= {r["album"][4:]: "new" for r in rows if r.get("action") == "new"}
    (bundle / "albums" / "targets.js").write_text(
        "window.OMP_TARGETS = " + json.dumps(sorted([p, k] for p, k in targets.items()), ensure_ascii=False) + ";\n",
        encoding="utf-8")
    # Two folders with one name ("Dom" in Other and in Info) get their parent in the title.
    seen = Counter(r["title"] for r in rows if r.get("action") == "into")
    for r in rows:
        if r.get("action") == "into" and seen[r["title"]] > 1:
            r["title"] = f"{r['title']} ({PurePosixPath(r['album']).parent.name})"
    # Pages of earlier runs that this run didn't write again are gone from the index: remove them.
    written = {r["slug"] + ".html" for r in rows if r["ready"]}
    for old in (bundle / "albums").glob("*.html"):
        if old.name not in written:
            old.unlink()
    moves = [(json.loads(pr["data"] or "{}"), pr["target"]) for pr in props if pr["action"] == "relocate"]
    (bundle / "index.html").write_text(index_page(run_id, rows, moves), encoding="utf-8")
    # The pages in the index's order, for Shift+arrows on a page (albums first, then those receiving photos).
    pages = [r["slug"] + ".html" for kind in (False, True) for r in rows
             if r["ready"] and (r.get("kind") == "proposal") == kind]
    (bundle / "albums" / "nav.js").write_text(f"window.OMP_PAGES = {json.dumps(pages, ensure_ascii=False)};\n",
                                              encoding="utf-8")
    (bundle / "README.txt").write_text(README, encoding="utf-8")
    lib = bundle / "library.json"
    meta = json.loads(lib.read_text(encoding="utf-8")) if lib.exists() else {
        "id": hashlib.sha1(f"{cfg.root}{datetime.now().isoformat()}".encode()).hexdigest()[:16],
        "created": datetime.now().isoformat(timespec="seconds")}
    meta |= {"run": run_id, "updated": datetime.now().isoformat(timespec="seconds"), "omp": __version__}
    lib.write_text(json.dumps(meta, indent=1), encoding="utf-8")
    ready = [r for r in rows if r["ready"]]
    print(f"report: {len(ready)} of {len(rows)} albums ready, {sum(r['photos'] for r in ready)} photos -> {bundle}/index.html")


def index_page(run_id: str, rows: list[dict], moves: list | None = None) -> str:
    moves_html = ""
    if moves:
        items = "".join(f"<li>{html.escape(d.get('source', ''))} → <b>{html.escape(t)}</b> · {d.get('n', 0)} photos"
                        f"{' · merges into the existing album' if d.get('merge') else ''}</li>" for d, t in moves)
        moves_html = (f"<h2>Album folders outside Pictures/Albums/&lt;year&gt;/ ({len(moves)})</h2>"
                      f"<p class='k'>They move where they belong in the production run (after review).</p><ul>{items}</ul>")
    def card(r):
        name, parent = html.escape(PurePosixPath(r["album"]).name), html.escape(str(PurePosixPath(r["album"]).parent))
        if not r["ready"]:
            return (f'<div class="al wait"><div class="ph">processing</div><div><b>{name}</b><span>{parent}</span>'
                    f'<span>{r["done"]} of {r["photos"]} photos done</span></div></div>')
        key = html.escape("omp-labels-" + run_id + "::" + r["album"])
        if r.get("kind") == "proposal":
            name, parent = html.escape(r["title"]), html.escape(r["subtitle"])
        return (f'<div class="al" data-key="{key}" data-n="{r["photos"]}" data-album="{html.escape(r["album"])}">'
                f'<a href="albums/{quote(r["slug"])}.html"><img src="t/{r["cover"]}.jpg" alt="" loading="lazy"></a>'
                f'<div><a href="albums/{quote(r["slug"])}.html"><b>{name}</b></a><span>{parent}</span>'
                f'<span>{r["photos"]} photos · {r["bursts"]} bursts · keep {r["keep"]} · {r["review"]} to review'
                f'{" · " + str(r["joining"]) + " joining from unsorted folders" if r.get("joining") else ""}'
                f'{" · <b>" + str(r["unsure"]) + " unsure</b>" if r.get("unsure") else ""}</span>'
                f'<span class="lab"></span><button class="trust" title="accept every proposal of this album as it is">'
                f'Trust automatic picks</button></div></div>')
    proposals = {r["album"]: r["proposals"] for r in rows if r["ready"]}
    seeded = {r["album"]: r["seeded"] for r in rows if r["ready"] and r["seeded"]}
    ready = sum(1 for r in rows if r["ready"])
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>OrderMyPhotos albums</title>
<style>
:root {{ --bg: #f6f6f4; --panel: #fff; --ink: #1d1d1b; --muted: #6b6b66; --line: #deded8; --keep: #1a7a42; --accent: #2f5fd0; --chip: #efefea; --on-fill: #fff; color-scheme: light; }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{ --bg: #141413; --panel: #1e1e1c; --ink: #ecece8; --muted: #a3a39c; --line: #34342f; --keep: #3fbf74; --accent: #7aa2ff; --chip: #2a2a27; --on-fill: #121211; color-scheme: dark; }} }}
:root[data-theme="dark"] {{ --bg: #141413; --panel: #1e1e1c; --ink: #ecece8; --muted: #a3a39c; --line: #34342f; --keep: #3fbf74; --accent: #7aa2ff; --chip: #2a2a27; --on-fill: #121211; color-scheme: dark; }}
body {{ margin: 0; background: var(--bg); color: var(--ink); font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }}
main {{ max-width: 1400px; margin: 0 auto; padding: 16px; }}
header {{ display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: center; margin: 4px 0 14px; }}
h1 {{ font-size: 18px; margin: 0; }} h1 small {{ color: var(--muted); font-weight: 400; }}
.sp {{ flex: 1; }}
button {{ font: inherit; color: inherit; background: var(--chip); border: 1px solid var(--line); border-radius: 6px; padding: 3px 10px; cursor: pointer; }}
button.primary {{ background: var(--accent); border-color: var(--accent); color: var(--on-fill); }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px; }}
.al {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }}
.al a {{ color: inherit; text-decoration: none; }}
.al img, .al .ph {{ display: block; width: 100%; aspect-ratio: 4 / 3; object-fit: cover; background: #000; }}
.al .ph {{ background: var(--chip); color: var(--muted); display: flex; align-items: center; justify-content: center; }}
.al > div {{ padding: 8px 10px; display: flex; flex-direction: column; gap: 3px; align-items: flex-start; }}
.al span {{ color: var(--muted); font-size: 12px; }}
.al .lab.done {{ color: var(--keep); }}
.al.wait {{ opacity: .7; }}
h2 {{ font-size: 15px; margin: 24px 0 10px; }}
</style></head><body><main>
<header><h1>Review · run {html.escape(run_id)} <small>{ready} of {len(rows)} albums ready</small></h1><span class="sp"></span>
<a href="people.html" id="people">People: name the faces</a>
<button class="primary" id="exp">Export all decisions</button></header>
<div class="grid">{"".join(card(r) for r in rows if r.get("kind") != "proposal")}</div>
{'<h2>Albums and folders receiving photos</h2><p class="k">Each one shows the photos that would move into it, marked with the folder they come from.</p><div class="grid">' + "".join(card(r) for r in rows if r.get("kind") == "proposal") + "</div>" if any(r.get("kind") == "proposal" for r in rows) else ""}
{moves_html}</main>
<script>
const RUN = {json.dumps(run_id)};
const PROPOSALS = {json.dumps(proposals, ensure_ascii=False).replace("</", "<\\/")};
const SEEDED = {json.dumps(seeded, ensure_ascii=False).replace("</", "<\\/")};  // labels already imported into the DB
const load = k => {{ try {{ return JSON.parse(localStorage.getItem(k) || "null"); }} catch (e) {{ return null; }} }};
const labelsOf = a => load(a.dataset.key) || SEEDED[a.dataset.album] || {{}};
function refresh() {{
  document.querySelectorAll(".al[data-key]").forEach(a => {{
    const n = Object.entries(labelsOf(a)).filter(([k, l]) => k !== "__proposal__" && l.confirmed).length;
    const trusted = load(a.dataset.key + "::trusted");
    const el = a.querySelector(".lab");
    el.textContent = trusted ? "trusted: automatic picks accepted" : n ? `labelled ${{n}} / ${{a.dataset.n}}` : "not labelled yet";
    el.classList.toggle("done", !!trusted || n >= +a.dataset.n);
  }});
}}
document.querySelectorAll(".trust").forEach(b => b.onclick = () => {{
  const a = b.closest(".al"), labels = Object.assign({{}}, labelsOf(a));
  for (const [name, verdict, group] of PROPOSALS[a.dataset.album])
    if (!(labels[name] && labels[name].confirmed)) labels[name] = {{verdict, group, confirmed: true, trusted: true}};
  try {{ localStorage.setItem(a.dataset.key, JSON.stringify(labels)); localStorage.setItem(a.dataset.key + "::trusted", "true"); }} catch (e) {{}}
  refresh();
}});
document.getElementById("exp").onclick = () => {{
  const albums = {{}};
  document.querySelectorAll(".al[data-key]").forEach(a => {{
    const labels = labelsOf(a);
    if (Object.keys(labels).length) albums[a.dataset.album] = {{trusted: !!load(a.dataset.key + "::trusted"), labels}};
  }});
  const names = load("omp-people-" + RUN + "::names") || undefined;  // from the People page (P40)
  const blob = new Blob([JSON.stringify({{run: RUN, exported: new Date().toISOString(), albums, names}}, null, 1)], {{type: "application/json"}});
  const l = document.createElement("a"); l.href = URL.createObjectURL(blob); l.download = `decisions-${{RUN}}.json`; l.click();
}};
refresh();
</script></body></html>
"""


def import_labels(cfg: Config, con, path: str):
    """Accepts one album's export ({album, labels}) or the index's ({albums: {album: {labels}}})."""
    from .albums import page_target

    spec = json.loads(Path(path).read_text(encoding="utf-8"))
    if spec.get("names"):  # the People page's names (P40), per face or animal box
        from .people import SCHEMA as PEOPLE_SCHEMA
        con.executescript(PEOPLE_SCHEMA)
        con.executemany("INSERT OR REPLACE INTO name_label(key, who, name, nick, owner, source) VALUES (?,?,?,?,?,?)",
                        [(k, v.get("who") or "", v.get("name") or None, v.get("nick") or None, int(bool(v.get("owner"))),
                          f"user:{spec.get('run')}:{spec.get('exported')}") for k, v in spec["names"].items()])
        con.commit()
        print(f"labels: {len(spec['names'])} names of faces and animals"
              f" ({len({v.get('who') for v in spec['names'].values() if v.get('who')})} people and pets)")
    albums = spec.get("albums") or ({spec.get("album"): {"labels": spec["labels"]}} if "labels" in spec else {})
    by_name = {}
    for r in con.execute("SELECT id, path, album FROM file"):
        by_name[(r["album"], PurePosixPath(r["path"]).name)] = r["id"]
        by_name[(None, r["path"])] = r["id"]  # a joining photo is named by its path (P33)
        by_name.setdefault((None, PurePosixPath(r["path"]).name), r["id"])
    src = f"user:{spec.get('run')}:{spec.get('exported')}"
    n = 0
    con.execute("CREATE TABLE IF NOT EXISTS proposal_label (run TEXT, key TEXT, value TEXT, source TEXT,"
                " created TEXT NOT NULL DEFAULT (datetime('now')))")
    for album, entry in albums.items():
        if "__proposal__" in entry["labels"]:
            con.execute("INSERT INTO proposal_label(run, key, value, source) VALUES (?,?,?,?)",
                        (spec.get("run"), album, json.dumps(entry["labels"]["__proposal__"], ensure_ascii=False), src))
        for name, lab in entry["labels"].items():
            if name == "__proposal__":
                continue
            id_ = by_name.get((album, name)) or by_name.get((None, name))
            if not lab.get("confirmed") or not id_:
                continue
            kinds = ("group", "verdict", "category", "note", "rotate")
            for kind in kinds:
                if kind in lab and (lab[kind] not in (None, "") or kind == "group"):
                    con.execute("INSERT INTO label(id, kind, value, source) VALUES (?,?,?,?)",
                                (id_, kind, str(lab[kind]), src + (":trusted" if lab.get("trusted") else "")))
            if "out" in lab:  # taken out of this album or proposal ("" when undone): S11 routes it elsewhere
                con.execute("INSERT INTO label(id, kind, value, source) VALUES (?,?,?,?)",
                            (id_, "out", page_target(album) if lab["out"] else "", src))
            if "move" in lab:  # moved with `m`: an album or folder path, or "new:<name>" ("" when undone)
                con.execute("INSERT INTO label(id, kind, value, source) VALUES (?,?,?,?)",
                            (id_, "move", lab["move"] or "", src))
            n += 1
    con.commit()
    print(f"labels: {n} confirmed photos imported from {path}")
