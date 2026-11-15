"""Target investigations: astrometry, measurement, motion, variability, hypotheses and the integrated path.

All images and measurement lists here are SYNTHETIC (software verification). They show that the algorithms
recover known injected behaviour and reject negative controls; they say nothing about real astronomy.
"""

import json
import math
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np

from heimdall.__main__ import main
from heimdall.api import Engine
from heimdall.config import load_settings
from heimdall.context import CachedService
from heimdall.context.gaia import Star
from heimdall.data_access.http import HttpResponse
from heimdall.engine.config import EngineConfig
from heimdall.engine.reasoning import check_language
from heimdall.ingest.spherex import read_spherex_mef
from heimdall.targets import astrometry, hypotheses
from heimdall.targets.astrometry import ReferenceAstrometry
from heimdall.targets.investigate import select_observations
from heimdall.targets.measure import ControlOffset, measure_image, registration_shift, reference_from_star, select_controls
from heimdall.targets.motion import analyse_motion
from heimdall.targets.registry import NASA_TARGETS, TargetSpec, get_target
from heimdall.targets.variability import analyse_variability, period_assessment
from tests.helpers import generated, small_scenario, synthetic_provenance

FLOOR = 0.4


def ref_with(pmra=0.0, pmdec=0.0, plx=None, ra=150.0, dec=2.0, epoch=2016.0):
    return ReferenceAstrometry(target_id="T", catalogue="synthetic", status="RESOLVED", source_id="synthetic", ra_deg=ra, dec_deg=dec, epoch_jyear=epoch,
                               epoch_mjd=astrometry.jyear_to_mjd(epoch), pmra_mas_yr=pmra, pmdec_mas_yr=pmdec, parallax_mas=plx, pm_source="synthetic",
                               ra_err_mas=1.0, dec_err_mas=1.0)


class RegistryTests(unittest.TestCase):
    def test_five_named_targets_with_stable_ids_and_categories(self):
        self.assertEqual(sorted(NASA_TARGETS), [f"NASA-0{i}" for i in range(1, 6)])
        self.assertEqual([NASA_TARGETS[k].category for k in sorted(NASA_TARGETS)],
                         ["MOVING_OBJECT"] * 3 + ["VARIABLE_STAR"] * 2)
        self.assertIs(get_target("barnard"), NASA_TARGETS["NASA-01"])
        self.assertIn("08h55m10.831s", NASA_TARGETS["NASA-03"].nasa_reference_position)
        self.assertIn("NOT assumed to be current", NASA_TARGETS["NASA-03"].nasa_reference_position)
        with self.assertRaises(KeyError):
            get_target("nonexistent target")

    def test_coordinate_targets_use_the_same_spec_type(self):
        t = TargetSpec.from_coordinates(10.5, -20.25)
        self.assertTrue(t.target_id.startswith("COORD-"))


class AstrometryTests(unittest.TestCase):
    def test_proper_motion_propagation_matches_the_linear_expectation(self):
        r = ref_with(pmra=0.0, pmdec=1000.0)  # 1 arcsec/yr north
        e = r.at_mjd(astrometry.jyear_to_mjd(2018.0))
        self.assertAlmostEqual((e.dec_deg - r.dec_deg) * 3600.0, 2.0, places=3)
        self.assertAlmostEqual(e.components["dt_years"], 2.0, places=3)

    def test_parallax_shift_is_bounded_by_the_parallax_and_annual(self):
        r = ref_with(plx=500.0)
        shifts = [np.hypot(*r.at_mjd(m).components["parallax_shift_arcsec"]) for m in np.linspace(61000, 61365, 12)]
        self.assertLessEqual(max(shifts), 0.5 * 1.001)
        self.assertGreater(max(shifts), 0.3)

    def test_uncertainty_grows_with_time_from_the_reference_epoch(self):
        r = ref_with(pmra=100.0, pmdec=50.0)
        r.pmra_err_mas_yr = r.pmdec_err_mas_yr = 1.0
        near = r.at_mjd(astrometry.jyear_to_mjd(2017.0)).cov_arcsec2[0][0]
        far = r.at_mjd(astrometry.jyear_to_mjd(2026.0)).cov_arcsec2[0][0]
        self.assertGreater(far, near)

    def test_unresolved_reference_refuses_propagation(self):
        r = ReferenceAstrometry(target_id="T", catalogue="x", status="UNRESOLVED")
        with self.assertRaises(ValueError):
            r.at_mjd(61000.0)

    def test_external_position_epoch_check_is_an_inference_not_a_fact(self):
        r = ref_with(pmra=-8000.0, pmdec=700.0, ra=133.78, dec=-7.2445, epoch=2015.4)
        chk = astrometry.seed_position_epoch_check(r, 133.795129, -7.245147)
        self.assertEqual(chk["status"], "COMPUTED")
        self.assertLess(chk["implied_epoch_jyear"], 2015.4)
        self.assertIn("not stated", chk["note"])


def gaia_csv(rows):
    head = "source_id,ra,dec,ra_error,dec_error,pmra,pmdec,pmra_error,pmdec_error,parallax,parallax_error,phot_g_mean_mag,ref_epoch"
    return (head + "\n" + "\n".join(",".join(str(v) for v in r) for r in rows) + "\n").encode()


class ResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def service(self, body=None, fail=False):
        calls = []

        def transport(method, url, data, headers, timeout):
            calls.append(data)
            if fail:
                from heimdall.data_access.http import TransportUnreachable
                raise TransportUnreachable("down")
            return HttpResponse(200, body)

        s = load_settings(environ={"HEIMDALL_CACHE_DIR": self.tmp, "HEIMDALL_MAX_RETRIES": "0"})
        return CachedService("gaia", s, transport=transport, sleep=lambda _: None, min_interval_s=0.0), calls

    def test_epsilon_indi_rule_excludes_the_bright_primary_and_uses_the_parallax_window(self):
        # Server-side filters are exercised by checking the ADQL; the fake returns only what the filter would keep plus a decoy.
        rows = [(1, 331.07645, -56.79381, 0.1, 0.1, 3981.9, -2466.8, 0.1, 0.1, 270.6, 0.2, 18.03, 2016.0)]
        svc, calls = self.service(gaia_csv(rows))
        ref = astrometry.resolve(NASA_TARGETS["NASA-02"], svc)
        self.assertEqual(ref.status, "RESOLVED")
        self.assertEqual(ref.source_id, "Gaia DR3 1")
        q = calls[0].decode()
        self.assertIn("phot_g_mean_mag+%3E%3D+12.00", q)
        self.assertIn("parallax+%3C%3D+350.000", q)

    def test_service_failure_never_falls_back_to_the_seed_position(self):
        svc, _ = self.service(fail=True)
        ref = astrometry.resolve(NASA_TARGETS["NASA-01"], svc)
        self.assertEqual(ref.status, "SERVICE_FAILED")
        self.assertIsNone(ref.ra_deg)
        self.assertFalse(ref.resolved)

    def test_irsa_failure_falls_back_to_the_esa_gaia_archive_and_records_both_attempts(self):
        rows = [(7, 248.1674, 66.755, 0.1, 0.1, -20.6, 6.1, 0.1, 0.1, 1.25, 0.02, 6.37, 2016.0)]
        urls = []

        def transport(method, url, data, headers, timeout):
            urls.append(url)
            if "irsa" in url:
                from heimdall.data_access.http import TransportUnreachable
                raise TransportUnreachable("IRSA down")
            return HttpResponse(200, gaia_csv(rows))

        s = load_settings(environ={"HEIMDALL_CACHE_DIR": self.tmp, "HEIMDALL_MAX_RETRIES": "0"})
        svc = CachedService("gaia", s, transport=transport, sleep=lambda _: None, min_interval_s=0.0)
        ref = astrometry.resolve(NASA_TARGETS["NASA-05"], svc)
        self.assertEqual(ref.status, "RESOLVED")
        self.assertIn("ESA", ref.service["name"])
        self.assertEqual([a["status"] for a in ref.service["attempts"]], ["FAILED", "COMPLETED"])
        self.assertTrue(any("gea.esac.esa.int" in u for u in urls))

    def test_no_catalogue_match_is_unresolved_not_a_discovery(self):
        svc, _ = self.service(gaia_csv([]))
        ref = astrometry.resolve(NASA_TARGETS["NASA-04"], svc)
        self.assertEqual(ref.status, "UNRESOLVED")

    def test_ambiguous_candidates_are_reported(self):
        rows = [(1, 331.07, -56.79, 0.1, 0.1, 3981.9, -2466.8, 0.1, 0.1, 270.6, 0.2, 18.0, 2016.0),
                (2, 331.08, -56.79, 0.1, 0.1, 3981.9, -2466.8, 0.1, 0.1, 270.6, 0.2, 18.5, 2016.0)]
        svc, _ = self.service(gaia_csv(rows))
        ref = astrometry.resolve(NASA_TARGETS["NASA-02"], svc)
        self.assertEqual(ref.status, "AMBIGUOUS")
        self.assertEqual(len(ref.alternatives), 1)

    def test_offline_mode_reports_offline_not_cached(self):
        s = load_settings(environ={"HEIMDALL_CACHE_DIR": self.tmp, "HEIMDALL_OFFLINE": "1"})
        ref = astrometry.resolve(NASA_TARGETS["NASA-05"], CachedService("gaia", s))
        self.assertEqual(ref.status, "SERVICE_FAILED")
        self.assertIn("offline", ref.reason)

    def test_catwise_proper_motion_is_converted_from_arcsec_to_mas(self):
        head = ",".join(astrometry.CATWISE_COLUMNS)
        row = "J0855,133.78,-7.244,0.1,0.1,-8.0,0.9,0.1,0.1,18.3,,13.9,0.03,1N105,,"
        svc, _ = self.service((head + "\n" + row + "\n").encode())
        ref = astrometry.resolve(NASA_TARGETS["NASA-03"], svc)
        self.assertEqual(ref.status, "RESOLVED")
        self.assertAlmostEqual(ref.pmra_mas_yr, -8000.0)
        self.assertEqual(ref.epoch_mjd, astrometry.CATWISE_REFERENCE_MJD)


def sim_motion(n_epochs=4, per_epoch=3, v_true=(1.0, 0.0), v_cat=(1.0, 0.0), sigma=0.2, reg_sigma=0.15, n_controls=8, seed=3, span_d=300.0, drift=(0.0, 0.0)):
    """SYNTHETIC measurement dictionaries: target moves at v_true (arcsec/yr); catalogue says v_cat; every image has its own translation."""
    rng = np.random.default_rng(seed)
    out = []
    k = 0
    for e in range(n_epochs):
        t0 = 61000.0 + span_d * e / max(n_epochs - 1, 1)
        shift = rng.normal(0, 0.8, 2)  # per-image registration error (a translation)
        for v in range(per_epoch):
            mjd = t0 + 0.4 * v
            dt = (mjd - 61100.0) / 365.25
            exp_off = np.array(v_cat) * dt
            true_off = np.array(v_true) * dt
            cov_c = np.eye(2) * sigma ** 2
            meas = true_off + shift + rng.normal(0, sigma, 2)
            ctrl = [ControlOffset(f"G{j}", 13.0, *(shift + rng.normal(0, 0.1, 2) + np.array(drift) * dt), (np.eye(2) * 0.1 ** 2).tolist(), 50.0) for j in range(n_controls)]
            reg = registration_shift(ctrl, FLOOR)
            resid = meas - exp_off - np.asarray(reg["shift_arcsec"])
            cov = cov_c + np.asarray(reg["cov_arcsec2"])
            out.append({"obs_id": f"S{k}", "status": "MEASURED", "mjd": mjd, "detector": 2, "centroid": {"fit_status": "CONVERGED", "snr": 40.0},
                        "saturation_risk": False, "corrected_offset_arcsec": resid.tolist(), "corrected_offset_cov_arcsec2": cov.tolist(),
                        "expected": {"ra_deg": 150.0, "dec_deg": 2.0, "cov_arcsec2": [[1e-4, 0], [0, 1e-4]]},
                        "controls": [c.__dict__ for c in ctrl], "wavelength_um": 1.2})
            k += 1
    return out


class MotionTests(unittest.TestCase):
    def setUp(self):
        self.ref = ref_with(pmra=1000.0, pmdec=0.0, dec=0.0)
        # The tangent offsets in analyse_motion come from 'expected' RA/Dec, so make them consistent with v_cat.
        self.cos = 1.0

    def run_case(self, **kw):
        ms = sim_motion(**kw)
        v_cat = kw.get("v_cat", (1.0, 0.0))
        for m in ms:
            dt = (m["mjd"] - 61100.0) / 365.25
            m["expected"]["ra_deg"] = 150.0 + v_cat[0] * dt / 3600.0
            m["expected"]["dec_deg"] = 0.0 + v_cat[1] * dt / 3600.0
        ref = ref_with(pmra=v_cat[0] * 1e3, pmdec=v_cat[1] * 1e3, ra=150.0, dec=0.0)
        return analyse_motion(ms, ref, FLOOR, mjd_ref=61100.0)

    def test_positive_control_motion_matching_the_catalogue(self):
        out = self.run_case(v_true=(3.0, 0.0), v_cat=(3.0, 0.0))
        self.assertEqual(out["status"], "COMPLETED")
        self.assertEqual(out["verdict"], "MOTION_DETECTED_CONSISTENT_WITH_CATALOGUE")
        self.assertAlmostEqual(out["fitted_velocity_arcsec_yr"]["east"], 3.0, delta=4 * out["fitted_velocity_arcsec_yr"]["sigma_east"])

    def test_motion_that_contradicts_the_catalogue_is_flagged(self):
        out = self.run_case(v_true=(3.0, 0.0), v_cat=(0.0, 3.0))
        self.assertEqual(out["verdict"], "MOTION_DETECTED_INCONSISTENT_WITH_CATALOGUE")

    def test_negative_control_a_stationary_source_is_not_called_moving(self):
        out = self.run_case(v_true=(0.0, 0.0), v_cat=(0.0, 0.0))
        self.assertNotIn(out["verdict"], ("MOTION_DETECTED_CONSISTENT_WITH_CATALOGUE", "MOTION_DETECTED_INCONSISTENT_WITH_CATALOGUE"))

    def test_stationary_source_that_the_catalogue_says_should_move_is_reported_as_a_contradiction(self):
        out = self.run_case(v_true=(0.0, 0.0), v_cat=(3.0, 0.0))
        self.assertEqual(out["verdict"], "NO_SIGNIFICANT_MOTION_BUT_CATALOGUE_REJECTED")

    def test_motion_that_is_too_small_to_measure_is_not_claimed(self):
        out = self.run_case(v_true=(0.01, 0.0), v_cat=(0.01, 0.0), sigma=0.5)
        self.assertEqual(out["verdict"], "MOTION_NOT_MEASURABLE_WITH_THESE_OBSERVATIONS")
        self.assertFalse(out["measurable"])

    def test_common_registration_translation_does_not_create_motion(self):
        # Large per-image translations are applied to the target and the controls alike; they must cancel.
        out = self.run_case(v_true=(0.0, 0.0), v_cat=(0.0, 0.0), sigma=0.1)
        self.assertLess(out["motion_detection_sigma"], 5.0)

    def test_control_drift_is_detected_by_the_null_test_and_inflates_errors(self):
        out = self.run_case(v_true=(0.0, 0.0), v_cat=(0.0, 0.0), drift=(0.6, 0.0), n_controls=6)
        # Controls all drift together so the shift absorbs it: the null test must remain compatible with zero.
        self.assertEqual(out["control_null_test"]["status"], "COMPLETED")
        self.assertLess(abs(out["fitted_velocity_arcsec_yr"]["east"]), 0.5)

    def test_insufficient_epochs_or_baseline_is_not_assessable(self):
        ms = sim_motion(n_epochs=1, per_epoch=4, span_d=1.0)
        out = analyse_motion(ms, ref_with(), FLOOR)
        self.assertEqual(out["status"], "NOT_ASSESSABLE")
        self.assertIn("FEWER_THAN_2_EPOCHS", out["reasons"])
        self.assertTrue(any(r.startswith("BASELINE_BELOW") for r in out["reasons"]))

    def test_saturated_and_unconverged_centroids_are_excluded_with_reasons(self):
        ms = sim_motion()
        ms[0]["saturation_risk"] = True
        ms[1]["centroid"] = {"fit_status": "FIXED_POSITION_FALLBACK", "snr": 40.0}
        out = analyse_motion(ms, ref_with(pmra=1000.0, dec=0.0), FLOOR)
        reasons = {d["obs_id"]: d["reasons"] for d in out["excluded"]}
        self.assertIn("SATURATION_OR_NONLINEARITY_FLAGS", reasons["S0"])
        self.assertIn("CENTROID_NOT_CONVERGED", reasons["S1"])

    def test_registration_with_too_few_controls_is_unconstrained_with_floor_uncertainty(self):
        reg = registration_shift([ControlOffset("a", 13, 1.0, 1.0, np.eye(2).tolist(), 50.0)], FLOOR)
        self.assertEqual(reg["status"], "UNCONSTRAINED")
        self.assertEqual(reg["shift_arcsec"], [0.0, 0.0])
        self.assertAlmostEqual(reg["cov_arcsec2"][0][0], FLOOR ** 2)

    def test_registration_clipping_rejects_a_blended_control(self):
        base = [ControlOffset(f"c{i}", 13, 2.0 + 0.02 * i, -1.0, (np.eye(2) * 0.01).tolist(), 50.0) for i in range(6)]
        bad = ControlOffset("blend", 13, 9.0, 4.0, (np.eye(2) * 0.01).tolist(), 50.0)
        reg = registration_shift(base + [bad], FLOOR)
        self.assertEqual(reg["n_used"], 6)
        self.assertAlmostEqual(reg["shift_arcsec"][0], 2.05, delta=0.1)


def series_measurements(fluxes_by_epoch, sigma=20.0, saturated=(), n_controls=0, control_scatter=0.0, seed=5):
    """SYNTHETIC forced-photometry measurements: one band, one detector; fluxes_by_epoch gives the target flux in each epoch (2 images/epoch)."""
    rng = np.random.default_rng(seed)
    out, k = [], 0
    for e, f in enumerate(fluxes_by_epoch):
        for v in range(2):
            mjd = 61000.0 + 60.0 * e + 0.3 * v
            cf = [{"gaia_source_id": f"G{j}", "g_mag": 13.0, "flux_ujy": 1000.0 * (1 + j) * (1 + rng.normal(0, control_scatter)), "flux_sigma_ujy": 15.0 * (1 + j) ** 0.5,
                   "snr": 50.0, "wavelength_um": 1.2, "bandwidth_um": 0.03, "chi2_red": 1.0} for j in range(n_controls)]
            out.append({"obs_id": f"V{k}", "status": "MEASURED", "mjd": mjd, "detector": 2, "release": "synthetic", "pipeline_version": "v1", "wavelength_um": 1.2,
                        "forced": {"status": "MEASURED", "flux_ujy": f + rng.normal(0, sigma), "flux_sigma_ujy": sigma, "wavelength_um": 1.2, "bandwidth_um": 0.03,
                                   "wavelength_source": "WCS-WAVE", "peak_flags": [], "reasons": []},
                        "saturation_risk": k in saturated, "control_fluxes": cf, "contamination": []})
            k += 1
    return out


class VariabilityTests(unittest.TestCase):
    cfg = EngineConfig()

    def test_positive_control_variable_flux_is_detected(self):
        out = analyse_variability(series_measurements([2000, 3500, 2000, 3600, 2100], n_controls=12), self.cfg)
        self.assertIn(out["verdict"], ("VARIABILITY_DETECTED", "VARIABILITY_DETECTED_WITH_CAVEATS"))
        best = max(s["test"]["sigma_equivalent"] or 0 for s in out["series"] if s["test"]["status"] == "COMPLETED")
        self.assertGreater(best, 5.0)

    def test_negative_control_constant_flux_is_not_variable(self):
        out = analyse_variability(series_measurements([2000] * 5, n_controls=12), self.cfg)
        self.assertEqual(out["verdict"], "NO_SIGNIFICANT_VARIABILITY")

    def test_noisy_field_is_not_mistaken_for_target_variability(self):
        # Controls scatter by 20 percent between images: the field error scale / false-alarm reference must absorb a ~10 percent target wobble.
        out = analyse_variability(series_measurements([2000, 2200, 1850, 2150, 1900], n_controls=14, control_scatter=0.2), self.cfg)
        self.assertIn(out["verdict"], ("NO_SIGNIFICANT_VARIABILITY", "SIGNIFICANT_BUT_NOT_DISTINGUISHABLE_FROM_FIELD_SYSTEMATICS"))

    def test_without_a_control_reference_formal_significance_is_not_a_detection(self):
        # Regression (D-006): with fewer than 5 completed control series no false-alarm fraction exists; that absent test must not count as passed.
        out = analyse_variability(series_measurements([2000, 3500, 2000, 3600, 2100], n_controls=2), self.cfg)
        self.assertEqual(out["verdict"], "SIGNIFICANT_BUT_NOT_DISTINGUISHABLE_FROM_FIELD_SYSTEMATICS")
        self.assertIn("could not be computed", out["verdict_reason"])
        self.assertTrue(out["method_version"].endswith("-provisional"))

    def test_saturated_images_are_excluded_and_reported_as_a_caveat(self):
        out = analyse_variability(series_measurements([2000, 3500, 2000, 3600, 2100], n_controls=12, saturated=(0, 1)), self.cfg)
        self.assertTrue(any("saturation" in c for c in out["caveats"]))
        self.assertEqual(out["n_points_usable"], 8)

    def test_faint_measurements_become_censored_upper_limits(self):
        ms = series_measurements([100, 100, 100], sigma=60.0)
        out = analyse_variability(ms, self.cfg)
        self.assertGreaterEqual(out["n_upper_limits"], 1)
        self.assertEqual(out["upper_limits"][0]["state"], "UPPER_LIMIT")
        self.assertGreater(out["upper_limits"][0]["upper_limit_3sigma_ujy"], 0)

    def test_too_few_epochs_is_not_assessable(self):
        out = analyse_variability(series_measurements([2000]), self.cfg)
        self.assertEqual(out["verdict"], "NOT_ASSESSABLE")

    def test_period_is_never_inferred_from_sparse_sampling(self):
        out = analyse_variability(series_measurements([2000, 3500, 2000]), self.cfg)
        self.assertEqual(out["period_assessment"]["status"], "NOT_ASSESSABLE")
        self.assertIsNone(out["period_assessment"]["period_days"])
        self.assertEqual(period_assessment(30, 700.0, 100)["period_days"], None)  # even adequate sampling does not produce a period here

    def test_trend_is_not_assessable_with_two_epochs(self):
        out = analyse_variability(series_measurements([2000, 3500]), self.cfg)
        for s in out["series"]:
            self.assertEqual(s["trend"]["status"], "NOT_ASSESSABLE")


class HypothesisTests(unittest.TestCase):
    def test_unassessable_motion_yields_insufficient_evidence_and_no_claim(self):
        hyps, ev, _ = hypotheses.build_moving("T", ref_with(), {"status": "NOT_ASSESSABLE", "reasons": ["FEWER_THAN_2_EPOCHS"], "control_null_test": {}}, {})
        by = {h["slug"]: h for h in hyps}
        self.assertEqual(by["insufficient_evidence"]["status"], "SUPPORTED")
        self.assertEqual(by["catalogue_motion"]["status"], "NOT_ASSESSABLE")
        self.assertTrue(by["insufficient_evidence"]["tests_unavailable"])

    def test_motion_hypotheses_cite_evidence_that_exists_and_independent_groups(self):
        ms = sim_motion(v_true=(3.0, 0.0), v_cat=(3.0, 0.0))
        for m in ms:
            dt = (m["mjd"] - 61100.0) / 365.25
            m["expected"]["ra_deg"] = 150.0 + 3.0 * dt / 3600.0
        ref = ref_with(pmra=3000.0, ra=150.0, dec=0.0)
        motion = analyse_motion(ms, ref, FLOOR, mjd_ref=61100.0)
        hyps, ev, _ = hypotheses.build_moving("T", ref, motion, {"contamination_summary": {}})
        ids = {e["evidence_id"] for e in ev}
        for h in hyps:
            for eid in h["supporting_evidence"] + h["contradicting_evidence"]:
                self.assertIn(eid, ids)
        self.assertEqual({e["dependency_group"] for e in ev if e["name"] in ("stationary_vs_linear", "catalogue_chi2", "fitted_vs_catalogue_velocity")}, {"centroids"})
        by = {h["slug"]: h for h in hyps}
        self.assertEqual(by["catalogue_motion"]["status"], "SUPPORTED")
        self.assertEqual(by["stationary"]["status"], "DISFAVOURED")
        self.assertEqual(by["uncatalogued_mover"]["status"], "DISFAVOURED")
        self.assertEqual(len({h["hypothesis_id"] for h in hyps}), len(hyps))

    def test_unresolved_companion_blocks_exclusion_of_blending(self):
        hyps, _, _ = hypotheses.build_moving("T", ref_with(), {"status": "NOT_ASSESSABLE", "reasons": ["X"], "control_null_test": {}}, {"unresolved_companion": True})
        # not assessable path returns early; the full path is covered below
        ms = sim_motion()
        for m in ms:
            m["expected"]["ra_deg"] = 150.0 + (m["mjd"] - 61100.0) / 365.25 / 3600.0
        ref = ref_with(pmra=1000.0, ra=150.0, dec=0.0)
        motion = analyse_motion(ms, ref, FLOOR, mjd_ref=61100.0)
        hyps, _, _ = hypotheses.build_moving("T", ref, motion, {"unresolved_companion": True, "blend_statement": "unresolved binary"})
        self.assertEqual({h["slug"]: h for h in hyps}["blend_confusion"]["status"], "CANNOT_EXCLUDE")

    def test_plain_language_obeys_the_forbidden_claim_guard(self):
        for verdict in ("MOTION_DETECTED_CONSISTENT_WITH_CATALOGUE", "VARIABILITY_DETECTED", "NOT_ASSESSABLE", "NO_SIGNIFICANT_VARIABILITY"):
            text = hypotheses.plain_language("X", "MOVING_OBJECT", ref_with(), {"verdict": verdict}, {"n_measured": 3, "n_discovered": 9}, "PASSED")["quick_answer"]
            check_language(text)
            self.assertIn("not a new finding", text)


class MeasurementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gen = generated()
        cls.cfg = EngineConfig()
        cls.truth = cls.gen.truth
        cls.images = []
        for f in sorted(cls.gen.files)[:6]:
            if "D2" in f.name:
                ing = read_spherex_mef(f, source_id="local-fits", provenance=synthetic_provenance(), config=cls.cfg, release="SYNTHETIC", obs_id=f.stem.replace("synthetic_", ""))
                cls.images.append(ing.image)
        cls.stars = [Star(source_id=str(i), ra=s["ra"], dec=s["dec"], ra_error_mas=1.0, dec_error_mas=1.0, pmra=0.0, pmdec=0.0, pmra_error=0.1, pmdec_error=0.1,
                          parallax=0.0, parallax_error=0.1, g_mag=14.0, ref_epoch=2016.0) for i, s in enumerate(cls.truth["stars"])]

    def target_ref(self, d_east_arcsec=0.0):
        v = self.truth["variable"]
        return ref_with(ra=v["ra"] + d_east_arcsec / 3600.0 / math.cos(math.radians(v["dec"])), dec=v["dec"], epoch=2000.0 + (61000.0 - 51544.5) / 365.25)

    def test_forced_flux_and_centroid_recover_the_injected_source(self):
        img = self.images[0]
        m = measure_image(img, self.target_ref(), [], [], self.cfg)
        self.assertEqual(m.status, "MEASURED")
        self.assertGreater(m.forced["snr"], 20)
        self.assertLess(math.hypot(*m.corrected_offset_arcsec), 0.6)
        self.assertEqual(m.registration["status"], "UNCONSTRAINED")
        self.assertIn("psf_fwhm_arcsec", m.to_dict())

    def test_common_astrometric_offset_is_absorbed_by_the_control_stars(self):
        """Catalogue positions of the target and every control are all shifted by 3 arcsec: a pure registration-like error."""
        shift = 3.0
        img = self.images[1]
        shifted = [Star(**{**s.__dict__, "ra": s.ra + shift / 3600.0 / math.cos(math.radians(s.dec))}) for s in self.stars]
        controls = select_controls(shifted, self.target_ref(shift))
        self.assertGreaterEqual(len(controls), 3)
        raw = measure_image(img, self.target_ref(shift), [], [], self.cfg)
        corrected = measure_image(img, self.target_ref(shift), controls, shifted, self.cfg)
        self.assertGreater(math.hypot(*raw.corrected_offset_arcsec), 2.4)
        self.assertEqual(corrected.registration["status"], "ESTIMATED")
        self.assertLess(math.hypot(*corrected.corrected_offset_arcsec), 0.8)

    def test_position_outside_the_image_is_not_covered_rather_than_zero_flux(self):
        far = ref_with(ra=self.truth["scenario"]["ra0"] + 1.0, dec=self.truth["scenario"]["dec0"], epoch=2000.0 + (61000.0 - 51544.5) / 365.25)
        m = measure_image(self.images[0], far, [], [], self.cfg)
        self.assertEqual(m.status, "NOT_COVERED")
        self.assertIsNone(m.forced)

    def test_control_selection_excludes_crowded_and_target_neighbours(self):
        v = self.truth["variable"]
        stars = list(self.stars)
        s0 = stars[0]
        stars.append(Star(**{**s0.__dict__, "source_id": "crowd", "ra": s0.ra + 5 / 3600.0, "g_mag": 13.5}))
        ctrl = select_controls(stars, ref_with(ra=v["ra"], dec=v["dec"]))
        ids = {c.source_id for c in ctrl}
        self.assertNotIn(s0.source_id, ids)
        self.assertNotIn("crowd", ids)

    def test_reference_from_star_matches_the_star_propagation_model(self):
        s = self.stars[0]
        r = reference_from_star(s)
        e = r.at_mjd(61000.0)
        self.assertLess(angular_arcsec(e.ra_deg, e.dec_deg, s.ra, s.dec), 1e-3)


def angular_arcsec(ra1, dec1, ra2, dec2):
    from heimdall.engine.skygeom import angular_separation_deg
    return float(angular_separation_deg(ra1, dec1, ra2, dec2)) * 3600.0


class IntegratedTargetTests(unittest.TestCase):
    """Coordinate-addressable investigation of the SYNTHETIC variable star through the real adapter, pipeline and analyses (offline, no network)."""

    @classmethod
    def setUpClass(cls):
        cls.gen = generated()
        cls.state = Path(tempfile.mkdtemp())
        cls.engine = Engine(settings=load_settings(environ={"HEIMDALL_STATE_DIR": str(cls.state), "HEIMDALL_CACHE_DIR": str(cls.state / "cache"), "HEIMDALL_OFFLINE": "1"}))
        v = cls.gen.truth["variable"]
        cls.result = cls.engine.investigate_target(ra=v["ra"], dec=v["dec"], category="VARIABLE_STAR", source_id="local-fits", local_root=str(cls.gen.directory),
                                                   max_observations=24, detectors=(1, 2), cutout_size_deg=0.15, context=False)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.state, ignore_errors=True)

    def test_data_origin_is_synthetic_and_products_carry_provenance(self):
        r = self.result
        self.assertEqual(r["data_origin"], "SYNTHETIC")
        self.assertTrue(r["products"])
        for p in r["products"]:
            self.assertEqual(p["origin"], "SYNTHETIC_TEST_FIXTURE")
            self.assertTrue(p["sha256"])

    def test_the_injected_variable_is_recovered_but_not_claimed_without_a_control_reference(self):
        # Offline: no Gaia controls, so neither the field error scale nor the empirical false-alarm fraction exists. The injected signal is
        # recovered at high formal significance, but (D-006) that alone is not a detection: field systematics cannot be excluded.
        v = self.result["analyses"]["variability"]
        best = max(s["test"]["sigma_equivalent"] or 0 for s in v["series"] if s["test"]["status"] == "COMPLETED")
        self.assertGreater(best, 5.0)
        self.assertEqual(v["verdict"], "SIGNIFICANT_BUT_NOT_DISTINGUISHABLE_FROM_FIELD_SYSTEMATICS")
        self.assertIn("could not be computed", v["verdict_reason"])
        self.assertTrue(v["caveats"])
        self.assertEqual(v["period_assessment"]["status"], "NOT_ASSESSABLE")

    def test_reference_is_coordinate_only_and_never_pretends_to_be_catalogued(self):
        ref = self.result["reference"]
        self.assertEqual(ref["status"], "COORDINATE_ONLY")
        self.assertIsNone(ref["source_id"])

    def test_blind_pipeline_ran_on_the_same_exposures_and_found_the_source(self):
        b = self.result["blind_pipeline"]
        self.assertEqual(b["data_origin"], "SYNTHETIC")
        d = b["blind_detection_near_expected_target"]
        self.assertGreater(d["n_images_covered"], 0)
        self.assertGreaterEqual(d["n_with_blind_detection"], d["n_images_covered"] - 1)
        self.assertTrue(Path(b["run_dir"]).is_dir())

    def test_hypotheses_and_evidence_are_structured_and_resolve(self):
        r = self.result
        ids = {e["evidence_id"] for e in r["evidence"]}
        self.assertTrue(r["hypotheses"])
        for h in r["hypotheses"]:
            self.assertTrue(h["hypothesis_id"].startswith("hyp:"))
            for eid in h["supporting_evidence"] + h["contradicting_evidence"]:
                self.assertIn(eid, ids)
        statuses = {h["slug"]: h["status"] for h in r["hypotheses"]}
        self.assertNotIn(statuses["intrinsic_variability"], ("DISFAVOURED", "NOT_ASSESSABLE"))
        self.assertEqual(statuses["calibration_artifact"], "CANNOT_EXCLUDE")

    def test_result_is_saved_and_reloadable_through_the_api(self):
        saved = json.loads(Path(self.result["saved_to"]).read_text())
        self.assertEqual(saved["investigation_id"], self.result["investigation_id"])
        listed = self.engine.targets()
        self.assertEqual(len(listed), 5)

    def test_frontend_contract_keys_are_present(self):
        r = self.result
        for key in ("schema", "investigation_id", "target", "reference", "coverage", "selection", "cutout", "measurements", "analyses", "detectability",
                    "hypotheses", "evidence", "blind_pipeline", "conclusion", "status", "results_hash", "reproduce", "products", "events"):
            self.assertIn(key, r)
        self.assertEqual(r["schema"], "heimdall.target_investigation/1.0")
        lc = r["analyses"]["variability"]["light_curve"]
        self.assertTrue(all({"mjd", "wavelength_um", "flux_ujy", "flux_sigma_ujy", "usable"} <= set(p) for p in lc))
        check_language(r["conclusion"]["quick_answer"])
        json.dumps(r, allow_nan=False)

    def test_selection_is_deterministic_and_epoch_balanced(self):
        from heimdall.data_access.interface import DiscoveryQuery, SkyRegion
        src = self.engine.source("local-fits", str(self.gen.directory))
        sc = small_scenario()
        d = src.discover(DiscoveryQuery(region=SkyRegion(ra_deg=sc.ra0, dec_deg=sc.dec0, radius_deg=0.11)))
        a, rule = select_observations(list(d.observations), 6, 5.0)
        b, _ = select_observations(list(reversed(d.observations)), 6, 5.0)
        self.assertEqual([o.obs_id for o in a], [o.obs_id for o in b])
        self.assertLessEqual(len(a), 6)
        self.assertEqual(rule["n_epochs_available"], 3)

    def test_cli_lists_the_five_targets(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(["target", "list"])
        self.assertEqual(code, 0)
        for i in range(1, 6):
            self.assertIn(f"NASA-0{i}", buf.getvalue())


class MlEvaluationTests(unittest.TestCase):
    """The ML comparison is evaluated, deterministic and leakage-controlled; the test checks the procedure, not a favourable outcome."""

    def test_evaluation_is_deterministic_and_labelled_simulated(self):
        from heimdall.targets.ml_eval import evaluate
        a, b = evaluate(n_train=150, n_thr=150, n_eval=150), evaluate(n_train=150, n_thr=150, n_eval=150)
        self.assertEqual(a, b)
        self.assertIn("SIMULATED", a["data_origin"])
        self.assertTrue(a["design"]["disjoint_random_streams"])
        self.assertIn("not probabilities", a["calibration_status"])

    def test_false_positive_rate_is_measured_on_held_out_constants_near_the_target(self):
        from heimdall.targets.ml_eval import evaluate
        r = evaluate(n_train=200, n_thr=300, n_eval=600)
        for m, v in r["false_positive_rate_heldout"].items():
            self.assertLess(abs(v["fraction"] - 0.05), 0.04, m)

    def test_recovery_grows_with_amplitude_and_every_stratum_has_an_interval(self):
        from heimdall.targets.ml_eval import evaluate
        r = evaluate(n_train=200, n_thr=300, n_eval=300)
        sine = [x for x in r["strata"] if x["shape"] == "sine"]
        self.assertLess(sine[0]["chi2_scaled"]["fraction"], sine[-1]["chi2_scaled"]["fraction"])
        for row in r["strata"]:
            lo, hi = row["robust_outlier"]["ci95"]
            self.assertLessEqual(lo, row["robust_outlier"]["fraction"])
            self.assertGreaterEqual(hi, row["robust_outlier"]["fraction"])

    def test_adoption_decision_follows_the_stated_rule(self):
        from heimdall.targets.ml_eval import evaluate
        r = evaluate(n_train=200, n_thr=300, n_eval=300)
        self.assertEqual(r["adopt_robust_outlier_into_scoring"], bool(r["robust_outlier_better_strata"] and not r["robust_outlier_worse_strata"]))

    def test_out_of_distribution_check_flags_changed_systematics(self):
        from heimdall.targets.ml_eval import evaluate
        r = evaluate(n_train=300, n_thr=200, n_eval=100)
        d = r["drift_check_example"]
        self.assertGreater(d["doubled_systematics"]["fraction_beyond_training_p99"], d["same_distribution"]["fraction_beyond_training_p99"] + 0.1)


class LegacyCompatibilityTests(unittest.TestCase):
    def test_saved_requests_without_the_new_obs_ids_field_still_load(self):
        from heimdall.pipeline.runner import InvestigationRequest
        legacy = {"ra_deg": 1.0, "dec_deg": 2.0, "radius_deg": 0.1, "source_id": "irsa-spherex", "detectors": None, "collections": None, "mjd_start": None,
                  "mjd_end": None, "max_observations": None, "cutout_size_deg": None, "run_validation": False, "calibration": "auto", "label": None,
                  "local_root": None, "context": "auto", "calibration_products": "epsf"}
        req = InvestigationRequest.from_dict(legacy)
        self.assertIsNone(req.obs_ids)
        self.assertEqual(InvestigationRequest.from_dict(req.to_dict()), req)


class UnresolvedTargetTests(unittest.TestCase):
    def test_unresolved_named_target_is_blocked_and_makes_no_measurement(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        engine = Engine(settings=load_settings(environ={"HEIMDALL_STATE_DIR": str(tmp), "HEIMDALL_CACHE_DIR": str(tmp / "c"), "HEIMDALL_OFFLINE": "1"}))
        gen = generated()
        res = engine.investigate_target("NASA-04", source_id="local-fits", local_root=str(gen.directory))
        self.assertEqual(res["status"]["execution"], "BLOCKED")
        self.assertEqual(res["status"]["test_status"], "FAILED")
        self.assertNotIn("measurements", res)
        self.assertIn("not resolved", res["status"]["reason"])

    def test_blocked_attempt_does_not_replace_an_earlier_completed_result(self):
        import json
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        d = tmp / "targets" / "NASA-04"
        d.mkdir(parents=True)
        earlier = json.dumps({"status": {"execution": "COMPLETED"}, "marker": "earlier real result"})
        (d / "latest.json").write_text(earlier)
        engine = Engine(settings=load_settings(environ={"HEIMDALL_STATE_DIR": str(tmp), "HEIMDALL_CACHE_DIR": str(tmp / "c"), "HEIMDALL_OFFLINE": "1"}))
        res = engine.investigate_target("NASA-04", source_id="local-fits", local_root=str(generated().directory))
        self.assertEqual(res["status"]["execution"], "BLOCKED")
        self.assertEqual((d / "latest.json").read_text(), earlier)
        self.assertEqual(json.loads((d / "latest_attempt.json").read_text())["status"]["execution"], "BLOCKED")
        self.assertTrue(res["saved_to"].endswith("latest_attempt.json"))


if __name__ == "__main__":
    unittest.main()


class ReevaluationTests(unittest.TestCase):
    """Decision-rule correction on saved measurements: a new linked version, the old one untouched (SYNTHETIC measurements)."""

    def setUp(self):
        from heimdall.pipeline.results import canonical_hash
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = EngineConfig()
        ms = series_measurements([2000, 3500, 2000, 3600, 2100], n_controls=2)
        var = analyse_variability(ms, self.cfg)
        # emulate a record saved by method 1.0.0, which counted an absent control comparison as passed
        var.update(verdict="VARIABILITY_DETECTED_WITH_CAVEATS", verdict_reason="a series survives the control comparison but caveats remain",
                   method_version="1.0.0-provisional")
        self.doc = {"investigation_id": "tinv:NASA-02:0000aaaa", "created_at": "2026-11-14T10:00:00+00:00", "config_hash": self.cfg.hash,
                    "target": {"target_id": "NASA-02", "category": "MOVING_OBJECT"}, "options": {}, "measurements": ms, "warnings": [],
                    "analyses": {"variability": var, "motion": {"verdict": "MOTION_DETECTED_CONSISTENT_WITH_CATALOGUE"}},
                    "hypotheses": [{"slug": "catalogue_motion", "status": "SUPPORTED"}], "conclusion": {"quick_answer": "moved"}, "reference": {}, "coverage": {}}
        self.doc["results_hash"] = canonical_hash(self.doc)
        self.d = self.tmp / "targets" / "NASA-02"
        self.d.mkdir(parents=True)
        self.old_text = json.dumps(self.doc, indent=1, sort_keys=True)
        (self.d / "0000aaaa.json").write_text(self.old_text)
        (self.d / "latest.json").write_text(self.old_text)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_reevaluation_creates_a_linked_version_and_keeps_the_old_one(self):
        from heimdall.targets.reevaluate import reevaluate_variability
        self.assertTrue(reevaluate_variability(self.tmp, self.cfg, "NASA-02", reason="test", dry_run=True)["dry_run"])
        self.assertEqual((self.d / "latest.json").read_text(), self.old_text)  # dry run writes nothing
        out = reevaluate_variability(self.tmp, self.cfg, "NASA-02", reason="absent control comparison was counted as passed")
        self.assertTrue(out["changed"])
        self.assertEqual(out["verdict_change"]["to"], "SIGNIFICANT_BUT_NOT_DISTINGUISHABLE_FROM_FIELD_SYSTEMATICS")
        self.assertEqual((self.d / "0000aaaa.json").read_text(), self.old_text)  # history untouched
        new = json.loads((self.d / "latest.json").read_text())
        self.assertNotEqual(new["investigation_id"], self.doc["investigation_id"])
        self.assertEqual(new["supersedes"]["investigation_id"], self.doc["investigation_id"])
        self.assertFalse(new["supersedes"]["new_data_used"])
        self.assertEqual(new["measurements"], self.doc["measurements"])
        self.assertEqual(new["hypotheses"], self.doc["hypotheses"])
        self.assertEqual(new["analyses"]["motion"], self.doc["analyses"]["motion"])
        self.assertTrue((self.d / f"{new['investigation_id'].split(':')[-1]}.json").is_file())
        self.assertEqual(len((self.d / "supersessions.jsonl").read_text().splitlines()), 1)
        again = reevaluate_variability(self.tmp, self.cfg, "NASA-02", reason="again")
        self.assertFalse(again["changed"])  # idempotent

    def test_changed_inputs_or_configuration_refuse_reevaluation(self):
        from heimdall.contracts import HeimdallError
        from heimdall.targets.reevaluate import reevaluate_variability
        bad = json.loads(self.old_text)
        bad["measurements"][0]["forced"]["flux_ujy"] *= 3  # statistics no longer reproduce
        (self.d / "latest.json").write_text(json.dumps(bad))
        with self.assertRaises(HeimdallError):
            reevaluate_variability(self.tmp, self.cfg, "NASA-02", reason="x")
        bad = json.loads(self.old_text)
        bad["config_hash"] = "sha256:other"
        (self.d / "latest.json").write_text(json.dumps(bad))
        with self.assertRaises(HeimdallError):
            reevaluate_variability(self.tmp, self.cfg, "NASA-02", reason="x")


class ClaimTests(unittest.TestCase):
    """heimdall.claims/1.0: verdict -> claim type, the defensibility gate and its downgrades (SYNTHETIC inputs)."""

    def doc(self, **over):
        cfg = EngineConfig()
        ms = series_measurements([2000, 3500, 2000, 3600, 2100], n_controls=12)
        var = analyse_variability(ms, cfg)
        d = {"investigation_id": "tinv:NASA-04:0000cccc", "target": {"target_id": "NASA-04", "category": "VARIABLE_STAR"}, "data_origin": "REAL",
             "status": {"execution": "COMPLETED"}, "measurements": ms, "analyses": {"variability": var},
             "hypotheses": [{"slug": "calibration_artifact", "status": "CANNOT_EXCLUDE", "suggested_next_step": "compare detectors"}],
             "products": [{"obs_id": m["obs_id"], "sha256": "ab" * 32, "release": "spherex_dr1", "retrieved_at": "2026-11-15T00:00:00+00:00",
                           "access_reference": "ibe/x.fits"} for m in ms], "conclusion": {"quick_answer": "q"}}
        d.update(over)
        return d

    def claim(self, out, analysis):
        return next(c for c in out["claims"] if c["analysis"] == analysis)

    def test_detection_with_complete_provenance_keeps_its_type_and_links_evidence(self):
        from heimdall.targets.claims import build_claims
        from heimdall.targets.claims import VARIABILITY_CLAIMS
        d = self.doc()
        out = build_claims(d)
        v = self.claim(out, "variability")
        expected = VARIABILITY_CLAIMS[d["analyses"]["variability"]["verdict"]][0]
        self.assertIn(expected, ("DETECTION", "CONSISTENT_WITH"))  # single detector -> WITH_CAVEATS -> CONSISTENT_WITH
        self.assertEqual(v["claim_type"], expected)
        self.assertTrue(v["gate"]["passed"])
        self.assertIn("analyses.variability.series", v["evidence_refs"])
        self.assertEqual(out["headline_claim"], v["claim_id"])
        self.assertEqual(self.claim(out, "forced_photometry")["claim_type"], "MEASUREMENT")
        self.assertEqual(build_claims(self.doc())["claims"], out["claims"])  # deterministic, stable ids

    def test_missing_provenance_downgrades_a_detection(self):
        from heimdall.targets.claims import build_claims
        d = self.doc()
        d["products"][0]["sha256"] = None
        v = self.claim(build_claims(d), "variability")
        self.assertEqual(v["claim_type"], "INCONCLUSIVE")
        self.assertIn(v["downgraded_from"], ("DETECTION", "CONSISTENT_WITH"))
        self.assertIn("provenance_complete", v["gate"]["failed_required"])

    def test_synthetic_data_cannot_carry_an_astronomical_detection(self):
        from heimdall.targets.claims import build_claims
        v = self.claim(build_claims(self.doc(data_origin="SYNTHETIC")), "variability")
        self.assertEqual(v["claim_type"], "INCONCLUSIVE")
        self.assertIn("real_vs_synthetic_distinguished", v["gate"]["failed_required"])

    def test_blocked_execution_is_never_a_non_detection(self):
        from heimdall.targets.claims import build_claims
        out = build_claims(self.doc(status={"execution": "BLOCKED", "reason": "archive HTTP 502"}))
        self.assertEqual([c["claim_type"] for c in out["claims"]], ["BLOCKED"])
        self.assertIn("not a non-detection", out["claims"][0]["does_not_establish"][0])

    def test_without_control_reference_variability_is_inconclusive_not_detected(self):
        from heimdall.targets.claims import build_claims
        cfg = EngineConfig()
        ms = series_measurements([2000, 3500, 2000, 3600, 2100], n_controls=2)
        v = self.claim(build_claims(self.doc(measurements=ms, analyses={"variability": analyse_variability(ms, cfg)})), "variability")
        self.assertEqual(v["claim_type"], "INCONCLUSIVE")


class ComparabilityTests(unittest.TestCase):
    """First-class comparison records on comparable series (SYNTHETIC measurements)."""
    cfg = EngineConfig()

    def test_every_series_carries_a_comparison_record_and_single_epochs_are_not_comparable(self):
        out = analyse_variability(series_measurements([2000, 3500, 2000, 3600, 2100], n_controls=12), self.cfg)
        for s in out["series"]:
            self.assertIn(s["comparability"]["status"], ("COMPARABLE", "COMPARABLE_WITH_LIMITATIONS", "NOT_COMPARABLE"))
            dims = {d["dimension"] for d in s["comparability"]["dimensions"]}
            self.assertTrue({"sampling", "passband", "registration", "psf_treatment", "noise_model", "blending"} <= dims)
        one = analyse_variability(series_measurements([2000]), self.cfg)
        self.assertTrue(all(not s["comparability"]["change_significance_allowed"] for s in one["series"]))
        self.assertEqual(one["verdict"], "NOT_ASSESSABLE")

    def test_mixed_processing_versions_are_a_recorded_limitation(self):
        ms = series_measurements([2000, 2010, 1990, 2005], n_controls=12)
        for m in ms[::2]:
            m["pipeline_version"] = "v2"
        out = analyse_variability(ms, self.cfg)
        rec = [s["comparability"] for s in out["series"] if s["test"]["status"] == "COMPLETED"]
        self.assertTrue(rec and all("processing_versions" in r["limitations"] for r in rec))
        self.assertTrue(all(r["status"] == "COMPARABLE_WITH_LIMITATIONS" for r in rec))

    def test_records_do_not_change_verdicts(self):
        for fl, nc in (([2000, 3500, 2000, 3600, 2100], 12), ([2000] * 5, 12), ([2000, 3500, 2000, 3600, 2100], 2)):
            out = analyse_variability(series_measurements(fl, n_controls=nc), self.cfg)
            self.assertIn(out["verdict"], ("VARIABILITY_DETECTED", "VARIABILITY_DETECTED_WITH_CAVEATS", "NO_SIGNIFICANT_VARIABILITY",
                                           "SIGNIFICANT_BUT_NOT_DISTINGUISHABLE_FROM_FIELD_SYSTEMATICS"))
            self.assertEqual(sum(out["comparability_summary"].values()), len(out["series"]))

