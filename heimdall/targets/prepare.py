"""Readiness-first target investigations: the analysis runs from a frozen local input manifest, never from a live archive request.

``request_investigation`` is what users and the API call. It never raises an archive error and never asks the user to retry:

* all data local  -> the analysis runs (offline, zero network calls) or a previously stored identical result is returned;
* data missing    -> a durable job is created (or joined) that owns discovery, download, validation, retries and, once the data are ready,
                    the analysis; the caller gets the job id and a progress state;
* terminal states -> NO_COVERAGE / UNAVAILABLE_AFTER_DEADLINE / INVALID_SOURCE_DATA with a precise, user-safe explanation.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..context import CachedService
from ..dataservice.localsource import LocalStoreSource
from ..dataservice.inventory import now_iso
from ..dataservice.jobs import parse_ts
from ..dataservice.schemas import JOB_TO_READINESS, TERMINAL, JobState, PlanItem, ProductPlan, Readiness, RegionSpec, canonical, digest
from ..dataservice.service import DataService
from . import astrometry
from .investigate import (DEFAULT_DETECTORS, MID_SURVEY_MJD, TargetOptions, coordinate_only_reference, cutout_geometry, investigate_target,
                          select_observations)
from .registry import TargetSpec

KIND_TARGET = "TARGET_INVESTIGATION"
INDEX_RADIUS_DEG = 0.05


def _offline(settings):
    return dataclasses.replace(settings, offline=True)


CATALOGUE_ATTEMPT_TIMEOUT_S = 30.0


def _single_attempt(settings):
    """Catalogue calls made by a worker: one bounded attempt per job run. The durable job owns retries (backoff, deadline, recovery), so
    inner HTTP retries would only hold the single worker during a catalogue outage and starve every other queued job."""
    return dataclasses.replace(settings, max_retries=0, request_timeout_s=min(settings.request_timeout_s, CATALOGUE_ATTEMPT_TIMEOUT_S))


def request_key(spec: TargetSpec, opts: TargetOptions, config_hash: str, manifest_id: str | None) -> str:
    return "tres:" + digest([spec.to_dict(), opts.to_dict(), config_hash, manifest_id], 20)


def saved_version(service: DataService, investigation_id: str | None, saved_to: str | None = None) -> dict | None:
    """The immutable per-version file of a target investigation (never ``latest.json``, which moves on), verified by its id. Looked up next
    to the recorded result path, then under the state directory that holds the service (``<state>/dataservice``), so moved or imported
    state stays valid."""
    if not investigation_id or investigation_id.count(":") != 2:
        return None
    _, tid, h = investigation_id.split(":")
    candidates = ([Path(saved_to).parent / f"{h}.json"] if saved_to else []) + [service.db.path.parent.parent / "targets" / tid / f"{h}.json"]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if doc.get("investigation_id") == investigation_id:
            return doc
    return None


def _stored_result(service: DataService, key: str) -> dict | None:
    r = service.db.one("SELECT * FROM investigation_results WHERE key=?", (key,))
    return None if r is None else saved_version(service, r["investigation_id"], r["saved_to"])


def advance(service: DataService, spec: TargetSpec, opts: TargetOptions, *, settings, config, registry, store, online: bool, progress=None,
            analyse: bool = True) -> dict:
    """One non-blocking step. Returns {"state": "DONE", "result": ...} or {"state": "WAIT"|<terminal readiness>, ...}. Online = a worker may use the network.
    ``analyse=False``: if every input is local but no stored identical result exists, return WAIT(analysis) instead of computing inline."""
    cfg_settings = settings if online else _offline(settings)
    cat_settings = _single_attempt(cfg_settings) if online else cfg_settings
    gaia = CachedService("gaia", cat_settings)
    catwise = CachedService("catwise", cat_settings)
    ref = astrometry.resolve(spec, catwise if spec.catalogue == "catwise2020" else gaia, gaia_service=gaia)
    if not ref.resolved:
        coord = spec.target_id.startswith("COORD-")
        if coord and (ref.status == "UNRESOLVED" or settings.offline):
            # no catalogue entry (or the operator runs fully offline): an arbitrary coordinate is investigated as a coordinate-only reference, as always
            ref = coordinate_only_reference(spec, ref)
        elif online and ref.status == "UNRESOLVED":
            # a definite "no catalogue entry" for a named target: the analysis reports it explicitly (BLOCKED, with the reason)
            return {"state": "DONE", "result": investigate_target(spec, source=LocalStoreSource(service), settings=cfg_settings, config=config, registry=registry,
                                                                  store=store, options=opts, progress=progress), "manifest": None}
        else:
            # catalogue answer not cached yet (or the catalogue service failed): a worker resolves it with its own retries
            return {"state": "WAIT", "waiting_on": "catalogue", "detail": f"reference astrometry not yet available ({ref.status}: {ref.reason})", "child": None}
    c0 = ref.at_mjd(MID_SURVEY_MJD)
    detectors = tuple(opts.detectors or DEFAULT_DETECTORS.get(spec.target_id) or ()) or None
    region0 = (c0.ra_deg, c0.dec_deg)
    region = RegionSpec(region0[0] % 360.0, region0[1], INDEX_RADIUS_DEG, detectors, opts.mjd_start, opts.mjd_end, None)
    idx = service.ensure_index(region)
    if idx["readiness"] == Readiness.PREPARING.value:
        return {"state": "WAIT", "waiting_on": "index", "child": idx["job"]["job_id"], "detail": "indexing the archive for this region"}
    if idx["readiness"] not in (Readiness.READY.value, Readiness.NO_COVERAGE.value):
        return {"state": idx["readiness"], "child": idx["job"]["job_id"] if idx["job"] else None, "detail": idx["message"]}
    manifest = None
    footprint_index = None
    if idx["readiness"] == Readiness.READY.value:
        src = LocalStoreSource(service)
        from ..data_access.interface import DiscoveryQuery, SkyRegion
        f = {"detectors": ",".join(str(d) for d in detectors)} if detectors else {}
        if opts.mjd_start is not None:
            f["mjd_start"] = repr(float(opts.mjd_start))
        if opts.mjd_end is not None:
            f["mjd_end"] = repr(float(opts.mjd_end))
        disc = src.discover(DiscoveryQuery(region=SkyRegion(ra_deg=region.ra_deg, dec_deg=region.dec_deg, radius_deg=INDEX_RADIUS_DEG), filters=f))
        selected, _ = select_observations(list(disc.observations), opts.max_observations, config.temporal.epoch_gap_days)
        if selected:
            from ..ingest.spherex import read_spherex_mef  # noqa: F401 - import check only; keeps optional dependency failures here, not mid-run
            cen, size, _, _ = cutout_geometry(spec, ref, opts, selected)
            if opts.blind_pipeline:
                # the blind pipeline discovers over the cutout footprint (all detectors, no time window): that cone must be indexed locally too
                idx2 = service.ensure_index(RegionSpec(cen.ra_deg % 360.0, cen.dec_deg, size / 2.0, None, None, None, None))
                if idx2["readiness"] == Readiness.PREPARING.value:
                    return {"state": "WAIT", "waiting_on": "index", "child": idx2["job"]["job_id"], "detail": "indexing the archive for the analysis footprint"}
                if idx2["readiness"] not in (Readiness.READY.value, Readiness.NO_COVERAGE.value):
                    return {"state": idx2["readiness"], "child": idx2["job"]["job_id"] if idx2["job"] else None, "detail": idx2["message"]}
                footprint_index = idx2.get("as_of")
            by_id = {o.obs_id: o for o in service.observations(region) or []}
            items = []
            for o in selected:
                rec = by_id[o.obs_id]
                items.append(PlanItem(rec.obs_id, rec.release, rec.detector, rec.access_reference, cen.ra_deg % 360.0, cen.dec_deg, size, rec.archive_checksum))
            plan = ProductPlan(tuple(items), tuple(sorted({("epsf", i.detector) for i in items if i.detector}))) if not _synthetic(by_id) else ProductPlan(tuple(items), ())
            st = service.ensure_products(plan, region=region, request={"target": spec.target_id, "options": opts.to_dict()})
            if st["readiness"] == Readiness.PREPARING.value:
                return {"state": "WAIT", "waiting_on": "products", "child": st["job"]["job_id"], "detail": "acquiring and verifying products"}
            if st["readiness"] != Readiness.READY.value:
                return {"state": st["readiness"], "child": st["job"]["job_id"] if st["job"] else None, "detail": st["message"]}
            manifest = service.manifest(st["manifest_id"])
            # field-star catalogue for registration controls: must be cached (a worker warms it) or the analysis would silently lose its controls
            gq = astrometry.gaia_cone_with_fallback(gaia, cen.ra_deg, cen.dec_deg, size * 0.75, 17.0)
            if gq.status != "COMPLETED" and not _synthetic(by_id):
                return {"state": "WAIT", "waiting_on": "field_catalogue", "child": None, "detail": f"field catalogue not yet available ({gq.reason})"}
    key = request_key(spec, opts, config.hash, (manifest["manifest_id"] if manifest else f"idx:{idx.get('as_of')}") + f"|fp:{footprint_index}")
    prior = _stored_result(service, key)
    if prior is not None:
        return {"state": "DONE", "result": prior, "manifest": manifest, "reused": True}
    if not analyse:
        return {"state": "WAIT", "waiting_on": "analysis", "child": None, "detail": "all inputs are verified locally; the analysis is queued for a worker"}
    res = investigate_target(spec, source=LocalStoreSource(service, manifest), settings=cfg_settings, config=config, registry=registry, store=store, options=opts,
                             progress=progress, input_manifest=manifest)
    if res.get("saved_to"):
        service.db.execute("INSERT OR REPLACE INTO investigation_results(key,investigation_id,saved_to,manifest_id,created_at) VALUES(?,?,?,?,?)",
                           (key, res["investigation_id"], res["saved_to"], manifest["manifest_id"] if manifest else None, now_iso(service.clock())))
    return {"state": "DONE", "result": res, "manifest": manifest}


def _synthetic(by_id: dict) -> bool:
    return any(r.synthetic for r in by_id.values())


def request_investigation(service: DataService, spec: TargetSpec, opts: TargetOptions, *, settings, config, registry, store, wait_s: float = 0.0,
                          progress=None, analyse_inline: bool = True) -> dict:
    """The user-facing entry point. Returns the completed investigation or a durable job reference; never an archive exception.
    ``analyse_inline=False`` (HTTP transport): only a stored identical result is returned immediately; any computation goes to a worker job."""
    step = advance(service, spec, opts, settings=settings, config=config, registry=registry, store=store, online=False, progress=progress,
                   analyse=analyse_inline)
    if step["state"] == "DONE":
        out = step["result"]
        out["data_status"] = {"readiness": Readiness.READY.value, "served_from": "verified local data (no archive request)", "manifest_id": (step.get("manifest") or {}).get("manifest_id"),
                              "result_kind": "STORED_RESULT_REPLAY" if step.get("reused") else "COMPUTED_NOW",
                              "result_created_at": out.get("created_at")}
        imp = service.db.path.parent.parent / "evidence_import.json"
        if imp.is_file():
            try:
                out["data_status"]["evidence_bundle"] = json.loads(imp.read_text(encoding="utf-8")).get("bundle_id")
            except (OSError, ValueError):
                out["data_status"]["evidence_bundle"] = "unreadable import record"
        return out
    payload = {"spec": spec.to_dict(), "options": opts.to_dict()}
    job, _ = service.jobs.submit(KIND_TARGET, payload, revive_complete=True)  # reached only when the analysis cannot be served from local data
    deadline = None
    if wait_s > 0 and not service.offline:
        import time
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline and service.jobs.get(job["job_id"])["state"] not in TERMINAL:
            if not service.tick(max_seconds=max(1.0, deadline - time.monotonic())):
                break
    return investigation_status(service, job["job_id"], spec=spec)


def investigation_status(service: DataService, job_id: str, spec: TargetSpec | None = None) -> dict:
    job = service.jobs.get(job_id)
    if job is None:
        raise KeyError(job_id)
    readiness = JOB_TO_READINESS.get(job["state"], Readiness.PREPARING)
    st = service._status(readiness, job)
    result = job["result"] or {}
    doc = saved_version(service, result.get("investigation_id"), result.get("saved_to")) if job["state"] is JobState.COMPLETE else None
    if doc is not None:
        out = doc
        out["data_status"] = {"readiness": "READY", "job_id": job_id, "manifest_id": result.get("manifest_id")}
        return out
    st["status"] = {"execution": "PREPARING_DATA" if readiness is Readiness.PREPARING else readiness.value, "test_status": None, "reason": st["message"]}
    st["investigation"] = {"target": (spec.target_id if spec else job["payload"]["spec"]["target_id"]), "result_available": False,
                           "note": "no scientific result exists yet; this is a data-preparation status, not a non-detection"}
    st["events"] = service.jobs.events(job_id)
    return st


def make_handler(*, settings, config, registry, store):
    """Worker handler for TARGET_INVESTIGATION jobs: advance online; wait on child jobs; finish with a saved result or a terminal state."""
    def handler(service: DataService, job: dict) -> dict:
        jid = job["job_id"]
        spec = _spec_from(job["payload"]["spec"])
        opts = TargetOptions(**{k: (tuple(v) if isinstance(v, list) else v) for k, v in job["payload"]["options"].items()})
        service.jobs.transition(jid, JobState.DISCOVERING, reason="preparing data for the investigation", lease=True)
        try:
            step = advance(service, spec, opts, settings=settings, config=config, registry=registry, store=store, online=True)
        except Exception as exc:  # noqa: BLE001 - recorded: request errors fail; unexpected errors retry a bounded number of times, then FAILED
            return service.jobs.fail_or_retry(service.jobs.get(jid), exc)
        state = step["state"]
        if state == "DONE":
            res = step["result"]
            return service.jobs.transition(jid, JobState.COMPLETE, reason="investigation complete from verified local data",
                                           result={"saved_to": res.get("saved_to"), "investigation_id": res.get("investigation_id"),
                                                   "manifest_id": (step.get("manifest") or {}).get("manifest_id"), "status": res.get("status")})
        if state == "WAIT" and step.get("waiting_on") in ("catalogue", "field_catalogue"):
            return service.jobs.schedule_retry(service.jobs.get(jid), error={"code": "CATALOGUE_PENDING", "message": step["detail"]},
                                               reason=f"{step['detail']}; retrying automatically", progress={"waiting_on": step["waiting_on"]})
        return service._apply_step(jid, step)
    return handler


def _spec_from(d: dict) -> TargetSpec:
    names = {f.name for f in dataclasses.fields(TargetSpec)}
    d = {k: (tuple(v) if isinstance(v, list) else v) for k, v in d.items() if k in names}
    return TargetSpec(**d)
