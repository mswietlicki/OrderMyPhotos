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
            embedding of its own photos), else Widoki (views) or Other/Random. No photo stays
            unsorted (user, 2026-11-03)
  relocate  album folders outside Pictures/Albums/<year>/ (the phone gallery's, a wrong year): moved there,
            merged into an album of the same name if there is one (user, 2026-11-03)
Events are split at long gaps, and consecutive days away merge into one trip. There's no "home" (P37):
photos taken at home are events like any other. The library's
albums come from the OneDrive manifest (library.py), so a proposal can name an album the sample lacks.
"""

import json
import math
import random
import re
import time
import unicodedata
from pathlib import PurePosixPath
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .captions import SEASONS, part_of_day
from .config import Config
from .db import log_timing
from .db import get_vecs
from .library import library_for
from .people import MIN_SCORE as FACE_SCORE, MIN_SIZE as FACE_SIZE
from .profiles import DEFAULT_FOLDERS, OTHER_YEAR, album_summary, folder_profiles
from .vlm import VLM

VERSION = "albums@1"
EVENT_GAP = 3 * 3600        # a gap this long between photos starts a new event ...
TRIP_GAP = 30 * 3600        # ... unless both sides are away and closer than this (one trip)
AWAY_KM = 30                # this far from the rolling base (where the owner's photos are around then) is away
BASE_DAYS = 30
TRIP_LOOK = 0.6             # days away that look less alike than this, with no one in common, are two trips
CATEGORY_FOLDERS = {        # S3b: where reference photos go (decisions.md open question 8 for the names)
    "documents": "Pictures/Info/Documents", "receipts": "Pictures/Info/Paragony", "labels": "Pictures/Info/Hardware",
    "specs": "Pictures/Info/Hardware", "screens": "Pictures/Info/Ekrany",
    "to_remember": "Pictures/Info/Notatki", "memes": "Pictures/Other/Meme",
}
# decisions.md P26: full nudity goes to the album's private sibling, "<album> PRIV" (the user's convention)
# These stay with their event when it's a trip (a dinner on holiday belongs to the holiday).
STAY_WITH_TRIP = {"food", "maps_topos", "shopping"}
# Thematic folders (P29): the user's own, plus three fallbacks, so that no photo stays unsorted.
THEME_ROOT = re.compile(r"^Pictures/(?:Info|Other|Places|People)/[^/]+|^Pictures/Albums/[^/0-9][^/]*")
NOT_THEMES = {"Pictures/Albums/Facebook", "Pictures/Albums/Screeshots"}  # where photos came from, not what they show
VIEWS, RANDOM = "Pictures/Other/Views", "Pictures/Other/Random"
PERSON_FACE = 0.12        # a named person's face this big (share of the frame height) makes them the main subject
THEME_MIN = 0.55          # the most similar thematic folder must be at least this similar (SigLIP 2 cosine) ...
THEME_MARGIN = 0.02       # ... and beat the second by this much
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
- People and pets recognized in the photos (the owner isn't listed): {people}
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
  party, a game evening with colleagues) are an event of their own, never a folder. So is an occasion, an
  activity or a visit to a place, even in two or three photos without people: a dinner out, a walk to a bridge,
  a bike ride, an injury at training. Even one or two photos are an event when they show people the owner
  doesn't photograph every day, or a place that isn't everyday. "name": the album name in the owner's style
  (their names are below; the date is added in front). Album names name the place or the activity and the
  people: add "z <name>" in the right Polish form for the people recognized in many of the photos, and no
  one else. In Polish, 2-6 words, no date, no quotes. Up to 3 "alternatives".
- "album": the group is part of one of the albums listed with the group (its path in "target"): the same day,
  place, people or activity. Prefer joining such an album over a new one; albums proposed for other photos
  are listed too.
- "folder": the group isn't an event and belongs in one of the owner's thematic folders (its path in
  "target"): things kept for information (receipts, documents, hardware, screens), a pet, views, everyday
  photos. A folder is for photos kept for their subject, not for an occasion. A folder for a person or a pet
  is offered with the group only when they're in the photos. Pictures/Other/Random is only for photos that
  make no sense; a fine photo that belongs nowhere goes to Pictures/Other/<year>.
- "new_folder": it isn't an event, no folder fits, and more photos like these could follow: "name" is a
  short folder name in the style of the folders below, naming a subject (cars, plants, receipts), never an
  occasion (a dinner, a trip: those are events) and never "random" or "other" (there's Random for that).

The owner's thematic folders:
{folders}

The owner's album names: {examples}

Answer in JSON. "why" is one short sentence. "target" is "" for new_album and new_folder.
"""
SUMMARY_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["description", "name"],
                  "properties": {"description": {"type": "string"}, "name": {"type": "string"}}}
# A new folder named like a starter folder in Polish is that folder (s5: "Koty" next to Cats).
SAME_FOLDER = {"koty": "Pictures/Other/Cats", "kot": "Pictures/Other/Cats", "widoki": "Pictures/Other/Views",
               "jedzenie": "Pictures/Other/Food", "zwierzęta": "Pictures/Other/Animals", "miasto": "Pictures/Other/City",
               "natura": "Pictures/Other/Nature", "przyroda": "Pictures/Other/Nature", "rośliny": "Pictures/Other/Nature",
               "dom": "Pictures/Other/Home", "samochody": "Pictures/Other/Vehicles", "pojazdy": "Pictures/Other/Vehicles",
               "zakupy": "Pictures/Other/Shopping", "ubrania": "Pictures/Other/Clothes", "dokumenty": "Pictures/Info/Documents",
               "rachunki": "Pictures/Info/Paragony", "paragony": "Pictures/Info/Paragony", "notatki": "Pictures/Info/Notes",
               "mapy": "Pictures/Info/Maps", "ogłoszenia": "Pictures/Info/Notices", "ekrany": "Pictures/Info/Screens",
               "sprzęt": "Pictures/Info/Hardware", "elektronika": "Pictures/Info/Hardware", "sztuka": "Pictures/Other/Art",
               "grafika": "Pictures/Other/Art", "memy": "Pictures/Other/Meme", "zrzuty ekranu": "Pictures/Albums/Screeshots"}
MSG_ALIKE = 0.75           # messenger photos of one day that look this alike were sent together: one group
SAME_LOOK = 0.85           # new folders whose photos look this alike (mean SigLIP 2) are one folder
LOOKALIKE = 0.8            # groups within a day whose photos look this alike (mean SigLIP 2) belong together
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


SESSION_GAP = 15 * 60      # photos this close in time, at one place, were taken together: they move as one (P35)
SESSION_KM = 1.0
JOIN_MARGIN = 2 * 3600     # a session this close before or after an album joins it only with people or looks (P35)
CROWD = 4                  # a session showing this many people, twice the album's around it, at a place the
                           # backgrounds say is another, is an event of its own, not the album's by time (P41)
PLACE_KM = 5               # sessions within EVENT_GAP are one event at the same place (or without GPS) ...
PEOPLE_GAP = 36 * 3600     # ... and within this when they share a person the owner doesn't photograph every day
RARE_DAYS = 6              # a person seen on at most this many days within +-60 days is "not every day"
PET_ALBUM_ANIMALS = 0.4    # an undated album whose photos are mostly animals is a pet's album ...
PET_LIKE = 0.55            # ... and takes only photos this close to one of its own (DINOv2; own p10 0.61, others <= 0.42)
NEARBY_DAYS = 3            # albums this close in time are offered to an event
RANDOM_NAME = re.compile(r"losow|random", re.I)  # "Zdjęcia losowe" is Random by another name ...
OTHER_NAME = re.compile(r"\binne\b|r[óo]żne|\bmisc|\bother\b|pozosta", re.I)  # ... and "Inne" is Other/<year>
FIT_MIN = 0.4             # a photo in a folder that fits its refined description less surely than this is decided again
PET_DAYS = 3               # the same animal on this many other days is probably the owner's own pet
NO_GPS_GAP = 3600          # sessions without a position join only this close in time
MIN_NEW_FOLDER = 3         # a new thematic folder needs this many photos (one-photo folders were noise in s4d)
FACE_SAME = 0.35           # faces this alike (InsightFace cosine) are one person (s5: someone else 0.06-0.15)
SMALL_EVENT = 2            # an event this small is an album only with new people in it (the user's rule)


class Base:
    """Where the owner is around a moment: the median of their daily positions within BASE_DAYS of it (P37).
    It follows a move, so there's no fixed home; it only tells a trip from everyday places. Each day counts
    once, so a trip's busy days don't pull the base along."""

    def __init__(self, rows):
        import numpy as np

        days = defaultdict(list)
        for r in rows:
            if r["lat"] is not None and r["taken_ts"]:
                days[int(r["taken_ts"] // 86400)].append((r["lat"], r["lon"]))
        self.days = np.array(sorted(days))
        self.ll = np.array([np.median(days[d], axis=0) for d in sorted(days)]).reshape(-1, 2)

    def at(self, ts):
        import numpy as np

        d = int(ts // 86400)
        lo, hi = np.searchsorted(self.days, [d - BASE_DAYS, d + BASE_DAYS + 1])
        if hi - lo < 3:
            return None
        return tuple(np.median(self.ll[lo:hi], axis=0))


def near_word(a: str, b: str) -> bool:
    """One letter apart (a typo: "Coty" for "Koty")."""
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) <= 1
    s, l_ = (a, b) if len(a) < len(b) else (b, a)
    return any(l_[:k] + l_[k + 1:] == s for k in range(len(l_)))


def album_key(path: str) -> str:
    """An album's name as an answer may write it: without its folders, in any case or Unicode form."""
    name = unicodedata.normalize("NFC", path.strip().rstrip("/").split("/")[-1]).lower()
    return re.sub(r"\s+", " ", re.sub(r"\s*\(proposed\)$", "", name)).strip()


def other_of(ts: float) -> str:
    """The folder for a fine photo of that year that belongs nowhere else (user: "create an Other in {Year}
    and put them there; leave Random for truly random photos")."""
    return OTHER_YEAR.replace("<year>", str(datetime.fromtimestamp(ts).year))


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


def is_away(rs, base):
    """Far from where the owner's photos are in the weeks around (the rolling base)."""
    c = centre(rs)
    b = base.at(rs[0]["taken_ts"]) if base and c else None
    return bool(b) and km(b, c) > AWAY_KM


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


def events_of(sessions, clusters, days_of, base, sig=None):
    """Sessions in one event: EVENT_GAP apart at the same place, or PEOPLE_GAP apart sharing a person the
    owner doesn't photograph every day (a face is a strong sign the event goes on), or a trip away: TRIP_GAP
    apart, both away, wherever the road goes ("a euro trip where you drive all the time"), unless the
    people and the subject both change completely (no one in common and photos that look nothing alike).
    Returns lists of photos."""
    import numpy as np

    def look(a, b):  # how alike two groups of photos are: their mean SigLIP 2 embeddings
        va = [sig[r["id"]] for r in a if sig and r["id"] in sig]
        vb = [sig[r["id"]] for r in b if sig and r["id"] in sig]
        if not va or not vb:
            return 1.0
        ma, mb = np.mean(va, axis=0), np.mean(vb, axis=0)
        return float(ma @ mb / (np.linalg.norm(ma) * np.linalg.norm(mb)))

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
            same_place = km(a, b) <= PLACE_KM if a and b else gap <= NO_GPS_GAP
            shared = {p for p in ppl(e) & ppl(s) if rare(p, s[0]["taken_ts"])}
            trip = gap <= TRIP_GAP and is_away(e, base) and is_away(s, base)
            if trip and ppl(e) and ppl(s) and not ppl(e) & ppl(s) and look(e, s) < TRIP_LOOK:
                trip = False  # other people and another subject: another trip
            if (gap <= EVENT_GAP and same_place) or (gap <= PEOPLE_GAP and shared) or trip:
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
    lib = library_for(cfg, con)
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
    # Who is in each photo (S8): the faces big and sure enough to be clustered, without the owner's. A group
    # that shows people, none of whom is in an album's photos, is someone else's event however alike it
    # looks, and doesn't join that album (P39; user, s5: "Different person on the photos").
    def unit(b):
        v = np.frombuffer(b, dtype=np.float32 if len(b) == 2048 else np.float16).astype(np.float32)
        return v / np.linalg.norm(v)

    face_vecs = defaultdict(list)
    try:
        try:  # the owner's faces (S8; `people` runs before `place` so the owner is known here)
            mine = [unit(r[0]) for r in con.execute("SELECT f.emb FROM face f JOIN person p ON p.pid=f.person"
                                                    " WHERE p.owner=1 AND f.emb IS NOT NULL")]
        except Exception:  # noqa: BLE001 - no people stage yet: every face counts
            mine = []
        owner_mat = np.stack(mine) if mine else None
        for r in con.execute("SELECT id, emb FROM face WHERE emb IS NOT NULL AND size >= ? AND score >= ?",
                             (FACE_SIZE, FACE_SCORE)):
            v = unit(r[1])
            if owner_mat is None or float((owner_mat @ v).max()) < FACE_SAME:  # not the owner, clustered or not
                face_vecs[r[0]].append(v)
    except Exception:  # noqa: BLE001 - no faces stage yet
        pass

    def faces_of(rs):
        vs = [v for r in rs for v in face_vecs.get(r["id"], ())]
        return np.stack(vs) if vs else None

    def strangers(rs, faces):
        """The photos show people, and none of them is among `faces` (a destination's)."""
        mine = faces_of(rs)
        return mine is not None and faces is not None and float((mine @ faces.T).max()) < FACE_SAME

    album_mat, album_ids, album_faces = defaultdict(list), defaultdict(list), defaultdict(list)
    for r in con.execute("SELECT f.id, f.album, p.taken_ts FROM file f JOIN photo p ON p.id=f.id WHERE f.policy='cull'"):
        album_faces[r["album"]] += face_vecs.get(r["id"], [])
        if r["id"] in sig:
            album_mat[r["album"]].append(sig[r["id"]])
            album_ids[r["album"]].append((r["id"], r["taken_ts"]))
    album_mat = {a: np.stack(v) for a, v in album_mat.items()}
    album_faces = {a: np.stack(v) for a, v in album_faces.items() if v}
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
                if look is not None and ((during and look >= LIKE) or look >= STRONG) \
                        and not strangers([r], album_faces.get(album)):
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
    album_pts, album_people, album_rows = defaultdict(list), defaultdict(set), defaultdict(list)
    for r in con.execute("SELECT f.album, p.id, p.taken_ts, p.lat, p.lon FROM file f JOIN photo p ON p.id=f.id"
                         " WHERE f.policy='cull'"):
        album_pts[r["album"]].append((r["taken_ts"], (r["lat"], r["lon"]) if r["lat"] is not None else None))
        album_rows[r["album"]].append({"id": r["id"], "taken_ts": r["taken_ts"]})
        album_people[r["album"]] |= clusters.get(r["id"], set())

    # 3. A session during an existing album's event joins it: inside the album's time span at its place, or
    # just before or after it (JOIN_MARGIN) only when it shares people with it or looks like it (P25, P35).
    # A session of people none of whom is in the album is another event, however close in time (P41; user, s6:
    # a birthday party inside a week-long album "clearly deserves its own album"). So is a crowd the album's
    # photos around it don't have, at a place their backgrounds say is another (user: a new place doesn't make
    # an album, but with other things, like the number of people, it may).
    vlm = VLM(cfg.llm)

    def backgrounds(xs, n=6):
        ss = [caps[r["id"]]["setting"] for r in sorted(xs, key=lambda r: r["taken_ts"])
              if caps.get(r["id"], {}).get("setting") not in (None, "", "unknown")]
        return ss[::max(1, len(ss) // n)][:n]

    def same_place(xs, ys):
        """P(yes) that two groups of photos were taken at one place, from what their backgrounds show."""
        a, b = backgrounds(xs), backgrounds(ys)
        if not a or not b:
            return None
        q = ("The backgrounds of two groups of photos, as described:\nGroup A:\n" + "\n".join(f"  - {x}" for x in a)
             + "\nGroup B:\n" + "\n".join(f"  - {x}" for x in b) + "\n\nWere both taken at the same place (the same"
             " room, home, venue or spot)? Answer with one word: yes or no.")
        ans = vlm.choice([{"type": "text", "text": q}], ["yes", "no"])
        return ans.get("yes", 0.5) if ans else None

    def people_in(xs):
        return max((caps.get(r["id"], {}).get("people_count") or 0 for r in xs), default=0)

    elsewhere = []

    def crowd_elsewhere(s, album):
        n = people_in(s)
        if n < CROWD:
            return False
        t = s[0]["taken_ts"]
        around = [r for r in album_rows.get(album, []) if abs(r["taken_ts"] - t) <= 12 * 3600] or \
            sorted(album_rows.get(album, []), key=lambda r: abs(r["taken_ts"] - t))[:10]
        if n < 2 * max(1, people_in(around)):
            return False
        p = same_place(s, around)
        if p is None or p >= 0.5:
            return False
        elsewhere.append(f"{datetime.fromtimestamp(t, tz):%d %b %H:%M} ({len(s)} photos, {n} people,"
                         f" not at {album.split('/')[-1]}'s place: {p:.2f})")
        return True

    def join_of(s, banned):
        t0s, t1s, c = s[0]["taken_ts"], s[-1]["taken_ts"], centre(s)
        ppl = set().union(*(clusters.get(r["id"], set()) for r in s))
        vs = [sig[r["id"]] for r in s if r["id"] in sig]
        best = None
        for album, a in (lib.albums.items() if lib else ()):
            if album in banned or not (a["start"] - JOIN_MARGIN <= t1s and t0s <= a["end"] + JOIN_MARGIN):
                continue
            if crowd_elsewhere(s, album) or strangers(s, album_faces.get(album)):
                continue
            inside = a["start"] <= t0s and t1s <= a["end"]
            near = [xy for ts, xy in album_pts.get(album, []) if xy and abs(ts - t0s) <= 3 * 3600]
            place_ok = None if c is None or not near else min(km(c, xy) for xy in near) <= PLACE_KM
            shared = bool(ppl & album_people.get(album, set()))
            look = float((album_mat[album] @ np.stack(vs).T).max()) if album in album_mat and vs else None
            alike = look is not None and look >= LIKE
            if (inside and place_ok is not False) or shared or alike:
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
              f" {len(elsewhere)} crowds at another place {elsewhere}; {time.perf_counter() - t0:.1f} s")
        return

    # 4. Events: sessions close in time at one place, or sharing people the owner doesn't photograph every
    # day, are one event; days away are one trip.
    base = Base(rows + out_rows)
    events = events_of(free, clusters, days_of, base, sig)
    geo = Geocoder(cfg.models)
    examples = random.Random(7).sample(lib.names, min(60, len(lib.names))) if lib else []
    try:  # who is in each photo (S8): named people, without the owner
        who = defaultdict(set)
        for r in con.execute("SELECT f.id, p.short FROM face f JOIN person p ON p.pid=f.person"
                             " WHERE p.short IS NOT NULL AND p.owner=0"):
            who[r["id"]].add(r["short"])
    except Exception:  # noqa: BLE001 - no people stage yet
        who = {}
    try:  # and the pets the user named (P40): "<river> z Olą i Kotem"
        for r in con.execute("SELECT m.id, p.short FROM pet_member m JOIN pet p ON p.pid=m.pid WHERE p.short IS NOT NULL"):
            who.setdefault(r["id"], set()).add(r["short"])
    except Exception:  # noqa: BLE001 - no pets yet
        pass

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
        lines = [f"  - {c.get('title_en')}: {c.get('description_en')}"  # S11 reads English only (user)
                 for c in (caps.get(r["id"]) for r in kept[::step][:30]) if c]
        when = (f"{d0:%A %d %B %Y}" if d0.date() == d1.date() else f"{d0:%d %B} – {d1:%d %B %Y}, {len(data['days'])} days") + \
               f", {SEASONS[d0.month]}, {part_of_day(d0.hour)}" + (", away" if data["away"] else "")
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
    taken = {r[0]: r[1] for r in con.execute("SELECT id, taken_ts FROM photo")}
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
        return other_of(r["taken_ts"]), "nothing more specific"

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
                           | set(DEFAULT_FOLDERS)) - NOT_THEMES - gated)
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
        messenger = messenger or all(r["id"] not in camera and MESSENGER.search("/" + r["path"]) for r in rs)
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
        where = (", ".join(p for p, _ in places.most_common(3)) or "no GPS") + \
            (" (away from where the owner's photos are in the weeks around)" if away else "")
        bg = backgrounds(rs, 4)  # the place without GPS, or a finer one than GPS (P41)
        if bg:
            where += "; the backgrounds: " + "; ".join(bg)
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
        days = pet_days(rs)
        # The pet the user named, when most of these photos show it (s6: said for every group the cat was in, it
        # took 27 photos out of the albums of those days)
        named = [(n, k) for n, k in Counter(pet_name[r["id"]] for r in rs if r["id"] in pet_name).most_common(1)
                 if 2 * k >= len(rs)]
        pet = (f"\n- The owner's own pet {named[0][0]} is in {named[0][1]} of these photos. Photos of just the pet,"
               f" not of an event or an album of these days, go to its own folder (a new_folder named \"{named[0][0]}\"),"
               " not Cats") if named else \
            (f"\n- The same animal is in the owner's photos on {days} other days: probably the owner's own pet,"
             " which gets a folder of its own (a new_folder named after it, like \"Czarny kot\"), not Cats") \
            if days >= PET_DAYS else ""
        return d0, (f"- {when}\n- {where}\n- {people}\n- {len(rs)} photos. What they show:\n" + "\n".join(lines)
                    + (f"\n- The owner's notes on these photos: {'; '.join(note)}" if note else "") + pet)

    def novel(rs):
        """New people: someone not in the owner's other photos within 3 days."""
        ppl = set().union(*(clusters.get(r["id"], set()) for r in rs))
        lo, hi = int(rs[0]["taken_ts"] // 86400) - 3, int(rs[-1]["taken_ts"] // 86400) + 3
        own = {int(r["taken_ts"] // 86400) for r in rs}
        return any(not any(lo <= d <= hi and d not in own for d in days_of.get(p, ())) for p in ppl)

    # An animal that comes back day after day is the owner's own pet (P37): photos of animals, by the
    # zero-shot kind or the caption, and the days the same one (DINOv2, as for pet albums) is seen on.
    animal_word = re.compile(r"\b(cat|kitten|dog|puppy|kot|kotek|pies|piesek)\b", re.I)
    animals = [i for i in dino if (kinds_all.get(i, {}).get("top") or [{}])[0].get("key") == "animal"
               or animal_word.search(f"{caps.get(i, {}).get('title_en', '')} {caps.get(i, {}).get('description_en', '')}")]
    animal_mat = np.stack([dino[i] for i in animals]) if animals else None
    animal_day = np.array([int((taken.get(i) or 0) // 86400) for i in animals])
    animal_set = set(animals)
    try:  # the pets the user named on the People page (P40)
        pet_name = {r[0]: r[1] or r[2] for r in con.execute(
            "SELECT m.id, p.short, p.name FROM pet_member m JOIN pet p ON p.pid=m.pid WHERE p.source='user'"
            " AND p.name IS NOT NULL")}
    except Exception:  # noqa: BLE001 - no pets named
        pet_name = {}
    own_pet, pet_folder_names = set(pet_name), {n.lower() for n in pet_name.values()}

    def pet_days(rs):
        own = {int(r["taken_ts"] // 86400) for r in rs}
        best = 0
        for r in rs:
            if r["id"] in animal_set and animal_mat is not None:
                close = animal_day[(animal_mat @ dino[r["id"]]) >= PET_LIKE]
                best = max(best, len(set(close.tolist()) - own))
        return best

    unoffered = Counter()  # albums an answer named that weren't offered to it (P41, s6: 19 photos lost this way)

    def decide(rs, away, banned, messenger=False, more=(), no_album=False, proposed=None, refined=None, hint=""):
        """(d0, kind, target, name, why, alternatives) for a group of photos; a new album's target is its
        path. `more`: new folders other groups filled, offered in a second pass to groups whose own new folder
        stayed too small; then no new one. Later passes (P38) add `proposed` (new albums for other photos,
        with what they hold), `refined` (what the folders hold so far) and a `hint` (where this group is now
        and where the photos that look like it went)."""
        d0, group = describe(rs, away, messenger)
        other = other_of(rs[0]["taken_ts"])
        extra = list(more)
        ids = {r["id"] for r in rs}
        for f in persons.values():  # a person's folder, when that person is in the photos
            if any(f == persons.get(n) for r in rs for _, n in big_face.get(r["id"], ())):
                extra.append(f)
        for album, mat in pet_albums.items():  # a pet's album, only for its own animal
            if any(i in dino and float((mat @ dino[i]).max()) >= PET_LIKE for i in ids):
                extra.append(album)
        lo, hi = rs[0]["taken_ts"] - NEARBY_DAYS * 86400, rs[-1]["taken_ts"] + NEARBY_DAYS * 86400
        near = [a for a, v in (lib.albums.items() if lib else ()) if v["end"] >= lo and v["start"] <= hi and a not in banned
                and not strangers(rs, album_faces.get(a))]
        near = sorted(near, key=lambda a: abs(lib.albums[a]["start"] - rs[0]["taken_ts"]))[:8]
        albums_txt = "\n".join(f"  {a}: {datetime.fromtimestamp(lib.albums[a]['start'], tz):%d %b}–"
                               f"{datetime.fromtimestamp(lib.albums[a]['end'], tz):%d %b %Y}"
                               f"{' — ' + summaries[a][:200] if summaries.get(a) else ''}" for a in near)
        new_near = {a: desc for a, desc in (proposed or {}).items()
                    if a not in banned and abs(album_day(a) - rs[0]["taken_ts"]) <= NEARBY_DAYS * 86400}
        if new_near:
            albums_txt += ("\n" if albums_txt else "") + "\n".join(f"  {a} (proposed): {desc[:220]}" for a, desc in new_near.items())
        albums_txt = albums_txt or "  (none)"
        tail = DECIDE_GROUP.format(
            group=group, albums=albums_txt if not messenger else "  (none: a messenger photo joins an album only by its look)",
            extra="\n".join(f"  {f}: photos of the owner themselves (never name the owner in an album name)"
                            if f == owner_folder else line(f) for f in extra) or "  (none)",
            banned=("\nNot these (the owner took the photos out of them): " + ", ".join(sorted(banned))) if banned else "")
        if more:
            tail += "\nA new folder isn't possible for this group: choose a folder above or offered here.\n"
        if no_album:
            why_not = no_album if isinstance(no_album, str) else "This group is too small for an album of its own" \
                " (one or two photos without new people)"
            tail += f"\n{why_not}: choose an existing album or a folder.\n"
        if refined:
            tail += "\nWhat the folders hold so far (from the photos sorted into them):\n" + \
                "\n".join(f"  {f}: {desc[:200]}" for f, desc in refined.items()) + "\n"
        if hint:
            tail += "\n" + hint + "\n"
        allowed_albums = (set(near) | set(new_near)) if not messenger else set()
        allowed_folders = (set(open_folders) | set(extra) | {other}) - banned
        try:
            out = vlm.json([{"type": "text", "text": head + tail}], DECIDE_SCHEMA, max_tokens=400, temperature=0)  # repeatable
        except (ValueError, KeyError) as exc:
            return d0, "folder", other, None, f"no answer ({exc})", []
        kind, target, name = out["kind"], out["target"].strip().rstrip("/"), out["name"].strip().strip('"')
        if re.match(r"^Pictures/Other/(<year>|\d{4})$", target):
            target = other
        name = re.sub(r"^\d{4}-\d{2}(-\d{2})?\s*", "", name)  # the date goes in front by itself
        if messenger and kind == "new_album":
            kind = "new_folder" if name else "folder"
        if kind == "album" and target not in allowed_albums | set(new_near):  # the name, written a little differently
            offered = allowed_albums | set(new_near)
            for key in (album_key, lambda a: re.sub(r"^\d{4}-\d{2}-\d{2} ", "", album_key(a))):  # then without the date
                same = [a for a in offered if key(a) == key(target)]
                if len(same) == 1:
                    target = same[0]
                    break
        if kind == "album" and target in new_near:  # joins an album proposed for other photos
            return d0, "new_album", target, target.split("/")[-1][11:], out.get("why", ""), []
        if kind == "album" and target not in allowed_albums:  # not offered (or taken out of): no guessing
            unoffered[target] += 1
            kind, target = "folder", other
        elif kind == "folder" and target not in allowed_folders:  # a folder named slightly differently
            byname = {f.split("/")[-1].lower(): f for f in allowed_folders}
            target = byname.get(target.split("/")[-1].lower(), other)
        if kind == "new_album" and no_album:
            kind, target = "folder", other
        if kind == "new_folder" and more:
            kind, target = "folder", other
        if kind == "new_folder":
            name = re.sub(r"[\\/:*?\"<>|]+", " ", name).strip()[:40] or "Random"
            existing = {f.split("/")[-1].lower(): f for f in allowed_folders}
            target = existing.get(name.lower()) or SAME_FOLDER.get(name.lower()) or next(
                (f for k, f in SAME_FOLDER.items() if len(k) >= 4 and near_word(k, name.lower())), None) or f"Pictures/Other/{name}"
            if RANDOM_NAME.search(name):
                target = RANDOM
            elif OTHER_NAME.search(name):
                target = other
            if target in banned:
                kind, target = "folder", other
        if kind == "new_album" and not name:
            kind, target = "folder", other
        if kind == "new_album":
            target = f"Pictures/Albums/{d0:%Y}/{d0:%Y-%m-%d} {name}"
        return d0, kind, target, name, out.get("why", ""), out.get("alternatives", [])

    def album_day(path):
        m = re.search(r"/(\d{4}-\d{2}-\d{2})[^/]*$", path)
        return datetime.strptime(m[1], "%Y-%m-%d").replace(tzinfo=tz).timestamp() + 43200 if m else 0

    def summary_of(target, rs):
        """What a destination holds, from its photos' captions, and for a new album a better name (P38)."""
        caps_ = [caps[r["id"]] for r in rs if r["id"] in caps]
        step = max(1, len(caps_) // 20)
        lines = "\n".join(f"- {c.get('title_en')}: {c.get('description_en', '')[:140]}" for c in caps_[::step][:20])
        if not lines:
            return "", ""
        new = target.startswith("Pictures/Albums/") and re.search(r"/\d{4}-\d{2}-\d{2} ", target)
        prompt = (f"Photos the owner's library would put in \"{target}\":\n{lines}\n\nIn one sentence, say " +
                  ("what these photos are (the event), concretely." if new else "what most of these photos show (what"
                   " the folder collects), concretely; leave out what only a few of them show.") +
                  (" Also give a better album name in the owner's style, in Polish, 2-6 words, naming the place or"
                   " the activity and the people (examples: " + "; ".join(examples[:30]) + "), or the same name if"
                   " it fits." if new else ' Set "name" to "".'))
        try:
            out = vlm.json([{"type": "text", "text": prompt}], SUMMARY_SCHEMA, max_tokens=200, temperature=0)
        except (ValueError, KeyError):
            return "", ""
        return out["description"].strip(), re.sub(r"^\d{4}-\d{2}(-\d{2})?\s*", "", out["name"].strip().strip('"'))

    def mean_vec(rs):
        vs = [sig[r["id"]] for r in rs if r["id"] in sig]
        if not vs:
            return None
        v = np.mean(vs, axis=0)
        return v / np.linalg.norm(v)

    def one_day_events(decisions):
        """A folder is for a subject that comes back; a new folder whose photos all come from one day is an
        event (s5: "Obiad na zewnątrz", "Most kolejowy" should have been albums). Messenger photos stay."""
        days = defaultdict(set)
        for d in decisions:
            if d[5] == "new_folder" and not d[3]:
                days[d[6]] |= {datetime.fromtimestamp(r["taken_ts"], tz).date() for r in d[0]}
        for d in decisions:
            if d[5] == "new_folder" and not d[3] and len(days[d[6]]) == 1:
                d0 = datetime.fromtimestamp(d[0][0]["taken_ts"], tz)
                name = d[6].split("/")[-1]
                d[4:] = [d0, "new_album", f"Pictures/Albums/{d0:%Y}/{d0:%Y-%m-%d} {name}", name,
                         d[8] + " (one day: an event)", []]

    def pet_moments(decisions, grown):
        """The owner's own pet at home isn't an event (s5: "Kot na blacie", "Koty na drzewie" became albums): a
        new album at home that is mostly the pet the user named, or an animal seen on other days too, with
        nobody else in it, is decided again without an album. A trip, or animals seen only that day (a zoo),
        stays an album."""
        for d in decisions:
            ours = sum(r["id"] in own_pet for r in d[0])  # the pet the user named on the People page (P40)
            if d[5] == "new_album" and not d[1] and faces_of(d[0]) is None and (2 * ours >= len(d[0])
                    or 2 * sum(r["id"] in animal_set for r in d[0]) >= len(d[0]) and pet_days(d[0]) >= PET_DAYS):
                d[4:] = decide(d[0], d[1], d[2], d[3], more=grown, no_album="These photos are of the owner's own pet,"
                               " which isn't an event")

    def small_albums(decisions, grown):
        """One or two photos make an album only with new people, counted per album once every pass is done (s6:
        one photo was left in "Czarny kot w domu" after the rest of it went elsewhere)."""
        held = defaultdict(list)
        for d in decisions:
            if d[5] == "new_album":
                held[d[6]] += d[0]
        for d in decisions:
            if d[5] == "new_album" and len(held[d[6]]) <= SMALL_EVENT and not novel(held[d[6]]):
                d[4:] = decide(d[0], d[1], d[2], d[3], more=grown, no_album=True)

    def pet_folder_only(decisions, grown):
        """The folder of the pet the user named holds photos of that pet, after every pass (s6: accidental shots,
        a harness and its receipt went there); what doesn't show it, burst by burst, is decided again."""
        for d in list(decisions):
            if d[5] not in ("folder", "new_folder") or d[6].split("/")[-1].lower() not in pet_folder_names:
                continue
            units = defaultdict(list)
            for r in d[0]:
                units[burst_of.get(r["id"], r["id"])].append(r)
            out_ = [u for u in units.values() if not any(r["id"] in own_pet for r in u)]
            if not out_:
                continue
            d[0] = [r for u in units.values() if u not in out_ for r in u]
            for u in out_:
                decisions.append([u, d[1], d[2] | {d[6]}, d[3], *decide(u, d[1], d[2] | {d[6]}, d[3], more=grown,
                                                                         no_album=not novel(u))])
        decisions[:] = [d for d in decisions if d[0]]

    pet_shown = {a for a in (lib.albums if lib else ()) if any(r["id"] in own_pet for r in album_rows.get(a, ()))}

    def pet_in_album(decisions):
        """The owner's pet at home during an album of theirs that shows it too goes to that album, not to the
        pet's folder (user, s6: the week's album with its cat photos "is fine"; P37 gives the pet a folder)."""
        for d in decisions:
            if d[5] not in ("folder", "new_folder") or d[1] or d[3] or d[6].split("/")[-1].lower() not in pet_folder_names:
                continue
            t0_, t1_ = min(r["taken_ts"] for r in d[0]), max(r["taken_ts"] for r in d[0])
            for a in sorted(pet_shown - d[2]):
                if lib.albums[a]["start"] - JOIN_MARGIN <= t0_ and t1_ <= lib.albums[a]["end"] + JOIN_MARGIN:
                    d[5:9] = ["album", a, None, d[8] + f" (the owner's pet during {a.split('/')[-1]}, which shows it too)"]
                    break

    def merge_alike_folders(decisions):
        """New folders whose photos look the same are one folder, under the name of the bigger (s5: the owner's
        cat as "Czarny kot" and as "Coty")."""
        held = defaultdict(list)
        for d in decisions:
            if d[5] == "new_folder":
                held[d[6]] += d[0]
        names = sorted(held, key=lambda f: -len(held[f]))
        vec = {f: mean_vec(held[f]) for f in names}
        into = {}
        for k, f in enumerate(names):
            for g in names[:k]:
                if vec[f] is not None and vec[g] is not None and into.get(g, g) == g and float(vec[f] @ vec[g]) >= SAME_LOOK:
                    into[f] = g
                    break
        for d in decisions:
            if d[6] in into:
                d[6] = into[d[6]]
                if d[5] == "new_folder":
                    d[7] = d[6].split("/")[-1]

    def part_of(rs, album, rows):
        """P(yes) that this group is part of the event of `album` (the library's or a new one), whose photos are
        `rows`. Asked with what both sides show and when, and whether a person is in both (P39: a one-line
        description of the album left the night before a trip at 0.5)."""
        mine, theirs = faces_of(rs), faces_of(rows)
        if strangers(rs, theirs):
            return 0.0
        s0, s1 = min(r["taken_ts"] for r in rs), max(r["taken_ts"] for r in rs)
        a0, a1 = min(r["taken_ts"] for r in rows), max(r["taken_ts"] for r in rows)

        def shows(xs, n):
            cs = [caps[r["id"]] for r in sorted(xs, key=lambda r: r["taken_ts"]) if r["id"] in caps]
            return "\n".join(f"  - {c.get('title_en')}: {c.get('description_en', '')[:150]}"
                             + (f" (place: {c['setting']})" if c.get("setting") not in (None, "", "unknown") else "")
                             for c in cs[::max(1, len(cs) // n)][:n])

        def span(x0, x1):
            d0, d1 = datetime.fromtimestamp(x0, tz), datetime.fromtimestamp(x1, tz)
            return f"{d0:%A %d %B %Y %H:%M} – {d1:%A %d %B %H:%M}"

        def sent(xs):  # messenger photos carry only the day they arrived (P32), not a time
            return all(r["id"] not in camera and MESSENGER.search("/" + r.get("path", "")) for r in xs)

        gap = (a0 - s1) / 3600 if s1 < a0 else (s0 - a1) / 3600 if s0 > a1 else 0
        long = f"{gap * 60:.0f} minutes" if gap < 1 else f"{gap:.1f} hours"
        timing = ("taken during the album's time" if not gap else
                  f"taken {long} before the album's first photo" if s1 < a0 else f"taken {long} after the album's last photo")
        if sent(rs):
            timing = f"received by a messenger on {datetime.fromtimestamp(s0, tz):%A %d %B %Y} (no time of day)"
        theirs_when = (f"received by a messenger on {datetime.fromtimestamp(a0, tz):%A %d %B %Y} (no time of day)"
                       if sent(rows) else f"from {span(a0, a1)}")
        both = mine is not None and theirs is not None and float((mine @ theirs.T).max()) >= FACE_SAME
        q = (f"An album of the owner's: \"{album.split('/')[-1]}\", photos {theirs_when}. What they show:\n"
             f"{shows(rows, 10)}\n\nA group of photos {timing}" + ("" if sent(rs) else f" ({span(s0, s1)})")
             + f":\n{shows(rs, 12)}\n" + ("\nThe same person (not the owner) is in both.\n" if both else "") +
             "\nIs this group part of that album's event: the same outing, trip or occasion (before, during or after it:"
             " packing and setting off, the night before, the way back)? Shopping for a trip, or another errand before"
             " it, isn't part of the trip. Neither is a different activity at a different place the same day, like a"
             " party with other people. On a trip or an outing the place changes as it goes. Answer with one word: yes"
             " or no.")
        # (user, s6: the pet-shop trip the day before a camping trip is its own album, P41)
        ans = vlm.choice([{"type": "text", "text": q}], ["yes", "no"])
        return ans.get("yes", 0.5) if ans else 0.5

    def fixed(target):
        """What a thematic folder is for: its starter description (themes.yaml) or the profile written once
        from the owner's photos in it; None for a folder this run makes."""
        return profile.get(target) or (profile.get(OTHER_YEAR) if re.match(r"^Pictures/Other/\d{4}$", target) else None)

    def refine(decisions, grown):
        """Passes 2 and 3 (P38). Pass 2: every destination is described from what it got (and a new album
        renamed), then every group is decided again with those descriptions, the new albums proposed for
        other photos, and where the photos taken within a day that look like it went. Pass 3: each photo
        in a folder is checked against what the folder holds; the ones that don't fit are decided again."""
        held = defaultdict(list)
        for d in decisions:
            held[d[6]] += d[0]
        # A thematic folder is made for one kind of photo, so what it's for doesn't change with what one run put
        # in it (user, s6): only new folders and new albums are described from what they got.
        described = {t_: summary_of(t_, rs) for t_, rs in held.items() if t_ != RANDOM and not fixed(t_)
                     and t_ not in (lib.albums if lib else {})}
        proposed = {t_: s[0] for t_, s in described.items() if re.search(r"^Pictures/Albums/\d{4}/\d{4}-\d{2}-\d{2} ", t_)
                    and t_ not in (lib.albums if lib else {})}
        refined = {t_: s[0] for t_, s in described.items() if t_ not in proposed and not t_.startswith("Pictures/Albums/2")}
        vecs = [mean_vec(d[0]) for d in decisions]
        for k, d in enumerate(decisions):
            alike = []
            for j, e in enumerate(decisions):
                if j != k and vecs[k] is not None and vecs[j] is not None and e[6] != d[6] \
                        and abs(e[0][0]["taken_ts"] - d[0][0]["taken_ts"]) <= 86400:
                    s = float(vecs[k] @ vecs[j])
                    if s >= LOOKALIKE:
                        alike.append(f"{e[6]} ({s:.2f})")
            # The group's own new album is offered too: "keep it there" names it (s6: 10 trip photos went to
            # Other/2022 when it wasn't)
            own = {t_: v for t_, v in proposed.items() if not strangers(d[0], faces_of(held[t_]))}
            same_day = [t_ for t_ in own if t_ != d[6] and abs(album_day(t_) - d[0][0]["taken_ts"]) <= 86400]
            # Pass 1 stands unless there's a reason to look again: photos that look alike went elsewhere within
            # a day, or a folder's group has a new album of its day to join. (Deciding everything again flipped
            # good answers in s5: a bike ride the user liked as an album went to Sport.)
            if not alike and not (d[5] in ("folder", "new_folder") and same_day):
                continue
            hint = (f"Now this group is in {d[6]}; keep it there unless the albums or folders below fit better."
                    + (" Photos taken within a day that look like it went to: " + ", ".join(sorted(set(alike))[:4]) + "."
                       if alike else ""))
            d[4:] = decide(d[0], d[1], d[2], d[3], more=grown, no_album=len(d[0]) <= SMALL_EVENT and not novel(d[0]),
                           proposed=own, refined=refined, hint=hint)
        # The new albums' names, from what they hold now.
        held = defaultdict(list)
        for d in decisions:
            held[d[6]] += d[0]
        for t_, rs in held.items():
            if t_ in described and t_ in proposed and len(rs) == len(held[t_]):
                name = described[t_][1]
            else:
                name = summary_of(t_, rs)[1] if re.search(r"^Pictures/Albums/\d{4}/\d{4}-\d{2}-\d{2} ", t_) \
                    and t_ not in (lib.albums if lib else {}) else ""
            if name and name != t_.split("/")[-1][11:]:
                new = t_.rsplit("/", 1)[0] + "/" + t_.split("/")[-1][:11] + name
                for d in decisions:
                    if d[6] == t_:
                        d[6], d[7] = new, name
        # A new album within a day of an album of the library, or of a bigger new album: is it part of that
        # one's event? The best yes wins (s6: the night camping before the user's album of the trip 0.93, its
        # morning walk 0.96, every other pair in s5 and s6 0.29 or less).
        joined = 0
        new = defaultdict(list)
        for d in decisions:
            if d[5] == "new_album" and not d[3]:
                new[d[6]] += d[0]

        def apart(xs, ys):
            x0, x1 = min(r["taken_ts"] for r in xs), max(r["taken_ts"] for r in xs)
            y0, y1 = min(r["taken_ts"] for r in ys), max(r["taken_ts"] for r in ys)
            return max(y0 - x1, x0 - y1, 0)

        for t_ in sorted(new, key=lambda t_: len(new[t_])):
            rs = new[t_]
            if not rs:
                continue
            banned = set().union(*(d[2] for d in decisions if d[6] == t_))
            cands = [(a, album_rows[a]) for a in (lib.albums if lib else ()) if a not in banned and album_rows.get(a)
                     and apart(rs, album_rows[a]) <= 86400]
            cands += [(u, us) for u, us in new.items() if u != t_ and len(us) >= len(rs) and apart(rs, us) <= 86400]
            p_, best = max(((part_of(rs, a, rows), a) for a, rows in cands), default=(0.0, None))
            if p_ < 0.5:
                continue
            for d in decisions:
                if d[6] == t_:
                    if best in new:
                        d[6], d[7] = best, best.split("/")[-1][11:]
                    else:
                        d[5:8] = ["album", best, None]
                    d[8] += f" (part of {best.split('/')[-1]}, {p_:.2f})"
            if best in new:
                new[best] += rs
            new[t_] = []
            joined += 1
        # Pass 3: each photo in a folder against what the folder now holds.
        held = defaultdict(list)
        for d in decisions:
            if d[5] in ("folder", "new_folder"):
                held[d[6]] += d[0]
        now = {t_: fixed(t_) or summary_of(t_, rs)[0] for t_, rs in held.items()}
        # A burst is checked, and moves, as one. A folder of animals takes only photos of an animal (s5: the
        # monitor unboxed at home stayed in the cat's folder); the others are asked about.
        beasts = {t_ for t_, rs in held.items() if sum(r["id"] in animal_set for r in rs) >= PET_ALBUM_ANIMALS * len(rs)}

        def fits(target, rs):
            if target.split("/")[-1].lower() in pet_folder_names:  # the folder of the pet the user named (s6: receipts)
                return any(r["id"] in own_pet for r in rs)
            if target in beasts:
                return any(r["id"] in animal_set for r in rs)
            ps = []
            for c in [caps[r["id"]] for r in rs if r["id"] in caps][:3]:
                q = (f"The owner's folder \"{target}\" holds: {now[target]}\nA photo: {c.get('title_en')}: "
                     f"{c.get('description_en', '')[:200]}\nDoes this photo belong in this folder? Answer with one word: yes or no.")
                ans = vlm.choice([{"type": "text", "text": q}], ["yes", "no"])
                ps.append(ans.get("yes", 0.5) if ans else 0.5)
            return not ps or sum(ps) / len(ps) >= FIT_MIN

        moved = 0
        for d in list(decisions):
            if d[5] not in ("folder", "new_folder") or not now.get(d[6]):
                continue
            units = defaultdict(list)
            for r in d[0]:
                units[burst_of.get(r["id"], r["id"])].append(r)
            keep, out_ = [], []
            for u in units.values():
                (keep if fits(d[6], u) else out_).append(u)
            if not out_ or not keep:
                continue
            d[0] = [r for u in keep for r in u]
            for u in out_:
                moved += len(u)
                decisions.append([u, d[1], d[2] | {d[6]}, d[3],
                                  *decide(u, d[1], d[2] | {d[6]}, d[3], more=grown, no_album=not novel(u))])
        print(f"  refine: {len(described)} destinations described, {len(proposed)} new albums offered to the rest,"
              f" {joined} new albums part of another album's event, {moved} photos moved out of folders they didn't fit")

    folder_moves = defaultdict(list)  # thematic folder -> [(photo, why)]: one proposal per folder
    counts = Counter()
    # Every event, and every messenger photo with a caption (P32: always to a folder), is decided first;
    # one without a caption goes by its look.
    decisions = []  # [photos, away, banned, messenger, d0, kind, target, name, why, alternatives]
    for e in events:
        away, banned = is_away(e, base), banned_of(e)
        decisions.append([e, away, banned, False, *decide(e, away, banned)])
    # Messenger photos that arrived the same day and look alike are one group, decided like an event (user,
    # s5: the railway-bridge photos someone sent are an event); a single one is decided on its own, to a folder.
    by_day = defaultdict(list)
    for r in unplaced:
        if caps.get(r["id"]):
            by_day[datetime.fromtimestamp(r["taken_ts"], tz).date()].append(r)
            continue
        target, why = route(r)
        if target in banned_of([r]):
            target, why = other_of(r["taken_ts"]), "nothing more specific"
        folder_moves[target].append((r, f"sent, {why}"))
    for day_rs in by_day.values():
        groups = []
        for r in day_rs:
            v = sig.get(r["id"])
            g = next((g for g in groups if v is not None and (m := mean_vec(g)) is not None and float(m @ v) >= MSG_ALIKE), None)
            (g.append(r) if g is not None else groups.append([r]))
        for g in groups:
            banned = banned_of(g)
            if len(g) >= 2:
                decisions.append([g, False, banned, False, *decide(g, False, banned)])
            else:
                decisions.append([g, False, banned, True, *decide(g, False, banned, messenger=True)])
    # A new folder needs MIN_NEW_FOLDER photos. The groups of smaller ones are decided again, among the
    # existing folders and the new ones that did fill up.
    size = Counter()
    for d in decisions:
        if d[5] == "new_folder":
            size[d[6]] += len(d[0])
    grown = sorted(f for f, n in size.items() if n >= MIN_NEW_FOLDER)
    for d in decisions:
        if d[5] == "new_folder" and d[6] not in grown:
            d[4:] = decide(d[0], d[1], d[2], d[3], more=grown or [OTHER_YEAR])
        # One or two photos make an album only with new people (user: "even 1 photo could create
        # an event if it's in a new place with new people and not surrounded by photos with the same people").
        if d[5] == "new_album" and len(d[0]) <= SMALL_EVENT and not novel(d[0]):
            d[4:] = decide(d[0], d[1], d[2], d[3], more=grown, no_album=True)
    merge_alike_folders(decisions)
    one_day_events(decisions)
    pet_moments(decisions, grown)
    refine(decisions, grown)
    merge_alike_folders(decisions)
    one_day_events(decisions)
    pet_moments(decisions, grown)
    small_albums(decisions, grown)
    pet_folder_only(decisions, grown)
    pet_in_album(decisions)
    for rs, away, banned, messenger, d0, kind, target, name, why, alts in decisions:
        counts[kind] += 1
        if messenger or kind in ("folder", "new_folder"):
            target = other_of(rs[0]["taken_ts"]) if messenger and kind in ("album", "new_album") else target
            folder_moves[target] += [(r, f"sent: {why}" if messenger else why) for r in rs]
            continue
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
            target = MEDIA_FOLDERS.get(mk) or other_of(r["taken_ts"])
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

    # A burst moves as one (P35, P39): a photo decided apart from its burst (a messenger photo without a
    # caption goes by its look) goes where the rest of it went, an album before a folder (user, s5: B17 and
    # B18 half in a new album and half in Other/2022). The private sibling keeps its own.
    where, action_of = {}, {}
    for r in con.execute("SELECT p.pid, p.action, p.target, m.id FROM proposal p JOIN proposal_member m ON m.pid=p.pid"
                         " WHERE p.run=? AND p.action IN ('add', 'create', 'theme')", (run_id,)):
        if not r["target"].endswith(" PRIV"):
            where[r["id"]], action_of[r["pid"]] = r["pid"], r["action"]
    burst_ids = defaultdict(list)
    for i, gid in burst_of.items():
        if i in where:
            burst_ids[gid].append(i)
    rejoined, touched, came = 0, set(), defaultdict(list)
    for ids in burst_ids.values():
        n = Counter(where[i] for i in ids)
        if len(n) < 2:
            continue
        best = max(n, key=lambda pid: (action_of[pid] != "theme", sum(where[i] == pid and i in caps for i in ids), n[pid]))
        for i in ids:
            if where[i] != best:
                con.execute("UPDATE proposal_member SET pid=? WHERE pid=? AND id=?", (best, where[i], i))
                touched |= {best, where[i]}
                came[best].append(i)
                rejoined += 1
    for pid in touched:
        ts = [r[0] for r in con.execute("SELECT x.taken_ts FROM proposal_member m JOIN photo x ON x.id=m.id WHERE m.pid=?",
                                        (pid,))]
        if not ts:
            con.execute("DELETE FROM proposal WHERE pid=?", (pid,))
            continue
        data = json.loads(con.execute("SELECT data FROM proposal WHERE pid=?", (pid,)).fetchone()[0] or "{}")
        data["n"] = len(ts)
        if "reasons" in data:
            data["reasons"] |= {i: "with the rest of its burst" for i in came[pid]}
        con.execute("UPDATE proposal SET start=?, end=?, data=? WHERE pid=?",
                    (min(ts), max(ts), json.dumps(data, ensure_ascii=False), pid))
    if rejoined:
        print(f"  bursts: {rejoined} photos decided apart went with the rest of their burst")

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
    if unoffered:
        print(f"  albums answered but not offered (went to Other/<year>): {dict(unoffered.most_common(5))}")
    con.commit()
    dt = time.perf_counter() - t0
    log_timing(con, "albums", run_id, len(rows), dt, "cpu + 4090 qwen3.8-27b")
    con.commit()
    print(f"albums: {len(rows)} unsorted photos; {sum(len(v) for v in copies.values())} already in {len(copies)} albums,"
          f" {len(out_rows)} taken out in review, {sum(len(v) for v in media.values())} downloaded, {len(received)} from messengers"
          f" ({sum(len(v) for v in received_joins.values())} join an album they look like),"
          f" {sum(len(v) for v in joins.values())} join {len(joins)} albums by time, {len(sessions)} sessions,"
          f" {len(events)} events -> {dict(counts)}, {len(folder_moves)} folders; library index {'loaded: ' + str(len(lib.albums)) + ' albums' if lib else 'missing'};"
          f" {vlm.stats()}; {dt:.0f} s")
