"""Motion analysis of a target from registered per-image centroids.

Inputs are ``ImageMeasurement`` dictionaries (``measure.py``): for every image the target's centroid offset
from the catalogue-predicted position, already corrected for the image's translation registration against
Gaia control stars, with a covariance that includes the centroid fit, the registration estimate and the
catalogue prediction.

Three models are compared on the *measured* positions in a fixed tangent frame:

- ``catalogue``: the catalogue-propagated track (no free parameters). chi2 of the residuals.
- ``stationary``: constant position (2 parameters).
- ``linear``: constant velocity (4 parameters), giving a fitted angular velocity with uncertainty.

Registration error is not assumed away. The same linear fit is applied to the control stars (each with a
leave-one-out registration so the shift is not fitted to the star being tested); the robust scatter of their
fitted velocity pulls is the empirical error scale applied to the target (never below 1), and the spread of
control velocities in mas/yr is reported as the systematic floor for motion.

Angular velocity is not a physical velocity. Without a distance it is converted to nothing.
"""

from __future__ import annotations

import math

import numpy as np

from ..engine.skygeom import offsets_arcsec
from ..engine.stats import Significance, chi2_significance, mad_std
from ..engine.temporal import build_epochs
from .astrometry import ReferenceAstrometry
from .measure import ControlOffset, registration_shift

MIN_POINTS = 3
MIN_EPOCHS = 2
MIN_BASELINE_DAYS = 20.0
MIN_CENTROID_SNR = 8.0
DETECT_SIGMA = 5.0  # equivalent sigma needed to call motion detected
CONSISTENT_SIGMA = 3.0  # catalogue is rejected above this
MEASURABLE_SIGMA = 3.0
YEAR = 365.25


def _sig(stat: float, dof: int) -> dict | None:
    if dof <= 0 or not math.isfinite(stat):
        return None
    s: Significance = chi2_significance(stat, dof)
    return {"statistic": float(stat), "dof": int(dof), "p_value": s.p_value, "log10_p": s.log10_p, "sigma_equivalent": s.sigma_equivalent}


def usable_points(measurements: list[dict]) -> tuple[list[dict], list[dict]]:
    keep, drop = [], []
    for m in measurements:
        why = []
        if m["status"] != "MEASURED":
            why.append(m["status"])
        else:
            c = m.get("centroid")
            if c is None or c["fit_status"] != "CONVERGED":
                why.append("CENTROID_NOT_CONVERGED")
            elif c["snr"] < MIN_CENTROID_SNR:
                why.append(f"CENTROID_SNR_BELOW_{MIN_CENTROID_SNR:g}")
            if m.get("saturation_risk"):
                why.append("SATURATION_OR_NONLINEARITY_FLAGS")
            if m.get("corrected_offset_arcsec") is None:
                why.append("NO_CORRECTED_OFFSET")
        (drop if why else keep).append({"obs_id": m["obs_id"], "reasons": why} if why else m)
    return keep, drop


def _gls(design: np.ndarray, y: np.ndarray, covs: list[np.ndarray]) -> tuple[np.ndarray, np.ndarray, float]:
    n = len(covs)
    W = np.zeros((2 * n, 2 * n))
    for i, c in enumerate(covs):
        W[2 * i:2 * i + 2, 2 * i:2 * i + 2] = np.linalg.inv(c)
    A = design.T @ W @ design
    cov = np.linalg.inv(A)
    beta = cov @ (design.T @ W @ y)
    r = y - design @ beta
    return beta, cov, float(r @ W @ r)


def _linear_fit(t_yr: np.ndarray, pos: np.ndarray, covs: list[np.ndarray], free_velocity: bool):
    n = len(t_yr)
    rows = []
    for ti in t_yr:
        if free_velocity:
            rows.append([[1, 0, ti, 0], [0, 1, 0, ti]])
        else:
            rows.append([[1, 0], [0, 1]])
    design = np.vstack([np.array(r, dtype=float) for r in rows])
    return _gls(design, pos.reshape(2 * n), covs)


def _tangent(ra, dec, ra0, dec0) -> np.ndarray:
    return np.asarray(offsets_arcsec(ra, dec, ra0, dec0), dtype=float)


def control_null_test(measurements: list[dict], floor_arcsec: float) -> dict:
    """Fit the same linear model to each Gaia control star with leave-one-out registration; return the velocity pulls."""
    per_control: dict[str, list[tuple[float, np.ndarray, np.ndarray]]] = {}
    for m in measurements:
        if m["status"] != "MEASURED" or not m.get("controls") or m.get("mjd") is None:
            continue
        ctr = [ControlOffset(**c) for c in m["controls"]]
        for j, c in enumerate(ctr):
            reg = registration_shift(ctr, floor_arcsec, exclude=j)
            if reg["status"] != "ESTIMATED":
                continue
            d = np.array([c.d_east_arcsec, c.d_north_arcsec]) - np.asarray(reg["shift_arcsec"])
            cov = np.asarray(c.cov_arcsec2) + np.asarray(reg["cov_arcsec2"])
            per_control.setdefault(c.gaia_source_id, []).append((float(m["mjd"]), d, cov))
    pulls, vel = [], []
    rows = []
    for gid, pts in per_control.items():
        mjds = np.array([p[0] for p in pts])
        if len(pts) < MIN_POINTS or (mjds.max() - mjds.min()) < MIN_BASELINE_DAYS:
            continue
        t = (mjds - mjds.mean()) / YEAR
        pos = np.array([p[1] for p in pts])
        covs = [p[2] for p in pts]
        beta, cov, chi2 = _linear_fit(t, pos, covs, True)
        zx, zy = beta[2] / math.sqrt(cov[2, 2]), beta[3] / math.sqrt(cov[3, 3])
        pulls += [float(zx), float(zy)]
        vel.append(beta[2:].tolist())
        rows.append({"gaia_source_id": gid, "n_points": len(pts), "velocity_east_mas_yr": beta[2] * 1e3, "velocity_north_mas_yr": beta[3] * 1e3,
                     "pull_east": float(zx), "pull_north": float(zy)})
    if len(rows) < 4:
        return {"status": "NOT_ASSESSABLE", "n_controls": len(rows), "error_scale": 1.0, "controls": rows,
                "reason": "fewer than 4 control stars with enough points and baseline; the null scatter cannot be estimated"}
    pulls_a = np.array(pulls)
    scale = max(1.0, float(mad_std(pulls_a)))
    v = np.array(vel) * 1e3
    return {"status": "COMPLETED", "n_controls": len(rows), "n_pulls": len(pulls), "pull_robust_std": float(mad_std(pulls_a)),
            "pull_rms": float(np.sqrt(np.mean(pulls_a ** 2))), "error_scale": scale,
            "velocity_systematic_mas_yr": float(np.sqrt(np.mean(v ** 2))), "controls": rows,
            "meaning": "stationary stars should give pulls with unit scatter; a larger scatter is registration or centroid error not in the formal covariance"}


def analyse_motion(measurements: list[dict], ref: ReferenceAstrometry, floor_arcsec: float, *, mjd_ref: float | None = None) -> dict:
    keep, dropped = usable_points(measurements)
    out: dict = {"method_id": "target_motion", "method_version": "1.0.0-provisional", "n_images": len(measurements), "n_usable": len(keep),
                 "excluded": dropped, "units": {"position": "arcsec (tangent plane, east/north)", "velocity": "arcsec/yr (angular, not physical)"}}
    mjds = np.array([m["mjd"] for m in keep], dtype=float)
    epochs, _ = build_epochs([(m["obs_id"], m["mjd"]) for m in keep], 5.0)
    baseline = float(mjds.max() - mjds.min()) if len(mjds) else 0.0
    out.update(n_epochs=len(epochs), baseline_days=baseline)
    reasons = []
    if len(keep) < MIN_POINTS:
        reasons.append(f"FEWER_THAN_{MIN_POINTS}_USABLE_POINTS")
    if len(epochs) < MIN_EPOCHS:
        reasons.append(f"FEWER_THAN_{MIN_EPOCHS}_EPOCHS")
    if baseline < MIN_BASELINE_DAYS:
        reasons.append(f"BASELINE_BELOW_{MIN_BASELINE_DAYS:g}_DAYS")
    null = control_null_test(measurements, floor_arcsec)
    out["control_null_test"] = null
    if reasons:
        out.update(status="NOT_ASSESSABLE", reasons=reasons, verdict="NOT_ASSESSABLE")
        return out
    scale = null.get("error_scale", 1.0)
    t0 = mjd_ref if mjd_ref is not None else float(np.median(mjds))
    exp0 = ref.at_mjd(t0)
    pos, covs, t_yr, exp_off = [], [], [], []
    for m in keep:
        e = m["expected"]
        eo = _tangent(e["ra_deg"], e["dec_deg"], exp0.ra_deg, exp0.dec_deg)
        r = np.asarray(m["corrected_offset_arcsec"])
        pos.append(eo + r)
        exp_off.append(eo)
        covs.append(np.asarray(m["corrected_offset_cov_arcsec2"]) * scale ** 2)
        t_yr.append((m["mjd"] - t0) / YEAR)
    pos, exp_off, t_yr = np.array(pos), np.array(exp_off), np.array(t_yr)
    resid = pos - exp_off
    chi2_cat = float(sum(resid[i] @ np.linalg.inv(covs[i]) @ resid[i] for i in range(len(keep))))
    _, cov_s, chi2_stat = _linear_fit(t_yr, pos, covs, False)
    beta_l, cov_l, chi2_lin = _linear_fit(t_yr, pos, covs, True)
    # Expected (catalogue) velocity from the propagated track itself.
    A = np.vstack([np.array([[1, 0, ti, 0], [0, 1, 0, ti]]) for ti in t_yr])
    cat_beta, _, _ = _gls(A, exp_off.reshape(-1), [np.eye(2)] * len(keep))
    v_cat = cat_beta[2:]
    v_fit, sig_v = beta_l[2:], np.sqrt(np.diag(cov_l)[2:])
    dv = v_fit - v_cat
    pull_cat = float(math.sqrt(dv @ np.linalg.inv(cov_l[2:, 2:]) @ dv))
    span_yr = baseline / YEAR
    disp_expected = float(np.hypot(*v_cat) * span_yr)
    sig_disp = float(np.sqrt(max(sig_v @ sig_v, 0.0)) * span_yr)
    s_stat_vs_lin = _sig(chi2_stat - chi2_lin, 2)
    s_cat = _sig(chi2_cat, 2 * len(keep))
    s_lin = _sig(chi2_lin, 2 * len(keep) - 4)
    s_stat = _sig(chi2_stat, 2 * len(keep) - 2)
    measurable = bool(sig_disp > 0 and disp_expected / sig_disp >= MEASURABLE_SIGMA)
    motion_sigma = (s_stat_vs_lin or {}).get("sigma_equivalent", 0.0) or 0.0
    cat_sigma = (s_cat or {}).get("sigma_equivalent", 0.0) or 0.0
    detected = motion_sigma >= DETECT_SIGMA
    if detected and cat_sigma < CONSISTENT_SIGMA:
        verdict = "MOTION_DETECTED_CONSISTENT_WITH_CATALOGUE"
    elif detected:
        verdict = "MOTION_DETECTED_INCONSISTENT_WITH_CATALOGUE"
    elif measurable and cat_sigma >= CONSISTENT_SIGMA:
        verdict = "NO_SIGNIFICANT_MOTION_BUT_CATALOGUE_REJECTED"
    elif not measurable:
        verdict = "MOTION_NOT_MEASURABLE_WITH_THESE_OBSERVATIONS"
    else:
        verdict = "NO_SIGNIFICANT_MOTION"
    out.update(
        status="COMPLETED", reasons=[], verdict=verdict, reference_mjd=t0, error_scale_applied=scale,
        catalogue_velocity_arcsec_yr={"east": float(v_cat[0]), "north": float(v_cat[1]), "total": float(np.hypot(*v_cat))},
        fitted_velocity_arcsec_yr={"east": float(v_fit[0]), "north": float(v_fit[1]), "sigma_east": float(sig_v[0]), "sigma_north": float(sig_v[1]),
                                   "total": float(np.hypot(*v_fit))},
        velocity_difference_pull_sigma=pull_cat, expected_displacement_arcsec=disp_expected, displacement_uncertainty_arcsec=sig_disp,
        measurable=measurable, measurable_criterion=f"expected displacement over the baseline / its uncertainty >= {MEASURABLE_SIGMA:g}",
        chi2={"catalogue": s_cat, "stationary": s_stat, "linear": s_lin, "stationary_vs_linear": s_stat_vs_lin},
        motion_detection_sigma=motion_sigma, thresholds={"detect_sigma": DETECT_SIGMA, "catalogue_rejected_sigma": CONSISTENT_SIGMA},
        track=[{"obs_id": m["obs_id"], "mjd": m["mjd"], "measured_offset_arcsec": pos[i].tolist(), "expected_offset_arcsec": exp_off[i].tolist(),
                "residual_arcsec": resid[i].tolist(), "sigma_arcsec": [math.sqrt(covs[i][0, 0]), math.sqrt(covs[i][1, 1])],
                "wavelength_um": m.get("wavelength_um"), "detector": m.get("detector")} for i, m in enumerate(keep)],
        residual_rms_arcsec=float(np.sqrt(np.mean(resid ** 2))),
        limitations=["translation-only registration", "controls assumed stationary after Gaia propagation",
                     "angular motion only; no distance, orbit or physical velocity is derived",
                     "centroids are not deblended from neighbours; see per-image contamination"])
    return out
