"""Re-evaluate a saved target investigation after a decision-rule correction, without new data and without erasing history.

Used when a defect in a *decision rule* (not in the measurements) invalidates a saved verdict. The saved measurements are re-analysed
with the corrected method; the per-series statistics must reproduce exactly (otherwise the inputs are not the same and a full rerun is
required). The result is a **new linked version**: new ``investigation_id``, ``supersedes`` block (old id, reason, changed sections,
method versions, verdict change), and ``latest.json`` points to it. The superseded file is never modified; the supersession is also
appended to ``supersessions.jsonl`` in the target directory.

Scope today: the variability analysis of targets whose hypotheses and conclusion do not depend on it (moving-object targets, whose
primary analysis is motion). A variable-star target whose verdict changes needs a full rerun (hypotheses are built from variability).
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

from ..contracts import ErrorCode, HeimdallError
from ..pipeline.results import canonical_hash, plain
from .registry import MOVING
from .variability import analyse_variability


def _series_stats(v: dict) -> dict:
    return {s["series_id"]: (s["test"]["status"], s["test"].get("chi2"), s["test"].get("dof")) for s in v.get("series", [])}


def reevaluate_variability(state_dir: Path, config, target_id: str, *, reason: str, now: datetime | None = None, dry_run: bool = False) -> dict:
    d = Path(state_dir) / "targets" / target_id
    latest = d / "latest.json"
    if not latest.is_file():
        raise HeimdallError.of(ErrorCode.INPUT_INVALID, f"no saved investigation for {target_id}")
    old = json.loads(latest.read_text(encoding="utf-8"))
    var_old = (old.get("analyses") or {}).get("variability")
    if var_old is None:
        return {"changed": False, "reason": "the saved investigation has no variability analysis", "investigation_id": old["investigation_id"]}
    if old.get("config_hash") != config.hash:
        raise HeimdallError.of(ErrorCode.INPUT_INVALID, "engine configuration differs from the saved investigation; run a full investigation instead")
    var_new = plain(analyse_variability(old["measurements"], config))
    if _series_stats(var_new) != _series_stats(var_old):
        raise HeimdallError.of(ErrorCode.INPUT_INVALID, "per-series statistics do not reproduce from the saved measurements; a full rerun is required")
    if (var_new["verdict"], var_new["verdict_reason"]) == (var_old["verdict"], var_old["verdict_reason"]):
        return {"changed": False, "reason": "the corrected rule gives the same verdict", "investigation_id": old["investigation_id"], "verdict": var_old["verdict"]}
    if old["target"].get("category") != MOVING:
        raise HeimdallError.of(ErrorCode.INPUT_INVALID, "hypotheses of this target depend on the variability verdict; run a full investigation instead")
    now = now or datetime.now(UTC)
    new = copy.deepcopy(old)
    new["analyses"]["variability"] = var_new
    new["created_at"] = now.isoformat()
    new["supersedes"] = {
        "investigation_id": old["investigation_id"], "superseded_created_at": old.get("created_at"), "reason": reason,
        "changed_sections": ["analyses.variability"],
        "unchanged": "measurements, products, input data, reference astrometry, motion analysis, hypotheses and conclusion are carried over unchanged "
                     "(the hypotheses and conclusion of a moving-object target are built from the motion analysis)",
        "method_versions": {"target_variability": [var_old.get("method_version"), var_new.get("method_version")]},
        "verdict_change": {"from": var_old["verdict"], "to": var_new["verdict"], "new_reason": var_new["verdict_reason"]},
        "new_data_used": False}
    new["warnings"] = list(old.get("warnings", [])) + [
        f"re-evaluated from saved measurements on {now.date().isoformat()} after a decision-rule correction (supersedes {old['investigation_id']})"]
    new["investigation_id"] = f"tinv:{target_id}:{canonical_hash([new['target'], new['options'], new['config_hash'], now.isoformat(), 'reevaluation', old['investigation_id']])[7:15]}"
    new["results_hash"] = canonical_hash({k: new.get(k) for k in ("target", "reference", "coverage", "analyses", "hypotheses", "conclusion", "config_hash")})
    new.pop("saved_to", None)
    out = {"changed": True, "superseded": old["investigation_id"], "investigation_id": new["investigation_id"], "verdict_change": new["supersedes"]["verdict_change"]}
    if dry_run:
        return out | {"dry_run": True}
    path = d / f"{new['investigation_id'].split(':')[-1]}.json"
    old_file = d / f"{old['investigation_id'].split(':')[-1]}.json"
    if not old_file.is_file():  # keep the superseded version inspectable even if only latest.json held it
        old_file.write_text(latest.read_text(encoding="utf-8"), encoding="utf-8")
    text = json.dumps(new, indent=1, sort_keys=True, allow_nan=False)
    path.write_text(text, encoding="utf-8")
    tmp = latest.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(latest)
    with (d / "supersessions.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": now.isoformat(), "old": old["investigation_id"], "new": new["investigation_id"], "reason": reason,
                             "changed_sections": ["analyses.variability"]}, sort_keys=True) + "\n")
    return out | {"saved_to": str(path)}
