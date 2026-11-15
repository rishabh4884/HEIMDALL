"""The data-source boundary.

Every real or test data source implements ``DataSource``. Source-specific
details (endpoints, authentication, product formats) stay inside the adapter;
the scientific engine and the UI see only the types defined here and in
``heimdall.contracts``.

Contract for implementations:
- A failed operation raises ``HeimdallError`` with a specific code:
  ``CREDENTIALS_MISSING``, ``AUTHENTICATION_FAILED``, ``ACCESS_DENIED``,
  ``ARCHIVE_UNREACHABLE``, ``ARCHIVE_TIMEOUT``, ``RETRIEVAL_FAILED``,
  ``RELEASE_UNAVAILABLE`` or ``DATA_MALFORMED``.
- A successful query that finds nothing returns a result with outcome
  ``NO_OBSERVATIONS``. It never raises, and a failure never returns it.
- Partial success returns outcome ``PARTIAL`` with the failed items listed.
- Credentials are read only from ``Settings`` and are never logged, stored in
  records, or placed in error messages or access references.
- Capabilities are reported as unverified until confirmed against the real
  source.

The request and result shapes are PROVISIONAL. Query filters and product
descriptions will be revised after the real products are inspected.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType

from ..contracts._base import non_empty_str, require
from ..contracts.observations import Observation
from ..contracts.outcomes import FailedItem, ResultOutcome
from ..contracts.provenance import Provenance, SourceProduct


@dataclass(frozen=True, slots=True, kw_only=True)
class SkyRegion:
    """A circular region on the sky, in degrees. Frame handling is provisional."""

    ra_deg: float
    dec_deg: float
    radius_deg: float
    frame: str = "ICRS"

    def __post_init__(self) -> None:
        for name in ("ra_deg", "dec_deg", "radius_deg"):
            value = getattr(self, name)
            require(
                isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value),
                f"SkyRegion.{name} must be a finite number",
            )
        require(0.0 <= self.ra_deg < 360.0, "SkyRegion.ra_deg must be in [0, 360)")
        require(-90.0 <= self.dec_deg <= 90.0, "SkyRegion.dec_deg must be in [-90, 90]")
        require(0.0 < self.radius_deg <= 180.0, "SkyRegion.radius_deg must be in (0, 180]")
        require(non_empty_str(self.frame), "SkyRegion.frame must be non-empty")


@dataclass(frozen=True, slots=True, kw_only=True)
class DiscoveryQuery:
    region: SkyRegion
    release: str | None = None
    # Source-specific filters, documented by each adapter. Provisional.
    filters: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "filters", MappingProxyType(dict(self.filters)))


@dataclass(frozen=True, slots=True, kw_only=True)
class DiscoveryResult:
    outcome: ResultOutcome
    observations: tuple[Observation, ...]
    provenance: Provenance
    failed_items: tuple[FailedItem, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "observations", tuple(self.observations))
        object.__setattr__(self, "failed_items", tuple(self.failed_items))
        if self.outcome is ResultOutcome.NO_OBSERVATIONS:
            require(not self.observations and not self.failed_items, "NO_OBSERVATIONS means a clean, empty success")
        elif self.outcome is ResultOutcome.OK:
            require(bool(self.observations), "OK discovery requires at least one observation")
            require(not self.failed_items, "OK discovery has no failed items; use PARTIAL")
        elif self.outcome is ResultOutcome.PARTIAL:
            require(bool(self.failed_items), "PARTIAL requires the failed items to be listed")
        else:
            require(False, f"discovery outcome must be OK, NO_OBSERVATIONS or PARTIAL, not {self.outcome}")


@dataclass(frozen=True, slots=True, kw_only=True)
class RetrievalRequest:
    obs_id: str
    region: SkyRegion | None = None
    options: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require(non_empty_str(self.obs_id), "RetrievalRequest.obs_id must be non-empty")
        object.__setattr__(self, "options", MappingProxyType(dict(self.options)))


@dataclass(frozen=True, slots=True, kw_only=True)
class RetrievedProduct:
    """A retrieved product, kept opaque until normalization parses it."""

    product: SourceProduct
    location: str  # local path of the stored bytes
    media_type: str | None = None

    def __post_init__(self) -> None:
        require(non_empty_str(self.location), "RetrievedProduct.location must be non-empty")


@dataclass(frozen=True, slots=True, kw_only=True)
class RetrievalResult:
    outcome: ResultOutcome
    obs_id: str
    products: tuple[RetrievedProduct, ...]
    provenance: Provenance
    failed_items: tuple[FailedItem, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "products", tuple(self.products))
        object.__setattr__(self, "failed_items", tuple(self.failed_items))
        if self.outcome is ResultOutcome.OK:
            require(bool(self.products) and not self.failed_items, "OK retrieval has products and no failed items")
        elif self.outcome is ResultOutcome.PARTIAL:
            require(bool(self.failed_items), "PARTIAL requires the failed items to be listed")
        elif self.outcome is ResultOutcome.DATA_UNAVAILABLE:
            require(not self.products, "DATA_UNAVAILABLE retrieval carries no products")
        else:
            require(False, f"retrieval outcome must be OK, PARTIAL or DATA_UNAVAILABLE, not {self.outcome}")


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceDescription:
    source_id: str
    display_name: str
    requires_credentials: bool
    # False until each capability has been exercised against the real source.
    capabilities_verified: bool = False
    notes: str = ""

    def __post_init__(self) -> None:
        require(non_empty_str(self.source_id), "SourceDescription.source_id must be non-empty")
        require(non_empty_str(self.display_name), "SourceDescription.display_name must be non-empty")


@dataclass(frozen=True, slots=True, kw_only=True)
class AccessReport:
    """Returned by a successful access check. Failures raise ``HeimdallError``."""

    source_id: str
    checked_at: datetime
    detail: str = ""

    def __post_init__(self) -> None:
        require(self.checked_at.tzinfo is not None, "AccessReport.checked_at must be timezone-aware")


class DataSource(ABC):
    """Interface implemented by every data-source adapter."""

    @property
    @abstractmethod
    def source_id(self) -> str: ...

    @abstractmethod
    def describe(self) -> SourceDescription: ...

    @abstractmethod
    def check_access(self) -> AccessReport:
        """Verify configuration, credentials and reachability, without retrieving data.

        Raises only access codes: CREDENTIALS_MISSING, AUTHENTICATION_FAILED, ACCESS_DENIED, ARCHIVE_UNREACHABLE, ARCHIVE_TIMEOUT. An HTTP
        5xx (from the archive or a proxy) is ARCHIVE_UNREACHABLE with the status in the error detail. RETRIEVAL_FAILED belongs to item-level
        discovery/retrieval failures, not to an access check."""

    @abstractmethod
    def discover(self, query: DiscoveryQuery) -> DiscoveryResult:
        """List observations covering a region. Discovery is not detection."""

    @abstractmethod
    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        """Retrieve the products for one observation, keeping them opaque."""
