"""Association, temporal, spectral, reasoning, Planet X and validation-registry logic on constructed inputs."""

import math
import unittest

import numpy as np

from heimdall.contracts import AttentionLevel, CandidateKind, CheckState, Validity, ValidityReason, ValidityState
from heimdall.engine import planetx, reasoning, validation
from heimdall.engine.association import associate
from heimdall.engine.config import AssociationParams, EngineConfig, TemporalParams
from heimdall.engine.motion import MotionFit, ParallaxFit
from heimdall.engine.spectral import assemble_spectrum, compare_point_sets
from heimdall.engine.stats import chi2_significance
from heimdall.engine.temporal import SeriesPoint, build_epochs, build_series, select_time_window, variability_test
from tests.helpers import make_det

CFG = EngineConfig()
D = 1 / 3600.0


def point(obs, mjd, epoch, wl, flux, sigma=10.0, det=2, release="r1", usable=True, version="v1"):
    return SeriesPoint(obs_id=obs, mjd=mjd, epoch_index=epoch, detector=det, release=release, pipeline_version=version,
                       wavelength_um=wl, bandwidth_um=0.03, wavelength_source="WCS-WAVE", flux_ujy=flux, flux_sigma_ujy=sigma,
                       measurement="FORCED", det_id=None, quality_grade=None, peak_flags=[], usable=usable)


class AssociationTests(unittest.TestCase):
    def test_matches_within_gate_and_keeps_unmatched(self):
        dets = [make_det("a:1", "a", 150.0, 2.0), make_det("b:1", "b", 150.0 + 0.3 * D, 2.0),
                make_det("c:1", "c", 150.0 + 20 * D, 2.0)]
        clusters = associate(dets, AssociationParams())
        sizes = sorted(len(c.det_ids) for c in clusters)
        self.assertEqual(sizes, [1, 2])
        self.assertFalse(any(c.ambiguous for c in clusters))

    def test_same_observation_conflict_is_explicit(self):
        dets = [make_det("a:1", "a", 150.0, 2.0), make_det("b:1", "b", 150.0 + 0.2 * D, 2.0),
                make_det("b:2", "b", 150.0 - 0.3 * D, 2.0)]
        clusters = associate(dets, AssociationParams())
        main = next(c for c in clusters if len(c.det_ids) == 2)
        released = next(c for c in clusters if len(c.det_ids) == 1)
        self.assertTrue(main.ambiguous and "CONFLICT" in main.ambiguity_kinds)
        self.assertIn("b:1", main.det_ids)  # closer to the unambiguous member
        self.assertEqual(released.det_ids, ["b:2"])
        self.assertIn("RELEASED_FROM_CONFLICT", released.ambiguity_kinds)
        self.assertEqual(released.released_from, main.source_id)
        self.assertIn(sorted(["a:1", "b:2"]), main.alternative_groupings)

    def test_two_close_sources_seen_in_every_image_stay_two_full_sources(self):
        """Regression for the real 0.3-degree run: a bright star chained to a neighbour lost all its detections."""
        dets = []
        for k in range(10):
            dets.append(make_det(f"o{k}:A", f"o{k}", 150.0, 2.0, sigma_arcsec=0.03, snr=120.0))
            dets.append(make_det(f"o{k}:B", f"o{k}", 150.0 + 1.8 * D, 2.0, sigma_arcsec=0.03, snr=60.0))
        clusters = associate(dets, AssociationParams())
        self.assertEqual(sorted(len(c.det_ids) for c in clusters), [10, 10])
        a = next(c for c in clusters if "o0:A" in c.det_ids)
        b = next(c for c in clusters if "o0:B" in c.det_ids)
        self.assertTrue(all(x.endswith(":A") for x in a.det_ids) and all(x.endswith(":B") for x in b.det_ids))
        # Against the mean of ten 0.03" centroids, B (1.8" away) is outside the gate: a separate source, not a conflict.
        self.assertFalse(a.ambiguous or b.ambiguous)

    def test_uncertainty_sets_the_gate(self):
        far = 3.0 * D
        tight = associate([make_det("a:1", "a", 150, 2, sigma_arcsec=0.05), make_det("b:1", "b", 150 + far, 2, sigma_arcsec=0.05)],
                          AssociationParams(registration_floor_arcsec=0.1))
        loose = associate([make_det("a:1", "a", 150, 2, sigma_arcsec=1.0), make_det("b:1", "b", 150 + far, 2, sigma_arcsec=1.0)],
                          AssociationParams(registration_floor_arcsec=0.1))
        self.assertEqual(len(tight), 2)
        self.assertEqual(len(loose), 1)


class TemporalSpectralTests(unittest.TestCase):
    def test_epochs_and_time_windows(self):
        epochs, index = build_epochs([("a", 61000.0), ("b", 61001.0), ("c", 61200.0), ("d", None)], 5.0)
        self.assertEqual([e.n_observations for e in epochs], [2, 1])
        self.assertNotIn("d", index)
        keep, excluded = select_time_window([("a", 61000.0), ("b", 61300.0), ("c", None)], 60999.0, 61100.0)
        self.assertEqual(keep, ["a"])
        self.assertEqual(dict(excluded), {"b": "AFTER_TIME_WINDOW", "c": "OBSERVATION_TIME_MISSING"})

    def test_series_respect_wavelength_tolerance_detector_and_release(self):
        pts = [point("a", 61000, 0, 1.200, 100), point("b", 61200, 1, 1.205, 100), point("c", 61400, 2, 1.260, 100),
               point("d", 61400, 2, 1.201, 100, det=3), point("e", 61500, 3, 1.202, 100, release="r2")]
        series = build_series(pts, 0.5, "S")
        groups = sorted(sorted(p.obs_id for p in s.points) for s in series)
        self.assertEqual(groups, [["a", "b"], ["c"], ["d"], ["e"]])

    def test_variability_constant_versus_variable_and_minimum_data(self):
        rng = np.random.default_rng(7)
        const = [point(f"o{i}", 61000 + 100 * i, i, 1.2, 1000 + rng.normal(0, 10)) for i in range(6)]
        series = build_series(const, 0.5, "S")[0]
        out = variability_test(series, TemporalParams())
        self.assertEqual(out.status, "COMPLETED")
        self.assertLess(out.significance.sigma_equivalent, 4)
        var = [point(f"o{i}", 61000 + 100 * i, i, 1.2, 1000 + (300 if i == 2 else 0) + rng.normal(0, 10)) for i in range(6)]
        out = variability_test(build_series(var, 0.5, "S")[0], TemporalParams())
        self.assertGreater(out.significance.sigma_equivalent, 10)
        self.assertEqual(out.max_pull_obs_id, "o2")
        self.assertLess(out.leave_one_out.sigma_equivalent, 4)  # single-point excess exposed
        few = variability_test(build_series(const[:2], 0.5, "S")[0], TemporalParams())
        self.assertEqual(few.status, "INSUFFICIENT_INPUTS")
        self.assertTrue(any(r.startswith("FEWER_THAN_3") for r in few.reasons))

    def test_systematic_floor_reduces_significance(self):
        var = [point(f"o{i}", 61000 + 100 * i, i, 1.2, 1000 * (1 + 0.05 * (-1) ** i), sigma=5) for i in range(6)]
        s = build_series(var, 0.5, "S")[0]
        z0 = variability_test(s, TemporalParams()).significance.sigma_equivalent
        z1 = variability_test(s, TemporalParams(flux_systematic_fraction=0.05)).significance.sigma_equivalent
        self.assertLess(z1, z0)

    def test_spectrum_coverage_states_and_comparison(self):
        pts = [point("a", 61000, 0, 1.2, 100), point("b", 61000, 0, 1.4, 120), point("c", 61000, 0, 1.5, 130),
               point("x", 61000, 0, 3.0, 50, det=4, usable=False)]
        spec = assemble_spectrum("S", pts)
        states = {c["detector"]: c["state"] for c in spec.coverage}
        self.assertEqual(states[2], "MEASURED")
        self.assertEqual(states[4], "COVERED_NOT_USABLE")
        self.assertEqual(states[1], "NOT_IN_SELECTION")
        self.assertIsNotNone(spec.slope)
        cmp_ok = compare_point_sets("e0 vs e1", [point("a", 0, 0, 1.2, 100)], [point("b", 200, 1, 1.205, 160)], 0.5)
        self.assertEqual(cmp_ok.status, "COMPLETED")
        self.assertGreater(cmp_ok.significance.sigma_equivalent, 3)
        self.assertEqual(compare_point_sets("x", [point("a", 0, 0, 1.2, 1)], [point("b", 1, 1, 1.4, 1)], 0.5).status, "NOT_ASSESSABLE")
        self.assertEqual(compare_point_sets("x", [point("a", 0, 0, 1.2, 1)], [point("b", 1, 1, 1.2, 1, release="r2")], 0.5).status,
                         "INCOMPATIBLE")


def evidence(**kw):
    base = dict(subject_id="S1", subject_kind="SOURCE", kind=CandidateKind.VARIABILITY, category="VARIABILITY_CANDIDATE",
                statistic_name="chi2", statistic_meaning="m", statistic_method="variability_chi2",
                significance=chi2_significance(200.0, 5), ra=150.0, dec=2.0, cov=[[0.1, 0], [0, 0.1]],
                detections=[make_det("o1:1", "o1", 150, 2), make_det("o2:1", "o2", 150, 2)],
                points=[point("o1", 61000, 0, 1.2, 1000), point("o2", 61200, 1, 1.2, 1500), point("o3", 61400, 2, 1.2, 1000)],
                images={o: reasoning.ImageFacts(o, 0, "r1", "v1", 1.0, 1.0, "WCS-WAVE") for o in ("o1", "o2", "o3")},
                ambiguous=False, ambiguity_kinds=[], n_points=3, n_epochs=3, min_points=3, min_epochs=2,
                leave_one_out=chi2_significance(150.0, 4), systematics_perturbed=chi2_significance(150.0, 5),
                wavelength_span_um=0.0, tolerance_um=0.015, flux_error_scale=1.0, flux_error_scale_se=0.05)
    base.update(kw)
    return reasoning.Evidence(**base)


NO_RUN = Validity.unvalidated(ValidityReason.NO_CONTROL_RUN, scope_cell="cell")
VALID = Validity(state=ValidityState.VALIDATED_FOR_CONFIG, scope_cell="cell", validation_run_ref="V-1")


class ReasoningTests(unittest.TestCase):
    def test_strong_clean_candidate_is_watch_until_validated_then_attend(self):
        watch = reasoning.assess(evidence(), NO_RUN, CFG, None)
        self.assertEqual(watch.candidate.attention_level, AttentionLevel.WATCH)
        self.assertEqual(watch.candidate.cap_reason, ValidityReason.NO_CONTROL_RUN)
        attend = reasoning.assess(evidence(), VALID, CFG, None)
        self.assertEqual(attend.candidate.attention_level, AttentionLevel.ATTEND)
        ruled = {a.alt_id: a.state.value for a in attend.candidate.alternatives}
        self.assertEqual(ruled["statistical_fluctuation"], "RULED_OUT")

    def test_failed_critical_check_blocks_attention_even_when_validated(self):
        ev = evidence(min_neighbour_px=1.0)
        out = reasoning.assess(ev, VALID, CFG, None)
        self.assertEqual(out.candidate.attention_level, AttentionLevel.INCONCLUSIVE)
        self.assertIn("blending", out.candidate.attention_reason.failed_checks)
        self.assertEqual(out.candidate.validity.state, ValidityState.UNVALIDATED)
        self.assertEqual(out.candidate.validity.reasons[0], ValidityReason.ASSUMPTION_CHECK_FAILED)

    def test_supported_ordinary_explanation_gives_explained(self):
        ev = evidence(detections=[make_det("o1:1", "o1", 150, 2, flags=["OVERFLOW"]), make_det("o2:1", "o2", 150, 2)])
        out = reasoning.assess(ev, VALID, CFG, None)
        self.assertEqual(out.candidate.attention_level, AttentionLevel.EXPLAINED)
        supported = [a.alt_id for a in out.candidate.alternatives if a.state.value == "SUPPORTED"]
        self.assertIn("saturation_or_nonlinearity", supported)
        self.assertTrue(any(s.kind.value == "CONCLUSION" for s in out.candidate.statements))

    def test_single_point_excess_is_explained(self):
        out = reasoning.assess(evidence(leave_one_out=chi2_significance(2.0, 4)), VALID, CFG, None)
        self.assertEqual(out.candidate.attention_level, AttentionLevel.EXPLAINED)
        self.assertTrue(out.contradictions)

    def test_unresolved_critical_check_for_tracks(self):
        ev = evidence(subject_kind="TRACK", kind=CandidateKind.MOTION, statistic_method="parallax_motion_fit", points=[],
                      parallax=ParallaxFit(status="COMPLETED", reasons=[], n_points=6, parallax_arcsec=100.0,
                                           parallax_sigma_arcsec=5.0, gof=chi2_significance(5.0, 7), dof=7, chi2=5.0,
                                           distance_interval_au=[1900.0, 2300.0]),
                      motion=None, min_points=3, min_epochs=2, n_points=6)
        out = reasoning.assess(ev, VALID, CFG, None)
        self.assertEqual(out.candidate.attention_level, AttentionLevel.INCONCLUSIVE)
        self.assertIn("known_object_check", out.candidate.attention_reason.unresolved_checks)

    def test_noise_miscalibration_supports_ordinary_explanation(self):
        imgs = {o: reasoning.ImageFacts(o, 0, "r1", "v1", 1.6, 1.6, "WCS-WAVE") for o in ("o1", "o2", "o3")}
        out = reasoning.assess(evidence(images=imgs), VALID, CFG, None)
        self.assertEqual(out.candidate.attention_level, AttentionLevel.EXPLAINED)
        noise = next(c for c in out.candidate.checks if c.check_id == "noise_model_calibrated")
        self.assertEqual(noise.state, CheckState.FAILED)

    def test_noise_check_respects_estimate_precision(self):
        imprecise = {o: reasoning.ImageFacts(o, 0, "r1", "v1", 1.45, 1.05, "WCS-WAVE", psf_noise_se=0.3) for o in ("o1", "o2", "o3")}
        out = reasoning.assess(evidence(images=imprecise), VALID, CFG, None)
        noise = next(c for c in out.candidate.checks if c.check_id == "noise_model_calibrated")
        self.assertEqual(noise.state, CheckState.NOT_ASSESSABLE)
        self.assertEqual(out.candidate.attention_level, AttentionLevel.INCONCLUSIVE)
        alt = next(a for a in out.candidate.alternatives if a.alt_id == "noise_underestimated")
        self.assertEqual(alt.state.value, "NOT_ASSESSABLE")
        precise = {o: reasoning.ImageFacts(o, 0, "r1", "v1", 1.45, 1.05, "WCS-WAVE", psf_noise_se=0.02) for o in ("o1", "o2", "o3")}
        out = reasoning.assess(evidence(images=precise), VALID, CFG, None)
        self.assertEqual(out.candidate.attention_level, AttentionLevel.EXPLAINED)

    def test_motion_candidate_explained_by_measured_astrometric_error_scale(self):
        base = dict(kind=CandidateKind.MOTION, statistic_method="stationarity_chi2", points=[], min_points=2, min_epochs=1,
                    motion=MotionFit(status="COMPLETED", reasons=[], n_points=12, n_distinct_times=12, baseline_days=365,
                                     gof_linear=chi2_significance(20.0, 20)))
        ok = reasoning.assess(evidence(**base, error_scale=1.0, error_scale_se=0.05), VALID, CFG, None)
        self.assertEqual(next(a for a in ok.candidate.alternatives if a.alt_id == "astrometric_errors_underestimated").state.value,
                         "RULED_OUT")
        bad = reasoning.assess(evidence(**base, error_scale=1.6, error_scale_se=0.05,
                                        calibrated_significance=chi2_significance(30.0, 22)), VALID, CFG, None)
        self.assertEqual(bad.candidate.attention_level, AttentionLevel.EXPLAINED)
        self.assertIn("astrometric_error_model", [c.check_id for c in bad.candidate.checks if c.state is CheckState.FAILED])
        unknown = reasoning.assess(evidence(**base), VALID, CFG, None)
        self.assertIn("astrometric_error_model", unknown.candidate.attention_reason.unresolved_checks)

    def test_variability_explained_by_measured_flux_error_scale(self):
        bad = reasoning.assess(evidence(flux_error_scale=1.6, flux_error_scale_se=0.05,
                                        calibrated_significance=chi2_significance(9.0, 5)), VALID, CFG, None)
        self.assertEqual(bad.candidate.attention_level, AttentionLevel.EXPLAINED)
        alt = next(a for a in bad.candidate.alternatives if a.alt_id == "photometric_errors_underestimated")
        self.assertEqual(alt.state.value, "SUPPORTED")
        still = reasoning.assess(evidence(flux_error_scale=1.6, flux_error_scale_se=0.05,
                                          calibrated_significance=chi2_significance(150.0, 5)), VALID, CFG, None)
        self.assertEqual(still.candidate.attention_level, AttentionLevel.INCONCLUSIVE)  # critical check failed, not explained
        unknown = reasoning.assess(evidence(flux_error_scale=None, flux_error_scale_se=None), VALID, CFG, None)
        self.assertIn("photometric_error_model", unknown.candidate.attention_reason.unresolved_checks)

    def test_photometric_error_model_recovers_a_known_scale(self):
        from heimdall.engine.temporal import VariabilityResult, photometric_error_model
        rng = np.random.default_rng(3)
        results = [VariabilityResult(series_id=f"s{i}", status="COMPLETED", reasons=[], n_points=int(k) + 1, n_epochs=2,
                                     systematic_fraction=0.0, chi2=float(1.4 ** 2 * rng.chisquare(k)), dof=int(k))
                   for i, k in enumerate(rng.integers(2, 12, 400))]
        m = photometric_error_model(results)
        self.assertEqual(m["status"], "COMPLETED")
        self.assertLess(abs(m["scale"] - 1.4), 3 * m["scale_se"] + 0.02)
        self.assertEqual(photometric_error_model(results[:5])["status"], "INSUFFICIENT_INPUTS")

    def test_language_guard(self):
        reasoning.check_language("The source's flux changes with time (hypothesis).")
        for bad in ("We discovered Planet X", "confirmed detection", "This is proof"):
            with self.assertRaises(ValueError):
                reasoning.check_language(bad)


class PlanetXTests(unittest.TestCase):
    def fits(self, plx, sigma, mu=(0.1, 0.0), gof_p=0.5):
        dist = 206264.806 / plx if plx / sigma >= 3 else None
        lo = 206264.806 / (plx + 2 * sigma) if plx + 2 * sigma > 0 else None
        hi = 206264.806 / (plx - 2 * sigma) if plx - 2 * sigma > 0 else None
        p = ParallaxFit(status="COMPLETED", reasons=[], n_points=12, parallax_arcsec=plx, parallax_sigma_arcsec=sigma,
                        mu_arcsec_per_yr=list(mu), chi2=10.0, dof=19, gof=chi2_significance(-2 * math.log(gof_p), 2),
                        distance_au=dist, distance_interval_au=[lo, hi])
        m = MotionFit(status="COMPLETED", reasons=[], n_points=12, n_distinct_times=12, baseline_days=365,
                      gof_stationary=chi2_significance(500.0, 22))
        return m, p

    def evaluate(self, plx, sigma, **kw):
        m, p = self.fits(plx, sigma, **kw)
        return planetx.evaluate("T", motion=m, parallax=p, n_epoch_clusters=3, validity_state="UNVALIDATED", config=CFG,
                                evidence_prefix="T")

    def test_consistent_distance_requires_confirmation(self):
        out = self.evaluate(206.26, 1.0)  # 1000 AU
        self.assertEqual(out.status, "CONSISTENT_REQUIRES_CONFIRMATION")
        states = {r.req_id: r.state for r in out.requirements}
        self.assertEqual(states["independent_confirmation"], "NOT_SATISFIED")
        self.assertEqual(states["not_a_known_solar_system_object"], "NOT_ASSESSABLE")
        self.assertEqual(states["not_a_star"], "SATISFIED")

    def test_out_of_range_and_unbound_motion_are_inconsistent(self):
        self.assertEqual(self.evaluate(4125.0, 5.0).status, "INCONSISTENT")  # 50 AU
        self.assertEqual(self.evaluate(206.26, 1.0, mu=(400.0, 0.0)).status, "INCONSISTENT")  # faster than escape

    def test_weak_parallax_is_unconstrained_and_missing_fit_not_assessable(self):
        self.assertEqual(self.evaluate(50.0, 40.0).status, "UNCONSTRAINED")
        out = planetx.evaluate("S", motion=None, parallax=None, n_epoch_clusters=1, validity_state="UNVALIDATED",
                               config=CFG, evidence_prefix="S")
        self.assertEqual(out.status, "NOT_ASSESSABLE")
        self.assertEqual(planetx.region_assessment(0, CFG)["status"], "NOT_ASSESSABLE")

    def test_motion_bound(self):
        self.assertAlmostEqual(planetx.parallax_arcsec(1000.0), 206.2648, places=3)
        self.assertAlmostEqual(planetx.escape_speed_au_per_yr(1.0), 2 * math.pi * math.sqrt(2), places=12)
        a, b = planetx.max_separation_arcsec(1.0, 100.0), planetx.max_separation_arcsec(10.0, 100.0)
        self.assertLess(a, b)
        self.assertLess(planetx.max_separation_arcsec(10.0, 1000.0), b)
        with self.assertRaises(ValueError):
            planetx.max_separation_arcsec(1.0, 0.5)


class ValidationRegistryTests(unittest.TestCase):
    def test_null_outcome_states(self):
        rng = np.random.default_rng(8)
        self.assertEqual(validation._null_outcome(list(rng.uniform(size=400)), CFG)[2], "VALIDATED_FOR_CONFIG")
        self.assertEqual(validation._null_outcome(list(rng.uniform(size=400) ** 4), CFG)[2], "MISCALIBRATED")
        out = validation._null_outcome(list(rng.uniform(size=20)), CFG)
        self.assertEqual((out[2], out[3]), ("UNVALIDATED", ["INSUFFICIENT_SAMPLE"]))

    def test_scope_separation_synthetic_never_validates_real(self):
        reg = validation.ValidationRegistry(None)
        scope = {"origin": "SYNTHETIC", "release": "SYNTHETIC", "config_hash": CFG.hash}
        reg.add(validation._run("null_variability_constant_sources", "SYNTHETIC_INJECTION", "VARIABILITY_SIGNIFICANCE", scope, 300,
                                {}, {}, "VALIDATED_FOR_CONFIG", [], "e", "n"))
        self.assertEqual(reg.validity_for("VARIABILITY", "SYNTHETIC", "SYNTHETIC", CFG.hash).state, ValidityState.VALIDATED_FOR_CONFIG)
        real = reg.validity_for("VARIABILITY", "REAL", "spherex_qr2", CFG.hash)
        self.assertEqual(real.state, ValidityState.UNVALIDATED)
        self.assertEqual(real.reasons, (ValidityReason.NO_CONTROL_RUN,))
        other_config = reg.validity_for("VARIABILITY", "SYNTHETIC", "SYNTHETIC", "sha256:other")
        self.assertEqual(other_config.state, ValidityState.UNVALIDATED)
        self.assertEqual(reg.validity_for("TRACK", "SYNTHETIC", "SYNTHETIC", CFG.hash).reasons, (ValidityReason.NO_CONTROL_RUN,))

    def test_miscalibrated_cell_and_transient_combination(self):
        reg = validation.ValidationRegistry(None)
        scope = {"origin": "REAL", "release": "r", "config_hash": "h"}
        reg.add(validation._run("p", "REAL_DATA_EMPIRICAL", "VARIABILITY_SIGNIFICANCE", scope, 300, {}, {}, "VALIDATED_FOR_CONFIG",
                                [], "e", "n"))
        reg.add(validation._run("q", "REAL_DATA_EMPIRICAL", "DETECTION_FALSE_POSITIVE", scope, 30, {}, {}, "MISCALIBRATED",
                                ["CRITERIA_FAILED"], "e", "n"))
        self.assertEqual(reg.validity_for("TRANSIENT", "REAL", "r", "h").state, ValidityState.MISCALIBRATED)

    def test_independent_self_checks_pass(self):
        checks = validation.self_checks()
        self.assertTrue(checks)
        self.assertTrue(all(c["state"] == "PASSED" for c in checks), checks)


if __name__ == "__main__":
    unittest.main()
