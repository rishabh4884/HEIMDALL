"""Public DR1 image URLs and strictly local offline fallbacks."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from heimdall.hips import HiPSCache, ROOT, location


class HiPSTests(unittest.TestCase):
    def test_url_resolution_matches_published_cube_layout(self):
        self.assertEqual(location("/sky/spherex/allsky/0.png"), ("Norder3/Allsky.png", "image/png"))
        self.assertEqual(location("/sky/spherex/allsky/1.png"), ("Norder3/Allsky_1.png", "image/png"))
        self.assertEqual(location("/sky/spherex/5/11166/1.png"), ("Norder5/Dir10000/Npix11166_1.png", "image/png"))
        for path in ("/sky/spherex/allsky/102.png", "/sky/spherex/5/12288/0.png", "/sky/spherex/6/0/1.png", "/sky/spherex/../../etc/passwd"):
            self.assertIsNone(location(path))

    def test_offline_only_returns_the_exact_cached_plane(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = HiPSCache(Path(tmp), offline=True)
            sample = service.root / "Norder3/Allsky_1.png"
            sample.parent.mkdir(parents=True)
            data = b"\x89PNG\r\n\x1a\n" + b"\0\0\0\rIHDR" + b"\0\0\0\x01" * 2 + b"\0" * 12
            sample.write_bytes(data)
            with patch.object(service.opener, "open") as network:
                self.assertEqual(service.get("/sky/spherex/allsky/1.png"), (200, data, "image/png"))
                self.assertEqual(service.get("/sky/spherex/allsky/0.png")[0], 404)
                network.assert_not_called()

    def test_upstream_validated_and_cached(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, _limit):
                return b"obs_title = SPHEREx DR1\ndata_cube_crval3_arr = 0.753"

        with tempfile.TemporaryDirectory() as tmp:
            service = HiPSCache(Path(tmp), offline=False)
            path = "/sky/spherex/properties"
            with patch.object(service.opener, "open", return_value=Response()) as network:
                self.assertEqual(service.get(path)[0], 200)
                self.assertEqual(service.get(path)[0], 200)
                network.assert_called_once_with(ROOT + "/properties", timeout=6)
