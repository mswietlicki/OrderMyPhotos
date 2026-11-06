"""S8 people: who is in the photos (design.md S8, decisions.md P27, P40).

The faces (InsightFace buffalo_l, faces.py) are clustered, and the clusters are named:
  0. by the user on the People page (`people.html` in the bundle, written here): a full name and a nickname
     per person, stored per face so they outlive re-clustering;
and otherwise from what the user already did by hand:
  1. their People folders (`Pictures/People/<Name>`, folder policy `people`): the face that appears in
     most of a folder's photos is that person;
  2. their album names ("Gorce z Zosią", "Urodziny Jacka"): the local LLM reads every album name once for the
     people it mentions, in their base form, and matches them to the People folders; a cluster that keeps
     appearing in albums naming someone gets that name.
The owner (the OneDrive account's display name) is marked, so album names don't say "z <owner>". Without a
People folder of theirs, the owner is the face their phone's front camera sees most (P39).
Animals the subject detector found are clustered the same way (DINOv2 on the crop) and named on the same
page, so captions and albums can say "Jan z Filemonem".
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
MAX_EXACT = 30000                  # faces in one component clustered exactly (~7 GB while scipy works)
MIN_CLUSTER = 3                    # faces a cluster needs to become a person
FOLDER_SHARE = 0.3                 # a People folder names the cluster found in at least this share of its photos
FRONT_SHARE, FRONT_MIN = 0.4, 5    # a camera setting with this share of selfies (of at least this many photos) is a front camera
SELFIE_FACE = 0.2                  # ... a selfie: tagged so (zero-shot), or a face this big (s6: 9 of 10 front-camera photos, rear <= 7 %)
SELFIE_MIN = 3                     # the owner is the biggest face in at least this many of its photos (and most of them)
OWNER_SAME = 0.35                  # a cluster whose faces match the owner's this well (median best cosine) is the owner too
NAME_SAME = 0.35                   # ... and an unnamed cluster that matches a person the user named this well is them
PET_SIZE, PET_SCORE = 0.15, 0.5    # animal boxes (S3 subjects) this big and sure are named on the People page
PET_LINK = 0.4                     # average-linkage cosine distance at which animal clusters stop merging (DINOv2 crop)
PET_SAME = 0.6                     # an unnamed animal cluster this close (median best cosine) to a named pet is that pet

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
CREATE TABLE IF NOT EXISTS name_label (  -- the user's names from the People page (P40), per face or animal box
  key TEXT PRIMARY KEY,            -- face:<photo id>:<box> | pet:<photo id>:<box>
  who TEXT NOT NULL,               -- the person or pet: the full name, else the nickname; "" = not them, not a person
  name TEXT, nick TEXT, owner INTEGER NOT NULL DEFAULT 0,
  source TEXT, created TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS pet (
  pid INTEGER PRIMARY KEY, run TEXT NOT NULL,
  name TEXT, short TEXT, source TEXT, kind TEXT,   -- kind: the detector's class (cat, dog, ...)
  boxes INTEGER, photos INTEGER, data TEXT
);
CREATE TABLE IF NOT EXISTS pet_member (pid INTEGER NOT NULL, id TEXT NOT NULL, box TEXT NOT NULL, size REAL);
CREATE TABLE IF NOT EXISTS animal_vec (key TEXT PRIMARY KEY, vec BLOB NOT NULL);  -- DINOv2 of an animal's crop
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


def _average_linkage(emb: np.ndarray, link: float) -> np.ndarray:
    from sklearn.cluster import AgglomerativeClustering

    if len(emb) < 2:
        return np.zeros(len(emb), int)
    return AgglomerativeClustering(n_clusters=None, metric="cosine", linkage="average",
                                   distance_threshold=link).fit_predict(emb)


def _components(emb: np.ndarray, link: float, chunk: int = 1024) -> np.ndarray:
    """Connected components of the graph "cosine distance below `link`", a block of rows at a time (a little
    looser, so rounding never cuts an edge the clustering would use)."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    x = (emb / np.linalg.norm(emb, axis=1, keepdims=True)).astype(np.float32)
    n = len(x)
    comp = np.arange(n)
    for i in range(0, n, chunk):
        r, c = np.nonzero(x[i:i + chunk] @ x.T > 1 - link - 1e-4)
        a, b = comp[r + i], comp[c]
        keep = a != b
        if keep.any():
            g = coo_matrix((np.ones(int(keep.sum()), np.int8), (a[keep], b[keep])), shape=(n, n))
            comp = connected_components(g, directed=False)[1][comp]
    return comp


def cluster(emb: np.ndarray, link: float = LINK) -> np.ndarray:
    """Average-linkage clusters cut at `link`, one connected component at a time (P43). Average linkage never
    merges two clusters that have no pair closer than `link`, so this is the same as clustering all faces at
    once, without the n² distance matrix that a whole library's faces (~80k: 25 GB, twice that while it
    works) don't fit in. A component too big even for that is clustered in slices whose clusters are then
    merged by their mean faces: close to the exact result, and logged."""
    labels = np.empty(len(emb), int)
    nxt = 0
    for k in np.unique(comp := _components(emb, link)):
        idx = np.flatnonzero(comp == k)
        if len(idx) <= MAX_EXACT:
            lab = _average_linkage(emb[idx], link)
        else:
            print(f"people: a component of {len(idx)} faces, over {MAX_EXACT}: clustered in slices, then merged")
            part = np.empty(len(idx), int)
            for s in range(0, len(idx), MAX_EXACT):
                part[s:s + MAX_EXACT] = _average_linkage(emb[idx[s:s + MAX_EXACT]], link) + s
            ids = np.unique(part)
            means = np.stack([emb[idx[part == p]].mean(0) for p in ids])
            lab = _average_linkage(means, link)[np.searchsorted(ids, part)]
        labels[idx] = lab + nxt
        nxt += int(lab.max()) + 1
    return labels


def user_names(clusters, keys, said, vecs, same):
    """Step 0 (P40): the user's names. A cluster is who most of its named members are; members the user took
    out ("") leave it; clusters with one name are one; an unnamed cluster whose members match a named one's
    (median best cosine >= `same`) is them. Returns {cluster: label row}; `clusters` is changed in place."""
    named = {}
    for c in list(clusters):
        clusters[c] = [r for r in clusters[c] if said.get(keys[id(r)], {}).get("who") != ""]
        votes = Counter(said[keys[id(r)]]["who"] for r in clusters[c] if keys[id(r)] in said)
        if votes:
            who = votes.most_common(1)[0][0]
            named[c] = next(said[keys[id(r)]] for r in clusters[c] if said.get(keys[id(r)], {}).get("who") == who)
        elif not clusters[c]:
            del clusters[c]
    first = {}
    for c in sorted(named, key=lambda c: -len(clusters[c])):
        who = named[c]["who"]
        if who in first:
            clusters[first[who]] += clusters.pop(c)
            del named[c]
        else:
            first[who] = c
    refs = {c: vecs([r for r in clusters[c] if keys[id(r)] in said]) for c in named}
    for c in [c for c in clusters if c not in named]:
        best = max(((float(np.median((vecs(clusters[c]) @ v.T).max(axis=1))), d) for d, v in refs.items()),
                   default=(0.0, None))
        if best[0] >= same:
            clusters[best[1]] += clusters.pop(c)
    return named


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


def selfie_face(con, clusters):
    """The cluster that is most often the biggest face in the front camera's photos, or None. A phone's front
    camera is the setting (model, f-number, 35 mm focal length) whose photos are mostly selfies: tagged so, or
    with a face at arm's length (the zero-shot tag alone found 2 of 10 in s6)."""
    kind = {r[0]: (json.loads(r[1] or "{}").get("top") or [{}])[0].get("key")
            for r in con.execute("SELECT id, data FROM signal WHERE kind='kind'")}
    lens = {r[0]: (r[1], r[2], r[3]) for r in con.execute(
        "SELECT id, model, fnumber, focal35 FROM photo WHERE make IS NOT NULL AND focal35 IS NOT NULL")}
    face = Counter()
    for i, s in con.execute("SELECT id, MAX(size) FROM face WHERE score >= ? GROUP BY id", (MIN_SCORE,)):
        face[i] = s
    n = Counter(lens.values())
    selfies = Counter(k for i, k in lens.items() if kind.get(i) == "selfie" or face[i] >= SELFIE_FACE)
    front = {k for k, c in n.items() if c >= FRONT_MIN and selfies[k] >= FRONT_SHARE * c}
    size = {r[0]: r[1] for r in con.execute("SELECT fid, size FROM face")}
    biggest = {}
    for c, m in clusters.items():
        for r in m:
            if lens.get(r["id"]) in front and size[r["fid"]] > biggest.get(r["id"], (0, None))[0]:
                biggest[r["id"]] = (size[r["fid"]], c)
    votes = Counter(c for _, c in biggest.values())
    if not votes:
        return None
    c, k = votes.most_common(1)[0]
    return c if k >= SELFIE_MIN and k > sum(votes.values()) / 2 else None


def run(cfg: Config, con, run_id: str):
    con.executescript(SCHEMA)
    t0 = time.perf_counter()
    lib = library_for(cfg, con)
    files = defaultdict(list)
    for r in con.execute("SELECT id, path, album, policy FROM file"):
        files[r["id"]].append(dict(r))
    rows = con.execute("SELECT fid, id, box, emb FROM face WHERE size >= ? AND score >= ? AND emb IS NOT NULL",
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
    index = {r["fid"]: k for k, r in enumerate(rows)}

    def vecs(m):
        return emb[[index[r["fid"]] for r in m]]

    # 0. The user's names from the People page.
    said = {r["key"]: dict(r) for r in con.execute("SELECT * FROM name_label WHERE key LIKE 'face:%'")}
    keys = {id(r): f"face:{r['id']}:{r['box']}" for r in rows}
    named = user_names(clusters, keys, said, vecs, NAME_SAME)
    names = {c: (lab["who"], len(clusters[c])) for c, lab in named.items()}
    source = {c: "user" for c in named}

    # 1. People folders: the dominant cluster of each folder.
    folder_photos = defaultdict(set)
    for id_, fs in files.items():
        for f in fs:
            if f["policy"] == "people":
                folder_photos[f["album"].split("/")[-1]].add(id_)
    for folder, photos in folder_photos.items():
        counts = Counter()
        for c, m in clusters.items():
            counts[c] = len({r["id"] for r in m} & photos)
        if not counts:
            continue
        c, n = counts.most_common(1)[0]
        with_faces = len({r["id"] for m in clusters.values() for r in m} & photos)
        if c in named:
            continue
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

    # 3. The owner: the person the user marked "This is me", the cluster their People folder names, or else the
    # face the front camera sees most. One person is sometimes split into clusters (s5: the owner in four, one
    # of them only in photos others sent), so the clusters of the same face are the owner's too.
    owner = lib.owner if lib else None
    core = next((c for c, lab in named.items() if lab["owner"]), None)
    how = "marked by the user" if core is not None else "unknown"
    if core is None and (core := next((c for c, (name, _) in names.items() if owner and name == owner), None)) is not None:
        how = "found"
    if core is None and (core := selfie_face(con, clusters)) is not None:
        how = "from the front camera"
    owners = set()
    if core is not None:
        ref = vecs(clusters[core])
        owners = {c for c, m in clusters.items() if c == core or c not in named
                  and float(np.median((vecs(m) @ ref.T).max(axis=1))) >= OWNER_SAME}

    # P57: once the user has been through the People page, a card they left without a name, and didn't mark
    # "I don't know them", is still someone they know: "Sometimes I do not remember a person's name but I still want
    # them to be saved". Only "?" makes a stranger. The page saves nothing for such a card, so it's read here: a
    # cluster with no label after the review is known.
    reviewed = con.execute("SELECT 1 FROM name_label WHERE source LIKE 'people-page:%' LIMIT 1").fetchone() is not None

    # Store: one person per cluster, the faces pointing at it.
    con.execute("DELETE FROM person WHERE run=?", (run_id,))
    con.execute("UPDATE face SET person=NULL")
    n_named = n_known = 0
    for c, m in sorted(clusters.items(), key=lambda kv: -len(kv[1])):
        name = names.get(c, (None, 0))[0]
        sh = short.get(name, Counter()).most_common(1)[0][0] if name in short else (name.split()[0] if name else None)
        if c in named:  # the user's: the full name if they gave one, and their nickname, else the first name
            lab = named[c]
            # "@me": the owner without a name; "@P1": cards merged without a name yet; "?P3": someone the user
            # doesn't know, one anonymous person that still counts as someone else
            name = lab["name"] or (lab["who"] if not lab["who"].startswith(("@", "?")) else None)
            sh = lab["nick"] or (name.split()[0] if name else None)
        n_named += bool(name or sh)
        known = bool(name or sh) or c in owners or (not named[c]["who"].startswith("?") if c in named else reviewed)
        n_known += known and not (name or sh) and c not in owners
        cur = con.execute("INSERT INTO person(run, name, short, source, owner, faces, photos, data) VALUES (?,?,?,?,?,?,?,?)",
                          (run_id, name, sh, source.get(c), int(c in owners), len(m),
                           len({r["id"] for r in m}), json.dumps({"cluster": c, "known": known})))
        con.executemany("UPDATE face SET person=? WHERE fid=?", [(cur.lastrowid, r["fid"]) for r in m])
    con.commit()
    n_pets = pets(cfg, con, run_id)
    from .naming import write_page
    page = write_page(cfg, con, run_id)
    dt = time.perf_counter() - t0
    log_timing(con, "people", run_id, len(rows), dt, "cpu + 4090 qwen3.8-27b")
    con.commit()
    print(f"people: {len(rows)} faces, {len(clusters)} clusters of {MIN_CLUSTER}+, {n_named} named, {n_known} known without a name"
          f" ({sum(bool(lab['name'] or lab['nick'] or not lab['who'].startswith(('@', '?'))) for lab in named.values())} by the user, {sum(1 for s in source.values() if s == 'people-folder')} from People folders,"
          f" {sum(1 for s in source.values() if s == 'album-names')} from album names); owner {how}"
          f"{f' ({len(owners)} clusters)' if owners else ''};"
          f" {len(ment)} album names read; {vlm.stats()}; {n_pets} animals; {page}; {dt:.0f} s")


def owner_short(con):
    """The owner's name as the user gave it (nickname, else first name), or None."""
    try:
        r = con.execute("SELECT short FROM person WHERE owner=1 AND source='user' AND short IS NOT NULL LIMIT 1").fetchone()
    except Exception:  # noqa: BLE001 - no people stage yet
        return None
    return r[0] if r else None


def recognized(con) -> dict[str, str]:
    """Photo id -> who it shows, for its caption (P40): the people by face, with where they are when there are
    several, and the named animals. "Jan (the owner, on the left), Ola (on the right); the cat Filemon"."""
    # Only the names the user gave (source "user", P67): a People folder's or an album's name is only a
    # suggestion on the People page (user, 2026-11-06: "want captions limited to the names you gave"). The
    # owner's other clusters take the owner's name.
    by, out = defaultdict(list), {}
    try:
        rows = con.execute("SELECT f.id, f.box, p.short, p.owner, p.source FROM face f JOIN person p ON p.pid=f.person"
                           " WHERE p.short IS NOT NULL OR p.owner=1").fetchall()
    except Exception:  # noqa: BLE001 - no people stage yet
        return {}
    mine = owner_short(con)
    for r in rows:
        short = r["short"] if r["source"] == "user" else (mine if r["owner"] else None)
        if short or r["owner"]:
            x0, _, x1, _ = json.loads(r["box"])
            by[r["id"]].append(((x0 + x1) / 2, short, bool(r["owner"])))
    for i, ps in by.items():
        ps = sorted({(n, o): (x, n, o) for x, n, o in ps}.values())
        parts = []
        for x, n, o in ps:
            where = ("on the left" if x < 0.4 else "on the right" if x > 0.6 else "in the middle") if len(ps) > 1 else ""
            extra = ", ".join(s for s in ("the owner" if o and n else "", where) if s)
            parts.append(f"{n or 'the owner'}" + (f" ({extra})" if extra else ""))
        out[i] = ", ".join(parts)
    try:
        for r in con.execute("SELECT DISTINCT m.id, p.short, p.kind FROM pet_member m JOIN pet p ON p.pid=m.pid"
                             " WHERE p.short IS NOT NULL AND p.source='user'"):
            out[r["id"]] = "; ".join(s for s in (out.get(r["id"]), f"the {r['kind'] or 'animal'} {r['short']}") if s)
    except Exception:  # noqa: BLE001 - no pets yet
        pass
    return out


def animal_vecs(cfg: Config, con, rows) -> np.ndarray:
    """DINOv2 (its CLS token) of each animal's crop, cached by key: the same animal in two photos is close."""
    have = {r[0]: np.frombuffer(r[1], np.float16).astype(np.float32)
            for r in con.execute("SELECT key, vec FROM animal_vec")}
    todo = [r for r in rows if r["key"] not in have]
    if todo:
        import torch
        from PIL import Image
        from transformers import AutoModel

        from .vision import DINO, device, free

        dev = device()
        dtype = torch.float16 if dev != "cpu" else torch.float32
        model = AutoModel.from_pretrained(DINO, dtype=dtype).to(dev).eval()
        mean = torch.tensor([0.485, 0.456, 0.406]).view(3, 1, 1)
        std = torch.tensor([0.229, 0.224, 0.225]).view(3, 1, 1)
        for r in todo:
            img = Image.open(cfg.preview(r["id"])).convert("RGB")
            x0, y0, x1, y1 = json.loads(r["box"])
            crop = img.crop((int(x0 * img.width), int(y0 * img.height), int(x1 * img.width), int(y1 * img.height)))
            x = torch.from_numpy(np.asarray(crop.resize((224, 224), Image.Resampling.BICUBIC), np.float32) / 255)
            x = ((x.permute(2, 0, 1) - mean) / std).unsqueeze(0).to(dev, dtype)
            with torch.inference_mode():
                v = torch.nn.functional.normalize(model(pixel_values=x).pooler_output.float(), dim=-1)[0].cpu().numpy()
            have[r["key"]] = v
            con.execute("INSERT OR REPLACE INTO animal_vec(key, vec) VALUES (?,?)",
                        (r["key"], v.astype(np.float16).tobytes()))
        con.commit()
        del model
        free()
    out = np.stack([have[r["key"]] for r in rows])
    return out / np.linalg.norm(out, axis=1, keepdims=True)


def pets(cfg: Config, con, run_id: str) -> int:
    """The animals to name (P40): the subject detector's animal boxes, big and sure enough, clustered on their
    crops' DINOv2 like faces, then named from the People page (step 0 above, with its own thresholds)."""
    try:
        rows = [dict(r) | {"key": f"pet:{r['id']}:{r['box']}"} for r in con.execute(
            "SELECT id, label, box, size FROM subject WHERE kind='animal' AND score >= ? AND size >= ?",
            (PET_SCORE, PET_SIZE))]
    except Exception:  # noqa: BLE001 - no subjects stage
        rows = []
    con.execute("DELETE FROM pet WHERE run=?", (run_id,))
    con.execute("DELETE FROM pet_member")
    if not rows:
        return 0
    emb = animal_vecs(cfg, con, rows)
    at = {r["key"]: k for k, r in enumerate(rows)}
    clusters = defaultdict(list)  # every animal is shown: one photo of a pet is worth naming too
    for r, lab in zip(rows, cluster(emb, PET_LINK)):
        clusters[int(lab)].append(r)
    clusters = dict(clusters)
    said = {r["key"]: dict(r) for r in con.execute("SELECT * FROM name_label WHERE key LIKE 'pet:%'")}
    keys = {id(r): r["key"] for r in rows}
    named = user_names(clusters, keys, said, lambda m: emb[[at[r["key"]] for r in m]], PET_SAME)
    for c, m in sorted(clusters.items(), key=lambda kv: -len(kv[1])):
        lab = named.get(c)
        name = (lab["name"] or (lab["who"] if not lab["who"].startswith(("@", "?")) else None)) if lab else None
        cur = con.execute("INSERT INTO pet(run, name, short, source, kind, boxes, photos, data) VALUES (?,?,?,?,?,?,?,?)",
                          (run_id, name, (lab["nick"] or (name.split()[0] if name else None)) if lab else None,
                           "user" if lab else None,
                           Counter(r["label"] for r in m).most_common(1)[0][0], len(m), len({r["id"] for r in m}),
                           json.dumps({"cluster": c})))
        con.executemany("INSERT INTO pet_member(pid, id, box, size) VALUES (?,?,?,?)",
                        [(cur.lastrowid, r["id"], r["box"], r["size"]) for r in m])
    con.commit()
    return len(rows)
