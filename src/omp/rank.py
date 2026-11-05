"""S5 ranking: which frame of a burst is best, how many to keep, and the quality floor for every photo.

Sharpness is read from the full-resolution tile grid (previews.py) inside a subject mask built from the
depth map's near layer and the face boxes, so a soft background (bokeh) doesn't count against a frame.
Scores are relative within a burst; the floor is absolute (calibrated per camera on the library, or on
the sample for now). Close calls and floor candidates are marked for the VLM (judge.py).
"""

import json
import math
import sys
import time

import numpy as np
from PIL import Image

from .config import Config
from .db import get_data, get_vecs, log_timing
from .runtime import Progress
from .evaluate import latest_labels

VERSION = "rank@1"
W_SHARP, W_QUALITY, W_AESTH, W_FACE = 0.35, 0.30, 0.25, 0.10
# Happier is better (user, P50): the frame where its people smile more, by the mean P(happiness) of the faces.
W_SMILE, SMILE_SPAN = 0.15, 0.30   # the full penalty at 0.30 less than the group's best
HARD_SHARP = -1.0        # log Tenengrad vs the group's best: 37 % of its gradient energy, ~60 % of the edge strength
CLOSE = 0.08             # contenders within this score of the best go to the VLM judge
# ... and within this one when the burst shows people (P55): the reviewer picks people's frames by their pose and
# expression, which the features don't see, so more of those go to the judge, which is asked about it.
CLOSE_PEOPLE = 0.20
# An extra frame is kept when it looks different from every kept frame and isn't clearly worse: on the
# sample (run s1 labels) every frame the user archived was either near-identical (DINOv2 >= 0.94) or
# clearly softer, while the extra frames they kept (another moment, a vista) were 0.71-0.93 and as sharp.
KEEP_DIVERSE = 0.95      # DINOv2 cosine to every kept frame must be below this
KEEP_SOFT = -0.25        # ... and its sharpness within this of the best (log Tenengrad) ...
KEEP_CLOSE = 0.08        # ... or its score within this of the best
# Once there are labels, extra frames are decided by a small model fitted to them instead (decisions.md P23).
# Measured on the s2 labels, each album predicted by a model fitted on the other albums: AUC 0.67; keeping
# at P >= 0.30 left 13 of 109 user-kept extras archived and kept 99 of 163 the user archived.
EXTRA_FEATURES = ["sim", "rel_sharp", "d_quality", "d_aesthetics", "d_score", "faces", "burst_size", "rank", "d_smile"]
KEEP_P = 0.30            # keep an extra frame when P(the user keeps it) is at least this
MAYBE_P = 0.50           # ... and mark it "maybe redundant" below this, so a review can sweep those quickly
MIN_TRAIN = 50           # labelled extra frames needed before the model replaces the rules
# Below that, the model fitted on earlier sessions' labels (P55), so a new library starts with the reviewer's
# taste: `python -m omp.rank train-extra <models>/omp/extra-frames.json <db>:<run> ...` writes it.
SHARED = "omp/extra-frames.json"
SAME = 0.985             # an extra this close to another kept extra is the same picture twice
FLOOR_SHARP = 2.5        # log Tenengrad below the camera's median p90 (≈ e^-2.5 = 8 % of its energy)
FLOOR_QUALITY = 0.60
FLOOR_AESTH = 0.40


def subject_mask(cfg, id_, grid_shape, faces):
    gh, gw = grid_shape
    mask = np.zeros((gh, gw), bool)
    p = cfg.depth(id_)
    if p.exists():
        d = np.asarray(Image.open(p), np.float32) / 65535
        near = d >= np.quantile(d, 0.65)  # the nearest third of the frame
        frac = np.asarray(Image.fromarray(near.astype(np.uint8) * 255).resize((gw, gh), Image.Resampling.BOX)) / 255
        mask |= frac >= 0.5
    for f in faces:
        x0, y0, x1, y1 = json.loads(f["box"])
        cx, cy, w, h = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) * 1.5, (y1 - y0) * 1.5
        c0, c1 = int(max(0, (cx - w / 2) * gw)), int(min(gw, math.ceil((cx + w / 2) * gw)))
        r0, r1 = int(max(0, (cy - h / 2) * gh)), int(min(gh, math.ceil((cy + h / 2) * gh)))
        mask[r0:r1, c0:c1] = True
    return mask


def photo_features(cfg, con):
    sharp_rows = {r["id"]: r for r in con.execute("SELECT id, vec, shape, data FROM signal WHERE kind='sharp'")}
    iqa, expo, kind, depth, expr = (get_data(con, k) for k in ("iqa", "exposure", "kind", "depth", "expression"))
    faces = {}
    for f in con.execute("SELECT * FROM face"):
        faces.setdefault(f["id"], []).append(f)
    meta = {r["id"]: dict(r) for r in con.execute("SELECT id, make, model, taken_local, taken_ts FROM photo")}
    feats = {}
    for id_, r in sharp_rows.items():
        grid = np.frombuffer(r["vec"], np.float16).reshape(json.loads(r["shape"])).astype(np.float32)
        ten = grid[0]
        fs = [f for f in faces.get(id_, []) if f["score"] >= 0.6]
        mask = subject_mask(cfg, id_, ten.shape, fs)
        s_global = float(np.quantile(ten, 0.9))
        # Subject sharpness: the detailed part of the subject (top quarter of its tiles), so plain snow or
        # sky inside the mask doesn't drag it down.
        s_subject = float(np.quantile(ten[mask], 0.75)) if mask.sum() >= 6 else s_global
        face_sharp = None
        if fs:
            vals = []
            gh, gw = ten.shape
            for f in fs:
                # Detector boxes can reach past the frame: clamp, and take at least one tile.
                x0, y0, x1, y1 = (min(max(v, 0.0), 1.0) for v in json.loads(f["box"]))
                r0, c0 = min(int(y0 * gh), gh - 1), min(int(x0 * gw), gw - 1)
                sub = ten[r0:max(r0 + 1, math.ceil(y1 * gh)), c0:max(c0 + 1, math.ceil(x1 * gw))]
                vals.append(float(sub.max()))
            face_sharp = float(np.mean(vals))
        sd = json.loads(r["data"])
        e = expo.get(id_, {})
        q = iqa.get(id_, {})
        feats[id_] = {
            "sharp": 0.5 * s_subject + 0.5 * s_global,
            "sharp_subject": s_subject, "sharp_global": s_global, "face_sharp": face_sharp,
            "subject_tiles": float(mask.mean()), "motion": sd["motion"], "noise": sd["noise"],
            "quality": q.get("quality"), "aesthetics": q.get("aesthetics"),
            "clip_hi": e.get("clip_hi"), "clip_lo": e.get("clip_lo"), "lum": e.get("median"),
            # Faces under 3 % of the frame height come and go between frames: they don't count.
            "faces": sum(1 for f in fs if f["size"] >= 0.03), "big_faces": sum(1 for f in fs if f["size"] >= 0.08),
            "eyes": [min(x for x in (f["eye_l"], f["eye_r"]) if x is not None)
                     for f in fs if f["size"] >= 0.08 and (f["eye_l"] is not None or f["eye_r"] is not None)],
            "purpose": kind.get(id_, {}).get("purpose"), "memory_p": kind.get(id_, {}).get("value"),
            "depth_std": depth.get(id_, {}).get("std"),
            "smile": expr.get(id_, {}).get("value"), "smiling": expr.get(id_, {}).get("smiling"),
            "camera": f"{meta[id_]['make']} {meta[id_]['model']}", "day": (meta[id_]["taken_local"] or "")[:10],
        }
    return feats


def burst_scores(members, feats):
    f = [feats[i] for i in members]
    best_sharp = max(x["sharp"] for x in f)
    best_q = max(x["quality"] for x in f)
    best_a = max(x["aesthetics"] for x in f)
    max_faces = max(x["faces"] for x in f)
    smiles = [x["smile"] for x in f if x.get("smile") is not None]
    best_smile = max(smiles) if smiles else None
    out = {}
    for i, x in zip(members, f):
        rel_sharp = x["sharp"] - best_sharp
        s = W_SHARP * max(rel_sharp / 1.5, -1) + W_QUALITY * max((x["quality"] - best_q) / 0.10, -1) \
            + W_AESTH * max((x["aesthetics"] - best_a) / 0.15, -1)
        if max_faces:
            s += W_FACE * ((x["faces"] - max_faces) / max_faces)
        if best_smile is not None and x.get("smile") is not None:
            s += W_SMILE * max((x["smile"] - best_smile) / SMILE_SPAN, -1)
        if x["clip_hi"] is not None:
            s -= max(0.0, x["clip_hi"] - 0.05) * 2 + max(0.0, x["clip_lo"] - 0.10) * 2
        reasons = []
        if rel_sharp <= HARD_SHARP:
            reasons.append(f"subject soft: {math.exp(rel_sharp / 2):.0%} of the best frame's detail")
        if x["motion"] > 0.5 and rel_sharp < -0.3:
            reasons.append("motion blur")
        if max_faces and x["faces"] < max_faces:
            reasons.append(f"{max_faces - x['faces']} face(s) fewer than the best frame")
        if best_smile is not None and x.get("smile") is not None and best_smile - x["smile"] >= SMILE_SPAN / 2:
            reasons.append(f"smiles less than the best frame ({x['smile']:.0%} vs {best_smile:.0%})")
        out[i] = {"score": s, "rel_sharp": rel_sharp, "reasons": reasons, "hard": rel_sharp <= HARD_SHARP}
    return out


def floor_check(x, camera_median):
    """Absolute quality floor: is this photo bad on its own?"""
    why = []
    if x["sharp_global"] < camera_median - FLOOR_SHARP:
        why.append(f"badly blurred ({math.exp((x['sharp_global'] - camera_median) / 2):.0%} of this camera's usual detail)")
    if x["lum"] is not None and (x["lum"] < 12 or x["clip_lo"] > 0.6):
        why.append("nearly black")
    if x["lum"] is not None and (x["lum"] > 245 or x["clip_hi"] > 0.6):
        why.append("nearly white")
    if x["quality"] is not None and x["quality"] < FLOOR_QUALITY and x["aesthetics"] < FLOOR_AESTH:
        why.append(f"low technical and aesthetic quality ({x['quality']:.2f} / {x['aesthetics']:.2f})")
    return why


def suspect(x) -> str | None:
    """A hint that isn't reliable enough to archive on (zero-shot SigLIP called 3 kept photos in 5
    "accidental" on the sample): it only sends the photo to the VLM's worth-keeping check."""
    return "zero-shot: looks accidental" if x["purpose"] == "junk" else None


def extra_vec(i, best, pos, n, sc, feats, dino):
    f, fb = feats[i], feats[best]
    return [float(dino[i] @ dino[best]), sc[i]["rel_sharp"], (f["quality"] or 0) - (fb["quality"] or 0),
            (f["aesthetics"] or 0) - (fb["aesthetics"] or 0), sc[i]["score"] - sc[best]["score"], f["faces"], n, pos,
            (f["smile"] or 0) - (fb["smile"] or 0) if f.get("smile") is not None and fb.get("smile") is not None else 0.0]


class SharedModel:
    """The extra-frame model fitted on earlier sessions' labels (P55): one logistic regression on EXTRA_FEATURES, for a
    session with too few labels of its own. Its threshold was chosen on a session it wasn't fitted on."""

    def __init__(self, d: dict):
        self.mu, self.sd, self.w = (np.array(d[k], float) for k in ("mu", "sd", "coef"))
        self.b, self.keep_p, self.about = float(d["intercept"]), float(d["keep_p"]), d.get("trained_on")

    @classmethod
    def load(cls, cfg):
        path = cfg.models / SHARED
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return cls(d) if d.get("features") == EXTRA_FEATURES else None  # fitted on other features: not used

    def p(self, x, album=None):
        z = float(((np.array(x, float) - self.mu) / self.sd) @ self.w + self.b)
        return 1 / (1 + math.exp(-z))


def training_rows(con, run: str):
    """(X, y, albums) for the extra-frame model from a session that's been reviewed: every burst frame but the pick
    with a verdict label, its EXTRA_FEATURES as extra_vec computes them, from the stored decisions."""
    labels, dino = latest_labels(con), get_vecs(con, "dino")
    album_of = {r[0]: r[1] for r in con.execute("SELECT id, MIN(album) FROM file GROUP BY id")}
    dec = {r[0]: (r[1], r[2], r[3], json.loads(r[4] or "{}")) for r in con.execute(
        "SELECT id, gid, rank, score, features FROM decision WHERE run=? AND gid IS NOT NULL", (run,))}
    bursts = {}
    for i, d in dec.items():
        bursts.setdefault(d[0], []).append(i)
    X, y, albums = [], [], []
    for members in bursts.values():
        best = min(members, key=lambda i: dec[i][1])
        fb = dec[best][3]
        for i in members:
            f, lab = dec[i][3], labels.get(i, {}).get("verdict")
            if i == best or not lab or f.get("exact_copy") or i not in dino or best not in dino:
                continue
            smile = (f["smile"] - fb["smile"]) if f.get("smile") is not None and fb.get("smile") is not None else 0.0
            X.append([float(dino[i] @ dino[best]), f.get("rel_sharp") or 0.0, (f.get("quality") or 0) - (fb.get("quality") or 0),
                      (f.get("aesthetics") or 0) - (fb.get("aesthetics") or 0), dec[i][2] - dec[best][2], f.get("faces") or 0,
                      len(members), dec[i][1], smile])
            y.append(int(lab == "keep"))
            albums.append(album_of.get(i))
    return np.array(X, float), np.array(y), albums


def train_shared(out: str, sources: list[str]):
    """Fit the shared extra-frame model on reviewed sessions (`<db>:<run>`). Each session is also predicted by a
    model fitted on the others, and the threshold is the lowest P that keeps the reviewer's lost extras under 15 %."""
    import sqlite3

    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score

    sets = []
    for src in sources:
        db, run = src.rsplit(":", 1)
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        sets.append((run, *training_rows(con, run)))
        print(f"{run}: {len(sets[-1][2])} labelled extra frames, {int(sets[-1][2].sum())} kept")

    def fit(X, y):
        mu, sd = X.mean(0), X.std(0) + 1e-9
        return LogisticRegression(C=0.5, max_iter=2000).fit((X - mu) / sd, y), mu, sd

    held = []  # (P, y) of each session predicted by the others
    for k, (run, X, y, _) in enumerate(sets):
        rest = [s for j, s in enumerate(sets) if j != k]
        if not rest:
            continue
        m, mu, sd = fit(np.vstack([s[1] for s in rest]), np.concatenate([s[2] for s in rest]))
        p = m.predict_proba((X - mu) / sd)[:, 1]
        held.append((p, y))
        print(f"  {run} predicted by the others: AUC {roc_auc_score(y, p):.3f}")
        for t in (0.3, 0.4, 0.5, 0.6):
            print(f"    keep at P >= {t}: of the kept extras lost {int((y & (p < t)).sum())} of {int(y.sum())}, "
                  f"kept that were archived {int((~y.astype(bool) & (p >= t)).sum())}")
    keep_p = KEEP_P
    if held:
        P, Y = np.concatenate([h[0] for h in held]), np.concatenate([h[1] for h in held]).astype(bool)
        for t in np.arange(0.70, 0.19, -0.05):
            if (Y & (P < t)).sum() <= 0.15 * Y.sum():
                keep_p = round(float(t), 2)
                break
    X, y = np.vstack([s[1] for s in sets]), np.concatenate([s[2] for s in sets])
    m, mu, sd = fit(X, y)
    d = {"features": EXTRA_FEATURES, "mu": mu.tolist(), "sd": sd.tolist(), "coef": m.coef_[0].tolist(),
         "intercept": float(m.intercept_[0]), "keep_p": keep_p, "trained_on": {s[0]: len(s[2]) for s in sets},
         "fitted": time.strftime("%Y-%m-%d")}
    from pathlib import Path

    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(d, indent=1), encoding="utf-8")
    print(f"wrote {out}: {len(y)} frames, keep at P >= {keep_p}")


class ExtraModel:
    """Logistic regression on EXTRA_FEATURES, fitted to the user's labels. A labelled album is always
    predicted by a model fitted without it, so its evaluation stays honest."""

    keep_p = KEEP_P

    def __init__(self, X, y, albums):
        self.X, self.y, self.albums, self.cache = np.array(X, float), np.array(y), np.array(albums), {}

    def p(self, x, album):
        from sklearn.linear_model import LogisticRegression

        if album not in self.cache:
            m = self.albums != album
            if m.sum() < MIN_TRAIN or len(set(self.y[m])) < 2:
                self.cache[album] = None
            else:
                mu, sd = self.X[m].mean(0), self.X[m].std(0) + 1e-9
                self.cache[album] = (LogisticRegression(C=0.5, max_iter=1000).fit((self.X[m] - mu) / sd, self.y[m]), mu, sd)
        fit = self.cache[album]
        if fit is None:
            return None
        model, mu, sd = fit
        return float(model.predict_proba(((np.array(x, float) - mu) / sd)[None])[0, 1])


def keep_people(con, run_id: str, feats: dict, event_of: dict) -> list[str]:
    """Every person the user knows keeps at least one photo of each event (P55). The user, on a burst the floor had
    archived: "a keep because this is the only photos of her in this outfit". When all of a person's photos in an
    event would go to the archive, the best of them (a burst's pick first, then by quality and aesthetics) is kept and
    protected, so the judge's floor check doesn't archive it either. Known: named, the owner, or a card the user left
    without a name and didn't mark "I don't know them" (P57)."""
    try:
        names = {r[0]: r[1] or ("the owner" if r[2] else "someone you know") for r in con.execute(
            "SELECT pid, short, owner, data FROM person") if r[1] or r[2] or json.loads(r[3] or "{}").get("known")}
    except Exception:  # noqa: BLE001 - no people stage yet
        return []
    photos = {}
    for r in con.execute("SELECT id, person FROM face WHERE person IS NOT NULL AND score >= 0.6"):
        if r[1] in names and r[0] in event_of and r[0] in feats:
            photos.setdefault((event_of[r[0]], r[1]), set()).add(r[0])
    dec = {r[0]: [r[1], json.loads(r[2] or "{}"), r[3], json.loads(r[4] or "[]")] for r in con.execute(
        "SELECT id, verdict, features, rank, reasons FROM decision WHERE run=?", (run_id,))}
    kept = []
    for (_, pid), ids in sorted(photos.items(), key=lambda kv: (kv[0][0], str(kv[0][1]))):
        ids = [i for i in ids if i in dec and not dec[i][1].get("exact_copy")]
        if not ids or any(dec[i][0] == "keep" for i in ids):
            continue
        best = max(ids, key=lambda i: (dec[i][2] == 0, (feats[i]["quality"] or 0) + (feats[i]["aesthetics"] or 0)))
        why = f"the only photos of {names[pid]} in this event: the best one is kept"
        reasons = [why] + [r for r in dec[best][3] if r != "whole burst below the quality floor"]
        con.execute("UPDATE decision SET verdict='keep', protected=?, tier='review', reasons=? WHERE id=? AND run=?",
                    (why, json.dumps(reasons), best, run_id))
        dec[best][0] = "keep"
        kept.append(best)
    return kept


def run(cfg: Config, con, run_id: str):
    t0 = time.perf_counter()
    feats = photo_features(cfg, con)
    # People folders only teach faces (P27): they aren't grouped, ranked or reviewed.
    judged = {r[0] for r in con.execute("SELECT DISTINCT id FROM file WHERE policy NOT IN ('ignore', 'people', 'reference')")}
    feats = {i: x for i, x in feats.items() if i in judged}
    dino = get_vecs(con, "dino")
    album_of = {}
    for r in con.execute("SELECT id, album FROM file ORDER BY path"):
        album_of.setdefault(r["id"], r["album"])
    labels = latest_labels(con)
    from .text import DOCS_FOLDER
    try:
        texts = {r["id"]: dict(r) for r in con.execute("SELECT id, rotate, mostly_text, chars, blocks FROM text")}
    except Exception:  # noqa: BLE001 - no text stage yet
        texts = {}
    orient = {r[0]: int(r[1]) for r in con.execute("SELECT id, value FROM signal WHERE kind='orient' AND value > 0")}
    policy_of = {}
    for r in con.execute("SELECT id, policy FROM file ORDER BY path"):
        policy_of.setdefault(r["id"], r["policy"])
    from .nudity import levels

    nude = levels(con)  # tagged, not moved (P53)
    docset_of = {}
    for g in con.execute("SELECT gid FROM grp WHERE run=? AND kind='docset'", (run_id,)):
        members = [r[0] for r in con.execute("SELECT id FROM grp_member WHERE gid=? ORDER BY ord", (g["gid"],))]
        for k, i in enumerate(members):
            docset_of[i] = (k + 1, len(members))
    con.execute("DELETE FROM decision WHERE run=?", (run_id,))

    by_cam = {}
    for x in feats.values():
        by_cam.setdefault(x["camera"], []).append(x["sharp_global"])
    cam_median = {c: float(np.median(v)) for c, v in by_cam.items()}
    event_of = {r[0]: r[1] for r in con.execute(
        "SELECT m.id, g.gid FROM grp g JOIN grp_member m ON m.gid=g.gid WHERE g.run=? AND g.kind='event'", (run_id,))}
    day_count = {}
    for x in feats.values():
        day_count[x["day"]] = day_count.get(x["day"], 0) + 1

    bursts = [(g["gid"], [r[0] for r in con.execute("SELECT id FROM grp_member WHERE gid=? ORDER BY ord", (g["gid"],))],
               g["cohesion"]) for g in con.execute("SELECT gid, cohesion FROM grp WHERE run=? AND kind='burst'", (run_id,))]
    in_burst = {i for _, m, _ in bursts for i in m}
    from .db import exact_copies

    copies = {r[0] for r in con.execute(
        "SELECT m.id FROM grp_member m JOIN grp g ON g.gid=m.gid WHERE g.run=? AND g.kind='copy' AND m.ord > 0", (run_id,))}
    exact = exact_copies(con, run_id)  # copy -> the photo it duplicates (P54)

    # Progress in photos, for the app: each photo is decided once, in decide()
    with Progress(con, "rank", "rules", total=len(feats), commit=False) as prog:
        def decide(id_, gid, verdict, rank, score, reasons, tier, floor=None, protected=None, extra=None):
            x = feats[id_]
            f = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in x.items()}
            f.update(extra or {})
            tx = texts.get(id_)
            if tx:
                f["rotate"] = tx["rotate"] or None
                if tx["mostly_text"]:
                    f["text_page"] = True
                    if tx["blocks"] is None:
                        f["ocr_needed"] = True  # tagged until the OCR post-process reads it
                    if policy_of.get(id_) == "cull" and verdict == "keep":
                        f["place"] = f"{album_of.get(id_)}/{DOCS_FOLDER}"  # an album's text pages go to its subfolder
            if not (tx and tx["mostly_text"]) and orient.get(id_):  # a turned photo (P36); text pages keep the text stage's
                f["rotate"] = orient[id_]
            if id_ in nude:
                f["nudity"] = nude[id_]  # "full" or "partial": a tag in the plan (P53)
            if id_ in docset_of:
                f["docset"] = list(docset_of[id_])
                reasons = reasons + [f"page {docset_of[id_][0]} of a document set of {docset_of[id_][1]}"]
            con.execute(
                "INSERT OR REPLACE INTO decision(id, run, gid, verdict, rank, score, floor, protected, tier, reasons, features)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (id_, run_id, gid, verdict, rank, score, json.dumps(floor) if floor else None, protected, tier,
                 json.dumps(reasons), json.dumps(f)))
            prog.done(id_)

        def protection(x):
            if day_count.get(x["day"], 0) <= 1:
                return "only photo of its day"
            return None

        # The extra-frame model: features of every non-best frame, labels where the user gave them.
        scored, train_X, train_y, train_album = {}, [], [], []
        for gid, members, cohesion in bursts:
            sc = burst_scores(members, feats)
            order = sorted(members, key=lambda i: -sc[i]["score"])
            scored[gid] = (sc, order)
            for pos, i in enumerate(order[1:], 1):
                lab = labels.get(i, {}).get("verdict")
                if lab:
                    train_X.append(extra_vec(i, order[0], pos, len(members), sc, feats, dino))
                    train_y.append(int(lab == "keep"))
                    train_album.append(album_of.get(i))
        model = ExtraModel(train_X, train_y, train_album) if len(train_y) >= MIN_TRAIN else SharedModel.load(cfg)

        for gid, members, cohesion in bursts:
            sc, order = scored[gid]
            best = order[0]
            kept, p_extra = [best], {}
            for pos, i in enumerate(order[1:], 1):
                if sc[i]["hard"]:
                    continue
                p = model.p(extra_vec(i, best, pos, len(members), sc, feats, dino), album_of.get(i)) if model else None
                if p is not None:
                    p_extra[i] = p
                    if p >= model.keep_p and not any(float(dino[i] @ dino[k]) >= SAME for k in kept[1:]):
                        kept.append(i)
                    continue
                similar = any(float(dino[i] @ dino[k]) >= KEEP_DIVERSE for k in kept)
                worse = sc[i]["rel_sharp"] < KEEP_SOFT and sc[i]["score"] < sc[best]["score"] - KEEP_CLOSE
                if not similar and not worse:
                    kept.append(i)
            margin = CLOSE_PEOPLE if any(feats[m]["faces"] for m in members) else CLOSE
            close = [i for i in order[1:] if sc[best]["score"] - sc[i]["score"] <= margin and not sc[i]["hard"]]
            best_floor = floor_check(feats[best], cam_median[feats[best]["camera"]])
            whole_bad = bool(best_floor) and not protection(feats[best])
            for rank, i in enumerate(order):
                x = feats[i]
                reasons = list(sc[i]["reasons"])
                floor = floor_check(x, cam_median[x["camera"]])
                keep = i in kept and not whole_bad
                if i == best:
                    reasons.insert(0, "best of burst" if not close else f"best of burst, close call with {len(close)} other(s)")
                elif i in kept and i in p_extra:
                    reasons.insert(0, f"also kept: you'd likely keep it too ({p_extra[i]:.0%})" if p_extra[i] >= MAYBE_P
                                   else f"kept, but maybe redundant ({p_extra[i]:.0%} that you'd keep it)")
                elif i in kept:
                    reasons.insert(0, "also kept: visibly different and not clearly worse")
                elif i in p_extra:
                    reasons.insert(0, f"likely redundant ({p_extra[i]:.0%} that you'd keep it)")
                elif not reasons:
                    reasons.append(f"weaker than the best frame (score {sc[i]['score'] - sc[best]['score']:+.2f})")
                if whole_bad:
                    reasons.append("whole burst below the quality floor")
                maybe = i in kept and i in p_extra and p_extra[i] < MAYBE_P
                tier = "auto" if (not close and cohesion is not None and cohesion > 0.1 and not floor and not maybe) else "review"
                decide(i, gid, "keep" if keep else "archive", rank, sc[i]["score"], reasons, tier, floor,
                       protection(x) if not keep else None,
                       {"judge": i in close or (i == best and bool(close)), "rel_sharp": round(sc[i]["rel_sharp"], 3),
                        "suspect": suspect(x), "p_extra": round(p_extra[i], 3) if i in p_extra else None, "maybe": maybe})

        for id_, x in feats.items():
            if id_ in in_burst:
                continue
            if id_ in exact:  # the same pixels saved again: archived, never shown for review (P54)
                decide(id_, None, "archive", 0, 0.0, ["an exact copy of a photo that is kept"], "auto",
                       extra={"exact_copy": True, "copy_of": exact[id_]})
                continue
            if id_ in copies:
                decide(id_, None, "archive", 0, 0.0, ["a copy of the same photo in another folder"], "review")
                continue
            floor = floor_check(x, cam_median[x["camera"]])
            prot = protection(x)
            hint = suspect(x)
            if floor and not prot:
                decide(id_, None, "archive", 0, 0.0, ["bad on its own"] + floor, "review", floor, None,
                       {"floor_vlm": True, "suspect": hint})
            else:
                decide(id_, None, "keep", 0, 0.0, ["single"] + (floor or []), "auto" if not (floor or hint) else "review",
                       floor or None, prot, {"floor_vlm": bool(floor), "suspect": hint})
    kept_back = keep_people(con, run_id, feats, event_of)
    con.commit()
    dt = time.perf_counter() - t0
    log_timing(con, "rank", run_id, len(feats), dt, "cpu")
    con.commit()
    summary = {tuple(r[:2]): r[2] for r in con.execute(
        "SELECT verdict, tier, COUNT(*) FROM decision WHERE run=? GROUP BY verdict, tier", (run_id,))}
    floors = con.execute("SELECT COUNT(*) FROM decision WHERE run=? AND floor IS NOT NULL", (run_id,)).fetchone()[0]
    judge = con.execute("SELECT COUNT(DISTINCT gid) FROM decision WHERE run=? AND json_extract(features,'$.judge')",
                        (run_id,)).fetchone()[0]
    which = (f"fitted on {len(train_y)} labelled frames" if isinstance(model, ExtraModel) else
             f"shared, from {model.about}" if model else "not fitted (rules)")
    print(f"rank: extra-frame model {which}; {summary}; {floors} below the floor; {judge} bursts need the VLM judge; {len(kept_back)} kept as a person's only photos of an event; camera medians {cam_median};"
          f" {dt:.1f} s")


if __name__ == "__main__" and len(sys.argv) >= 4 and sys.argv[1] == "train-extra":
    train_shared(sys.argv[2], sys.argv[3:])
