"""S3 faces: InsightFace buffalo_l (the same models Immich uses) on the 1536 px preview, on the CPU.

Eye openness is measured from the 106-point landmarks without relying on their index layout: the
contour points inside a small box around each eye centre of the 5-point detector are the eyelids
and corners, and their height/width ratio drops towards 0 when the eye closes.

Then each face's expression (P50): EmotiEffLib's EfficientNet-B0 (AffectNet: 8 expressions, valence and
arousal), ONNX on the CPU. Its happiness is how much a face smiles: on s6's 287 faces it agreed with the
local VLM asked "is this person smiling?" at AUC 0.85, and where it says happy (>= 0.5) the VLM agreed 99 %.
"""

import json
import time
from pathlib import Path

import numpy as np
from PIL import Image

from .config import Config
from .db import log_timing, put_signal
from .runtime import Progress

MODEL = "insightface/buffalo_l@2.1+emb"  # +emb: run 1 stored no embeddings
# sb-ai-lab/EmotiEffLib at 520a051c64, models/affectnet_emotions/onnx (Apache-2.0), in /srv/mimir/models
EXPR_FILE = "emotiefflib/enet_b0_8_va_mtl.onnx"   # sha256 c43e056a…7e812
EXPR_MODEL = "emotiefflib/enet_b0_8_va_mtl@1"
EXPRESSIONS = ["anger", "contempt", "disgust", "fear", "happiness", "neutral", "sadness", "surprise"]
EXPR_MIN_SCORE, EXPR_MIN_SIZE = 0.6, 0.03   # the faces rank counts; smaller ones come and go between frames
SMILING = 0.5                                # P(happiness) from which a face counts as smiling (precision 0.99 on s6)


def eye_openness(lm106: np.ndarray, center: np.ndarray, iod: float) -> float | None:
    d = lm106 - center
    sel = lm106[(np.abs(d[:, 0]) <= 0.30 * iod) & (np.abs(d[:, 1]) <= 0.15 * iod)]
    if len(sel) < 4:
        return None
    width = sel[:, 0].max() - sel[:, 0].min()
    return float((sel[:, 1].max() - sel[:, 1].min()) / max(width, 1e-6))


def run(cfg: Config, con, run_id: str, det_size=1024):
    detect(cfg, con, run_id, det_size)
    expressions(cfg, con, run_id)


def analysis(models_dir, device: str = "cpu", det_size=1024, threads: int | None = None):
    """InsightFace buffalo_l, on the CPU (with `threads` onnxruntime threads, or all cores) or on the 4090 (CUDA, without
    TF32, so it stays close to the CPU's)."""
    from insightface.app import FaceAnalysis

    from .helper import ort_options

    cuda = device == "cuda"
    providers = [("CUDAExecutionProvider", {"use_tf32": 0}), "CPUExecutionProvider"] if cuda else ["CPUExecutionProvider"]
    extra = {"sess_options": ort_options(threads)} if threads and not cuda else {}
    app = FaceAnalysis(name="buffalo_l", root=str(Path(models_dir) / "insightface"),
                       allowed_modules=["detection", "landmark_2d_106", "landmark_3d_68", "recognition"],
                       providers=providers, **extra)
    app.prepare(ctx_id=0 if cuda else -1, det_thresh=0.5, det_size=(det_size, det_size))
    return app


def face_rows(app, path) -> dict:
    """One preview's faces, as plain values: {"w", "h", "faces": [{"bbox", "score", "yaw", "pitch", "roll", "eye_l",
    "eye_r", "emb"}]}. The same on the CPU and in the 4090 worker."""
    img = Image.open(path).convert("RGB")
    bgr = np.asarray(img)[:, :, ::-1].copy()
    h, w = bgr.shape[:2]
    rows = []
    for f in app.get(bgr):
        kps = np.asarray(f.kps, dtype=np.float32)
        iod = float(np.linalg.norm(kps[0] - kps[1]))
        lm = f.get("landmark_2d_106")
        pose = f.get("pose")
        pitch, yaw, roll = (float(v) for v in pose) if pose is not None else (None, None, None)
        # `normed_embedding` is a property of insightface's Face, not one of its dict keys.
        emb = f.normed_embedding if f.get("embedding") is not None else None
        rows.append({"bbox": [float(v) for v in f.bbox], "score": float(f.det_score), "yaw": yaw, "pitch": pitch,
                     "roll": roll,
                     "eye_l": eye_openness(np.asarray(lm), kps[0], iod) if lm is not None else None,
                     "eye_r": eye_openness(np.asarray(lm), kps[1], iod) if lm is not None else None,
                     "emb": [float(v) for v in emb] if emb is not None else None})
    return {"w": w, "h": h, "faces": rows}


class Faces:
    """`omp worker`'s faces engine (the 4090, P61)."""

    def __init__(self, device):
        from .config import Config

        self.app = analysis(Config().models, "cuda" if device == "cuda" else "cpu")

    def __call__(self, path) -> dict:
        return face_rows(self.app, path)


def detect(cfg: Config, con, run_id: str, det_size=1024):
    """The faces of every preview: here on the CPU, and on the 4090 through the work queue (helper.Share, P61)."""
    from .helper import ENABLED, LOCAL_ORT_THREADS, Share

    done = {r[0] for r in con.execute("SELECT id FROM signal WHERE kind='faces' AND model=?", (MODEL,))}
    ids = [r[0] for r in con.execute("SELECT id FROM signal WHERE kind='sharp' ORDER BY id") if r[0] not in done]
    if not ids:
        return
    app = analysis(cfg.models, "cpu", det_size, threads=LOCAL_ORT_THREADS if ENABLED else None)
    t0 = time.perf_counter()
    total = 0
    with Progress(con, "signals.faces", MODEL, total=len(ids)) as prog:
        share = Share(cfg, "faces", ids)
        for id_, r in share:
            with prog.item(id_):
                r = r or face_rows(app, cfg.preview(id_))
                w, h = r["w"], r["h"]
                con.execute("DELETE FROM face WHERE id=?", (id_,))  # older models' rows too
                con.execute("DELETE FROM face_expr WHERE id=?", (id_,))  # new faces, new expressions
                con.execute("DELETE FROM signal WHERE id=? AND kind='expression'", (id_,))
                for f in r["faces"]:
                    x0, y0, x1, y1 = f["bbox"]
                    con.execute(
                        "INSERT INTO face(id, model, box, score, size, yaw, pitch, roll, eye_l, eye_r, emb)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (id_, MODEL, f"[{x0 / w:.4f},{y0 / h:.4f},{x1 / w:.4f},{y1 / h:.4f}]", f["score"],
                         (y1 - y0) / h, f["yaw"], f["pitch"], f["roll"], f["eye_l"], f["eye_r"],
                         np.asarray(f["emb"], np.float16).tobytes() if f["emb"] is not None else None),
                    )
                con.execute(
                    "INSERT OR REPLACE INTO signal(id, kind, model, value, data) VALUES (?,?,?,?,?)",
                    (id_, "faces", MODEL, len(r["faces"]), None),
                )
                total += len(r["faces"])
    con.commit()
    dt = time.perf_counter() - t0
    log_timing(con, "signals.faces", run_id, prog.ok, dt, "cpu" + share.note())
    con.commit()
    print(f"signals.faces: {prog.ok} images, {total} faces, {dt:.1f} s ({prog.ok / max(dt, 1e-6):.1f} img/s,"
          f" cpu{share.note()})")


def square_crop(img: np.ndarray, box) -> np.ndarray:
    """The square around a face's box, side = its longer edge, clipped to the image: on s6 that agreed best
    with the VLM (AUC 0.85, vs 0.83 for the box as it is and 0.79 with a 20 % margin)."""
    h, w = img.shape[:2]
    x0, y0, x1, y1 = box
    cx, cy, side = (x0 + x1) / 2 * w, (y0 + y1) / 2 * h, max((x1 - x0) * w, (y1 - y0) * h)
    return img[max(0, int(cy - side / 2)):min(h, int(cy + side / 2)), max(0, int(cx - side / 2)):min(w, int(cx + side / 2))]


class Expression:
    """EmotiEffLib's preprocessing (facial_analysis.py, ONNX): RGB face, 224 px bilinear, ImageNet mean/std.
    The output is 8 expression logits, then valence and arousal."""

    MEAN, STD = np.array([0.485, 0.456, 0.406]), np.array([0.229, 0.224, 0.225])

    def __init__(self, cfg: Config):
        import cv2
        import onnxruntime as ort

        self.cv2 = cv2
        self.sess = ort.InferenceSession(str(cfg.models / EXPR_FILE), providers=["CPUExecutionProvider"])

    def __call__(self, face: np.ndarray) -> dict:
        x = self.cv2.resize(face, (224, 224)) / 255
        x = ((x - self.MEAN) / self.STD).transpose(2, 0, 1).astype("float32")[None]
        out = self.sess.run(None, {"input": x})[0][0]
        p = np.exp(out[:8] - out[:8].max())
        p /= p.sum()
        return {"happy": float(p[4]), "valence": float(out[8]), "arousal": float(out[9]),
                "top": EXPRESSIONS[int(p.argmax())], "probs": {e: round(float(v), 4) for e, v in zip(EXPRESSIONS, p)}}


def expressions(cfg: Config, con, run_id: str):
    """Every counted face's expression, and per photo how much its people smile: the `expression` signal's
    value is the mean P(happiness) of its faces (None without faces), used by rank, captions and tags."""
    done = {r[0] for r in con.execute("SELECT id FROM signal WHERE kind='expression' AND model=?", (EXPR_MODEL,))}
    ids = [r[0] for r in con.execute("SELECT id FROM signal WHERE kind='faces' AND model=? ORDER BY id", (MODEL,))
           if r[0] not in done]
    if not ids:
        return
    model = Expression(cfg)
    t0 = time.perf_counter()
    total = 0
    with Progress(con, "signals.expression", EXPR_MODEL, total=len(ids)) as prog:
        for id_ in ids:
            with prog.item(id_):
                faces = con.execute("SELECT fid, box FROM face WHERE id=? AND score >= ? AND size >= ?",
                                    (id_, EXPR_MIN_SCORE, EXPR_MIN_SIZE)).fetchall()
                con.execute("DELETE FROM face_expr WHERE id=?", (id_,))
                found = []
                if faces:
                    img = np.asarray(Image.open(cfg.preview(id_)).convert("RGB"))
                    for f in faces:
                        e = model(square_crop(img, json.loads(f["box"])))
                        con.execute("INSERT INTO face_expr(fid, id, model, happy, valence, arousal, top, probs)"
                                    " VALUES (?,?,?,?,?,?,?,?)", (f["fid"], id_, EXPR_MODEL, e["happy"], e["valence"],
                                                                 e["arousal"], e["top"], json.dumps(e["probs"])))
                        found.append(e)
                total += len(found)
                happy = [e["happy"] for e in found]
                put_signal(con, id_, "expression", EXPR_MODEL, value=float(np.mean(happy)) if happy else None, data={
                    "faces": len(found), "smiling": sum(h >= SMILING for h in happy),
                    "happy_max": round(max(happy), 4) if happy else None,
                    "valence": round(float(np.mean([e["valence"] for e in found])), 4) if found else None})
    con.commit()
    dt = time.perf_counter() - t0
    log_timing(con, "signals.expression", run_id, prog.ok, dt, "cpu")
    con.commit()
    print(f"signals.expression: {prog.ok} images, {total} faces, {dt:.1f} s ({prog.ok / max(dt, 1e-6):.1f} img/s on cpu)")


WORDS = {"happiness": "smiling", "neutral": "neutral", "surprise": "surprised", "sadness": "sad", "anger": "angry",
         "fear": "scared", "disgust": "disgusted", "contempt": "scornful"}


def described(con) -> dict[str, str]:
    """Photo id -> how its people look, for its caption (P50): left to right, with the names people.py knows,
    for the faces the model is fairly sure about (P >= 0.5). "Jan smiling, Ola neutral, someone surprised"."""
    q = ("SELECT e.id, e.top, e.probs, f.box, {} FROM face_expr e JOIN face f ON f.fid = e.fid {}")
    try:  # only the names the user gave, as people.recognized (P67)
        rows = con.execute(q.format("CASE WHEN p.source = 'user' THEN p.short END AS short, p.owner",
                                    "LEFT JOIN person p ON p.pid = f.person")).fetchall()
    except Exception:  # noqa: BLE001 - no people stage yet
        rows = con.execute(q.format("NULL AS short, 0 AS owner", "")).fetchall()
    from .people import owner_short  # noqa: PLC0415

    mine = owner_short(con)
    by = {}
    for r in rows:
        if json.loads(r["probs"])[r["top"]] < 0.5:
            continue
        x0, _, x1, _ = json.loads(r["box"])
        who = r["short"] or ((mine or "the owner") if r["owner"] else "someone")
        by.setdefault(r["id"], []).append(((x0 + x1) / 2, who, WORDS[r["top"]]))
    out = {}
    for i, fs in by.items():
        fs.sort()
        if len(fs) > 6:
            out[i] = f"{len(fs)} faces, {sum(w == 'smiling' for _, _, w in fs)} of them smiling"
        else:
            out[i] = ", ".join(f"{who} {w}" for _, who, w in fs)
    return out
