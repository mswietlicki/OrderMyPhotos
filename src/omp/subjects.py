"""S3 subjects (decisions.md P22): every person and animal in the frame, with its pose and what it's doing.

RT-DETR finds people and animals, ViTPose++ gives their keypoints (the COCO head for people, the AP-10K
head for animals), and SigLIP 2 names the action from the subject's crop. Two frames that look alike as a
whole are still different photos when a main subject moved, turned or started doing something else.
"""

import json
import time

import numpy as np
import torch
from PIL import Image

from .config import Config
from .db import log_timing
from .runtime import Progress
from .vision import SIGLIP, device, free, siglip_text

DET = "PekingU/rtdetr_r50vd_coco_o365"
POSE = "usyd-community/vitpose-plus-base"
MODEL = "rtdetr-r50-o365+vitpose-plus-base+siglip2@1"
POSE_PEOPLE, POSE_ANIMALS = 0, 3   # ViTPose++ expert heads: 0 = COCO body, 3 = AP-10K animals (per the paper)
MIN_SIZE = 0.03                     # subjects under 3 % of the frame height are ignored
MAX_SUBJECTS = 12
ANIMALS = {"dog", "cat", "horse", "bird", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "rabbit", "deer",
           "duck", "goose", "chicken", "pig", "goat", "squirrel", "fox", "monkey"}

# (key, English prompt, Polish prompt). Averaged, like the S3b kinds.
PERSON_ACTIONS = [
    ("standing", "a person standing still", "osoba stojąca w miejscu"),
    ("walking", "a person walking", "osoba idąca"),
    ("running", "a person running", "osoba biegnąca"),
    ("jumping", "a person jumping in the air", "osoba skacząca w powietrzu"),
    ("sitting", "a person sitting", "osoba siedząca"),
    ("lying", "a person lying down", "osoba leżąca"),
    ("skiing", "a person skiing", "osoba jadąca na nartach"),
    ("climbing", "a person climbing", "osoba wspinająca się"),
    ("cycling", "a person riding a bicycle", "osoba jadąca na rowerze"),
    ("swimming", "a person swimming", "osoba pływająca"),
    ("dancing", "a person dancing", "osoba tańcząca"),
    ("hugging", "people hugging", "ludzie przytulający się"),
    ("kissing", "people kissing", "ludzie całujący się"),
    ("eating", "a person eating or drinking", "osoba jedząca lub pijąca"),
    ("talking", "a person talking", "osoba rozmawiająca"),
    ("laughing", "a person laughing", "osoba śmiejąca się"),
    ("posing", "a person posing for the camera", "osoba pozująca do zdjęcia"),
    ("waving", "a person waving a hand", "osoba machająca ręką"),
    ("pointing", "a person pointing at something", "osoba wskazująca coś"),
    ("holding a child", "a person holding a child", "osoba trzymająca dziecko"),
    ("playing", "a person playing", "osoba bawiąca się"),
    ("working", "a person working with their hands", "osoba pracująca rękami"),
]
ANIMAL_ACTIONS = [
    ("standing", "an animal standing", "zwierzę stojące"),
    ("walking", "an animal walking", "zwierzę idące"),
    ("running", "an animal running", "zwierzę biegnące"),
    ("jumping", "an animal jumping", "zwierzę skaczące"),
    ("sitting", "an animal sitting", "zwierzę siedzące"),
    ("lying", "an animal lying down", "zwierzę leżące"),
    ("eating", "an animal eating", "zwierzę jedzące"),
    ("playing", "an animal playing", "zwierzę bawiące się"),
    ("sleeping", "an animal sleeping", "zwierzę śpiące"),
    ("looking at the camera", "an animal looking at the camera", "zwierzę patrzące w obiektyw"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS subject (
  sid INTEGER PRIMARY KEY,
  id TEXT NOT NULL, model TEXT NOT NULL,
  kind TEXT NOT NULL,              -- person | animal
  label TEXT,                      -- detector class (person, dog, cat, ...)
  box TEXT NOT NULL,               -- JSON [x0,y0,x1,y1], normalized
  score REAL, size REAL,           -- detector score; box height / image height
  kps TEXT,                        -- JSON [[x, y, score], ...] normalized to the box (0..1)
  facing TEXT,                     -- camera | side | away | unknown
  cut INTEGER,                     -- the box touches the frame edge
  action TEXT, action_p REAL, actions TEXT  -- top action, its probability, JSON top-3
);
CREATE INDEX IF NOT EXISTS subject_photo ON subject(id);
"""


def facing_of(kps: np.ndarray, kind: str) -> str:
    """From the face keypoints (COCO: nose, eyes, ears; AP-10K: eyes, nose)."""
    if kind == "person":
        nose, le, re_, lear, rear = (kps[i, 2] for i in range(5))
        if nose > 0.5 and le > 0.5 and re_ > 0.5:
            return "camera"
        if nose > 0.4 and (le > 0.4) != (re_ > 0.4):
            return "side"
        if nose < 0.3 and le < 0.3 and re_ < 0.3 and (lear > 0.3 or rear > 0.3 or kps[5, 2] > 0.5):
            return "away"
        return "unknown"
    le, re_, nose = kps[0, 2], kps[1, 2], kps[2, 2]
    if le > 0.5 and re_ > 0.5:
        return "camera"
    if nose > 0.4 and (le > 0.4) != (re_ > 0.4):
        return "side"
    return "unknown"


def run(cfg: Config, con, run_id: str):
    from transformers import (AutoModel, AutoProcessor, RTDetrForObjectDetection, RTDetrImageProcessor,
                              VitPoseForPoseEstimation)

    con.executescript(SCHEMA)
    done = {r[0] for r in con.execute("SELECT id FROM signal WHERE kind='subjects' AND model=?", (MODEL,))}
    ids = [r[0] for r in con.execute("SELECT id FROM signal WHERE kind='sharp' ORDER BY id") if r[0] not in done]
    if not ids:
        return
    dev = device()
    dtype = torch.float16 if dev != "cpu" else torch.float32
    det_proc = RTDetrImageProcessor.from_pretrained(DET)
    det = RTDetrForObjectDetection.from_pretrained(DET).to(dev).eval()   # fp32: detection heads are touchy in fp16
    pose_proc = AutoProcessor.from_pretrained(POSE)
    pose = VitPoseForPoseEstimation.from_pretrained(POSE, dtype=dtype).to(dev).eval()
    sig = AutoModel.from_pretrained(SIGLIP, dtype=dtype).to(dev).eval()
    sig_proc = AutoProcessor.from_pretrained(SIGLIP)

    def action_text(table):
        t = siglip_text(sig, sig_proc, [p for _, en, pl in table for p in (en, pl)], dev)
        return torch.nn.functional.normalize(t.view(len(table), 2, -1).mean(1), dim=-1)

    texts = {"person": (PERSON_ACTIONS, action_text(PERSON_ACTIONS)), "animal": (ANIMAL_ACTIONS, action_text(ANIMAL_ACTIONS))}
    scale, bias = sig.logit_scale.detach().exp().float(), sig.logit_bias.detach().float()
    id2label = det.config.id2label
    t0 = time.perf_counter()
    n_subj = 0
    with Progress(con, "signals.subjects", MODEL, total=len(ids)) as prog:
        for id_ in ids:
            with prog.item(id_):
                img = Image.open(cfg.preview(id_)).convert("RGB")
                w, h = img.size
                with torch.inference_mode():
                    inputs = det_proc(images=img, return_tensors="pt").to(dev)
                    out = det(**inputs)
                # Post-processing on the CPU: boolean-mask indexing on XPU returns garbage (torch 2.15.0+xpu).
                out.logits, out.pred_boxes = out.logits.float().cpu(), out.pred_boxes.float().cpu()
                res = det_proc.post_process_object_detection(out, target_sizes=torch.tensor([[h, w]]), threshold=0.4)[0]
                found = []
                for score, label, box in zip(res["scores"].tolist(), res["labels"].tolist(), res["boxes"].tolist()):
                    name = id2label[label].lower()
                    kind = "person" if name == "person" else "animal" if name in ANIMALS else None
                    x0, y0, x1, y1 = max(0, box[0]), max(0, box[1]), min(w, box[2]), min(h, box[3])
                    if kind and (y1 - y0) / h >= MIN_SIZE and x1 > x0:
                        found.append({"kind": kind, "label": name, "score": score, "box": [x0, y0, x1, y1]})
                found = sorted(found, key=lambda s: -(s["box"][3] - s["box"][1]))[:MAX_SUBJECTS]
                con.execute("DELETE FROM subject WHERE id=? AND model=?", (id_, MODEL))
                for kind, head in (("person", POSE_PEOPLE), ("animal", POSE_ANIMALS)):
                    group = [s for s in found if s["kind"] == kind]
                    if not group:
                        continue
                    coco = [[s["box"][0], s["box"][1], s["box"][2] - s["box"][0], s["box"][3] - s["box"][1]] for s in group]
                    with torch.inference_mode():
                        pin = pose_proc(img, boxes=[coco], return_tensors="pt").to(dev)
                        pin["pixel_values"] = pin["pixel_values"].to(dtype)
                        pout = pose(**pin, dataset_index=torch.full((len(group),), head, device=dev))
                    pout.heatmaps = pout.heatmaps.float().cpu()
                    poses = pose_proc.post_process_pose_estimation(pout, boxes=[coco])[0]
                    crops = []
                    for s, p in zip(group, poses):
                        x0, y0, x1, y1 = s["box"]
                        kp = p["keypoints"].float().cpu().numpy()
                        sc = p["scores"].float().cpu().numpy()
                        rel = np.stack([(kp[:, 0] - x0) / max(x1 - x0, 1), (kp[:, 1] - y0) / max(y1 - y0, 1), sc], 1)
                        s["kps"] = rel
                        mx, my = (x1 - x0) * 0.1, (y1 - y0) * 0.1
                        crops.append(img.crop((max(0, x0 - mx), max(0, y0 - my), min(w, x1 + mx), min(h, y1 + my))))
                    table, tvec = texts[kind]
                    with torch.inference_mode():
                        si = sig_proc(images=crops, return_tensors="pt", max_num_patches=256).to(dev)
                        si["pixel_values"] = si["pixel_values"].to(dtype)
                        emb = sig.get_image_features(**si)
                        emb = emb if torch.is_tensor(emb) else emb.pooler_output
                        emb = torch.nn.functional.normalize(emb.float(), dim=-1)
                        probs = torch.softmax(emb @ tvec.T * scale + bias, -1).cpu().numpy()
                    for s, pr in zip(group, probs):
                        order = np.argsort(-pr)
                        x0, y0, x1, y1 = s["box"]
                        cut = int(x0 <= 0.01 * w or y0 <= 0.01 * h or x1 >= 0.99 * w or y1 >= 0.99 * h)
                        con.execute(
                            "INSERT INTO subject(id, model, kind, label, box, score, size, kps, facing, cut, action, action_p, actions)"
                            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (id_, MODEL, kind, s["label"], json.dumps([round(x0 / w, 4), round(y0 / h, 4), round(x1 / w, 4), round(y1 / h, 4)]),
                             s["score"], (y1 - y0) / h, json.dumps(np.round(s["kps"], 3).tolist()), facing_of(s["kps"], kind), cut,
                             table[order[0]][0], float(pr[order[0]]),
                             json.dumps([[table[j][0], round(float(pr[j]), 3)] for j in order[:3]])))
                        n_subj += 1
                con.execute("INSERT OR REPLACE INTO signal(id, kind, model, value) VALUES (?,?,?,?)",
                            (id_, "subjects", MODEL, len(found)))
                con.commit()
    dt = time.perf_counter() - t0
    log_timing(con, "signals.subjects", run_id, prog.ok, dt, dev)
    con.commit()
    print(f"signals.subjects: {prog.ok} images, {n_subj} subjects, {dt:.1f} s ({prog.ok / max(dt, 1e-6):.1f} img/s on {dev})")
    del det, pose, sig
    free()
