"""S3 expensive signal: relative depth from the existing Depth-Anything-V2 service on the B50, called
through llama-swap (which loads it on demand and accounts for its VRAM). While the 4090 is free, it takes
photos from the other end of the list with the same model in `omp worker` (`Depth`, P52)."""

import base64
import io
import os
import time
from pathlib import Path

import httpx
import numpy as np
from PIL import Image

from .config import Config
from .db import log_timing, put_signal
from .runtime import Progress, post

MODEL = "depth-anything-v2-small"
INPUT_PX = 768
LOCAL = "hf-local/Depth-Anything-V2-Small-hf"  # the depth service's own copy of the model, under /models


def request(path) -> bytes:
    """What the service is sent for a photo: its preview at INPUT_PX, as a JPEG."""
    img = Image.open(path).convert("RGB")
    s = INPUT_PX / max(img.size)
    img = img.resize((round(img.width * s), round(img.height * s)), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=92)
    return buf.getvalue()


class Depth:
    """The depth service's computation (mimir stacks/depth/app.py: the same model, versions and scaling), in
    `omp worker` on the 4090. One preview in, the service's 16-bit PNG out (base64)."""

    def __init__(self, dev):
        import torch
        from transformers import AutoImageProcessor, AutoModelForDepthEstimation

        self.torch, self.dev = torch, dev
        self.dtype = torch.float16 if dev != "cpu" else torch.float32
        path = Path(os.environ.get("OMP_MODELS", "/models")) / LOCAL
        self.processor = AutoImageProcessor.from_pretrained(path)
        self.model = AutoModelForDepthEstimation.from_pretrained(path, dtype=self.dtype).to(dev).eval()

    def __call__(self, path) -> dict:
        torch = self.torch
        image = Image.open(io.BytesIO(request(path))).convert("RGB")
        inputs = self.processor(images=image, return_tensors="pt").to(self.dev, self.dtype)
        with torch.inference_mode():
            predicted = self.model(**inputs).predicted_depth
        depth_map = torch.nn.functional.interpolate(
            predicted.unsqueeze(1).float(), size=image.size[::-1], mode="bicubic", align_corners=False
        ).squeeze().cpu().numpy()
        lo, hi = float(depth_map.min()), float(depth_map.max())
        scaled = ((depth_map - lo) / max(hi - lo, 1e-6) * 65535).astype(np.uint16)
        buf = io.BytesIO()
        Image.fromarray(scaled).save(buf, format="PNG")
        return {"png": base64.b64encode(buf.getvalue()).decode()}


def run(cfg: Config, con, run_id: str, ids=None):
    from .helper import Share

    done = {r[0] for r in con.execute("SELECT id FROM signal WHERE kind='depth' AND model=?", (MODEL,))}
    if ids is None:
        ids = [r[0] for r in con.execute("SELECT id FROM signal WHERE kind='sharp' ORDER BY id")]
    ids = [i for i in ids if i not in done]
    if not ids:
        return
    url = f"{cfg.llm}/upstream/{MODEL}/v1/depth"
    t0 = time.perf_counter()
    infer_ms = []
    with httpx.Client(timeout=300) as client:
        with Progress(con, "signals.depth", MODEL, total=len(ids)) as prog:
            share = Share(cfg, "depth", ids)
            for id_, r in share:
                with prog.item(id_):
                    if r is None:  # the B50's service
                        res = post(client, url, files={"file": ("p.jpg", request(cfg.preview(id_)), "image/jpeg")})
                        infer_ms.append(float(res.headers.get("X-Inference-Ms", "nan")))
                        png = res.content
                    elif "error" in r:
                        raise RuntimeError(r["error"])
                    else:  # done on the 4090
                        png = base64.b64decode(r["png"])
                    out = cfg.depth(id_)
                    out.parent.mkdir(parents=True, exist_ok=True)
                    tmp = out.with_name(out.name + ".tmp")
                    tmp.write_bytes(png)
                    os.replace(tmp, out)  # a new file, never written through a link to another session's
                    d = np.asarray(Image.open(io.BytesIO(png)), dtype=np.float32) / 65535  # 1 = closest
                    put_signal(con, id_, "depth", MODEL, value=float(d.std()), data={
                        "std": float(d.std()),
                        "near_frac": float((d > 0.7).mean()),
                        "p10": float(np.quantile(d, 0.1)), "p50": float(np.median(d)), "p90": float(np.quantile(d, 0.9)),
                    })
    con.commit()
    dt = time.perf_counter() - t0
    log_timing(con, "signals.depth", run_id, prog.ok, dt, "b50 via llama-swap" + share.note())
    con.commit()
    print(f"signals.depth: {prog.ok} images, {dt:.1f} s ({prog.ok / max(dt, 1e-6):.1f} img/s,"
          f" model {np.nanmedian(infer_ms) if infer_ms else float('nan'):.0f} ms median{share.note()})")
