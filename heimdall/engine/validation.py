"""Executable validation procedures and the validation registry.

Verification levels (what a run can establish):
- ``MATHEMATICAL``: independent numerical checks of the engine's formulas.
- ``SOFTWARE``: reproducibility of identical computations.
- ``SYNTHETIC_INJECTION`` / ``REAL_DATA_INJECTION``: behaviour for PSF-model
  sources injected into synthetic or real images (real noise, masks, PSF model
  assumed correct; injected sources carry no photon noise).
- ``REAL_DATA_EMPIRICAL``: statistics of real data under a stated null
  (inverted images, empty positions, sources assumed stationary).

A significance scope cell is (kind | data origin | release | config hash).
Validity states for a cell:
- ``VALIDATED_FOR_CONFIG``: the cell's null procedure ran with at least
  ``min_null_trials`` trials and met its frozen criteria (KS p-value uniformity
  ``>= ks_min_p`` and no significant excess of false positives at the watch
  threshold, binomial p ``>= 0.01``).
- ``MISCALIBRATED``: enough trials, criteria failed.
- ``UNVALIDATED``: no run (``NO_CONTROL_RUN``), too few trials
  (``INSUFFICIENT_SAMPLE``) or controls that do not represent the candidates
  (``CONTROLS_NOT_REPRESENTATIVE``).
Synthetic runs only ever validate synthetic cells. Software tests never
validate a scientific cell.
"""

from __future__ import annotations

import collections
import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from scipy import stats as _st

from ..contracts import Validity, ValidityReason, ValidityState
from .association import SourceCluster, gate_d2
from .config import EngineConfig
from .detection import (DetectionRecord, DetectionRun, ImageAnalysis, add_point_sources, detect, fit_psf, forced_photometry,
                        prepare)
from .motion import motion_test
from .skygeom import angular_separation_deg, offsets_arcsec
from .stats import binomial_excess_pvalue, chi2_significance, ks_uniform, poisson_interval, pull_summary, weighted_mean, wilson_interval
from .temporal import build_epochs

# 1.1.0: precision-aware noise-model calibration; injection SNR bins shared across images.
# 1.2.0: moving-object injection-recovery, PSF-mismatch sensitivity, Gaia quasi-stationary motion controls.
# 1.3.0: moving-object injection uses explicit truth attribution, pure/mixed/ambiguous/no-body tracks and a duplicate policy.
VALIDATION_VERSION = "validation-1.3.0"
CELL_KINDS = {"VARIABILITY": "VARIABILITY_SIGNIFICANCE", "MOTION": "MOTION_SIGNIFICANCE",
              "TRANSIENT": "TRANSIENT_SIGNIFICANCE", "TRACK": "MOVING_OBJECT_LINKING"}


@dataclass
class ValidationRun:
    run_id: str
    procedure: str
    procedure_version: str
    verification_level: str
    cell_kind: str | None
    scope: dict
    scope_cell: str | None
    created_at: str
    sample_size: int
    metrics: dict
    criteria: dict
    outcome: str  # VALIDATED_FOR_CONFIG | MISCALIBRATED | UNVALIDATED | PASSED | FAILED | INFORMATIONAL | NOT_ASSESSABLE
    reasons: list[str]
    establishes: str
    does_not_establish: str


def scope_cell(cell_kind: str, origin: str, release: str, config_hash: str) -> str:
    return f"{cell_kind}|{origin}|{release}|{config_hash}"


def _run(procedure: str, level: str, cell_kind: str | None, scope: dict, sample: int, metrics: dict, criteria: dict,
         outcome: str, reasons: list[str], establishes: str, not_establish: str) -> ValidationRun:
    created = datetime.now(UTC).isoformat()
    cell = scope_cell(cell_kind, scope["origin"], scope["release"], scope["config_hash"]) if cell_kind else None
    digest = hashlib.sha256(json.dumps([procedure, cell, created, sample], default=str).encode()).hexdigest()[:12]
    return ValidationRun(run_id=f"V-{procedure}-{digest}", procedure=procedure, procedure_version=VALIDATION_VERSION,
                         verification_level=level, cell_kind=cell_kind, scope=scope, scope_cell=cell, created_at=created,
                         sample_size=sample, metrics=metrics, criteria=criteria, outcome=outcome, reasons=reasons,
                         establishes=establishes, does_not_establish=not_establish)


def _null_outcome(pvalues: list[float], config: EngineConfig, extra_reason: ValidityReason | None = None):
    v = config.validation
    alpha = 2.0 * float(_st.norm.sf(config.significance.watch_sigma))
    p = np.asarray(pvalues, dtype=float)
    n = int(p.size)
    n_fp = int(np.sum(p < alpha))
    ks_d, ks_p = ks_uniform(p) if n else (math.nan, math.nan)
    binom_p = binomial_excess_pvalue(n_fp, n, alpha) if n else math.nan
    metrics = {"n_trials": n, "alpha_watch": alpha, "false_positives_at_watch": n_fp,
               "false_positive_rate": n_fp / n if n else None, "fpr_interval_95": list(wilson_interval(n_fp, n)) if n else None,
               "ks_statistic": ks_d, "ks_pvalue": ks_p, "binomial_excess_pvalue": binom_p}
    criteria = {"min_trials": v.min_null_trials, "ks_min_p": v.ks_min_p, "binomial_min_p": 0.01}
    if n < v.min_null_trials:
        return metrics, criteria, "UNVALIDATED", [ValidityReason.INSUFFICIENT_SAMPLE.value]
    if extra_reason is not None:
        return metrics, criteria, "UNVALIDATED", [extra_reason.value]
    if ks_p >= v.ks_min_p and binom_p >= 0.01:
        return metrics, criteria, "VALIDATED_FOR_CONFIG", []
    return metrics, criteria, "MISCALIBRATED", [ValidityReason.CRITERIA_FAILED.value]


def _rng(config: EngineConfig, label: str) -> np.random.Generator:
    seed = int(hashlib.sha256(f"{config.validation.seed}:{label}".encode()).hexdigest()[:16], 16)
    return np.random.default_rng(seed)


def _empty_positions(an: ImageAnalysis, avoid: list[tuple[float, float]], rng: np.random.Generator, n: int,
                     min_sep_px: float, margin_px: int) -> list[tuple[float, float]]:
    x0, x1, y0, y1 = an.image.window
    if x1 - x0 <= 2 * margin_px or y1 - y0 <= 2 * margin_px:
        return []
    chosen: list[tuple[float, float]] = []
    for _ in range(n * 30):
        if len(chosen) >= n:
            break
        x = float(rng.uniform(x0 + margin_px, x1 - 1 - margin_px))
        y = float(rng.uniform(y0 + margin_px, y1 - 1 - margin_px))
        ix, iy = int(round(x)) - an.image.origin[0], int(round(y)) - an.image.origin[1]
        if an.bad[iy, ix]:
            continue
        if any(math.hypot(x - ax, y - ay) < min_sep_px for ax, ay in avoid + chosen):
            continue
        chosen.append((x, y))
    return chosen


def injection_recovery(analyses: dict[str, ImageAnalysis], runs: dict[str, DetectionRun], config: EngineConfig,
                       scope: dict, rounds: int = 6) -> ValidationRun:
    """Inject PSF-model point sources at a grid of SNRs, re-run detection, measure completeness and calibration."""
    v = config.validation
    p = config.detection
    grid = list(v.injection_snr_grid)
    results = []
    flux_sigmas = []
    grid_index = 0  # shared across images, so small images that fit few injections still cover every SNR bin
    for obs_id in sorted(analyses):
        an = analyses[obs_id]
        if an.image.psf is None or obs_id not in runs:
            continue
        rng = _rng(config, f"inject:{obs_id}")
        originals = [(d.x, d.y) for d in runs[obs_id].accepted + runs[obs_id].rejected]
        k = 0
        for _ in range(rounds):
            remaining = v.injections_per_image - k
            if remaining <= 0:
                break
            pos = _empty_positions(an, originals, rng, remaining, float(p.psf_stamp_px), p.edge_margin_px + p.psf_stamp_px // 2)
            if not pos:
                break
            sources, truth = [], []
            for x, y in pos:
                fit = fit_psf(an, x, y, p, fixed=True)
                if fit.status == "FAILED" or fit.sum_wp2 <= 0:
                    continue
                sigma_a = math.sqrt(1.0 / fit.sum_wp2)
                snr = grid[grid_index % len(grid)]
                grid_index += 1
                k += 1
                sources.append((x, y, snr * sigma_a))
                truth.append((x, y, snr, snr * sigma_a))
                omega, _ = an.image.pixel_solid_angle_sr(x, y)
                flux_sigmas.append(sigma_a * omega * 1e12)
            if not sources:
                continue
            new_an = prepare(an.image.with_image(add_point_sources(an.image, sources)), config)
            new_run = detect(new_an, config)
            for x, y, snr, amp in truth:
                best = min(new_run.accepted, key=lambda d: math.hypot(d.x - x, d.y - y), default=None)
                ok = best is not None and math.hypot(best.x - x, best.y - y) <= v.match_radius_px
                row = {"obs_id": obs_id, "snr_true": snr, "recovered": bool(ok)}
                if ok:
                    ra_t, dec_t = an.image.pixel_to_world(x, y)
                    delta = offsets_arcsec(best.ra, best.dec, float(ra_t), float(dec_t))
                    row["flux_pull"] = (best.amplitude - amp) / best.amplitude_sigma if best.amplitude_sigma > 0 else math.nan
                    row["position_d2"] = float(delta @ np.linalg.solve(best.cov(), delta)) if best.astrometry_usable else math.nan
                results.append(row)
            originals = originals + [(x, y) for x, y, _, _ in truth]
    by_snr = []
    for s in grid:
        rows = [r for r in results if r["snr_true"] == s]
        n_rec = sum(r["recovered"] for r in rows)
        lo, hi = wilson_interval(n_rec, len(rows)) if rows else (None, None)
        by_snr.append({"snr": s, "injected": len(rows), "recovered": n_rec,
                       "completeness": n_rec / len(rows) if rows else None, "interval_95": [lo, hi]})
    limit = None
    pts = [(b["snr"], b["completeness"]) for b in by_snr if b["completeness"] is not None]
    for (s1, c1), (s2, c2) in zip(pts, pts[1:]):
        if c1 < v.completeness_target <= c2:
            limit = s1 + (v.completeness_target - c1) * (s2 - s1) / (c2 - c1)
            break
    if limit is None and pts and pts[0][1] >= v.completeness_target:
        limit = pts[0][0]
    pulls = [r["flux_pull"] for r in results if r.get("recovered") and r["snr_true"] >= 5]
    d2 = [r["position_d2"] for r in results if r.get("recovered") and r["snr_true"] >= 5 and math.isfinite(r.get("position_d2", math.nan))]
    ps = pull_summary(np.array(pulls))
    # Median-based: robust to the non-Gaussian tail of low-SNR centroids (median of chi2(2) is 2 ln 2).
    pos_ratio = math.sqrt(float(np.median(d2)) / (2.0 * math.log(2.0))) if d2 else None
    pos_ratio_mean = math.sqrt(float(np.mean(d2)) / 2.0) if d2 else None
    frac68 = float(np.mean(np.array(d2) <= 2.2958)) if d2 else None
    median_sigma = float(np.median(flux_sigmas)) if flux_sigmas else None
    metrics = {"n_injected": len(results), "completeness_by_snr": by_snr,
               f"snr_at_{int(v.completeness_target * 100)}pct_completeness": limit,
               "median_flux_sigma_ujy": median_sigma,
               "flux_limit_ujy_at_target": limit * median_sigma if limit and median_sigma else None,
               "flux_pull": {"n": ps.n, "mean": ps.mean, "std": ps.std},
               "position_calibration_ratio": pos_ratio, "position_calibration_ratio_from_mean": pos_ratio_mean,
               "position_fraction_within_68pct_region": frac68}
    criteria = {"min_recovered_snr_ge_5": v.min_null_trials, "pull_std_range": [config.quality.noise_ratio_low, config.quality.noise_ratio_high],
                "position_ratio_range": [config.quality.noise_ratio_low, config.quality.noise_ratio_high]}
    lo, hi = config.quality.noise_ratio_low, config.quality.noise_ratio_high
    if ps.n < v.min_null_trials or not d2:
        outcome, reasons = "UNVALIDATED", [ValidityReason.INSUFFICIENT_SAMPLE.value]
    elif lo <= ps.std <= hi and pos_ratio is not None and lo <= pos_ratio <= hi:
        outcome, reasons = "VALIDATED_FOR_CONFIG", []
    else:
        outcome, reasons = "MISCALIBRATED", [ValidityReason.CRITERIA_FAILED.value]
    level = "SYNTHETIC_INJECTION" if scope["origin"] == "SYNTHETIC" else "REAL_DATA_INJECTION"
    return _run("injection_recovery", level, "DETECTION_PHOTOMETRY", scope, len(results), metrics, criteria, outcome, reasons,
                "completeness, flux and position uncertainty calibration for injected PSF-model point sources in these images",
                "performance for sources the PSF model does not describe, photon noise of bright sources, moving-object linking")


def null_detection(analyses: dict[str, ImageAnalysis], config: EngineConfig, scope: dict,
                   thresholds: tuple[float, ...] = (4.0,)) -> ValidationRun:
    """False detections in sign-inverted images (no real source is negative).

    Criterion (frozen in the configuration): at the operating threshold, the
    false-detection rate per image must be at most ``max_false_detections_per_image``.
    VALIDATED_FOR_CONFIG when the 95% Poisson upper bound is below it,
    MISCALIBRATED when the lower bound is above it, otherwise INSUFFICIENT_SAMPLE.
    Lower thresholds and the independent-footprint approximation are reported
    for information only (the approximation is unreliable at low thresholds).
    """
    op = config.detection.threshold_sigma
    tol = config.validation.max_false_detections_per_image
    n_img = len(analyses)
    rows = []
    for t in sorted(set(thresholds) | {op}):
        observed, expected = 0, 0.0
        for obs_id in sorted(analyses):
            run = detect(analyses[obs_id], config, threshold_sigma=t, invert=True)
            observed += len(run.accepted)
            expected += run.nominal_false_positives or 0.0
        lo, hi = poisson_interval(observed)
        rows.append({"threshold_sigma": t, "observed": observed, "per_image": observed / n_img if n_img else None,
                     "per_image_interval_95": [lo / n_img, hi / n_img] if n_img else None,
                     "nominal_independent_footprint_approximation": expected, "operating": t == op})
    at_op = next(r for r in rows if r["operating"])
    lo_rate, hi_rate = at_op["per_image_interval_95"] if n_img else (math.nan, math.nan)
    if n_img and lo_rate > tol:
        outcome, reasons = "MISCALIBRATED", [ValidityReason.CRITERIA_FAILED.value]
    elif n_img and hi_rate <= tol:
        outcome, reasons = "VALIDATED_FOR_CONFIG", []
    else:
        outcome, reasons = "UNVALIDATED", [ValidityReason.INSUFFICIENT_SAMPLE.value]
    level = "SYNTHETIC_INJECTION" if scope["origin"] == "SYNTHETIC" else "REAL_DATA_EMPIRICAL"
    return _run("null_detection_inverted", level, "DETECTION_FALSE_POSITIVE", scope, n_img,
                {"by_threshold": rows, "n_images": n_img, "operating_threshold_sigma": op},
                {"max_false_detections_per_image": tol, "decision": "95% Poisson interval of the per-image rate"},
                outcome, reasons,
                "the false-detection rate per image at the operating threshold for noise-like (sign-symmetric) fluctuations",
                "false positives from positive-only artefacts (cosmic rays, ghosts), which inverted images cannot reveal")


def null_variability(analyses: dict[str, ImageAnalysis], runs: dict[str, DetectionRun], clusters: list[SourceCluster],
                     image_info: dict[str, dict], config: EngineConfig, scope: dict, max_positions: int = 400) -> ValidationRun:
    """Constant sources injected at empty positions, measured by forced photometry in every covering image."""
    p = config.detection
    t = config.temporal
    rng = _rng(config, "null_variability")
    scale = max(an.image.meta.pixel_scale_arcsec for an in analyses.values()) if analyses else 0.0
    avoid_arcsec = (p.psf_stamp_px / 2.0 + 2.0) * scale   # away from detected sources (neighbours are also subtracted)
    spacing_arcsec = p.psf_stamp_px * scale                # trials do not share fit stamps
    positions: list[tuple[float, float]] = []
    for obs_id in sorted(analyses):
        an = analyses[obs_id]
        avoid = [(d.x, d.y) for d in runs[obs_id].accepted] if obs_id in runs else []
        for x, y in _empty_positions(an, avoid, rng, 200, p.psf_stamp_px / 2.0 + 2.0, p.edge_margin_px + p.psf_stamp_px // 2):
            ra, dec = (float(v) for v in an.image.pixel_to_world(x, y))
            if any(angular_separation_deg(ra, dec, c.ra, c.dec) * 3600 < avoid_arcsec for c in clusters):
                continue
            if any(angular_separation_deg(ra, dec, a, b) * 3600 < spacing_arcsec for a, b in positions):
                continue
            positions.append((ra, dec))
            if len(positions) >= max_positions:
                break
        if len(positions) >= max_positions:
            break
    sigmas = []
    for ra, dec in positions[:50]:
        for obs_id in sorted(analyses):
            f = forced_photometry(analyses[obs_id], ra, dec, config, runs[obs_id].accepted if obs_id in runs else None)
            if f.status == "MEASURED":
                sigmas.append(f.flux_sigma_ujy)
    flux = 20.0 * float(np.median(sigmas)) if sigmas else 0.0
    modified: dict[str, ImageAnalysis] = {}
    for obs_id, an in analyses.items():
        srcs = []
        for ra, dec in positions:
            if an.image.contains(ra, dec):
                x, y = (float(v) for v in an.image.world_to_pixel(ra, dec))
                omega, _ = an.image.pixel_solid_angle_sr(x, y)
                srcs.append((x, y, flux / (omega * 1e12)))
        modified[obs_id] = prepare(an.image.with_image(add_point_sources(an.image, srcs)), config) if srcs else an
    pv_src, pv_empty = [], []
    for variant, store, mapping in (("source", pv_src, modified), ("empty", pv_empty, analyses)):
        for ra, dec in positions:
            groups: dict[tuple, list] = {}
            for obs_id in sorted(mapping):
                f = forced_photometry(mapping[obs_id], ra, dec, config, runs[obs_id].accepted if obs_id in runs else None)
                if f.status != "MEASURED":
                    continue
                info = image_info.get(obs_id, {})
                groups.setdefault((f.detector, info.get("release")), []).append((f, info.get("epoch_index")))
            for items in groups.values():
                epochs = {e for _, e in items if e is not None}
                if len(items) < t.min_points_variability or len(epochs) < t.min_epochs_variability:
                    continue
                fl = np.array([f.flux_ujy for f, _ in items])
                sg = np.array([f.flux_sigma_ujy for f, _ in items])
                sg = np.sqrt(sg ** 2 + (t.flux_systematic_fraction * fl) ** 2)
                wm = weighted_mean(fl, sg)
                store.append(chi2_significance(wm.chi2, wm.dof).p_value)
    metrics, criteria, outcome, reasons = _null_outcome(pv_src, config)
    metrics["injected_flux_ujy"] = flux
    metrics["n_positions"] = len(positions)
    em, _, eo, _ = _null_outcome(pv_empty, config)
    metrics["empty_position_test"] = {"n_trials": em["n_trials"], "ks_pvalue": em["ks_pvalue"],
                                      "false_positive_rate": em["false_positive_rate"], "outcome_if_used": eo}
    level = "SYNTHETIC_INJECTION" if scope["origin"] == "SYNTHETIC" else "REAL_DATA_INJECTION"
    return _run("null_variability_constant_sources", level, "VARIABILITY_SIGNIFICANCE", scope, len(pv_src), metrics, criteria,
                outcome, reasons,
                "calibration of the constant-flux chi-square p-values for constant PSF-model sources (and empty positions) "
                "measured across these images",
                "photon noise of real sources, spectral structure within the wavelength tolerance, intrinsic PSF variations")


def null_motion(clusters: list[SourceCluster], detections: dict[str, DetectionRecord], image_info: dict[str, dict],
                config: EngineConfig, scope: dict, exclude: set[str] | None = None,
                include_only: set[str] | None = None) -> ValidationRun:
    """Stationarity test on well-measured multi-epoch sources that should not move.

    With ``include_only`` (Gaia DR3 stars whose catalogued proper motion is negligible at SPHEREx
    precision) the controls are known to be stationary; otherwise they are assumed stationary.
    """
    floor2 = config.association.registration_floor_arcsec ** 2
    pvalues, used = [], 0
    for c in clusters:
        if c.ambiguous or (exclude and c.source_id in exclude) or (include_only is not None and c.source_id not in include_only):
            continue
        dets = [detections[d] for d in c.det_ids if d in detections and detections[d].astrometry_usable]
        dets = [d for d in dets if d.quality_grade in ("A", "B")]
        epochs = {image_info.get(d.obs_id, {}).get("epoch_index") for d in dets}
        if len(dets) < 3 or len(epochs - {None}) < 2:
            continue
        m = motion_test([d.ra for d in dets], [d.dec for d in dets], [d.cov() + floor2 * np.eye(2) for d in dets],
                        [d.mjd for d in dets])
        if m.status == "COMPLETED":
            pvalues.append(m.gof_stationary.p_value)
            used += 1
    metrics, criteria, outcome, reasons = _null_outcome(pvalues, config)
    metrics["note"] = ("controls are Gaia DR3 stars with negligible catalogued proper motion (grade A/B detections)"
                       if include_only is not None else
                       "controls are detected sources with grade A/B in all images, assumed stationary (no catalogue available)")
    metrics["control_definition"] = "GAIA_QUASI_STATIONARY" if include_only is not None else "ASSUMED_STATIONARY"
    level = "SYNTHETIC_INJECTION" if scope["origin"] == "SYNTHETIC" else "REAL_DATA_EMPIRICAL"
    return _run("null_motion_stationary_sources", level, "MOTION_SIGNIFICANCE", scope, used, metrics, criteria, outcome, reasons,
                "calibration of the stationarity-test p-values (centroid errors plus registration floor) on real sources",
                "the assumption that the controls are stationary (a genuine mover among them inflates the false-positive rate)")


def noise_summary(runs: dict[str, DetectionRun], config: EngineConfig, scope: dict) -> ValidationRun:
    lo, hi = config.quality.noise_ratio_low, config.quality.noise_ratio_high
    rows = []
    for obs_id in sorted(runs):
        n = runs[obs_id].noise
        use_psf = n.get("psf_ratio") is not None
        r = n.get("psf_ratio") if use_psf else n.get("pixel_ratio")
        se = (n.get("psf_ratio_se") if use_psf else n.get("pixel_ratio_se")) or 0.0
        if r is None:
            state = "NOT_MEASURABLE"
        elif lo <= r <= hi:
            state = "WITHIN"
        elif r - 2 * se > hi or r + 2 * se < lo:
            state = "SIGNIFICANTLY_OUTSIDE"
        else:
            state = "UNDETERMINED"
        rows.append({"obs_id": obs_id, "scale_used": "PSF" if use_psf else "pixel", "ratio": r, "se": se,
                     "psf_ratio": n.get("psf_ratio"), "psf_samples": n.get("psf_samples"),
                     "pixel_ratio": n.get("pixel_ratio"), "pixel_samples": n.get("pixel_samples"), "state": state})
    states = {r["state"] for r in rows}
    if "SIGNIFICANTLY_OUTSIDE" in states:
        outcome = "FAILED"
    elif not rows or states & {"UNDETERMINED", "NOT_MEASURABLE"}:
        outcome = "NOT_ASSESSABLE"
    else:
        outcome = "PASSED"
    pixel = [r["pixel_ratio"] for r in rows if r["pixel_ratio"] is not None]
    level = "SYNTHETIC_INJECTION" if scope["origin"] == "SYNTHETIC" else "REAL_DATA_EMPIRICAL"
    return _run("noise_model_calibration", level, None, scope, len(rows),
                {"images": rows, "median_pixel_ratio": float(np.median(pixel)) if pixel else None},
                {"ratio_range": [lo, hi], "decision": "outside only if beyond 2 standard errors"},
                outcome, [] if outcome == "PASSED" else [outcome], "agreement of empirical and propagated noise per image",
                "noise behaviour at positions of real sources")


def robustness(analyses: dict[str, ImageAnalysis], runs: dict[str, DetectionRun], config: EngineConfig, scope: dict) -> ValidationRun:
    """Stability of detections and fluxes to the background mesh and deblending settings."""
    import dataclasses
    d = config.detection
    variants = {"mesh_half": dataclasses.replace(d, background_mesh_px=max(8, d.background_mesh_px // 2)),
                "mesh_double": dataclasses.replace(d, background_mesh_px=d.background_mesh_px * 2),
                "no_deblend": dataclasses.replace(d, deblend_passes=0)}
    out = {}
    for name, params in variants.items():
        cfg = config.replace(detection=params)
        n_base = n_found = 0
        shifts = []
        for obs_id, an in sorted(analyses.items()):
            if obs_id not in runs:
                continue
            base = [r for r in runs[obs_id].accepted if r.snr_fit >= d.threshold_sigma + 1.0]
            alt = detect(prepare(an.image, cfg), cfg).accepted
            for r in base:
                n_base += 1
                best = min(alt, key=lambda a: math.hypot(a.x - r.x, a.y - r.y), default=None)
                if best is not None and math.hypot(best.x - r.x, best.y - r.y) <= 1.0:
                    n_found += 1
                    shifts.append(abs(best.flux_ujy - r.flux_ujy) / r.flux_sigma_ujy if r.flux_sigma_ujy else math.nan)
        out[name] = {"baseline_detections": n_base, "recovered": n_found,
                     "recovered_fraction": n_found / n_base if n_base else None,
                     "median_abs_flux_change_sigma": float(np.nanmedian(shifts)) if shifts else None}
    fracs = [v["recovered_fraction"] for v in out.values() if v["recovered_fraction"] is not None]
    outcome = "NOT_ASSESSABLE" if not fracs else ("PASSED" if min(fracs) >= 0.95 else "FAILED")
    return _run("robustness_detection_settings", "SOFTWARE" if scope["origin"] == "SYNTHETIC" else "REAL_DATA_EMPIRICAL", None,
                scope, sum(v["baseline_detections"] for v in out.values()), {"variants": out},
                {"min_recovered_fraction": 0.95}, outcome, [] if outcome == "PASSED" else [outcome],
                "stability of detections (SNR >= threshold + 1) and fluxes to background-mesh and deblending choices",
                "robustness to choices not varied here (PSF model, flag policy, thresholds)")


def reproducibility(analyses: dict[str, ImageAnalysis], runs: dict[str, DetectionRun], config: EngineConfig, scope: dict) -> ValidationRun:
    from ..pipeline.results import canonical_hash
    rows = []
    for obs_id, an in sorted(analyses.items()):
        if obs_id not in runs:
            continue
        again = detect(prepare(an.image, config), config)
        h1 = canonical_hash([runs[obs_id].accepted, runs[obs_id].rejected])
        h2 = canonical_hash([again.accepted, again.rejected])
        rows.append({"obs_id": obs_id, "identical": h1 == h2})
    ok = all(r["identical"] for r in rows) if rows else None
    return _run("reproducibility_detection", "SOFTWARE", None, scope, len(rows), {"images": rows}, {"identical": True},
                "NOT_ASSESSABLE" if ok is None else ("PASSED" if ok else "FAILED"), [] if ok else ["NOT_IDENTICAL"],
                "bit-identical detection outputs when re-run on the same inputs and configuration",
                "anything about scientific validity")


def self_checks() -> list[dict]:
    """Independent numerical checks of engine formulas (MATHEMATICAL level)."""
    from .ephemeris import observer_heliocentric_au
    from .motion import parallax_fit
    from .psf import GaussianPsfModel
    from .skygeom import gnomonic, inverse_gnomonic, radec_from_vector, unit_vector
    from .stats import chi2_logsf, two_sided_sigma_from_logp
    checks = []

    def add(name, ok, detail):
        checks.append({"check": name, "state": "PASSED" if ok else "FAILED", "detail": detail})

    worst = max(abs(chi2_logsf(x, k) - float(_st.chi2.logsf(x, k))) for x in (0.5, 3.0, 20.0, 80.0) for k in (1, 2, 5, 30))
    add("chi2_logsf_vs_scipy", worst < 1e-9, f"max |difference| {worst:.2e}")
    tail = chi2_logsf(5000.0, 2)
    add("chi2_logsf_extreme_tail", abs(tail - (-2500.0)) < 1e-9, f"log Q(chi2=5000, dof=2) = {tail:.6f} (exact -2500)")
    z = two_sided_sigma_from_logp(chi2_logsf(9.0, 1))
    add("sigma_from_chi2_dof1", abs(z - 3.0) < 1e-9, f"chi2 = 9, dof 1 -> Z = {z:.12f} (exact 3)")
    sep = float(angular_separation_deg(10.0, 20.0, 10.0, 21.0))
    add("angular_separation", abs(sep - 1.0) < 1e-12, f"1 degree along a meridian -> {sep:.15f}")
    xi, eta = gnomonic(150.3, 2.9, 150.0, 3.0)
    ra, dec = inverse_gnomonic(xi, eta, 150.0, 3.0)
    add("gnomonic_round_trip", abs(float(ra) - 150.3) < 1e-10 and abs(float(dec) - 2.9) < 1e-10, "forward and inverse TAN agree")
    stamp = GaussianPsfModel(0.7, "check").render(0.3, -0.2, -20, -20, 41, 41)
    add("psf_normalisation", abs(stamp.sum() - 1.0) < 1e-9, f"pixel-integrated Gaussian sums to {stamp.sum():.12f}")
    mjd = np.array([61000.0, 61000.5, 61182.6, 61183.1, 61365.3, 61365.8])
    obs = observer_heliocentric_au(mjd)
    body = 700.0 * unit_vector(150.0, 3.0)
    ra_b, dec_b = radec_from_vector(body - obs)
    fit = parallax_fit(ra_b, dec_b, np.repeat((np.eye(2) * 0.04)[None], 6, axis=0), mjd, obs)
    ok = fit.status == "COMPLETED" and abs(fit.distance_au - 700.0) / 700.0 < 1e-3
    add("parallax_distance_recovery", ok, f"exact-geometry body at 700 AU -> {fit.distance_au if fit.distance_au else float('nan'):.3f} AU")
    r = np.linalg.norm(obs, axis=1)
    add("observer_distance_from_sun", bool(np.all((r > 0.98) & (r < 1.02))), f"|R| in [{r.min():.4f}, {r.max():.4f}] AU")
    return checks


def _link_field(analyses: dict[str, ImageAnalysis], detections: dict[str, list[DetectionRecord]], image_info: dict[str, dict],
                observer: dict[str, np.ndarray], config: EngineConfig):
    """Association, transient test and linking on given detections: the pipeline's moving-object chain."""
    from .association import associate
    from .linking import link_moving, tracklet_eligible, transient_test
    dets = [d for o in sorted(detections) for d in detections[o]]
    clusters = associate(dets, config.association)
    by_id = {d.det_id: d for d in dets}
    release = {o: image_info[o]["release"] for o in image_info}
    inputs = []
    for c in clusters:
        members = [by_id[x] for x in c.det_ids]
        epochs = {image_info[d.obs_id]["epoch_index"] for d in members}
        if len(epochs) != 1:
            continue
        forced = [forced_photometry(analyses[o], c.ra, c.dec, config, detections.get(o)) for o in sorted(analyses)
                  if analyses[o].image.contains(c.ra, c.dec)]
        t = transient_test(c, members, forced, release, config)
        if t.status == "TRANSIENT_LIKE" and tracklet_eligible(members, config)[0]:
            inputs.append((c, members, epochs.pop()))
    return link_moving(inputs, observer, config), by_id


MAX_FALSE_LINKS_PER_EXPERIMENT = 0.5  # unchanged acceptance threshold (upper 95% limit of spurious tracks per experiment)
FALSE_LINK_UNRESOLVED = "FALSE_LINK_CRITERION_UNRESOLVED"
INJECTION_POLICY = (
    "Detections of a track are attributed to injected bodies from the TRUE injected positions only (never from the linker's "
    "acceptance): a detection is the body's when its separation is within r = sqrt(chi2_2^-1(gate_probability)) x "
    "sqrt(sigma_det^2 + s^2) (the association gate with the detection's own centroid error sigma_det and the registration "
    "floor s), ambiguous within ambiguous_factor x r (or when two bodies are that close), foreign beyond. Track classes: pure "
    "(all detections the dominant body's), mixed (>=1 body and >=1 foreign detection), no_body (none), ambiguous (otherwise). "
    "Recovery: a body has >=1 pure track spanning >=2 distinct epochs. Duplicate policy: further pure tracks of that body are "
    "'extra_pure' and count as spurious. Spurious (definite) = mixed + no_body + extra_pure; ambiguous tracks are reported "
    "separately and counted only in the conservative bound.")


def attribute_experiment(record: dict, *, gate: float, floor_arcsec: float, ambiguous_factor: float,
                         fixed_radius_arcsec: float | None = None) -> dict:
    """Classify one experiment's tracks against its injected truth (see ``INJECTION_POLICY``). Pure function."""
    bodies = record["bodies"]

    def radius(sigma):
        return fixed_radius_arcsec if fixed_radius_arcsec is not None else math.sqrt(gate) * math.hypot(sigma or 0.0, floor_arcsec)

    tracks = []
    for t in record["tracks"]:
        n = len(t["dets"])
        base = {"track_id": t["track_id"], "n_dets": n, "epochs": sorted({d["epoch"] for d in t["dets"]}), "distance_au": t.get("distance_au")}
        if t.get("pre_existing"):
            tracks.append({**base, "class": "pre_existing", "dominant_body": None, "n_body": 0, "n_ambiguous": 0, "n_foreign": 0,
                           "body_epochs": [], "labels": []})
            continue
        labels = []
        for d in t["dets"]:
            r = radius(d.get("sigma_arcsec"))
            near = sorted((float(angular_separation_deg(d["ra"], d["dec"], b["positions"][d["obs_id"]][0], b["positions"][d["obs_id"]][1])) * 3600, bi)
                          for bi, b in enumerate(bodies) if d["obs_id"] in b["positions"])
            near = [x for x in near if x[0] <= ambiguous_factor * r]
            if not near:
                labels.append(("foreign", None))
            elif near[0][0] <= r and len(near) == 1:
                labels.append(("body", near[0][1]))
            else:
                labels.append(("ambiguous", near[0][1]))
        counts = collections.Counter(bi for lab, bi in labels if lab == "body")
        dominant = min(counts, key=lambda bi: (-counts[bi], bi)) if counts else None
        n_body = counts[dominant] if dominant is not None else 0
        n_amb = sum(1 for lab, _ in labels if lab == "ambiguous")
        n_for = n - n_body - n_amb
        if n_body == n:
            cls = "pure"
        elif n_body == 0 and n_amb == 0:
            cls = "no_body"
        elif n_body >= 1 and n_for >= 1:
            cls = "mixed"
        else:
            cls = "ambiguous"
        body_eps = sorted({d["epoch"] for d, (lab, bi) in zip(t["dets"], labels) if lab == "body" and bi == dominant})
        tracks.append({**base, "class": cls, "dominant_body": bodies[dominant]["id"] if dominant is not None else None, "n_body": n_body,
                       "n_ambiguous": n_amb, "n_foreign": n_for, "body_epochs": body_eps,
                       "labels": [lab if bi is None else f"{lab}:{bodies[bi]['id']}" for lab, bi in labels]})
    roles = {"pure": "extra_pure", "mixed": "spurious_mixed", "no_body": "spurious_no_body", "ambiguous": "ambiguous", "pre_existing": "pre_existing"}
    for t in tracks:
        t["role"] = roles[t["class"]]
    out_bodies = []
    for b in bodies:
        mine = [t for t in tracks if t["dominant_body"] == b["id"]]
        pures = [t for t in mine if t["class"] == "pure"]
        qualifying = [t for t in pures if len(t["epochs"]) >= 2]
        best = max(qualifying, key=lambda t: (len(t["epochs"]), t["n_dets"], t["track_id"] == "")) if qualifying else None
        if best is not None:
            best["role"] = "recovery"
        has = [t for t in mine if t["n_body"] > 0]
        out_bodies.append({
            "id": b["id"], "recovered": best is not None, "recovery_track": best["track_id"] if best else None,
            "n_pure": len(pures), "extra_pure": len(pures) - (1 if best else 0),
            "n_mixed": sum(t["class"] == "mixed" for t in mine), "n_ambiguous": sum(t["class"] == "ambiguous" for t in mine),
            "body_detections_in_ge2_epochs_any_track": any(len(t["body_epochs"]) >= 2 for t in mine),
            "outcome": "recovered" if best else ("mixed_only" if any(t["class"] == "mixed" for t in mine) else
                                                 ("ambiguous_only" if any(t["class"] == "ambiguous" for t in mine) else
                                                  ("pure_insufficient_epochs" if pures else "no_track")))})
    return {"tracks": tracks, "bodies": out_bodies}


def summarize_injection(experiments: list[dict], config: EngineConfig, *, flux_sigma: float, half_width_deg: float, baseline_tracks: int,
                        snr_grid: tuple[float, ...], legacy: dict | None = None, fixed_radius_arcsec: float | None = None,
                        ambiguous_factor: float | None = None) -> tuple[dict, dict, str, list[str]]:
    """Metrics, criteria and status from per-experiment truth and tracks. Deterministic; used live and for offline re-analysis."""
    p, v = config.planetx, config.validation
    factor = v.attribution_ambiguous_factor if ambiguous_factor is None else ambiguous_factor
    g = gate_d2(config.association)
    floor = config.association.registration_floor_arcsec
    rows, classes, roles, body_outcomes = [], collections.Counter(), collections.Counter(), collections.Counter()
    attributed = []
    for rec in experiments:
        att = attribute_experiment(rec, gate=g, floor_arcsec=floor, ambiguous_factor=factor, fixed_radius_arcsec=fixed_radius_arcsec)
        attributed.append({"experiment": rec["experiment"], **att})
        classes.update(t["class"] for t in att["tracks"])
        roles.update(t["role"] for t in att["tracks"])
        by_track = {t["track_id"]: t for t in att["tracks"]}
        for b, ob in zip(rec["bodies"], att["bodies"]):
            body_outcomes[ob["outcome"]] += 1
            dist = by_track[ob["recovery_track"]]["distance_au"] if ob["recovery_track"] else None
            rows.append({"experiment": rec["experiment"], "r_au": b["r_au"], "snr": b["snr"], "flux_ujy": b["flux_ujy"],
                         "covered": len(b["epochs_covered"]) >= 2, "recovered": ob["recovered"], "distance_au": dist})
    n_exp = len(experiments)
    covered = [r for r in rows if r["covered"]]
    rec_n = sum(r["recovered"] for r in covered)

    def frac(sub):
        k = sum(r["recovered"] for r in sub)
        return k, len(sub), list(wilson_interval(k, len(sub)))

    by_snr = []
    for s_ in snr_grid:
        k, n_, iv = frac([r for r in covered if r["snr"] == s_])
        by_snr.append({"snr": s_, "flux_ujy": s_ * flux_sigma, "covered": n_, "recovered": k, "completeness": k / n_ if n_ else None, "interval_95": iv})
    edges = np.geomspace(p.r_min_au, p.r_max_au, 4)
    by_r = []
    for lo_r, hi_r in zip(edges[:-1], edges[1:]):
        sub = [r for r in rows if lo_r <= r["r_au"] < hi_r or (hi_r == edges[-1] and r["r_au"] == hi_r)]
        k, n_, iv = frac([r for r in sub if r["covered"]])
        by_r.append({"r_au": [float(lo_r), float(hi_r)], "injected": len(sub), "covered_2_epochs": n_, "recovered": k,
                     "completeness_given_coverage": k / n_ if n_ else None, "interval_95": iv})
    derr = [abs(r["distance_au"] - r["r_au"]) / r["r_au"] for r in rows if r["recovered"] and r["distance_au"]]
    definite = classes["mixed"] + classes["no_body"] + roles["extra_pure"]
    conservative = definite + classes["ambiguous"]
    iv_def = poisson_interval(definite)
    iv_con = poisson_interval(conservative)
    metrics = {
        "n_injected": len(rows), "n_covered_2_epochs": len(covered), "n_recovered": rec_n,
        "coverage_fraction": len(covered) / len(rows) if rows else None,
        "completeness_given_coverage": rec_n / len(covered) if covered else None,
        "completeness_interval_95": list(wilson_interval(rec_n, len(covered))), "by_snr": by_snr, "by_distance": by_r,
        "median_relative_distance_error": float(np.median(derr)) if derr else None,
        "n_experiments": n_exp, "track_classes": dict(sorted(classes.items())), "track_roles": dict(sorted(roles.items())),
        "body_outcomes": dict(sorted(body_outcomes.items())),
        "spurious_tracks_definite": definite, "spurious_tracks_with_ambiguous": conservative,
        "false_links_per_experiment": definite / n_exp if n_exp else None,
        "false_links_interval_95": [iv_def[0] / n_exp, iv_def[1] / n_exp] if n_exp else None,
        "spurious_with_ambiguous_per_experiment": conservative / n_exp if n_exp else None,
        "spurious_with_ambiguous_interval_95": [iv_con[0] / n_exp, iv_con[1] / n_exp] if n_exp else None,
        "false_link_definition": "a spurious track is any track that is not the body's single pure recovery track: mixed (body plus "
                                 "foreign detections), no_body, or a further pure track of the same body (duplicate); tracks that match a "
                                 "track found without injection are excluded; ambiguous tracks are reported separately. The rate is the "
                                 "mean number of spurious tracks per injection experiment with a Garwood interval that assumes independent "
                                 "experiments; it is not a per-candidate false-positive rate.",
        "recovery_definition": "fraction of covered injected bodies (inside images of >= 2 epochs) with >= 1 pure track spanning >= 2 "
                               "distinct epochs, Wilson 95% interval; a measurement of this linker, field, cadence and constant-flux point "
                               "sources, not a sensitivity of the survey",
        "attribution": {"policy": INJECTION_POLICY, "gate_d2": g, "registration_floor_arcsec": floor, "ambiguous_factor": factor,
                        "fixed_radius_arcsec": fixed_radius_arcsec,
                        "centroid_sigma_used": any(d.get("sigma_arcsec") is not None for rec in experiments for t in rec["tracks"] for d in t["dets"])},
        "baseline_tracks_without_injection": baseline_tracks, "median_flux_sigma_ujy": flux_sigma, "field_half_width_deg": half_width_deg,
        "scenario_au": [p.r_min_au, p.r_max_au], "software": {"validation_version": VALIDATION_VERSION, "config_hash": config.hash},
        "legacy_validation_1_2": legacy, "attribution_results": attributed, "experiments": experiments}
    criteria = {"min_covered_injections": v.min_null_trials, "max_false_links_per_experiment_upper95": MAX_FALSE_LINKS_PER_EXPERIMENT,
                "applied_to": "spurious tracks with ambiguous tracks counted (conservative) for validation; definite count for miscalibration"}
    if not rows or not covered:
        return metrics, criteria, "NOT_ASSESSABLE", ["NO_COVERED_INJECTIONS"]
    if len(covered) < v.min_null_trials:
        return metrics, criteria, "UNVALIDATED", [ValidityReason.INSUFFICIENT_SAMPLE.value]
    if iv_con[1] / n_exp <= MAX_FALSE_LINKS_PER_EXPERIMENT:
        return metrics, criteria, "VALIDATED_FOR_CONFIG", []
    if iv_def[0] / n_exp > MAX_FALSE_LINKS_PER_EXPERIMENT:
        return metrics, criteria, "MISCALIBRATED", [ValidityReason.CRITERIA_FAILED.value]
    # The sample minimum is met; the interval simply straddles the threshold (neither shown nor refuted at this size).
    return metrics, criteria, "UNVALIDATED", [FALSE_LINK_UNRESOLVED]


def reanalyze_injection(saved_metrics: dict, config: EngineConfig, **overrides) -> tuple[dict, dict, str, list[str]]:
    """Recompute the injection accounting from a saved run's truth and tracks (no experiment is rerun; input is not modified)."""
    return summarize_injection(saved_metrics["experiments"], config, flux_sigma=saved_metrics["median_flux_sigma_ujy"],
                               half_width_deg=saved_metrics["field_half_width_deg"], baseline_tracks=saved_metrics["baseline_tracks_without_injection"],
                               snr_grid=tuple(b["snr"] for b in saved_metrics["by_snr"]), legacy=saved_metrics.get("legacy_validation_1_2"), **overrides)


def moving_object_injection(analyses: dict[str, ImageAnalysis], runs: dict[str, DetectionRun], image_info: dict[str, dict],
                            observer: dict[str, np.ndarray], config: EngineConfig, scope: dict, *, n_experiments: int = 4,
                            movers_per_experiment: int = 8, snr_grid: tuple[float, ...] = (8.0, 15.0, 30.0)) -> ValidationRun:
    """Inject distant bodies (exact parallax and orbital geometry) and measure linking completeness and false links.

    Each body has a heliocentric distance drawn log-uniformly in the configured scenario range, a
    transverse velocity up to the circular speed, a constant flux of ``snr x`` the median forced-flux
    sigma, and a direction inside the field at the first epoch. Its apparent position in each image
    is computed from the observer's heliocentric position at that image's time. Recovery: a linked
    track spanning at least two epochs whose detections lie within 2 pixels of the body's true
    positions. Bodies that leave the analysed area are counted separately (coverage selection effect).
    """
    from .skygeom import angular_separation_deg as sep_deg
    from .skygeom import inverse_gnomonic, radec_from_vector, unit_vector
    p = config.planetx
    rng = _rng(config, "moving_object_injection")
    obs_ids = [o for o in sorted(analyses) if o in observer and image_info[o]["mjd"] is not None]
    if len(obs_ids) < 3 or len({image_info[o]["epoch_index"] for o in obs_ids}) < 2:
        return _run("moving_object_injection", "SYNTHETIC_INJECTION" if scope["origin"] == "SYNTHETIC" else "REAL_DATA_INJECTION",
                    "MOVING_OBJECT_LINKING", scope, 0, {}, {}, "NOT_ASSESSABLE", ["FEWER_THAN_2_EPOCHS"],
                    "nothing", "linking completeness (needs images in at least two epochs)")
    centres = []
    for o in obs_ids:
        img = analyses[o].image
        x0, x1, y0, y1 = img.window
        centres.append([float(v) for v in img.pixel_to_world((x0 + x1 - 1) / 2.0, (y0 + y1 - 1) / 2.0)])
    ra0, dec0 = float(np.median([c[0] for c in centres])), float(np.median([c[1] for c in centres]))
    half = min(min(analyses[o].image.window[1] - analyses[o].image.window[0], analyses[o].image.window[3] - analyses[o].image.window[2])
               * analyses[o].image.meta.pixel_scale_arcsec / 7200.0 for o in obs_ids)
    sigmas = [f.flux_sigma_ujy for o in obs_ids[:8] for f in [forced_photometry(analyses[o], ra0, dec0, config, runs[o].accepted)]
              if f.status == "MEASURED"]
    flux_sigma = float(np.median(sigmas)) if sigmas else None
    if flux_sigma is None:
        return _run("moving_object_injection", "REAL_DATA_INJECTION", "MOVING_OBJECT_LINKING", scope, 0, {}, {},
                    "NOT_ASSESSABLE", ["NO_FLUX_SCALE"], "nothing", "linking completeness")
    mjd = np.array([image_info[o]["mjd"] for o in obs_ids])
    obs_pos = np.array([observer[o] for o in obs_ids])
    baseline_links, base_by_id = _link_field(analyses, {o: runs[o].accepted for o in analyses}, image_info, observer, config)
    base_points: list[dict] = []
    for bt in baseline_links.tracks:
        pts: dict[str, list] = {}
        for x in bt.det_ids:
            pts.setdefault(base_by_id[x].obs_id, []).append((base_by_id[x].ra, base_by_id[x].dec))
        base_points.append(pts)

    def pre_existing(track, by_id_) -> bool:
        """The field's own track (present without injection); detection ids differ between runs, so match by position."""
        dets_ = [by_id_[x] for x in track.det_ids]
        tol = 2 * analyses[dets_[0].obs_id].image.meta.pixel_scale_arcsec / 3600.0
        return any(sum(1 for d in dets_ if any(float(sep_deg(d.ra, d.dec, a, b)) < tol for a, b in bp.get(d.obs_id, [])))
                   >= 0.5 * len(dets_) for bp in base_points)

    rows, false_links, experiments = [], [], []
    idx = {o: i for i, o in enumerate(obs_ids)}
    truncated = ["baseline"] if baseline_links.truncated else []
    k = 0
    for e in range(n_experiments):
        bodies = []
        for _ in range(movers_per_experiment):
            r = float(np.exp(rng.uniform(np.log(p.r_min_au), np.log(p.r_max_au))))
            for _attempt in range(50):
                xi, eta = rng.uniform(-0.6 * half, 0.6 * half, 2)
                if all(math.hypot(xi - b["xi"], eta - b["eta"]) > 0.25 * half for b in bodies):
                    break
            ra_s, dec_s = inverse_gnomonic(xi, eta, ra0, dec0)
            u0 = unit_vector(float(ra_s), float(dec_s))
            tdir = np.cross(u0, rng.normal(size=3))
            tdir /= np.linalg.norm(tdir)
            v = tdir * rng.uniform(0.0, 1.0) * 2.0 * math.pi / math.sqrt(r)
            # Start so that the apparent position at the first image is (ra_s, dec_s).
            pos0 = obs_pos[0] + r * u0
            body = pos0[None, :] + np.outer((mjd - mjd[0]) / 365.25, v)
            ra_t, dec_t = radec_from_vector(body - obs_pos)
            snr = snr_grid[k % len(snr_grid)]
            k += 1
            bodies.append({"xi": xi, "eta": eta, "r_au": r, "snr": snr, "flux_ujy": snr * flux_sigma,
                           "ra": np.asarray(ra_t), "dec": np.asarray(dec_t)})
        modified, dets = {}, {}
        for i, o in enumerate(obs_ids):
            img = analyses[o].image
            srcs = []
            for b in bodies:
                if img.contains(float(b["ra"][i]), float(b["dec"][i]), margin_px=3):
                    x, y = (float(t) for t in img.world_to_pixel(float(b["ra"][i]), float(b["dec"][i])))
                    omega, _ = img.pixel_solid_angle_sr(x, y)
                    srcs.append((x, y, b["flux_ujy"] / (omega * 1e12)))
            an = prepare(img.with_image(add_point_sources(img, srcs)), config) if srcs else analyses[o]
            modified[o] = an
            dets[o] = detect(an, config).accepted if srcs else runs[o].accepted
        for o in analyses:
            modified.setdefault(o, analyses[o])
            dets.setdefault(o, runs[o].accepted)
        links, by_id = _link_field(modified, dets, image_info, observer, config)
        if links.truncated:
            truncated.append(f"experiment {e}")
        claimed = set()
        rec_bodies = []
        for bi, b in enumerate(bodies):
            inside = [o for o in obs_ids if analyses[o].image.contains(float(b["ra"][idx[o]]), float(b["dec"][idx[o]]), margin_px=3)]
            covered_epochs = {image_info[o]["epoch_index"] for o in inside}
            hit = None  # legacy validation-1.2 accounting, kept for comparison only (first track within 2 px wins)
            for t in links.tracks:
                near = [d for d in (by_id[x] for x in t.det_ids) if d.obs_id in idx and float(sep_deg(
                    d.ra, d.dec, b["ra"][idx[d.obs_id]], b["dec"][idx[d.obs_id]])) * 3600 < 2 * analyses[d.obs_id].image.meta.pixel_scale_arcsec]
                if len(near) >= 0.5 * len(t.det_ids) and len(set(t.epoch_indices)) >= 2:
                    hit = t
                    claimed.add(t.track_id)
                    break
            rows.append({"epochs_covered": len(covered_epochs), "recovered": hit is not None})
            rec_bodies.append({"id": f"E{e}B{bi}", "r_au": b["r_au"], "flux_ujy": b["flux_ujy"], "snr": b["snr"],
                               "epochs_covered": sorted(covered_epochs), "inside_obs": inside,
                               "positions": {o: [float(b["ra"][idx[o]]), float(b["dec"][idx[o]])] for o in obs_ids}})
        false_links.append(len([t for t in links.tracks if t.track_id not in claimed and not pre_existing(t, by_id)]))
        rec_tracks = []
        for t in links.tracks:
            rec_tracks.append({"track_id": t.track_id, "ambiguous_link": t.ambiguous, "diagnostics": t.diagnostics,
                               "n_points": t.n_points, "p_value": t.parallax.gof.p_value if t.parallax and t.parallax.gof else None,
                               "distance_au": t.parallax.distance_au if t.parallax else None, "pre_existing": pre_existing(t, by_id),
                               "dets": [{"det_id": d.det_id, "obs_id": d.obs_id, "epoch": image_info[d.obs_id]["epoch_index"], "ra": d.ra, "dec": d.dec,
                                         "sigma_arcsec": d.sigma_major_arcsec} for d in (by_id[x] for x in t.det_ids)]})
        experiments.append({"experiment": e, "truncated": links.truncated, "n_evaluations": links.n_evaluations, "n_inputs": links.n_inputs,
                            "n_feasible_pairs": links.n_feasible_pairs, "n_rejected_low_effective_dof": links.n_rejected_low_effective_dof,
                            "n_absorbed_chains": links.n_absorbed_chains, "notes": links.notes, "bodies": rec_bodies, "tracks": rec_tracks})
    level = "SYNTHETIC_INJECTION" if scope["origin"] == "SYNTHETIC" else "REAL_DATA_INJECTION"
    if truncated:
        # An incomplete search misses valid chains, so its recoveries and false links measure the cap, not the search.
        return _run("moving_object_injection", level, "MOVING_OBJECT_LINKING", scope, len(rows),
                    {"truncated_searches": truncated, "max_link_evaluations": p.max_link_evaluations,
                     "note": "linking search stopped at the evaluation bound; no recovery or completeness is reported"},
                    {}, "NOT_ASSESSABLE", ["LINKING_SEARCH_TRUNCATED"], "nothing about linking completeness",
                    "completeness, recovery or false-link rate (the search was incomplete)")
    cov_rows = [r for r in rows if r["epochs_covered"] >= 2]
    n_fl = int(sum(false_links))
    fl_lo, fl_hi = poisson_interval(n_fl)
    n_fields = len(false_links)
    legacy_outcome = ("UNVALIDATED" if len(cov_rows) < config.validation.min_null_trials else
                      "VALIDATED_FOR_CONFIG" if fl_hi / n_fields <= 0.5 else "MISCALIBRATED" if fl_lo / n_fields > 0.5 else "UNVALIDATED")
    legacy = {"definition": "validation-1.2: recovery = the first track with >= 50% of its detections within 2 pixels of truth and >= 2 track "
                            "epochs; false link = every other track not matching a field track",
              "n_covered": len(cov_rows), "n_recovered": sum(r["recovered"] for r in cov_rows), "false_links": n_fl,
              "false_links_per_experiment": n_fl / n_fields, "false_links_interval_95": [fl_lo / n_fields, fl_hi / n_fields],
              "status": legacy_outcome, "known_flaws": "mixed tracks can count as recoveries and pure tracks listed later as false links"}
    metrics, criteria, outcome, reasons = summarize_injection(
        experiments, config, flux_sigma=flux_sigma, half_width_deg=half, baseline_tracks=len(baseline_links.tracks), snr_grid=snr_grid, legacy=legacy)
    return _run("moving_object_injection", level, "MOVING_OBJECT_LINKING", scope, len(rows), metrics, criteria, outcome, reasons,
                "for this field, cadence, linker settings and constant-flux injected point sources: how many covered bodies are linked "
                "into a pure track spanning >= 2 epochs, and how many spurious (mixed, body-less or duplicate) tracks the injection "
                "produces per experiment, with the attribution policy and truth preserved for audit; VALIDATED_FOR_CONFIG applies only "
                "to the false-link criterion and does not imply adequate completeness",
                "bodies with other light curves or PSFs, other cadences or fields, real-sky linking sensitivity, known-object rejection")


def psf_mismatch(analyses: dict[str, ImageAnalysis], runs: dict[str, DetectionRun], config: EngineConfig, scope: dict, *,
                 broaden_sigma_px: float = 0.3, snr: float = 20.0, per_image: int = 10, max_images: int = 12) -> ValidationRun:
    """Sensitivity to PSF-model error: inject sources whose true PSF is the model convolved with a Gaussian
    (sigma ``broaden_sigma_px``), measure them with the unmodified model, and report flux bias, uncertainty
    calibration, centroid calibration and fit quality. Informational: no acceptance criterion."""
    from scipy.ndimage import gaussian_filter
    p = config.detection
    rows = []
    for obs_id in sorted(analyses)[:max_images]:
        an = analyses[obs_id]
        if an.image.psf is None or obs_id not in runs:
            continue
        rng = _rng(config, f"psf_mismatch:{obs_id}")
        pos = _empty_positions(an, [(d.x, d.y) for d in runs[obs_id].accepted], rng, per_image, float(p.psf_stamp_px),
                               p.edge_margin_px + p.psf_stamp_px // 2)
        if not pos:
            continue
        image = np.array(an.image.image, copy=True)
        ox, oy = an.image.origin
        truth = []
        for x, y in pos:
            sigma_a = math.sqrt(1.0 / fit_psf(an, x, y, p, fixed=True).sum_wp2)
            stamp, x_ll, y_ll = an.image.psf.stamp(x, y, 21)
            broad = gaussian_filter(stamp, broaden_sigma_px, mode="constant")
            broad *= stamp.sum() / broad.sum()
            ys, xs = slice(max(y_ll - oy, 0), min(y_ll - oy + 21, image.shape[0])), slice(max(x_ll - ox, 0), min(x_ll - ox + 21, image.shape[1]))
            image[ys, xs] += snr * sigma_a * broad[ys.start - (y_ll - oy):ys.stop - (y_ll - oy), xs.start - (x_ll - ox):xs.stop - (x_ll - ox)]
            truth.append((x, y, snr * sigma_a))
        new = detect(prepare(an.image.with_image(image), config), config)
        for x, y, amp in truth:
            best = min(new.accepted, key=lambda d: math.hypot(d.x - x, d.y - y), default=None)
            if best is None or math.hypot(best.x - x, best.y - y) > config.validation.match_radius_px:
                rows.append({"recovered": False})
                continue
            ra_t, dec_t = an.image.pixel_to_world(x, y)
            delta = offsets_arcsec(best.ra, best.dec, float(ra_t), float(dec_t))
            rows.append({"recovered": True, "flux_ratio": best.amplitude / amp,
                         "pull": (best.amplitude - amp) / best.amplitude_sigma, "chi2_red": best.chi2_red,
                         "d2": float(delta @ np.linalg.solve(best.cov(), delta)) if best.astrometry_usable else math.nan})
    rec = [r for r in rows if r["recovered"]]
    d2 = [r["d2"] for r in rec if math.isfinite(r["d2"])]
    metrics = {"broaden_sigma_px": broaden_sigma_px, "snr": snr, "n_injected": len(rows), "n_recovered": len(rec),
               "median_flux_ratio": float(np.median([r["flux_ratio"] for r in rec])) if rec else None,
               "flux_pull": {k: v for k, v in vars(pull_summary(np.array([r["pull"] for r in rec]))).items()} if rec else None,
               "median_chi2_red": float(np.median([r["chi2_red"] for r in rec])) if rec else None,
               "position_calibration_ratio": math.sqrt(float(np.median(d2)) / (2.0 * math.log(2.0))) if d2 else None}
    level = "SYNTHETIC_INJECTION" if scope["origin"] == "SYNTHETIC" else "REAL_DATA_INJECTION"
    return _run("psf_mismatch_sensitivity", level, None, scope, len(rows), metrics, {"criterion": "none (informational)"},
                "INFORMATIONAL" if rows else "NOT_ASSESSABLE", [] if rows else ["NO_INJECTIONS"],
                f"how a PSF {broaden_sigma_px} px (Gaussian sigma) broader than the model biases fluxes, uncertainties, centroids "
                "and fit chi-square in these images",
                "other forms of PSF error (asymmetry, wavelength or position dependence)")


def self_check_run(scope: dict) -> ValidationRun:
    checks = self_checks()
    ok = all(c["state"] == "PASSED" for c in checks)
    return _run("independent_numerical_checks", "MATHEMATICAL", None, scope, len(checks), {"checks": checks},
                {"all_pass": True}, "PASSED" if ok else "FAILED", [] if ok else ["FAILED"],
                "the engine's statistics, geometry, PSF normalisation and parallax formulas agree with independent references",
                "anything about data quality or scientific validity")


class ValidationRegistry:
    """Validation runs, persisted as JSON when a directory is given; maps scope cells to validity."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else None
        self._runs: list[dict] = []
        if self.root and self.root.is_dir():
            for path in sorted(self.root.glob("*.json")):
                try:
                    self._runs.append(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    continue

    def add(self, run: ValidationRun | dict) -> None:
        from ..pipeline.results import plain
        record = plain(run)
        self._runs.append(record)
        if self.root:
            self.root.mkdir(parents=True, exist_ok=True)
            (self.root / f"{record['run_id']}.json").write_text(json.dumps(record, indent=1, sort_keys=True), encoding="utf-8")

    def runs(self) -> list[dict]:
        return list(self._runs)

    def latest(self, cell: str) -> dict | None:
        matching = [r for r in self._runs if r.get("scope_cell") == cell]
        return max(matching, key=lambda r: r["created_at"]) if matching else None

    def _cell_validity(self, cell_kind: str, origin: str, release: str, config_hash: str) -> Validity:
        cell = scope_cell(cell_kind, origin, release, config_hash)
        run = self.latest(cell)
        if run is None:
            return Validity.unvalidated(ValidityReason.NO_CONTROL_RUN, scope_cell=cell)
        if run["outcome"] == "VALIDATED_FOR_CONFIG":
            return Validity(state=ValidityState.VALIDATED_FOR_CONFIG, scope_cell=cell, validation_run_ref=run["run_id"])
        if run["outcome"] == "MISCALIBRATED":
            return Validity(state=ValidityState.MISCALIBRATED, reasons=(ValidityReason.CRITERIA_FAILED,), scope_cell=cell,
                            validation_run_ref=run["run_id"])
        reasons = [ValidityReason(r) for r in run.get("reasons", []) if r in ValidityReason.__members__]
        return Validity.unvalidated(*(reasons or [ValidityReason.INSUFFICIENT_SAMPLE]), scope_cell=cell)

    def validity_for(self, candidate_key: str, origin: str, release: str, config_hash: str) -> Validity:
        if candidate_key == "TRANSIENT":
            first = self._cell_validity("VARIABILITY_SIGNIFICANCE", origin, release, config_hash)
            second = self._cell_validity("DETECTION_FALSE_POSITIVE", origin, release, config_hash)
            cell = scope_cell("TRANSIENT_SIGNIFICANCE", origin, release, config_hash)
            if first.state is ValidityState.VALIDATED_FOR_CONFIG and second.state is ValidityState.VALIDATED_FOR_CONFIG:
                return Validity(state=ValidityState.VALIDATED_FOR_CONFIG, scope_cell=cell,
                                validation_run_ref=f"{first.validation_run_ref}+{second.validation_run_ref}")
            for part in (first, second):
                if part.state is ValidityState.MISCALIBRATED:
                    return Validity(state=ValidityState.MISCALIBRATED, reasons=(ValidityReason.CRITERIA_FAILED,), scope_cell=cell,
                                    validation_run_ref=part.validation_run_ref)
            bad = first if first.state is not ValidityState.VALIDATED_FOR_CONFIG else second
            return Validity.unvalidated(*bad.reasons, scope_cell=cell)
        return self._cell_validity(CELL_KINDS[candidate_key], origin, release, config_hash)
