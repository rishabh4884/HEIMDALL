"""PSF models for detection and photometry.

Preference order from IRSA documentation:

1. ``EPSF_EMBEDDED``: the ``EPSF`` binary table in images from pipeline
   version 7.0 or later (QR3+). The ePSF is a sub-pixel-resolved model of the
   pixel-convolved PSF, so it is *sampled* at native pixel offsets.
2. ``EPSF_CALIBRATION``: the per-detector ePSF calibration product. IRSA
   advises using the ePSF for all images; the effective PSF of older images
   is unchanged.
3. ``LEGACY_PSF_HDU``: the ``PSF`` cube of QR1/QR2 images, a super-resolution
   PSF oversampled 10x with unit sum on the fine grid. IRSA advises against
   it. It is integrated onto native pixels here: an approximation.
4. ``GAUSSIAN_HEADER_FWHM``: a circular Gaussian with the header median
   ``PSF_FWHM``: an approximation.

Pixel convention: 0-based pixel centres at integer coordinates. Rendered
stamps have unit total flux over the full PSF (they are not renormalised when
an image edge truncates them).
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod

import numpy as np
from scipy.ndimage import map_coordinates
from scipy.special import erf


class PsfModel(ABC):
    kind: str
    source: str
    approximation: bool

    @abstractmethod
    def _render_local(self, x0: float, y0: float, x_ll: int, y_ll: int, nx: int, ny: int) -> np.ndarray:
        """Render on the pixel grid starting at (x_ll, y_ll), size (ny, nx), unnormalised."""

    def render(self, x0: float, y0: float, x_ll: int, y_ll: int, nx: int, ny: int) -> np.ndarray:
        stamp = self._render_local(x0, y0, x_ll, y_ll, nx, ny)
        stamp = np.clip(stamp, 0.0, None)
        return stamp

    def stamp(self, x0: float, y0: float, size: int) -> tuple[np.ndarray, int, int]:
        half = size // 2
        x_ll = int(math.floor(x0 + 0.5)) - half
        y_ll = int(math.floor(y0 + 0.5)) - half
        return self.render(x0, y0, x_ll, y_ll, size, size), x_ll, y_ll

    def describe(self) -> dict[str, object]:
        return {"kind": self.kind, "source": self.source, "approximation": self.approximation}


class _ZonedMixin:
    centers: np.ndarray  # (n, 2) parent-image coordinates (0-based)
    offset: tuple[float, float]  # add to local pixel coordinates to get parent coordinates

    def zone_index(self, x: float, y: float) -> int:
        px = x + self.offset[0]
        py = y + self.offset[1]
        d2 = (self.centers[:, 0] - px) ** 2 + (self.centers[:, 1] - py) ** 2
        return int(np.argmin(d2))


class EpsfModel(_ZonedMixin, PsfModel):
    """Zoned ePSF (photutils-style, oversampled), sampled at native pixel offsets."""

    def __init__(self, arrays: np.ndarray, centers: np.ndarray, oversampling: tuple[int, int],
                 offset: tuple[float, float], kind: str, source: str) -> None:
        if arrays.ndim != 3 or arrays.shape[0] != centers.shape[0]:
            raise ValueError("ePSF arrays and zone centres do not match")
        self.arrays = np.asarray(arrays, dtype=float)
        self.centers = np.asarray(centers, dtype=float)
        self.oversampling = oversampling  # (ovs_y, ovs_x)
        self.offset = offset
        self.kind = kind
        self.source = source
        self.approximation = False

    def _render_local(self, x0, y0, x_ll, y_ll, nx, ny):
        epsf = self.arrays[self.zone_index(x0, y0)]
        cy = (epsf.shape[0] - 1) / 2.0
        cx = (epsf.shape[1] - 1) / 2.0
        xs = x_ll + np.arange(nx) - x0
        ys = y_ll + np.arange(ny) - y0
        gx, gy = np.meshgrid(xs * self.oversampling[1] + cx, ys * self.oversampling[0] + cy)
        values = map_coordinates(epsf, [gy.ravel(), gx.ravel()], order=3, mode="constant", cval=0.0)
        values = values.reshape(ny, nx)
        # Normalise using the full (untruncated) stamp so edge truncation keeps total flux = 1.
        full = self._full_norm(epsf, x0, y0)
        return values / full

    def _full_norm(self, epsf: np.ndarray, x0: float, y0: float) -> float:
        ext = int(math.ceil(max(epsf.shape) / min(self.oversampling))) + 2
        size = 2 * ext + 1
        x_ll = int(math.floor(x0 + 0.5)) - ext
        y_ll = int(math.floor(y0 + 0.5)) - ext
        cy = (epsf.shape[0] - 1) / 2.0
        cx = (epsf.shape[1] - 1) / 2.0
        xs = x_ll + np.arange(size) - x0
        ys = y_ll + np.arange(size) - y0
        gx, gy = np.meshgrid(xs * self.oversampling[1] + cx, ys * self.oversampling[0] + cy)
        total = map_coordinates(epsf, [gy.ravel(), gx.ravel()], order=3, mode="constant", cval=0.0)
        total = float(np.clip(total, 0.0, None).sum())
        if not total > 0:
            raise ValueError("ePSF renders to zero flux")
        return total


class LegacyPsfModel(_ZonedMixin, PsfModel):
    """Zoned legacy super-resolution PSF cube (unit sum on the fine grid), integrated onto native pixels.

    Each native pixel is integrated with ``sub`` x ``sub`` bilinear samples of
    the fine grid, which keeps the rendered PSF continuous in the source
    position (needed for centroid fitting).
    """

    def __init__(self, planes: np.ndarray, centers: np.ndarray, oversampling: int,
                 offset: tuple[float, float], source: str, sub: int = 5) -> None:
        self.planes = np.asarray(planes, dtype=float)
        self.centers = np.asarray(centers, dtype=float)
        self.ovs = int(oversampling)
        self.offset = offset
        self.kind = "LEGACY_PSF_HDU"
        self.source = source
        self.approximation = True
        self.sub = int(sub)
        self._offs = (np.arange(self.sub) + 0.5) / self.sub - 0.5

    def _render_local(self, x0, y0, x_ll, y_ll, nx, ny):
        plane = self.planes[self.zone_index(x0, y0)]
        c = (plane.shape[-1] - 1) / 2.0
        u = ((x_ll + np.arange(nx))[:, None] + self._offs[None, :] - x0) * self.ovs + c
        v = ((y_ll + np.arange(ny))[:, None] + self._offs[None, :] - y0) * self.ovs + c
        vv, uu = np.broadcast_arrays(v[:, :, None, None], u[None, None, :, :])
        vals = map_coordinates(plane, [vv.ravel(), uu.ravel()], order=1, mode="constant", cval=0.0)
        out = vals.reshape(ny, self.sub, nx, self.sub).sum(axis=(1, 3)) * (self.ovs / self.sub) ** 2
        total = float(plane.sum())
        return out / total if total > 0 else out


class GaussianPsfModel(PsfModel):
    """Circular Gaussian integrated exactly over each pixel."""

    def __init__(self, sigma_px: float, source: str) -> None:
        if not sigma_px > 0:
            raise ValueError("Gaussian PSF sigma must be positive")
        self.sigma_px = float(sigma_px)
        self.kind = "GAUSSIAN_HEADER_FWHM"
        self.source = source
        self.approximation = True

    def _render_local(self, x0, y0, x_ll, y_ll, nx, ny):
        s = math.sqrt(2.0) * self.sigma_px
        xe = x_ll + np.arange(nx + 1) - 0.5 - x0
        ye = y_ll + np.arange(ny + 1) - 0.5 - y0
        fx = 0.5 * np.diff(erf(xe / s))
        fy = 0.5 * np.diff(erf(ye / s))
        return np.outer(fy, fx)


def gaussian_from_fwhm(fwhm_arcsec: float, pixel_scale_arcsec: float, source: str) -> GaussianPsfModel:
    sigma_px = fwhm_arcsec / pixel_scale_arcsec / (2.0 * math.sqrt(2.0 * math.log(2.0)))
    return GaussianPsfModel(sigma_px, source)


def load_epsf_table(hdu, offset: tuple[float, float], kind: str, source: str) -> EpsfModel:
    """Build an ``EpsfModel`` from an ``EPSF`` binary-table HDU (columns XCENTER, YCENTER, EPSF)."""
    data = hdu.data
    header = hdu.header
    names = {n.upper() for n in data.columns.names}
    for required in ("XCENTER", "YCENTER", "EPSF"):
        if required not in names:
            raise ValueError(f"EPSF table lacks column {required}")
    arrays = np.asarray(data["EPSF"], dtype=float)
    if arrays.ndim != 3:
        raise ValueError("EPSF column must hold 2-D arrays")
    centers = np.stack([np.asarray(data["XCENTER"], dtype=float), np.asarray(data["YCENTER"], dtype=float)], axis=1)
    ovs = (int(header.get("OVSMPY", 1)), int(header.get("OVSMPX", 1)))
    return EpsfModel(arrays, centers, ovs, offset, kind, source)
