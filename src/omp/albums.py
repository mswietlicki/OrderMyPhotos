"""S11: albums for unsorted photos (design.md S11, decisions.md P25).

The unsorted photos (folder policy `unsorted`, whatever the folder) become proposals, in this order:
  copy      already in an album (same content hash anywhere in the library's albums): archive this copy
  theme     downloaded images (memes, wallpapers, art, infographics, screenshots): a media folder, never
            an event album, even when they arrived during the trip (user, 2026-11-03)
  add       taken while an album's event or trip was going on: it joins that album, its text pages the
            album's Dokumenty/ folder. A photo taken during an event almost always belongs to it, no
            matter the directory it's in (user, 2026-11-03). Messenger photos (WhatsApp, Messenger,
            Facebook) carry only the day they arrived and can be anything, so each is decided on its own:
            a real photo joins an album it arrived during (or up to 3 days after) when it looks like it, or
            one within 60 days it looks very much like, never on the day alone; the rest goes to thematic
            folders
  category  other reference photos (receipts, documents, labels, ...) to their category album
  create    other events with enough photos to keep: a new album, named by the local LLM in the style
            of the user's own album names
  theme     everything else goes to a thematic folder: a person's folder when a recognized person is the
            main subject, else the thematic folder it looks most like (each folder is the average SigLIP 2
            embedding of its own photos), else Widoki (views), Dom (at home) or Other/Random. No photo stays
            unsorted (user, 2026-11-03)
  relocate  album folders outside Pictures/Albums/<year>/ (the phone gallery's, a wrong year): moved there,
            merged into an album of the same name if there is one (user, 2026-11-03)
Events are split at long gaps, and consecutive days away from home merge into one trip. The library's
albums come from the OneDrive manifest (library.py), so a proposal can name an album the sample lacks.
"""

import json
import math
import random
import re
import time
from pathlib import PurePosixPath
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .captions import SEASONS, part_of_day
from .config import Config
from .db import log_timing
from .db import get_vecs
from .library import Library, manifests_dir
from .profiles import DEFAULT_FOLDERS, album_summary, folder_profiles
from .vlm import VLM

VERSION = "albums@1"
EVENT_GAP = 3 * 3600        # a gap this long between photos starts a new event ...
TRIP_GAP = 30 * 3600        # ... unless both sides are away from home and closer than this (one trip)
AWAY_KM = 30                # this far from home counts as away
CATEGORY_FOLDERS = {        # S3b: where reference photos go (decisions.md open question 8 for the names)
    "documents": "Pictures/Info/Documents", "receipts": "Pictures/Info/Paragony", "labels": "Pictures/Info/Hardware",
    "specs": "Pictures/Info/Hardware", "screens": "Pictures/Info/Ekrany", "home": "Pictures/Info/Dom",
    "to_remember": "Pictures/Info/Notatki", "memes": "Pictures/Other/Meme",
}
# decisions.md P26: full nudity goes to the album's private sibling, "<album> PRIV" (the user's convention)
# These stay with their event when it's a trip (a dinner on holiday belongs to the holiday).
STAY_WITH_TRIP = {"food", "maps_topos", "shopping"}
# Thematic folders (P29): the user's own, plus three fallbacks, so that no photo stays unsorted.
THEME_ROOT = re.compile(r"^Pictures/(?:Info|Other|Places|People)/[^/]+|^Pictures/Albums/[^/0-9][^/]*")
NOT_THEMES = {"Pictures/Albums/Facebook", "Pictures/Albums/Screeshots"}  # where photos came from, not what they show
VIEWS, HOME, RANDOM = "Pictures/Other/Widoki", "Pictures/Other/Dom", "Pictures/Other/Random"
PERSON_FACE = 0.12        # a named person's face this big (share of the frame height) makes them the main subject
THEME_MIN = 0.55          # the most similar thematic folder must be at least this similar (SigLIP 2 cosine) ...
THEME_MARGIN = 0.02       # ... and beat the second by this much
HOME_KM = 2
# Downloaded images (memes, wallpapers, art, infographics, screenshots) never join an event or trip album,
# even when they arrived during the trip (user, 2026-11-03): they go to a media folder (P31).
MEDIA_FOLDERS = {"meme": "Pictures/Other/Meme", "wallpaper": "Pictures/Other/Wallpapers", "art": "Pictures/Other/Art",
                 "infographic": "Pictures/Other/Infographic", "screenshot": "Pictures/Albums/Screeshots"}
MEDIA_THEMES = {"Pictures/Other/Meme", "Pictures/Other/Funny", "Pictures/Other/Art", "Pictures/Other/Ideas",
                "Pictures/Other/Wallpapers", "Pictures/Info/Infographic"}
DOWNLOAD_NAME = re.compile(r"^(Screenshot|wallhaven-|unnamed|images?\(|download)", re.I)
# Messenger photos (P32): sent by others, EXIF stripped, only the day they arrived. WhatsApp's own
# measure on the user's albums: 43 of 53 arrived during the event, 1 three days after, 3 some 40 days after.
# But the day alone says little: 63 % of all WhatsApp images arrive during some album, and the user put
# 1.9 % of those into it. So a messenger photo joins only an album it looks like.
MESSENGER = re.compile(r"/(IMG-\d{8}-WA\d+|received_\d+|FB_IMG_\d+|signal-\d{4}-\d{2}-\d{2}|photo_\d{4}-\d{2}-\d{2}_)[^/]*$"
                       r"|/WhatsApp[^/]*/|/Messenger/", re.I)
LAG_DAYS = 3              # a messenger photo arriving this many days after an album's last day can still be from it ...
LIKE = 0.80               # ... when it looks like the album's photos (best SigLIP 2 cosine; own album median 0.82,
                          #     other albums' p95 0.78) ...
FAR_DAYS, STRONG = 60, 0.88  # ... or within 60 days when it looks very much like one
PERSON_ALBUM_MIN = 50     # a person's undated album (like Pictures/Albums/<name>) is preferred when it's this big

SCHEMA = """
CREATE TABLE IF NOT EXISTS proposal (
  pid INTEGER PRIMARY KEY, run TEXT NOT NULL,
  action TEXT NOT NULL,            -- copy | category | add | create | theme | relocate
  target TEXT,                     -- album folder the photos go to (existing or new)
  name TEXT,                       -- album name without the date (create), for editing in review
  start REAL, end REAL, data TEXT
);
CREATE TABLE IF NOT EXISTS proposal_member (pid INTEGER NOT NULL, id TEXT NOT NULL, PRIMARY KEY (pid, id));
"""

NAME_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["name", "alternatives", "why"],
    "properties": {"name": {"type": "string"}, "alternatives": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
                   "why": {"type": "string"}},
}
NAME_PROMPT = """Name a new photo album for the owner of this photo library, in their own style.
Their existing album names (the date in front is added separately):
{examples}

This event:
- {when}
- {where}
- People recognized in the photos (the owner isn't listed): {people}
- {count} photos. What they show (captions of some of them):
{captions}

Write the name in Polish, 2-6 words, like the examples. The owner usually names an album by the place or
activity and the people who were there ("Gorce z Zosią", "Rowery z Bartkiem i Martą"): add "z <name>" in the right
Polish form for the people recognized above who appear in many of the photos, and no one else. No date, no
quotes, no emoji. Also give up to 3 alternatives and one short sentence on why."""

DECIDE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["kind", "target", "name", "alternatives", "why"],
    "properties": {"kind": {"type": "string", "enum": ["new_album", "album", "folder", "new_folder"]},
                   "target": {"type": "string"}, "name": {"type": "string"},
                   "alternatives": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
                   "why": {"type": "string"}},
}
# The static part first (instructions, folders, the owner's album names), so the server reuses its cache
# across calls; the group comes last.
DECIDE_HEAD = """You sort the photo library of one person (the owner). You get a group of photos the owner took
together, and decide where the whole group belongs. Choose one kind:

- "new_album": the group is an event of its own, something that happened: an outing, a trip, a party, a
  meeting or a work event, a game night, a visit, a celebration, or a shopping trip with a series of photos
  (for example glasses being tried on). Photos taken together at work (a presentation, a meeting, an office
  party, a game evening with colleagues) are an event of their own, never a folder. Even one or two photos are an event when they show people the owner
  doesn't photograph every day, or a place that isn't everyday. "name": the album name in the owner's style
  (their names are below; the date is added in front). Album names name the place or the activity and the
  people: add "z <name>" in the right Polish form for the people recognized in many of the photos, and no
  one else. In Polish, 2-6 words, no date, no quotes. Up to 3 "alternatives".
- "album": the group is part of one of the existing albums listed with the group (its path in "target").
- "folder": the group isn't an event and belongs in one of the owner's thematic folders (its path in
  "target"): things kept for information (receipts, documents, hardware, screens), a pet, views, everyday
  photos. A folder for a person or a pet is offered with the group only when they're in the photos.
- "new_folder": it isn't an event, no folder fits, and more photos like these could follow: "name" is a
  short folder name in the style of the folders below.

The owner's thematic folders:
{folders}

The owner's album names: {examples}

Answer in JSON. "why" is one short sentence. "target" is "" for new_album and new_folder.
"""
DECIDE_GROUP = """
The group:
{group}

Existing albums close in time:
{albums}

Folders offered for this group only:
{extra}{banned}
"""


def km(a, b):
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(b[1] - a[1]) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def home_of(rows, tz):
    """The most frequent ~5 km cell among photos taken in the evening or at night."""
    cells = Counter()
    for r in rows:
        if r["lat"] is None:
            continue
        h = datetime.fromtimestamp(r["taken_ts"], tz).hour
        if h >= 20 or h < 8:
            cells[(round(r["lat"] / 0.05) * 0.05, round(r["lon"] / 0.05) * 0.05)] += 1
    if not cells:
        for r in rows:
            if r["lat"] is not None:
                cells[(round(r["lat"] / 0.05) * 0.05, round(r["lon"] / 0.05) * 0.05)] += 1
    return cells.most_common(1)[0][0] if cells else None


SESSION_GAP = 15 * 60      # photos this close in time, at one place, were taken together: they move as one (P35)
SESSION_KM = 1.0
JOIN_MARGIN = 2 * 3600     # a session this close before or after an album joins it only with people or looks (P35)
PLACE_KM = 5               # sessions within EVENT_GAP are one event at the same place (or without GPS) ...
PEOPLE_GAP = 36 * 3600     # ... and within this when they share a person the owner doesn't photograph every day
RARE_DAYS = 6              # a person seen on at most this many days within +-60 days is "not every day"
PET_ALBUM_ANIMALS = 0.4    # an undated album whose photos are mostly animals is a pet's album ...
PET_LIKE = 0.55            # ... and takes only photos this close to one of its own (DINOv2; own p10 0.61, others <= 0.42)
NEARBY_DAYS = 3            # albums this close in time are offered to an event
MIN_NEW_FOLDER = 3         # a new thematic folder needs this many photos (one-photo folders were noise in s4d)
SMALL_EVENT = 2            # an event this small is an album only with new people in it (the user's rule)


def page_target(key: str) -> str:
    """The album or folder a review page (or an out-label naming it) stands for: the page key without its
    prefix ("new:", "copies:", or the first version's "proposal:<action>:")."""
    if key.startswith("proposal:"):
        return key.split(":", 2)[2]
    for prefix in ("new:", "copies:", "into:"):
        if key.startswith(prefix):
            return key[len(prefix):]
    return key


def centre(rs):
    """The median position of the photos with GPS, or None."""
    pts = [(r["lat"], r["lon"]) for r in rs if r["lat"] is not None]
    if not pts:
        return None
    return sorted(p[0] for p in pts)[len(pts) // 2], sorted(p[1] for p in pts)[len(pts) // 2]


def is_away(rs, home):
    c = centre(rs)
    return bool(home and c) and km(home, c) > AWAY_KM


def sessions_of(rows, burst_of):
    """Photos taken together: at most SESSION_GAP apart and no jump of more than SESSION_KM. A burst or a
    document set that spans two sessions joins them, so it's never split."""
    out = []
    for r in sorted(rows, key=lambda r: r["taken_ts"]):
        if out:
            p = out[-1][-1]
            jump = km((p["lat"], p["lon"]), (r["lat"], r["lon"])) if p["lat"] is not None and r["lat"] is not None else 0
            if r["taken_ts"] - p["taken_ts"] <= SESSION_GAP and jump <= SESSION_KM:
                out[-1].append(r)
                continue
        out.append([r])
    parent = list(range(len(out)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    first = {}
    for k, s in enumerate(out):
        for r in s:
            g = burst_of.get(r["id"])
            if g is not None:
                if g in first:
                    parent[find(k)] = find(first[g])
                else:
                    first[g] = k
    merged = defaultdict(list)
    for k, s in enumerate(out):
        merged[find(k)] += s
    return sorted((sorted(s, key=lambda r: r["taken_ts"]) for s in merged.values()), key=lambda s: s[0]["taken_ts"])


def events_of(sessions, clusters, days_of, home):
    """Sessions in one event: EVENT_GAP apart at the same place, or PEOPLE_GAP apart sharing a person the
    owner doesn't photograph every day (a face is a strong sign the event goes on), or a trip away from home.
    Returns lists of photos."""
    def ppl(rs):
        return set().union(*(clusters.get(r["id"], set()) for r in rs))

    def rare(p, ts):
        d = int(ts // 86400)
        return sum(1 for x in days_of.get(p, ()) if abs(x - d) <= 60) <= RARE_DAYS

    events, last = [], []  # each event's photos, and its last session
    for s in sessions:
        if events:
            e = events[-1]
            gap = s[0]["taken_ts"] - e[-1]["taken_ts"]
            a, b = centre(last[-1]), centre(s)
            same_place = a is None or b is None or km(a, b) <= PLACE_KM
            shared = {p for p in ppl(e) & ppl(s) if rare(p, s[0]["taken_ts"])}
            if (gap <= EVENT_GAP and same_place) or (gap <= PEOPLE_GAP and shared) \
                    or (gap <= TRIP_GAP and is_away(e, home) and is_away(s, home)):
                e += s
                last[-1] = s
                continue
        events.append(list(s))
        last.append(s)
    return events


def run(cfg: Config, con, run_id: str, place_only: bool = False):
    """place_only (the `place` stage, before grouping): only copies and the photos that join an existing
    album, so grouping puts them in the album's bursts and captions describe them with its context (P33).
    No LLM calls."""
    from .geo import Geocoder

    con.executescript(SCHEMA)
    t0 = time.perf_counter()
    tz = ZoneInfo(cfg.tz)
    lib = Library.load(manifests_dir(), cfg.tz)
    rows = [dict(r) for r in con.execute(
        "SELECT p.id, p.taken_ts, p.taken_local, p.lat, p.lon, MIN(f.path) AS path FROM photo p JOIN file f ON f.id=p.id"
        " WHERE f.policy='unsorted' GROUP BY p.id ORDER BY p.taken_ts")]
    # Photos the reviewer took out of an album or a proposal (`o` in the bundle): they skip every rule that
    # would put them in an album and go to a thematic folder, never back where they came from.
    taken_out = {}
    for r in con.execute("SELECT id, value FROM label WHERE kind='out' ORDER BY lid"):
        taken_out[r[0]] = r[1]  # the latest label wins; "" means undone
    taken_out = {i: page_target(v) for i, v in taken_out.items() if v}
    # Photos the reviewer moved (`m`): to an album or a folder they chose, or to a new album they named.
    moved = {}
    for r in con.execute("SELECT id, value FROM label WHERE kind='move' ORDER BY lid"):
        moved[r[0]] = r[1]
    moved = {i: v for i, v in moved.items() if v}
    cull = [dict(r) for r in con.execute(
        "SELECT p.id, p.taken_ts, p.taken_local, p.lat, p.lon, MIN(f.path) AS path FROM photo p JOIN file f ON f.id=p.id"
        " WHERE f.policy='cull' GROUP BY p.id ORDER BY p.taken_ts") if r["id"] in taken_out or r["id"] in moved]
    moved_rows = [r for r in rows + cull if r["id"] in moved]
    out_rows = [r for r in rows + cull if r["id"] in taken_out and r["id"] not in moved]  # an album's own ones too
    rows = [r for r in rows if r["id"] not in taken_out and r["id"] not in moved]
    dec = {r["id"]: dict(r) for r in con.execute("SELECT id, verdict, features FROM decision WHERE run=?", (run_id,))}
    caps = {r["id"]: json.loads(r["data"]) for r in con.execute("SELECT id, data FROM caption")}
    sample_albums = {r[0] for r in con.execute("SELECT DISTINCT album FROM file WHERE policy='cull'")}
    in_sample_album = {r[0] for r in con.execute("SELECT DISTINCT id FROM file WHERE policy='cull'")}
    con.execute("DELETE FROM proposal_member WHERE pid IN (SELECT pid FROM proposal WHERE run=?)", (run_id,))
    con.execute("DELETE FROM proposal WHERE run=?", (run_id,))

    nude = {r[0] for r in con.execute("SELECT id FROM signal WHERE kind='nudity' AND value >= 1")}

    def add(action, target, name, members, data):
        # Full nudity goes to the album's " PRIV" sibling (P26), as its own proposal.
        if target and action in ("add", "create") and any(r["id"] in nude for r in members):
            private = [r for r in members if r["id"] in nude]
            members = [r for r in members if r["id"] not in nude]
            base = target[:-len("/Dokumenty")] if target.endswith("/Dokumenty") else target
            _add(action, f"{base} PRIV", f"{name} PRIV" if name else None, private,
                 {"n": len(private), "why": "full nudity: the album's private sibling"})
            if not members:
                return
        _add(action, target, name, members, data)

    def _add(action, target, name, members, data):
        ts = [r["taken_ts"] for r in members]
        cur = con.execute("INSERT INTO proposal(run, action, target, name, start, end, data) VALUES (?,?,?,?,?,?,?)",
                          (run_id, action, target, name, min(ts), max(ts), json.dumps(data, ensure_ascii=False)))
        for r in members:
            con.execute("INSERT INTO proposal_member(pid, id) VALUES (?,?)", (cur.lastrowid, r["id"]))

    # 0. The reviewer's moves: exactly where they said.
    by_move = defaultdict(list)
    for r in moved_rows:
        by_move[moved[r["id"]]].append(r)
    for target, members in by_move.items():
        if target.startswith("new:"):
            name = target[4:].strip()
            d0 = datetime.fromtimestamp(min(r["taken_ts"] for r in members), tz)
            add("create", f"Pictures/Albums/{d0:%Y}/{d0:%Y-%m-%d} {name}", name, members,
                {"n": len(members), "why": "a new album you named in review", "moved": True})
        else:
            add("add" if re.match(r"^Pictures/Albums/\d{4}/", target) else "theme", target, None, members,
                {"n": len(members), "why": "moved here in review", "moved": True})

    # 1. Already in an album: the same bytes anywhere in the library's albums, or in an album of the sample.
    rest, copies = [], defaultdict(list)
    for r in rows:
        albums = (lib.by_hash.get(r["id"], []) if lib else []) or ([a for a in sample_albums if r["id"] in in_sample_album])
        if albums:
            copies[albums[0]].append(r)
        else:
            rest.append(r)
    for album, members in copies.items():
        add("copy", album, None, members, {"n": len(members), "why": f"already in {album}"})

    # 1b. What isn't a camera photo is decided photo by photo: downloaded images go to a media folder,
    # whenever they arrived (P31); messenger photos are set aside for 2m (P32).
    import numpy as np

    sig = get_vecs(con, "siglip")
    kinds_all = {r["id"]: json.loads(r["data"] or "{}") for r in con.execute("SELECT id, data FROM signal WHERE kind='kind'")}
    camera = {r[0] for r in con.execute("SELECT id FROM photo WHERE make IS NOT NULL OR time_source='exif'")}

    def purpose_of(r):
        """memory | reference | media | junk: the VLM caption's, else SigLIP 2's zero-shot kind. A camera photo
        is media only when both say so, and a download-style name counts when there's no caption."""
        c, k = caps.get(r["id"], {}), kinds_all.get(r["id"], {})
        if r["id"] in camera:  # a camera photo of a screen or a poster is still part of the trip
            if c.get("purpose") == "media":
                return "media" if k.get("purpose") == "media" else "memory"
            return c.get("purpose", "memory")
        if c.get("purpose"):
            return c["purpose"]
        if DOWNLOAD_NAME.match(PurePosixPath(r["path"]).name):
            return "media"
        return k.get("purpose", "memory")

    def media_kind(r):
        top = (kinds_all.get(r["id"], {}).get("top") or [{}])[0].get("key")
        return top if top in MEDIA_FOLDERS else ("meme" if caps.get(r["id"], {}).get("category") == "memes" else "other")

    media, received, kept_rest = defaultdict(list), [], []
    for r in rest:
        r["purpose"] = purpose_of(r)
        if r["purpose"] == "media":
            media[media_kind(r)].append(r)
        elif r["id"] not in camera and MESSENGER.search("/" + r["path"]):
            received.append(r)
        else:
            kept_rest.append(r)
    rest = kept_rest

    # 2m. Messenger photos: a real photo joins the album of an event it arrived during, or up to LAG_DAYS after,
    # when it looks like that album's photos, or an album within FAR_DAYS it looks very much like. An album
    # without processed photos can't be compared, so it takes none (albums are processed before unsorted
    # folders). Everything else (documents too) goes to the category or thematic folders below.
    album_mat, album_ids = defaultdict(list), defaultdict(list)
    for r in con.execute("SELECT f.id, f.album, p.taken_ts FROM file f JOIN photo p ON p.id=f.id WHERE f.policy='cull'"):
        if r["id"] in sig:
            album_mat[r["album"]].append(sig[r["id"]])
            album_ids[r["album"]].append((r["id"], r["taken_ts"]))
    album_mat = {a: np.stack(v) for a, v in album_mat.items()}
    from .db import put_signal

    con.execute("DELETE FROM signal WHERE kind='placed_at'")

    def day(ts):
        return datetime.fromtimestamp(ts, tz).date()

    spans = {a: (day(v["start"]), day(v["end"])) for a, v in lib.albums.items()} if lib else {}
    received_joins, unplaced = defaultdict(list), []
    for r in received:
        best = None
        if r["purpose"] in ("memory", "junk") and r["taken_ts"]:
            d = day(r["taken_ts"])
            for album, (s, e) in spans.items():
                if not (s - timedelta(days=FAR_DAYS) <= d <= e + timedelta(days=FAR_DAYS)):
                    continue
                look = like = None
                if album in album_mat and r["id"] in sig:
                    sims = album_mat[album] @ sig[r["id"]]
                    j = int(np.argmax(sims))
                    look, like = float(sims[j]), album_ids[album][j]
                during = s <= d <= e + timedelta(days=LAG_DAYS)
                if look is not None and ((during and look >= LIKE) or look >= STRONG):
                    score = (during, look)
                    if best is None or score > best[0]:
                        best = (score, album, look, during, like)
        if best:
            _, album, look, during, like = best
            received_joins[album].append((r, look, during))
            if like:  # grouping (S4) puts it at the moment of the album photo it looks most like (P33)
                put_signal(con, r["id"], "placed_at", "place@1", value=like[1],
                           data={"album": album, "like": like[0], "sim": round(look, 4)})
        else:
            unplaced.append(r)
    for album, items in received_joins.items():
        looks = [lk for _, lk, _ in items if lk is not None]
        add("add", album, None, [r for r, _, _ in items], {
            "n": len(items), "received": True,
            "why": f"sent around {album.split('/Albums/')[-1]}" + (f", looks like it ({max(looks):.2f})" if looks else "")})

    # 2. Sessions: photos taken together (at most SESSION_GAP apart, at one place) move as one, and a burst or
    # a document set is never split (P35). Photos the reviewer took out of an album or a proposal go through
    # the same rules, but never back to where they came from.
    from .text import DOCS_FOLDER
    try:
        pages = {r[0] for r in con.execute("SELECT id FROM text WHERE mostly_text=1")}
    except Exception:  # noqa: BLE001 - no text stage yet
        pages = set()
    burst_of = {r[0]: r[1] for r in con.execute(
        "SELECT m.id, g.gid FROM grp g JOIN grp_member m ON m.gid=g.gid WHERE g.run=? AND g.kind IN ('burst', 'docset')",
        (run_id,))}
    for r in out_rows:
        r["purpose"] = purpose_of(r)
        (unplaced if r["id"] not in camera and MESSENGER.search("/" + r["path"]) else rest).append(r)

    def base(t):
        return (t or "").removesuffix(" PRIV").removesuffix(f"/{DOCS_FOLDER}")

    def banned_of(rs):
        return {base(taken_out[r["id"]]) for r in rs if r["id"] in taken_out}

    sessions = sessions_of(rest, burst_of)
    try:  # face clusters (S8) other than the owner's, and the names album names use
        clusters = defaultdict(set)
        for r in con.execute("SELECT f.id, f.person FROM face f JOIN person p ON p.pid=f.person WHERE p.owner=0"):
            clusters[r[0]].add(r[1])
        short = {r[0]: r[1] for r in con.execute("SELECT pid, short FROM person WHERE short IS NOT NULL AND owner=0")}
        days_of = defaultdict(set)
        for r in con.execute("SELECT f.person, p.taken_ts FROM face f JOIN photo p ON p.id=f.id WHERE f.person IS NOT NULL"):
            days_of[r[0]].add(int(r[1] // 86400))
    except Exception:  # noqa: BLE001 - no people stage yet
        clusters, short, days_of = defaultdict(set), {}, defaultdict(set)
    album_pts, album_people = defaultdict(list), defaultdict(set)
    for r in con.execute("SELECT f.album, p.id, p.taken_ts, p.lat, p.lon FROM file f JOIN photo p ON p.id=f.id"
                         " WHERE f.policy='cull'"):
        album_pts[r["album"]].append((r["taken_ts"], (r["lat"], r["lon"]) if r["lat"] is not None else None))
        album_people[r["album"]] |= clusters.get(r["id"], set())

    # 3. A session during an existing album's event joins it: inside the album's time span at its place, or
    # just before or after it (JOIN_MARGIN) only when it shares people with it or looks like it (P25, P35).
    def join_of(s, banned):
        t0s, t1s, c = s[0]["taken_ts"], s[-1]["taken_ts"], centre(s)
        ppl = set().union(*(clusters.get(r["id"], set()) for r in s))
        vs = [sig[r["id"]] for r in s if r["id"] in sig]
        best = None
        for album, a in (lib.albums.items() if lib else ()):
            if album in banned or not (a["start"] - JOIN_MARGIN <= t1s and t0s <= a["end"] + JOIN_MARGIN):
                continue
            inside = a["start"] <= t0s and t1s <= a["end"]
            near = [xy for ts, xy in album_pts.get(album, []) if xy and abs(ts - t0s) <= 3 * 3600]
            place_ok = None if c is None or not near else min(km(c, xy) for xy in near) <= PLACE_KM
            shared = bool(ppl & album_people.get(album, set()))
            look = float((album_mat[album] @ np.stack(vs).T).max()) if album in album_mat and vs else None
            if (inside and place_ok is not False) or shared or (look is not None and look >= LIKE):
                score = (inside, shared, look or 0.0)
                if best is None or score > best[0]:
                    best = (score, album, inside, shared, look)
        return best

    joins, free = defaultdict(list), []
    for s in sessions:
        j = join_of(s, banned_of(s))
        if not j:
            free.append(s)
            continue
        _, album, inside, shared, look = j
        why = ("taken during " if inside else "just before or after ") + album.split("/Albums/")[-1] + \
              (", the same people" if shared else "") + (f", looks like it ({look:.2f})" if look and look >= LIKE else "")
        for r in s:
            joins[f"{album}/{DOCS_FOLDER}" if r["id"] in pages else album].append((r, why))
    for target, items in joins.items():
        add("add", target, None, [r for r, _ in items],
            {"n": len(items), "why": Counter(w for _, w in items).most_common(1)[0][0]})
    if place_only:
        con.commit()
        n = sum(len(v) for v in joins.values()) + sum(len(v) for v in received_joins.values())
        print(f"place: {n} unsorted photos join an existing album, {sum(len(v) for v in copies.values())} are copies;"
              f" {time.perf_counter() - t0:.1f} s")
        return

    # 4. Events: sessions close in time at one place, or sharing people the owner doesn't photograph every
    # day, are one event; days away from home are one trip.
    home = home_of(rows, tz)
    events = events_of(free, clusters, days_of, home)
    geo = Geocoder(cfg.models)
    vlm = VLM(cfg.llm)
    examples = random.Random(7).sample(lib.names, min(60, len(lib.names))) if lib else []
    try:  # who is in each photo (S8): named people, without the owner
        who = defaultdict(set)
        for r in con.execute("SELECT f.id, p.short FROM face f JOIN person p ON p.pid=f.person"
                             " WHERE p.short IS NOT NULL AND p.owner=0"):
            who[r["id"]].add(r["short"])
    except Exception:  # noqa: BLE001 - no people stage yet
        who = {}

    def context(ev, away):
        kept = [r for r in ev if dec.get(r["id"], {}).get("verdict", "keep") == "keep"]
        places, people = Counter(), Counter()
        for r in ev:
            if r["lat"] is not None:
                g = geo.lookup(r["lat"], r["lon"])
                places[f"{g['place']}, {g['region']}, {g['country']}"] += 1
            people.update(who.get(r["id"], ()))
        d0 = datetime.fromtimestamp(ev[0]["taken_ts"], tz)
        data = {"n": len(ev), "keep": len(kept), "away": away, "places": places.most_common(5),
                "people": people.most_common(6),
                "days": sorted({datetime.fromtimestamp(r["taken_ts"], tz).date().isoformat() for r in ev})}
        return kept, d0, data

    def name_for(ev, kept, d0, data):
        d1 = datetime.fromtimestamp(ev[-1]["taken_ts"], tz)
        step = max(1, len(kept) // 30)
        lines = [f"  - {c.get('title_pl')}: {c.get('description_en')}"
                 for c in (caps.get(r["id"]) for r in kept[::step][:30]) if c]
        when = (f"{d0:%A %d %B %Y}" if d0.date() == d1.date() else f"{d0:%d %B} – {d1:%d %B %Y}, {len(data['days'])} days") + \
               f", {SEASONS[d0.month]}, {part_of_day(d0.hour)}" + (", away from home" if data["away"] else "")
        where = "; ".join(f"{p} ({n} photos)" for p, n in data["places"][:4]) or "no GPS"
        people = ", ".join(f"{p} ({n} photos)" for p, n in data["people"]) or "nobody recognized"
        out = vlm.json([{"type": "text", "text": NAME_PROMPT.format(
            examples="\n".join(f"  {n}" for n in examples), when=when, where=where, people=people, count=len(ev),
            captions="\n".join(lines) or "  (no captions)")}], NAME_SCHEMA, max_tokens=300)
        return out["name"].strip().strip('"'), out

    # Thematic folders: the user's own (with a profile, P35), each also described by the average SigLIP 2
    # embedding of its photos for the cheap fallback, plus the default folders.
    by_theme = defaultdict(list)
    for r in con.execute("SELECT id, path FROM file"):
        m = THEME_ROOT.match(r["path"])
        if m and m.group() not in NOT_THEMES and r["id"] in sig:
            by_theme[m.group()].append(r["id"])
    try:
        ment = {r[0]: json.loads(r[1]) for r in con.execute("SELECT album, data FROM album_mention")}
        named = [r[0] for r in con.execute("SELECT DISTINCT name FROM person WHERE name IS NOT NULL")]
    except Exception:  # noqa: BLE001 - no people stage yet
        ment, named = {}, []
    # An undated album named after a person ("Pictures/Albums/<first name>") is that person's folder. Faces
    # fill it, never its look: "photos of someone" would pull in every portrait. First names come from the
    # People folders and from the people the album names mention, diminutives included ("z Zosią" -> Zosia).
    folders = lib.folders if lib else {}
    first = {}  # a first name, lowercased -> that person's People folder ("" if they have none)
    for f in folders:
        parts = f.split("/")
        if len(parts) > 2 and parts[:2] == ["Pictures", "People"]:
            first.setdefault(parts[2].split()[0].lower(), parts[2])
    for ppl in ment.values():
        for b, folder in ppl:
            if folder or b.lower() not in first:
                first[b.lower()] = folder or ""
    person_albums = {}
    for album in set(folders) | set(by_theme):
        m = re.match(r"^Pictures/Albums/([^/0-9][^/]*)$", album)
        if m and m.group(1).lower() in first:
            person_albums[album] = first[m.group(1).lower()]
    persons = {name: f"Pictures/People/{name}" for name in named}  # person -> where photos of them go
    try:
        owner = con.execute("SELECT name FROM person WHERE owner=1 AND name IS NOT NULL").fetchone()
    except Exception:  # noqa: BLE001 - no people stage yet
        owner = None
    owner_folder = f"Pictures/People/{owner[0]}" if owner else None
    for album, folder in person_albums.items():
        if folder in persons and folders.get(album, 0) >= PERSON_ALBUM_MIN:
            persons[folder] = album  # their undated album, when it's a big one
    person_folders = set(persons.values())
    # A pet's album (an undated album whose photos are mostly animals) takes only its own animal: photos
    # close to one of its own (DINOv2). Other animals go to Cats or elsewhere (user, 2026-11-03).
    dino = get_vecs(con, "dino")
    pet_albums = {}
    for album, ids in by_theme.items():
        if re.match(r"^Pictures/Albums/[^/0-9][^/]*$", album) and album not in person_albums:
            tops = [(kinds_all.get(i, {}).get("top") or [{}])[0].get("key") for i in ids]
            vecs = [dino[i] for i in ids if i in dino]
            if tops and sum(t == "animal" for t in tops) / len(tops) >= PET_ALBUM_ANIMALS and vecs:
                pet_albums[album] = np.stack(vecs)
    centroids = {}
    for theme, ids in by_theme.items():
        if theme in person_folders or theme in person_albums or theme in pet_albums \
                or theme.startswith("Pictures/People/") or len(ids) < 3:
            continue
        v = np.mean([sig[i] for i in ids], axis=0)
        centroids[theme] = v / np.linalg.norm(v)
    theme_names = list(centroids)
    theme_mat = np.stack([centroids[k] for k in theme_names]) if theme_names else None
    big_face = defaultdict(list)
    try:
        for r in con.execute("SELECT f.id, f.size, p.name FROM face f JOIN person p ON p.pid=f.person"
                             " WHERE p.name IS NOT NULL AND f.size >= ?", (PERSON_FACE,)):
            big_face[r["id"]].append((r["size"], r["name"]))
    except Exception:  # noqa: BLE001 - no people stage yet
        pass
    kinds = kinds_all

    def route(r):
        """The cheap fallback for one photo without a caption: a thematic folder by its look (P29)."""
        if big_face.get(r["id"]):
            name = max(big_face[r["id"]])[1]
            if name in persons:
                return persons[name], f"{name} is the main subject"
        if theme_mat is not None and r["id"] in sig:
            sims = theme_mat @ sig[r["id"]]
            order = np.argsort(-sims)
            best = float(sims[order[0]])
            second = float(sims[order[1]]) if len(order) > 1 else -1.0
            if best >= THEME_MIN and best - second >= THEME_MARGIN:
                return theme_names[order[0]], f"looks like {theme_names[order[0]].split('/')[-1]} ({best:.2f})"
        top = [k["key"] for k in kinds.get(r["id"], {}).get("top", [])][:1]
        if top and top[0] in ("landscape", "city"):
            return VIEWS, "a view"
        if home and r["lat"] is not None and km(home, (r["lat"], r["lon"])) <= HOME_KM:
            return HOME, "taken at home"
        return RANDOM, "nothing more specific"

    # 5. Each event is decided as a whole by the local LLM, from what it shows, when and where it was taken
    # and who is in it, against profiles of the albums near in time and of the thematic folders (P35).
    folder_ids = defaultdict(list)
    for r in con.execute("SELECT id, path FROM file"):
        m = THEME_ROOT.match(r["path"])
        if m and m.group() not in NOT_THEMES:
            folder_ids[m.group()].append(r["id"])
    profile = folder_profiles(cfg, con, vlm, {f: ids for f, ids in folder_ids.items() if len(ids) >= 3})
    summaries = album_summary(con)
    gated = person_folders | set(person_albums) | set(pet_albums) | {f for f in folder_ids if f.startswith("Pictures/People/")}
    open_folders = sorted(({m.group() for f in folders if (m := THEME_ROOT.match(f))} | set(folder_ids)
                           | set(CATEGORY_FOLDERS.values()) | set(DEFAULT_FOLDERS)) - NOT_THEMES - gated)
    notes = defaultdict(list)
    for r in con.execute("SELECT id, value FROM label WHERE kind='note' ORDER BY lid"):
        if r[1]:
            notes[r[0]].append(r[1])

    def line(f):
        return f"  {f}: {profile.get(f) or f.split('/')[-1]}"

    head = DECIDE_HEAD.format(folders="\n".join(line(f) for f in open_folders),
                              examples="; ".join(examples) or "(none)")

    def describe(rs, away, messenger=False):
        d0, d1 = (datetime.fromtimestamp(rs[i]["taken_ts"], tz) for i in (0, -1))
        if messenger:
            when = f"received by a messenger on {d0:%A %d %B %Y} (no time of day)"
        else:
            when = (f"{d0:%A %d %B %Y}, {d0:%H:%M}–{d1:%H:%M}" if d0.date() == d1.date()
                    else f"{d0:%d %B %H:%M} – {d1:%d %B %Y %H:%M}") + f", {SEASONS[d0.month]}"
        places = Counter()
        for r in rs:
            if r["lat"] is not None:
                g = geo.lookup(r["lat"], r["lon"])
                places[f"{g['place']}, {g['region']}"] += 1
        c = centre(rs)
        dist = f"{km(home, c):.0f} km from home" if home and c else ""
        where = (", ".join(p for p, _ in places.most_common(3)) or "no GPS") + (f" ({dist}{', away' if away else ''})" if dist else "")
        ppl = set().union(*(clusters.get(r["id"], set()) for r in rs))
        names = Counter(short[p] for r in rs for p in clusters.get(r["id"], ()) if p in short)
        lo, hi = int(rs[0]["taken_ts"] // 86400) - 3, int(rs[-1]["taken_ts"] // 86400) + 3
        own_days = {int(r["taken_ts"] // 86400) for r in rs}
        new = [p for p in ppl if not any(lo <= dd <= hi and dd not in own_days for dd in days_of.get(p, ()))]
        people = (f"people: {len(ppl)}" + (f" ({', '.join(f'{n} in {k} photos' for n, k in names.most_common(5))} recognized)" if names else "")
                  + (f"; {len(new)} of them not in the owner's other photos within 3 days" if new else "")) if ppl \
            else "no people recognized (the owner isn't counted)"
        step = max(1, len(rs) // 15)
        lines = []
        for r in rs[::step][:15]:
            cp = caps.get(r["id"])
            if cp:
                lines.append(f"  - {cp.get('title_en')}: {cp.get('description_en', '')[:160]}")
            else:
                top = (kinds_all.get(r["id"], {}).get("top") or [{}])[0].get("key")
                lines.append(f"  - (no caption; looks like: {top or 'unknown'})")
        note = sorted({n for r in rs for n in notes.get(r["id"], ())})
        return d0, (f"- {when}\n- {where}\n- {people}\n- {len(rs)} photos. What they show:\n" + "\n".join(lines)
                    + (f"\n- The owner's notes on these photos: {'; '.join(note)}" if note else ""))

    def novel(rs):
        """New people: someone not in the owner's other photos within 3 days. (Not also "away from home": the
        evening cell that stands for home is the office's on the sample, where the work events are.)"""
        ppl = set().union(*(clusters.get(r["id"], set()) for r in rs))
        lo, hi = int(rs[0]["taken_ts"] // 86400) - 3, int(rs[-1]["taken_ts"] // 86400) + 3
        own = {int(r["taken_ts"] // 86400) for r in rs}
        return any(not any(lo <= d <= hi and d not in own for d in days_of.get(p, ())) for p in ppl)

    def decide(rs, away, banned, messenger=False, more=(), no_album=False):
        """(d0, kind, target, name, why, alternatives) for a group of photos. `more`: new folders other groups
        filled, offered in a second pass to groups whose own new folder stayed too small; then no new one."""
        d0, group = describe(rs, away, messenger)
        extra = list(more)
        ids = {r["id"] for r in rs}
        for f in persons.values():  # a person's folder, when that person is in the photos
            if any(f == persons.get(n) for r in rs for _, n in big_face.get(r["id"], ())):
                extra.append(f)
        for album, mat in pet_albums.items():  # a pet's album, only for its own animal
            if any(i in dino and float((mat @ dino[i]).max()) >= PET_LIKE for i in ids):
                extra.append(album)
        lo, hi = rs[0]["taken_ts"] - NEARBY_DAYS * 86400, rs[-1]["taken_ts"] + NEARBY_DAYS * 86400
        near = [a for a, v in (lib.albums.items() if lib else ()) if v["end"] >= lo and v["start"] <= hi and a not in banned]
        near = sorted(near, key=lambda a: abs(lib.albums[a]["start"] - rs[0]["taken_ts"]))[:8]
        albums_txt = "\n".join(f"  {a}: {datetime.fromtimestamp(lib.albums[a]['start'], tz):%d %b}–"
                               f"{datetime.fromtimestamp(lib.albums[a]['end'], tz):%d %b %Y}"
                               f"{' — ' + summaries[a][:200] if summaries.get(a) else ''}" for a in near) or "  (none)"
        tail = DECIDE_GROUP.format(
            group=group, albums=albums_txt if not messenger else "  (none: a messenger photo joins an album only by its look)",
            extra="\n".join(f"  {f}: photos of the owner themselves (never name the owner in an album name)"
                            if f == owner_folder else line(f) for f in extra) or "  (none)",
            banned=("\nNot these (the owner took the photos out of them): " + ", ".join(sorted(banned))) if banned else "")
        if more:
            tail += "\nA new folder isn't possible for this group: choose a folder above or offered here.\n"
        if no_album:
            tail += ("\nThis group is too small for an album of its own (one or two photos without new people, or"
                     " at home): choose an existing album or a folder.\n")
        allowed_albums = set(near) if not messenger else set()
        allowed_folders = (set(open_folders) | set(extra)) - banned
        try:
            out = vlm.json([{"type": "text", "text": head + tail}], DECIDE_SCHEMA, max_tokens=400, temperature=0)  # repeatable
        except (ValueError, KeyError) as exc:
            return d0, "folder", RANDOM, None, f"no answer ({exc})", []
        kind, target, name = out["kind"], out["target"].strip().rstrip("/"), out["name"].strip().strip('"')
        name = re.sub(r"^\d{4}-\d{2}(-\d{2})?\s*", "", name)  # the date goes in front by itself
        if messenger and kind == "new_album":
            kind = "new_folder" if name else "folder"
        if kind == "album" and target not in allowed_albums:  # not offered (or taken out of): no guessing
            kind, target = "folder", RANDOM
        elif kind == "folder" and target not in allowed_folders:  # a folder named slightly differently
            byname = {f.split("/")[-1].lower(): f for f in allowed_folders}
            target = byname.get(target.split("/")[-1].lower(), RANDOM)
        if kind == "new_album" and no_album:
            kind, target = "folder", RANDOM
        if kind == "new_folder" and more:
            kind, target = "folder", RANDOM
        if kind == "new_folder":
            name = re.sub(r"[\\/:*?\"<>|]+", " ", name).strip()[:40] or "Random"
            existing = {f.split("/")[-1].lower(): f for f in allowed_folders}
            target = existing.get(name.lower(), f"Pictures/Other/{name}")
            if target in banned:
                kind, target = "folder", RANDOM
        if kind == "new_album" and not name:
            kind, target = "folder", RANDOM
        return d0, kind, target, name, out.get("why", ""), out.get("alternatives", [])

    folder_moves = defaultdict(list)  # thematic folder -> [(photo, why)]: one proposal per folder
    counts = Counter()
    # Every event, and every messenger photo with a caption (P32: always to a folder), is decided first;
    # one without a caption goes by its look.
    decisions = []  # [photos, away, banned, messenger, d0, kind, target, name, why, alternatives]
    for e in events:
        away, banned = is_away(e, home), banned_of(e)
        decisions.append([e, away, banned, False, *decide(e, away, banned)])
    for r in unplaced:
        banned = banned_of([r])
        if caps.get(r["id"]):
            decisions.append([[r], False, banned, True, *decide([r], False, banned, messenger=True)])
        else:
            target, why = route(r)
            if target in banned:
                target, why = RANDOM, "nothing more specific"
            folder_moves[target].append((r, f"sent, {why}"))
    # A new folder needs MIN_NEW_FOLDER photos. The groups of smaller ones are decided again, among the
    # existing folders and the new ones that did fill up.
    size = Counter()
    for d in decisions:
        if d[5] == "new_folder":
            size[d[6]] += len(d[0])
    grown = sorted(f for f, n in size.items() if n >= MIN_NEW_FOLDER)
    for d in decisions:
        if d[5] == "new_folder" and d[6] not in grown:
            d[4:] = decide(d[0], d[1], d[2], d[3], more=grown or [RANDOM])
        # One or two photos make an album only with new people away from home (user: "even 1 photo could create
        # an event if it's in a new place with new people and not surrounded by photos with the same people").
        if d[5] == "new_album" and len(d[0]) <= SMALL_EVENT and not novel(d[0]):
            d[4:] = decide(d[0], d[1], d[2], d[3], more=grown, no_album=True)
    for rs, away, banned, messenger, d0, kind, target, name, why, alts in decisions:
        counts[kind] += 1
        if messenger or kind in ("folder", "new_folder"):
            target = RANDOM if messenger and kind in ("album", "new_album") else target
            folder_moves[target] += [(r, f"sent: {why}" if messenger else why) for r in rs]
            continue
        if kind == "new_album":
            target = f"Pictures/Albums/{d0:%Y}/{d0:%Y-%m-%d} {name}"
        # In an album, text pages go to its Dokumenty/, and a receipt or document among other photos to its folder.
        inner, docs = [], defaultdict(list)
        for r in rs:
            c = caps.get(r["id"], {})
            if len(rs) >= 3 and c.get("purpose") == "reference" and c.get("category") in ("receipts", "documents"):
                folder_moves[CATEGORY_FOLDERS[c["category"]]].append((r, f"a {c['category'][:-1]} from {name or target.split('/')[-1]}"))
            elif r["id"] in pages:
                docs[f"{target}/{DOCS_FOLDER}"].append(r)
            else:
                inner.append(r)
        kept, _, data = context(rs, away)
        data |= {"why": why, "alternatives": alts, "n": len(rs)}
        if inner:
            add("create" if kind == "new_album" else "add", target, name if kind == "new_album" else None, inner, data)
        for t_, members in docs.items():
            add("create" if kind == "new_album" else "add", t_, None, members, {"n": len(members), "why": "its text pages"})

    # The downloaded images: the media folder they look most like, else the one for their kind.
    media_names = [n for n in theme_names if n in MEDIA_THEMES]
    media_mat = np.stack([centroids[n] for n in media_names]) if media_names else None
    for mk, members in media.items():
        for r in members:
            target = MEDIA_FOLDERS.get(mk, RANDOM)
            if media_mat is not None and r["id"] in sig:
                sims = media_mat @ sig[r["id"]]
                j = int(np.argmax(sims))
                if float(sims[j]) >= THEME_MIN:
                    target = media_names[j]
            folder_moves[target].append((r, f"downloaded or not a camera photo ({mk})"))

    # One proposal per thematic folder, with each photo's reason.
    for target, items in folder_moves.items():
        new = target not in folders and target not in folder_ids and target not in DEFAULT_FOLDERS
        add("theme", target, None, [r for r, _ in items],
            {"n": len(items), "new_folder": new, "why": Counter(w for _, w in items).most_common(1)[0][0],
             "reasons": {r["id"]: w for r, w in items}})

    # Album folders in the wrong place (the phone gallery's, a wrong year): moved where they belong (P30).
    relocated = []
    if lib:
        for folder, target, n in lib.misplaced_albums():
            members = [r for r in rows if str(PurePosixPath(r["path"]).parent) == folder]
            exists = target in lib.albums
            cur = con.execute("INSERT INTO proposal(run, action, target, name, start, end, data) VALUES (?,?,?,?,?,?,?)",
                              (run_id, "relocate", target, None, None, None, json.dumps(
                                  {"source": folder, "n": n, "merge": exists,
                                   "why": f"{'merge into' if exists else 'move to'} {target}"}, ensure_ascii=False)))
            for r in members:
                con.execute("INSERT OR IGNORE INTO proposal_member(pid, id) VALUES (?,?)", (cur.lastrowid, r["id"]))
            relocated.append((folder, target, n, exists))

    # The namer measured against the user: for photos that join an existing album, the name it would have
    # given that event, next to the user's own.
    naming = []
    for pr in con.execute("SELECT pid, target, data FROM proposal WHERE run=? AND action='add'", (run_id,)).fetchall():
        if pr["target"].endswith(("/Dokumenty", " PRIV")):
            continue
        ids = {r[0] for r in con.execute("SELECT id FROM proposal_member WHERE pid=?", (pr["pid"],))}
        ev = [r for r in rows if r["id"] in ids]
        if len(ev) < 3:
            continue
        kept, d0, data = context(ev, False)
        name, _ = name_for(ev, kept or ev, d0, data)
        real = pr["target"].split("/")[-1][11:]
        naming.append((real, name, data["people"]))
        con.execute("UPDATE proposal SET data=? WHERE pid=?",
                    (json.dumps(json.loads(pr["data"]) | {"would_name": name, "people": data["people"]},
                                ensure_ascii=False), pr["pid"]))
    for real, name, ppl in naming:
        print(f"  naming check: yours {real!r:40s} proposed {name!r:40s} people {ppl}")
    themes = Counter()
    for r in con.execute("SELECT p.target, COUNT(m.id) FROM proposal p JOIN proposal_member m ON m.pid=p.pid"
                         " WHERE p.run=? AND p.action='theme' GROUP BY p.target", (run_id,)):
        themes[r[0]] += r[1]
    print(f"  thematic folders: {dict(themes.most_common())} ({len(profile)} with a profile, {len(pet_albums)} pets,"
          f" {len(persons)} people)")
    print(f"  album folders in the wrong place: {len(relocated)} with {sum(c[2] for c in relocated)} photos,"
          f" {sum(1 for c in relocated if c[3])} of them into an existing album")
    con.commit()
    dt = time.perf_counter() - t0
    log_timing(con, "albums", run_id, len(rows), dt, "cpu + 4090 qwen3.8-27b")
    con.commit()
    print(f"albums: {len(rows)} unsorted photos; {sum(len(v) for v in copies.values())} already in {len(copies)} albums,"
          f" {len(out_rows)} taken out in review, {sum(len(v) for v in media.values())} downloaded, {len(received)} from messengers"
          f" ({sum(len(v) for v in received_joins.values())} join an album they look like),"
          f" {sum(len(v) for v in joins.values())} join {len(joins)} albums by time, {len(sessions)} sessions,"
          f" {len(events)} events -> {dict(counts)}, {len(folder_moves)} folders; home {'found' if home else 'unknown'}; library index {'loaded: ' + str(len(lib.albums)) + ' albums' if lib else 'missing'};"
          f" {vlm.stats()}; {dt:.0f} s")
