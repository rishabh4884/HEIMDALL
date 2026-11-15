"""External context (Gaia DR3, JPL SB Ident), offline caching, evidence reasoning with context, independence checks,
follow-up ranking and Planet X context rules.

All service responses here are TEST INPUTS (invented rows, or the documented sb_ident example format); no network is used.
"""

import json
import math
import shutil
import tempfile
import unittest
from pathlib import Path

from heimdall.config import load_settings
from heimdall.context import CachedService
from heimdall.context.gaia import GaiaCatalogue, match_position, parse_stars
from heimdall.context.small_bodies import (Prediction, FieldQuery, SmallBodyIdentifier, _sexagesimal, match_detections,
                                           parse_dec, parse_response, subject_status)
from heimdall.contracts import AttentionLevel, CandidateKind, CheckState, ErrorCode, HeimdallError, ResultOutcome
from heimdall.data_access import DiscoveryQuery, SkyRegion
from heimdall.data_access.http import HttpResponse
from heimdall.data_access.irsa_spherex import IrsaSpherexSource
from heimdall.engine import planetx, reasoning
from heimdall.engine.motion import MotionFit, ParallaxFit
from heimdall.engine.stats import chi2_significance
from tests.helpers import make_det
from tests.test_analysis_units import CFG, VALID, evidence

GAIA_CSV = ("source_id,ra,dec,ra_error,dec_error,pmra,pmdec,pmra_error,pmdec_error,parallax,parallax_error,phot_g_mean_mag,ref_epoch\n"
            "111,150.0,2.0,0.1,0.1,1000.0,0.0,0.1,0.1,2.0,0.1,15.0,2016.0\n"
            "222,150.01,2.01,0.2,0.2,1.0,-1.0,0.2,0.2,0.5,0.2,18.0,2016.0\n")
SB_DOC_EXAMPLE = {"signature": {"source": "NASA/JPL Small-Body Identification API", "version": "1.1"},
                  "fields_first": ["Object name", "Astrometric RA (hh:mm:ss)", "Astrometric Dec (dd mm'ss\")",
                                   "Dist. from center RA (\")", "Dist. from center Dec (\")", "Dist. from center Norm (\")",
                                   "Visual magnitude (V)", "RA rate (deg/s)", "Dec rate (deg/s)", "Est. error RA (\")",
                                   "Est. error Dec (\")"],
                  "n_first_pass": 2, "n_second_pass": 1,
                  "data_first_pass": [["194006 (2001 SG10)", "08:55:26", "+10 05'19\"", "-7.E4", "-141.", "6.7E4", "23.1",
                                       "-4.233E+01", "6.650E+00", "141.1", "141.1"],
                                      ["10193 Nishimoto (1996 PR1)", "10:16:58", "+10 28'34\"", "2.E3", "814.", "1951.", "19.4",
                                       "-2.564E+01", "1.035E+01", "0.8", "0.6"]],
                  "fields_second": ["Object name", "Astrometric RA (hh:mm:ss)", "Astrometric Dec (dd mm'ss\")",
                                    "Dist. from center RA (\")", "Dist. from center Dec (\")", "Dist. from center Norm (\")",
                                    "Visual magnitude (V)", "RA rate (deg/s)", "Dec rate (deg/s)"],
                  "data_second_pass": [["10193 Nishimoto (1996 PR1)", "10:16:58.22", "+10 28'34.3\"", "2.E3", "814.", "1951.",
                                        "19.4", "-2.564E+01", "1.035E+01"]]}


class Fake:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    def __call__(self, method, url, data, headers, timeout):
        self.calls += 1
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item


class ContextServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def service(self, name, *responses, offline=False):
        env = {"HEIMDALL_CACHE_DIR": str(self.tmp), "HEIMDALL_MAX_RETRIES": "0"}
        if offline:
            env["HEIMDALL_OFFLINE"] = "1"
        return CachedService(name, load_settings(environ=env), transport=Fake(*responses), sleep=lambda s: None, min_interval_s=0)

    def test_gaia_parse_propagate_and_match(self):
        stars = parse_stars(GAIA_CSV)
        self.assertEqual(len(stars), 2)
        ra, dec, cov = stars[0].at_epoch(51544.5 + 26 * 365.25)  # J2026: 10 yr after 2016 at 1 arcsec/yr east
        self.assertAlmostEqual((ra - 150.0) * 3600 * math.cos(math.radians(2.0)), 10.0, places=3)
        m = match_position(ra, dec, [[0.04, 0], [0, 0.04]], 51544.5 + 26 * 365.25, stars, 0.4, 0.9973)
        self.assertEqual((m["status"], m["gaia_source_id"]), ("MATCH", "111"))
        self.assertEqual(match_position(150.2, 2.2, [[0.04, 0], [0, 0.04]], 61000, stars, 0.4, 0.9973)["status"], "NO_MATCH")

    def test_epoch_propagation_agrees_with_astropy_space_motion(self):
        """Independent reference: astropy's rigorous space-motion propagation (rv = 0) over 10 years."""
        from astropy import units as u
        from astropy.coordinates import Distance, SkyCoord
        from astropy.time import Time
        from heimdall.context.gaia import Star
        for dec in (2.0, 45.0, -70.0):
            star = Star("t", 150.0, dec, 0.1, 0.1, 400.0, -250.0, 0.1, 0.1, 5.0, 0.1, 15.0, 2016.0)
            mjd = 51544.5 + 26 * 365.25
            ra, dec_new, _ = star.at_epoch(mjd)
            ref = SkyCoord(ra=150.0 * u.deg, dec=dec * u.deg, pm_ra_cosdec=400.0 * u.mas / u.yr, pm_dec=-250.0 * u.mas / u.yr,
                           distance=Distance(parallax=5.0 * u.mas), radial_velocity=0 * u.km / u.s,
                           obstime=Time(2016.0, format="jyear")).apply_space_motion(new_obstime=Time(mjd, format="mjd"))
            sep = SkyCoord(ra * u.deg, dec_new * u.deg).separation(SkyCoord(ref.ra, ref.dec)).to_value(u.mas)
            self.assertLess(sep, 5.0, f"dec {dec}: {sep:.2f} mas")

    def test_query_failure_is_never_no_match(self):
        failed = GaiaCatalogue(self.service("gaia", HttpResponse(503, b""))).cone(150, 2, 0.1, 21)
        self.assertEqual((failed.status, failed.n_stars), ("FAILED", 0))
        bad = GaiaCatalogue(self.service("gaia", HttpResponse(200, b"a,b\n1,2\n"))).cone(150, 2, 0.1, 21)
        self.assertEqual(bad.status, "FAILED")
        off = GaiaCatalogue(self.service("gaia", HttpResponse(200, GAIA_CSV.encode()), offline=True)).cone(150, 2, 0.1, 21)
        self.assertEqual(off.status, "OFFLINE_NOT_CACHED")

    def test_responses_are_cached_for_offline_reruns(self):
        live = GaiaCatalogue(self.service("gaia", HttpResponse(200, GAIA_CSV.encode()))).cone(150, 2, 0.1, 21)
        self.assertEqual((live.status, live.from_cache, live.n_stars), ("COMPLETED", False, 2))
        again = GaiaCatalogue(self.service("gaia", RuntimeError("must not be called"), offline=True)).cone(150, 2, 0.1, 21)
        self.assertEqual((again.status, again.from_cache, again.n_stars), ("COMPLETED", True, 2))

    def test_sbident_parsing_and_request_format(self):
        preds, version = parse_response(json.dumps(SB_DOC_EXAMPLE).encode())
        self.assertEqual(version, "1.1")
        by = {p.name: p for p in preds}
        self.assertEqual(set(by), {"10193 Nishimoto (1996 PR1)"})  # first-pass-only screening candidates are excluded
        self.assertAlmostEqual(by["10193 Nishimoto (1996 PR1)"].ra, 15 * (10 + 16 / 60 + 58.22 / 3600), places=9)
        nishimoto = by["10193 Nishimoto (1996 PR1)"]
        # The first-pass error describes the two-body position, not the integrated second-pass one, so it is kept
        # for information only and the second-pass position uses the stated assumed error when matching.
        self.assertEqual((nishimoto.source_pass, nishimoto.error_arcsec, nishimoto.first_pass_error_arcsec), ("second", None, 0.8))
        self.assertAlmostEqual(parse_dec("-10 30'00.0\""), -10.5)
        self.assertEqual(_sexagesimal(-10.5, hours=False), "M10-30-00.00")
        self.assertEqual(_sexagesimal(150.0, hours=True), "10-00-00.00")
        sbi = SmallBodyIdentifier(self.service("sb", HttpResponse(200, json.dumps(SB_DOC_EXAMPLE).encode())))
        q = sbi.field("o1", 61000.0, 150.0, 2.0, 0.1, (-3943.8, 5463.0, 1999.5, -0.38, 1.0, 7.0))
        self.assertEqual((q.status, len(q.predictions)), ("COMPLETED", 1))
        self.assertIn("xobs", q.params)
        self.assertEqual((q.params["vmag-lim"], q.params["mag-required"]), ("21", "true"))
        self.assertEqual(sbi.field("o2", 61000.0, 150.0, 2.0, 0.1, None).status, "FAILED")

    def test_known_object_states(self):
        pred = Prediction(name="(TEST) 1", ra=150.0, dec=2.0, vmag=18.0, error_arcsec=0.5, source_pass="second")
        q = {o: FieldQuery(o, "COMPLETED", "", False, {}, [pred]) for o in ("a", "b")}
        dets = [make_det("a:1", "a", 150.0, 2.0), make_det("b:1", "b", 150.0 + 0.5 / 3600, 2.0)]
        matches = [m for o in ("a", "b") for m in match_detections([d for d in dets if d.obs_id == o], q[o], 0.4, 1.0, 0.9973)]
        self.assertEqual(subject_status(["a", "b"], matches, q)["status"], "CONFIRMED_MATCH")
        self.assertEqual(subject_status(["a", "b"], matches[:1], q)["status"], "PROBABLE_MATCH")
        self.assertEqual(subject_status(["a", "b"], [], q)["status"], "NO_MATCH")
        q["b"] = FieldQuery("b", "FAILED", "HTTP 503", False, {})
        self.assertEqual(subject_status(["a", "b"], [], q)["status"], "NOT_ASSESSABLE")
        far = [make_det("a:2", "a", 150.01, 2.0)]
        self.assertEqual(match_detections(far, q["a"], 0.4, 1.0, 0.9973), [])


class AdapterRobustnessTests(unittest.TestCase):
    HEADER = ("collection,observationid,energy_bandpassname,energy_bounds_lower,energy_bounds_upper,time_bounds_lower,"
              "time_bounds_upper,provenance_version,uri,contentchecksum,contentlength\n")
    ROW = "{c},TESTONLY_0001_1,SPHEREx-D2,1.1e-06,1.6e-06,61000.0,61000.001,{v},ibe/data/spherex/{r}/x.fits,md5:00ff,1000\n"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def src(self, *responses, offline=False, sleeps=None):
        env = {"HEIMDALL_CACHE_DIR": str(self.tmp), "HEIMDALL_MAX_RETRIES": "2"}
        if offline:
            env["HEIMDALL_OFFLINE"] = "1"
        return IrsaSpherexSource(load_settings(environ=env), transport=Fake(*responses),
                                 sleep=(sleeps.append if sleeps is not None else lambda s: None), route_order=("tap", "sia2"), jitter=0.0)

    def test_duplicate_release_listings_are_counted_once(self):
        body = self.HEADER + self.ROW.format(c="spherex_qr2", v="6.4", r="qr2") + self.ROW.format(c="spherex_qr3", v="7.0", r="qr3")
        q = DiscoveryQuery(region=SkyRegion(ra_deg=150, dec_deg=2, radius_deg=0.05))
        res = self.src(HttpResponse(200, body.encode())).discover(q)
        self.assertEqual(len(res.observations), 1)
        obs = res.observations[0]
        self.assertEqual(obs.metadata["release"].value, "spherex_qr3")
        self.assertEqual(obs.metadata["superseded_listings"].value, "spherex_qr2")

    def test_offline_reruns_use_cached_discovery_and_never_the_network(self):
        q = DiscoveryQuery(region=SkyRegion(ra_deg=150, dec_deg=2, radius_deg=0.05))
        body = (self.HEADER + self.ROW.format(c="spherex_qr2", v="6.4", r="qr2")).encode()
        self.assertEqual(self.src(HttpResponse(200, body)).discover(q).provenance.origin.value, "LIVE")
        cached = self.src(RuntimeError("network must not be used"), offline=True).discover(q)
        self.assertEqual((cached.outcome, cached.provenance.origin.value), (ResultOutcome.OK, "CACHED"))
        other = DiscoveryQuery(region=SkyRegion(ra_deg=10, dec_deg=2, radius_deg=0.05))
        with self.assertRaises(HeimdallError) as ctx:
            self.src(RuntimeError("no"), offline=True).discover(other)
        self.assertEqual(ctx.exception.code, ErrorCode.ARCHIVE_UNREACHABLE)

    def test_retry_after_is_honoured(self):
        sleeps = []
        q = DiscoveryQuery(region=SkyRegion(ra_deg=150, dec_deg=2, radius_deg=0.05))
        res = self.src(HttpResponse(429, b"", {"Retry-After": "7"}), HttpResponse(200, self.HEADER.encode()), sleeps=sleeps).discover(q)
        self.assertEqual(res.outcome, ResultOutcome.NO_OBSERVATIONS)
        self.assertEqual(sleeps, [7.0])


def motion_evidence(**kw):
    base = dict(kind=CandidateKind.MOTION, statistic_method="stationarity_chi2", points=[], min_points=2, min_epochs=1,
                motion=MotionFit(status="COMPLETED", reasons=[], n_points=12, n_distinct_times=12, baseline_days=365,
                                 gof_linear=chi2_significance(20.0, 20), mu_arcsec_per_yr=[0.3, 0.0], mu_cov=[[0.01, 0], [0, 0.01]]),
                error_scale=1.0, error_scale_se=0.05, leave_one_image_out=(chi2_significance(60.0, 20), "o1"),
                known_object={"status": "NO_MATCH", "reason": "no known body", "n_images": 3})
    base.update(kw)
    return evidence(**base)


class ReasoningWithContextTests(unittest.TestCase):
    def test_catalogued_stationary_star_explains_apparent_motion(self):
        cat = {"status": "MATCH", "n_within_gate": 1, "gaia_source_id": "333", "separation_arcsec": 0.3, "g_mag": 16.0,
               "pm_mas_yr": [2.0, -1.0], "pm_error_mas_yr": [0.1, 0.1]}
        out = reasoning.assess(motion_evidence(catalogue=cat), VALID, CFG, None)
        self.assertEqual(out.candidate.attention_level, AttentionLevel.EXPLAINED)
        alt = next(a for a in out.candidate.alternatives if a.alt_id == "apparent_motion_contradicted_by_catalogue")
        self.assertEqual(alt.evidence[0].ref_id, "gaia-dr3:333")
        known = next(a for a in out.candidate.alternatives if a.alt_id == "known_solar_system_object")
        self.assertEqual(known.state.value, "RULED_OUT")

    def test_multi_epoch_stationary_source_excludes_known_bodies_by_motion(self):
        out = reasoning.assess(motion_evidence(known_object={"status": "EXCLUDED_BY_MOTION", "reason": "fixed for 380 days"}),
                               VALID, CFG, None)
        alt = next(a for a in out.candidate.alternatives if a.alt_id == "known_solar_system_object")
        self.assertEqual((alt.state.value, alt.evidence[0].kind.value), ("RULED_OUT", "MEASUREMENT"))
        self.assertEqual(next(c for c in out.candidate.checks if c.check_id == "known_object_check").state, CheckState.PASSED)

    def test_high_proper_motion_star_consistent_with_catalogue(self):
        cat = {"status": "MATCH", "n_within_gate": 1, "gaia_source_id": "444", "separation_arcsec": 0.3, "g_mag": 12.0,
               "pm_mas_yr": [310.0, 5.0], "pm_error_mas_yr": [0.1, 0.1]}
        out = reasoning.assess(motion_evidence(catalogue=cat), VALID, CFG, None)
        self.assertIn("high_proper_motion_star", [a.alt_id for a in out.candidate.alternatives if a.state.value == "SUPPORTED"])

    def test_single_image_driven_motion_is_explained(self):
        out = reasoning.assess(motion_evidence(leave_one_image_out=(chi2_significance(10.0, 20), "o2")), VALID, CFG, None)
        self.assertEqual(out.candidate.attention_level, AttentionLevel.EXPLAINED)
        self.assertIn("single_image_systematic", [a.alt_id for a in out.candidate.alternatives if a.state.value == "SUPPORTED"])

    def test_known_object_resolves_the_track_check(self):
        track = dict(subject_kind="TRACK", statistic_method="parallax_motion_fit", motion=None,
                     parallax=ParallaxFit(status="COMPLETED", reasons=[], n_points=6, parallax_arcsec=100.0, parallax_sigma_arcsec=5.0,
                                          gof=chi2_significance(5.0, 7), dof=7, chi2=5.0, distance_interval_au=[1900.0, 2300.0]))
        clear = reasoning.assess(motion_evidence(**track), VALID, CFG, None)
        check = next(c for c in clear.candidate.checks if c.check_id == "known_object_check")
        self.assertEqual(check.state, CheckState.PASSED)
        hit = {"status": "CONFIRMED_MATCH", "name": "(TEST) 2", "n_images_matched": 3, "n_images": 3, "matches": []}
        out = reasoning.assess(motion_evidence(**track, known_object=hit), VALID, CFG, None)
        self.assertEqual(out.candidate.attention_level, AttentionLevel.EXPLAINED)
        failed_query = reasoning.assess(motion_evidence(**track, known_object={"status": "NOT_ASSESSABLE", "reason": "HTTP 503"}),
                                        VALID, CFG, None)
        self.assertIn("known_object_check", failed_query.candidate.attention_reason.unresolved_checks)

    def test_follow_up_ranking_is_ordered_and_explained(self):
        a = reasoning.assess(evidence(subject_id="S1"), VALID, CFG, None)  # ATTEND
        b = reasoning.assess(evidence(subject_id="S2", min_neighbour_px=1.0), VALID, CFG, None)  # INCONCLUSIVE
        c = reasoning.assess(evidence(subject_id="S3", leave_one_out=chi2_significance(1.0, 4)), VALID, CFG, None)  # EXPLAINED
        reasoning.rank_follow_up([c, b, a])
        self.assertEqual([x.follow_up["rank"] for x in (a, b, c)], [1, 2, 3])
        self.assertIn("not a probability", a.follow_up["basis"])


class ContextOutputTests(unittest.TestCase):
    CONTEXT = {"reason": None,
               "gaia": {"status": "COMPLETED", "service": "gaia_dr3_source@IRSA", "g_max": 21.0,
                        "stars": [{"source_id": "111", "ra": 150.0, "dec": 2.0, "g_mag": 15.0}]},
               "small_bodies": {"service": "sb", "n_queries": 2, "status_counts": {"COMPLETED": 1, "FAILED": 1},
                                "queries": [{"obs_id": "a", "status": "COMPLETED", "mjd": 61000.0,
                                             "predictions": [{"name": "(TEST) 1", "ra": 150.01, "dec": 2.0, "vmag": 18.0}]},
                                            {"obs_id": "b", "status": "FAILED", "predictions": []}]}}

    def test_skymap_external_layers_come_from_the_run(self):
        from heimdall.skymap.layers import _external_layers
        ext = _external_layers(self.CONTEXT)
        self.assertEqual((ext["star_catalogue"]["status"], len(ext["star_catalogue"]["items"])), ("AVAILABLE", 1))
        bodies = ext["solar_system_ephemerides"]
        self.assertEqual((bodies["status"], bodies["failed_obs_ids"], bodies["items"][0]["obs_id"]), ("PARTIAL", ["b"], "a"))
        self.assertEqual(_external_layers({"reason": "synthetic scene"})["star_catalogue"]["status"], "NOT_AVAILABLE")

    def test_unavailable_analyses_reflect_what_ran(self):
        from heimdall.pipeline.runner import unavailable_analyses
        names = {u["analysis"]: u["state"] for u in unavailable_analyses(self.CONTEXT, [])}
        self.assertNotIn("reference_star_catalogue", names)
        self.assertEqual(names["known_solar_system_object_check"], "PARTIAL")
        self.assertIn("moving_object_completeness", names)
        synthetic = {u["analysis"] for u in unavailable_analyses({"reason": "synthetic scene"}, [])}
        self.assertTrue({"reference_star_catalogue", "known_solar_system_object_check"} <= synthetic)


class PlanetXContextTests(unittest.TestCase):
    def fits(self):
        m = MotionFit(status="COMPLETED", reasons=[], n_points=12, n_distinct_times=12, baseline_days=365,
                      gof_stationary=chi2_significance(500.0, 22))
        p = ParallaxFit(status="COMPLETED", reasons=[], n_points=12, parallax_arcsec=206.26, parallax_sigma_arcsec=1.0,
                        mu_arcsec_per_yr=[0.1, 0.0], chi2=10.0, dof=19, gof=chi2_significance(10.0, 19), distance_au=1000.0,
                        distance_interval_au=[990.4, 1009.8])
        return dict(motion=m, parallax=p, n_epoch_clusters=3, validity_state="UNVALIDATED", config=CFG, evidence_prefix="T")

    def test_context_requirements(self):
        clear = planetx.evaluate("T", **self.fits(), known_object={"status": "NO_MATCH", "reason": "none"})
        self.assertEqual(clear.status, "CONSISTENT_REQUIRES_CONFIRMATION")
        self.assertEqual({r.req_id: r.state for r in clear.requirements}["not_a_known_solar_system_object"], "SATISFIED")
        known = planetx.evaluate("T", **self.fits(), known_object={"status": "CONFIRMED_MATCH", "name": "(TEST) 3"})
        self.assertEqual(known.status, "INCONSISTENT")

    def test_low_dof_fit_and_unreliable_measurement_are_not_consistent(self):
        """Regression for the real 0.3-degree run: 3 positions (dof 1) of a blended, chained source were 'consistent'."""
        f = self.fits()
        f["parallax"] = ParallaxFit(status="COMPLETED", reasons=[], n_points=3, parallax_arcsec=422.0, parallax_sigma_arcsec=112.5,
                                    mu_arcsec_per_yr=[4.9, -8.0], chi2=0.25, dof=1, gof=chi2_significance(0.25, 1),
                                    distance_au=489.0, distance_interval_au=[319.0, 1046.0])
        low = planetx.evaluate("T", **f, known_object={"status": "EXCLUDED_BY_MOTION", "reason": "fixed"})
        self.assertEqual({r.req_id: r.state for r in low.requirements}["fit_quality"], "NOT_ASSESSABLE")
        self.assertEqual(low.status, "NOT_ASSESSABLE")
        good = planetx.evaluate("T", **self.fits(), known_object={"status": "NO_MATCH", "reason": "none"})
        planetx.require_reliable_measurement(good, ["blending", "association_unambiguous"], [])
        self.assertEqual(good.status, "NOT_ASSESSABLE")
        self.assertEqual({r.req_id: r.state for r in good.requirements}["measurement_reliable"], "NOT_SATISFIED")

    def test_cli_summary_prints_a_completeness_limited_region(self):
        import contextlib
        import io
        from heimdall.__main__ import _print_summary
        summary = {"run_id": "R", "data_origin": "REAL", "status": {"execution": "COMPLETED", "outcome": "OK"},
                   "observations": {"included": 1, "total": 1, "failed_items": 0}, "epochs": 1, "detections": 0, "sources": 0,
                   "tracks": 0, "candidates_by_attention": {}, "validation": [], "warnings": [], "results_hash": "h",
                   "planet_x": {"region": {"status": "COMPLETENESS_LIMITED", "statement": "recovered 3/4 (TEST)"}, "candidates": []}}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            _print_summary(summary)
        self.assertIn("COMPLETENESS_LIMITED (recovered 3/4 (TEST))", out.getvalue())

    def test_region_statement_requires_completeness(self):
        self.assertEqual(planetx.region_assessment(0, CFG)["status"], "NOT_ASSESSABLE")
        inj = {"completeness_given_coverage": 0.8, "completeness_interval_95": [0.6, 0.9], "n_recovered": 8, "n_covered_2_epochs": 10,
               "coverage_fraction": 0.4, "field_half_width_deg": 0.1, "n_injected": 25, "false_links_per_experiment": 0.0,
               "by_snr": [{"flux_ujy": 300.0, "recovered": 8, "covered": 10}], "by_distance": []}
        out = planetx.region_assessment(0, CFG, inj, 0, "UNVALIDATED")
        self.assertEqual(out["status"], "COMPLETENESS_LIMITED")
        self.assertIn("not evidence that no such body exists", out["statement"])


if __name__ == "__main__":
    unittest.main()
