"""Effective-degrees-of-freedom linking criterion: calculation, mixed-chain rejection, legitimate-track preservation, config,
serialization, truncation and validator compatibility (SYNTHETIC geometry; no network)."""

import dataclasses
import json
import math
import unittest

import numpy as np

from heimdall.engine import validation
from heimdall.engine.association import SourceCluster, gate_d2
from heimdall.engine.config import EngineConfig, PlanetXParams
from heimdall.engine.linking import effective_dof, link_moving, rate_scale_arcsec_per_day, tracklet_rate_precision
from heimdall.engine.motion import parallax_fit
from heimdall.engine.planetx import parallax_arcsec
from heimdall.engine.skygeom import radec_from_vector, unit_vector
from tests.helpers import make_det

CFG = EngineConfig()
FLOOR2 = CFG.association.registration_floor_arcsec ** 2
RA0, DEC0 = 150.78, 3.35
SCALE = rate_scale_arcsec_per_day(CFG)
# epochs (mjd of the first image): three early epochs and one half a year later, as in the real field
EPOCH_T0 = {0: 60790.8, 1: 60803.6, 2: 60814.9, 3: 60999.0}
LONG, SHORT = [0.0, 1.0, 2.0, 3.0], [0.0, 0.001]


def earth(t):
    phase, obliquity = 2 * math.pi * t / 365.25, math.radians(23.44)
    return np.array([math.cos(phase), math.sin(phase) * math.cos(obliquity), math.sin(phase) * math.sin(obliquity)])


def apparent(r_au, t, direction=(RA0, DEC0)):
    ra, dec = radec_from_vector((r_au * unit_vector(*direction) - earth(t))[None, :])
    return float(ra[0]), float(dec[0])


class Field:
    """Builds linker inputs and the observer table from true-body or foreign tracklets."""

    def __init__(self):
        self.inputs, self.observer, self.n = [], {}, 0

    def add(self, epoch, offsets, *, r_au=None, foreign=None, sigma=0.1, tag="S", direction=(RA0, DEC0), t0=None):
        dets = []
        for k, dt in enumerate(offsets):
            t = (EPOCH_T0[epoch] if t0 is None else t0) + dt
            if foreign is None:
                ra, dec = apparent(r_au, t, direction)
            else:  # an unrelated source: a fixed offset (arcsec east/north) from the body's epoch position plus a slow drift
                ra0, dec0 = apparent(foreign["r_au"], EPOCH_T0[epoch])
                ra = ra0 + (foreign["east"] + foreign["drift"] * dt) / 3600.0 / math.cos(math.radians(dec0))
                dec = dec0 + foreign["north"] / 3600.0
            obs = f"{tag}{self.n}v{k}"
            self.observer[obs] = earth(t)
            dets.append(make_det(f"{obs}:1", obs, ra, dec, sigma_arcsec=sigma, mjd=t))
        c = SourceCluster(source_id=f"{tag}{self.n}", ra=dets[0].ra, dec=dets[0].dec, cov_arcsec2=[[sigma ** 2, 0], [0, sigma ** 2]],
                          det_ids=[d.det_id for d in dets], obs_ids=[d.obs_id for d in dets], n_obs=len(dets))
        self.inputs.append((c, dets, epoch))
        self.n += 1
        return dets

    def link(self, config=CFG):
        return link_moving(self.inputs, self.observer, config)


# an unrelated slow source whose chain with a short body tracklet is admitted by the raw criteria (chosen by scan, see the precondition test)
FOREIGN = {"r_au": 800.0, "east": -40.0, "north": 0.0, "drift": 0.0}


class NumericalTests(unittest.TestCase):
    def dets(self, times, sigma=0.1):
        return [make_det(f"d{i}", f"o{i}", RA0, DEC0, sigma_arcsec=sigma, mjd=60000.0 + t) for i, t in enumerate(times)]

    def test_two_point_rate_precision_matches_the_closed_form(self):
        sig = math.sqrt(0.1 ** 2 + FLOOR2)
        got, why = tracklet_rate_precision(self.dets([0.0, 2.0]), FLOOR2)
        self.assertEqual(why, "")
        self.assertAlmostEqual(got, math.sqrt(2.0) * sig / 2.0, places=9)

    def test_many_point_precision_is_the_regression_slope_error(self):
        times = [0.0, 1.0, 2.0, 3.0]
        got, _ = tracklet_rate_precision(self.dets(times), FLOOR2)
        self.assertAlmostEqual(got, math.sqrt(0.1 ** 2 + FLOOR2) / math.sqrt(sum((t - 1.5) ** 2 for t in times)), places=9)

    def test_actual_detection_uncertainty_is_used_not_the_floor_alone(self):
        tight, _ = tracklet_rate_precision(self.dets([0.0, 1.0], sigma=0.05), FLOOR2)
        loose, _ = tracklet_rate_precision(self.dets([0.0, 1.0], sigma=2.0), FLOOR2)
        self.assertGreater(loose, 4 * tight)
        self.assertAlmostEqual(loose, math.sqrt(2.0) * math.sqrt(2.0 ** 2 + FLOOR2), places=9)

    def test_anisotropic_covariance_reports_the_worst_axis(self):
        ds = self.dets([0.0, 1.0])
        for d in ds:
            d.cov_sky_arcsec2 = [[0.01, 0.0], [0.0, 1.0]]
        got, _ = tracklet_rate_precision(ds, FLOOR2)
        self.assertAlmostEqual(got, math.sqrt(2.0) * math.sqrt(1.0 + FLOOR2), places=9)

    def test_missing_or_invalid_inputs_are_reported_never_substituted(self):
        ds = self.dets([0.0, 1.0])
        ds[0].cov_sky_arcsec2 = [[float("nan"), 0.0], [0.0, 0.01]]
        self.assertEqual(tracklet_rate_precision(ds, FLOOR2), (None, "COVARIANCE_UNAVAILABLE_OR_NOT_POSITIVE_DEFINITE"))
        ds = self.dets([0.0, 1.0])
        ds[1].cov_sky_arcsec2 = [[-1.0, 0.0], [0.0, -1.0]]
        self.assertEqual(tracklet_rate_precision(ds, -10.0)[0], None)  # a negative floor cannot rescue a bad covariance
        self.assertEqual(tracklet_rate_precision(self.dets([0.0, 0.0]), FLOOR2), (None, "SINGLE_TIME"))
        self.assertEqual(tracklet_rate_precision(self.dets([0.0]), FLOOR2), (None, "FEWER_THAN_2_ASTROMETRIC_DETECTIONS"))
        ds = self.dets([0.0, 1.0])
        ds[1].astrometry_usable = False
        self.assertEqual(tracklet_rate_precision(ds, FLOOR2)[1], "FEWER_THAN_2_ASTROMETRIC_DETECTIONS")

    def test_effective_dof_counts(self):
        cases = {(True, True): 3, (True, False): 1, (False, False): -1, (False, False, False): 1, (False,) * 4: 3, (True,) * 3: 7,
                 (True,) * 5: 15, (True, True, False): 5}
        for flags, expected in cases.items():
            self.assertEqual(effective_dof(list(flags)), expected, flags)

    def test_rate_scale_is_the_annual_parallax_rate_at_r_max(self):
        self.assertAlmostEqual(SCALE, parallax_arcsec(2000.0) * 2 * math.pi / 365.25, places=12)
        self.assertAlmostEqual(SCALE, 1.77, places=2)
        half = dataclasses.replace(CFG, planetx=dataclasses.replace(CFG.planetx, link_rate_info_fraction=0.5))
        self.assertAlmostEqual(rate_scale_arcsec_per_day(half), SCALE / 2, places=12)

    def test_informativeness_boundary_is_inclusive(self):
        sig = math.sqrt(0.1 ** 2 + FLOOR2)
        dt_edge = math.sqrt(2.0) * sig / SCALE
        at, _ = tracklet_rate_precision(self.dets([0.0, dt_edge * (1 + 1e-9)]), FLOOR2)
        above, _ = tracklet_rate_precision(self.dets([0.0, dt_edge * (1 - 1e-3)]), FLOOR2)
        self.assertLessEqual(at, SCALE)
        self.assertGreater(above, SCALE)


class LinkingBehaviourTests(unittest.TestCase):
    def test_true_body_with_measured_tracklets_is_linked_with_diagnostics(self):
        f = Field()
        for e in (0, 1, 3):
            f.add(e, LONG, r_au=800.0)
        res = f.link()
        self.assertEqual((len(res.tracks), res.truncated, res.n_rejected_low_effective_dof), (1, False, 0))
        t = res.tracks[0]
        self.assertEqual(t.diagnostics["effective_dof"], 7)  # 3 tracklets x (2 + 2) - 5
        self.assertEqual(t.diagnostics["raw_dof"], 2 * 12 - 5)
        self.assertTrue(all(x["informative"] and x["unavailable_reason"] is None for x in t.diagnostics["tracklets"]))
        self.assertLess(abs(t.parallax.distance_au - 800.0) / 800.0, 0.02)

    def test_two_measured_tracklets_sit_exactly_on_the_acceptance_boundary(self):
        f = Field()
        f.add(0, LONG, r_au=800.0)
        f.add(3, LONG, r_au=800.0)
        res = f.link()
        self.assertEqual(len(res.tracks), 1)
        self.assertEqual(res.tracks[0].diagnostics["effective_dof"], 3)  # == min_link_dof: accepted

    def test_short_body_tracklet_joined_to_a_foreign_tracklet_is_rejected_with_evidence(self):
        f = Field()
        f.add(0, SHORT, r_au=800.0, tag="B")
        f.add(3, [0.0, 2.7], foreign=FOREIGN, tag="F")
        old_behaviour = f.link(dataclasses.replace(CFG, planetx=dataclasses.replace(CFG.planetx, link_rate_info_fraction=1e9)))
        self.assertEqual(len(old_behaviour.tracks), 1, "precondition: the fit test alone admits this wrong chain")
        new = f.link()
        self.assertEqual((len(new.tracks), new.n_rejected_low_effective_dof), (0, 1))
        ex = new.rejected_examples[0]
        self.assertEqual((ex["effective_dof"], ex["raw_dof"]), (1, 3))
        self.assertEqual([x["informative"] for x in ex["tracklets"]], [False, True])
        self.assertGreater(ex["tracklets"][0]["rate_sigma_arcsec_day"], SCALE)

    def test_more_points_in_the_foreign_tracklet_do_not_rescue_the_chain(self):
        f = Field()
        f.add(0, SHORT, r_au=800.0, tag="B")
        f.add(3, list(np.linspace(0.0, 2.7, 12)), foreign=FOREIGN, tag="F")
        res = f.link()
        self.assertEqual(len(res.tracks), 0)
        self.assertEqual(res.rejected_examples[0]["effective_dof"], 1)  # raw dof would be 23

    def test_unmeasured_tracklets_need_four_positions_to_be_testable(self):
        for n_epochs, expected_tracks in ((3, 0), (4, 1)):
            f = Field()
            for e in range(n_epochs):
                f.add(e, SHORT, r_au=800.0)
            res = f.link()
            self.assertEqual(len(res.tracks), expected_tracks, n_epochs)
            if expected_tracks:
                self.assertEqual(res.tracks[0].diagnostics["effective_dof"], 3)
                self.assertFalse(any(x["informative"] for x in res.tracks[0].diagnostics["tracklets"]))

    def test_unavailable_rate_precision_is_diagnosed_and_not_assumed_informative(self):
        f = Field()
        a = f.add(0, LONG, r_au=800.0)
        f.add(3, LONG, r_au=800.0)
        for d in a[1:]:
            d.astrometry_usable = False  # only one usable detection left: the tracklet's rate cannot be estimated
        res = f.link()
        self.assertEqual(len(res.tracks), 0)
        first = res.rejected_examples[0]["tracklets"][0]
        self.assertEqual(first["unavailable_reason"], "FEWER_THAN_2_ASTROMETRIC_DETECTIONS")
        self.assertFalse(first["informative"])
        self.assertIsNone(first["rate_sigma_arcsec_day"])

    def test_thresholds_other_than_the_dof_check_are_unchanged(self):
        pp = PlanetXParams()
        self.assertEqual((pp.min_link_dof, pp.link_gof_min_p, pp.r_min_au, pp.r_max_au, pp.link_rate_info_fraction), (3, 0.01, 100.0, 2000.0, 1.0))
        self.assertAlmostEqual(parallax_arcsec(pp.r_max_au) / 3.0, 34.4, places=1)  # parallax-uncertainty limit

    def test_truncation_is_still_reported_and_diagnostics_stay_consistent(self):
        f = Field()
        for e in (0, 1, 2, 3):
            f.add(e, LONG, r_au=800.0)
        res = f.link(dataclasses.replace(CFG, planetx=dataclasses.replace(CFG.planetx, max_link_evaluations=2)))
        self.assertTrue(res.truncated)
        self.assertTrue(any("some chains were not evaluated" in n for n in res.notes))
        self.assertEqual(res.rate_scale_arcsec_day, SCALE)
        self.assertGreaterEqual(res.n_rejected_low_effective_dof, 0)

    def test_serialization_is_deterministic_and_complete(self):
        def run():
            f = Field()
            f.add(0, LONG, r_au=800.0)
            f.add(3, LONG, r_au=800.0)
            f.add(0, SHORT, r_au=800.0, tag="B")
            f.add(3, [0.0, 2.7], foreign=FOREIGN, tag="F")
            return json.dumps(dataclasses.asdict(f.link()), sort_keys=True, default=str)
        a, b = run(), run()
        self.assertEqual(a, b)
        d = json.loads(a)
        self.assertIn("diagnostics", d["tracks"][0])
        self.assertEqual(d["tracks"][0]["diagnostics"]["rule"][:13], "effective_dof")
        self.assertIn("rejected_examples", d)

    def test_tracks_remain_consumable_by_the_corrected_validator(self):
        f = Field()
        dets = []
        for e in (0, 1, 3):
            dets += f.add(e, LONG, r_au=800.0)
        track = f.link().tracks[0]
        by_id = {d.det_id: d for d in dets}
        positions = {d.obs_id: list(apparent(800.0, d.mjd)) for d in dets}
        record = {"bodies": [{"id": "B0", "positions": positions}],
                  "tracks": [{"track_id": track.track_id, "distance_au": track.parallax.distance_au, "pre_existing": False,
                              "dets": [{"obs_id": by_id[x].obs_id, "epoch": e, "ra": by_id[x].ra, "dec": by_id[x].dec, "sigma_arcsec": 0.1}
                                       for x, e in zip(track.det_ids, [0] * 4 + [1] * 4 + [3] * 4)]}]}
        out = validation.attribute_experiment(record, gate=gate_d2(CFG.association), floor_arcsec=0.4, ambiguous_factor=2.0)
        self.assertEqual((out["tracks"][0]["class"], out["tracks"][0]["role"]), ("pure", "recovery"))


class ConfigurationTests(unittest.TestCase):
    def test_parameter_is_part_of_the_configuration_and_its_hash(self):
        self.assertIn("planetx.link_rate_info_fraction", CFG.flat())
        other = dataclasses.replace(CFG, planetx=dataclasses.replace(CFG.planetx, link_rate_info_fraction=0.5))
        self.assertNotEqual(CFG.hash, other.hash)
        self.assertEqual(EngineConfig.from_dict(CFG.to_dict()).hash, CFG.hash)
        self.assertEqual(EngineConfig.from_dict({"planetx": {"link_rate_info_fraction": 0.5}}).planetx.link_rate_info_fraction, 0.5)
        self.assertTrue(CFG.version.startswith("1.2.0"))

    def test_invalid_values_are_rejected(self):
        for bad in (0.0, -1.0, float("nan"), float("inf")):
            with self.assertRaises(ValueError, msg=str(bad)):
                EngineConfig.from_dict({"planetx": {"link_rate_info_fraction": bad}})
        with self.assertRaises(ValueError):
            EngineConfig.from_dict({"planetx": {"link_rate_info_fraction_typo": 1.0}})


class StatisticalPremiseTests(unittest.TestCase):
    """The premise behind the criterion, on the actual parallax fit: an unmeasured rate leaves wrong chains testable almost never."""

    def acceptance(self, body_offsets, n=120, seed=3):
        rng = np.random.default_rng(seed)
        sig_true, sig_fit = 0.2, math.hypot(0.2, 0.4)
        accepted = 0
        for _ in range(n):
            r = float(np.exp(rng.uniform(math.log(100), math.log(2000))))
            tb = [EPOCH_T0[0] + x for x in body_offsets]
            tf = [EPOCH_T0[3] + x for x in (0.0, 2.7)]
            pts = [(t, *apparent(r, t)) for t in tb]
            pts = [(t, ra + rng.normal(0, sig_true) / 3600 / math.cos(math.radians(DEC0)), dec + rng.normal(0, sig_true) / 3600) for t, ra, dec in pts]
            ra, dec = apparent(r, tf[0])
            off, drift = rng.uniform(-100, 100, 2) / 3600, rng.normal(0, 0.5, 2) / 3600
            pts += [(t, ra + off[0] + drift[0] * (t - tf[0]) + rng.normal(0, sig_true) / 3600,
                     dec + off[1] + drift[1] * (t - tf[0]) + rng.normal(0, sig_true) / 3600) for t in tf]
            fit = parallax_fit([p[1] for p in pts], [p[2] for p in pts], [np.eye(2) * sig_fit ** 2] * len(pts), [p[0] for p in pts],
                               np.array([earth(p[0]) for p in pts]))
            accepted += int(fit.status == "COMPLETED" and fit.gof is not None and fit.gof.p_value >= 0.01)
        return accepted / n

    def test_wrong_pairings_pass_the_chi_square_test_unless_the_rate_is_measured(self):
        unmeasured, measured = self.acceptance(SHORT), self.acceptance(LONG)
        self.assertGreater(unmeasured, 0.25)  # the fit test alone is nearly powerless here (about 45% pass)
        self.assertLess(measured, 0.10)  # with a measured rate it rejects wrong pairings (about 1% pass)


if __name__ == "__main__":
    unittest.main()
