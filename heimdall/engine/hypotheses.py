"""Versioned catalogue of hypotheses and the status rules that evaluate them against evidence.

A hypothesis declares which observed patterns trigger it, its predictions (explicit functions returning SUPPORTS, CONTRADICTS, NEUTRAL or
NOT_ASSESSABLE), which predictions are required or decisive, its competitors, falsifiers and follow-up observations. Instrumental and
ordinary explanations already assessed by ``reasoning.assess`` are *re-expressed* from the candidate's alternatives, never recomputed.

Statuses: SUPPORTED (only identification-type hypotheses, with independent evidence), CONSISTENT (not contradicted; NOT support),
UNRESOLVED (conflicting evidence), CONTRADICTED, NOT_ASSESSABLE (a required prediction cannot be evaluated, or the pattern is not
established). Missing or incomplete evidence is never turned into a contradiction. No probabilities or scores exist here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

from .evidence import STAR_MAX_PARALLAX_ARCSEC, EvidenceItem
from .reasoning import check_language

HYPOTHESIS_CATALOGUE_VERSION = "hypotheses-1.0.0-provisional"
SUPPORTS, CONTRADICTS, NEUTRAL, NA = "SUPPORTS", "CONTRADICTS", "NEUTRAL", "NOT_ASSESSABLE"
FLUX_PATTERNS = ("FLUX_CHANGE", "SINGLE_EPOCH")
MOTION_PATTERNS = ("POSITION_CHANGE", "LINKED_MOTION")
INSTRUMENTAL_IDS = frozenset({
    "image_artefact", "saturation_or_nonlinearity", "blending", "misassociation", "registration_error", "astrometric_errors_underestimated",
    "photometric_errors_underestimated", "noise_underestimated", "calibration_release_difference", "pipeline_version_difference",
    "single_image_systematic", "single_measurement_outlier", "statistical_fluctuation", "chance_linkage", "spectral_slope_within_tolerance"})
ORDINARY_IDS = {"catalogued_star": "CATALOGUED_STAR_IDENTITY", "apparent_motion_contradicted_by_catalogue": "MOTION_CONTRADICTED_BY_CATALOGUE"}
ALT_TO_STATUS = {"SUPPORTED": "SUPPORTED", "RULED_OUT": "CONTRADICTED", "PLAUSIBLE_UNTESTED": "CONSISTENT", "NOT_ASSESSABLE": "NOT_ASSESSABLE"}
PX_STATUS = {"INCONSISTENT": "CONTRADICTED", "NOT_ASSESSABLE": "NOT_ASSESSABLE", "UNCONSTRAINED": "NOT_ASSESSABLE", "CONSISTENT_REQUIRES_CONFIRMATION": "CONSISTENT"}
NON_DECISIVE_PX = {"independent_confirmation", "validated_significance", "brightness_consistent", "measurement_reliable"}
ORDINARY_FAMILIES = ("INSTRUMENTAL", "ORDINARY", "KNOWN_OBJECT")


@dataclass
class PredResult:
    result: str
    evidence_ids: list[str] = field(default_factory=list)
    reason: str = ""
    missing_dependency: str | None = None


@dataclass
class Prediction:
    pred_id: str
    description: str
    evaluate: Callable[["Ctx"], PredResult]
    required: bool = False
    decisive: bool = False
    discriminates_against: tuple[str, ...] = ()


@dataclass
class HypothesisSpec:
    hyp_id: str
    family: str  # KNOWN_OBJECT | ORDINARY | ASTROPHYSICAL | HYPOTHESIS_OF_INTEREST
    statement: str
    triggers: tuple[str, ...]
    predictions: tuple[Prediction, ...]
    competitors: tuple[str, ...] = ()
    falsifiers: tuple[str, ...] = ()
    follow_ups: tuple[dict, ...] = ()
    max_status: str = "CONSISTENT"
    when: Callable[["Ctx"], bool] | None = None


@dataclass
class Ctx:
    """Everything a prediction may look at for one subject."""
    subject_kind: str
    subject_id: str
    patterns: list[dict]  # {pattern, candidate_id, admissible, reasons, attention, candidate}
    evidence: dict[str, list[EvidenceItem]]
    group_of: dict[str, str]
    source: dict | None
    analyses: dict
    r_min_au: float

    def ev(self, typ: str) -> EvidenceItem | None:
        items = self.evidence.get(typ) or []
        return items[0] if items else None

    def alt_state(self, alt_id: str, candidate_ids=None) -> str | None:
        for p in self.patterns:
            if candidate_ids is not None and p["candidate_id"] not in candidate_ids:
                continue
            for a in p["candidate"]["candidate"]["alternatives"]:
                if a["alt_id"] == alt_id:
                    return a["state"]
        return None

    def analysis_reason(self, name: str) -> str:
        a = (self.analyses or {}).get(name) or {}
        return f"{name} analysis {a.get('status', 'not available')}: {'; '.join(a.get('reasons') or []) or 'no result'}"


# ---------------------------------------------------------------- prediction functions ----------

def _na(reason: str, missing: str | None = None) -> PredResult:
    return PredResult(NA, [], reason, missing)


def _star_significant(v: dict) -> bool:
    pm, pe, plx, pxe = v.get("pm_mas_yr") or [None, None], v.get("pm_error_mas_yr") or [None, None], v.get("parallax_mas"), v.get("parallax_error_mas")
    big_pm = all(x is not None for x in (*pm, *pe)) and pe[0] and pe[1] and ((pm[0] / pe[0]) ** 2 + (pm[1] / pe[1]) ** 2) ** 0.5 >= 3.0
    big_plx = plx is not None and pxe and plx / pxe >= 3.0
    return bool(big_pm or big_plx)


def p_jpl_identification(ctx: Ctx) -> PredResult:
    it = ctx.ev("KNOWN_SMALL_BODY")
    if it is None:
        return _na("no known-object query result for this subject (context stage not run or not applicable)", "JPL SB Ident query")
    if not it.admissible:
        return _na(it.not_assessable_reason or "known-object query did not complete", "JPL SB Ident query")
    st, ids = it.value["status"], [it.evidence_id]
    if st == "CONFIRMED_MATCH":
        return PredResult(SUPPORTS, ids, f"{it.value.get('name')} matches the predicted position in every detection image")
    if st == "PROBABLE_MATCH":
        return PredResult(NEUTRAL, ids, it.value.get("note") or "a known body matches one image only; identity not established")
    return PredResult(CONTRADICTS, ids, it.value.get("reason") or "no known body predicted within tolerance")


def p_no_known_body(ctx: Ctx) -> PredResult:
    it = ctx.ev("KNOWN_SMALL_BODY")
    if it is None or not it.admissible:
        return _na((it.not_assessable_reason if it else None) or "no completed known-object query", "JPL SB Ident query")
    st, ids = it.value["status"], [it.evidence_id]
    if st == "CONFIRMED_MATCH":
        return PredResult(CONTRADICTS, ids, f"identified as {it.value.get('name')}")
    if st == "EXCLUDED_BY_MOTION":
        return PredResult(CONTRADICTS, ids, "fixed within the association gate over the epoch gap: no inner-system body moves so little")
    if st == "PROBABLE_MATCH":
        return PredResult(NEUTRAL, ids, "a known body matches part of the detections; the rest is unexplained")
    return PredResult(NEUTRAL, ids, "no known body predicted within tolerance; this does not establish an unknown body (bodies fainter than V = 21 or "
                                    "without magnitude parameters are not queried)")


def p_rate_above_bound(ctx: Ctx) -> PredResult:
    it = ctx.ev("TRACKLET_RATE")
    if it is not None:
        return PredResult(SUPPORTS, [it.evidence_id], it.value["reason"])
    px = ctx.ev("PARALLAX")
    iv = px.value.get("distance_interval_au") if px is not None else None
    if iv and iv[0] is not None and iv[1] is not None:
        if iv[1] < ctx.r_min_au:
            return PredResult(SUPPORTS, [px.evidence_id], f"parallax distance interval {iv} AU lies inside r_min = {ctx.r_min_au:g} AU")
        if iv[0] >= ctx.r_min_au:
            return PredResult(CONTRADICTS, [px.evidence_id], f"parallax distance interval {iv} AU lies beyond r_min = {ctx.r_min_au:g} AU")
        return PredResult(NEUTRAL, [px.evidence_id], f"parallax distance interval {iv} AU straddles r_min = {ctx.r_min_au:g} AU")
    if px is not None:
        return PredResult(NEUTRAL, [px.evidence_id], "parallax gives no bounded distance interval")
    return _na("no apparent-rate or distance estimate for this subject", "tracklet rate or parallax distance")


def p_loo_survival(ctx: Ctx) -> PredResult:
    st = ctx.alt_state("single_measurement_outlier")
    it = ctx.ev("FLUX_CHANGE")
    ids = [it.evidence_id] if it else []
    return {"RULED_OUT": PredResult(SUPPORTS, ids, "the excess survives removal of any single measurement"),
            "SUPPORTED": PredResult(CONTRADICTS, ids, "the excess is driven by a single measurement"),
            "PLAUSIBLE_UNTESTED": PredResult(NEUTRAL, ids, "single-measurement dependence not excluded")}.get(st or "", _na("leave-one-out test not available"))


def p_gaia_counterpart(ctx: Ctx) -> PredResult:
    it = ctx.ev("CATALOGUE_STAR")
    if it is None or not it.admissible:
        return _na((it.not_assessable_reason if it else None) or "no completed Gaia query", "Gaia DR3 cone query")
    if it.value["status"] == "MATCH":
        return PredResult(SUPPORTS, [it.evidence_id], f"coincides with Gaia DR3 {it.value['gaia_source_id']} (G = {it.value.get('g_mag')})")
    return PredResult(NEUTRAL, [it.evidence_id], "no Gaia DR3 star within the gate; Gaia is incomplete near G = 21, so this does not exclude a faint star")


def p_stellar_astrometry(ctx: Ctx) -> PredResult:
    px = ctx.ev("PARALLAX")
    if px is None:
        return _na(ctx.analysis_reason("parallax") if ctx.source else "no parallax measurement", "parallax fit with >= 3 positions in >= 2 epochs")
    v, sg = px.value["parallax_arcsec"], (px.uncertainty or {}).get("parallax_sigma_arcsec") or 0.0
    if v - 3 * sg > STAR_MAX_PARALLAX_ARCSEC:
        return PredResult(CONTRADICTS, [px.evidence_id], "parallax exceeds 1 arcsec at 3 sigma: larger than any star's")
    return PredResult(NEUTRAL, [px.evidence_id], "parallax does not exclude a star")


def p_extragalactic_astrometry(ctx: Ctx) -> PredResult:
    ids, seen = [], False
    px = ctx.ev("PARALLAX")
    if px is not None:
        seen = True
        v, sg = px.value["parallax_arcsec"], (px.uncertainty or {}).get("parallax_sigma_arcsec") or 0.0
        if sg > 0 and v / sg >= 3.0 and v > 0:
            return PredResult(CONTRADICTS, [px.evidence_id], f"significant parallax ({v:.2f} arcsec): not extragalactic")
        ids.append(px.evidence_id)
    star = ctx.ev("CATALOGUE_STAR")
    if star is not None and star.admissible and star.value["status"] == "MATCH":
        seen = True
        if _star_significant(star.value):
            return PredResult(CONTRADICTS, [star.evidence_id], "the Gaia counterpart has a significant proper motion or parallax")
        ids.append(star.evidence_id)
    if not seen:
        return _na("neither a parallax nor a Gaia astrometric measurement is available", "parallax fit or Gaia match")
    return PredResult(NEUTRAL, ids, "no significant parallax or proper motion measured; this does not identify the source as extragalactic")


def p_transient_excess(ctx: Ctx) -> PredResult:
    it = ctx.ev("SINGLE_EPOCH_EXCESS")
    if it is None:
        return _na("no single-epoch excess measurement")
    st = it.value["status"]
    if st == "TRANSIENT_LIKE":
        return PredResult(SUPPORTS, [it.evidence_id], f"flux excess over {it.value.get('n_references')} comparable reference image(s) (z = {it.value.get('z'):.1f})")
    if st == "NOT_TRANSIENT":
        return PredResult(CONTRADICTS, [it.evidence_id], "no significant excess over comparable references")
    return _na(it.not_assessable_reason or "no comparable reference image", "comparable-wavelength reference images")


def p_not_moving(ctx: Ctx) -> PredResult:
    ids = []
    if ctx.alt_state("moving_object") == "SUPPORTED":
        return PredResult(CONTRADICTS, ids, "linked into a moving-object track")
    it = ctx.ev("TRACKLET_RATE")
    if it is not None:
        return PredResult(CONTRADICTS, [it.evidence_id], "its apparent rate is that of a nearby moving body")
    ko = ctx.ev("KNOWN_SMALL_BODY")
    if ko is not None and ko.admissible and ko.value["status"] == "CONFIRMED_MATCH":
        return PredResult(CONTRADICTS, [ko.evidence_id], f"identified as {ko.value.get('name')}")
    return PredResult(NEUTRAL, [], "no evidence that it moves")


def p_no_catalogued_star(ctx: Ctx) -> PredResult:
    it = ctx.ev("CATALOGUE_STAR")
    if it is None or not it.admissible:
        return _na((it.not_assessable_reason if it else None) or "no completed Gaia query", "Gaia DR3 cone query")
    if it.value["status"] == "MATCH":
        return PredResult(CONTRADICTS, [it.evidence_id], "coincides with a catalogued Gaia star: a known source, not a newly appearing one")
    return PredResult(NEUTRAL, [it.evidence_id], "no Gaia star within the gate")


def p_within_epoch_evolution(ctx: Ctx) -> PredResult:
    return _na("no within-epoch light-curve analysis exists", "within-epoch light curve")


def p_gaia_pm_consistent(ctx: Ctx) -> PredResult:
    it = ctx.ev("CATALOGUE_STAR")
    ids = [it.evidence_id] if it else []
    if ctx.alt_state("high_proper_motion_star") == "SUPPORTED":
        return PredResult(SUPPORTS, ids, "the apparent motion matches the Gaia DR3 proper motion of the star at this position")
    if ctx.alt_state("apparent_motion_contradicted_by_catalogue") == "SUPPORTED":
        return PredResult(CONTRADICTS, ids, "the star at this position is effectively stationary in Gaia DR3; the apparent motion is a measurement effect")
    if ctx.alt_state("high_proper_motion_star") == "RULED_OUT":
        return PredResult(CONTRADICTS, ids, "parallax exceeds that of any star")
    if it is None or not it.admissible:
        return _na((it.not_assessable_reason if it else None) or "no completed Gaia query", "Gaia DR3 cone query")
    return PredResult(NEUTRAL, ids, "no Gaia star with consistent proper motion")


def p_spectrum_multi_detector(ctx: Ctx) -> PredResult:
    it = ctx.ev("SPECTRAL_SHAPE")
    if it is None:
        return _na("no spectrum assembled for this subject", "spectrum")
    det, ws = it.value.get("detectors_measured") or [], it.value.get("wavelength_sources") or []
    if len(det) < 2:
        return PredResult(NA, [it.evidence_id], f"{len(det)} detector(s) measured ({det}); a spectral shape needs at least two", "measurements in >= 2 SPHEREx detectors")
    if "CWAVE_CBAND" not in ws:
        return PredResult(NA, [it.evidence_id], f"wavelengths come from {ws}; calibrated CWAVE/CBAND wavelengths are required", "CWAVE/CBAND spectral calibration")
    return PredResult(NEUTRAL, [it.evidence_id], "multi-detector calibrated spectrum available")


def p_photospheric_reference(ctx: Ctx) -> PredResult:
    return _na("no validated photospheric or template reference is configured; temperature and composition cannot be inferred from flux or band labels",
               "photospheric model grid with SPHEREx band-response integration and an absolute-flux calibration mapping (see docs/THERMAL_REFERENCE_FEASIBILITY.md)")


def _has_spectrum(ctx: Ctx) -> bool:
    return ctx.ev("SPECTRAL_SHAPE") is not None and any(p["pattern"] in FLUX_PATTERNS for p in ctx.patterns)


# ---------------------------------------------------------------- the catalogue ----------

def _fu(action, data, distinguishes, feasible=False, dependency=None):
    return {"action": action, "data_needed": data, "distinguishes": list(distinguishes), "feasible_in_heimdall": feasible, "dependency": dependency}


CATALOGUE: tuple[HypothesisSpec, ...] = (
    HypothesisSpec("KNOWN_SMALL_BODY", "KNOWN_OBJECT", "The signal is a known asteroid, comet or trans-Neptunian object at its predicted position.",
                   ("SINGLE_EPOCH", "POSITION_CHANGE", "LINKED_MOTION"),
                   (Prediction("jpl_identification", "a known body is predicted within tolerance in every detection image", p_jpl_identification, True, True),),
                   ("UNKNOWN_INNER_SOLAR_SYSTEM_BODY", "PLANET_X_SCENARIO"),
                   ("a completed JPL query finds no body within tolerance", "the match covers only part of the detections"),
                   (_fu("rerun the SB Identification query including bodies fainter than V = 21 and comets", "JPL SB Ident", ["KNOWN_SMALL_BODY", "UNKNOWN_INNER_SOLAR_SYSTEM_BODY"], True),),
                   max_status="SUPPORTED"),
    HypothesisSpec("HIGH_PROPER_MOTION_STAR", "ORDINARY", "The apparent motion is the proper motion of a catalogued nearby star.", ("POSITION_CHANGE",),
                   (Prediction("gaia_pm_consistent", "a Gaia DR3 star at this position has a consistent proper motion", p_gaia_pm_consistent, True, True),),
                   ("UNKNOWN_INNER_SOLAR_SYSTEM_BODY", "PLANET_X_SCENARIO"), ("the Gaia proper motion disagrees with the measured motion", "parallax above any star's"),
                   (_fu("compare the Gaia DR3 proper motion with the measured motion", "Gaia DR3", ["HIGH_PROPER_MOTION_STAR"], True),), max_status="SUPPORTED"),
    HypothesisSpec("STELLAR_VARIABILITY", "ASTROPHYSICAL", "The source is a star whose flux changes with time.", ("FLUX_CHANGE",),
                   (Prediction("excess_survives_leave_one_out", "the flux excess does not depend on a single measurement", p_loo_survival, True, True),
                    Prediction("gaia_star_counterpart", "a Gaia DR3 star coincides with the source", p_gaia_counterpart, False, False, ("EXTRAGALACTIC_VARIABLE",)),
                    Prediction("astrometry_allows_a_star", "the parallax does not exceed any star's", p_stellar_astrometry, False, True)),
                   ("EXTRAGALACTIC_VARIABLE", "ASTROPHYSICAL_TRANSIENT"), ("a significant parallax above 1 arcsec", "the flux excess is driven by one measurement"),
                   (_fu("observe at more epochs at the same wavelength to measure the variability timescale", "additional SPHEREx epochs", ["STELLAR_VARIABILITY", "EXTRAGALACTIC_VARIABLE"]),
                    _fu("check Gaia variability tables for the counterpart", "Gaia DR3 variability tables", ["STELLAR_VARIABILITY"], False, "Gaia variability tables are not configured")),
                   ),
    HypothesisSpec("EXTRAGALACTIC_VARIABLE", "ASTROPHYSICAL", "The source is an extragalactic object (for example an active nucleus) whose flux changes with time.", ("FLUX_CHANGE",),
                   (Prediction("excess_survives_leave_one_out", "the flux excess does not depend on a single measurement", p_loo_survival, True, True),
                    Prediction("astrometry_allows_extragalactic", "no significant parallax or proper motion", p_extragalactic_astrometry, True, True, ("STELLAR_VARIABILITY",))),
                   ("STELLAR_VARIABILITY",), ("a significant parallax or proper motion", "a Gaia counterpart with significant proper motion"),
                   (_fu("obtain morphology or spectroscopy to separate a faint star from an extragalactic source", "imaging or spectroscopy", ["EXTRAGALACTIC_VARIABLE", "STELLAR_VARIABILITY"], False, "no morphology or spectroscopy in HEIMDALL"),)),
    HypothesisSpec("ASTROPHYSICAL_TRANSIENT", "ASTROPHYSICAL", "A source appeared in one epoch and is absent at a comparable wavelength elsewhere (an eruptive or explosive event, class unspecified).", ("SINGLE_EPOCH",),
                   (Prediction("excess_over_references", "the source is brighter than at comparable wavelength in other images", p_transient_excess, True, True),
                    Prediction("not_a_moving_object", "no evidence that the source moves", p_not_moving, False, True, ("UNKNOWN_INNER_SOLAR_SYSTEM_BODY", "KNOWN_SMALL_BODY")),
                    Prediction("no_catalogued_star", "no catalogued star at the position", p_no_catalogued_star, False, True, ("STELLAR_VARIABILITY",)),
                    Prediction("evolution_within_epoch", "the brightness evolves within the epoch as an event would", p_within_epoch_evolution, False, False, ("STELLAR_VARIABILITY",))),
                   ("STELLAR_VARIABILITY", "UNKNOWN_INNER_SOLAR_SYSTEM_BODY", "KNOWN_SMALL_BODY"), ("a moving-object link", "a catalogued star at the position"),
                   (_fu("re-observe the position at the same wavelength", "new SPHEREx or other imaging", ["ASTROPHYSICAL_TRANSIENT", "STELLAR_VARIABILITY"]),
                    _fu("within-epoch light curve", "within-epoch light-curve analysis", ["ASTROPHYSICAL_TRANSIENT"], False, "not implemented"))),
    HypothesisSpec("UNKNOWN_INNER_SOLAR_SYSTEM_BODY", "ASTROPHYSICAL", "The signal is a solar-system body inside r_min that is not in the queried small-body catalogue.", ("SINGLE_EPOCH", "POSITION_CHANGE", "LINKED_MOTION"),
                   (Prediction("no_known_body", "no known body is predicted at the position", p_no_known_body, True, True),
                    Prediction("rate_above_distant_bound", "the apparent rate or distance is that of a body inside r_min", p_rate_above_bound, False, True, ("PLANET_X_SCENARIO",))),
                   ("KNOWN_SMALL_BODY", "PLANET_X_SCENARIO"), ("an identification with a known body", "a source fixed over the epoch gap"),
                   (_fu("obtain more epochs to determine an orbit", "additional astrometry", ["UNKNOWN_INNER_SOLAR_SYSTEM_BODY", "PLANET_X_SCENARIO"]),
                    _fu("query fainter bodies and comets", "JPL SB Ident without the V limit", ["KNOWN_SMALL_BODY"], True))),
    HypothesisSpec("THERMAL_DUST_OR_RED_SPECTRUM", "ASTROPHYSICAL", "The source shows emission beyond a stellar photosphere (thermal or dust-related).", (),
                   (Prediction("calibrated_multi_detector_spectrum", "a calibrated spectrum spans at least two SPHEREx detectors", p_spectrum_multi_detector, True),
                    Prediction("photospheric_reference", "a photospheric or template reference exists to compare against", p_photospheric_reference, True)),
                   ("STELLAR_VARIABILITY",), ("a spectrum consistent with a photosphere against a validated reference",),
                   (_fu("add a validated photospheric reference and multi-detector calibrated spectra", "photospheric model grid and calibration mapping; Gaia GSP-Phot Teff exists only for G < 19 (docs/THERMAL_REFERENCE_FEASIBILITY.md)", ["THERMAL_DUST_OR_RED_SPECTRUM"], False, "no reference configured"),),
                   when=_has_spectrum),
)
CATALOGUE_BY_ID = {h.hyp_id: h for h in CATALOGUE}


# ---------------------------------------------------------------- evaluation ----------

def _row(spec_id, p: Prediction, r: PredResult, ctx: Ctx) -> dict:
    groups = sorted({ctx.group_of[e] for e in r.evidence_ids if e in ctx.group_of})
    return {"prediction_id": p.pred_id, "description": p.description, "result": r.result, "required": p.required, "decisive": p.decisive,
            "discriminates_against": list(p.discriminates_against), "evidence_ids": r.evidence_ids, "group_ids": groups, "reason": r.reason,
            "not_assessable_reason": r.reason if r.result == NA else None, "missing_dependency": r.missing_dependency}


def apply_status_rules(rows: list[dict], *, max_status: str, gate_open: bool, gate_reasons: list[str], gated: bool) -> tuple[str, str]:
    """(status, rule) from prediction rows. Conservative: absence of evidence is never a contradiction."""
    contra_decisive = [r for r in rows if r["result"] == CONTRADICTS and r["decisive"]]
    if contra_decisive:
        return "CONTRADICTED", f"decisive prediction contradicted: {contra_decisive[0]['prediction_id']}"
    if gated and not gate_open:
        return "NOT_ASSESSABLE", "the triggering pattern is not established: " + "; ".join(gate_reasons)
    required_na = [r for r in rows if r["required"] and r["result"] == NA]
    contra = [r for r in rows if r["result"] == CONTRADICTS]
    supp = [r for r in rows if r["result"] == SUPPORTS]
    if contra and supp:
        return "UNRESOLVED", "supporting and contradicting predictions both present"
    if contra:
        return "UNRESOLVED", f"non-decisive prediction contradicted: {contra[0]['prediction_id']}"
    if required_na:
        return "NOT_ASSESSABLE", f"required prediction not assessable: {required_na[0]['prediction_id']}"
    required = [r for r in rows if r["required"]]
    discr_ok = all(r["result"] != NA for r in rows if r["discriminates_against"])
    if max_status == "SUPPORTED" and required and all(r["result"] == SUPPORTS for r in required) and discr_ok and all(r["group_ids"] for r in required):
        return "SUPPORTED", "all required predictions supported by independent evidence; no contradiction"
    return "CONSISTENT", "not contradicted under the assessed conditions; this is not support"


def evaluate_spec(spec: HypothesisSpec, ctx: Ctx, trigger_patterns: list[dict]) -> dict:
    rows = [_row(spec.hyp_id, p, p.evaluate(ctx), ctx) for p in spec.predictions]
    gate_open = any(t["admissible"] for t in trigger_patterns) if trigger_patterns else True
    gate_reasons = sorted({r for t in trigger_patterns for r in t["reasons"]})
    gated = spec.family == "ASTROPHYSICAL"
    status, rule = apply_status_rules(rows, max_status=spec.max_status, gate_open=gate_open, gate_reasons=gate_reasons, gated=gated)
    sup = {g for r in rows if r["result"] == SUPPORTS for g in r["group_ids"]}
    con = {g for r in rows if r["result"] == CONTRADICTS for g in r["group_ids"]}
    open_ = status in ("CONSISTENT", "UNRESOLVED", "NOT_ASSESSABLE")
    return {"hypothesis_id": spec.hyp_id, "family": spec.family, "statement": check_language(spec.statement), "status": status, "status_rule": rule,
            "applies_to_candidates": sorted({t["candidate_id"] for t in trigger_patterns}), "predictions": rows,
            "n_supporting_groups": len(sup), "n_contradicting_groups": len(con),
            "assumptions": ["the pattern's quality gate is open" if gated else "evidence admissibility as recorded"],
            "competitors": list(spec.competitors), "falsifiers": list(spec.falsifiers),
            "follow_ups": [dict(f) for f in spec.follow_ups] if open_ else [], "max_status": spec.max_status}


def from_alternative(alt: dict, cand: dict) -> dict:
    """Re-express an alternative already assessed by reasoning.assess (never recomputed)."""
    hid = ORDINARY_IDS.get(alt["alt_id"], alt["alt_id"].upper())
    family = "ORDINARY" if alt["alt_id"] in ORDINARY_IDS else "INSTRUMENTAL"
    status = ALT_TO_STATUS[alt["state"]]
    refs = [f"{e['kind']}:{e['ref_id']}" for e in alt.get("evidence") or []]
    return {"hypothesis_id": hid, "family": family, "statement": check_language(alt["description"][0].upper() + alt["description"][1:] + "."),
            "status": status, "status_rule": f"existing assessment {alt['state']} (reasoning rules)", "applies_to_candidates": [cand["candidate_id"]],
            "predictions": [{"prediction_id": "existing_alternative_assessment", "description": alt["description"], "result":
                             {"SUPPORTED": SUPPORTS, "RULED_OUT": CONTRADICTS, "PLAUSIBLE_UNTESTED": NEUTRAL, "NOT_ASSESSABLE": NA}[alt["state"]],
                             "required": True, "decisive": True, "discriminates_against": [], "evidence_ids": [], "group_ids": [], "reason": "", "evidence_refs": refs,
                             "not_assessable_reason": "the check backing this alternative could not be assessed" if alt["state"] == "NOT_ASSESSABLE" else None,
                             "missing_dependency": None}],
            "n_supporting_groups": 0, "n_contradicting_groups": 0, "assumptions": [], "competitors": [], "falsifiers": ["the check that rules it out fails"],
            "follow_ups": [], "max_status": "SUPPORTED", "evidence_refs": refs}


def planet_x_hypothesis(cand: dict, ctx: Ctx) -> dict | None:
    """The existing Planet X evaluation, wrapped as one hypothesis. Never a probability."""
    px = cand.get("planet_x")
    if not px or not px.get("applicable"):
        return None
    rows = []
    for r in px["requirements"]:
        res = {"SATISFIED": SUPPORTS, "NOT_SATISFIED": CONTRADICTS, "NOT_ASSESSABLE": NA}[r["state"]]
        decisive = res == CONTRADICTS and r["req_id"] not in NON_DECISIVE_PX
        if res == CONTRADICTS and not decisive:
            res = NEUTRAL  # unmet confirmation or validation requirements do not contradict the scenario
        rows.append({"prediction_id": r["req_id"], "description": r["description"], "result": res, "required": False, "decisive": decisive,
                     "discriminates_against": [], "evidence_ids": [], "group_ids": [], "reason": r["detail"], "evidence_refs": r.get("evidence") or [],
                     "not_assessable_reason": r["detail"] if res == NA else None, "missing_dependency": None})
    status = PX_STATUS[px["status"]]
    return {"hypothesis_id": "PLANET_X_SCENARIO", "family": "HYPOTHESIS_OF_INTEREST", "statement": check_language(px["hypothesis"]), "status": status,
            "status_rule": f"planetx rules: {px['status']} (a status of consistency with the stated scenario, never a probability)",
            "applies_to_candidates": [cand["candidate_id"]], "predictions": rows, "n_supporting_groups": 0, "n_contradicting_groups": 0,
            "assumptions": ["scenario " + json.dumps(px.get("scenario"), sort_keys=True, default=str)], "competitors": ["KNOWN_SMALL_BODY", "HIGH_PROPER_MOTION_STAR", "UNKNOWN_INNER_SOLAR_SYSTEM_BODY"],
            "falsifiers": ["parallax inconsistent with the scenario range", "a known body or a catalogued star at the position"],
            "follow_ups": [_fu("independent confirmation with observations outside HEIMDALL", "independent astrometry", ["PLANET_X_SCENARIO"])] if status != "CONTRADICTED" else [],
            "max_status": "CONSISTENT", "planet_x_source_status": px["status"], "disclaimer": px.get("disclaimer")}
