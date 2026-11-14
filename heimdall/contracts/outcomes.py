"""Results, errors and failure reasons (contract §8).

A *result* is a successful operation with a scientific or data-availability
outcome, for example ``NO_OBSERVATIONS``. An *error* is a failed operation and
is raised as ``HeimdallError``. A failed request is never evidence that
nothing was found.

Error messages and details must never contain secrets.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

from ._base import non_empty_str, require


class ResultOutcome(StrEnum):
    OK = "OK"
    NO_OBSERVATIONS = "NO_OBSERVATIONS"  # query succeeded and returned none
    INSUFFICIENT_OBSERVATIONS = "INSUFFICIENT_OBSERVATIONS"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"  # absence established by a successful query
    PARTIAL = "PARTIAL"  # some items failed; see failed_items
    INCONCLUSIVE = "INCONCLUSIVE"
    NOT_ASSESSABLE = "NOT_ASSESSABLE"
    NOT_DETECTED = "NOT_DETECTED"


class ErrorCategory(StrEnum):
    INPUT = "INPUT"
    CONFIGURATION = "CONFIGURATION"
    ACCESS = "ACCESS"
    NETWORK = "NETWORK"
    DATA = "DATA"
    ANALYSIS = "ANALYSIS"
    INTERNAL = "INTERNAL"


class ErrorCode(StrEnum):
    INPUT_INVALID = "INPUT_INVALID"
    CONFIG_INVALID = "CONFIG_INVALID"
    SOURCE_NOT_CONFIGURED = "SOURCE_NOT_CONFIGURED"
    CREDENTIALS_MISSING = "CREDENTIALS_MISSING"
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    ACCESS_DENIED = "ACCESS_DENIED"
    ARCHIVE_UNREACHABLE = "ARCHIVE_UNREACHABLE"
    ARCHIVE_TIMEOUT = "ARCHIVE_TIMEOUT"
    RETRIEVAL_FAILED = "RETRIEVAL_FAILED"
    RELEASE_UNAVAILABLE = "RELEASE_UNAVAILABLE"
    DATA_MALFORMED = "DATA_MALFORMED"
    ANALYSIS_STAGE_FAILED = "ANALYSIS_STAGE_FAILED"
    INTERNAL = "INTERNAL"


_CATEGORY: dict[ErrorCode, ErrorCategory] = {
    ErrorCode.INPUT_INVALID: ErrorCategory.INPUT,
    ErrorCode.CONFIG_INVALID: ErrorCategory.CONFIGURATION,
    ErrorCode.SOURCE_NOT_CONFIGURED: ErrorCategory.CONFIGURATION,
    ErrorCode.CREDENTIALS_MISSING: ErrorCategory.CONFIGURATION,
    ErrorCode.AUTHENTICATION_FAILED: ErrorCategory.ACCESS,
    ErrorCode.ACCESS_DENIED: ErrorCategory.ACCESS,
    ErrorCode.ARCHIVE_UNREACHABLE: ErrorCategory.NETWORK,
    ErrorCode.ARCHIVE_TIMEOUT: ErrorCategory.NETWORK,
    ErrorCode.RETRIEVAL_FAILED: ErrorCategory.NETWORK,
    ErrorCode.RELEASE_UNAVAILABLE: ErrorCategory.DATA,
    ErrorCode.DATA_MALFORMED: ErrorCategory.DATA,
    ErrorCode.ANALYSIS_STAGE_FAILED: ErrorCategory.ANALYSIS,
    ErrorCode.INTERNAL: ErrorCategory.INTERNAL,
}

# Transient network failures may be retried; everything else needs a fix first.
_RETRYABLE = frozenset({ErrorCode.ARCHIVE_UNREACHABLE, ErrorCode.ARCHIVE_TIMEOUT, ErrorCode.RETRIEVAL_FAILED})


def category_of(code: ErrorCode) -> ErrorCategory:
    return _CATEGORY[code]


@dataclass(frozen=True, slots=True, kw_only=True)
class ErrorInfo:
    code: ErrorCode
    message: str
    detail: Mapping[str, str] = field(default_factory=dict)
    source: str | None = None
    request_id: str | None = None

    def __post_init__(self) -> None:
        require(non_empty_str(self.message), "ErrorInfo.message must be non-empty")
        object.__setattr__(self, "detail", MappingProxyType(dict(self.detail)))

    @property
    def category(self) -> ErrorCategory:
        return _CATEGORY[self.code]

    @property
    def retryable(self) -> bool:
        return self.code in _RETRYABLE


class HeimdallError(Exception):
    """A failed operation, carrying a typed ``ErrorInfo``."""

    def __init__(self, info: ErrorInfo) -> None:
        super().__init__(f"{info.code}: {info.message}")
        self.info = info

    @classmethod
    def of(cls, code: ErrorCode, message: str, **detail: str) -> HeimdallError:
        return cls(ErrorInfo(code=code, message=message, detail=detail))

    @property
    def code(self) -> ErrorCode:
        return self.info.code


@dataclass(frozen=True, slots=True, kw_only=True)
class FailedItem:
    """One item that failed inside an otherwise successful (partial) operation."""

    ref: str
    error: ErrorInfo
