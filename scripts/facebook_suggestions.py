"""Names for the People page from the user's Facebook friends' profile pictures (P64). Runs in the app's container,
on the server; nothing leaves it.

Reads `<session>/facebook-friends.json`: {"friends": [{"name", "profile", "jpg" (base64)}]}, the user's friends
list and profile pictures, saved from their own signed-in browser. Finds the largest face in each picture with
InsightFace buffalo_l (the run's faces model), and compares it with each people card's mean face. Writes
`<session>/suggestions-facebook.json`, which the People page shows: per card, up to 3 friends at cosine >= MIN and
within NEAR of the nearest, and every friend's name (for typing). The pictures go to `<bundle>/fb/`.

Left out: the owner's cards, and a friend whose picture's largest face is the owner's (a picture with them).
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

from omp import faces
from omp.config import Config

MIN, NEAR, OWNED = 0.35, 0.08, 0.50

session, bundle = Path(sys.argv[1]), Path(sys.argv[2])
data = json.loads((session / "facebook-friends.json").read_text(encoding="utf-8"))
app = faces.analysis(Config().models, "cpu", det_size=640)
(bundle / "fb").mkdir(parents=True, exist_ok=True)

# 1. each friend's face: the largest in the picture
friends, nofaces = [], 0
for f in data["friends"]:
    if not f.get("jpg"):
        continue
    raw = base64.b64decode(f["jpg"])
    found = app.get(np.asarray(Image.open(io.BytesIO(raw)).convert("RGB"))[:, :, ::-1].copy())
    if not found:
        nofaces += 1
        continue
    face = max(found, key=lambda x: (x.bbox[2] - x.bbox[0]) * (x.bbox[3] - x.bbox[1]))
    pic = "fb/" + hashlib.sha1(f["profile"].encode()).hexdigest()[:16] + ".jpg"
    (bundle / pic).write_bytes(raw)
    friends.append({"name": f["name"], "pic": pic, "emb": face.normed_embedding.astype(np.float32), "n": len(found)})
F = np.stack([f["emb"] for f in friends])

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
S = C @ F.T                                  # cards x friends
order = np.argsort(-S, axis=1)

# 3. the suggestions
owner = {r[0] for r in con.execute("SELECT pid FROM person WHERE owner AND run=?", (run,))}
oi = [i for i, p in enumerate(pids) if p in owner]
owned = set(np.where(S[oi].max(axis=0) >= OWNED)[0]) if oi else set()
sug = {}
for i, p in enumerate(pids):
    if p in owner:
        continue
    top = [j for j in order[i, :4] if j not in owned and S[i, j] >= MIN][:3]
    top = [j for j in top if S[i, j] >= S[i, top[0]] - NEAR]
    if top:
        sug[f"P{p}"] = [{"name": friends[j]["name"], "cos": round(float(S[i, j]), 3), "pic": friends[j]["pic"],
                         **({"faces": friends[j]["n"]} if friends[j]["n"] > 1 else {})} for j in top]
sig = con.execute("SELECT COUNT(*), MAX(pid), SUM(faces) FROM person WHERE run=?", (run,)).fetchone()
out = {"made": datetime.datetime.now().isoformat(timespec="seconds"), "source": "facebook-friends.json",
       "run": run, "people": list(sig), "cards": sug, "names": sorted({f["name"] for f in data["friends"]})}
path = session / "suggestions-facebook.json"
path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
path.chmod(0o600)

# 4. counts, and the calibration against the cards already named from People folders or album names
print(f"friends: {len(data['friends'])}, a face in {len(friends)} ({nofaces} without one); cards: {len(pids)},"
      f" the owner's {len(oi)}; friends' pictures of the owner, left out: {len(owned)}")
print(f"suggestions: {len(sug)} cards, {sum(len(v) for v in sug.values())} names,"
      f" {len({s['name'] for v in sug.values() for s in v})} different friends")
named = {r[0]: [n for n in r[1:3] if n] for r in con.execute(
    "SELECT pid, name, short FROM person WHERE run=? AND source IN ('people-folder', 'album-names')", (run,))}
cal = [(S[i, order[i, 0]], any(t.lower() in friends[order[i, 0]]["name"].lower()
                               for n in named[p] for t in n.split() if len(t) > 2))
       for i, p in enumerate(pids) if named.get(p)]
for t in (0.35, 0.45, 0.55):
    sel = [ok for c, ok in cal if c >= t]
    print(f"  named cards at cos >= {t:.2f}: {len(sel)} of {len(cal)}, {sum(sel)} agree with their name")
