"""S2 decode + previews, plus the classical S3 signals that need full resolution, as one queue that the CPU and
the GPUs take from as fast as each can (workqueue.py, P60): the CPU's process pool, `omp worker` on the 4090
(gpu_previews.py through llama-swap, P59), a second `omp worker` on the B50, and any remote workers.

Each photo is decoded once with its EXIF orientation applied. From the full-resolution luminance we
keep a tile grid of sharpness measures, so later stages can ask "how sharp is the subject" once the
subject mask (depth, faces) is known, instead of a single global number that rewards noise and
punishes bokeh (v1's Laplacian std at 400 px).

Every path decodes with Pillow, so the pixels are the same; a GPU only does the arithmetic (resize, sharpness,
exposure), within one level of Pillow's resize. JPEGs up to MAX_SHARP_MP are for anyone; the rest, and whatever a
GPU worker can't decode, are the CPU's. Each photo's `sharp` signal records where it was made (`device`).
"""

import os
import queue
import time
from pathlib import Path

import cv2
import imagehash
import numpy as np
from PIL import Image, ImageFile, ImageOps

from .config import Config
from .db import log_timing, put_signal
from .runtime import Progress, check, worker_pool
from .sharpness import GRID_LONG, NOISE_KERNEL, grid_from_tiles, grid_shape  # noqa: F401 - GRID_LONG for others

MODEL = "classic@1"
PREVIEW_PX, THUMB_PX = 1536, 384
MAX_SHARP_MP = 25       # above this, sharpness is measured at half resolution (108 MP phones)
GPU_EXT = {".jpg", ".jpeg"}
CHUNK = int(os.environ.get("OMP_PREVIEWS_CHUNK", "24"))          # JPEGs per request to a GPU worker
IN_FLIGHT = int(os.environ.get("OMP_PREVIEWS_IN_FLIGHT", "3"))   # requests at once per worker: decoding overlaps GPU work
LANES = os.environ.get("OMP_PREVIEWS_LANES", "cpu,4090,b50")     # which of the CPU, the 4090 and the B50 take part
B50_PORT = int(os.environ.get("OMP_PREVIEWS_B50_PORT", "8791"))  # the B50's worker, on 127.0.0.1 (host network)

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:  # pragma: no cover
    pass


def decode(path: Path) -> Image.Image:
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    return img.convert("RGB")


def resized(img: Image.Image, long_side: int) -> Image.Image:
    w, h = img.size
    scale = long_side / max(w, h)
    if scale >= 1:
        return img.copy()
    factor = int(1 / scale / 2)
    small = img.reduce(factor) if factor >= 2 else img
    return small.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.Resampling.LANCZOS)


def tile_mean(a: np.ndarray, grid: tuple[int, int]) -> np.ndarray:
    return cv2.resize(a, grid, interpolation=cv2.INTER_AREA)


def tile_stats(gray: np.ndarray) -> np.ndarray:
    """The eight full-resolution tile means of the sharpness grid (sharpness.py), with OpenCV."""
    grid = grid_shape(*gray.shape)
    smooth = cv2.GaussianBlur(gray, (0, 0), 0.8)
    gx = cv2.Sobel(smooth, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(smooth, cv2.CV_32F, 0, 1, ksize=3)
    out = [tile_mean(gx * gx, grid), tile_mean(gy * gy, grid), tile_mean(gx * gy, grid)]
    del gx, gy
    lap = cv2.Laplacian(smooth, cv2.CV_32F, ksize=3)
    out += [tile_mean(lap * lap, grid), tile_mean(lap, grid)]
    del lap
    out += [tile_mean(gray, grid), tile_mean(gray * gray, grid)]
    out.append(tile_mean(np.abs(cv2.filter2D(gray, cv2.CV_32F, NOISE_KERNEL)), grid))
    return np.stack(out)


def sharpness_grid(gray: np.ndarray) -> dict:
    return grid_from_tiles(tile_stats(gray))


def exposure(rgb_small: np.ndarray) -> dict:
    y = cv2.cvtColor(rgb_small, cv2.COLOR_RGB2GRAY)
    hsv = cv2.cvtColor(rgb_small, cv2.COLOR_RGB2HSV)
    return {
        "clip_hi": float((y >= 250).mean()),
        "clip_lo": float((y <= 5).mean()),
        "median": float(np.median(y)),
        "p01": float(np.quantile(y, 0.01)),
        "p99": float(np.quantile(y, 0.99)),
        "saturation": float(hsv[..., 1].mean() / 255),
    }


def hashes(thumb: Image.Image) -> dict:
    return {"phash": str(imagehash.phash(thumb)), "dhash": str(imagehash.dhash(thumb))}


def process(args) -> dict:
    """One photo on a CPU core: decode, previews, sharpness, exposure, hashes."""
    id_, path, preview, thumb = args
    t0 = time.perf_counter()
    truncated = False
    try:
        img = decode(Path(path))
    except OSError as exc:
        if "truncated" not in str(exc) and "broken data stream" not in str(exc):
            return {"id": id_, "error": f"decode: {exc}"}
        # A JPEG that lost its last bytes (an interrupted download or copy), or with a broken stretch: decode what's
        # there, the rest comes out grey. The photo is still sorted like the others ("no photo stays unsorted",
        # P29), and marked.
        ImageFile.LOAD_TRUNCATED_IMAGES = True
        try:
            img = decode(Path(path))
            truncated = True
        except Exception as exc2:  # noqa: BLE001
            return {"id": id_, "error": f"decode: {exc2}"}
        finally:
            ImageFile.LOAD_TRUNCATED_IMAGES = False
    except Exception as exc:  # noqa: BLE001 - recorded, the stage continues
        return {"id": id_, "error": f"decode: {exc}"}
    w, h = img.size
    t_decode = time.perf_counter() - t0
    prev = resized(img, PREVIEW_PX)
    Path(preview).parent.mkdir(parents=True, exist_ok=True)
    prev.save(preview, "JPEG", quality=88, optimize=True)
    th = resized(prev, THUMB_PX)
    Path(thumb).parent.mkdir(parents=True, exist_ok=True)
    th.save(thumb, "JPEG", quality=80, optimize=True)

    sharp_img = img if w * h <= MAX_SHARP_MP * 1e6 else resized(img, max(w, h) // 2)
    gray = np.asarray(sharp_img.convert("L"), dtype=np.float32)
    del img, sharp_img
    sharp = sharpness_grid(gray)
    sharp["scale"] = gray.shape[1] / w
    del gray
    return {
        "id": id_, "w": w, "h": h, "sharp": sharp, "exposure": exposure(np.asarray(prev)), "hashes": hashes(th),
        "truncated": truncated, "seconds": time.perf_counter() - t0, "decode_s": t_decode,
    }


def run(cfg: Config, con, run_id: str, force=False):
    from . import gpu_previews, helper, workqueue

    lanes = {x.strip() for x in LANES.split(",")}
    have = set() if force else {r[0] for r in con.execute("SELECT id FROM signal WHERE kind='sharp' AND model=?", (MODEL,))}
    rows = con.execute(
        "SELECT p.id, MIN(f.path) AS path, p.width * p.height AS px FROM photo p JOIN file f ON f.id = p.id"
        " WHERE f.policy != 'ignore' GROUP BY p.id"
    ).fetchall()
    todo = [r for r in rows if r["id"] not in have]

    def item(r):
        return r["id"], str(cfg.root / r["path"]), str(cfg.preview(r["id"])), str(cfg.thumb(r["id"]))

    def for_anyone(r) -> bool:  # a JPEG a GPU worker can take
        return Path(r["path"]).suffix.lower() in GPU_EXT and (r["px"] or 0) <= MAX_SHARP_MP * 1e6

    q = workqueue.WorkQueue([item(r) for r in todo if for_anyone(r)])
    for r in todo:
        if not for_anyone(r):
            q.to_cpu(item(r))
    split = f"previews: {len(todo)} to do, {len(q.items)} JPEGs for anyone, {len(q.cpu_only)} for the CPU"
    workers, local = [], None
    path_of = lambda it: it[1]  # noqa: E731
    if "4090" in lanes and helper.ENABLED and q.items:
        gate = helper.gate(cfg.llm)
        workers.append(workqueue.HttpWorker(q, "4090", f"{cfg.llm}/upstream/{helper.WORKER}/v1/previews", path_of,
                                            CHUNK, IN_FLIGHT, allowed=gate.allowed, backoff=gate.backoff))
    if "b50" in lanes and q.items:
        local = workqueue.Local(B50_PORT, "xpu", pidfile=Path(cfg.db).parent / "b50-worker.pid")
        if local.url:
            workers.append(workqueue.HttpWorker(q, "b50", f"{local.url}/v1/previews", path_of, CHUNK, IN_FLIGHT))
    for name, url in workqueue.remotes():
        workers.append(workqueue.HttpWorker(q, name, f"{url}/v1/previews", path_of, CHUNK, IN_FLIGHT))
    # The CPU's slots: fewer when GPU workers decode on the same cores (the 4090's and the B50's in this machine)
    cpu_slots = cfg.workers if not workers else max(1, int(os.environ.get("OMP_PREVIEWS_CPU", max(2, cfg.workers - 6))))
    if "cpu" not in lanes and workers:
        cpu_slots = 1  # only what the GPUs can't do
    print(f"{split}; workers: cpu x{cpu_slots}" + "".join(f", {w.name}" for w in workers))
    started = time.perf_counter()
    per_image = []

    def store(id_, sharp, expo, hashes_, device, truncated=False):
        data = {k: sharp[k] for k in ("p90", "median", "motion", "noise", "scale")} | {"device": device}
        put_signal(con, id_, "sharp", MODEL, value=sharp["p90"], vec=sharp["grid"], data=data)
        put_signal(con, id_, "exposure", MODEL, data=expo)
        put_signal(con, id_, "phash", MODEL, data=hashes_)
        if truncated:
            put_signal(con, id_, "truncated", MODEL, value=1)
            print(f"previews: {id_[:10]} is truncated or broken; decoded what's there (signal truncated)")
        q.note(device)
        prog.done(id_)

    pending = len(todo)
    with Progress(con, "previews", MODEL, total=len(todo), before=len(rows) - len(todo)) as prog,             worker_pool(cpu_slots) as pool:
        inflight = {}
        try:
            while pending:
                check()
                while len(inflight) < cpu_slots and (got := q.take(1, cpu=True)):
                    inflight[pool.submit(process, got[0])] = got[0]
                for f in [f for f in inflight if f.done()]:
                    inflight.pop(f)
                    res = f.result()
                    pending -= 1
                    if "error" in res:
                        prog.fail(res["id"], res["error"])
                        continue
                    per_image.append(res["seconds"])
                    store(res["id"], res["sharp"], res["exposure"], res["hashes"], "cpu", res["truncated"])
                try:
                    name, it, x = q.out.get(timeout=0.2)
                except queue.Empty:
                    continue
                pending -= 1
                pj, tj, thumb_px, tiles = gpu_previews.decode_reply(x)
                for path, data in ((it[2], pj), (it[3], tj)):
                    Path(path).parent.mkdir(parents=True, exist_ok=True)
                    Path(path).write_bytes(data)
                store(it[0], grid_from_tiles(tiles) | {"scale": 1.0}, x["exposure"], hashes(Image.fromarray(thumb_px)), name)
        finally:
            q.stop = True
            if local:
                local.close()
    elapsed = time.perf_counter() - started
    n = prog.ok
    by = ", ".join(f"{k} {v}" for k, v in sorted(q.done_by.items(), key=lambda kv: -kv[1]))
    log_timing(con, "previews", run_id, n, elapsed, " + ".join(k for k in q.done_by) or "cpu")
    con.commit()
    mean = np.mean(per_image) if per_image else 0
    print(f"previews: {n} photos ({by}), {prog.failed} errors, {elapsed:.1f} s wall ({n / max(elapsed, 1e-6):.1f}/s),"
          f" {mean:.2f} s per CPU photo per core")
