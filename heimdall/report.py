"""Portable investigation report (``heimdall.report/1.0``): machine-readable JSON plus a readable Markdown rendering.

Built only from the saved investigation and the pure functions that restate it (claims, patterns), so the report adds no measurement.
It identifies its data (products with SHA-256, release, route, retrieval time, manifest), methods and versions, validation state,
claims with their gate, patterns, hypotheses, limitations, version history and the exact reproduction route.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from . import __version__
from .patterns import build_patterns
from .targets.claims import build_claims

REPORT_SCHEMA = "heimdall.report/1.0"


def build_report(result: dict, versions: list[dict] | None = None) -> dict:
    claims = build_claims(result)
    patterns = build_patterns(result)
    man = result.get("input_manifest") or {}
    prods = [{k: p.get(k) for k in ("obs_id", "release", "sha256", "retrieved_at", "access_reference", "origin")} for p in result.get("products") or []]
    an = result.get("analyses") or {}
    return {
        "schema": REPORT_SCHEMA, "generated_at": datetime.now(UTC).isoformat(timespec="seconds"), "software_version": __version__,
        "investigation_id": result.get("investigation_id"), "target": result.get("target"), "data_origin": result.get("data_origin"),
        "status": result.get("status"), "quick_answer": claims["quick_answer"],
        "data": {"manifest_id": man.get("manifest_id"), "manifest_note": None if man else "investigation predates frozen manifests; inputs identified by product checksums",
                 "products": prods, "calibrations": man.get("calibrations") or result.get("calibration_products"),
                 "releases": sorted({p.get("release") for p in prods if p.get("release")}), "discovery": result.get("discovery")},
        "methods": {k: {"id": (v or {}).get("method_id"), "version": (v or {}).get("method_version")} for k, v in an.items()} | {
            "claims": claims["method_version"], "config_hash": result.get("config_hash"), "methods_version": result.get("methods_version")},
        "validation": {"methods_validated_on_real_data": [], "statement": "no method behind these claims is validated on real data; claims carry "
                       "strength PROVISIONAL_UNVALIDATED_METHOD where they assert anything (docs/VALIDATION.md)"},
        "claims": claims["claims"], "patterns": patterns, "hypotheses": result.get("hypotheses"),
        "hypotheses_note": "a SUPPORTED hypothesis is favoured only over the alternatives that were tested; CANNOT_EXCLUDE / NOT_ASSESSABLE "
                           "alternatives remain open, which is why the matching claim may read CONSISTENT_WITH",
        "limitations": sorted(set((an.get("variability") or {}).get("caveats", []) + (an.get("motion") or {}).get("limitations", []) + list(result.get("warnings") or []))),
        "versions": versions or [], "supersedes": result.get("supersedes"),
        "reproduce": {"command": result.get("reproduce"), "api": f"GET /api/v1/investigations/{result.get('investigation_id')}",
                      "offline": "import an evidence bundle (python3 -m heimdall evidence import) and replay with HEIMDALL_OFFLINE=1"}}


def render_markdown(rep: dict) -> str:
    t = rep.get("target") or {}
    lines = [f"# HEIMDALL investigation report: {t.get('name') or t.get('target_id')}", "",
             f"- Investigation `{rep['investigation_id']}` ({rep.get('data_origin')}); generated {rep['generated_at']} by HEIMDALL {rep['software_version']}",
             f"- Execution {((rep.get('status') or {}).get('execution'))}; test status {((rep.get('status') or {}).get('test_status'))}", "",
             "## Quick answer (from the admissible claims)", "", rep["quick_answer"], "", "## Claims", "",
             "| Analysis | Claim type | Strength | Gate | Statement |", "|---|---|---|---|---|"]
    for c in rep["claims"]:
        gate = "passed" if c["gate"]["passed"] else "failed: " + ", ".join(c["gate"]["failed_required"])
        lines.append(f"| {c['analysis']} | {c['claim_type']}" + (f" (from {c['downgraded_from']})" if c.get("downgraded_from") else "")
                     + f" | {c.get('strength')} | {gate} | {c['statement'].replace('|', '/')} |")
    lines += ["", "## Patterns", ""]
    for d in rep["patterns"]["dimensions"]:
        lines.append(f"- **{d['dimension']}**: {d['status']}" + (f" -- {d.get('finding')}" if d.get("finding") else "") + (f" ({d['reason']})" if d.get("reason") else ""))
    lines += ["", "## Hypotheses", ""]
    open_alt = [h.get("slug") for h in rep.get("hypotheses") or [] if h.get("status") in ("CANNOT_EXCLUDE", "NOT_ASSESSABLE")]
    for h in rep.get("hypotheses") or []:
        qual = (f" (relative to the tested alternatives only; not excluded: {', '.join(open_alt)})" if h.get("status") == "SUPPORTED" and open_alt else "")
        lines.append(f"- {h.get('title') or h.get('slug')}: **{h.get('status')}**{qual}" + (f" -- next: {h['suggested_next_step']}" if h.get("suggested_next_step") else ""))
    lines += ["", "## Data", "", f"- Manifest: {rep['data']['manifest_id'] or rep['data']['manifest_note']}", f"- Releases: {', '.join(rep['data']['releases'])}",
              f"- Products: {len(rep['data']['products'])} (SHA-256 per product in the JSON report)", "", "## Methods and validation", ""]
    for k, v in rep["methods"].items():
        lines.append(f"- {k}: {v}")
    lines += [f"- Validation: {rep['validation']['statement']}", "", "## Limitations", ""] + [f"- {x}" for x in rep["limitations"]] + ["", "## Versions", ""]
    for v in rep.get("versions") or []:
        lines.append(f"- `{v['investigation_id']}` {v.get('created_at')} -- {((v.get('status') or {}).get('execution'))}"
                     + (f"; supersedes `{v['supersedes']}`" if v.get("supersedes") else "") + (f"; superseded by `{v['superseded_by']}`" if v.get("superseded_by") else ""))
    lines += ["", "## Reproduce", "", f"- `{rep['reproduce']['command']}`", f"- {rep['reproduce']['api']}", f"- {rep['reproduce']['offline']}", ""]
    return "\n".join(lines)


def to_json(rep: dict) -> str:
    return json.dumps(rep, indent=1, sort_keys=True, allow_nan=False, default=str)
