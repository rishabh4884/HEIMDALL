"""Positional-change analysis.

Positions are tangent-plane offsets p_i = (xi, eta) in arcsec about a
reference direction, each with a 2x2 covariance C_i that already includes the
per-image registration floor. Times t_i are in Julian years from the
weighted reference epoch.

1. Stationary versus linear motion (generalised least squares, W_i = C_i^-1):
   stationary  p_i = p0                 chi2_s, dof 2(n-1)
   linear      p_i = p0 + mu t_i        chi2_l, dof 2n-4
   Delta chi2 = chi2_s - chi2_l is chi-square with 2 dof under the stationary
   model (nested linear Gaussian models). Requires two distinct epochs.

2. Five-parameter parallax-motion fit (distant solar-system body or star):
   p_i = p0 + mu t_i + varpi * f_i,  f_i = -(R_i . e, R_i . n)
   with R_i the observer's heliocentric position in AU, (e, n) the east/north
   unit vectors at the reference direction and varpi in arcsec
   (varpi = 206264.806 / r_AU for a body at heliocentric distance r).
   This is the linearised geometry: valid while the body's own orbital
   curvature over the baseline is negligible. Requires at least three points
   and a non-degenerate design (parallax separable from proper motion).

Both are computed only when inputs permit; otherwise a reason is returned.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .skygeom import offsets_arcsec, tangent_basis
from .stats import Significance, chi2_significance

DAYS_PER_YEAR = 365.25
ARCSEC_PER_RAD = 206264.80624709636


@dataclass
class MotionFit:
    status: str  # COMPLETED | INSUFFICIENT_INPUTS
    reasons: list[str]
    n_points: int
    n_distinct_times: int
    baseline_days: float
    ref_ra: float | None = None
    ref_dec: float | None = None
    ref_mjd: float | None = None
    chi2_stationary: float | None = None
    dof_stationary: int | None = None
    chi2_linear: float | None = None
    dof_linear: int | None = None
    motion: Significance | None = None
    gof_stationary: Significance | None = None
    gof_linear: Significance | None = None
    mu_arcsec_per_yr: list[float] | None = None  # (east, north)
    mu_cov: list[list[float]] | None = None
    mu_total_arcsec_per_yr: float | None = None
    mu_total_sigma: float | None = None
    max_residual_mahalanobis: float | None = None


def _prepare(ra, dec, cov, mjd):
    ra = np.asarray(ra, dtype=float)
    dec = np.asarray(dec, dtype=float)
    mjd = np.asarray(mjd, dtype=float)
    cov = np.asarray(cov, dtype=float).reshape(-1, 2, 2)
    vecs = np.stack([np.cos(np.radians(dec)) * np.cos(np.radians(ra)),
                     np.cos(np.radians(dec)) * np.sin(np.radians(ra)), np.sin(np.radians(dec))], axis=1)
    centre = vecs.mean(axis=0)
    centre /= np.linalg.norm(centre)
    ra0 = math.degrees(math.atan2(centre[1], centre[0])) % 360.0
    dec0 = math.degrees(math.asin(np.clip(centre[2], -1, 1)))
    pos = offsets_arcsec(ra, dec, ra0, dec0)
    weights = np.linalg.inv(cov)
    return ra0, dec0, pos, weights, mjd


def _gls(design: np.ndarray, pos: np.ndarray, weights: np.ndarray):
    """design (n, 2, k), pos (n, 2), weights (n, 2, 2) -> (theta, cov, chi2, residuals)."""
    k = design.shape[2]
    normal = np.zeros((k, k))
    rhs = np.zeros(k)
    for x_i, w_i, p_i in zip(design, weights, pos):
        normal += x_i.T @ w_i @ x_i
        rhs += x_i.T @ w_i @ p_i
    cov = np.linalg.inv(normal)
    theta = cov @ rhs
    resid = pos - np.einsum("nij,j->ni", design, theta)
    chi2 = float(np.einsum("ni,nij,nj->", resid, weights, resid))
    return theta, cov, chi2, resid, normal


def motion_test(ra, dec, cov_arcsec2, mjd, *, min_points: int = 2) -> MotionFit:
    """Stationary versus linear-motion test for one source's positions."""
    n = len(ra)
    mjd_arr = np.asarray(mjd, dtype=float)
    distinct = int(np.unique(np.round(mjd_arr, 6)).size) if n else 0
    baseline = float(mjd_arr.max() - mjd_arr.min()) if n else 0.0
    out = MotionFit(status="INSUFFICIENT_INPUTS", reasons=[], n_points=n, n_distinct_times=distinct, baseline_days=baseline)
    if n < max(2, min_points):
        out.reasons.append(f"FEWER_THAN_{max(2, min_points)}_POSITIONS")
    if distinct < 2:
        out.reasons.append("NO_TIME_BASELINE")
    if out.reasons:
        return out
    ra0, dec0, pos, weights, mjd_arr = _prepare(ra, dec, cov_arcsec2, mjd)
    w_trace = np.array([np.trace(w) for w in weights])
    t_ref = float(np.sum(w_trace * mjd_arr) / np.sum(w_trace))
    t = (mjd_arr - t_ref) / DAYS_PER_YEAR
    eye = np.eye(2)
    d_stat = np.repeat(eye[None, :, :], n, axis=0)
    d_lin = np.concatenate([d_stat, t[:, None, None] * eye[None, :, :]], axis=2)
    _, _, chi2_s, _, _ = _gls(d_stat, pos, weights)
    theta, cov_l, chi2_l, resid_l, _ = _gls(d_lin, pos, weights)
    delta = max(chi2_s - chi2_l, 0.0)
    mu = theta[2:4]
    mu_cov = cov_l[2:4, 2:4]
    mu_tot = float(math.hypot(mu[0], mu[1]))
    if mu_tot > 0:
        g = mu / mu_tot
        mu_sig = float(math.sqrt(max(g @ mu_cov @ g, 0.0)))
    else:
        mu_sig = float(math.sqrt(max(np.trace(mu_cov) / 2, 0.0)))
    md = [float(math.sqrt(max(r @ w @ r, 0.0))) for r, w in zip(resid_l, weights)]
    dof_l = 2 * n - 4
    out.status = "COMPLETED"
    out.ref_ra, out.ref_dec, out.ref_mjd = ra0, dec0, t_ref
    out.chi2_stationary, out.dof_stationary = chi2_s, 2 * (n - 1)
    out.chi2_linear, out.dof_linear = chi2_l, dof_l
    out.motion = chi2_significance(delta, 2)
    out.gof_stationary = chi2_significance(chi2_s, 2 * (n - 1))
    out.gof_linear = chi2_significance(chi2_l, dof_l) if dof_l > 0 else None
    out.mu_arcsec_per_yr = [float(mu[0]), float(mu[1])]
    out.mu_cov = mu_cov.tolist()
    out.mu_total_arcsec_per_yr = mu_tot
    out.mu_total_sigma = mu_sig
    out.max_residual_mahalanobis = max(md)
    return out


@dataclass
class AstrometricErrorModel:
    """Empirical check of the positional error model on the field's own multi-image sources.

    For every source with at least ``min_detections`` astrometric detections,
    residuals about its weighted mean position give chi2 = sum r^T (C + s^2 I)^-1 r
    with dof = 2(n - 1). The error scale is sqrt(sum chi2 / sum dof) (1 when the
    assumed errors are right; > 1 when they are underestimated), with standard error
    scale / sqrt(2 dof). Sources more than 5x the median per-source scale are
    excluded as gross outliers (blends, movers) and counted. Per-image mean residuals
    estimate registration offsets. Genuinely moving sources inflate the scale, so
    the result is conservative for motion searches.
    """

    status: str
    reasons: list[str]
    floor_arcsec: float
    n_sources: int = 0
    n_detections: int = 0
    dof: int = 0
    scale: float | None = None
    scale_se: float | None = None
    median_source_scale: float | None = None
    n_outlier_sources: int = 0
    residual_rms_arcsec: list[float] | None = None
    image_offsets: list[dict] = field(default_factory=list)
    image_offset_rms_arcsec: list[float] | None = None


def astrometric_error_model(groups, floor_arcsec: float, *, min_detections: int = 6, min_sources: int = 5) -> AstrometricErrorModel:
    """``groups``: one list of detections (with ra, dec, cov(), obs_id) per source."""
    floor2 = floor_arcsec ** 2
    per_source = []
    for dets in groups:
        if len(dets) < min_detections:
            continue
        pos = offsets_arcsec([d.ra for d in dets], [d.dec for d in dets], dets[0].ra, dets[0].dec)
        weights = [np.linalg.inv(d.cov() + floor2 * np.eye(2)) for d in dets]
        mean = np.linalg.solve(sum(weights), sum(w @ p for w, p in zip(weights, pos)))
        res = pos - mean
        chi2 = float(sum(r @ w @ r for r, w in zip(res, weights)))
        per_source.append((dets, res, chi2, 2 * (len(dets) - 1)))
    out = AstrometricErrorModel(status="INSUFFICIENT_INPUTS", reasons=[], floor_arcsec=floor_arcsec)
    if len(per_source) < min_sources:
        out.reasons.append(f"FEWER_THAN_{min_sources}_SOURCES_WITH_{min_detections}_ASTROMETRIC_DETECTIONS")
        return out
    scales = np.array([math.sqrt(c / k) for _, _, c, k in per_source])
    median = float(np.median(scales))
    kept = [s for s, sc in zip(per_source, scales) if sc <= 5.0 * median]
    chi2_tot = sum(c for _, _, c, _ in kept)
    dof_tot = sum(k for _, _, _, k in kept)
    scale = math.sqrt(chi2_tot / dof_tot)
    residuals = np.concatenate([r for _, r, _, _ in kept])
    by_image: dict[str, list] = {}
    for dets, res, _, _ in kept:
        for d, r in zip(dets, res):
            by_image.setdefault(d.obs_id, []).append(r)
    offsets = []
    for obs_id in sorted(by_image):
        a = np.array(by_image[obs_id])
        if len(a) < 3:
            continue
        m = a.mean(axis=0)
        se = a.std(axis=0, ddof=1) / math.sqrt(len(a))
        chi2 = float(np.sum((m / np.where(se > 0, se, np.inf)) ** 2))
        offsets.append({"obs_id": obs_id, "n": int(len(a)), "mean_arcsec": m.tolist(), "se_arcsec": se.tolist(),
                        "significance": chi2_significance(chi2, 2)})
    out.status = "COMPLETED"
    out.n_sources = len(kept)
    out.n_detections = int(sum(len(d) for d, _, _, _ in kept))
    out.dof = int(dof_tot)
    out.scale = scale
    out.scale_se = scale / math.sqrt(2.0 * dof_tot)
    out.median_source_scale = median
    out.n_outlier_sources = len(per_source) - len(kept)
    out.residual_rms_arcsec = residuals.std(axis=0).tolist()
    out.image_offsets = offsets
    if offsets:
        means = np.array([o["mean_arcsec"] for o in offsets])
        out.image_offset_rms_arcsec = np.sqrt((means ** 2).mean(axis=0)).tolist()
    return out


@dataclass
class ParallaxFit:
    status: str  # COMPLETED | INSUFFICIENT_INPUTS
    reasons: list[str]
    n_points: int
    ref_ra: float | None = None
    ref_dec: float | None = None
    ref_mjd: float | None = None
    parallax_arcsec: float | None = None
    parallax_sigma_arcsec: float | None = None
    parallax_snr: float | None = None
    mu_arcsec_per_yr: list[float] | None = None
    cov: list[list[float]] | None = None
    chi2: float | None = None
    dof: int | None = None
    gof: Significance | None = None
    condition_number: float | None = None
    distance_au: float | None = None
    distance_interval_au: list[float | None] | None = None  # from parallax +/- 2 sigma; None = unbounded
    notes: list[str] = field(default_factory=list)


def parallax_fit(ra, dec, cov_arcsec2, mjd, observer_au, *, max_condition: float = 1e10) -> ParallaxFit:
    """Five-parameter parallax-motion fit (see module docstring)."""
    n = len(ra)
    out = ParallaxFit(status="INSUFFICIENT_INPUTS", reasons=[], n_points=n)
    if n < 3:
        out.reasons.append("FEWER_THAN_3_POSITIONS")
        return out
    ra0, dec0, pos, weights, mjd_arr = _prepare(ra, dec, cov_arcsec2, mjd)
    _, east, north = tangent_basis(ra0, dec0)
    obs = np.asarray(observer_au, dtype=float).reshape(-1, 3)
    f = np.stack([-(obs @ east), -(obs @ north)], axis=1)
    w_trace = np.array([np.trace(w) for w in weights])
    t_ref = float(np.sum(w_trace * mjd_arr) / np.sum(w_trace))
    t = (mjd_arr - t_ref) / DAYS_PER_YEAR
    eye = np.eye(2)
    design = np.concatenate([np.repeat(eye[None], n, axis=0), t[:, None, None] * eye[None], f[:, :, None]], axis=2)
    normal = np.zeros((5, 5))
    for x_i, w_i in zip(design, weights):
        normal += x_i.T @ w_i @ x_i
    scale = np.sqrt(np.diag(normal))
    if np.any(scale <= 0):
        out.reasons.append("DEGENERATE_DESIGN")
        return out
    cond = float(np.linalg.cond(normal / np.outer(scale, scale)))
    out.condition_number = cond
    if not np.isfinite(cond) or cond > max_condition:
        out.reasons.append("PARALLAX_NOT_SEPARABLE_FROM_PROPER_MOTION")
        return out
    theta, cov, chi2, _, _ = _gls(design, pos, weights)
    plx, plx_sig = float(theta[4]), float(math.sqrt(max(cov[4, 4], 0.0)))
    dof = 2 * n - 5
    out.status = "COMPLETED"
    out.ref_ra, out.ref_dec, out.ref_mjd = ra0, dec0, t_ref
    out.parallax_arcsec, out.parallax_sigma_arcsec = plx, plx_sig
    out.parallax_snr = plx / plx_sig if plx_sig > 0 else None
    out.mu_arcsec_per_yr = [float(theta[2]), float(theta[3])]
    out.cov = cov.tolist()
    out.chi2, out.dof = chi2, dof
    out.gof = chi2_significance(chi2, dof) if dof > 0 else None
    if plx > 0 and plx_sig > 0 and plx / plx_sig >= 3.0:
        out.distance_au = ARCSEC_PER_RAD / plx
    hi_plx, lo_plx = plx + 2 * plx_sig, plx - 2 * plx_sig
    out.distance_interval_au = [ARCSEC_PER_RAD / hi_plx if hi_plx > 0 else None,
                                ARCSEC_PER_RAD / lo_plx if lo_plx > 0 else None]
    out.notes.append("linearised geometry; orbital curvature over the baseline neglected")
    if out.distance_interval_au[0] is None:
        out.notes.append("parallax + 2 sigma is not positive: no distance constraint")
    return out
