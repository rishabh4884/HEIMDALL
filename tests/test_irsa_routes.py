"""IRSA discovery routes: SIA2 preferred, bounded TAP fallback, circuit breaker, caching, failure never an empty result.

Transports are fakes; nothing here touches the network. ``tests/fixtures/sia2_dr1_live_sample.csv`` is the genuine response format and two
genuine rows of an IRSA SIA2 DR1 answer retrieved on 2026-11-15 (used only to exercise parsing).
"""

import random
import shutil
import tempfile
import unittest
from pathlib import Path

from heimdall.config import load_settings
from heimdall.contracts import ErrorCode, HeimdallError, ResultOutcome
from heimdall.data_access import DiscoveryQuery, SkyRegion
from heimdall.data_access.http import HttpClient, HttpResponse, TransportTimeout
from heimdall.data_access.irsa_spherex import IrsaSpherexSource
from heimdall.data_access.resilience import CircuitBreaker

LIVE = (Path(__file__).parent / "fixtures" / "sia2_dr1_live_sample.csv").read_text()
HEADER = LIVE.splitlines()[0] + "\n"
ROW1 = LIVE.splitlines()[1] + "\n"
TAP_HEADER = ("collection,observationid,energy_bandpassname,energy_bounds_lower,energy_bounds_upper,time_bounds_lower,"
              "time_bounds_upper,provenance_version,uri,contentchecksum,contentlength\n")
TAP_ROW = ("spherex_dr1,2025W33_2C_0321_1,SPHEREx-D6,4.4e-06,5.0e-06,60902.86,60902.87,27,"
           "ibe/data/spherex/dr1/level2/2025W33_2C/l2b-v27-2026-228/6/level2_2025W33_2C_0321_1D6_spx_l2b-v27-2026-228.fits,md5:00ff,1000\n")
Q = DiscoveryQuery(region=SkyRegion(ra_deg=269.4485, dec_deg=4.7394, radius_deg=0.02))
DOWN = HttpResponse(502, b"<html>Proxy Error</html>")


def ok(text):
    return HttpResponse(200, text.encode() if isinstance(text, str) else text)


class Router:
    """Routes by URL: SIA2 (GET .../SIA?) and TAP (POST .../TAP/sync) get separate scripted behaviour."""

    def __init__(self, sia=None, tap=None):
        self.sia, self.tap, self.calls = sia, tap, []

    def __call__(self, method, url, data, headers, timeout):
        route = "sia2" if "/SIA?" in url else "tap" if "/TAP/" in url else "other"
        self.calls.append((route, method, url, timeout))
        item = {"sia2": self.sia, "tap": self.tap}[route]
        if route == "sia2" and isinstance(item, dict):  # per-collection answers; other collections are validly empty
            col = url.split("COLLECTION=")[1].split("&")[0]
            return item.get(col, ok(HEADER))
        if item is None:
            raise AssertionError(f"unexpected call to {route}")
        item = item[min(sum(1 for c in self.calls if c[0] == route) - 1, len(item) - 1)] if isinstance(item, list) else item
        if isinstance(item, Exception):
            raise item
        return item

    def routes(self):
        return [c[0] for c in self.calls]


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def src(self, router, offline=False, **kw):
        env = {"HEIMDALL_CACHE_DIR": str(self.tmp), "HEIMDALL_MAX_RETRIES": "1"}
        if offline:
            env["HEIMDALL_OFFLINE"] = "1"
        self.sleeps = []
        kw.setdefault("jitter", 0.0)
        return IrsaSpherexSource(load_settings(environ=env), transport=router, sleep=self.sleeps.append, **kw)

    def test_sia2_is_preferred_and_parses_the_live_format(self):
        router = Router(sia={"spherex_dr1": ok(HEADER + ROW1)})
        s = self.src(router)
        res = s.discover(Q)
        self.assertEqual(res.outcome, ResultOutcome.OK)
        self.assertEqual(set(router.routes()), {"sia2"})
        obs = res.observations[0]
        self.assertEqual(obs.obs_id, "2025W33_2C_0321_1D6")
        p = obs.provenance.products[0]
        self.assertEqual((p.release, p.product_version), ("spherex_dr1", "27-2026-228"))
        self.assertTrue(p.access_reference.startswith("ibe/data/spherex/dr1/level2/"))
        self.assertIsNone(p.checksum)  # SIA2 carries no checksum; HEIMDALL records sha256 of what it retrieves
        self.assertEqual(s.last_discovery["route_used"], "sia2")
        self.assertIn("SIA2", res.provenance.notes)
        self.assertEqual(res.provenance.origin.value, "LIVE")

    def test_sia2_502_falls_over_to_tap(self):
        # SIA2 502 -> TAP success (the reverse failover direction)
        router = Router(sia=[DOWN], tap=[ok(TAP_HEADER + TAP_ROW)])
        s = self.src(router)
        res = s.discover(Q)
        self.assertEqual(res.outcome, ResultOutcome.OK)
        self.assertEqual(s.last_discovery["route_used"], "tap")
        self.assertEqual([a["outcome"] for a in s.last_discovery["attempts"]], ["FAILED", "OK"])
        self.assertIn("earlier route(s) failed", res.provenance.notes)
        self.assertEqual(res.observations[0].provenance.products[0].checksum, "00ff")

    def test_tap_primary_502_falls_back_to_sia2(self):
        router = Router(sia={"spherex_dr1": ok(HEADER + ROW1)}, tap=[DOWN])
        s = self.src(router, route_order=("tap", "sia2"))
        res = s.discover(Q)
        self.assertEqual(res.outcome, ResultOutcome.OK)
        self.assertEqual(s.last_discovery["route_used"], "sia2")
        self.assertEqual(s.last_discovery["attempts"][0]["http_status"], "502")

    def test_tap_timeout_falls_back_to_sia2(self):
        router = Router(sia={"spherex_dr1": ok(HEADER + ROW1)}, tap=[TransportTimeout("t")])
        s = self.src(router, route_order=("tap", "sia2"))
        res = s.discover(Q)
        self.assertEqual(s.last_discovery["route_used"], "sia2")
        self.assertEqual(s.last_discovery["attempts"][0]["error_code"], "ARCHIVE_TIMEOUT")
        self.assertEqual(res.outcome, ResultOutcome.OK)

    def test_both_routes_failing_raises_and_is_never_no_observations(self):
        router = Router(sia=[DOWN], tap=[DOWN])
        s = self.src(router)
        with self.assertRaises(HeimdallError) as ctx:
            s.discover(Q)
        self.assertNotEqual(ctx.exception.code, ErrorCode.INPUT_INVALID)
        self.assertIn("every route", ctx.exception.info.message)
        self.assertEqual({a["route"] for a in s.last_discovery["attempts"]}, {"sia2", "tap"})
        self.assertIsNone(s.last_discovery["route_used"])
        # bounded: max_retries=1 -> 2 attempts per route
        self.assertEqual(len(router.calls), 4)

    def test_both_routes_timing_out_raises_timeout(self):
        s = self.src(Router(sia=[TransportTimeout("t")], tap=[TransportTimeout("t")]))
        with self.assertRaises(HeimdallError) as ctx:
            s.discover(Q)
        self.assertEqual(ctx.exception.code, ErrorCode.ARCHIVE_TIMEOUT)

    def test_valid_empty_sia2_answer_is_no_observations(self):
        res = self.src(Router(sia=[ok(HEADER)])).discover(Q)
        self.assertEqual(res.outcome, ResultOutcome.NO_OBSERVATIONS)
        self.assertEqual(res.observations, ())

    def test_malformed_sia2_responses_are_failures_not_empty(self):
        bodies = ["<VOTABLE><INFO name='QUERY_STATUS' value='ERROR'/></VOTABLE>", "", "a,b\n1,2\n",
                  HEADER.replace("access_url", "something_else") + ROW1]
        for body in bodies:
            with self.subTest(body=body[:20]):
                s = self.src(Router(sia=[ok(body)], tap=[ok("<error/>")]))
                with self.assertRaises(HeimdallError) as ctx:
                    s.discover(Q)
                self.assertEqual(ctx.exception.code, ErrorCode.DATA_MALFORMED)
        self.assertEqual(list((self.tmp / "irsa-spherex").glob("*.bin")) if (self.tmp / "irsa-spherex").exists() else [], [])

    def test_malformed_sia2_falls_over_to_valid_tap(self):
        s = self.src(Router(sia=[ok("<error/>")], tap=[ok(TAP_HEADER + TAP_ROW)]))
        self.assertEqual(s.discover(Q).outcome, ResultOutcome.OK)
        self.assertEqual(s.last_discovery["route_used"], "tap")

    def test_non_irsa_or_non_spherex_access_urls_are_rejected(self):
        for url in ("https://evil.example/ibe/data/spherex/dr1/level2/x/level2_A_1D2_spx.fits",
                    "http://irsa.ipac.caltech.edu/ibe/data/spherex/dr1/level2/x/level2_A_1D2_spx.fits",
                    "https://irsa.ipac.caltech.edu/ibe/data/other/level2_A_1D2_spx.fits",
                    "https://irsa.ipac.caltech.edu/ibe/data/spherex/../x/level2_A_1D2_spx.fits"):
            with self.subTest(url=url):
                cells = ROW1.split(",")
                bad = HEADER + ",".join(url if c.startswith("https://irsa") else c for c in cells)
                with self.assertRaises(HeimdallError) as ctx:
                    self.src(Router(sia=[ok(bad)], tap=[ok("<e/>")])).discover(Q)
                self.assertEqual(ctx.exception.code, ErrorCode.DATA_MALFORMED)

    def test_non_science_rows_are_filtered_not_listed(self):
        row = ROW1.replace(",science,", ",calibration,", 1)
        self.assertEqual(self.src(Router(sia={"spherex_dr1": ok(HEADER + row)})).discover(Q).outcome, ResultOutcome.NO_OBSERVATIONS)

    def test_row_from_the_wrong_collection_is_malformed(self):
        row = ROW1.replace("spherex_dr1", "spherex_qr2")
        with self.assertRaises(HeimdallError):
            self.src(Router(sia=[ok(HEADER + row)], tap=[ok("<e/>")])).discover(DiscoveryQuery(region=Q.region, release="spherex_dr1"))

    def test_offline_replay_from_cached_sia2_discovery(self):
        live = self.src(Router(sia={"spherex_dr1": ok(HEADER + ROW1)}))
        first = live.discover(Q)
        off = self.src(Router(), offline=True)  # any network call would raise AssertionError
        again = off.discover(Q)
        self.assertEqual(again.provenance.origin.value, "CACHED")
        self.assertEqual([o.obs_id for o in again.observations], [o.obs_id for o in first.observations])
        self.assertEqual(off.last_discovery["route_used"], "sia2")
        self.assertTrue(off.last_discovery["from_cache"])
        other = DiscoveryQuery(region=SkyRegion(ra_deg=10.0, dec_deg=2.0, radius_deg=0.02))
        with self.assertRaises(HeimdallError) as ctx:  # cache miss offline is a failure, not "no observations"
            self.src(Router(), offline=True).discover(other)
        self.assertEqual(ctx.exception.code, ErrorCode.ARCHIVE_UNREACHABLE)

    def test_error_pages_are_not_cached(self):
        s = self.src(Router(sia=[ok("<html/>")], tap=[DOWN]))
        with self.assertRaises(HeimdallError):
            s.discover(Q)
        with self.assertRaises(HeimdallError):
            self.src(Router(), offline=True).discover(Q)

    def test_legacy_collection_uses_tap_only_and_is_labelled(self):
        router = Router(tap=[ok(TAP_HEADER + TAP_ROW.replace("spherex_dr1", "spherex_qr2"))])
        s = self.src(router)
        res = s.discover(DiscoveryQuery(region=Q.region, release="spherex_qr2"))
        self.assertEqual(set(router.routes()), {"tap"})
        self.assertIn("legacy", res.provenance.notes)
        # a legacy-only TAP outage must not be answered by SIA2 (which cannot search QR2)
        s2 = self.src(Router(tap=[DOWN]))
        with self.assertRaises(HeimdallError):
            s2.discover(DiscoveryQuery(region=Q.region, release="spherex_qr2"))

    def test_default_tap_query_is_restricted_to_current_collections(self):
        router = Router(sia=[DOWN], tap=[ok(TAP_HEADER)])
        self.src(router).discover(Q)
        tap_call = next(c for c in router.calls if c[0] == "tap")
        self.assertEqual(tap_call[1], "POST")

    def test_circuit_breaker_opens_and_skips_a_failing_route(self):
        now = [0.0]
        router = Router(sia=[DOWN], tap=[ok(TAP_HEADER + TAP_ROW)])
        s = self.src(router, clock=lambda: now[0], breaker_threshold=2, breaker_cooldown_s=100.0)
        s.discover(Q)
        s.discover(Q)
        self.assertEqual(s.breakers["sia2"].state, "OPEN")
        n = sum(1 for c in router.calls if c[0] == "sia2")
        s.discover(Q)
        self.assertEqual(sum(1 for c in router.calls if c[0] == "sia2"), n)  # skipped while open
        self.assertEqual(s.last_discovery["attempts"][0]["outcome"], "SKIPPED_CIRCUIT_OPEN")
        now[0] = 150.0
        s.discover(Q)
        self.assertGreater(sum(1 for c in router.calls if c[0] == "sia2"), n)  # half-open trial

    def test_all_routes_open_is_a_failure(self):
        now = [0.0]
        s = self.src(Router(sia=[DOWN], tap=[DOWN]), clock=lambda: now[0], breaker_threshold=1, breaker_cooldown_s=100.0)
        with self.assertRaises(HeimdallError):
            s.discover(Q)
        with self.assertRaises(HeimdallError) as ctx:
            s.discover(Q)
        self.assertIn("cooldown", ctx.exception.info.message)

    def test_auth_errors_do_not_fail_over_or_trip_the_breaker(self):
        router = Router(sia=[HttpResponse(403, b"")], tap=[ok(TAP_HEADER + TAP_ROW)])
        s = self.src(router)
        with self.assertRaises(HeimdallError) as ctx:
            s.discover(Q)
        self.assertEqual(ctx.exception.code, ErrorCode.ACCESS_DENIED)
        self.assertEqual(s.breakers["sia2"].failures, 0)

    def test_route_timeouts_differ_per_route(self):
        router = Router(sia=[ok(HEADER)], tap=[ok(TAP_HEADER)])
        s = self.src(router, route_order=("tap", "sia2"), route_timeouts={"tap": 11.0, "sia2": 22.0})
        s.discover(Q)
        self.assertEqual(router.calls[0][3], 11.0)
        s2 = self.src(Router(sia=[ok(HEADER)]), route_timeouts={"tap": 11.0, "sia2": 22.0})
        r = s2._http.transport
        s2.discover(Q)
        self.assertEqual(r.calls[0][3], 22.0)

    def test_backoff_has_bounded_jitter(self):
        slept = []
        client = HttpClient("x", 5, 3, lambda *a: HttpResponse(503, b""), slept.append, backoff_s=1.0, jitter=0.25, rng=random.Random(1))
        with self.assertRaises(HeimdallError):
            client.request("GET", "https://irsa.ipac.caltech.edu/x")
        self.assertEqual(len(slept), 3)
        for k, d in enumerate(slept):
            self.assertGreaterEqual(d, 2 ** k)
            self.assertLessEqual(d, 2 ** k * 1.25)
        self.assertNotEqual(slept, [1.0, 2.0, 4.0])

    def test_circuit_breaker_unit(self):
        t = [0.0]
        b = CircuitBreaker(2, 10.0, lambda: t[0])
        b.failure()
        self.assertTrue(b.allow())
        b.failure()
        self.assertFalse(b.allow())
        t[0] = 11.0
        self.assertTrue(b.allow())
        b.failure()
        self.assertFalse(b.allow())  # half-open trial failed -> open again
        b.success()
        self.assertEqual(b.state, "CLOSED")

    def test_route_health_reports_each_component_without_raising(self):
        def transport(method, url, data, headers, timeout):
            if "/SIA?" in url:
                return ok(HEADER + ROW1)
            if "/TAP/" in url:
                return DOWN
            if "nasa-irsa-spherex" in url:
                return HttpResponse(200, b"")
            return ok(b"SIMPLE  =                    T" + b" " * 60)
        health = {h["route"]: h for h in self.src(transport).route_health()}
        self.assertEqual({k: v["status"] for k, v in health.items()}, {"tap": "FAILED", "sia2": "OK", "product": "OK", "calibration": "OK"})
        self.assertEqual(health["tap"]["http_status"], "502")
        report = self.src(transport).check_access()
        self.assertIn("sia2", report.detail)
        with self.assertRaises(HeimdallError):
            self.src(Router(sia=[DOWN], tap=[DOWN])).check_access()


if __name__ == "__main__":
    unittest.main()
