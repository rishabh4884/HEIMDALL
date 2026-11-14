"""Evidence items, competing hypotheses and the plain-language conclusion for a target investigation.

Everything is derived by explicit rules from the analyses (``motion``, ``variability``) and the measurement
context. A hypothesis status is one of

- ``SUPPORTED``: the stated tests favour it and the main alternatives are disfavoured;
- ``CONSISTENT``: not contradicted, but not uniquely supported;
- ``CANNOT_EXCLUDE``: a test that could exclude it was not decisive (an unavailable or limited test is the reason);
- ``DISFAVOURED``: a test contradicts it;
- ``NOT_ASSESSABLE``: the required observations or models do not exist (the blocking dependency is named).

Evidence items carry a ``dependency_group``; two items of one group are one observation analysed twice and are
never counted as independent. A missing catalogue match is not evidence of anything; this module never
emits a discovery or Planet X statement.
"""

from __future__ import annotations

from ..pipeline.results import canonical_hash
from .astrometry import ReferenceAstrometry


def _id(prefix: str, *parts) -> str:
    return f"{prefix}:{canonical_hash(list(parts))[7:15]}"


class Evidence:
    def __init__(self, target: str) -> None:
        self.target = target
        self.items: list[dict] = []

    def add(self, name: str, statement: str, *, value=None, unit: str | None = None, uncertainty=None, origin: str = "MEASURED",
            group: str, direction: str = "neutral", source: str = "") -> str:
        eid = _id("ev", self.target, name)
        self.items.append({"evidence_id": eid, "name": name, "statement": statement, "value": value, "unit": unit, "uncertainty": uncertainty,
                           "origin": origin, "dependency_group": group, "direction": direction, "source": source})
        return eid


def _hyp(target: str, slug: str, title: str, status: str, summary: str, *, support=(), contra=(), tests_run=(), tests_unavailable=(),
         assumptions=(), limitations=(), next_step: str = "") -> dict:
    return {"hypothesis_id": _id("hyp", target, slug), "slug": slug, "title": title, "status": status, "summary": summary,
            "supporting_evidence": list(support), "contradicting_evidence": list(contra), "tests_run": list(tests_run),
            "tests_unavailable": [{"test": t, "reason": r} for t, r in tests_unavailable], "assumptions": list(assumptions),
            "limitations": list(limitations), "suggested_next_step": next_step}


def build_moving(target: str, ref: ReferenceAstrometry, motion: dict, ctx: dict) -> tuple[list[dict], list[dict], dict]:
    ev = Evidence(target)
    v = motion.get("verdict")
    ids: dict[str, str] = {}
    if motion.get("status") == "COMPLETED":
        s = (motion["chi2"].get("stationary_vs_linear") or {}).get("sigma_equivalent")
        ids["motion"] = ev.add("stationary_vs_linear", f"A constant-velocity model improves on a fixed position by {s:.1f} sigma-equivalent (2 extra parameters).",
                               value=s, unit="sigma", group="centroids", direction="motion", source="targets.motion")
        c = (motion["chi2"].get("catalogue") or {}).get("sigma_equivalent")
        ids["cat"] = ev.add("catalogue_chi2", f"The catalogue-propagated track leaves residuals of {c:.1f} sigma-equivalent.", value=c, unit="sigma",
                            group="centroids", direction="catalogue" if (c or 0) < 3 else "against_catalogue", source="targets.motion")
        fv, cv = motion["fitted_velocity_arcsec_yr"], motion["catalogue_velocity_arcsec_yr"]
        ids["vel"] = ev.add("fitted_vs_catalogue_velocity", f"Fitted angular velocity {fv['total']:.3f} arcsec/yr vs catalogue {cv['total']:.3f} arcsec/yr "
                            f"({motion['velocity_difference_pull_sigma']:.1f} sigma apart).", value=fv["total"], unit="arcsec/yr",
                            uncertainty=[fv["sigma_east"], fv["sigma_north"]], group="centroids", source="targets.motion")
        ids["disp"] = ev.add("expected_displacement", f"Catalogue displacement over the baseline {motion['expected_displacement_arcsec']:.2f} arcsec; "
                             f"its measurement uncertainty {motion['displacement_uncertainty_arcsec']:.2f} arcsec.", value=motion["expected_displacement_arcsec"],
                             unit="arcsec", uncertainty=motion["displacement_uncertainty_arcsec"], origin="INFERRED", group="centroids")
    null = motion.get("control_null_test", {})
    if null.get("status") == "COMPLETED":
        ids["null"] = ev.add("control_null", f"{null['n_controls']} stationary Gaia control stars give velocity-pull scatter {null['pull_robust_std']:.2f} "
                             f"(systematic floor {null['velocity_systematic_mas_yr']:.0f} mas/yr).", value=null["pull_robust_std"], unit="pull sigma", group="controls")
    cont = ctx.get("contamination_summary", {})
    if cont.get("n_with_neighbour"):
        ids["blend"] = ev.add("neighbour_in_stamp", f"{cont['n_with_neighbour']} images have a Gaia neighbour inside the PSF stamp (closest {cont['closest_arcsec']:.1f} arcsec).",
                              value=cont["closest_arcsec"], unit="arcsec", origin="CATALOGUE", group="catalogue_context")
    measurable = bool(motion.get("measurable"))
    not_assessable = motion.get("status") != "COMPLETED"
    hyps = []
    if not_assessable:
        hyps.append(_hyp(target, "insufficient_evidence", "Insufficient evidence", "SUPPORTED",
                         "The observations do not support a motion measurement: " + ", ".join(motion.get("reasons", [])),
                         tests_unavailable=[("linear motion fit", ", ".join(motion.get("reasons", [])))],
                         next_step="Retrieve more epochs with a longer baseline or exposures with unsaturated, converged centroids."))
        for slug, title in (("catalogue_motion", "Moving as the catalogue predicts"), ("stationary", "Stationary source"),
                            ("registration_artifact", "Registration or astrometric artefact")):
            hyps.append(_hyp(target, slug, title, "NOT_ASSESSABLE", "No motion measurement was possible.",
                             tests_unavailable=[("motion model comparison", "insufficient usable centroids/epochs/baseline")]))
        return hyps, ev.items, ids
    cat_ok = v == "MOTION_DETECTED_CONSISTENT_WITH_CATALOGUE"
    hyps.append(_hyp(target, "catalogue_motion", "Known source moving as its catalogue astrometry predicts",
                     "SUPPORTED" if cat_ok else ("DISFAVOURED" if v in ("MOTION_DETECTED_INCONSISTENT_WITH_CATALOGUE", "NO_SIGNIFICANT_MOTION_BUT_CATALOGUE_REJECTED") else "CANNOT_EXCLUDE"),
                     {"MOTION_DETECTED_CONSISTENT_WITH_CATALOGUE": "Motion is detected and the catalogue track reproduces the positions within tolerance.",
                      "MOTION_NOT_MEASURABLE_WITH_THESE_OBSERVATIONS": "The expected displacement is not larger than the measurement uncertainty, so the observations cannot test the catalogue motion."
                      }.get(v, f"Verdict: {v}."),
                     support=[ids[k] for k in ("motion", "cat", "vel") if k in ids] if cat_ok else [],
                     contra=[ids[k] for k in ("cat", "vel") if k in ids] if not cat_ok and v != "MOTION_NOT_MEASURABLE_WITH_THESE_OBSERVATIONS" else [],
                     tests_run=["catalogue chi2", "stationary vs linear motion", "fitted vs catalogue velocity"],
                     assumptions=["Gaia/CatWISE proper motion and parallax are correct", "linear motion over the baseline"],
                     limitations=["translation-only registration"], next_step="Repeat with additional epochs and unsaturated bands to tighten the velocity."))
    stationary_rejected = v in ("MOTION_DETECTED_CONSISTENT_WITH_CATALOGUE", "MOTION_DETECTED_INCONSISTENT_WITH_CATALOGUE")
    hyps.append(_hyp(target, "stationary", "Stationary source (no genuine motion)",
                     "DISFAVOURED" if stationary_rejected else ("CANNOT_EXCLUDE" if not measurable else "CONSISTENT"),
                     "A fixed position is rejected by the linear-motion comparison." if stationary_rejected
                     else ("The observations are not sensitive enough to exclude a fixed position." if not measurable
                           else "No significant motion despite measurability; stationary is consistent."),
                     contra=[ids["motion"]] if stationary_rejected and "motion" in ids else [], tests_run=["stationary vs linear motion"],
                     next_step="A longer baseline increases the lever arm for motion."))
    scale = motion.get("error_scale_applied", 1.0)
    sysm = null.get("velocity_systematic_mas_yr")
    big = sysm is not None and measurable and (sysm / 1e3) > 0.3 * motion["catalogue_velocity_arcsec_yr"]["total"]
    hyps.append(_hyp(target, "registration_artifact", "Apparent motion produced by registration/astrometric error",
                     "NOT_ASSESSABLE" if null.get("status") != "COMPLETED" else ("CANNOT_EXCLUDE" if big or scale > 1.5 else "DISFAVOURED"),
                     ("Stationary control stars measured in the same images show no comparable drift (pull scatter %.2f, systematic floor %.0f mas/yr)." % (
                         null.get("pull_robust_std", float("nan")), null.get("velocity_systematic_mas_yr", float("nan"))))
                     if null.get("status") == "COMPLETED" else "Too few control stars to calibrate registration systematics.",
                     contra=[ids["null"]] if "null" in ids and not big and scale <= 1.5 else [], tests_run=["leave-one-out control-star velocity null test"],
                     tests_unavailable=[] if null.get("status") == "COMPLETED" else [("control-star null test", null.get("reason", "unavailable"))],
                     assumptions=["controls are stationary after Gaia propagation"], limitations=["translation-only model"],
                     next_step="Add controls or per-image astrometric solutions if the null scatter is large."))
    blend = ids.get("blend")
    hyps.append(_hyp(target, "blend_confusion", "Centroid dominated by a blended neighbour or unresolved companion",
                     "CANNOT_EXCLUDE" if blend or ctx.get("unresolved_companion") else "DISFAVOURED",
                     ctx.get("blend_statement", "No catalogued neighbour lies inside the PSF stamp in the measured images."),
                     support=[blend] if blend else [], tests_run=["Gaia neighbours inside the PSF stamp"],
                     tests_unavailable=[("PSF deblending of a multi-component model", "components are unresolved at the instrument resolution")] if ctx.get("unresolved_companion") else [],
                     next_step="Compare with higher-resolution astrometry of the components."))
    hyps.append(_hyp(target, "uncatalogued_mover", "Possibly moving uncatalogued source", "DISFAVOURED" if ref.resolved else "NOT_ASSESSABLE",
                     "The target is a catalogued source; a catalogue identification exists, so no unexplained-source hypothesis is needed. (A missing match would not itself be evidence of a new object.)",
                     tests_run=["catalogue identification"], limitations=["applies to this target only; the blind pipeline run is reported separately"]))
    if v in ("MOTION_NOT_MEASURABLE_WITH_THESE_OBSERVATIONS", "NO_SIGNIFICANT_MOTION"):
        hyps.append(_hyp(target, "insufficient_evidence", "Insufficient evidence", "SUPPORTED", "Motion is not measurable with the selected observations.",
                         next_step="Use a longer baseline or more precise (brighter, unsaturated) epochs."))
    return hyps, ev.items, ids


def build_variable(target: str, ref: ReferenceAstrometry, var: dict, ctx: dict) -> tuple[list[dict], list[dict], dict]:
    ev = Evidence(target)
    ids: dict[str, str] = {}
    v = var.get("verdict")
    best = None
    for s in var.get("series", []):
        t = s["test"]
        if t["status"] == "COMPLETED" and (best is None or (t["sigma_equivalent"] or 0) > (best["test"]["sigma_equivalent"] or 0)):
            best = s
    if best:
        t = best["test"]
        ids["var"] = ev.add("most_significant_series", f"Series {best['series_id']} (detector {best['detector']}, {best['wavelength_um']:.3f} um, {best['n_points']} points, "
                            f"{best['n_epochs']} epochs): constant-flux chi2 {t['chi2']:.1f}/{t['dof']}, {t['sigma_equivalent']:.1f} sigma-equivalent after the field error scale.",
                            value=t["sigma_equivalent"], unit="sigma", group="forced_photometry", direction="variable", source="targets.variability")
        if t.get("empirical_false_alarm_fraction") is not None:
            ids["fa"] = ev.add("control_false_alarm", f"{t['empirical_false_alarm_fraction']:.2f} of {t['n_control_series']} Gaia control-star series have a reduced chi2 at least this large.",
                               value=t["empirical_false_alarm_fraction"], group="controls", direction="constant" if t["empirical_false_alarm_fraction"] > 0.05 else "variable")
    cr = var.get("control_reference", {}).get("error_model", {})
    if cr.get("status") == "COMPLETED":
        ids["scale"] = ev.add("field_error_scale", f"Control-star ensemble implies the formal flux errors are underestimated by x{cr['scale']:.2f}.", value=cr["scale"], group="controls")
    if var.get("caveats"):
        ids["caveats"] = ev.add("caveats", "; ".join(var["caveats"]), origin="INFERRED", group="quality")
    detected = v in ("VARIABILITY_DETECTED", "VARIABILITY_DETECTED_WITH_CAVEATS")
    hyps = []
    if v == "NOT_ASSESSABLE":
        hyps.append(_hyp(target, "insufficient_evidence", "Insufficient evidence", "SUPPORTED", var.get("verdict_reason", ""),
                         tests_unavailable=[("constant-flux chi2 per comparable series", var.get("verdict_reason", ""))],
                         next_step="Obtain more epochs in the same detector/wavelength band, or unsaturated measurements."))
        for slug, title in (("intrinsic_variability", "Intrinsic variability"), ("constant_source", "Constant source"),
                            ("calibration_artifact", "Calibration or pipeline artefact")):
            hyps.append(_hyp(target, slug, title, "NOT_ASSESSABLE", "No comparable series with enough points and epochs."))
        return hyps, ev.items, ids
    hyps.append(_hyp(target, "intrinsic_variability", "Intrinsic variability of the source",
                     "SUPPORTED" if v == "VARIABILITY_DETECTED" else ("CONSISTENT" if detected else ("CANNOT_EXCLUDE" if v.startswith("SIGNIFICANT") else "DISFAVOURED")),
                     var.get("verdict_reason", ""), support=[ids[k] for k in ("var", "fa") if k in ids] if detected else [],
                     contra=[ids[k] for k in ("var", "fa") if k in ids] if v == "NO_SIGNIFICANT_VARIABILITY" else [],
                     tests_run=["constant-flux chi2 per comparable series", "control-star false-alarm comparison", "straight-line trend", "cross-epoch spectral comparison"],
                     tests_unavailable=[("period search", var["period_assessment"]["reason"])], assumptions=["forced PSF photometry at the catalogue position is unbiased"],
                     limitations=["few epochs; wavelength sampling varies by exposure"], next_step="Add epochs at matched wavelengths; compare with an independent photometric monitor."))
    art_status = "CANNOT_EXCLUDE" if (var.get("caveats") or v.startswith("SIGNIFICANT")) else "DISFAVOURED"
    hyps.append(_hyp(target, "calibration_artifact", "Calibration, pipeline-version or bandpass differences mimic variability", art_status,
                     "Caveats remain: " + "; ".join(var["caveats"]) if var.get("caveats") else "Series are matched in detector, release and wavelength and the control stars do not show comparable scatter.",
                     support=[ids["caveats"]] if "caveats" in ids else [], contra=[ids["fa"]] if "fa" in ids and not var.get("caveats") else [],
                     tests_run=["comparable-series construction", "control-star ensemble"], tests_unavailable=[("absolute calibration comparison across releases", "not modelled")]))
    blend = any(c for c in var.get("caveats", []) if "neighbour" in c)
    hyps.append(_hyp(target, "blend_contamination", "Variability of a blended or contaminating neighbour", "CANNOT_EXCLUDE" if blend or ctx.get("unresolved_companion") else "DISFAVOURED",
                     ctx.get("blend_statement", "No bright catalogued neighbour in the PSF stamp."), tests_run=["Gaia neighbours inside the PSF stamp"],
                     tests_unavailable=[("deblended photometry", "neighbours are not resolved at SPHEREx resolution")] if blend or ctx.get("unresolved_companion") else []))
    hyps.append(_hyp(target, "constant_source", "Constant source within the measurement precision", "SUPPORTED" if v == "NO_SIGNIFICANT_VARIABILITY" else ("DISFAVOURED" if detected else "CANNOT_EXCLUDE"),
                     "No series departs from a constant flux at the threshold." if v == "NO_SIGNIFICANT_VARIABILITY" else "See the variability tests.",
                     limitations=["sensitivity is limited by the flux errors; see per-series sigma"]))
    if v == "NO_SIGNIFICANT_VARIABILITY" or not detected:
        hyps.append(_hyp(target, "insufficient_evidence", "Insufficient evidence", "CONSISTENT", "Sampling is sparse; absence of detected variability is not a measurement of constancy."))
    return hyps, ev.items, ids


def plain_language(spec_name: str, category: str, ref: ReferenceAstrometry, analysis: dict, coverage: dict, test_status: str) -> dict:
    q = []
    if not ref.resolved:
        q.append(f"HEIMDALL could not identify {spec_name} in the reference catalogue, so no measurement was attempted.")
    elif coverage.get("n_measured", 0) == 0:
        q.append(f"SPHEREx observed the region {coverage.get('n_discovered', 0)} times but no usable measurement of {spec_name} could be made.")
    else:
        v = analysis.get("verdict")
        q.append({
            "MOTION_DETECTED_CONSISTENT_WITH_CATALOGUE": f"{spec_name} moved across the SPHEREx images by about the amount its catalogue motion predicts.",
            "MOTION_DETECTED_INCONSISTENT_WITH_CATALOGUE": f"{spec_name} moved, but not as the catalogue predicts; instrumental and blending explanations are not yet excluded.",
            "MOTION_NOT_MEASURABLE_WITH_THESE_OBSERVATIONS": f"These SPHEREx images are not precise enough, over this time span, to measure the motion of {spec_name}.",
            "NO_SIGNIFICANT_MOTION": f"No significant motion of {spec_name} was measured.",
            "NO_SIGNIFICANT_MOTION_BUT_CATALOGUE_REJECTED": f"No motion was measured for {spec_name} although the catalogue says it should have moved; this needs checking.",
            "VARIABILITY_DETECTED": f"The brightness of {spec_name} changed between SPHEREx observations beyond what the measurement noise and comparison stars explain.",
            "VARIABILITY_DETECTED_WITH_CAVEATS": f"The brightness of {spec_name} appears to change, but caveats (see evidence) mean artefacts are not excluded.",
            "SIGNIFICANT_BUT_NOT_DISTINGUISHABLE_FROM_FIELD_SYSTEMATICS": f"{spec_name} shows formal variation, but comparison stars show similar scatter, so it is not distinguished from instrument effects.",
            "NO_SIGNIFICANT_VARIABILITY": f"No significant brightness change of {spec_name} was found in the selected observations.",
            "NOT_ASSESSABLE": f"The selected observations do not allow a valid test for {spec_name}."}.get(v, f"Result: {v}."))
    q.append("This is a measurement within stated limits, not a new finding.")
    return {"quick_answer": " ".join(q), "test_status": test_status, "deeper": "See hypotheses, evidence, tests run and unavailable tests for the underlying numbers."}
