"""Command-line interface to the HEIMDALL scientific engine.

    python3 -m heimdall version | check | sources | access | coverage | investigate | runs |
                        inspect | reinvestigate | validate | reproduce | skymap | playback | export | config | cache | demo

Run ``python3 -m heimdall <command> -h`` for options. ``check`` and every
command read ``./.env`` if present, then the environment; secrets are never
printed. Exit codes: 0 success, 1 operation failed, 2 usage or configuration
error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import SCHEMA_VERSION, __version__
from .config import load_settings
from .contracts.outcomes import ErrorCode, HeimdallError
from .data_access.registry import default_registry

USAGE = "usage: python3 -m heimdall {version|check|sources|access|coverage|investigate|target|runs|inspect|reinvestigate|validate|reproduce|skymap|playback|export|config|cache|demo}"


def _env_file() -> Path | None:
    p = Path(".env")
    return p if p.is_file() else None


def _check() -> int:
    try:
        settings = load_settings(env_file=_env_file())
    except HeimdallError as exc:
        print(f"configuration error [{exc.code}]: {exc.info.message}", file=sys.stderr)
        return 2
    print("HEIMDALL configuration")
    for key, value in settings.summary().items():
        print(f"  {key}: {value}")
    print(f"  registered adapters: {', '.join(default_registry.available()) or '(none yet)'}")
    try:
        default_registry.create(settings)
    except HeimdallError as exc:
        status = "not ready" if exc.code in (ErrorCode.SOURCE_NOT_CONFIGURED, ErrorCode.CONFIG_INVALID) else "error"
        print(f"  data source: {status} [{exc.code}] {exc.info.message}")
        return 0 if exc.code is ErrorCode.SOURCE_NOT_CONFIGURED else 1
    print("  data source: adapter created (access not checked; run 'access')")
    return 0


def _ints(text: str | None) -> list[int] | None:
    return [int(x) for x in text.split(",") if x.strip()] if text else None


def _strs(text: str | None) -> list[str] | None:
    return [x.strip() for x in text.split(",") if x.strip()] if text else None


def _print(data, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=1, sort_keys=True, default=str))
    elif isinstance(data, dict):
        for k, v in data.items():
            print(f"{k}: {json.dumps(v, default=str) if isinstance(v, (dict, list)) else v}")
    elif isinstance(data, list):
        for item in data:
            print(json.dumps(item, default=str))
    else:
        print(data)


def _print_summary(s: dict) -> None:
    print(f"run {s['run_id']}  [{s['data_origin']}]  execution={s['status']['execution']} outcome={s['status']['outcome']}")
    if s["data_origin"] == "SYNTHETIC":
        print("  SYNTHETIC DATA - NOT ASTRONOMICAL OBSERVATIONS")
    o = s["observations"]
    print(f"  observations: {o['included']}/{o['total']} usable, {o['failed_items']} failed item(s); epochs {s['epochs']}")
    print(f"  detections {s['detections']}, sources {s['sources']}, tracks {s['tracks']}")
    print(f"  candidates by attention: {s['candidates_by_attention'] or '{}'}")
    for name, outcome, n in s["validation"]:
        print(f"  validation {name}: {outcome} (n={n})")
    px = s.get("planet_x") or {}
    if px:
        region = px["region"]
        print(f"  Planet X region status: {region['status']} ({region.get('statement') or region.get('reason')})")
        for c in px.get("candidates", []):
            print(f"  Planet X evaluation {c['candidate_id']}: {c['status']}")
    for w in s["warnings"]:
        print(f"  warning: {w}")
    print(f"  results_hash {s['results_hash']}")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python3 -m heimdall", description="HEIMDALL scientific engine")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("version", help="print software and schema versions")
    sub.add_parser("check", help="validate configuration (never prints secrets)")
    s = sub.add_parser("sources", help="list registered data sources")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("irsa-status", help="probe IRSA TAP, SIA2, direct product and calibration access separately (contacts IRSA)")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("access", help="check access to a data source (contacts the source)")
    s.add_argument("source")
    s.add_argument("--local-root")

    def region(sp):
        sp.add_argument("--source", required=True, help="irsa-spherex or local-fits")
        sp.add_argument("--ra", type=float, required=True, help="ICRS right ascension, degrees")
        sp.add_argument("--dec", type=float, required=True, help="ICRS declination, degrees")
        sp.add_argument("--radius", type=float, required=True, help="region radius, degrees")
        sp.add_argument("--detectors", help="comma-separated detector numbers 1-6")
        sp.add_argument("--collections", help="comma-separated IRSA collections, e.g. spherex_qr2")
        sp.add_argument("--mjd-start", type=float)
        sp.add_argument("--mjd-end", type=float)
        sp.add_argument("--local-root", help="directory for --source local-fits")

    s = sub.add_parser("coverage", help="list observations covering a region (no pixels retrieved; local index first, or a durable job)")
    region(s)
    s.add_argument("--json", action="store_true")
    s.add_argument("--live", action="store_true", help="operator diagnostic: query the archive directly instead of the local index")
    s = sub.add_parser("investigate", help="run the full investigation pipeline on a region")
    s.add_argument("--live", action="store_true", help="operator diagnostic: read the archive directly instead of verified local data")
    region(s)
    s.add_argument("--max-observations", type=int, help="evenly spaced selection in time")
    s.add_argument("--cutout-size", type=float, help="IRSA cutout size in degrees (default 2 x radius)")
    s.add_argument("--validate", action="store_true", help="run the validation procedures on these images")
    s.add_argument("--no-calibration", action="store_true", help="do not fetch any calibration product")
    s.add_argument("--calibration-products", choices=["epsf", "full"], default="epsf",
                   help="full adds the CWAVE/CBAND spectral WCS and the per-pixel solid-angle map (about 50 MB per detector)")
    s.add_argument("--no-context", action="store_true", help="do not query Gaia DR3 or JPL SB Ident")
    s.add_argument("--config", type=Path, help="engine configuration JSON (see 'config template')")
    s.add_argument("--label")
    s.add_argument("--out", type=Path, help="run directory (default: <state_dir>/runs/<run_id>)")
    s.add_argument("--no-svg", action="store_true")
    s.add_argument("--quiet", action="store_true")
    s = sub.add_parser("target", help="named (NASA-01..05) or coordinate-addressable target investigations")
    s.add_argument("action", choices=["list", "resolve", "run", "show", "matrix", "reevaluate", "claims", "followup", "followup-run"])
    s.add_argument("target", nargs="?", help="NASA-01..NASA-05 or a name fragment")
    s.add_argument("--ra", type=float)
    s.add_argument("--dec", type=float)
    s.add_argument("--category", default="MOVING_OBJECT", choices=["MOVING_OBJECT", "VARIABLE_STAR"], help="for --ra/--dec targets")
    s.add_argument("--source", default="irsa-spherex")
    s.add_argument("--local-root")
    s.add_argument("--max-observations", type=int, default=16)
    s.add_argument("--detectors", help="comma-separated detector numbers 1-6 (default: per-target)")
    s.add_argument("--mjd-start", type=float)
    s.add_argument("--mjd-end", type=float)
    s.add_argument("--cutout-size", type=float)
    s.add_argument("--max-seconds", type=float, default=1800.0, help="stop starting new retrievals after this many seconds (result is PARTIAL)")
    s.add_argument("--no-blind", action="store_true", help="skip the blind HEIMDALL pipeline run on the same exposures")
    s.add_argument("--wait", type=float, default=0.0, metavar="SECONDS", help="run the data worker inline for at most this long; on expiry the durable job id and "
                   "status are returned (the request keeps being worked on; do not resubmit)")
    s.add_argument("--no-context", action="store_true")
    s.add_argument("--write", type=Path, help="matrix: write markdown here")
    s.add_argument("--reason", help="reevaluate: why the saved verdict is re-evaluated (recorded in the new version)")
    s.add_argument("--dry-run", action="store_true", help="reevaluate: report the change without writing")
    s.add_argument("--json", action="store_true")
    s.add_argument("--quiet", action="store_true")
    s = sub.add_parser("data", help="SPHEREx data service: inventory, jobs, worker, prefetch, sync, freshness, retention")
    s.add_argument("action", choices=["status", "freshness", "jobs", "job", "work", "worker", "prefetch", "sync", "cancel", "evict", "manifest", "resume", "index"])
    s.add_argument("ref", nargs="?", help="job id (job, cancel) or manifest id (manifest)")
    s.add_argument("--ra", type=float)
    s.add_argument("--dec", type=float)
    s.add_argument("--radius", type=float, default=0.05)
    s.add_argument("--detectors")
    s.add_argument("--cutout-size", type=float)
    s.add_argument("--max-observations", type=int, default=16)
    s.add_argument("--max-seconds", type=float, default=300.0, help="work: wall-time budget for this worker run")
    s.add_argument("--max-idle", type=float, default=30.0, help="worker: longest sleep between scheduling checks (seconds)")
    s.add_argument("--wait", type=float, default=0.0, metavar="SECONDS", help="prefetch/index: run the worker inline for at most this long")
    s.add_argument("--state", help="jobs: filter by state")
    s.add_argument("--max-age-s", type=float, help="sync: refresh regions older than this")
    s.add_argument("--target-bytes", type=int, help="evict: reduce unpinned storage to this size (default: the configured quota)")
    s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("runs", help="list stored investigations")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("inspect", help="inspect a stored investigation")
    s.add_argument("run", help="run id or run directory")
    s.add_argument("--section", default="summary")
    s.add_argument("--id", dest="item_id")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("validate", help="re-run an investigation with validation, or run the independent self-checks")
    s.add_argument("run", nargs="?")
    s.add_argument("--self-check", action="store_true")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("reinvestigate", help="rebuild the scientific investigations of a saved run offline (no detection, no download)")
    s.add_argument("run")
    s.add_argument("--out", type=Path, help="write the rebuilt investigations to this JSON file")
    s = sub.add_parser("reproduce", help="re-run a stored investigation and compare results hashes")
    s.add_argument("run")
    s = sub.add_parser("skymap", help="sky-map layers of a stored investigation")
    s.add_argument("run")
    s.add_argument("--svg", type=Path, help="also write an SVG rendering")
    s.add_argument("--viewport", nargs=3, type=float, metavar=("RA", "DEC", "FOV"))
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("playback", help="time-ordered observation frames of a stored investigation")
    s.add_argument("run")
    s.add_argument("--mjd-start", type=float)
    s.add_argument("--mjd-end", type=float)
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("export", help="export results as JSON or CSV")
    s.add_argument("run")
    s.add_argument("--what", required=True, choices=["candidates", "detections", "sources", "observations", "validation"])
    s.add_argument("--format", default="json", choices=["json", "csv"])
    s.add_argument("--out", type=Path)
    s = sub.add_parser("config", help="show, hash or template the engine configuration")
    s.add_argument("action", choices=["show", "hash", "template"])
    s.add_argument("--config", type=Path)
    s = sub.add_parser("cache", help="list or clear cached products")
    s.add_argument("action", choices=["list", "clear"])
    s.add_argument("--older-than-days", type=float)
    s = sub.add_parser("context-status", help="probe Gaia (IRSA, ESA), CatWISE and JPL reachability once each; classify failures from evidence")
    s.add_argument("--timeout", type=float, default=20.0)
    s = sub.add_parser("inventory", help="partitioned all-sky metadata inventory: plan, step (queue INDEX jobs), status/accounting")
    s.add_argument("action", choices=["plan", "step", "status", "mirror-plan"])
    s.add_argument("--tile-deg", type=float, default=2.0)
    s.add_argument("--max-bytes", type=int, default=10 * 1024 ** 3, help="mirror-plan: byte budget")
    s.add_argument("--domain", default="0,360,-90,90", help="ra_min,ra_max,dec_min,dec_max (default: whole sky)")
    s.add_argument("--max-new", type=int, default=4)
    s.add_argument("--max-in-flight", type=int, default=8)
    s.add_argument("--refresh-age-s", type=float)
    s.add_argument("--limit", type=int, default=50)
    s = sub.add_parser("scientist", help="capabilities, compare two versions/runs, reverse evidence lookup")
    s.add_argument("action", choices=["capabilities", "compare", "lookup"])
    s.add_argument("a", nargs="?", help="compare: first tinv:/run id; lookup: obs_id or sha256")
    s.add_argument("b", nargs="?", help="compare: second tinv:/run id")
    s = sub.add_parser("evidence", help="portable evidence bundles: export saved results with their exact inputs, verify, import into fresh state")
    s.add_argument("action", choices=["export", "verify", "import"])
    s.add_argument("path", type=Path, help="bundle directory (export/import/verify) or .tar.gz (verify/import)")
    s.add_argument("--run", action="append", default=[], help="export: also include this blind-pipeline run id (repeatable)")
    s.add_argument("--archive", action="store_true", help="export: also write <path>.tar.gz")
    s.add_argument("--force", action="store_true", help="import: merge into a non-empty state directory")
    s = sub.add_parser("serve", help="HTTP JSON API for the frontend (readiness-first; see docs/FRONTEND_HANDOFF.md)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--with-worker", action="store_true", help="also run the data worker in this process (otherwise run `data worker` separately)")
    s = sub.add_parser("demo", help="SYNTHETIC end-to-end demonstration through the real pipeline")
    s.add_argument("--out", type=Path)
    s.add_argument("--no-validate", action="store_true")
    s.add_argument("--small", action="store_true", help="smaller, faster scene")
    return p


def _run(args) -> int:
    from .api import Engine
    from .engine.config import EngineConfig
    from .pipeline.runner import InvestigationRequest

    cfg_path = getattr(args, "config", None)
    if args.command == "context-status":
        from .context.health import context_health
        h = context_health(args.timeout)
        print(f"proxy configured: {h['proxy_configured'] or 'none'}")
        for r in h["services"]:
            print(f"{r['checked_at']} {r['service']:18s} {r['classification']:22s} http={r.get('http_status') or '-'} {r['elapsed_s']:6.2f}s {r.get('detail') or r.get('note') or ''}")
        return 0 if any(r["classification"] == "OK" for r in h["services"]) else 1
    if args.command == "inventory":
        from .dataservice.allsky import AllSkyIndex
        eng = Engine.from_environment(_env_file())
        idx = AllSkyIndex(eng.data_service())
        domain = tuple(float(x) for x in args.domain.split(","))
        dom = idx.domain_id(args.tile_deg, domain)
        if args.action == "plan":
            _print(idx.plan(args.tile_deg, domain), True)
        elif args.action == "step":
            _print(idx.step(dom, max_new=args.max_new, max_in_flight=args.max_in_flight, refresh_age_s=args.refresh_age_s), True)
        elif args.action == "mirror-plan":
            from .dataservice.allsky import mirror_plan
            _print(mirror_plan(eng.data_service(), dom, max_bytes=args.max_bytes), True)
        else:
            _print(idx.accounting(dom, limit=args.limit), True)
        return 0
    if args.command == "scientist":
        from .scientist import capabilities, compare, lookup
        eng = Engine.from_environment(_env_file())
        if args.action == "capabilities":
            _print(capabilities(eng), True)
        elif args.action == "compare":
            _print(compare(eng, args.a, args.b), True)
        else:
            key = args.a or ""
            _print(lookup(eng, sha256=key) if len(key) == 64 and all(c in "0123456789abcdef" for c in key) else lookup(eng, obs_id=key), True)
        return 0
    if args.command == "evidence":
        from .config import load_settings
        from .evidence import export_bundle, import_bundle, verify_bundle
        st = load_settings(env_file=_env_file())
        if args.action == "export":
            _print(export_bundle(Path(st.state_dir), Path(st.cache_dir), args.path, runs=args.run, archive=args.archive), True)
        elif args.action == "verify":
            rep = verify_bundle(args.path)
            _print(rep, True)
            return 0 if rep["ok"] else 1
        else:
            _print(import_bundle(args.path, Path(st.state_dir), Path(st.cache_dir), force=args.force), True)
        return 0
    if args.command == "serve":
        from .server import serve
        serve(lambda: Engine.from_environment(_env_file()), host=args.host, port=args.port, with_worker=args.with_worker)
        return 0
    engine = Engine.from_environment(_env_file(), cfg_path if args.command == "investigate" else None)
    if args.command == "sources":
        _print(engine.sources(), args.json)
    elif args.command == "irsa-status":
        health = engine.source("irsa-spherex").route_health()
        if args.json:
            _print(health, True)
        else:
            for h in health:
                print(f"{h['checked_at']} {h['route']:12s} {h['status']:6s} http={h['http_status'] or '-':4s} {h['elapsed_s']:6.2f}s {h['detail']}")
        return 0 if any(h["status"] == "OK" for h in health[:2]) else 1  # healthy when either discovery route works
    elif args.command == "access":
        _print(engine.check_access(args.source, args.local_root), False)
    elif args.command == "coverage":
        cov = engine.coverage(args.source, args.ra, args.dec, args.radius, detectors=_ints(args.detectors),
                              collections=_strs(args.collections), mjd_start=args.mjd_start, mjd_end=args.mjd_end,
                              local_root=args.local_root, live=args.live)
        if args.json:
            _print(cov, True)
        elif cov["outcome"] not in ("OK", "NO_OBSERVATIONS"):
            job = (cov.get("data_status") or {}).get("job") or {}
            print(f"{cov['outcome']}: {cov['note']} (job={job.get('job_id')}; do not resubmit)")
        else:
            print(f"{cov['outcome']}: {cov['n_observations']} observation(s); per detector {cov['per_detector']}; "
                  f"{len(cov['epochs'])} epoch(s)")
            for e in cov["epochs"]:
                print(f"  epoch {e['index']}: MJD {e['mjd_start']:.3f}-{e['mjd_end']:.3f}, {e['n_observations']} observation(s)")
            if cov["note"]:
                print(f"  {cov['note']}")
    elif args.command == "investigate":
        req = InvestigationRequest(ra_deg=args.ra, dec_deg=args.dec, radius_deg=args.radius, source_id=args.source,
                                   detectors=_ints(args.detectors), collections=_strs(args.collections),
                                   mjd_start=args.mjd_start, mjd_end=args.mjd_end, max_observations=args.max_observations,
                                   cutout_size_deg=args.cutout_size, run_validation=args.validate,
                                   calibration="none" if args.no_calibration else "auto", label=args.label,
                                   local_root=args.local_root, context="none" if args.no_context else "auto",
                                   calibration_products=args.calibration_products)
        progress = None if args.quiet else (lambda stage, msg: print(f"[{stage}] {msg}", file=sys.stderr))
        result, run_dir = engine.investigate(req, progress=progress, svg=not args.no_svg, out_dir=args.out, live=args.live)
        if run_dir is None and "run_id" not in result:  # accepted: data are being prepared by a durable job
            job = result.get("job") or {}
            print(f"{result['readiness']}: {result['message']} (job={job.get('job_id')}; do not resubmit)")
            return 0
        from .api import summarize
        _print_summary(summarize(result))
        print(f"  saved to {run_dir}")
        return 0 if result["status"]["execution"] != "FAILED" else 1
    elif args.command == "data":
        return _data(engine, args)
    elif args.command == "target":
        return _target(engine, args)
    elif args.command == "runs":
        _print(engine.runs(), args.json)
    elif args.command == "inspect":
        data = engine.inspect(args.run, args.section, args.item_id)
        if args.section == "summary" and not args.json:
            _print_summary(data)
        else:
            _print(data, True)
    elif args.command == "validate":
        if args.self_check:
            checks = engine.self_check()
            for c in checks:
                print(f"{c['state']:7s} {c['check']}: {c['detail']}")
            return 0 if all(c["state"] == "PASSED" for c in checks) else 1
        if not args.run:
            print("validate needs a run id/directory or --self-check", file=sys.stderr)
            return 2
        out = engine.validate(args.run)
        if args.json:
            _print(out, True)
        else:
            _print_summary(out["summary"])
            print(f"  saved to {out['run_dir']}")
    elif args.command == "reinvestigate":
        inv = engine.reinvestigate(args.run, args.out)
        print(f"{inv['n_investigations']} investigations rebuilt offline from run {inv['source_run']['run_id']}")
        print("interpretation:", inv["interpretation_counts"])
        if args.out:
            print(f"written to {args.out}")
    elif args.command == "reproduce":
        out = engine.reproduce(args.run)
        _print(out, False)
        return 0 if out["identical"] else 1
    elif args.command == "skymap":
        viewport = tuple(args.viewport) if args.viewport else None
        sm = engine.skymap(args.run, viewport)
        if args.svg:
            from .skymap.svg import render_svg
            args.svg.write_text(render_svg(sm), encoding="utf-8")
            print(f"SVG written to {args.svg}")
        if args.json or not args.svg:
            _print(sm, True)
    elif args.command == "playback":
        _print(engine.playback(args.run, args.mjd_start, args.mjd_end), True)
    elif args.command == "export":
        text = engine.export(args.run, args.what, args.format)
        if args.out:
            args.out.write_text(text, encoding="utf-8")
            print(f"written to {args.out}")
        else:
            print(text)
    elif args.command == "config":
        cfg = EngineConfig.load(args.config) if args.config else EngineConfig()
        if args.action == "hash":
            print(cfg.hash)
        else:
            print(json.dumps(cfg.to_dict(), indent=1, sort_keys=True))
    elif args.command == "cache":
        from .data_access.cache import ProductCache
        cache = ProductCache(Path(engine.settings.cache_dir) / "irsa-spherex")
        if args.action == "list":
            _print(cache.entries(), False)
        else:
            print(f"removed {cache.invalidate(older_than_days=args.older_than_days)} cached product(s)")
    elif args.command == "demo":
        print("Generating a SYNTHETIC scene (not astronomical data) and running the full pipeline ...", file=sys.stderr)
        result, run_dir = engine.demo(args.out, validate=not args.no_validate, small=args.small)
        from .api import summarize
        _print_summary(summarize(result))
        print(f"  saved to {run_dir} (skymap.svg, skymap.json, investigation.json, events.jsonl)")
    return 0


def _data(engine, args) -> int:
    from .dataservice.schemas import JobState, RegionSpec
    svc = engine.data_service()
    if args.action == "status":
        _print({"metrics": svc.metrics(), "freshness": svc.freshness()}, True)
    elif args.action == "freshness":
        reg = RegionSpec(args.ra % 360.0, args.dec, args.radius, tuple(_ints(args.detectors)) if args.detectors else None) if args.ra is not None else None
        _print(svc.freshness(reg), True)
    elif args.action == "jobs":
        want = {JobState(args.state)} if args.state else None
        _print([svc.describe_job(j) for j in svc.jobs.list(want)], True)
    elif args.action == "job":
        _print(svc.job_status(args.ref), True)
    elif args.action == "cancel":
        _print(svc.describe_job(svc.jobs.cancel(args.ref)), True)
    elif args.action == "resume":
        _print({"resumed": svc.resume()}, True)
    elif args.action == "work":
        _print(svc.work(args.max_seconds), True)
    elif args.action == "worker":  # long-running deployment mode; SIGTERM/SIGINT stop it gracefully
        import signal
        import threading
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        print(f"[worker] started; heartbeat at {svc.heartbeat_path}", file=sys.stderr)
        _print(svc.serve(stop, max_idle_s=args.max_idle), True)
    elif args.action == "sync":
        _print({"queued_refresh_jobs": [j["job_id"] for j in svc.sync(max_age_s=args.max_age_s)]}, True)
    elif args.action == "evict":
        _print(svc.store.evict(target_bytes=args.target_bytes, dry_run=args.dry_run), True)
    elif args.action == "manifest":
        _print(svc.manifest(args.ref), True)
    else:  # index | prefetch
        if args.ra is None or args.dec is None:
            print("error: --ra and --dec are required", file=sys.stderr)
            return 2
        region = RegionSpec(args.ra % 360.0, args.dec, args.radius, tuple(_ints(args.detectors)) if args.detectors else None)
        _print(svc.ensure_index(region, wait_s=args.wait) if args.action == "index" else
               svc.prefetch(region, cutout_size_deg=args.cutout_size, max_observations=args.max_observations, wait_s=args.wait), True)
    return 0


def _target(engine, args) -> int:
    if args.action == "list":
        for t in engine.targets():
            saved = t["saved"]
            print(f"{t['target_id']}  {t['name']:45s} {t['category']:14s} " + (f"saved: {saved['status']['execution']}/{saved['status']['test_status']}" if saved else "not yet run"))
        return 0
    if args.action == "matrix":
        from .targets.matrix import build_matrix
        text = build_matrix(engine)
        if args.write:
            args.write.write_text(text, encoding="utf-8")
            print(f"written to {args.write}")
        else:
            print(text)
        return 0
    if args.action == "show":
        _print(engine.target_result(args.target), True)
        return 0
    if args.action == "followup":  # discriminating follow-up plan (persisted; actions never repeated)
        from .followup import plan
        _print(plan(engine, args.target), True)
        return 0
    if args.action == "followup-run":  # args.target carries the action id
        from .followup import run
        _print(run(engine, args.target), True)
        return 0
    if args.action == "claims":  # auditable claims with the defensibility gate, derived from the saved result
        from .targets.claims import build_claims
        _print(build_claims(engine.target_result(args.target)), True)
        return 0
    if args.action == "reevaluate":  # decision-rule correction on saved measurements; creates a linked new version, never edits the old one
        from .targets.reevaluate import reevaluate_variability
        if not args.target or not args.reason:
            print("error: reevaluate needs a target id and --reason", file=sys.stderr)
            return 2
        _print(reevaluate_variability(Path(engine.settings.state_dir), engine.config, args.target, reason=args.reason, dry_run=args.dry_run), True)
        return 0
    if args.action == "resolve":
        _print(engine.resolve_target(args.target, ra=args.ra, dec=args.dec, category=args.category), True)
        return 0
    progress = None if args.quiet else (lambda msg: print(f"[target] {msg}", file=sys.stderr))
    res = engine.investigate_target(args.target, ra=args.ra, dec=args.dec, category=args.category, source_id=args.source, local_root=args.local_root,
                                    progress=progress, max_observations=args.max_observations, detectors=tuple(_ints(args.detectors)) if args.detectors else None,
                                    mjd_start=args.mjd_start, mjd_end=args.mjd_end, cutout_size_deg=args.cutout_size, max_seconds=args.max_seconds,
                                    blind_pipeline=not args.no_blind, context=not args.no_context, wait_s=args.wait)
    if "investigation_id" not in res:  # accepted, not finished: a durable job owns acquisition and retries
        if args.json:
            _print(res, True)
        else:
            j = res.get("job") or {}
            print(f"{res['readiness']}  job={j.get('job_id')}  state={j.get('state')}  attempts={j.get('attempts')}  next_attempt_in_s={j.get('next_attempt_in_s')}")
            print(f"  {res['message']}")
            print(f"  progress: python3 -m heimdall data job {j.get('job_id')}    worker: python3 -m heimdall data work")
        return 0 if res["readiness"] in ("PREPARING", "NO_COVERAGE", "READY") else 1
    if args.json:
        _print(res, True)
    else:
        c = res.get("coverage", {})
        print(f"{res['investigation_id']}  {res['target']['name']}  execution={res['status']['execution']}  test={res['status']['test_status']}")
        print(f"  coverage: {c.get('n_discovered')} observations found, {c.get('n_selected')} selected, {c.get('n_measured')} measured; releases {c.get('per_release')}")
        print(f"  {res['conclusion']['quick_answer']}")
        for h in res.get("hypotheses", []):
            print(f"  [{h['status']:15s}] {h['title']}")
        print(f"  saved to {res.get('saved_to')}")
    return 0 if res["status"]["execution"] != "BLOCKED" else 1


def main(argv: list[str] | None = None) -> int:
    args_list = sys.argv[1:] if argv is None else argv
    parser = _parser()
    try:
        args = parser.parse_args(args_list)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2
    if args.command == "version":
        print(f"heimdall {__version__} (schema {SCHEMA_VERSION})")
        return 0
    if args.command == "check":
        return _check()
    try:
        return _run(args)
    except HeimdallError as exc:
        print(f"error [{exc.code}]: {exc.info.message}", file=sys.stderr)
        for k, v in exc.info.detail.items():
            print(f"  {k}: {v}", file=sys.stderr)
        return 2 if exc.code in (ErrorCode.INPUT_INVALID, ErrorCode.CONFIG_INVALID, ErrorCode.SOURCE_NOT_CONFIGURED) else 1


if __name__ == "__main__":
    raise SystemExit(main())
