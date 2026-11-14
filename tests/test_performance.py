"""Performance and memory regression bounds on representative workloads (generous limits; SYNTHETIC inputs)."""

import tempfile
import time
import tracemalloc
import unittest
from pathlib import Path

import numpy as np

from heimdall.engine.association import associate
from heimdall.engine.config import AssociationParams, EngineConfig
from heimdall.engine.detection import detect, prepare
from heimdall.ingest.spherex import read_spherex_mef
from heimdall.skymap.index import SkyIndex
from heimdall.synthetic import Scenario, generate
from tests.helpers import make_det, synthetic_provenance


class PerformanceTests(unittest.TestCase):
    def test_detection_memory_and_time_on_a_crowded_256px_image(self):
        tmp = Path(tempfile.mkdtemp())
        gen = generate(tmp, Scenario(npix=256, n_stars=300, detectors=(2,), epochs_mjd=(61000.0,), visits_per_epoch=1,
                                     min_source_separation_px=3.0))
        cfg = EngineConfig()
        img = read_spherex_mef(gen.files[0], source_id="local-fits", provenance=synthetic_provenance(), config=cfg).image
        tracemalloc.start()
        t = time.perf_counter()
        run = detect(prepare(img, cfg), cfg)
        elapsed = time.perf_counter() - t
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.assertGreater(len(run.accepted), 250)
        self.assertLess(peak / 1e6, 150.0, f"peak {peak / 1e6:.1f} MB")
        self.assertLess(elapsed / len(run.accepted), 0.05, f"{elapsed:.2f} s for {len(run.accepted)} detections")

    def test_association_scales_to_ten_thousand_detections(self):
        rng = np.random.default_rng(0)
        ra, dec = rng.uniform(150, 151, 500), rng.uniform(2, 3, 500)
        dets = [make_det(f"o{k}:{i}", f"o{k}", ra[i] + rng.normal(0, 0.2) / 3600, dec[i] + rng.normal(0, 0.2) / 3600)
                for k in range(20) for i in range(500)]
        t = time.perf_counter()
        clusters = associate(dets, AssociationParams())
        self.assertLess(time.perf_counter() - t, 15.0)
        self.assertLess(abs(len(clusters) - 500), 25)

    def test_sky_index_queries(self):
        rng = np.random.default_rng(1)
        items = [{"ra": float(a), "dec": float(b)} for a, b in zip(rng.uniform(0, 360, 100_000), rng.uniform(-90, 90, 100_000))]
        t = time.perf_counter()
        idx = SkyIndex(items)
        for _ in range(1000):
            idx.cone(float(rng.uniform(0, 360)), float(rng.uniform(-60, 60)), 1.0)
        self.assertLess(time.perf_counter() - t, 5.0)


if __name__ == "__main__":
    unittest.main()
