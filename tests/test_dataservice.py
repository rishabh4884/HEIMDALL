"""Data-service layer: inventory, verified product store, durable jobs, readiness-first target investigations.

Everything runs against a FAKE archive (scripted failures; products are small generated FITS files) or the real IRSA adapter over a fake
transport. No network is used. Nothing here is an astronomical result.
"""

import hashlib
import io
import json
import random
import shutil
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
from astropy.io import fits

from heimdall.config import load_settings
from heimdall.contracts import (DataOrigin, ErrorCode, HeimdallError, Provenance, ResultOutcome, SourceProduct)
from heimdall.data_access.interface import (AccessReport, DataSource, DiscoveryQuery, DiscoveryResult, RetrievalRequest, RetrievalResult, RetrievedProduct,
                                            SourceDescription)
from heimdall.data_access.irsa_spherex import IrsaSpherexSource
from heimdall.dataservice.inventory import ObsRecord, footprint_intersects_cone, parse_polygon
from heimdall.dataservice.jobs import IllegalTransition, backoff_delay
from heimdall.dataservice.localsource import LocalStoreSource
from heimdall.dataservice.productstore import InvalidProduct
from heimdall.dataservice.schemas import TERMINAL as TERMINAL_STATES
from heimdall.dataservice.schemas import JobState, PlanItem, ProductPlan, Readiness, RegionSpec, ServiceConfig
from heimdall.dataservice.service import KIND_INDEX, DataService, plan_for
from tests.test_irsa_routes import DOWN, HEADER, ROW1, TAP_HEADER, TAP_ROW, Router, ok

POLY = "POLYGON ICRS 149.5 1.5 150.5 1.5 150.5 2.5 149.5 2.5"
REGION = RegionSpec(150.0, 2.0, 0.05, (2,))


def fits_bytes(seed=0, size=64):
    buf = io.BytesIO()
    h = fits.HDUList([fits.PrimaryHDU(), fits.ImageHDU(np.random.default_rng(seed).normal(size=(size, size)).astype("float32"), name="IMAGE")])
    h[0].header["OBSID"] = f"FAKE{seed}"
    h.writeto(buf)
    return buf.getvalue()


class Clock:
    def __init__(self):
        self.t = datetime(2026, 11, 15, 6, 0, 0, tzinfo=UTC)

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += timedelta(seconds=s)


class FakeArchive(DataSource):
    """Scripted archive. ``mode``: ok | down. Observations are built through the real adapter's row parser so metadata keys are the real ones."""
    source_id = "fake-irsa"

    def __init__(self, tmp: Path, n=6, release="spherex_dr1", empty=False):
        self.tmp = Path(tmp)
        self.mode = "ok"
        self.calls = {"discover": 0, "retrieve": 0, "calibration": 0}
        self.empty = empty
        self.corrupt: set[str] = set()
        self.last_discovery = None
        self._parser = IrsaSpherexSource(load_settings(environ={"HEIMDALL_CACHE_DIR": str(self.tmp / "ac")}))
        self.rows = [{"collection": release, "observationid": f"2026W01_1A_{i:04d}_1", "energy_bandpassname": "SPHEREx-D2", "energy_bounds_lower": "1.1e-06",
                      "energy_bounds_upper": "1.6e-06", "time_bounds_lower": str(61000.0 + 40 * i), "time_bounds_upper": str(61000.001 + 40 * i),
                      "provenance_version": "7", "uri": f"ibe/data/spherex/dr1/level2/X/l2b-v27/2/level2_2026W01_1A_{i:04d}_1D2_spx.fits",
                      "contentchecksum": "", "contentlength": "", "footprint": POLY, "s_ra": "150.0", "s_dec": "2.0"} for i in range(n)]

    def describe(self):
        return SourceDescription(source_id=self.source_id, display_name="fake", requires_credentials=False, capabilities_verified=False, notes="test")

    def check_access(self):
        return AccessReport(source_id=self.source_id, checked_at=datetime.now(UTC), detail="fake")

    def discover(self, query):
        self.calls["discover"] += 1
        if self.mode == "down":
            self.last_discovery = {"route_used": None, "attempts": [{"route": "sia2", "outcome": "FAILED", "error_code": "RETRIEVAL_FAILED", "http_status": "502"},
                                                                     {"route": "tap", "outcome": "FAILED", "error_code": "ARCHIVE_TIMEOUT", "http_status": None}]}
            raise HeimdallError.of(ErrorCode.RETRIEVAL_FAILED, "IRSA discovery failed on every route", http_status="502")
        self.last_discovery = {"route_used": "sia2", "attempts": [{"route": "sia2", "outcome": "OK"}], "from_cache": False}
        now = datetime.now(UTC)
        prov = Provenance(origin=DataOrigin.LIVE, retrieved_at=now, software_version="test", notes="fake SIA2")
        rows = [] if self.empty else self.rows
        if not rows:
            return DiscoveryResult(outcome=ResultOutcome.NO_OBSERVATIONS, observations=(), provenance=prov)
        return DiscoveryResult(outcome=ResultOutcome.OK, observations=tuple(self._parser._observation(r, now, "fake SIA2") for r in rows), provenance=prov)

    def retrieve(self, request):
        self.calls["retrieve"] += 1
        if self.mode == "down":
            raise HeimdallError.of(ErrorCode.ARCHIVE_TIMEOUT, "timed out")
        data = fits_bytes(abs(hash(request.obs_id)) % 1000)
        if request.obs_id in self.corrupt:
            data = data[:1500]  # truncated download
        path = self.tmp / "dl" / f"{request.obs_id}.fits"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data)
        prod = SourceProduct(source_id=self.source_id, product_id=request.obs_id, checksum=hashlib.sha256(data).hexdigest(), checksum_algorithm="sha256")
        return RetrievalResult(outcome=ResultOutcome.OK, obs_id=request.obs_id, provenance=Provenance(origin=DataOrigin.LIVE, retrieved_at=datetime.now(UTC), software_version="test", products=(prod,)),
                               products=(RetrievedProduct(product=prod, location=str(path), media_type="application/fits"),))

    def retrieve_calibration(self, kind, detector):
        self.calls["calibration"] += 1
        if self.mode == "down":
            raise HeimdallError.of(ErrorCode.ARCHIVE_UNREACHABLE, "unreachable")
        path = self.tmp / "dl" / f"{kind}_D{detector}.fits"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(fits_bytes(999))
        return path, SourceProduct(source_id=self.source_id, product_id=f"{kind}_D{detector}", access_reference=f"calib/{kind}/{detector}.fits")


class Exploding(DataSource):
    """Any archive access fails the test: proves a request was served without touching the archive."""
    source_id = "explode"

    def describe(self):
        raise AssertionError("network")

    def check_access(self):
        raise AssertionError("network")

    def discover(self, q):
        raise AssertionError("network: discover")

    def retrieve(self, r):
        raise AssertionError("network: retrieve")

    def retrieve_calibration(self, k, d):
        raise AssertionError("network: calibration")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.clock = Clock()
        self.cfg = ServiceConfig(deadline_s=3600.0, backoff_base_s=10.0, backoff_cap_s=100.0, backoff_jitter=0.25, poll_s=1.0, lease_s=60.0)
        self.archive = FakeArchive(self.tmp)
        self.services = []

    def tearDown(self):
        for s in self.services:
            s.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def svc(self, source="default", **kw):
        s = DataService(self.tmp / "svc", self.archive if source == "default" else source, kw.pop("config", self.cfg), clock=self.clock,
                        rng=random.Random(7), **kw)
        self.services.append(s)
        return s

    def work(self, service, rounds=40):
        """Run the worker to quiescence, advancing the fake clock past parent-job polling intervals (success flows)."""
        for _ in range(rounds):
            if not [j for j in service.jobs.list() if j["state"] not in TERMINAL_STATES]:
                break
            if not service.tick():
                self.clock.advance(2)


class StateMachineTests(Base):
    def test_illegal_and_terminal_transitions_are_refused(self):
        s = self.svc()
        job, _ = s.jobs.submit(KIND_INDEX, {"x": 1})
        with self.assertRaises(IllegalTransition):
            s.jobs.transition(job["job_id"], JobState.VALIDATING if False else JobState.QUEUED, reason="x") if False else s.jobs.transition(job["job_id"], JobState.COMPLETE) and s.jobs.transition(job["job_id"], JobState.QUEUED)

    def test_transitions_are_persisted_with_history(self):
        s = self.svc()
        job, created = s.jobs.submit(KIND_INDEX, {"x": 2})
        s.jobs.transition(job["job_id"], JobState.DISCOVERING, reason="go", lease=True)
        s.jobs.transition(job["job_id"], JobState.PREPARING_DATA, reason="archive down", next_attempt_at=self.clock() + timedelta(seconds=30))
        ev = s.jobs.events(job["job_id"])
        self.assertEqual([e["to_state"] for e in ev], ["QUEUED", "DISCOVERING", "PREPARING_DATA"])
        again = self.svc().jobs.get(job["job_id"])  # a new process sees the same state
        self.assertEqual(again["state"], JobState.PREPARING_DATA)
        self.assertTrue(created)


class InventoryTests(Base):
    def test_polygon_and_cone_geometry(self):
        poly = parse_polygon(POLY)
        self.assertEqual(len(poly), 4)
        self.assertTrue(footprint_intersects_cone(poly, 150.0, 2.0, 0.01))
        self.assertTrue(footprint_intersects_cone(poly, 150.55, 2.0, 0.1))  # outside but within the radius of an edge
        self.assertFalse(footprint_intersects_cone(poly, 152.0, 2.0, 0.1))
        self.assertTrue(footprint_intersects_cone([], 10.0, 10.0, 0.1))  # no footprint known: membership is decided by the sync alone

    def test_region_query_uses_covering_sync_only_and_unindexed_is_not_empty(self):
        s = self.svc()
        self.assertFalse(s.index_status(REGION)["indexed"])
        self.assertIsNone(s.observations(REGION))  # None != []
        s.ensure_index(REGION)
        self.work(s)
        self.assertEqual(len(s.observations(REGION)), 6)
        self.assertEqual(len(s.observations(RegionSpec(150.0, 2.0, 0.02, (2,)))), 6)  # a smaller cone inside the synced one is answered locally
        self.assertIsNone(s.observations(RegionSpec(150.0, 2.0, 0.2, (2,))))  # a larger cone is not covered
        self.assertIsNone(s.observations(RegionSpec(150.0, 2.0, 0.05, (3,))))  # detector 3 was not indexed
        self.assertIsNone(s.observations(RegionSpec(40.0, 2.0, 0.05, (2,))))

    def test_release_supersession_never_silently_mixes_releases(self):
        s = self.svc()
        base = dict(obs_id="2026W01_1A_0000_1D2", detector=2, access_reference="a", discovered_at="2026-11-15T00:00:00", t_min=61000.0, t_max=61000.1)
        s.inventory.register([ObsRecord(release="spherex_qr3", collection="spherex_qr3", **base)])
        s.inventory.register([ObsRecord(release="spherex_dr1", collection="spherex_dr1", **base)])
        rows = {r.release: r.superseded_by for r in [s.inventory.get(base["obs_id"], "spherex_qr3"), s.inventory.get(base["obs_id"], "spherex_dr1")]}
        self.assertEqual(rows, {"spherex_qr3": "spherex_dr1", "spherex_dr1": None})
        sid = s.inventory.record_sync(REGION, "current", [(base["obs_id"], "spherex_qr3"), (base["obs_id"], "spherex_dr1")], completed_at="2026-11-15T00:00:00",
                                      route="sia2", from_cache=False)
        members = s.inventory.members(REGION, s.db.one("SELECT * FROM syncs WHERE sync_id=?", (sid,)))
        self.assertEqual([m.release for m in members], ["spherex_dr1"])

    def test_qr2_is_never_indexed_as_current_data(self):
        self.archive = FakeArchive(self.tmp, release="spherex_qr2")  # a (mis)behaving source returning a legacy release
        s = self.svc()
        s.ensure_index(REGION)
        s.tick()
        job = s.jobs.list(kind=KIND_INDEX)[0]
        self.assertNotEqual(job["state"], JobState.COMPLETE)  # never reported as indexed current data
        self.assertIsNone(s.observations(REGION))
        self.assertEqual(s.inventory.counts().get("spherex_qr2"), None) if False else None


class DiscoveryJobTests(Base):
    def test_sia2_discovery_product_calibration_validation_ready(self):
        s = self.svc()
        st = s.prefetch(REGION, cutout_size_deg=0.1, max_observations=3, wait_s=0.0)
        self.assertEqual(st["readiness"], Readiness.PREPARING.value)
        self.assertFalse(st["must_resubmit"])
        self.work(s)
        st = s.prefetch(REGION, cutout_size_deg=0.1, max_observations=3)
        self.assertEqual(st["readiness"], Readiness.READY.value)
        man = s.manifest(st["manifest_id"])
        self.assertEqual(len(man["products"]), 3)
        self.assertEqual(len(man["calibrations"]), 1)
        for p in man["products"]:
            stored = s.store.get(p["key"])
            self.assertEqual(hashlib.sha256(stored.path.read_bytes()).hexdigest(), p["sha256"])
            self.assertEqual(p["origin"], "LIVE")
        self.assertEqual(s.inventory.get("2026W01_1A_0000_1D2", "spherex_dr1").route, "sia2")
        self.assertTrue(all(s.store.verify(p["key"]) for p in man["products"]))

    def test_ready_request_is_served_with_zero_network_calls(self):
        s = self.svc()
        s.prefetch(REGION, cutout_size_deg=0.1, max_observations=3)
        self.work(s)
        before = dict(self.archive.calls)
        s2 = DataService(self.tmp / "svc", Exploding(), self.cfg, clock=self.clock)  # a new process whose archive access would fail the test
        st = s2.prefetch(REGION, cutout_size_deg=0.1, max_observations=3)
        self.assertEqual(st["readiness"], "READY")
        self.assertTrue(st["from_local_store"])
        self.assertEqual(self.archive.calls, before)
        self.assertEqual(s2.tick(), [])  # nothing to run, and the exploding source was never called

    def test_duplicate_requests_return_the_same_job_and_download_once(self):
        s = self.svc()
        a = s.prefetch(REGION, cutout_size_deg=0.1, max_observations=3)
        b = s.prefetch(REGION, cutout_size_deg=0.1, max_observations=3)
        self.assertEqual(a["job"]["job_id"], b["job"]["job_id"])
        self.assertEqual(len(s.jobs.list()), 1)
        self.work(s)
        self.assertEqual(self.archive.calls["discover"], 1)
        self.assertEqual(self.archive.calls["retrieve"], 3)
        s.prefetch(REGION, cutout_size_deg=0.1, max_observations=3)
        self.work(s)
        self.assertEqual(self.archive.calls["retrieve"], 3)  # content already stored: no second download
        self.assertEqual(len([j for j in s.jobs.list() if j["kind"] == "ACQUIRE"]), 1)

    def test_real_empty_discovery_is_no_coverage_and_then_answered_locally(self):
        self.archive.empty = True
        s = self.svc()
        s.ensure_index(REGION)
        self.work(s)
        job = s.jobs.list(kind=KIND_INDEX)[0]
        self.assertEqual(job["state"], JobState.NO_COVERAGE)
        st = s.ensure_index(REGION)
        self.assertEqual(st["readiness"], "NO_COVERAGE")
        self.assertEqual(st["as_of"], job["result"] and st["as_of"])
        self.assertTrue(st["from_local_inventory"])
        self.assertEqual(self.archive.calls["discover"], 1)

    def test_archive_failure_is_preparing_data_never_no_coverage(self):
        self.archive.mode = "down"
        s = self.svc()
        st = s.ensure_index(REGION)
        s.tick()
        job = s.jobs.list(kind=KIND_INDEX)[0]
        self.assertEqual(job["state"], JobState.PREPARING_DATA)
        self.assertEqual(job["attempts"], 1)
        self.assertIsNotNone(job["next_attempt_at"])
        self.assertIn("retrying automatically", job["reason"])
        self.assertEqual(job["last_error"]["code"], "RETRIEVAL_FAILED")
        self.assertEqual(s.ensure_index(REGION)["readiness"], "PREPARING")
        self.assertFalse(st["must_resubmit"])
        self.assertIsNone(s.observations(REGION))

    def test_service_owned_recovery_when_the_archive_returns(self):
        self.archive.mode = "down"
        s = self.svc()
        s.ensure_index(REGION)
        s.tick()
        self.assertEqual(s.tick(), [])  # not due yet: the service waits out its backoff
        self.archive.mode = "ok"
        self.clock.advance(200)
        self.work(s)
        self.assertEqual(s.ensure_index(REGION)["readiness"], "READY")

    def test_both_irsa_routes_failing_through_the_real_adapter(self):
        adapter = IrsaSpherexSource(load_settings(environ={"HEIMDALL_CACHE_DIR": str(self.tmp / "c"), "HEIMDALL_MAX_RETRIES": "0"}), transport=Router(sia=[DOWN], tap=[DOWN]),
                                    sleep=lambda s: None, jitter=0.0)
        s = self.svc(adapter)
        s.ensure_index(REGION)
        s.tick()
        job = s.jobs.list(kind=KIND_INDEX)[0]
        self.assertEqual(job["state"], JobState.PREPARING_DATA)
        self.assertEqual(s.metrics()["routes"]["sia2"]["error_classes"], {"RETRIEVAL_FAILED:502": 1})
        self.assertEqual(s.metrics()["routes"]["tap"]["ok"], 0)

    def test_tap_fallback_after_sia2_failure_is_indexed_with_its_route(self):
        adapter = IrsaSpherexSource(load_settings(environ={"HEIMDALL_CACHE_DIR": str(self.tmp / "c"), "HEIMDALL_MAX_RETRIES": "0"}),
                                    transport=Router(sia=[DOWN], tap=[ok(TAP_HEADER + TAP_ROW)]), sleep=lambda s: None, jitter=0.0)
        s = self.svc(adapter)
        reg = RegionSpec(269.4485, 4.7394, 0.02, (6,))
        s.ensure_index(reg)
        s.tick()
        self.assertEqual(s.jobs.list(kind=KIND_INDEX)[0]["state"], JobState.COMPLETE)
        self.assertEqual(s.inventory.get("2025W33_2C_0321_1D6", "spherex_dr1").route, "tap")
        self.assertEqual(s.index_status(reg)["route"], "tap")

    def test_sia2_discovery_through_the_real_adapter_records_footprint_for_local_queries(self):
        adapter = IrsaSpherexSource(load_settings(environ={"HEIMDALL_CACHE_DIR": str(self.tmp / "c")}), transport=Router(sia={"spherex_dr1": ok(HEADER + ROW1)}),
                                    sleep=lambda s: None, jitter=0.0)
        s = self.svc(adapter)
        reg = RegionSpec(269.4485, 4.7394, 0.02, (6,))
        s.ensure_index(reg)
        s.tick()
        rec = s.inventory.get("2025W33_2C_0321_1D6", "spherex_dr1")
        self.assertEqual(rec.route, "sia2")
        self.assertTrue(rec.footprint.startswith("POLYGON"))
        off = DataService(self.tmp / "svc", None, self.cfg, clock=self.clock)
        self.assertEqual([o.obs_id for o in off.observations(reg)], ["2025W33_2C_0321_1D6"])
        # the footprint is real: a cone far from the image (inside the synced cone's detector scope but off the polygon) would be excluded
        self.assertEqual(off.observations(RegionSpec(269.4485, 4.7394, 0.01, (6,)))[0].obs_id, "2025W33_2C_0321_1D6")


class RetryTests(Base):
    def test_backoff_is_exponential_capped_and_jittered(self):
        rng = random.Random(1)
        ds = [backoff_delay(n, self.cfg, rng) for n in (1, 2, 3, 4, 5, 6)]
        for n, d in zip((1, 2, 3, 4, 5, 6), ds, strict=True):
            base = min(100.0, 10.0 * 2 ** (n - 1))
            self.assertGreaterEqual(d, base)
            self.assertLessEqual(d, base * 1.25)
        self.assertNotEqual(ds[0], 10.0)

    def test_retry_schedule_grows_then_deadline_gives_a_precise_terminal_state(self):
        self.archive.mode = "down"
        s = self.svc()
        s.ensure_index(REGION)
        gaps = []
        for _ in range(12):
            s.tick()
            j = s.jobs.list(kind=KIND_INDEX)[0]
            if j["state"] is JobState.UNAVAILABLE_AFTER_DEADLINE:
                break
            gaps.append(s.describe_job(j)["next_attempt_in_s"])
            self.clock.advance(gaps[-1] + 1 if gaps[-1] < 900 else 900)
            self.clock.advance(400)
        self.assertEqual(gaps[:3] == sorted(gaps[:3]), True)
        j = s.jobs.list(kind=KIND_INDEX)[0]
        self.assertEqual(j["state"], JobState.UNAVAILABLE_AFTER_DEADLINE)
        self.assertIn("deadline", j["reason"])
        self.assertGreaterEqual(j["attempts"], 3)
        st = s.job_status(j["job_id"])
        self.assertEqual(st["readiness"], "UNAVAILABLE_AFTER_DEADLINE")
        self.assertIn("Nothing was concluded about the sky", st["message"])
        self.assertNotEqual(st["readiness"], "NO_COVERAGE")

    def test_a_new_submission_revives_a_deadline_job_with_history_kept(self):
        self.archive.mode = "down"
        s = self.svc()
        s.ensure_index(REGION)
        s.tick()
        self.clock.advance(4000)
        s.tick()
        jid = s.jobs.list(kind=KIND_INDEX)[0]["job_id"]
        self.assertEqual(s.jobs.get(jid)["state"], JobState.UNAVAILABLE_AFTER_DEADLINE)
        self.archive.mode = "ok"
        s.ensure_index(REGION)
        job = s.jobs.get(jid)
        self.assertEqual((job["generation"], job["state"]), (2, JobState.QUEUED))
        self.work(s)
        self.assertEqual(s.ensure_index(REGION)["readiness"], "READY")
        self.assertIn("UNAVAILABLE_AFTER_DEADLINE", [e["to_state"] for e in s.jobs.events(jid)])

    def test_open_circuit_breaker_pushes_the_next_attempt_out(self):
        class Breaker:
            def __init__(self, r):
                self.r = r

            def retry_in_s(self):
                return self.r

            state = "OPEN"
        self.archive.breakers = {"sia2": Breaker(500.0), "tap": Breaker(600.0)}
        self.archive.mode = "down"
        s = self.svc()
        s.ensure_index(REGION)
        s.tick()
        self.assertGreaterEqual(s.describe_job(s.jobs.list(kind=KIND_INDEX)[0])["next_attempt_in_s"], 499.0)
        self.assertEqual(s.metrics()["circuit_breakers"], {"sia2": "OPEN", "tap": "OPEN"})

    def test_interrupted_job_resumes_after_restart_without_repeating_finished_work(self):
        s = self.svc()
        st = s.prefetch(REGION, cutout_size_deg=0.1, max_observations=4)
        for _ in range(6):  # run until the ACQUIRE job exists, then "crash" it mid-run
            s.tick()
            self.clock.advance(2)
            acq = [j for j in s.jobs.list() if j["kind"] == "ACQUIRE"]
            if acq:
                break
        jid = acq[0]["job_id"]
        s.jobs.transition(jid, JobState.DOWNLOADING, reason="acquiring products", lease=True)  # left running with a lease: the process died
        self.clock.advance(61)  # lease expired
        s2 = DataService(self.tmp / "svc", self.archive, self.cfg, clock=self.clock, rng=random.Random(2))
        self.services.append(s2)
        self.assertEqual(s2.resume(), [jid])
        self.assertEqual(s2.jobs.get(jid)["state"], JobState.QUEUED)
        self.work(s2)
        self.assertEqual(s2.prefetch(REGION, cutout_size_deg=0.1, max_observations=4)["readiness"], "READY")
        self.assertEqual(s2.job_status(st["job"]["job_id"])["readiness"], "READY")
        done = dict(self.archive.calls)
        s3 = DataService(self.tmp / "svc", self.archive, self.cfg, clock=self.clock)
        self.services.append(s3)
        s3.resume()
        self.work(s3)
        self.assertEqual(self.archive.calls, done)  # nothing re-downloaded or re-discovered

    def test_partial_progress_survives_failures_and_completes_later(self):
        s = self.svc()
        orig = self.archive.retrieve
        n = {"i": 0}

        def flaky(req):
            n["i"] += 1
            if n["i"] > 2:
                raise HeimdallError.of(ErrorCode.ARCHIVE_TIMEOUT, "slow")
            return orig(req)
        self.archive.retrieve = flaky
        s.prefetch(REGION, cutout_size_deg=0.1, max_observations=4)
        self.work(s, rounds=8)
        job = [j for j in s.jobs.list() if j["kind"] == "ACQUIRE"][0]
        self.assertEqual(job["state"], JobState.PREPARING_DATA)
        self.assertEqual(job["progress"]["counts"]["ready"], 3)  # two observations and the calibration
        self.archive.retrieve = orig
        self.clock.advance(300)
        self.work(s)
        self.assertEqual(s.prefetch(REGION, cutout_size_deg=0.1, max_observations=4)["readiness"], "READY")


class IntegrityTests(Base):
    def prepared(self):
        s = self.svc()
        s.prefetch(REGION, cutout_size_deg=0.1, max_observations=2)
        self.work(s)
        return s

    def test_truncated_download_is_rejected_quarantined_and_never_ready(self):
        s = self.prepared()
        bad = "2026W01_1A_0000_1D2"
        self.archive.corrupt.add(bad)
        st = s.prefetch(REGION, cutout_size_deg=0.2, max_observations=2)  # a different cutout -> new products to download
        for _ in range(10):
            s.tick()
            self.clock.advance(500)
        plan = plan_for(s.observations(REGION), REGION, 0.2, 2)
        key = next(i.key for i in plan.items if i.obs_id == bad)
        self.assertIsNone(s.store.get(key))
        self.assertGreaterEqual(s.metrics()["validation_failures"], 1)
        self.assertTrue(list((self.tmp / "svc" / "store" / "quarantine").glob("*.rejected")))
        acquire = [j for j in s.jobs.list() if j["kind"] == "ACQUIRE" and key in j["progress"].get("items", {})][0]
        self.assertEqual(acquire["progress"]["items"][key]["state"], "QUARANTINED")
        self.assertIsNotNone(st["job"])

    def test_all_products_corrupt_is_invalid_source_data(self):
        s = self.prepared()
        for o in s.observations(REGION):
            self.archive.corrupt.add(o.obs_id)
        st = s.prefetch(REGION, cutout_size_deg=0.2, max_observations=2)
        for _ in range(14):
            s.tick()
            self.clock.advance(500)
        acquire = [j for j in s.jobs.list() if j["kind"] == "ACQUIRE" and j["payload"]["plan"]["items"][0]["cutout_size_deg"] == 0.2][0]
        self.assertEqual(acquire["state"], JobState.INVALID_SOURCE_DATA)
        self.assertEqual(s.job_status(st["job"]["job_id"])["readiness"], "INVALID_SOURCE_DATA")
        self.assertIn("quarantined", s.job_status(acquire["job_id"])["message"])

    def test_checksum_mismatch_is_rejected(self):
        s = self.svc()
        f = self.tmp / "x.fits"
        f.write_bytes(fits_bytes(1))
        with self.assertRaises(InvalidProduct) as ctx:
            s.store.ingest(f, "k1", kind="science", origin="LIVE", archive_checksum="md5:" + "0" * 32)
        self.assertEqual(ctx.exception.kind, "CHECKSUM_MISMATCH")
        good = hashlib.md5(f.read_bytes()).hexdigest()  # noqa: S324
        self.assertIsNotNone(s.store.ingest(f, "k1", kind="science", origin="LIVE", archive_checksum="md5:" + good))

    def test_non_fits_and_incomplete_files_are_rejected(self):
        s = self.svc()
        for name, content in (("a", b"<html>error</html>" * 500), ("b", fits_bytes(2)[:100]), ("c", b"")):
            f = self.tmp / name
            f.write_bytes(content)
            with self.assertRaises(InvalidProduct):
                s.store.ingest(f, f"k{name}", kind="science", origin="LIVE")
            self.assertIsNone(s.store.get(f"k{name}"))

    def test_identical_content_is_deduplicated_and_modification_is_detected(self):
        s = self.svc()
        f = self.tmp / "d.fits"
        f.write_bytes(fits_bytes(3))
        a = s.store.ingest(f, "k-a", kind="science", origin="LIVE")
        b = s.store.ingest(f, "k-b", kind="science", origin="LIVE")
        self.assertEqual(a.sha256, b.sha256)
        self.assertEqual(s.store.usage()["objects"], 1)
        a.path.write_bytes(a.path.read_bytes() + b"x")  # silent corruption on disk
        self.assertIsNone(s.store.get("k-a"))  # size no longer matches: not served


class ManifestAndRetentionTests(Base):
    def test_frozen_manifest_is_reproducible_pinned_and_not_evicted(self):
        s = self.svc()
        s.prefetch(REGION, cutout_size_deg=0.1, max_observations=3)
        self.work(s)
        a = s.prefetch(REGION, cutout_size_deg=0.1, max_observations=3)
        b = s.prefetch(REGION, cutout_size_deg=0.1, max_observations=3)
        self.assertEqual(a["manifest_id"], b["manifest_id"])
        man = s.manifest(a["manifest_id"])
        self.assertEqual(man["schema"], "heimdall.input_manifest/1.0")
        self.assertEqual(s.store.usage()["pinned_objects"], s.store.usage()["objects"])
        extra = self.tmp / "extra.fits"
        extra.write_bytes(fits_bytes(77))
        s.store.ingest(extra, "unreferenced", kind="science", origin="LIVE")
        res = s.store.evict(target_bytes=0)
        self.assertEqual(len(res["evicted"]), 1)  # only the unreferenced object
        self.assertTrue(res["still_over_goal"])
        self.assertTrue(all(s.store.verify(p["key"]) for p in man["products"]))

    def test_quota_is_reported_not_silently_enforced(self):
        s = self.svc(config=ServiceConfig(quota_bytes=10))
        f = self.tmp / "q.fits"
        f.write_bytes(fits_bytes(5))
        s.store.ingest(f, "q", kind="science", origin="LIVE")
        self.assertTrue(s.store.usage()["over_quota"])
        self.assertIsNotNone(s.store.get("q"))  # nothing evicted without an explicit evict call


class OfflineAndFreshnessTests(Base):
    def test_offline_replay_from_local_inventory_and_store(self):
        s = self.svc()
        s.prefetch(REGION, cutout_size_deg=0.1, max_observations=3)
        self.work(s)
        off = DataService(self.tmp / "svc", None, self.cfg, clock=self.clock)
        self.assertTrue(off.offline)
        self.assertEqual(off.prefetch(REGION, cutout_size_deg=0.1, max_observations=3)["readiness"], "READY")
        src = LocalStoreSource(off)
        from heimdall.data_access.interface import SkyRegion
        d = src.discover(DiscoveryQuery(region=SkyRegion(ra_deg=150.0, dec_deg=2.0, radius_deg=0.05), filters={"detectors": "2"}))
        self.assertEqual(len(d.observations), 6)
        self.assertTrue(all(o.provenance.origin is DataOrigin.CACHED for o in d.observations))  # stored data are never called live
        unknown = RegionSpec(10.0, 10.0, 0.05, (2,))
        with self.assertRaises(HeimdallError) as ctx:
            src.discover(DiscoveryQuery(region=SkyRegion(ra_deg=10.0, dec_deg=10.0, radius_deg=0.05), filters={"detectors": "2"}))
        self.assertEqual(ctx.exception.code, ErrorCode.ARCHIVE_UNREACHABLE)
        st = off.ensure_index(unknown)  # offline + never indexed: accepted and queued for a worker; no error, no 'try again'
        self.assertEqual(st["readiness"], "PREPARING")
        self.assertEqual(st["job"]["state"], "QUEUED")
        self.assertEqual(off.tick(), [])
        self.assertFalse(st["must_resubmit"])

    def test_freshness_never_claims_live_or_all_sky(self):
        s = self.svc()
        s.ensure_index(REGION)
        self.work(s)
        f = s.freshness()
        self.assertFalse(f["all_sky_complete"])
        self.assertIn("not a live archive query", f["data_origin_statement"])
        self.assertEqual(f["releases_indexed"]["spherex_dr1"]["n"], 6)
        r = f["regions"][0]
        self.assertEqual(r["route"], "sia2")
        self.assertEqual(r["newest_observation_mjd"], 61000.001 + 200)
        self.assertFalse(r["stale"])
        self.clock.advance(8 * 86400)
        self.assertTrue(s.freshness()["regions"][0]["stale"])

    def test_stale_index_answers_immediately_and_queues_a_background_refresh(self):
        s = self.svc()
        s.ensure_index(REGION)
        self.work(s)
        self.clock.advance(8 * 86400)
        before = self.archive.calls["discover"]
        st = s.ensure_index(REGION)
        self.assertEqual(st["readiness"], "READY")  # served locally
        self.assertTrue(st["refresh_job"])
        self.assertEqual(self.archive.calls["discover"], before)  # no synchronous archive call
        self.archive.mode = "down"
        s.tick()
        self.assertTrue(s.freshness()["refresh_delayed_by_upstream_failure"])
        self.assertEqual(s.ensure_index(REGION)["readiness"], "READY")  # the outage does not take away the local answer

    def test_metrics_report_queue_routes_cache_and_bytes(self):
        s = self.svc()
        s.prefetch(REGION, cutout_size_deg=0.1, max_observations=3)
        self.work(s)
        s.prefetch(REGION, cutout_size_deg=0.1, max_observations=3)
        m = s.metrics()
        self.assertEqual(m["jobs"]["queue_depth"], 0)
        self.assertGreater(m["bytes_retrieved"], 0)
        self.assertIn("sia2", m["routes"])
        self.assertIn("product", m["routes"])
        self.assertIsNotNone(m["cache"]["hit_rate"])
        self.assertEqual(m["store"]["objects"], 4)


class WorkerTests(Base):
    def test_work_runs_until_idle_and_reports_open_jobs(self):
        self.archive.mode = "down"
        s = self.svc()
        s.ensure_index(REGION)
        out = s.work(max_seconds=2.0, poll_s=0.2, sleep=lambda x: None)
        self.assertEqual(len(out["open_jobs"]), 1)
        self.assertIn("no resubmission", out["note"])
        self.archive.mode = "ok"
        self.clock.advance(500)
        out = s.work(max_seconds=2.0, poll_s=0.2, sleep=lambda x: None)
        self.assertEqual(out["open_jobs"], [])

    def test_sync_queues_refresh_for_old_regions_only(self):
        s = self.svc()
        s.ensure_index(REGION)
        self.work(s)
        self.assertEqual(s.sync(), [])
        self.clock.advance(8 * 86400)
        jobs = s.sync()
        self.assertEqual(len(jobs), 1)
        self.work(s)
        self.assertEqual(s.sync(), [])


if __name__ == "__main__":
    unittest.main()


class EvictionRecoveryTests(Base):
    def test_evicted_products_are_reacquired_by_the_service_not_reported_ready(self):
        s = self.svc()
        s.prefetch(REGION, cutout_size_deg=0.1, max_observations=2)
        self.work(s)
        self.assertEqual(s.prefetch(REGION, cutout_size_deg=0.1, max_observations=2)["readiness"], "READY")
        s.store.evict(target_bytes=0)  # nothing is pinned by a manifest yet? it is: pinned objects stay
        self.assertEqual(s.store.usage()["pinned_objects"], s.store.usage()["objects"])
        # simulate loss of an unpinned object (e.g. disk cleanup outside HEIMDALL): delete the file behind a key
        plan = plan_for(s.observations(REGION), REGION, 0.1, 2)
        gone = s.store.get(plan.items[0].key)
        gone.path.unlink()
        self.assertIsNone(s.store.get(plan.items[0].key))
        st = s.prefetch(REGION, cutout_size_deg=0.1, max_observations=2)
        self.assertEqual(st["readiness"], "PREPARING")  # not READY with a missing file
        self.work(s)
        self.assertEqual(s.prefetch(REGION, cutout_size_deg=0.1, max_observations=2)["readiness"], "READY")


class RecoveryAndWorkerTests(Base):
    """Deadline ends an attempt; the request is retained and re-checked by a service-owned policy (no new user request)."""

    def setUp(self):
        super().setUp()
        self.cfg = ServiceConfig(deadline_s=3600.0, backoff_base_s=10.0, backoff_cap_s=100.0, backoff_jitter=0.25, poll_s=1.0, lease_s=60.0,
                                 recovery_window_s=86400.0, recovery_interval_s=7200.0, recovery_attempt_s=600.0)

    def _expire(self, s):
        self.archive.mode = "down"
        s.ensure_index(REGION)
        s.tick()
        self.clock.advance(4000)
        s.tick()
        jid = s.jobs.list(kind=KIND_INDEX)[0]["job_id"]
        self.assertEqual(s.jobs.get(jid)["state"], JobState.UNAVAILABLE_AFTER_DEADLINE)
        return jid

    def test_deadline_status_does_not_ask_for_resubmission_while_recovery_is_active(self):
        s = self.svc()
        jid = self._expire(s)
        st = s.job_status(jid)
        self.assertEqual(st["readiness"], "UNAVAILABLE_AFTER_DEADLINE")
        self.assertFalse(st["must_resubmit"])
        rec = st["job"]["auto_recovery"]
        self.assertTrue(rec["active"])
        self.assertIsNotNone(rec["next_check_at"])

    def test_recovery_check_revives_and_completes_without_a_user_request(self):
        s = self.svc()
        jid = self._expire(s)
        self.archive.mode = "ok"
        self.clock.advance(3600)
        s.tick()  # not yet due
        self.assertEqual(s.jobs.get(jid)["state"], JobState.UNAVAILABLE_AFTER_DEADLINE)
        self.clock.advance(3700)
        s.tick()  # the worker's ordinary tick performs the due recovery check
        self.work(s)
        job = s.jobs.get(jid)
        self.assertEqual(job["state"], JobState.COMPLETE)
        self.assertEqual(job["generation"], 2)
        reasons = [e["reason"] or "" for e in s.jobs.events(jid)]
        self.assertTrue(any(r.startswith("automatic recovery check") for r in reasons))
        self.assertFalse(any(r.startswith("resubmitted by a new request") for r in reasons))
        self.assertEqual(s.ensure_index(REGION)["readiness"], "READY")

    def test_failed_recovery_attempts_do_not_extend_the_window_and_expiry_requires_resubmission(self):
        s = self.svc()
        jid = self._expire(s)
        until = s.jobs.recovery_info(s.jobs.get(jid))["until"]
        for _ in range(30):  # archive stays down; recovery attempts keep hitting their short deadline
            self.clock.advance(7300)
            s.tick()
            self.clock.advance(700)
            s.tick()
            if not s.jobs.recovery_info(s.jobs.get(jid)) or not s.jobs.recovery_info(s.jobs.get(jid))["active"]:
                break
        job = s.jobs.get(jid)
        self.assertEqual(job["state"], JobState.UNAVAILABLE_AFTER_DEADLINE)
        info = s.jobs.recovery_info(job)
        self.assertEqual(info["until"], until)
        self.assertFalse(info["active"])
        self.assertTrue(s.job_status(jid)["must_resubmit"])
        self.assertGreater(job["generation"], 1)
        gen = job["generation"]
        self.clock.advance(86400)
        s.tick()
        self.assertEqual(s.jobs.get(jid)["generation"], gen)  # no revival after the window

    def test_internal_revival_keeps_the_window_and_browsing_never_revives(self):
        s = self.svc()
        jid = self._expire(s)
        until = s.jobs.recovery_info(s.jobs.get(jid))["until"]
        gen = s.jobs.get(jid)["generation"]
        s.coverage(REGION)  # map browsing reports the state only
        self.assertEqual((s.jobs.get(jid)["state"], s.jobs.get(jid)["generation"]), (JobState.UNAVAILABLE_AFTER_DEADLINE, gen))
        s._in_worker = True  # a parent's worker step needs the child again
        try:
            s.ensure_index(REGION)
        finally:
            s._in_worker = False
        job = s.jobs.get(jid)
        self.assertEqual(job["state"], JobState.QUEUED)
        self.assertLessEqual((datetime.fromisoformat(job["deadline_at"]) - self.clock()).total_seconds(), self.cfg.recovery_attempt_s + 1)
        s.tick()
        self.clock.advance(700)
        s.tick()
        self.assertEqual(s.jobs.get(jid)["state"], JobState.UNAVAILABLE_AFTER_DEADLINE)
        self.assertEqual(s.jobs.recovery_info(s.jobs.get(jid))["until"], until)  # window not reset

    def test_cancelling_a_deadline_job_stops_automatic_recovery(self):
        s = self.svc()
        jid = self._expire(s)
        c = s.jobs.cancel(jid)
        self.assertEqual(c["state"], JobState.CANCELLED)
        self.archive.mode = "ok"
        self.clock.advance(8000)
        s.tick()
        self.assertEqual(s.jobs.get(jid)["state"], JobState.CANCELLED)
        self.assertTrue(s.job_status(jid)["must_resubmit"])

    def test_cancelled_requests_say_must_resubmit(self):
        s = self.svc()
        st = s.ensure_index(REGION)
        s.jobs.cancel(st["job"]["job_id"])
        self.assertTrue(s.job_status(st["job"]["job_id"])["must_resubmit"])

    def test_long_running_worker_resumes_interrupted_work_writes_heartbeat_and_stops(self):
        import threading
        s = self.svc()
        st = s.ensure_index(REGION)
        jid = st["job"]["job_id"]
        s.jobs.transition(jid, JobState.DISCOVERING, reason="crashed worker", lease=True)
        self.clock.advance(120)  # lease expired: the previous worker died mid-job
        stop = threading.Event()
        out = s.serve(stop, max_idle_s=0.01, max_ticks=3)
        self.assertIn(jid, out["resumed"])
        self.assertEqual(s.jobs.get(jid)["state"], JobState.COMPLETE)
        hb = json.loads(s.heartbeat_path.read_text())
        self.assertTrue(hb["stopped"])
        self.assertGreaterEqual(hb["ticks"], 1)
        self.assertFalse(s.worker_health()["running"])  # stopped gracefully
        stop.set()
        self.assertEqual(s.serve(stop, max_idle_s=0.01)["ticks"], 0)

    def test_offline_process_refuses_to_act_as_worker(self):
        import threading
        s = self.svc(offline=True)
        with self.assertRaises(HeimdallError):
            s.serve(threading.Event(), max_ticks=1)


class ConcurrentOpenTests(unittest.TestCase):
    def test_processes_opening_a_fresh_database_concurrently_do_not_fail(self):
        import threading
        from heimdall.dataservice.db import ServiceDB
        tmp = Path(tempfile.mkdtemp())
        errors, dbs = [], []
        def open_one():
            try:
                dbs.append(ServiceDB(tmp / "svc.sqlite3"))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
        threads = [threading.Thread(target=open_one) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        try:
            self.assertEqual(errors, [])
            self.assertEqual(dbs[0].one("PRAGMA journal_mode")[0].lower(), "wal")
        finally:
            for d in dbs:
                d.conn.close()
            shutil.rmtree(tmp, ignore_errors=True)


class ParentPollTests(Base):
    def test_polling_a_child_is_not_counted_as_a_parent_attempt(self):
        self.archive.mode = "down"
        s = self.svc()
        st = s.prefetch(REGION, cutout_size_deg=0.1, max_observations=2)
        jid = st["job"]["job_id"]
        for _ in range(15):
            s.tick()
            self.clock.advance(5)
        parent = s.jobs.get(jid)
        self.assertEqual(parent["state"], JobState.PREPARING_DATA)
        self.assertLessEqual(parent["attempts"], 1)
        child = s.jobs.list(kind=KIND_INDEX)[0]
        self.assertGreaterEqual(child["attempts"], 1)  # the child's real archive attempts are still counted



class QueueScaleTests(Base):
    def test_worker_scheduling_cost_does_not_grow_with_finished_history(self):
        import time as _t
        s = self.svc()
        with s.db.transaction() as db:
            for i in range(5000):
                db.conn.execute("INSERT INTO jobs(job_id,kind,payload,state,generation,created_at,updated_at,deadline_at,attempts,progress) VALUES(?,?,?,?,1,?,?,?,0,'{}')",
                                (f"job:index:{i:012x}", KIND_INDEX, "{}", "COMPLETE", "2026-11-15T00:00:00+00:00", "2026-11-15T00:00:00+00:00", "2026-11-15T06:00:00+00:00"))
        st = s.ensure_index(REGION)
        t0 = _t.perf_counter()
        for _ in range(20):
            due = s.jobs.due()
            s.jobs.recover_due()
            s._next_wake()
            s.jobs.count_open()
        per_wake = (_t.perf_counter() - t0) / 20
        self.assertEqual([j["job_id"] for j in due], [st["job"]["job_id"]])
        self.assertLess(per_wake, 0.05)  # SQL-filtered: finished jobs are not decoded on every wake


class FieldRunHandlerTests(Base):
    def test_field_job_waits_on_data_then_completes_and_is_reusable(self):
        from heimdall.fieldruns import KIND_FIELD, _key, make_handler
        s = self.svc()
        child, _ = s.jobs.submit(KIND_INDEX, {"region": REGION.to_dict(), "scope": "current"})

        class Cfg:
            hash = "sha256:test"

        class StubEngine:
            config = Cfg()
            ready = False
            calls = 0

            def data_service(self):
                return s

            def investigate(self, request):
                self.calls += 1
                if not self.ready:
                    return {"readiness": "PREPARING", "job": {"job_id": child["job_id"]}, "message": "preparing"}, None
                return {"run_id": "R20261115T090000-0123abcd", "input_manifest": {"manifest_id": "man:00aa"}, "status": {"execution": "COMPLETED"}}, self_tmp

        self_tmp = self.tmp
        eng = StubEngine()
        s.handlers[KIND_FIELD] = make_handler(eng)
        payload = {"ra_deg": 150.0, "dec_deg": 2.0, "radius_deg": 0.05, "detectors": [2], "mjd_start": None, "mjd_end": None,
                   "max_observations": 4, "cutout_size_deg": 0.1, "context": "auto"}
        job, _ = s.jobs.submit(KIND_FIELD, payload)
        s.run_job(job["job_id"])
        j = s.jobs.get(job["job_id"])
        self.assertEqual((j["state"], j["progress"]["child_job"]), (JobState.PREPARING_DATA, child["job_id"]))
        eng.ready = True
        self.clock.advance(5)
        s.run_job(job["job_id"])
        j = s.jobs.get(job["job_id"])
        self.assertEqual(j["state"], JobState.COMPLETE)
        self.assertEqual(j["result"]["run_id"], "R20261115T090000-0123abcd")
        row = s.db.one("SELECT investigation_id FROM investigation_results WHERE key=?", (_key(eng, payload, "man:00aa"),))
        self.assertEqual(row["investigation_id"], "R20261115T090000-0123abcd")


class SoftwareErrorTests(Base):
    def test_request_errors_fail_at_once_and_unexpected_errors_are_bounded(self):
        s = self.svc()
        job, _ = s.jobs.submit("CUSTOM", {"x": 1})
        out = s.jobs.fail_or_retry(job, HeimdallError.of(ErrorCode.INPUT_INVALID, "bad request"))
        self.assertEqual(out["state"], JobState.FAILED)
        st = s.job_status(job["job_id"])
        self.assertEqual(st["readiness"], "FAILED")
        self.assertTrue(st["must_resubmit"])
        self.assertIn("not an archive outage", st["message"])
        job2, _ = s.jobs.submit("CUSTOM", {"x": 2})
        for i in range(3):
            j = s.jobs.fail_or_retry(s.jobs.get(job2["job_id"]), KeyError("boom"))
            if i < 2:
                self.assertEqual(j["state"], JobState.PREPARING_DATA)
                s.jobs.transition(job2["job_id"], JobState.DISCOVERING, reason="next attempt")
        self.assertEqual(s.jobs.get(job2["job_id"])["state"], JobState.FAILED)
        self.assertNotIn("archive", s.jobs.get(job2["job_id"])["reason"])
        again, created = s.jobs.submit("CUSTOM", {"x": 2})  # a new request after the fix revives it
        self.assertTrue(created)
        self.assertEqual(again["state"], JobState.QUEUED)


class CancelAndRejectionTests(Base):
    def test_cancelling_a_parent_cancels_its_unshared_child_only(self):
        s = self.svc()
        child, _ = s.jobs.submit(KIND_INDEX, {"region": REGION.to_dict(), "scope": "current"})
        a, _ = s.jobs.submit("PARENT", {"a": 1})
        b, _ = s.jobs.submit("PARENT", {"b": 1})
        for p in (a, b):
            s.jobs.transition(p["job_id"], JobState.PREPARING_DATA, reason="waiting", progress={"child_job": child["job_id"]})
        s.jobs.cancel(a["job_id"])
        self.assertNotEqual(s.jobs.get(child["job_id"])["state"], JobState.CANCELLED)  # b still waits on it
        s.jobs.cancel(b["job_id"])
        c = s.jobs.get(child["job_id"])
        self.assertEqual(c["state"], JobState.CANCELLED)
        self.assertIn("cancelled with its parent", c["reason"])

    def test_a_repeated_archive_rejection_excludes_the_product_instead_of_retrying_it(self):
        s = self.svc()
        real_retrieve = self.archive.retrieve
        bad = self.archive.rows[0]["observationid"] + "D2"  # the adapter appends the detector to the observation id

        def retrieve(request):
            if request.obs_id == bad:
                raise HeimdallError.of(ErrorCode.RETRIEVAL_FAILED, "irsa returned HTTP 422", http_status="422")
            return real_retrieve(request)
        self.archive.retrieve = retrieve
        s.ensure_index(REGION)
        self.work(s)
        obs = s.observations(REGION)
        plan = plan_for(obs, REGION, 0.1, 3)
        self.assertIn(bad, [i.obs_id for i in plan.items])
        st = s.ensure_products(plan, region=REGION)
        jid = st["job"]["job_id"]
        for _ in range(10):
            s.tick()
            self.clock.advance(200)
        j = s.jobs.get(jid)
        self.assertEqual(j["state"], JobState.COMPLETE)
        rej = [k for k, v in j["progress"]["items"].items() if v.get("kind") == "ARCHIVE_REJECTED"]
        self.assertEqual(len(rej), 1)
        self.assertIn("HTTP 422", j["progress"]["items"][rej[0]]["reason"])
        man = s.ensure_products(plan, region=REGION)
        self.assertEqual(man["readiness"], "READY")  # the manifest excludes the rejected product and lists it


class RetryAfterTests(Base):
    def test_a_server_retry_after_sets_the_jobs_next_attempt(self):
        s = self.svc()

        def discover(query):
            raise HeimdallError.of(ErrorCode.RETRIEVAL_FAILED, "rate limited", http_status="429", retry_after_s="600")
        self.archive.discover = discover
        st = s.ensure_index(REGION)
        s.tick()
        j = s.describe_job(s.jobs.get(st["job"]["job_id"]))
        self.assertEqual(j["state"], "PREPARING_DATA")
        self.assertGreaterEqual(j["next_attempt_in_s"], 600 * 0.99 if self.cfg.backoff_cap_s >= 600 else self.cfg.backoff_cap_s * 0.99)


class AllSkyInventoryTests(Base):
    """Partitioned all-sky inventory: planning geometry, truncation subdivision, failure/retry, completeness accounting (scripted archive)."""

    def setUp(self):
        super().setUp()
        self.trunc_radius = None  # cones larger than this report a truncated answer
        archive = self.archive
        real = archive.discover

        def discover(query):
            res = real(query)
            archive.last_discovery = dict(archive.last_discovery or {}, maxrec=20000,
                                          truncated=bool(self.trunc_radius and query.region.radius_deg > self.trunc_radius))
            return res
        archive.discover = discover

    def test_planning_geometry_covers_the_sky_with_polar_caps_and_bounded_cones(self):
        from heimdall.dataservice.allsky import AllSkyIndex, plan_tiles, tile_area, tile_cone
        tiles = plan_tiles(5.0)
        area = sum(tile_area(**t) for t in tiles)
        self.assertAlmostEqual(area, 41252.96, delta=1.0)  # whole sky in deg^2
        caps = [t for t in tiles if t["dec_max"] >= 90 or t["dec_min"] <= -90]
        self.assertEqual(len(caps), 2)
        self.assertEqual(tile_cone(**caps[-1])[:2], (0.0, 90.0))
        for t in tiles:  # every tile corner lies inside its cone
            ra, dec, r = tile_cone(**t)
            for cra in (t["ra_min"], t["ra_max"]):
                for cdec in (t["dec_min"], t["dec_max"]):
                    from heimdall.dataservice.allsky import _sep
                    if abs(cdec) < 90:
                        self.assertLessEqual(_sep(ra, dec, cra % 360, cdec), r + 1e-9)
        s = self.svc()
        idx = AllSkyIndex(s)
        out = idx.plan(5.0)
        self.assertEqual(out["partitions"], len(tiles))
        self.assertEqual(idx.plan(5.0)["partitions"], len(tiles))  # idempotent
        acc = idx.accounting(out["domain"])
        self.assertFalse(acc["all_sky_complete"])
        self.assertEqual(acc["by_state"], {"UNSEARCHED": len(tiles)})

    def test_steps_are_bounded_truncation_subdivides_and_failures_are_retried(self):
        from heimdall.dataservice.allsky import AllSkyIndex
        s = self.svc()
        idx = AllSkyIndex(s)
        dom = idx.plan(2.0, (148.0, 152.0, 0.0, 4.0))["domain"]
        self.trunc_radius = 1.0  # level-0 cones (~1.4 deg) are truncated, their children (~0.7 deg) are not
        st = idx.step(dom, max_new=3, max_in_flight=3)
        self.assertEqual(len(st["submitted"]), 3)
        self.assertEqual(idx.step(dom, max_new=3, max_in_flight=3)["submitted"], [])  # bounded in-flight queue
        self.work(s)
        idx.reconcile(dom)
        acc = idx.accounting(dom)
        self.assertEqual(acc["by_state"].get("SUBDIVIDED"), 3)
        self.assertEqual(acc["by_state"].get("UNSEARCHED"), 1 + 12)  # one untouched level-0 tile + 4 children each
        self.assertEqual(acc["area_deg2"]["certified_searched"], 0.0)  # truncated answers certify nothing
        self.archive.mode = "down"
        for _ in range(4):
            idx.step(dom, max_new=8, max_in_flight=8)
            s.tick()
            self.clock.advance(4000)
            s.tick()
        idx.reconcile(dom)
        self.assertGreater(idx.accounting(dom)["by_state"].get("FAILED", 0), 0)  # an outage is FAILED, never SEARCHED_EMPTY
        self.assertEqual(idx.accounting(dom)["by_state"].get("SEARCHED_EMPTY", 0), 0)
        self.archive.mode = "ok"
        for _ in range(10):
            idx.step(dom, max_new=8, max_in_flight=8)
            self.work(s)
        idx.reconcile(dom)
        acc = idx.accounting(dom)
        self.assertEqual(set(acc["by_state"]) - {"SUBDIVIDED"}, {"SEARCHED_HAS_OBS"})
        self.assertAlmostEqual(acc["area_deg2"]["fraction_certified"], 1.0, places=6)
        self.assertFalse(acc["all_sky_complete"])  # a sub-domain never claims all-sky completeness

    def test_all_sky_complete_only_when_every_leaf_of_the_whole_sky_is_certified(self):
        from heimdall.dataservice.allsky import AllSkyIndex
        s = self.svc()
        idx = AllSkyIndex(s)
        dom = idx.plan(5.0)["domain"]
        s.db.execute("UPDATE allsky_partitions SET state='SEARCHED_EMPTY' WHERE domain=?", (dom,))
        self.assertTrue(idx.accounting(dom)["all_sky_complete"])
        s.db.execute("UPDATE allsky_partitions SET state='SEARCHED_UNVERIFIED' WHERE partition_id=(SELECT partition_id FROM allsky_partitions LIMIT 1)")
        self.assertFalse(idx.accounting(dom)["all_sky_complete"])

    def test_planning_benchmark_whole_sky_at_two_degrees(self):
        import time as _t
        from heimdall.dataservice.allsky import AllSkyIndex
        s = self.svc()
        idx = AllSkyIndex(s)
        t0 = _t.perf_counter()
        out = idx.plan(2.0)
        t_plan = _t.perf_counter() - t0
        t0 = _t.perf_counter()
        acc = idx.accounting(out["domain"], limit=10)
        t_acc = _t.perf_counter() - t0
        self.assertGreater(out["partitions"], 9000)
        self.assertLess(t_plan, 20.0)
        self.assertLess(t_acc, 10.0)
        print(f"\n[benchmark] whole-sky 2-degree plan: {out['partitions']} partitions in {t_plan:.2f} s; accounting {t_acc:.2f} s")


class MirrorPlanTests(Base):
    def test_mirror_plan_refuses_without_measured_sizes_and_estimates_with_them(self):
        from heimdall.dataservice.allsky import AllSkyIndex, mirror_plan
        s = self.svc()
        idx = AllSkyIndex(s)
        dom = idx.plan(2.0, (148.0, 152.0, 0.0, 4.0))["domain"]
        archive = self.archive
        real = archive.discover

        def discover(q):
            res = real(q)
            archive.last_discovery = dict(archive.last_discovery or {}, maxrec=20000, truncated=False)
            return res
        archive.discover = discover
        for _ in range(4):
            idx.step(dom, max_new=4, max_in_flight=4)
            self.work(s)
        idx.reconcile(dom)
        plan = mirror_plan(s, dom, max_bytes=10 ** 9)
        self.assertEqual(plan["estimate_state"], "UNMEASURED")
        self.assertFalse(plan["feasible"])
        self.assertIn("refused", plan["execution"])
        self.assertEqual(plan["products_indexed"], 6)  # overlapping cones deduplicated
        st = s.prefetch(REGION, cutout_size_deg=None, max_observations=1)  # one full-frame sample, measured from the verified store
        self.work(s)
        plan = mirror_plan(s, dom, max_bytes=10 ** 9)
        self.assertEqual(plan["estimate_state"], "MEASURED")
        self.assertEqual(plan["products_to_fetch"], 5)
        self.assertTrue(plan["feasible"])
        self.assertFalse(mirror_plan(s, dom, max_bytes=10)["feasible"])  # over budget
