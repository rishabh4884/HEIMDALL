"""IAU constellation boundaries (CDS VI/49): ordered B1875 outlines, edge densification, precession, validation and caching."""

import gzip
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from heimdall.constellations import BoundaryError, boundaries, build, check_against_bound20, densify, parse_constbnd

CRUX = ["11.83333 -55.00000 Cru     ", "12.83333 -55.00000 Cru  Cen", "12.83333 -64.00000 Cru  Cen", "11.83333 -64.00000 Cru  Mus",
        "11.83333 -55.00000 Cru  Cen"]  # the worked example in the catalogue's constbnd.txt


def fixture(n=88):
    """Crux plus synthetic rectangles in the published layout (format test only; not real boundaries)."""
    rows = list(CRUX)
    for c in range(1, n):
        ra, dec = (c * 0.25) % 24, -80 + c * 1.7
        name = f"C{c:02d}"
        rows += [f"{ra:8.5f} {dec:+09.5f} {name:<4}    ", f"{ra + .2:8.5f} {dec:+09.5f} {name:<4} Xxx", f"{ra + .2:8.5f} {dec + 1:+09.5f} {name:<4} Xxx",
                 f"{ra:8.5f} {dec + 1:+09.5f} {name:<4} Xxx", f"{ra:8.5f} {dec:+09.5f} {name:<4} Xxx"]
    return "\n".join(rows) + "\n"


class ConstellationTests(unittest.TestCase):
    def test_parses_ordered_closed_outlines(self):
        out = parse_constbnd(fixture())
        self.assertEqual(len(out), 88)
        self.assertEqual(out[0]["cst"], "Cru")
        self.assertEqual((out[0]["ra1875"][0], out[0]["dec1875"][0]), (out[0]["ra1875"][-1], out[0]["dec1875"][-1]))
        with self.assertRaises(BoundaryError):
            parse_constbnd(fixture(40))

    def test_edges_follow_constant_ra_or_dec_and_cross_the_ra_seam_the_short_way(self):
        ra, dec = densify([359.0, 1.0], [10.0, 10.0])
        self.assertTrue(all(d == 10.0 for d in dec))
        self.assertTrue(all(r >= 359.0 or r <= 1.0 for r in ra))
        ra, dec = densify([20.0, 20.0], [0.0, 3.0])
        self.assertEqual(len(dec), 7)
        self.assertTrue(all(r == 20.0 for r in ra))

    def test_precessed_crux_matches_its_published_j2000_vertices(self):
        crux = build(fixture())[0]
        # The four original (type O) J2000 vertices of Crux, verbatim from CDS VI/49 bound_20.dat (2024-07-23 revision).
        b20 = "\n".join(["194.4383810 -64.6769638 CRU  O", "194.3345110 -55.6771049 CRU  O", "179.0707680 -55.6957932 CRU  O",
                          "179.0573640 -64.6957855 CRU  O"])
        check = check_against_bound20([crux], b20)
        self.assertEqual(check["vertices_checked"], 4)
        self.assertLess(check["max_deviation_deg"], 0.001)  # ~3.6 arcsec

    def test_mirror_fallback_cache_and_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            def transport(method, url, data, headers, timeout):
                if "cdsarc" in url:
                    raise TimeoutError
                if url.endswith("bound_20.dat.gz"):
                    return SimpleNamespace(status=404, body=b"", headers={})
                return SimpleNamespace(status=200, body=fixture().encode(), headers={})
            doc = boundaries(Path(tmp), offline=False, transport=transport)
            self.assertIn("vizier.cfa.harvard.edu", doc["source_url"])
            self.assertIn("Davenhall & Leggett", doc["attribution"])
            self.assertIn("not performed", doc["validation"]["note"])
            self.assertTrue(boundaries(Path(tmp), offline=True, transport=lambda *a: self.fail("cached copy must be used"))["cached"])
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(BoundaryError) as e:
                boundaries(Path(tmp), offline=True, transport=lambda *a: self.fail("offline must not fetch"))
            self.assertEqual(e.exception.kind, "OFFLINE")


if __name__ == "__main__":
    unittest.main()
