"""Co-registered display pair of two exact locally cached SPHEREx products (``heimdall.product_pair/1.0``).

Both images are parsed by the normal ingest path (QC, flag vocabulary, WCS with SIP, spectral WCS) and resampled onto one
north-up, east-left gnomonic grid centred on the requested position, so a browser can show them side by side or blink them
without guessing alignment. Display pixels are 8-bit stretches and carry no photometric meaning.

A difference image is produced only when the pair is comparable: same detector and the wavelength at the grid centre within the
configured temporal tolerance (``EngineConfig.temporal.wavelength_tolerance_fraction`` x bandwidth). When a saved investigation is
named and both observations belong to one of its comparable series, that series' own comparison record is the authority. The
difference is in units of its propagated uncertainty, after a per-image sigma-clipped median background subtraction; it is not
PSF-matched and resampling correlates neighbouring pixels, so it is an inspection aid, never a measurement.
"""

from __future__ import annotations

import base64
import math
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from .contracts.provenance import DataOrigin, Provenance
from .engine.config import EngineConfig
from .ingest.spherex import read_spherex_mef
from .web_preview import _candidate_paths

PAIR_SCHEMA = "heimdall.product_pair/1.0"
MAX_NPIX = 128
CLIP_SIGMA = 5.0


def _load(service, cache: Path, sha: str):
    import hashlib
    for path in _candidate_paths(service, cache, sha):
        try:
            if hashlib.sha256(Path(path).read_bytes()).hexdigest() != sha:
                continue
        except OSError:
            continue
        # Real cached products ingest as CACHED. A synthetic test fixture is refused under that origin by the ingest guard and
        # is only accepted when declared as a fixture, so it stays marked synthetic (meta.synthetic) all the way to the display.
        for origin in (DataOrigin.CACHED, DataOrigin.SYNTHETIC_TEST_FIXTURE):
            prov = Provenance(origin=origin, retrieved_at=datetime.now(UTC), software_version="preview")
            ing = read_spherex_mef(path, source_id="local-cache", provenance=prov)
            if ing.image is not None:
                return ing.image
            if not any(i.check == "data_origin" and i.state == "FAILED" for i in ing.qc.items):
                break
    return None


def _grid(ra0: float, dec0: float, size_arcsec: float, npix: int):
    """World coordinates of an npix x npix gnomonic grid, row 0 = north edge, column 0 = east edge."""
    step = math.radians(size_arcsec / 3600.0 / npix)
    off = (np.arange(npix) - (npix - 1) / 2.0) * step
    xi = -off[None, :]          # east to the left: xi decreases with column
    eta = -off[:, None]         # north up: eta decreases with row
    xi, eta = np.broadcast_arrays(xi, eta)
    a0, d0 = math.radians(ra0), math.radians(dec0)
    den = math.cos(d0) - eta * math.sin(d0)
    ra = np.degrees(a0 + np.arctan2(xi, den)) % 360.0
    dec = np.degrees(np.arctan2(math.sin(d0) + eta * math.cos(d0), np.hypot(xi, den)))
    return ra, dec


def _bilinear(arr: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    ny, nx = arr.shape
    out = np.full(x.shape, np.nan)
    ok = np.isfinite(x) & np.isfinite(y) & (x >= 0) & (y >= 0) & (x <= nx - 1) & (y <= ny - 1)
    xi, yi = np.floor(x[ok]).astype(int), np.floor(y[ok]).astype(int)
    xi1, yi1 = np.minimum(xi + 1, nx - 1), np.minimum(yi + 1, ny - 1)
    fx, fy = x[ok] - xi, y[ok] - yi
    a = arr.astype(float)
    out[ok] = (a[yi, xi] * (1 - fx) * (1 - fy) + a[yi, xi1] * fx * (1 - fy) + a[yi1, xi] * (1 - fx) * fy + a[yi1, xi1] * fx * fy)
    return out


def _resample(img, ra, dec):
    """(analysis value, analysis variance, coverage, display value, engine-mask fraction) on the grid.

    The analysis arrays exclude pixels the engine's flag policy rejects (as photometry does). The display array keeps every finite
    pixel, so the picture is not punched with holes; the mask is returned separately for an optional overlay."""
    x, y = img.world_to_pixel(ra, dec)
    x = np.asarray(x, float) - img.origin[0]
    y = np.asarray(y, float) - img.origin[1]
    bad = img.bad_mask().astype(float)
    value = _bilinear(np.where(bad > 0, np.nan, img.image), x, y)
    var = _bilinear(np.where(bad > 0, np.nan, img.variance), x, y)
    covered = np.isfinite(_bilinear(np.zeros_like(bad), x, y))
    display = _bilinear(np.where(np.isfinite(img.image), img.image, np.nan), x, y)
    masked = _bilinear(bad, x, y)
    return value, var, covered, display, masked


def _u8(values: np.ndarray, lo: float, hi: float) -> str:
    out = np.zeros(values.shape, dtype=np.uint8)
    ok = np.isfinite(values)
    out[ok] = np.rint(1 + 254 * np.clip((values[ok] - lo) / (hi - lo), 0, 1)).astype(np.uint8)  # 0 = no data
    return base64.b64encode(out.tobytes()).decode("ascii")


def _stretch(*arrays) -> tuple[float, float]:
    vals = np.concatenate([a[np.isfinite(a)] for a in arrays]) if arrays else np.array([])
    if vals.size == 0:
        return 0.0, 1.0
    lo, hi = np.percentile(vals, [2, 99.5])
    return float(lo), float(hi if hi > lo else lo + 1.0)


def _clipped_median(a: np.ndarray) -> float:
    v = a[np.isfinite(a)]
    for _ in range(3):
        if v.size < 10:
            break
        med, mad = np.median(v), np.median(np.abs(v - np.median(v))) * 1.4826
        v = v[np.abs(v - med) <= 3 * mad] if mad > 0 else v
    return float(np.median(v)) if v.size else 0.0


def _series_record(doc: dict | None, a: str, b: str) -> dict | None:
    for s in ((doc or {}).get("analyses") or {}).get("variability", {}).get("series") or []:
        ids = {p.get("obs_id") for p in s.get("points") or []}
        if a in ids and b in ids:
            return {"series_id": s.get("series_id"), **(s.get("comparability") or {})}
    return None


def aligned_pair(service, cache: Path, sha_a: str, sha_b: str, ra: float, dec: float, size_arcsec: float = 240.0,
                 npix: int = 96, investigation: dict | None = None) -> dict | None:
    npix = int(max(16, min(MAX_NPIX, npix)))
    imgs = [_load(service, cache, s) for s in (sha_a, sha_b)]
    if any(i is None for i in imgs):
        return None
    gra, gdec = _grid(ra, dec, size_arcsec, npix)
    samples = [_resample(i, gra, gdec) for i in imgs]
    tol = EngineConfig().temporal.wavelength_tolerance_fraction
    meta = []
    for sha, img, (val, var, cov, _disp, _mask) in zip((sha_a, sha_b), imgs, samples, strict=True):
        cx, cy = img.world_to_pixel(ra, dec)
        lam, bw, src = img.wavelength_at(float(cx), float(cy)) if img.contains(ra, dec) else (math.nan, math.nan, None)
        meta.append({"sha256": sha, "obs_id": img.obs_id, "detector": img.meta.detector, "release": img.meta.release,
                     "pipeline_version": img.meta.pipeline_version, "mjd": img.meta.mjd_avg, "psf_fwhm_arcsec": img.meta.psf_fwhm_arcsec,
                     "native_pixel_arcsec": round(img.meta.pixel_scale_arcsec, 4), "bunit": "MJy/sr", "synthetic": bool(img.meta.synthetic),
                     "contains_centre": bool(img.contains(ra, dec)),
                     "wavelength_um": None if not math.isfinite(lam) else round(lam, 5),
                     "bandwidth_um": None if not math.isfinite(bw) else round(bw, 5), "wavelength_source": src,
                     "coverage_fraction": round(float(cov.mean()), 4), "usable_fraction": round(float(np.isfinite(val).mean()), 4)})
    a, b = meta
    reasons, limitations = [], ["no PSF matching", "bilinear resampling correlates neighbouring pixels", "WCS alignment only (no source-based registration)"]
    if not (a["contains_centre"] and b["contains_centre"]):
        reasons.append("the requested position is outside at least one image")
    if a["detector"] != b["detector"]:
        reasons.append(f"different detectors ({a['detector']} vs {b['detector']}) sample different wavelengths")
    elif a["wavelength_um"] is None or b["wavelength_um"] is None:
        reasons.append("the wavelength at the position is not established for both images")
    else:
        allowed = tol * min(a["bandwidth_um"], b["bandwidth_um"])
        dl = abs(a["wavelength_um"] - b["wavelength_um"])
        if dl > allowed:
            reasons.append(f"wavelength difference {dl:.4f} um exceeds {tol} x bandwidth ({allowed:.4f} um)")
    if a["release"] != b["release"] or a["pipeline_version"] != b["pipeline_version"]:
        limitations.append(f"processing differs ({a['release']} {a['pipeline_version']} vs {b['release']} {b['pipeline_version']})")
    series = _series_record(investigation, a["obs_id"], b["obs_id"])
    if series:
        limitations += [f"series {series.get('series_id')}: {x}" for x in series.get("limitations") or []]
    status = "NOT_COMPARABLE" if reasons else "COMPARABLE_WITH_LIMITATIONS"
    lo_a, hi_a = _stretch(samples[0][3])
    lo_b, hi_b = _stretch(samples[1][3])
    lo_c, hi_c = _stretch(samples[0][3], samples[1][3])
    out = {
        "schema": PAIR_SCHEMA,
        "grid": {"ra_deg": ra, "dec_deg": dec, "size_arcsec": size_arcsec, "npix": npix, "pixel_arcsec": round(size_arcsec / npix, 4),
                 "projection": "TAN", "orientation": "north up, east left", "pixel_format": "grayscale_8_base64; 0 = no data (outside the image or non-finite)",
                 "mask_format": "uint8 base64, 1 = pixel touched by a flag the engine excludes from photometry (shown as an optional overlay)"},
        "images": [m | {"display": {"own": _u8(s[3], lo, hi), "own_stretch": [lo, hi], "common": _u8(s[3], lo_c, hi_c),
                                     "engine_mask": base64.b64encode((np.nan_to_num(s[4], nan=1.0) > 0).astype(np.uint8).tobytes()).decode("ascii"),
                                     "engine_mask_fraction": round(float(np.nanmean(np.nan_to_num(s[4], nan=1.0) > 0)), 4)}}
                   for m, s, (lo, hi) in zip(meta, samples, ((lo_a, hi_a), (lo_b, hi_b)), strict=True)],
        "common_stretch": [lo_c, hi_c],
        "comparability": {"status": status, "reasons": reasons, "limitations": limitations, "series": series,
                          "basis": "same detector and wavelength at the position within the temporal tolerance"
                                   + ("; investigation series record attached" if series else "")},
        "difference": None,
        "display_note": "8-bit display stretches; brightness changes in the viewer change display only",
    }
    if status != "NOT_COMPARABLE":
        (va, sa, *_), (vb, sb, *_) = samples
        diff = (vb - _clipped_median(vb)) - (va - _clipped_median(va))
        sigma = np.sqrt(sa + sb)
        z = np.where(sigma > 0, diff / sigma, np.nan)
        enc = np.zeros(z.shape, dtype=np.uint8)
        ok = np.isfinite(z)
        enc[ok] = np.rint(128 + 127 * np.clip(z[ok] / CLIP_SIGMA, -1, 1)).astype(np.uint8)
        enc[ok & (enc == 0)] = 1
        out["difference"] = {
            "order": "B minus A", "units": "sigma (propagated variance)", "clip_sigma": CLIP_SIGMA,
            "pixels": base64.b64encode(enc.tobytes()).decode("ascii"),
            "encoding": "0 = masked/no data; 128 = 0 sigma; 1 and 255 = -/+ clip",
            "masked_fraction": round(float(1 - ok.mean()), 4),
            "preparation": ["normal ingest and QC of both exact cached products (checksums verified)",
                            "bad-flag pixels (engine exclude flags) and non-finite pixels masked before resampling",
                            "bilinear resampling of IMAGE and VARIANCE onto the common grid",
                            "per-image 3-sigma-clipped median background subtracted", "difference divided by sqrt(var_A + var_B)"],
            "limitations": limitations + ["not a photometric measurement or a detection; use the investigation's measurements"],
        }
    return out
