"""Spatial index for viewport queries.

- Exact cone queries use a k-d tree on unit vectors (chord distance), so a
  query costs O(log N + k).
- Zoomed-out views aggregate items into tiles. Level L divides declination
  into bands of height h = 180 / 2^(L+1) degrees; each band is split into
  max(1, floor(360 cos(dec_c) / h)) right-ascension cells, so tiles have
  roughly equal area. Tile ids are "L<level>:<band>:<cell>".
"""

from __future__ import annotations

import math

import numpy as np
from scipy.spatial import cKDTree

from ..engine.skygeom import unit_vector


def tile_of(ra: float, dec: float, level: int) -> tuple[str, float, float]:
    h = 180.0 / 2 ** (level + 1)
    nb = int(round(180.0 / h))
    band = min(int((dec + 90.0) // h), nb - 1)
    dec_c = -90.0 + (band + 0.5) * h
    n = max(1, int(360.0 * math.cos(math.radians(dec_c)) // h))
    cell = min(int((ra % 360.0) // (360.0 / n)), n - 1)
    return f"L{level}:{band}:{cell}", (cell + 0.5) * 360.0 / n, dec_c


def level_for_fov(fov_deg: float, cells_across: int = 32) -> int:
    target = max(fov_deg / cells_across, 1e-6)
    return max(0, min(20, int(math.ceil(math.log2(180.0 / target) - 1))))


class SkyIndex:
    def __init__(self, items: list[dict], ra_key: str = "ra", dec_key: str = "dec") -> None:
        self.items = items
        self.ra = np.array([float(i[ra_key]) for i in items]) if items else np.zeros(0)
        self.dec = np.array([float(i[dec_key]) for i in items]) if items else np.zeros(0)
        self.tree = cKDTree(unit_vector(self.ra, self.dec)) if items else None

    def cone(self, ra: float, dec: float, radius_deg: float) -> list[int]:
        if self.tree is None:
            return []
        chord = 2.0 * math.sin(math.radians(min(radius_deg, 180.0)) / 2.0)
        return sorted(self.tree.query_ball_point(unit_vector(ra, dec), chord))

    def tiles(self, level: int, indices: list[int] | None = None) -> list[dict]:
        counts: dict[str, list] = {}
        for i in (range(len(self.items)) if indices is None else indices):
            tid, rc, dc = tile_of(self.ra[i], self.dec[i], level)
            entry = counts.setdefault(tid, [rc, dc, 0])
            entry[2] += 1
        return [{"tile": t, "ra_centre": v[0], "dec_centre": v[1], "count": v[2]} for t, v in sorted(counts.items())]

    def viewport(self, ra: float, dec: float, fov_deg: float, max_items: int = 2000) -> dict:
        """Items inside the circle circumscribing a square viewport, or tile counts when there are too many."""
        idx = self.cone(ra, dec, fov_deg * math.sqrt(2.0) / 2.0)
        if len(idx) <= max_items:
            return {"mode": "items", "count": len(idx), "items": [self.items[i] for i in idx]}
        level = level_for_fov(fov_deg)
        return {"mode": "tiles", "count": len(idx), "level": level, "tiles": self.tiles(level, idx)}
