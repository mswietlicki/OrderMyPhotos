"""The library catalog (P58): what every session saw and what the reviewer decided, kept apart from the
sessions so that it outlives them.

The user: "Make sure to save full inventory with metadata for future use. We may want to run this on other disks
and storages and photos may duplicate; if something is archived during this process it should also always be
archived in future sessions."

- **Inventory:** after a session reads its photos, every file is saved with its library and path (`location`),
  and every distinct photo with its metadata and perceptual hashes (`media`). A photo is its QuickXorHash, the
  same on OneDrive, a drive or a share, so a copy anywhere is the same row.
- **Outcome:** when the reviewer confirms a session's final decision, where each photo ends up is saved
  (`outcome`): kept (at its path or where the plan moves it) or archived, and whether archived photos are kept or
  deleted.
- **Archived before:** a later session archives a photo whose latest confirmed outcome is "archived", without
  asking again (rank.py). A photo counts as archived when none of its copies in that session was kept: an exact
  copy archived next to a kept original is a duplicate, not a rejected photo.
- **Kept before**, in the same library: kept again (user: "yes, keep them"), out of the judge's reach. A copy of it
  saved again in the new session is still archived as a duplicate.
- **Kept in another library** (a copy of a library photo found on a drive or share): archived as a duplicate
  (user: "if those are duplicates of kept photos then those also should be archived as duplicates"), as long as the
  catalog still has the photo in that library. A photo archived as a duplicate counts as neither kept nor archived
  there, so it never makes the original look rejected.
- Test sets (copies in OMP_APPLY_ROOTS, P48) neither write nor read the catalog: they're experiments, and they
  must run like fresh sessions.

The catalog is `<state>/catalog.db` (`/srv/mimir/data/photos/catalog.db` on Mimir), or OMP_CATALOG; OMP_CATALOG=off
turns it off.
"""

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS session (
  session TEXT PRIMARY KEY,
  library TEXT NOT NULL,           -- 'onedrive', or the root of a drive or share
  root TEXT NOT NULL,              -- where the session read it from
  files INTEGER, photos INTEGER,
  inventory_at REAL, outcome_at REAL,
  archived TEXT                    -- the final decision: keep | delete the archived photos
);
CREATE TABLE IF NOT EXISTS location (   -- every file a session saw, where it was
  library TEXT NOT NULL,
  path TEXT NOT NULL,              -- relative to the library root
  id TEXT NOT NULL,                -- hex QuickXorHash
  size INTEGER, mtime REAL, media TEXT, policy TEXT, album TEXT,
  session TEXT NOT NULL, seen REAL NOT NULL,
  PRIMARY KEY (library, path)
);
CREATE INDEX IF NOT EXISTS location_id ON location(id);
CREATE TABLE IF NOT EXISTS media (      -- every distinct photo or video, by content
  id TEXT PRIMARY KEY,
  size INTEGER, media TEXT,
  taken_local TEXT, taken_ts REAL, time_source TEXT, utc_offset TEXT,
  make TEXT, model TEXT, lens TEXT,
  iso REAL, exposure REAL, fnumber REAL, focal REAL, focal35 REAL,
  lat REAL, lon REAL, alt REAL,
  width INTEGER, height INTEGER, orientation INTEGER, motion_photo INTEGER,
  meta TEXT,
  phash TEXT, dhash TEXT,          -- for finding resized or recompressed copies later
  first_seen REAL, session TEXT, updated REAL
);
CREATE TABLE IF NOT EXISTS outcome (    -- the confirmed plan of a session, per file
  session TEXT NOT NULL,
  path TEXT NOT NULL,
  id TEXT NOT NULL,
  library TEXT NOT NULL,
  verdict TEXT NOT NULL,           -- keep | archive | duplicate (archived because the same photo is kept)
  dest TEXT,                       -- where the plan moves it; NULL when it stays
  why TEXT,
  at REAL NOT NULL,
  PRIMARY KEY (session, path)
);
CREATE INDEX IF NOT EXISTS outcome_id ON outcome(id);
"""

META = ["taken_local", "taken_ts", "time_source", "utc_offset", "make", "model", "lens", "iso", "exposure", "fnumber",
        "focal", "focal35", "lat", "lon", "alt", "width", "height", "orientation", "motion_photo", "meta"]
ARCHIVE = "Archive/OrderMyPhotos/"
# plan.py's reasons for archiving a photo because the same photo is kept (P54, copies): duplicates, not rejections
DUPLICATE = ("an exact copy of a photo", "a copy of a photo")
ONEDRIVE = Path("/srv/mimir/onedrive")


def path() -> Path | None:
    """The catalog, or None when it's off."""
    env = os.environ.get("OMP_CATALOG")
    if env == "off":
        return None
    return Path(env) if env else Path(os.environ.get("OMP_STATE_DIR", "/state")) / "catalog.db"


def library_of(root) -> tuple[str, str]:
    """The library a root belongs to, and the root's folder in it ("" for the whole library). OneDrive is one
    library whichever snapshot of the mirror is read, and a session may read one of its folders; any other root
    is a library of its own."""
    r = Path(root).resolve()
    if r.is_relative_to(ONEDRIVE) and "mirror" in r.relative_to(ONEDRIVE).parts:
        parts = r.relative_to(ONEDRIVE).parts
        return "onedrive", "/".join(parts[parts.index("mirror") + 1:])
    return str(Path(root)), ""


def _in(prefix: str, rel: str) -> str:
    return f"{prefix}/{rel}" if prefix else rel


def is_test(root) -> bool:
    """A test set: a copy of some photos, in one of the roots the app applies plans to (P48)."""
    r = Path(root).resolve()
    roots = [Path(p).resolve() for p in os.environ.get("OMP_APPLY_ROOTS", "").split(":") if p]
    return any(r == b or r.is_relative_to(b) for b in roots)


def open_catalog(root) -> sqlite3.Connection | None:
    """The catalog for a session on `root`, or None for a test set or when it's off."""
    p = path()
    if p is None or is_test(root):
        return None
    p.parent.mkdir(parents=True, exist_ok=True)
    cat = sqlite3.connect(p, timeout=60)
    cat.execute("PRAGMA journal_mode=WAL")
    cat.executescript(SCHEMA)
    return cat


def save(cfg, con, session: str) -> str:
    """Save a session's inventory: its files with their library and path, and its photos with their metadata."""
    cat = open_catalog(cfg.root)
    if cat is None:
        return "catalog: off for this session (a test set, or OMP_CATALOG=off)"
    (lib, prefix), now = library_of(cfg.root), time.time()
    files = [(_in(prefix, f[0]), *f[1:]) for f in con.execute("SELECT path, id, size, mtime, media, policy, album FROM file")]
    cols = {r[1] for r in con.execute("PRAGMA table_info(photo)")}
    meta = [c for c in META if c in cols]
    photos = {r[0]: r[1:] for r in con.execute(f"SELECT id, {', '.join(meta)} FROM photo")}
    hashes = {}
    for i, data in con.execute("SELECT id, data FROM signal WHERE kind='phash'"):
        d = json.loads(data or "{}")
        hashes[i] = (d.get("phash"), d.get("dhash"))
    size_of = {}
    for f in files:
        size_of.setdefault(f[1], (f[2], f[4]))
    with cat:
        # The session saw all of its root, so what it didn't see there isn't there any more.
        if prefix:
            cat.execute("DELETE FROM location WHERE library=? AND substr(path, 1, ?) = ?",
                        (lib, len(prefix) + 1, prefix + "/"))
        else:
            cat.execute("DELETE FROM location WHERE library=?", (lib,))
        cat.executemany(
            "INSERT OR REPLACE INTO location(library, path, id, size, mtime, media, policy, album, session, seen)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)", [(lib, *f, session, now) for f in files])
        rows = []
        for i, (size, media) in size_of.items():
            m = dict(zip(meta, photos.get(i, [None] * len(meta))))
            rows.append([i, size, media] + [m.get(c) for c in META] + list(hashes.get(i, (None, None))) + [now, session, now])
        names = ["id", "size", "media"] + META + ["phash", "dhash", "first_seen", "session", "updated"]
        # what a later session doesn't know (no metadata, no hashes yet) doesn't erase what's known
        keep_first = ", ".join(f"{c}=excluded.{c}" if c in ("session", "updated") else f"{c}=COALESCE(excluded.{c}, {c})"
                               for c in names if c not in ("id", "first_seen"))
        cat.executemany(f"INSERT INTO media({', '.join(names)}) VALUES ({', '.join('?' * len(names))})"
                        f" ON CONFLICT(id) DO UPDATE SET {keep_first}", rows)
        cat.execute("INSERT INTO session(session, library, root, files, photos, inventory_at) VALUES (?,?,?,?,?,?)"
                    " ON CONFLICT(session) DO UPDATE SET library=excluded.library, root=excluded.root,"
                    " files=excluded.files, photos=excluded.photos, inventory_at=excluded.inventory_at",
                    (session, lib + (f"/{prefix}" if prefix else ""), str(cfg.root), len(files), len(photos), now))
    with_hash = sum(1 for i in size_of if hashes.get(i, (None,))[0])
    cat.close()
    return (f"catalog: {len(files)} files of {lib} and {len(size_of)} photos and videos saved"
            f" ({len(photos)} with metadata, {with_hash} with perceptual hashes) -> {path()}")


def record_outcome(cfg, con, session: str, final: dict) -> str:
    """Save a session's confirmed plan: where each photo it judged ends up, kept or archived."""
    cat = open_catalog(cfg.root)
    if cat is None:
        return "catalog: off for this session"
    plan = json.loads((cfg.bundle / "plan.json").read_text(encoding="utf-8"))
    moves = {m["from"]: m for m in plan["moves"]}
    (lib, prefix), now = library_of(cfg.root), time.time()
    rows, counts = [], {"keep": 0, "archive": 0}
    for p, i, policy in con.execute("SELECT path, id, policy FROM file WHERE media='photo'"):
        if policy in ("ignore", "people", "reference") or p.split("/")[0] == "OrderMyPhotos":
            continue  # not judged, as in the plan
        m = moves.get(p)
        verdict = "archive" if m and m["to"].startswith(ARCHIVE) else "keep"
        if verdict == "archive" and (m.get("why") or "").startswith(DUPLICATE):
            verdict = "duplicate"  # archived because the same photo is kept: not a judgement on the photo
        counts[verdict] = counts.get(verdict, 0) + 1
        rows.append((session, _in(prefix, p), i, lib, verdict, _in(prefix, m["to"]) if m else None,
                     m.get("why") if m else None, now))
    with cat:
        cat.execute("DELETE FROM outcome WHERE session=?", (session,))
        cat.executemany("INSERT INTO outcome(session, path, id, library, verdict, dest, why, at) VALUES (?,?,?,?,?,?,?,?)", rows)
        cat.execute("INSERT INTO session(session, library, root, outcome_at, archived) VALUES (?,?,?,?,?)"
                    " ON CONFLICT(session) DO UPDATE SET outcome_at=excluded.outcome_at, archived=excluded.archived",
                    (session, lib + (f"/{prefix}" if prefix else ""), str(cfg.root), now, final.get("archived")))
    cat.close()
    return (f"catalog: the outcome of {session} saved: {counts['keep']} kept, {counts['archive']} archived,"
            f" {counts.get('duplicate', 0)} archived as duplicates ({final.get('archived')})")


def decided_before(cfg, session: str) -> dict[str, dict]:
    """Photo id -> the latest confirmed judgement of it in another session:
    {"verdict": keep | archive, "session", "library", "at", "why", "where", "present"}.

    Per session, a photo is kept if any of its copies there was kept, and archived if one was archived on its merits.
    A session where it was only archived as a duplicate (the same photo kept elsewhere) says nothing about it and is
    skipped. `where`: a kept copy's path in its library (where the plan moved it); `present`: whether the photo was
    still in that library at its last inventory."""
    cat = open_catalog(cfg.root)
    if cat is None:
        return {}
    latest = {}
    for i, s, lib, at, kept, rejected, where, why in cat.execute(
            "SELECT id, session, MIN(library), MAX(at), MAX(verdict = 'keep'), MAX(verdict = 'archive'),"
            " MIN(CASE WHEN verdict = 'keep' THEN COALESCE(dest, path) END),"
            " MIN(CASE WHEN verdict = 'archive' THEN why END)"
            " FROM outcome WHERE session != ? GROUP BY id, session", (session,)):
        if not (kept or rejected):
            continue
        if i not in latest or at > latest[i]["at"]:
            latest[i] = {"verdict": "keep" if kept else "archive", "session": s, "library": lib, "at": at,
                         "why": None if kept else why, "where": where if kept else None}
    present = {}
    for lib in {b["library"] for b in latest.values() if b["verdict"] == "keep"}:
        present[lib] = {r[0] for r in cat.execute("SELECT DISTINCT id FROM location WHERE library=?", (lib,))}
    cat.close()
    for i, b in latest.items():
        b["present"] = i in present.get(b["library"], ())
    return latest


def split_before(cfg, session: str) -> tuple[dict, dict, dict]:
    """What a session does with photos judged before (P58), as three dicts id -> decided_before()'s entry:
    - archived: archived on its merits before: archived again;
    - kept: kept before in this same library: kept again;
    - duplicates: kept before in another library and still there: this copy is a duplicate, archived.
    A photo kept in another library but gone from it at that library's last inventory is kept here: it may be the
    only copy left."""
    lib = library_of(cfg.root)[0]
    archived, kept, duplicates = {}, {}, {}
    for i, b in decided_before(cfg, session).items():
        if b["verdict"] == "archive":
            archived[i] = b
        elif b["library"] != lib and b["present"]:
            duplicates[i] = b
        else:
            kept[i] = b
    return archived, kept, duplicates


def main(argv=None):
    """`python -m omp.catalog save|outcome` for the active session's env (OMP_ROOT, OMP_DB, OMP_BUNDLE, OMP_RUN). It
    reads the session's DB read-only, so it can run while the session does."""
    from .config import Config

    argv = argv or sys.argv[1:]
    cfg, session = Config(), os.environ.get("OMP_RUN", "")
    if not session or argv[:1] not in (["save"], ["outcome"]):
        sys.exit("usage: OMP_RUN=<session> [OMP_ROOT=... OMP_DB=... OMP_BUNDLE=...] python -m omp.catalog save|outcome")
    con = sqlite3.connect(f"file:{cfg.db}?mode=ro", uri=True, timeout=60)
    if argv[0] == "save":
        print(save(cfg, con, session))
    else:
        final = json.loads((Path(cfg.db).parent / "final.json").read_text(encoding="utf-8"))
        print(record_outcome(cfg, con, session, final))


if __name__ == "__main__":
    main()
