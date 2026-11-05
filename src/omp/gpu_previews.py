"""Previews with the 4090: the `previews` engine of `omp worker` (llama-swap's omp-vision-cuda). The user, during the
full run: "could the 4090 do decoding, sharpness and encoding? Qwen is not needed now".

For a chunk of JPEG originals it does what previews.process does on a CPU core, with the arithmetic on the GPU:
- **decode** with Pillow, as previews.decode (EXIF orientation applied), on CPU threads: Pillow lets go of the
  GIL while it decodes. It's the same decoder as the CPU path, so the pixels are the same. nvJPEG was tried
  first: on this card its batched decode took 59 ms a photo, mostly on one CPU thread, and it decoded a
  truncated JPEG without a word;
- **preview (1536 px) and thumbnail (384 px)** on the GPU, with Pillow's own steps: reduce() by a whole factor,
  then a Lanczos resample, horizontal pass first, rounded to 8 bits between passes, with Pillow's coefficients
  (within one level of Pillow's 8-bit fixed point);
- **sharpness** on the GPU: the luminance by Pillow's "L" formula, and sharpness.TorchTiles' eight tile means;
- **exposure** on the GPU, with OpenCV's integer formulas for the grey value and the HSV saturation;
- **JPEG encoding** with Pillow on the threads, as on the CPU path (quality 88 and 80, optimized).

It returns the JPEG bytes, the thumbnail's pixels (the stage hashes them, as previews.process hashes its
thumbnail), the tiles and the exposure: the stage writes the files and the DB. A photo Pillow can't decode
outright (cut short, a broken stretch, not an image) comes back as an error, and the stage's CPU path deals with
it as before, marks included. Photos over MAX_SHARP_MP are the CPU's too. The worker reads the originals where
the app does (the library roots, mounted read-only at the same paths).
"""

import base64
import io
import os
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PIL import Image, ImageOps

from . import config  # noqa: F401 - Pillow's pixel limit, as in the app
from .sharpness import TorchTiles

PREVIEW_PX, THUMB_PX, MAX_SHARP_MP = 1536, 384, 25  # as previews.py
THREADS = int(os.environ.get("OMP_PREVIEW_THREADS", "16"))


def lanczos_weights(n: int, m: int) -> np.ndarray:
    """Pillow's Lanczos coefficients (Resample.c precompute_coeffs) for n pixels to m, as an (m, n) matrix."""
    scale = n / m
    fscale = max(scale, 1.0)
    support = 3.0 * fscale
    out = np.zeros((m, n), np.float64)
    for j in range(m):
        center = (j + 0.5) * scale
        lo, hi = max(int(center - support + 0.5), 0), min(int(center + support + 0.5), n)
        x = (np.arange(lo, hi) - center + 0.5) / fscale
        w = np.where(np.abs(x) < 3.0, np.sinc(x) * np.sinc(x / 3.0), 0.0)
        s = w.sum()
        out[j, lo:hi] = w / s if s != 0 else w
    return out.astype(np.float32)


def box_weights(n: int, f: int) -> np.ndarray:
    """Pillow's Image.reduce(f) along one axis: blocks of f averaged, the last one over what's left."""
    m = -(-n // f)
    out = np.zeros((m, n), np.float32)
    for j in range(m):
        a, b = j * f, min(n, (j + 1) * f)
        out[j, a:b] = 1.0 / (b - a)
    return out


def _load(path: str) -> np.ndarray:
    """previews.decode, as an (H, W, 3) uint8 array."""
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    return np.asarray(img.convert("RGB"))


def _encode(prev: np.ndarray, thumb: np.ndarray) -> tuple[bytes, bytes]:
    out = []
    for a, q in ((prev, 88), (thumb, 80)):
        buf = io.BytesIO()
        Image.fromarray(a).save(buf, "JPEG", quality=q, optimize=True)
        out.append(buf.getvalue())
    return out[0], out[1]


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


class Previews:
    """The engine: `batch(paths)` -> one result per path, {"error"} for those the CPU should do."""

    def __init__(self, device):
        import torch

        self.torch, self.dev = torch, torch.device(device)
        self.tiles = TorchTiles(device)
        self.mats = {}
        sdiv = [0] + [int(round((255 << 12) / i)) for i in range(1, 256)]  # OpenCV's RGB2HSV saturation table
        self.sdiv = torch.tensor(sdiv, dtype=torch.int64, device=self.dev)
        self.pool = ThreadPoolExecutor(THREADS)

    def _mat(self, kind: str, n: int, m: int):
        key = (kind, n, m)
        if key not in self.mats:
            w = lanczos_weights(n, m) if kind == "lanczos" else box_weights(n, m)
            self.mats[key] = self.torch.tensor(w, device=self.dev)
            if len(self.mats) > 64:  # many sizes: keep the cache small
                self.mats.pop(next(iter(self.mats)))
        return self.mats[key]

    def _pass(self, img, mat, axis, rounded=True):
        """One resampling pass along an axis (2: width, 1: height) of a (3, H, W) float tensor; rounded to 8 bits
        as Pillow does after each Lanczos pass."""
        out = img @ mat.T if axis == 2 else mat @ img
        return self.torch.floor(out.clamp(0, 255) + 0.5) if rounded else out

    def resized(self, img, long_side: int):
        """previews.resized() for a float (3, H, W) tensor of 8-bit values."""
        _, h, w = img.shape
        scale = long_side / max(w, h)
        if scale >= 1:
            return img
        factor = int(1 / scale / 2)
        if factor >= 2:  # reduce() averages each block at once: one rounding
            img = self._pass(self._pass(img, self._mat("box", w, factor), 2, rounded=False), self._mat("box", h, factor), 1)
        tw, th = max(1, round(w * scale)), max(1, round(h * scale))
        if img.shape[2] != tw:
            img = self._pass(img, self._mat("lanczos", img.shape[2], tw), 2)
        if img.shape[1] != th:
            img = self._pass(img, self._mat("lanczos", img.shape[1], th), 1)
        return img

    def exposure(self, prev) -> dict:
        """previews.exposure() for a (3, H, W) uint8 preview, with OpenCV's integer RGB2GRAY and HSV saturation."""
        torch = self.torch
        r, g, b = (prev[k].to(torch.int64) for k in range(3))
        y = (r * 4899 + g * 9617 + b * 1868 + 8192) >> 14
        v, vmin = torch.maximum(torch.maximum(r, g), b), torch.minimum(torch.minimum(r, g), b)
        s = ((v - vmin) * self.sdiv[v] + (1 << 11)) >> 12
        q = torch.quantile(y.flatten().double(), torch.tensor([0.01, 0.5, 0.99], dtype=torch.float64, device=self.dev))
        return {"clip_hi": float((y >= 250).double().mean()), "clip_lo": float((y <= 5).double().mean()),
                "median": float(q[1]), "p01": float(q[0]), "p99": float(q[2]),
                "saturation": float(s.double().mean() / 255)}

    def gpu(self, a: np.ndarray):
        """The GPU part for one decoded photo: (result, preview pixels, thumbnail pixels)."""
        torch = self.torch
        with torch.inference_mode():
            t = torch.from_numpy(a).to(self.dev)
            h, w = a.shape[:2]
            f = t.permute(2, 0, 1).float()
            prev = self.resized(f, PREVIEW_PX)
            th = self.resized(prev, THUMB_PX)
            r, g, b = (t[..., k].to(torch.int32) for k in range(3))
            gray = ((r * 19595 + g * 38470 + b * 7471 + 0x8000) >> 16).to(torch.uint8)  # Pillow's RGB -> "L"
            tiles = self.tiles(gray)
            prev8, th8 = prev.to(torch.uint8), th.to(torch.uint8)
            res = {"w": w, "h": h, "tiles": tiles.tolist(), "exposure": self.exposure(prev8)}
            return res, prev8.permute(1, 2, 0).contiguous().cpu().numpy(), th8.permute(1, 2, 0).contiguous().cpu().numpy()

    def batch(self, paths: list[str]) -> list[dict]:
        t0 = time.perf_counter()
        out: list = [None] * len(paths)
        loads = [self.pool.submit(_load, p) for p in paths]
        encodes = {}
        for k, fut in enumerate(loads):
            try:
                a = fut.result()
            except Exception as exc:  # noqa: BLE001 - the CPU path: cut short, broken, not an image
                out[k] = {"error": f"decode: {type(exc).__name__}: {str(exc)[:200]}"}
                continue
            if a.shape[0] * a.shape[1] > MAX_SHARP_MP * 1e6:
                out[k] = {"error": "over 25 MP: the CPU's"}
                continue
            try:
                out[k], prev, th = self.gpu(a)
            except Exception as exc:  # noqa: BLE001
                out[k] = {"error": f"gpu: {type(exc).__name__}: {str(exc)[:200]}"}
                continue
            del a
            encodes[k] = (self.pool.submit(_encode, prev, th), th)
        for k, (fut, th) in encodes.items():
            try:
                pj, tj = fut.result()
                out[k] |= {"preview": b64(pj), "thumb": b64(tj), "thumb_px": b64(th.tobytes()), "thumb_shape": list(th.shape)}
            except Exception as exc:  # noqa: BLE001
                out[k] = {"error": f"encode: {type(exc).__name__}: {str(exc)[:200]}"}
        n = sum(1 for x in out if "error" not in x)
        for x in out:
            if "error" not in x:
                x["seconds"] = (time.perf_counter() - t0) / max(n, 1)
        return out


def decode_reply(x: dict) -> tuple[bytes, bytes, np.ndarray, np.ndarray]:
    """One result, for the stage: the preview and thumbnail JPEGs, the thumbnail's pixels, the tiles."""
    thumb_px = np.frombuffer(base64.b64decode(x["thumb_px"]), np.uint8).reshape(x["thumb_shape"])
    return (base64.b64decode(x["preview"]), base64.b64decode(x["thumb"]), thumb_px, np.asarray(x["tiles"], np.float32))
