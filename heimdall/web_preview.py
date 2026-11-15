"""Bounded, read-only pixel preview of an exact locally cached FITS product."""

from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path

import numpy as np
from astropy.io import fits

MAX_BYTES = 32 * 1024 * 1024
MAX_AXIS = 4096
PREVIEW_AXIS = 256


def _candidate_paths(service, cache: Path, sha: str):
    row = service.db.one("SELECT key FROM product_keys WHERE sha256=? AND kind='science' LIMIT 1", (sha,))
    if row is not None:
        product = service.store.get(row["key"])
        if product is not None:
            yield product.path
    # Historical investigations reference the adapter cache by checksum, before the verified store existed.
    for sidecar in (cache / "irsa-spherex").glob("*/*.json"):
        try:
            if sidecar.stat().st_size > 64 * 1024:
                continue
            record = json.loads(sidecar.read_text(encoding="utf-8"))
            if isinstance(record, dict) and record.get("sha256") == sha:
                yield sidecar.with_suffix(".bin")
        except (OSError, ValueError):
            continue


def cached_preview(service, cache: Path, sha: str) -> dict | None:
    for path in _candidate_paths(service, cache, sha):
        try:
            with path.open("rb") as handle:
                data = handle.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES or hashlib.sha256(data).hexdigest() != sha:
                continue
            preview = _render(data)
            if preview is not None:
                return preview | {"sha256": sha}
        except (OSError, ValueError, TypeError, IndexError, OverflowError):
            continue
    return None


def _render(data: bytes) -> dict | None:
    with fits.open(io.BytesIO(data), memmap=False) as hdus:
        hdu = hdus["IMAGE"] if "IMAGE" in hdus else hdus[0]
        nx, ny = hdu.header.get("NAXIS1", 0), hdu.header.get("NAXIS2", 0)
        if hdu.header.get("NAXIS") != 2 or not (0 < nx <= MAX_AXIS and 0 < ny <= MAX_AXIS):
            return None
        stride = max(1, (max(nx, ny) + PREVIEW_AXIS - 1) // PREVIEW_AXIS)
        sample = np.asarray(hdu.data[::stride, ::stride], dtype=np.float64)
        sample = np.flipud(sample)
        finite = np.isfinite(sample)
        if not finite.any():
            return None
        lo, hi = np.percentile(sample[finite], [2, 99.5])
        if hi <= lo:
            hi = float(lo) + 1.0
        values = np.zeros(sample.shape, dtype=np.uint8)
        values[finite] = np.rint(255 * np.clip((sample[finite] - float(lo)) / (float(hi) - float(lo)), 0, 1)).astype(np.uint8)
        return {
            "schema": "heimdall.product_preview/1.0", "width": values.shape[1], "height": values.shape[0],
            "source_width": nx, "source_height": ny, "hdu": hdu.name, "step_pixels": stride,
            "pixel_format": "grayscale_8_base64", "pixels": base64.b64encode(values.tobytes()).decode("ascii"),
            "stretch_percentiles": [2, 99.5], "stretch_values": [float(lo), float(hi)],
        }
