"""Method and record-schema registry (contract SCI-32, §7.1).

Two declarations are kept independent and are never inferred from each other:

1. **Schema presence**: for each field within its record schema, ``REQUIRED``,
   ``EXPECTED`` or ``OPTIONAL``. It governs only the validity of that record.
2. **Calculation input role**: for each derived method, each input is
   ``required`` or ``optional``. It governs only that calculation.

A field may be ``OPTIONAL`` in its source record yet ``required`` by a
calculation. If it is absent, that calculation returns ``U`` with
``REQUIRED_INPUT_UNAVAILABLE`` and ``blocked_by`` cause
``OMITTED_OPTIONAL_FIELD``. The source record stays valid and untouched.

Scientific methods are registered explicitly by the engine.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from ._base import ContractViolation, non_empty_str, require
from .values import (
    BlockedBy,
    BlockedCause,
    InputRole,
    NotEstablishedDetail,
    Tag,
    UnavailableReason,
    UncertaintyStatus,
    ValueClass,
    ValueObject,
    unavailable,
)


class Presence(StrEnum):
    REQUIRED = "REQUIRED"
    EXPECTED = "EXPECTED"
    OPTIONAL = "OPTIONAL"


@dataclass(frozen=True, slots=True, kw_only=True)
class FieldSpec:
    name: str
    presence: Presence
    value_class: ValueClass

    def __post_init__(self) -> None:
        require(non_empty_str(self.name), "FieldSpec.name must be non-empty")


@dataclass(frozen=True, slots=True, kw_only=True)
class RecordCheck:
    """Result of checking a record against its schema."""

    valid: bool
    missing_required: tuple[str, ...]
    not_recorded: tuple[str, ...]  # EXPECTED fields absent: record defects
    omitted_optional: tuple[str, ...]  # intentional; not defects, never tagged
    undeclared: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class RecordSchema:
    schema_id: str
    version: str
    fields: tuple[FieldSpec, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "fields", tuple(self.fields))
        names = [f.name for f in self.fields]
        require(len(names) == len(set(names)), f"duplicate field names in schema {self.schema_id}")

    def check(self, record: Mapping[str, ValueObject]) -> RecordCheck:
        declared = {f.name: f for f in self.fields}
        missing_required = tuple(n for n, f in declared.items() if f.presence is Presence.REQUIRED and n not in record)
        not_recorded = tuple(n for n, f in declared.items() if f.presence is Presence.EXPECTED and n not in record)
        omitted = tuple(n for n, f in declared.items() if f.presence is Presence.OPTIONAL and n not in record)
        undeclared = tuple(sorted(n for n in record if n not in declared))
        return RecordCheck(
            valid=not missing_required,
            missing_required=missing_required,
            not_recorded=not_recorded,
            omitted_optional=omitted,
            undeclared=undeclared,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class InputSpec:
    name: str
    role: InputRole
    uncertainty_required: bool = False
    substitution_allowed: bool = False

    def __post_init__(self) -> None:
        require(non_empty_str(self.name), "InputSpec.name must be non-empty")


@dataclass(frozen=True, slots=True, kw_only=True)
class MethodSpec:
    method_id: str
    version: str
    inputs: tuple[InputSpec, ...]
    # A method may declare optional inputs only if it defines how to proceed
    # without them (its reduced-information rule).
    has_reduced_information_rule: bool = False
    description: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "inputs", tuple(self.inputs))
        require(non_empty_str(self.method_id), "MethodSpec.method_id must be non-empty")
        require(non_empty_str(self.version), "MethodSpec.version must be non-empty")
        require(bool(self.inputs), "a method must declare at least one input")
        names = [i.name for i in self.inputs]
        require(len(names) == len(set(names)), f"duplicate input names in method {self.method_id}")
        if any(i.role is InputRole.OPTIONAL for i in self.inputs):
            require(
                self.has_reduced_information_rule,
                f"method {self.method_id} declares optional inputs without a reduced-information rule",
            )

    def input(self, name: str) -> InputSpec | None:
        return next((i for i in self.inputs if i.name == name), None)


@dataclass(frozen=True, slots=True, kw_only=True)
class InputResolution:
    """Outcome of resolving a method's inputs before computing it.

    ``computable`` is False when a required input (or its required uncertainty)
    is unavailable; ``blocked`` then holds the ``U`` value the calculation must
    return. Computational readiness says nothing about scientific validity.
    """

    computable: bool
    used: Mapping[str, ValueObject]
    optional_missing: tuple[str, ...] = ()
    substituted: tuple[str, ...] = ()
    ignored: tuple[str, ...] = ()
    blocked: ValueObject | None = None


_REASON_TO_CAUSE: dict[UnavailableReason, BlockedCause] = {
    UnavailableReason.UNAVAILABLE: BlockedCause.UNAVAILABLE,
    UnavailableReason.NOT_RECORDED: BlockedCause.NOT_RECORDED,
    UnavailableReason.NOT_APPLICABLE: BlockedCause.NOT_APPLICABLE,
    UnavailableReason.NOT_ESTABLISHED: BlockedCause.NOT_ESTABLISHED,
    UnavailableReason.NOT_ASSESSABLE: BlockedCause.NOT_ESTABLISHED,
}


def _unavailability_cause(spec: InputSpec, value: ValueObject | None) -> BlockedCause | None:
    """Return why an input is unusable, or None if it is usable."""
    if value is None:
        return BlockedCause.OMITTED_OPTIONAL_FIELD
    if value.tag is Tag.UNAVAILABLE:
        assert value.reason is not None
        return _REASON_TO_CAUSE[value.reason]
    if spec.uncertainty_required:
        assert value.uncertainty is not None
        if value.uncertainty.status is UncertaintyStatus.UNAVAILABLE:
            return BlockedCause.UNAVAILABLE
        if value.uncertainty.status is UncertaintyStatus.NOT_ESTABLISHED:
            return BlockedCause.NOT_ESTABLISHED
    return None


class MethodRegistry:
    """Registry of derived-calculation methods."""

    def __init__(self) -> None:
        self._methods: dict[str, MethodSpec] = {}

    def register(self, spec: MethodSpec) -> None:
        if spec.method_id in self._methods:
            raise ContractViolation(f"method {spec.method_id} is already registered")
        self._methods[spec.method_id] = spec

    def get(self, method_id: str) -> MethodSpec:
        try:
            return self._methods[method_id]
        except KeyError:
            raise ContractViolation(f"method {method_id} is not registered") from None

    def __contains__(self, method_id: object) -> bool:
        return method_id in self._methods

    def validate_derived(self, value: ValueObject) -> None:
        """Check that a D value names a registered method and only declared inputs with matching roles."""
        require(value.tag is Tag.DERIVED, "validate_derived expects a D value")
        assert value.method_id is not None
        spec = self.get(value.method_id)
        for ref in value.derived_from:
            declared = spec.input(ref.name)
            require(declared is not None, f"input {ref.name} is not declared by method {spec.method_id}")
            assert declared is not None
            require(
                declared.role is ref.role,
                f"input {ref.name} has role {ref.role} but method {spec.method_id} declares {declared.role}",
            )

    def resolve_inputs(
        self,
        method_id: str,
        value_class: ValueClass,
        inputs: Mapping[str, ValueObject],
        substitutions: Mapping[str, ValueObject] | None = None,
    ) -> InputResolution:
        """Apply the §7.1 propagation rules.

        ``inputs`` maps input names to values. A name absent from ``inputs``
        means the field was omitted from its source record. Names not declared
        by the method are ignored (an unrelated value never affects a result).
        ``substitutions`` are explicit ``A`` values for unavailable required
        inputs, accepted only where the method permits substitution.
        """
        spec = self.get(method_id)
        substitutions = dict(substitutions or {})
        declared = {i.name for i in spec.inputs}
        ignored = tuple(sorted(n for n in inputs if n not in declared))

        for name, sub in substitutions.items():
            in_spec = spec.input(name)
            require(in_spec is not None, f"substitution for undeclared input {name}")
            assert in_spec is not None
            require(in_spec.substitution_allowed, f"method {method_id} does not permit substitution for {name}")
            require(sub.tag is Tag.ASSUMED, f"substitution for {name} must be an explicit A value")

        used: dict[str, ValueObject] = {}
        blocked: list[BlockedBy] = []
        optional_missing: list[str] = []
        substituted: list[str] = []

        for in_spec in spec.inputs:
            value = inputs.get(in_spec.name)
            cause = _unavailability_cause(in_spec, value)
            if cause is None:
                assert value is not None
                used[in_spec.name] = value
                continue
            if in_spec.role is InputRole.OPTIONAL:
                optional_missing.append(in_spec.name)
                continue
            if in_spec.name in substitutions:
                used[in_spec.name] = substitutions[in_spec.name]
                substituted.append(in_spec.name)
                continue
            blocked.append(BlockedBy(ref=in_spec.name, cause=cause))

        if blocked:
            return InputResolution(
                computable=False,
                used=used,
                optional_missing=tuple(optional_missing),
                substituted=tuple(substituted),
                ignored=ignored,
                blocked=unavailable(
                    value_class=value_class,
                    reason=UnavailableReason.NOT_ESTABLISHED,
                    detail=NotEstablishedDetail.REQUIRED_INPUT_UNAVAILABLE,
                    blocked_by=tuple(blocked),
                ),
            )
        return InputResolution(
            computable=True,
            used=used,
            optional_missing=tuple(optional_missing),
            substituted=tuple(substituted),
            ignored=ignored,
        )


@dataclass(slots=True)
class SchemaRegistry:
    """Registry of record schemas, keyed by schema id."""

    _schemas: dict[str, RecordSchema] = field(default_factory=dict)

    def register(self, schema: RecordSchema) -> None:
        if schema.schema_id in self._schemas:
            raise ContractViolation(f"schema {schema.schema_id} is already registered")
        self._schemas[schema.schema_id] = schema

    def get(self, schema_id: str) -> RecordSchema:
        try:
            return self._schemas[schema_id]
        except KeyError:
            raise ContractViolation(f"schema {schema_id} is not registered") from None
