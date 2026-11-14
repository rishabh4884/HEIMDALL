"""Detections, associations and candidates.

``Candidate`` enforces the structural invariants of the attention rules
(contract §10.5). It does not classify: assigning a level is the job of the
categorization layer. It refuses combinations that would let an
unsupported conclusion look validated, for example ``ATTEND`` without
``VALIDATED_FOR_CONFIG``, or ``WATCH`` when a critical assumption failed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from ._base import non_empty_str, require
from .observations import Measurement
from .validity import (
    Alternative,
    AlternativeState,
    Check,
    CheckState,
    Statement,
    Validity,
    ValidityReason,
    ValidityState,
)
from .values import Tag, UnavailableReason, ValueClass, ValueObject, unavailable


@dataclass(frozen=True, slots=True, kw_only=True)
class Detection:
    """A source-like signal found in one observation. Detection is not validation."""

    det_id: str
    obs_id: str
    method_id: str
    method_version: str
    measurements: tuple[Measurement, ...] = ()
    quality_flags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("det_id", "obs_id", "method_id", "method_version"):
            require(non_empty_str(getattr(self, name)), f"Detection.{name} must be non-empty")
        object.__setattr__(self, "measurements", tuple(self.measurements))
        object.__setattr__(self, "quality_flags", tuple(self.quality_flags))


@dataclass(frozen=True, slots=True, kw_only=True)
class Association:
    """A judgement that detections in different observations are the same source.

    Ambiguity is represented, never forced into a single match.
    """

    assoc_id: str
    det_ids: tuple[str, ...]
    ambiguity: ValueObject
    # Competing groupings of detection ids that were also plausible.
    alternative_groupings: tuple[tuple[str, ...], ...] = ()
    method_id: str = ""

    def __post_init__(self) -> None:
        require(non_empty_str(self.assoc_id), "Association.assoc_id must be non-empty")
        object.__setattr__(self, "det_ids", tuple(self.det_ids))
        object.__setattr__(self, "alternative_groupings", tuple(tuple(g) for g in self.alternative_groupings))
        require(len(self.det_ids) >= 2, "an association links at least two detections")
        require(len(set(self.det_ids)) == len(self.det_ids), "an association must not repeat a detection")
        require(isinstance(self.ambiguity, ValueObject), "Association.ambiguity must be a ValueObject")


class CandidateKind(StrEnum):
    MOTION = "MOTION"
    VARIABILITY = "VARIABILITY"
    SPECTRAL = "SPECTRAL"
    # Added in the engine work: present in one epoch, absent at a comparable wavelength elsewhere.
    TRANSIENT = "TRANSIENT"


class AttentionLevel(StrEnum):
    ATTEND = "ATTEND"
    WATCH = "WATCH"
    EXPLAINED = "EXPLAINED"
    INCONCLUSIVE = "INCONCLUSIVE"
    NOT_ASSESSABLE = "NOT_ASSESSABLE"


class AttentionReasonCode(StrEnum):
    ASSUMPTION_CHECK_FAILED = "ASSUMPTION_CHECK_FAILED"
    ASSUMPTION_CHECK_UNRESOLVED = "ASSUMPTION_CHECK_UNRESOLVED"
    OTHER = "OTHER"


@dataclass(frozen=True, slots=True, kw_only=True)
class AttentionReason:
    code: AttentionReasonCode
    failed_checks: tuple[str, ...] = ()
    unresolved_checks: tuple[str, ...] = ()
    note: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "failed_checks", tuple(self.failed_checks))
        object.__setattr__(self, "unresolved_checks", tuple(self.unresolved_checks))


def _not_applicable_anomaly() -> ValueObject:
    # SCI-12: no anomaly score in v1.
    return unavailable(value_class=ValueClass.QUANTITY, reason=UnavailableReason.NOT_APPLICABLE)


@dataclass(frozen=True, slots=True, kw_only=True)
class Candidate:
    candidate_id: str
    kind: CandidateKind
    attention_level: AttentionLevel
    validity: Validity
    measurements: tuple[Measurement, ...] = ()
    checks: tuple[Check, ...] = ()
    alternatives: tuple[Alternative, ...] = ()
    statements: tuple[Statement, ...] = ()
    attention_reason: AttentionReason | None = None
    # Set only when the validity cap is the sole reason for WATCH.
    cap_reason: ValidityReason | None = None
    anomaly: ValueObject = field(default_factory=_not_applicable_anomaly)

    def __post_init__(self) -> None:
        require(non_empty_str(self.candidate_id), "Candidate.candidate_id must be non-empty")
        for name in ("measurements", "checks", "alternatives", "statements"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        ids = [c.check_id for c in self.checks]
        require(len(ids) == len(set(ids)), "check ids must be unique within a candidate")
        require(
            self.anomaly.tag is Tag.UNAVAILABLE and self.anomaly.reason is UnavailableReason.NOT_APPLICABLE,
            "no anomaly score exists in v1; anomaly must be U/NOT_APPLICABLE",
        )
        self._check_attention_invariants()

    @property
    def failed_critical_checks(self) -> tuple[str, ...]:
        return tuple(c.check_id for c in self.checks if c.is_applicable_critical and c.state is CheckState.FAILED)

    @property
    def unresolved_critical_checks(self) -> tuple[str, ...]:
        return tuple(
            c.check_id for c in self.checks if c.is_applicable_critical and c.state is CheckState.NOT_ASSESSABLE
        )

    @property
    def ordinary_explanation_supported(self) -> bool:
        return any(a.ordinary and a.state is AlternativeState.SUPPORTED for a in self.alternatives)

    def _check_attention_invariants(self) -> None:
        level = self.attention_level
        failed = self.failed_critical_checks
        unresolved = self.unresolved_critical_checks
        blocked_levels = {AttentionLevel.INCONCLUSIVE, AttentionLevel.EXPLAINED, AttentionLevel.NOT_ASSESSABLE}

        if level is AttentionLevel.EXPLAINED:
            require(self.ordinary_explanation_supported, "EXPLAINED requires a SUPPORTED ordinary explanation")

        if failed:
            require(level in blocked_levels, "a failed critical assumption check yields INCONCLUSIVE, never ATTEND or WATCH")
            if level is AttentionLevel.INCONCLUSIVE:
                require(
                    self.attention_reason is not None
                    and self.attention_reason.code is AttentionReasonCode.ASSUMPTION_CHECK_FAILED,
                    "INCONCLUSIVE after a failed critical check requires reason ASSUMPTION_CHECK_FAILED",
                )
        elif unresolved:
            require(level in blocked_levels, "an unresolved critical check yields INCONCLUSIVE, never ATTEND or WATCH")
            if level is AttentionLevel.INCONCLUSIVE:
                require(
                    self.attention_reason is not None
                    and self.attention_reason.code is AttentionReasonCode.ASSUMPTION_CHECK_UNRESOLVED,
                    "INCONCLUSIVE after an unresolved critical check requires reason ASSUMPTION_CHECK_UNRESOLVED",
                )

        if level is AttentionLevel.ATTEND:
            require(
                self.validity.state is ValidityState.VALIDATED_FOR_CONFIG,
                "ATTEND requires validity VALIDATED_FOR_CONFIG",
            )
            require(not self.ordinary_explanation_supported, "ATTEND is not allowed when an ordinary explanation is supported")

        if self.cap_reason is not None:
            require(level is AttentionLevel.WATCH, "cap_reason is only set when the validity cap produced WATCH")
            require(
                self.validity.state is not ValidityState.VALIDATED_FOR_CONFIG,
                "a validity cap cannot apply to a validated significance",
            )
