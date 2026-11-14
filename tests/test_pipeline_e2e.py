"""End-to-end investigations through the real pipeline on SYNTHETIC products (software verification only)."""

import contextlib
import csv
import io
import json
import os
import shutil
import tempfile
import unittest
import xml.dom.minidom
from pathlib import Path
from unittest import mock

from astropy.io import fits

from heimdall.__main__ import main
from heimdall.api import Engine
from heimdall.config import load_settings
from heimdall.engine.skygeom import angular_separation_deg
from heimdall.pipeline.runner import STAGES, InvestigationRequest
from heimdall.skymap.layers import build_skymap
from heimdall.skymap.svg import render_svg
from tests.helpers import generated, small_scenario


def near(c, pos, arcsec):
    return angular_separation_deg(c["position"]["ra_deg"], c["position"]["dec_deg"], pos["ra"], pos["dec"]) * 3600 < arcsec


class InvestigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gen = generated()
        cls.state = Path(tempfile.mkdtemp())
        cls.engine = Engine(settings=load_settings(environ={"HEIMDALL_STATE_DIR": str(cls.state)}))
        sc = small_scenario()
        cls.request = InvestigationRequest(ra_deg=sc.ra0, dec_deg=sc.dec0, radius_deg=0.11, source_id="local-fits",
                                           local_root=str(cls.gen.directory))
        cls.result, cls.run_dir = cls.engine.investigate(cls.request)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.state, ignore_errors=True)

    def candidates(self, kind):
        return [c for c in self.result["candidates"] if c["kind"] == kind]

    def test_every_stage_ran_and_status_is_separate_from_validity(self):
        r = self.result
        self.assertEqual((r["status"]["execution"], r["status"]["outcome"], r["data_origin"]), ("COMPLETED", "OK", "SYNTHETIC"))
        self.assertEqual([s["stage"] for s in r["stages"]], [s for s in STAGES if s != "outputs"])
        statuses = {s["stage"]: s["status"] for s in r["stages"]}
        self.assertEqual(statuses["validation"], "NOT_APPLICABLE")
        self.assertEqual(statuses["calibration"], "NOT_APPLICABLE")
        self.assertEqual(statuses["context"], "NOT_APPLICABLE")  # real catalogues never describe a synthetic sky
        self.assertIn("synthetic", r["context"]["reason"])
        self.assertTrue(all(v == "COMPLETED" for k, v in statuses.items() if k not in ("validation", "calibration", "context")))
        self.assertTrue(all(o["included"] for o in r["observations"]))
        self.assertEqual(len(r["epochs"]), 3)
        json.dumps(r, allow_nan=False)

    def test_moving_source_is_linked_and_its_distance_measured(self):
        truth = self.gen.truth["mover"]
        self.assertEqual(len(self.result["tracks"]), 1)
        plx = self.result["tracks"][0]["parallax"]
        self.assertLess(abs(plx["distance_au"] - truth["distance_au"]) / truth["distance_au"], 0.02)
        lo, hi = plx["distance_interval_au"]
        self.assertTrue(lo <= truth["distance_au"] <= hi)
        track = self.candidates("MOTION")
        track = [c for c in track if c["subject_kind"] == "TRACK"][0]
        self.assertEqual(track["planet_x"]["status"], "CONSISTENT_REQUIRES_CONFIRMATION")
        self.assertEqual(track["candidate"]["attention_level"], "INCONCLUSIVE")
        self.assertIn("known_object_check", track["candidate"]["attention_reason"]["unresolved_checks"])
        self.assertFalse(track["unusualness"]["is_probability_of_planet_x"])

    def test_variable_and_transient_are_recovered_and_capped(self):
        var = [c for c in self.candidates("VARIABILITY") if near(c, self.gen.truth["variable"], 3)]
        self.assertEqual(len(var), 1)
        self.assertGreater(var[0]["statistic"]["significance"]["sigma_equivalent"], 5)
        self.assertEqual(var[0]["candidate"]["attention_level"], "WATCH")
        self.assertEqual(var[0]["candidate"]["cap_reason"], "NO_CONTROL_RUN")
        tr = [c for c in self.candidates("TRANSIENT") if near(c, self.gen.truth["transient"], 3)]
        self.assertEqual(len(tr), 1)
        self.assertNotEqual(tr[0]["candidate"]["attention_level"], "EXPLAINED")
        self.assertFalse(any(c["candidate"]["attention_level"] == "ATTEND" for c in self.result["candidates"]))

    def test_cosmic_rays_are_explained_as_artefacts(self):
        explained = [c for c in self.candidates("TRANSIENT") if c["candidate"]["attention_level"] == "EXPLAINED"]
        artefacts = [c for c in explained if any(a["alt_id"] == "image_artefact" and a["state"] == "SUPPORTED"
                                                 for a in c["candidate"]["alternatives"])]
        self.assertGreaterEqual(len(artefacts), len(self.gen.truth["cosmic_rays"]) // 2)

    def test_unavailable_analyses_and_region_statement(self):
        names = {u["analysis"] for u in self.result["unavailable_analyses"]}
        self.assertIn("known_solar_system_object_check", names)
        self.assertEqual(self.result["planet_x"]["region"]["status"], "NOT_ASSESSABLE")
        for s in self.result["sources"]:
            self.assertIn(s["analyses"]["variability"]["status"], ("COMPLETED", "INSUFFICIENT_INPUTS"))
            if s["analyses"]["variability"]["status"] != "COMPLETED":
                self.assertTrue(s["analyses"]["variability"]["reasons"])

    def test_playback_is_time_ordered_without_interpolation(self):
        frames = self.result["playback"]["frames"]
        self.assertEqual(len(frames), len(self.result["observations"]))
        mjds = [f["mjd"] for f in frames]
        self.assertEqual(mjds, sorted(mjds))
        self.assertEqual(len({f["obs_id"] for f in frames}), len(frames))
        windowed = self.engine.playback(self.run_dir, mjd_start=mjds[0], mjd_end=mjds[0] + 1.0)
        self.assertTrue(all(mjds[0] <= f["mjd"] <= mjds[0] + 1.0 for f in windowed["frames"]))
        self.assertTrue(windowed["window"]["excluded"])

    def test_skymap_contract_and_svg(self):
        sm = build_skymap(self.result, viewport=(self.request.ra_deg, self.request.dec_deg, 0.1))
        layers = sm["layers"]
        self.assertEqual(sm["frame"]["system"], "ICRS")
        self.assertEqual(sm["watermark"], "SYNTHETIC - NOT REAL DATA")
        self.assertEqual(sm["external_layers"]["star_catalogue"]["status"], "NOT_AVAILABLE")
        self.assertEqual(len(layers["coverage"]), len(self.result["observations"]))
        self.assertEqual(len(layers["tracks"]), 1)
        self.assertGreater(layers["coverage_depth"]["max_depth"], 1)
        self.assertIn(sm["viewport"]["detections"]["mode"], ("items", "tiles"))
        xml.dom.minidom.parseString(render_svg(sm))
        self.assertTrue((self.run_dir / "skymap.svg").is_file())

    def test_reproduction_gives_the_same_results_hash(self):
        out = self.engine.reproduce(self.run_dir)
        self.assertTrue(out["identical"], out)

    def test_inspect_and_export(self):
        cid = self.result["candidates"][0]["candidate_id"]
        self.assertEqual(self.engine.inspect(self.result["run_id"], "candidates", cid)["candidate_id"], cid)
        rows = list(csv.DictReader(io.StringIO(self.engine.export(self.run_dir, "candidates", "csv"))))
        self.assertEqual(len(rows), len(self.result["candidates"]))
        self.assertIn("attention_level", rows[0])
        summary = self.engine.inspect(self.run_dir)
        self.assertEqual(summary["data_origin"], "SYNTHETIC")


class FailureModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gen = generated()
        cls.state = Path(tempfile.mkdtemp())
        cls.engine = Engine(settings=load_settings(environ={"HEIMDALL_STATE_DIR": str(cls.state)}))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.state, ignore_errors=True)

    def request(self, root, **kw):
        sc = small_scenario()
        base = dict(ra_deg=sc.ra0, dec_deg=sc.dec0, radius_deg=0.11, source_id="local-fits", local_root=str(root))
        base.update(kw)
        return InvestigationRequest(**base)

    def test_one_broken_product_gives_a_partial_result(self):
        root = self.state / "partial"
        shutil.copytree(self.gen.directory, root)
        victim = sorted(root.glob("*.fits"))[3]
        with fits.open(victim, mode="update") as h:
            h["IMAGE"].header["L2DQAFLG"] = "Fail"
        (root / "broken.fits").write_bytes(b"garbage")
        result, _ = self.engine.investigate(self.request(root), save=False)
        self.assertEqual(result["status"]["execution"], "PARTIAL")
        self.assertEqual(result["failed_items"][0]["ref"], "broken.fits")
        excluded = [o for o in result["observations"] if not o["included"]]
        self.assertEqual(len(excluded), 1)
        self.assertIn("L2_DQA_NOT_PASSED", excluded[0]["exclusion_reasons"])
        self.assertTrue(result["tracks"])

    def test_empty_region_is_a_successful_empty_result(self):
        result, _ = self.engine.investigate(self.request(self.gen.directory, ra_deg=200.0, dec_deg=40.0), save=False)
        self.assertEqual((result["status"]["execution"], result["status"]["outcome"]), ("COMPLETED", "NO_OBSERVATIONS"))
        later = [s for s in result["stages"] if s["stage"] != "discovery"]
        self.assertTrue(all(s["status"] == "INSUFFICIENT_INPUTS" for s in later))
        self.assertEqual(result["candidates"], [])

    def test_discovery_failure_is_failed_not_empty(self):
        result, _ = self.engine.investigate(self.request("/nonexistent/heimdall"), save=False)
        self.assertEqual(result["status"]["execution"], "FAILED")
        self.assertIsNone(result["status"]["outcome"])
        self.assertEqual(result["status"]["error"]["code"], "ARCHIVE_UNREACHABLE")

    def test_validation_runs_are_scoped_to_synthetic_data(self):
        result, _ = self.engine.investigate(self.request(self.gen.directory, run_validation=True), save=False)
        runs = result["validation"]["runs"]
        procedures = {r["procedure"] for r in runs}
        self.assertTrue({"independent_numerical_checks", "injection_recovery", "null_detection_inverted",
                         "null_variability_constant_sources", "null_motion_stationary_sources", "moving_object_injection"} <= procedures)
        moi = next(r for r in runs if r["procedure"] == "moving_object_injection")
        m = moi["metrics"]
        self.assertEqual(m["n_injected"], 32)
        self.assertGreater(m["n_covered_2_epochs"], 0)
        bright = next(b for b in m["by_snr"] if b["snr"] == 30.0)
        self.assertGreaterEqual(bright["completeness"], 0.5)  # bright injected bodies that stay in the field are linked
        # Tracks the scene already contains (its genuine synthetic body) are not false links of the injection.
        self.assertEqual(m["false_links_per_experiment"], 0.0)
        self.assertIn("track found without injection", m["false_link_definition"])
        self.assertEqual(m["spurious_tracks_with_ambiguous"] >= m["spurious_tracks_definite"], True)
        self.assertIn("attribution", m)
        region = result["planet_x"]["region"]
        self.assertEqual(region["status"], "COMPLETENESS_LIMITED")
        self.assertIn("not evidence that no such body exists", region["statement"])
        for r in runs:
            self.assertEqual(r["scope"]["origin"], "SYNTHETIC")
            if r["scope_cell"]:
                self.assertIn("|SYNTHETIC|", r["scope_cell"])
        self.assertEqual(next(r for r in runs if r["procedure"] == "independent_numerical_checks")["outcome"], "PASSED")
        real = self.engine.validation.validity_for("VARIABILITY", "REAL", "SYNTHETIC", self.engine.config.hash)
        self.assertEqual(real.state.value, "UNVALIDATED")


class CliTests(unittest.TestCase):
    def run_cli(self, *args, state):
        out, err = io.StringIO(), io.StringIO()
        env = {k: v for k, v in os.environ.items() if not k.startswith("HEIMDALL_")}
        env["HEIMDALL_STATE_DIR"] = str(state)
        with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("heimdall.__main__._env_file", return_value=None):
            code = main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_demo_inspect_export_and_config(self):
        state = Path(tempfile.mkdtemp())
        try:
            code, out, _ = self.run_cli("demo", "--small", "--no-validate", state=state)
            self.assertEqual(code, 0)
            self.assertIn("SYNTHETIC DATA - NOT ASTRONOMICAL OBSERVATIONS", out)
            run = next((state / "runs").iterdir())
            code, out, _ = self.run_cli("inspect", str(run), "--section", "tracks", "--json", state=state)
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(out))
            code, out, _ = self.run_cli("export", str(run), "--what", "detections", "--format", "csv", state=state)
            self.assertEqual(code, 0)
            self.assertIn("det_id", out.splitlines()[0])
            code, out, _ = self.run_cli("skymap", str(run), "--svg", str(state / "map.svg"), state=state)
            self.assertEqual(code, 0)
            self.assertTrue((state / "map.svg").is_file())
            code, out, _ = self.run_cli("config", "hash", state=state)
            self.assertTrue(out.strip().startswith("sha256:"))
            code, out, _ = self.run_cli("validate", "--self-check", state=state)
            self.assertEqual(code, 0)
            self.assertNotIn("FAILED", out)
            code, _, err = self.run_cli("inspect", "no-such-run", state=state)
            self.assertEqual(code, 2)
            self.assertIn("INPUT_INVALID", err)
        finally:
            shutil.rmtree(state, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
