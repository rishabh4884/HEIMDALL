"""Scientific validity, checks, alternatives, evidence and statements.

This module encodes states and structural rules from contract §10.2 and §10.5.
It does not decide scientific outcomes; validation procedures supply those
outcomes. It guarantees that records cannot express an unsupported state,
for example a validated significance without a scope cell, or a "ruled out"
alternative without evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ._base import non_empty_str, require


class ValidityState(StrEnum):
    NOT_ASSESSED = "NOT_ASSESSED"
    UNVALIDATED = "UNVALIDATED"
    VALIDATED_FOR_CONFIG = "VALIDATED_FOR_CONFIG"
    MISCALIBRATED = "MISCALIBRATED"
    NOT_ASSESSABLE = "NOT_ASSESSABLE"


class ValidityReason(StrEnum):
    ASSUMPTION_CHECK_FAILED = "ASSUMPTION_CHECK_FAILED"
    ASSUMPTION_CHECK_UNRESOLVED = "ASSUMPTION_CHECK_UNRESOLVED"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    NO_CONTROL_RUN = "NO_CONTROL_RUN"
    INSUFFICIENT_SAMPLE = "INSUFFICIENT_SAMPLE"
    CONTROLS_NOT_REPRESENTATIVE = "CONTROLS_NOT_REPRESENTATIVE"
    CRITERIA_FAILED = "CRITERIA_FAILED"


# Candidate-level reasons first, then cell-level reasons (contract §10.2).
REASON_PRECEDENCE: tuple[ValidityReason, ...] = (
    ValidityReason.ASSUMPTION_CHECK_FAILED,
    ValidityReason.ASSUMPTION_CHECK_UNRESOLVED,
    ValidityReason.OUT_OF_SCOPE,
    ValidityReason.NO_CONTROL_RUN,
    ValidityReason.INSUFFICIENT_SAMPLE,
    ValidityReason.CONTROLS_NOT_REPRESENTATIVE,
)

UNVALIDATED_REASONS = frozenset(REASON_PRECEDENCE)


def order_reasons(reasons: tuple[ValidityReason, ...] | list[ValidityReason]) -> tuple[ValidityReason, ...]:
    """Deduplicate and order reasons by precedence; the first is primary."""
    unique = set(reasons)
    rank = {r: i for i, r in enumerate(REASON_PRECEDENCE)}
    return tuple(sorted(unique, key=lambda r: rank.get(r, len(rank))))


@dataclass(frozen=True, slots=True, kw_only=True)
class Validity:
    """Validity of a significance (or sensitivity limit) for one scope cell.

    ``VALIDATED_FOR_CONFIG`` is a property of a validated scope cell's error
    model. It says nothing about a candidate's own data quality, association,
    assumptions or attention conditions.
    """

    state: ValidityState
    reasons: tuple[ValidityReason, ...] = ()
    scope_cell: str | None = None
    validation_run_ref: str | None = None
    note: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "reasons", tuple(self.reasons))
        require(len(self.reasons) == len(set(self.reasons)), "validity reasons must be unique")
        if self.state is ValidityState.VALIDATED_FOR_CONFIG:
            require(non_empty_str(self.scope_cell), "VALIDATED_FOR_CONFIG requires a scope_cell")
            require(non_empty_str(self.validation_run_ref), "VALIDATED_FOR_CONFIG requires a validation_run_ref")
            require(not self.reasons, "VALIDATED_FOR_CONFIG carries no failure reasons")
        elif self.state is ValidityState.UNVALIDATED:
            require(bool(self.reasons), "UNVALIDATED requires at least one reason")
            require(
                all(r in UNVALIDATED_REASONS for r in self.reasons),
                "UNVALIDATED reasons must be candidate-level or cell-level reasons, not CRITERIA_FAILED",
            )
            require(self.reasons == order_reasons(self.reasons), "UNVALIDATED reasons must be in precedence order")
        elif self.state is ValidityState.MISCALIBRATED:
            require(self.reasons == (ValidityReason.CRITERIA_FAILED,), "MISCALIBRATED requires CRITERIA_FAILED only")
            require(non_empty_str(self.scope_cell), "MISCALIBRATED requires the scope_cell that failed")
        else:
            require(not self.reasons, f"{self.state} carries no reason codes")

    @property
    def primary_reason(self) -> ValidityReason | None:
        return self.reasons[0] if self.reasons else None

    @classmethod
    def not_assessed(cls) -> Validity:
        return cls(state=ValidityState.NOT_ASSESSED)

    @classmethod
    def unvalidated(cls, *reasons: ValidityReason, scope_cell: str | None = None) -> Validity:
        return cls(state=ValidityState.UNVALIDATED, reasons=order_reasons(list(reasons)), scope_cell=scope_cell)


class CheckState(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    NOT_ASSESSABLE = "NOT_ASSESSABLE"


@dataclass(frozen=True, slots=True, kw_only=True)
class Applicability:
    """Whether a check applies, decided by a rule in the frozen config.

    A check may not be declared inapplicable after the fact because evaluation
    was difficult, data were missing, or it would fail.
    """

    applies: bool
    rule_id: str | None = None

    def __post_init__(self) -> None:
        if not self.applies:
            require(non_empty_str(self.rule_id), "an inapplicable check must cite its applicability rule_id")


class EvidenceKind(StrEnum):
    OBSERVATION = "OBSERVATION"
    MEASUREMENT = "MEASUREMENT"
    CHECK = "CHECK"
    VALIDATION_RUN = "VALIDATION_RUN"
    EXTERNAL_REFERENCE = "EXTERNAL_REFERENCE"
    RECORD = "RECORD"


@dataclass(frozen=True, slots=True, kw_only=True)
class EvidenceRef:
    kind: EvidenceKind
    ref_id: str

    def __post_init__(self) -> None:
        require(non_empty_str(self.ref_id), "EvidenceRef.ref_id must be non-empty")


@dataclass(frozen=True, slots=True, kw_only=True)
class Check:
    check_id: str
    state: CheckState
    applicability: Applicability
    critical: bool = False
    reason: str | None = None
    evidence: tuple[EvidenceRef, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", tuple(self.evidence))
        require(non_empty_str(self.check_id), "Check.check_id must be non-empty")
        if self.state is CheckState.NOT_APPLICABLE:
            require(not self.applicability.applies, "NOT_APPLICABLE requires an applicability rule saying it does not apply")
        else:
            require(self.applicability.applies, f"a check that does not apply must be NOT_APPLICABLE, not {self.state}")
        if self.state is not CheckState.PASSED:
            require(non_empty_str(self.reason), f"a {self.state} check requires a reason")

    @property
    def is_applicable_critical(self) -> bool:
        return self.critical and self.state is not CheckState.NOT_APPLICABLE


class AlternativeState(StrEnum):
    SUPPORTED = "SUPPORTED"
    RULED_OUT = "RULED_OUT"
    PLAUSIBLE_UNTESTED = "PLAUSIBLE_UNTESTED"
    NOT_ASSESSABLE = "NOT_ASSESSABLE"


@dataclass(frozen=True, slots=True, kw_only=True)
class Alternative:
    """A competing explanation and its state (SCI-14)."""

    alt_id: str
    description: str
    state: AlternativeState
    # True for an ordinary explanation (artefact, known object, calibration...).
    ordinary: bool
    evidence: tuple[EvidenceRef, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", tuple(self.evidence))
        require(non_empty_str(self.alt_id), "Alternative.alt_id must be non-empty")
        require(non_empty_str(self.description), "Alternative.description must be non-empty")
        if self.state in (AlternativeState.RULED_OUT, AlternativeState.SUPPORTED):
            require(bool(self.evidence), f"an alternative marked {self.state} requires cited evidence")


class StatementKind(StrEnum):
    HYPOTHESIS = "HYPOTHESIS"
    CONCLUSION = "CONCLUSION"


@dataclass(frozen=True, slots=True, kw_only=True)
class Statement:
    """An interpretation, kept separate from tagged values.

    Measured facts are ``M`` values and derived results are ``D`` values; a
    statement is a hypothesis or a conclusion about them. A conclusion must
    cite evidence. Neither form may claim a discovery the evidence does not
    support; wording rules are enforced at the presentation layer later.
    """

    kind: StatementKind
    text: str
    evidence: tuple[EvidenceRef, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", tuple(self.evidence))
        require(non_empty_str(self.text), "Statement.text must be non-empty")
        if self.kind is StatementKind.CONCLUSION:
            require(bool(self.evidence), "a CONCLUSION requires cited evidence")
