"""Target investigations through the data service: readiness-first, frozen manifests, durable jobs. SYNTHETIC fixtures; no network.

The synthetic products are served by a scripted 'archive' (a local-FITS adapter wrapper with an outage switch). The results are software
checks of the contract, never astronomical findings.
"""

import contextlib
import io
import json
import os
import random
import shutil
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from heimdall.__main__ import main
from heimdall.api import Engine
from heimdall.config import load_settings
from heimdall.contracts import ErrorCode, HeimdallError
from heimdall.data_access.local_fits import LocalFitsSource
from heimdall.dataservice.schemas import TERMINAL, JobState, ServiceConfig
from heimdall.dataservice.service import DataService
from heimdall.engine.config import EngineConfig
from heimdall.engine.validation import ValidationRegistry
from heimdall.pipeline.runner import RunStore
from heimdall.targets import prepare
from heimdall.targets.investigate import TargetOptions
from heimdall.targets.registry import TargetSpec
from tests.helpers import generated
from tests.test_dataservice import Clock, Exploding


class SyntheticArchive(LocalFitsSource):
    """Serves the synthetic product set like an archive would, with an outage switch and call counters."""

    def __init__(self, settings, root):
        super().__init__(settings, root=root)
        self.down = False
        self.calls = {"discover": 0, "retrieve": 0}

    def discover(self, query):
        self.calls["discover"] += 1
        if self.down:
            raise HeimdallError.of(ErrorCode.RETRIEVAL_FAILED, "IRSA discovery failed on every route", http_status="502")
        return super().discover(query)

    def retrieve(self, request):
        self.calls["retrieve"] += 1
        if self.down:
            raise HeimdallError.of(ErrorCode.ARCHIVE_TIMEOUT, "timed out")
        return super().retrieve(request)


class SyntheticTargetFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gen = generated()
        cls.tmp = Path(tempfile.mkdtemp())
        env = {"HEIMDALL_STATE_DIR": str(cls.tmp / "state"), "HEIMDALL_CACHE_DIR": str(cls.tmp / "cache"), "HEIMDALL_OFFLINE": "1"}
        cls.settings = load_settings(environ=env)
        cls.config = EngineConfig()
        cls.registry = ValidationRegistry(cls.tmp / "state" / "validation")
        cls.store = RunStore(cls.tmp / "state" / "runs")
        v = cls.gen.truth["variable"]
        cls.spec = TargetSpec.from_coordinates(v["ra"], v["dec"], category="VARIABLE_STAR")
        cls.opts = TargetOptions(max_observations=24, detectors=(1, 2), cutout_size_deg=0.15, context=False)
        cls.clock = Clock()
        cls.archive = SyntheticArchive(cls.settings, cls.gen.directory)
        cfg = ServiceConfig(scope="synthetic", poll_s=1.0, backoff_base_s=5.0, backoff_cap_s=20.0, deadline_s=7200.0)
        cls.svc = DataService(cls.tmp / "svc", cls.archive, cfg, clock=cls.clock, rng=random.Random(3))
        cls.svc.handlers[prepare.KIND_TARGET] = prepare.make_handler(settings=cls.settings, config=cls.config, registry=cls.registry, store=cls.store)
        cls.kw = dict(settings=cls.settings, config=cls.config, registry=cls.registry, store=cls.store)

    @classmethod
    def tearDownClass(cls):
        cls.svc.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def settle(self, rounds=80):
        for _ in range(rounds):
            if not [j for j in self.svc.jobs.list() if j["state"] not in TERMINAL]:
                return
            if not self.svc.tick():
                self.clock.advance(2)

    def test_1_uncached_request_becomes_a_durable_job_not_an_error_and_survives_an_outage(self):
        self.archive.down = True
        a = prepare.request_investigation(self.svc, self.spec, self.opts, **self.kw)
        self.assertNotIn("investigation_id", a)
        self.assertEqual(a["readiness"], "PREPARING")
        self.assertEqual(a["status"]["execution"], "PREPARING_DATA")
        self.assertFalse(a["must_resubmit"])
        self.assertIn("note", a["investigation"])
        b = prepare.request_investigation(self.svc, self.spec, self.opts, **self.kw)
        self.assertEqual(a["job"]["job_id"], b["job"]["job_id"])  # same request -> same job
        for _ in range(5):  # the archive stays down: the service keeps retrying on its own schedule
            self.svc.tick()
            self.clock.advance(30)
        st = prepare.investigation_status(self.svc, a["job"]["job_id"])
        self.assertEqual(st["readiness"], "PREPARING")
        self.assertTrue(any(j["state"] is JobState.PREPARING_DATA and j["attempts"] >= 1 for j in self.svc.jobs.list()))
        self.assertGreaterEqual(self.archive.calls["discover"], 2)
        self.archive.down = False  # recovery: nobody resubmits
        self.settle()
        done = prepare.investigation_status(self.svc, a["job"]["job_id"])
        self.assertIn("investigation_id", done)
        self.assertEqual(done["data_status"]["readiness"], "READY")

    def test_2_the_completed_investigation_ran_from_a_frozen_local_manifest(self):
        self.settle()
        r = prepare.request_investigation(self.svc, self.spec, self.opts, **self.kw)
        self.assertEqual(r["data_origin"], "SYNTHETIC")  # synthetic fixtures are never relabelled real
        man = r["input_manifest"]
        self.assertTrue(man["manifest_id"].startswith("man:"))
        self.assertEqual({p["sha256"] for p in r["products"]}, {p["sha256"] for p in man["products"]})
        self.assertTrue(all(p["origin"] == "SYNTHETIC_TEST_FIXTURE" for p in man["products"]))
        self.assertEqual(r["data_status"]["readiness"], "READY")
        self.assertEqual(self.svc.store.usage()["pinned_objects"], self.svc.store.usage()["objects"])
        v = r["analyses"]["variability"]
        # context=False -> no control stars: the injected signal is formally significant but, without a control reference, not claimed (D-006)
        self.assertEqual(v["verdict"], "SIGNIFICANT_BUT_NOT_DISTINGUISHABLE_FROM_FIELD_SYSTEMATICS")
        self.assertGreater(max(x["test"]["sigma_equivalent"] or 0 for x in v["series"] if x["test"]["status"] == "COMPLETED"), 5.0)
        json.dumps(r, allow_nan=False)

    def test_3_an_identical_request_is_served_with_zero_archive_calls_by_a_new_process(self):
        self.settle()
        first = prepare.request_investigation(self.svc, self.spec, self.opts, **self.kw)
        before = dict(self.archive.calls)
        fresh = DataService(self.tmp / "svc", Exploding(), ServiceConfig(scope="synthetic"), clock=self.clock)
        try:
            again = prepare.request_investigation(fresh, self.spec, self.opts, **self.kw)
        finally:
            fresh.close()
        self.assertEqual(again["investigation_id"], first["investigation_id"])
        self.assertEqual(self.archive.calls, before)
        self.assertIn("no archive request", again["data_status"]["served_from"])

    def test_4_a_changed_request_on_ready_data_runs_locally_without_the_archive(self):
        self.settle()
        before = dict(self.archive.calls)
        fresh = DataService(self.tmp / "svc", Exploding(), ServiceConfig(scope="synthetic"), clock=self.clock)
        try:
            opts2 = TargetOptions(max_observations=24, detectors=(1, 2), cutout_size_deg=0.15, context=False, blind_pipeline=False)
            r = prepare.request_investigation(fresh, self.spec, opts2, **self.kw)
        finally:
            fresh.close()
        self.assertIn("investigation_id", r)
        self.assertEqual(self.archive.calls, before)
        self.assertIn(r["status"]["execution"], ("COMPLETED", "PARTIAL"))  # PARTIAL: synthetic set has no ePSF calibration (fallback PSF, stated)
        self.assertEqual(r["blind_pipeline"]["status"], "NOT_RUN")


    def test_5_transport_mode_defers_computation_to_a_worker_even_when_data_are_local(self):
        self.settle()
        opts3 = TargetOptions(max_observations=24, detectors=(1, 2), cutout_size_deg=0.15, context=False, blind_pipeline=False, max_seconds=900.0)
        a = prepare.request_investigation(self.svc, self.spec, opts3, analyse_inline=False, **self.kw)
        self.assertNotIn("investigation_id", a)  # acknowledged quickly with a job; nothing computed in the request
        self.assertEqual(a["readiness"], "PREPARING")
        self.settle()
        done = prepare.investigation_status(self.svc, a["job"]["job_id"])
        self.assertIn("investigation_id", done)
        again = prepare.request_investigation(self.svc, self.spec, opts3, analyse_inline=False, **self.kw)
        self.assertEqual(again["investigation_id"], done["investigation_id"])  # stored identical result returned at once


class CatalogueAttemptTests(unittest.TestCase):
    def test_worker_catalogue_calls_make_one_bounded_attempt(self):
        from heimdall.config import load_settings as ls
        st = ls(environ={"HEIMDALL_REQUEST_TIMEOUT_S": "120", "HEIMDALL_MAX_RETRIES": "4"})
        one = prepare._single_attempt(st)
        self.assertEqual((one.max_retries, one.request_timeout_s), (0, prepare.CATALOGUE_ATTEMPT_TIMEOUT_S))
        self.assertEqual((st.max_retries, st.request_timeout_s), (4, 120.0))  # the caller's settings are not mutated


class EnginePreparationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.env = {"HEIMDALL_STATE_DIR": str(self.tmp / "state"), "HEIMDALL_CACHE_DIR": str(self.tmp / "cache"), "HEIMDALL_OFFLINE": "1"}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_named_target_without_local_data_returns_a_job_reference_not_an_archive_error(self):
        eng = Engine(settings=load_settings(environ=self.env))
        try:
            a = eng.investigate_target("NASA-04")
            b = eng.investigate_target("NASA-04")
            self.assertEqual(a["readiness"], "PREPARING")
            self.assertEqual(a["job"]["job_id"], b["job"]["job_id"])
            self.assertEqual(a["status"]["execution"], "PREPARING_DATA")
            self.assertNotIn("investigation_id", a)
            self.assertIn("do not need to resubmit", a["message"])
            self.assertEqual(eng.investigation_status(a["job"]["job_id"])["readiness"], "PREPARING")
            self.assertEqual(eng.data_service().tick(), [])  # offline process: acquisition waits for a worker with archive access
        finally:
            eng.data_service().close()

    def test_engine_coverage_is_readiness_first_and_never_touches_the_archive_offline(self):
        eng = Engine(settings=load_settings(environ=self.env))
        try:
            cov = eng.coverage("irsa-spherex", 150.1, 2.2, 0.05, detectors=[2])
            self.assertEqual((cov["access"], cov["outcome"]), ("LOCAL_INDEX", "PREPARING"))
            self.assertIsNone(cov["n_observations"])
            self.assertFalse(cov["data_status"]["must_resubmit"])
            self.assertIn("not 'no observations'", cov["note"])
            again = eng.coverage("irsa-spherex", 150.1, 2.2, 0.05, detectors=[2])
            self.assertEqual(again["data_status"]["job"]["job_id"], cov["data_status"]["job"]["job_id"])
        finally:
            eng.data_service().close()

    def test_blind_investigation_is_readiness_first(self):
        from heimdall.contracts import HeimdallError
        from heimdall.pipeline.runner import InvestigationRequest
        eng = Engine(settings=load_settings(environ=self.env))
        try:
            req = InvestigationRequest(ra_deg=150.1, dec_deg=2.2, radius_deg=0.05, source_id="irsa-spherex", detectors=[2], max_observations=4,
                                       cutout_size_deg=0.1)
            st, run_dir = eng.investigate(req)
            self.assertIsNone(run_dir)
            self.assertEqual(st["readiness"], "PREPARING")
            self.assertIn("not a non-detection", st["note"])
            self.assertFalse(st["must_resubmit"])
            self.assertEqual(eng.investigate(req)[0]["job"]["job_id"], st["job"]["job_id"])  # same request joins the same job
            import dataclasses
            with self.assertRaises(HeimdallError):
                eng.investigate(dataclasses.replace(req, calibration_products="full"))
        finally:
            eng.data_service().close()

    def test_cli_returns_the_job_id_and_exit_zero(self):
        with mock.patch.dict(os.environ, self.env, clear=False):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(["target", "run", "NASA-04", "--wait", "0"])
            self.assertEqual(code, 0)
            self.assertIn("PREPARING", out.getvalue())
            self.assertIn("job=job:target_investigation:", out.getvalue())
            self.assertNotIn("try again", out.getvalue().lower())
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main(["data", "jobs"]), 0)
            jobs = json.loads(out.getvalue())
            self.assertEqual([j["state"] for j in jobs], ["QUEUED"])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main(["data", "status"]), 0)
            self.assertFalse(json.loads(out.getvalue())["freshness"]["all_sky_complete"])


if __name__ == "__main__":
    unittest.main()


class FollowupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.env = {"HEIMDALL_STATE_DIR": str(self.tmp / "state"), "HEIMDALL_CACHE_DIR": str(self.tmp / "cache"), "HEIMDALL_OFFLINE": "1"}
        d = self.tmp / "state" / "targets" / "NASA-01"
        d.mkdir(parents=True)
        meas = [{"obs_id": f"O{i}", "status": "MEASURED", "saturation_risk": True, "forced": {"status": "UNUSABLE"}} for i in range(4)]
        doc = {"investigation_id": "tinv:NASA-01:0000eeee", "target": {"target_id": "NASA-01", "category": "MOVING_OBJECT"}, "options": {"max_observations": 8},
               "analyses": {"motion": {"verdict": "NOT_ASSESSABLE", "reasons": ["FEWER_THAN_2_EPOCHS", "FEWER_THAN_3_USABLE_POINTS"]}},
               "measurements": meas, "hypotheses": [{"slug": "catalogue_motion", "status": "NOT_ASSESSABLE"}, {"slug": "stationary", "status": "NOT_ASSESSABLE"},
                                                    {"slug": "registration_artifact", "status": "NOT_ASSESSABLE"}]}
        (d / "latest.json").write_text(json.dumps(doc))
        self.eng = Engine(settings=load_settings(environ=self.env))

    def tearDown(self):
        self.eng.data_service().close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_plan_ranks_discriminating_actions_runs_them_once_and_respects_the_budget(self):
        from heimdall.followup import plan, run
        p = plan(self.eng, "NASA-01")
        kinds = [a["kind"] for a in p["actions"]]
        self.assertEqual(kinds[0], "other_detectors")  # saturation makes more epochs in the same detectors low value
        self.assertEqual(next(a for a in p["actions"] if a["kind"] == "more_epochs")["expected_value"], "LOW")
        self.assertTrue(all(a["distinguishes"] for a in p["actions"]))
        first = run(self.eng, p["next_action"])
        self.assertEqual(first["state"], "SUBMITTED")  # a durable job (offline: waits for a worker)
        self.assertFalse(first["data_status"]["must_resubmit"])
        again = run(self.eng, p["next_action"])
        self.assertIn("never repeated", again["note"])
        p2 = plan(self.eng, "NASA-01", budget_products=8)
        self.assertEqual(next(a for a in p2["actions"] if a["action_id"] == p["next_action"])["state"], "SUBMITTED")
        self.assertIn("budget", p2["stopping_reason"])
        over = run(self.eng, next(a["action_id"] for a in p2["actions"] if a["kind"] == "more_epochs"), budget_products=8)
        self.assertIn("over budget", over["note"])


class CatalogueOutagePolicyTests(unittest.TestCase):
    """Optional catalogue context: a coordinate request proceeds coordinate-only after the grace; a named target keeps waiting."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.settings = load_settings(environ={"HEIMDALL_STATE_DIR": str(self.tmp / "state"), "HEIMDALL_CACHE_DIR": str(self.tmp / "cache")})
        self.svc = DataService(self.tmp / "svc", None, ServiceConfig(), offline=True)

    def tearDown(self):
        self.svc.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _advance(self, spec, degraded):
        from unittest import mock
        from heimdall.targets.astrometry import ReferenceAstrometry
        failed = ReferenceAstrometry(target_id=spec.target_id, catalogue="gaia_dr3", status="SERVICE_FAILED", reason="request to gea.esac.esa.int timed out",
                                     ra_deg=None, dec_deg=None)
        with mock.patch.object(prepare.astrometry, "resolve", return_value=failed):
            return prepare.advance(self.svc, spec, TargetOptions(context=False), settings=self.settings, config=EngineConfig(), registry=None, store=None,
                                   online=True, allow_degraded=degraded)

    def test_coordinate_request_proceeds_narrowed_after_grace_and_named_target_waits(self):
        from heimdall.targets.registry import TargetSpec, get_target
        coord = TargetSpec.from_coordinates(150.1, 2.2, category="VARIABLE_STAR")
        self.assertEqual(self._advance(coord, False)["waiting_on"], "catalogue")  # within the grace: recoverable wait
        after = self._advance(coord, True)
        self.assertEqual(after["waiting_on"], "index")  # past the grace: continues to data preparation with a coordinate-only reference
        named = self._advance(get_target("NASA-04"), True)
        self.assertEqual(named["waiting_on"], "catalogue")  # a named target's identity is essential: it keeps waiting
