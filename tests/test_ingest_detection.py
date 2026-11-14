"""Ingestion, quality control, detection and photometry on SYNTHETIC SPHEREx-layout products."""

import math
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np
from astropy.io import fits

from heimdall.contracts import DataOrigin
from heimdall.engine.config import EngineConfig
from heimdall.engine.detection import add_point_sources, detect, fit_psf, forced_photometry, prepare
from heimdall.engine.skygeom import angular_separation_deg
from heimdall.ingest.flags import FlagVocabulary
from heimdall.ingest.spherex import read_spherex_mef
from heimdall.synthetic import FLAG_BITS
from tests.helpers import generated, synthetic_provenance

CFG = EngineConfig()


def ingest(path, **kw):
    return read_spherex_mef(path, source_id="local-fits", provenance=synthetic_provenance(), config=CFG, **kw)


class IngestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = generated().files
        cls.tmp = Path(tempfile.mkdtemp())

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def variant(self, name, edit):
        path = self.tmp / f"{name}.fits"
        shutil.copy(self.files[0], path)
        with fits.open(path, mode="update") as h:
            edit(h)
        return path

    def test_synthetic_product_parses_with_every_check(self):
        r = ingest(self.files[0])
        self.assertFalse(r.qc.excluded)
        states = {i.check: i.state for i in r.qc.items}
        for check in ("required_hdus", "image_unit", "variance_unit", "flag_vocabulary", "celestial_wcs", "spectral_wcs",
                      "observation_time", "detector", "fine_astrometry", "l2_dqa", "psf_model"):
            self.assertEqual(states[check], "PASSED", check)
        img = r.image
        self.assertTrue(img.meta.synthetic)
        self.assertEqual(img.psf.kind, "EPSF_EMBEDDED")
        lam, bw, src = img.wavelength_at(47.5, 47.5)
        self.assertEqual(src, "WCS-WAVE")
        self.assertAlmostEqual(lam, 0.80, delta=0.01)
        self.assertAlmostEqual(bw, lam / 41.0, delta=1e-4)
        self.assertEqual(r.observation.provenance.origin, DataOrigin.SYNTHETIC_TEST_FIXTURE)

    def test_missing_hdu_excludes_with_reason(self):
        path = self.tmp / "nohdu.fits"
        with fits.open(self.files[0]) as h:
            fits.HDUList([x.copy() for x in h if x.name != "VARIANCE"]).writeto(path, overwrite=True)
        r = ingest(path)
        self.assertTrue(r.qc.excluded)
        self.assertIn("REQUIRED_HDU_MISSING", r.qc.exclusion_reasons)
        self.assertIsNone(r.image)

    def test_unexpected_unit_excludes(self):
        r = ingest(self.variant("unit", lambda h: h["IMAGE"].header.__setitem__("BUNIT", "DN")))
        self.assertIn("IMAGE_UNIT_UNEXPECTED", r.qc.exclusion_reasons)

    def test_flags_without_vocabulary_are_not_interpreted(self):
        def strip(h):
            for key in [k for k in h["FLAGS"].header.keys() if k.startswith("MP_")]:
                del h["FLAGS"].header[key]
        r = ingest(self.variant("noflags", strip))
        self.assertIn("FLAG_SEMANTICS_NOT_ESTABLISHED", r.qc.exclusion_reasons)

    def test_synthetic_product_with_real_provenance_is_refused(self):
        from datetime import UTC, datetime
        from heimdall.contracts import Provenance
        real = Provenance(origin=DataOrigin.CACHED, retrieved_at=datetime.now(UTC), software_version="t")
        r = read_spherex_mef(self.files[0], source_id="x", provenance=real, config=CFG)
        self.assertIn("SYNTHETIC_PROVENANCE_MISMATCH", r.qc.exclusion_reasons)

    def test_unsupported_time_scale_marks_temporal_unsuitable(self):
        r = ingest(self.variant("tt", lambda h: h["IMAGE"].header.__setitem__("TIMESYS", "TT")))
        self.assertFalse(r.qc.excluded)
        self.assertFalse(r.qc.suitability["temporal"])
        self.assertIn("TIME_SCALE_NOT_SUPPORTED", r.qc.suitability_reasons["temporal"])

    def test_failed_dqa_excludes_and_bad_finast_limits_astrometry(self):
        r = ingest(self.variant("dqa", lambda h: h["IMAGE"].header.__setitem__("L2DQAFLG", "Fail")))
        self.assertIn("L2_DQA_NOT_PASSED", r.qc.exclusion_reasons)
        r = ingest(self.variant("finast", lambda h: h["IMAGE"].header.__setitem__("FINAST", 1)))
        self.assertFalse(r.qc.excluded)
        self.assertFalse(r.qc.suitability["astrometry"])

    def test_unreadable_file(self):
        bad = self.tmp / "bad.fits"
        bad.write_bytes(b"not a fits file")
        r = ingest(bad)
        self.assertIn("PRODUCT_UNREADABLE", r.qc.exclusion_reasons)

    def test_crop_keeps_image_coordinates(self):
        full = ingest(self.files[0]).image
        r = ingest(self.files[0], crop=(*[float(v) for v in full.pixel_to_world(30.0, 40.0)], 0.01))
        img = r.image
        self.assertNotEqual(img.origin, (0, 0))
        ra, dec = img.pixel_to_world(30.0, 40.0)
        ra0, dec0 = full.pixel_to_world(30.0, 40.0)
        self.assertLess(float(angular_separation_deg(ra, dec, ra0, dec0)), 1e-12)
        self.assertLess(img.image.size, full.image.size)


class DetectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gen = generated()
        cls.img = ingest(cls.gen.files[0]).image
        cls.an = prepare(cls.img, CFG)
        cls.drun = detect(cls.an, CFG)

    def test_recovers_injected_stars_with_calibrated_flux(self):
        lam = self.img.wavelength_at(47.5, 47.5)[0]
        pulls, found = [], 0
        for s in self.gen.truth["stars"]:
            if not self.img.contains(s["ra"], s["dec"], margin_px=4):
                continue
            best = min(self.drun.accepted, key=lambda d: angular_separation_deg(d.ra, d.dec, s["ra"], s["dec"]))
            if angular_separation_deg(best.ra, best.dec, s["ra"], s["dec"]) * 3600 < 2.0:
                found += 1
                true = s["flux_ujy"] * best.wavelength_um ** s["spectral_index"]
                pulls.append((best.flux_ujy - true) / best.flux_sigma_ujy)
        self.assertGreater(found, 20)
        self.assertLess(abs(np.median(pulls)), 1.0)
        self.assertLess(np.std(pulls), 2.0)
        self.assertAlmostEqual(lam, 0.80, delta=0.02)

    def test_noise_diagnostics_are_near_unity(self):
        self.assertAlmostEqual(self.drun.noise["pixel_ratio"], 1.0, delta=0.1)
        self.assertEqual(self.drun.status, "COMPLETED")

    def test_inverted_image_has_no_bright_false_detections(self):
        neg = detect(self.an, CFG, invert=True)
        self.assertLessEqual(len(neg.accepted), 1)

    def test_injected_source_position_and_flux(self):
        x, y, snr = 40.3, 55.7, 30.0
        sigma_a = math.sqrt(1.0 / fit_psf(self.an, x, y, CFG.detection, fixed=True).sum_wp2)
        new = prepare(self.img.with_image(add_point_sources(self.img, [(x, y, snr * sigma_a)])), CFG)
        rec = min(detect(new, CFG).accepted, key=lambda d: math.hypot(d.x - x, d.y - y))
        self.assertLess(math.hypot(rec.x - x, rec.y - y), 0.2)
        self.assertAlmostEqual(rec.amplitude / (snr * sigma_a), 1.0, delta=0.15)
        self.assertEqual(rec.fit_status, "CONVERGED")

    def test_flagged_pixels_are_masked_and_reported(self):
        x, y = 30.0, 30.0
        sigma_a = math.sqrt(1.0 / fit_psf(self.an, x, y, CFG.detection, fixed=True).sum_wp2)
        image = add_point_sources(self.img, [(x, y, 50 * sigma_a)])
        flags = self.img.flags.copy()
        flags[30, 30] |= 1 << FLAG_BITS["OVERFLOW"]
        flagged = self.img.with_image(image)
        flagged.flags = flags
        rec = min(detect(prepare(flagged, CFG), CFG).accepted, key=lambda d: math.hypot(d.x - x, d.y - y))
        self.assertIn("OVERFLOW", rec.peak_flags)
        self.assertEqual(rec.quality_grade, "D")
        hot = flags.copy()
        hot[28:33, 28:33] |= 1 << FLAG_BITS["HOT"]
        masked = self.img.with_image(image)
        masked.flags = hot
        an = prepare(masked, CFG)
        self.assertTrue(an.bad[28:33, 28:33].all())
        self.assertGreater(forced_photometry(an, *[float(v) for v in self.img.pixel_to_world(x, y)], CFG).masked_fraction, 0.5)

    def test_forced_photometry_outside_image_is_not_covered(self):
        out = forced_photometry(self.an, (self.gen.truth["scenario"]["ra0"] + 5.0) % 360, 0.0, CFG)
        self.assertEqual(out.status, "NOT_COVERED")

    def test_detection_is_deterministic(self):
        again = detect(prepare(self.img, CFG), CFG)
        self.assertEqual([(d.det_id, d.x, d.y, d.flux_ujy) for d in again.accepted],
                         [(d.det_id, d.x, d.y, d.flux_ujy) for d in self.drun.accepted])


class FlagVocabularyTests(unittest.TestCase):
    """Both header conventions seen on real IRSA products (card layout copied, values are test inputs)."""

    def test_qr2_mp_convention(self):
        h = fits.Header()
        h["HIERARCH MP_TRANSIENT"] = (0, "Transient detected during SUR")
        h["HIERARCH MP_OUTLIER"] = (19, "Pixel flagged by Detect Outliers")
        v = FlagVocabulary.from_header(h)
        self.assertEqual((v.bits, v.convention), ({"TRANSIENT": 0, "OUTLIER": 19}, "MP_<NAME>"))
        self.assertEqual(v.names_in((1 << 19) | 1), ("OUTLIER", "TRANSIENT"))

    def test_qr3_mskn_convention_and_inconsistent_planes(self):
        h = fits.Header()
        for n, name in ((0, "TRANSIENT"), (19, "OUTLIER"), (22, "CROSSTALK")):
            h[f"MSKN{n:04d}"] = name
            h[f"MSKM{n:04d}"] = 1 << n
            h[f"MSKD{n:04d}"] = f"description of {name}"
        h["MSKN0030"] = "BROKEN"
        h["MSKM0030"] = 5  # not 1 << 30: must be ignored and reported
        v = FlagVocabulary.from_header(h)
        self.assertEqual(v.bits, {"TRANSIENT": 0, "OUTLIER": 19, "CROSSTALK": 22})
        self.assertEqual(v.convention, "MSKN/MSKM")
        self.assertEqual(v.descriptions["CROSSTALK"], "description of CROSSTALK")
        self.assertEqual(len(v.inconsistent), 1)
        self.assertFalse(FlagVocabulary.from_header(fits.Header()).defined)


if __name__ == "__main__":
    unittest.main()
