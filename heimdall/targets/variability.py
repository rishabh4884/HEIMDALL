"""Uncertainty-aware light curves and variability tests for a target.

SPHEREx observes a sky position through a linear variable filter, so successive exposures sample different
wavelengths. A raw flux-versus-time list therefore mixes spectrum and time. This module never treats it as one
light curve: points are grouped into *comparable series* (same detector and release, wavelengths within a
fraction of the bandwidth, see ``engine.temporal.build_series``) and each series is tested separately.

Calibration and contamination are handled with the target's own field:

- the same series construction and constant-flux test is applied to Gaia control stars measured in the same
  images; their reduced chi2 distribution is the empirical false-alarm reference and gives the field error scale
  (``engine.temporal.photometric_error_model``) applied to the target's flux errors when it exceeds 1;
- saturation/non-linearity flags, neighbour contamination, pipeline-version mixing and single-detector coverage
  are reported as caveats that downgrade the verdict instead of being silently ignored.

Measurements below 3 sigma are reported as censored upper limits and are not used in chi2 tests.
No period is ever fitted: ``period_assessment`` states the sampling requirement and why it fails or passes.
"""

from __future__ import annotations

import math
from collections import defaultdict

import numpy as np

from ..engine.config import EngineConfig
from ..engine.spectral import assemble_spectrum, compare_point_sets
from ..engine.temporal import (ComparableSeries, SeriesPoint, VariabilityResult, build_epochs, build_series,
                               photometric_error_model, variability_test)

MIN_SNR = 3.0
DETECT_SIGMA = 5.0
FALSE_ALARM_LIMIT = 0.05
MIN_EPOCHS_FOR_TREND = 3
MIN_EPOCHS_FOR_PERIOD = 6  # and a baseline of at least two cycles of any candidate period
CONTAMINATION_RATIO_LIMIT = 0.1


def _point(m: dict, epoch_of: dict[str, int], gaia: str | None = None) -> tuple[SeriesPoint | None, dict | None]:
    f = m["forced"]
    reasons: list[str] = []
    usable = f["status"] == "MEASURED" and f.get("flux_ujy") is not None and f.get("flux_sigma_ujy")
    if not usable:
        reasons += list(f.get("reasons") or ["FORCED_PHOTOMETRY_UNUSABLE"])
    if m.get("saturation_risk"):
        usable = False
        reasons.append("SATURATION_OR_NONLINEARITY_FLAGS")
    wl = f.get("wavelength_um")
    if wl is None or not math.isfinite(wl):
        usable = False
        reasons.append("NO_WAVELENGTH")
    snr = (f["flux_ujy"] / f["flux_sigma_ujy"]) if f.get("flux_ujy") is not None and f.get("flux_sigma_ujy") else None
    limit = None
    if usable and snr is not None and snr < MIN_SNR:
        limit = {"obs_id": m["obs_id"], "mjd": m["mjd"], "wavelength_um": wl, "upper_limit_3sigma_ujy": max(f["flux_ujy"], 0.0) + 3.0 * f["flux_sigma_ujy"],
                 "state": "UPPER_LIMIT", "measured_flux_ujy": f["flux_ujy"], "sigma_ujy": f["flux_sigma_ujy"]}
        usable = False
        reasons.append("BELOW_3_SIGMA_CENSORED")
    return (SeriesPoint(obs_id=m["obs_id"], mjd=m["mjd"], epoch_index=epoch_of.get(m["obs_id"]), detector=m.get("detector"), release=m.get("release"),
                        pipeline_version=m.get("pipeline_version"), wavelength_um=wl, bandwidth_um=f.get("bandwidth_um"),
                        wavelength_source=f.get("wavelength_source"), flux_ujy=f.get("flux_ujy"), flux_sigma_ujy=f.get("flux_sigma_ujy"),
                        measurement="FORCED", det_id=None, quality_grade=None, peak_flags=list(f.get("peak_flags") or []), usable=bool(usable),
                        reasons=reasons), limit)


def _series_summary(s: ComparableSeries, r: VariabilityResult | None, scale: float) -> dict:
    d = {"series_id": s.series_id, "detector": s.detector, "release": s.release, "wavelength_um": s.wavelength_um,
         "wavelength_span_um": s.wavelength_span_um, "bandwidth_um": s.bandwidth_um, "n_points": len(s.points), "n_epochs": s.n_epochs,
         "pipeline_versions": s.pipeline_versions, "points": [{"obs_id": p.obs_id, "mjd": p.mjd, "flux_ujy": p.flux_ujy,
                                                               "flux_sigma_ujy": p.flux_sigma_ujy, "wavelength_um": p.wavelength_um} for p in s.points]}
    if r is not None:
        d["test"] = {"status": r.status, "reasons": r.reasons, "weighted_mean_ujy": r.weighted_mean_ujy, "chi2": r.chi2, "dof": r.dof,
                     "reduced_chi2": (r.chi2 / r.dof) if r.chi2 is not None and r.dof else None,
                     "sigma_equivalent": r.significance.sigma_equivalent if r.significance else None,
                     "log10_p": r.significance.log10_p if r.significance else None, "peak_to_peak_ujy": r.peak_to_peak_ujy,
                     "fractional_peak_to_peak": (r.peak_to_peak_ujy / r.weighted_mean_ujy) if r.peak_to_peak_ujy is not None and r.weighted_mean_ujy else None,
                     "max_abs_pull": r.max_abs_pull, "leave_one_out_sigma": r.leave_one_out.sigma_equivalent if r.leave_one_out else None,
                     "error_scale_applied": scale}
    return d


def _trend(s: ComparableSeries, scale: float) -> dict:
    ep = {p.epoch_index for p in s.points if p.epoch_index is not None}
    if len(ep) < MIN_EPOCHS_FOR_TREND or len(s.points) < MIN_EPOCHS_FOR_TREND:
        return {"series_id": s.series_id, "status": "NOT_ASSESSABLE", "reason": f"fewer than {MIN_EPOCHS_FOR_TREND} epochs"}
    t = np.array([p.mjd for p in s.points], dtype=float)
    f = np.array([p.flux_ujy for p in s.points], dtype=float)
    sg = np.array([p.flux_sigma_ujy for p in s.points], dtype=float) * scale
    tt = (t - t.mean()) / 365.25
    w = 1.0 / sg ** 2
    A = np.vstack([np.ones_like(tt), tt]).T
    cov = np.linalg.inv((A.T * w) @ A)
    beta = cov @ ((A.T * w) @ f)
    chi2 = float(np.sum(w * (f - A @ beta) ** 2))
    z = beta[1] / math.sqrt(cov[1, 1])
    return {"series_id": s.series_id, "status": "COMPLETED", "slope_ujy_per_yr": float(beta[1]), "slope_sigma_ujy_per_yr": float(math.sqrt(cov[1, 1])),
            "slope_significance_sigma": float(abs(z)), "chi2": chi2, "dof": len(f) - 2,
            "note": "a straight-line trend; with so few epochs it cannot separate a trend from slow variability"}


def period_assessment(epochs: int, baseline_days: float, n_points: int) -> dict:
    ok = epochs >= MIN_EPOCHS_FOR_PERIOD
    return {"status": "NOT_ASSESSABLE" if not ok else "SAMPLING_ADEQUATE_FOR_SEARCH_ONLY",
            "n_epochs": epochs, "baseline_days": baseline_days, "n_points": n_points,
            "reason": (f"{epochs} epochs over {baseline_days:.0f} d; a period search needs at least {MIN_EPOCHS_FOR_PERIOD} epochs and a baseline "
                       "covering at least two cycles of any claimed period, and SPHEREx wavelength sampling varies between exposures")
            if not ok else "sampling meets the minimum epoch count; no period search is performed by this analysis",
            "period_days": None}


def analyse_variability(measurements: list[dict], cfg: EngineConfig, *, extra_caveats: list[str] | None = None) -> dict:
    meas = [m for m in measurements if m["status"] == "MEASURED" and m.get("forced")]
    epochs, epoch_of = build_epochs([(m["obs_id"], m["mjd"]) for m in meas if m.get("mjd") is not None], cfg.temporal.epoch_gap_days)
    pts, limits = [], []
    for m in meas:
        p, lim = _point(m, epoch_of)
        if p is not None:
            pts.append(p)
        if lim is not None:
            limits.append(lim)
    baseline = float(max((p.mjd for p in pts if p.mjd), default=0) - min((p.mjd for p in pts if p.mjd), default=0))
    # Control series from the same images.
    by_control: dict[str, list[SeriesPoint]] = defaultdict(list)
    for m in meas:
        for c in m.get("control_fluxes", []):
            by_control[c["gaia_source_id"]].append(SeriesPoint(
                obs_id=m["obs_id"], mjd=m["mjd"], epoch_index=epoch_of.get(m["obs_id"]), detector=m.get("detector"), release=m.get("release"),
                pipeline_version=m.get("pipeline_version"), wavelength_um=c["wavelength_um"], bandwidth_um=c["bandwidth_um"],
                wavelength_source=None, flux_ujy=c["flux_ujy"], flux_sigma_ujy=c["flux_sigma_ujy"], measurement="FORCED", det_id=None,
                quality_grade=None, peak_flags=[], usable=True))
    ctl_results: list[VariabilityResult] = []
    for gid, cp in by_control.items():
        for s in build_series(cp, cfg.temporal.wavelength_tolerance_fraction, f"CTL:{gid}"):
            ctl_results.append(variability_test(s, cfg.temporal))
    err = photometric_error_model(ctl_results, min_series=10)
    scale = float(err["scale"]) if err["status"] == "COMPLETED" and err["scale"] and err["scale"] > 1.0 else 1.0
    ctl_ratio = np.array([r.chi2 / r.dof for r in ctl_results if r.status == "COMPLETED" and r.dof])
    tgt_scaled = []
    for p in pts:
        q = SeriesPoint(**{**p.__dict__})
        if q.flux_sigma_ujy:
            q.flux_sigma_ujy = q.flux_sigma_ujy * scale
        tgt_scaled.append(q)
    series = build_series(tgt_scaled, cfg.temporal.wavelength_tolerance_fraction, "TGT")
    series_out, results = [], []
    for s in series:
        r = variability_test(s, cfg.temporal)
        results.append(r)
        d = _series_summary(s, r, scale)
        if r.status == "COMPLETED" and r.dof and len(ctl_ratio) >= 5:
            d["test"]["empirical_false_alarm_fraction"] = float(np.mean(ctl_ratio >= r.chi2 / r.dof))
            d["test"]["n_control_series"] = int(len(ctl_ratio))
        d["trend"] = _trend(s, scale)
        series_out.append(d)
    completed = [d for d in series_out if d["test"]["status"] == "COMPLETED"]
    # Cross-epoch spectral comparison (bands matched by wavelength, never by time alone).
    by_epoch: dict[int, list[SeriesPoint]] = defaultdict(list)
    for p in tgt_scaled:
        if p.epoch_index is not None:
            by_epoch[p.epoch_index].append(p)
    comps = []
    keys = sorted(by_epoch)
    for a, b in zip(keys, keys[1:]):
        c = compare_point_sets(f"epoch {a} vs {b}", by_epoch[a], by_epoch[b], cfg.temporal.wavelength_tolerance_fraction)
        comps.append({"label": c.label, "status": c.status, "reasons": c.reasons, "n_pairs": c.n_pairs,
                      "sigma_equivalent": c.significance.sigma_equivalent if c.significance else None,
                      "pairs": c.pairs[:12]})
    spectrum = assemble_spectrum("target", [p for p in tgt_scaled if p.usable])
    # Caveats.
    caveats = list(extra_caveats or [])
    if any(m.get("saturation_risk") for m in meas):
        caveats.append(f"{sum(1 for m in meas if m.get('saturation_risk'))} of {len(meas)} measured images have saturation/non-linearity flags in the PSF footprint (excluded from tests)")
    heavy = [(m["obs_id"], c) for m in meas for c in m.get("contamination", []) if c.get("optical_flux_ratio_to_target", 0) >= CONTAMINATION_RATIO_LIMIT]
    if heavy:
        caveats.append(f"Gaia neighbour(s) with optical flux ratio >= {CONTAMINATION_RATIO_LIMIT} inside the PSF stamp (e.g. {heavy[0][1]['gaia_source_id']} at {heavy[0][1]['separation_arcsec']:.1f} arcsec); not deblended")
    versions = sorted({p.pipeline_version for p in pts if p.pipeline_version})
    if len(versions) > 1:
        caveats.append(f"mixed pipeline versions {versions}: calibration differences can mimic variability")
    if len({p.detector for p in pts if p.usable}) <= 1 and pts:
        caveats.append("single detector in the usable measurements (no cross-detector calibration check)")
    if err["status"] != "COMPLETED":
        caveats.append(f"field error scale not estimated ({err.get('reason')}); formal flux errors used")
    sig = [d["test"]["sigma_equivalent"] for d in completed if d["test"].get("sigma_equivalent") is not None]
    survive = [d for d in completed if (d["test"].get("sigma_equivalent") or 0) >= DETECT_SIGMA
               and (d["test"].get("empirical_false_alarm_fraction") is None or d["test"]["empirical_false_alarm_fraction"] <= FALSE_ALARM_LIMIT)]
    if not completed:
        verdict, why = "NOT_ASSESSABLE", "no comparable series has at least the minimum points and epochs"
    elif survive and not caveats:
        verdict, why = "VARIABILITY_DETECTED", "a series exceeds the significance threshold and the control false-alarm limit"
    elif survive:
        verdict, why = "VARIABILITY_DETECTED_WITH_CAVEATS", "a series survives the control comparison but caveats remain"
    elif sig and max(sig) >= DETECT_SIGMA:
        verdict, why = "SIGNIFICANT_BUT_NOT_DISTINGUISHABLE_FROM_FIELD_SYSTEMATICS", "formal significance is not supported by the control-star reference"
    else:
        verdict, why = "NO_SIGNIFICANT_VARIABILITY", "no completed series exceeds the significance threshold"
    return {"method_id": "target_variability", "method_version": "1.0.0-provisional", "status": "COMPLETED" if completed else "INSUFFICIENT_INPUTS",
            "verdict": verdict, "verdict_reason": why, "thresholds": {"detect_sigma": DETECT_SIGMA, "false_alarm_limit": FALSE_ALARM_LIMIT, "min_snr": MIN_SNR},
            "n_images": len(measurements), "n_measured": len(meas), "n_points_usable": sum(p.usable for p in pts), "n_upper_limits": len(limits),
            "n_epochs": len(epochs), "epochs": [{"index": e.index, "mjd_start": e.mjd_start, "mjd_end": e.mjd_end, "n": e.n_observations} for e in epochs],
            "baseline_days": baseline, "series": series_out, "upper_limits": limits, "epoch_comparisons": comps, "spectrum": {
                "coverage": spectrum.coverage, "band_means": spectrum.band_means, "slope": spectrum.slope, "notes": spectrum.notes},
            "control_reference": {"error_model": err, "n_control_series_tested": len(ctl_results), "n_control_series_completed": int(len(ctl_ratio)),
                                  "median_reduced_chi2": float(np.median(ctl_ratio)) if len(ctl_ratio) else None},
            "period_assessment": period_assessment(len(epochs), baseline, sum(p.usable for p in pts)),
            "caveats": caveats,
            "light_curve": [{"obs_id": p.obs_id, "mjd": p.mjd, "wavelength_um": p.wavelength_um, "detector": p.detector, "flux_ujy": p.flux_ujy,
                             "flux_sigma_ujy": p.flux_sigma_ujy, "usable": p.usable, "reasons": p.reasons, "flags": p.peak_flags,
                             "epoch_index": p.epoch_index} for p in pts],
            "units": {"flux": "microJansky (F_nu)", "wavelength": "micrometre", "time": "MJD (UTC)"},
            }
