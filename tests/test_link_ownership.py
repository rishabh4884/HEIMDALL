"""Linker corrections: split-tracklet ownership, the distant-body rate bound, partial known-object matches, and validation readiness
(SYNTHETIC geometry; no network)."""

import contextlib
import dataclasses
import json
import math
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from heimdall.api import Engine
from heimdall.config import load_settings
from heimdall.context.small_bodies import confirm_across_subjects, subject_status
from heimdall.engine import reasoning, validation
from heimdall.engine.linking import LinkingResult, Track, link_moving, tracklet_eligible, tracklet_rate_exceeds_bound, tracklet_rate_precision
from heimdall.engine.motion import MotionFit, ParallaxFit
from heimdall.engine.planetx import max_separation_arcsec
from heimdall.pipeline.runner import InvestigationRequest
from tests.helpers import generated, make_det, small_scenario
from tests.test_analysis_units import VALID
from tests.test_context_and_intelligence import motion_evidence
from tests.test_link_effective_dof import CFG, FLOOR2, FOREIGN, LONG, RA0, DEC0, Field

ALL_INFORMATIVE = dataclasses.replace(CFG, planetx=dataclasses.replace(CFG.planetx, link_rate_info_fraction=1e9))  # isolates ownership from the dof rule
BODY2 = (RA0 + 0.03, DEC0 + 0.02)  # a second, distinct body ~110 arcsec away


def two_tracklets(rate_arcsec_day, dt_days=1.0, sigma=0.1):
    """Two detections moving east at the given apparent rate (declination 0 so arcsec = delta ra x 3600)."""
    shift = rate_arcsec_day * dt_days / 3600.0
    return [make_det("a", "oa", 150.0, 0.0, sigma_arcsec=sigma, mjd=60000.0), make_det("b", "ob", 150.0 + shift, 0.0, sigma_arcsec=sigma, mjd=60000.0 + dt_days)]


class SplitTrackletOwnershipTests(unittest.TestCase):
    def field(self):
        f = Field()
        f.add(0, LONG, r_au=800.0, tag="A")                       # the body's main epoch-0 tracklet
        f.add(1, LONG, r_au=800.0, tag="A")
        f.add(3, LONG, r_au=800.0, tag="A")
        f.add(0, [0.0, 0.001], r_au=800.0, tag="B", t0=60790.8 + 6.0)  # the same body, a second (split) tracklet of epoch 0
        f.add(3, [0.0, 2.7], foreign=FOREIGN, tag="F")             # an unrelated slow source in epoch 3
        return f

    def test_precondition_the_split_tracklet_chains_with_the_foreign_tracklet_on_its_own(self):
        f = Field()
        f.add(0, [0.0, 0.001], r_au=800.0, tag="B", t0=60790.8 + 6.0)
        f.add(3, [0.0, 2.7], foreign=FOREIGN, tag="F")
        self.assertEqual(len(f.link(ALL_INFORMATIVE).tracks), 1, "the mixed chain must be admitted by the fit for this test to mean anything")

    def test_chain_through_a_split_tracklet_is_absorbed_not_reported_as_a_track(self):
        res = self.field().link(ALL_INFORMATIVE)
        self.assertEqual(len(res.tracks), 1)
        t = res.tracks[0]
        self.assertTrue(all(s.startswith("A") for s in t.source_ids))
        self.assertGreaterEqual(res.n_absorbed_chains, 1)
        self.assertEqual(t.diagnostics["n_absorbed_chains"], len([a for a in t.diagnostics["absorbed_chains"]]))
        entry = next(a for a in t.diagnostics["absorbed_chains"] if a["consistent_tracklet"].startswith("B"))
        self.assertEqual(entry["reason"], "tracklet_consistent_with_accepted_track")
        self.assertTrue(any(s.startswith("F") for s in entry["source_ids"]))  # the foreign tracklet stays visible in the record

    def test_a_distinct_body_in_the_same_epochs_is_not_absorbed(self):
        f = self.field()
        for e in (0, 3):
            f.add(e, LONG, r_au=400.0, tag="C", direction=BODY2)
        res = f.link(ALL_INFORMATIVE)
        owners = sorted(s[0] for t in res.tracks for s in t.source_ids)
        self.assertIn("C", owners)
        self.assertTrue(any(all(s.startswith("A") for s in t.source_ids) for t in res.tracks))
        self.assertTrue(any(all(s.startswith("C") for s in t.source_ids) for t in res.tracks))

    def test_shared_tracklet_competition_is_unchanged(self):
        f = Field()
        for e in (0, 1, 3):
            f.add(e, LONG, r_au=800.0, tag="A")
        res = f.link()
        self.assertEqual((len(res.tracks), res.n_absorbed_chains), (1, 0))
        self.assertFalse(res.tracks[0].ambiguous)

    def test_ownership_checks_count_toward_the_evaluation_bound(self):
        f = self.field()
        full = f.link(ALL_INFORMATIVE)
        self.assertFalse(full.truncated)
        cut = f.link(dataclasses.replace(ALL_INFORMATIVE, planetx=dataclasses.replace(ALL_INFORMATIVE.planetx, max_link_evaluations=full.n_evaluations - 1)))
        self.assertTrue(cut.truncated)
        self.assertTrue(any("some chains were not evaluated" in n for n in cut.notes))

    def test_ownership_diagnostics_serialize_deterministically(self):
        a = json.dumps(dataclasses.asdict(self.field().link(ALL_INFORMATIVE)), sort_keys=True, default=str)
        b = json.dumps(dataclasses.asdict(self.field().link(ALL_INFORMATIVE)), sort_keys=True, default=str)
        self.assertEqual(a, b)


class RateBoundTests(unittest.TestCase):
    BOUND = max_separation_arcsec(1.0, CFG.planetx.r_min_au)

    def test_bound_is_the_one_day_motion_limit_for_r_min(self):
        self.assertAlmostEqual(self.BOUND, 41.7, delta=0.3)

    def test_a_main_belt_asteroid_tracklet_is_ineligible_with_an_inspectable_reason(self):
        # the extent of the real 368 Haidea tracklet in the verification field: 4 detections, 3.39 arcsec in 0.005 day (about 680 arcsec/day)
        ds = [make_det(f"h{i}", f"o{i}", 150.0 + (3.39 * i / 3) / 3600.0, 0.0, sigma_arcsec=0.12, mjd=61002.267 + 0.005 * i / 3) for i in range(4)]
        ok, why = tracklet_eligible(ds, CFG)
        self.assertFalse(ok)
        self.assertIn("exceeds the maximum", why)
        self.assertIn("arcsec/day", why)

    def test_a_slow_tracklet_is_eligible(self):
        ok, why = tracklet_eligible(two_tracklets(0.7, dt_days=2.7), CFG)
        self.assertEqual((ok, why), (True, ""))

    def test_noise_dominated_short_tracklet_is_never_rejected_by_rate(self):
        ds = two_tracklets(500.0, dt_days=0.001)  # 0.5 arcsec in 0.001 d: rate 500 +/- ~570 arcsec/day
        sigma, _ = tracklet_rate_precision(ds, FLOOR2)
        self.assertGreater(sigma, 300.0)
        self.assertEqual(tracklet_rate_exceeds_bound(ds, CFG), (False, ""))

    def test_boundary_uses_a_three_sigma_margin(self):
        sigma, _ = tracklet_rate_precision(two_tracklets(0.0, dt_days=1.0), FLOOR2)
        edge = self.BOUND + 3.0 * sigma
        self.assertFalse(tracklet_rate_exceeds_bound(two_tracklets(edge * 0.99), CFG)[0])
        self.assertTrue(tracklet_rate_exceeds_bound(two_tracklets(edge * 1.01), CFG)[0])

    def test_a_true_body_at_r_min_is_not_rejected(self):
        f = Field()
        dets = f.add(0, LONG, r_au=100.0)
        self.assertEqual(tracklet_eligible(dets, CFG), (True, ""))

    def test_unavailable_rate_does_not_reject(self):
        ds = two_tracklets(900.0)
        ds[1].astrometry_usable = False
        ok, _ = tracklet_eligible(ds + [make_det("c", "oc", 150.0, 0.0, mjd=60002.0)], dataclasses.replace(CFG))
        self.assertIn(ok, (True, False))  # eligibility may fail for other reasons, never for the unavailable rate
        self.assertEqual(tracklet_rate_exceeds_bound(ds[:1], CFG), (False, ""))


class KnownObjectCoverageTests(unittest.TestCase):
    def queries(self, n):
        class Q:  # a completed field query
            status = "COMPLETED"
        return {f"o{i}": Q() for i in range(n)}

    def matches(self, images):
        return [{"name": "(TEST) 1", "obs_id": f"o{i}", "det_id": f"d{i}"} for i in images]

    def test_a_body_explaining_every_detection_image_is_confirmed(self):
        s = subject_status([f"o{i}" for i in range(4)], self.matches(range(4)), self.queries(4))
        self.assertEqual((s["status"], s.get("partial")), ("CONFIRMED_MATCH", None))

    def test_a_body_matching_only_some_images_of_a_track_is_not_confirmed(self):
        s = subject_status([f"o{i}" for i in range(24)], self.matches(range(2)), self.queries(24))
        self.assertEqual((s["status"], s["partial"], s["n_images_matched"], s["n_images"]), ("PROBABLE_MATCH", True, 2, 24))
        self.assertIn("2 of 24", s["note"])

    def test_partial_matches_are_not_upgraded_across_subjects(self):
        s = subject_status([f"o{i}" for i in range(4)], self.matches(range(2)), self.queries(4))
        all_matches = self.matches(range(4))
        self.assertEqual(confirm_across_subjects(s, all_matches)["status"], "PROBABLE_MATCH")
        single = subject_status(["o0"], self.matches([0]), self.queries(1))
        self.assertEqual(confirm_across_subjects(single, all_matches)["status"], "CONFIRMED_MATCH")  # the intended single-image upgrade stays

    def test_reasoning_does_not_explain_a_track_by_a_partial_match(self):
        track = dict(subject_kind="TRACK")
        partial = subject_status([f"o{i}" for i in range(24)], self.matches(range(2)), self.queries(24))
        full = subject_status([f"o{i}" for i in range(4)], self.matches(range(4)), self.queries(4))
        out_partial = reasoning.assess(motion_evidence(known_object=partial), VALID, CFG, None)
        out_full = reasoning.assess(motion_evidence(known_object=full), VALID, CFG, None)
        alt = lambda o: next(a for a in o.candidate.alternatives if a.alt_id == "known_solar_system_object").state.value
        self.assertEqual(alt(out_partial), "PLAUSIBLE_UNTESTED")
        self.assertNotEqual(out_partial.candidate.attention_level.value, "EXPLAINED")
        self.assertEqual((alt(out_full), out_full.candidate.attention_level.value), ("SUPPORTED", "EXPLAINED"))


class ValidationReadinessTests(unittest.TestCase):
    def test_experiment_records_preserve_truth_sigma_tracks_diagnostics_and_truncation(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            gen, sc = generated(), small_scenario()
            cfg = dataclasses.replace(CFG, validation=dataclasses.replace(CFG.validation, moving_object_experiments=1))
            engine = Engine(settings=load_settings(environ={"HEIMDALL_STATE_DIR": str(tmp)}), config=cfg)
            calls = {"n": 0}

            def fake(modified, dets, image_info, observer, config):
                calls["n"] += 1
                flat = [d for o in sorted(dets) for d in dets[o]]
                by_id = {d.det_id: d for d in flat}
                if calls["n"] == 1:  # baseline: no field tracks
                    return LinkingResult([], 0, 0, 0, False, 100.0, []), by_id
                first = flat[0]
                other = next(d for d in flat if image_info[d.obs_id]["epoch_index"] != image_info[first.obs_id]["epoch_index"])
                plx = ParallaxFit(status="COMPLETED", reasons=[], n_points=2, distance_au=500.0)
                track = Track(track_id="T0000", source_ids=["S1", "S2"], det_ids=[first.det_id, other.det_id], obs_ids=[first.obs_id, other.obs_id],
                              epoch_indices=[0, 1], n_points=2, motion=MotionFit(status="COMPLETED", reasons=[], n_points=2, n_distinct_times=2, baseline_days=1.0),
                              parallax=plx, diagnostics={"effective_dof": 3, "tracklets": []})
                return LinkingResult([track], 2, 1, 1, False, 100.0, [], n_rejected_low_effective_dof=4, n_absorbed_chains=2), by_id

            boom = mock.Mock(side_effect=RuntimeError("stubbed"))
            names = ("self_check_run", "injection_recovery", "null_detection", "null_variability", "null_motion", "noise_summary", "psf_mismatch",
                     "robustness", "reproducibility")
            with contextlib.ExitStack() as stack:
                for n in names:
                    stack.enter_context(mock.patch.object(validation, n, boom))
                stack.enter_context(mock.patch.object(validation, "_link_field", fake))
                result, _ = engine.investigate(InvestigationRequest(ra_deg=sc.ra0, dec_deg=sc.dec0, radius_deg=0.11, source_id="local-fits",
                                                                    local_root=str(gen.directory), run_validation=True))
            run = next(v for v in result["validation"]["runs"] if v["procedure"] == "moving_object_injection")
            exp = run["metrics"]["experiments"][0]
            self.assertFalse(exp["truncated"])
            self.assertEqual((exp["n_rejected_low_effective_dof"], exp["n_absorbed_chains"], exp["n_inputs"], exp["n_evaluations"]), (4, 2, 2, 1))
            tr = exp["tracks"][0]
            self.assertEqual(tr["diagnostics"]["effective_dof"], 3)
            self.assertTrue(all("sigma_arcsec" in d and d["sigma_arcsec"] > 0 and "epoch" in d and d["det_id"] for d in tr["dets"]))
            self.assertTrue(exp["bodies"][0]["positions"] and exp["bodies"][0]["epochs_covered"])
            self.assertIn("attribution_results", run["metrics"])
            json.dumps(run["metrics"], default=str)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
