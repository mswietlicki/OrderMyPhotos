"""S3 expensive signal: relative depth from the existing Depth-Anything-V2 service on the B50, called
through llama-swap (which loads it on demand and accounts for its VRAM)."""

import io
import time

import httpx
import numpy as np
from PIL import Image

from .config import Config
from .db import log_timing, put_signal
from .runtime import Progress, post

MODEL = "depth-anything-v2-small"
INPUT_PX = 768


def run(cfg: Config, con, run_id: str, ids=None):
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
            for id_ in ids:
                with prog.item(id_):
                    img = Image.open(cfg.preview(id_)).convert("RGB")
                    s = INPUT_PX / max(img.size)
                    img = img.resize((round(img.width * s), round(img.height * s)), Image.Resampling.LANCZOS)
                    buf = io.BytesIO()
                    img.save(buf, "JPEG", quality=92)
                    r = post(client, url, files={"file": ("p.jpg", buf.getvalue(), "image/jpeg")})
                    infer_ms.append(float(r.headers.get("X-Inference-Ms", "nan")))
                    out = cfg.depth(id_)
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_bytes(r.content)
                    d = np.asarray(Image.open(io.BytesIO(r.content)), dtype=np.float32) / 65535  # 1 = closest
                    put_signal(con, id_, "depth", MODEL, value=float(d.std()), data={
                        "std": float(d.std()),
                        "near_frac": float((d > 0.7).mean()),
                        "p10": float(np.quantile(d, 0.1)), "p50": float(np.median(d)), "p90": float(np.quantile(d, 0.9)),
                    })
    con.commit()
    dt = time.perf_counter() - t0
    log_timing(con, "signals.depth", run_id, prog.ok, dt, "b50 via llama-swap")
    con.commit()
    print(f"signals.depth: {prog.ok} images, {dt:.1f} s ({prog.ok / max(dt, 1e-6):.1f} img/s,"
          f" model {np.nanmedian(infer_ms):.0f} ms median)")
