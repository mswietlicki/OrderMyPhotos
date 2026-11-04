"""S3 faces: InsightFace buffalo_l (the same models Immich uses) on the 1536 px preview, on the CPU.

Eye openness is measured from the 106-point landmarks without relying on their index layout: the
contour points inside a small box around each eye centre of the 5-point detector are the eyelids
and corners, and their height/width ratio drops towards 0 when the eye closes.
"""

import time

import numpy as np
from PIL import Image

from .config import Config
from .db import log_timing
from .runtime import Progress

MODEL = "insightface/buffalo_l@2.1+emb"  # +emb: run 1 stored no embeddings


def eye_openness(lm106: np.ndarray, center: np.ndarray, iod: float) -> float | None:
    d = lm106 - center
    sel = lm106[(np.abs(d[:, 0]) <= 0.30 * iod) & (np.abs(d[:, 1]) <= 0.15 * iod)]
    if len(sel) < 4:
        return None
    width = sel[:, 0].max() - sel[:, 0].min()
    return float((sel[:, 1].max() - sel[:, 1].min()) / max(width, 1e-6))


def run(cfg: Config, con, run_id: str, det_size=1024):
    from insightface.app import FaceAnalysis

    done = {r[0] for r in con.execute("SELECT id FROM signal WHERE kind='faces' AND model=?", (MODEL,))}
    ids = [r[0] for r in con.execute("SELECT id FROM signal WHERE kind='sharp' ORDER BY id") if r[0] not in done]
    if not ids:
        return
    app = FaceAnalysis(name="buffalo_l", root=str(cfg.models / "insightface"),
                       allowed_modules=["detection", "landmark_2d_106", "landmark_3d_68", "recognition"],
                       providers=["CPUExecutionProvider"])
    app.prepare(ctx_id=-1, det_thresh=0.5, det_size=(det_size, det_size))
    t0 = time.perf_counter()
    total = 0
    with Progress(con, "signals.faces", MODEL, total=len(ids)) as prog:
        for id_ in ids:
            with prog.item(id_):
                img = Image.open(cfg.preview(id_)).convert("RGB")
                bgr = np.asarray(img)[:, :, ::-1].copy()
                h, w = bgr.shape[:2]
                faces = app.get(bgr)
                con.execute("DELETE FROM face WHERE id=?", (id_,))  # older models' rows too
                for f in faces:
                    x0, y0, x1, y1 = [float(v) for v in f.bbox]
                    kps = np.asarray(f.kps, dtype=np.float32)
                    iod = float(np.linalg.norm(kps[0] - kps[1]))
                    lm = f.get("landmark_2d_106")
                    eye_l = eye_openness(np.asarray(lm), kps[0], iod) if lm is not None else None
                    eye_r = eye_openness(np.asarray(lm), kps[1], iod) if lm is not None else None
                    pose = f.get("pose")
                    pitch, yaw, roll = (float(v) for v in pose) if pose is not None else (None, None, None)
                    # `normed_embedding` is a property of insightface's Face, not one of its dict keys.
                    emb = f.normed_embedding if f.get("embedding") is not None else None
                    con.execute(
                        "INSERT INTO face(id, model, box, score, size, yaw, pitch, roll, eye_l, eye_r, emb)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (id_, MODEL, f"[{x0 / w:.4f},{y0 / h:.4f},{x1 / w:.4f},{y1 / h:.4f}]", float(f.det_score),
                         (y1 - y0) / h, yaw, pitch, roll, eye_l, eye_r,
                         np.asarray(emb, np.float16).tobytes() if emb is not None else None),
                    )
                con.execute(
                    "INSERT OR REPLACE INTO signal(id, kind, model, value, data) VALUES (?,?,?,?,?)",
                    (id_, "faces", MODEL, len(faces), None),
                )
                total += len(faces)
    con.commit()
    dt = time.perf_counter() - t0
    log_timing(con, "signals.faces", run_id, prog.ok, dt, "cpu")
    con.commit()
    print(f"signals.faces: {prog.ok} images, {total} faces, {dt:.1f} s ({prog.ok / max(dt, 1e-6):.1f} img/s on cpu)")
