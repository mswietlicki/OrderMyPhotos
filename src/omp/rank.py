"""S5 ranking: which frame of a burst is best, how many to keep, and the quality floor for every photo.

Sharpness is read from the full-resolution tile grid (previews.py) inside a subject mask built from the
depth map's near layer and the face boxes, so a soft background (bokeh) doesn't count against a frame.
Scores are relative within a burst; the floor is absolute (calibrated per camera on the library, or on
the sample for now). Close calls and floor candidates are marked for the VLM (judge.py).
"""

import json
import math
import time

import numpy as np
from PIL import Image

from .config import Config
from .db import get_data, get_vecs, log_timing

VERSION = "rank@1"
W_SHARP, W_QUALITY, W_AESTH, W_FACE = 0.35, 0.30, 0.25, 0.10
HARD_SHARP = -1.0        # log Tenengrad vs the group's best: 37 % of its gradient energy, ~60 % of the edge strength
CLOSE = 0.08             # contenders within this score of the best go to the VLM judge
# An extra frame is kept when it looks different from every kept frame and isn't clearly worse: on the
# sample (run s1 labels) every frame the user archived was either near-identical (DINOv2 >= 0.94) or
# clearly softer, while the extra frames they kept (another moment, a vista) were 0.71-0.93 and as sharp.
KEEP_DIVERSE = 0.95      # DINOv2 cosine to every kept frame must be below this
KEEP_SOFT = -0.25        # ... and its sharpness within this of the best (log Tenengrad) ...
KEEP_CLOSE = 0.08        # ... or its score within this of the best
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
    iqa, expo, kind, depth = (get_data(con, k) for k in ("iqa", "exposure", "kind", "depth"))
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
            "camera": f"{meta[id_]['make']} {meta[id_]['model']}", "day": (meta[id_]["taken_local"] or "")[:10],
        }
    return feats


def burst_scores(members, feats):
    f = [feats[i] for i in members]
    best_sharp = max(x["sharp"] for x in f)
    best_q = max(x["quality"] for x in f)
    best_a = max(x["aesthetics"] for x in f)
    max_faces = max(x["faces"] for x in f)
    out = {}
    for i, x in zip(members, f):
        rel_sharp = x["sharp"] - best_sharp
        s = W_SHARP * max(rel_sharp / 1.5, -1) + W_QUALITY * max((x["quality"] - best_q) / 0.10, -1) \
            + W_AESTH * max((x["aesthetics"] - best_a) / 0.15, -1)
        if max_faces:
            s += W_FACE * ((x["faces"] - max_faces) / max_faces)
        if x["clip_hi"] is not None:
            s -= max(0.0, x["clip_hi"] - 0.05) * 2 + max(0.0, x["clip_lo"] - 0.10) * 2
        reasons = []
        if rel_sharp <= HARD_SHARP:
            reasons.append(f"subject soft: {math.exp(rel_sharp / 2):.0%} of the best frame's detail")
        if x["motion"] > 0.5 and rel_sharp < -0.3:
            reasons.append("motion blur")
        if max_faces and x["faces"] < max_faces:
            reasons.append(f"{max_faces - x['faces']} face(s) fewer than the best frame")
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


def run(cfg: Config, con, run_id: str):
    t0 = time.perf_counter()
    feats = photo_features(cfg, con)
    dino = get_vecs(con, "dino")
    con.execute("DELETE FROM decision WHERE run=?", (run_id,))

    by_cam = {}
    for x in feats.values():
        by_cam.setdefault(x["camera"], []).append(x["sharp_global"])
    cam_median = {c: float(np.median(v)) for c, v in by_cam.items()}
    day_count = {}
    for x in feats.values():
        day_count[x["day"]] = day_count.get(x["day"], 0) + 1

    bursts = [(g["gid"], [r[0] for r in con.execute("SELECT id FROM grp_member WHERE gid=? ORDER BY ord", (g["gid"],))],
               g["cohesion"]) for g in con.execute("SELECT gid, cohesion FROM grp WHERE run=? AND kind='burst'", (run_id,))]
    in_burst = {i for _, m, _ in bursts for i in m}
    copies = {r[0] for r in con.execute(
        "SELECT m.id FROM grp_member m JOIN grp g ON g.gid=m.gid WHERE g.run=? AND g.kind='copy' AND m.ord > 0", (run_id,))}

    def decide(id_, gid, verdict, rank, score, reasons, tier, floor=None, protected=None, extra=None):
        x = feats[id_]
        f = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in x.items()}
        f.update(extra or {})
        con.execute(
            "INSERT OR REPLACE INTO decision(id, run, gid, verdict, rank, score, floor, protected, tier, reasons, features)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (id_, run_id, gid, verdict, rank, score, json.dumps(floor) if floor else None, protected, tier,
             json.dumps(reasons), json.dumps(f)))

    def protection(x):
        if day_count.get(x["day"], 0) <= 1:
            return "only photo of its day"
        return None

    for gid, members, cohesion in bursts:
        sc = burst_scores(members, feats)
        order = sorted(members, key=lambda i: -sc[i]["score"])
        best = order[0]
        kept = [best]
        for i in order[1:]:
            if sc[i]["hard"]:
                continue
            similar = any(float(dino[i] @ dino[k]) >= KEEP_DIVERSE for k in kept)
            worse = sc[i]["rel_sharp"] < KEEP_SOFT and sc[i]["score"] < sc[best]["score"] - KEEP_CLOSE
            if not similar and not worse:
                kept.append(i)
        close = [i for i in order[1:] if sc[best]["score"] - sc[i]["score"] <= CLOSE and not sc[i]["hard"]]
        best_floor = floor_check(feats[best], cam_median[feats[best]["camera"]])
        whole_bad = bool(best_floor) and not protection(feats[best])
        for rank, i in enumerate(order):
            x = feats[i]
            reasons = list(sc[i]["reasons"])
            floor = floor_check(x, cam_median[x["camera"]])
            keep = i in kept and not whole_bad
            if i == best:
                reasons.insert(0, "best of burst" if not close else f"best of burst, close call with {len(close)} other(s)")
            elif i in kept:
                reasons.insert(0, "also kept: visibly different and not clearly worse")
            elif not reasons:
                reasons.append(f"weaker than the best frame (score {sc[i]['score'] - sc[best]['score']:+.2f})")
            if whole_bad:
                reasons.append("whole burst below the quality floor")
            tier = "auto" if (not close and cohesion is not None and cohesion > 0.1 and not floor) else "review"
            decide(i, gid, "keep" if keep else "archive", rank, sc[i]["score"], reasons, tier, floor,
                   protection(x) if not keep else None,
                   {"judge": i in close or (i == best and bool(close)), "rel_sharp": round(sc[i]["rel_sharp"], 3),
                    "suspect": suspect(x)})

    for id_, x in feats.items():
        if id_ in in_burst:
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
    con.commit()
    dt = time.perf_counter() - t0
    log_timing(con, "rank", run_id, len(feats), dt, "cpu")
    con.commit()
    summary = {tuple(r[:2]): r[2] for r in con.execute(
        "SELECT verdict, tier, COUNT(*) FROM decision WHERE run=? GROUP BY verdict, tier", (run_id,))}
    floors = con.execute("SELECT COUNT(*) FROM decision WHERE run=? AND floor IS NOT NULL", (run_id,)).fetchone()[0]
    judge = con.execute("SELECT COUNT(DISTINCT gid) FROM decision WHERE run=? AND json_extract(features,'$.judge')",
                        (run_id,)).fetchone()[0]
    print(f"rank: {summary}; {floors} below the floor; {judge} bursts need the VLM judge; camera medians {cam_median};"
          f" {dt:.1f} s")
