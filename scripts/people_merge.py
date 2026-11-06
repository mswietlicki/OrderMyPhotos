"""People page: fewer cards to name (P68, P70). Merges, as the page's Merge does:

1. the people of the same name;
2. each unnamed person into the named person they surely are: the mean faces at cosine >= T, and >= M nearer than
   to anyone else;
3. the unnamed people that are surely one person, with each other: average-linkage groups of their mean faces at
   cosine >= G (each pair of cards in a group is near on average, so a chain of look-alikes doesn't join);
4. step 2 again, for the groups step 3 made;
5. with --strangers N: the unnamed people seen in N photos or fewer marked "I don't know them" (P57), as the page's
   "Mark the N unnamed" button does.

With --emb2 <npz> (keys, embs: the same faces measured by a second model, e.g. glintr100, P71) a card's mean face is
both models' mean faces side by side, so the cosine is the mean of the two models' cosines; only the faces both
measured count.

Never merged: two people seen in the same photo (one person is in a photo once), strangers ("I don't know them"),
"Not a person", animals.

Prints the calibration first, on the user's own names: for step 2, the cards the user merged into a named person, each
against that person without it, and the strangers, which should match nobody. For step 3, the named people's cards
grouped as if unnamed, and how often a group mixes people (all cards, and the small ones like most unnamed cards).

Reads the cards and the page's state from the app (`/api/people/cards`, `/api/people`) and the faces' embeddings
from the session's DB. With --apply, saves through the page's own save (`PUT /api/people`) with the state's version:
if anything saved meanwhile, nothing is saved (409). The state before goes to `people.json.before-merge-<time>`.

    docker exec -i omp-omp-1 python - /state/sessions/onedrive [--named T M] [--group G] [--emb2 <npz>] [--strangers N]
        [--apply] < scripts/people_merge.py
"""
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.spatial.distance import squareform

API = "http://127.0.0.1:8790"
args = sys.argv[1:]
session = Path(args[0])
apply = "--apply" in args
T, M = (float(args[args.index("--named") + 1]), float(args[args.index("--named") + 2])) if "--named" in args else (0.60, 0.15)
G = float(args[args.index("--group") + 1]) if "--group" in args else 0.60
STRANGERS = int(args[args.index("--strangers") + 1]) if "--strangers" in args else None


def get(path):
    with urllib.request.urlopen(API + path) as r:
        return json.load(r)


cards = {c["id"]: c for c in get("/api/people/cards")["people"]}
saved = get("/api/people")
state, version = saved["state"] or {}, saved["version"]
emb = {}
con = sqlite3.connect(f"file:{session / 'omp.db'}?mode=ro", uri=True)
for pic, box, e in con.execute("SELECT id, box, emb FROM face WHERE person IS NOT NULL AND emb IS NOT NULL"):
    v = np.frombuffer(e, np.float16).astype(np.float32)
    emb[f"face:{pic}:{box}"] = v / np.linalg.norm(v)
emb2 = None
if "--emb2" in args:
    z = np.load(args[args.index("--emb2") + 1])
    emb2 = {str(k): (lambda v: v / np.linalg.norm(v))(e.astype(np.float32)) for k, e in zip(z["keys"], z["embs"])}
ids = list(cards)
top = {i: cards[i]["faces"][0]["k"] if cards[i]["faces"] else i for i in ids}


def st(i, make=False):
    """The page's st(): a card's state, if it's of this clustering (its `top`), made when asked."""
    s = state.get(i)
    if s is not None and s.get("top") == top[i]:
        return s
    if not make:
        return {}
    state[i] = {"top": top[i], "name": "", "nick": "", "me": False, "ignore": False, "out": []}
    return state[i]


def who(s):
    return (s.get("name") or "").strip() or (s.get("nick") or "").strip()


def root(i):
    r = i
    for _ in range(99):
        n = st(r).get("into")
        if not n or n not in cards or n == r:
            break
        r = n
    return r


def groups():
    g = defaultdict(list)
    for i in ids:
        g[root(i)].append(i)
    return g


def nfaces(r, g):
    return sum(len(cards[c]["faces"]) for c in g[r])


def centroid(members, out=()):
    ks = [f["k"] for c in members for f in cards[c]["faces"]
          if f["k"] in emb and f["k"] not in out and (emb2 is None or f["k"] in emb2)]
    if not ks:
        return None
    m = np.mean([emb[k] for k in ks], axis=0)
    m /= np.linalg.norm(m)
    if emb2 is None:
        return m
    m2 = np.mean([emb2[k] for k in ks], axis=0)
    return np.concatenate([m, m2 / np.linalg.norm(m2)]) / np.sqrt(2)


def photos(members):
    return {f["k"].split(":")[1] for c in members for f in cards[c]["faces"]}


def grouped(units, vecs, pics, thr):
    """Average-linkage groups at cosine >= thr; two units seen in the same photo are as far apart as can be."""
    X = np.stack(vecs)
    D = np.clip(1 - X @ X.T, 0, 2)
    for i in range(len(units)):
        for j in range(i + 1, len(units)):
            if pics[i] & pics[j]:
                D[i, j] = D[j, i] = 2.0
    np.fill_diagonal(D, 0)
    lab = fcluster(linkage(squareform(D, checks=False), method="average"), t=1 - thr, criterion="distance")
    by = defaultdict(list)
    for u, l in zip(units, lab):
        by[l].append(u)
    return list(by.values())


merges = []


def merge(rs):
    """The page's merge(): into the one with the most faces; names filled from the others; `out` joined."""
    g = groups()
    rs = list(dict.fromkeys(root(r) for r in rs))
    if len(rs) < 2:
        return
    into = max(rs, key=lambda r: nfaces(r, g))
    t = st(into, make=True)
    for r in rs:
        if r == into:
            continue
        s = st(r, make=True)
        for f in ("name", "nick"):
            if not (t.get(f) or "").strip() and (s.get(f) or "").strip():
                t[f] = s[f]
        t["me"] = bool(t.get("me") or s.get("me"))
        t["unknown"] = bool(t.get("unknown") and s.get("unknown"))
        t["group"] = t.get("group") or s.get("group") or ""
        t["out"] = sorted(set(t.get("out") or []) | set(s.get("out") or []))
        s["into"] = into
        merges.append((r, into))


def people():
    """The named people (and the owner, unnamed) with their mean faces; the unnamed known; the strangers."""
    g = groups()
    named, unnamed, strangers = {}, [], []
    for r in g:
        s = st(r)
        if s.get("ignore"):
            continue
        label = who(s).lower() or ("@me" if s.get("me") else "")
        if label:
            c = centroid(g[r], set(s.get("out") or []))
            if c is not None:
                named[r] = (label, c)
        elif s.get("unknown"):
            strangers.append(r)
        else:
            unnamed.append(r)
    return g, named, unnamed, strangers


def to_named(dry=False):
    """Step 2 (and 4): each unnamed person into the named person they surely are."""
    g, named, unnamed, _ = people()
    rs = list(named)
    N = np.stack([named[r][1] for r in rs])
    P = [photos(g[r]) for r in rs]
    picks = []
    for u in unnamed:
        c = centroid(g[u], set(st(u).get("out") or []))
        if c is None:
            continue
        sims = N @ c
        pu = photos(g[u])
        for j in range(len(rs)):
            if sims[j] >= T - 0.1 and P[j] & pu:
                sims[j] = -1                 # seen in a photo with them: someone else
        o = np.argsort(-sims)[:2]
        if sims[o[0]] >= T and sims[o[0]] - sims[o[1]] >= M:
            picks.append((u, rs[o[0]]))
    if not dry:
        for u, r in picks:
            merge([r, u])
    return len(picks)


# --- the calibration, before anything changes
g, named, unnamed, strangers = people()
print(f"cards {len(ids)}, people {len(g)}: named {len(named)}, unnamed known {len(unnamed)}, strangers {len(strangers)}")
rs = list(named)
N = np.stack([named[r][1] for r in rs])
cal = []
for i, r in enumerate(rs):
    ms = g[r]
    out = set(st(r).get("out") or [])
    if len(ms) < 2:
        continue
    for m in ms:
        cm, rest = centroid([m], out), centroid([x for x in ms if x != m], out)
        if cm is None or rest is None:
            continue
        sims = N @ cm
        sims[i] = float(rest @ cm)
        o = np.argsort(-sims)[:2]
        cal.append((float(sims[o[0]]), float(sims[o[0]] - sims[o[1]]), o[0] == i))
sx = []
for r in strangers:
    c = centroid(g[r])
    if c is not None:
        sims = np.sort(N @ c)[::-1]
        sx.append((sims[0], sims[0] - sims[1]))
ok = [c for c in cal if c[0] >= T and c[1] >= M]
print(f"step 2 at {T:.2f}/{M:.2f}: of {len(cal)} cards merged into named people, {len(ok)} found one, {sum(c[2] for c in ok)} the"
      f" right one; strangers that would match: {sum(1 for b, m in sx if b >= T and m >= M)} of {len(sx)}")
# step 3's calibration: the named people's cards, grouped as if they had no names
cc = [(c, named[root(c)][0]) for r in rs for c in g[r] if centroid([c]) is not None]
person_of = dict(cc)
small = {c for c, _ in cc if len(cards[c]["faces"]) <= 10}
for thr in sorted({0.45, 0.50, 0.55, 0.60, G}):
    joined = wrong = joined_s = wrong_s = 0
    for grp in grouped([c for c, _ in cc], [centroid([c]) for c, _ in cc], [photos([c]) for c, _ in cc], thr):
        members = [(c, person_of[c]) for c in grp]
        if len(members) < 2:
            continue
        major = Counter(p for _, p in members).most_common(1)[0][0]
        joined += len(members) - 1
        wrong += sum(1 for _, p in members if p != major)
        joined_s += sum(1 for c, _ in members if c in small)
        wrong_s += sum(1 for c, p in members if p != major and c in small)
    print(f"step 3 at {thr:.2f}: named cards grouped as if unnamed: {joined} joins, {wrong} into another person's group"
          f" ({100 * wrong / max(joined, 1):.1f}%); small cards (<= 10 faces) in groups {joined_s}, wrong {wrong_s}")

# --- the merges
before = json.dumps(state, ensure_ascii=False)
by_name = defaultdict(list)
for r, (label, _) in named.items():
    if not label.startswith("@"):
        by_name[label].append(r)
for rs_ in by_name.values():
    merge(rs_)
n1 = len(merges)
n2 = to_named()
g, named, unnamed, strangers = people()
U = [u for u in unnamed if centroid(g[u], set(st(u).get("out") or [])) is not None]
n3 = 0
if len(U) > 1:
    found = grouped(U, [centroid(g[u], set(st(u).get("out") or [])) for u in U], [photos(g[u]) for u in U], G)
    sizes = Counter(len(v) for v in found if len(v) > 1)
    for members in found:
        if len(members) > 1:
            merge(members)
            n3 += len(members) - 1
    print(f"step 3 groups by size: {dict(sorted(sizes.items()))}")
n4 = to_named()
g, named, unnamed, strangers = people()
print(f"merges: same name {n1}, into named {n2}, unnamed together {n3}, groups into named {n4};"
      f" now {len(g)} people: named {len(named)}, unnamed known {len(unnamed)}, strangers {len(strangers)}")
marked = []
if STRANGERS is not None:
    for r in unnamed:
        if len(photos(g[r])) <= STRANGERS:
            st(r, make=True)["unknown"] = True
            marked.append(r)
    print(f"marked 'I don't know them': {len(marked)} unnamed people seen in {STRANGERS} photos or fewer;"
          f" unnamed known left {len(unnamed) - len(marked)}")

if apply and (merges or marked):
    touched = {c for pair in merges for c in pair} | set(marked)
    roots = {root(c) for c in touched}
    members = [c for c in ids if root(c) in roots]
    names, keys = {}, []
    for c in members:
        r = root(c)
        s = st(r)
        w = who(s) or ("@me" if s.get("me") else "") or (("?" + r) if s.get("unknown") else "") or s.get("group") or \
            ("@" + r if len(g[r]) > 1 else "")
        for f in cards[c]["faces"]:
            keys.append(f["k"])
            if s.get("ignore") or f["k"] in (s.get("out") or []):
                names[f["k"]] = {"who": ""}
            elif w:
                names[f["k"]] = {"who": w, "name": (s.get("name") or "").strip(), "nick": (s.get("nick") or "").strip(),
                                 "owner": bool(s.get("me"))}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (session / f"people.json.before-merge-{stamp}").write_text(before, encoding="utf-8")
    req = urllib.request.Request(API + "/api/people", method="PUT", headers={"Content-Type": "application/json"},
                                 data=json.dumps({"state": state, "names": names, "keys": keys, "version": version},
                                                 ensure_ascii=False).encode())
    try:
        with urllib.request.urlopen(req) as r:
            print(f"saved: {len(merges)} merges, {len(marked)} marked, {len(names)} faces' names; the state before: people.json.before-merge-{stamp}")
    except urllib.error.HTTPError as e:
        print(f"not saved: {e.code} {json.load(e).get('detail')}")
