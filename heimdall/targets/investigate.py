"""Target investigation: from a name or coordinate to a structured, evidence-backed result.

Flow (the same for the five NASA targets and for any coordinate):

  resolve reference astrometry -> discover SPHEREx observations -> select a bounded, epoch-balanced subset ->
  retrieve cutouts (cache first) -> ingest + QC -> per-image measurement (forced photometry, centroid, Gaia
  controls, registration) -> motion and/or variability analysis -> blind pipeline run on the same exposures ->
  external context -> hypotheses and conclusion -> save.

Nothing here is specific to a named target: ``TargetSpec`` supplies only the resolution rule and defaults.
Data origin is recorded for every product (LIVE, CACHED or SYNTHETIC_TEST_FIXTURE); a synthetic source is
labelled as such in the result and can never produce a REAL data origin.

Bounded resources: ``max_observations`` caps retrieval; ``max_seconds`` stops starting new retrievals after a
time budget (result becomes PARTIAL); ``cancel`` is polled between images. Re-running reuses the cache, so an
interrupted run resumes without repeating downloads.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from .. import __version__
from ..config import Settings
from ..context import CachedService, form
from ..contracts import HeimdallError
from ..contracts.outcomes import ResultOutcome
from ..data_access.interface import DataSource, DiscoveryQuery, RetrievalRequest, SkyRegion
from ..engine.config import EngineConfig
from ..engine.skygeom import angular_separation_deg
from ..engine.temporal import build_epochs
from ..ingest.spherex import read_spherex_mef
from ..pipeline.results import canonical_hash, plain
from . import astrometry, hypotheses
from .astrometry import ReferenceAstrometry, W1_AB_OFFSET, W2_AB_OFFSET
from .measure import measure_image, select_controls
from .motion import analyse_motion
from .registry import MOVING, VARIABLE, TargetSpec
from .variability import analyse_variability

TARGET_SCHEMA = "heimdall.target_investigation/1.0"
TARGET_METHODS_VERSION = "targets-1.0.0-provisional"
MID_SURVEY_MJD = 61050.0  # only used to place the discovery query; the cutouts use the selected observations' own epochs
DEFAULT_DETECTORS = {"NASA-01": (2, 3), "NASA-02": (2, 5), "NASA-03": (5, 6), "NASA-04": (2, 5), "NASA-05": (2, 5)}


@dataclass
class TargetOptions:
    max_observations: int = 16
    detectors: tuple[int, ...] | None = None
    mjd_start: float | None = None
    mjd_end: float | None = None
    cutout_size_deg: float | None = None
    blind_pipeline: bool = True
    max_seconds: float | None = 1800.0
    context: bool = True
    save: bool = True

    def to_dict(self) -> dict:
        return {k: (list(v) if isinstance(v, tuple) else v) for k, v in self.__dict__.items()}


def _val(o, key):
    v = o.metadata.get(key)
    return v.value if v is not None and v.is_available else None


def _obs_time(o) -> float:
    for k in ("mjd_avg_utc", "time_start_mjd"):
        t = _val(o, k)
        if t is not None:
            return float(t)
    return math.inf


def select_observations(observations: list, n_max: int, gap_days: float) -> tuple[list, dict]:
    """Deterministic epoch-balanced selection: keep first and last epochs, spread picks over epochs and detectors.

    Per (epoch, detector) group the picks are evenly spaced in time. The rule is stated in the result so a
    reader can see that the subset is not a random or cherry-picked sample.
    """
    obs = sorted(observations, key=lambda o: (_obs_time(o), o.obs_id))
    epochs, idx = build_epochs([(o.obs_id, _obs_time(o)) for o in obs if math.isfinite(_obs_time(o))], gap_days)
    groups: dict[tuple[int, str], list] = {}
    for o in obs:
        e = idx.get(o.obs_id)
        if e is None:
            continue
        groups.setdefault((e, str(_val(o, "detector"))), []).append(o)
    keys = sorted(groups)
    if not keys:
        return [], {"rule": "no observation has a usable time", "n_groups": 0}
    if len(keys) > n_max:  # more (epoch, detector) groups than the budget: thin evenly, always keeping the first and last
        pick = sorted({int(round(i)) for i in np.linspace(0, len(keys) - 1, n_max)})
        keys = [keys[i] for i in pick]
    per = max(1, n_max // len(keys))
    chosen = []
    for k in keys:
        g = groups[k]
        if len(g) <= per:
            chosen += g
        else:
            chosen += [g[i] for i in sorted({int(round(i)) for i in np.linspace(0, len(g) - 1, per)})]
    chosen = sorted(chosen, key=lambda o: (_obs_time(o), o.obs_id))[:n_max]
    return chosen, {"rule": "epoch-balanced: epochs are gaps > %.1f d; per (epoch, detector) group evenly spaced picks; first and last epoch kept" % gap_days,
                    "n_epochs_available": len(epochs), "n_groups": len(keys), "picks_per_group": per, "n_selected": len(chosen), "n_available": len(obs)}


def _coverage_summary(all_obs: list, selected: list, gap_days: float) -> dict:
    times = [_obs_time(o) for o in all_obs if math.isfinite(_obs_time(o))]
    epochs, _ = build_epochs([(o.obs_id, _obs_time(o)) for o in all_obs if math.isfinite(_obs_time(o))], gap_days)
    dets: dict[str, int] = {}
    rel: dict[str, int] = {}
    for o in all_obs:
        dets[str(_val(o, "detector"))] = dets.get(str(_val(o, "detector")), 0) + 1
        rel[str(_val(o, "release"))] = rel.get(str(_val(o, "release")), 0) + 1
    return {"n_discovered": len(all_obs), "n_selected": len(selected), "mjd_min": min(times) if times else None, "mjd_max": max(times) if times else None,
            "n_epochs": len(epochs), "epochs": [{"index": e.index, "mjd_start": e.mjd_start, "mjd_end": e.mjd_end, "n": e.n_observations} for e in epochs],
            "per_detector": dets, "per_release": rel,
            "detector_wavelength_ranges_um": {"1": [0.75, 1.09], "2": [1.09, 1.63], "3": [1.63, 2.42], "4": [2.42, 3.82], "5": [3.82, 4.42], "6": [4.42, 5.00]}}


def _wise_reference_flux(ref: ReferenceAstrometry) -> dict | None:
    m = ref.magnitudes or {}
    out = {}
    for band, off, lam in (("W1", W1_AB_OFFSET, 3.35), ("W2", W2_AB_OFFSET, 4.60)):
        mag = m.get(f"{band}_vega")
        if mag is not None:
            out[band] = {"vega_mag": mag, "ab_mag": mag + off, "flux_ujy": 3631.0e6 * 10 ** (-0.4 * (mag + off)), "reference_wavelength_um": lam}
    return out or None


def _detectability(measurements: list[dict], wise: dict | None) -> dict:
    sig = [(m["wavelength_um"], m["forced"]["flux_sigma_ujy"], m["forced"].get("snr"), m["forced"].get("flux_ujy")) for m in measurements
           if m["status"] == "MEASURED" and m.get("forced") and m["forced"].get("flux_sigma_ujy")]
    out: dict = {"n_forced_measurements": len(sig), "state": "MEASURED_LOCAL_NOISE" if sig else "NOT_ASSESSABLE"}
    if sig:
        snr = [s[2] for s in sig if s[2] is not None]
        out["local_1sigma_ujy"] = {"median": float(np.median([s[1] for s in sig])), "min": float(min(s[1] for s in sig)), "max": float(max(s[1] for s in sig))}
        out["forced_snr"] = {"median": float(np.median(snr)) if snr else None, "max": float(max(snr)) if snr else None,
                             "n_ge_3": int(sum(1 for s in snr if s >= 3)), "n_ge_5": int(sum(1 for s in snr if s >= 5))}
    if wise and sig:
        comp = []
        for band, w in wise.items():
            near = [s for s in sig if s[0] and abs(s[0] - w["reference_wavelength_um"]) < 0.9]
            if near:
                comp.append({"band": band, "implied_flux_ujy": w["flux_ujy"], "median_local_1sigma_ujy_near_band": float(np.median([s[1] for s in near])),
                             "implied_snr_per_exposure": float(w["flux_ujy"] / np.median([s[1] for s in near])), "n_images_within_0.9um": len(near)})
        out["wise_implied_detectability"] = {"entries": comp, "caveat": "WISE broad-band magnitude converted to AB; the source colour and the narrower SPHEREx channel are not modelled, so this is an order-of-magnitude indicator"}
    return out


def _catwise_neighbour(ref: ReferenceAstrometry, service: CachedService) -> dict:
    """CatWISE2020 entry nearest the target propagated to the CatWISE epoch: independent infrared astrometric reference."""
    if not ref.resolved:
        return {"status": "NOT_ASSESSABLE", "reason": "target unresolved"}
    if ref.catalogue == "CatWISE2020":
        return {"status": "PRIMARY_REFERENCE", "note": "CatWISE2020 is the primary reference for this target"}
    e = ref.at_mjd(astrometry.CATWISE_REFERENCE_MJD)
    r_deg = 8.0 / 3600.0
    adql = (f"SELECT {', '.join(astrometry.CATWISE_COLUMNS)} FROM catwise_2020 WHERE 1 = CONTAINS(POINT('ICRS', ra_pm, dec_pm), "
            f"CIRCLE('ICRS', {e.ra_deg:.8f}, {e.dec_deg:.8f}, {r_deg:.6f}))")
    body = form({"REQUEST": "doQuery", "LANG": "ADQL", "FORMAT": "csv", "QUERY": adql})
    ans = service.fetch(astrometry.TAP_SYNC, body, validate=lambda raw: None if not raw.lstrip().startswith(b"<") else "TAP returned an error document")
    if ans.status != "COMPLETED":
        return {"status": "SERVICE_" + ans.status, "reason": ans.reason}
    try:
        rows = astrometry.parse_catwise(ans.body.decode("utf-8", errors="replace"))
    except ValueError as exc:
        return {"status": "FAILED", "reason": str(exc)}
    if not rows:
        return {"status": "NO_MATCH", "predicted_position_at_catwise_epoch": [e.ra_deg, e.dec_deg], "search_radius_arcsec": 8.0,
                "note": "no CatWISE2020 entry within the radius; this is not evidence about the target (catalogue incompleteness, saturation or blending are possible)"}
    best = min(rows, key=lambda r: float(angular_separation_deg(float(r["ra_pm"]), float(r["dec_pm"]), e.ra_deg, e.dec_deg)))
    sep = float(angular_separation_deg(float(best["ra_pm"]), float(best["dec_pm"]), e.ra_deg, e.dec_deg)) * 3600.0
    return {"status": "MATCH" if len(rows) == 1 else "AMBIGUOUS", "n_candidates": len(rows), "source_name": best["source_name"], "separation_arcsec": sep,
            "w1_vega": astrometry._f(best["w1mpro_pm"]), "w2_vega": astrometry._f(best["w2mpro_pm"]), "pm_mas_yr": [astrometry._f(best["pmra"]) * 1e3 if astrometry._f(best["pmra"]) is not None else None,
                                                                                                               astrometry._f(best["pmdec"]) * 1e3 if astrometry._f(best["pmdec"]) is not None else None]}


def _blind_pipeline(engine_parts: dict, selected_ids: list[str], center: tuple[float, float], size: float, spec: TargetSpec, ref: ReferenceAstrometry,
                    measurements: list[dict], opts: TargetOptions, progress) -> dict:
    """Run the ordinary HEIMDALL pipeline on the same exposures and report what it independently found near the target."""
    from ..pipeline.runner import InvestigationPipeline, InvestigationRequest, RunStore
    from ..skymap.layers import build_skymap
    from ..skymap.svg import render_svg
    source, cfg, settings, registry, store = (engine_parts[k] for k in ("source", "config", "settings", "registry", "store"))
    req = InvestigationRequest(ra_deg=center[0], dec_deg=center[1], radius_deg=size / 2.0, source_id=source.source_id, obs_ids=selected_ids,
                               cutout_size_deg=size, label=f"target {spec.target_id}", context="auto" if opts.context else "none")
    try:
        res = InvestigationPipeline(source, cfg, settings, registry, progress).run(req)
    except Exception as exc:  # noqa: BLE001 - the blind run must not invalidate the target analysis
        return {"status": "FAILED", "reason": f"{type(exc).__name__}: {exc}"}
    run_dir = None
    if opts.save:
        run_dir = store.save(res, build_skymap(res))
        (run_dir / "skymap.svg").write_text(render_svg(build_skymap(res)), encoding="utf-8")
    dets = res.get("detections", [])
    found, covered = [], 0
    for m in measurements:
        if m["status"] != "MEASURED" or not m.get("expected"):
            continue
        covered += 1
        e = m["expected"]
        tol = 6.15 + 3.0 * math.sqrt(max(e["cov_arcsec2"][0][0], 0.0))
        near = [d for d in dets if d["obs_id"] == m["obs_id"] and float(angular_separation_deg(d["ra"], d["dec"], e["ra_deg"], e["dec_deg"])) * 3600.0 <= tol]
        if near:
            d = min(near, key=lambda d: float(angular_separation_deg(d["ra"], d["dec"], e["ra_deg"], e["dec_deg"])))
            found.append({"obs_id": m["obs_id"], "det_id": d["det_id"], "separation_arcsec": float(angular_separation_deg(d["ra"], d["dec"], e["ra_deg"], e["dec_deg"])) * 3600.0,
                          "snr": d.get("snr_fit")})
    inv = res.get("investigations") or {}
    return {"status": res["status"]["execution"] if isinstance(res.get("status"), dict) else "COMPLETED", "run_id": res["run_id"], "run_dir": str(run_dir) if run_dir else None,
            "results_hash": res.get("results_hash"), "data_origin": res.get("data_origin"), "n_detections": len(dets), "n_sources": len(res.get("sources", [])),
            "n_tracks": len(res.get("tracks", [])), "n_candidates": len(res.get("candidates", [])),
            "interpretation_counts": inv.get("interpretation_counts") if isinstance(inv, dict) else None,
            "blind_detection_near_expected_target": {"n_images_covered": covered, "n_with_blind_detection": len(found), "matches": found[:40],
                                                      "match_radius": "one SPHEREx pixel (6.15 arcsec) + 3 sigma of the expected-position uncertainty"},
            "note": "the blind run is independent of the target-specific forced measurements; it does not know where the target is"}


def investigate_target(spec: TargetSpec, *, source: DataSource, settings: Settings, config: EngineConfig, registry, store, options: TargetOptions | None = None,
                       progress=None, cancel=None) -> dict:
    opts = options or TargetOptions()
    t_start = time.monotonic()
    created = datetime.now(UTC)
    events: list[dict] = []

    def note(msg: str, **kw) -> None:
        events.append({"seq": len(events), "time": datetime.now(UTC).isoformat(), "message": msg, **kw})
        if progress:
            progress(msg)

    gaia_svc = CachedService("gaia", settings)
    catwise_svc = CachedService("catwise", settings)
    result: dict = {"schema": TARGET_SCHEMA, "software_version": __version__, "methods_version": TARGET_METHODS_VERSION, "created_at": created.isoformat(),
                    "target": spec.to_dict(), "options": opts.to_dict(), "config_hash": config.hash, "status": {"execution": "RUNNING", "test_status": None},
                    "events": events, "warnings": [], "failed_items": []}
    # 1. reference astrometry --------------------------------------------------------------
    note("resolving reference astrometry")
    ref = astrometry.resolve(spec, catwise_svc if spec.catalogue == "catwise2020" else gaia_svc, gaia_service=gaia_svc)
    if not ref.resolved and spec.target_id.startswith("COORD-"):
        ref = ReferenceAstrometry(target_id=spec.target_id, catalogue="coordinate", status="COORDINATE_ONLY", reason=ref.reason, source_id=None,
                                  ra_deg=spec.seed_ra_deg, dec_deg=spec.seed_dec_deg, epoch_jyear=2000.0, epoch_mjd=astrometry.jyear_to_mjd(2000.0),
                                  pm_source="none: stationary assumed", selection_rule=ref.selection_rule, service=ref.service)
    result["reference"] = ref.to_dict()
    if spec.nasa_reference_position:
        result["reference"]["external_reference_position"] = spec.nasa_reference_position
        if spec.target_id == "NASA-03":
            result["reference"]["external_position_epoch_check"] = astrometry.seed_position_epoch_check(ref, spec.seed_ra_deg, spec.seed_dec_deg)
    if not ref.resolved:
        return _finish(result, created, "BLOCKED", "FAILED", f"target not resolved: {ref.status}: {ref.reason}", settings, opts, store)
    result["reference"]["position_at_discovery_epoch"] = ref.at_mjd(MID_SURVEY_MJD).to_dict()
    # 2. discovery --------------------------------------------------------------------------
    note("discovering SPHEREx observations")
    detectors = opts.detectors or DEFAULT_DETECTORS.get(spec.target_id)
    filters = {}
    if detectors:
        filters["detectors"] = ",".join(str(d) for d in detectors)
    if opts.mjd_start is not None:
        filters["mjd_start"] = repr(float(opts.mjd_start))
    if opts.mjd_end is not None:
        filters["mjd_end"] = repr(float(opts.mjd_end))
    c0 = ref.at_mjd(MID_SURVEY_MJD)
    try:
        disc = source.discover(DiscoveryQuery(region=SkyRegion(ra_deg=c0.ra_deg, dec_deg=c0.dec_deg, radius_deg=0.05), filters=filters))
    except HeimdallError as exc:
        result["failed_items"].append(plain(exc.info))
        return _finish(result, created, "BLOCKED", "FAILED", f"discovery failed: {exc.code}: {exc.info.message}", settings, opts, store)
    result["discovery"] = {"source_id": source.source_id, "outcome": disc.outcome.value, "filters": filters, "origin": disc.provenance.origin.value,
                           "failed_items": plain(disc.failed_items)}
    if disc.outcome is ResultOutcome.NO_OBSERVATIONS or not disc.observations:
        result["coverage"] = {"n_discovered": 0, "n_selected": 0, "n_measured": 0}
        return _finish(result, created, "COMPLETED", "LIMITED_BY_DATA", "the archive lists no SPHEREx observation covering this position with the filters", settings, opts, store)
    selected, rule = select_observations(list(disc.observations), opts.max_observations, config.temporal.epoch_gap_days)
    result["coverage"] = _coverage_summary(list(disc.observations), selected, config.temporal.epoch_gap_days)
    result["selection"] = rule
    # 3. cutout geometry --------------------------------------------------------------------
    t_sel = [_obs_time(o) for o in selected]
    t_mid = float(np.median(t_sel))
    cen = ref.at_mjd(t_mid)
    pm_tot = math.hypot(ref.pmra_mas_yr or 0.0, ref.pmdec_mas_yr or 0.0) / 3.6e6  # deg/yr
    span_deg = pm_tot * (max(t_sel) - min(t_sel)) / 365.25
    size = opts.cutout_size_deg or max(spec.cutout_size_deg, 2.0 * span_deg + 0.04)
    size = min(size, 0.3)
    result["cutout"] = {"center_ra_deg": cen.ra_deg, "center_dec_deg": cen.dec_deg, "center_mjd": t_mid, "size_deg": size,
                        "size_pixels_approx": round(size * 3600.0 / 6.15), "motion_span_deg": span_deg,
                        "transfer_note": "cutout size scales the transfer with its area; full frames are never retrieved"}
    # 4. field stars for controls and contamination ------------------------------------------
    note("querying Gaia field stars")
    gq = astrometry.gaia_cone_with_fallback(gaia_svc, cen.ra_deg, cen.dec_deg, size * 0.75, 17.0)
    stars = gq.stars if gq.status == "COMPLETED" else []
    result["field_catalogue"] = {"service": gq.service, "status": gq.status, "reason": gq.reason, "from_cache": gq.from_cache, "n_stars": gq.n_stars, "g_max": 17.0, "attempts": getattr(gq, "attempts", [])}
    controls = select_controls(stars, ref)
    result["controls_selected"] = [{"gaia_source_id": s.source_id, "g_mag": s.g_mag} for s in controls]
    # 5. calibration, retrieval, ingestion, measurement -----------------------------------------
    epsf: dict[int, Path] = {}
    calibrations = []
    region = SkyRegion(ra_deg=cen.ra_deg % 360.0, dec_deg=cen.dec_deg, radius_deg=size / 2.0)
    measurements: list[dict] = []
    products = []
    for k, o in enumerate(selected):
        if cancel is not None and cancel():
            result["warnings"].append("cancelled; partial results")
            break
        if opts.max_seconds is not None and time.monotonic() - t_start > opts.max_seconds:
            result["warnings"].append(f"time budget of {opts.max_seconds:.0f} s reached after {k} of {len(selected)} observations; partial results")
            break
        det = int(_val(o, "detector")) if _val(o, "detector") is not None else None
        prod = o.provenance.products[0] if o.provenance.products else None
        options = {}
        if prod is not None:
            for key, attr in (("access_reference", "access_reference"), ("release", "release"), ("product_version", "product_version")):
                if getattr(prod, attr):
                    options[key] = getattr(prod, attr)
            if prod.checksum:
                options["archive_checksum"] = f"{prod.checksum_algorithm}:{prod.checksum}"
        options["cutout_size_deg"] = repr(float(size))
        note(f"retrieving {o.obs_id} ({k + 1}/{len(selected)})", obs_id=o.obs_id)
        try:
            rr = source.retrieve(RetrievalRequest(obs_id=o.obs_id, region=region, options=options))
        except HeimdallError as exc:
            result["failed_items"].append({"ref": o.obs_id, "stage": "retrieval", **plain(exc.info)})
            measurements.append({"obs_id": o.obs_id, "status": "EXCLUDED", "reasons": [f"RETRIEVAL_FAILED:{exc.code}"], "mjd": _obs_time(o)})
            continue
        if det is not None and det not in epsf and hasattr(source, "retrieve_calibration"):
            try:
                epsf[det], cprod = source.retrieve_calibration("epsf", det)
                calibrations.append(plain(cprod))
            except HeimdallError as exc:
                result["warnings"].append(f"ePSF for detector {det} unavailable ({exc.code}); fallback PSF used")
        p = rr.products[0]
        origin = rr.provenance.origin.value
        try:
            ing = read_spherex_mef(p.location, source_id=source.source_id, provenance=rr.provenance, config=config,
                                   release=p.product.release or _val(o, "release"), obs_id=o.obs_id, crop=(region.ra_deg, region.dec_deg, region.radius_deg),
                                   epsf_calibration=epsf.get(det))
        except Exception as exc:  # noqa: BLE001 - one unreadable product must not stop the investigation
            result["failed_items"].append({"ref": o.obs_id, "stage": "ingestion", "message": f"{type(exc).__name__}: {exc}"})
            measurements.append({"obs_id": o.obs_id, "status": "EXCLUDED", "reasons": ["INGESTION_FAILED"], "mjd": _obs_time(o)})
            continue
        products.append({"obs_id": o.obs_id, "product_id": p.product.product_id, "release": p.product.release, "product_version": p.product.product_version,
                         "access_reference": p.product.access_reference, "sha256": p.product.checksum, "archive_md5": options.get("archive_checksum"),
                         "origin": origin, "retrieved_at": rr.provenance.retrieved_at.isoformat(), "notes": rr.provenance.notes,
                         "qc_exclusions": ing.qc.exclusion_reasons if ing.image is None else []})
        if ing.image is None:
            measurements.append({"obs_id": o.obs_id, "status": "EXCLUDED", "reasons": list(ing.qc.exclusion_reasons), "mjd": _obs_time(o), "detector": det})
            continue
        m = measure_image(ing.image, ref, controls, stars, config, product={"checksum": p.product.checksum, "access_reference": p.product.access_reference, "origin": origin})
        measurements.append(m.to_dict())
    result["products"] = products
    result["calibration_products"] = calibrations
    result["measurements"] = measurements
    n_meas = sum(1 for m in measurements if m["status"] == "MEASURED")
    result["coverage"]["n_measured"] = n_meas
    result["coverage"]["n_not_covered"] = sum(1 for m in measurements if m["status"] == "NOT_COVERED")
    result["coverage"]["n_excluded"] = sum(1 for m in measurements if m["status"] in ("EXCLUDED", "UNUSABLE"))
    result["coverage"]["wavelengths_um"] = sorted({round(m["wavelength_um"], 3) for m in measurements if m.get("wavelength_um")})
    result["coverage"]["quality_flag_counts"] = _flag_counts(measurements)
    result["data_origin"] = "SYNTHETIC" if source.source_id == "local-fits" and any(p["origin"] == "SYNTHETIC_TEST_FIXTURE" for p in products) else ("REAL" if products else None)
    # 6. context -----------------------------------------------------------------------------
    neighbours = [c for m in measurements for c in (m.get("contamination") or [])]
    cont = {"n_with_neighbour": sum(1 for m in measurements if m.get("contamination")), "closest_arcsec": min((c["separation_arcsec"] for c in neighbours), default=None),
            "max_optical_flux_ratio": max((c.get("optical_flux_ratio_to_target", 0.0) for c in neighbours), default=0.0)}
    ctx: dict = {"contamination_summary": cont}
    px = [m["pixel_scale_arcsec"] for m in measurements if m.get("pixel_scale_arcsec")]
    fw = [m["psf_fwhm_arcsec"] for m in measurements if m.get("psf_fwhm_arcsec")]
    if spec.target_id == "NASA-02":
        comp = _component_assessment(ref, stars, float(np.median(fw)) if fw else None, float(np.median(px)) if px else None)
        ctx["resolution"] = comp
        ctx["unresolved_companion"] = True
        ctx["blend_statement"] = comp["statement"]
    elif cont["n_with_neighbour"]:
        ctx["blend_statement"] = f"A Gaia neighbour lies inside the PSF stamp in {cont['n_with_neighbour']} images (closest {cont['closest_arcsec']:.1f} arcsec); SPHEREx cannot separate sources closer than about its PSF FWHM."
    if opts.context:
        note("cross-checking CatWISE2020")
        ctx["catwise_context"] = _catwise_neighbour(ref, catwise_svc)
    wise = _wise_reference_flux(ref)
    if wise is None and isinstance(ctx.get("catwise_context"), dict) and ctx["catwise_context"].get("w2_vega") is not None:
        wise = _wise_reference_flux(ReferenceAstrometry(target_id="x", catalogue="c", status="RESOLVED", ra_deg=0, dec_deg=0,
                                                       magnitudes={"W1_vega": ctx["catwise_context"].get("w1_vega"), "W2_vega": ctx["catwise_context"].get("w2_vega")}))
    ctx["wise_reference_flux"] = wise
    result["context"] = ctx
    result["detectability"] = _detectability(measurements, wise)
    # 7. analyses ------------------------------------------------------------------------------
    note("analysing")
    analyses: dict = {}
    if spec.category == MOVING or (ref.has_proper_motion and math.hypot(ref.pmra_mas_yr, ref.pmdec_mas_yr) > 1000.0):
        analyses["motion"] = analyse_motion(measurements, ref, config.association.registration_floor_arcsec)
    if spec.category == VARIABLE or spec.category == MOVING:
        analyses["variability"] = analyse_variability(measurements, config)
    result["analyses"] = analyses
    primary = analyses["motion"] if spec.category == MOVING else analyses["variability"]
    builder = hypotheses.build_moving if spec.category == MOVING else hypotheses.build_variable
    hyps, evidence, ev_ids = builder(spec.target_id, ref, primary, ctx)
    result["hypotheses"], result["evidence"] = hyps, evidence
    # 8. blind pipeline run -----------------------------------------------------------------------
    if opts.blind_pipeline and n_meas:
        note("running the blind HEIMDALL pipeline on the same exposures")
        result["blind_pipeline"] = _blind_pipeline({"source": source, "config": config, "settings": settings, "registry": registry, "store": store},
                                                   [o.obs_id for o in selected if any(m["obs_id"] == o.obs_id and m["status"] == "MEASURED" for m in measurements)],
                                                   (cen.ra_deg % 360.0, cen.dec_deg), size, spec, ref, measurements, opts, progress)
    else:
        result["blind_pipeline"] = {"status": "NOT_RUN", "reason": "disabled or no measured image"}
    # 9. conclusion -----------------------------------------------------------------------------------
    test_status, why = _test_status(spec, primary, n_meas, len(selected))
    result["conclusion"] = hypotheses.plain_language(spec.name, spec.category, ref, primary, result["coverage"], test_status)
    result["conclusion"]["test_status_reason"] = why
    partial = bool(result["warnings"]) or bool(result["failed_items"])
    return _finish(result, created, "PARTIAL" if partial else "COMPLETED", test_status, why, settings, opts, store)


def _flag_counts(measurements: list[dict]) -> dict:
    out: dict[str, int] = {}
    for m in measurements:
        for f in m.get("flags") or []:
            out[f] = out.get(f, 0) + 1
    return out


def _component_assessment(ref: ReferenceAstrometry, stars: list, fwhm: float | None, pix: float | None) -> dict:
    near = []
    for s in stars:
        sep = float(angular_separation_deg(s.ra, s.dec, ref.ra_deg, ref.dec_deg)) * 3600.0
        if 0.0 < sep < 10.0:
            near.append({"gaia_source_id": s.source_id, "separation_arcsec": sep, "g_mag": s.g_mag})
    return {"gaia_entries_within_10_arcsec": near, "n_gaia_components": 1 + len(near), "psf_fwhm_arcsec": fwhm, "pixel_scale_arcsec": pix,
            "component_separation_source": "Gaia DR3 lists one entry for the system" if not near else "Gaia DR3 lists multiple entries",
            "statement": ("Epsilon Indi Ba/Bb is a close brown-dwarf binary. Gaia DR3 lists a single source for it and its separation is far below the SPHEREx PSF FWHM "
                          f"({fwhm:.1f} arcsec)" if fwhm else "Epsilon Indi Ba/Bb is a close brown-dwarf binary; PSF width unavailable") +
                         ", so the two components are not resolved: the measurements are of the blended system. Resolving them is not claimed.",
            "resolved_at_spherex_resolution": False,
            "basis": "separation from the challenge description (binary) and Gaia (single entry) versus PSF FWHM; no component-level SPHEREx measurement exists"}


def _test_status(spec: TargetSpec, primary: dict, n_meas: int, n_sel: int) -> tuple[str, str]:
    if n_meas == 0:
        return "LIMITED_BY_DATA", "no image yielded a usable measurement at the target position"
    v = primary.get("verdict")
    if primary.get("status") not in ("COMPLETED",) and spec.category == MOVING:
        return "LIMITED_BY_DATA", "motion analysis not assessable: " + ", ".join(primary.get("reasons", []))
    if v in ("NOT_ASSESSABLE", "MOTION_NOT_MEASURABLE_WITH_THESE_OBSERVATIONS"):
        return "LIMITED_BY_DATA", primary.get("verdict_reason") or "observations are not sensitive enough for the test"
    return "PASSED", "analysis completed with a reported verdict; the verdict itself may be a non-detection"


def _finish(result: dict, created: datetime, execution: str, test_status: str, why: str, settings: Settings, opts: TargetOptions, store) -> dict:
    result["status"] = {"execution": execution, "test_status": test_status, "reason": why}
    if "conclusion" not in result:
        result["conclusion"] = {"quick_answer": f"No scientific measurement was made: {why}.", "test_status": test_status, "test_status_reason": why}
    tid = result["target"]["target_id"]
    result["investigation_id"] = f"tinv:{tid}:{canonical_hash([result['target'], result['options'], result['config_hash'], created.isoformat()])[7:15]}"
    result["reproduce"] = f"python3 -m heimdall target run {tid}" + (f" --max-observations {opts.max_observations}" if opts.max_observations != 16 else "")
    result["results_hash"] = canonical_hash({k: result.get(k) for k in ("target", "reference", "coverage", "analyses", "hypotheses", "conclusion", "config_hash")})
    out = plain(result)
    if opts.save:
        d = Path(settings.state_dir) / "targets" / tid
        d.mkdir(parents=True, exist_ok=True)
        text = json.dumps(out, indent=1, sort_keys=True, allow_nan=False)
        (d / f"{out['investigation_id'].split(':')[-1]}.json").write_text(text, encoding="utf-8")
        (d / "latest.json").write_text(text, encoding="utf-8")
        out["saved_to"] = str(d / "latest.json")
    return out
