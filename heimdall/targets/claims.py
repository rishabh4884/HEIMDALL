"""Auditable claims for a target investigation, with a defensibility gate (contract ``heimdall.claims/1.0``).

Every user-visible conclusion of a target investigation is restated as an explicit claim with a precise ``claim_type``
(MEASUREMENT, DETECTION, NON_DETECTION, UPPER_LIMIT, CONSISTENT_WITH, INCONCLUSIVE, NOT_ASSESSABLE, BLOCKED, LIMITED_BY_DATA), the measured
values with units and uncertainties, the method and version, links to the exact evidence (JSON paths into the result, observation ids,
product checksums, manifest), what the claim does *not* establish, and the next discriminating step.

Claims are a deterministic function of the saved investigation: nothing is measured or inferred here. ``build_claims`` can therefore be
applied to an investigation saved before this module existed (``derived_from`` records which version was read).

The defensibility gate runs eight checks before a claim keeps its type (spec: provenance complete; measurement/uncertainty valid;
comparability/quality gates; artefact and ordinary-explanation checks visible; validation scope not overstated; evidence dependencies not
double-counted; real vs synthetic distinguished; reproducible frozen inputs). A failed *required* check downgrades the claim to the most
accurate weaker type and records ``downgraded_from`` and the failing checks; informational checks are reported but do not downgrade.
"""

from __future__ import annotations

from ..pipeline.results import canonical_hash

CLAIMS_SCHEMA = "heimdall.claims/1.0"
CLAIMS_METHOD_VERSION = "1.1.0"  # 1.1.0: analysis-specific, status-aware artefact gate; real uncertainty check

# What each analysis verdict may claim. Strength order (strongest first) is used for downgrades.
MOTION_CLAIMS = {
    "MOTION_DETECTED_CONSISTENT_WITH_CATALOGUE": ("CONSISTENT_WITH", "The measured apparent motion is consistent with the catalogue proper motion."),
    "MOTION_DETECTED_INCONSISTENT_WITH_CATALOGUE": ("DETECTION", "Apparent motion was detected that differs from the catalogue proper motion."),
    "NO_SIGNIFICANT_MOTION_BUT_CATALOGUE_REJECTED": ("INCONCLUSIVE", "No significant motion was measured, but the catalogue motion is also rejected."),
    "NO_SIGNIFICANT_MOTION": ("NON_DETECTION", "No significant apparent motion in this dataset at the stated sensitivity."),
    "MOTION_NOT_MEASURABLE_WITH_THESE_OBSERVATIONS": ("LIMITED_BY_DATA", "The observations are not sensitive enough to measure the expected motion."),
    "NOT_ASSESSABLE": ("NOT_ASSESSABLE", "Motion could not be assessed with the usable observations."),
}
VARIABILITY_CLAIMS = {
    "VARIABILITY_DETECTED": ("DETECTION", "Flux variability was detected in at least one comparable series and survived the control-star comparison."),
    "VARIABILITY_DETECTED_WITH_CAVEATS": ("CONSISTENT_WITH", "Flux variability is indicated, but caveats remain that could mimic it."),
    "SIGNIFICANT_BUT_NOT_DISTINGUISHABLE_FROM_FIELD_SYSTEMATICS": ("INCONCLUSIVE", "A formally significant flux change is not distinguishable from field systematics."),
    "NO_SIGNIFICANT_VARIABILITY": ("NON_DETECTION", "No significant variability in this dataset at the stated threshold; this is not a measurement of constancy."),
    "NOT_ASSESSABLE": ("NOT_ASSESSABLE", "No comparable series had enough points and epochs for a variability test."),
}
DOWNGRADE = {"DETECTION": "INCONCLUSIVE", "CONSISTENT_WITH": "INCONCLUSIVE", "NON_DETECTION": "INCONCLUSIVE", "MEASUREMENT": "NOT_ASSESSABLE",
             "UPPER_LIMIT": "NOT_ASSESSABLE"}
# Claim types that assert something about the sky and therefore need every required check.
ASSERTIVE = {"DETECTION", "CONSISTENT_WITH", "NON_DETECTION", "MEASUREMENT", "UPPER_LIMIT"}


def _check(name: str, passed: bool, detail: str, required: bool = True) -> dict:
    return {"check": name, "passed": bool(passed), "required": required, "detail": detail}


def _products_complete(result: dict) -> tuple[bool, str]:
    prods = result.get("products") or []
    if not prods:
        return False, "no products recorded"
    missing = [p.get("obs_id") or p.get("product_id") for p in prods if not (p.get("sha256") and p.get("release") and p.get("retrieved_at") and p.get("access_reference"))]
    return (not missing), (f"{len(prods)} products, each with sha256, release, retrieval time and access reference" if not missing
                           else f"{len(missing)} products lack checksum/release/retrieval time/access reference")


# Artefact / ordinary-explanation hypotheses that bear on each analysis (slugs from targets/hypotheses.py).
ARTEFACT_SLUGS = {"motion": ("registration_artifact", "blend_confusion", "unresolved_companion"),
                  "variability": ("calibration_artifact", "blend_contamination")}
UNRESOLVED = {"SUPPORTED", "CANNOT_EXCLUDE", "NOT_ASSESSABLE", "CONSISTENT"}


def _artefact_check(result: dict, analysis_name: str, claim_type: str) -> dict:
    """Required for assertive claims: the analysis' own artefact hypotheses must be recorded; none may be SUPPORTED; a DETECTION additionally
    needs every one of them excluded (a CONSISTENT_WITH claim, by definition, allows unexcluded alternatives)."""
    if analysis_name not in ARTEFACT_SLUGS:  # per-image measurements/limits: quality flags and saturation are recorded per image instead
        return _check("artefact_and_ordinary_explanations_visible", True, "per-image quantity; flags, saturation and contamination recorded per image")
    slugs = ARTEFACT_SLUGS[analysis_name]
    hyps = [h for h in result.get("hypotheses") or [] if h.get("slug") in slugs]
    states = {h["slug"]: h.get("status") for h in hyps}
    if claim_type not in ASSERTIVE:
        return _check("artefact_and_ordinary_explanations_visible", True, f"non-assertive claim; recorded: {states or 'none'}")
    if not hyps:
        return _check("artefact_and_ordinary_explanations_visible", False, f"no artefact hypothesis recorded for the {analysis_name} analysis")
    supported = [k for k, v in states.items() if v == "SUPPORTED"]
    open_ = [k for k, v in states.items() if v in UNRESOLVED]
    ok = not supported and (claim_type != "DETECTION" or not open_)
    return _check("artefact_and_ordinary_explanations_visible", ok, f"{analysis_name} artefact hypotheses {states}"
                  + ("; an artefact explanation is supported" if supported else "; not excluded, so a detection is not claimed" if not ok else ""))


def _gate(result: dict, analysis: dict | None, claim_type: str, *, uncertainty_ok: bool, comparability: tuple[bool, str], dependency: tuple[bool, str],
          analysis_name: str = "") -> list[dict]:
    synthetic = result.get("data_origin") != "REAL"
    prov_ok, prov_detail = _products_complete(result)
    mv = (analysis or {}).get("method_version") or ""
    man = (result.get("input_manifest") or {}).get("manifest_id")
    return [
        _check("provenance_complete", prov_ok, prov_detail),
        _check("measurement_and_uncertainty_valid", uncertainty_ok, "values carry uncertainties or a stated reason they are unavailable"
               if uncertainty_ok else "a numerical claim lacks an uncertainty"),
        _check("comparability_and_quality_gates", comparability[0], comparability[1]),
        _artefact_check(result, analysis_name, claim_type),
        _check("validation_scope_not_overstated", True, f"method {analysis.get('method_id') if analysis else '-'} {mv or '-'}: "
               + ("provisional method, unvalidated on real data; the claim is worded accordingly" if "provisional" in mv else "method version recorded"),
               required=False),
        _check("evidence_dependencies_not_double_counted", dependency[0], dependency[1]),
        _check("real_vs_synthetic_distinguished", not synthetic or claim_type not in {"DETECTION", "CONSISTENT_WITH"},
               f"data_origin={result.get('data_origin')}" + ("; a synthetic dataset can only demonstrate software behaviour" if synthetic else "")),
        _check("reproducible_frozen_inputs", bool(man) or prov_ok, f"frozen manifest {man}" if man else
               "no frozen manifest (investigation predates the data service); inputs identified by product checksums", required=False),
    ]


def _finalise(claim: dict, checks: list[dict]) -> dict:
    failed = [c["check"] for c in checks if c["required"] and not c["passed"]]
    claim["gate"] = {"checks": checks, "passed": not failed, "failed_required": failed}
    if failed and claim["claim_type"] in DOWNGRADE:
        claim["downgraded_from"] = claim["claim_type"]
        claim["claim_type"] = DOWNGRADE[claim["claim_type"]]
        claim["statement"] = f"Downgraded: {claim['statement']} (failed checks: {', '.join(failed)})"
    claim["claim_id"] = "clm:" + canonical_hash([claim["subject"], claim["analysis"], claim.get("downgraded_from") or claim["claim_type"],
                                                  claim.get("derived_from")])[7:19]
    return claim


def _motion_claim(result: dict, m: dict, base: dict) -> dict:
    ctype, statement = MOTION_CLAIMS.get(m.get("verdict"), ("INCONCLUSIVE", f"Unrecognised motion verdict {m.get('verdict')}."))
    fv, cv = m.get("fitted_velocity_arcsec_yr") or {}, m.get("catalogue_velocity_arcsec_yr") or {}
    values = []
    if fv.get("total") is not None:
        values.append({"name": "fitted_apparent_velocity", "value": fv.get("total"), "east": fv.get("east"), "north": fv.get("north"),
                       "sigma_east": fv.get("sigma_east"), "sigma_north": fv.get("sigma_north"), "unit": "arcsec/yr (angular)", "origin": "MEASURED"})
    if cv.get("total") is not None:
        values.append({"name": "catalogue_velocity", "value": cv.get("total"), "unit": "arcsec/yr (angular)", "origin": "CATALOGUE"})
    if m.get("velocity_difference_pull_sigma") is not None:
        values.append({"name": "measured_minus_catalogue", "value": m["velocity_difference_pull_sigma"], "unit": "sigma", "origin": "MEASURED"})
    unc_ok = all(v.get("origin") != "MEASURED" or v["name"] != "fitted_apparent_velocity" or (v.get("sigma_east") is not None and v.get("sigma_north") is not None)
                 for v in values)
    null = m.get("control_null_test") or {}
    comp = (null.get("status") == "COMPLETED" or ctype not in ASSERTIVE,
            f"control-star null test {null.get('status')} ({null.get('n_controls')} controls, pull scatter {null.get('pull_robust_std')})"
            if null else "no control-star null test recorded")
    ev = [e for e in result.get("evidence") or [] if e.get("source") == "targets.motion"]
    groups = sorted({e.get("dependency_group") for e in ev if e.get("dependency_group")})
    used = [t.get("obs_id") for t in m.get("track") or []]
    claim = base | {"analysis": "motion", "claim_type": ctype, "statement": statement, "values": values,
                    "method": {"id": m.get("method_id"), "version": m.get("method_version")},
                    "evidence_refs": ["analyses.motion", "analyses.motion.track", "analyses.motion.control_null_test"] + [f"evidence[{e['evidence_id']}]" for e in ev],
                    "observations_used": used, "n_usable": m.get("n_usable"), "baseline_days": m.get("baseline_days"),
                    "assumptions": m.get("limitations") or [], "reasons": m.get("reasons") or [],
                    "does_not_establish": ["distance, physical velocity, orbit or mass (angular motion only)",
                                           "that the source is newly discovered or unusual"] + (["that registration error or a blended companion is excluded"]
                                                                                               if ctype in ASSERTIVE else [])}
    checks = _gate(result, m, ctype, uncertainty_ok=unc_ok, comparability=comp, analysis_name="motion",
                   dependency=(True, f"motion evidence counted once per dependency group: {groups}" if groups else "no motion evidence items"))
    return _finalise(claim, checks)


def _variability_claim(result: dict, v: dict, base: dict) -> dict:
    ctype, statement = VARIABILITY_CLAIMS.get(v.get("verdict"), ("INCONCLUSIVE", f"Unrecognised variability verdict {v.get('verdict')}."))
    completed = [s for s in v.get("series") or [] if (s.get("test") or {}).get("status") == "COMPLETED"]
    best = max(completed, key=lambda s: s["test"].get("sigma_equivalent") or 0, default=None)
    values = []
    if best is not None:
        t = best["test"]
        values.append({"name": "strongest_series_departure_from_constant", "series_id": best["series_id"], "value": t.get("sigma_equivalent"),
                       "unit": "sigma (formal)", "chi2": t.get("chi2"), "dof": t.get("dof"), "empirical_false_alarm_fraction": t.get("empirical_false_alarm_fraction"),
                       "wavelength_um": best.get("wavelength_um"), "n_points": best.get("n_points"), "origin": "MEASURED",
                       "comparability": (best.get("comparability") or {}).get("status"),
                       "comparability_limitations": (best.get("comparability") or {}).get("limitations")})
    ref = (v.get("control_reference") or {})
    n_ctl = ref.get("n_control_series_completed") or 0
    comp_ok = ctype not in ("DETECTION", "CONSISTENT_WITH") or (best is not None and best["test"].get("empirical_false_alarm_fraction") is not None
                                                               and (best.get("comparability") or {}).get("status", "COMPARABLE") != "NOT_COMPARABLE")
    comp = (comp_ok, f"{n_ctl} completed control series; empirical false-alarm fraction "
            + ("computed" if best is not None and best["test"].get("empirical_false_alarm_fraction") is not None else "not computed"))
    claim = base | {"analysis": "variability", "claim_type": ctype, "statement": statement, "values": values,
                    "method": {"id": v.get("method_id"), "version": v.get("method_version")},
                    "evidence_refs": ["analyses.variability", "analyses.variability.series", "analyses.variability.control_reference",
                                      "analyses.variability.light_curve"],
                    "observations_used": [p.get("obs_id") for p in v.get("light_curve") or [] if p.get("usable")],
                    "n_usable": v.get("n_points_usable"), "baseline_days": v.get("baseline_days"),
                    "assumptions": ["points are compared only within comparable series (detector, release, wavelength within tolerance)",
                                    "no period is fitted"], "caveats": v.get("caveats") or [], "reasons": [v.get("verdict_reason")],
                    "does_not_establish": ["a period or variability class", "that the source is constant (a non-detection is sensitivity-limited)"
                                           if ctype == "NON_DETECTION" else "a physical cause of any flux change"]}
    unc_ok = all(s["test"].get("chi2") is not None for s in completed)
    checks = _gate(result, v, ctype, uncertainty_ok=unc_ok, comparability=comp, analysis_name="variability",
                   dependency=(True, "one claim from the strongest series; other series are not added as independent confirmations"))
    return _finalise(claim, checks)


def _measurement_claims(result: dict, base: dict) -> list[dict]:
    meas = [m for m in result.get("measurements") or [] if m.get("status") == "MEASURED" and m.get("forced")]
    out = []
    if meas:
        usable = [m for m in meas if (m["forced"].get("status") == "MEASURED" and m["forced"].get("flux_ujy") is not None and m["forced"].get("flux_sigma_ujy"))]
        unusable = [{"obs_id": m["obs_id"], "reasons": list(m["forced"].get("reasons") or ["NO_VALID_FLUX_OR_UNCERTAINTY"])} for m in meas if m not in usable]
        sat = sum(1 for m in usable if m.get("saturation_risk"))
        claim = base | {"analysis": "forced_photometry", "claim_type": "MEASUREMENT",
                        "statement": f"Forced photometry at the expected position in {len(usable)} image(s) with a valid flux and uncertainty "
                                     f"({len(unusable)} more without one, listed as excluded); {sat} are saturation/non-linearity flagged. "
                                     "A forced flux is a measurement at a position, not a detection.", "excluded": unusable,
                        "values": [{"name": "forced_flux", "obs_id": m["obs_id"], "value": m["forced"].get("flux_ujy"), "sigma": m["forced"].get("flux_sigma_ujy"),
                                    "unit": "microJansky", "wavelength_um": m["forced"].get("wavelength_um"), "mjd": m.get("mjd"),
                                    "saturation_flagged": bool(m.get("saturation_risk")), "origin": "MEASURED"} for m in usable],
                        "method": {"id": "forced_photometry", "version": result.get("methods_version")},
                        "evidence_refs": ["measurements"], "observations_used": [m["obs_id"] for m in usable], "n_usable": len(usable),
                        "assumptions": ["position from catalogue propagation or the user coordinate", "ePSF or fallback PSF as recorded per image"],
                        "does_not_establish": ["that a source is detected at this position", "absolute calibration beyond the archive's units"]}
        checks = _gate(result, {"method_id": "forced_photometry", "method_version": "provisional"}, "MEASUREMENT",
                       uncertainty_ok=bool(usable), comparability=(True, "per-image measurement; no comparison"),
                       dependency=(True, "one measurement per image"))
        out.append(_finalise(claim, checks))
    var = (result.get("analyses") or {}).get("variability") or {}
    lims = var.get("upper_limits") or []
    if lims:
        claim = base | {"analysis": "upper_limits", "claim_type": "UPPER_LIMIT",
                        "statement": f"{len(lims)} image(s) give 3-sigma upper limits (flux below the 3-sigma threshold); a limit is not zero flux.",
                        "values": [{"name": "upper_limit_3sigma", "obs_id": u["obs_id"], "value": u["upper_limit_3sigma_ujy"], "unit": "microJansky",
                                    "measured": u.get("measured_flux_ujy"), "sigma": u.get("sigma_ujy"), "origin": "MEASURED"} for u in lims],
                        "method": {"id": "censoring", "version": var.get("method_version")},
                        "evidence_refs": ["analyses.variability.upper_limits"], "observations_used": [u["obs_id"] for u in lims],
                        "does_not_establish": ["that the source is absent"]}
        checks = _gate(result, var, "UPPER_LIMIT", uncertainty_ok=True, comparability=(True, "per-image limit"),
                       dependency=(True, "one limit per image"))
        out.append(_finalise(claim, checks))
    return out


def build_claims(result: dict) -> dict:
    """Claims for a saved ``heimdall.target_investigation/1.0`` document (pure function)."""
    target = (result.get("target") or {}).get("target_id")
    base = {"subject": target, "derived_from": result.get("investigation_id"), "data_origin": result.get("data_origin")}
    st = result.get("status") or {}
    claims: list[dict] = []
    if st.get("execution") == "BLOCKED":
        claims.append(_finalise(base | {"analysis": "execution", "claim_type": "BLOCKED", "statement": f"No scientific result: {st.get('reason')}",
                                        "values": [], "method": {}, "evidence_refs": ["status", "failed_items"], "observations_used": [],
                                        "does_not_establish": ["anything about the sky (an archive or service failure is not a non-detection)"]}, []))
    else:
        analyses = result.get("analyses") or {}
        claims += _measurement_claims(result, base)
        if analyses.get("motion"):
            claims.append(_motion_claim(result, analyses["motion"], base))
        if analyses.get("variability"):
            claims.append(_variability_claim(result, analyses["variability"], base))
    nxt = [h.get("suggested_next_step") for h in result.get("hypotheses") or [] if h.get("suggested_next_step")]
    primary = "motion" if (result.get("target") or {}).get("category") == "MOVING_OBJECT" else "variability"
    head = next((c for c in claims if c["analysis"] == primary), next((c for c in claims if c["analysis"] == "execution"), None))
    return {"schema": CLAIMS_SCHEMA, "method_version": CLAIMS_METHOD_VERSION, "investigation_id": result.get("investigation_id"),
            "quick_answer": (result.get("conclusion") or {}).get("quick_answer"),
            "headline_claim": head["claim_id"] if head else None, "claims": claims,
            "next_discriminating_steps": sorted(set(nxt))[:5],
            "traversal": "quick_answer -> headline_claim -> claims[].evidence_refs (JSON paths in the investigation) -> measurements[] -> "
                         "products[] (sha256, access_reference) -> input_manifest (when present)",
            "note": "claims restate the saved investigation; they add no measurement and change no verdict except by the documented gate downgrade"}
