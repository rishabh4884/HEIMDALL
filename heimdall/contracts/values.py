"""Tagged scientific values (contract §7.1).

Every scientific value is a ``ValueObject`` carrying exactly one tag:

- ``M`` measured or source-reported, with a mandatory origin
- ``D`` derived by a registered method from declared inputs
- ``A`` assumed, with a stated source or rationale
- ``U`` unavailable or not established, with a reason

A value whose origin or derivation cannot be established is ``U`` with reason
``NOT_ESTABLISHED``. It is never tagged ``D`` or ``M`` by default. Zero never
stands for missing.

Registration of ``D`` methods is checked by ``MethodRegistry`` (registry.py),
because a single value cannot know which methods exist.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from ._base import non_empty_str, require


class Tag(StrEnum):
    MEASURED = "M"
    DERIVED = "D"
    ASSUMED = "A"
    UNAVAILABLE = "U"


ACCESSIBLE_TAG_NAMES: dict[Tag, str] = {
    Tag.MEASURED: "Measured or source-reported",
    Tag.DERIVED: "Derived",
    Tag.ASSUMED: "Assumed",
    Tag.UNAVAILABLE: "Unavailable",
}


class Origin(StrEnum):
    SOURCE_REPORTED = "SOURCE_REPORTED"
    DIRECT_MEASUREMENT = "DIRECT_MEASUREMENT"


class ValueClass(StrEnum):
    QUANTITY = "QUANTITY"
    CATEGORICAL = "CATEGORICAL"
    IDENTIFIER = "IDENTIFIER"
    TIME = "TIME"
    COUNT = "COUNT"


class UncertaintyStatus(StrEnum):
    PROVIDED = "PROVIDED"
    UNAVAILABLE = "UNAVAILABLE"
    NOT_ESTABLISHED = "NOT_ESTABLISHED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class UnavailableReason(StrEnum):
    UNAVAILABLE = "UNAVAILABLE"
    NOT_ESTABLISHED = "NOT_ESTABLISHED"
    NOT_ASSESSABLE = "NOT_ASSESSABLE"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    NOT_RECORDED = "NOT_RECORDED"


class NotEstablishedDetail(StrEnum):
    ORIGIN_NOT_ESTABLISHED = "ORIGIN_NOT_ESTABLISHED"
    DERIVATION_NOT_ESTABLISHED = "DERIVATION_NOT_ESTABLISHED"
    CALCULATION_FAILED = "CALCULATION_FAILED"
    REQUIRED_INPUT_UNAVAILABLE = "REQUIRED_INPUT_UNAVAILABLE"


class BlockedCause(StrEnum):
    OMITTED_OPTIONAL_FIELD = "OMITTED_OPTIONAL_FIELD"
    NOT_RECORDED = "NOT_RECORDED"
    UNAVAILABLE = "UNAVAILABLE"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    NOT_ESTABLISHED = "NOT_ESTABLISHED"


class InputRole(StrEnum):
    REQUIRED = "required"
    OPTIONAL = "optional"


# Uncertainty statuses permitted for each value class.
ALLOWED_UNCERTAINTY: dict[ValueClass, frozenset[UncertaintyStatus]] = {
    ValueClass.QUANTITY: frozenset(
        {UncertaintyStatus.PROVIDED, UncertaintyStatus.UNAVAILABLE, UncertaintyStatus.NOT_ESTABLISHED}
    ),
    ValueClass.CATEGORICAL: frozenset({UncertaintyStatus.NOT_APPLICABLE}),
    ValueClass.IDENTIFIER: frozenset({UncertaintyStatus.NOT_APPLICABLE}),
    ValueClass.TIME: frozenset({UncertaintyStatus.PROVIDED, UncertaintyStatus.NOT_APPLICABLE}),
    ValueClass.COUNT: frozenset({UncertaintyStatus.NOT_APPLICABLE}),
}

Scalar = int | float | str


def _is_real_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


@dataclass(frozen=True, slots=True, kw_only=True)
class Uncertainty:
    """Uncertainty attached to a value. Its status is separate from the tag."""

    status: UncertaintyStatus
    sigma: float | None = None
    # What ``sigma`` represents, for example "1-sigma" or "recorded precision".
    kind: str | None = None

    def __post_init__(self) -> None:
        if self.status is UncertaintyStatus.PROVIDED:
            require(
                _is_real_number(self.sigma) and self.sigma >= 0,
                "PROVIDED uncertainty requires a finite, non-negative sigma",
            )
            require(non_empty_str(self.kind), "PROVIDED uncertainty must state what sigma represents (kind)")
        else:
            require(self.sigma is None, f"uncertainty with status {self.status} must not carry a sigma")

    @classmethod
    def provided(cls, sigma: float, kind: str) -> Uncertainty:
        return cls(status=UncertaintyStatus.PROVIDED, sigma=sigma, kind=kind)

    @classmethod
    def unavailable(cls) -> Uncertainty:
        return cls(status=UncertaintyStatus.UNAVAILABLE)

    @classmethod
    def not_established(cls) -> Uncertainty:
        return cls(status=UncertaintyStatus.NOT_ESTABLISHED)

    @classmethod
    def not_applicable(cls) -> Uncertainty:
        return cls(status=UncertaintyStatus.NOT_APPLICABLE)


@dataclass(frozen=True, slots=True, kw_only=True)
class OriginInfo:
    """Where an ``M`` value came from."""

    kind: Origin
    source: str | None = None
    version_or_epoch: str | None = None
    procedure_id: str | None = None

    def __post_init__(self) -> None:
        if self.kind is Origin.SOURCE_REPORTED:
            require(non_empty_str(self.source), "SOURCE_REPORTED origin requires a named source")
        else:
            require(non_empty_str(self.procedure_id), "DIRECT_MEASUREMENT origin requires a procedure_id")


@dataclass(frozen=True, slots=True, kw_only=True)
class InputRef:
    """One input of a derived value: the registry input name, the value used and its role."""

    name: str
    ref: str
    role: InputRole

    def __post_init__(self) -> None:
        require(non_empty_str(self.name), "InputRef.name must be non-empty")
        require(non_empty_str(self.ref), "InputRef.ref must be non-empty")


@dataclass(frozen=True, slots=True, kw_only=True)
class BlockedBy:
    """A required input that made a derived value unavailable, and why."""

    ref: str
    cause: BlockedCause

    def __post_init__(self) -> None:
        require(non_empty_str(self.ref), "BlockedBy.ref must be non-empty")


@dataclass(frozen=True, slots=True, kw_only=True)
class Assumption:
    source: str | None = None
    rationale: str | None = None

    def __post_init__(self) -> None:
        require(
            non_empty_str(self.source) or non_empty_str(self.rationale),
            "an assumption requires a source or a rationale",
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ValueObject:
    tag: Tag
    value_class: ValueClass
    value: Scalar | None = None
    uncertainty: Uncertainty | None = None
    unit: str | None = None
    origin: OriginInfo | None = None
    derived_from: tuple[InputRef, ...] = ()
    method_id: str | None = None
    assumption: Assumption | None = None
    reason: UnavailableReason | None = None
    detail: NotEstablishedDetail | None = None
    blocked_by: tuple[BlockedBy, ...] = ()
    optional_inputs_missing: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("derived_from", "blocked_by", "optional_inputs_missing"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        if self.tag is Tag.UNAVAILABLE:
            self._check_unavailable()
        else:
            self._check_available()

    # -- rules ---------------------------------------------------------------

    def _check_unavailable(self) -> None:
        require(self.value is None, "a U value must have value None")
        require(self.reason is not None, "a U value requires a reason")
        require(self.uncertainty is None, "a U value carries no uncertainty")
        require(self.origin is None, "a U value carries no origin")
        require(self.method_id is None and not self.derived_from, "a U value carries no derivation")
        require(self.assumption is None, "a U value carries no assumption")
        require(not self.optional_inputs_missing, "optional_inputs_missing applies only to D values")
        if self.reason is UnavailableReason.NOT_ESTABLISHED:
            require(self.detail is not None, "reason NOT_ESTABLISHED requires a detail code")
        else:
            require(self.detail is None, "a detail code is only allowed with reason NOT_ESTABLISHED")
        if self.detail is NotEstablishedDetail.REQUIRED_INPUT_UNAVAILABLE:
            require(bool(self.blocked_by), "REQUIRED_INPUT_UNAVAILABLE requires blocked_by entries")
        else:
            require(not self.blocked_by, "blocked_by is only allowed with REQUIRED_INPUT_UNAVAILABLE")

    def _check_available(self) -> None:
        require(self.value is not None, f"a {self.tag} value must not be None; use a U value instead")
        require(self.reason is None and self.detail is None, "reason/detail are only allowed on U values")
        require(not self.blocked_by, "blocked_by is only allowed on U values")
        require(self.uncertainty is not None, "every non-U value must declare an uncertainty status")
        allowed = ALLOWED_UNCERTAINTY[self.value_class]
        require(
            self.uncertainty.status in allowed,
            f"uncertainty status {self.uncertainty.status} is not allowed for class {self.value_class}",
        )
        self._check_value_type()

        if self.tag is Tag.MEASURED:
            require(self.origin is not None, "an M value requires an origin")
            require(self.method_id is None and not self.derived_from, "an M value carries no derivation")
            require(self.assumption is None, "an M value carries no assumption")
            require(not self.optional_inputs_missing, "optional_inputs_missing applies only to D values")
        elif self.tag is Tag.DERIVED:
            require(non_empty_str(self.method_id), "a D value requires a method_id")
            require(bool(self.derived_from), "a D value requires derived_from inputs")
            require(self.origin is None, "a D value carries no origin; it is computed")
            require(self.assumption is None, "a D value lists assumptions as A inputs, not as its own assumption")
            names = [i.name for i in self.derived_from]
            require(len(names) == len(set(names)), "derived_from input names must be unique")
        elif self.tag is Tag.ASSUMED:
            require(self.assumption is not None, "an A value requires a source or rationale")
            require(self.origin is None, "an A value carries no origin")
            require(self.method_id is None and not self.derived_from, "an A value carries no derivation")
            require(not self.optional_inputs_missing, "optional_inputs_missing applies only to D values")

    def _check_value_type(self) -> None:
        v = self.value
        vc = self.value_class
        if vc is ValueClass.QUANTITY:
            require(_is_real_number(v), "a QUANTITY value must be a finite number")
        elif vc is ValueClass.COUNT:
            require(isinstance(v, int) and not isinstance(v, bool) and v >= 0, "a COUNT must be a non-negative int")
        elif vc is ValueClass.IDENTIFIER:
            require(non_empty_str(v), "an IDENTIFIER must be a non-empty string")
        elif vc is ValueClass.CATEGORICAL:
            require(non_empty_str(v), "a CATEGORICAL value must be a non-empty string")
        elif vc is ValueClass.TIME:
            # Representation is provisional until real time metadata is inspected.
            require(non_empty_str(v) or _is_real_number(v), "a TIME value must be a string or a finite number")

    # -- helpers -------------------------------------------------------------

    @property
    def is_available(self) -> bool:
        return self.tag is not Tag.UNAVAILABLE

    @property
    def accessible_name(self) -> str:
        return ACCESSIBLE_TAG_NAMES[self.tag]


def _default_uncertainty(value_class: ValueClass, uncertainty: Uncertainty | None) -> Uncertainty:
    if uncertainty is not None:
        return uncertainty
    require(
        value_class is not ValueClass.QUANTITY,
        "a QUANTITY must state its uncertainty explicitly (provided, unavailable or not established)",
    )
    return Uncertainty.not_applicable()


def measured(
    value: Scalar,
    *,
    value_class: ValueClass,
    origin: OriginInfo,
    uncertainty: Uncertainty | None = None,
    unit: str | None = None,
) -> ValueObject:
    return ValueObject(
        tag=Tag.MEASURED,
        value_class=value_class,
        value=value,
        uncertainty=_default_uncertainty(value_class, uncertainty),
        unit=unit,
        origin=origin,
    )


def derived(
    value: Scalar,
    *,
    value_class: ValueClass,
    method_id: str,
    derived_from: tuple[InputRef, ...] | list[InputRef],
    uncertainty: Uncertainty | None = None,
    unit: str | None = None,
    optional_inputs_missing: tuple[str, ...] = (),
) -> ValueObject:
    return ValueObject(
        tag=Tag.DERIVED,
        value_class=value_class,
        value=value,
        uncertainty=_default_uncertainty(value_class, uncertainty),
        unit=unit,
        method_id=method_id,
        derived_from=tuple(derived_from),
        optional_inputs_missing=optional_inputs_missing,
    )


def assumed(
    value: Scalar,
    *,
    value_class: ValueClass,
    assumption: Assumption,
    uncertainty: Uncertainty | None = None,
    unit: str | None = None,
) -> ValueObject:
    return ValueObject(
        tag=Tag.ASSUMED,
        value_class=value_class,
        value=value,
        uncertainty=_default_uncertainty(value_class, uncertainty),
        unit=unit,
        assumption=assumption,
    )


def unavailable(
    *,
    value_class: ValueClass,
    reason: UnavailableReason,
    detail: NotEstablishedDetail | None = None,
    blocked_by: tuple[BlockedBy, ...] = (),
    unit: str | None = None,
) -> ValueObject:
    return ValueObject(
        tag=Tag.UNAVAILABLE,
        value_class=value_class,
        reason=reason,
        detail=detail,
        blocked_by=blocked_by,
        unit=unit,
    )
