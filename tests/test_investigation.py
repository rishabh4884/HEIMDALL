"""Scientific reasoning / investigation engine tests. All fixtures here are SYNTHETIC hand-built result structures
(no real observations); they test the rules, not the sky."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from heimdall.api import Engine
from heimdall.engine import investigation
from heimdall.engine.config import EngineConfig
from heimdall.engine.evidence import ResultIndex, dependency_groups, extract

SIG = {"sigma_equivalent": 5.0}


def candidate(kind="MOTION", subject_kind="SOURCE", sid="S1", attention="WATCH", validity="VALIDATED_FOR_CONFIG", context=None, alts=(), px=None):
    return {"candidate_id": f"C:{kind}:{sid}", "kind": kind, "subject_kind": subject_kind, "subject_id": sid, "category": "X",
            "position": {"ra_deg": 10.0, "dec_deg": 5.0, "ellipse_1sigma": None}, "quality_grade": "B",
            "statistic": {"method_id": "m", "name": "chi2", "significance": SIG, "error_scale": 1.0, "flux_error_scale": 1.0},
            "cell_validity": {"state": validity},
            "candidate": {"attention_level": attention, "attention_reason": {"failed_checks": [], "unresolved_checks": []},
                          "alternatives": [dict(a) for a in alts], "checks": [], "validity": {"state": validity, "reasons": []}},
            "context": context or {}, "planet_x": px}


def source(sid="S1", with_variability=False):
    s = {"source_id": sid, "ra": 10.0, "dec": 5.0, "det_ids": [f"O1:{sid}a", f"O2:{sid}b", f"O3:{sid}c"], "obs_ids": ["O1", "O2", "O3"],
         "motion": {"gof_stationary": SIG, "gof_linear": SIG, "n_points": 3, "baseline_days": 100, "mu_arcsec_per_yr": [1.0, 0.0], "mu_cov": None},
         "parallax": {"parallax_arcsec": 0.5, "parallax_sigma_arcsec": 0.05, "distance_au": 400000.0, "dof": 1},
         "variability": [], "series": [], "spectrum": None, "spectral_comparisons": [], "analyses": {}}
    if with_variability:
        s["variability"] = [{"series_id": "V0", "status": "COMPLETED", "chi2": 30.0, "dof": 2, "significance": SIG, "leave_one_out": SIG,
                             "max_abs_pull": 3.0, "peak_to_peak_ujy": 5.0}]
        s["series"] = [{"series_id": "V0", "obs_ids": ["O1", "O2", "O3"], "wavelength_um": 1.0}]
    return s


def result(cands, sources=None, **extra):
    r = {"run_id": "R-SYNTHETIC", "config": EngineConfig().to_dict(), "config_hash": "h", "sources": sources or [source()], "tracks": [], "transients": [],
         "linking": {"not_linked": {}}, "context": {}, "candidates": cands, "astrometric_error_model": {"status": "COMPLETED", "scale": 1.1, "n_sources": 40},
         "photometric_error_model": None}
    r.update(extra)
    return r


def hyp(inv, hid):
    return next((h for h in inv["hypotheses"] if h["hypothesis_id"] == hid), None)


def build_one(cands, sources=None, **kw):
    return investigation.build(result(cands, sources, **kw))["investigations"][0]


class EvidenceAndGroups(unittest.TestCase):  # SYNTHETIC
    def test_astrometric_items_share_one_group(self):
        c = candidate()
        items = extract(ResultIndex(result([c])), "SOURCE", "S1", [c])
        types = {i.type for i in items}
        self.assertTrue({"POSITION_CHANGE", "PROPER_MOTION", "PARALLAX"} <= types)
        groups, _ = dependency_groups(items)
        astrom = [g for g in groups if any("POSITION_CHANGE" in e for e in g["evidence_ids"])][0]
        self.assertEqual(sum(1 for e in astrom["evidence_ids"] if any(t in e for t in ("PROPER_MOTION", "PARALLAX", "POSITION_CHANGE"))), 3)
        self.assertIn("SHARED_FIT", astrom["merged_because"])

    def test_cross_domain_and_image_level_items_are_not_merged(self):
        cm, cv = candidate("MOTION"), candidate("VARIABILITY")
        r = result([cm, cv], [source(with_variability=True)])
        r["photometric_error_model"] = {"status": "COMPLETED", "scale": 1.0, "n_series": 10}
        items = extract(ResultIndex(r), "SOURCE", "S1", [cm, cv])
        groups, rel = dependency_groups(items)
        flux = next(i for i in items if i.type == "FLUX_CHANGE")
        pos = next(i for i in items if i.type == "POSITION_CHANGE")
        gid = {e: g["group_id"] for g in groups for e in g["evidence_ids"]}
        self.assertNotEqual(gid[flux.evidence_id], gid[pos.evidence_id])  # share detections but different measurement domains
        kinds = {x["kind"] for x in rel}
        self.assertIn("SHARED_DETECTION_CROSS_DOMAIN", kinds)
        self.assertIn("IMAGE_CORRELATED", kinds)
        for em in (i for i in items if i.type == "ERROR_MODEL"):
            self.assertEqual(sum(1 for g in groups if em.evidence_id in g["evidence_ids"] and len(g["evidence_ids"]) > 1), 0)

    def test_independent_groups_counted_not_items(self):
        c = candidate()
        inv = build_one([c])
        self.assertLess(len(inv["dependency_groups"]), len(inv["evidence"]))

    def test_spectral_change_depends_on_flux(self):
        cv = candidate("VARIABILITY")
        s = source(with_variability=True)
        s["spectral_comparisons"] = [{"label": "a", "status": "COMPLETED", "significance": SIG, "n_pairs": 3}]
        s["spectrum"] = {"slope": {"slope": 1.0, "slope_sigma": 0.1, "n_points": 3}, "coverage": [], "wavelength_sources": ["x"], "points": []}
        items = extract(ResultIndex(result([cv], [s])), "SOURCE", "S1", [cv])
        groups, _ = dependency_groups(items)
        gid = {e: g["group_id"] for g in groups for e in g["evidence_ids"]}
        flux = next(i for i in items if i.type == "FLUX_CHANGE")
        sc = next(i for i in items if i.type == "SPECTRAL_CHANGE")
        self.assertEqual(gid[flux.evidence_id], gid[sc.evidence_id])


class HypothesisRules(unittest.TestCase):  # SYNTHETIC
    def test_confirmed_known_body_is_supported_and_explains(self):
        c = candidate(context={"known_object": {"status": "CONFIRMED_MATCH", "name": "(1) Ceres", "matches": []}})
        inv = build_one([c])
        self.assertEqual(hyp(inv, "KNOWN_SMALL_BODY")["status"], "SUPPORTED")
        self.assertEqual(inv["interpretation"]["status"], "EXPLAINED")
        self.assertEqual(hyp(inv, "UNKNOWN_INNER_SOLAR_SYSTEM_BODY")["status"], "CONTRADICTED")

    def test_partial_match_is_consistent_not_supported(self):
        c = candidate(context={"known_object": {"status": "PROBABLE_MATCH", "name": "x", "matches": []}})
        h = hyp(build_one([c]), "KNOWN_SMALL_BODY")
        self.assertNotEqual(h["status"], "SUPPORTED")

    def test_no_match_does_not_establish_unknown_body(self):
        c = candidate(context={"known_object": {"status": "NO_MATCH", "matches": []}})
        h = hyp(build_one([c]), "UNKNOWN_INNER_SOLAR_SYSTEM_BODY")
        self.assertEqual(h["status"], "CONSISTENT")  # never SUPPORTED: max status for astrophysical hypotheses
        self.assertEqual(h["max_status"], "CONSISTENT")

    def test_incomplete_external_query_is_not_assessable_not_contradiction(self):
        c = candidate(context={"known_object": {"status": "NOT_ASSESSABLE", "reason": "service unavailable"}})
        h = hyp(build_one([c]), "UNKNOWN_INNER_SOLAR_SYSTEM_BODY")
        self.assertEqual(h["status"], "NOT_ASSESSABLE")
        self.assertIn("service unavailable", json.dumps(h["predictions"]))
        self.assertEqual(hyp(build_one([c]), "KNOWN_SMALL_BODY")["status"], "NOT_ASSESSABLE")

    def test_failed_critical_gate_blocks_astrophysical_hypotheses(self):
        c = candidate(attention="INCONCLUSIVE")
        c["candidate"]["attention_reason"] = {"failed_checks": ["leave_one_out"], "unresolved_checks": []}
        inv = build_one([c])
        self.assertEqual(inv["interpretation"]["status"], "INCONCLUSIVE")
        for h in inv["hypotheses"]:
            if h["family"] == "ASTROPHYSICAL":
                self.assertEqual(h["status"], "NOT_ASSESSABLE")

    def test_miscalibrated_cell_closes_gate(self):
        inv = build_one([candidate(validity="MISCALIBRATED")])
        self.assertEqual(inv["interpretation"]["status"], "INCONCLUSIVE")

    def test_spectrum_alone_does_not_trigger_thermal_interpretation(self):
        s = source(with_variability=True)
        s["spectrum"] = {"slope": {"slope": 2.0, "slope_sigma": 0.1, "n_points": 8}, "coverage": [{"detector": 1, "state": "MEASURED"}],
                         "wavelength_sources": ["CWAVE_CBAND"], "points": []}
        inv = build_one([candidate("VARIABILITY")], [s])
        h = hyp(inv, "THERMAL_DUST_OR_RED_SPECTRUM")
        self.assertEqual(h["status"], "NOT_ASSESSABLE")
        self.assertIn("photospheric_reference", {p["prediction_id"] for p in h["predictions"] if p["result"] == "NOT_ASSESSABLE"})
        self.assertNotIn("temperature", json.dumps(inv["interpretation"]).lower())

    def test_correlated_evidence_cannot_make_astrophysical_hypothesis_supported(self):
        inv = build_one([candidate("VARIABILITY")], [source(with_variability=True)])
        for h in inv["hypotheses"]:
            if h["family"] == "ASTROPHYSICAL":
                self.assertNotEqual(h["status"], "SUPPORTED")

    def test_extragalactic_contradicted_by_significant_parallax(self):
        inv = build_one([candidate("VARIABILITY")], [source(with_variability=True)])
        self.assertEqual(hyp(inv, "EXTRAGALACTIC_VARIABLE")["status"], "CONTRADICTED")


class PlanetXRestrictions(unittest.TestCase):  # SYNTHETIC
    PX = {"applicable": True, "status": "CONSISTENT_REQUIRES_CONFIRMATION", "scenario": "s", "hypothesis": "A distant body would move slowly.",
          "disclaimer": "d", "requirements": [{"req_id": "distance_in_range", "description": "x", "state": "SATISFIED", "detail": "ok", "evidence": []},
                                              {"req_id": "independent_confirmation", "description": "y", "state": "NOT_SATISFIED", "detail": "none", "evidence": []}]}

    def test_planet_x_is_status_only_and_unmet_confirmation_is_not_contradiction(self):
        inv = build_one([candidate(px=self.PX)])
        h = hyp(inv, "PLANET_X_SCENARIO")
        self.assertEqual(h["status"], "CONSISTENT")
        self.assertEqual(h["max_status"], "CONSISTENT")
        self.assertEqual(inv["interpretation"]["status"], "PROVISIONAL")
        text = json.dumps(inv).lower()
        self.assertNotIn("probability\":", text)
        self.assertNotIn("score", [k for k in inv["interpretation"]])
        conf = next(p for p in h["predictions"] if p["prediction_id"] == "independent_confirmation")
        self.assertNotEqual(conf["result"], "CONTRADICTS")

    def test_no_position_evidence_is_not_applicable(self):
        inv = build_one([candidate("VARIABILITY")], [source(with_variability=True)])
        self.assertEqual(inv["planet_x"]["status"], "NOT_APPLICABLE")
        self.assertIsNone(hyp(inv, "PLANET_X_SCENARIO"))


class StatusAndDeterminism(unittest.TestCase):  # SYNTHETIC
    def test_deterministic_and_inputs_unmodified(self):
        r = result([candidate()])
        before = copy.deepcopy(r)
        a, b = investigation.build(r), investigation.build(r)
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))
        self.assertEqual(r, before)

    def test_attention_levels_unchanged(self):
        c = candidate()
        investigation.build(result([c]))
        self.assertEqual(c["candidate"]["attention_level"], "WATCH")

    def test_evidence_items_have_required_fields(self):
        inv = build_one([candidate(context={"known_object": {"status": "NO_MATCH", "matches": []}})])
        for e in inv["evidence"]:
            for k in ("type", "subject_id", "candidate_ids", "ref", "value", "provenance", "admissible", "admissibility_reasons", "basis", "not_assessable_reason"):
                self.assertIn(k, e)


class PipelineAndInterfaces(unittest.TestCase):  # SYNTHETIC end to end (software test, not scientific validation)
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.engine = Engine()
        cls.result, cls.run_dir = cls.engine.demo(out_dir=Path(cls.tmp.name), validate=False, small=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_stage_ran_in_normal_pipeline(self):
        stages = [s["stage"] for s in self.result["stages"]]
        self.assertLess(stages.index("reasoning"), stages.index("investigation"))
        self.assertEqual(next(s for s in self.result["stages"] if s["stage"] == "investigation")["status"], "COMPLETED")
        self.assertEqual(self.result["schema"], "heimdall.investigation/1.1")

    def test_every_candidate_references_its_investigation(self):
        ids = {i["investigation_id"] for i in self.result["investigations"]["investigations"]}
        for c in self.result["candidates"]:
            pi = c["physical_interpretation"]
            self.assertIn(pi["investigation_id"], ids)
            self.assertIn("interpretation_status", pi)

    def test_offline_reinvestigation_matches_live_stage(self):
        out = Path(self.tmp.name) / "re.json"
        saved = self.engine.load(self.run_dir)
        again = self.engine.reinvestigate(self.run_dir, out)
        self.assertTrue(out.exists())
        live = {k: v for k, v in saved["investigations"].items()}
        self.assertEqual(json.dumps(live["investigations"], sort_keys=True), json.dumps(again["investigations"], sort_keys=True))

    def test_inspect_investigations_section(self):
        summ = self.engine.inspect(self.run_dir, "investigations")
        self.assertTrue(summ["summaries"])
        one = self.engine.inspect(self.run_dir, "investigations", summ["summaries"][0]["investigation_id"])
        for k in ("evidence", "dependency_groups", "hypotheses", "missing_evidence", "follow_ups", "upstream", "versions", "planet_x"):
            self.assertIn(k, one)

    def test_skymap_candidate_metadata(self):
        sm = self.engine.skymap(self.run_dir)
        cands = sm["layers"]["candidates"]
        self.assertTrue(cands)
        for c in cands:
            self.assertIsNotNone(c["investigation_id"])
            self.assertIn(c["interpretation_status"], {"EXPLAINED", "PROVISIONAL", "UNRESOLVED", "UNEXPLAINED_UNDER_INVESTIGATION", "NOT_ASSESSABLE", "INCONCLUSIVE"})
            self.assertIsNotNone(c["ra"])
            self.assertTrue(c["evidence_refs"] is not None)

    def test_cli_inspect_and_reinvestigate(self):
        import contextlib
        import io

        from heimdall.__main__ import main
        out = Path(self.tmp.name) / "cli.json"
        for argv in (["inspect", str(self.run_dir), "--section", "investigations", "--json"],
                     ["reinvestigate", str(self.run_dir), "--out", str(out)]):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(main(argv), 0)
            self.assertTrue(buf.getvalue())
        self.assertIn("investigations", json.loads(out.read_text()))

    def test_old_run_without_investigations_is_rebuilt_on_read(self):
        saved = copy.deepcopy(self.engine.load(self.run_dir))
        saved.pop("investigations")
        self.assertTrue(self.engine.reinvestigate(saved)["investigations"])


if __name__ == "__main__":
    unittest.main()
