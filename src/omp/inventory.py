"""S0 inventory + S1 exact copies: walk the library, hash, read metadata, pick up v1's traces."""

import json
import os
import re
import subprocess
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

from .config import PHOTO_EXT, VIDEO_EXT, Config, Policy
from .db import log_timing
from .qxh import quickxorhash_file

CONTEXT_FILES = {"context.txt": 1, "_trip-context.txt": 0, "DESCRIPTION.md": 0}  # name -> trusted

EXIF_TAGS = [
    "DateTimeOriginal", "SubSecTimeOriginal", "OffsetTimeOriginal", "CreateDate", "SubSecCreateDate",
    "Make", "Model", "SerialNumber", "LensModel", "ISO", "ExposureTime", "FNumber", "FocalLength",
    "FocalLengthIn35mmFormat", "Composite:GPSLatitude", "Composite:GPSLongitude", "GPSAltitude",
    "GPSDateTime", "Orientation", "ImageWidth", "ImageHeight", "Title", "Description", "Subject",
    "Keywords", "ImageDescription", "XPTitle", "XPComment", "XPKeywords", "Rating", "HierarchicalSubject",
    "RegionInfo", "EmbeddedVideoType", "MotionPhoto", "MicroVideo", "BurstID", "BurstUUID", "Software",
    "CreatorTool", "MIMEType",
]

NAME_PATTERNS = [
    (re.compile(r"(?<!\d)(20\d{2}|19\d{2})(\d{2})(\d{2})[_-](\d{2})(\d{2})(\d{2})"), "%Y%m%d%H%M%S"),
    (re.compile(r"(?<!\d)(20\d{2}|19\d{2})-(\d{2})-(\d{2}) (\d{2})\.(\d{2})\.(\d{2})"), "%Y%m%d%H%M%S"),
    (re.compile(r"IMG-(20\d{2})(\d{2})(\d{2})-WA"), "%Y%m%d"),
]

# Camera junk that isn't a description (Immich reads ImageDescription first).
JUNK_DESCRIPTIONS = re.compile(r"^\s*(OLYMPUS DIGITAL CAMERA|SONY DSC|DCIM.*|default|Description|\s*)\s*$", re.I)


def media_of(path: Path) -> str:
    ext = path.suffix.lower()
    return "photo" if ext in PHOTO_EXT else "video" if ext in VIDEO_EXT else "other"


def logical_album(rel: str) -> tuple[str, int]:
    """Album folder of a file, with v1's `_archived/` mapped back to the album it came from."""
    parts = PurePosixPath(rel).parts
    if "_archived" in parts:
        i = parts.index("_archived")
        return "/".join(parts[:i]), 1
    return "/".join(parts[:-1]), 0


def _hash_one(args):
    path, rel = args
    st = os.stat(path)
    return rel, quickxorhash_file(path).hex(), st.st_size, st.st_mtime


def walk(cfg: Config):
    media, context, v1 = [], [], []
    for dirpath, dirnames, filenames in os.walk(cfg.root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        if Path(dirpath) == cfg.root:  # the review bundle is ours, not photos
            dirnames[:] = [d for d in dirnames if d != cfg.bundle.name]
        for name in sorted(filenames):
            full = Path(dirpath) / name
            rel = full.relative_to(cfg.root).as_posix()
            if name in CONTEXT_FILES:
                context.append((full, rel))
            elif name.startswith("burst_") and name.endswith(".json") and "_archived" in full.parts:
                v1.append((full, rel))
            elif media_of(full) != "other":
                media.append((full, rel))
    return media, context, v1


def run_exiftool(paths: list[Path]) -> list[dict]:
    out = []
    for i in range(0, len(paths), 200):
        batch = paths[i : i + 200]
        cmd = ["exiftool", "-json", "-n", "-G1", "-charset", "filename=utf8", "-api", "largefilesupport=1"]
        cmd += [f"-{t}" for t in EXIF_TAGS] + [str(p) for p in batch]
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        if res.stdout.strip():
            out.extend(json.loads(res.stdout))
    return out


def pick(d: dict, name: str):
    for k, v in d.items():
        if k == name or k.endswith(":" + name):
            if v not in (None, "", "0000:00:00 00:00:00"):
                return v
    return None


def parse_exif_time(value: str | None, subsec, offset: str | None, tz: ZoneInfo):
    if not value or not isinstance(value, str):
        return None
    m = re.match(r"(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})", value)
    if not m:
        return None
    try:
        dt = datetime(*map(int, m.groups()))
    except ValueError:
        return None
    if subsec not in (None, ""):
        digits = re.sub(r"\D", "", str(subsec))
        if digits:
            dt += timedelta(seconds=float("0." + digits))
    if offset and re.match(r"^[+-]\d{2}:\d{2}$", offset):
        sign = 1 if offset[0] == "+" else -1
        hh, mm = map(int, offset[1:].split(":"))
        aware = dt.replace(tzinfo=timezone(sign * timedelta(hours=hh, minutes=mm)))
    else:
        aware = dt.replace(tzinfo=tz)
        offset = aware.strftime("%z")
        offset = offset[:3] + ":" + offset[3:]
    return dt, aware.timestamp(), offset


def time_from_name(name: str, tz: ZoneInfo):
    for rx, fmt in NAME_PATTERNS:
        m = rx.search(name)
        if m:
            try:
                dt = datetime.strptime("".join(m.groups()), fmt)
            except ValueError:
                continue
            aware = dt.replace(tzinfo=tz)
            off = aware.strftime("%z")
            return dt, aware.timestamp(), off[:3] + ":" + off[3:]
    return None


def photo_row(meta: dict, rel: str, mtime: float, tz: ZoneInfo) -> dict:
    name = PurePosixPath(rel).name
    t = parse_exif_time(pick(meta, "DateTimeOriginal"), pick(meta, "SubSecTimeOriginal"),
                        pick(meta, "OffsetTimeOriginal"), tz)
    source = "exif"
    if t is None:
        t = parse_exif_time(pick(meta, "CreateDate"), pick(meta, "SubSecCreateDate"), None, tz)
    if t is None:
        t, source = time_from_name(name, tz), "filename"
    if t is None:
        gps = pick(meta, "GPSDateTime")
        if isinstance(gps, str) and (m := re.match(r"(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})", gps)):
            utc = datetime(*map(int, m.groups()), tzinfo=timezone.utc)
            local = utc.astimezone(tz)
            off = local.strftime("%z")
            t, source = (local.replace(tzinfo=None), utc.timestamp(), off[:3] + ":" + off[3:]), "gps"
    if t is None:
        local = datetime.fromtimestamp(mtime, tz)
        off = local.strftime("%z")
        t, source = (local.replace(tzinfo=None), mtime, off[:3] + ":" + off[3:]), "mtime"
    dt, ts, offset = t

    orientation = pick(meta, "Orientation") or 1
    w, h = pick(meta, "ImageWidth"), pick(meta, "ImageHeight")
    if orientation in (5, 6, 7, 8) and w and h:
        w, h = h, w

    existing = {}
    for field_, names in {
        "title": ["XMP-dc:Title", "XPTitle"],
        "description": ["XMP-dc:Description", "ImageDescription", "XPComment"],
        "keywords": ["XMP-dc:Subject", "Keywords", "XPKeywords", "HierarchicalSubject"],
    }.items():
        for n in names:
            v = pick(meta, n)
            if v in (None, "") or (field_ == "description" and isinstance(v, str) and JUNK_DESCRIPTIONS.match(v)):
                continue
            existing[field_] = {"value": v, "from": n}
            break
    creator = pick(meta, "CreatorTool") or ""
    provenance = "v2" if "OrderMyPhotos" in str(creator) else "unknown"
    lat, lon = pick(meta, "GPSLatitude"), pick(meta, "GPSLongitude")
    if lat == 0 and lon == 0:
        lat = lon = None
    return {
        "taken_local": dt.isoformat(timespec="milliseconds"),
        "taken_ts": ts,
        "time_source": source,
        "utc_offset": offset,
        "make": pick(meta, "Make"),
        "model": pick(meta, "Model"),
        "lens": pick(meta, "LensModel"),
        "iso": pick(meta, "ISO"),
        "exposure": pick(meta, "ExposureTime"),
        "fnumber": pick(meta, "FNumber"),
        "focal": pick(meta, "FocalLength"),
        "focal35": pick(meta, "FocalLengthIn35mmFormat"),
        "lat": lat,
        "lon": lon,
        "alt": pick(meta, "GPSAltitude"),
        "width": w,
        "height": h,
        "orientation": orientation,
        "motion_photo": int(bool(pick(meta, "EmbeddedVideoType") or pick(meta, "MotionPhoto") or pick(meta, "MicroVideo"))),
        "meta": json.dumps({
            "existing": existing, "provenance": provenance,
            "burst": pick(meta, "BurstID") or pick(meta, "BurstUUID"),
            "serial": pick(meta, "SerialNumber"), "software": pick(meta, "Software"),
            "rating": pick(meta, "Rating"), "regions": bool(pick(meta, "RegionInfo")),
        }, default=str),
    }


def run(cfg: Config, con, run_id: str):
    started = time.perf_counter()
    policy = Policy(cfg.policy_file)
    tz = ZoneInfo(cfg.tz)
    media, context, v1 = walk(cfg)

    known = {r["path"]: (r["size"], r["mtime"], r["id"]) for r in con.execute("SELECT path, size, mtime, id FROM file")}
    todo = []
    for full, rel in media:
        st = full.stat()
        k = known.get(rel)
        if k and k[0] == st.st_size and abs(k[1] - st.st_mtime) < 1e-3:
            # Unchanged file; the folder policy may have changed, so it's applied again.
            con.execute("UPDATE file SET seen_run=?, policy=? WHERE path=?", (run_id, policy(rel), rel))
        else:
            todo.append((full, rel))
    with ProcessPoolExecutor(max_workers=cfg.workers) as pool:
        hashed = list(pool.map(_hash_one, todo, chunksize=4))
    for rel, id_, size, mtime in hashed:
        album, v1_arch = logical_album(rel)
        con.execute(
            "INSERT OR REPLACE INTO file(path, id, size, mtime, media, policy, album, v1_archived, seen_run)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (rel, id_, size, mtime, media_of(Path(rel)), policy(rel), album, v1_arch, run_id),
        )
    con.commit()
    t_hash = time.perf_counter() - started

    # Metadata, once per content hash (copies share it).
    have = {r[0] for r in con.execute("SELECT id FROM photo")}
    first = {}
    for r in con.execute("SELECT path, id, mtime FROM file WHERE media='photo' ORDER BY path"):
        if r["id"] not in have and r["id"] not in first:
            first[r["id"]] = (r["path"], r["mtime"])
    paths = [cfg.root / p for p, _ in first.values()]
    by_path = {}
    for m in run_exiftool(paths):
        rel = Path(m["SourceFile"]).relative_to(cfg.root).as_posix()
        by_path[rel] = m
    for id_, (rel, mtime) in first.items():
        row = photo_row(by_path.get(rel, {}), rel, mtime, tz)
        cols = ",".join(row)
        con.execute(f"INSERT OR REPLACE INTO photo(id,{cols}) VALUES (?{',?' * len(row)})", (id_, *row.values()))
    con.commit()

    # v1 traces: burst records and album context.
    for full, rel in v1:
        rec = json.loads(full.read_text(encoding="utf-8-sig"))
        album, _ = logical_album(rel)
        con.execute(
            "INSERT OR REPLACE INTO v1_burst(album, first_file, kept, archived, reasoning) VALUES (?,?,?,?,?)",
            (album, rec.get("first_file"), json.dumps([k["file"] for k in rec.get("kept", [])]),
             json.dumps([a["original"] for a in rec.get("archived", [])]), rec.get("vlm_reasoning")),
        )
    for full, rel in context:
        album = str(PurePosixPath(rel).parent)
        con.execute(
            "INSERT OR REPLACE INTO album_context(album, source, trusted, text) VALUES (?,?,?,?)",
            (album, full.name, CONTEXT_FILES[full.name], full.read_text(encoding="utf-8-sig", errors="replace")),
        )
    # Titles/descriptions/keywords in albums that v1's caption runs touched (a DESCRIPTION.md or
    # _trip-context.txt next to them) are machine-written: v2 may overwrite them (legacy.md).
    machine = {r[0] for r in con.execute("SELECT album FROM album_context WHERE trusted=0")}
    for r in con.execute("SELECT p.id, p.meta, MIN(f.album) AS album FROM photo p JOIN file f ON f.id=p.id GROUP BY p.id").fetchall():
        meta = json.loads(r["meta"])
        if meta["existing"] and meta["provenance"] == "unknown" and r["album"] in machine:
            meta["provenance"] = "v1"
            con.execute("UPDATE photo SET meta=? WHERE id=?", (json.dumps(meta, default=str), r["id"]))
    con.commit()

    elapsed = time.perf_counter() - started
    log_timing(con, "inventory.hash", run_id, len(todo), t_hash, f"cpu x{cfg.workers}")
    log_timing(con, "inventory", run_id, len(media), elapsed, f"cpu x{cfg.workers}")
    con.commit()
    copies = con.execute(
        "SELECT COUNT(*) FROM (SELECT id FROM file GROUP BY id HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    print(f"inventory: {len(media)} media files ({len(todo)} hashed), {len(first)} new photos read,"
          f" {len(v1)} v1 burst records, {len(context)} context files, {copies} exact-copy groups,"
          f" {elapsed:.1f} s")
