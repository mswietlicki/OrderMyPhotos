"""S2 decode + previews, plus the classical S3 signals that need full resolution: on the 4090 for JPEGs
(gpu_previews.py, through `omp worker`), on the CPU for the rest.

Each photo is decoded once with its EXIF orientation applied. From the full-resolution luminance we
keep a tile grid of sharpness measures, so later stages can ask "how sharp is the subject" once the
subject mask (depth, faces) is known, instead of a single global number that rewards noise and
punishes bokeh (v1's Laplacian std at 400 px).

Both paths decode with Pillow, so the pixels are the same; the 4090 only does the arithmetic (resize, sharpness,
exposure) and is within one level of Pillow's resize. Each photo's `sharp` signal records where it was made
(`device`: 4090 or cpu).
"""

import os
import queue
import threading
import time
from collections import deque
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
CHUNK = int(os.environ.get("OMP_PREVIEWS_CHUNK", "24"))  # JPEGs per request to the 4090
IN_FLIGHT = int(os.environ.get("OMP_PREVIEWS_IN_FLIGHT", "3"))  # requests at once: decoding overlaps GPU work

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


class Sender:
    """Chunks of JPEGs to the 4090 worker (`previews` engine), IN_FLIGHT requests at a time, while the 4090 may be borrowed
    (helper.Gate, as for the vision stages). Results, and chunks given back to the CPU, come out of `out`."""

    GIVE_UP = 600  # seconds the 4090 may stay unavailable, or failing, before the CPU takes the rest

    def __init__(self, cfg, items: list):
        from . import helper

        self.helper, self.cfg = helper, cfg
        self.chunks = deque(items[i:i + CHUNK] for i in range(0, len(items), CHUNK))
        self.url = f"{cfg.llm}/upstream/{helper.WORKER}/v1/previews"
        self.out, self.lock, self.stop, self.remote = queue.Queue(), threading.Lock(), False, 0
        self.threads = [threading.Thread(target=self._run, daemon=True) for _ in range(IN_FLIGHT if items else 0)]
        for t in self.threads:
            t.start()

    def alive(self) -> bool:
        return any(t.is_alive() for t in self.threads)

    def _give_back(self):
        with self.lock:
            while self.chunks:
                self.out.put(("cpu", self.chunks.popleft(), None))

    def _run(self):
        import httpx

        client, gate = httpx.Client(timeout=900), self.helper.gate(self.cfg.llm)
        unavailable = 0.0
        while not self.stop:
            with self.lock:
                if not self.chunks:
                    return
                chunk = self.chunks.popleft()
            try:
                if not gate.allowed():
                    raise RuntimeError("the 4090 isn't free")
                r = client.post(self.url, json={"paths": [c[1] for c in chunk]})
                r.raise_for_status()
                for c, x in zip(chunk, r.json()["results"], strict=True):
                    self.out.put(("gpu", c, x))
                self.remote += len(chunk)
                unavailable = 0.0
            except (httpx.HTTPError, ValueError, KeyError, RuntimeError) as exc:
                with self.lock:
                    self.chunks.appendleft(chunk)
                if unavailable >= self.GIVE_UP:
                    print(f"previews: the 4090 unavailable for {self.GIVE_UP} s ({type(exc).__name__}:"
                          f" {str(exc)[:120]}); the CPU takes the rest of this pass")
                    self._give_back()
                    return
                if not isinstance(exc, RuntimeError):
                    print(f"previews: 4090 worker {type(exc).__name__} {str(exc)[:120]}; trying again")
                    gate.backoff()
                end = time.monotonic() + 15
                while time.monotonic() < end and not self.stop:
                    time.sleep(1)
                unavailable += 15


def run(cfg: Config, con, run_id: str, force=False):
    from . import gpu_previews, helper

    four = helper.ENABLED and os.environ.get("OMP_PREVIEWS_4090", "1") != "0"
    have = set() if force else {r[0] for r in con.execute("SELECT id FROM signal WHERE kind='sharp' AND model=?", (MODEL,))}
    rows = con.execute(
        "SELECT p.id, MIN(f.path) AS path, p.width * p.height AS px FROM photo p JOIN file f ON f.id = p.id"
        " WHERE f.policy != 'ignore' GROUP BY p.id"
    ).fetchall()

    def on_4090(r) -> bool:
        return four and Path(r["path"]).suffix.lower() in GPU_EXT and (r["px"] or 0) <= MAX_SHARP_MP * 1e6

    todo = [r for r in rows if r["id"] not in have]
    item = {r["id"]: (r["id"], str(cfg.root / r["path"]), str(cfg.preview(r["id"])), str(cfg.thumb(r["id"])))
            for r in todo}
    cpu = deque(item[r["id"]] for r in todo if not on_4090(r))
    gpu_items = [item[r["id"]] for r in todo if on_4090(r)]
    print(f"previews: {len(todo)} to do, {len(gpu_items)} JPEGs with the 4090, {len(cpu)} on the CPU")
    started = time.perf_counter()
    per_image, counts = [], {"4090": 0, "cpu": 0}

    def store(id_, sharp, expo, hashes_, device, truncated=False):
        data = {k: sharp[k] for k in ("p90", "median", "motion", "noise", "scale")} | {"device": device}
        put_signal(con, id_, "sharp", MODEL, value=sharp["p90"], vec=sharp["grid"], data=data)
        put_signal(con, id_, "exposure", MODEL, data=expo)
        put_signal(con, id_, "phash", MODEL, data=hashes_)
        if truncated:
            put_signal(con, id_, "truncated", MODEL, value=1)
            print(f"previews: {id_[:10]} is truncated or broken; decoded what's there (signal truncated)")
        counts[device] += 1
        prog.done(id_)

    with Progress(con, "previews", MODEL, total=len(todo), before=len(rows) - len(todo)) as prog, \
            worker_pool(cfg.workers) as pool:
        sender = Sender(cfg, gpu_items)
        inflight = {}
        try:
            while True:
                check()
                while cpu and len(inflight) < 2 * cfg.workers:
                    it = cpu.popleft()
                    inflight[pool.submit(process, it)] = it
                for f in [f for f in inflight if f.done()]:
                    inflight.pop(f)
                    res = f.result()
                    if "error" in res:
                        prog.fail(res["id"], res["error"])
                        continue
                    per_image.append(res["seconds"])
                    store(res["id"], res["sharp"], res["exposure"], res["hashes"], "cpu", res["truncated"])
                try:
                    kind, it, x = sender.out.get(timeout=0.2)
                except queue.Empty:
                    kind = None
                if kind == "cpu":
                    cpu.extend(it)  # a chunk the 4090 gave back
                elif kind == "gpu" and "error" in x:
                    cpu.append(it)  # cut short, broken, over 25 MP: the CPU path, which marks what it has to
                elif kind == "gpu":
                    pj, tj, thumb_px, tiles = gpu_previews.decode_reply(x)
                    for path, data in ((it[2], pj), (it[3], tj)):
                        Path(path).parent.mkdir(parents=True, exist_ok=True)
                        Path(path).write_bytes(data)
                    sharp = grid_from_tiles(tiles) | {"scale": 1.0}
                    store(it[0], sharp, x["exposure"], hashes(Image.fromarray(thumb_px)), "4090")
                if not cpu and not inflight and not sender.alive() and sender.out.empty():
                    break
        finally:
            sender.stop = True
    elapsed = time.perf_counter() - started
    n = prog.ok
    log_timing(con, "previews", run_id, n, elapsed, f"4090 + cpu x{cfg.workers}" if sender.remote else f"cpu x{cfg.workers}")
    con.commit()
    mean = np.mean(per_image) if per_image else 0
    print(f"previews: {n} photos ({counts['4090']} with the 4090, {counts['cpu']} on the CPU), {prog.failed} errors,"
          f" {elapsed:.1f} s wall ({n / max(elapsed, 1e-6):.1f}/s), {mean:.2f} s per CPU photo per core")
