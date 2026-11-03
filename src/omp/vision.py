"""S3 learned signals: embeddings, zero-shot kinds, IQA. One model at a time on the GPU, so the peak
fits next to the always-on RAG models on the B50 (~8 GiB free)."""

import gc
import time

import numpy as np
import torch
from PIL import Image

from .config import Config
from .db import done_ids, log_timing, put_signal

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

def run_dino(cfg: Config, con, run_id: str):
    from transformers import AutoModel

    ids = todo_ids(con, "dino", DINO)
    if not ids:
        return
    dev = device()
    dtype = torch.float16 if dev != "cpu" else torch.float32
    model = AutoModel.from_pretrained(DINO, dtype=dtype).to(dev).eval()
    mean = torch.tensor([0.485, 0.456, 0.406]).view(3, 1, 1)
    std = torch.tensor([0.229, 0.224, 0.225]).view(3, 1, 1)
    t0 = time.perf_counter()
    for id_ in ids:
        img = load_preview(cfg, id_)
        # Keep the aspect ratio: long side 448, both sides multiples of the 14 px patch.
        s = 448 / max(img.size)
        w, h = (max(14, round(img.width * s / 14) * 14), max(14, round(img.height * s / 14) * 14))
        x = torch.from_numpy(np.asarray(img.resize((w, h), Image.Resampling.BICUBIC), np.float32) / 255)
        x = ((x.permute(2, 0, 1) - mean) / std).unsqueeze(0).to(dev, dtype)
        with torch.inference_mode():
            out = model(pixel_values=x)
        cls = torch.nn.functional.normalize(out.pooler_output.float(), dim=-1)[0].cpu().numpy()
        patch = torch.nn.functional.normalize(out.last_hidden_state[:, 1:].float().mean(1), dim=-1)[0].cpu().numpy()
        put_signal(con, id_, "dino", DINO, vec=cls)
        put_signal(con, id_, "dino_patch", DINO, vec=patch)
    _timed(con, run_id, "signals.dino", len(ids), t0, dev)
    del model
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


def run_siglip(cfg: Config, con, run_id: str):
    from transformers import AutoModel, AutoProcessor

    ids = todo_ids(con, "siglip", SIGLIP)
    if not ids:
        return
    dev = device()
    dtype = torch.float16 if dev != "cpu" else torch.float32
    model = AutoModel.from_pretrained(SIGLIP, dtype=dtype).to(dev).eval()
    processor = AutoProcessor.from_pretrained(SIGLIP)
    prompts = [p for _, _, ps in KINDS for p in ps]
    text = siglip_text(model, processor, prompts, dev)
    k = 2
    text = torch.nn.functional.normalize(text.view(len(KINDS), k, -1).mean(1), dim=-1)
    scale, bias = model.logit_scale.detach().exp().float(), model.logit_bias.detach().float()
    t0 = time.perf_counter()
    for id_ in ids:
        img = load_preview(cfg, id_, 1024)
        inputs = processor(images=[img], return_tensors="pt", max_num_patches=576).to(dev)
        inputs["pixel_values"] = inputs["pixel_values"].to(dtype)
        with torch.inference_mode():
            out = model.get_image_features(**inputs)
        out = out if torch.is_tensor(out) else out.pooler_output
        emb = torch.nn.functional.normalize(out.float(), dim=-1)
        logits = (emb @ text.T) * scale + bias
        probs = torch.sigmoid(logits)[0].cpu().numpy()
        soft = torch.softmax(logits[0], -1).cpu().numpy()
        purposes = {}
        for (key, purpose, _), p in zip(KINDS, soft):
            purposes[purpose] = purposes.get(purpose, 0.0) + float(p)
        top = sorted(zip([k_ for k_, _, _ in KINDS], soft.tolist(), probs.tolist()), key=lambda t: -t[1])[:3]
        put_signal(con, id_, "siglip", SIGLIP, vec=emb[0].cpu().numpy())
        put_signal(con, id_, "kind", SIGLIP + "+zeroshot@1", value=purposes.get("memory", 0.0),
                   data={"purpose": max(purposes, key=purposes.get), "purposes": purposes,
                         "top": [{"key": a, "p": round(b, 4), "sigmoid": round(c, 4)} for a, b, c in top]})
    _timed(con, run_id, "signals.siglip", len(ids), t0, dev)
    del model
    free()


# --- Q-ReAlign Mini: technical quality and aesthetics, 0..1 ----------------------------------------

LEVELS = ["excellent", "good", "fair", "poor", "bad"]
WEIGHTS = [1.0, 0.75, 0.5, 0.25, 0.0]


def run_iqa(cfg: Config, con, run_id: str):
    from transformers import AutoModelForImageTextToText, AutoProcessor

    ids = todo_ids(con, "iqa", QREALIGN)
    if not ids:
        return
    dev = device()
    dtype = torch.bfloat16 if dev != "cpu" else torch.float32
    processor = AutoProcessor.from_pretrained(QREALIGN)
    processor.tokenizer.padding_side = "left"
    model = AutoModelForImageTextToText.from_pretrained(QREALIGN, dtype=dtype).to(dev).eval()
    level_ids = [processor.tokenizer(" " + w, add_special_tokens=False).input_ids[0] for w in LEVELS]
    weights = torch.tensor(WEIGHTS)

    def prompt(what):
        msgs = [{"role": "user", "content": [
            {"type": "image"}, {"type": "text", "text": f"How would you rate the {what} of this image?"}]}]
        return processor.apply_chat_template(msgs, add_generation_prompt=True) + f"The {what} of the image is"

    prompts = [prompt("quality"), prompt("aesthetics")]
    t0 = time.perf_counter()
    for id_ in ids:
        img = load_preview(cfg, id_, 1024)
        rows = []
        for p in prompts:  # one at a time: batching two image prompts breaks Qwen3.5's rope index
            batch = processor(text=[p], images=[img], return_tensors="pt")
            # Batch of one, no padding: without the mask, get_rope_index skips a boolean-mask index
            # that returns the wrong length on XPU (torch 2.15.0+xpu).
            del batch["attention_mask"]
            batch = batch.to(dev)
            batch["pixel_values"] = batch["pixel_values"].to(dtype)
            with torch.inference_mode():
                rows.append(model(**batch).logits[0, -1, level_ids].float().cpu())
        probs = torch.stack(rows).softmax(-1)
        quality, aesthetics = (probs * weights).sum(-1).tolist()
        put_signal(con, id_, "iqa", QREALIGN, value=quality,
                   data={"quality": quality, "aesthetics": aesthetics,
                         "q_probs": [round(p, 4) for p in probs[0].tolist()],
                         "a_probs": [round(p, 4) for p in probs[1].tolist()]})
    _timed(con, run_id, "signals.iqa", len(ids), t0, dev)
    del model
    free()
