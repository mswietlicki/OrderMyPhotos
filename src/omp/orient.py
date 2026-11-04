"""Which way up each photo is (decisions.md P36): a phone's orientation sensor guesses wrong when it
points down at a page or a table, so the EXIF Orientation alone isn't enough (user, 2026-11-03: "a lot of
photos don't seem rotated correctly").

1. A screen that learns from the library itself. Most photos are upright once their EXIF orientation is
   applied, so a sample of them, turned by 90, 180 and 270 degrees, gives labelled examples for free: a
   logistic regression on SigLIP 2 embeddings learns the four turns. A photo is then looked at in all four
   turns, and the turn that makes the four views agree wins. On 200 held-out photos turned at random that
   finds the turn 93.5 % of the time (one view alone: 68.5 %).
2. Shots pointing down at a table or a page have no natural "up", and there the screen is right but unsure
   (its margins 0.4-2.5 on the sample's turned pages). So a photo the screen calls turned is checked with
   the resident VLM, as the text stage does (P24): "is it upright?" as it is and turned back. The turn is
   proposed only when the VLM prefers the turned photo.
Text pages keep the text stage's own answer.
"""

import random
import time

import numpy as np
import torch
from PIL import Image

from .config import Config
from .db import get_vecs, log_timing, put_signal
from .runtime import check
from .vision import SIGLIP, device, free, load_preview
from .vlm import VLM, image_part

MODEL = SIGLIP + "+orient@2"
TRAIN = 1500          # photos, each also turned three ways
SCREEN = 0.3          # the four views' margin for a turn to be checked with the VLM
TURNS = (0, 90, 180, 270)  # degrees clockwise the photo is turned from upright (the class)
UPRIGHT = ("Is this photo upright, the way it was meant to be seen (people, text, objects and the horizon the "
           "right way up)? Answer with one word: yes or no.")


def run(cfg: Config, con, run_id: str):
    from sklearn.linear_model import LogisticRegression
    from transformers import AutoModel, AutoProcessor

    stored = get_vecs(con, "siglip")
    done = {r[0] for r in con.execute("SELECT id FROM signal WHERE kind='orient' AND model=?", (MODEL,))}
    try:
        pages = {r[0] for r in con.execute("SELECT id FROM text WHERE mostly_text=1")}
    except Exception:  # noqa: BLE001 - no text stage yet
        pages = set()
    todo = [i for i in stored if i not in done and i not in pages]
    if not todo:
        return
    t0 = time.perf_counter()
    dev = device()
    dtype = torch.float16 if dev != "cpu" else torch.float32
    model = AutoModel.from_pretrained(SIGLIP, dtype=dtype).to(dev).eval()
    processor = AutoProcessor.from_pretrained(SIGLIP)

    def embed(img):
        inputs = processor(images=[img], return_tensors="pt", max_num_patches=576).to(dev)
        inputs["pixel_values"] = inputs["pixel_values"].to(dtype)
        with torch.inference_mode():
            out = model.get_image_features(**inputs)
        out = out if torch.is_tensor(out) else out.pooler_output
        return torch.nn.functional.normalize(out.float(), dim=-1)[0].cpu().numpy()

    def views(img, as_is=None):
        """The photo turned clockwise by 0, 90, 180 and 270 degrees (PIL turns counter-clockwise)."""
        return [as_is if as_is is not None and d == 0 else embed(img.rotate(-d, expand=True)) for d in TURNS]

    # 1. The screen, trained on the library's own photos turned four ways.
    train = random.Random(13).sample(sorted(stored), min(TRAIN, len(stored)))
    X, y = [], []
    for i in train:
        check()  # a stop ends it between photos
        try:
            img = load_preview(cfg, i, 1024)
        except (FileNotFoundError, OSError):
            continue
        for k, v in enumerate(views(img, stored[i])):
            X.append(v)
            y.append(k)
    clf = LogisticRegression(C=2.0, max_iter=3000).fit(np.array(X), np.array(y))
    t1 = time.perf_counter()

    def turn_of(img, as_is):
        """(clockwise turn the photo has, margin over the next best): the turn t that maximises
        sum_k log P(class (t + k) | the photo turned by k more)."""
        P = np.log(clf.predict_proba(np.array(views(img, as_is))) + 1e-9)
        scores = [sum(P[k, (t + k) % 4] for k in range(4)) for t in range(4)]
        best = int(np.argmax(scores))
        return TURNS[best], float(scores[best] - sorted(scores)[-2])

    # 2. The screen looks at every photo. What it flags waits for the VLM, so the chat model is asked in one
    # stretch at the end, and the 4090 is free for other work while the screen runs (P51).
    flagged = []
    for i in todo:
        check()
        try:
            img = load_preview(cfg, i, 1024)
        except (FileNotFoundError, OSError):
            continue
        turn, margin = turn_of(img, stored[i])
        data = {"screen": turn, "margin": round(margin, 2)}
        if turn and margin >= SCREEN:
            flagged.append((i, turn, data))
            continue
        put_signal(con, i, "orient", MODEL, value=0, data=data)
        con.commit()
    del model
    free()

    # 3. The VLM checks what the screen flagged.
    vlm = VLM(cfg.llm)

    def p_upright(img: Image.Image) -> float:
        ans = vlm.choice([image_part(img), {"type": "text", "text": UPRIGHT}], ["yes", "no"])
        return ans.get("yes", 0.5) if ans else 0.5

    turned = 0
    for i, turn, data in flagged:
        check()
        img, fix = load_preview(cfg, i, 1024), 0
        now, back = p_upright(img), p_upright(img.rotate(turn, expand=True))  # rotate(turn): undo it
        data |= {"vlm_as_is": round(now, 3), "vlm_turned_back": round(back, 3)}
        if back >= 0.5 > now:
            fix = (360 - turn) % 360  # degrees clockwise that make it upright
            turned += 1
        put_signal(con, i, "orient", MODEL, value=fix, data=data)
        con.commit()
    flagged = len(flagged)
    t2 = time.perf_counter()
    log_timing(con, "orient", run_id, len(todo), t2 - t0, f"{dev} + 4090 qwen3.8-27b")
    con.commit()
    print(f"orient: screen trained on {len(train)} photos x 4 turns in {t1 - t0:.0f} s; {len(todo)} photos looked at"
          f" in 4 turns, {flagged} flagged, {turned} turned by the VLM's check; {vlm.stats()}; {t2 - t0:.0f} s")
