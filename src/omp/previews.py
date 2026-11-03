"""S2 decode + previews, plus the classical S3 signals that need full resolution (CPU, one pass).

Each photo is decoded once with its EXIF orientation applied. From the full-resolution luminance we
keep a tile grid of sharpness measures, so later stages can ask "how sharp is the subject" once the
subject mask (depth, faces) is known, instead of a single global number that rewards noise and
punishes bokeh (v1's Laplacian std at 400 px).
"""

import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import cv2
import imagehash
import numpy as np
from PIL import Image, ImageOps

from .config import Config
from .db import log_timing, put_signal

MODEL = "classic@1"
PREVIEW_PX, THUMB_PX = 1536, 384
GRID_LONG = 24          # tiles along the long side
MAX_SHARP_MP = 25       # above this, sharpness is measured at half resolution (108 MP phones)

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


def sharpness_grid(gray: np.ndarray) -> dict:
    h, w = gray.shape
    gw, gh = (GRID_LONG, max(1, round(GRID_LONG * h / w))) if w >= h else (max(1, round(GRID_LONG * w / h)), GRID_LONG)
    grid = (gw, gh)
    smooth = cv2.GaussianBlur(gray, (0, 0), 0.8)
    gx = cv2.Sobel(smooth, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(smooth, cv2.CV_32F, 0, 1, ksize=3)
    jxx, jyy, jxy = tile_mean(gx * gx, grid), tile_mean(gy * gy, grid), tile_mean(gx * gy, grid)
    del gx, gy
    ten = jxx + jyy
    coherence = np.sqrt((jxx - jyy) ** 2 + 4 * jxy**2) / (ten + 1e-6)
    angle2 = np.arctan2(2 * jxy, jxx - jyy)  # 2θ of the dominant gradient direction
    lap = cv2.Laplacian(smooth, cv2.CV_32F, ksize=3)
    lap_var = tile_mean(lap * lap, grid) - tile_mean(lap, grid) ** 2
    del lap
    mean = tile_mean(gray, grid)
    std = np.sqrt(np.maximum(tile_mean(gray * gray, grid) - mean**2, 0))
    # Immerkaer's noise estimate per tile, then the median over the flattest tiles.
    kernel = np.array([[1, -2, 1], [-2, 4, -2], [1, -2, 1]], np.float32)
    resp = np.abs(cv2.filter2D(gray, cv2.CV_32F, kernel))
    noise_tiles = tile_mean(resp, grid) * np.sqrt(np.pi / 2) / 6
    flat = ten <= np.quantile(ten, 0.2)
    noise = float(np.median(noise_tiles[flat]))
    # Motion blur: strong, coherent gradients that point the same way across the frame.
    weight = coherence * ten
    motion = float(np.abs((weight * np.exp(1j * angle2)).sum()) / (weight.sum() + 1e-6))
    return {
        "grid": np.stack([np.log1p(ten), np.log1p(np.maximum(lap_var, 0)), coherence, std, mean / 255]),
        "p90": float(np.quantile(np.log1p(ten), 0.9)),
        "median": float(np.median(np.log1p(ten))),
        "motion": motion,
        "noise": noise,
    }


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


def process(args) -> dict:
    id_, path, preview, thumb = args
    t0 = time.perf_counter()
    try:
        img = decode(Path(path))
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
    expo = exposure(np.asarray(prev))
    hashes = {"phash": str(imagehash.phash(th)), "dhash": str(imagehash.dhash(th))}
    return {
        "id": id_, "w": w, "h": h, "sharp": sharp, "exposure": expo, "hashes": hashes,
        "seconds": time.perf_counter() - t0, "decode_s": t_decode,
    }


def run(cfg: Config, con, run_id: str, force=False):
    done = set() if force else {r[0] for r in con.execute("SELECT id FROM signal WHERE kind='sharp' AND model=?", (MODEL,))}
    rows = con.execute(
        "SELECT p.id, MIN(f.path) AS path FROM photo p JOIN file f ON f.id = p.id"
        " WHERE f.policy != 'ignore' GROUP BY p.id"
    ).fetchall()
    todo = [(r["id"], str(cfg.root / r["path"]), str(cfg.preview(r["id"])), str(cfg.thumb(r["id"])))
            for r in rows if r["id"] not in done]
    started = time.perf_counter()
    n = errors = 0
    per_image = []
    with ProcessPoolExecutor(max_workers=cfg.workers) as pool:
        for res in pool.map(process, todo, chunksize=1):
            if "error" in res:
                errors += 1
                put_signal(con, res["id"], "error.previews", MODEL, data={"error": res["error"]})
                continue
            s = res["sharp"]
            put_signal(con, res["id"], "sharp", MODEL, value=s["p90"], vec=s["grid"],
                       data={k: s[k] for k in ("p90", "median", "motion", "noise", "scale")})
            put_signal(con, res["id"], "exposure", MODEL, data=res["exposure"])
            put_signal(con, res["id"], "phash", MODEL, data=res["hashes"])
            per_image.append(res["seconds"])
            n += 1
    con.commit()
    elapsed = time.perf_counter() - started
    log_timing(con, "previews", run_id, n, elapsed, f"cpu x{cfg.workers}")
    con.commit()
    mean = np.mean(per_image) if per_image else 0
    print(f"previews: {n} photos, {errors} errors, {elapsed:.1f} s wall ({n / max(elapsed, 1e-6):.1f}/s),"
          f" {mean:.2f} s per photo per core")
