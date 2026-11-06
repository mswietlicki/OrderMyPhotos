"""The review in the web app (P47): every album and folder of a session is one review unit, existing or new,
all treated the same. A unit shows the photos that will be in it once the plan is applied: its own photos
that stay, and those the sorting put there.

Every change is saved on the server the moment it's made, so nothing is lost by leaving the page, and the
review looks the same from any device or browser. The reviewer can, per unit:
- keep or archive a photo (`verdict` labels, which the plan follows);
- take a photo out (`out`): it's sorted again by the `resort` phase, never back into this unit;
- move a photo to another album or folder by hand (`move`);
- rename the unit, or dismantle it: all its photos are taken out, and nothing is sorted into it again;
- confirm it (`place`): every photo in it is pinned there, and later sorting rounds don't touch it.

After a resort, the units that received photos are open again, showing only what's new; what was confirmed
stays confirmed. A photo's place is worked out the same way here, in the sorting (albums.py) and in the plan:
the reviewer's latest word on it wins.
"""

import json
import sqlite3
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
  status TEXT NOT NULL,            -- confirmed | dismantled
  name TEXT,                       -- its new path, when the reviewer renamed it
  round INTEGER, updated REAL
);
"""
SUBS = (" PRIV", "/Dokumenty")     # an album's private and documents parts are reviewed with it (P24, P26)
_cache: dict = {"key": None}


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


def placements(session) -> dict:
    """Every photo's unit, part, state and verdict, recomputed only when the DB changes."""
    db = session.config().db
    stamp = tuple(p.stat().st_mtime_ns if p.exists() else 0 for p in (db, db.with_name(db.name + "-wal")))
    if _cache["key"] == (db, stamp):
        return _cache["data"]
    con = connect(session)
    run = session.run_id
    files = {}
    for r in con.execute("SELECT id, MIN(path) AS path, MIN(album) AS album, MIN(policy) AS policy FROM file"
                         " WHERE media='photo' AND policy IN ('cull', 'describe', 'unsorted') GROUP BY id"):
        files[r["id"]] = dict(r)
    dec = {r["id"]: r for r in con.execute(
        "SELECT id, gid, verdict, rank, tier, features FROM decision WHERE run=?", (run,))} if _has(con, "decision") else {}
    verdict, word = {}, {}
    for r in con.execute("SELECT id, kind, value FROM label WHERE kind IN ('verdict', 'out', 'move', 'place') ORDER BY lid"):
        if r["kind"] == "verdict":
            verdict[r["id"]] = "archive" if r["value"] == "bad" else r["value"]
        else:
            word[r["id"]] = (r["kind"], r["value"])
    prop = {}
    if _has(con, "proposal"):
        for r in con.execute("SELECT m.id, p.pid, p.action, p.target, p.data FROM proposal p JOIN proposal_member m"
                             " ON m.pid=p.pid WHERE p.run=? AND p.action IN ('add', 'create', 'theme', 'copy')", (run,)):
            prop[r["id"]] = r
    status = {r["key"]: dict(r) for r in con.execute("SELECT * FROM review_unit")} if _has(con, "review_unit") else {}
    from .db import exact_copies

    exact = exact_copies(con, run) if _has(con, "grp") else {}
    con.close()

    photos = {}
    for i, f in files.items():
        d = dec.get(i)
        feat = json.loads(d["features"] or "{}") if d else {}
        pr = prop.get(i)
        own = f["album"] if f["policy"] in ("cull", "describe") else None
        kind, value = word.get(i, (None, ""))
        if pr is not None and pr["action"] == "copy" and kind not in ("move", "place"):
            continue  # an exact copy of a photo in an album: archived as a copy, nothing to review
        if i in exact and kind != "move":  # a confirmed album's "place" labels don't count, only a move by hand
            continue  # the same pixels as a photo that's kept, saved again: archived, nothing to review (P54)
        if (feat.get("archived_before") or feat.get("library_copy")) and kind != "move":
            continue  # archived in an earlier session, or kept in another storage (P58)
        state, target = "open", None
        if kind in ("move", "place") and value and not value.startswith("new:"):
            target, state = value, "confirmed"
        elif kind == "move" and value.startswith("new:"):  # a new album named by hand: made by the next sort
            if pr is not None and pr["action"] == "create" and json.loads(pr["data"] or "{}").get("moved"):
                target = pr["target"]
            else:
                target, state = (pr["target"] if pr is not None else own) or "(not sorted)", "out"
        elif kind == "out" and value:
            if pr is not None and base(pr["target"])[0] != base(value)[0]:
                target = pr["target"]          # sorted again: open in its new unit
            else:
                target, state = value, "out"   # waiting for the resort
        if target is None:
            target = pr["target"] if pr is not None else (feat.get("place") or own) if own else None
        if target is None:
            target = "(not sorted)"
        unit, sub = base(target)
        if state == "confirmed" and own and unit == own:
            sub = base(feat.get("place") or own)[1]
        why, unsure = "", None
        if pr is not None and target == pr["target"]:
            data = json.loads(pr["data"] or "{}")
            why = (data.get("reasons") or {}).get(i) or data.get("why", "")
            unsure = (data.get("unsure") or {}).get(i)
        photos[i] = {"id": i, "path": f["path"], "unit": unit, "sub": sub, "state": state,
                     "incoming": own != unit, "verdict": verdict.get(i) or (d["verdict"] if d else "keep"),
                     "pipeline": d["verdict"] if d else None, "gid": d["gid"] if d else None,
                     "best": bool(d and d["rank"] == 1), "tier": d["tier"] if d else None,
                     "rotate": feat.get("rotate") or 0, "nudity": feat.get("nudity"), "why": why, "unsure": unsure}
    units = defaultdict(lambda: Counter())
    existing = {f["album"] for f in files.values() if f["policy"] in ("cull", "describe")}
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
                    "open": c["open"], "confirmed": c["confirmed"], "out": c["out"], "unsure": c["unsure"], "state": state}
    for key, st in status.items():  # a dismantled unit stays listed, empty, until its photos have new places
        if st["status"] == "dismantled" and key not in out:
            out[key] = {"key": key, "name": PurePosixPath(key).name, "kind": "album" if key.startswith("Pictures/Albums/")
                        else "folder", "new": key not in existing, "n": 0, "keep": 0, "archive": 0, "open": 0,
                        "confirmed": 0, "out": 0, "unsure": 0, "state": "dismantled"}
    data = {"photos": photos, "units": out}
    _cache.update(key=(db, stamp), data=data)
    return data


def pending(session) -> dict:
    """What stands between the review and the plan: units still open, photos waiting to be sorted again."""
    try:
        d = placements(session)
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
    members = [p for p in d["photos"].values() if p["unit"] == key]
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
    return data


@router.get("/api/people")
def api_people_get():
    """The People page's state (names, nicknames, merges, faces taken out) as the page last saved it, and the
    names per face in the DB, from which a page with no saved state starts (an earlier import)."""
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
    return {"state": json.loads(path.read_text(encoding="utf-8")) if path.exists() else None, "labels": labels}


@router.put("/api/people")
def api_people_put(state: dict = Body(...), names: dict = Body(...), keys: list = Body(...)):
    """Saved on every change: the page's state, and the names per face, so the next run of `people` uses them.
    Only the faces the page shows (`keys`) change: one without a name now loses its old one, and the faces it
    doesn't show keep theirs."""
    from .people import SCHEMA as PEOPLE_SCHEMA  # noqa: PLC0415

    s = _session()
    tmp = s.dir / "people.json.tmp"
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    tmp.replace(s.dir / "people.json")
    con = connect(s, write=True)
    with con:
        con.executescript(PEOPLE_SCHEMA)
        gone = [k for k in keys if k not in names]
        con.executemany("DELETE FROM name_label WHERE key=?", [(k,) for k in gone])
        con.executemany("INSERT OR REPLACE INTO name_label(key, who, name, nick, owner, source) VALUES (?,?,?,?,?,?)",
                        [(k, v.get("who") or "", v.get("name") or None, v.get("nick") or None, int(bool(v.get("owner"))),
                          f"people-page:{s.id}") for k, v in names.items()])
    con.close()
    return {"saved": len(names)}


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


@router.post("/api/review/unit/reopen")
def api_reopen(key: str = Body(..., embed=True)):
    """Review a confirmed or dismantled unit again: its photos become open, as the sorting left them."""
    s = _session()
    d = placements(s)
    con = connect(s, write=True)
    with con:
        _labels(con, [(p["id"], "place", "") for p in d["photos"].values() if p["unit"] == key and p["state"] == "confirmed"],
                f"review:{s.id}:{_round(s)}:reopen")
        con.execute("DELETE FROM review_unit WHERE key=?", (key,))
    con.close()
    return {"pending": pending(s)}


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
