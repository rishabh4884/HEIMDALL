"""JSON conversion and hashing of engine results.

``plain`` converts dataclasses, enums, numpy values and contract records into
JSON-compatible data. Non-finite floats become ``null``: in HEIMDALL outputs a
``null`` number always means "not computed or not available", and the
accompanying status/reason fields say why. Secrets cannot be converted.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
from collections.abc import Mapping
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

import numpy as np

from ..config import Secret

INVESTIGATION_SCHEMA = "heimdall.investigation/1.1"


def plain(obj: Any) -> Any:
    if isinstance(obj, Secret):
        raise TypeError("Secret values must never be serialized")
    if isinstance(obj, Enum):
        return obj.value
    if obj is None or isinstance(obj, (bool, str)):
        return obj
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, (int, np.integer)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        value = float(obj)
        return value if math.isfinite(value) else None
    if isinstance(obj, np.ndarray):
        return plain(obj.tolist())
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: plain(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, Mapping):
        return {str(k): plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        items = [plain(v) for v in obj]
        return sorted(items, key=json.dumps) if isinstance(obj, (set, frozenset)) else items
    raise TypeError(f"cannot convert object of type {type(obj).__name__}")


def canonical_json(obj: Any) -> str:
    return json.dumps(plain(obj), sort_keys=True, separators=(",", ":"), allow_nan=False)


def canonical_hash(obj: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def scientific_core(result: dict) -> dict:
    """The part of a result that must be identical when a run is reproduced.

    Excludes run ids, timestamps, durations and validity references (validity
    depends on the validation registry at run time).
    """
    def strip_candidate(c: dict) -> dict:
        cand = c.get("candidate", {})
        return {"candidate_id": c.get("candidate_id"), "statistic": c.get("statistic"), "quality_grade": c.get("quality_grade"),
                "checks": [(k.get("check_id"), k.get("state")) for k in cand.get("checks", [])],
                "alternatives": [(a.get("alt_id"), a.get("state")) for a in cand.get("alternatives", [])],
                "planet_x_status": (c.get("planet_x") or {}).get("status")}

    return {
        "config_hash": result.get("config_hash"),
        "inputs": sorted((o.get("obs_id"), o.get("product_checksum")) for o in result.get("observations", [])),
        "detections": result.get("detections"),
        "sources": [{k: v for k, v in s.items() if k != "forced"} for s in result.get("sources", [])],
        "tracks": result.get("tracks"),
        "transients": result.get("transients"),
        "candidates": [strip_candidate(c) for c in result.get("candidates", [])],
        "investigations": [(i.get("investigation_id"), (i.get("interpretation") or {}).get("status"),
                            [(h.get("hypothesis_id"), h.get("status")) for h in i.get("hypotheses", [])])
                           for i in (result.get("investigations") or {}).get("investigations", [])],
    }
