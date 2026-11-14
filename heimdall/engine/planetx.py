"""Evaluation of the distant solar-system body ("Planet X") hypothesis.

HEIMDALL can only test whether a *measured moving source* is consistent with a
body at heliocentric distance r in [r_min, r_max] (a user-set scenario, tagged
as an assumption). It never estimates a probability that Planet X exists or
has been found. The best achievable state is
``CONSISTENT_REQUIRES_CONFIRMATION``: independent follow-up is always required.

Physics used (Sun-centred, AU, years; GM_sun = 4 pi^2 AU^3/yr^2):
- annual parallax amplitude varpi = 206264.8" / r;
- escape speed v_esc(r) = 2 pi sqrt(2 / r) AU/yr bounds a bound orbit's speed;
- the observer's displacement over dt is at most min(v_E dt, 2.04 AU) with
  v_E <= 6.39 AU/yr (Earth's perihelion speed), so the apparent displacement of
  a body with r >= r_min is at most
  [min(v_E dt, 2.04) + v_esc(r_min) dt] / (r_min - 1)   (radians).
  This bound defines the moving-object linking radius.

Unusualness (a statistic), physical interpretation (a distance) and relevance
to the hypothesis (requirements below) are reported separately.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .config import EngineConfig
from .motion import MotionFit, ParallaxFit

# 1.1.0: known-object and catalogue requirements; completeness-limited region statements.
# 1.2.0: fit_quality needs >= min_link_dof; measurement_reliable (critical measurement checks) required for CONSISTENT.
# 1.3.0: linker rate-bound eligibility, split-tracklet ownership, partial known-object matches are not confirmed.
PLANETX_RULES_VERSION = "planetx-1.3.0-provisional"
ARCSEC_PER_RAD = 206264.80624709636
EARTH_MAX_SPEED_AU_PER_YR = 6.39
OBSERVER_MAX_CHORD_AU = 2.04
STAR_MAX_PARALLAX_ARCSEC = 1.0  # the nearest star (Proxima Centauri) has about 0.77"
DISCLAIMER = ("Not a probability that Planet X exists or was found. States describe consistency of measured data with a "
              "stated scenario; independent confirmation is always required.")


def parallax_arcsec(r_au: float) -> float:
    return ARCSEC_PER_RAD / r_au


def escape_speed_au_per_yr(r_au: float) -> float:
    return 2.0 * math.pi * math.sqrt(2.0 / r_au)


def max_separation_arcsec(dt_days: float, r_min_au: float) -> float:
    """Largest apparent displacement over dt for any bound body with r >= r_min (see module docstring)."""
    if not r_min_au > 1.0:
        raise ValueError("r_min_au must exceed 1 AU")
    dt_yr = abs(dt_days) / 365.25
    reflex = min(EARTH_MAX_SPEED_AU_PER_YR * dt_yr, OBSERVER_MAX_CHORD_AU) + 1e-4
    own = escape_speed_au_per_yr(r_min_au) * dt_yr
    return (reflex + own) / (r_min_au - 1.0) * ARCSEC_PER_RAD


@dataclass
class Requirement:
    req_id: str
    description: str
    state: str  # SATISFIED | NOT_SATISFIED | NOT_ASSESSABLE
    detail: str
    evidence: list[str] = field(default_factory=list)


@dataclass
class PlanetXAssessment:
    subject_id: str
    applicable: bool
    status: str  # NOT_APPLICABLE | NOT_ASSESSABLE | INCONSISTENT | UNCONSTRAINED | CONSISTENT_REQUIRES_CONFIRMATION
    hypothesis: str
    scenario: dict
    expected_parallax_arcsec: list[float]
    measured: dict | None
    requirements: list[Requirement]
    competing_explanations: list[dict]
    separation_of_claims: dict
    rules_version: str = PLANETX_RULES_VERSION
    disclaimer: str = DISCLAIMER


def _scenario(config: EngineConfig) -> dict:
    p = config.planetx
    return {"r_min_au": p.r_min_au, "r_max_au": p.r_max_au, "tag": "A",
            "rationale": "user-configurable exploratory distance range; not a literature estimate"}


def not_applicable(subject_id: str, config: EngineConfig, why: str) -> PlanetXAssessment:
    p = config.planetx
    return PlanetXAssessment(
        subject_id=subject_id, applicable=False, status="NOT_APPLICABLE",
        hypothesis="A distant solar-system body would show parallactic and orbital motion.",
        scenario=_scenario(config), expected_parallax_arcsec=[parallax_arcsec(p.r_max_au), parallax_arcsec(p.r_min_au)],
        measured=None, requirements=[], competing_explanations=[],
        separation_of_claims={"unusualness": "see candidate statistic", "physical_interpretation": "not assessed",
                              "planet_x_relevance": why})


def evaluate(subject_id: str, *, motion: MotionFit | None, parallax: ParallaxFit | None, n_epoch_clusters: int,
             validity_state: str, config: EngineConfig, evidence_prefix: str, known_object: dict | None = None,
             catalogue: dict | None = None) -> PlanetXAssessment:
    p = config.planetx
    sig = config.significance
    reqs: list[Requirement] = []

    def add(req_id, description, state, detail, evidence=()):
        reqs.append(Requirement(req_id, description, state, detail, list(evidence)))

    add("multi_epoch_coverage", f"detections in at least {p.min_epoch_clusters_for_parallax} separate epochs",
        "SATISFIED" if n_epoch_clusters >= p.min_epoch_clusters_for_parallax else "NOT_SATISFIED",
        f"{n_epoch_clusters} epoch(s)")
    if motion is None or motion.status != "COMPLETED":
        add("positional_change", "positions inconsistent with a fixed point", "NOT_ASSESSABLE",
            ", ".join(motion.reasons) if motion else "no motion analysis")
    else:
        z = motion.gof_stationary.sigma_equivalent
        add("positional_change", "positions inconsistent with a fixed point",
            "SATISFIED" if z >= sig.watch_sigma else "NOT_SATISFIED", f"stationarity test {z:.1f} sigma-equivalent",
            [f"{evidence_prefix}:motion"])

    measured = None
    lo_hyp, hi_hyp = p.r_min_au, p.r_max_au
    if parallax is None or parallax.status != "COMPLETED":
        why = ", ".join(parallax.reasons) if parallax else "parallax fit not run"
        for rid, desc in (("parallax_measured", "parallax detected at >= 3 sigma"),
                          ("distance_in_range", f"distance interval overlaps {lo_hyp:g}-{hi_hyp:g} AU"),
                          ("bound_orbit", "implied transverse speed below the escape speed"),
                          ("fit_quality", "parallax-motion model fits the positions")):
            add(rid, desc, "NOT_ASSESSABLE", why)
        add("not_a_star", "parallax larger than any star's (> 1 arcsec)", "NOT_ASSESSABLE", why)
    else:
        plx, s = parallax.parallax_arcsec, parallax.parallax_sigma_arcsec
        lo_d, hi_d = parallax.distance_interval_au
        measured = {"parallax_arcsec": plx, "parallax_sigma_arcsec": s, "distance_au": parallax.distance_au,
                    "distance_interval_au_2sigma": [lo_d, hi_d], "mu_arcsec_per_yr": parallax.mu_arcsec_per_yr,
                    "chi2": parallax.chi2, "dof": parallax.dof}
        snr = plx / s if s > 0 else 0.0
        add("parallax_measured", "parallax detected at >= 3 sigma", "SATISFIED" if snr >= 3 else "NOT_SATISFIED",
            f"parallax {plx:.3f} +/- {s:.3f} arcsec", [f"{evidence_prefix}:parallax"])
        if lo_d is None:
            add("distance_in_range", f"distance interval overlaps {lo_hyp:g}-{hi_hyp:g} AU", "NOT_SATISFIED",
                "parallax is significantly negative: unphysical for any body", [f"{evidence_prefix}:parallax"])
        else:
            overlap = (hi_d is None or hi_d >= lo_hyp) and lo_d <= hi_hyp
            add("distance_in_range", f"distance interval overlaps {lo_hyp:g}-{hi_hyp:g} AU",
                "SATISFIED" if overlap else "NOT_SATISFIED",
                f"2-sigma distance interval {lo_d:.1f}-{'unbounded' if hi_d is None else f'{hi_d:.1f}'} AU",
                [f"{evidence_prefix}:parallax"])
        if parallax.distance_au and parallax.mu_arcsec_per_yr:
            r = parallax.distance_au
            mu = math.hypot(*parallax.mu_arcsec_per_yr) / ARCSEC_PER_RAD
            v = mu * r
            vesc = escape_speed_au_per_yr(r)
            add("bound_orbit", "implied transverse speed below the escape speed",
                "SATISFIED" if v <= vesc else "NOT_SATISFIED",
                f"transverse speed {v:.3f} AU/yr vs escape speed {vesc:.3f} AU/yr at {r:.0f} AU", [f"{evidence_prefix}:parallax"])
        else:
            add("bound_orbit", "implied transverse speed below the escape speed", "NOT_ASSESSABLE",
                "distance not established (parallax below 3 sigma)")
        if parallax.gof is None:
            add("fit_quality", "parallax-motion model fits the positions", "NOT_ASSESSABLE", "no degrees of freedom left")
        elif (parallax.dof or 0) < config.planetx.min_link_dof:
            # With fewer than min_link_dof degrees of freedom a good fit tests almost nothing (same rule as linking).
            add("fit_quality", "parallax-motion model fits the positions", "NOT_ASSESSABLE",
                f"only {parallax.dof} degree(s) of freedom (minimum {config.planetx.min_link_dof}): the fit cannot be tested",
                [f"{evidence_prefix}:parallax"])
        else:
            add("fit_quality", "parallax-motion model fits the positions",
                "SATISFIED" if parallax.gof.p_value >= 1e-3 else "NOT_SATISFIED",
                f"chi2 {parallax.chi2:.1f} for {parallax.dof} dof (p = {parallax.gof.p_value:.2g})", [f"{evidence_prefix}:parallax"])
        cat = catalogue or {}
        if plx - 3 * s > STAR_MAX_PARALLAX_ARCSEC:
            add("not_a_star", "parallax larger than any star's (> 1 arcsec)", "SATISFIED", "parallax exceeds 1 arcsec at 3 sigma")
        elif cat.get("status") == "MATCH":
            add("not_a_star", "parallax larger than any star's (> 1 arcsec)", "NOT_SATISFIED",
                f"coincides with Gaia DR3 star {cat['gaia_source_id']}", [f"gaia-dr3:{cat['gaia_source_id']}"])
        else:
            add("not_a_star", "parallax larger than any star's (> 1 arcsec)", "NOT_ASSESSABLE",
                "parallax does not exclude a star and no catalogued star matches")
    ko = known_object or {}
    if ko.get("status") in ("NO_MATCH", "EXCLUDED_BY_MOTION"):
        add("not_a_known_solar_system_object", "no known asteroid, comet or TNO at these positions and times", "SATISFIED",
            ko.get("reason", "no known body within tolerance"), ["jpl-sbident:field-queries"])
    elif ko.get("status") in ("CONFIRMED_MATCH", "PROBABLE_MATCH"):
        add("not_a_known_solar_system_object", "no known asteroid, comet or TNO at these positions and times",
            "NOT_SATISFIED", f"{ko['status'].lower()}: {ko['name']}", [f"jpl-sbident:{ko['name']}"])
    else:
        add("not_a_known_solar_system_object", "no known asteroid, comet or TNO at these positions and times", "NOT_ASSESSABLE",
            ko.get("reason", "no solar-system ephemeris query completed"))
    add("brightness_consistent", "flux consistent with a body of plausible size and albedo", "NOT_ASSESSABLE",
        "no validated reflectance/thermal model for 0.75-5 um is implemented")
    add("validated_significance", "candidate significance validated for this configuration",
        "SATISFIED" if validity_state == "VALIDATED_FOR_CONFIG" else "NOT_SATISFIED", f"validity {validity_state}")
    add("independent_confirmation", "confirmation by independent observations", "NOT_SATISFIED",
        "requires follow-up outside HEIMDALL")

    state = {r.req_id: r.state for r in reqs}
    if state["multi_epoch_coverage"] == "NOT_SATISFIED":
        status = "NOT_ASSESSABLE"
    elif any(state[k] == "NOT_SATISFIED" for k in ("distance_in_range", "bound_orbit", "fit_quality", "not_a_star")) \
            or (state["not_a_known_solar_system_object"] == "NOT_SATISFIED" and ko.get("status") == "CONFIRMED_MATCH"):
        status = "INCONSISTENT"
    elif state["parallax_measured"] == "NOT_ASSESSABLE" or state["distance_in_range"] == "NOT_ASSESSABLE" \
            or state.get("fit_quality") == "NOT_ASSESSABLE":
        status = "NOT_ASSESSABLE"
    elif state["positional_change"] != "SATISFIED" or state["parallax_measured"] == "NOT_SATISFIED" \
            or (measured and (measured["distance_interval_au_2sigma"][1] is None)):
        status = "UNCONSTRAINED"
    else:
        status = "CONSISTENT_REQUIRES_CONFIRMATION"

    competing = []
    if measured and measured["distance_interval_au_2sigma"][0] is not None:
        lo_d, hi_d = measured["distance_interval_au_2sigma"]
        competing.append({"explanation": "inner solar-system body (asteroid or comet)",
                          "state": "SUPPORTED" if hi_d is not None and hi_d < 6 else ("RULED_OUT" if lo_d > 6 else "PLAUSIBLE_UNTESTED")})
        competing.append({"explanation": "trans-Neptunian object of the known population (about 30-100 AU)",
                          "state": "SUPPORTED" if hi_d is not None and hi_d < lo_hyp and lo_d > 20 else
                          ("RULED_OUT" if lo_d > lo_hyp else "PLAUSIBLE_UNTESTED")})
    competing.append({"explanation": "known catalogued solar-system object",
                      "state": {"CONFIRMED_MATCH": "SUPPORTED", "PROBABLE_MATCH": "PLAUSIBLE_UNTESTED",
                                "NO_MATCH": "RULED_OUT"}.get(ko.get("status"), "NOT_ASSESSABLE")})
    competing.append({"explanation": "unrelated detections linked by chance, or artefacts", "state": "PLAUSIBLE_UNTESTED"})
    return PlanetXAssessment(
        subject_id=subject_id, applicable=True, status=status,
        hypothesis=f"The source is a solar-system body at {lo_hyp:g}-{hi_hyp:g} AU from the Sun (hypothesis, not a finding).",
        scenario=_scenario(config), expected_parallax_arcsec=[parallax_arcsec(p.r_max_au), parallax_arcsec(p.r_min_au)],
        measured=measured, requirements=reqs, competing_explanations=competing,
        separation_of_claims={
            "unusualness": "positional-change statistic of the candidate (how inconsistent with a fixed point)",
            "physical_interpretation": "distance from the parallax fit, if measured",
            "planet_x_relevance": f"status {status}: consistency with the stated distance scenario only",
        })


def require_reliable_measurement(px: PlanetXAssessment, failed: list[str], unresolved: list[str]) -> PlanetXAssessment:
    """A hypothesis cannot be found consistent on a measurement that failed (or could not pass) critical checks.

    Called by the reasoning once the candidate's checks are known: adds the ``measurement_reliable``
    requirement and turns ``CONSISTENT_REQUIRES_CONFIRMATION`` into ``NOT_ASSESSABLE`` when it is not met.
    """
    if not px.applicable:
        return px
    ok = not failed and not unresolved
    detail = "all applicable critical checks passed" if ok else \
        f"failed critical checks: {failed or 'none'}; unresolved: {unresolved or 'none'}"
    px.requirements.append(Requirement(req_id="measurement_reliable", description="the measurement passed every applicable "
                                       "critical check (quality, association, blending, error models, independence)",
                                       state="SATISFIED" if ok else "NOT_SATISFIED", detail=detail, evidence=[]))
    if not ok and px.status == "CONSISTENT_REQUIRES_CONFIRMATION":
        px.status = "NOT_ASSESSABLE"
    return px


def region_assessment(n_tracks: int, config: EngineConfig, injection: dict | None = None, n_consistent: int = 0,
                      injection_outcome: str | None = None) -> dict:
    """What the investigated region says about the hypothesis as a whole.

    Without a moving-object injection measurement the status is NOT_ASSESSABLE. With one it is
    COMPLETENESS_LIMITED: a conditional sensitivity statement for this field, these epochs and
    this configuration, never evidence that no body exists.
    """
    base = {"n_candidate_tracks": n_tracks, "n_consistent_candidates": n_consistent, "scenario": _scenario(config),
            "disclaimer": DISCLAIMER}
    if not injection or injection.get("completeness_given_coverage") is None:
        return {"status": "NOT_ASSESSABLE",
                "reason": "moving-object detection completeness (injection of moving sources and linking efficiency) has not "
                          "been measured, so a lack of candidates is not evidence of absence", **base}
    lo, hi = injection["completeness_interval_95"]
    rows = "; ".join(f"{b['flux_ujy']:.0f} uJy: {b['recovered']}/{b['covered']}" for b in injection["by_snr"] if b["covered"])
    rows += "; by distance: " + "; ".join(f"{b['r_au'][0]:.0f}-{b['r_au'][1]:.0f} AU: {b['recovered']}/{b['covered_2_epochs']}"
                                          for b in injection.get("by_distance", []) if b["covered_2_epochs"])
    statement = (f"{n_consistent} candidate(s) consistent with the scenario. For injected bodies at {config.planetx.r_min_au:g}-"
                 f"{config.planetx.r_max_au:g} AU that stayed in the analysed area for at least two epochs, the search recovered "
                 f"{injection['n_recovered']}/{injection['n_covered_2_epochs']} (95% interval {lo:.2f}-{hi:.2f}; by flux: {rows}). "
                 f"{injection['coverage_fraction']:.0%} of injected bodies stayed in the analysed area for at least two epochs (field "
                 f"half-width {injection['field_half_width_deg']:.3f} deg). This quantifies sensitivity for this field, these epochs "
                 "and this configuration only; it is not evidence that no such body exists here, elsewhere or below these limits.")
    return {"status": "COMPLETENESS_LIMITED", "statement": statement, "injection_validity": injection_outcome,
            "completeness": {k: injection[k] for k in ("n_injected", "n_covered_2_epochs", "n_recovered", "coverage_fraction",
                                                       "completeness_given_coverage", "completeness_interval_95", "by_snr",
                                                       "by_distance", "false_links_per_experiment")}, **base}
