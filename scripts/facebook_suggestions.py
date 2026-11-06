"""Names for the People page from the user's Facebook friends' photos (P64). Runs in the app's container, on the
server; nothing leaves it.

The friends, saved from the user's own signed-in browser, in the session's folder:
- `facebook-photos-<n>.bin` (collected in parts): a JSON index line {"friends": [{"name", "profile", "photos": [{"kind", "off", "len"}]}]},
  then the JPEGs, at `off` from the end of that line. Kinds: "thumb" (the friends list's picture), "profile" (the
  profile picture, larger), "of" (photos the friend is tagged in), "by" (photos the friend posted).
- `facebook-friends.json` (the first version; a part replaces a friend there): {"friends": [{"name", "profile", "jpg" (base64)}]}.

Each friend's face is the one that recurs across their photos: InsightFace buffalo_l (the run's faces model)
finds the faces, faces at cosine >= LINK join, and the group seen in the most photos wins (a tagged photo counts
more than one they posted, the profile picture most). The owner's face, often tagged with friends, never wins.
A friend with only a profile picture gets its largest face. Each people card's mean face is compared with each
friend's, and `<session>/suggestions-facebook.json` gets, per card, up to 3 friends at cosine >= MIN and within
NEAR of the nearest, and every friend's name (for typing). The profile pictures go to `<bundle>/fb/`.

The file is tied to the clustering it was made from (the run, and its people's count, last id and faces). After
the people phase runs again, run this again.

    docker exec -i omp-omp-1 python -W ignore - /state/sessions/onedrive /cache/onedrive/bundle < scripts/facebook_suggestions.py

Prints counts only, and how often Facebook agrees with the cards that already have a name from a People folder or
album names (the calibration).
"""
import base64
import datetime
import hashlib
import io
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
from PIL import Image

from omp.config import Config

MIN, NEAR, OWNED, LINK = 0.35, 0.08, 0.50, 0.45
WEIGHT = {"pp": 2.0, "of": 1.5, "by": 0.5}     # a photo's say in whose face is the friend's ("pp": profile picture)

session, bundle = Path(sys.argv[1]), Path(sys.argv[2])


def load_friends():
    """[{"name", "profile", "photos": [(kind, jpeg bytes)]}]: `facebook-friends.json`, then each
    `facebook-photos*.bin` (collected in parts) in place of a friend's entry there."""
    by, sources = {}, []
    v1 = session / "facebook-friends.json"
    if v1.exists():
        for f in json.loads(v1.read_text(encoding="utf-8"))["friends"]:
            by[f["profile"]] = {"name": f["name"], "profile": f["profile"],
                                "photos": [("profile", base64.b64decode(f["jpg"]))] if f.get("jpg") else []}
        sources.append(v1.name)
    for binp in sorted(session.glob("facebook-photos*.bin")):
        with open(binp, "rb") as fh:
            index = json.loads(fh.readline())
            base = fh.tell()
            for f in index["friends"]:
                photos = []
                for p in f["photos"]:
                    fh.seek(base + p["off"])
                    photos.append((p["kind"], fh.read(p["len"])))
                by[f["profile"]] = {"name": f["name"], "profile": f["profile"], "photos": photos}
        sources.append(binp.name)
    return list(by.values()), ", ".join(sources)


def app_():
    from insightface.app import FaceAnalysis  # noqa: PLC0415

    a = FaceAnalysis(name="buffalo_l", root=str(Path(Config().models) / "insightface"),
                     allowed_modules=["detection", "recognition"], providers=["CPUExecutionProvider"])
    a.prepare(ctx_id=-1, det_thresh=0.5, det_size=(640, 640))
    return a


def components(E):
    """Groups of faces at cosine >= LINK, joined through each other (union-find)."""
    parent = list(range(len(E)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    G = E @ E.T
    for i in range(len(E)):
        for j in range(i + 1, len(E)):
            if G[i, j] >= LINK:
                parent[find(i)] = find(j)
    groups = {}
    for i in range(len(E)):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def identity(faces, owner_c):
    """The friend's face: (unit embedding, photos it's seen in, faces in the profile picture), or None."""
    if not faces:
        return None
    E = np.stack([f["emb"] for f in faces])
    pp_faces = sum(1 for f in faces if f["src"] == "pp" and f["kind"] == "profile") or sum(1 for f in faces if f["src"] == "pp")
    scored = []
    for comp in components(E):
        photos = {(faces[i]["src"], faces[i]["photo"]) if faces[i]["src"] != "pp" else ("pp", 0) for i in comp}
        cen = E[comp].mean(axis=0)
        cen /= np.linalg.norm(cen)
        size = max(faces[i]["area"] for i in comp)
        scored.append((sum(WEIGHT[s] for s, _ in photos), size, cen, len(photos)))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    for weight, _, cen, n in scored:
        if owner_c is not None and float((owner_c @ cen).max()) >= OWNED:
            continue                     # the owner, tagged with the friend
        if n >= 2 or all(f["src"] == "pp" for f in faces):
            return cen, n, pp_faces
    return None


# 1. the friends' faces
friends, source = load_friends()
app = app_()
(bundle / "fb").mkdir(parents=True, exist_ok=True)
photos_n = faces_n = 0
for fr in friends:
    fr["faces"], fr["pic"] = [], None
    best_pp = None
    for k, (kind, raw) in enumerate(fr["photos"]):
        try:
            img = Image.open(io.BytesIO(raw)).convert("RGB")
        except Exception:  # noqa: BLE001 - a broken download is skipped
            continue
        photos_n += 1
        src = "pp" if kind in ("thumb", "profile") else kind
        if src == "pp" and (best_pp is None or img.width > best_pp[0]):
            best_pp = (img.width, raw)
        for f in app.get(np.asarray(img)[:, :, ::-1].copy()):
            area = float((f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1])) / (img.width * img.height)
            fr["faces"].append({"emb": f.normed_embedding.astype(np.float32), "src": src, "kind": kind, "photo": k, "area": area})
            faces_n += 1
    if best_pp:
        fr["pic"] = "fb/" + hashlib.sha1(fr["profile"].encode()).hexdigest()[:16] + ".jpg"
        (bundle / fr["pic"]).write_bytes(best_pp[1])

# 2. each card's mean face, for the newest run's clustering
con = sqlite3.connect(f"file:{session / 'omp.db'}?mode=ro", uri=True)
(run,) = con.execute("SELECT run FROM person GROUP BY run ORDER BY MAX(pid) DESC").fetchone()
by = {}
for person, emb in con.execute("SELECT f.person, f.emb FROM face f JOIN person p ON p.pid = f.person"
                               " WHERE p.run=? AND f.emb IS NOT NULL", (run,)):
    e = np.frombuffer(emb, np.float16).astype(np.float32)
    by.setdefault(person, []).append(e / np.linalg.norm(e))
pids = sorted(by)
C = np.stack([np.mean(by[p], axis=0) for p in pids])
C /= np.linalg.norm(C, axis=1, keepdims=True)
owner = {r[0] for r in con.execute("SELECT pid FROM person WHERE owner AND run=?", (run,))}
oi = [i for i, p in enumerate(pids) if p in owner]
owner_c = C[oi] if oi else None

# 3. each friend's face, then the suggestions
known = []
for fr in friends:
    got = identity(fr["faces"], owner_c)
    if got and fr["pic"]:
        known.append({**fr, "emb": got[0], "seen": got[1], "pp_faces": got[2]})
T = np.stack([k["emb"] for k in known])
S = C @ T.T                                  # cards x friends
order = np.argsort(-S, axis=1)
sug = {}
for i, p in enumerate(pids):
    if p in owner:
        continue
    top = [j for j in order[i, :3] if S[i, j] >= MIN]
    top = [j for j in top if S[i, j] >= S[i, top[0]] - NEAR]
    if top:
        sug[f"P{p}"] = [{"name": known[j]["name"], "cos": round(float(S[i, j]), 3), "pic": known[j]["pic"], "seen": known[j]["seen"],
                         **({"faces": known[j]["pp_faces"]} if known[j]["seen"] == 1 and known[j]["pp_faces"] > 1 else {})}
                        for j in top]
sig = con.execute("SELECT COUNT(*), MAX(pid), SUM(faces) FROM person WHERE run=?", (run,)).fetchone()
out = {"made": datetime.datetime.now().isoformat(timespec="seconds"), "source": source,
       "run": run, "people": list(sig), "cards": sug, "names": sorted({f["name"] for f in friends})}
path = session / "suggestions-facebook.json"
path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
path.chmod(0o600)

# 4. counts, and the calibration against the cards already named from People folders or album names
seen = [k["seen"] for k in known]
print(f"friends: {len(friends)}, photos {photos_n}, faces {faces_n}; a face for {len(known)}"
      f" (seen in 1 photo: {seen.count(1)}, 2-4: {sum(1 for s in seen if 2 <= s <= 4)}, 5+: {sum(1 for s in seen if s >= 5)});"
      f" cards: {len(pids)}, the owner's {len(oi)}")
print(f"suggestions: {len(sug)} cards, {sum(len(v) for v in sug.values())} names,"
      f" {len({s['name'] for v in sug.values() for s in v})} different friends")
named = {r[0]: [n for n in r[1:3] if n] for r in con.execute(
    "SELECT pid, name, short FROM person WHERE run=? AND source IN ('people-folder', 'album-names')", (run,))}
cal = [(S[i, order[i, 0]], known[order[i, 0]]["seen"], any(t.lower() in known[order[i, 0]]["name"].lower()
                                                             for n in named[p] for t in n.split() if len(t) > 2))
       for i, p in enumerate(pids) if named.get(p)]
for t in (0.35, 0.45, 0.55):
    sel = [(s, ok) for c, s, ok in cal if c >= t]
    print(f"  named cards at cos >= {t:.2f}: {len(sel)} of {len(cal)}, {sum(ok for _, ok in sel)} agree with their name"
          f" (of them seen in 2+ photos: {sum(1 for s, _ in sel if s >= 2)}, agree {sum(ok for s, ok in sel if s >= 2)})")
