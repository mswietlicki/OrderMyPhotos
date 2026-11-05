"""`omp worker`: the 4090's share of the vision stages (P49). DINOv2, SigLIP 2 and Q-ReAlign, orient's turned views
and the depth service's model (P52): the same engines as on the B50, plus the previews of JPEG originals
(gpu_previews.py), as an HTTP service that llama-swap starts on demand as `omp-vision-cuda`: while it
works the chat model is evicted, and the chat model comes back on its next request.

POST /v1/<engine>  {"paths": [preview, ...]}  ->  {"results": [result or {"error"}, ...], "seconds", "device"}
GET  /health

It reads the previews from the sessions' cache, mounted read-only at the same path as in the app, and only
returns results: the stage that asked writes them to its DB.
"""

import os
import threading
import time

from fastapi import Body, FastAPI, HTTPException

from . import depth, gpu_previews, orient, subjects, vision

ENGINES = {"dino": vision.Dino, "siglip": vision.Siglip, "iqa": vision.Iqa, "orient": orient.Views, "depth": depth.Depth,
           "subjects": subjects.Subjects, "previews": gpu_previews.Previews}
app = FastAPI(title="omp worker")
loaded: dict = {}
locks = {name: threading.Lock() for name in ENGINES}  # several lanes ask at once: one load, one call at a time


@app.get("/health")
def health():
    return {"status": "ok", "device": vision.device(), "loaded": sorted(loaded)}


@app.post("/v1/{name}")
def run(name: str, paths: list[str] = Body(..., embed=True)):
    if name not in ENGINES:
        raise HTTPException(404, f"no engine {name!r}")
    with locks[name]:
        if name not in loaded:
            loaded[name] = ENGINES[name](vision.device())
    engine = loaded[name]
    if getattr(engine, "concurrent", False):  # takes the chunk at once, several requests side by side (previews)
        t0 = time.perf_counter()
        return {"results": engine.batch(paths), "seconds": round(time.perf_counter() - t0, 3), "device": vision.device()}
    with locks[name]:
        out, t0 = [], time.perf_counter()
        for path in paths:
            try:
                out.append(engine(path))
            except Exception as exc:  # noqa: BLE001 - one bad photo is that photo's error, not the request's
                out.append({"error": f"{type(exc).__name__}: {exc}"})
    return {"results": out, "seconds": round(time.perf_counter() - t0, 3), "device": vision.device()}


def main():
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), log_level="warning")
