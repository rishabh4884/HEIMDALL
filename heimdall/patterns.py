"""Shared pattern record across motion, temporal, spectral and spatial dimensions (``heimdall.patterns/1.0``).

One record per dimension, each linking to the analysis paths, measurements and evidence it rests on, its assumptions, dependency groups,
validity and status. Missing dimensions are listed with the reason instead of being dropped. Temporal findings use comparable series only
(same detector/release, wavelength within tolerance); wavelength-dependent differences are reported as spectral structure, never as
temporal variability. A pure function of the saved investigation: nothing is measured here.
"""

from __future__ import annotations

PATTERNS_SCHEMA = "heimdall.patterns/1.0"


def _ev(result: dict, source: str) -> tuple[list[str], list[str]]:
    ev = [e for e in result.get("evidence") or [] if e.get("source") == source]
    return [e["evidence_id"] for e in ev if e.get("evidence_id")], sorted({e.get("dependency_group") for e in ev if e.get("dependency_group")})


def build_patterns(result: dict) -> dict:
    an = result.get("analyses") or {}
    m, v = an.get("motion"), an.get("variability")
    dims: list[dict] = []
    if m:
        ids, groups = _ev(result, "targets.motion")
        dims.append({"dimension": "motion", "status": m.get("status"), "finding": m.get("verdict"),
                     "quantities": {"fitted_velocity_arcsec_yr": m.get("fitted_velocity_arcsec_yr"), "catalogue_velocity_arcsec_yr": m.get("catalogue_velocity_arcsec_yr"),
                                    "baseline_days": m.get("baseline_days"), "n_usable": m.get("n_usable")},
                     "units": m.get("units"), "links": {"analysis": "analyses.motion", "track": "analyses.motion.track", "evidence": ids},
                     "dependency_groups": groups, "assumptions": m.get("limitations") or [], "reasons": m.get("reasons") or [],
                     "physical_interpretation": "angular only: distance, physical speed and orbit require independent constraints"})
    else:
        dims.append({"dimension": "motion", "status": "NOT_RUN", "reason": "not requested for this target category"})
    if v:
        ids, groups = _ev(result, "targets.variability")
        completed = [s for s in v.get("series") or [] if (s.get("test") or {}).get("status") == "COMPLETED"]
        dims.append({"dimension": "temporal", "status": v.get("status"), "finding": v.get("verdict"), "reason": v.get("verdict_reason"),
                     "quantities": {"n_epochs": v.get("n_epochs"), "baseline_days": v.get("baseline_days"), "comparable_series_tested": len(completed),
                                    "upper_limits": v.get("n_upper_limits"), "comparability": v.get("comparability_summary")},
                     "links": {"analysis": "analyses.variability", "series": "analyses.variability.series", "light_curve": "analyses.variability.light_curve",
                               "evidence": ids},
                     "dependency_groups": groups, "caveats": v.get("caveats") or [],
                     "guard": "only points in the same comparable passband are compared over time; wavelength differences are not variability",
                     "period": v.get("period_assessment")})
        sp = v.get("spectrum") or {}
        bands = sp.get("band_means") or []
        dims.append({"dimension": "spectral", "status": "DESCRIPTIVE" if bands else "NOT_ASSESSABLE",
                     "finding": "per-detector band means and slope (descriptive; not a calibrated SED fit)" if bands else None,
                     "reason": None if bands else "no usable wavelength-resolved points",
                     "quantities": {"band_means": [{k: b.get(k) for k in ("detector", "weighted_mean_ujy", "sigma_ujy", "n_points")} for b in bands],
                                    "slope": sp.get("slope"), "coverage": sp.get("coverage")},
                     "links": {"spectrum": "analyses.variability.spectrum", "epoch_comparisons": "analyses.variability.epoch_comparisons"},
                     "assumptions": ["points from different epochs are not simultaneous", "no cross-detector calibration or stacking is applied"],
                     "guard": "a band chi2 includes genuine spectral structure and is never used as a variability test"})
    else:
        dims += [{"dimension": "temporal", "status": "NOT_RUN", "reason": "variability analysis not run"},
                 {"dimension": "spectral", "status": "NOT_RUN", "reason": "no wavelength-resolved forced photometry"}]
    ctx = result.get("context") or {}
    meas = result.get("measurements") or []
    blended = sorted({c.get("gaia_source_id") for x in meas for c in x.get("contamination") or [] if c.get("optical_flux_ratio_to_target", 0) >= 0.1})
    dims.append({"dimension": "spatial", "status": "DESCRIPTIVE",
                 "finding": ("unresolved companion at instrument resolution" if ctx.get("unresolved_companion") else
                             f"{len(blended)} bright neighbour(s) inside the PSF stamp" if blended else "no bright neighbour recorded inside the PSF stamp"),
                 "quantities": {"bright_neighbours": blended, "psf_fwhm_arcsec": next((x.get("psf_fwhm_arcsec") for x in meas if x.get("psf_fwhm_arcsec")), None),
                                "contamination_summary": ctx.get("contamination_summary")},
                 "links": {"measurements": "measurements[].contamination", "context": "context"},
                 "assumptions": ["Gaia neighbours propagated to each epoch", "no deblending or morphology fit is performed"],
                 "limitation": "point-like vs extended structure is not assessed (no validated morphology method)"})
    absent = ("NOT_RUN", "NOT_ASSESSABLE", "INSUFFICIENT_INPUTS")
    present = [d["dimension"] for d in dims if d["status"] not in absent]
    missing = [{"dimension": d["dimension"], "reason": d.get("reason")} for d in dims if d["status"] in absent]
    return {"schema": PATTERNS_SCHEMA, "investigation_id": result.get("investigation_id"), "data_origin": result.get("data_origin"),
            "dimensions": dims, "present": present, "missing": missing,
            "cross_dimension": {"independent_lines_of_evidence": sorted({g for d in dims for g in d.get("dependency_groups", [])}),
                                "note": "findings sharing a dependency group (e.g. the same centroids) are one line of evidence, not several"}}
