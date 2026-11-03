"""S8 people: who is in the photos (design.md S8, decisions.md P27).

The faces (InsightFace buffalo_l, faces.py) are clustered, and the clusters are named from what the
user already did by hand:
  1. their People folders (`Pictures/People/<Name>`, folder policy `people`): the face that appears in
     most of a folder's photos is that person;
  2. their album names ("Gorce z Zosią", "Urodziny Jacka"): the local LLM reads every album name once for the
     people it mentions, in their base form, and matches them to the People folders; a cluster that keeps
     appearing in albums naming someone gets that name.
The owner (the OneDrive account's display name) is marked, so album names don't say "z <owner>".
Names are proposals; the user confirms or renames them in the bundle's People view.
"""

import json
import time
from collections import Counter, defaultdict

import numpy as np

from .config import Config
from .db import log_timing
from .library import library_for
from .vlm import VLM

VERSION = "people@1"
MIN_SIZE, MIN_SCORE = 0.04, 0.65   # faces smaller or less certain than this aren't clustered
LINK = 0.55                        # average-linkage cosine distance at which clusters stop merging
MIN_CLUSTER = 3                    # faces a cluster needs to become a person
FOLDER_SHARE = 0.3                 # a People folder names the cluster found in at least this share of its photos

SCHEMA = """
CREATE TABLE IF NOT EXISTS person (
  pid INTEGER PRIMARY KEY, run TEXT NOT NULL,
  name TEXT,                       -- full name (a People folder) or the base name from album names
  short TEXT,                      -- how album names call them ("Zosia"), for naming
  source TEXT,                     -- people-folder | album-names | NULL (unnamed)
  owner INTEGER NOT NULL DEFAULT 0,
  faces INTEGER, photos INTEGER, data TEXT
);
CREATE TABLE IF NOT EXISTS album_mention (album TEXT PRIMARY KEY, data TEXT NOT NULL);
"""

MENTION_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["albums"],
    "properties": {"albums": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["album", "people"],
        "properties": {"album": {"type": "string"}, "people": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["base", "folder"],
            "properties": {"base": {"type": "string"}, "folder": {"type": "string"}}}}}}}},
}
MENTION_PROMPT = """These are names of photo albums in a Polish personal library. For each album, list the people
it mentions by first name or nickname (not places, events or animals), in the base form (nominative):
"z Zosią" -> Zosia, "Urodziny Jacka" -> Jacek, "z Bartkiem" -> Bartek, "u Marty" -> Marta.
For each person, give the People folder that is the same person if one clearly is (a diminutive of the same
first name counts: Zosia -> Zofia, Kasia -> Katarzyna, Bartek -> Bartłomiej, Ola -> Aleksandra), otherwise "".
People folders: {folders}

Albums:
{albums}"""


def cluster(emb: np.ndarray) -> np.ndarray:
    from sklearn.cluster import AgglomerativeClustering

    if len(emb) < 2:
        return np.zeros(len(emb), int)
    return AgglomerativeClustering(n_clusters=None, metric="cosine", linkage="average",
                                   distance_threshold=LINK).fit_predict(emb)


def mentions(con, vlm, lib, folders):
    """Album name -> [(base name, People folder or "")], from the LLM, cached in the DB."""
    have = {r[0]: json.loads(r[1]) for r in con.execute("SELECT album, data FROM album_mention")}
    todo = [a for a in sorted(lib.albums) if a not in have]
    for k in range(0, len(todo), 60):
        chunk = todo[k:k + 60]
        out = vlm.json([{"type": "text", "text": MENTION_PROMPT.format(
            folders=", ".join(folders) or "(none)", albums="\n".join(lib.albums[a]["name"] for a in chunk))}],
            MENTION_SCHEMA, max_tokens=4000)
        by_name = {e["album"]: e["people"] for e in out["albums"]}
        for a in chunk:
            ppl = by_name.get(lib.albums[a]["name"], [])
            have[a] = [[p["base"], p["folder"] if p["folder"] in folders else ""] for p in ppl]
            con.execute("INSERT OR REPLACE INTO album_mention(album, data) VALUES (?,?)",
                        (a, json.dumps(have[a], ensure_ascii=False)))
        con.commit()
    return have


def run(cfg: Config, con, run_id: str):
    con.executescript(SCHEMA)
    t0 = time.perf_counter()
    lib = library_for(cfg, con)
    files = defaultdict(list)
    for r in con.execute("SELECT id, path, album, policy FROM file"):
        files[r["id"]].append(dict(r))
    rows = con.execute("SELECT fid, id, emb FROM face WHERE size >= ? AND score >= ? AND emb IS NOT NULL",
                       (MIN_SIZE, MIN_SCORE)).fetchall()
    if not rows:
        print("people: no faces")
        return
    emb = np.stack([np.frombuffer(r["emb"], np.float16).astype(np.float32) for r in rows])
    emb /= np.linalg.norm(emb, axis=1, keepdims=True)
    labels = cluster(emb)
    members = defaultdict(list)
    for r, lab in zip(rows, labels):
        members[int(lab)].append(r)
    clusters = {c: m for c, m in members.items() if len(m) >= MIN_CLUSTER}

    # 1. People folders: the dominant cluster of each folder.
    folder_photos = defaultdict(set)
    for id_, fs in files.items():
        for f in fs:
            if f["policy"] == "people":
                folder_photos[f["album"].split("/")[-1]].add(id_)
    names, source = {}, {}
    for folder, photos in folder_photos.items():
        counts = Counter()
        for c, m in clusters.items():
            counts[c] = len({r["id"] for r in m} & photos)
        if not counts:
            continue
        c, n = counts.most_common(1)[0]
        with_faces = len({r["id"] for m in clusters.values() for r in m} & photos)
        if n >= 3 and n >= FOLDER_SHARE * max(with_faces, 1) and (c not in names or n > names[c][1]):
            names[c] = (folder, n)
            source[c] = "people-folder"

    # 2. Album names: a cluster that keeps appearing in the albums naming one person.
    vlm = VLM(cfg.llm)
    ment = mentions(con, vlm, lib, sorted(folder_photos)) if lib else {}
    short = {}
    for a, ppl in ment.items():
        for base, folder in ppl:
            if folder:
                short.setdefault(folder, Counter())[base] += 1
    album_of_photo = {id_: {f["album"] for f in fs if f["policy"] in ("cull", "unsorted")} for id_, fs in files.items()}
    votes = defaultdict(Counter)
    for c, m in clusters.items():
        if c in names:
            continue
        albums_seen = Counter(a for r in m for a in album_of_photo.get(r["id"], ()))
        for a, n in albums_seen.items():
            for base, folder in ment.get(a, []):
                votes[c][folder or base] += 1
    taken = {n for n, _ in names.values()}
    for c, v in votes.items():
        if not v:
            continue
        name, n = v.most_common(1)[0]
        if n >= 2 and name not in taken:
            names[c] = (name, n)
            source[c] = "album-names"
            taken.add(name)

    # Store: one person per cluster, the faces pointing at it.
    con.execute("DELETE FROM person WHERE run=?", (run_id,))
    con.execute("UPDATE face SET person=NULL")
    owner = lib.owner if lib else None
    n_named = 0
    for c, m in sorted(clusters.items(), key=lambda kv: -len(kv[1])):
        name = names.get(c, (None, 0))[0]
        n_named += name is not None
        sh = short.get(name, Counter()).most_common(1)[0][0] if name in short else (name.split()[0] if name else None)
        cur = con.execute("INSERT INTO person(run, name, short, source, owner, faces, photos, data) VALUES (?,?,?,?,?,?,?,?)",
                          (run_id, name, sh, source.get(c), int(bool(owner) and name == owner), len(m),
                           len({r["id"] for r in m}), json.dumps({"cluster": c})))
        con.executemany("UPDATE face SET person=? WHERE fid=?", [(cur.lastrowid, r["fid"]) for r in m])
    con.commit()
    dt = time.perf_counter() - t0
    log_timing(con, "people", run_id, len(rows), dt, "cpu + 4090 qwen3.8-27b")
    con.commit()
    print(f"people: {len(rows)} faces, {len(clusters)} clusters of {MIN_CLUSTER}+, {n_named} named"
          f" ({sum(1 for s in source.values() if s == 'people-folder')} from People folders,"
          f" {sum(1 for s in source.values() if s == 'album-names')} from album names); owner {'found' if owner else 'unknown'};"
          f" {len(ment)} album names read; {vlm.stats()}; {dt:.0f} s")
