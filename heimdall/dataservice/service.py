"""The SPHEREx data service: readiness-first access to verified local data, with owned background acquisition.

Request path (never calls the archive): ``readiness(...)`` answers from the inventory and product store. If data are missing the request
becomes (or joins) a durable job; ``tick()`` -- run by a worker, an operator command or a scheduler, not by the user -- performs discovery
and downloads with bounded retries, route switching (inside the IRSA adapter), circuit breakers and exponential backoff with jitter,
and finishes in exactly one terminal state. An upstream failure is never ``NO_COVERAGE``.
"""

from __future__ import annotations

import json
import random
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..contracts import ErrorCode, HeimdallError, ResultOutcome
from ..data_access.interface import DataSource, DiscoveryQuery, RetrievalRequest, SkyRegion
from .db import ServiceDB
from .inventory import Inventory, ObsRecord, now_iso
from .jobs import JobQueue, parse_ts
from .productstore import InvalidProduct, ProductStore
from .schemas import (CURRENT_COLLECTIONS, JOB_TO_READINESS, SCOPE_CURRENT, SCOPE_SYNTHETIC, STATUS_SCHEMA, TERMINAL, USER_MESSAGES, JobState, PlanItem,
                      ProductPlan, Readiness, RegionSpec, ServiceConfig, calibration_key, canonical, digest)

KIND_INDEX, KIND_ACQUIRE, KIND_PREFETCH = "INDEX", "ACQUIRE", "PREFETCH"
TRANSIENT_CODES = {ErrorCode.ARCHIVE_TIMEOUT, ErrorCode.ARCHIVE_UNREACHABLE, ErrorCode.RETRIEVAL_FAILED, ErrorCode.DATA_MALFORMED,
                   ErrorCode.ACCESS_DENIED, ErrorCode.AUTHENTICATION_FAILED}


def _md(o, key):
    v = o.metadata.get(key)
    return v.value if v is not None and v.is_available else None


def _error_class(exc: HeimdallError) -> str:
    st = exc.info.detail.get("http_status")
    return f"{exc.code.value}" + (f":{st}" if st else "")


class DataService:
    def __init__(self, root: Path, source: DataSource | None = None, config: ServiceConfig | None = None, *, offline: bool = False,
                 clock=lambda: datetime.now(UTC), rng: random.Random | None = None, handlers: dict | None = None) -> None:
        self.cfg = config or ServiceConfig()
        self.root = Path(root)
        self.db = ServiceDB(self.root / "service.sqlite3")
        self.inventory = Inventory(self.db)
        self.store = ProductStore(self.db, self.root / "store", self.cfg.quota_bytes)
        self.clock = clock
        self.jobs = JobQueue(self.db, self.cfg, clock, rng)
        self.source = source
        self.offline = offline or source is None
        self.handlers = dict(handlers or {})  # extension job kinds (e.g. TARGET_INVESTIGATION), registered by higher layers

    def close(self) -> None:
        self.db.close()

    # ================================================================ readiness (no network) ====
    def index_status(self, region: RegionSpec) -> dict:
        region.validate()
        s = self.inventory.covering_sync(region, self.cfg.scope)
        if s is None:
            return {"indexed": False}
        age = (self.clock() - parse_ts(s["completed_at"])).total_seconds()
        return {"indexed": True, "sync_id": s["sync_id"], "completed_at": s["completed_at"], "age_s": age, "n_members": s["n_obs"], "route": s["route"],
                "stale": age > self.cfg.index_max_age_s, "from_cache": bool(s["from_cache"])}

    def observations(self, region: RegionSpec) -> list[ObsRecord] | None:
        """Indexed, non-superseded observations for the region, or None if the region is not indexed (which is not 'no observations')."""
        s = self.inventory.covering_sync(region, self.cfg.scope)
        return None if s is None else self.inventory.members(region, s)

    def plan_missing(self, plan: ProductPlan) -> list[str]:
        miss = [i.key for i in plan.items if self.store.get(i.key, count=True) is None]
        miss += [calibration_key(k, d) for k, d in plan.calibrations if self.store.get(calibration_key(k, d), count=True) is None]
        return miss

    def freeze_manifest(self, request: dict, plan: ProductPlan, region: RegionSpec | None = None, excluded: list[str] | None = None) -> dict:
        """Freeze exactly the local products and calibrations an analysis will use. Pins them so retention cannot remove them."""
        items = []
        for i in sorted(plan.items, key=lambda x: x.key):
            p = self.store.get(i.key)
            if p is None:
                raise KeyError(f"product {i.key} is not ready")
            items.append({"key": i.key, "obs_id": i.obs_id, "release": i.release, "detector": i.detector, "sha256": p.sha256, "origin": p.origin, "route": p.route,
                          "access_reference": p.access_reference, "archive_checksum": p.archive_checksum, "retrieved_at": p.retrieved_at,
                          "cutout": [i.cutout_ra_deg, i.cutout_dec_deg, i.cutout_size_deg]})
        cals = []
        for k, d in sorted(plan.calibrations):
            p = self.store.get(calibration_key(k, d))
            if p is None:
                raise KeyError(f"calibration {k} D{d} is not ready")
            cals.append({"kind": k, "detector": d, "sha256": p.sha256, "origin": p.origin, "access_reference": p.access_reference, "retrieved_at": p.retrieved_at})
        sync = self.inventory.covering_sync(region, self.cfg.scope) if region else None
        body = {"schema": "heimdall.input_manifest/1.0", "request": request, "products": items, "calibrations": cals,
                "excluded_quarantined": sorted(excluded or []),
                "index": {"sync_id": sync["sync_id"], "completed_at": sync["completed_at"], "route": sync["route"]} if sync else None,
                "scope": self.cfg.scope}
        mid = f"man:{digest(body, 16)}"  # content hash: the same inputs always give the same manifest id
        self.db.execute("INSERT OR IGNORE INTO manifests(manifest_id,created_at,body) VALUES(?,?,?)", (mid, now_iso(self.clock()), canonical(body)))
        self.store.pin([x["sha256"] for x in items + cals], mid)
        return {"manifest_id": mid, **body}

    def manifest(self, manifest_id: str) -> dict | None:
        r = self.db.one("SELECT body FROM manifests WHERE manifest_id=?", (manifest_id,))
        return {"manifest_id": manifest_id, **json.loads(r["body"])} if r else None

    # ================================================================ requests ====
    def ensure_index(self, region: RegionSpec, *, wait_s: float = 0.0) -> dict:
        """Status of the region index. Indexed -> immediate (a stale index also queues a background refresh). Otherwise a durable INDEX job."""
        region.validate()
        st = self.index_status(region)
        if st["indexed"]:
            refresh = None
            if st["stale"]:
                refresh, _ = self.jobs.submit(KIND_INDEX, {"region": region.to_dict(), "scope": self.cfg.scope, "refresh_of": st["sync_id"]})
            n = st["n_members"]
            readiness = Readiness.READY if n else Readiness.NO_COVERAGE
            return self._status(readiness, job=None, extra={"index": st, "refresh_job": refresh["job_id"] if refresh else None,
                                                            "as_of": st["completed_at"], "from_local_inventory": True})
        job, _ = self.jobs.submit(KIND_INDEX, {"region": region.to_dict(), "scope": self.cfg.scope}, revive_complete=True)
        return self._wait_and_status(job, wait_s, region=region)

    def coverage(self, region: RegionSpec, *, offset: int = 0, limit: int = 500, wait_s: float = 0.0) -> dict:
        """Readiness-first coverage for map browsing: observations from the local inventory when the region is indexed (with the index
        time), otherwise a durable INDEX job (no analysis is ever started). Pagination bounds the response."""
        st = self.ensure_index(region, wait_s=wait_s)
        obs = self.observations(region)
        if obs is None:
            return st | {"observations": None, "note": "region not indexed yet; this is not 'no observations'"}
        obs = sorted(obs, key=lambda o: (o.t_min if o.t_min is not None else 0.0, o.obs_id, o.release))
        limit = max(1, min(int(limit), 2000))
        offset = max(0, int(offset))
        per_det: dict[str, int] = {}
        per_rel: dict[str, int] = {}
        for o in obs:
            per_det[str(o.detector)] = per_det.get(str(o.detector), 0) + 1
            per_rel[o.release] = per_rel.get(o.release, 0) + 1
        page = [{"obs_id": o.obs_id, "release": o.release, "collection": o.collection, "detector": o.detector, "t_min_mjd": o.t_min, "t_max_mjd": o.t_max,
                 "wavelength_min_um": o.wl_min_um, "wavelength_max_um": o.wl_max_um, "footprint": o.footprint, "centre_deg": [o.s_ra, o.s_dec],
                 "route": o.route, "synthetic": o.synthetic} for o in obs[offset:offset + limit]]
        sync = self.inventory.covering_sync(region, self.cfg.scope)
        return st | {"n_observations": len(obs), "per_detector": per_det, "per_release": per_rel, "offset": offset, "limit": limit,
                     "observations": page, "index_as_of": sync["completed_at"] if sync is not None else None,
                     "note": "local inventory (cached archive answers as of index_as_of), not a live query; rendering coverage does not run any analysis"}

    def _manifest_if_ready(self, plan: ProductPlan, region: RegionSpec | None, request: dict | None, payload: dict) -> dict | None:
        """A frozen manifest if every product is verified locally, or if the only missing ones were quarantined as invalid by a COMPLETE job
        (they are then excluded and listed in the manifest; the analysis never sees them)."""
        miss = self.plan_missing(plan)
        excluded: list[str] = []
        if miss:
            j = self.jobs.get(self.jobs.job_id(KIND_ACQUIRE, payload))
            q = set((j["result"] or {}).get("quarantined", [])) if j and j["state"] is JobState.COMPLETE else set()
            if not q or not set(miss) <= q:
                return None
            plan = ProductPlan(tuple(i for i in plan.items if i.key not in q), plan.calibrations)
            excluded = sorted(q)
        return self.freeze_manifest(request or {"plan": plan.to_dict()}, plan, region, excluded)

    def ensure_products(self, plan: ProductPlan, *, region: RegionSpec | None = None, wait_s: float = 0.0, request: dict | None = None) -> dict:
        payload = {"plan": plan.to_dict(), "region": region.to_dict() if region else None, "scope": self.cfg.scope}
        man = self._manifest_if_ready(plan, region, request, payload)
        if man is not None:
            return self._status(Readiness.READY, job=None, extra={"manifest_id": man["manifest_id"], "from_local_store": True, "excluded_quarantined": man["excluded_quarantined"]})
        job, _ = self.jobs.submit(KIND_ACQUIRE, payload, revive_complete=True)  # reached only when products are actually missing
        return self._wait_and_status(job, wait_s, plan=plan, region=region, request=request, payload=payload)

    def _prefetch_step(self, region: RegionSpec, cutout_size_deg: float | None, max_observations: int) -> dict:
        """One non-blocking orchestration step: index the region, then acquire a bounded, deterministic product set. Never calls the archive itself."""
        idx = self.ensure_index(region)
        if idx["readiness"] == Readiness.PREPARING.value:
            return {"state": "WAIT", "child": idx["job"]["job_id"], "detail": "indexing the archive for this region"}
        if idx["readiness"] != Readiness.READY.value:
            return {"state": idx["readiness"], "child": idx["job"]["job_id"] if idx["job"] else None, "detail": idx["message"]}
        plan = plan_for(self.observations(region) or [], region, cutout_size_deg, max_observations)
        st = self.ensure_products(plan, region=region, request={"prefetch": region.to_dict(), "cutout_size_deg": cutout_size_deg, "max_observations": max_observations})
        if st["readiness"] == Readiness.PREPARING.value:
            return {"state": "WAIT", "child": st["job"]["job_id"], "detail": "acquiring and verifying products"}
        if st["readiness"] != Readiness.READY.value:
            return {"state": st["readiness"], "child": st["job"]["job_id"] if st["job"] else None, "detail": st["message"]}
        return {"state": "READY", "manifest_id": st["manifest_id"]}

    def prefetch(self, region: RegionSpec, *, cutout_size_deg: float | None, max_observations: int, wait_s: float = 0.0) -> dict:
        """Explicit ingestion of a region (index + products + calibrations). Local data -> READY now; otherwise a durable PREFETCH job."""
        region.validate()
        step = self._prefetch_step(region, cutout_size_deg, max_observations) if not self._would_create_jobs(region, cutout_size_deg, max_observations) else {"state": "NEEDS_JOB"}
        if step["state"] == "READY":
            return self._status(Readiness.READY, job=None, extra={"manifest_id": step["manifest_id"], "from_local_store": True})
        if step["state"] in (Readiness.NO_COVERAGE.value,):
            return self._status(Readiness.NO_COVERAGE, job=None, extra={"from_local_inventory": True})
        job, _ = self.jobs.submit(KIND_PREFETCH, {"region": region.to_dict(), "cutout_size_deg": cutout_size_deg, "max_observations": max_observations, "scope": self.cfg.scope},
                                 revive_complete=True)  # reached only when the region is not fully local
        return self._wait_and_status(job, wait_s)

    def _would_create_jobs(self, region: RegionSpec, cutout_size_deg, max_observations) -> bool:
        """True when the region is not fully local, so the request must become a durable job (checked without creating child jobs)."""
        if not self.index_status(region)["indexed"]:
            return True
        obs = self.observations(region) or []
        if not obs:
            return False
        return bool(self.plan_missing(plan_for(obs, region, cutout_size_deg, max_observations)))

    def _run_prefetch(self, job: dict) -> dict:
        jid, p = job["job_id"], job["payload"]
        self.jobs.transition(jid, JobState.DISCOVERING, reason="preparing region data", lease=True)
        step = self._prefetch_step(RegionSpec.from_dict(p["region"]), p["cutout_size_deg"], p["max_observations"])
        return self._apply_step(jid, step)

    def _apply_step(self, jid: str, step: dict) -> dict:
        """Map a child-orchestration step onto the parent job's state (shared by PREFETCH and TARGET_INVESTIGATION jobs)."""
        st = step["state"]
        if st == "READY":
            return self.jobs.transition(jid, JobState.COMPLETE, reason="all required verified data are stored locally", result={"manifest_id": step["manifest_id"]})
        if st == "WAIT":
            child = self.jobs.get(step["child"]) if step.get("child") else None
            nxt = parse_ts(child["next_attempt_at"]) if child and child["next_attempt_at"] else None
            now = self.clock()
            when = nxt if nxt and nxt > now else now + timedelta(seconds=self.cfg.poll_s)
            return self.jobs.transition(jid, JobState.PREPARING_DATA, reason=step["detail"], next_attempt_at=when,
                                        progress={"waiting_on": step.get("waiting_on"), "child_job": step.get("child")})
        terminal = {Readiness.NO_COVERAGE.value: JobState.NO_COVERAGE, Readiness.UNAVAILABLE_AFTER_DEADLINE.value: JobState.UNAVAILABLE_AFTER_DEADLINE,
                    Readiness.INVALID_SOURCE_DATA.value: JobState.INVALID_SOURCE_DATA, Readiness.CANCELLED.value: JobState.CANCELLED}.get(st)
        if terminal is None:
            return self.jobs.schedule_retry(self.jobs.get(jid), error={"code": "UNKNOWN", "message": str(step)}, reason="retrying automatically")
        return self.jobs.transition(jid, terminal, reason=step.get("detail", ""), progress={"child_job": step.get("child")})

    def _wait_and_status(self, job: dict, wait_s: float, **ctx) -> dict:
        if wait_s > 0 and not self.offline:
            deadline = time.monotonic() + wait_s
            while time.monotonic() < deadline and self.jobs.get(job["job_id"])["state"] not in TERMINAL:
                if not self.tick(max_seconds=max(0.0, deadline - time.monotonic())):
                    break
        job = self.jobs.get(job["job_id"])
        extra = {}
        if job["state"] is JobState.COMPLETE and "plan" in ctx:
            man = self._manifest_if_ready(ctx["plan"], ctx.get("region"), ctx.get("request"), ctx["payload"])
            if man is not None:
                extra["manifest_id"] = man["manifest_id"]
        return self._status(JOB_TO_READINESS.get(job["state"], Readiness.PREPARING), job=job, extra=extra)

    def _status(self, readiness: Readiness, job: dict | None, extra: dict | None = None) -> dict:
        out = {"schema": STATUS_SCHEMA, "readiness": readiness.value, "message": USER_MESSAGES[readiness], "job": self.describe_job(job) if job else None,
               "must_resubmit": self.jobs.must_resubmit(job)}
        if readiness is Readiness.PREPARING and self.offline:
            out["message"] += " (This process is offline; a worker with archive access will acquire the data.)"
        out.update(extra or {})
        return out

    def describe_job(self, job: dict) -> dict:
        now = self.clock()
        nxt = parse_ts(job["next_attempt_at"])
        return {"job_id": job["job_id"], "kind": job["kind"], "state": job["state"].value, "generation": job["generation"], "attempts": job["attempts"],
                "created_at": job["created_at"], "updated_at": job["updated_at"], "deadline_at": job["deadline_at"], "next_attempt_at": job["next_attempt_at"],
                "next_attempt_in_s": max(0.0, (nxt - now).total_seconds()) if nxt else None, "reason": job["reason"], "progress": job["progress"],
                "last_error": job["last_error"], "result": job["result"], "auto_recovery": self.jobs.recovery_info(job)}

    def job_status(self, job_id: str) -> dict:
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        st = self._status(JOB_TO_READINESS.get(job["state"], Readiness.PREPARING), job)
        st["events"] = self.jobs.events(job_id)
        return st

    # ================================================================ worker ====
    def resume(self) -> list[str]:
        """After a restart: running jobs whose lease expired return to QUEUED so they run again (idempotently); nothing is lost or duplicated."""
        resumed = []
        for j in self.jobs.list():
            if j["state"] in (JobState.DISCOVERING, JobState.DOWNLOADING, JobState.VALIDATING) and (parse_ts(j["lease_until"]) or self.clock()) <= self.clock():
                self.jobs.transition(j["job_id"], JobState.QUEUED, reason="resumed after interruption")
                resumed.append(j["job_id"])
        return resumed

    def tick(self, *, max_jobs: int = 25, max_seconds: float | None = None) -> list[dict]:
        """Run due jobs once each (bounded by count and wall time). Returns what ran. Safe to call from any scheduler; offline processes do nothing."""
        if self.offline:
            return []
        t0 = time.monotonic()
        ran = []
        self.jobs.recover_due()  # low-frequency, service-owned re-checks of requests that reached their deadline
        for job in self.jobs.due():
            if len(ran) >= max_jobs or (max_seconds is not None and time.monotonic() - t0 > max_seconds):
                break
            ran.append(self.run_job(job["job_id"], budget_s=None if max_seconds is None else max(1.0, max_seconds - (time.monotonic() - t0))))
        return ran

    def work(self, max_seconds: float = 300.0, *, poll_s: float = 5.0, sleep=time.sleep) -> dict:
        """Operator/scheduler entry point: resume interrupted jobs, then run due jobs until nothing is open or the wall-time budget ends.
        Jobs waiting for a scheduled retry stay PREPARING_DATA in the database; a later invocation (or scheduler tick) continues them."""
        t0 = time.monotonic()
        resumed = self.resume()
        ran: list[str] = []
        while time.monotonic() - t0 < max_seconds and not self.offline:
            done = self.tick(max_seconds=max(1.0, max_seconds - (time.monotonic() - t0)))
            ran += [d.get("job_id", "") for d in done]
            open_jobs = [j for j in self.jobs.list() if j["state"] not in TERMINAL]
            if not open_jobs:
                break
            if not done:
                nxt = [parse_ts(j["next_attempt_at"]) for j in open_jobs if j["next_attempt_at"]]
                wait = poll_s if not nxt else max(0.2, min(poll_s, (min(nxt) - self.clock()).total_seconds()))
                if time.monotonic() - t0 + wait >= max_seconds:
                    break
                sleep(wait)
        open_jobs = [j for j in self.jobs.list() if j["state"] not in TERMINAL]
        return {"resumed": resumed, "jobs_run": len(ran), "open_jobs": [self.describe_job(j) for j in open_jobs], "offline": self.offline,
                "note": "open jobs continue on the next worker run; no resubmission is needed" if open_jobs else "no open jobs"}

    # -- long-running worker (deployment mode) ---------------------------------------------------------------
    @property
    def heartbeat_path(self) -> Path:
        return self.db.path.parent / "worker_heartbeat.json"

    def _write_heartbeat(self, body: dict) -> None:
        tmp = self.heartbeat_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(body, sort_keys=True, indent=1, default=str), encoding="utf-8")
        tmp.replace(self.heartbeat_path)  # atomic

    def worker_health(self, stale_after_s: float = 120.0) -> dict:
        """Last heartbeat of the long-running worker (``data worker``), and whether it looks alive."""
        try:
            hb = json.loads(self.heartbeat_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"running": False, "heartbeat": None, "note": "no worker heartbeat recorded; start `python3 -m heimdall data worker`"}
        age = (self.clock() - parse_ts(hb["at"])).total_seconds()
        return {"running": (not hb.get("stopped")) and age <= max(stale_after_s, 3 * float(hb.get("max_idle_s", 30.0))), "heartbeat_age_s": age, "heartbeat": hb}

    def _next_wake(self) -> datetime | None:
        times = [parse_ts(j["next_attempt_at"]) for j in self.jobs.list({JobState.PREPARING_DATA}) if j["next_attempt_at"]]
        times += [parse_ts(i["next_check_at"]) for i in (self.jobs.recovery_info(j) for j in self.jobs.list({JobState.UNAVAILABLE_AFTER_DEADLINE}))
                  if i and i["active"]]
        return min(times, default=None)

    def serve(self, stop, *, max_idle_s: float = 30.0, tick_budget_s: float = 300.0, max_ticks: int | None = None) -> dict:
        """Run until ``stop`` (a ``threading.Event``) is set: resume interrupted work, run due jobs, recover deadline-expired requests on
        the recorded policy, write an atomic heartbeat each loop, and sleep until the next scheduled attempt (at most ``max_idle_s``).
        Stopping is graceful: the current job finishes its bounded step; a hard kill is recovered by lease expiry + ``resume()``."""
        import os
        if self.offline:
            raise HeimdallError.of(ErrorCode.CONFIG_INVALID, "the data worker cannot run with HEIMDALL_OFFLINE set")
        import threading
        state = {"started_at": now_iso(self.clock()), "ticks": 0, "jobs_run": 0, "open_jobs": None, "busy": False}
        lock = threading.Lock()
        def beat(stopped: bool = False) -> None:
            with lock:  # the heartbeat never touches the database, so it cannot contend with the job being run
                self._write_heartbeat({"pid": os.getpid(), "at": now_iso(self.clock()), **state, "max_idle_s": max_idle_s, "stopped": stopped})
        beat()
        resumed = self.resume()
        state["resumed_at_start"] = resumed
        done = threading.Event()
        def pulse() -> None:  # liveness while a long bounded job step runs
            while not done.wait(max(1.0, min(max_idle_s, 15.0))):
                beat()
        threading.Thread(target=pulse, name="heimdall-heartbeat", daemon=True).start()
        try:
            while not stop.is_set() and (max_ticks is None or state["ticks"] < max_ticks):
                state["busy"] = True
                ran = self.tick(max_seconds=tick_budget_s)
                state.update(busy=False, ticks=state["ticks"] + 1, jobs_run=state["jobs_run"] + len(ran),
                             open_jobs=sum(j["state"] not in TERMINAL for j in self.jobs.list()))
                beat()
                if not ran:
                    nxt = self._next_wake()
                    wait = max_idle_s if nxt is None else min(max_idle_s, max(0.2, (nxt - self.clock()).total_seconds()))
                    stop.wait(wait)
        finally:
            done.set()
            beat(stopped=True)
        return {"ticks": state["ticks"], "jobs_run": state["jobs_run"], "resumed": resumed, "stopped_gracefully": True}

    def run_job(self, job_id: str, budget_s: float | None = None) -> dict:
        job = self.jobs.get(job_id)
        if job is None or job["state"] in TERMINAL:
            return job or {}
        if self.jobs.expired(job):
            return self.jobs.transition(job_id, JobState.UNAVAILABLE_AFTER_DEADLINE, reason="deadline reached before the data could be acquired",
                                        detail={"attempts": job["attempts"]})
        self.jobs.record_attempt(job_id)
        job = self.jobs.get(job_id)
        if job["kind"] == KIND_INDEX:
            return self._run_index(job)
        if job["kind"] == KIND_ACQUIRE:
            return self._run_acquire(job, budget_s)
        if job["kind"] == KIND_PREFETCH:
            return self._run_prefetch(job)
        if job["kind"] in self.handlers:
            return self.handlers[job["kind"]](self, job)
        return self.jobs.transition(job_id, JobState.INVALID_SOURCE_DATA, reason=f"unknown job kind {job['kind']}")

    # -- route health bookkeeping --------------------------------------------------------------------------
    def _note_routes(self, attempts: list[dict] | None, latency: float, failed_error: str | None = None) -> None:
        now = now_iso(self.clock())
        for a in attempts or []:
            if a.get("outcome") == "SKIPPED_CIRCUIT_OPEN":
                continue
            self.db.execute("INSERT INTO route_events(at,route,ok,error_class,latency_s) VALUES(?,?,?,?,?)",
                            (now, a["route"], int(a["outcome"] == "OK"), None if a["outcome"] == "OK" else f"{a.get('error_code')}:{a.get('http_status') or ''}", latency))

    def _breaker_hint(self) -> float:
        br = getattr(self.source, "breakers", None)
        return min((b.retry_in_s() for b in br.values()), default=0.0) if br else 0.0

    # -- INDEX ---------------------------------------------------------------------------------------------------
    def _run_index(self, job: dict) -> dict:
        jid, payload = job["job_id"], job["payload"]
        region = RegionSpec.from_dict(payload["region"])
        self.jobs.transition(jid, JobState.DISCOVERING, reason="searching the archive", lease=True)
        filters = {}
        if region.detectors:
            filters["detectors"] = ",".join(str(d) for d in sorted(region.detectors))
        if region.mjd_start is not None:
            filters["mjd_start"] = repr(float(region.mjd_start))
        if region.mjd_end is not None:
            filters["mjd_end"] = repr(float(region.mjd_end))
        if region.collections:
            filters["collections"] = ",".join(sorted(region.collections))
        t0 = time.monotonic()
        try:
            res = self.source.discover(DiscoveryQuery(region=SkyRegion(ra_deg=region.ra_deg, dec_deg=region.dec_deg, radius_deg=region.radius_deg), filters=filters))
        except HeimdallError as exc:
            self._note_routes((getattr(self.source, "last_discovery", None) or {}).get("attempts"), time.monotonic() - t0)
            return self.jobs.schedule_retry(self.jobs.get(jid), error={"code": exc.code.value, "message": exc.info.message, "detail": dict(exc.info.detail)},
                                            reason=f"archive unavailable ({_error_class(exc)}); retrying automatically", min_delay_s=self._breaker_hint())
        ld = getattr(self.source, "last_discovery", None) or {}
        self._note_routes(ld.get("attempts"), time.monotonic() - t0)
        route = ld.get("route_used") or getattr(self.source, "source_id", None)
        recs = [self._record(o, route, res.provenance.retrieved_at) for o in res.observations]
        scope = payload.get("scope", SCOPE_CURRENT)
        if scope == SCOPE_CURRENT and any(r.legacy for r in recs):
            recs = [r for r in recs if not r.legacy]  # legacy releases are never indexed as current data
        self.inventory.register(recs)
        if not recs and res.outcome is ResultOutcome.OK:
            return self.jobs.schedule_retry(self.jobs.get(jid), error={"code": "DATA_MALFORMED", "message": "answer lists only legacy releases"},
                                            reason="answer contained no current-release observation; retrying automatically", min_delay_s=self._breaker_hint())
        sid = self.inventory.record_sync(region, scope, [(r.obs_id, r.release) for r in recs], completed_at=now_iso(self.clock()), route=route,
                                         from_cache=res.provenance.origin.value == "CACHED")
        outcome = {"sync_id": sid, "n_observations": len(recs), "route": route, "discovery_outcome": res.outcome.value}
        if not recs and res.outcome not in (ResultOutcome.NO_OBSERVATIONS, ResultOutcome.OK):
            # rows were returned but none could be parsed: an archive contract problem, not an empty sky. Retry; never NO_COVERAGE.
            return self.jobs.schedule_retry(self.jobs.get(jid), error={"code": "DATA_MALFORMED", "message": "discovery rows could not be parsed"},
                                            reason="archive answer could not be parsed; retrying automatically", min_delay_s=self._breaker_hint())
        if not recs:
            return self.jobs.transition(jid, JobState.NO_COVERAGE, reason="the archive answered successfully and lists no matching observation", result=outcome)
        return self.jobs.transition(jid, JobState.COMPLETE, reason="region indexed", result=outcome)

    def _record(self, o, route, retrieved_at) -> ObsRecord:
        prod = o.provenance.products[0] if o.provenance.products else None
        release = str(_md(o, "release") or (prod.release if prod else "") or "synthetic")
        synthetic = o.provenance.origin.value == "SYNTHETIC_TEST_FIXTURE" or bool(getattr(o, "is_synthetic", False))
        centre = _md(o, "footprint_centre_deg")
        ra, dec = (float(x) for x in centre.split(",")) if centre else (None, None)
        det = _md(o, "detector")
        t0 = _md(o, "time_start_mjd") if _md(o, "time_start_mjd") is not None else _md(o, "mjd_avg_utc")
        t1 = _md(o, "time_end_mjd") if _md(o, "time_end_mjd") is not None else t0
        return ObsRecord(obs_id=o.obs_id, release=release, collection=release, detector=int(det) if det is not None else 0,
                         access_reference=(prod.access_reference if prod else "") or "", discovered_at=now_iso(self.clock()), t_min=t0, t_max=t1,
                         wl_min_um=_md(o, "wavelength_min"), wl_max_um=_md(o, "wavelength_max"), footprint=_md(o, "footprint"), s_ra=ra, s_dec=dec, route=route,
                         archive_checksum=f"{prod.checksum_algorithm}:{prod.checksum}" if prod and prod.checksum else None,
                         product_version=prod.product_version if prod else None, source_version=str(_md(o, "pipeline_version") or "") or None,
                         origin="SYNTHETIC_TEST_FIXTURE" if synthetic else "LIVE", legacy=(not synthetic and release not in CURRENT_COLLECTIONS), synthetic=synthetic)

    # -- ACQUIRE -------------------------------------------------------------------------------------------------
    def _run_acquire(self, job: dict, budget_s: float | None) -> dict:
        jid = job["job_id"]
        plan = ProductPlan.from_dict(job["payload"]["plan"])
        progress = job["progress"] or {}
        items = progress.setdefault("items", {})
        self.jobs.transition(jid, JobState.DOWNLOADING, reason="acquiring products", lease=True)
        t0 = time.monotonic()
        failures, last_error = 0, None
        todo = [(i.key, "obs", i) for i in plan.items] + [(calibration_key(k, d), "calib", (k, d)) for k, d in plan.calibrations]
        for key, kind, what in todo:
            if self.store.get(key, count=True) is not None:
                items[key] = {"state": "READY"}
                continue
            st = items.get(key, {})
            if st.get("state") == "QUARANTINED":
                continue
            if failures >= self.cfg.max_download_failures_per_run or (budget_s is not None and time.monotonic() - t0 > budget_s):
                break
            try:
                self._fetch(key, kind, what)
                items[key] = {"state": "READY"}
                self.jobs.transition(jid, JobState.VALIDATING, reason=f"validated {key}", lease=True)
                self.jobs.transition(jid, JobState.DOWNLOADING, reason="acquiring products", lease=True)
            except InvalidProduct as bad:
                n = st.get("corrupt_attempts", 0) + 1
                items[key] = {"state": "QUARANTINED" if n > self.cfg.corrupt_retries else "RETRY_CORRUPT", "corrupt_attempts": n, "reason": bad.reason, "kind": bad.kind}
                if n > self.cfg.corrupt_retries:
                    self.db.bump("quarantined_products")
                else:
                    failures += 1
                    last_error = {"code": "DATA_MALFORMED", "message": bad.reason}
            except HeimdallError as exc:
                failures += 1
                last_error = {"code": exc.code.value, "message": exc.info.message, "detail": dict(exc.info.detail)}
                items[key] = {"state": "WAITING", "error": _error_class(exc), "attempts": st.get("attempts", 0) + 1}
                self.db.execute("INSERT INTO route_events(at,route,ok,error_class,latency_s) VALUES(?,?,?,?,?)",
                                (now_iso(self.clock()), "calibration" if kind == "calib" else "product", 0, _error_class(exc), time.monotonic() - t0))
        job = self.jobs.get(jid)
        pending = [k for k, _, _ in todo if self.store.get(k) is None and items.get(k, {}).get("state") != "QUARANTINED"]
        quarantined = [k for k, v in items.items() if v.get("state") == "QUARANTINED"]
        progress["counts"] = {"ready": sum(1 for v in items.values() if v.get("state") == "READY"), "pending": len(pending), "quarantined": len(quarantined),
                              "total": len(todo)}
        if pending:
            return self.jobs.schedule_retry(job, error=last_error or {"code": "INCOMPLETE", "message": "run budget exhausted"}, progress=progress,
                                            reason=f"{len(pending)} of {len(todo)} products still to acquire; retrying automatically", min_delay_s=self._breaker_hint())
        required_calib_bad = any(k.startswith("calib:") for k in quarantined)
        ready_obs = [i for i in plan.items if self.store.get(i.key) is not None]
        if required_calib_bad or not ready_obs:
            return self.jobs.transition(jid, JobState.INVALID_SOURCE_DATA, reason="required products failed validation and were quarantined", progress=progress,
                                        result={"quarantined": quarantined})
        return self.jobs.transition(jid, JobState.COMPLETE, reason="all required verified data are stored locally" + (f"; {len(quarantined)} product(s) quarantined" if quarantined else ""),
                                    progress=progress, result={"ready": len(ready_obs), "quarantined": quarantined})

    def _fetch(self, key: str, kind: str, what) -> None:
        if kind == "calib":
            ckind, det = what
            t0 = time.monotonic()
            path, prod = self.source.retrieve_calibration(ckind, det)
            self.db.execute("INSERT INTO route_events(at,route,ok,error_class,latency_s) VALUES(?,?,?,?,?)", (now_iso(self.clock()), "calibration", 1, None, time.monotonic() - t0))
            self.store.ingest(path, key, kind="calibration", origin="LIVE", access_reference=prod.access_reference, route="calibration",
                              meta={"product_id": prod.product_id, "release": prod.release})
            return
        i: PlanItem = what
        options = {"access_reference": i.access_reference, "release": i.release}
        region = None
        if i.cutout_size_deg is not None:
            options["cutout_size_deg"] = repr(float(i.cutout_size_deg))
            region = SkyRegion(ra_deg=i.cutout_ra_deg % 360.0, dec_deg=i.cutout_dec_deg, radius_deg=i.cutout_size_deg / 2.0)
        t0 = time.monotonic()
        rr = self.source.retrieve(RetrievalRequest(obs_id=i.obs_id, region=region, options=options))
        self.db.execute("INSERT INTO route_events(at,route,ok,error_class,latency_s) VALUES(?,?,?,?,?)", (now_iso(self.clock()), "product", 1, None, time.monotonic() - t0))
        p = rr.products[0]
        origin = "SYNTHETIC_TEST_FIXTURE" if rr.provenance.origin.value == "SYNTHETIC_TEST_FIXTURE" else "LIVE"
        self.store.ingest(Path(p.location), key, kind="science", origin=origin, obs_id=i.obs_id, release=i.release, access_reference=i.access_reference,
                          route="irsa-product" if origin == "LIVE" else "local-fixture", archive_checksum=i.archive_checksum,
                          meta={"cutout": [i.cutout_ra_deg, i.cutout_dec_deg, i.cutout_size_deg], "retrieval_origin": rr.provenance.origin.value})
        self.db.bump("bytes_retrieved", Path(p.location).stat().st_size)

    # ================================================================ operations / metrics ====
    def metrics(self) -> dict:
        now = self.clock()
        jobs = self.jobs.list()
        by_state: dict[str, int] = {}
        for j in jobs:
            by_state[j["state"].value] = by_state.get(j["state"].value, 0) + 1
        open_jobs = [j for j in jobs if j["state"] not in TERMINAL]
        oldest = min((parse_ts(j["created_at"]) for j in open_jobs), default=None)
        awaiting = [j for j in jobs if j["state"] is JobState.PREPARING_DATA]
        routes: dict[str, dict] = {}
        for r in self.db.query("SELECT route, COUNT(*) n, SUM(ok) ok, AVG(latency_s) lat, MAX(latency_s) mx FROM route_events GROUP BY route"):
            errs = {e["error_class"]: e["n"] for e in self.db.query("SELECT error_class, COUNT(*) n FROM route_events WHERE route=? AND ok=0 GROUP BY error_class", (r["route"],))}
            routes[r["route"]] = {"requests": r["n"], "ok": r["ok"] or 0, "mean_latency_s": r["lat"], "max_latency_s": r["mx"], "error_classes": errs}
        hits, misses = self.db.counter("store_hits"), self.db.counter("store_misses")
        breakers = {k: b.state for k, b in (getattr(self.source, "breakers", None) or {}).items()}
        fresh = self.freshness()
        return {"jobs": {"by_state": by_state, "queue_depth": len(open_jobs), "oldest_open_age_s": (now - oldest).total_seconds() if oldest else 0.0,
                         "awaiting_upstream_recovery": len(awaiting), "total_attempts": sum(j["attempts"] for j in jobs),
                         "next_retries": sorted(j["next_attempt_at"] for j in awaiting if j["next_attempt_at"])[:5]},
                "routes": routes, "circuit_breakers": breakers, "store": self.store.usage(),
                "cache": {"hits": hits, "misses": misses, "hit_rate": hits / (hits + misses) if hits + misses else None},
                "bytes_retrieved": self.db.counter("bytes_retrieved"), "validation_failures": self.db.counter("validation_failures"),
                "quarantined_products": self.db.counter("quarantined_products"), "freshness_lag_s": fresh["max_index_age_s"],
                "worker": self.worker_health(),
                "awaiting_automatic_recovery": sum(bool((self.jobs.recovery_info(j) or {}).get("active")) for j in jobs)}

    def freshness(self, region: RegionSpec | None = None) -> dict:
        """What the local index represents -- never 'live', never all-sky unless proven (it cannot be: syncs are per region)."""
        now = self.clock()
        syncs = self.db.query("SELECT * FROM syncs WHERE scope=? ORDER BY completed_at DESC", (self.cfg.scope,))
        if region is not None:
            s = self.inventory.covering_sync(region, self.cfg.scope)
            syncs = [s] if s is not None else []
        pending_refresh = [j for j in self.jobs.list(kind=KIND_INDEX) if j["state"] not in TERMINAL and (j["payload"].get("refresh_of") or j["attempts"] > 0)]
        regs = []
        for s in syncs:
            age = (now - parse_ts(s["completed_at"])).total_seconds()
            regs.append({"sync_id": s["sync_id"], "cone": [s["ra"], s["dec"], s["radius"]], "collections": json.loads(s["collections"]), "last_successful_refresh": s["completed_at"],
                         "age_s": age, "n_observations": s["n_obs"], "newest_observation_mjd": s["newest_t_max"], "route": s["route"],
                         "from_cached_discovery": bool(s["from_cache"]), "stale": age > self.cfg.index_max_age_s})
        return {"scope": self.cfg.scope, "data_origin_statement": "local inventory (cached archive answers), not a live archive query",
                "all_sky_complete": False, "complete_through": "only the indexed regions listed, as of each region's last_successful_refresh; the archive's own "
                                                              "SIA2 availability lags new data by about a day",
                "releases_indexed": self.inventory.counts(self.cfg.scope), "regions": regs,
                "max_index_age_s": max((r["age_s"] for r in regs), default=0.0),
                "refresh_delayed_by_upstream_failure": any(j["state"] is JobState.PREPARING_DATA for j in pending_refresh)}

    def sync(self, *, max_age_s: float | None = None) -> list[dict]:
        """Queue refresh jobs for indexed regions older than ``max_age_s`` (default: the configured index age). Returns the jobs."""
        limit = self.cfg.index_max_age_s if max_age_s is None else max_age_s
        out = []
        newest: dict = {}
        for s in self.db.query("SELECT * FROM syncs WHERE scope=? ORDER BY sync_id", (self.cfg.scope,)):
            newest[(s["ra"], s["dec"], s["radius"], s["detectors"], s["mjd_start"], s["mjd_end"], s["collections"])] = s  # the latest sync of each region
        for s in newest.values():
            if (self.clock() - parse_ts(s["completed_at"])).total_seconds() >= limit:
                reg = RegionSpec(s["ra"], s["dec"], s["radius"], tuple(json.loads(s["detectors"])) if s["detectors"] else None, s["mjd_start"], s["mjd_end"],
                                 tuple(json.loads(s["collections"])))
                job, _ = self.jobs.submit(KIND_INDEX, {"region": reg.to_dict(), "scope": self.cfg.scope, "refresh_of": s["sync_id"]})
                out.append(job)
        return out


def plan_for(obs: list[ObsRecord], region: RegionSpec, cutout_size_deg: float | None, max_observations: int, gap_days: float = 5.0) -> ProductPlan:
    """A deterministic, bounded plan: evenly spaced picks over the observations by time (first and last kept), plus one ePSF per detector."""
    obs = sorted(obs, key=lambda o: (o.t_min if o.t_min is not None else float("inf"), o.obs_id))
    if len(obs) > max_observations:
        import numpy as np
        idx = sorted({int(round(i)) for i in np.linspace(0, len(obs) - 1, max_observations)})
        obs = [obs[i] for i in idx]
    items = tuple(PlanItem(o.obs_id, o.release, o.detector, o.access_reference,
                           region.ra_deg if cutout_size_deg else None, region.dec_deg if cutout_size_deg else None, cutout_size_deg, o.archive_checksum) for o in obs)
    return ProductPlan(items, tuple(sorted({("epsf", o.detector) for o in obs if o.detector})))
