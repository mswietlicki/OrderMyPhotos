"""S3 expensive signal: relative depth from the existing Depth-Anything-V2 service on the B50, called
through llama-swap (which loads it on demand and accounts for its VRAM), and the same model in `omp worker` on
the 4090 (`Depth`, P52), through the work queue (P60). The B50's service gets SERVICE_SLOTS requests besides the
stage's own, so its preprocessing and PNG run side by side (P62); the model is a small part of a photo's time."""

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


MODEL_PX, MULTIPLE = 518, 14  # Depth-Anything's processor: the long side to 518, each side a multiple of 14


def request(path) -> bytes:
    """What the service is sent for a photo: its preview at INPUT_PX, as a JPEG. A strip too thin for the model
    raises ValueError at once: the processor would round its short side to 0 and the service answer 500, which the
    stage retried for 15 minutes, holding up the lane (a 3060 x 42 photo on the full run)."""
    img = Image.open(path).convert("RGB")
    s = INPUT_PX / max(img.size)
    w, h = round(img.width * s), round(img.height * s)
    if round(MODEL_PX * min(w, h) / max(w, h) / MULTIPLE) < 1:
        raise ValueError(f"too thin for depth: {img.width}x{img.height}")
    img = img.resize((w, h), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=92)
    return buf.getvalue()


PNG_LEVEL = 1          # zlib level of the depth PNG: lossless either way; the default (6) took 97 ms a photo, 1 takes 21
SERVICE_SLOTS = int(os.environ.get("OMP_DEPTH_SERVICE_SLOTS", "2"))  # requests to the B50's service besides the stage's own
THREADS = int(os.environ.get("OMP_DEPTH_THREADS", "4"))              # the 4090 engine's photos at once


class Depth:
    """The depth service's computation (mimir stacks/depth/app.py: the same model, versions and scaling), in
    `omp worker` on the 4090. One preview in, the service's 16-bit PNG out (base64). A chunk goes through THREADS
    threads (`batch`): the model takes ~10 ms of a photo's ~90, the rest is the CPU's (resizing, the processor, the
    PNG), and those parts let go of the GIL; the model's part is one at a time."""

    concurrent = True

    def __init__(self, dev):
        import threading
        from concurrent.futures import ThreadPoolExecutor

        import torch
        from transformers import AutoImageProcessor, AutoModelForDepthEstimation

        self.torch, self.dev = torch, dev
        self.dtype = torch.float16 if dev != "cpu" else torch.float32
        path = Path(os.environ.get("OMP_MODELS", "/models")) / LOCAL
        self.processor = AutoImageProcessor.from_pretrained(path)
        self.model = AutoModelForDepthEstimation.from_pretrained(path, dtype=self.dtype).to(dev).eval()
        self.gpu, self.pool = threading.Lock(), ThreadPoolExecutor(THREADS)

    def __call__(self, path) -> dict:
        torch = self.torch
        image = Image.open(io.BytesIO(request(path))).convert("RGB")
        inputs = self.processor(images=image, return_tensors="pt")
        with self.gpu, torch.inference_mode():
            predicted = self.model(**inputs.to(self.dev, self.dtype)).predicted_depth
            depth_map = torch.nn.functional.interpolate(
                predicted.unsqueeze(1).float(), size=image.size[::-1], mode="bicubic", align_corners=False
            ).squeeze().cpu().numpy()
        lo, hi = float(depth_map.min()), float(depth_map.max())
        scaled = ((depth_map - lo) / max(hi - lo, 1e-6) * 65535).astype(np.uint16)
        buf = io.BytesIO()
        Image.fromarray(scaled).save(buf, format="PNG", compress_level=PNG_LEVEL)
        return {"png": base64.b64encode(buf.getvalue()).decode()}

    def batch(self, paths: list) -> list[dict]:
        def one(p):
            try:
                return self(p)
            except Exception as exc:  # noqa: BLE001 - that photo's error, done again on the B50
                return {"error": f"{type(exc).__name__}: {exc}"}

        return list(self.pool.map(one, paths))


def service_worker(url: str, slots: int):
    """A work-queue worker for the B50's depth service, which takes one image per multipart request (its own API):
    `slots` requests at once next to the stage's own, so the service's preprocessing and PNG run side by side."""
    from .workqueue import HttpWorker

    class Service(HttpWorker):
        def request(self, client, chunk):
            out = []
            for it in chunk:
                try:
                    data = request(self.path_of(it))
                except ValueError as exc:  # too thin: this photo's error (the stage skips it), not the worker's
                    out.append({"error": str(exc)})
                    continue
                r = client.post(url, files={"file": ("p.jpg", data, "image/jpeg")})
                r.raise_for_status()
                out.append({"png": base64.b64encode(r.content).decode()})
            return out

    return lambda q, path_of: Service(q, "b50 service", url, path_of, chunk=1, slots=slots, give_up=None)


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
            share = Share(cfg, "depth", ids, extra=[service_worker(url, SERVICE_SLOTS)] if SERVICE_SLOTS else [])
            for id_, r in share:
                with prog.item(id_):
                    if r is None:  # the B50's service, from this thread
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
