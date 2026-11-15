"""Co-registered twin-view pair (GET /evidence/pair) on synthetic SPHEREx-format products, and module serving."""

import base64
import hashlib
import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from heimdall.api import Engine
from heimdall.config import load_settings
from heimdall.server import Api, ApiError, make_handler
from heimdall.synthetic import Scenario, generate


class PairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.gen = generate(root / "gen", Scenario(npix=64, detectors=(1, 2), epochs_mjd=(61000.0, 61182.6), visits_per_epoch=1, n_stars=25))
        cache = root / "cache" / "irsa-spherex" / "ab"
        cache.mkdir(parents=True)
        cls.files = {}
        for f in cls.gen.files:
            data = f.read_bytes()
            sha = hashlib.sha256(data).hexdigest()
            (cache / f"{sha}.bin").write_bytes(data)
            (cache / f"{sha}.json").write_text(json.dumps({"sha256": sha}))
            from astropy.io import fits
            with fits.open(f) as h:
                cls.files[sha] = (int(h["IMAGE"].header["DETECTOR"]), float(h["IMAGE"].header.get("MJD-AVG") or 0))
        settings = load_settings(environ={"HEIMDALL_STATE_DIR": str(root / "state"), "HEIMDALL_CACHE_DIR": str(root / "cache"), "HEIMDALL_OFFLINE": "1"})
        cls.api = Api(Engine(settings=settings))
        cls.sc = Scenario()

    @classmethod
    def tearDownClass(cls):
        cls.api.svc.close()
        cls.tmp.cleanup()

    def pair(self, a, b, **extra):
        q = {"a": [a], "b": [b], "ra": ["30.0"], "dec": ["-10.0"], "npix": ["48"], "size_arcsec": ["180"]} | {k: [str(v)] for k, v in extra.items()}
        return self.api.handle("GET", "/api/v1/evidence/pair", q, None)[1]

    def test_same_detector_pair_is_aligned_north_up_and_differenced_with_caveats(self):
        same = [s for s, (det, _) in self.files.items() if det == 1]
        out = self.pair(same[0], same[1])
        self.assertEqual(out["schema"], "heimdall.product_pair/1.0")
        self.assertEqual(out["grid"]["orientation"], "north up, east left")
        self.assertEqual(out["comparability"]["status"], "COMPARABLE_WITH_LIMITATIONS")
        self.assertIn("no PSF matching", out["comparability"]["limitations"])
        for im in out["images"]:
            self.assertEqual(len(base64.b64decode(im["display"]["own"])), 48 * 48)
            self.assertTrue(im["synthetic"])
            self.assertGreater(im["coverage_fraction"], 0.9)
        d = out["difference"]
        self.assertEqual(d["order"], "B minus A")
        self.assertEqual(len(base64.b64decode(d["pixels"])), 48 * 48)
        self.assertIn("not a photometric measurement", d["limitations"][-1])

    def test_aligned_grids_agree_on_star_positions_despite_rotation(self):
        """The two epochs are rotated by 25 degrees on the detector; after resampling, the brightest pixels coincide."""
        import numpy as np
        same = sorted([s for s, (det, _) in self.files.items() if det == 1], key=lambda s: self.files[s][1])
        out = self.pair(same[0], same[1])
        imgs = [np.frombuffer(base64.b64decode(i["display"]["common"]), dtype=np.uint8).reshape(48, 48).astype(float) for i in out["images"]]
        a, b = [(x - x.mean()) for x in imgs]
        corr0 = float((a * b).sum() / np.sqrt((a * a).sum() * (b * b).sum()))
        flipped = float((a * np.fliplr(b)).sum() / np.sqrt((a * a).sum() * (b * b).sum()))
        self.assertGreater(corr0, 0.5)
        self.assertGreater(corr0, flipped + 0.2)

    def test_cross_detector_pair_is_not_comparable_and_has_no_difference(self):
        d1 = next(s for s, (det, _) in self.files.items() if det == 1)
        d2 = next(s for s, (det, _) in self.files.items() if det == 2)
        out = self.pair(d1, d2)
        self.assertEqual(out["comparability"]["status"], "NOT_COMPARABLE")
        self.assertIsNone(out["difference"])
        self.assertTrue(any("different detectors" in r for r in out["comparability"]["reasons"]))

    def test_inputs_are_validated_and_missing_products_refused(self):
        sha = next(iter(self.files))
        for bad in ({"a": "x", "b": sha}, {"a": sha, "b": sha}):
            with self.assertRaises(ApiError) as e:
                self.api.handle("GET", "/api/v1/evidence/pair", {k: [v] for k, v in bad.items()} | {"ra": ["30"], "dec": ["-10"]}, None)
            self.assertEqual(e.exception.status, 400)
        with self.assertRaises(ApiError) as e:
            self.pair(sha, "0" * 64)
        self.assertEqual(e.exception.status, 404)

    def test_every_web_module_is_served_and_nothing_else(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.api, None, None))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        for name in ("workbench.js", "sky.js", "hips.js", "charts.js", "records.js", "workbench.css"):
            with urllib.request.urlopen(f"{base}/{name}", timeout=5) as r:
                self.assertIn("default-src 'self'", r.headers["Content-Security-Policy"])
        for bad in ("/../server.py", "/web_pair.py", "/README.md"):
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(base + bad, timeout=5)
            e.exception.close()


if __name__ == "__main__":
    unittest.main()
