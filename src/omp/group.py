"""S4 grouping: events, copies, and bursts in the user's sense (same scene, same place, maybe minutes apart).

Pairs inside one event and a long window are scored by the margin between their scene similarity and
a threshold that rises with the time gap. Long gaps also need GPS proximity and a geometric
same-viewpoint check. Clusters grow by average margin with a diameter guard, so a chain A≈B≈C with
A≉C doesn't merge.
"""

import json
import math
import re
import time

import cv2
import numpy as np

from .config import Config
from .db import get_data, get_vecs, log_timing

VERSION = "group@1"
EVENT_GAP = 2 * 3600          # a gap this long starts a new event
WINDOW = 3600                 # burst candidates are at most this far apart
THR_MIN, THR_MAX = 0.70, 0.90 # DINOv2 cosine needed at 0 s and at the window's end
DIAMETER = 0.08               # no pair inside a burst may fall this far below its threshold
GPS_MAX_M = 100               # same place, checked when the gap is over GPS_AFTER_S
GPS_AFTER_S = 60
VIEW_AFTER_S = 120            # longer gaps need a same-viewpoint check
VIEW_MIN_INLIERS = 30
COPY_SIM, COPY_PHASH = 0.97, 8
SAME_TEXT = 0.7               # text pages are one burst only if this share of words matches (retakes)
DOCSET_GAP = 30 * 60          # different pages shot within this of each other form a document set


def text_similarity(a: str, b: str) -> float:
    """Share of the shorter text's words (3+ letters) found in the other. On the sample, retakes of the same
    page or board scored 0.84-0.99 and different pages at most 0.48; a character-level ratio scored some
    retakes as low as 0.15, because OCR of a page shot from a step further away comes out in another order."""
    words = lambda s: set(re.findall(r"\w{3,}", s.lower()))
    wa, wb = words(a), words(b)
    return len(wa & wb) / max(1, min(len(wa), len(wb)))


def threshold(gap: float) -> float:
    x = math.log10(1 + gap / 10) / math.log10(1 + WINDOW / 10)
    return THR_MIN + (THR_MAX - THR_MIN) * min(max(x, 0.0), 1.0)


def haversine(a, b):
    if a[0] is None or b[0] is None:
        return None
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


class Viewpoint:
    """SIFT + RANSAC homography between two previews: many inliers means the camera stood in the same
    place (a homography also covers turning the camera on the spot)."""

    def __init__(self, cfg):
        self.cfg, self.sift, self.cache = cfg, cv2.SIFT_create(nfeatures=3000), {}
        self.matcher = cv2.BFMatcher(cv2.NORM_L2)

    def features(self, id_):
        if id_ not in self.cache:
            img = cv2.imread(str(self.cfg.preview(id_)), cv2.IMREAD_GRAYSCALE)
            s = 1024 / max(img.shape)
            img = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
            self.cache[id_] = self.sift.detectAndCompute(img, None)
        return self.cache[id_]

    def inliers(self, a, b) -> int:
        ka, da = self.features(a)
        kb, db = self.features(b)
        if da is None or db is None or len(ka) < 8 or len(kb) < 8:
            return 0
        good = [m for m, n in (p for p in self.matcher.knnMatch(da, db, k=2) if len(p) == 2)
                if m.distance < 0.75 * n.distance]
        if len(good) < 8:
            return len(good)
        src = np.float32([ka[m.queryIdx].pt for m in good])
        dst = np.float32([kb[m.trainIdx].pt for m in good])
        _, mask = cv2.findHomography(src, dst, cv2.RANSAC, 8.0)
        return int(mask.sum()) if mask is not None else 0


def segment_events(photos):
    events, current = [], []
    for p in photos:
        if current and p["taken_ts"] - current[-1]["taken_ts"] > EVENT_GAP:
            events.append(current)
            current = []
        current.append(p)
    if current:
        events.append(current)
    return events


def cluster(n, margin):
    """Average-margin agglomeration with a diameter guard. margin[i, j] is -inf where not allowed.

    The two clusters with the best mean margin merge, as long as it's positive and no pair across them is
    below -DIAMETER. Clusters in different connected parts of the allowed pairs can never merge, so each part
    is done on its own; and inside a part, the clusters' margin sums and minimums are kept and merged instead
    of recomputed. Before, every round recomputed every pair of clusters: ~n³/3 submatrices for an event of
    n photos, hours for 2,000 (P52). The clusters are the same, in the same order (by their first photo)."""
    parent = list(range(n))

    def root(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, j in zip(*np.nonzero(np.isfinite(margin))):
        if i < j:
            parent[root(i)] = root(j)
    parts = {}
    for i in range(n):
        parts.setdefault(root(i), []).append(i)
    out = []
    for part in parts.values():
        out += _cluster_part(part, margin) if len(part) > 1 else [part]
    return sorted(out, key=lambda c: c[0])


def _cluster_part(part, margin):
    """`cluster` on one connected part (sorted indices). Row x stands for the cluster whose first photo is
    part[x]; merging y into x keeps x, as the list of clusters did."""
    k = len(part)
    total = margin[np.ix_(part, part)].astype(np.float64)   # sums of the pairs across two clusters
    low = total.copy()                                       # their minimum
    size = np.ones(k)
    alive = np.ones(k, dtype=bool)
    upper = np.triu(np.ones((k, k), dtype=bool), 1)
    members = [[p] for p in part]
    while True:
        with np.errstate(invalid="ignore"):
            score = total / np.outer(size, size)
        ok = upper & alive[:, None] & alive[None, :] & (low >= -DIAMETER)
        score = np.where(ok, score, -np.inf)
        best = int(np.argmax(score))  # the first best in (x, y) order, as the scan of every pair found it
        x, y = divmod(best, k)
        if not score[x, y] > 0.0:
            return [members[i] for i in range(k) if alive[i]]
        total[x, :] += total[y, :]
        total[:, x] = total[x, :]
        low[x, :] = np.minimum(low[x, :], low[y, :])
        low[:, x] = low[x, :]
        size[x] += size[y]
        alive[y] = False
        members[x] = sorted(members[x] + members[y])


def run(cfg: Config, con, run_id: str):
    t0 = time.perf_counter()
    photos = [dict(r) for r in con.execute(
        "SELECT p.id, p.taken_ts, p.lat, p.lon, MIN(f.path) AS path, MIN(f.album) AS album"
        " FROM photo p JOIN file f ON f.id = p.id WHERE f.policy NOT IN ('ignore', 'people', 'reference') GROUP BY p.id ORDER BY p.taken_ts")]
    dino = get_vecs(con, "dino")
    phash = get_data(con, "phash")
    photos = [p for p in photos if p["id"] in dino]
    # A messenger photo that joins an album has only the day it arrived. It is placed at the moment of the
    # album photo it looks most like (S11 `place`), so a re-sent copy is found as a copy and a friend's shot
    # of the same moment can join the album's burst (P33). After the original, if they're at the same moment.
    placed = {r[0]: r[1] for r in con.execute("SELECT id, value FROM signal WHERE kind='placed_at'")}
    for p in photos:
        if p["id"] in placed:
            p["taken_ts"], p["placed"] = placed[p["id"]], True
    photos.sort(key=lambda p: (p["taken_ts"], p.get("placed", False)))
    con.execute("DELETE FROM grp_member WHERE gid IN (SELECT gid FROM grp WHERE run=?)", (run_id,))
    con.execute("DELETE FROM grp WHERE run=?", (run_id,))
    view = Viewpoint(cfg)
    stats = {"pairs": 0, "view_checks": 0, "view_rejects": 0, "gps_rejects": 0}

    def add_group(kind, ids, cohesion, data):
        cur = con.execute("INSERT INTO grp(run, kind, cohesion, data) VALUES (?,?,?,?)",
                          (run_id, kind, cohesion, json.dumps(data)))
        for k, id_ in enumerate(ids):
            con.execute("INSERT INTO grp_member(gid, id, ord) VALUES (?,?,?)", (cur.lastrowid, id_, k))

    # Copies: the same frame saved twice (same capture second or no usable time, near-identical pixels).
    by_id = {p["id"]: p for p in photos}
    seen = set()
    for i, a in enumerate(photos):
        for b in photos[i + 1:]:
            if b["taken_ts"] - a["taken_ts"] > 0.01:  # a copy keeps the capture time exactly
                break
            if a["album"] == b["album"]:
                continue  # two files of one folder with the same second are a camera burst, not a copy
            ph = bin(int(phash[a["id"]]["phash"], 16) ^ int(phash[b["id"]]["phash"], 16)).count("1")
            if float(dino[a["id"]] @ dino[b["id"]]) >= COPY_SIM and ph <= COPY_PHASH:
                add_group("copy", [a["id"], b["id"]], None, {"phash": ph})
                seen.add(b["id"])
    photos = [p for p in photos if p["id"] not in seen]

    try:
        pages = {r[0]: r[1] for r in con.execute("SELECT id, text FROM text WHERE mostly_text=1")}
    except Exception:  # noqa: BLE001 - no text stage yet
        pages = {}
    doc_links = []
    events = segment_events(photos)
    n_bursts = 0
    for e_idx, ev in enumerate(events):
        ids = [p["id"] for p in ev]
        add_group("event", ids, None, {"start": ev[0]["taken_ts"], "end": ev[-1]["taken_ts"], "index": e_idx})
        n = len(ev)
        if n < 2:
            continue
        emb = np.stack([dino[i] for i in ids])
        sim = emb @ emb.T
        margin = np.full((n, n), -np.inf)
        detail = {}
        for i in range(n):
            for j in range(i + 1, n):
                gap = ev[j]["taken_ts"] - ev[i]["taken_ts"]
                if gap > WINDOW:
                    break
                if ids[i] in pages and ids[j] in pages:
                    # Pages of one document look alike to an image embedding; only retakes of the same page
                    # (the same text) are a burst. Different pages make a document set instead (P16).
                    # Until the OCR has read them, two text pages count as different pages: all kept.
                    a_text, b_text = pages[ids[i]], pages[ids[j]]
                    if not a_text or not b_text or text_similarity(a_text, b_text) < SAME_TEXT:
                        if gap <= DOCSET_GAP:
                            doc_links.append((ids[i], ids[j]))
                        continue
                m = sim[i, j] - threshold(gap)
                if m < -DIAMETER:
                    continue
                stats["pairs"] += 1
                d = haversine((ev[i]["lat"], ev[i]["lon"]), (ev[j]["lat"], ev[j]["lon"]))
                if gap > GPS_AFTER_S and d is not None and d > GPS_MAX_M:
                    stats["gps_rejects"] += 1
                    continue
                inl = None
                if gap > VIEW_AFTER_S:
                    stats["view_checks"] += 1
                    inl = view.inliers(ids[i], ids[j])
                    if inl < VIEW_MIN_INLIERS:
                        stats["view_rejects"] += 1
                        continue
                margin[i, j] = margin[j, i] = m
                detail[(i, j)] = {"sim": round(float(sim[i, j]), 3), "gap": round(gap, 1),
                                  "thr": round(threshold(gap), 3), "dist_m": None if d is None else round(d),
                                  "inliers": inl}
        for c in cluster(n, margin):
            if len(c) < 2:
                continue
            pairs = [(i, j) for k, i in enumerate(c) for j in c[k + 1:]]
            cohesion = float(np.mean([margin[i, j] for i, j in pairs]))
            span = ev[c[-1]]["taken_ts"] - ev[c[0]]["taken_ts"]
            add_group("burst", [ids[i] for i in c], cohesion, {
                "event": e_idx, "span_s": round(span, 1),
                "min_margin": round(float(min(margin[i, j] for i, j in pairs)), 3),
                "pairs": {f"{ids[i][:8]}-{ids[j][:8]}": detail.get((i, j)) for i, j in pairs},
            })
            n_bursts += 1
    # Document sets: connected pages, in shooting order.
    parent = {}
    def root(x):
        while parent.get(x, x) != x:
            x = parent[x]
        return x
    for a, b in doc_links:
        parent[root(a)] = root(b)
    sets = {}
    for x in {x for link in doc_links for x in link}:
        sets.setdefault(root(x), []).append(x)
    ts = {p["id"]: p["taken_ts"] for p in photos}
    for members in sets.values():
        add_group("docset", sorted(members, key=lambda i: ts[i]), None, {"pages": len(members)})
    con.commit()
    dt = time.perf_counter() - t0
    log_timing(con, "group", run_id, len(photos), dt, "cpu")
    con.commit()
    sizes = [r[0] for r in con.execute(
        "SELECT COUNT(*) FROM grp g JOIN grp_member m ON m.gid=g.gid WHERE g.run=? AND g.kind='burst' GROUP BY g.gid",
        (run_id,))]
    print(f"group: {len(events)} events, {len(seen)} copies, {len(sets)} document sets, {n_bursts} bursts (sizes {sorted(sizes, reverse=True)}),"
          f" {sum(sizes)} of {len(photos)} photos in bursts; {stats}; {dt:.1f} s")
    del by_id
