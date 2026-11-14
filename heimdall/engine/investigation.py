"""Subject-level scientific investigations built from a serialized investigation result.

``build`` takes the plain-JSON result (a live pipeline result after ``plain()`` or a saved ``investigation.json``) and returns one
investigation per subject (a source, or a linked track) that has at least one candidate. It runs no detection and downloads nothing, so
the same code serves the pipeline stage and offline re-investigation. Candidate attention levels and the follow-up rank are never changed.

Interpretation statuses (per subject, over the patterns not already explained by an ordinary hypothesis):
  EXPLAINED      every pattern has a SUPPORTED ordinary/identification hypothesis (instrumental, catalogue, known body)
  INCONCLUSIVE   no unexplained pattern is established (failed or unresolved critical checks, MISCALIBRATED cell)
  PROVISIONAL    an astrophysical hypothesis, or the Planet X scenario, is CONSISTENT (not contradicted; not support)
  UNRESOLVED     conflicting evidence for a hypothesis
  UNEXPLAINED_UNDER_INVESTIGATION  an established pattern that every catalogue hypothesis contradicts: kept open, never read as Planet X
  NOT_ASSESSABLE required analyses are unavailable
"""

from __future__ import annotations

from .config import EngineConfig
from .evidence import EVIDENCE_VERSION, ResultIndex, candidate_gate, dependency_groups, extract
from .hypotheses import (CATALOGUE, HYPOTHESIS_CATALOGUE_VERSION, INSTRUMENTAL_IDS, ORDINARY_FAMILIES, ORDINARY_IDS, Ctx, evaluate_spec,
                         from_alternative, planet_x_hypothesis)
from .planetx import PLANETX_RULES_VERSION
from .reasoning import REASONING_RULES_VERSION

INVESTIGATION_RULES_VERSION = "investigation-1.0.0-provisional"
INVESTIGATIONS_SCHEMA = "heimdall.investigations/1.0"
PATTERN_OF = {("VARIABILITY", "SOURCE"): "FLUX_CHANGE", ("TRANSIENT", "SOURCE"): "SINGLE_EPOCH", ("MOTION", "SOURCE"): "POSITION_CHANGE",
              ("MOTION", "TRACK"): "LINKED_MOTION"}
STATUS_ORDER = {"SUPPORTED": 0, "CONSISTENT": 1, "UNRESOLVED": 2, "NOT_ASSESSABLE": 3, "CONTRADICTED": 4}
FAMILY_ORDER = {"KNOWN_OBJECT": 0, "ORDINARY": 1, "INSTRUMENTAL": 2, "ASTROPHYSICAL": 3, "HYPOTHESIS_OF_INTEREST": 4}


def _subjects(result: dict) -> dict[tuple[str, str], list[dict]]:
    out: dict[tuple[str, str], list[dict]] = {}
    for c in result.get("candidates") or []:
        out.setdefault((c["subject_kind"], c["subject_id"]), []).append(c)
    return {k: sorted(v, key=lambda c: c["candidate_id"]) for k, v in sorted(out.items())}


def _interpret(patterns: list[dict], hyps: list[dict]) -> tuple[str, list[str], list[dict], list[str]]:
    explained = {c for h in hyps if h["family"] in ORDINARY_FAMILIES and h["status"] == "SUPPORTED" for c in h["applies_to_candidates"]}
    unexplained = [p for p in patterns if p["candidate_id"] not in explained]
    viable = sorted(h["hypothesis_id"] for h in hyps if h["status"] in ("SUPPORTED", "CONSISTENT"))
    contradictions = sorted({f"{h['hypothesis_id']}: {h['status_rule']}" for h in hyps if h["status"] == "CONTRADICTED" and h["family"] in ("ASTROPHYSICAL", "HYPOTHESIS_OF_INTEREST")})
    if not patterns:
        return "NOT_ASSESSABLE", viable, [], contradictions
    if not unexplained:
        return "EXPLAINED", viable, [], contradictions
    open_patterns = [{"pattern": p["pattern"], "candidate_id": p["candidate_id"]} for p in unexplained]
    adm = [p for p in unexplained if p["admissible"]]
    if not adm:
        if all(p["attention"] == "NOT_ASSESSABLE" for p in unexplained):
            return "NOT_ASSESSABLE", viable, open_patterns, contradictions
        return "INCONCLUSIVE", viable, open_patterns, contradictions
    ids = {p["candidate_id"] for p in adm}
    relevant = [h for h in hyps if h["family"] in ("ASTROPHYSICAL", "HYPOTHESIS_OF_INTEREST") and ids & set(h["applies_to_candidates"])]
    if any(h["status"] == "UNRESOLVED" for h in relevant):
        return "UNRESOLVED", viable, open_patterns, contradictions
    if any(h["status"] in ("CONSISTENT", "SUPPORTED") for h in relevant):
        return "PROVISIONAL", viable, open_patterns, contradictions
    if relevant and all(h["status"] == "NOT_ASSESSABLE" for h in relevant):
        return "NOT_ASSESSABLE", viable, open_patterns, contradictions
    return "UNEXPLAINED_UNDER_INVESTIGATION", viable, open_patterns, contradictions


def _investigate(index: ResultIndex, subject: tuple[str, str], cands: list[dict], cfg: EngineConfig, versions: dict) -> dict:
    kind, sid = subject
    items = extract(index, kind, sid, cands)
    groups, relations = dependency_groups(items)
    group_of = {e: g["group_id"] for g in groups for e in g["evidence_ids"]}
    by_type: dict[str, list] = {}
    for it in items:
        by_type.setdefault(it.type, []).append(it)
    patterns = []
    for c in cands:
        ok, reasons = candidate_gate(c)
        patterns.append({"pattern": PATTERN_OF[(c["kind"], c["subject_kind"])], "candidate_id": c["candidate_id"], "admissible": ok, "reasons": reasons,
                         "attention": c["candidate"]["attention_level"], "validity": (c.get("cell_validity") or {}).get("state"), "candidate": c})
    src = index.sources.get(sid) if kind == "SOURCE" else None
    ctx = Ctx(subject_kind=kind, subject_id=sid, patterns=patterns, evidence=by_type, group_of=group_of, source=src,
              analyses=(src or {}).get("analyses") or {}, r_min_au=cfg.planetx.r_min_au)
    hyps: list[dict] = []
    for p in patterns:  # existing instrumental / ordinary alternatives, re-expressed
        for a in p["candidate"]["candidate"]["alternatives"]:
            if a["alt_id"] in INSTRUMENTAL_IDS or a["alt_id"] in ORDINARY_IDS:
                hyps.append(from_alternative(a, p["candidate"]))
    present = {p["pattern"] for p in patterns}
    for spec in CATALOGUE:
        trig = [p for p in patterns if p["pattern"] in spec.triggers]
        if (trig and (spec.when is None or spec.when(ctx))) or (not spec.triggers and spec.when is not None and spec.when(ctx)):
            hyps.append(evaluate_spec(spec, ctx, trig or [p for p in patterns if p["pattern"] in ("FLUX_CHANGE", "SINGLE_EPOCH")]))
    px_cand = next((p["candidate"] for p in patterns if p["pattern"] in ("POSITION_CHANGE", "LINKED_MOTION") and (p["candidate"].get("planet_x") or {}).get("applicable")), None)
    px_h = planet_x_hypothesis(px_cand, ctx) if px_cand else None
    if px_h:
        hyps.append(px_h)
    hyps.sort(key=lambda h: (FAMILY_ORDER[h["family"]], STATUS_ORDER[h["status"]], h["hypothesis_id"], h["applies_to_candidates"]))
    for n, h in enumerate(hyps):
        h["instance_id"] = f"H:{sid}:{n}"
    status, viable, unexplained, contradictions = _interpret([{k: v for k, v in p.items() if k != "candidate"} for p in patterns], hyps)
    followups, seen = [], set()
    for h in hyps:
        for f in h["follow_ups"]:
            if f["action"] not in seen:
                seen.add(f["action"])
                followups.append({**f, "for_hypotheses": sorted({x["hypothesis_id"] for x in hyps if any(y["action"] == f["action"] for y in x["follow_ups"])})})
    missing = sorted({f"{h['hypothesis_id']}.{r['prediction_id']}: {r['not_assessable_reason']}" for h in hyps for r in h["predictions"] if r["result"] == "NOT_ASSESSABLE" and r["required"]})
    if px_h:
        planet = {"candidate_id": px_cand["candidate_id"], "status": px_h["status"], "source_status": px_h["planet_x_source_status"], "disclaimer": px_h["disclaimer"]}
    else:
        planet = {"status": "NOT_APPLICABLE", "reason": "no positional-change evidence; an unexplained pattern is never assumed to be Planet X"}
    first = cands[0]
    return {
        "investigation_id": f"I:{sid}",
        "subject": {"kind": kind, "id": sid, "ra_deg": first["position"]["ra_deg"], "dec_deg": first["position"]["dec_deg"], "ellipse_1sigma": first["position"].get("ellipse_1sigma"),
                    "member_source_ids": (index.tracks.get(sid) or {}).get("source_ids") if kind == "TRACK" else [sid]},
        "candidate_ids": [c["candidate_id"] for c in cands],
        "patterns": [{k: v for k, v in p.items() if k != "candidate"} for p in patterns],
        "evidence": [i.to_dict() for i in items], "dependency_groups": groups, "evidence_relations": relations,
        "hypotheses": hyps,
        "interpretation": {"status": status, "viable_hypotheses": viable, "unexplained_patterns": unexplained, "contradictions": contradictions,
                           "note": "a status of consistency is not support; no probability or score is computed"},
        "missing_evidence": missing, "follow_ups": followups, "planet_x": planet,
        "upstream": {"source_analyses": (src or {}).get("analyses"), "context_reason": index.context.get("reason"),
                     "candidate_attention": {c["candidate_id"]: c["candidate"]["attention_level"] for c in cands},
                     "validity": {c["candidate_id"]: (c.get("cell_validity") or {}).get("state") for c in cands}},
        "versions": versions,
    }


def build(result: dict, config: EngineConfig | None = None) -> dict:
    """Investigations for every subject with a candidate. Deterministic; reads only ``result``."""
    cfg = config or EngineConfig.from_dict(result["config"])
    index = ResultIndex(result)
    versions = {"investigation_rules": INVESTIGATION_RULES_VERSION, "hypothesis_catalogue": HYPOTHESIS_CATALOGUE_VERSION, "evidence": EVIDENCE_VERSION,
                "reasoning_rules": REASONING_RULES_VERSION, "planetx_rules": PLANETX_RULES_VERSION, "config_hash": result.get("config_hash"),
                "source_run_id": result.get("run_id")}
    invs = [_investigate(index, s, c, cfg, versions) for s, c in _subjects(result).items()]
    counts: dict[str, int] = {}
    for i in invs:
        counts[i["interpretation"]["status"]] = counts.get(i["interpretation"]["status"], 0) + 1
    hyp_counts: dict[str, int] = {}
    for i in invs:
        for h in i["hypotheses"]:
            if h["family"] in ("ASTROPHYSICAL", "KNOWN_OBJECT", "HYPOTHESIS_OF_INTEREST"):
                k = f"{h['hypothesis_id']}:{h['status']}"
                hyp_counts[k] = hyp_counts.get(k, 0) + 1
    return {"schema": INVESTIGATIONS_SCHEMA, "versions": versions, "n_investigations": len(invs), "interpretation_counts": dict(sorted(counts.items())),
            "hypothesis_counts": dict(sorted(hyp_counts.items())), "investigations": invs}
