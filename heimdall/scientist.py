"""Scientist-facing contracts: capabilities, version/alternate comparison and reverse evidence lookup.

* ``capabilities()`` (``heimdall.capabilities/1.0``): which analyses exist, their entry points, prerequisites, limits and validation state.
  It is generated from the code's own constants so it cannot drift from what the backend enforces.
* ``compare(a, b)`` (``heimdall.comparison/1.0``): two saved target-investigation versions or two blind-pipeline runs. It reports explicit
  parameter, input and method differences, the changes in conclusions, and a comparability verdict (``COMPARABLE`` /
  ``COMPARABLE_WITH_LIMITATIONS`` / ``NOT_COMPARABLE``) with reasons. A ``NOT_COMPARABLE`` pair yields no change statement.
* ``lookup(obs_id=..., sha256=...)`` (``heimdall.evidence_lookup/1.0``): the reverse path from an observation or a product checksum to
  every saved investigation version, claim, manifest and run that depends on it.
"""

from __future__ import annotations

import json
from pathlib import Path

CAPABILITIES_SCHEMA = "heimdall.capabilities/1.0"
COMPARISON_SCHEMA = "heimdall.comparison/1.0"
LOOKUP_SCHEMA = "heimdall.evidence_lookup/1.0"


def capabilities(engine) -> dict:
    from . import __version__
    from .dataservice.schemas import CURRENT_COLLECTIONS, ServiceConfig
    from .server import COVERAGE_GRID_DEG, MAX_BODY
    from .targets.claims import CLAIMS_METHOD_VERSION, VALIDATED_METHODS
    from .targets.variability import METHOD_VERSION as VAR_VERSION
    cfg = ServiceConfig.from_env()
    unval = "UNVALIDATED_ON_REAL_DATA (software-verified on synthetic controls; see docs/VALIDATION.md)"
    return {
        "schema": CAPABILITIES_SCHEMA, "software_version": __version__, "offline": bool(engine.settings.offline),
        "data": {"collections": list(CURRENT_COLLECTIONS), "never_mixed": "QR2 (downloadable, not searchable) is never mixed into current results",
                 "detectors": [1, 2, 3, 4, 5, 6], "per_detector_validation": "NOT_VALIDATED for any detector (docs/SPHEREX_FIDELITY.md)",
                 "all_sky_index": "partitioned inventory available (GET /inventory/partitions); completeness is reported per partition, never assumed"},
        "analyses": [
            {"id": "coverage", "entry": "GET /api/v1/coverage", "prerequisites": ["indexed region (else a durable INDEX job)"],
             "limits": {"radius_deg_max": 1.0, "page_size_max": 2000, "grid_deg": COVERAGE_GRID_DEG}, "validation": "NOT_APPLICABLE (inventory)",
             "side_effects": "may queue one INDEX job per 0.01-degree cell; never downloads images or runs analysis"},
            {"id": "target_investigation", "entry": "POST /api/v1/investigations", "inputs": ["target_id (NASA-01..05)", "or ra/dec + category"],
             "prerequisites": ["reference astrometry (Gaia/CatWISE; cached or fetched by a worker)", "indexed region", "verified products + ePSF"],
             "limits": {"max_observations": 64, "cutout_size_deg": [0.01, 0.5]},
             "methods": {"motion": f"target_motion 1.0.0-provisional: {unval}", "variability": f"target_variability {VAR_VERSION}: {unval}",
                         "forced_photometry": unval}},
            {"id": "field_run", "entry": "POST /api/v1/runs", "description": "blind detection, association, linking, interpretation on a field",
             "prerequisites": ["prefetched region (else a durable FIELD_INVESTIGATION job)"],
             "limits": {"radius_deg_max": 0.5, "max_observations": 32, "cutout_size_deg": [0.01, 0.5]},
             "validation": "blind pipeline: synthetic injection/recovery only; moving-object completeness not measured (Planet X region status NOT_ASSESSABLE)"},
            {"id": "claims", "entry": "GET /api/v1/targets/{id}/claims, /investigations/{id}/claims", "method_version": CLAIMS_METHOD_VERSION,
             "validated_methods": sorted(VALIDATED_METHODS), "claim_types": ["MEASUREMENT", "DETECTION", "CANDIDATE", "NON_DETECTION", "UPPER_LIMIT",
                                                                              "CONSISTENT_WITH", "INCONCLUSIVE", "NOT_ASSESSABLE", "LIMITED_BY_DATA", "BLOCKED"]},
            {"id": "comparison", "entry": "GET /api/v1/compare?a=&b=", "inputs": ["two tinv: versions or two run ids"],
             "output": "parameter/input/method differences, conclusion changes, comparability verdict"},
            {"id": "evidence_lookup", "entry": "GET /api/v1/evidence/lookup?obs_id=|sha256=", "output": "every dependent version, claim, manifest, run"},
            {"id": "versions", "entry": "GET /api/v1/targets/{id}/versions", "output": "immutable versions with supersession links"},
            {"id": "patterns", "entry": "GET /api/v1/targets/{id}/patterns", "output": "motion/temporal/spectral/spatial records, links, missing dimensions"},
            {"id": "followup", "entry": "GET /api/v1/targets/{id}/followup; POST /api/v1/followup/{action_id}/run",
             "output": "discriminating actions with hypotheses distinguished, cost, availability, budget and stopping reason; never repeated"},
            {"id": "mirror_plan", "entry": "CLI inventory mirror-plan", "output": "full-frame mirror estimate from measured sizes; refuses without a measured sample"},
            {"id": "inventory_partitions", "entry": "GET /api/v1/inventory/partitions", "output": "per-partition search state and accounting"},
        ],
        "not_available": [
            {"id": "period_search", "reason": "sparse SPHEREx cadence; no alias/false-alarm-calibrated period search implemented", "state": "NOT_ASSESSABLE"},
            {"id": "thermal_or_physical_parameters", "reason": "needs model grids, band integration and absolute calibration (docs/THERMAL_REFERENCE_FEASIBILITY.md)",
             "state": "NOT_ASSESSABLE"},
            {"id": "detector_combination_stacking", "reason": "cross-detector calibration and covariance not demonstrated", "state": "NOT_AVAILABLE"},
            {"id": "planet_x_discovery", "reason": "angular motion alone cannot establish distance/orbit; completeness not measured", "state": "BOUNDED_SCENARIO_ONLY"},
        ],
        "jobs": {"deadline_s": cfg.deadline_s, "recovery_window_s": cfg.recovery_window_s, "recovery_interval_s": cfg.recovery_interval_s,
                 "states": ["QUEUED", "DISCOVERING", "DOWNLOADING", "VALIDATING", "PREPARING_DATA", "COMPLETE", "NO_COVERAGE",
                            "UNAVAILABLE_AFTER_DEADLINE", "INVALID_SOURCE_DATA", "CANCELLED", "FAILED"],
                 "worker_model": "one worker process per state directory (SQLite); polling, no push channel"},
        "transport": {"request_body_max_bytes": MAX_BODY, "auth": "optional bearer token (HEIMDALL_API_TOKEN)", "errors": "heimdall.http/1.0 envelope"},
    }


# ----------------------------------------------------------------------------------------------- comparison

def _diff(a: dict, b: dict, keys) -> dict:
    return {k: {"a": a.get(k), "b": b.get(k)} for k in keys if a.get(k) != b.get(k)}


def _verdicts(doc: dict) -> dict:
    an = doc.get("analyses") or {}
    return {k: (v or {}).get("verdict") for k, v in an.items()}


def compare_target_versions(a: dict, b: dict) -> dict:
    from .targets.claims import build_claims
    reasons_nc, limits = [], []
    ta, tb = (a.get("target") or {}).get("target_id"), (b.get("target") or {}).get("target_id")
    if ta != tb:
        reasons_nc.append(f"different targets ({ta} vs {tb}): a change statement between them is meaningless")
    if a.get("data_origin") != b.get("data_origin"):
        reasons_nc.append(f"different data origin ({a.get('data_origin')} vs {b.get('data_origin')})")
    pa = {p.get("sha256") for p in a.get("products") or []}
    pb = {p.get("sha256") for p in b.get("products") or []}
    rel_a = sorted({p.get("release") for p in a.get("products") or []})
    rel_b = sorted({p.get("release") for p in b.get("products") or []})
    if a.get("config_hash") != b.get("config_hash"):
        limits.append("engine configuration differs (results may differ for configuration reasons alone)")
    if pa != pb:
        limits.append(f"inputs differ: {len(pa & pb)} shared products, {len(pa - pb)} only in a, {len(pb - pa)} only in b")
    if rel_a != rel_b:
        limits.append(f"release mix differs ({rel_a} vs {rel_b})")
    mv = {k: ((a.get("analyses") or {}).get(k, {}) or {}).get("method_version") for k in (a.get("analyses") or {})}
    mvb = {k: ((b.get("analyses") or {}).get(k, {}) or {}).get("method_version") for k in (b.get("analyses") or {})}
    if mv != mvb:
        limits.append(f"method versions differ ({mv} vs {mvb})")
    verdict = "NOT_COMPARABLE" if reasons_nc else ("COMPARABLE_WITH_LIMITATIONS" if limits else "COMPARABLE")
    out = {"schema": COMPARISON_SCHEMA, "kind": "target_investigation_versions", "a": a.get("investigation_id"), "b": b.get("investigation_id"),
           "comparability": {"status": verdict, "reasons": reasons_nc or limits},
           "parameters": _diff(a.get("options") or {}, b.get("options") or {}, sorted(set(a.get("options") or {}) | set(b.get("options") or {}))),
           "inputs": {"shared_products": len(pa & pb), "only_a": sorted(pa - pb), "only_b": sorted(pb - pa),
                      "manifest_a": (a.get("input_manifest") or {}).get("manifest_id"), "manifest_b": (b.get("input_manifest") or {}).get("manifest_id")},
           "methods": {"a": mv, "b": mvb, "config_hash_a": a.get("config_hash"), "config_hash_b": b.get("config_hash")},
           "claims_note": "claims of both versions are evaluated with the current claims gate, so a rule fix shows in both",
           "supersession": {"b_supersedes_a": (b.get("supersedes") or {}).get("investigation_id") == a.get("investigation_id"),
                            "reason": (b.get("supersedes") or {}).get("reason")}}
    if verdict == "NOT_COMPARABLE":
        out["changes"] = None
        out["note"] = "not comparable: no change in conclusions is reported"
        return out
    ca = {c["analysis"]: c for c in build_claims(a)["claims"]}
    cb = {c["analysis"]: c for c in build_claims(b)["claims"]}
    out["changes"] = {
        "verdicts": {k: {"a": v, "b": _verdicts(b).get(k)} for k, v in _verdicts(a).items() if _verdicts(b).get(k) != v},
        "claims": {k: {"a": (ca.get(k) or {}).get("claim_type"), "b": (cb.get(k) or {}).get("claim_type")} for k in sorted(set(ca) | set(cb))
                   if (ca.get(k) or {}).get("claim_type") != (cb.get(k) or {}).get("claim_type")},
        "hypotheses": {h["slug"]: {"a": h.get("status"), "b": next((x.get("status") for x in b.get("hypotheses") or [] if x.get("slug") == h["slug"]), None)}
                       for h in a.get("hypotheses") or [] if next((x.get("status") for x in b.get("hypotheses") or [] if x.get("slug") == h["slug"]), None) != h.get("status")},
        "test_status": {"a": (a.get("status") or {}).get("test_status"), "b": (b.get("status") or {}).get("test_status")}}
    return out


def compare_runs(a: dict, b: dict) -> dict:
    ra, rb = a.get("request") or {}, b.get("request") or {}
    reasons_nc, limits = [], []
    from .engine.skygeom import angular_separation_deg
    sep = float(angular_separation_deg(ra.get("ra_deg", 0), ra.get("dec_deg", 0), rb.get("ra_deg", 0), rb.get("dec_deg", 0)))
    if sep > max(ra.get("radius_deg") or 0, rb.get("radius_deg") or 0):
        reasons_nc.append(f"fields do not overlap (centres {sep:.3f} deg apart)")
    if a.get("data_origin") != b.get("data_origin"):
        reasons_nc.append(f"different data origin ({a.get('data_origin')} vs {b.get('data_origin')})")
    oa = {o.get("obs_id") for o in a.get("observations") or [] if o.get("included")}
    ob = {o.get("obs_id") for o in b.get("observations") or [] if o.get("included")}
    if oa != ob:
        limits.append(f"observations differ: {len(oa & ob)} shared, {len(oa - ob)} only in a, {len(ob - oa)} only in b")
    params = _diff(ra, rb, sorted((set(ra) | set(rb)) - {"label", "obs_ids"}))
    if params:
        limits.append(f"parameters differ: {sorted(params)}")
    if (a.get("config") or {}) != (b.get("config") or {}):
        limits.append("engine configuration differs")
    verdict = "NOT_COMPARABLE" if reasons_nc else ("COMPARABLE_WITH_LIMITATIONS" if limits else "COMPARABLE")
    out = {"schema": COMPARISON_SCHEMA, "kind": "blind_runs", "a": a.get("run_id"), "b": b.get("run_id"),
           "comparability": {"status": verdict, "reasons": reasons_nc or limits}, "parameters": params,
           "inputs": {"shared_observations": len(oa & ob), "only_a": sorted(oa - ob), "only_b": sorted(ob - oa),
                      "manifest_a": (a.get("input_manifest") or {}).get("manifest_id"), "manifest_b": (b.get("input_manifest") or {}).get("manifest_id")},
           "results_hash": {"a": a.get("results_hash"), "b": b.get("results_hash"), "identical": a.get("results_hash") == b.get("results_hash")}}
    if verdict == "NOT_COMPARABLE":
        out["changes"] = None
        out["note"] = "not comparable: no change in results is reported"
        return out

    def counts(r):
        cands = r.get("candidates") or []
        att: dict[str, int] = {}
        for c in cands:
            att[str(c.get("attention_level"))] = att.get(str(c.get("attention_level")), 0) + 1
        return {"detections": len(r.get("detections") or []), "sources": len(r.get("sources") or []), "candidates": len(cands),
                "tracks": len(r.get("tracks") or []), "attention": att}
    ca, cb = counts(a), counts(b)
    out["changes"] = {k: {"a": ca[k], "b": cb[k]} for k in ca if ca[k] != cb[k]}
    return out


def compare(engine, a: str, b: str) -> dict:
    from .contracts import ErrorCode, HeimdallError
    if a.startswith("tinv:") and b.startswith("tinv:"):
        return compare_target_versions(_target_version(engine, a), _target_version(engine, b))
    if a.startswith("R") and b.startswith("R"):
        return compare_runs(engine.load(a), engine.load(b))
    raise HeimdallError.of(ErrorCode.INPUT_INVALID, "compare two target versions (tinv:...) or two runs (R...); mixing kinds is not comparable")


def _target_version(engine, inv_id: str) -> dict:
    from .contracts import ErrorCode, HeimdallError
    try:
        _, tid, h = inv_id.split(":")
    except ValueError:
        raise HeimdallError.of(ErrorCode.INPUT_INVALID, f"bad investigation id {inv_id!r}") from None
    p = Path(engine.settings.state_dir) / "targets" / tid / f"{h}.json"
    if not p.is_file():
        raise HeimdallError.of(ErrorCode.INPUT_INVALID, f"no saved investigation {inv_id}")
    doc = json.loads(p.read_text(encoding="utf-8"))
    if doc.get("investigation_id") != inv_id:
        raise HeimdallError.of(ErrorCode.INPUT_INVALID, f"record {p.name} does not hold {inv_id}")
    return doc


# ----------------------------------------------------------------------------------------------- reverse lookup

def lookup(engine, *, obs_id: str | None = None, sha256: str | None = None, max_items: int = 500) -> dict:
    """Every saved conclusion that depends on an observation or a product checksum (bounded scan of saved versions and runs)."""
    from .targets.claims import build_claims
    if not obs_id and not sha256:
        from .contracts import ErrorCode, HeimdallError
        raise HeimdallError.of(ErrorCode.INPUT_INVALID, "give obs_id or sha256")
    state = Path(engine.settings.state_dir)
    versions, claims_out, products = [], [], []
    for p in sorted((state / "targets").glob("*/*.json"))[:max_items]:
        if p.name in ("latest.json", "latest_attempt.json"):
            continue
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        prods = [x for x in doc.get("products") or [] if (obs_id and x.get("obs_id") == obs_id) or (sha256 and x.get("sha256") == sha256)]
        if not prods:
            continue
        ids = {x.get("obs_id") for x in prods}
        products += [{"investigation_id": doc.get("investigation_id"), **{k: x.get(k) for k in ("obs_id", "sha256", "release", "retrieved_at", "access_reference")}}
                     for x in prods]
        meas = [m.get("obs_id") for m in doc.get("measurements") or [] if m.get("obs_id") in ids]
        versions.append({"investigation_id": doc.get("investigation_id"), "created_at": doc.get("created_at"), "target": (doc.get("target") or {}).get("target_id"),
                         "measurement_refs": [f"measurements[obs_id={o}]" for o in meas], "superseded_by": None})
        for c in build_claims(doc)["claims"]:
            if ids & set(c.get("observations_used") or []):
                claims_out.append({"claim_id": c["claim_id"], "investigation_id": doc.get("investigation_id"), "analysis": c["analysis"],
                                   "claim_type": c["claim_type"], "strength": c.get("strength")})
    sup = {}
    for p in (state / "targets").glob("*/supersessions.jsonl"):
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                sup[r["old"]] = r["new"]
            except (ValueError, KeyError):
                continue
    for v in versions:
        v["superseded_by"] = sup.get(v["investigation_id"])
    svc = engine.data_service()
    manifests, runs = [], []
    for r in svc.db.query("SELECT manifest_id, body FROM manifests"):
        body = json.loads(r["body"])
        if any((obs_id and x.get("obs_id") == obs_id) or (sha256 and x.get("sha256") == sha256) for x in body.get("products", []) + body.get("calibrations", [])):
            manifests.append(r["manifest_id"])
    for r in svc.db.query("SELECT investigation_id, manifest_id FROM investigation_results WHERE key LIKE 'frun:%'"):
        if r["manifest_id"] in manifests:
            runs.append(r["investigation_id"])
    return {"schema": LOOKUP_SCHEMA, "query": {"obs_id": obs_id, "sha256": sha256}, "products": products, "versions": versions, "claims": claims_out,
            "manifests": manifests, "runs": runs,
            "note": "reverse path: product -> measurements -> claims -> versions; superseded versions are listed with their successor"}
