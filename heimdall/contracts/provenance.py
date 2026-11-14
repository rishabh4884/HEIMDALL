"""Provenance and source metadata (contract §7.2, EVD-04).

``DataOrigin.SYNTHETIC_TEST_FIXTURE`` exists so that software-test fixtures
can never be mistaken for real observations. Anything carrying it is not
astronomical data and must never be presented as such.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from ._base import non_empty_str, require


class DataOrigin(StrEnum):
    LIVE = "LIVE"
    CACHED = "CACHED"
    PRECOMPUTED = "PRECOMPUTED"
    SYNTHETIC_TEST_FIXTURE = "SYNTHETIC_TEST_FIXTURE"


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceProduct:
    """Identity of one product obtained from a data source.

    Fields are generic on purpose. Source-specific identifiers (for example
    observation or detector naming) are added only after inspecting real
    products.
    """

    source_id: str
    product_id: str
    release: str | None = None
    product_version: str | None = None
    checksum: str | None = None
    checksum_algorithm: str | None = None
    # Must never contain credentials or signed tokens.
    access_reference: str | None = None

    def __post_init__(self) -> None:
        require(non_empty_str(self.source_id), "SourceProduct.source_id must be non-empty")
        require(non_empty_str(self.product_id), "SourceProduct.product_id must be non-empty")
        require(
            (self.checksum is None) == (self.checksum_algorithm is None),
            "checksum and checksum_algorithm must be given together",
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class Provenance:
    origin: DataOrigin
    retrieved_at: datetime
    software_version: str
    products: tuple[SourceProduct, ...] = ()
    config_hash: str | None = None
    notes: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "products", tuple(self.products))
        require(
            self.retrieved_at.tzinfo is not None and self.retrieved_at.utcoffset() is not None,
            "retrieved_at must be timezone-aware",
        )
        require(non_empty_str(self.software_version), "software_version must be non-empty")

    @property
    def is_synthetic(self) -> bool:
        return self.origin is DataOrigin.SYNTHETIC_TEST_FIXTURE
