"""Bounded investigation loop: discriminating follow-up actions for a saved target investigation (``heimdall.followup/1.0``).

From the admissible claims and the competing hypotheses, the planner proposes actions that could change the interpretation. Each action
names the hypotheses it can distinguish and what each would predict, its cost (products, estimated bytes from *measured* stored sizes),
whether its inputs are available, and a transparent priority (named components, not a probability). Actions that the engine cannot run
are listed with the reason.

Actions are persisted with deterministic ids (target + kind + parameters), so an identical follow-up is never submitted twice; repeated
planning reports the earlier execution and its outcome. Supported actions run through the ordinary readiness-first path as durable jobs,
within an explicit per-plan budget. The plan records why the loop stops (all actions done, unavailable, over budget, or no open
hypotheses), so an inconclusive action is not repeated endlessly.
"""

from __future__ import annotations

import json

from .contracts import ErrorCode, HeimdallError
from .dataservice.inventory import now_iso
from .dataservice.schemas import digest

FOLLOWUP_SCHEMA = "heimdall.followup/1.0"
OPEN = {"CANNOT_EXCLUDE", "NOT_ASSESSABLE", "CONSISTENT", "SUPPORTED"}
DDL = """CREATE TABLE IF NOT EXISTS followup_actions(
  action_id TEXT PRIMARY KEY, target_id TEXT NOT NULL, kind TEXT NOT NULL, params TEXT NOT NULL, state TEXT NOT NULL,
  from_investigation TEXT NOT NULL, job_id TEXT, est_products INTEGER, est_bytes INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  outcome TEXT)"""


def _db(service):
    service.db.execute(DDL)
    return service.db


def _mean_cutout_bytes(service) -> float | None:
    r = service.db.one("SELECT COUNT(*) n, AVG(p.size) s FROM product_keys k JOIN products p ON p.sha256=k.sha256 WHERE k.kind='science'")
    return r["s"] if r and r["n"] else None


def propose(result: dict, service) -> list[dict]:
    """Rule-based discriminating actions from the investigation's hypotheses and analysis limits (pure apart from size lookup)."""
    from .targets.investigate import DEFAULT_DETECTORS
    target = (result.get("target") or {}).get("target_id")
    opts = dict(result.get("options") or {})
    hyps = {h.get("slug"): h for h in result.get("hypotheses") or []}
    open_h = sorted(k for k, h in hyps.items() if h.get("status") in OPEN and k != "insufficient_evidence")
    an = result.get("analyses") or {}
    motion, var = an.get("motion") or {}, an.get("variability") or {}
    meas = [m for m in result.get("measurements") or [] if m.get("status") == "MEASURED"]
    sat = sum(1 for m in meas if m.get("saturation_risk"))
    mean_b = _mean_cutout_bytes(service)
    n_now = int(opts.get("max_observations") or 16)
    acts = []

    def add(kind, params, distinguishes, predictions, rationale, available=True, unavailable_reason=None, n_new=0):
        acts.append({"kind": kind, "params": params, "distinguishes": distinguishes, "predictions": predictions, "rationale": rationale,
                     "available": available, "unavailable_reason": unavailable_reason,
                     "cost": {"new_products_max": n_new, "estimated_bytes": int(n_new * mean_b) if (mean_b and n_new) else (0 if not n_new else None),
                              "estimate_basis": "measured mean stored cutout size" if mean_b else "no measured size"}})

    reasons = set(motion.get("reasons") or [])
    if motion and (reasons & {"FEWER_THAN_2_EPOCHS", "BASELINE_BELOW_20_DAYS", "FEWER_THAN_3_USABLE_POINTS"}) and n_now < 64:
        add("more_epochs", {"max_observations": min(64, n_now * 2)}, [h for h in ("catalogue_motion", "stationary", "registration_artifact") if h in hyps] or open_h,
            {"catalogue_motion": "fitted motion within errors of the catalogue value", "stationary": "no displacement beyond the registration floor",
             "registration_artifact": "control-star null scatter comparable to the target displacement"},
            "motion was not assessable for lack of usable epochs/baseline; more evenly spaced observations extend both", n_new=min(64, n_now * 2) - n_now)
    if var and var.get("verdict") == "NOT_ASSESSABLE" and n_now < 64:
        add("more_epochs", {"max_observations": min(64, n_now * 2)}, [h for h in ("intrinsic_variability", "constant_source", "calibration_artifact") if h in hyps] or open_h,
            {"intrinsic_variability": "a comparable series departs from constant flux beyond the control reference",
             "constant_source": "series consistent with constant flux at the stated sensitivity"},
            "no comparable series had enough points and epochs", n_new=min(64, n_now * 2) - n_now)
    if meas and sat >= max(1, len(meas) // 2):
        current = tuple(opts.get("detectors") or DEFAULT_DETECTORS.get(target) or ())
        others = tuple(d for d in range(1, 7) if d not in current)
        add("other_detectors", {"detectors": list(range(1, 7))}, open_h,
            {"any": "unsaturated measurements in detectors/wavelengths where the source is fainter make the tests assessable"},
            f"{sat} of {len(meas)} measured images are saturation-flagged in detectors {list(current) or 'default'}; other detectors {list(others)} sample "
            "other wavelengths where the source may not saturate", available=bool(others), unavailable_reason=None if others else "all detectors already used",
            n_new=n_now)
    ref = var.get("control_reference") or {}
    if var and (ref.get("n_control_series_completed") or 0) < 5:
        add("more_control_stars", {"note": "requires a larger field-star sample in the same images"}, [h for h in ("calibration_artifact", "intrinsic_variability") if h in hyps]
            or ["field systematics"], {"field systematics": "control-star chi2 distribution calibrates the error model"},
            f"only {ref.get('n_control_series_completed', 0)} completed control series (5 needed for a false-alarm reference)", available=False,
            unavailable_reason="a larger cutout changes the measured field; not exposed as an automatic action yet (run an alternate analysis with cutout_size_deg)")
    for slug, h in hyps.items():
        for tu in h.get("tests_unavailable") or []:
            test = tu.get("test") if isinstance(tu, dict) else (tu[0] if isinstance(tu, (list, tuple)) else str(tu))
            reason = tu.get("reason") if isinstance(tu, dict) else (tu[1] if isinstance(tu, (list, tuple)) and len(tu) > 1 else "")
            if "deblend" in str(test).lower():
                add("deblending", {"test": test}, [slug], {slug: "component fluxes/centroids separated"}, "listed as unavailable by the hypothesis",
                    available=False, unavailable_reason=reason or "not implemented")
    saturated = bool(meas) and sat >= max(1, len(meas) // 2)
    for a in acts:
        a["action_id"] = "fup:" + digest([target, a["kind"], a["params"]], 14)
        low = a["kind"] == "more_epochs" and saturated
        a["expected_value"] = "LOW" if low else "HIGH" if a["available"] else "NOT_RUNNABLE"
        if low:
            a["rationale"] += "; but most images are saturation-flagged in these detectors, so more epochs in the same detectors are unlikely to resolve it"
        a["priority"] = {"open_hypotheses_addressed": len([d for d in a["distinguishes"] if d in open_h]), "available": a["available"],
                         "expected_value": a["expected_value"], "new_products": a["cost"]["new_products_max"],
                         "rule": "order: available first, then expected value (a known blocking limit such as saturation lowers it), then more open "
                                 "hypotheses addressed, then fewer new products (heuristic, not a probability)"}
    acts.sort(key=lambda a: (not a["available"], a["expected_value"] == "LOW", -a["priority"]["open_hypotheses_addressed"], a["cost"]["new_products_max"]))
    seen, uniq = set(), []
    for a in acts:  # the same action proposed by two rules is one action
        if a["action_id"] not in seen:
            seen.add(a["action_id"])
            uniq.append(a)
    return uniq


def plan(engine, target_id: str, *, budget_products: int = 32) -> dict:
    svc = engine.data_service()
    db = _db(svc)
    res = engine.target_result(target_id)
    acts = propose(res, svc)
    now = now_iso(svc.clock())
    for a in acts:
        db.execute("INSERT OR IGNORE INTO followup_actions(action_id,target_id,kind,params,state,from_investigation,est_products,est_bytes,created_at,updated_at) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?)", (a["action_id"], target_id, a["kind"], json.dumps(a["params"], sort_keys=True),
                                                   "PROPOSED" if a["available"] else "UNAVAILABLE", res["investigation_id"], a["cost"]["new_products_max"],
                                                   a["cost"]["estimated_bytes"], now, now))
    _refresh(engine, target_id)
    rows = {r["action_id"]: dict(r) for r in db.query("SELECT * FROM followup_actions WHERE target_id=?", (target_id,))}
    for a in acts:
        r = rows[a["action_id"]]
        a["state"], a["job_id"], a["outcome"] = r["state"], r["job_id"], json.loads(r["outcome"]) if r["outcome"] else None
    spent = sum(r["est_products"] or 0 for r in rows.values() if r["state"] in ("SUBMITTED", "DONE"))
    runnable = [a for a in acts if a["state"] == "PROPOSED" and spent + a["cost"]["new_products_max"] <= budget_products]
    hyps_open = [h.get("slug") for h in res.get("hypotheses") or [] if h.get("status") in OPEN and h.get("slug") != "insufficient_evidence"]
    states = {a["state"] for a in acts}
    stop = (None if runnable else "no open competing hypotheses" if not hyps_open else
            "no discriminating action is defined for this result" if not acts else
            "no runnable action: the remaining actions are unavailable (see their reasons)" if states <= {"UNAVAILABLE"} else
            "all runnable actions were executed; the rest are unavailable" if "PROPOSED" not in states else
            f"budget of {budget_products} new products reached")
    return {"schema": FOLLOWUP_SCHEMA, "target_id": target_id, "from_investigation": res["investigation_id"], "open_hypotheses": hyps_open,
            "budget": {"new_products": budget_products, "committed": spent}, "actions": acts, "next_action": runnable[0]["action_id"] if runnable else None,
            "stopping_reason": stop}


def _refresh(engine, target_id: str) -> None:
    svc = engine.data_service()
    for r in svc.db.query("SELECT * FROM followup_actions WHERE target_id=? AND state='SUBMITTED'", (target_id,)):
        job = svc.jobs.get(r["job_id"]) if r["job_id"] else None
        if job is None:
            continue
        st = job["state"].value
        if st in ("COMPLETE", "NO_COVERAGE", "UNAVAILABLE_AFTER_DEADLINE", "INVALID_SOURCE_DATA", "CANCELLED", "FAILED"):
            out = {"job_state": st, "investigation_id": (job["result"] or {}).get("investigation_id"), "status": (job["result"] or {}).get("status")}
            svc.db.execute("UPDATE followup_actions SET state='DONE', outcome=?, updated_at=? WHERE action_id=?", (json.dumps(out), now_iso(svc.clock()), r["action_id"]))


def run(engine, action_id: str, *, budget_products: int = 32) -> dict:
    """Submit one proposed action as a durable job (never twice; never over budget)."""
    from .targets.investigate import TargetOptions
    from .targets import prepare
    svc = engine.data_service()
    db = _db(svc)
    r = db.one("SELECT * FROM followup_actions WHERE action_id=?", (action_id,))
    if r is None:
        raise HeimdallError.of(ErrorCode.INPUT_INVALID, f"unknown follow-up action {action_id}; plan first")
    if r["state"] != "PROPOSED":
        return {"action_id": action_id, "state": r["state"], "job_id": r["job_id"], "note": "not submitted again (actions are never repeated)"}
    spent = db.one("SELECT COALESCE(SUM(est_products),0) FROM followup_actions WHERE target_id=? AND state IN ('SUBMITTED','DONE')", (r["target_id"],))[0]
    if spent + (r["est_products"] or 0) > budget_products:
        return {"action_id": action_id, "state": "PROPOSED", "note": f"over budget ({spent} + {r['est_products']} > {budget_products} new products)"}
    res = engine.target_result(r["target_id"])
    base = {k: v for k, v in (res.get("options") or {}).items() if k in TargetOptions.__dataclass_fields__}
    params = json.loads(r["params"])
    opts = TargetOptions(**{**base, **{k: (tuple(v) if isinstance(v, list) else v) for k, v in params.items() if k in TargetOptions.__dataclass_fields__},
                             "detectors": tuple(params["detectors"]) if params.get("detectors") else (tuple(base["detectors"]) if base.get("detectors") else None)})
    from .targets.registry import get_target
    st = prepare.request_investigation(svc, get_target(r["target_id"]), opts, settings=engine.settings, config=engine.config, registry=engine.validation,
                                       store=engine.store, analyse_inline=False)
    job_id = (st.get("job") or {}).get("job_id")
    state = "SUBMITTED" if job_id else "DONE"
    outcome = None if job_id else json.dumps({"investigation_id": st.get("investigation_id"), "note": "served from a stored identical result"})
    db.execute("UPDATE followup_actions SET state=?, job_id=?, outcome=?, updated_at=? WHERE action_id=?", (state, job_id, outcome, now_iso(svc.clock()), action_id))
    return {"action_id": action_id, "state": state, "job_id": job_id, "data_status": {k: st.get(k) for k in ("readiness", "message", "must_resubmit")}}
