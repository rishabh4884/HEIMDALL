"""Interfaces between the pipeline stages (PROVISIONAL signatures).

Flow: data access -> normalization -> analysis stages (detection, association,
motion/variability/spectral analysis) -> validation -> categorization ->
presentation (via ``heimdall.serialization``).

Each stage implements its interface and may revise a signature through a
versioned change if real data require it.

``ComputationStatus`` records whether a stage *ran*. It is separate from
scientific validity: a stage can complete while its result stays unvalidated,
and "not applicable" is distinct from "failed".
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Generic, Protocol, TypeVar

from ..contracts._base import non_empty_str, require
from ..contracts.candidates import Candidate
from ..contracts.observations import Observation
from ..contracts.outcomes import ErrorInfo
from ..contracts.validity import Check
from ..data_access.interface import RetrievalResult

ConfigValue = str | int | float | bool


def config_hash(params: Mapping[str, ConfigValue]) -> str:
    """Deterministic hash of a configuration, recorded in every result (ARC-09)."""
    canonical = json.dumps(dict(params), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class StageConfig:
    stage_id: str
    version: str
    params: Mapping[str, ConfigValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require(non_empty_str(self.stage_id), "StageConfig.stage_id must be non-empty")
        require(non_empty_str(self.version), "StageConfig.version must be non-empty")
        for key, value in self.params.items():
            require(non_empty_str(key), "StageConfig parameter names must be non-empty")
            require(isinstance(value, (str, int, float, bool)), f"parameter {key} must be a scalar")
        object.__setattr__(self, "params", MappingProxyType(dict(self.params)))

    @property
    def hash(self) -> str:
        return config_hash({"__stage__": self.stage_id, "__version__": self.version, **self.params})


class ComputationStatus(StrEnum):
    COMPLETED = "COMPLETED"
    NOT_APPLICABLE = "NOT_APPLICABLE"  # the analysis does not apply to these inputs
    INSUFFICIENT_INPUTS = "INSUFFICIENT_INPUTS"  # required inputs are unavailable
    FAILED = "FAILED"  # the computation itself failed


T = TypeVar("T")


@dataclass(frozen=True, slots=True, kw_only=True)
class StageOutcome(Generic[T]):
    status: ComputationStatus
    config_hash: str
    outputs: tuple[T, ...] = ()
    reasons: tuple[str, ...] = ()
    error: ErrorInfo | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "outputs", tuple(self.outputs))
        object.__setattr__(self, "reasons", tuple(self.reasons))
        require(non_empty_str(self.config_hash), "StageOutcome.config_hash must be non-empty")
        if self.status is ComputationStatus.FAILED:
            require(self.error is not None, "a FAILED stage outcome requires an error")
            require(not self.outputs, "a FAILED stage outcome carries no outputs")
        else:
            require(self.error is None, "only a FAILED stage outcome carries an error")
        if self.status in (ComputationStatus.NOT_APPLICABLE, ComputationStatus.INSUFFICIENT_INPUTS):
            require(bool(self.reasons), f"a {self.status} stage outcome must state why")
            require(not self.outputs, f"a {self.status} stage outcome carries no outputs")


@dataclass(frozen=True, slots=True, kw_only=True)
class Exclusion:
    """An input excluded from further processing, with an explicit reason."""

    ref: str
    reason: str

    def __post_init__(self) -> None:
        require(non_empty_str(self.ref) and non_empty_str(self.reason), "Exclusion needs a ref and a reason")


@dataclass(frozen=True, slots=True, kw_only=True)
class NormalizationResult:
    observations: tuple[Observation, ...]
    excluded: tuple[Exclusion, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "observations", tuple(self.observations))
        object.__setattr__(self, "excluded", tuple(self.excluded))


In = TypeVar("In", contravariant=True)
Out = TypeVar("Out", covariant=True)


class Normalizer(Protocol):
    """Turns retrieved source products into observation records."""

    normalizer_id: str
    version: str

    def normalize(self, retrieval: RetrievalResult) -> NormalizationResult: ...


class AnalysisStage(Protocol[In, Out]):
    """A detection, association or analysis stage."""

    stage_id: str
    version: str

    def run(self, inputs: In, config: StageConfig) -> StageOutcome[Out]: ...


class Validator(Protocol):
    """Evaluates checks for a candidate under a frozen configuration."""

    validator_id: str
    version: str

    def evaluate(self, candidate: Candidate, config: StageConfig) -> tuple[Check, ...]: ...
