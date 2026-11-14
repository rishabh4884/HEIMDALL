"""Observations and measurements (PROVISIONAL).

These records are deliberately generic. Source-specific fields (detectors,
wavelength metadata, time representation, footprints, flag vocabularies) are
kept in ``metadata`` as tagged values until the real data products have been
inspected. Promoting a field to a first-class attribute is a versioned schema
change, not an assumption made in advance.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from ._base import non_empty_str, require
from .provenance import Provenance
from .values import ValueObject


def _freeze_value_map(name: str, values: Mapping[str, ValueObject]) -> Mapping[str, ValueObject]:
    for key, value in values.items():
        require(non_empty_str(key), f"{name} keys must be non-empty strings")
        require(isinstance(value, ValueObject), f"{name}[{key!r}] must be a ValueObject")
    return MappingProxyType(dict(values))


@dataclass(frozen=True, slots=True, kw_only=True)
class Observation:
    obs_id: str
    source_id: str
    provenance: Provenance
    metadata: Mapping[str, ValueObject] = field(default_factory=dict)
    # Quality flag names as reported by the source; vocabulary is provisional.
    quality_flags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require(non_empty_str(self.obs_id), "Observation.obs_id must be non-empty")
        require(non_empty_str(self.source_id), "Observation.source_id must be non-empty")
        object.__setattr__(self, "metadata", _freeze_value_map("metadata", self.metadata))
        object.__setattr__(self, "quality_flags", tuple(self.quality_flags))

    @property
    def is_synthetic(self) -> bool:
        return self.provenance.is_synthetic


@dataclass(frozen=True, slots=True, kw_only=True)
class Measurement:
    """A named, tagged value about a subject (an observation, detection or series)."""

    measurement_id: str
    name: str
    subject_ref: str
    value: ValueObject

    def __post_init__(self) -> None:
        require(non_empty_str(self.measurement_id), "Measurement.measurement_id must be non-empty")
        require(non_empty_str(self.name), "Measurement.name must be non-empty")
        require(non_empty_str(self.subject_ref), "Measurement.subject_ref must be non-empty")
        require(isinstance(self.value, ValueObject), "Measurement.value must be a ValueObject")
