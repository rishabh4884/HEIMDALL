"""Dependency-free HTTP JSON transport over ``heimdall.api.Engine`` for the frontend (``python3 -m heimdall serve``).

Contract ``heimdall.http/1.0``. Every path is readiness-first: a request is validated and acknowledged quickly with either an existing
result, a local-inventory answer, or a durable job reference. No request handler contacts the archive or runs an analysis inline
(``analyse_inline=False``); workers do that (``--with-worker`` starts one in this process, or run ``python3 -m heimdall data worker``).

Endpoints (prefix ``/api/v1``):

  GET  /health                         software/schema versions, offline flag, worker heartbeat
  GET  /status                         data-service metrics and index freshness
  GET  /capabilities                   versioned capability contract (analyses, prerequisites, limits, validation state)
  GET  /compare?a=&b=                  two target versions (tinv:) or two runs (R...): differences, conclusion changes, comparability
  GET  /evidence/lookup?obs_id=|sha256=  reverse lookup: every version, claim, manifest and run that depends on an input
  GET  /targets                        registered NASA targets with their saved result summary
  GET  /targets/{target_id}            latest usable saved result (404 if none)
  GET  /targets/{target_id}/attempt    latest attempt (may differ from the latest usable result; 404 if none)
  GET  /targets/{target_id}/versions   every saved version (newest first) with supersession links
  GET  /targets/{target_id}/claims     auditable claims of the latest usable result (heimdall.claims/1.0); also /investigations/{id}/claims
  GET  /investigations/{investigation_id}   an immutable saved investigation version
  GET  /coverage?ra&dec&radius[&detectors][&offset][&limit]   observations from the local index, or an INDEX job
  POST /investigations                 {"target_id": "NASA-01"} or {"ra":..,"dec":..,"category":..}, optional "options"
                                       -> 200 completed result, or 202 data_status with a job id (reconnect with GET /jobs/{id})
  POST /runs                           explicit blind field run {"ra","dec","radius"<=0.5,"detectors","max_observations"<=32,"cutout_size_deg"}
                                       -> 200 stored identical run, or 202 durable FIELD_INVESTIGATION job
  GET  /runs/{run_id}                  a saved blind-pipeline run (heimdall investigation.json)
  GET  /jobs[?state=&kind=&limit=]     job list
  GET  /jobs/{job_id}                  job status (target jobs: the investigation status or completed result)
  POST /jobs/{job_id}/cancel
  GET  /manifests/{manifest_id}        frozen input manifest

Errors: ``{"schema": "heimdall.http/1.0", "error": {"code", "message"}}`` with 400/401/404/405/413/500. Optional bearer token
(``HEIMDALL_API_TOKEN``) is the authentication integration point; the server binds to 127.0.0.1 by default.
"""

from __future__ import annotations

import hmac
import json
import math
import os
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .contracts import HeimdallError

HTTP_SCHEMA = "heimdall.http/1.0"
MAX_BODY = 64 * 1024
RE_TARGET = re.compile(r"^(NASA-0[1-9]|COORD-[0-9.]+[+-][0-9.]+)$")
RE_INV = re.compile(r"^tinv:(NASA-0[1-9]|COORD-[0-9.]+[+-][0-9.]+):([0-9a-f]{8})$")
RE_JOB = re.compile(r"^job:[a-z_]{1,40}:[0-9a-f]{12}$")
RE_MAN = re.compile(r"^man:[0-9a-f]{8,64}$")
RE_RUN = re.compile(r"^R\d{8}T\d{6}-[0-9a-f]{8}$")
CATEGORIES = {"MOVING_OBJECT", "VARIABLE_STAR"}
OPTION_TYPES = {"max_observations": int, "detectors": list, "mjd_start": float, "mjd_end": float, "cutout_size_deg": float,
                "blind_pipeline": bool, "context": bool}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _num(q: dict, name: str, lo: float, hi: float, default: float | None = None) -> float:
    raw = q.get(name, [None])[0]
    if raw is None:
        if default is None:
            raise ApiError(400, "INPUT_INVALID", f"missing parameter {name}")
        return default
    try:
        v = float(raw)
    except ValueError:
        raise ApiError(400, "INPUT_INVALID", f"{name} must be a number") from None
    if not (lo <= v <= hi):
        raise ApiError(400, "INPUT_INVALID", f"{name} must be within [{lo}, {hi}]")
    return v


def _ra(v: float) -> float:
    r = v % 360.0
    return 0.0 if r >= 360.0 else r  # (-1e-20) % 360.0 == 360.0 in floating point


COVERAGE_GRID_DEG = 0.01


def _snap_cone(ra: float, dec: float, radius: float) -> tuple[float, float, float]:
    """Snap a browsing cone to a 0.01-degree grid (enlarging the radius so it still contains the requested cone), so map panning maps to a
    bounded set of index jobs instead of one job per floating-point coordinate."""
    g = COVERAGE_GRID_DEG
    sra, sdec = _ra(round(ra / g) * g), max(-90.0, min(90.0, round(dec / g) * g))
    shift = math.hypot(g / 2 * max(math.cos(math.radians(min(abs(dec), 89.0))), 0.0), g / 2)
    return round(sra, 6), round(sdec, 6), round(math.ceil((radius + shift) / g) * g, 6)


def _detectors(raw) -> tuple[int, ...] | None:
    if raw in (None, "", []):
        return None
    items = raw.split(",") if isinstance(raw, str) else raw
    try:
        dets = tuple(sorted({int(d) for d in items}))
    except (TypeError, ValueError):
        raise ApiError(400, "INPUT_INVALID", "detectors must be integers 1-6") from None
    if not dets or not all(1 <= d <= 6 for d in dets):
        raise ApiError(400, "INPUT_INVALID", "detectors must be integers 1-6")
    return dets


def _options(raw: dict | None) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ApiError(400, "INPUT_INVALID", "options must be an object")
    out = {}
    for k, v in raw.items():
        if k not in OPTION_TYPES:
            raise ApiError(400, "INPUT_INVALID", f"unknown option {k!r}; allowed: {sorted(OPTION_TYPES)}")
        if k == "detectors":
            out["detectors"] = _detectors(v)
        elif OPTION_TYPES[k] is bool:
            if not isinstance(v, bool):
                raise ApiError(400, "INPUT_INVALID", f"{k} must be true/false")
            out[k] = v
        else:
            try:
                out[k] = OPTION_TYPES[k](v)
            except (TypeError, ValueError):
                raise ApiError(400, "INPUT_INVALID", f"{k} must be a number") from None
    if "max_observations" in out and not 1 <= out["max_observations"] <= 64:
        raise ApiError(400, "INPUT_INVALID", "max_observations must be 1-64")
    if "cutout_size_deg" in out and not 0.01 <= out["cutout_size_deg"] <= 0.5:
        raise ApiError(400, "INPUT_INVALID", "cutout_size_deg must be 0.01-0.5")
    return out


class Api:
    """Routing and validation, independent of the socket layer (tested directly)."""

    def __init__(self, engine, worker_running=lambda: False) -> None:
        self.engine = engine
        self.svc = engine.data_service()  # created eagerly: one service object per process
        self.targets_dir = Path(engine.settings.state_dir) / "targets"
        self.worker_running = worker_running

    def handle(self, method: str, path: str, query: dict, body: dict | None) -> tuple[int, dict]:
        parts = [p for p in path.split("/") if p]
        if parts[:2] != ["api", "v1"]:
            raise ApiError(404, "NOT_FOUND", "unknown path; the API lives under /api/v1")
        p = parts[2:]
        route = (method, p[0] if p else "")
        if route == ("GET", "health"):
            return 200, self.health()
        if route == ("GET", "capabilities") and len(p) == 1:
            from .scientist import capabilities
            return 200, capabilities(self.engine)
        if route == ("GET", "compare") and len(p) == 1:
            from .scientist import compare
            a, b = (query.get("a", [""])[0], query.get("b", [""])[0])
            for x in (a, b):
                if not (RE_INV.match(x) or RE_RUN.match(x)):
                    raise ApiError(400, "INPUT_INVALID", "a and b must be tinv:<target>:<8 hex> or run ids")
            return 200, compare(self.engine, a, b)
        if route == ("GET", "evidence") and len(p) == 2 and p[1] == "lookup":
            from .scientist import lookup
            obs = query.get("obs_id", [None])[0]
            sha = query.get("sha256", [None])[0]
            if obs is not None and not re.match(r"^[0-9A-Za-z_]{3,64}$", obs):
                raise ApiError(400, "INPUT_INVALID", "obs_id must be an observation identifier")
            if sha is not None and not re.match(r"^[0-9a-f]{64}$", sha):
                raise ApiError(400, "INPUT_INVALID", "sha256 must be 64 hex characters")
            if not obs and not sha:
                raise ApiError(400, "INPUT_INVALID", "give obs_id or sha256")
            return 200, lookup(self.engine, obs_id=obs, sha256=sha)
        if route == ("GET", "status"):
            return 200, {"metrics": self.svc.metrics(), "freshness": self.svc.freshness()}
        if route == ("GET", "targets"):
            if len(p) == 1:
                return 200, {"targets": self.engine.targets()}
            tid = self._target_id(p[1])
            if len(p) == 2:
                return 200, self._read_json(self.targets_dir / tid / "latest.json", f"no saved investigation for {tid}")
            if len(p) == 3 and p[2] == "attempt":
                return 200, self._read_json(self.targets_dir / tid / "latest_attempt.json", f"no recorded attempt for {tid}")
            if len(p) == 3 and p[2] == "versions":
                return 200, self.versions(tid)
            if len(p) == 3 and p[2] == "claims":
                from .targets.claims import build_claims
                return 200, build_claims(self._read_json(self.targets_dir / tid / "latest.json", f"no saved investigation for {tid}"))
        if route == ("GET", "investigations") and len(p) in (2, 3) and (len(p) == 2 or p[2] == "claims"):
            m = RE_INV.match(p[1])
            if not m:
                raise ApiError(400, "INPUT_INVALID", "investigation id must look like tinv:<target>:<8 hex>")
            doc = self._read_json(self.targets_dir / m.group(1) / f"{m.group(2)}.json", f"no saved investigation {p[1]}")
            if doc.get("investigation_id") != p[1]:
                raise ApiError(404, "NOT_FOUND", f"no saved investigation {p[1]}")
            if len(p) == 3:
                from .targets.claims import build_claims
                return 200, build_claims(doc)
            return 200, doc
        if route == ("POST", "investigations") and len(p) == 1:
            return self.submit(body or {})
        if route == ("POST", "runs") and len(p) == 1:
            return self.submit_run(body or {})
        if route == ("GET", "runs") and len(p) == 2:
            if not RE_RUN.match(p[1]):
                raise ApiError(400, "INPUT_INVALID", "run id must look like R<yyyymmdd>T<hhmmss>-<8 hex>")
            try:
                return 200, self.engine.load(p[1])
            except Exception:  # noqa: BLE001 - unknown or unreadable run directory
                raise ApiError(404, "NOT_FOUND", f"no saved run {p[1]}") from None
        if route == ("GET", "coverage") and len(p) == 1:
            return 200, self.coverage(query)
        if route == ("GET", "jobs"):
            if len(p) == 1:
                return 200, self.jobs(query)
            jid = self._job_id(p[1])
            if len(p) == 2:
                return 200, self.job(jid)
        if route == ("POST", "jobs") and len(p) == 3 and p[2] == "cancel":
            jid = self._job_id(p[1])
            if self.svc.jobs.get(jid) is None:
                raise ApiError(404, "NOT_FOUND", f"no job {jid}")
            self.svc.jobs.cancel(jid)
            return 200, self.job(jid)
        if route == ("GET", "manifests") and len(p) == 2:
            if not RE_MAN.match(p[1]):
                raise ApiError(400, "INPUT_INVALID", "manifest id must look like man:<hex>")
            man = self.svc.manifest(p[1])
            if man is None:
                raise ApiError(404, "NOT_FOUND", f"no manifest {p[1]}")
            return 200, man
        if method not in ("GET", "POST"):
            raise ApiError(405, "METHOD_NOT_ALLOWED", f"{method} is not supported")
        raise ApiError(404, "NOT_FOUND", f"no route for {method} {path}")

    # -- handlers --------------------------------------------------------------------------------------------
    def health(self) -> dict:
        from .dataservice.schemas import STATUS_SCHEMA
        return {"schema": HTTP_SCHEMA, "software_version": __version__, "result_schema": "heimdall.target_investigation/1.0",
                "data_status_schema": STATUS_SCHEMA, "offline": bool(self.engine.settings.offline), "worker": self.svc.worker_health(),
                "in_process_worker": self.worker_running()}

    def submit(self, body: dict) -> tuple[int, dict]:
        if not isinstance(body, dict):
            raise ApiError(400, "INPUT_INVALID", "body must be a JSON object")
        unknown = set(body) - {"target_id", "ra", "dec", "category", "options"}
        if unknown:
            raise ApiError(400, "INPUT_INVALID", f"unknown fields {sorted(unknown)}")
        opts = _options(body.get("options"))
        if body.get("target_id") is not None:
            if not isinstance(body["target_id"], str) or not RE_TARGET.match(body["target_id"]) or body["target_id"].startswith("COORD-"):
                raise ApiError(400, "INPUT_INVALID", "target_id must be a registered id (NASA-01..NASA-05); use ra/dec for coordinates")
            kw = {"ref": body["target_id"]}
        else:
            q = {k: [str(body[k])] for k in ("ra", "dec") if body.get(k) is not None}
            cat = body.get("category", "MOVING_OBJECT")
            if cat not in CATEGORIES:
                raise ApiError(400, "INPUT_INVALID", f"category must be one of {sorted(CATEGORIES)}")
            kw = {"ra": _ra(_num(q, "ra", -360.0, 720.0)), "dec": _num(q, "dec", -90.0, 90.0), "category": cat}
        res = self.engine.investigate_target(**kw, analyse_inline=False, wait_s=0.0, **opts)
        if "investigation_id" in res:
            return 200, res
        return 202, res

    def versions(self, tid: str) -> dict:
        """Every saved immutable version of a target investigation, newest first, with supersession links (bounded to 200)."""
        d = self.targets_dir / tid
        if not d.is_dir():
            raise ApiError(404, "NOT_FOUND", f"no saved investigation for {tid}")
        latest = self._read_json(d / "latest.json", "none").get("investigation_id") if (d / "latest.json").is_file() else None
        attempt = self._read_json(d / "latest_attempt.json", "none").get("investigation_id") if (d / "latest_attempt.json").is_file() else None
        out = []
        for f in sorted(d.glob("*.json")):
            if f.name in ("latest.json", "latest_attempt.json") or not re.match(r"^[0-9a-f]{8}\.json$", f.name):
                continue
            doc = self._read_json(f, "missing")
            sup = doc.get("supersedes") or {}
            out.append({"investigation_id": doc.get("investigation_id"), "created_at": doc.get("created_at"), "status": doc.get("status"),
                        "supersedes": sup.get("investigation_id"), "supersession_reason": sup.get("reason"),
                        "is_latest_usable": doc.get("investigation_id") == latest, "is_latest_attempt": doc.get("investigation_id") == attempt})
        out.sort(key=lambda v: v["created_at"] or "", reverse=True)
        superseded = {v["supersedes"]: v["investigation_id"] for v in out if v["supersedes"]}
        for v in out:
            v["superseded_by"] = superseded.get(v["investigation_id"])
        return {"target_id": tid, "latest_usable": latest, "latest_attempt": attempt, "versions": out[:200]}

    def submit_run(self, body: dict) -> tuple[int, dict]:
        """Explicit blind field run (never started by browsing). 200 = stored identical run; 202 = durable job."""
        from .fieldruns import request_field_run
        from .pipeline.runner import InvestigationRequest
        if not isinstance(body, dict):
            raise ApiError(400, "INPUT_INVALID", "body must be a JSON object")
        allowed = {"ra", "dec", "radius", "detectors", "max_observations", "cutout_size_deg", "mjd_start", "mjd_end"}
        unknown = set(body) - allowed
        if unknown:
            raise ApiError(400, "INPUT_INVALID", f"unknown fields {sorted(unknown)}")
        q = {k: [str(v)] for k, v in body.items() if v is not None and k in ("ra", "dec", "radius", "max_observations", "cutout_size_deg", "mjd_start", "mjd_end")}
        req = InvestigationRequest(ra_deg=_ra(_num(q, "ra", -360.0, 720.0)), dec_deg=_num(q, "dec", -90.0, 90.0), radius_deg=_num(q, "radius", 0.005, 0.5, 0.05),
                                   source_id="irsa-spherex", detectors=list(_detectors(body.get("detectors")) or []) or None,
                                   max_observations=int(_num(q, "max_observations", 1, 32, 8)), cutout_size_deg=_num(q, "cutout_size_deg", 0.01, 0.5, 0.1),
                                   mjd_start=_num(q, "mjd_start", 50000, 80000) if "mjd_start" in q else None,
                                   mjd_end=_num(q, "mjd_end", 50000, 80000) if "mjd_end" in q else None)
        out = request_field_run(self.engine, req)
        return (202 if out.get("readiness") == "PREPARING" else 200), out

    def coverage(self, query: dict) -> dict:
        from .dataservice.schemas import RegionSpec
        exact = (_ra(_num(query, "ra", -360.0, 720.0)), _num(query, "dec", -90.0, 90.0), _num(query, "radius", 1e-4, 1.0, 0.05))
        dets = _detectors(query.get("detectors", [None])[0])
        region = RegionSpec(*exact, dets)
        snapped = False
        if self.svc.inventory.covering_sync(region, self.svc.cfg.scope) is None:
            # not indexed: snap to the grid so map panning maps to a bounded set of index jobs (the snapped cone contains the requested one)
            region, snapped = RegionSpec(*_snap_cone(*exact), dets), True
        ra, dec, radius = region.ra_deg, region.dec_deg, region.radius_deg
        out = self.svc.coverage(region, offset=int(_num(query, "offset", 0, 1e7, 0)), limit=int(_num(query, "limit", 1, 2000, 500)))
        return out | {"queried_cone": {"ra_deg": ra, "dec_deg": dec, "radius_deg": radius, "snapped": snapped,
                                       "note": (f"snapped to a {COVERAGE_GRID_DEG} degree grid; contains the requested cone" if snapped
                                                else "answered for the requested cone from an existing index")}}

    def jobs(self, query: dict) -> dict:
        from .dataservice.schemas import JobState
        state = query.get("state", [None])[0]
        try:
            want = {JobState(state)} if state else None
        except ValueError:
            raise ApiError(400, "INPUT_INVALID", f"unknown state {state!r}") from None
        kind = query.get("kind", [None])[0]
        limit = int(_num(query, "limit", 1, 1000, 200))
        rows = self.svc.jobs.list(want, kind)
        return {"total": len(rows), "jobs": [self.svc.describe_job(j) for j in rows[-limit:]]}

    def job(self, jid: str) -> dict:
        job = self.svc.jobs.get(jid)
        if job is None:
            raise ApiError(404, "NOT_FOUND", f"no job {jid}")
        from .fieldruns import KIND_FIELD, field_status
        from .targets.prepare import KIND_TARGET, investigation_status
        if job["kind"] == KIND_TARGET:
            return investigation_status(self.svc, jid)
        if job["kind"] == KIND_FIELD:
            return field_status(self.engine, jid)
        return self.svc.job_status(jid)

    # -- helpers ---------------------------------------------------------------------------------------------
    @staticmethod
    def _target_id(raw: str) -> str:
        if not RE_TARGET.match(raw):
            raise ApiError(400, "INPUT_INVALID", "target id must be NASA-0N or COORD-<ra><+/-dec>")
        return raw

    @staticmethod
    def _job_id(raw: str) -> str:
        if not RE_JOB.match(raw):
            raise ApiError(400, "INPUT_INVALID", "job id must look like job:<kind>:<12 hex>")
        return raw

    def _read_json(self, path: Path, missing: str) -> dict:
        root = self.targets_dir.resolve()
        rp = path.resolve()
        if root not in rp.parents:
            raise ApiError(400, "INPUT_INVALID", "invalid path")
        if not rp.is_file():
            raise ApiError(404, "NOT_FOUND", missing)
        try:
            return json.loads(rp.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:  # a server-side record problem, not a client error
            import sys
            print(f"[http] unreadable record {rp}: {type(exc).__name__}: {exc}", file=sys.stderr)
            raise ApiError(500, "RECORD_UNREADABLE", "a saved record could not be read; see the server log") from None


def make_handler(api: Api, token: str | None, cors_origin: str | None):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"HEIMDALL/{__version__}"

        def log_message(self, fmt, *args):  # concise access log on stderr, no bodies, no headers (tokens stay out of logs)
            import sys
            print(f"[http] {self.address_string()} {fmt % args}", file=sys.stderr)

        def _cors(self) -> None:
            origin = self.headers.get("Origin")
            allowed = cors_origin or ""
            if origin and (origin == allowed or (not allowed and re.match(r"^http://(localhost|127\.0\.0\.1)(:\d+)?$", origin))):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

        def _send(self, status: int, body: dict) -> None:
            data = json.dumps(body, allow_nan=False, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self._cors()
            self.end_headers()
            self.wfile.write(data)

        def _dispatch(self, method: str) -> None:
            try:
                if token and not hmac.compare_digest((self.headers.get("Authorization") or "").encode(), f"Bearer {token}".encode()):
                    raise ApiError(401, "UNAUTHORIZED", "missing or invalid bearer token")
                url = urlsplit(self.path)
                body = None
                if method == "POST":
                    try:
                        n = int(self.headers.get("Content-Length") or 0)
                    except ValueError:
                        raise ApiError(400, "INPUT_INVALID", "invalid Content-Length") from None
                    if n < 0:
                        raise ApiError(400, "INPUT_INVALID", "invalid Content-Length")
                    if n > MAX_BODY:
                        raise ApiError(413, "PAYLOAD_TOO_LARGE", f"body larger than {MAX_BODY} bytes")
                    raw = self.rfile.read(n) if n else b"{}"
                    try:
                        body = json.loads(raw or b"{}")
                    except ValueError:
                        raise ApiError(400, "INPUT_INVALID", "body is not valid JSON") from None
                status, out = api.handle(method, url.path, parse_qs(url.query, max_num_fields=20), body)
                self._send(status, out)
            except ApiError as e:
                self._send(e.status, {"schema": HTTP_SCHEMA, "error": {"code": e.code, "message": e.message}})
            except HeimdallError as e:
                code = getattr(getattr(e, "code", None), "value", "ERROR")
                status = {"INPUT_INVALID": 400, "AUTHENTICATION_FAILED": 502, "ACCESS_DENIED": 502, "ARCHIVE_UNREACHABLE": 503,
                          "ARCHIVE_TIMEOUT": 503, "RETRIEVAL_FAILED": 503}.get(code, 500)
                detail = {k: str(v) for k, v in dict(e.info.detail).items() if k in ("classification", "http_status", "routes", "host", "offline")}
                self._send(status, {"schema": HTTP_SCHEMA, "error": {"code": code, "message": e.info.message, "detail": detail,
                                                                      "category": "UPSTREAM_OUTAGE" if status == 503 else "ERROR"}})
            except ValueError as e:
                self._send(400, {"schema": HTTP_SCHEMA, "error": {"code": "INPUT_INVALID", "message": str(e)}})
            except Exception as e:  # noqa: BLE001 - a stable error body, never a stack trace to the client
                import sys
                import traceback
                traceback.print_exc(file=sys.stderr)
                self._send(500, {"schema": HTTP_SCHEMA, "error": {"code": "INTERNAL", "message": type(e).__name__}})

        def do_GET(self):  # noqa: N802
            self._dispatch("GET")

        def do_POST(self):  # noqa: N802
            self._dispatch("POST")

        def do_OPTIONS(self):  # noqa: N802
            self.send_response(204)
            self._cors()
            self.end_headers()

    return Handler


def serve(engine_factory, host: str = "127.0.0.1", port: int = 8765, with_worker: bool = False) -> None:
    """Run the HTTP server until interrupted. ``with_worker`` also runs the data worker in a thread with its own engine/database connection."""
    import signal
    import sys
    engine = engine_factory()
    stop = threading.Event()
    worker = None
    api = Api(engine, worker_running=lambda: bool(worker and worker.is_alive()))  # initialises the service database first
    if with_worker and not engine.settings.offline:
        wservice = engine_factory().data_service()  # own connection, opened after the API's
        worker = threading.Thread(target=lambda: wservice.serve(stop), name="heimdall-worker", daemon=True)
        worker.start()
    httpd = ThreadingHTTPServer((host, port), make_handler(api, os.environ.get("HEIMDALL_API_TOKEN") or None,
                                                           os.environ.get("HEIMDALL_API_CORS_ORIGIN") or None))
    httpd.daemon_threads = True

    def shutdown(*_):
        stop.set()
        threading.Thread(target=httpd.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    print(f"[http] HEIMDALL API on http://{host}:{port}/api/v1 (worker in process: {bool(worker)})", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.server_close()
        if worker is not None:
            worker.join(timeout=600)
