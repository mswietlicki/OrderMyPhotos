"""People page: merge the cards of the same name, and each unnamed card into the named person it surely is (P68).

Reads the page's state (`people.json`) and the faces' embeddings. A named person's face is the mean of their cards'
faces, without the faces taken out. An unnamed card (not a stranger, not "Not a person") joins the nearest named person
when its mean face is at cosine >= T to theirs and >= M nearer than to anyone else. Without --apply it prints counts
only, and the calibration: the cards the user merged into a named person, each against that person without it, and
the strangers ("I don't know them"), which should match nobody.

With --apply it writes the merges into `people.json` as the page's Merge does: `into` the card with the most faces,
names filled from the others, faces taken out joined. It writes the merged faces' names into `name_label` as the
page's save does. Both are backed up next to them first (`*.before-merge-<time>`). The page saves its whole state:
nobody may change anything on an open People page meanwhile, and an open page must be reloaded after.

    docker exec -i omp-omp-1 python - /state/sessions/onedrive < scripts/people_merge.py                    # counts
    docker exec -i omp-omp-1 python - /state/sessions/onedrive --apply 0.60 0.15 < scripts/people_merge.py  # merges
"""
import json
import shutil
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

session = Path(sys.argv[1])
apply = "--apply" in sys.argv
con = sqlite3.connect(session / "omp.db", timeout=30)
state = json.loads((session / "people.json").read_text(encoding="utf-8"))
(run,) = con.execute("SELECT run FROM person GROUP BY run ORDER BY MAX(pid) DESC").fetchone()

# the cards, as the page has them (naming.page_data): P<pid>, its faces big and frontal first; the first is the
# card's `top`, and a state with another `top` belongs to an earlier clustering, which the page ignores
rows = defaultdict(list)
pids = {r[0] for r in con.execute("SELECT pid FROM person WHERE run=?", (run,))}
for pid, pic, box, size, yaw, emb in con.execute("SELECT person, id, box, size, yaw, emb FROM face WHERE person IS NOT NULL"):
    if pid in pids:
        rows[pid].append((pic, box, size, yaw, emb))
faces, topk = {}, {}
for pid, fs in rows.items():
    fs = sorted(fs, key=lambda f: -f[2] * (1 - min(abs(f[3] or 0), 90) / 90))
    c = f"P{pid}"
    topk[c] = f"face:{fs[0][0]}:{fs[0][1]}"
    faces[c] = [(f"face:{pic}:{box}", (lambda e: e / np.linalg.norm(e))(np.frombuffer(emb, np.float16).astype(np.float32)))
                for pic, box, _, _, emb in fs if emb is not None]
ids = sorted(faces, key=lambda c: -len(faces[c]))
keys = {c: {k for k, _ in faces[c]} for c in ids}
allkeys = {}
for pid, fs in rows.items():
    allkeys[f"P{pid}"] = [f"face:{pic}:{box}" for pic, box, *_ in fs]
st = {c: s for c, s in state.items() if c in topk and s.get("top") == topk[c]}
stale = sum(1 for c, s in state.items() if c.startswith("P") and c not in st)


def S(c):
    return st.get(c, {})


def who(s):
    return (s.get("name") or "").strip() or (s.get("nick") or "").strip()


def root(c):
    r = c
    for _ in range(99):
        n = S(r).get("into")
        if not n or n not in keys or n == r:
            break
        r = n
    return r


def groups():
    g = defaultdict(list)
    for c in ids:
        g[root(c)].append(c)
    return g


def centroid(cards, out):
    es = [e for c in cards for k, e in faces[c] if k not in out]
    if not es:
        return None
    m = np.mean(es, axis=0)
    return m / np.linalg.norm(m)


g = groups()
print(f"people cards {len(ids)}, with page state {len(st)} (stale {stale}); roots {len(g)}")

# 1. the same name on several roots: one person
by_name = defaultdict(list)
for r in g:
    s = S(r)
    if who(s) and not s.get("ignore"):
        by_name[who(s).lower()].append(r)
same = {n: rs for n, rs in by_name.items() if len(rs) > 1}
print(f"named roots {sum(len(v) for v in by_name.values())}, names {len(by_name)};"
      f" names on several roots {len(same)} ({sum(len(v) for v in same.values())} roots)")
merges = []                       # (card, into)


def merge(rs):
    """The page's merge(): into the root with the most faces; names filled from the others; `out` joined."""
    rs = list(dict.fromkeys(root(r) for r in rs))
    if len(rs) < 2:
        return
    size = {r: sum(len(allkeys[c]) for c in ids if root(c) == r) for r in rs}
    into = max(rs, key=lambda r: size[r])
    t = st.setdefault(into, {"top": topk[into], "name": "", "nick": "", "me": False, "ignore": False, "out": []})
    for r in rs:
        if r == into:
            continue
        s = st.setdefault(r, {"top": topk[r], "name": "", "nick": "", "me": False, "ignore": False, "out": []})
        for f in ("name", "nick"):
            if not (t.get(f) or "").strip() and (s.get(f) or "").strip():
                t[f] = s[f]
        t["me"] = bool(t.get("me") or s.get("me"))
        t["unknown"] = bool(t.get("unknown") and s.get("unknown"))
        t["group"] = t.get("group") or s.get("group") or ""
        t["out"] = sorted(set(t.get("out") or []) | set(s.get("out") or []))
        s["into"] = into
        merges.append((r, into))


for rs in same.values():
    merge(rs)
g = groups()

# 2. the named people (and the owner) and their mean faces; the unnamed known cards; the strangers
person = {}                       # root -> label
for r in g:
    s = S(r)
    if s.get("ignore"):
        continue
    if who(s):
        person[r] = who(s).lower()
    elif s.get("me"):
        person[r] = "@me"
named = list(person)
cen = {r: centroid(g[r], set(S(r).get("out") or [])) for r in named}
named = [r for r in named if cen[r] is not None]
N = np.stack([cen[r] for r in named])
unnamed = [r for r in g if r not in person and not S(r).get("ignore") and not S(r).get("unknown")]
strangers = [r for r in g if r not in person and not S(r).get("ignore") and S(r).get("unknown")]
print(f"named people {len(named)}; unnamed known roots {len(unnamed)}; strangers {len(strangers)}")


def best2(c, exclude=None):
    sims = N @ c
    if exclude is not None:
        sims = sims.copy()
        sims[exclude] = -1
    o = np.argsort(-sims)[:2]
    return o[0], float(sims[o[0]]), float(sims[o[1]])


# calibration: each card the user merged into a named person, against that person without it
cal = []
for i, r in enumerate(named):
    ms = g[r]
    if len(ms) < 2:
        continue
    out = set(S(r).get("out") or [])
    for m in ms:
        cm = centroid([m], out)
        rest = centroid([x for x in ms if x != m], out)
        if cm is None or rest is None:
            continue
        sims = N @ cm
        sims[i] = float(rest @ cm)            # its own person, without it
        o = np.argsort(-sims)[:2]
        cal.append((float(sims[o[0]]), float(sims[o[0]] - sims[o[1]]), o[0] == i))
strange = [best2(centroid([c for c in g[r]], set()))[1:] for r in strangers]
cand = [(r,) + best2(centroid(g[r], set(S(r).get("out") or []))) for r in unnamed]
print(f"calibration: {len(cal)} merged-in cards of named people; {len(strange)} strangers")
for T in (0.45, 0.5, 0.55, 0.6, 0.65):
    for M in (0.05, 0.1, 0.15):
        ok = [c for c in cal if c[0] >= T and c[1] >= M]
        fp = sum(1 for b, s2 in strange if b >= T and b - s2 >= M)
        n = sum(1 for _, _, b, s2 in cand if b >= T and b - s2 >= M)
        print(f"  T {T:.2f} M {M:.2f}: merged-in found {len(ok)}/{len(cal)}, right {sum(c[2] for c in ok)};"
              f" strangers that would match {fp}/{len(strange)}; unnamed to merge {n}/{len(unnamed)}")

if apply:
    T, M = float(sys.argv[sys.argv.index("--apply") + 1]), float(sys.argv[sys.argv.index("--apply") + 2])
    for r, i, b, s2 in cand:
        if b >= T and b - s2 >= M:
            merge([named[i], r])
    g = groups()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(session / "people.json", session / f"people.json.before-merge-{stamp}")
    cols = [r[1] for r in con.execute("PRAGMA table_info(name_label)")]
    (session / f"name_label.before-merge-{stamp}.json").write_text(json.dumps(
        [dict(zip(cols, r)) for r in con.execute("SELECT * FROM name_label")], ensure_ascii=False), encoding="utf-8")
    # the merged faces' names, as the page's names() for the changed cards
    touched = {c for pair in merges for c in pair}
    touched = {c for r in {root(c) for c in touched} for c in g[r]}
    rows, gone = [], []
    for c in touched:
        r = root(c)
        s = S(r)
        w = who(s) or ("@me" if s.get("me") else "") or (("?" + r) if s.get("unknown") else "") or s.get("group") or \
            ("@" + r if len(g[r]) > 1 else "")
        for k in allkeys[c]:
            if s.get("ignore") or k in (s.get("out") or []):
                rows.append((k, "", None, None, 0))
            elif w:
                rows.append((k, w, (s.get("name") or "").strip() or None, (s.get("nick") or "").strip() or None,
                             int(bool(s.get("me")))))
            else:
                gone.append(k)
    with con:
        con.executemany("DELETE FROM name_label WHERE key=?", [(k,) for k in gone])
        con.executemany("INSERT OR REPLACE INTO name_label(key, who, name, nick, owner, source) VALUES (?,?,?,?,?,?)",
                        [r + (f"people-merge:{stamp}",) for r in rows])
    keep = {c: s for c, s in state.items() if c not in st}       # other kinds (animals), and stale entries, as they were
    keep.update({c: s for c, s in st.items() if who(s) or s.get("me") or s.get("ignore") or s.get("unknown")
                 or s.get("into") or s.get("group") or s.get("out")})
    tmp = session / "people.json.tmp"
    tmp.write_text(json.dumps(keep, ensure_ascii=False), encoding="utf-8")
    tmp.replace(session / "people.json")
    print(f"applied: {len(merges)} merges ({len(same)} names on several cards; the rest by face),"
          f" {len(rows)} face names written, {len(gone)} cleared; backups people.json.before-merge-{stamp},"
          f" name_label.before-merge-{stamp}.json")
