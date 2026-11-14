"""Spectrum assembly and comparison.

Each SPHEREx image samples one wavelength (and bandwidth) at a given sky
position, so a source spectrum is the set of its flux measurements across
images, ordered by wavelength. Outputs keep every input measurement and its
status; nothing is resampled or interpolated.

Wavelength fidelity: ``WCS-WAVE`` is an approximation (about 1 nm, IRSA) meant
for visualisation; per-pixel ``CWAVE``/``CBAND`` from the spectral WCS
calibration product are the documented science values. Every point records
which one was used.

Coverage states per detector band:
- ``MEASURED``: at least one usable measurement;
- ``COVERED_NOT_USABLE``: observed, but every measurement was excluded;
- ``NOT_IN_SELECTION``: no selected observation covers the band.
None of these is a measured absence of a spectral feature.

Descriptive quantities only: per-band weighted means and a power-law slope
(log F against log lambda). No physical property (temperature, composition)
is inferred: that needs validated templates or models, not available here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from ..ingest.spherex import DETECTOR_BANDS_UM
from .stats import Significance, chi2_significance, weighted_mean
from .temporal import SeriesPoint


@dataclass
class Spectrum:
    source_id: str
    points: list[SeriesPoint]
    coverage: list[dict]
    band_means: list[dict]
    slope: dict | None
    wavelength_sources: list[str]
    notes: list[str] = field(default_factory=list)


def assemble_spectrum(source_id: str, points: list[SeriesPoint], min_snr_slope: float = 3.0) -> Spectrum:
    pts = sorted(points, key=lambda p: (p.wavelength_um if p.wavelength_um is not None else math.inf, p.mjd or 0.0, p.obs_id))
    coverage = []
    band_means = []
    for det, (lo, hi) in DETECTOR_BANDS_UM.items():
        in_band = [p for p in pts if p.detector == det]
        usable = [p for p in in_band if p.usable and p.flux_ujy is not None and p.flux_sigma_ujy]
        state = "MEASURED" if usable else ("COVERED_NOT_USABLE" if in_band else "NOT_IN_SELECTION")
        wl = [p.wavelength_um for p in usable if p.wavelength_um is not None and math.isfinite(p.wavelength_um)]
        coverage.append({"detector": det, "nominal_range_um": [lo, hi], "state": state, "n_measurements": len(in_band),
                         "n_usable": len(usable), "wavelength_min_um": min(wl) if wl else None,
                         "wavelength_max_um": max(wl) if wl else None})
        if len(usable) >= 1:
            wm = weighted_mean(np.array([p.flux_ujy for p in usable]), np.array([p.flux_sigma_ujy for p in usable]))
            band_means.append({"detector": det, "n_points": len(usable), "weighted_mean_ujy": wm.mean,
                               "sigma_ujy": wm.sigma, "chi2": wm.chi2, "dof": wm.dof, "method_id": "band_weighted_mean",
                               "note": "chi2 includes genuine spectral structure within the band; it is not a variability test"})
    good = [p for p in pts if p.usable and p.flux_ujy is not None and p.flux_sigma_ujy and p.flux_ujy > 0
            and p.wavelength_um and p.flux_ujy / p.flux_sigma_ujy >= min_snr_slope]
    slope = None
    if len(good) >= 3 and len({round(p.wavelength_um, 4) for p in good}) >= 2:
        x = np.log10([p.wavelength_um for p in good])
        y = np.log10([p.flux_ujy for p in good])
        s = np.array([p.flux_sigma_ujy / (p.flux_ujy * math.log(10)) for p in good])
        w = 1.0 / s ** 2
        a = np.vstack([np.ones_like(x), x]).T
        cov = np.linalg.inv((a.T * w) @ a)
        beta = cov @ ((a.T * w) @ y)
        chi2 = float(np.sum(w * (y - a @ beta) ** 2))
        dof = len(good) - 2
        slope = {"slope": float(beta[1]), "slope_sigma": float(math.sqrt(cov[1, 1])), "intercept": float(beta[0]),
                 "chi2": chi2, "dof": dof, "gof": chi2_significance(chi2, dof) if dof > 0 else None,
                 "n_points": len(good), "method_id": "spectral_slope_fit",
                 "meaning": "d log10(F_nu) / d log10(lambda); descriptive, not a physical model"}
    sources = sorted({p.wavelength_source for p in pts if p.wavelength_source})
    notes = []
    if "WCS-WAVE" in sources:
        notes.append("WCS-WAVE wavelengths are approximate (about 1 nm); CWAVE/CBAND calibration gives science values")
    return Spectrum(source_id=source_id, points=pts, coverage=coverage, band_means=band_means, slope=slope,
                    wavelength_sources=sources, notes=notes)


@dataclass
class SpectralComparison:
    label: str
    status: str  # COMPLETED | NOT_ASSESSABLE | INCOMPATIBLE
    reasons: list[str]
    n_pairs: int
    pairs: list[dict]
    significance: Significance | None
    unmatched_a: int
    unmatched_b: int


def compare_point_sets(label: str, a: list[SeriesPoint], b: list[SeriesPoint], tolerance_fraction: float) -> SpectralComparison:
    """Compare two sets of measurements at matched wavelengths (same detector, |dlambda| <= tol x bandwidth)."""
    ua = [p for p in a if p.usable and p.wavelength_um and p.bandwidth_um and p.flux_sigma_ujy]
    ub = [p for p in b if p.usable and p.wavelength_um and p.bandwidth_um and p.flux_sigma_ujy]
    releases = {p.release for p in ua} | {p.release for p in ub}
    if len(releases) > 1:
        return SpectralComparison(label, "INCOMPATIBLE", [f"RELEASE_MISMATCH:{sorted(map(str, releases))}"], 0, [], None, len(ua), len(ub))
    candidates = []
    for i, p in enumerate(ua):
        for j, q in enumerate(ub):
            if p.detector != q.detector:
                continue
            dl = abs(p.wavelength_um - q.wavelength_um)
            if dl <= tolerance_fraction * min(p.bandwidth_um, q.bandwidth_um):
                candidates.append((dl, i, j))
    used_a, used_b, pairs = set(), set(), []
    for dl, i, j in sorted(candidates):
        if i in used_a or j in used_b:
            continue
        used_a.add(i)
        used_b.add(j)
        p, q = ua[i], ub[j]
        z = (p.flux_ujy - q.flux_ujy) / math.hypot(p.flux_sigma_ujy, q.flux_sigma_ujy)
        pairs.append({"obs_a": p.obs_id, "obs_b": q.obs_id, "detector": p.detector, "wavelength_a_um": p.wavelength_um,
                      "wavelength_b_um": q.wavelength_um, "flux_a_ujy": p.flux_ujy, "flux_b_ujy": q.flux_ujy, "z": z})
    if not pairs:
        return SpectralComparison(label, "NOT_ASSESSABLE", ["NO_WAVELENGTH_OVERLAP"], 0, [], None, len(ua), len(ub))
    chi2 = float(sum(p["z"] ** 2 for p in pairs))
    return SpectralComparison(label, "COMPLETED", [], len(pairs), pairs, chi2_significance(chi2, len(pairs)),
                              len(ua) - len(pairs), len(ub) - len(pairs))
