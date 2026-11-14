"""Map projections for the sky-map data contract.

- ``gnomonic`` (re-exported from skygeom): local views around a field centre.
- ``mollweide``: equal-area all-sky view.

Orientation convention: by default, east is to the left (the astronomical
convention for maps viewed from inside the celestial sphere), so x increases
toward decreasing right ascension. Pass ``east_left=False`` for the opposite.
"""

from __future__ import annotations

import math

import numpy as np

from ..engine.skygeom import gnomonic, inverse_gnomonic

__all__ = ["gnomonic", "inverse_gnomonic", "mollweide", "inverse_mollweide", "local_map_xy"]

_SQRT2 = math.sqrt(2.0)


def _mollweide_theta(lat_rad: np.ndarray) -> np.ndarray:
    theta = np.array(lat_rad, dtype=float, copy=True)
    target = math.pi * np.sin(lat_rad)
    polar = np.abs(np.abs(lat_rad) - math.pi / 2) < 1e-12
    for _ in range(50):
        f = 2 * theta + np.sin(2 * theta) - target
        fp = 2 + 2 * np.cos(2 * theta)
        step = np.where(np.abs(fp) > 1e-15, f / fp, 0.0)
        theta = theta - step
        if np.all(np.abs(step) < 1e-13):
            break
    return np.where(polar, np.sign(lat_rad) * math.pi / 2, theta)


def mollweide(lon_deg, lat_deg, lon0_deg: float = 0.0, east_left: bool = True) -> tuple[np.ndarray, np.ndarray]:
    """Mollweide projection. x in [-2*sqrt(2), 2*sqrt(2)], y in [-sqrt(2), sqrt(2)]."""
    lam = np.radians(((np.asarray(lon_deg, dtype=float) - lon0_deg + 180.0) % 360.0) - 180.0)
    phi = np.radians(np.asarray(lat_deg, dtype=float))
    theta = _mollweide_theta(phi)
    x = (2 * _SQRT2 / math.pi) * lam * np.cos(theta)
    y = _SQRT2 * np.sin(theta)
    return (-x if east_left else x), y


def inverse_mollweide(x, y, lon0_deg: float = 0.0, east_left: bool = True) -> tuple[np.ndarray, np.ndarray]:
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    if east_left:
        x = -x
    theta = np.arcsin(np.clip(y / _SQRT2, -1.0, 1.0))
    phi = np.arcsin(np.clip((2 * theta + np.sin(2 * theta)) / math.pi, -1.0, 1.0))
    with np.errstate(divide="ignore", invalid="ignore"):
        lam = np.where(np.abs(np.cos(theta)) > 1e-15, math.pi * x / (2 * _SQRT2 * np.cos(theta)), 0.0)
    lon = (np.degrees(lam) + lon0_deg) % 360.0
    return lon, np.degrees(phi)


def local_map_xy(ra_deg, dec_deg, ra0_deg: float, dec0_deg: float, east_left: bool = True) -> tuple[np.ndarray, np.ndarray]:
    """Gnomonic map coordinates in degrees with the chosen east orientation."""
    xi, eta = gnomonic(ra_deg, dec_deg, ra0_deg, dec0_deg)
    return (-xi if east_left else xi), eta
