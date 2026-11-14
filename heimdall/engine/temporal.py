"""Observation times, epochs, comparable series and variability.

- Times are MJD (UTC) at mid-exposure (``MJD-AVG``), ordered with the
  observation id as a deterministic tie-breaker.
- Epochs: consecutive observations closer than ``epoch_gap_days`` belong to the
  same epoch (SPHEREx revisits a field in clusters of days, about six months
  apart).
- Comparable series: SPHEREx samples a different wavelength in each image, so a
  light curve may only combine measurements from the same detector and
  release whose wavelengths agree within ``tolerance_fraction`` x bandwidth.
- Variability: chi-square of a constant-flux model with uncertainties
  sigma_i^2 + (f_sys F_i)^2, dof = n - 1. Leave-one-out repeats the test without
  the most deviant point, to expose single-point (artefact-like) excesses.

No interpolation, intermediate frame or missing epoch is ever synthesised.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np
from astropy.time import Time

from .config import TemporalParams
from .stats import Significance, chi2_significance, weighted_mean


def iso_utc(mjd: float | None) -> str | None:
    if mjd is None or not math.isfinite(mjd):
        return None
    return Time(mjd, format="mjd", scale="utc").isot + "Z"


@dataclass
class Epoch:
    index: int
    mjd_start: float
    mjd_end: float
    n_observations: int
    obs_ids: list[str]

    @property
    def mjd_mid(self) -> float:
        return 0.5 * (self.mjd_start + self.mjd_end)


def build_epochs(items: list[tuple[str, float]], gap_days: float) -> tuple[list[Epoch], dict[str, int]]:
    """Cluster (obs_id, mjd) items into epochs. Items without a finite time are skipped."""
    timed = sorted((m, o) for o, m in items if m is not None and math.isfinite(m))
    epochs: list[Epoch] = []
    index: dict[str, int] = {}
    previous = None
    for mjd, obs_id in timed:
        if previous is None or mjd - previous > gap_days:
            epochs.append(Epoch(index=len(epochs), mjd_start=mjd, mjd_end=mjd, n_observations=0, obs_ids=[]))
        e = epochs[-1]
        e.mjd_end = mjd
        e.n_observations += 1
        e.obs_ids.append(obs_id)
        index[obs_id] = e.index
        previous = mjd
    return epochs, index


def select_time_window(items: list[tuple[str, float | None]], mjd_start: float | None, mjd_end: float | None):
    """Return (selected ids, excluded [(id, reason)]) for a closed time window."""
    selected, excluded = [], []
    for obs_id, mjd in items:
        if mjd is None or not math.isfinite(mjd):
            excluded.append((obs_id, "OBSERVATION_TIME_MISSING"))
        elif mjd_start is not None and mjd < mjd_start:
            excluded.append((obs_id, "BEFORE_TIME_WINDOW"))
        elif mjd_end is not None and mjd > mjd_end:
            excluded.append((obs_id, "AFTER_TIME_WINDOW"))
        else:
            selected.append(obs_id)
    return selected, excluded


@dataclass
class SeriesPoint:
    obs_id: str
    mjd: float | None
    epoch_index: int | None
    detector: int | None
    release: str | None
    pipeline_version: str | None
    wavelength_um: float | None
    bandwidth_um: float | None
    wavelength_source: str | None
    flux_ujy: float | None
    flux_sigma_ujy: float | None
    measurement: str  # PSF_FIT (detected) | FORCED
    det_id: str | None
    quality_grade: str | None
    peak_flags: list[str]
    usable: bool
    reasons: list[str] = field(default_factory=list)

    @property
    def snr(self) -> float | None:
        if self.flux_ujy is None or not self.flux_sigma_ujy:
            return None
        return self.flux_ujy / self.flux_sigma_ujy


@dataclass
class ComparableSeries:
    series_id: str
    detector: int
    release: str
    wavelength_um: float
    wavelength_span_um: float
    bandwidth_um: float
    tolerance_um: float
    points: list[SeriesPoint]
    n_epochs: int
    pipeline_versions: list[str]


def build_series(points: list[SeriesPoint], tolerance_fraction: float, id_prefix: str) -> list[ComparableSeries]:
    usable = [p for p in points if p.usable and p.wavelength_um is not None and p.bandwidth_um
              and math.isfinite(p.wavelength_um) and p.flux_ujy is not None and p.flux_sigma_ujy and p.mjd is not None]
    groups: dict[tuple[int, str], list[SeriesPoint]] = defaultdict(list)
    for p in usable:
        groups[(p.detector or 0, p.release or "UNKNOWN")].append(p)
    out: list[ComparableSeries] = []
    for (det, rel) in sorted(groups):
        pts = sorted(groups[(det, rel)], key=lambda p: (p.wavelength_um, p.mjd, p.obs_id))
        i = 0
        while i < len(pts):
            anchor = pts[i]
            tol = tolerance_fraction * anchor.bandwidth_um
            members = [anchor]
            j = i + 1
            while j < len(pts) and pts[j].wavelength_um - anchor.wavelength_um <= tol:
                members.append(pts[j])
                j += 1
            i = j
            members.sort(key=lambda p: (p.mjd, p.obs_id))
            wl = np.array([p.wavelength_um for p in members])
            out.append(ComparableSeries(
                series_id=f"{id_prefix}:D{det}:{len(out):03d}", detector=det, release=rel,
                wavelength_um=float(wl.mean()), wavelength_span_um=float(wl.max() - wl.min()),
                bandwidth_um=float(np.median([p.bandwidth_um for p in members])), tolerance_um=tol, points=members,
                n_epochs=len({p.epoch_index for p in members if p.epoch_index is not None}),
                pipeline_versions=sorted({p.pipeline_version for p in members if p.pipeline_version})))
    return out


@dataclass
class VariabilityResult:
    series_id: str
    status: str  # COMPLETED | INSUFFICIENT_INPUTS
    reasons: list[str]
    n_points: int
    n_epochs: int
    systematic_fraction: float
    weighted_mean_ujy: float | None = None
    weighted_mean_sigma_ujy: float | None = None
    chi2: float | None = None
    dof: int | None = None
    significance: Significance | None = None
    max_abs_pull: float | None = None
    max_pull_obs_id: str | None = None
    leave_one_out: Significance | None = None
    peak_to_peak_ujy: float | None = None


def _sigmas(points: list[SeriesPoint], f_sys: float) -> np.ndarray:
    f = np.array([p.flux_ujy for p in points], dtype=float)
    s = np.array([p.flux_sigma_ujy for p in points], dtype=float)
    return np.sqrt(s ** 2 + (f_sys * f) ** 2)


def photometric_error_model(results: list[VariabilityResult], min_series: int = 10, seed: int = 20261029) -> dict:
    """Field-level flux error scale from the repeated measurements of the field's own sources.

    For each completed constant-flux test with dof >= 2, ``r = chi2 / (dof * m_dof)``, where
    ``m_k = (1 - 2/(9k))^3`` is the Wilson-Hilferty median of chi2_k / k. If the stated flux errors
    are right, the median of ``r`` is 1. ``scale = sqrt(median r)``; its standard error comes from a
    seeded bootstrap of the median. Assumption: most sources are not variable at this precision (the
    median tolerates a minority of genuine variables).
    """
    ratios = np.array([v.chi2 / (v.dof * (1.0 - 2.0 / (9.0 * v.dof)) ** 3) for v in results
                       if v.status == "COMPLETED" and v.chi2 is not None and v.dof is not None and v.dof >= 2])
    base = {"method": "sqrt(median of chi2 / (dof x null median)) over the field's constant-flux tests", "n_series": int(len(ratios)),
            "assumption": "most sources are not variable at this precision"}
    if len(ratios) < min_series:
        return {**base, "status": "INSUFFICIENT_INPUTS", "scale": None, "scale_se": None,
                "reason": f"{len(ratios)} comparable series with dof >= 2 (minimum {min_series})"}
    rng = np.random.default_rng(seed)
    boot = np.sqrt(np.median(rng.choice(ratios, size=(200, len(ratios)), replace=True), axis=1))
    return {**base, "status": "COMPLETED", "scale": float(math.sqrt(np.median(ratios))), "scale_se": float(np.std(boot, ddof=1)),
            "reason": ""}


def variability_test(series: ComparableSeries, params: TemporalParams) -> VariabilityResult:
    pts = series.points
    out = VariabilityResult(series_id=series.series_id, status="INSUFFICIENT_INPUTS", reasons=[], n_points=len(pts),
                            n_epochs=series.n_epochs, systematic_fraction=params.flux_systematic_fraction)
    if len(pts) < params.min_points_variability:
        out.reasons.append(f"FEWER_THAN_{params.min_points_variability}_COMPARABLE_POINTS")
    if series.n_epochs < params.min_epochs_variability:
        out.reasons.append(f"FEWER_THAN_{params.min_epochs_variability}_EPOCHS")
    if out.reasons:
        return out
    flux = np.array([p.flux_ujy for p in pts], dtype=float)
    sig = _sigmas(pts, params.flux_systematic_fraction)
    wm = weighted_mean(flux, sig)
    pulls = (flux - wm.mean) / sig
    k = int(np.argmax(np.abs(pulls)))
    out.status = "COMPLETED"
    out.weighted_mean_ujy, out.weighted_mean_sigma_ujy = wm.mean, wm.sigma
    out.chi2, out.dof = wm.chi2, wm.dof
    out.significance = chi2_significance(wm.chi2, wm.dof)
    out.max_abs_pull, out.max_pull_obs_id = float(abs(pulls[k])), pts[k].obs_id
    out.peak_to_peak_ujy = float(flux.max() - flux.min())
    if len(pts) - 1 >= 2:
        keep = np.arange(len(pts)) != k
        loo = weighted_mean(flux[keep], sig[keep])
        out.leave_one_out = chi2_significance(loo.chi2, loo.dof)
    return out
