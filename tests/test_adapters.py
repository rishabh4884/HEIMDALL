"""IRSA SPHEREx adapter (fake transport, no network) and local-FITS adapter behaviour.

The fake TAP responses below are TEST INPUTS with invented identifiers; they
exercise parsing and failure handling only.
"""

import io
import shutil
import tempfile
import unittest
from pathlib import Path

from astropy.io import fits

from heimdall.config import load_settings
from heimdall.contracts import DataOrigin, ErrorCode, HeimdallError, ResultOutcome
from heimdall.data_access import DiscoveryQuery, RetrievalRequest, SkyRegion
from heimdall.data_access.cache import ProductCache
from heimdall.data_access.http import HttpResponse, TransportTimeout, TransportTlsError, TransportUnreachable
from heimdall.data_access.irsa_spherex import IrsaSpherexSource
from heimdall.data_access.local_fits import LocalFitsSource
from tests.contract.data_source_contract import DataSourceContract
from tests.helpers import generated, small_scenario

HEADER = ("collection,observationid,energy_bandpassname,energy_bounds_lower,energy_bounds_upper,time_bounds_lower,"
          "time_bounds_upper,provenance_version,uri,contentchecksum,contentlength\n")
ROW = ("spherex_qr2,TESTONLY_0001_1,SPHEREx-D2,1.1e-06,1.6e-06,61000.0,61000.001,6.4,"
       "ibe/data/spherex/qr2/level2/TEST/level2_TESTONLY_0001_1D2_spx.fits,md5:00ff,1000\n")
SIA_HEADER = "obs_collection,obs_id,access_url,t_min,t_max,em_min,em_max\n"
SIA_ROW = ("spherex_dr1,arbitrary-obscore-id,https://irsa.ipac.caltech.edu/ibe/data/spherex/dr1/level2/2025W33_2C/"
           "l2b-v27-2026-228/2/level2_2025W33_2C_0321_1D2_spx_l2b-v27-2026-228.fits,60902.0,60902.1,1.1e-06,1.6e-06\n")
FITS_BYTES = io.BytesIO()
fits.PrimaryHDU().writeto(FITS_BYTES)
FITS_BYTES = FITS_BYTES.getvalue()
QUERY = DiscoveryQuery(region=SkyRegion(ra_deg=150.0, dec_deg=2.0, radius_deg=0.05))


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, data, headers, timeout):
        self.calls.append((method, url, data))
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item


def ok(text):
    return HttpResponse(status=200, body=text.encode() if isinstance(text, str) else text)


class IrsaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.settings = load_settings(environ={"HEIMDALL_CACHE_DIR": str(self.tmp), "HEIMDALL_MAX_RETRIES": "2"})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def source(self, *responses, route_order=("tap", "sia2")):
        self.transport = FakeTransport(*responses)
        self.sleeps = []
        return IrsaSpherexSource(self.settings, transport=self.transport, sleep=self.sleeps.append, route_order=route_order, jitter=0.0)

    def test_discovery_parses_rows_into_tagged_observations(self):
        result = self.source(ok(HEADER + ROW)).discover(QUERY)
        self.assertEqual(result.outcome, ResultOutcome.OK)
        obs = result.observations[0]
        self.assertEqual(obs.obs_id, "TESTONLY_0001_1D2")
        self.assertEqual(obs.metadata["detector"].value, "2")
        self.assertAlmostEqual(obs.metadata["wavelength_min"].value, 1.1)
        product = obs.provenance.products[0]
        self.assertEqual((product.checksum_algorithm, product.release), ("md5", "spherex_qr2"))
        self.assertFalse(obs.is_synthetic)
        adql = self.transport.calls[0][2].decode()
        self.assertIn("CONTAINS", adql)

    def test_empty_result_is_no_observations(self):
        result = self.source(ok(HEADER)).discover(QUERY)
        self.assertEqual(result.outcome, ResultOutcome.NO_OBSERVATIONS)

    def test_transient_tap_failure_falls_back_to_documented_sia2(self):
        """SIA2 uses ObsCore CSV fields; no TAP-table schema is assumed."""
        down = HttpResponse(502, b"proxy error")
        query = DiscoveryQuery(region=QUERY.region, release="spherex_dr1", filters={"detectors": "2"})
        result = self.source(down, down, down, ok(SIA_HEADER + SIA_ROW)).discover(query)
        self.assertEqual(result.outcome, ResultOutcome.OK)
        self.assertEqual(len(result.observations), 1)
        obs = result.observations[0]
        self.assertEqual(obs.obs_id, "2025W33_2C_0321_1D2")
        self.assertEqual(obs.provenance.products[0].access_reference,
                         "ibe/data/spherex/dr1/level2/2025W33_2C/l2b-v27-2026-228/2/level2_2025W33_2C_0321_1D2_spx_l2b-v27-2026-228.fits")
        self.assertIsNone(obs.provenance.products[0].checksum)
        self.assertIn("SIA2", result.provenance.notes or "")
        self.assertTrue(self.transport.calls[-1][1].startswith("https://irsa.ipac.caltech.edu/SIA?"))

    def test_malformed_responses(self):
        for body in ("<VOTABLE><INFO name='QUERY_STATUS' value='ERROR'/></VOTABLE>", "a,b\n1,2\n"):
            with self.subTest(body=body[:10]):
                with self.assertRaises(HeimdallError) as ctx:
                    self.source(ok(body)).discover(QUERY)
                self.assertEqual(ctx.exception.code, ErrorCode.DATA_MALFORMED)
        bad_row = ROW.replace("SPHEREx-D2", "SPHEREx-X9")
        result = self.source(ok(HEADER + ROW + bad_row)).discover(QUERY)
        self.assertEqual(result.outcome, ResultOutcome.PARTIAL)
        self.assertEqual(result.failed_items[0].error.code, ErrorCode.DATA_MALFORMED)

    def test_http_and_network_failures_have_distinct_codes(self):
        cases = [(HttpResponse(401, b""), ErrorCode.AUTHENTICATION_FAILED), (HttpResponse(403, b""), ErrorCode.ACCESS_DENIED),
                 (HttpResponse(404, b""), ErrorCode.RETRIEVAL_FAILED), (HttpResponse(503, b""), ErrorCode.RETRIEVAL_FAILED),
                 (TransportTimeout("t"), ErrorCode.ARCHIVE_TIMEOUT), (TransportUnreachable("u"), ErrorCode.ARCHIVE_UNREACHABLE),
                 (TransportTlsError("x"), ErrorCode.ARCHIVE_UNREACHABLE)]
        for response, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(HeimdallError) as ctx:
                    self.source(response).discover(QUERY)
                self.assertEqual(ctx.exception.code, code)

    def test_retries_are_bounded_and_only_for_transient_failures(self):
        with self.assertRaises(HeimdallError):
            self.source(TransportTimeout("t")).discover(QUERY)
        # After bounded TAP retries, a timeout makes one bounded SIA2 attempt.
        # The fake transport times out there too; it must still stop promptly.
        self.assertEqual(len(self.transport.calls), 6)
        self.assertEqual(self.sleeps, [1.0, 2.0, 1.0, 2.0])
        with self.assertRaises(HeimdallError):
            self.source(HttpResponse(403, b"")).discover(QUERY)
        self.assertEqual(len(self.transport.calls), 1)
        result = self.source(HttpResponse(503, b""), ok(HEADER + ROW)).discover(QUERY)
        self.assertEqual(result.outcome, ResultOutcome.OK)

    def test_filters_are_validated_before_querying(self):
        for filters in ({"collections": "x'; DROP"}, {"detectors": "7"}, {"mjd_start": "soon"}):
            with self.subTest(filters=filters):
                with self.assertRaises(HeimdallError) as ctx:
                    self.source(ok(HEADER)).discover(DiscoveryQuery(region=QUERY.region, filters=filters))
                self.assertEqual(ctx.exception.code, ErrorCode.INPUT_INVALID)

    def test_cutout_retrieval_is_cached_and_verified(self):
        src = self.source(ok(FITS_BYTES))
        req = RetrievalRequest(obs_id="TESTONLY_0001_1D2", region=QUERY.region,
                               options={"access_reference": "ibe/data/spherex/qr2/x.fits"})
        first = src.retrieve(req)
        self.assertEqual(first.provenance.origin, DataOrigin.LIVE)
        self.assertIn("center=150.00000000,2.00000000d&size=0.100000", self.transport.calls[0][1])
        second = src.retrieve(req)
        self.assertEqual(second.provenance.origin, DataOrigin.CACHED)
        self.assertEqual(len(self.transport.calls), 1)
        Path(second.products[0].location).write_bytes(FITS_BYTES + b"corrupt")
        third = src.retrieve(req)
        self.assertEqual(third.provenance.origin, DataOrigin.LIVE)
        self.assertEqual(src.cache.corrupted_removed, 1)

    def test_non_fits_and_bad_references_are_rejected(self):
        req = RetrievalRequest(obs_id="TESTONLY_0001_1D2", options={"access_reference": "ibe/x.fits"})
        with self.assertRaises(HeimdallError) as ctx:
            self.source(ok(b"<html>error</html>")).retrieve(req)
        self.assertEqual(ctx.exception.code, ErrorCode.DATA_MALFORMED)
        for ref in ("https://elsewhere.example/x.fits", "ibe/../../etc/passwd"):
            with self.assertRaises(HeimdallError):
                self.source(ok(FITS_BYTES)).retrieve(RetrievalRequest(obs_id="TESTONLY_0001_1D2", options={"access_reference": ref}))

    def test_full_product_checksum_mismatch(self):
        req = RetrievalRequest(obs_id="TESTONLY_0001_1D2", options={"access_reference": "ibe/x.fits", "archive_checksum": "md5:00"})
        with self.assertRaises(HeimdallError) as ctx:
            self.source(ok(FITS_BYTES)).retrieve(req)
        self.assertEqual(ctx.exception.code, ErrorCode.DATA_MALFORMED)

    def test_cache_invalidation(self):
        cache = ProductCache(self.tmp / "c")
        cache.put("u1", b"a")
        cache.put("u2", b"b")
        self.assertEqual(len(cache.entries()), 2)
        self.assertEqual(cache.invalidate(url_contains="u1"), 1)
        self.assertIsNone(cache.get("u1"))
        self.assertIsNotNone(cache.get("u2"))


class IrsaConformance(DataSourceContract, unittest.TestCase):
    expect_synthetic = False

    def make_source(self):
        settings = load_settings(environ={"HEIMDALL_CACHE_DIR": tempfile.mkdtemp()})
        return IrsaSpherexSource(settings, transport=FakeTransport(ok("observationid\nTESTONLY_0001_1\n")), sleep=lambda s: None)


class LocalFitsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gen = generated()
        cls.settings = load_settings(environ={})

    def test_discovery_retrieval_and_synthetic_label(self):
        src = LocalFitsSource(self.settings, root=self.gen.directory)
        sc = small_scenario()
        result = src.discover(DiscoveryQuery(region=SkyRegion(ra_deg=sc.ra0, dec_deg=sc.dec0, radius_deg=0.05)))
        self.assertEqual(result.outcome, ResultOutcome.OK)
        self.assertEqual(len(result.observations), len(self.gen.files))
        self.assertTrue(all(o.is_synthetic for o in result.observations))
        got = src.retrieve(RetrievalRequest(obs_id=result.observations[0].obs_id))
        self.assertEqual(got.products[0].product.checksum_algorithm, "sha256")
        far = src.discover(DiscoveryQuery(region=SkyRegion(ra_deg=(sc.ra0 + 90) % 360, dec_deg=0.0, radius_deg=0.05)))
        self.assertEqual(far.outcome, ResultOutcome.NO_OBSERVATIONS)
        det = src.discover(DiscoveryQuery(region=SkyRegion(ra_deg=sc.ra0, dec_deg=sc.dec0, radius_deg=0.05),
                                          filters={"detectors": "1"}))
        self.assertTrue(all(o.metadata["detector"].value == "1" for o in det.observations))

    def test_unconfigured_and_missing_directory(self):
        with self.assertRaises(HeimdallError) as ctx:
            LocalFitsSource(self.settings).check_access()
        self.assertEqual(ctx.exception.code, ErrorCode.SOURCE_NOT_CONFIGURED)
        with self.assertRaises(HeimdallError) as ctx:
            LocalFitsSource(self.settings, root=Path("/nonexistent/heimdall")).check_access()
        self.assertEqual(ctx.exception.code, ErrorCode.ARCHIVE_UNREACHABLE)

    def test_directory_mixing_synthetic_and_unmarked_files_is_refused(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            shutil.copy(self.gen.files[0], tmp / "a.fits")
            unmarked = tmp / "unmarked_copy_for_guard_test.fits"
            shutil.copy(self.gen.files[1], unmarked)
            with fits.open(unmarked, mode="update") as h:
                h[0].header["SYNTHETC"] = False
                h["IMAGE"].header["OBSID"] = "GUARDTEST"
            sc = small_scenario()
            with self.assertRaises(HeimdallError) as ctx:
                LocalFitsSource(self.settings, root=tmp).discover(
                    DiscoveryQuery(region=SkyRegion(ra_deg=sc.ra0, dec_deg=sc.dec0, radius_deg=0.05)))
            self.assertEqual(ctx.exception.code, ErrorCode.INPUT_INVALID)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_unreadable_file_is_a_failed_item(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            shutil.copy(self.gen.files[0], tmp / "a.fits")
            (tmp / "broken.fits").write_bytes(b"garbage")
            sc = small_scenario()
            result = LocalFitsSource(self.settings, root=tmp).discover(
                DiscoveryQuery(region=SkyRegion(ra_deg=sc.ra0, dec_deg=sc.dec0, radius_deg=0.05)))
            self.assertEqual(result.outcome, ResultOutcome.PARTIAL)
            self.assertEqual(result.failed_items[0].ref, "broken.fits")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class LocalFitsConformance(DataSourceContract, unittest.TestCase):
    expect_synthetic = True

    def make_source(self):
        return LocalFitsSource(load_settings(environ={}), root=generated().directory)

    def query(self):
        sc = small_scenario()
        return DiscoveryQuery(region=SkyRegion(ra_deg=sc.ra0, dec_deg=sc.dec0, radius_deg=0.05))


if __name__ == "__main__":
    unittest.main()
