"""The sharpness grid's shared parts, without OpenCV, so the 4090 worker (which has no cv2) computes it the same
way as previews.py on the CPU.

A photo's luminance gives eight tile means at full resolution: the structure tensor (gx², gy², gx·gy of the
Sobel gradients of a lightly blurred image), the Laplacian (squared and plain), the luminance (plain and
squared), and Immerkaer's noise filter. previews.tile_stats computes them with OpenCV; TorchTiles with torch on
a GPU: the same filters with OpenCV's default border (reflect-101, torch's "reflect"), and OpenCV's INTER_AREA
weights for the tile means. grid_from_tiles turns either into the measures that are stored.
"""

import math

import numpy as np

GRID_LONG = 24          # tiles along the long side
NOISE_KERNEL = np.array([[1, -2, 1], [-2, 4, -2], [1, -2, 1]], np.float32)  # Immerkaer's noise estimate
SOBEL_X = np.array([[-1, 0, 1], [-2, 0, 2], [-1, 0, 1]], np.float32)
LAPLACIAN = np.array([[2, 0, 2], [0, -8, 0], [2, 0, 2]], np.float32)       # cv2.Laplacian, ksize=3
BLUR_SIGMA, BLUR_TAPS = 0.8, 7  # cv2.GaussianBlur(float32, (0, 0), 0.8): round(0.8 * 4 * 2 + 1) | 1 = 7 taps


def gaussian_taps(n: int = BLUR_TAPS, sigma: float = BLUR_SIGMA) -> np.ndarray:
    """cv2.getGaussianKernel(n, sigma): computed in double, normalized, then float32."""
    x = np.arange(n, dtype=np.float64) - (n - 1) / 2
    k = np.exp(-(x * x) / (2 * sigma * sigma))
    return (k / k.sum()).astype(np.float32)


def grid_shape(h: int, w: int) -> tuple[int, int]:
    """(gw, gh): GRID_LONG tiles along the long side."""
    return (GRID_LONG, max(1, round(GRID_LONG * h / w))) if w >= h else (max(1, round(GRID_LONG * w / h)), GRID_LONG)


def area_weights(n: int, m: int) -> np.ndarray:
    """OpenCV's INTER_AREA weights for shrinking n pixels to m, as an (m, n) matrix: its computeResizeAreaTab, so
    `Ry @ a @ Rx.T` is cv2.resize(a, (gw, gh), interpolation=cv2.INTER_AREA)."""
    scale = n / m
    out = np.zeros((m, n), np.float64)
    for d in range(m):
        f1 = d * scale
        f2 = f1 + scale
        cell = min(scale, n - f1)
        s1, s2 = math.ceil(f1), math.floor(f2)
        s2 = min(s2, n - 1)
        s1 = min(s1, s2)
        if s1 - f1 > 1e-3:
            out[d, s1 - 1] = (s1 - f1) / cell
        out[d, s1:s2] = 1 / cell
        if f2 - s2 > 1e-3:
            out[d, s2] = min(min(f2 - s2, 1.0), cell) / cell
    return out.astype(np.float32)


def grid_from_tiles(tiles: np.ndarray) -> dict:
    """The stored sharpness measures from the eight tile means (previews.tile_stats or TorchTiles)."""
    jxx, jyy, jxy, lap2, lap1, mean, mean2, resp = tiles
    ten = jxx + jyy
    coherence = np.sqrt((jxx - jyy) ** 2 + 4 * jxy**2) / (ten + 1e-6)
    angle2 = np.arctan2(2 * jxy, jxx - jyy)  # 2θ of the dominant gradient direction
    lap_var = lap2 - lap1**2
    std = np.sqrt(np.maximum(mean2 - mean**2, 0))
    # Immerkaer's noise estimate per tile, then the median over the flattest tiles.
    noise_tiles = resp * np.sqrt(np.pi / 2) / 6
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


class TorchTiles:
    """The eight tile means with torch, on any device. No boolean-mask ops (which torch 2.15.0+xpu gets wrong)."""

    def __init__(self, device):
        import torch
        import torch.nn.functional as F

        self.torch, self.F, self.dev = torch, F, torch.device(device)
        t = lambda a: torch.tensor(np.asarray(a, np.float32), device=self.dev)  # noqa: E731
        g = gaussian_taps()
        self.g_row, self.g_col = t(g).view(1, 1, 1, BLUR_TAPS), t(g).view(1, 1, BLUR_TAPS, 1)
        self.filters = t([SOBEL_X, SOBEL_X.T, LAPLACIAN]).view(3, 1, 3, 3)
        self.noise = t(NOISE_KERNEL).view(1, 1, 3, 3)
        self.areas = {}

    def _area(self, n: int, m: int):
        if (n, m) not in self.areas:
            self.areas[(n, m)] = self.torch.tensor(area_weights(n, m), device=self.dev)
        return self.areas[(n, m)]

    def __call__(self, gray) -> np.ndarray:
        """(8, gh, gw) float32 for a luminance image: a uint8 numpy array, or a 2-D tensor on the device."""
        torch, F = self.torch, self.F
        with torch.inference_mode():
            if isinstance(gray, np.ndarray):
                gray = torch.from_numpy(np.ascontiguousarray(gray)).to(self.dev)
            h, w = gray.shape
            gw, gh = grid_shape(h, w)
            x = gray.float().view(1, 1, h, w)
            p = BLUR_TAPS // 2
            smooth = F.conv2d(F.pad(x, (p, p, 0, 0), mode="reflect"), self.g_row)
            smooth = F.conv2d(F.pad(smooth, (0, 0, p, p), mode="reflect"), self.g_col)
            d = F.conv2d(F.pad(smooth, (1, 1, 1, 1), mode="reflect"), self.filters)[0]  # gx, gy, Laplacian
            del smooth
            resp = F.conv2d(F.pad(x, (1, 1, 1, 1), mode="reflect"), self.noise)[0, 0].abs()
            ry, rx = self._area(h, gh), self._area(w, gw).T

            def tm(a):
                return ry @ a @ rx

            gx, gy, lap, g = d[0], d[1], d[2], x[0, 0]
            out = torch.stack([tm(gx * gx), tm(gy * gy), tm(gx * gy), tm(lap * lap), tm(lap), tm(g), tm(g * g), tm(resp)])
            return out.cpu().numpy()
