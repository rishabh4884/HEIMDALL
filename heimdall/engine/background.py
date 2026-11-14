"""Background estimation.

Method: sigma-clipped median of usable pixels (``bad`` and background-excluded
pixels such as ``SOURCE`` removed). Images at least two mesh cells wide use a
mesh of such medians, median-filtered over 3x3 cells and interpolated
bilinearly; smaller images (typical cutouts) use one constant level.

The level uncertainty is estimated as sqrt(pi/2) * rms / sqrt(n), the
large-sample standard error of a median of Gaussian values. It is propagated
into PSF-fit flux uncertainties.

Note: SPHEREx images include zodiacal light (not subtracted by the pipeline);
it is part of the smooth background removed here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy.ndimage import map_coordinates, median_filter

from .config import DetectionParams
from .stats import sigma_clipped_stats


@dataclass
class Background:
    level: np.ndarray
    level_sigma: float
    rms: float
    n_used: int
    mesh_shape: tuple[int, int]
    method: str

    def summary(self) -> dict[str, object]:
        return {
            "median_level": float(np.median(self.level)),
            "level_sigma": self.level_sigma,
            "rms": self.rms,
            "n_pixels_used": self.n_used,
            "mesh_shape": list(self.mesh_shape),
            "method": self.method,
        }


def estimate_background(data: np.ndarray, bad: np.ndarray, exclude: np.ndarray, params: DetectionParams) -> Background:
    usable = ~bad & ~exclude & np.isfinite(data)
    if usable.sum() < 10:
        usable = ~bad & np.isfinite(data)
    if usable.sum() < 3:
        raise ValueError("too few usable pixels to estimate the background")
    med, rms, n = sigma_clipped_stats(data[usable], params.background_clip_sigma, params.background_max_iter)
    ny, nx = data.shape
    mesh = params.background_mesh_px
    if ny < 2 * mesh or nx < 2 * mesh:
        level = np.full(data.shape, med)
        return Background(level=level, level_sigma=math.sqrt(math.pi / 2) * rms / math.sqrt(max(n, 1)), rms=rms,
                          n_used=n, mesh_shape=(1, 1), method="global sigma-clipped median")

    nmy, nmx = ny // mesh, nx // mesh
    grid = np.full((nmy, nmx), np.nan)
    counts = np.zeros((nmy, nmx), dtype=int)
    for j in range(nmy):
        ys = slice(j * mesh, ny if j == nmy - 1 else (j + 1) * mesh)
        for i in range(nmx):
            xs = slice(i * mesh, nx if i == nmx - 1 else (i + 1) * mesh)
            cell = data[ys, xs][usable[ys, xs]]
            if cell.size >= max(10, mesh * mesh // 4):
                grid[j, i], _, counts[j, i] = sigma_clipped_stats(cell, params.background_clip_sigma, params.background_max_iter)
    grid = np.where(np.isfinite(grid), grid, med)
    if min(nmy, nmx) >= 3:
        grid = median_filter(grid, size=3, mode="nearest")
    cell_h = ny / nmy
    cell_w = nx / nmx
    yy = np.clip((np.arange(ny) + 0.5) / cell_h - 0.5, 0, nmy - 1)
    xx = np.clip((np.arange(nx) + 0.5) / cell_w - 0.5, 0, nmx - 1)
    gy, gx = np.meshgrid(yy, xx, indexing="ij")
    level = map_coordinates(grid, [gy.ravel(), gx.ravel()], order=1, mode="nearest").reshape(ny, nx)
    typical = float(np.median(counts[counts > 0])) if np.any(counts > 0) else float(n)
    return Background(level=level, level_sigma=math.sqrt(math.pi / 2) * rms / math.sqrt(max(typical, 1.0)), rms=rms,
                      n_used=n, mesh_shape=(nmy, nmx), method=f"mesh {mesh}px sigma-clipped medians, 3x3 median filter, bilinear")
