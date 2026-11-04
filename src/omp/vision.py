"""S3 learned signals: embeddings, zero-shot kinds, IQA. One model at a time on the GPU, so the peak
fits next to the always-on RAG models on the B50 (~8 GiB free)."""

import gc
import time

import numpy as np
import torch
from PIL import Image

from .config import Config
from .db import done_ids, get_vecs, log_timing, put_signal
from .runtime import Progress

DINO = "facebook/dinov2-base"
SIGLIP = "google/siglip2-so400m-patch16-naflex"
QREALIGN = "q-future/Q-ReAlign-Mini-0.8B"


def device() -> str:
    if hasattr(torch, "xpu") and torch.xpu.is_available():
        return "xpu"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def free():
    gc.collect()
    dev = device()
    if dev == "xpu":
        torch.xpu.empty_cache()
    elif dev == "cuda":
        torch.cuda.empty_cache()


def sync():
    dev = device()
    if dev == "xpu":
        torch.xpu.synchronize()
    elif dev == "cuda":
        torch.cuda.synchronize()


def todo_ids(con, kind, model):
    done = done_ids(con, kind, model)
    rows = con.execute(
        "SELECT p.id FROM photo p WHERE EXISTS (SELECT 1 FROM signal s WHERE s.id=p.id AND s.kind='sharp')"
        " ORDER BY p.taken_ts"
    ).fetchall()
    return [r[0] for r in rows if r[0] not in done]


def load_preview(cfg, id_, long_side=None) -> Image.Image:
    img = Image.open(cfg.preview(id_)).convert("RGB")
    if long_side and max(img.size) > long_side:
        s = long_side / max(img.size)
        img = img.resize((round(img.width * s), round(img.height * s)), Image.Resampling.LANCZOS)
    return img


def _timed(con, run_id, stage, n, t0, dev):
    sync()
    dt = time.perf_counter() - t0
    log_timing(con, stage, run_id, n, dt, dev)
    con.commit()
    print(f"{stage}: {n} images, {dt:.1f} s ({n / max(dt, 1e-6):.1f} img/s on {dev})")


# --- DINOv2: near-duplicate / burst embedding ------------------------------------------------------

# The models as engines: one photo's preview in, its result out (JSON-able), the same code here on the B50
# and in `omp worker` on the 4090, which takes photos from the other end of a stage's list (P49).

def open_preview(path, long_side=None) -> Image.Image:
    img = Image.open(path).convert("RGB")
    if long_side and max(img.size) > long_side:
        s = long_side / max(img.size)
        img = img.resize((round(img.width * s), round(img.height * s)), Image.Resampling.LANCZOS)
    return img


class Dino:
    def __init__(self, dev):
        from transformers import AutoModel

        self.dev, self.dtype = dev, torch.float16 if dev != "cpu" else torch.float32
        self.model = AutoModel.from_pretrained(DINO, dtype=self.dtype).to(dev).eval()
        self.mean = torch.tensor([0.485, 0.456, 0.406]).view(3, 1, 1)
        self.std = torch.tensor([0.229, 0.224, 0.225]).view(3, 1, 1)

    def __call__(self, path) -> dict:
        img = open_preview(path)
        # Keep the aspect ratio: long side 448, both sides multiples of the 14 px patch.
        s = 448 / max(img.size)
        w, h = (max(14, round(img.width * s / 14) * 14), max(14, round(img.height * s / 14) * 14))
        x = torch.from_numpy(np.asarray(img.resize((w, h), Image.Resampling.BICUBIC), np.float32) / 255)
        x = ((x.permute(2, 0, 1) - self.mean) / self.std).unsqueeze(0).to(self.dev, self.dtype)
        with torch.inference_mode():
            out = self.model(pixel_values=x)
        cls = torch.nn.functional.normalize(out.pooler_output.float(), dim=-1)[0].cpu().numpy()
        patch = torch.nn.functional.normalize(out.last_hidden_state[:, 1:].float().mean(1), dim=-1)[0].cpu().numpy()
        return {"cls": cls.tolist(), "patch": patch.tolist()}


def run_dino(cfg: Config, con, run_id: str):
    from .helper import Share

    ids = todo_ids(con, "dino", DINO)
    if not ids:
        return
    dev = device()
    engine = Dino(dev)
    t0 = time.perf_counter()
    with Progress(con, "signals.dino", DINO, total=len(ids)) as prog:
        share = Share(cfg, "dino", ids)
        for id_, r in share:
            with prog.item(id_):
                r = r or engine(cfg.preview(id_))
                if "error" in r:
                    raise RuntimeError(r["error"])
                put_signal(con, id_, "dino", DINO, vec=r["cls"])
                put_signal(con, id_, "dino_patch", DINO, vec=r["patch"])
    _timed(con, run_id, "signals.dino", prog.ok, t0, dev + share.note())
    del engine
    free()


# --- SigLIP 2: semantic embedding, zero-shot purpose/category, tag verification --------------------

# (key, purpose, prompts). English and Polish prompts are averaged; Polish is the user's language.
KINDS = [
    ("people", "memory", ["a photo of people", "zdjęcie ludzi"]),
    ("selfie", "memory", ["a selfie", "selfie"]),
    ("landscape", "memory", ["a landscape photo", "zdjęcie krajobrazu"]),
    ("city", "memory", ["a photo of a street or buildings in a town", "zdjęcie ulicy lub budynków w mieście"]),
    ("sport", "memory", ["a photo of people doing sport outdoors", "zdjęcie ludzi uprawiających sport"]),
    ("animal", "memory", ["a photo of an animal", "zdjęcie zwierzęcia"]),
    ("event", "memory", ["a photo of a party or celebration", "zdjęcie z imprezy lub uroczystości"]),
    ("interior", "memory", ["a photo of a room interior", "zdjęcie wnętrza pokoju"]),
    ("document", "reference", ["a photo of a paper document with text", "zdjęcie dokumentu z tekstem"]),
    ("receipt", "reference", ["a photo of a shop receipt or invoice", "zdjęcie paragonu lub faktury"]),
    ("label", "reference", ["a close-up photo of a label with a serial number or barcode",
                            "zdjęcie tabliczki znamionowej z numerem seryjnym"]),
    ("screen", "reference", ["a photo of a computer screen or display", "zdjęcie ekranu komputera"]),
    ("whiteboard", "reference", ["a photo of a whiteboard, slide or information board",
                                 "zdjęcie tablicy informacyjnej"]),
    ("map", "reference", ["a photo of a map or trail map", "zdjęcie mapy szlaków"]),
    ("food", "reference", ["a photo of food on a plate", "zdjęcie jedzenia na talerzu"]),
    ("product", "reference", ["a photo of a product in a shop", "zdjęcie produktu w sklepie"]),
    ("repair", "reference", ["a photo of pipes, wiring or a meter for a home repair",
                             "zdjęcie rur, kabli lub licznika"]),
    ("screenshot", "media", ["a screenshot of a phone app", "zrzut ekranu telefonu"]),
    ("meme", "media", ["a meme with text", "mem z napisem"]),
    ("wallpaper", "media", ["a desktop or phone wallpaper image", "tapeta na pulpit lub telefon"]),
    ("art", "media", ["digital art, an illustration or concept art", "grafika cyfrowa, ilustracja lub concept art"]),
    ("infographic", "media", ["an infographic or a chart with text", "infografika lub wykres z tekstem"]),
    ("accidental", "junk", ["an accidental photo taken inside a pocket, completely dark or blurry",
                            "przypadkowe zdjęcie, ciemne i rozmazane"]),
    ("floor", "junk", ["an accidental photo of the floor or the ceiling", "przypadkowe zdjęcie podłogi"]),
]


def siglip_text(model, processor, texts, dev):
    inputs = processor(text=texts, padding="max_length", max_length=64, return_tensors="pt").to(dev)
    with torch.inference_mode():
        out = model.get_text_features(**inputs)
    out = out if torch.is_tensor(out) else out.pooler_output
    return torch.nn.functional.normalize(out.float(), dim=-1)


KINDS_MODEL = SIGLIP + "+zeroshot@2"  # @2: wallpaper, art and infographic (media) added


def kind_row(soft, probs) -> tuple[float, dict]:
    purposes = {}
    for (key, purpose, _), p in zip(KINDS, soft):
        purposes[purpose] = purposes.get(purpose, 0.0) + float(p)
    top = sorted(zip([k_ for k_, _, _ in KINDS], soft.tolist(), probs.tolist()), key=lambda t: -t[1])[:3]
    return purposes.get("memory", 0.0), {"purpose": max(purposes, key=purposes.get), "purposes": purposes,
                                         "top": [{"key": a, "p": round(b, 4), "sigmoid": round(c, 4)} for a, b, c in top]}


class Siglip:
    def __init__(self, dev):
        from transformers import AutoModel, AutoProcessor

        self.dev, self.dtype = dev, torch.float16 if dev != "cpu" else torch.float32
        self.model = AutoModel.from_pretrained(SIGLIP, dtype=self.dtype).to(dev).eval()
        self.processor = AutoProcessor.from_pretrained(SIGLIP)

    def embed(self, img: Image.Image) -> np.ndarray:
        inputs = self.processor(images=[img], return_tensors="pt", max_num_patches=576).to(self.dev)
        inputs["pixel_values"] = inputs["pixel_values"].to(self.dtype)
        with torch.inference_mode():
            out = self.model.get_image_features(**inputs)
        out = out if torch.is_tensor(out) else out.pooler_output
        return torch.nn.functional.normalize(out.float(), dim=-1)[0].cpu().numpy()

    def __call__(self, path) -> dict:
        return {"emb": self.embed(open_preview(path, 1024)).tolist()}


def run_siglip(cfg: Config, con, run_id: str):
    from .helper import Share

    ids = todo_ids(con, "siglip", SIGLIP)
    # Kinds from an older label list are recomputed from the stored embeddings: no image pass needed.
    stale = [r[0] for r in con.execute("SELECT s.id FROM signal s WHERE s.kind='siglip' AND NOT EXISTS"
                                       " (SELECT 1 FROM signal k WHERE k.id=s.id AND k.kind='kind' AND k.model=?)",
                                       (KINDS_MODEL,))]
    if not ids and not stale:
        return
    dev = device()
    engine = Siglip(dev)
    model, processor = engine.model, engine.processor
    prompts = [p for _, _, ps in KINDS for p in ps]
    text = siglip_text(model, processor, prompts, dev)
    k = 2
    text = torch.nn.functional.normalize(text.view(len(KINDS), k, -1).mean(1), dim=-1)
    scale, bias = model.logit_scale.detach().exp().float(), model.logit_bias.detach().float()
    t0 = time.perf_counter()
    with Progress(con, "signals.siglip", SIGLIP, total=len(ids)) as prog:
        share = Share(cfg, "siglip", ids)
        for id_, r in share:
            with prog.item(id_):
                r = r or engine(cfg.preview(id_))
                if "error" in r:
                    raise RuntimeError(r["error"])
                emb = torch.tensor(r["emb"], device=dev, dtype=torch.float32)[None]
                logits = (emb @ text.T) * scale + bias
                value, data = kind_row(torch.softmax(logits[0], -1).cpu().numpy(), torch.sigmoid(logits)[0].cpu().numpy())
                put_signal(con, id_, "siglip", SIGLIP, vec=r["emb"])
                put_signal(con, id_, "kind", KINDS_MODEL, value=value, data=data)
    if ids:
        _timed(con, run_id, "signals.siglip", prog.ok, t0, dev + share.note())
    stored = get_vecs(con, "siglip")
    for id_ in stale:
        if id_ in stored:
            emb = torch.from_numpy(stored[id_]).to(dev).float()[None]
            logits = (emb @ text.T) * scale + bias
            value, data = kind_row(torch.softmax(logits[0], -1).cpu().numpy(), torch.sigmoid(logits)[0].cpu().numpy())
            put_signal(con, id_, "kind", KINDS_MODEL, value=value, data=data)
    con.commit()
    if stale:
        print(f"signals.siglip: kinds recomputed for {len(stale)} photos from their stored embeddings")
    del engine, model
    free()


# --- Q-ReAlign Mini: technical quality and aesthetics, 0..1 ----------------------------------------

LEVELS = ["excellent", "good", "fair", "poor", "bad"]
WEIGHTS = [1.0, 0.75, 0.5, 0.25, 0.0]


class Iqa:
    def __init__(self, dev):
        from transformers import AutoModelForImageTextToText, AutoProcessor

        self.dev, self.dtype = dev, torch.bfloat16 if dev != "cpu" else torch.float32
        self.processor = AutoProcessor.from_pretrained(QREALIGN)
        self.processor.tokenizer.padding_side = "left"
        self.model = AutoModelForImageTextToText.from_pretrained(QREALIGN, dtype=self.dtype).to(dev).eval()
        self.level_ids = [self.processor.tokenizer(" " + w, add_special_tokens=False).input_ids[0] for w in LEVELS]
        self.weights = torch.tensor(WEIGHTS)

        def prompt(what):
            msgs = [{"role": "user", "content": [
                {"type": "image"}, {"type": "text", "text": f"How would you rate the {what} of this image?"}]}]
            return self.processor.apply_chat_template(msgs, add_generation_prompt=True) + f"The {what} of the image is"

        self.prompts = [prompt("quality"), prompt("aesthetics")]

    def __call__(self, path) -> dict:
        img = open_preview(path, 1024)
        rows = []
        for p in self.prompts:  # one at a time: batching two image prompts breaks Qwen3.5's rope index
            batch = self.processor(text=[p], images=[img], return_tensors="pt")
            # Batch of one, no padding: without the mask, get_rope_index skips a boolean-mask index
            # that returns the wrong length on XPU (torch 2.15.0+xpu).
            del batch["attention_mask"]
            batch = batch.to(self.dev)
            batch["pixel_values"] = batch["pixel_values"].to(self.dtype)
            with torch.inference_mode():
                rows.append(self.model(**batch).logits[0, -1, self.level_ids].float().cpu())
        probs = torch.stack(rows).softmax(-1)
        quality, aesthetics = (probs * self.weights).sum(-1).tolist()
        return {"quality": quality, "aesthetics": aesthetics,
                "q_probs": [round(p, 4) for p in probs[0].tolist()], "a_probs": [round(p, 4) for p in probs[1].tolist()]}


def run_iqa(cfg: Config, con, run_id: str):
    from .helper import Share

    ids = todo_ids(con, "iqa", QREALIGN)
    if not ids:
        return
    dev = device()
    engine = Iqa(dev)
    t0 = time.perf_counter()
    with Progress(con, "signals.iqa", QREALIGN, total=len(ids)) as prog:
        share = Share(cfg, "iqa", ids)
        for id_, r in share:
            with prog.item(id_):
                r = r or engine(cfg.preview(id_))
                if "error" in r:
                    raise RuntimeError(r["error"])
                put_signal(con, id_, "iqa", QREALIGN, value=r["quality"], data=r)
    _timed(con, run_id, "signals.iqa", prog.ok, t0, dev + share.note())
    del engine
    free()


# --- Nudity: full nudity goes to the album's " PRIV" sibling; partial nudity is fine anywhere (P26) ---

NUDITY = "nudenet@3.4.2"
# Full nudity, as the user put it (2026-11-03): exposed genitals or anus, or a bare body (bare buttocks and
# bare breasts together). Topless, shirtless, swimwear or underwear alone are partial nudity and stay put.
FULL = {"FEMALE_GENITALIA_EXPOSED", "MALE_GENITALIA_EXPOSED", "ANUS_EXPOSED"}
BARE = ({"BUTTOCKS_EXPOSED"}, {"FEMALE_BREAST_EXPOSED"})
NUDITY_SCORE = 0.5     # detector score for a part to count


def full_nudity(detections) -> bool:
    parts = {d["class"] for d in detections if d["score"] >= NUDITY_SCORE}
    return bool(parts & FULL) or all(parts & group for group in BARE)


def run_nudity(cfg: Config, con, run_id: str):
    from nudenet import NudeDetector

    ids = todo_ids(con, "nudity", NUDITY)
    if not ids:
        return
    detector = NudeDetector()  # the bundled 320n ONNX model, on the CPU
    t0 = time.perf_counter()
    flagged = 0
    with Progress(con, "signals.nudity", NUDITY, total=len(ids)) as prog:
        for i in ids:
            with prog.item(i):
                dets = detector.detect(str(cfg.preview(i)))
                full = full_nudity(dets)
                flagged += full
                exposed = sorted({d["class"] for d in dets if d["score"] >= NUDITY_SCORE and d["class"].endswith("_EXPOSED")})
                put_signal(con, i, "nudity", NUDITY, value=float(full), data={"full": full, "exposed": exposed})
    dt = time.perf_counter() - t0
    log_timing(con, "signals.nudity", run_id, prog.ok, dt, "cpu")
    con.commit()
    print(f"signals.nudity: {prog.ok} images, {flagged} with full nudity, {dt:.1f} s ({prog.ok / max(dt, 1e-6):.1f} img/s on cpu)")
