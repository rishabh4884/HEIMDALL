"""Moving-object injection accounting: truth attribution, track classes, recovery, duplicates, status and re-analysis (SYNTHETIC)."""

import contextlib
import copy
import dataclasses
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from heimdall.api import Engine
from heimdall.config import load_settings
from heimdall.engine import planetx, validation
from heimdall.engine.association import gate_d2
from heimdall.engine.config import EngineConfig
from heimdall.engine.linking import LinkingResult
from heimdall.engine.stats import poisson_interval
from heimdall.pipeline.runner import InvestigationRequest
from tests.helpers import generated, small_scenario

CFG = EngineConfig()
G, FLOOR, FACTOR = gate_d2(CFG.association), CFG.association.registration_floor_arcsec, CFG.validation.attribution_ambiguous_factor
R0 = (G ** 0.5) * (0.1 ** 2 + FLOOR ** 2) ** 0.5  # attribution radius for sigma = 0.1 arcsec (about 1.42 arcsec)
# one injected body seen in 4 images: epoch 0 (a, b) and epoch 1 (c, d); declination 0 so arcsec = ra offset / 3600
POS = {"a": (150.0, 0.0), "b": (150.001, 0.0), "c": (150.01, 0.0), "d": (150.011, 0.0)}
EPOCH = {"a": 0, "b": 0, "c": 1, "d": 1}


def det(obs, offset_arcsec=0.2, sigma=0.1, foreign=False):
    ra = 150.2 if foreign else POS[obs][0] + offset_arcsec / 3600.0
    return {"obs_id": obs, "epoch": EPOCH[obs], "ra": ra, "dec": 0.0, "sigma_arcsec": sigma}


def body(name="E0B0", covered=(0, 1)):
    return {"id": name, "r_au": 500.0, "flux_ujy": 600.0, "snr": 8.0, "epochs_covered": list(covered), "inside_obs": list(POS),
            "positions": {o: list(v) for o, v in POS.items()}}


def track(tid, dets, pre_existing=False):
    return {"track_id": tid, "ambiguous_link": False, "distance_au": 500.0, "pre_existing": pre_existing, "dets": dets}


def experiment(e, tracks, covered=(0, 1)):
    return {"experiment": e, "truncated": False, "bodies": [body(f"E{e}B0", covered)], "tracks": tracks}


def attribute(tracks, **kw):
    return validation.attribute_experiment({"bodies": [body()], "tracks": tracks}, gate=G, floor_arcsec=FLOOR, ambiguous_factor=FACTOR, **kw)


def summarize(experiments, config=CFG):
    return validation.summarize_injection(experiments, config, flux_sigma=70.0, half_width_deg=0.075, baseline_tracks=0, snr_grid=(8.0, 15.0, 30.0))


PURE = lambda tid="T0": track(tid, [det("a"), det("b"), det("c"), det("d")])
MIXED = lambda tid="T1": track(tid, [det("a"), det("b"), det("c", foreign=True), det("d", foreign=True)])


class AttributionTests(unittest.TestCase):
    def test_valid_pure_track_is_the_recovery(self):
        out = attribute([PURE()])
        t, b = out["tracks"][0], out["bodies"][0]
        self.assertEqual((t["class"], t["role"], t["n_body"], t["epochs"]), ("pure", "recovery", 4, [0, 1]))
        self.assertTrue(b["recovered"] and b["extra_pure"] == 0 and b["outcome"] == "recovered")

    def test_body_tracklet_joined_to_a_foreign_tracklet_is_mixed_and_not_a_recovery(self):
        out = attribute([MIXED()])
        t, b = out["tracks"][0], out["bodies"][0]
        self.assertEqual((t["class"], t["role"], t["n_body"], t["n_foreign"], t["body_epochs"]), ("mixed", "spurious_mixed", 2, 2, [0]))
        self.assertFalse(b["recovered"])
        self.assertEqual((b["outcome"], b["body_detections_in_ge2_epochs_any_track"]), ("mixed_only", False))

    def test_multiple_pure_tracks_of_one_body_are_duplicates(self):
        out = attribute([track("Tshort", [det("a"), det("c")]), PURE("Tfull")])
        roles = {t["track_id"]: t["role"] for t in out["tracks"]}
        self.assertEqual(roles, {"Tshort": "extra_pure", "Tfull": "recovery"})  # the track with more epochs/detections is the recovery
        self.assertEqual((out["bodies"][0]["n_pure"], out["bodies"][0]["extra_pure"]), (2, 1))

    def test_track_without_injected_body_detections(self):
        out = attribute([track("Tf", [det("a", foreign=True), det("c", foreign=True)])])
        self.assertEqual((out["tracks"][0]["class"], out["tracks"][0]["role"]), ("no_body", "spurious_no_body"))
        self.assertEqual(out["bodies"][0]["outcome"], "no_track")

    def test_ambiguity_near_the_tolerance_boundary(self):
        inside, band, outside = R0 * 0.98, R0 * 1.3, R0 * FACTOR * 1.1
        self.assertEqual(attribute([track("t", [det("a", inside), det("c", inside)])])["tracks"][0]["class"], "pure")
        amb = attribute([track("t", [det("a", inside), det("b", inside), det("c", band)])])
        self.assertEqual((amb["tracks"][0]["class"], amb["tracks"][0]["n_ambiguous"]), ("ambiguous", 1))
        self.assertFalse(amb["bodies"][0]["recovered"])  # an ambiguous attribution is never a clean recovery
        self.assertEqual(attribute([track("t", [det("a", inside), det("b", inside), det("c", outside)])])["tracks"][0]["class"], "mixed")
        # an explicit fixed radius replaces the gate (reanalysis sensitivity)
        self.assertEqual(attribute([track("t", [det("a", 1.49), det("c", 1.49)])], fixed_radius_arcsec=1.5)["tracks"][0]["class"], "pure")
        self.assertEqual(attribute([track("t", [det("a", 1.51), det("c", 1.51)])], fixed_radius_arcsec=1.5)["tracks"][0]["class"], "ambiguous")

    def test_pure_track_after_a_mixed_track_is_not_false_and_order_is_irrelevant(self):
        for order in ((MIXED(), PURE()), (PURE(), MIXED())):
            out = attribute(list(order))
            roles = {t["track_id"]: t["role"] for t in out["tracks"]}
            self.assertEqual(roles, {"T0": "recovery", "T1": "spurious_mixed"})
            self.assertTrue(out["bodies"][0]["recovered"])

    def test_field_tracks_are_excluded_and_attribution_ignores_linker_flags(self):
        field = track("Tfield", [det("a", foreign=True), det("c", foreign=True)], pre_existing=True)
        linked_but_wrong = {**MIXED(), "ambiguous_link": False}
        out = attribute([field, linked_but_wrong])
        self.assertEqual([t["class"] for t in out["tracks"]], ["pre_existing", "mixed"])

    def test_two_bodies_each_detection_goes_to_at_most_one(self):
        two = {"bodies": [body("E0B0"), {**body("E0B1"), "positions": {o: [v[0] + 0.0004, v[1]] for o, v in POS.items()}}],  # 1.4 arcsec apart
               "tracks": [PURE()]}
        out = validation.attribute_experiment(two, gate=G, floor_arcsec=FLOOR, ambiguous_factor=FACTOR)
        self.assertEqual(out["tracks"][0]["class"], "ambiguous")  # two bodies inside the band: not attributable


class StatusAndDenominatorTests(unittest.TestCase):
    def make(self, n_pure=100, n_mixed=0, n_ambiguous=0, n_nobody=0):
        experiments = []
        for e in range(100):
            tr = [PURE("P")] if e < n_pure else []
            tr += [MIXED(f"M{k}") for k in range(1 if e < n_mixed else 0)]
            tr += [track(f"A{k}", [det("a"), det("b"), det("c", 1.8)]) for k in range(1 if e < n_ambiguous else 0)]
            tr += [track(f"N{k}", [det("a", foreign=True), det("c", foreign=True)]) for k in range(1 if e < n_nobody else 0)]
            experiments.append(experiment(e, tr))
        return experiments

    def test_clean_run_validates_and_denominators_are_stated(self):
        m, crit, outcome, reasons = summarize(self.make())
        self.assertEqual((outcome, reasons), ("VALIDATED_FOR_CONFIG", []))
        self.assertEqual((m["n_injected"], m["n_covered_2_epochs"], m["n_recovered"], m["n_experiments"]), (100, 100, 100, 100))
        self.assertEqual(m["spurious_tracks_definite"], 0)
        self.assertEqual(sum(m["track_classes"].values()), 100)
        self.assertEqual(crit["max_false_links_per_experiment_upper95"], 0.5)  # acceptance threshold unchanged

    def test_interval_straddling_the_threshold_has_a_specific_reason_not_insufficient_sample(self):
        m, _, outcome, reasons = summarize(self.make(n_pure=100, n_mixed=49))
        self.assertEqual(outcome, "UNVALIDATED")
        self.assertEqual(reasons, [validation.FALSE_LINK_UNRESOLVED])
        lo, hi = poisson_interval(49)
        self.assertEqual(m["false_links_interval_95"], [lo / 100, hi / 100])
        self.assertLessEqual(lo / 100, 0.5 < hi / 100)
        self.assertEqual(m["n_covered_2_epochs"], 100)  # the sample minimum itself is met

    def test_miscalibrated_only_when_the_definite_lower_bound_exceeds_the_threshold(self):
        experiments = [experiment(e, [PURE("P"), MIXED("M1"), MIXED("M2")]) for e in range(100)]  # 200 spurious tracks
        m, _, outcome, reasons = summarize(experiments)
        self.assertEqual((outcome, reasons), ("MISCALIBRATED", ["CRITERIA_FAILED"]))
        self.assertGreater(m["false_links_interval_95"][0], 0.5)

    def test_ambiguous_tracks_can_prevent_validation_but_never_cause_miscalibration(self):
        m, _, outcome, reasons = summarize(self.make(n_pure=100, n_ambiguous=100))
        self.assertEqual((outcome, reasons), ("UNVALIDATED", [validation.FALSE_LINK_UNRESOLVED]))
        self.assertEqual((m["spurious_tracks_definite"], m["spurious_tracks_with_ambiguous"]), (0, 100))
        self.assertLess(m["false_links_interval_95"][1], 0.5)  # the definite count alone would have validated

    def test_small_sample_and_unevaluable_runs_cannot_validate(self):
        _, _, outcome, reasons = summarize(self.make()[:50])
        self.assertEqual((outcome, reasons), ("UNVALIDATED", ["INSUFFICIENT_SAMPLE"]))
        _, _, outcome, reasons = summarize([experiment(e, [], covered=(0,)) for e in range(100)])
        self.assertEqual((outcome, reasons), ("NOT_ASSESSABLE", ["NO_COVERED_INJECTIONS"]))

    def test_each_track_has_exactly_one_role_and_classes_partition_tracks(self):
        m, *_ = summarize(self.make(n_pure=100, n_mixed=30, n_ambiguous=20, n_nobody=10))
        total = sum(len(a["tracks"]) for a in m["attribution_results"])
        self.assertEqual(sum(m["track_classes"].values()), total)
        self.assertEqual(sum(m["track_roles"].values()), total)
        self.assertEqual(m["spurious_tracks_definite"], 40)
        self.assertEqual(m["spurious_tracks_with_ambiguous"], 60)
        self.assertEqual(sum(m["body_outcomes"].values()), m["n_injected"])

    def test_serialization_is_deterministic_and_reanalysis_is_exact_and_non_mutating(self):
        m1, *_ = summarize(self.make(n_pure=90, n_mixed=20, n_ambiguous=5))
        m2, *_ = summarize(self.make(n_pure=90, n_mixed=20, n_ambiguous=5))
        self.assertEqual(json.dumps(m1, sort_keys=True, default=str), json.dumps(m2, sort_keys=True, default=str))
        saved = json.loads(json.dumps(m1, sort_keys=True))  # as stored in a validation record
        before = copy.deepcopy(saved)
        again, _, outcome, _ = validation.reanalyze_injection(saved, CFG)
        self.assertEqual(saved, before)  # the saved record is untouched
        for key in ("n_recovered", "track_classes", "spurious_tracks_definite", "false_links_interval_95", "completeness_interval_95"):
            self.assertEqual(again[key], m1[key], key)
        sens, *_ = validation.reanalyze_injection(saved, CFG, fixed_radius_arcsec=3.0)
        self.assertEqual(sens["attribution"]["fixed_radius_arcsec"], 3.0)

    def test_backward_compatible_keys_feed_the_region_statement(self):
        m, *_ = summarize(self.make())
        for key in ("n_injected", "n_covered_2_epochs", "n_recovered", "coverage_fraction", "completeness_given_coverage",
                    "completeness_interval_95", "by_snr", "by_distance", "false_links_per_experiment", "field_half_width_deg"):
            self.assertIn(key, m)
        out = planetx.region_assessment(0, CFG, m, 0, "VALIDATED_FOR_CONFIG")
        self.assertEqual(out["status"], "COMPLETENESS_LIMITED")


class ValidatorIntegrationTests(unittest.TestCase):
    """The real validator on a synthetic scene with `_link_field` mocked."""

    def run_validator(self, truncated):
        tmp = Path(tempfile.mkdtemp())
        try:
            gen, sc = generated(), small_scenario()
            cfg = dataclasses.replace(CFG, validation=dataclasses.replace(CFG.validation, moving_object_experiments=1))
            engine = Engine(settings=load_settings(environ={"HEIMDALL_STATE_DIR": str(tmp)}), config=cfg)
            fake = LinkingResult(tracks=[], n_inputs=0, n_feasible_pairs=0, n_evaluations=0, truncated=truncated, r_min_au=100.0, notes=[])
            boom = mock.Mock(side_effect=RuntimeError("stubbed: not under test"))
            names = ("self_check_run", "injection_recovery", "null_detection", "null_variability", "null_motion", "noise_summary",
                     "psf_mismatch", "robustness", "reproducibility")
            with contextlib.ExitStack() as stack:
                for n in names:
                    stack.enter_context(mock.patch.object(validation, n, boom))
                stack.enter_context(mock.patch.object(validation, "_link_field", return_value=(fake, {})))
                result, _ = engine.investigate(InvestigationRequest(ra_deg=sc.ra0, dec_deg=sc.dec0, radius_deg=0.11, source_id="local-fits",
                                                                    local_root=str(gen.directory), run_validation=True))
            return next(v for v in result["validation"]["runs"] if v["procedure"] == "moving_object_injection")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_truncated_search_stays_not_assessable(self):
        run = self.run_validator(truncated=True)
        self.assertEqual((run["outcome"], run["reasons"]), ("NOT_ASSESSABLE", ["LINKING_SEARCH_TRUNCATED"]))
        self.assertNotIn("n_recovered", run["metrics"])

    def test_complete_search_preserves_truth_classification_and_legacy_accounting(self):
        run = self.run_validator(truncated=False)
        m = run["metrics"]
        self.assertEqual(run["outcome"], "UNVALIDATED")  # 8 injected bodies are far below the minimum sample
        self.assertEqual(run["reasons"], ["INSUFFICIENT_SAMPLE"])
        self.assertEqual((m["n_injected"], m["n_recovered"], m["spurious_tracks_definite"], m["n_experiments"]), (8, 0, 0, 1))
        body = m["experiments"][0]["bodies"][0]
        self.assertTrue(body["positions"] and body["inside_obs"] and "epochs_covered" in body)  # truth preserved
        self.assertEqual(m["attribution_results"][0]["bodies"][0]["outcome"], "no_track")
        self.assertEqual(m["legacy_validation_1_2"]["n_recovered"], 0)
        self.assertIn("validation-1.3.0", json.dumps(m["software"]))
        json.dumps(m)  # serialisable


if __name__ == "__main__":
    unittest.main()
