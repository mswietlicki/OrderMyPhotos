"""The review in the web app (P47): every album and folder of a session is one review unit, existing or new,
all treated the same. A unit shows the photos that will be in it once the plan is applied: its own photos
that stay, and those the sorting put there.

Every change is saved on the server the moment it's made, so nothing is lost by leaving the page, and the
review looks the same from any device or browser. The reviewer can, per unit:
- keep or archive a photo (`verdict` labels, which the plan follows);
- take a photo out (`out`): it's sorted again by the `resort` phase, never back into this unit;
- move a photo to another album or folder by hand (`move`);
- rename the unit, or dismantle it: all its photos are taken out, and nothing is sorted into it again;
- archive it whole (P73): every photo in it is archived and pinned there, and nothing is sorted into it again;
- confirm it (`place`): every photo in it is pinned there, and later sorting rounds don't touch it.

After a resort, the units that received photos are open again, showing only what's new; what was confirmed
stays confirmed. A photo's place is worked out the same way here, in the sorting (albums.py) and in the plan:
the reviewer's latest word on it wins.
"""

import hashlib
import json
import sqlite3
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import FileResponse

router = APIRouter()
current = lambda: None  # noqa: E731 - set by web.py: the active session

SCHEMA = """
CREATE TABLE IF NOT EXISTS review_unit (
  key TEXT PRIMARY KEY,            -- the album or folder path the unit stands for
  status TEXT NOT NULL,            -- confirmed | dismantled | archived (Archive all, P73)
  name TEXT,                       -- its new path, when the reviewer renamed it
  round INTEGER, updated REAL
);
"""
SUBS = (" PRIV", "/Dokumenty")     # an album's private and documents parts are reviewed with it (P24, P26)
ARCHIVE_ALL = ":archive-all"       # the source of the verdicts Archive all wrote, so Review again can undo them
_cache: dict = {"key": None}
# One placements() at a time (P74): on the whole library one took ~15 s and ~7 GB (1.6 s and 0.3 GB since P75), and every request that came
# while the DB had changed started its own. The page polls the status every 3 s, so they piled up: 20 at once
# used 60 GB, and the kernel killed the app (2026-11-13).
_placing = threading.Lock()
# What placements are made from, apart from the review's labels (P75): read once, then only the labels added since
# are read, and only their photos worked out again. Read in full again when a pipeline process starts or ends
# (`forget`, from web.py), while one runs (`writer_alive`), or when the tables it's read from changed in any other
# way (`_fingerprint`: a script, a restored backup). Labels are only ever added, never changed or deleted.
_inputs: dict = {"b": None}
_generation = [0]
writer_alive = lambda: False  # noqa: E731 - set by web.py: a pipeline process is running


def forget():
    """A pipeline process started or ended: what placements are made from is read again in full."""
    _generation[0] += 1


def base(target: str) -> tuple[str, str]:
    """A target's unit and the part of it the photo goes to: ("Albums/X", " PRIV")."""
    for s in SUBS:
        if target.endswith(s):
            return target[: -len(s)], s
    return target, ""


def connect(session, write=False) -> sqlite3.Connection:
    db = session.config().db
    if not db.exists():
        raise HTTPException(409, "no data yet: the first phases haven't run")
    con = sqlite3.connect(db if write else f"file:{db}?mode=ro", uri=not write, timeout=30)
    con.row_factory = sqlite3.Row
    if write:
        con.executescript(SCHEMA)
    return con


def _has(con, table) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _stamp(db: Path) -> tuple:
    return tuple(p.stat().st_mtime_ns if p.exists() else 0 for p in (db, db.with_name(db.name + "-wal")))


def placements(session, stale_ok=False) -> dict:
    """Every photo's unit, part, state and verdict, recomputed only when the DB changes, by one caller at a time:
    the others wait for it and take its result. `stale_ok`: while it's being recomputed, the last result will do
    (the status the page polls)."""
    db = session.config().db
    if _cache["key"] == (db, _stamp(db)):
        return _cache["data"]
    if stale_ok and _placing.locked() and _cache["key"] and _cache["key"][0] == db:
        return _cache["data"]
    with _placing:
        stamp = _stamp(db)  # before reading: a change made while it reads makes the next call read again
        if _cache["key"] == (db, stamp):
            return _cache["data"]  # made while this one waited
        return _placements(session, db, stamp)


REVIEW_KINDS = ('verdict', 'out', 'move', 'place')  # the labels placements follow


def _fingerprint(con, run) -> tuple:
    """The tables placements are read from, apart from the labels, as counts and last ids: a few ms."""
    one = lambda sql, *a: tuple(con.execute(sql, a).fetchone())  # noqa: E731
    return (one("SELECT max(pid), count(*) FROM proposal WHERE run=?", run) if _has(con, "proposal") else None,
            one("SELECT count(*) FROM proposal_member") if _has(con, "proposal_member") else None,
            one("SELECT max(rowid), count(*) FROM decision") if _has(con, "decision") else None,
            one("SELECT max(rowid), count(*) FROM file"),
            one("SELECT max(gid), count(*) FROM grp") if _has(con, "grp") else None)


def _read_inputs(con, run) -> dict:
    """Everything a photo's place is made from but the labels, each piece read once: a proposal's data is parsed
    once, not once per photo in it (P75: 77k parses of the big albums' data took most of the 16 s), and SQLite
    takes the few features needed out of each decision's JSON."""
    files = {r[0]: (r[1], r[2], r[3]) for r in con.execute(
        "SELECT id, MIN(path), MIN(album), MIN(policy) FROM file"
        " WHERE media='photo' AND policy IN ('cull', 'describe', 'unsorted') GROUP BY id")}
    dec = {}  # id -> (gid, verdict, rank, tier, place, rotate, nudity, archived before or kept elsewhere)
    if _has(con, "decision"):
        for r in con.execute(
                "SELECT id, gid, verdict, rank, tier, json_extract(f, '$.place'), json_extract(f, '$.rotate'),"
                " json_extract(f, '$.nudity'), json_extract(f, '$.archived_before'), json_extract(f, '$.library_copy')"
                " FROM (SELECT *, nullif(features, '') AS f FROM decision WHERE run=?)", (run,)):
            dec[r[0]] = (*r[1:8], bool(r[8] or r[9]))
    props, prop = {}, {}  # pid -> (action, target, moved by hand, why, reasons, unsure); photo -> its pid
    if _has(con, "proposal"):
        for pid, action, target, data in con.execute("SELECT pid, action, target, data FROM proposal WHERE run=?"
                                                     " AND action IN ('add', 'create', 'theme', 'copy')", (run,)):
            d = json.loads(data or "{}")
            props[pid] = (action, target, bool(d.get("moved")), d.get("why", ""), d.get("reasons") or {}, d.get("unsure") or {})
        for i, pid in con.execute("SELECT m.id, p.pid FROM proposal p JOIN proposal_member m ON m.pid=p.pid"
                                  " WHERE p.run=? AND p.action IN ('add', 'create', 'theme', 'copy')", (run,)):
            prop[i] = pid
    from .db import exact_copies

    return {"files": files, "dec": dec, "props": props, "prop": prop,
            "exact": exact_copies(con, run) if _has(con, "grp") else {},
            "existing": {a for _, a, policy in files.values() if policy in ("cull", "describe")},
            "verdict": {}, "word": {}, "lid": 0, "photos": {}}


def _read_labels(con, b) -> set:
    """The review's labels added since the last read, into `b`; the photos they're about."""
    changed = set()
    for lid, i, kind, value in con.execute(f"SELECT lid, id, kind, value FROM label WHERE lid > ? AND kind IN {REVIEW_KINDS}"
                                           " ORDER BY lid", (b["lid"],)):
        if kind == "verdict":
            b["verdict"][i] = "archive" if value == "bad" else value
        else:
            b["word"][i] = (kind, value)
        changed.add(i)
        b["lid"] = lid
    return changed


def _photo(i, b) -> dict | None:
    """A photo's unit, part, state and verdict; None when it isn't reviewed."""
    path, album, policy = b["files"][i]
    d = b["dec"].get(i)
    pid = b["prop"].get(i)
    pr = b["props"][pid] if pid is not None else None
    own = album if policy in ("cull", "describe") else None
    kind, value = b["word"].get(i, (None, ""))
    if pr is not None and pr[0] == "copy" and kind not in ("move", "place"):
        return None  # an exact copy of a photo in an album: archived as a copy, nothing to review
    if i in b["exact"] and kind != "move":  # a confirmed album's "place" labels don't count, only a move by hand
        return None  # the same pixels as a photo that's kept, saved again: archived, nothing to review (P54)
    if d and d[7] and kind != "move":
        return None  # archived in an earlier session, or kept in another storage (P58)
    place = d[4] if d else None
    state, target = "open", None
    if kind in ("move", "place") and value and not value.startswith("new:"):
        target, state = value, "confirmed"
    elif kind == "move" and value.startswith("new:"):  # a new album named by hand: made by the next sort
        if pr is not None and pr[0] == "create" and pr[2]:
            target = pr[1]
        else:
            target, state = (pr[1] if pr is not None else own) or "(not sorted)", "out"
    elif kind == "out" and value:
        if pr is not None and base(pr[1])[0] != base(value)[0]:
            target = pr[1]                     # sorted again: open in its new unit
        else:
            target, state = value, "out"       # waiting for the resort
    if target is None:
        target = pr[1] if pr is not None else (place or own) if own else None
    if target is None:
        target = "(not sorted)"
    unit, sub = base(target)
    if state == "confirmed" and own and unit == own:
        sub = base(place or own)[1]
    why, unsure = "", None
    if pr is not None and target == pr[1]:
        why, unsure = pr[4].get(i) or pr[3], pr[5].get(i)
    return {"id": i, "path": path, "unit": unit, "sub": sub, "state": state,
            "incoming": own != unit, "verdict": b["verdict"].get(i) or (d[1] if d else "keep"),
            "pipeline": d[1] if d else None, "gid": d[0] if d else None,
            "best": bool(d and d[2] == 1), "tier": d[3] if d else None,
            "rotate": (d[5] if d else None) or 0, "nudity": d[6] if d else None, "why": why, "unsure": unsure}


def _placements(session, db, stamp) -> dict:
    con = connect(session)
    try:
        run = session.run_id
        key = (db, run, _fingerprint(con, run), _generation[0])
        b = _inputs["b"]
        if b is None or b["key"] != key or writer_alive():  # in full
            b = _read_inputs(con, run)
            _read_labels(con, b)
            photos = {i: p for i in b["files"] if (p := _photo(i, b)) is not None}
        else:  # only the photos with new labels; a new dict, so a reader of the last one isn't disturbed
            photos = dict(b["photos"])
            for i in _read_labels(con, b):
                p = _photo(i, b) if i in b["files"] else None
                if p is None:
                    photos.pop(i, None)
                else:
                    photos[i] = p
        b.update(key=key, photos=photos)
        _inputs["b"] = b
        status = {r["key"]: dict(r) for r in con.execute("SELECT * FROM review_unit")} if _has(con, "review_unit") else {}
    finally:
        con.close()
    units = defaultdict(lambda: Counter())
    existing = b["existing"]
    for p in photos.values():
        c = units[p["unit"]]
        c["n"] += 1
        c[p["state"]] += 1
        c[p["verdict"]] += 1
        c["unsure"] += bool(p["unsure"]) and p["state"] == "open"
    out = {}
    for key, c in units.items():
        st = status.get(key, {})
        # photos taken out wait here until the resort; they don't keep the unit open
        state = "dismantled" if st.get("status") == "dismantled" else "confirmed" if not c["open"] else "open"
        out[key] = {"key": key, "name": PurePosixPath(st.get("name") or key).name,
                    "kind": "album" if key.startswith("Pictures/Albums/") else "folder",
                    "new": key not in existing, "n": c["n"], "keep": c["keep"], "archive": c["archive"],
                    "open": c["open"], "confirmed": c["confirmed"], "out": c["out"], "unsure": c["unsure"], "state": state,
                    "archived": st.get("status") == "archived"}
    for key, st in status.items():  # a dismantled unit stays listed, empty, until its photos have new places
        if st["status"] == "dismantled" and key not in out:
            out[key] = {"key": key, "name": PurePosixPath(key).name, "kind": "album" if key.startswith("Pictures/Albums/")
                        else "folder", "new": key not in existing, "n": 0, "keep": 0, "archive": 0, "open": 0,
                        "confirmed": 0, "out": 0, "unsure": 0, "state": "dismantled", "archived": False}
    data = {"photos": photos, "units": out}
    _cache.update(key=(db, stamp), data=data)
    return data


def pending(session, stale_ok=False) -> dict:
    """What stands between the review and the plan: units still open, photos waiting to be sorted again."""
    try:
        d = placements(session, stale_ok)
    except HTTPException:
        return {"units": 0, "unconfirmed": 0, "to_sort": 0}
    units = [u for u in d["units"].values() if u["state"] != "dismantled"]
    return {"units": len(units), "unconfirmed": sum(u["state"] == "open" for u in units),
            "to_sort": sum(p["state"] == "out" for p in d["photos"].values())}


def _session():
    s = current()
    if not s:
        raise HTTPException(400, "no session")
    return s


@router.get("/api/review/units")
def api_units():
    s = _session()
    d = placements(s)
    units = sorted(d["units"].values(), key=lambda u: ({"open": 0, "confirmed": 1, "dismantled": 2}[u["state"]], u["key"]))
    return {"units": units, "pending": pending(s), "targets": sorted(k for k, u in d["units"].items() if u["state"] != "dismantled")}


@router.get("/api/review/unit")
def api_unit(key: str):
    s = _session()
    d = placements(s)
    if key not in d["units"]:
        raise HTTPException(404, "no such album or folder")
    members = [dict(p) for p in d["photos"].values() if p["unit"] == key]  # copies: the cached ones are kept (P75)
    con = connect(s)
    ids = [p["id"] for p in members]
    meta, caps = {}, {}
    for chunk in (ids[i:i + 500] for i in range(0, len(ids), 500)):
        q = ",".join("?" * len(chunk))
        for r in con.execute(f"SELECT id, taken_ts, taken_local FROM photo WHERE id IN ({q})", chunk):
            meta[r["id"]] = dict(r)
        if _has(con, "caption"):
            for r in con.execute(f"SELECT id, data FROM caption WHERE id IN ({q}) ORDER BY rowid", chunk):
                c = json.loads(r["data"] or "{}")
                caps[r["id"]] = {k: c.get(k) for k in ("title_en", "title_pl", "description_en", "description_pl")}
    con.close()
    for p in members:
        p |= {"taken": (meta.get(p["id"]) or {}).get("taken_local"), "ts": (meta.get(p["id"]) or {}).get("taken_ts") or 0,
              "name": PurePosixPath(p["path"]).name, "from": str(PurePosixPath(p["path"]).parent), "caption": caps.get(p["id"])}
    members.sort(key=lambda p: (p["ts"], p["name"]))
    return {"unit": d["units"][key], "photos": members, "round": _round(s)}


def _round(s) -> int:
    from .web import runner  # noqa: PLC0415 - the runner knows the review round
    return runner.control.get("rounds", 0) if runner.session and runner.session.id == s.id else 0


def _labels(con, rows, source):
    con.executemany("INSERT INTO label(id, kind, value, source) VALUES (?,?,?,?)", [(i, k, v, source) for i, k, v in rows])


def _renamed(key: str, name: str | None) -> str:
    """A unit's path after a rename. An album keeps its date in front: "<date> <new name>"."""
    name = (name or "").strip().strip("/")
    p = PurePosixPath(key)
    if not name or name == p.name:
        return key
    if key.startswith("Pictures/Albums/") and len(p.name) > 11 and p.name[10] == " " and not name[:10].count("-") == 2:
        name = f"{p.name[:10]} {name}"
    return str(p.parent / name)


@router.post("/api/review/unit/confirm")
def api_confirm(key: str = Body(...), name: str | None = Body(None), verdicts: dict = Body({}),
                out: list = Body([]), moves: dict = Body({}), back: list = Body([])):
    """Everything in the unit as the reviewer leaves it: kept or archived, taken out or moved, the rest pinned.
    `back`: photos taken out earlier that the reviewer puts back here."""
    s = _session()
    new_key = confirm(s, key, name, verdicts, out, moves, back)
    return {"key": new_key, "out": len(out), "moved": len(moves), "pending": pending(s)}


def confirm(s, key: str, name=None, verdicts=None, out=(), moves=None, back=()) -> str:
    verdicts, moves = verdicts or {}, moves or {}
    d = placements(s)
    members = [p for p in d["photos"].values() if p["unit"] == key]
    if not members:
        raise HTTPException(404, "no photos in this unit")
    con = connect(s, write=True)
    draft = (con.execute("SELECT name FROM review_unit WHERE key=?", (key,)).fetchone() or [None])[0]
    con.close()
    new_key = _renamed(key, name) if name else (draft or key)
    src = f"review:{s.id}:{_round(s)}"
    rows = []
    for p in members:
        i = p["id"]
        v = verdicts.get(i, p["verdict"])
        if v not in ("keep", "archive"):
            raise HTTPException(400, f"bad verdict {v!r}")
        rows.append((i, "verdict", v))
        if i in moves and moves[i] and base(moves[i])[0] != key:
            rows.append((i, "move", moves[i]))
        elif i in out:
            rows.append((i, "out", key))
        elif p["state"] != "out" or i in back:
            rows.append((i, "place", new_key + p["sub"]))
    con = connect(s, write=True)
    with con:
        _labels(con, rows, src)
        con.execute("INSERT OR REPLACE INTO review_unit(key, status, name, round, updated) VALUES (?,?,?,?,?)",
                    (key, "confirmed", new_key if new_key != key else None, _round(s), time.time()))
    con.close()
    return new_key


def confirm_rest(s) -> int:
    """Finished (user): every album and folder not confirmed yet is confirmed as it is now, its photos pinned
    with the verdicts they have, renames kept. Returns how many."""
    keys = [k for k, u in placements(s)["units"].items() if u["state"] == "open"]
    for k in keys:
        confirm(s, k)
    return len(keys)


@router.post("/api/review/photo")
def api_photo_action(key: str = Body(...), id: str = Body(...), action: str = Body(...), target: str | None = Body(None)):
    """One change to one photo, saved at once: keep, archive, out (sorted again), back (put back here), move."""
    s = _session()
    p = placements(s)["photos"].get(id)
    if not p or p["unit"] != key:
        raise HTTPException(404, "that photo isn't in this album or folder any more")
    if action in ("keep", "archive"):
        row = (id, "verdict", action)
    elif action == "out":
        row = (id, "out", key)
    elif action == "back":
        row = (id, "place", key + p["sub"])
    elif action == "move" and target and base(target)[0] != key:
        row = (id, "move", target)
    else:
        raise HTTPException(400, f"can't {action!r} here")
    con = connect(s, write=True)
    with con:
        _labels(con, [row], f"review:{s.id}:{_round(s)}")
    con.close()
    return {"ok": True}


@router.post("/api/review/unit/rename")
def api_rename(key: str = Body(...), name: str = Body(...)):
    """A new name, kept until the unit is confirmed (then its photos go there)."""
    s = _session()
    new_key = _renamed(key, name)
    con = connect(s, write=True)
    with con:
        row = con.execute("SELECT status FROM review_unit WHERE key=?", (key,)).fetchone()
        con.execute("INSERT OR REPLACE INTO review_unit(key, status, name, round, updated) VALUES (?,?,?,?,?)",
                    (key, row[0] if row else "open", new_key if new_key != key else None, _round(s), time.time()))
    con.close()
    return {"key": key, "name": new_key}


_cards_cache: dict = {}  # the last cards made, while the clustering they come from is the same


@router.get("/api/people/cards")
def api_people_cards():
    """The People page's cards (people and animals with their faces), made from the session's DB. Making them
    reads every face (the full library: 175k faces in 3,921 people and 2,952 animals), so they're kept until the
    clustering changes; only the names the user gave (`label`) are read again each time."""
    from .naming import page_data  # noqa: PLC0415

    s = _session()
    cfg = s.config()
    con = connect(s)
    try:
        if not _has(con, "person"):
            raise HTTPException(409, "no people yet: the phase that finds them hasn't run")
        key = (s.id, s.run_id, *con.execute("SELECT COUNT(*), MAX(pid), SUM(faces) FROM person WHERE run=?",
                                             (s.run_id,)).fetchone(),
               *(con.execute("SELECT COUNT(*), MAX(pid) FROM pet WHERE run=?", (s.run_id,)).fetchone()
                 if _has(con, "pet") else ()))
        if _cards_cache.get("key") != key:
            data = page_data(cfg, con, s.run_id, cfg.bundle)
            for c in data["people"] + data["pets"]:  # the crops, served from the bundle
                for f in c["faces"]:
                    f["src"] = "/library/OrderMyPhotos/" + f["src"]
            _cards_cache.update(key=key, data=data)
        data = _cards_cache["data"]
        said = {r[0]: r[1] for r in con.execute("SELECT key, who FROM name_label")} if _has(con, "name_label") else {}
    finally:
        con.close()
    for c in data["people"] + data["pets"]:  # who the user said most of a card's faces are, "" if nobody
        votes = Counter(said[f["k"]] for f in c["faces"] if said.get(f["k"]))
        c["label"] = votes.most_common(1)[0][0] if votes else ""
    fb = _facebook(s, key)
    for c in data["people"]:
        c["fb"] = fb["cards"].get(c["id"], [])
    data["fb_names"] = fb["names"]
    return data


def _facebook(s, key) -> dict:
    """Names suggested from the user's Facebook friends (P64): `suggestions-facebook.json` in the session's folder,
    made outside the app (on the server; the friends' names and pictures never go in the repo). Per card, up to 3
    friends whose profile picture's face is near the card's, with the similarity; and every friend's name, for
    typing. Only used for the clustering it was made from (the run, and its people's count, last id and faces)."""
    path = s.dir / "suggestions-facebook.json"
    none = {"cards": {}, "names": []}
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return none
    if d.get("run") != s.run_id or list(d.get("people") or []) != list(key[2:5]):
        return none
    cards = {cid: [{**m, "pic": "/library/OrderMyPhotos/" + m["pic"]} for m in ms] for cid, ms in d.get("cards", {}).items()}
    return {"cards": cards, "names": d.get("names", [])}


_people_lock = threading.Lock()  # a save's check of the version and its write, one at a time


def _people_version(path: Path) -> str:
    """Which saved state a page started from: its content's hash, so a state written by anything (another tab or
    device, a script, a restored backup) is a new version."""
    try:
        return hashlib.sha1(path.read_bytes()).hexdigest()[:16]
    except FileNotFoundError:
        return "none"


@router.get("/api/people")
def api_people_get():
    """The People page's state (names, nicknames, merges, faces taken out) as the page last saved it, its version,
    and the names per face in the DB, from which a page with no saved state starts (an earlier import)."""
    s = _session()
    path = s.dir / "people.json"
    labels = {}
    try:
        con = connect(s)
        if _has(con, "name_label"):
            labels = {r["key"]: r["who"] for r in con.execute("SELECT key, who FROM name_label")}
        con.close()
    except HTTPException:
        pass
    with _people_lock:
        version = _people_version(path)
        saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    return {"state": saved, "version": version, "labels": labels}


@router.put("/api/people")
def api_people_put(state: dict = Body(...), names: dict = Body(...), keys: list = Body(...), version: str | None = Body(None)):
    """Saved on every change: the page's state, and the names per face, so the next run of `people` uses them.
    Only the faces the page shows (`keys`) change: one without a name now loses its old one, and the faces it
    doesn't show keep theirs.

    The page sends its whole state, so a page loaded before another save (another tab or device, a script) would
    undo that save. `version` is the state the page started from (or last saved): when the state is newer, nothing
    is saved and the answer is 409, and the page asks to be reloaded (P69). A page without a version (loaded before
    this) gets 409 too."""
    from .people import SCHEMA as PEOPLE_SCHEMA  # noqa: PLC0415

    s = _session()
    path = s.dir / "people.json"
    with _people_lock:
        if version != _people_version(path):
            raise HTTPException(409, "The People page was changed elsewhere (another tab or device): reload it to see the changes")
        tmp = s.dir / "people.json.tmp"
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        con = connect(s, write=True)
        with con:
            con.executescript(PEOPLE_SCHEMA)
            gone = [k for k in keys if k not in names]
            con.executemany("DELETE FROM name_label WHERE key=?", [(k,) for k in gone])
            con.executemany("INSERT OR REPLACE INTO name_label(key, who, name, nick, owner, source) VALUES (?,?,?,?,?,?)",
                            [(k, v.get("who") or "", v.get("name") or None, v.get("nick") or None, int(bool(v.get("owner"))),
                              f"people-page:{s.id}") for k, v in names.items()])
        con.close()
        return {"saved": len(names), "version": _people_version(path)}


@router.post("/api/review/unit/dismantle")
def api_dismantle(key: str = Body(..., embed=True)):
    """Not an album or folder after all: all its photos are sorted again, and nothing goes there any more."""
    s = _session()
    d = placements(s)
    members = [p for p in d["photos"].values() if p["unit"] == key]
    con = connect(s, write=True)
    with con:
        _labels(con, [(p["id"], "out", key) for p in members], f"review:{s.id}:{_round(s)}:dismantle")
        con.execute("INSERT OR REPLACE INTO review_unit(key, status, name, round, updated) VALUES (?,?,?,?,?)",
                    (key, "dismantled", None, _round(s), time.time()))
    con.close()
    return {"out": len(members), "pending": pending(s)}


@router.post("/api/review/unit/archive")
def api_archive(key: str = Body(..., embed=True)):
    """Archive all (P73; user: "I need an option to archive whole album with all photos"): every photo in the
    unit but the ones taken out is archived and pinned there, and the unit is confirmed. With the plan they go to
    `Archive/OrderMyPhotos/`, and nothing is sorted into the unit any more (albums.py bans it, as a dismantled one).
    Review again gives each photo back the verdict it had before, unless it was changed since."""
    s = _session()
    members = [p for p in placements(s)["photos"].values() if p["unit"] == key and p["state"] != "out"]
    if not members:
        raise HTTPException(404, "no photos in this unit")
    con = connect(s, write=True)
    draft = (con.execute("SELECT name FROM review_unit WHERE key=?", (key,)).fetchone() or [None])[0]
    src = f"review:{s.id}:{_round(s)}"
    with con:
        _labels(con, [(p["id"], "verdict", "archive") for p in members], src + ARCHIVE_ALL)
        _labels(con, [(p["id"], "place", (draft or key) + p["sub"]) for p in members], src)
        con.execute("INSERT OR REPLACE INTO review_unit(key, status, name, round, updated) VALUES (?,?,?,?,?)",
                    (key, "archived", draft, _round(s), time.time()))
    con.close()
    return {"archived": len(members), "pending": pending(s)}


def _before_archive_all(con, members) -> list:
    """The verdicts Archive all replaced, for the photos still archived by it: each one's verdict before it
    (the reviewer's, else the pipeline's)."""
    labels = defaultdict(list)
    ids = [p["id"] for p in members]
    for chunk in (ids[i:i + 500] for i in range(0, len(ids), 500)):
        q = ",".join("?" * len(chunk))
        for r in con.execute(f"SELECT id, value, source FROM label WHERE kind='verdict' AND id IN ({q}) ORDER BY lid", chunk):
            labels[r["id"]].append((r["value"], r["source"] or ""))
    rows = []
    for p in members:
        vs = labels.get(p["id"])
        if vs and vs[-1][1].endswith(ARCHIVE_ALL):
            before = next((v for v, src in reversed(vs) if not src.endswith(ARCHIVE_ALL)), None)
            rows.append((p["id"], "verdict", before or p["pipeline"] or "keep"))
    return rows


@router.post("/api/review/unit/reopen")
def api_reopen(key: str = Body(..., embed=True)):
    """Review a confirmed, dismantled or archived unit again: its photos become open, as the sorting left them.
    An archived one's photos get back the verdicts they had before Archive all."""
    s = _session()
    d = placements(s)
    members = [p for p in d["photos"].values() if p["unit"] == key]
    con = connect(s, write=True)
    status = (con.execute("SELECT status FROM review_unit WHERE key=?", (key,)).fetchone() or [None])[0]
    verdicts = _before_archive_all(con, members) if status == "archived" else []
    with con:
        src = f"review:{s.id}:{_round(s)}:reopen"
        _labels(con, verdicts, src)
        _labels(con, [(p["id"], "place", "") for p in members if p["state"] == "confirmed"], src)
        con.execute("DELETE FROM review_unit WHERE key=?", (key,))
    con.close()
    return {"pending": pending(s), "verdicts_back": len(verdicts)}


@router.get("/api/photo/{id_}/{size}")
def api_photo(id_: str, size: str):
    """A photo's thumbnail (384 px) or preview (1536 px), from the session's cache."""
    s = _session()
    cfg = s.config()
    path = cfg.thumb(id_) if size == "thumb" else cfg.preview(id_) if size == "preview" else None
    if path is None or not all(c in "0123456789abcdef" for c in id_) or not Path(path).is_file():
        raise HTTPException(404)
    return FileResponse(path, headers={"Cache-Control": "max-age=86400"})


@router.get("/api/review/plan")
def api_plan():
    """The plan the review leads to, summed up for the final decision."""
    s = _session()
    plan = json.loads((s.config().bundle / "plan.json").read_text(encoding="utf-8")) if (s.config().bundle / "plan.json").exists() else None
    if not plan:
        raise HTTPException(409, "no plan yet")
    arch = [m for m in plan["moves"] if m["to"].startswith("Archive/")]
    size = 0
    for m in arch:
        try:
            size += (Path(s.root) / m["from"]).stat().st_size
        except OSError:
            pass
    to = Counter(str(PurePosixPath(m["to"]).parent) for m in plan["moves"] if not m["to"].startswith("Archive/"))
    from .apply import appliable  # noqa: PLC0415

    can, why = appliable(Path(s.root))
    return {"apply": {"possible": can, "why_not": why, "root": s.root},
            "counts": plan["counts"], "moves": len(plan["moves"]), "rotations": len(plan.get("rotations", [])),
            "archive": {"n": len(arch), "bytes": size, "why": Counter(m["why"] for m in arch).most_common()},
            "targets": to.most_common(), "generated": plan.get("generated")}


@router.post("/api/labels")
def api_labels(spec: dict = Body(...)):
    """A page's export (the People page's names, or album decisions in the bundle's format), imported at once."""
    from .db import connect as db_connect  # noqa: PLC0415
    from .report import import_labels  # noqa: PLC0415

    s = _session()
    d = s.dir / "labels"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{time.strftime('%Y%m%d-%H%M%S')}-{'names' if spec.get('names') else 'decisions'}.json"
    path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    con = db_connect(s.config().db)
    try:
        import_labels(s.config(), con, str(path))
    finally:
        con.close()
    return {"saved": path.name}
