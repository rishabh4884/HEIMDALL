"""Live checks against the public IRSA SPHEREx archive.

Skipped unless HEIMDALL_REAL_DATA_TESTS=1. They download about 15 MB (two
cutouts and one ePSF calibration file) into a temporary cache and need network
access. They verify that the adapter and ingestion still match the live
service; they do not validate any scientific result.
"""

import os
import shutil
import tempfile
import unittest

from heimdall.api import Engine
from heimdall.config import load_settings
from heimdall.data_access.irsa_spherex import IrsaSpherexSource
from heimdall.pipeline.runner import InvestigationRequest
from tests.contract.data_source_contract import DataSourceContract

FIELD = (150.7817877, 3.3502204)  # neutral field used in IRSA's SPHEREx Source Discovery Tool tutorial
ENABLED = os.environ.get("HEIMDALL_REAL_DATA_TESTS") == "1"


@unittest.skipUnless(ENABLED, "set HEIMDALL_REAL_DATA_TESTS=1 to run live IRSA checks")
class LiveIrsaConformance(DataSourceContract, unittest.TestCase):
    expect_synthetic = False

    def make_source(self):
        return IrsaSpherexSource(load_settings(environ={"HEIMDALL_CACHE_DIR": tempfile.mkdtemp()}))


@unittest.skipUnless(ENABLED, "set HEIMDALL_REAL_DATA_TESTS=1 to run live IRSA checks")
class LiveInvestigation(unittest.TestCase):
    def test_two_real_cutouts_through_the_pipeline(self):
        tmp = tempfile.mkdtemp()
        try:
            engine = Engine(settings=load_settings(environ={"HEIMDALL_CACHE_DIR": tmp, "HEIMDALL_STATE_DIR": tmp,
                                                            "HEIMDALL_REQUEST_TIMEOUT_S": "180"}))
            cov = engine.coverage("irsa-spherex", *FIELD, 0.02, detectors=[2])
            self.assertEqual(cov["outcome"], "OK")
            result, _ = engine.investigate(InvestigationRequest(ra_deg=FIELD[0], dec_deg=FIELD[1], radius_deg=0.02,
                                                                source_id="irsa-spherex", detectors=[2], max_observations=2))
            self.assertEqual(result["data_origin"], "REAL")
            self.assertIn(result["status"]["execution"], ("COMPLETED", "PARTIAL"))
            included = [o for o in result["observations"] if o["included"]]
            self.assertTrue(included)
            self.assertEqual(included[0]["meta"]["psf"]["kind"], "EPSF_CALIBRATION")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipUnless(ENABLED, "set HEIMDALL_REAL_DATA_TESTS=1 to run live IRSA checks")
class LiveContextServices(unittest.TestCase):
    def test_gaia_cone_and_a_known_asteroid_in_a_real_exposure(self):
        from heimdall.context import CachedService
        from heimdall.context.gaia import GaiaCatalogue
        from heimdall.context.small_bodies import SmallBodyIdentifier
        tmp = tempfile.mkdtemp()
        try:
            settings = load_settings(environ={"HEIMDALL_CACHE_DIR": tmp, "HEIMDALL_REQUEST_TIMEOUT_S": "180"})
            gaia = GaiaCatalogue(CachedService("gaia", settings)).cone(*FIELD, 0.05, 21.0)
            self.assertEqual(gaia.status, "COMPLETED")
            self.assertGreater(gaia.n_stars, 0)
            # Real exposure 2025W47_2A_0507_3D2: time and spacecraft state from its header.
            state = (-4.832063e3, 3.386248e3, 3.816402e3, 4.034652, -1.298129, 6.226891)
            q = SmallBodyIdentifier(CachedService("jpl_sbident", settings)).field(
                "2025W47_2A_0507_3D2", 61001.798483759, 150.78117, 3.349619, 0.0603, state)
            self.assertEqual(q.status, "COMPLETED", q.reason)
            self.assertIn("368 Haidea (A893 KC)", [p.name for p in q.predictions])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
