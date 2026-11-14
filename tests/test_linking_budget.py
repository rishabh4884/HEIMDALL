"""Linker evaluation bound: completion, explicit truncation, and the injection validator's handling of both (SYNTHETIC inputs)."""

import contextlib
import dataclasses
import math
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from heimdall.api import Engine
from heimdall.config import load_settings
from heimdall.engine import validation
from heimdall.engine.association import SourceCluster
from heimdall.engine.config import EngineConfig
from heimdall.engine.linking import link_moving
from heimdall.pipeline.runner import InvestigationRequest
from tests.helpers import generated, make_det, small_scenario

D = 1.0 / 3600.0


def many_tracklets(n_epochs=5, per_epoch=8):
    """Unrelated two-detection tracklets in several epochs: every cross-epoch pair is feasible (640 pairs)."""
    rng = np.random.default_rng(1)
    inputs, observer = [], {}
    for e in range(n_epochs):
        for k in range(per_epoch):
            ra, dec = 150.0 + rng.uniform(-100, 100) * D, 2.0 + rng.uniform(-100, 100) * D
            dets = []
            for j in range(2):
                obs = f"e{e}k{k}v{j}"
                observer[obs] = np.array([math.cos(0.5 * e + 0.001 * j), math.sin(0.5 * e + 0.001 * j), 0.0])
                dets.append(make_det(f"{obs}:1", obs, ra + 0.2 * j * D, dec, mjd=61000.0 + 60.0 * e + 0.3 * j))
            c = SourceCluster(source_id=f"S{e}{k}", ra=ra, dec=dec, cov_arcsec2=[[0.04, 0], [0, 0.04]], det_ids=[d.det_id for d in dets],
                              obs_ids=[d.obs_id for d in dets], n_obs=2)
            inputs.append((c, dets, e))
    return inputs, observer


def with_cap(cap):
    cfg = EngineConfig()
    return dataclasses.replace(cfg, planetx=dataclasses.replace(cfg.planetx, max_link_evaluations=cap))


class LinkerBoundTests(unittest.TestCase):
    def test_default_bound_lets_a_search_beyond_500_evaluations_complete(self):
        inputs, observer = many_tracklets()
        res = link_moving(inputs, observer, EngineConfig())
        self.assertGreater(res.n_feasible_pairs, 500)
        self.assertGreater(res.n_evaluations, 500)
        self.assertFalse(res.truncated)
        self.assertEqual(res.notes, [])

    def test_exceeding_the_bound_is_reported_as_truncation(self):
        inputs, observer = many_tracklets()
        res = link_moving(inputs, observer, with_cap(100))
        self.assertTrue(res.truncated)
        self.assertEqual(res.n_evaluations, 100)
        self.assertTrue(any("some chains were not evaluated" in n for n in res.notes))

    def test_linking_a_true_chain_is_unchanged_by_the_bound(self):
        full = link_moving(*many_tracklets(), EngineConfig())
        again = link_moving(*many_tracklets(), with_cap(10 ** 6))
        self.assertEqual([t.det_ids for t in full.tracks], [t.det_ids for t in again.tracks])


class ValidatorTruncationTests(unittest.TestCase):
    def test_truncated_search_is_not_a_recovery_measurement(self):
        """Pipeline on a synthetic scene with an evaluation bound of 0: every linking search is truncated."""
        tmp = Path(tempfile.mkdtemp())
        try:
            gen = generated()
            sc = small_scenario()
            cfg = EngineConfig()
            cfg = dataclasses.replace(cfg, planetx=dataclasses.replace(cfg.planetx, max_link_evaluations=0),
                                      validation=dataclasses.replace(cfg.validation, moving_object_experiments=1))
            engine = Engine(settings=load_settings(environ={"HEIMDALL_STATE_DIR": str(tmp)}), config=cfg)
            boom = mock.Mock(side_effect=RuntimeError("stubbed: not under test"))
            names = ("self_check_run", "injection_recovery", "null_detection", "null_variability", "null_motion", "noise_summary",
                     "psf_mismatch", "robustness", "reproducibility")
            with contextlib.ExitStack() as stack:
                for n in names:
                    stack.enter_context(mock.patch.object(validation, n, boom))
                result, _ = engine.investigate(InvestigationRequest(
                    ra_deg=sc.ra0, dec_deg=sc.dec0, radius_deg=0.11, source_id="local-fits", local_root=str(gen.directory),
                    run_validation=True))
            run = next(v for v in result["validation"]["runs"] if v["procedure"] == "moving_object_injection")
            self.assertEqual(run["outcome"], "NOT_ASSESSABLE")
            self.assertIn("LINKING_SEARCH_TRUNCATED", run["reasons"])
            self.assertTrue(run["metrics"]["truncated_searches"])
            self.assertNotIn("n_recovered", run["metrics"])
            self.assertNotEqual(result["planet_x"]["region"]["status"], "COMPLETENESS_LIMITED")
            self.assertTrue(result["linking"]["truncated"])
            self.assertTrue(any("truncated" in w for w in result["warnings"]))
            linking_stage = next(s for s in result["stages"] if s["stage"] == "transients_and_linking")
            self.assertTrue(linking_stage["partial"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
