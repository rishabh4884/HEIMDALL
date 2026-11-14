"""SYNTHETIC TEST FIXTURE: a fake data source for exercising the interface only.

It contains no astronomical values. Every record it produces carries
``DataOrigin.SYNTHETIC_TEST_FIXTURE`` and identifiers prefixed ``SYNTHETIC-``.
It exists to test the data-source boundary and its failure modes before the
real source is connected. It must never be registered in the application's
default registry or presented as real data.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from heimdall import __version__
from heimdall.config import Settings
from heimdall.contracts import (
    DataOrigin,
    ErrorCode,
    ErrorInfo,
    FailedItem,
    HeimdallError,
    Observation,
    Provenance,
    ResultOutcome,
    SourceProduct,
)
from heimdall.data_access import (
    AccessReport,
    DataSource,
    DiscoveryQuery,
    DiscoveryResult,
    RetrievalRequest,
    RetrievalResult,
    RetrievedProduct,
    SourceDescription,
)

SOURCE_ID = "synthetic-fixture"


class Mode(StrEnum):
    OK = "ok"
    EMPTY = "empty"
    PARTIAL = "partial"
    UNREACHABLE = "unreachable"
    TIMEOUT = "timeout"
    AUTH_FAILED = "auth_failed"
    ACCESS_DENIED = "access_denied"
    MALFORMED = "malformed"


_MODE_ERRORS = {
    Mode.UNREACHABLE: ErrorCode.ARCHIVE_UNREACHABLE,
    Mode.TIMEOUT: ErrorCode.ARCHIVE_TIMEOUT,
    Mode.AUTH_FAILED: ErrorCode.AUTHENTICATION_FAILED,
    Mode.ACCESS_DENIED: ErrorCode.ACCESS_DENIED,
    Mode.MALFORMED: ErrorCode.DATA_MALFORMED,
}


class SyntheticFixtureSource(DataSource):
    def __init__(self, settings: Settings, mode: Mode = Mode.OK) -> None:
        self._settings = settings
        self._mode = mode

    @property
    def source_id(self) -> str:
        return SOURCE_ID

    def describe(self) -> SourceDescription:
        return SourceDescription(
            source_id=SOURCE_ID,
            display_name="Synthetic test fixture (not real data)",
            requires_credentials=True,
            capabilities_verified=False,
            notes="Software-test fixture only.",
        )

    def _provenance(self) -> Provenance:
        return Provenance(
            origin=DataOrigin.SYNTHETIC_TEST_FIXTURE,
            retrieved_at=datetime.now(UTC),
            software_version=__version__,
            products=(SourceProduct(source_id=SOURCE_ID, product_id="SYNTHETIC-PRODUCT-001"),),
            notes="synthetic test fixture",
        )

    def _require_credentials(self) -> None:
        if self._settings.source_credential is None:
            raise HeimdallError.of(ErrorCode.CREDENTIALS_MISSING, "no credential configured for the source")

    def _raise_for_mode(self) -> None:
        code = _MODE_ERRORS.get(self._mode)
        if code is not None:
            raise HeimdallError.of(code, f"synthetic fixture simulating {code}")

    def check_access(self) -> AccessReport:
        self._require_credentials()
        if self._mode in (Mode.UNREACHABLE, Mode.TIMEOUT, Mode.AUTH_FAILED, Mode.ACCESS_DENIED):
            self._raise_for_mode()
        return AccessReport(source_id=SOURCE_ID, checked_at=datetime.now(UTC), detail="synthetic fixture")

    def discover(self, query: DiscoveryQuery) -> DiscoveryResult:
        self._require_credentials()
        self._raise_for_mode()
        provenance = self._provenance()
        if self._mode is Mode.EMPTY:
            return DiscoveryResult(outcome=ResultOutcome.NO_OBSERVATIONS, observations=(), provenance=provenance)
        observation = Observation(obs_id="SYNTHETIC-OBS-001", source_id=SOURCE_ID, provenance=provenance)
        if self._mode is Mode.PARTIAL:
            failed = FailedItem(
                ref="SYNTHETIC-OBS-002",
                error=ErrorInfo(code=ErrorCode.RETRIEVAL_FAILED, message="synthetic partial failure"),
            )
            return DiscoveryResult(
                outcome=ResultOutcome.PARTIAL, observations=(observation,), provenance=provenance, failed_items=(failed,)
            )
        return DiscoveryResult(outcome=ResultOutcome.OK, observations=(observation,), provenance=provenance)

    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        self._require_credentials()
        self._raise_for_mode()
        provenance = self._provenance()
        if self._mode is Mode.EMPTY:
            return RetrievalResult(
                outcome=ResultOutcome.DATA_UNAVAILABLE, obs_id=request.obs_id, products=(), provenance=provenance
            )
        product = RetrievedProduct(product=provenance.products[0], location="synthetic://not-a-real-file")
        return RetrievalResult(outcome=ResultOutcome.OK, obs_id=request.obs_id, products=(product,), provenance=provenance)
