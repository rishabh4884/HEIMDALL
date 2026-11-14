"""Categorization and scientific reasoning (rules ``REASONING_RULES_VERSION``).

For each candidate the engine records, separately:
- measured facts and derived values (tagged ``M``/``D`` values),
- checks (PASSED / FAILED / NOT_APPLICABLE / NOT_ASSESSABLE; critical or not),
- competing ordinary explanations (SUPPORTED / RULED_OUT / PLAUSIBLE_UNTESTED /
  NOT_ASSESSABLE), each citing evidence when SUPPORTED or RULED_OUT,
- hypotheses and conclusions (``Statement``), never claims of discovery,
- unusualness (a test statistic), physical interpretation and Planet X
  relevance, as three distinct fields.

Attention level (contract §10.5), first rule that applies:
1. an ordinary explanation is SUPPORTED          -> EXPLAINED
2. a critical check FAILED                       -> INCONCLUSIVE (ASSUMPTION_CHECK_FAILED)
3. a critical check NOT_ASSESSABLE               -> INCONCLUSIVE (ASSUMPTION_CHECK_UNRESOLVED)
4. Z >= attend and validity VALIDATED_FOR_CONFIG -> ATTEND
5. Z >= attend otherwise                         -> WATCH, cap_reason = validity reason
6. Z >= watch                                    -> WATCH
7. otherwise                                     -> INCONCLUSIVE
Z is the two-sided Gaussian-equivalent of the candidate's primary statistic.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from ..contracts import (
    Alternative,
    AlternativeState,
    Applicability,
    AttentionLevel,
    AttentionReason,
    AttentionReasonCode,
    Candidate,
    CandidateKind,
    Check,
    CheckState,
    EvidenceKind,
    EvidenceRef,
    Measurement,
    Statement,
    StatementKind,
    Validity,
    ValidityReason,
    ValidityState,
    ValueClass,
)
from .config import ARTEFACT_FLAGS, CHANGE_FLAGS, SATURATION_FLAGS, EngineConfig
from .detection import DetectionRecord
from .linking import TransientTest
from .methods import derived_quantity
from .motion import MotionFit, ParallaxFit
from . import planetx
from .planetx import PlanetXAssessment
from .skygeom import covariance_ellipse, is_positive_definite
from .stats import Significance, chi2_significance
from .temporal import SeriesPoint

# 1.1.0: precision-aware noise check; astrometric_error_model check and alternative (after the first real-data run).
# 1.2.0: known-object and Gaia catalogue context, leave-one-image-out, OUTLIER treated as a change flag, follow-up rank.
# 1.3.0: photometric error model check and alternative for variability and transients.
REASONING_RULES_VERSION = "reasoning-1.3.0-provisional"
GRADE_ORDER = {"A": 0, "B": 1, "C": 2, "D": 3, "U": 4}
FORBIDDEN_PHRASES = ("discover", "confirmed", "confirms", "proof", "proves", "planet x detected", "new planet",
                     "definitely", "certainly")

ALL = frozenset({"VARIABILITY", "MOTION", "TRACK", "TRANSIENT"})
# check_id -> (kinds where it applies, kinds where it is critical)
CHECK_RULES: dict[str, tuple[frozenset, frozenset]] = {
    "data_quality_grade": (ALL, ALL),
    "saturation_flags": (ALL, ALL),
    "artefact_flags_at_peak": (ALL, frozenset({"TRACK", "TRANSIENT"})),
    "association_unambiguous": (ALL, ALL),
    "minimum_data": (ALL, ALL),
    "noise_model_calibrated": (ALL, ALL),
    "fine_astrometry": (frozenset({"MOTION", "TRACK", "TRANSIENT"}), frozenset({"MOTION", "TRACK"})),
    "covariance_valid": (frozenset({"MOTION", "TRACK"}), frozenset({"MOTION", "TRACK"})),
    "release_consistency": (ALL, ALL),
    "pipeline_version_consistency": (ALL, frozenset()),
    "wavelength_comparability": (frozenset({"VARIABILITY", "TRANSIENT"}), frozenset({"VARIABILITY", "TRANSIENT"})),
    "blending": (ALL, frozenset({"VARIABILITY", "MOTION", "TRANSIENT"})),
    "model_goodness_of_fit": (frozenset({"MOTION", "TRACK"}), frozenset({"TRACK"})),
    "astrometric_error_model": (frozenset({"MOTION", "TRACK"}), frozenset({"MOTION", "TRACK"})),
    "photometric_error_model": (frozenset({"VARIABILITY", "TRANSIENT"}), frozenset({"VARIABILITY", "TRANSIENT"})),
    "not_single_image_driven": (frozenset({"MOTION", "TRACK"}), frozenset({"MOTION"})),
    "known_object_check": (frozenset({"MOTION", "TRACK", "TRANSIENT"}), frozenset({"TRACK"})),
    "catalog_crossmatch": (ALL, frozenset()),
    "robust_to_systematics": (frozenset({"VARIABILITY", "MOTION"}), frozenset()),
}


def check_language(text: str) -> str:
    lowered = text.lower()
    for phrase in FORBIDDEN_PHRASES:
        if phrase in lowered:
            raise ValueError(f"statement uses forbidden claim language: {phrase!r}")
    return text


@dataclass
class ImageFacts:
    obs_id: str
    finast: int | None
    release: str
    pipeline_version: str | None
    psf_noise_ratio: float | None
    pixel_noise_ratio: float | None
    wavelength_source: str | None
    psf_noise_se: float | None = None
    pixel_noise_se: float | None = None


@dataclass
class Evidence:
    subject_id: str
    subject_kind: str  # SOURCE | TRACK
    kind: CandidateKind
    category: str
    statistic_name: str
    statistic_meaning: str
    statistic_method: str
    significance: Significance
    ra: float
    dec: float
    cov: list[list[float]]
    detections: list[DetectionRecord]
    points: list[SeriesPoint]
    images: dict[str, ImageFacts]
    ambiguous: bool
    ambiguity_kinds: list[str]
    n_points: int
    n_epochs: int
    min_points: int
    min_epochs: int
    motion: MotionFit | None = None
    parallax: ParallaxFit | None = None
    leave_one_out: Significance | None = None
    systematics_perturbed: Significance | None = None
    transient: TransientTest | None = None
    linked_track: str | None = None
    wavelength_span_um: float | None = None
    tolerance_um: float | None = None
    min_neighbour_px: float | None = None
    # Empirical positional error scale of the field (motion.astrometric_error_model) and the
    # candidate's statistic recomputed with covariances multiplied by scale^2 (when scale > 1).
    error_scale: float | None = None
    error_scale_se: float | None = None
    calibrated_significance: Significance | None = None
    # Field-level flux error scale (temporal.photometric_error_model) for variability and transients.
    flux_error_scale: float | None = None
    flux_error_scale_se: float | None = None
    # External context (heimdall.context): known small bodies and Gaia DR3 match states.
    known_object: dict | None = None
    catalogue: dict | None = None
    # (lowest significance after removing any one image, the image removed)
    leave_one_image_out: tuple | None = None


@dataclass
class CandidateAssessment:
    candidate_id: str
    kind: str
    subject_kind: str
    subject_id: str
    category: str
    position: dict
    statistic: dict
    quality_grade: str
    cell_validity: Validity
    candidate: Candidate
    evidence_for: list[str]
    evidence_against: list[str]
    missing_evidence: list[str]
    contradictions: list[str]
    assumptions: list[str]
    unusualness: dict
    physical_interpretation: dict
    planet_x: PlanetXAssessment | None
    explanation: list[str]
    rules_version: str = REASONING_RULES_VERSION
    context: dict = field(default_factory=dict)
    follow_up: dict = field(default_factory=dict)


def _key(ev: Evidence) -> str:
    return "TRACK" if ev.subject_kind == "TRACK" else ev.kind.value


def worst_grade(dets: list[DetectionRecord]) -> str:
    grades = [d.quality_grade for d in dets] or ["U"]
    return max(grades, key=lambda g: GRADE_ORDER.get(g, 4))


def median_grade(dets: list[DetectionRecord]) -> str:
    grades = sorted((d.quality_grade for d in dets), key=lambda g: GRADE_ORDER.get(g, 4)) or ["U"]
    return grades[(len(grades) - 1) // 2]


def _flags(ev: Evidence) -> set[str]:
    out = {f for d in ev.detections for f in d.peak_flags}
    out |= {f for p in ev.points for f in p.peak_flags}
    return out


def _noise(ev: Evidence, config: EngineConfig) -> tuple[bool | None, str, bool]:
    """Noise-model check, aware of the precision of each ratio estimate.

    Per image the PSF-scale ratio (pixel-scale if unavailable) is: within
    [lo, hi]; significantly outside (beyond 2 standard errors); or undetermined.
    FAILED if any image is significantly outside; NOT_ASSESSABLE if none fails but
    some are undetermined; PASSED if all are within. The third value says whether a
    significant excess (noise underestimated) was found.
    """
    lo, hi = config.quality.noise_ratio_low, config.quality.noise_ratio_high
    obs = sorted({d.obs_id for d in ev.detections} | {p.obs_id for p in ev.points})
    within, outside, undetermined, under = 0, [], [], False
    for o in obs:
        facts = ev.images.get(o)
        if facts is None:
            undetermined.append(f"{o}: no diagnostics")
            continue
        if facts.psf_noise_ratio is not None:
            r, se, scale = facts.psf_noise_ratio, facts.psf_noise_se or 0.0, "PSF"
        else:
            r, se, scale = facts.pixel_noise_ratio, facts.pixel_noise_se or 0.0, "pixel"
        if r is None:
            undetermined.append(f"{o}: not measurable")
        elif lo <= r <= hi:
            within += 1
        elif r - 2 * se > hi or r + 2 * se < lo:
            outside.append(f"{o}={r:.2f}+/-{se:.2f} ({scale})")
            under = under or r > hi
        else:
            undetermined.append(f"{o}={r:.2f}+/-{se:.2f} ({scale})")
    if outside:
        return False, f"noise ratio significantly outside [{lo}, {hi}]: {', '.join(outside[:5])}", under
    if undetermined:
        return None, f"noise calibration not established at the required precision: {', '.join(undetermined[:5])}", False
    return True, f"noise ratios within [{lo}, {hi}] for {within} image(s)", False


def _evaluate_checks(ev: Evidence, config: EngineConfig, cid: str) -> list[Check]:
    key = _key(ev)
    flags = _flags(ev)
    imgs = [ev.images[o] for o in sorted({d.obs_id for d in ev.detections} | {p.obs_id for p in ev.points}) if o in ev.images]
    grade = median_grade(ev.detections)
    worst = worst_grade(ev.detections)
    results: dict[str, tuple[bool | None, str]] = {}
    min_grade = config.significance.min_quality_grade
    results["data_quality_grade"] = (GRADE_ORDER.get(grade, 4) <= GRADE_ORDER[min_grade] and worst != "D",
                                     f"median detection grade {grade}, worst {worst} (median at least {min_grade}; no D)")
    sat = sorted(flags & set(SATURATION_FLAGS))
    results["saturation_flags"] = (not sat, f"saturation flags at peak: {sat}" if sat else "no saturation flags at peak")
    art = sorted(flags & set(ARTEFACT_FLAGS))
    results["artefact_flags_at_peak"] = (not art, f"artefact flags at peak: {art}" if art else "no artefact flags at peak")
    results["association_unambiguous"] = (not ev.ambiguous, f"ambiguity: {ev.ambiguity_kinds}" if ev.ambiguous else "no competing association")
    enough = ev.n_points >= ev.min_points and ev.n_epochs >= ev.min_epochs
    results["minimum_data"] = (enough, f"{ev.n_points} points in {ev.n_epochs} epoch(s); minimum {ev.min_points} in {ev.min_epochs}")
    results["noise_model_calibrated"] = _noise(ev, config)[:2]
    if key in ("MOTION", "TRACK"):
        hi = config.quality.noise_ratio_high
        s, se = ev.error_scale, ev.error_scale_se
        if s is None:
            results["astrometric_error_model"] = (None, "astrometric error scale not measurable (too few well-measured multi-image sources)")
        elif s - 2 * (se or 0.0) > hi:
            results["astrometric_error_model"] = (False, f"field sources scatter {s:.2f} +/- {se:.2f} times the assumed position errors "
                                                         f"(limit {hi})")
        elif s <= hi:
            results["astrometric_error_model"] = (True, f"position error scale {s:.2f} +/- {se:.2f} (<= {hi}; below 1 means conservative)")
        else:
            results["astrometric_error_model"] = (None, f"position error scale {s:.2f} +/- {se:.2f} not established below {hi}")
    if key in ("VARIABILITY", "TRANSIENT"):
        hi = config.quality.noise_ratio_high
        s, se = ev.flux_error_scale, ev.flux_error_scale_se
        if s is None:
            results["photometric_error_model"] = (None, "flux error scale not measurable (too few comparable series in the field)")
        elif s - 2 * (se or 0.0) > hi:
            results["photometric_error_model"] = (False, f"field sources' fluxes scatter {s:.2f} +/- {se:.2f} times the stated "
                                                         f"errors (limit {hi})")
        elif s <= hi:
            results["photometric_error_model"] = (True, f"flux error scale {s:.2f} +/- {se:.2f} (<= {hi})")
        else:
            results["photometric_error_model"] = (None, f"flux error scale {s:.2f} +/- {se:.2f} not established below {hi}")
    fin = [i.finast for i in imgs]
    results["fine_astrometry"] = (all(f == 0 for f in fin) if fin else None,
                                  "FINAST = 0 in all images" if fin and all(f == 0 for f in fin) else f"FINAST values {sorted(set(map(str, fin)))}")
    pd = all(is_positive_definite(d.cov()) for d in ev.detections)
    results["covariance_valid"] = (pd, "all position covariances positive definite" if pd else "a position covariance is not positive definite")
    rel = sorted({i.release for i in imgs})
    results["release_consistency"] = (len(rel) <= 1, f"releases {rel}")
    ver = sorted({i.pipeline_version or "?" for i in imgs})
    results["pipeline_version_consistency"] = (len(ver) <= 1, f"pipeline versions {ver}")
    if key == "VARIABILITY":
        ok = ev.wavelength_span_um is not None and ev.tolerance_um is not None and ev.wavelength_span_um <= ev.tolerance_um + 1e-12
        results["wavelength_comparability"] = (ok, f"wavelength span {ev.wavelength_span_um:.4f} um within tolerance {ev.tolerance_um:.4f} um"
                                               if ok else "wavelength span exceeds tolerance")
    elif key == "TRANSIENT":
        n_ref = ev.transient.n_references if ev.transient else 0
        results["wavelength_comparability"] = (n_ref > 0 if ev.transient else None, f"{n_ref} comparable reference image(s)")
    blend = config.quality.blend_radius_px
    nn = ev.min_neighbour_px
    results["blending"] = (nn is None or nn >= blend, f"nearest detected neighbour {nn:.2f} px (limit {blend} px)"
                           if nn is not None else "no detected neighbour in the same images")
    if key == "TRACK":
        gof = ev.parallax.gof if ev.parallax else None
        results["model_goodness_of_fit"] = (None, "parallax-motion fit has no degrees of freedom") if gof is None else \
            (gof.p_value >= 1e-3, f"parallax-motion fit p = {gof.p_value:.3g}")
    elif key == "MOTION":
        gof = ev.motion.gof_linear if ev.motion else None
        results["model_goodness_of_fit"] = (None, "linear-motion fit has no degrees of freedom") if gof is None else \
            (gof.p_value >= 1e-3, f"linear-motion fit p = {gof.p_value:.3g}")
    ko = ev.known_object or {}
    if ko.get("status") in ("NO_MATCH", "EXCLUDED_BY_MOTION"):
        results["known_object_check"] = (True, ko.get("reason", "no known small body within tolerance"))
    elif ko.get("status") in ("CONFIRMED_MATCH", "PROBABLE_MATCH"):
        results["known_object_check"] = (False, f"{ko['status'].lower()}: {ko['name']} in {ko['n_images_matched']} of "
                                                f"{ko['n_images']} image(s)")
    else:
        results["known_object_check"] = (None, ko.get("reason", "no solar-system ephemeris query completed"))
    cat = ev.catalogue or {}
    if cat.get("status") == "MATCH":
        results["catalog_crossmatch"] = (True, f"Gaia DR3 {cat['gaia_source_id']} at {cat['separation_arcsec']:.2f} arcsec "
                                               f"(G = {cat['g_mag']})")
    elif cat.get("status") == "NO_MATCH":
        results["catalog_crossmatch"] = (True, "no Gaia DR3 star within the gate (absence in Gaia does not prove a non-star)")
    else:
        results["catalog_crossmatch"] = (None, cat.get("reason", "no star-catalogue query completed"))
    loo = ev.leave_one_image_out
    if key in ("MOTION", "TRACK"):
        if loo is None:
            results["not_single_image_driven"] = (None, "fewer than 4 astrometric points")
        else:
            ok = loo[0].sigma_equivalent >= config.significance.watch_sigma
            results["not_single_image_driven"] = (ok, f"lowest significance after removing one image: {loo[0].sigma_equivalent:.2f} "
                                                      f"(without {loo[1]})")
    if ev.systematics_perturbed is not None:
        z = ev.systematics_perturbed.sigma_equivalent
        results["robust_to_systematics"] = (z >= config.significance.watch_sigma,
                                            f"{z:.1f} sigma-equivalent with doubled systematic terms")
    else:
        results["robust_to_systematics"] = (None, "perturbed significance not computed")

    checks = []
    for check_id, (applies, critical) in CHECK_RULES.items():
        rule = f"{REASONING_RULES_VERSION}:{check_id}"
        if key not in applies:
            checks.append(Check(check_id=check_id, state=CheckState.NOT_APPLICABLE,
                                applicability=Applicability(applies=False, rule_id=rule),
                                critical=key in critical, reason=f"not applicable to {key} candidates"))
            continue
        ok, reason = results.get(check_id, (None, "not evaluated"))
        state = CheckState.PASSED if ok is True else CheckState.FAILED if ok is False else CheckState.NOT_ASSESSABLE
        checks.append(Check(check_id=check_id, state=state, applicability=Applicability(applies=True, rule_id=rule),
                            critical=key in critical, reason=reason,
                            evidence=(EvidenceRef(kind=EvidenceKind.RECORD, ref_id=f"{ev.subject_id}"),)))
    return checks


def _ref(cid: str, check_id: str) -> EvidenceRef:
    return EvidenceRef(kind=EvidenceKind.CHECK, ref_id=f"{cid}:check:{check_id}")


def _alternatives(ev: Evidence, checks: dict[str, Check], cell: Validity, config: EngineConfig, cid: str) -> list[Alternative]:
    key = _key(ev)
    z = ev.significance.sigma_equivalent
    alts: list[Alternative] = []

    def add(alt_id, description, state, evidence=(), ordinary=True):
        alts.append(Alternative(alt_id=alt_id, description=description, state=state, ordinary=ordinary, evidence=tuple(evidence)))

    def known_alternative(description):
        ko = ev.known_object or {}
        st = ko.get("status")
        ref = [EvidenceRef(kind=EvidenceKind.EXTERNAL_REFERENCE, ref_id=f"jpl-sbident:{ko.get('name') or 'field-queries'}")]
        if st == "CONFIRMED_MATCH":
            add("known_solar_system_object", f"{description}: {ko['name']}", AlternativeState.SUPPORTED, ref)
        elif st == "PROBABLE_MATCH":
            add("known_solar_system_object", f"{description}: {ko['name']} (one image)", AlternativeState.PLAUSIBLE_UNTESTED)
        elif st == "NO_MATCH":
            add("known_solar_system_object", description, AlternativeState.RULED_OUT, ref)
        elif st == "EXCLUDED_BY_MOTION":
            add("known_solar_system_object", description, AlternativeState.RULED_OUT,
                [EvidenceRef(kind=EvidenceKind.MEASUREMENT, ref_id=f"{cid}:positions")])
        else:
            add("known_solar_system_object", description, AlternativeState.NOT_ASSESSABLE)

    def from_check(check_id, alt_id, description, failed_means=AlternativeState.SUPPORTED,
                   passed_means=AlternativeState.PLAUSIBLE_UNTESTED):
        c = checks.get(check_id)
        if c is None or c.state is CheckState.NOT_APPLICABLE:
            return
        if c.state is CheckState.FAILED:
            add(alt_id, description, failed_means, [_ref(cid, check_id)])
        elif c.state is CheckState.PASSED:
            add(alt_id, description, passed_means, [_ref(cid, check_id)] if passed_means in (AlternativeState.RULED_OUT, AlternativeState.SUPPORTED) else ())
        else:
            add(alt_id, description, AlternativeState.NOT_ASSESSABLE)

    if cell.state is ValidityState.VALIDATED_FOR_CONFIG and z >= config.significance.attend_sigma:
        add("statistical_fluctuation", "statistical fluctuation under the stated noise model", AlternativeState.RULED_OUT,
            [EvidenceRef(kind=EvidenceKind.VALIDATION_RUN, ref_id=cell.validation_run_ref)])
    else:
        add("statistical_fluctuation", "statistical fluctuation under the stated noise model", AlternativeState.PLAUSIBLE_UNTESTED)
    noise_under = _noise(ev, config)[2]
    from_check("noise_model_calibrated", "noise_underestimated", "uncertainties underestimated (noise model miscalibrated)",
               failed_means=AlternativeState.SUPPORTED if noise_under else AlternativeState.PLAUSIBLE_UNTESTED,
               passed_means=AlternativeState.RULED_OUT)
    # A neighbour, a release mismatch or an ambiguous association make an ordinary explanation possible, not
    # demonstrated: they fail critical checks (-> INCONCLUSIVE) but leave the alternative PLAUSIBLE_UNTESTED.
    from_check("blending", "blending", "blending with a neighbouring source", failed_means=AlternativeState.PLAUSIBLE_UNTESTED)
    from_check("saturation_flags", "saturation_or_nonlinearity", "saturation or detector nonlinearity",
               passed_means=AlternativeState.RULED_OUT)
    from_check("release_consistency", "calibration_release_difference", "calibration difference between data releases",
               failed_means=AlternativeState.PLAUSIBLE_UNTESTED, passed_means=AlternativeState.RULED_OUT)
    from_check("pipeline_version_consistency", "pipeline_version_difference", "calibration difference between pipeline versions",
               failed_means=AlternativeState.PLAUSIBLE_UNTESTED)
    from_check("artefact_flags_at_peak", "image_artefact", "single-image artefact (cosmic ray, ghost, crosstalk, persistence)")
    if key in ("VARIABILITY", "TRANSIENT"):
        pem = checks.get("photometric_error_model")
        desc = "flux errors underestimated (the field's sources scatter more than the stated flux errors allow)"
        cal = ev.calibrated_significance
        if pem is None or pem.state is CheckState.NOT_ASSESSABLE:
            add("photometric_errors_underestimated", desc, AlternativeState.NOT_ASSESSABLE)
        elif pem.state is CheckState.PASSED:
            add("photometric_errors_underestimated", desc, AlternativeState.RULED_OUT, [_ref(cid, "photometric_error_model")])
        elif cal is not None and cal.sigma_equivalent < config.significance.watch_sigma:
            add("photometric_errors_underestimated", desc, AlternativeState.SUPPORTED,
                [_ref(cid, "photometric_error_model"), EvidenceRef(kind=EvidenceKind.MEASUREMENT, ref_id=f"{cid}:calibrated_significance")])
        else:
            add("photometric_errors_underestimated", desc, AlternativeState.PLAUSIBLE_UNTESTED)
    if key == "VARIABILITY":
        loo = ev.leave_one_out
        if loo is not None and loo.sigma_equivalent >= config.significance.watch_sigma:
            add("single_measurement_outlier", "excess driven by a single measurement", AlternativeState.RULED_OUT,
                [EvidenceRef(kind=EvidenceKind.MEASUREMENT, ref_id=f"{cid}:leave_one_out")])
        elif loo is not None:
            add("single_measurement_outlier", "excess driven by a single measurement", AlternativeState.SUPPORTED,
                [EvidenceRef(kind=EvidenceKind.MEASUREMENT, ref_id=f"{cid}:leave_one_out")])
        else:
            add("single_measurement_outlier", "excess driven by a single measurement", AlternativeState.NOT_ASSESSABLE)
        add("spectral_slope_within_tolerance", "flux change from wavelength differences within the matching tolerance",
            AlternativeState.PLAUSIBLE_UNTESTED)
    if key in ("MOTION", "TRACK"):
        from_check("fine_astrometry", "registration_error", "image registration or astrometric systematic error")
        from_check("association_unambiguous", "misassociation", "different sources associated as one",
                   failed_means=AlternativeState.PLAUSIBLE_UNTESTED)
        known_alternative("a known asteroid, comet or trans-Neptunian object")
        from_check("not_single_image_driven", "single_image_systematic", "a systematic error in one image (the excess "
                   "disappears without it)", passed_means=AlternativeState.RULED_OUT)
        aem = checks.get("astrometric_error_model")
        desc = "position errors underestimated (the field's sources scatter more than the error model allows)"
        cal = ev.calibrated_significance
        if aem is None or aem.state is CheckState.NOT_ASSESSABLE:
            add("astrometric_errors_underestimated", desc, AlternativeState.NOT_ASSESSABLE)
        elif aem.state is CheckState.PASSED:
            add("astrometric_errors_underestimated", desc, AlternativeState.RULED_OUT, [_ref(cid, "astrometric_error_model")])
        elif cal is not None and cal.sigma_equivalent < config.significance.watch_sigma:
            add("astrometric_errors_underestimated", desc, AlternativeState.SUPPORTED,
                [_ref(cid, "astrometric_error_model"), EvidenceRef(kind=EvidenceKind.MEASUREMENT, ref_id=f"{cid}:calibrated_significance")])
        else:
            add("astrometric_errors_underestimated", desc, AlternativeState.PLAUSIBLE_UNTESTED)
    if key == "MOTION":
        plx = ev.parallax
        cat = ev.catalogue or {}
        if plx is not None and plx.status == "COMPLETED" and plx.parallax_arcsec - 3 * plx.parallax_sigma_arcsec > 1.0:
            add("high_proper_motion_star", "nearby star with large proper motion", AlternativeState.RULED_OUT,
                [EvidenceRef(kind=EvidenceKind.MEASUREMENT, ref_id=f"{cid}:parallax")])
        elif cat.get("status") == "MATCH" and cat.get("n_within_gate") == 1 and None not in (cat.get("pm_mas_yr") or [None]):
            ref = [EvidenceRef(kind=EvidenceKind.EXTERNAL_REFERENCE, ref_id=f"gaia-dr3:{cat['gaia_source_id']}")]
            pm = np.array(cat["pm_mas_yr"], dtype=float) / 1e3
            if math.hypot(*pm) < config.context.quasi_stationary_pm_mas_yr / 1e3:
                add("apparent_motion_contradicted_by_catalogue", "the source is a Gaia DR3 star that is effectively stationary at "
                    f"this precision ({math.hypot(*pm) * 1e3:.1f} mas/yr): the apparent motion is a measurement effect",
                    AlternativeState.SUPPORTED, ref)
            elif ev.motion and ev.motion.mu_arcsec_per_yr and ev.motion.mu_cov:
                err = np.array([e or 0.0 for e in cat["pm_error_mas_yr"]]) / 1e3
                d = np.array(ev.motion.mu_arcsec_per_yr) - pm
                z = chi2_significance(float(d @ np.linalg.solve(np.array(ev.motion.mu_cov) + np.diag(err ** 2), d)), 2)
                if z.sigma_equivalent < config.significance.watch_sigma:
                    add("high_proper_motion_star", "a catalogued star whose Gaia DR3 proper motion matches the apparent motion",
                        AlternativeState.SUPPORTED, ref)
                else:
                    add("apparent_motion_contradicted_by_catalogue", "the apparent motion disagrees with the Gaia DR3 proper "
                        "motion of the star at this position", AlternativeState.SUPPORTED, ref)
        else:
            add("high_proper_motion_star", "nearby star with large proper motion", AlternativeState.PLAUSIBLE_UNTESTED)
    if key == "TRACK":
        add("chance_linkage", "unrelated detections linked by chance", AlternativeState.PLAUSIBLE_UNTESTED)
        plx = ev.parallax
        if plx is not None and plx.status == "COMPLETED" and plx.distance_interval_au and plx.distance_interval_au[0] is not None:
            lo_d, hi_d = plx.distance_interval_au
            ref = [EvidenceRef(kind=EvidenceKind.MEASUREMENT, ref_id=f"{cid}:parallax")]
            if hi_d is not None and hi_d < 6:
                add("inner_solar_system_body", "asteroid or comet within about 6 AU", AlternativeState.SUPPORTED, ref)
            elif lo_d > 6:
                add("inner_solar_system_body", "asteroid or comet within about 6 AU", AlternativeState.RULED_OUT, ref)
            else:
                add("inner_solar_system_body", "asteroid or comet within about 6 AU", AlternativeState.PLAUSIBLE_UNTESTED)
    if key == "TRANSIENT":
        if ev.linked_track:
            add("moving_object", "a moving solar-system body passing through the field", AlternativeState.SUPPORTED,
                [EvidenceRef(kind=EvidenceKind.RECORD, ref_id=ev.linked_track)])
        else:
            add("moving_object", "a moving solar-system body passing through the field", AlternativeState.PLAUSIBLE_UNTESTED)
        known_alternative("a known asteroid or comet")
        cat = ev.catalogue or {}
        if cat.get("status") == "MATCH":
            add("catalogued_star", f"the source coincides with Gaia DR3 star {cat['gaia_source_id']} (G = {cat['g_mag']}): a known "
                "star, not a new object; the flux excess is a variability or measurement question", AlternativeState.SUPPORTED,
                [EvidenceRef(kind=EvidenceKind.EXTERNAL_REFERENCE, ref_id=f"gaia-dr3:{cat['gaia_source_id']}")])
    return alts


def _attention(z: float, validity: Validity, checks: list[Check], alts: list[Alternative], config: EngineConfig):
    s = config.significance
    supported = [a for a in alts if a.ordinary and a.state is AlternativeState.SUPPORTED]
    failed = [c.check_id for c in checks if c.is_applicable_critical and c.state is CheckState.FAILED]
    unresolved = [c.check_id for c in checks if c.is_applicable_critical and c.state is CheckState.NOT_ASSESSABLE]
    trace = []
    if supported:
        trace.append(f"rule 1: ordinary explanation supported ({', '.join(a.alt_id for a in supported)})")
        return AttentionLevel.EXPLAINED, AttentionReason(code=AttentionReasonCode.OTHER, failed_checks=tuple(failed),
                                                         note="ordinary explanation supported"), None, trace
    if failed:
        trace.append(f"rule 2: critical checks failed ({', '.join(failed)})")
        return AttentionLevel.INCONCLUSIVE, AttentionReason(code=AttentionReasonCode.ASSUMPTION_CHECK_FAILED,
                                                            failed_checks=tuple(failed)), None, trace
    if unresolved:
        trace.append(f"rule 3: critical checks unresolved ({', '.join(unresolved)})")
        return AttentionLevel.INCONCLUSIVE, AttentionReason(code=AttentionReasonCode.ASSUMPTION_CHECK_UNRESOLVED,
                                                            unresolved_checks=tuple(unresolved)), None, trace
    if z >= s.attend_sigma:
        if validity.state is ValidityState.VALIDATED_FOR_CONFIG:
            trace.append(f"rule 4: Z = {z:.2f} >= {s.attend_sigma} and validity VALIDATED_FOR_CONFIG")
            return AttentionLevel.ATTEND, None, None, trace
        cap = validity.primary_reason or (ValidityReason.CRITERIA_FAILED if validity.state is ValidityState.MISCALIBRATED
                                          else ValidityReason.NO_CONTROL_RUN)
        trace.append(f"rule 5: Z = {z:.2f} >= {s.attend_sigma} but validity {validity.state.value}; capped at WATCH ({cap.value})")
        return AttentionLevel.WATCH, None, cap, trace
    if z >= s.watch_sigma:
        trace.append(f"rule 6: {s.watch_sigma} <= Z = {z:.2f} < {s.attend_sigma}")
        return AttentionLevel.WATCH, None, None, trace
    trace.append(f"rule 7: Z = {z:.2f} below the watch threshold {s.watch_sigma}")
    return AttentionLevel.INCONCLUSIVE, AttentionReason(code=AttentionReasonCode.OTHER, note="below watch threshold"), None, trace


def candidate_validity(cell: Validity, checks: list[Check]) -> Validity:
    failed = any(c.is_applicable_critical and c.state is CheckState.FAILED for c in checks)
    unresolved = any(c.is_applicable_critical and c.state is CheckState.NOT_ASSESSABLE for c in checks)
    if not (failed or unresolved):
        return cell
    reasons = []
    if failed:
        reasons.append(ValidityReason.ASSUMPTION_CHECK_FAILED)
    if unresolved:
        reasons.append(ValidityReason.ASSUMPTION_CHECK_UNRESOLVED)
    if cell.state is ValidityState.UNVALIDATED:
        reasons.extend(cell.reasons)
    return Validity.unvalidated(*reasons, scope_cell=cell.scope_cell)


HYPOTHESES = {
    "VARIABILITY": "The source's flux at about {wl:.3f} um changes with time (genuine astrophysical variability).",
    "MOTION": "The source's position changes with time (genuine motion on the sky).",
    "TRACK": "These detections, made at different times, belong to one moving body.",
    "TRANSIENT": "The source was present in one epoch and absent at a comparable wavelength in other images.",
}


def assess(ev: Evidence, cell: Validity, config: EngineConfig, planet_x: PlanetXAssessment | None,
           measurement_refs: dict[str, str] | None = None) -> CandidateAssessment:
    key = _key(ev)
    cid = f"C:{key}:{ev.subject_id}"
    checks = _evaluate_checks(ev, config, cid)
    by_id = {c.check_id: c for c in checks}
    alts = _alternatives(ev, by_id, cell, config, cid)
    validity = candidate_validity(cell, checks)
    z = ev.significance.sigma_equivalent
    level, reason, cap, trace = _attention(z, validity, checks, alts, config)
    if planet_x is not None:
        # Context checks have their own Planet X requirements (not_a_known_solar_system_object, not_a_star).
        measurement = [c for c in checks if c.is_applicable_critical and c.check_id not in ("known_object_check", "catalog_crossmatch")]
        planetx.require_reliable_measurement(planet_x, [c.check_id for c in measurement if c.state is CheckState.FAILED],
                                             [c.check_id for c in measurement if c.state is CheckState.NOT_ASSESSABLE])

    wl = float(np.mean([p.wavelength_um for p in ev.points if p.wavelength_um])) if ev.points else math.nan
    hypothesis = check_language(HYPOTHESES[key].format(wl=wl))
    statements = [Statement(kind=StatementKind.HYPOTHESIS, text=hypothesis)]
    supported = [a for a in alts if a.ordinary and a.state is AlternativeState.SUPPORTED]
    if level is AttentionLevel.EXPLAINED:
        text = check_language("The signal is attributed to an ordinary explanation: " + "; ".join(a.description for a in supported) + ".")
        statements.append(Statement(kind=StatementKind.CONCLUSION, text=text, evidence=tuple(e for a in supported for e in a.evidence)))
    elif level is AttentionLevel.INCONCLUSIVE and reason is not None and reason.code is not AttentionReasonCode.OTHER:
        ids = reason.failed_checks or reason.unresolved_checks
        text = check_language("No interpretation is supported because critical checks did not pass: " + ", ".join(ids) + ".")
        statements.append(Statement(kind=StatementKind.CONCLUSION, text=text, evidence=tuple(_ref(cid, i) for i in ids)))

    refs = measurement_refs or {}
    stat_inputs = {"VARIABILITY": ("variability_chi2", {"series_fluxes": refs.get("series", ev.subject_id)}),
                   "MOTION": ("stationarity_chi2", {"positions": refs.get("positions", ev.subject_id)}),
                   "TRACK": ("parallax_motion_fit", {"positions": refs.get("positions", ev.subject_id),
                                                     "times": refs.get("times", ev.subject_id),
                                                     "observer_positions": refs.get("observer", "ERFA epv00 builtin")}),
                   "TRANSIENT": ("flux_consistency_z", {"flux_a": refs.get("probe", ev.subject_id),
                                                        "flux_b": refs.get("reference", ev.subject_id)})}[key]
    measurements = [
        Measurement(measurement_id=f"{cid}:statistic", name=ev.statistic_name, subject_ref=ev.subject_id,
                    value=derived_quantity(ev.significance.statistic, stat_inputs[0], stat_inputs[1],
                                           uncertainty_kind="none", sigma=None)),
        Measurement(measurement_id=f"{cid}:sigma_equivalent", name="two-sided Gaussian-equivalent significance",
                    subject_ref=ev.subject_id,
                    value=derived_quantity(z, "chi2_two_sided_sigma", {"statistic": f"{cid}:statistic", "dof": str(ev.significance.dof)})),
        Measurement(measurement_id=f"{cid}:n_points", name="number of measurements", subject_ref=ev.subject_id,
                    value=derived_quantity(int(ev.n_points), "count", {"items": ev.subject_id}, value_class=ValueClass.COUNT)),
    ]
    candidate = Candidate(candidate_id=cid, kind=ev.kind, attention_level=level, validity=validity, measurements=measurements,
                          checks=checks, alternatives=alts, statements=statements, attention_reason=reason, cap_reason=cap)

    grade = worst_grade(ev.detections)
    evidence_for = [f"{ev.statistic_name}: {ev.significance.statistic:.3g} for {ev.significance.dof} dof, "
                    f"Z = {z:.2f} (two-sided Gaussian-equivalent; validity {validity.state.value})"]
    evidence_for += [f"ruled out: {a.description}" for a in alts if a.state is AlternativeState.RULED_OUT]
    changed = sorted({d.obs_id for d in ev.detections if set(d.peak_flags) & set(CHANGE_FLAGS)})
    if changed:
        evidence_for.append(f"the SPHEREx pipeline flagged the peak pixel OUTLIER ('Pixel flagged by Detect Outliers') in "
                            f"{len(changed)} image(s): consistent with a change on the sky (it is also set for some artefacts)")
    evidence_against = [f"check failed: {c.check_id} ({c.reason})" for c in checks
                        if c.state is CheckState.FAILED and c.applicability.applies]
    evidence_against += [f"ordinary explanation supported: {a.description}" for a in supported]
    missing = [f"{c.check_id}: {c.reason}" for c in checks if c.state is CheckState.NOT_ASSESSABLE]
    missing += [f"untested alternative: {a.description}" for a in alts
                if a.state in (AlternativeState.NOT_ASSESSABLE, AlternativeState.PLAUSIBLE_UNTESTED)]
    contradictions = []
    if key == "VARIABILITY" and ev.leave_one_out is not None and z >= config.significance.watch_sigma \
            and ev.leave_one_out.sigma_equivalent < config.significance.watch_sigma:
        contradictions.append("the excess disappears when the most deviant measurement is removed")
    if key == "MOTION" and ev.motion and ev.motion.gof_linear and ev.motion.gof_linear.p_value < 1e-3:
        contradictions.append("positions change, but a linear motion model does not describe them")
    if planet_x is not None and planet_x.status == "INCONSISTENT":
        contradictions.append("measured motion is inconsistent with the distant-body scenario")
    assumptions = ["pixel noise is Gaussian and described by the VARIANCE plane (tested by the noise-ratio check)",
                   "the PSF model describes the sources (tested by the reduced chi-square of each fit)"]
    if key in ("MOTION", "TRACK"):
        assumptions.append(f"per-image registration error {config.association.registration_floor_arcsec} arcsec (1 sigma)")
    if key == "VARIABILITY":
        assumptions.append("fluxes within the wavelength tolerance are comparable")
    e1, e2, pa = covariance_ellipse(np.asarray(ev.cov))
    return CandidateAssessment(
        candidate_id=cid, kind=ev.kind.value, subject_kind=ev.subject_kind, subject_id=ev.subject_id, category=ev.category,
        position={"ra_deg": ev.ra, "dec_deg": ev.dec, "cov_arcsec2": ev.cov,
                  "ellipse_1sigma": {"a_arcsec": e1, "b_arcsec": e2, "pa_deg": pa}},
        statistic={"name": ev.statistic_name, "method_id": ev.statistic_method, "significance": ev.significance,
                   "empirically_rescaled": ev.calibrated_significance,
                   "error_scale": ev.error_scale,
                   "flux_error_scale": ev.flux_error_scale,
                   "leave_one_image_out": {"sigma_equivalent": ev.leave_one_image_out[0].sigma_equivalent,
                                           "removed_obs_id": ev.leave_one_image_out[1]} if ev.leave_one_image_out else None,
                   "meaning": ev.statistic_meaning,
                   "not_meaning": "not a probability that the source is real, astrophysical or related to Planet X"},
        quality_grade=grade, cell_validity=cell, candidate=candidate, evidence_for=evidence_for,
        evidence_against=evidence_against, missing_evidence=missing, contradictions=contradictions, assumptions=assumptions,
        unusualness={"sigma_equivalent": z, "statistic": ev.statistic_name, "validity": validity.state.value,
                     "is_probability_of_planet_x": False},
        physical_interpretation={"status": "ORDINARY_EXPLANATION_SUPPORTED" if supported else "NOT_ESTABLISHED",
                                 "hypothesis": hypothesis,
                                 "note": "a significant statistic does not identify its physical cause"},
        planet_x=planet_x, explanation=trace,
        context={"known_object": ev.known_object, "catalogue": ev.catalogue})


ATTENTION_ORDER = {"ATTEND": 0, "WATCH": 1, "INCONCLUSIVE": 2, "NOT_ASSESSABLE": 3, "EXPLAINED": 4}
FOLLOW_UP_BASIS = ("lexicographic order: attention level (ATTEND, WATCH, INCONCLUSIVE, NOT_ASSESSABLE, EXPLAINED), validated "
                   "before unvalidated, fewer failed then fewer unresolved critical checks, higher error-rescaled "
                   "significance; an ordering for follow-up effort, not a probability of anything")


def rank_follow_up(assessments: list[CandidateAssessment]) -> None:
    """Assign follow-up ranks in place (1 = first) with the reasons that decided the order."""
    def key(a: CandidateAssessment):
        c = a.candidate
        failed = sum(1 for k in c.checks if k.is_applicable_critical and k.state is CheckState.FAILED)
        unresolved = sum(1 for k in c.checks if k.is_applicable_critical and k.state is CheckState.NOT_ASSESSABLE)
        resc = a.statistic.get("empirically_rescaled") or a.statistic["significance"]
        return (ATTENTION_ORDER.get(c.attention_level.value, 5), 0 if c.validity.state is ValidityState.VALIDATED_FOR_CONFIG else 1,
                failed, unresolved, -resc.sigma_equivalent, a.candidate_id), failed, unresolved, resc.sigma_equivalent
    ranked = sorted(assessments, key=lambda a: key(a)[0])
    for i, a in enumerate(ranked, start=1):
        _, failed, unresolved, z = key(a)
        a.follow_up = {"rank": i, "attention_level": a.candidate.attention_level.value,
                       "validity": a.candidate.validity.state.value, "failed_critical_checks": failed,
                       "unresolved_critical_checks": unresolved, "rescaled_sigma_equivalent": z, "basis": FOLLOW_UP_BASIS}
