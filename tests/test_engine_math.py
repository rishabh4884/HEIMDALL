"""Numerical checks of the engine's statistics, geometry, projections, PSF models, ephemeris and motion fits.

Reference values are independent: closed forms, scipy, astropy, or exact vector geometry.
"""

import math
import unittest

import numpy as np
from astropy import units as u
from astropy.coordinates import SkyCoord
from scipy import stats as st

from heimdall.engine import stats
from heimdall.engine.background import estimate_background
from heimdall.engine.config import DetectionParams
from heimdall.engine.ephemeris import observer_heliocentric_au
from heimdall.engine.motion import motion_test, parallax_fit
from heimdall.engine.psf import EpsfModel, GaussianPsfModel, LegacyPsfModel
from heimdall.engine.skygeom import (angular_separation_deg, covariance_ellipse, gnomonic, inverse_gnomonic,
                                     is_positive_definite, offsets_arcsec, polygon_contains, radec_from_vector, unit_vector)
from heimdall.skymap.index import SkyIndex, tile_of
from heimdall.skymap.projection import inverse_mollweide, mollweide


class StatsTests(unittest.TestCase):
    def test_chi2_logsf_matches_scipy(self):
        for k in (1, 2, 3, 7, 40):
            for x in (0.01, 0.7, 4.0, 15.0, 90.0):
                self.assertAlmostEqual(stats.chi2_logsf(x, k), float(st.chi2.logsf(x, k)), places=9)

    def test_chi2_logsf_dof2_closed_form_in_extreme_tail(self):
        # Q(chi2 = x, dof = 2) = exp(-x/2) exactly; p = exp(-5000) underflows a double, the log does not.
        self.assertAlmostEqual(stats.chi2_logsf(10000.0, 2), -5000.0, places=8)

    def test_two_sided_sigma_conventions(self):
        self.assertAlmostEqual(stats.chi2_significance(9.0, 1).sigma_equivalent, 3.0, places=9)
        self.assertAlmostEqual(stats.chi2_significance(25.0, 1).sigma_equivalent, 5.0, places=9)
        sig = stats.chi2_significance(4000.0, 1)  # Z = sqrt(4000) without underflow
        self.assertAlmostEqual(sig.sigma_equivalent, math.sqrt(4000.0), places=6)
        self.assertEqual(stats.chi2_significance(0.0, 3).sigma_equivalent, 0.0)

    def test_invalid_inputs_raise(self):
        with self.assertRaises(ValueError):
            stats.chi2_logsf(-1.0, 2)
        with self.assertRaises(ValueError):
            stats.chi2_logsf(1.0, 0)

    def test_bonferroni(self):
        base = stats.chi2_significance(25.0, 1)
        corrected = stats.bonferroni(base, 10)
        self.assertAlmostEqual(corrected.p_value, 10 * base.p_value, places=15)
        self.assertLess(corrected.sigma_equivalent, base.sigma_equivalent)
        self.assertEqual(stats.bonferroni(stats.chi2_significance(0.1, 1), 1000).p_value, 1.0)

    def test_intervals_and_weighted_mean(self):
        lo, hi = stats.wilson_interval(0, 10)
        self.assertEqual(lo, 0.0)
        self.assertAlmostEqual(hi, 0.2775, places=3)  # standard Wilson value
        lo, hi = stats.poisson_interval(0)
        self.assertAlmostEqual(hi, 3.6889, places=3)  # Garwood 95% upper limit for zero counts
        wm = stats.weighted_mean(np.array([1.0, 3.0]), np.array([1.0, 1.0]))
        self.assertAlmostEqual(wm.mean, 2.0)
        self.assertAlmostEqual(wm.sigma, 1 / math.sqrt(2))
        self.assertAlmostEqual(wm.chi2, 2.0)
        with self.assertRaises(ValueError):
            stats.weighted_mean(np.array([1.0]), np.array([0.0]))

    def test_sigma_clipping_resists_outliers(self):
        rng = np.random.default_rng(1)
        data = np.concatenate([rng.normal(5.0, 1.0, 5000), np.full(50, 100.0)])
        med, std, n = stats.sigma_clipped_stats(data)
        self.assertAlmostEqual(med, 5.0, delta=0.05)
        self.assertAlmostEqual(std, 1.0, delta=0.05)
        self.assertLess(n, data.size)

    def test_ks_uniform_detects_non_uniform_pvalues(self):
        rng = np.random.default_rng(2)
        self.assertGreater(stats.ks_uniform(rng.uniform(size=500))[1], 0.01)
        self.assertLess(stats.ks_uniform(rng.uniform(size=500) ** 3)[1], 1e-6)


class GeometryTests(unittest.TestCase):
    def test_separation_matches_astropy(self):
        rng = np.random.default_rng(3)
        ra1, ra2 = rng.uniform(0, 360, 50), rng.uniform(0, 360, 50)
        de1, de2 = rng.uniform(-89, 89, 50), rng.uniform(-89, 89, 50)
        ours = angular_separation_deg(ra1, de1, ra2, de2)
        ref = SkyCoord(ra1 * u.deg, de1 * u.deg).separation(SkyCoord(ra2 * u.deg, de2 * u.deg)).deg
        self.assertLess(np.max(np.abs(ours - ref)), 1e-10)

    def test_gnomonic_round_trip_and_ra_wrap(self):
        for ra0, dec0 in ((359.9, 0.0), (0.05, 45.0), (180.0, -89.5)):
            ra, dec = ra0 + 0.2, dec0 + 0.1 if dec0 < 89 else dec0
            xi, eta = gnomonic(ra % 360, dec, ra0, dec0)
            ra_b, dec_b = inverse_gnomonic(xi, eta, ra0, dec0)
            self.assertLess(float(angular_separation_deg(ra_b, dec_b, ra % 360, dec)) * 3600, 1e-6)

    def test_offsets_orientation(self):
        east = offsets_arcsec(10.0 + 1 / 3600 / math.cos(math.radians(30)), 30.0, 10.0, 30.0)
        north = offsets_arcsec(10.0, 30.0 + 1 / 3600, 10.0, 30.0)
        self.assertAlmostEqual(float(east[0]), 1.0, places=4)
        self.assertAlmostEqual(float(north[1]), 1.0, places=4)
        self.assertAlmostEqual(float(east[1]), 0.0, places=4)

    def test_covariance_ellipse_and_positive_definiteness(self):
        a, b, pa = covariance_ellipse(np.array([[1.0, 0.0], [0.0, 4.0]]))
        self.assertAlmostEqual(a, 2.0)
        self.assertAlmostEqual(b, 1.0)
        self.assertAlmostEqual(pa % 180, 0.0, places=6)  # major axis along north
        self.assertTrue(is_positive_definite([[1, 0.5], [0.5, 1]]))
        self.assertFalse(is_positive_definite([[1, 2], [2, 1]]))
        self.assertFalse(is_positive_definite([[1, 0], [0, float("nan")]]))

    def test_polygon_contains(self):
        ra = [10.0, 10.2, 10.2, 10.0]
        dec = [20.0, 20.0, 20.2, 20.2]
        self.assertTrue(polygon_contains(ra, dec, 10.1, 20.1))
        self.assertFalse(polygon_contains(ra, dec, 10.3, 20.1))

    def test_mollweide_round_trip_and_bounds(self):
        lon = np.array([0.0, 90.0, 179.0, 270.0, 30.0])
        lat = np.array([0.0, 45.0, -60.0, 89.0, -89.9])
        x, y = mollweide(lon, lat)
        self.assertTrue(np.all(np.abs(x) <= 2 * math.sqrt(2) + 1e-12))
        lon2, lat2 = inverse_mollweide(x, y)
        self.assertLess(np.max(np.abs(lat2 - lat)), 1e-8)
        self.assertLess(np.max(np.abs(((lon2 - lon + 180) % 360) - 180)[:4]), 1e-6)

    def test_sky_index_cone_and_tiles(self):
        rng = np.random.default_rng(4)
        items = [{"ra": float(r), "dec": float(d)} for r, d in zip(rng.uniform(149.9, 150.1, 500), rng.uniform(1.9, 2.1, 500))]
        idx = SkyIndex(items)
        hits = idx.cone(150.0, 2.0, 0.05)
        brute = [i for i, it in enumerate(items) if angular_separation_deg(it["ra"], it["dec"], 150.0, 2.0) <= 0.05]
        self.assertEqual(hits, brute)
        view = idx.viewport(150.0, 2.0, 0.2, max_items=10)
        self.assertEqual(view["mode"], "tiles")
        self.assertEqual(sum(t["count"] for t in view["tiles"]), view["count"])
        self.assertEqual(tile_of(150.0, 2.0, 5)[0], tile_of(150.0001, 2.0001, 5)[0])


class PsfAndBackgroundTests(unittest.TestCase):
    def test_gaussian_psf_is_normalised_and_centred(self):
        self.assertAlmostEqual(GaussianPsfModel(0.6, "test").render(0.25, -0.1, -15, -15, 31, 31).sum(), 1.0, places=10)
        # Centroid check with a well-sampled PSF: pixel-sampling aliasing scales as exp(-2 pi^2 sigma^2).
        stamp = GaussianPsfModel(1.5, "test").render(0.25, -0.1, -15, -15, 31, 31)
        ys, xs = np.mgrid[-15:16, -15:16]
        self.assertAlmostEqual(float((stamp * xs).sum()), 0.25, places=8)
        self.assertAlmostEqual(float((stamp * ys).sum()), -0.1, places=8)

    def test_epsf_sampling_reproduces_a_known_effective_psf(self):
        from heimdall.synthetic import _epsf_array
        sigma = 0.5
        arr = _epsf_array(sigma)
        model = EpsfModel(arr[None], np.array([[0.0, 0.0]]), (5, 5), (0.0, 0.0), "TEST", "test")
        direct = GaussianPsfModel(sigma, "test")
        a = model.render(0.4, 0.2, -5, -5, 11, 11)
        b = direct.render(0.4, 0.2, -5, -5, 11, 11)
        self.assertAlmostEqual(a.sum(), 1.0, places=6)
        self.assertLess(np.max(np.abs(a - b)), 2e-3)

    def test_legacy_psf_integration_is_continuous_in_position(self):
        fine = GaussianPsfModel(0.5 * 10, "fine").render(0.0, 0.0, -50, -50, 101, 101)
        model = LegacyPsfModel(fine[None], np.array([[0.0, 0.0]]), 10, (0.0, 0.0), "test")
        s1 = model.render(0.30, 0.0, -5, -5, 11, 11)
        s2 = model.render(0.31, 0.0, -5, -5, 11, 11)
        self.assertAlmostEqual(s1.sum(), 1.0, places=3)
        self.assertLess(np.max(np.abs(s1 - s2)), 0.01)

    def test_background_constant_and_mesh(self):
        rng = np.random.default_rng(5)
        params = DetectionParams()
        data = 0.3 + rng.normal(0, 0.02, (40, 40))
        bg = estimate_background(data, np.zeros_like(data, bool), np.zeros_like(data, bool), params)
        self.assertEqual(bg.mesh_shape, (1, 1))
        self.assertAlmostEqual(float(bg.level.mean()), 0.3, delta=0.003)
        yy = np.arange(128)[:, None] * np.ones((1, 128))
        ramp = 0.3 + 0.001 * yy + rng.normal(0, 0.01, (128, 128))
        bg = estimate_background(ramp, np.zeros_like(ramp, bool), np.zeros_like(ramp, bool), params)
        self.assertEqual(bg.mesh_shape, (4, 4))
        self.assertLess(float(np.abs(bg.level - (0.3 + 0.001 * yy))[16:-16].max()), 0.01)


class EphemerisAndMotionTests(unittest.TestCase):
    def test_observer_is_about_one_au_from_the_sun(self):
        r = np.linalg.norm(observer_heliocentric_au(np.linspace(61000, 61365, 13)), axis=1)
        self.assertTrue(np.all((r > 0.98) & (r < 1.02)))

    def test_parallax_fit_recovers_distance_from_exact_geometry(self):
        mjd = np.concatenate([61000 + np.arange(3) * 0.5, 61182.6 + np.arange(3) * 0.5, 61365.3 + np.arange(3) * 0.5])
        obs = observer_heliocentric_au(mjd)
        for r_au in (150.0, 800.0):
            body = r_au * unit_vector(40.0, 15.0)
            ra, dec = radec_from_vector(body - obs)
            fit = parallax_fit(ra, dec, np.repeat((np.eye(2) * 0.01)[None], 9, axis=0), mjd, obs)
            self.assertEqual(fit.status, "COMPLETED")
            # The five-parameter model is linear in 1/r; its truncation error is of order |R|/r.
            self.assertLess(abs(fit.distance_au - r_au) / r_au, 1.0 / r_au)

    def test_parallax_fit_minimum_data(self):
        fit = parallax_fit([1.0, 1.0], [2.0, 2.0], np.repeat(np.eye(2)[None], 2, axis=0), [61000, 61100], np.zeros((2, 3)))
        self.assertEqual(fit.status, "INSUFFICIENT_INPUTS")
        self.assertIn("FEWER_THAN_3_POSITIONS", fit.reasons)

    def test_motion_test_stationary_and_moving(self):
        rng = np.random.default_rng(6)
        mjd = np.linspace(61000, 61365, 8)
        cov = np.repeat((np.eye(2) * 0.1 ** 2)[None], 8, axis=0)
        noise = rng.normal(0, 0.1, (8, 2)) / 3600
        still = motion_test(150 + noise[:, 0], 2 + noise[:, 1], cov, mjd)
        self.assertEqual(still.status, "COMPLETED")
        self.assertLess(still.gof_stationary.sigma_equivalent, 4)
        mu = 2.0 / 3600 / 365.25  # 2 arcsec/yr north
        moving = motion_test(150 + noise[:, 0], 2 + noise[:, 1] + mu * (mjd - mjd[0]), cov, mjd)
        self.assertGreater(moving.motion.sigma_equivalent, 10)
        self.assertAlmostEqual(moving.mu_arcsec_per_yr[1], 2.0, delta=0.3)

    def test_astrometric_error_model_measures_scale_and_image_offsets(self):
        from heimdall.engine.motion import astrometric_error_model
        from tests.helpers import make_det
        rng = np.random.default_rng(9)
        assumed, true_sigma, floor = 0.2, 0.4, 0.0  # true scatter is twice the assumed error
        offsets = {f"o{k}": (0.0, 0.0) for k in range(12)}
        offsets["o3"] = (1.0, 0.0)  # one image registered 1 arcsec off in RA
        groups = []
        for s in range(40):
            ra0, dec0 = 150.0 + rng.uniform(-0.05, 0.05), 2.0 + rng.uniform(-0.05, 0.05)
            dets = []
            for k in range(12):
                dx, dy = rng.normal(0, true_sigma, 2) + np.array(offsets[f"o{k}"])
                dets.append(make_det(f"o{k}:{s}", f"o{k}", ra0 + dx / 3600 / math.cos(math.radians(dec0)), dec0 + dy / 3600,
                                     sigma_arcsec=assumed))
            groups.append(dets)
        model = astrometric_error_model(groups, floor)
        self.assertEqual(model.status, "COMPLETED")
        self.assertGreater(model.scale, 2.0)  # includes the registration offset of o3
        o3 = next(o for o in model.image_offsets if o["obs_id"] == "o3")
        self.assertGreater(o3["mean_arcsec"][0], 0.7)
        self.assertGreater(o3["significance"].sigma_equivalent, 5)
        clean = astrometric_error_model([[d for d in g if d.obs_id != "o3"] for g in groups], floor)
        self.assertAlmostEqual(clean.scale, 2.0, delta=0.15)
        self.assertEqual(astrometric_error_model(groups[:3], floor).status, "INSUFFICIENT_INPUTS")

    def test_motion_test_needs_a_time_baseline(self):
        out = motion_test([1, 1.00001], [2, 2], np.repeat(np.eye(2)[None], 2, axis=0), [61000, 61000])
        self.assertEqual(out.status, "INSUFFICIENT_INPUTS")
        self.assertIn("NO_TIME_BASELINE", out.reasons)


if __name__ == "__main__":
    unittest.main()
