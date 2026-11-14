"""Conversion of contract records to JSON-compatible data.

This is the boundary used by the future API and presentation layer: the UI
receives the same records the engine produced, converted here, and never
recomputes science. Secrets cannot be serialized.
"""

from __future__ import annotations

import dataclasses
import json
import math
from collections.abc import Mapping
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from . import SCHEMA_VERSION
from .config import Secret


def to_jsonable(obj: Any) -> Any:
    if isinstance(obj, Secret):
        raise TypeError("Secret values must never be serialized")
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj
    if isinstance(obj, float):
        if not math.isfinite(obj):
            raise ValueError("non-finite floats cannot be serialized")
        return obj
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, Mapping):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    raise TypeError(f"cannot serialize object of type {type(obj).__name__}")


def envelope(payload: Any, request_id: str | None = None) -> dict[str, Any]:
    """Wrap a payload with the schema version, as every API payload must carry it."""
    return {"schema_version": SCHEMA_VERSION, "request_id": request_id, "data": to_jsonable(payload)}


def dumps(payload: Any, request_id: str | None = None) -> str:
    return json.dumps(envelope(payload, request_id), sort_keys=True, allow_nan=False)
