"""Blind field investigations as durable jobs (``FIELD_INVESTIGATION``), for the HTTP transport.

``request_field_run`` never computes and never contacts the archive: it returns a stored identical run (same request, configuration and
frozen input manifest) or a durable job id. The worker handler prepares the region through the data service (prefetch -> manifest) and
runs the blind pipeline on verified local data only (``Engine.investigate``'s readiness path). An archive outage keeps the job in
``PREPARING_DATA``; it is never a non-detection.
"""

from __future__ import annotations

from .dataservice.inventory import now_iso
from .dataservice.schemas import JOB_TO_READINESS, JobState, Readiness, digest
from .pipeline.runner import InvestigationRequest

KIND_FIELD = "FIELD_INVESTIGATION"
FIELDS = ("ra_deg", "dec_deg", "radius_deg", "detectors", "mjd_start", "mjd_end", "max_observations", "cutout_size_deg", "context")


def _payload(request: InvestigationRequest) -> dict:
    d = request.to_dict()
    return {k: d[k] for k in FIELDS}


def _request(payload: dict) -> InvestigationRequest:
    return InvestigationRequest(source_id="irsa-spherex", label="field run (durable job)", **payload)


def _key(engine, payload: dict, manifest_id: str | None) -> str:
    return "frun:" + digest([payload, engine.config.hash, manifest_id], 20)


def _stored_run(engine, key: str) -> dict | None:
    svc = engine.data_service()
    r = svc.db.one("SELECT * FROM investigation_results WHERE key=?", (key,))
    if r is None:
        return None
    try:
        return engine.load(r["investigation_id"])
    except Exception:  # noqa: BLE001 - a missing run directory means it must be recomputed, not an error for the caller
        return None


def summary(run: dict) -> dict:
    from .api import summarize
    return {"run_id": run["run_id"], "summary": summarize(run), "data_origin": run.get("data_origin"), "status": run.get("status"),
            "input_manifest": (run.get("input_manifest") or {}).get("manifest_id"), "data_status": run.get("data_status")}


def request_field_run(engine, request: InvestigationRequest) -> dict:
    """User entry point: a stored identical run, or a durable job reference. No computation and no network here."""
    svc = engine.data_service()
    payload = _payload(request)
    from .dataservice.schemas import RegionSpec
    region = RegionSpec(request.ra_deg % 360.0, request.dec_deg, request.radius_deg, tuple(request.detectors) if request.detectors else None,
                        request.mjd_start, request.mjd_end, None)
    region.validate()
    st = svc.prefetch_status(region, cutout_size_deg=request.cutout_size_deg, max_observations=request.max_observations or 16)
    if st.get("manifest_id"):
        prior = _stored_run(engine, _key(engine, payload, st["manifest_id"]))
        if prior is not None:
            return summary(prior) | {"readiness": Readiness.READY.value, "reused": True}
    job, _ = svc.jobs.submit(KIND_FIELD, payload, revive_complete=True)
    st = field_status(engine, job["job_id"])
    if st.get("run"):  # an identical, already completed job: return the run itself
        return st["run"] | {"readiness": Readiness.READY.value, "job_id": job["job_id"], "reused": True}
    return st


def field_status(engine, job_id: str) -> dict:
    svc = engine.data_service()
    job = svc.jobs.get(job_id)
    if job is None:
        raise KeyError(job_id)
    st = svc._status(JOB_TO_READINESS.get(job["state"], Readiness.PREPARING), job)
    st["events"] = svc.jobs.events(job_id)
    if job["state"] is JobState.COMPLETE and (job["result"] or {}).get("run_id"):
        try:
            st["run"] = summary(engine.load(job["result"]["run_id"]))
        except Exception:  # noqa: BLE001
            st["run"] = None
    st["note"] = "a field run is a blind-pipeline analysis; until it completes there is no scientific result (not a non-detection)"
    return st


def make_handler(engine):
    def handler(service, job: dict) -> dict:
        jid, payload = job["job_id"], job["payload"]
        service.jobs.transition(jid, JobState.DISCOVERING, reason="preparing field data", lease=True)
        try:
            result, run_dir = engine.investigate(_request(payload))
        except Exception as exc:  # noqa: BLE001 - recorded: request errors fail; unexpected errors retry a bounded number of times, then FAILED
            return service.jobs.fail_or_retry(service.jobs.get(jid), exc)
        if run_dir is None:  # data not yet local: wait on the prefetch job that owns acquisition
            child = (result.get("job") or {}).get("job_id")
            readiness = result.get("readiness")
            if readiness == Readiness.PREPARING.value:
                return service._apply_step(jid, {"state": "WAIT", "waiting_on": "field data", "child": child, "detail": "preparing field data"})
            return service._apply_step(jid, {"state": readiness, "child": child, "detail": result.get("message", "")})
        man = (result.get("input_manifest") or {}).get("manifest_id")
        service.db.execute("INSERT OR REPLACE INTO investigation_results(key,investigation_id,saved_to,manifest_id,created_at) VALUES(?,?,?,?,?)",
                           (_key(engine, payload, man), result["run_id"], str(run_dir), man, now_iso(service.clock())))
        return service.jobs.transition(jid, JobState.COMPLETE, reason="field run complete from verified local data",
                                       result={"run_id": result["run_id"], "manifest_id": man, "status": result.get("status")})
    return handler
