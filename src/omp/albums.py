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
from .library import Library, manifests_dir
from .vlm import VLM

VERSION = "albums@1"
EVENT_GAP = 3 * 3600        # a gap this long between photos starts a new event ...
TRIP_GAP = 30 * 3600        # ... unless both sides are away from home and closer than this (one trip)
AWAY_KM = 30                # this far from home counts as away
MIN_ALBUM = 8               # kept photos an event needs for a new album proposal (P10)
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


def segment(rows, home):
    events, cur = [], []
    for r in rows:
        if cur and r["taken_ts"] - cur[-1]["taken_ts"] > EVENT_GAP:
            events.append(cur)
            cur = []
        cur.append(r)
    if cur:
        events.append(cur)

    def away(ev):
        d = [km(home, (r["lat"], r["lon"])) for r in ev if r["lat"] is not None] if home else []
        return bool(d) and sorted(d)[len(d) // 2] > AWAY_KM

    merged = []
    for ev in events:
        if merged and away(merged[-1]) and away(ev) and ev[0]["taken_ts"] - merged[-1][-1]["taken_ts"] <= TRIP_GAP:
            merged[-1] = merged[-1] + ev
        else:
            merged.append(ev)
    return [(ev, away(ev)) for ev in merged]


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
    taken_out = {i: v.split(":", 2)[2] if v.startswith("proposal:") else v for i, v in taken_out.items() if v}
    out_rows = [r for r in rows if r["id"] in taken_out]
    rows = [r for r in rows if r["id"] not in taken_out]
    out_rows += [dict(r) for r in con.execute(  # an album's own photos taken out of it
        "SELECT p.id, p.taken_ts, p.taken_local, p.lat, p.lon, MIN(f.path) AS path FROM photo p JOIN file f ON f.id=p.id"
        " WHERE f.policy='cull' GROUP BY p.id ORDER BY p.taken_ts") if r["id"] in taken_out]
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

    from .db import get_vecs

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

    # 2. Taken during an album's event or trip: it joins that album, text pages into its Dokumenty/.
    from .text import DOCS_FOLDER
    try:
        pages = {r[0] for r in con.execute("SELECT id FROM text WHERE mostly_text=1")}
    except Exception:  # noqa: BLE001 - no text stage yet
        pages = set()
    joins, outside = defaultdict(list), []
    for r in rest:
        album = lib.album_at(r["taken_ts"]) if lib else None
        if album:
            joins[f"{album}/{DOCS_FOLDER}" if r["id"] in pages else album].append(r)
        else:
            outside.append(r)
    for target, members in joins.items():
        add("add", target, None, members, {"n": len(members),
                                           "why": f"taken during {target.split('/Albums/')[-1]}"})
    rest = outside
    if place_only:
        con.commit()
        n = con.execute("SELECT COUNT(*) FROM proposal_member m JOIN proposal p ON p.pid=m.pid"
                        " WHERE p.run=? AND p.action='add'", (run_id,)).fetchone()[0]
        print(f"place: {n} unsorted photos join an existing album, {sum(len(v) for v in copies.values())} are copies;"
              f" {time.perf_counter() - t0:.1f} s")
        return

    # 3. Other reference photos to their category album (the VLM caption's purpose and category).
    home = home_of(rows, tz)
    events = segment(rest, home)
    cats, keep_rows = defaultdict(list), []
    for ev, away in events:
        for r in ev:
            c = caps.get(r["id"], {})
            if c.get("purpose") in ("reference", "media") and c.get("category") in CATEGORY_FOLDERS \
                    and not (away and c.get("category") in STAY_WITH_TRIP):
                cats[c["category"]].append(r)
            else:
                keep_rows.append(r["id"])
    for cat, members in cats.items():
        add("category", CATEGORY_FOLDERS[cat], None, members, {"n": len(members), "category": cat})
    keep_ids = set(keep_rows)

    # 4. The other events: a new album, or left in place.
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

    # Thematic folders: the user's own, each described by the average SigLIP 2 embedding of its photos.
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
        for base, folder in ppl:
            if folder or base.lower() not in first:
                first[base.lower()] = folder or ""
    person_albums = {}
    for album in set(folders) | set(by_theme):
        m = re.match(r"^Pictures/Albums/([^/0-9][^/]*)$", album)
        if m and m.group(1).lower() in first:
            person_albums[album] = first[m.group(1).lower()]
    persons = {name: f"Pictures/People/{name}" for name in named}  # person -> where photos of them go
    for album, folder in person_albums.items():
        if folder in persons and folders.get(album, 0) >= PERSON_ALBUM_MIN:
            persons[folder] = album  # their undated album, when it's a big one
    person_folders = set(persons.values())
    centroids = {}
    for theme, ids in by_theme.items():
        if theme in person_folders or theme in person_albums or theme.startswith("Pictures/People/") or len(ids) < 3:
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
        """A thematic folder for one photo that belongs to no album (P29)."""
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

    # The downloaded images: the media folder they look most like, else the one for their kind.
    media_names = [n for n in theme_names if n in MEDIA_THEMES]
    media_mat = np.stack([centroids[n] for n in media_names]) if media_names else None
    for mk, members in media.items():
        routed = defaultdict(list)
        for r in members:
            target = MEDIA_FOLDERS.get(mk, RANDOM)
            if media_mat is not None and r["id"] in sig:
                sims = media_mat @ sig[r["id"]]
                j = int(np.argmax(sims))
                if float(sims[j]) >= THEME_MIN:
                    target = media_names[j]
            routed[target].append(r)
        for target, rs in routed.items():
            add("theme", target, None, rs, {"n": len(rs), "why": f"downloaded or not a camera photo ({mk})"})

    # The messenger photos no album claimed: documents to their category folder, the rest to a thematic one.
    # They don't make albums of their own: without a time of day they can't be placed in an event.
    routed = defaultdict(list)
    for r in unplaced:
        c = caps.get(r["id"], {})
        if r["purpose"] == "reference" and c.get("category") in CATEGORY_FOLDERS:
            routed[(CATEGORY_FOLDERS[c["category"]], "category")].append((r, f"a sent {c['category']} photo"))
        else:
            target, why = route(r)
            routed[(target, "theme")].append((r, f"sent, {why}"))
    for (target, action), items in routed.items():
        add(action, target, None, [r for r, _ in items],
            {"n": len(items), "received": True, "why": Counter(w for _, w in items).most_common(1)[0][0]})

    # The photos the reviewer took out: the thematic folder they fit, unless that's where they came from.
    routed = defaultdict(list)
    for r in out_rows:
        left = taken_out[r["id"]]
        target, why = route(r)
        if target in (left, left.removesuffix(" PRIV").removesuffix("/Dokumenty")):
            target, why = RANDOM, "nothing more specific"
        routed[target].append((r, f"taken out of {PurePosixPath(left).name} in review; {why}"))
    for target, items in routed.items():
        add("theme", target, None, [r for r, _ in items],
            {"n": len(items), "taken_out": True, "why": Counter(w for _, w in items).most_common(1)[0][0]})

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

    counts = Counter()
    for ev, away in events:
        ev = [r for r in ev if r["id"] in keep_ids]
        if not ev:
            continue
        kept, d0, data = context(ev, away)
        if len(kept) < MIN_ALBUM:  # too small for an album: each photo to its thematic folder
            routed = defaultdict(list)
            for r in ev:
                target, why = route(r)
                routed[target].append((r, why))
            for target, items in routed.items():
                add("theme", target, None, [r for r, _ in items],
                    data | {"n": len(items), "why": Counter(w for _, w in items).most_common(1)[0][0]})
            counts["theme"] += 1
            continue
        name, out = name_for(ev, kept, d0, data)
        target = f"Pictures/Albums/{d0:%Y}/{d0:%Y-%m-%d} {name}"
        add("create", target, name, ev, data | {"alternatives": out.get("alternatives", []), "why": out.get("why")})
        counts["create"] += 1

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
    print(f"  thematic folders: {dict(themes.most_common())} ({len(centroids)} folders learned, {len(persons)} people)")
    print(f"  album folders in the wrong place: {len(relocated)} with {sum(c[2] for c in relocated)} photos,"
          f" {sum(1 for c in relocated if c[3])} of them into an existing album")
    con.commit()
    dt = time.perf_counter() - t0
    log_timing(con, "albums", run_id, len(rows), dt, "cpu + 4090 qwen3.8-27b")
    con.commit()
    print(f"albums: {len(rows)} unsorted photos; {sum(len(v) for v in copies.values())} already in {len(copies)} albums,"
          f" {len(out_rows)} taken out in review, {sum(len(v) for v in media.values())} downloaded, {len(received)} from messengers"
          f" ({sum(len(v) for v in received_joins.values())} join an album they look like),"
          f" {sum(len(v) for v in joins.values())} taken during {len(joins)} albums' events,"
          f" {sum(len(v) for v in cats.values())} to {len(cats)} category albums, {len(events)} events"
          f" -> {dict(counts)}; home {'found' if home else 'unknown'}; library index {'loaded: ' + str(len(lib.albums)) + ' albums' if lib else 'missing'};"
          f" {vlm.stats()}; {dt:.0f} s")
