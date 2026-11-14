"""Shared, provisional data contracts used by every HEIMDALL module.

This package depends only on the Python standard library and never imports
data access, pipeline, configuration or presentation code.
"""

from ._base import ContractViolation
from .candidates import (
    Association,
    AttentionLevel,
    AttentionReason,
    AttentionReasonCode,
    Candidate,
    CandidateKind,
    Detection,
)
from .observations import Measurement, Observation
from .outcomes import (
    ErrorCategory,
    ErrorCode,
    ErrorInfo,
    FailedItem,
    HeimdallError,
    ResultOutcome,
    category_of,
)
from .provenance import DataOrigin, Provenance, SourceProduct
from .registry import (
    FieldSpec,
    InputResolution,
    InputSpec,
    MethodRegistry,
    MethodSpec,
    Presence,
    RecordCheck,
    RecordSchema,
    SchemaRegistry,
)
from .validity import (
    Alternative,
    AlternativeState,
    Applicability,
    Check,
    CheckState,
    EvidenceKind,
    EvidenceRef,
    Statement,
    StatementKind,
    Validity,
    ValidityReason,
    ValidityState,
    order_reasons,
)
from .values import (
    ACCESSIBLE_TAG_NAMES,
    Assumption,
    BlockedBy,
    BlockedCause,
    InputRef,
    InputRole,
    NotEstablishedDetail,
    Origin,
    OriginInfo,
    Tag,
    UnavailableReason,
    Uncertainty,
    UncertaintyStatus,
    ValueClass,
    ValueObject,
    assumed,
    derived,
    measured,
    unavailable,
)

__all__ = [name for name in dir() if not name.startswith("_")]
