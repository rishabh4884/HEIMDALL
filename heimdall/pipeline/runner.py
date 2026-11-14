"""The integrated investigation pipeline.

discovery -> selection -> retrieval -> calibration -> ingestion + QC -> detection
-> epochs -> association -> forced photometry -> per-source spectral, temporal
and positional analysis -> transient test and moving-object linking ->
validation (optional) -> reasoning and Planet X evaluation -> outputs

Every stage records a ``ComputationStatus`` (did it run?) separately from any
scientific validity. Item failures (one product, one image) are recorded as
failed items and never stop unrelated work. A run's result is a plain JSON
document (schema ``heimdall.investigation/1.1``) plus an event log.
"""

from __future__ import annotations

import dataclasses
import json
import math
import platform
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from scipy import stats as _st

from .. import SCHEMA_VERSION, __version__
from ..config import Settings
from ..contracts import (Association, CandidateKind, DataOrigin, ErrorCode, ErrorInfo, FailedItem, HeimdallError,
                         ResultOutcome, ValueClass)
from ..data_access.interface import DataSource, DiscoveryQuery, RetrievalRequest, SkyRegion
from ..engine import linking as linking_mod
from ..engine import investigation as investigation_mod
from ..engine import planetx, reasoning, validation
from ..engine.association import ASSOCIATION_METHOD_ID, ASSOCIATION_METHOD_VERSION, associate
from ..engine.config import SATURATION_FLAGS, EngineConfig
from ..engine.detection import DETECTION_METHOD_ID, DETECTION_METHOD_VERSION, detect, forced_photometry, prepare
from ..engine.ephemeris import observer_heliocentric_au
from ..engine.methods import derived_quantity
from ..engine.motion import MotionFit, ParallaxFit, astrometric_error_model, motion_test, parallax_fit
from ..engine.skygeom import covariance_ellipse
from ..engine.spectral import assemble_spectrum, compare_point_sets
from ..engine.stats import bonferroni
from ..engine.temporal import SeriesPoint, build_epochs, build_series, iso_utc, photometric_error_model, variability_test
from ..context import CachedService
from ..context.gaia import GaiaCatalogue, match_position
from ..context.small_bodies import (SBIDENT_URL, SmallBodyIdentifier, confirm_across_subjects, match_detections,
                                    subject_status)
from ..engine.skygeom import offsets_arcsec
from ..engine.stats import chi2_significance
from ..ingest.spherex import read_spherex_mef
from .interfaces import ComputationStatus
from .results import INVESTIGATION_SCHEMA, canonical_hash, plain, scientific_core

PIPELINE_VERSION = "1.0.0"
STAGES = ("discovery", "selection", "retrieval", "calibration", "ingestion", "detection", "epochs", "association",
          "photometry", "source_analysis", "transients_and_linking", "context", "validation", "reasoning", "investigation", "outputs")
GLOBAL_UNAVAILABLE = [
    {"analysis": "spectral_anomaly_candidates", "state": "NOT_IMPLEMENTED",
     "reason": "no validated spectral template library or reference population to define a spectral anomaly"},
]


def unavailable_analyses(context: dict, validation_runs: list) -> list[dict]:
    """Analyses that did not run in this investigation, with the actual reason (never a fixed list)."""
    out = list(GLOBAL_UNAVAILABLE)
    gaia, sb = context.get("gaia") or {}, context.get("small_bodies") or {}
    if gaia.get("status") != "COMPLETED":
        out.append({"analysis": "reference_star_catalogue", "state": "NOT_ASSESSABLE",
                    "reason": context.get("reason") or f"Gaia DR3 query {gaia.get('status')}: {gaia.get('reason')}"})
    incomplete = sum(n for s, n in (sb.get("status_counts") or {}).items() if s != "COMPLETED")
    if context.get("reason") or incomplete:
        out.append({"analysis": "known_solar_system_object_check", "state": "NOT_ASSESSABLE" if context.get("reason") else "PARTIAL",
                    "reason": context.get("reason") or f"{incomplete} of {sb.get('n_queries')} JPL SB Ident queries did not complete"})
    if not any(getattr(v, "procedure", None) == "moving_object_injection" and v.outcome != "NOT_ASSESSABLE" for v in validation_runs):
        out.append({"analysis": "moving_object_completeness", "state": "NOT_ASSESSABLE",
                    "reason": "moving-object injection was not run on these images; non-detections are not evidence of absence"})
    return out


@dataclass
class InvestigationRequest:
    ra_deg: float
    dec_deg: float
    radius_deg: float
    source_id: str
    detectors: list[int] | None = None
    collections: list[str] | None = None
    mjd_start: float | None = None
    mjd_end: float | None = None
    max_observations: int | None = None
    cutout_size_deg: float | None = None
    run_validation: bool = False
    calibration: str = "auto"  # auto | none
    label: str | None = None
    local_root: str | None = None  # directory for the local-fits source (recorded for reproduction)
    context: str = "auto"  # auto: query Gaia DR3 and JPL SB Ident for real data | none
    calibration_products: str = "epsf"  # epsf | full (adds the CWAVE/CBAND spectral WCS and the solid-angle map)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> InvestigationRequest:
        return cls(**data)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class InvestigationPipeline:
    def __init__(self, source: DataSource, config: EngineConfig, settings: Settings,
                 registry: validation.ValidationRegistry | None = None, progress=None) -> None:
        self.source = source
        self.config = config
        self.settings = settings
        self.registry = registry or validation.ValidationRegistry(None)
        self.progress = progress
        self.events: list[dict] = []
        self.stages: list[dict] = []
        self._t0: dict[str, float] = {}

    # -- bookkeeping -------------------------------------------------------------

    def _event(self, etype: str, stage: str, message: str, subject: str | None = None) -> None:
        self.events.append({"seq": len(self.events), "time": _now(), "type": etype, "stage": stage, "subject": subject,
                            "message": message})
        if self.progress:
            self.progress(stage, message)

    def _start(self, name: str) -> dict:
        rec = {"stage": name, "status": None, "partial": False, "started_at": _now(), "duration_s": None, "counts": {},
               "reasons": [], "error": None}
        self.stages.append(rec)
        self._t0[name] = time.perf_counter()
        self._event("STAGE_STARTED", name, f"{name} started")
        return rec

    def _end(self, rec: dict, status: ComputationStatus, reasons=(), error: ErrorInfo | None = None, partial=False, **counts) -> None:
        rec["status"] = status.value
        rec["partial"] = bool(partial)
        rec["reasons"] = list(reasons)
        rec["error"] = plain(error) if error else None
        rec["counts"] = counts
        rec["duration_s"] = round(time.perf_counter() - self._t0[rec["stage"]], 3)
        etype = "STAGE_FAILED" if status is ComputationStatus.FAILED else "STAGE_COMPLETED"
        detail = ", ".join(f"{k}={v}" for k, v in counts.items())
        self._event(etype, rec["stage"], f"{rec['stage']}: {status.value}{' (partial)' if partial else ''} {detail}".strip())

    def _skip_remaining(self, reason: str) -> None:
        done = {s["stage"] for s in self.stages}
        for name in STAGES:
            if name not in done and name != "outputs":
                rec = self._start(name)
                self._end(rec, ComputationStatus.INSUFFICIENT_INPUTS, [reason])

    # -- main ------------------------------------------------------------------

    def run(self, req: InvestigationRequest) -> dict:
        cfg = self.config
        started = datetime.now(UTC)
        run_id = "R" + started.strftime("%Y%m%dT%H%M%S") + "-" + canonical_hash([req.to_dict(), cfg.hash, _now()])[7:15]
        region = SkyRegion(ra_deg=req.ra_deg % 360.0, dec_deg=req.dec_deg, radius_deg=req.radius_deg)
        res: dict = {"schema": INVESTIGATION_SCHEMA, "schema_version": SCHEMA_VERSION, "run_id": run_id,
                     "created_at": started.isoformat(), "request": req.to_dict(), "config_hash": cfg.hash,
                     "config": cfg.to_dict(), "data_origin": None, "observations": [], "failed_items": [], "warnings": [],
                     "epochs": [], "detections": [], "rejected_detections": [], "sources": [], "associations": [],
                     "transients": [], "tracks": [], "linking": None, "candidates": [], "planet_x": None,
                     "validation": {"requested": req.run_validation, "runs": []}, "playback": None,
                     "unavailable_analyses": unavailable_analyses({"reason": "the investigation ended before the context stage"}, []),
                     "look_elsewhere": {}}
        failed: list[FailedItem] = []

        # discovery ---------------------------------------------------------------
        rec = self._start("discovery")
        filters = {}
        if req.detectors:
            filters["detectors"] = ",".join(str(int(d)) for d in req.detectors)
        if req.collections:
            filters["collections"] = ",".join(req.collections)
        if req.mjd_start is not None:
            filters["mjd_start"] = repr(float(req.mjd_start))
        if req.mjd_end is not None:
            filters["mjd_end"] = repr(float(req.mjd_end))
        try:
            disc = self.source.discover(DiscoveryQuery(region=region, filters=filters))
        except HeimdallError as exc:
            self._end(rec, ComputationStatus.FAILED, [exc.info.message], error=exc.info)
            self._skip_remaining("discovery failed")
            return self._finish(res, failed, "FAILED", None, error=exc.info)
        failed.extend(disc.failed_items)
        if disc.outcome is ResultOutcome.NO_OBSERVATIONS:
            self._end(rec, ComputationStatus.COMPLETED, n_observations=0)
            self._skip_remaining("no observations cover the region")
            return self._finish(res, failed, "COMPLETED", ResultOutcome.NO_OBSERVATIONS)
        self._end(rec, ComputationStatus.COMPLETED, partial=disc.outcome is ResultOutcome.PARTIAL,
                  n_observations=len(disc.observations), n_failed=len(disc.failed_items))

        # selection ---------------------------------------------------------------
        rec = self._start("selection")

        def obs_time(o):
            for key in ("mjd_avg_utc", "time_start_mjd"):
                v = o.metadata.get(key)
                if v is not None and v.is_available:
                    return float(v.value)
            return math.inf

        ordered = sorted(disc.observations, key=lambda o: (obs_time(o), o.obs_id))
        selected = ordered
        not_selected = []
        if req.max_observations and len(ordered) > req.max_observations:
            idx = sorted({int(i) for i in np.round(np.linspace(0, len(ordered) - 1, req.max_observations))})
            selected = [ordered[i] for i in idx]
            not_selected = [o.obs_id for i, o in enumerate(ordered) if i not in set(idx)]
        self._end(rec, ComputationStatus.COMPLETED, n_selected=len(selected), n_not_selected=len(not_selected),
                  rule="time-ordered, evenly spaced" if not_selected else "all")

        # retrieval ---------------------------------------------------------------
        rec = self._start("retrieval")
        retrieved = {}
        for o in selected:
            prod = o.provenance.products[0] if o.provenance.products else None
            options = {}
            if prod is not None:
                if prod.access_reference:
                    options["access_reference"] = prod.access_reference
                if prod.release:
                    options["release"] = prod.release
                if prod.product_version:
                    options["product_version"] = prod.product_version
                if prod.checksum:
                    options["archive_checksum"] = f"{prod.checksum_algorithm}:{prod.checksum}"
            if req.cutout_size_deg:
                options["cutout_size_deg"] = repr(float(req.cutout_size_deg))
            try:
                rr = self.source.retrieve(RetrievalRequest(obs_id=o.obs_id, region=region, options=options))
            except HeimdallError as exc:
                failed.append(FailedItem(ref=o.obs_id, error=exc.info))
                self._event("ITEM_FAILED", "retrieval", f"{exc.code}: {exc.info.message}", o.obs_id)
                continue
            retrieved[o.obs_id] = (o, rr)
        if not retrieved:
            err = ErrorInfo(code=ErrorCode.RETRIEVAL_FAILED, message="no product could be retrieved")
            self._end(rec, ComputationStatus.FAILED, [err.message], error=err)
            self._skip_remaining("no products retrieved")
            return self._finish(res, failed, "FAILED", None, error=err)
        n_cached = sum(1 for _, rr in retrieved.values() if rr.provenance.origin is DataOrigin.CACHED)
        self._end(rec, ComputationStatus.COMPLETED, partial=len(retrieved) < len(selected), n_retrieved=len(retrieved),
                  n_from_cache=n_cached, n_failed=len(selected) - len(retrieved))

        origins = {rr.provenance.origin for _, rr in retrieved.values()}
        synthetic = DataOrigin.SYNTHETIC_TEST_FIXTURE in origins
        if synthetic and len(origins) > 1:
            raise HeimdallError.of(ErrorCode.INTERNAL, "synthetic and real products cannot be mixed in one run")
        origin_class = "SYNTHETIC" if synthetic else "REAL"
        res["data_origin"] = origin_class

        # calibration -------------------------------------------------------------
        rec = self._start("calibration")
        epsf: dict[int, Path] = {}
        calibrations = []
        detectors = sorted({int(o.metadata["detector"].value) for o, _ in retrieved.values()
                            if "detector" in o.metadata and o.metadata["detector"].is_available})
        spectral_cal: dict[int, Path] = {}
        solid_angle_cal: dict[int, Path] = {}
        if req.calibration == "auto" and hasattr(self.source, "retrieve_calibration"):
            kinds = [("epsf", epsf)] + ([("spectral_wcs", spectral_cal), ("solid_angle", solid_angle_cal)]
                                        if req.calibration_products == "full" else [])
            for det in detectors:
                for kind, store in kinds:
                    try:
                        path, product = self.source.retrieve_calibration(kind, det)
                        store[det] = path
                        calibrations.append(product)
                    except HeimdallError as exc:
                        res["warnings"].append(f"{kind} calibration for detector {det} unavailable ({exc.code}); fallback used")
            image_releases = {str(o.metadata["release"].value) for o, _ in retrieved.values() if "release" in o.metadata}
            for product in calibrations:
                cal_release = (product.release or "").replace("_calibration", "")
                if product.release and "qr2" in cal_release and any("qr3" in r for r in image_releases):
                    res["warnings"].append(f"{product.product_id} ({product.release}) is applied to QR3 images; no QR3 version is configured")
            self._end(rec, ComputationStatus.COMPLETED, partial=len(epsf) < len(detectors), n_epsf=len(epsf),
                      n_spectral_wcs=len(spectral_cal), n_solid_angle=len(solid_angle_cal))
        else:
            self._end(rec, ComputationStatus.NOT_APPLICABLE,
                      ["calibration disabled" if req.calibration != "auto" else "source provides no calibration products"])

        # ingestion ---------------------------------------------------------------
        rec = self._start("ingestion")
        images, qc_by_obs, obs_entries = {}, {}, {}
        for obs_id, (o, rr) in retrieved.items():
            product = rr.products[0]
            det = int(o.metadata["detector"].value) if "detector" in o.metadata and o.metadata["detector"].is_available else None
            release = product.product.release or (str(o.metadata["release"].value) if "release" in o.metadata else None)
            try:
                ing = read_spherex_mef(product.location, source_id=self.source.source_id, provenance=rr.provenance, config=cfg,
                                       release=release or ("SYNTHETIC" if synthetic else None), obs_id=obs_id,
                                       crop=(region.ra_deg, region.dec_deg, region.radius_deg),
                                       epsf_calibration=epsf.get(det), spectral_wcs_calibration=spectral_cal.get(det),
                                       solid_angle_calibration=solid_angle_cal.get(det))
            except Exception as exc:  # noqa: BLE001 - one unreadable product must not stop the run
                info = ErrorInfo(code=ErrorCode.DATA_MALFORMED, message=f"ingestion failed: {type(exc).__name__}: {exc}")
                failed.append(FailedItem(ref=obs_id, error=info))
                self._event("ITEM_FAILED", "ingestion", info.message, obs_id)
                continue
            qc_by_obs[obs_id] = ing.qc
            entry = {"obs_id": obs_id, "observation": ing.observation or o, "product_checksum": product.product.checksum,
                     "product_access_reference": product.product.access_reference, "retrieval_origin": rr.provenance.origin,
                     "included": ing.image is not None, "exclusion_reasons": ing.qc.exclusion_reasons, "qc": ing.qc}
            if ing.image is not None:
                img = ing.image
                m = img.meta
                entry["meta"] = {k: getattr(m, k) for k in ("detector", "release", "pipeline_version", "mjd_avg", "exposure_s",
                                                            "finast", "dqa", "psf", "wavelength_source", "solid_angle_source",
                                                            "pixel_scale_arcsec", "full_shape", "window", "parent_offset",
                                                            "synthetic", "calibration_ids")}
                entry["footprint"] = dict(zip(("ra", "dec"), img.footprint()))
                images[obs_id] = img
            else:
                self._event("ITEM_EXCLUDED", "ingestion", f"excluded: {', '.join(ing.qc.exclusion_reasons)}", obs_id)
            obs_entries[obs_id] = entry
        if not images:
            self._end(rec, ComputationStatus.INSUFFICIENT_INPUTS, ["no product passed quality control"],
                      n_ingested=len(obs_entries), n_usable=0)
            res["observations"] = list(obs_entries.values())
            self._skip_remaining("no usable images")
            return self._finish(res, failed, "COMPLETED", ResultOutcome.INSUFFICIENT_OBSERVATIONS)
        self._end(rec, ComputationStatus.COMPLETED, partial=len(images) < len(retrieved), n_usable=len(images),
                  n_excluded=len(obs_entries) - len(images))

        # detection ---------------------------------------------------------------
        rec = self._start("detection")
        analyses, runs = {}, {}
        for obs_id, img in sorted(images.items()):
            qc = qc_by_obs[obs_id]
            if not qc.suitability["detection"]:
                obs_entries[obs_id]["detection"] = {"status": "INSUFFICIENT_INPUTS", "reasons": qc.suitability_reasons.get("detection", [])}
                continue
            try:
                an = prepare(img, cfg)
                run = detect(an, cfg)
            except (ValueError, np.linalg.LinAlgError, FloatingPointError) as exc:
                info = ErrorInfo(code=ErrorCode.ANALYSIS_STAGE_FAILED, message=f"detection failed: {exc}")
                failed.append(FailedItem(ref=obs_id, error=info))
                self._event("ITEM_FAILED", "detection", info.message, obs_id)
                continue
            analyses[obs_id], runs[obs_id] = an, run
            obs_entries[obs_id]["detection"] = {k: getattr(run, k) for k in (
                "status", "reasons", "threshold_sigma", "n_peaks", "background", "noise", "nominal_false_positives",
                "nominal_note", "psf", "method_id", "method_version")}
            obs_entries[obs_id]["detection"]["n_accepted"] = len(run.accepted)
            obs_entries[obs_id]["detection"]["n_rejected"] = len(run.rejected)
        all_dets = [d for r in runs.values() for d in r.accepted]
        det_by_id = {d.det_id: d for d in all_dets}
        self._end(rec, ComputationStatus.COMPLETED if runs else ComputationStatus.INSUFFICIENT_INPUTS,
                  [] if runs else ["no image suitable for detection"], partial=len(runs) < len(images),
                  n_images=len(runs), n_detections=len(all_dets),
                  n_rejected=sum(len(r.rejected) for r in runs.values()))

        # epochs ------------------------------------------------------------------
        rec = self._start("epochs")
        epochs, epoch_of = build_epochs([(o, images[o].meta.mjd_avg) for o in analyses], cfg.temporal.epoch_gap_days)
        image_info = {o: {"epoch_index": epoch_of.get(o), "release": images[o].meta.release, "detector": images[o].meta.detector,
                          "mjd": images[o].meta.mjd_avg} for o in analyses}
        mjd_obs = [o for o in analyses if images[o].meta.mjd_avg is not None]
        observer = {}
        if mjd_obs:
            sc = [images[o].meta.spacecraft_geocentric_km or (math.nan, math.nan, math.nan) for o in mjd_obs]
            pos = observer_heliocentric_au([images[o].meta.mjd_avg for o in mjd_obs], sc)
            observer = {o: pos[i] for i, o in enumerate(mjd_obs)}
        self._end(rec, ComputationStatus.COMPLETED, n_epochs=len(epochs), gap_days=cfg.temporal.epoch_gap_days)

        # association -------------------------------------------------------------
        rec = self._start("association")
        clusters = associate(all_dets, cfg.association)
        cluster_of = {d: c.source_id for c in clusters for d in c.det_ids}
        n_amb = sum(c.ambiguous for c in clusters)
        self._end(rec, ComputationStatus.COMPLETED if all_dets else ComputationStatus.NOT_APPLICABLE,
                  [] if all_dets else ["no detections to associate"], n_sources=len(clusters), n_ambiguous=n_amb)

        # forced photometry -------------------------------------------------------
        rec = self._start("photometry")
        forced = {}
        for c in clusters:
            forced[c.source_id] = [forced_photometry(analyses[o], c.ra, c.dec, cfg, runs[o].accepted)
                                   for o in sorted(analyses) if analyses[o].image.contains(c.ra, c.dec)]
        n_meas = sum(1 for v in forced.values() for f in v if f.status == "MEASURED")
        self._end(rec, ComputationStatus.COMPLETED if clusters else ComputationStatus.NOT_APPLICABLE,
                  [] if clusters else ["no sources"], n_measurements=n_meas)

        # per-source analysis -------------------------------------------------------
        rec = self._start("source_analysis")
        floor2 = cfg.association.registration_floor_arcsec ** 2
        source_out, source_ctx = [], {}
        for c in clusters:
            dets = [det_by_id[d] for d in c.det_ids]
            det_obs = {d.obs_id: d for d in dets}
            points = []
            for f in forced[c.source_id]:
                info = image_info[f.obs_id]
                sat = any(fl in SATURATION_FLAGS for fl in f.peak_flags)
                warn = [fl for fl in f.peak_flags if fl in cfg.quality.warning_flags and fl not in SATURATION_FLAGS]
                poor = f.chi2_red is not None and f.chi2_red > cfg.quality.grade_max_chi2_red
                photometric = qc_by_obs[f.obs_id].suitability["photometry"]
                reasons = (list(f.reasons) + (["SATURATION_FLAG_AT_PEAK"] if sat else []) + [f"WARNING_FLAG_AT_PEAK:{w}" for w in warn]
                           + (["POOR_PSF_FIT"] if poor else []) + ([] if photometric else ["IMAGE_UNSUITABLE_FOR_PHOTOMETRY"]))
                d = det_obs.get(f.obs_id)
                points.append(SeriesPoint(
                    obs_id=f.obs_id, mjd=f.mjd, epoch_index=info["epoch_index"], detector=f.detector, release=info["release"],
                    pipeline_version=images[f.obs_id].meta.pipeline_version, wavelength_um=f.wavelength_um,
                    bandwidth_um=f.bandwidth_um, wavelength_source=f.wavelength_source, flux_ujy=f.flux_ujy,
                    flux_sigma_ujy=f.flux_sigma_ujy, measurement="FORCED", det_id=d.det_id if d else None,
                    quality_grade=d.quality_grade if d else None, peak_flags=f.peak_flags,
                    usable=f.status == "MEASURED" and not sat and not warn and not poor and photometric, reasons=reasons))
            spectrum = assemble_spectrum(c.source_id, points)
            series = build_series(points, cfg.temporal.wavelength_tolerance_fraction, c.source_id)
            var = [variability_test(s, cfg.temporal) for s in series]
            by_epoch: dict[int, list[SeriesPoint]] = {}
            for p in points:
                if p.epoch_index is not None:
                    by_epoch.setdefault(p.epoch_index, []).append(p)
            ep = sorted(by_epoch)
            comparisons = [compare_point_sets(f"epoch {ep[0]} vs epoch {e}", by_epoch[ep[0]], by_epoch[e],
                                              cfg.temporal.wavelength_tolerance_fraction) for e in ep[1:]]
            astro = [d for d in dets if d.astrometry_usable and d.mjd is not None and qc_by_obs[d.obs_id].suitability["astrometry"]]
            covs = [d.cov() + floor2 * np.eye(2) for d in astro]
            mt = motion_test([d.ra for d in astro], [d.dec for d in astro], covs, [d.mjd for d in astro],
                             min_points=cfg.temporal.min_points_motion)
            n_ep = len({image_info[d.obs_id]["epoch_index"] for d in dets})
            if len(astro) >= 3 and n_ep >= cfg.planetx.min_epoch_clusters_for_parallax and all(d.obs_id in observer for d in astro):
                plx = parallax_fit([d.ra for d in astro], [d.dec for d in astro], covs, [d.mjd for d in astro],
                                   np.array([observer[d.obs_id] for d in astro]))
            else:
                plx = ParallaxFit(status="INSUFFICIENT_INPUTS", n_points=len(astro),
                                  reasons=[f"needs >= 3 astrometric points in >= {cfg.planetx.min_epoch_clusters_for_parallax} epochs"])
            nn = [d.nearest_neighbour_px for d in dets if d.nearest_neighbour_px is not None]
            a, b, pa = covariance_ellipse(np.asarray(c.cov_arcsec2))
            analyses_state = {
                "spectral": {"status": "COMPLETED" if any(p.usable for p in points) else "INSUFFICIENT_INPUTS",
                             "reasons": [] if any(p.usable for p in points) else ["NO_USABLE_PHOTOMETRY"]},
                "variability": {"status": "COMPLETED" if any(v.status == "COMPLETED" for v in var) else "INSUFFICIENT_INPUTS",
                                "reasons": sorted({r for v in var for r in v.reasons}) if not any(v.status == "COMPLETED" for v in var)
                                else [] if var else ["NO_COMPARABLE_SERIES"]},
                "motion": {"status": mt.status, "reasons": mt.reasons},
                "parallax": {"status": plx.status, "reasons": plx.reasons},
            }
            if analyses_state["variability"]["status"] != "COMPLETED" and not analyses_state["variability"]["reasons"]:
                analyses_state["variability"]["reasons"] = ["NO_COMPARABLE_SERIES"]
            out = {"source_id": c.source_id, "ra": c.ra, "dec": c.dec, "cov_arcsec2": c.cov_arcsec2,
                   "ellipse_1sigma": {"a_arcsec": a, "b_arcsec": b, "pa_deg": pa}, "n_obs": c.n_obs, "n_epochs": n_ep,
                   "det_ids": c.det_ids, "obs_ids": c.obs_ids, "ambiguous": c.ambiguous, "ambiguity_kinds": c.ambiguity_kinds,
                   "alternative_groupings": c.alternative_groupings, "released_from": c.released_from,
                   "worst_detection_grade": reasoning.worst_grade(dets), "spectrum": spectrum,
                   "series": [{k: v for k, v in asdict(s).items() if k != "points"} | {"obs_ids": [p.obs_id for p in s.points]} for s in series],
                   "variability": var, "spectral_comparisons": comparisons, "motion": mt, "parallax": plx,
                   "analyses": analyses_state, "forced": [f for f in forced[c.source_id] if f.status != "NOT_COVERED"]}
            source_out.append(out)
            source_ctx[c.source_id] = {"cluster": c, "dets": dets, "points": points, "series": series, "var": var, "motion": mt,
                                       "parallax": plx, "n_epochs": n_ep, "min_nn": min(nn) if nn else None}
        groups = [[d for d in source_ctx[c.source_id]["dets"]
                   if d.astrometry_usable and d.mjd is not None and qc_by_obs[d.obs_id].suitability["astrometry"]]
                  for c in clusters if not {"CONFLICT", "RELEASED_FROM_CONFLICT"} & set(c.ambiguity_kinds)]
        astro_model = astrometric_error_model(groups, cfg.association.registration_floor_arcsec)
        res["astrometric_error_model"] = astro_model
        photo_model = photometric_error_model([v for sc in source_ctx.values() for v in sc["var"]], seed=cfg.validation.seed)
        res["photometric_error_model"] = photo_model
        self._end(rec, ComputationStatus.COMPLETED if clusters else ComputationStatus.NOT_APPLICABLE,
                  [] if clusters else ["no sources"], n_sources=len(clusters),
                  n_variability_tested=sum(1 for s in source_ctx.values() for v in s["var"] if v.status == "COMPLETED"),
                  n_motion_tested=sum(1 for s in source_ctx.values() if s["motion"].status == "COMPLETED"),
                  astrometric_error_scale=round(astro_model.scale, 3) if astro_model.scale else None)

        # transients and linking ---------------------------------------------------
        rec = self._start("transients_and_linking")
        image_release = {o: image_info[o]["release"] for o in image_info}
        transient_tests, link_inputs, not_linked = {}, [], {}
        for c in clusters:
            ctx = source_ctx[c.source_id]
            if ctx["n_epochs"] != 1:
                continue
            t = linking_mod.transient_test(c, ctx["dets"], forced[c.source_id], image_release, cfg)
            transient_tests[c.source_id] = t
            if t.status == "TRANSIENT_LIKE":
                eligible, why = linking_mod.tracklet_eligible(ctx["dets"], cfg)
                if eligible:
                    link_inputs.append((c, ctx["dets"], image_info[ctx["dets"][0].obs_id]["epoch_index"]))
                else:
                    not_linked[c.source_id] = why
        link = linking_mod.link_moving(link_inputs, observer, cfg)
        in_track = {s: t.track_id for t in link.tracks for s in t.source_ids}
        if link.truncated:
            res["warnings"].append(f"moving-object linking was truncated after {link.n_evaluations} evaluations: the track list is incomplete")
        self._end(rec, ComputationStatus.COMPLETED, partial=link.truncated, n_transient_tested=len(transient_tests),
                  n_transient_like=sum(1 for t in transient_tests.values() if t.status == "TRANSIENT_LIKE"),
                  n_link_inputs=len(link_inputs), n_tracks=len(link.tracks), truncated=link.truncated)

        # context: external catalogues ------------------------------------------------
        rec = self._start("context")
        context: dict = {"gaia": None, "small_bodies": None, "registration_vs_gaia": []}
        gaia_match: dict[str, dict] = {}
        known: dict[str, dict] = {}
        sb_queries, sb_matches = {}, []
        watch = cfg.significance.watch_sigma
        # Known-object queries are needed only where a small body could be the explanation: transients, tracks and
        # movers seen within one epoch. A source fixed within arcseconds across epochs cannot be a small body.
        subjects = set(in_track) | {s for s, t in transient_tests.items() if t.status == "TRANSIENT_LIKE"} | {
            c.source_id for c in clusters if source_ctx[c.source_id]["motion"].status == "COMPLETED"
            and source_ctx[c.source_id]["n_epochs"] == 1
            and source_ctx[c.source_id]["motion"].gof_stationary.sigma_equivalent >= watch}
        context_reason = ("context disabled for this run" if req.context == "none" else
                          "synthetic scene: real catalogues and ephemerides do not describe it" if synthetic else None)
        if context_reason:
            self._end(rec, ComputationStatus.NOT_APPLICABLE, [context_reason])
        else:
            cp = cfg.context
            gq = GaiaCatalogue(CachedService("gaia", self.settings, min_interval_s=cp.sb_min_interval_s)).cone(
                region.ra_deg, region.dec_deg, region.radius_deg + 0.02, cp.gaia_g_max)
            stars = {s.source_id: s for s in gq.stars}
            if gq.status == "COMPLETED":
                for c in clusters:
                    mj = [d.mjd for d in source_ctx[c.source_id]["dets"] if d.mjd is not None]
                    gaia_match[c.source_id] = match_position(c.ra, c.dec, c.cov_arcsec2, float(np.mean(mj)), gq.stars, floor2 ** 0.5,
                                                             cp.gate_probability) if mj else {"status": "NOT_ASSESSABLE", "reason": "no time"}
                reg: dict[str, list] = {}
                for c in clusters:
                    gm = gaia_match.get(c.source_id, {})
                    if gm.get("status") != "MATCH" or gm.get("n_within_gate") != 1:
                        continue
                    for d in source_ctx[c.source_id]["dets"]:
                        if d.astrometry_usable and d.mjd is not None and d.snr_fit >= 10:
                            sra, sdec, _ = stars[gm["gaia_source_id"]].at_epoch(d.mjd)
                            reg.setdefault(d.obs_id, []).append(offsets_arcsec(d.ra, d.dec, sra, sdec))
                for o in sorted(reg):
                    a = np.array(reg[o])
                    if len(a) >= 3:
                        m, se = a.mean(axis=0), a.std(axis=0, ddof=1) / math.sqrt(len(a))
                        z2 = float(np.sum((m / np.where(se > 0, se, np.inf)) ** 2))
                        context["registration_vs_gaia"].append({"obs_id": o, "n_stars": len(a), "mean_offset_arcsec": m.tolist(),
                                                                "se_arcsec": se.tolist(), "significance": chi2_significance(z2, 2)})
            context["gaia"] = {"service": gq.service, "status": gq.status, "reason": gq.reason, "from_cache": gq.from_cache,
                               "n_stars": gq.n_stars, "g_max": gq.g_max, "region": gq.region,
                               "n_sources_matched": sum(1 for m in gaia_match.values() if m.get("status") == "MATCH"),
                               "stars": [{"source_id": s.source_id, "ra": s.ra, "dec": s.dec, "ref_epoch": s.ref_epoch,
                                          "g_mag": s.g_mag, "pmra": s.pmra, "pmdec": s.pmdec, "parallax": s.parallax}
                                         for s in gq.stars]}
            sbi = SmallBodyIdentifier(CachedService("jpl_sbident", self.settings, min_interval_s=cp.sb_min_interval_s),
                                      vmag_limit=cp.sb_vmag_limit)
            for o in sorted({d.obs_id for s in subjects for d in source_ctx[s]["dets"]}):
                img = images[o]
                x0, x1, y0, y1 = img.window
                cra, cdec = (float(v) for v in img.pixel_to_world((x0 + x1 - 1) / 2.0, (y0 + y1 - 1) / 2.0))
                half = max(x1 - x0, y1 - y0) * img.meta.pixel_scale_arcsec / 7200.0 + 0.01
                state = None
                if img.meta.spacecraft_geocentric_km:
                    state = tuple(img.meta.spacecraft_geocentric_km) + tuple(img.meta.spacecraft_velocity_km_s or ())
                q = sbi.field(o, img.meta.mjd_avg, cra, cdec, half, state)
                sb_queries[o] = q
                if q.status == "COMPLETED":
                    sb_matches += match_detections(runs[o].accepted, q, floor2 ** 0.5, cp.sb_assumed_ephemeris_error_arcsec,
                                                   cp.gate_probability)
            for s in subjects:
                dets_s = source_ctx[s]["dets"]
                ids = {d.det_id for d in dets_s}
                known[s] = subject_status([d.obs_id for d in dets_s], [m for m in sb_matches if m["det_id"] in ids], sb_queries)
            counts: dict[str, int] = {}
            for q in sb_queries.values():
                counts[q.status] = counts.get(q.status, 0) + 1
            context["small_bodies"] = {
                "service": SBIDENT_URL, "n_queries": len(sb_queries), "status_counts": counts, "matches": sb_matches,
                "queries": [{"obs_id": q.obs_id, "status": q.status, "reason": q.reason, "from_cache": q.from_cache,
                             "n_predicted": len(q.predictions), "api_version": q.api_version, "params": q.params,
                             "mjd": images[q.obs_id].meta.mjd_avg,
                             "predictions": [{"name": p.name, "ra": p.ra, "dec": p.dec, "vmag": p.vmag} for p in q.predictions]}
                            for q in sb_queries.values()]}
            partial = gq.status != "COMPLETED" or any(q.status != "COMPLETED" for q in sb_queries.values())
            self._end(rec, ComputationStatus.COMPLETED, partial=partial, gaia=gq.status, n_gaia_stars=gq.n_stars,
                      n_sources_matched=context["gaia"]["n_sources_matched"], n_sb_queries=len(sb_queries),
                      n_sb_completed=counts.get("COMPLETED", 0), n_known_object_matches=len(sb_matches))
        context["reason"] = context_reason
        res["context"] = context

        def known_for(det_list, stationary_cluster=False):
            mjds = [d.mjd for d in det_list if d.mjd is not None]
            epochs_ = {image_info[d.obs_id]["epoch_index"] for d in det_list if d.obs_id in image_info}
            if stationary_cluster and len(epochs_) >= 2 and mjds and max(mjds) - min(mjds) >= cfg.temporal.epoch_gap_days:
                return {"status": "EXCLUDED_BY_MOTION", "reason": f"detected at one position (within the association gate) over "
                        f"{max(mjds) - min(mjds):.0f} days; any known small body moves far more (even at 1000 AU the parallactic "
                        "motion is several arcsec per day)"}
            if context_reason:
                return {"status": "NOT_ASSESSABLE", "reason": context_reason}
            ids = {d.det_id for d in det_list}
            return confirm_across_subjects(
                subject_status([d.obs_id for d in det_list], [m for m in sb_matches if m["det_id"] in ids], sb_queries), sb_matches)

        def catalogue_for(sid):
            if context_reason:
                return {"status": "NOT_ASSESSABLE", "reason": context_reason}
            if (context.get("gaia") or {}).get("status") != "COMPLETED":
                return {"status": "NOT_ASSESSABLE", "reason": f"Gaia query {context['gaia']['status']}: {context['gaia']['reason']}"}
            return gaia_match.get(sid, {"status": "NOT_ASSESSABLE", "reason": "not matched"})

        quasi_stationary = None
        if (context.get("gaia") or {}).get("status") == "COMPLETED":
            quasi_stationary = {s for s, m in gaia_match.items() if m.get("status") == "MATCH" and m.get("n_within_gate") == 1
                                and None not in (m["pm_mas_yr"] or [None])
                                and math.hypot(*m["pm_mas_yr"]) < cfg.context.quasi_stationary_pm_mas_yr}

        # validation ---------------------------------------------------------------
        rec = self._start("validation")
        releases = sorted({image_info[o]["release"] for o in image_info})
        release_label = releases[0] if len(releases) == 1 else "MIXED"
        scope = {"origin": origin_class, "release": release_label, "config_hash": cfg.hash,
                 "n_images": len(analyses), "obs_ids": sorted(analyses), "region": [region.ra_deg, region.dec_deg, region.radius_deg]}
        if req.run_validation and analyses:
            vruns = []
            for name, fn in (("self_checks", lambda: validation.self_check_run(scope)),
                             ("injection_recovery", lambda: validation.injection_recovery(analyses, runs, cfg, scope)),
                             ("null_detection", lambda: validation.null_detection(analyses, cfg, scope)),
                             ("null_variability", lambda: validation.null_variability(analyses, runs, clusters, image_info, cfg, scope)),
                             ("null_motion", lambda: validation.null_motion(clusters, det_by_id, image_info, cfg, scope, set(in_track),
                                                                             include_only=quasi_stationary)),
                             ("noise", lambda: validation.noise_summary(runs, cfg, scope)),
                             ("moving_object_injection", lambda: validation.moving_object_injection(
                                 analyses, runs, image_info, observer, cfg, scope,
                                 n_experiments=cfg.validation.moving_object_experiments,
                                 movers_per_experiment=cfg.validation.moving_object_bodies_per_experiment)),
                             ("psf_mismatch", lambda: validation.psf_mismatch(analyses, runs, cfg, scope)),
                             ("robustness", lambda: validation.robustness(_subset(analyses), runs, cfg, scope)),
                             ("reproducibility", lambda: validation.reproducibility(_subset(analyses), runs, cfg, scope))):
                try:
                    vr = fn()
                except Exception as exc:  # noqa: BLE001 - a failed procedure is recorded, never treated as passed
                    self._event("ITEM_FAILED", "validation", f"{name} failed: {type(exc).__name__}: {exc}", name)
                    res["warnings"].append(f"validation procedure {name} failed and was not counted")
                    continue
                self.registry.add(vr)
                vruns.append(vr)
                self._event("VALIDATION_RUN", "validation", f"{vr.procedure}: {vr.outcome} (n={vr.sample_size})", vr.run_id)
            res["validation"]["runs"] = vruns
            self._end(rec, ComputationStatus.COMPLETED, n_runs=len(vruns))
        else:
            self._end(rec, ComputationStatus.NOT_APPLICABLE,
                      ["validation not requested (use --validate)"] if not req.run_validation else ["no analysable images"])
        res["validation"]["scope"] = scope

        # reasoning ----------------------------------------------------------------
        rec = self._start("reasoning")
        facts = {o: reasoning.ImageFacts(obs_id=o, finast=images[o].meta.finast, release=images[o].meta.release,
                                         pipeline_version=images[o].meta.pipeline_version,
                                         psf_noise_ratio=runs[o].noise.get("psf_ratio"), pixel_noise_ratio=runs[o].noise.get("pixel_ratio"),
                                         wavelength_source=images[o].meta.wavelength_source,
                                         psf_noise_se=runs[o].noise.get("psf_ratio_se"),
                                         pixel_noise_se=runs[o].noise.get("pixel_ratio_se")) for o in runs}
        err_scale, err_se = astro_model.scale, astro_model.scale_se

        def rescaled(dets_):
            """Stationarity test with covariances multiplied by the empirical error scale squared (only if > 1)."""
            if err_scale is None or err_scale <= 1.0:
                return None
            use = [d for d in dets_ if d.astrometry_usable and d.mjd is not None]
            m = motion_test([d.ra for d in use], [d.dec for d in use],
                            [(d.cov() + floor2 * np.eye(2)) * err_scale ** 2 for d in use], [d.mjd for d in use])
            return m.gof_stationary if m.status == "COMPLETED" else None

        def leave_one_image_out(dets_):
            """Lowest stationarity significance after removing any single image (exposes single-image systematics)."""
            use = [d for d in dets_ if d.astrometry_usable and d.mjd is not None]
            if len(use) < 4:
                return None
            worst = None
            for i in range(len(use)):
                rest = use[:i] + use[i + 1:]
                m = motion_test([d.ra for d in rest], [d.dec for d in rest], [d.cov() + floor2 * np.eye(2) for d in rest],
                                [d.mjd for d in rest])
                if m.status == "COMPLETED" and (worst is None or m.gof_stationary.sigma_equivalent < worst[0].sigma_equivalent):
                    worst = (m.gof_stationary, use[i].obs_id)
            return worst
        s = cfg.significance
        assessments = []
        n_var_tests = n_motion_tests = 0
        f_scale, f_se = photo_model["scale"], photo_model["scale_se"]

        def validity(key):
            return self.registry.validity_for(key, origin_class, release_label, cfg.hash)

        for c in clusters:
            ctx = source_ctx[c.source_id]
            done = [v for v in ctx["var"] if v.status == "COMPLETED"]
            n_var_tests += len(done)
            # Sources detected in a single epoch are assessed by the transient test, not as variables.
            if done and c.source_id not in in_track and ctx["n_epochs"] >= 2:
                best_i = max(range(len(done)), key=lambda i: done[i].significance.sigma_equivalent)
                best = done[best_i]
                sig = bonferroni(best.significance, len(done))
                if sig.sigma_equivalent >= s.watch_sigma:
                    series = next(x for x in ctx["series"] if x.series_id == best.series_id)
                    perturbed = variability_test(series, dataclasses.replace(
                        cfg.temporal, flux_systematic_fraction=max(2 * cfg.temporal.flux_systematic_fraction, 0.01)))
                    ev = reasoning.Evidence(
                        subject_id=c.source_id, subject_kind="SOURCE", kind=CandidateKind.VARIABILITY, category="VARIABILITY_CANDIDATE",
                        statistic_name="constant-flux chi-square (most significant comparable series, Bonferroni-corrected)",
                        statistic_meaning="how inconsistent the comparable-wavelength fluxes are with a constant flux under the stated noise model",
                        statistic_method="variability_chi2", significance=sig, ra=c.ra, dec=c.dec, cov=c.cov_arcsec2,
                        detections=ctx["dets"], points=series.points, images=facts, ambiguous=c.ambiguous,
                        ambiguity_kinds=c.ambiguity_kinds, n_points=len(series.points), n_epochs=series.n_epochs,
                        min_points=cfg.temporal.min_points_variability, min_epochs=cfg.temporal.min_epochs_variability,
                        leave_one_out=best.leave_one_out,
                        systematics_perturbed=bonferroni(perturbed.significance, len(done)) if perturbed.significance else None,
                        wavelength_span_um=series.wavelength_span_um, tolerance_um=series.tolerance_um, min_neighbour_px=ctx["min_nn"],
                        flux_error_scale=f_scale, flux_error_scale_se=f_se,
                        calibrated_significance=bonferroni(chi2_significance(best.chi2 / f_scale ** 2, best.dof), len(done))
                        if f_scale and f_scale > 1.0 else None)
                    assessments.append(reasoning.assess(ev, validity("VARIABILITY"), cfg,
                                                        planetx.not_applicable(c.source_id, cfg, "variability alone carries no motion evidence"),
                                                        {"series": best.series_id}))
            mt = ctx["motion"]
            if mt.status == "COMPLETED":
                n_motion_tests += 1
            if mt.status == "COMPLETED" and c.source_id not in in_track and mt.gof_stationary.sigma_equivalent >= s.watch_sigma:
                astro = [d for d in ctx["dets"] if d.astrometry_usable]
                doubled = motion_test([d.ra for d in astro], [d.dec for d in astro],
                                      [d.cov() + 4 * floor2 * np.eye(2) for d in astro], [d.mjd for d in astro])
                cell = validity("MOTION")
                ev = reasoning.Evidence(
                    subject_id=c.source_id, subject_kind="SOURCE", kind=CandidateKind.MOTION, category="MOTION_CANDIDATE",
                    statistic_name="stationarity chi-square (fixed-position model)",
                    statistic_meaning="how inconsistent the measured positions are with a fixed point, given centroid errors and the registration floor",
                    statistic_method="stationarity_chi2", significance=mt.gof_stationary, ra=c.ra, dec=c.dec, cov=c.cov_arcsec2,
                    detections=ctx["dets"], points=[], images=facts, ambiguous=c.ambiguous, ambiguity_kinds=c.ambiguity_kinds,
                    n_points=mt.n_points, n_epochs=ctx["n_epochs"], min_points=cfg.temporal.min_points_motion, min_epochs=1,
                    motion=mt, parallax=ctx["parallax"],
                    systematics_perturbed=doubled.gof_stationary if doubled.status == "COMPLETED" else None,
                    min_neighbour_px=ctx["min_nn"], error_scale=err_scale, error_scale_se=err_se,
                    calibrated_significance=rescaled(ctx["dets"]) if err_scale and err_scale > 1.0 else mt.gof_stationary,
                    known_object=known_for(ctx["dets"], stationary_cluster=True), catalogue=catalogue_for(c.source_id),
                    leave_one_image_out=leave_one_image_out(ctx["dets"]))
                px = planetx.evaluate(c.source_id, motion=mt, parallax=ctx["parallax"], n_epoch_clusters=ctx["n_epochs"],
                                      validity_state=cell.state.value, config=cfg, evidence_prefix=c.source_id,
                                      known_object=ev.known_object, catalogue=ev.catalogue)
                assessments.append(reasoning.assess(ev, cell, cfg, px, {"positions": c.source_id}))
        for sid, t in sorted(transient_tests.items()):
            if t.status != "TRANSIENT_LIKE":
                continue
            ctx = source_ctx[sid]
            c = ctx["cluster"]
            ev = reasoning.Evidence(
                subject_id=sid, subject_kind="SOURCE", kind=CandidateKind.TRANSIENT, category="TRANSIENT_CANDIDATE",
                statistic_name="flux excess over comparable reference images (z squared)",
                statistic_meaning="how much brighter the source was in its detection image than at the same position and comparable wavelength in other images",
                statistic_method="flux_consistency_z", significance=t.significance, ra=c.ra, dec=c.dec, cov=c.cov_arcsec2,
                detections=ctx["dets"], points=[p for p in ctx["points"] if p.obs_id in {r["obs_id"] for r in t.references}],
                images=facts, ambiguous=c.ambiguous, ambiguity_kinds=c.ambiguity_kinds, n_points=1 + t.n_references,
                n_epochs=1, min_points=2, min_epochs=1, transient=t, linked_track=in_track.get(sid), min_neighbour_px=ctx["min_nn"],
                known_object=known_for(ctx["dets"]), catalogue=catalogue_for(sid), flux_error_scale=f_scale, flux_error_scale_se=f_se,
                calibrated_significance=chi2_significance(t.significance.statistic / f_scale ** 2, 1)
                if f_scale and f_scale > 1.0 and t.significance is not None else None)
            assessments.append(reasoning.assess(ev, validity("TRANSIENT"), cfg,
                                                planetx.not_applicable(sid, cfg, "a single-epoch flux excess carries no parallax evidence"),
                                                {"probe": t.probe_det_id or sid, "reference": f"{sid}:references"}))
        for t in link.tracks:
            dets = [det_by_id[d] for d in t.det_ids]
            n_ep = len(set(t.epoch_indices))
            cell = validity("TRACK")
            ra = float(np.mean([d.ra for d in dets]))
            dec = float(np.mean([d.dec for d in dets]))
            spread = np.cov(np.array([[d.ra * math.cos(math.radians(dec)), d.dec] for d in dets]).T) * 3600.0 ** 2 if len(dets) > 1 else np.eye(2)
            ev = reasoning.Evidence(
                subject_id=t.track_id, subject_kind="TRACK", kind=CandidateKind.MOTION, category="MOVING_OBJECT_CANDIDATE",
                statistic_name="stationarity chi-square of the linked positions",
                statistic_meaning="how inconsistent the linked positions are with one fixed point",
                statistic_method="parallax_motion_fit", significance=t.motion.gof_stationary, ra=ra, dec=dec,
                cov=np.asarray(spread).tolist(), detections=dets, points=[], images=facts, ambiguous=t.ambiguous,
                ambiguity_kinds=["SHARED_SOURCES"] if t.ambiguous else [], n_points=t.n_points, n_epochs=n_ep, min_points=3,
                min_epochs=cfg.planetx.min_epoch_clusters_for_parallax, motion=t.motion, parallax=t.parallax,
                min_neighbour_px=min([d.nearest_neighbour_px for d in dets if d.nearest_neighbour_px is not None], default=None),
                error_scale=err_scale, error_scale_se=err_se,
                calibrated_significance=rescaled(dets) if err_scale and err_scale > 1.0 else t.motion.gof_stationary,
                known_object=known_for(dets), leave_one_image_out=leave_one_image_out(dets))
            px = planetx.evaluate(t.track_id, motion=t.motion, parallax=t.parallax, n_epoch_clusters=n_ep,
                                  validity_state=cell.state.value, config=cfg, evidence_prefix=t.track_id,
                                  known_object=ev.known_object)
            assessments.append(reasoning.assess(ev, cell, cfg, px, {"positions": t.track_id, "times": t.track_id}))
        reasoning.rank_follow_up(assessments)
        assessments.sort(key=lambda a: a.follow_up["rank"])
        alpha = 2.0 * float(_st.norm.sf(s.watch_sigma))
        res["look_elsewhere"] = {"variability_tests": n_var_tests, "motion_tests": n_motion_tests,
                                 "transient_tests": len(transient_tests), "alpha_at_watch": alpha,
                                 "expected_false_alarms_at_watch_if_calibrated": (n_var_tests + n_motion_tests + len(transient_tests)) * alpha,
                                 "note": "variability uses a per-source Bonferroni correction over its comparable series; no field-wide correction is applied"}
        levels = {}
        for a in assessments:
            levels[a.candidate.attention_level.value] = levels.get(a.candidate.attention_level.value, 0) + 1
            self._event("CANDIDATE", "reasoning", f"{a.candidate_id}: {a.candidate.attention_level.value}", a.candidate_id)
        self._end(rec, ComputationStatus.COMPLETED, n_candidates=len(assessments), **{f"n_{k.lower()}": v for k, v in levels.items()})

        # outputs ------------------------------------------------------------------
        res["observations"] = list(obs_entries.values())
        res["epochs"] = epochs
        res["detections"] = [d for o in sorted(runs) for d in runs[o].accepted]
        res["rejected_detections"] = [d for o in sorted(runs) for d in runs[o].rejected]
        res["sources"] = source_out
        res["associations"] = [Association(assoc_id=f"A:{c.source_id}", det_ids=tuple(c.det_ids), method_id=ASSOCIATION_METHOD_ID,
                                           ambiguity=derived_quantity(len(c.released_det_ids) + len(c.alternative_groupings), "count",
                                                                      {"items": c.source_id}, value_class=ValueClass.COUNT),
                                           alternative_groupings=tuple(tuple(g) for g in c.alternative_groupings))
                               for c in clusters if len(c.det_ids) >= 2]
        res["transients"] = list(transient_tests.values())
        res["tracks"] = link.tracks
        res["linking"] = {k: getattr(link, k) for k in ("n_inputs", "n_feasible_pairs", "n_evaluations", "truncated", "r_min_au", "notes",
                                                    "n_rejected_low_effective_dof", "rejected_examples", "rate_scale_arcsec_day",
                                                    "n_absorbed_chains")}
        res["linking"]["not_linked"] = not_linked
        res["candidates"] = assessments
        moi = next((v for v in res["validation"]["runs"] if v.procedure == "moving_object_injection"
                    and v.outcome != "NOT_ASSESSABLE"), None)
        n_consistent = sum(1 for a in assessments if a.planet_x is not None and a.planet_x.status == "CONSISTENT_REQUIRES_CONFIRMATION")
        res["planet_x"] = {"candidates": [{"candidate_id": a.candidate_id, "status": a.planet_x.status}
                                          for a in assessments if a.planet_x is not None and a.planet_x.applicable],
                           "region": planetx.region_assessment(len(link.tracks), cfg, moi.metrics if moi else None, n_consistent,
                                                               moi.outcome if moi else None)}
        res["unavailable_analyses"] = unavailable_analyses(res["context"], res["validation"]["runs"])
        res["playback"] = self._playback(images, obs_entries, epochs, epoch_of, runs, clusters, forced, link, det_by_id)
        res["methods"] = {"detection": [DETECTION_METHOD_ID, DETECTION_METHOD_VERSION],
                          "association": [ASSOCIATION_METHOD_ID, ASSOCIATION_METHOD_VERSION],
                          "reasoning": reasoning.REASONING_RULES_VERSION, "planet_x": planetx.PLANETX_RULES_VERSION,
                          "validation": validation.VALIDATION_VERSION, "pipeline": PIPELINE_VERSION}
        res["provenance_calibrations"] = calibrations
        # investigation (after reasoning; reads only the structured results above) ------------
        rec = self._start("investigation")
        try:
            keys = ("sources", "transients", "tracks", "linking", "context", "candidates", "astrometric_error_model",
                    "photometric_error_model", "config", "config_hash", "run_id")
            snapshot = plain({k: res.get(k) for k in keys})
            inv = investigation_mod.build(snapshot, cfg)
            res["investigations"] = inv
            by_cand = {}
            for i in inv["investigations"]:
                for cid in i["candidate_ids"]:
                    by_cand[cid] = i
            for a in assessments:
                i = by_cand.get(a.candidate_id)
                if i is not None:
                    a.physical_interpretation = {**a.physical_interpretation, "investigation_id": i["investigation_id"],
                                                 "interpretation_status": i["interpretation"]["status"]}
            self._end(rec, ComputationStatus.COMPLETED, n_investigations=inv["n_investigations"],
                      **{f"n_{k.lower()}": v for k, v in inv["interpretation_counts"].items()})
        except Exception as exc:  # an investigation failure must not corrupt the other results
            res["investigations"] = {"schema": investigation_mod.INVESTIGATIONS_SCHEMA, "status": "FAILED", "investigations": [],
                                     "n_investigations": 0, "reason": f"{type(exc).__name__}: {exc}"}
            self._end(rec, ComputationStatus.FAILED, [f"{type(exc).__name__}: {exc}"], partial=True)
        partial = bool(failed) or any(st["partial"] for st in self.stages)
        return self._finish(res, failed, "PARTIAL" if partial else "COMPLETED",
                            ResultOutcome.PARTIAL if partial else ResultOutcome.OK)

    # -- helpers -----------------------------------------------------------------

    def _playback(self, images, obs_entries, epochs, epoch_of, runs, clusters, forced, link, det_by_id) -> dict:
        frames = []
        order = sorted(obs_entries, key=lambda o: ((obs_entries[o].get("meta") or {}).get("mjd_avg") or math.inf, o))
        meas_by_obs: dict[str, list] = {}
        for c in clusters:
            for f in forced[c.source_id]:
                if f.status != "NOT_COVERED":
                    meas_by_obs.setdefault(f.obs_id, []).append({"source_id": c.source_id, "ra": c.ra, "dec": c.dec,
                                                                 "flux_ujy": f.flux_ujy, "flux_sigma_ujy": f.flux_sigma_ujy,
                                                                 "status": f.status})
        for i, o in enumerate(order):
            e = obs_entries[o]
            meta = e.get("meta") or {}
            img = images.get(o)
            lam = img.wavelength_at(*(((img.window[0] + img.window[1] - 1) / 2.0, (img.window[2] + img.window[3] - 1) / 2.0))) if img else (None, None, None)
            frames.append({"index": i, "obs_id": o, "mjd": meta.get("mjd_avg"), "iso_utc": iso_utc(meta.get("mjd_avg")),
                           "epoch_index": epoch_of.get(o), "detector": meta.get("detector"),
                           "wavelength_um": lam[0], "bandwidth_um": lam[1], "wavelength_source": lam[2],
                           "footprint": e.get("footprint"), "included": e["included"], "exclusion_reasons": e["exclusion_reasons"],
                           "suitability": e["qc"].suitability, "detections": [d.det_id for d in runs[o].accepted] if o in runs else [],
                           "measurements": meas_by_obs.get(o, [])})
        return {"schema": "heimdall.playback/1.0", "ordering": "MJD-AVG (UTC) ascending; obs_id breaks ties",
                "interpolation": "none: frames are real observations only; gaps are not filled",
                "epochs": epochs, "frames": frames,
                "tracks": [{"track_id": t.track_id, "points": sorted(
                    [{"det_id": d, "obs_id": det_by_id[d].obs_id, "mjd": det_by_id[d].mjd, "ra": det_by_id[d].ra,
                      "dec": det_by_id[d].dec} for d in t.det_ids], key=lambda p: (p["mjd"] or math.inf, p["obs_id"]))}
                    for t in link.tracks]}

    def _finish(self, res: dict, failed: list[FailedItem], execution: str, outcome: ResultOutcome | None,
                error: ErrorInfo | None = None) -> dict:
        res["failed_items"] = failed
        res["status"] = {"execution": execution, "outcome": outcome.value if outcome else None,
                         "error": error, "note": "execution status says whether the computation ran; it is not scientific validity"}
        res["stages"] = self.stages
        res["events"] = self.events
        res["provenance"] = {"software_version": __version__, "schema_version": SCHEMA_VERSION,
                             "engine_config_version": self.config.version, "config_hash": self.config.hash,
                             "pipeline_version": PIPELINE_VERSION, "data_source": plain(self.source.describe()),
                             "finished_at": _now(), "python": platform.python_version(),
                             "libraries": _library_versions()}
        out = plain(res)
        out["results_hash"] = canonical_hash(scientific_core(out))
        return out


def _subset(analyses: dict, n: int = 6) -> dict:
    keys = sorted(analyses)[:n]
    return {k: analyses[k] for k in keys}


def _library_versions() -> dict:
    import astropy
    import scipy
    return {"numpy": np.__version__, "scipy": scipy.__version__, "astropy": astropy.__version__}


class RunStore:
    """Run directories: ``<root>/<run_id>/investigation.json`` plus events, request and configuration."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def save(self, result: dict, skymap: dict | None = None, directory: Path | None = None) -> Path:
        run_dir = Path(directory) if directory else self.root / result["run_id"]
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "investigation.json").write_text(json.dumps(result, indent=1, sort_keys=True, allow_nan=False), encoding="utf-8")
        with open(run_dir / "events.jsonl", "w", encoding="utf-8") as fh:
            for event in result.get("events", []):
                fh.write(json.dumps(event, sort_keys=True) + "\n")
        (run_dir / "request.json").write_text(json.dumps(result["request"], indent=1, sort_keys=True), encoding="utf-8")
        (run_dir / "config.json").write_text(json.dumps(result["config"], indent=1, sort_keys=True), encoding="utf-8")
        if skymap is not None:
            (run_dir / "skymap.json").write_text(json.dumps(skymap, indent=1, sort_keys=True, allow_nan=False), encoding="utf-8")
        (run_dir / "manifest.json").write_text(json.dumps(self.manifest(result), indent=1, sort_keys=True), encoding="utf-8")
        return run_dir

    @staticmethod
    def manifest(result: dict) -> dict:
        """Every input, calibration and external query of a run, with checksums: the audit and reproduction record."""
        ctx = result.get("context") or {}
        sb = ctx.get("small_bodies") or {}
        return {
            "schema": "heimdall.manifest/1.0", "run_id": result.get("run_id"), "data_origin": result.get("data_origin"),
            "software_version": (result.get("provenance") or {}).get("software_version"), "config_hash": result.get("config_hash"),
            "results_hash": result.get("results_hash"), "request": result.get("request"),
            "inputs": [{"obs_id": o["obs_id"], "access_reference": o.get("product_access_reference"),
                        "sha256": o.get("product_checksum"), "retrieval_origin": o.get("retrieval_origin"),
                        "release": (o.get("meta") or {}).get("release"), "pipeline_version": (o.get("meta") or {}).get("pipeline_version"),
                        "mjd_avg": (o.get("meta") or {}).get("mjd_avg"), "included": o.get("included"),
                        "exclusion_reasons": o.get("exclusion_reasons")} for o in result.get("observations", [])],
            "failed_items": result.get("failed_items", []),
            "calibrations": result.get("provenance_calibrations", []),
            "context_queries": {"gaia": {k: (ctx.get("gaia") or {}).get(k) for k in ("service", "status", "from_cache", "n_stars", "g_max")},
                                "small_bodies": [{k: q.get(k) for k in ("obs_id", "status", "from_cache", "n_predicted")}
                                                 for q in sb.get("queries", [])]},
            "reproduce": "python3 -m heimdall reproduce <run directory> (add HEIMDALL_OFFLINE=1 to use only cached inputs)",
        }

    def resolve(self, ref: str | Path) -> Path:
        path = Path(ref)
        if (path / "investigation.json").is_file():
            return path
        if (self.root / str(ref) / "investigation.json").is_file():
            return self.root / str(ref)
        raise HeimdallError.of(ErrorCode.INPUT_INVALID, "no investigation found at this path or run id", ref=str(ref))

    def load(self, ref: str | Path) -> dict:
        return json.loads((self.resolve(ref) / "investigation.json").read_text(encoding="utf-8"))

    def list(self) -> list[dict]:
        out = []
        for path in sorted(self.root.glob("*/investigation.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            out.append({"run_id": data.get("run_id"), "created_at": data.get("created_at"), "data_origin": data.get("data_origin"),
                        "status": data.get("status"), "n_candidates": len(data.get("candidates", [])), "path": str(path.parent)})
        return out
