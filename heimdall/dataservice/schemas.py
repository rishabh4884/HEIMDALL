"""Schemas, states and configuration of the SPHEREx data-service layer.

Guarantee (and its limit): HEIMDALL owns acquisition, retries, provenance and eventual terminal status, so an upstream (IRSA) outage does
not surface as a user-facing error or require resubmission. It cannot create data that has never been ingested while every upstream
route is down; such a request stays ``PREPARING_DATA`` until the source recovers or the configured deadline gives the precise terminal
state ``UNAVAILABLE_AFTER_DEADLINE``. A failed route is never ``NO_COVERAGE``.

Deadline versus recovery: the deadline ends the *active attempt* and publishes ``UNAVAILABLE_AFTER_DEADLINE`` honestly. The request itself
is retained: a service-owned recovery policy re-checks it at a low, capped frequency (``recovery_interval_s``) for ``recovery_window_s``
after it first reached the deadline, without any new user request. Only after that window (or after a cancellation) does the status say
``must_resubmit: true``.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from enum import StrEnum

SCHEMA_VERSION = 1
STATUS_SCHEMA = "heimdall.data_status/1.0"


class JobState(StrEnum):
    QUEUED = "QUEUED"
    DISCOVERING = "DISCOVERING"
    DOWNLOADING = "DOWNLOADING"
    VALIDATING = "VALIDATING"
    PREPARING_DATA = "PREPARING_DATA"  # awaiting bounded background retry after an upstream failure
    COMPLETE = "COMPLETE"  # all required verified local data exist
    NO_COVERAGE = "NO_COVERAGE"  # a successful discovery found nothing
    UNAVAILABLE_AFTER_DEADLINE = "UNAVAILABLE_AFTER_DEADLINE"
    INVALID_SOURCE_DATA = "INVALID_SOURCE_DATA"  # retrieved data failed validation
    CANCELLED = "CANCELLED"


TERMINAL = frozenset({JobState.COMPLETE, JobState.NO_COVERAGE, JobState.UNAVAILABLE_AFTER_DEADLINE, JobState.INVALID_SOURCE_DATA,
                      JobState.CANCELLED})
ACTIVE_RUNNING = frozenset({JobState.DISCOVERING, JobState.DOWNLOADING, JobState.VALIDATING})
# Allowed transitions. Terminal states are revived (generation + 1, recorded in job_events) only by a new submission or, for
# UNAVAILABLE_AFTER_DEADLINE, by the documented service-owned recovery policy (JobQueue.recover_due); never by a silent state edit.
TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.QUEUED: frozenset({JobState.DISCOVERING, JobState.DOWNLOADING, JobState.PREPARING_DATA, JobState.CANCELLED,
                                JobState.UNAVAILABLE_AFTER_DEADLINE, JobState.COMPLETE, JobState.NO_COVERAGE, JobState.INVALID_SOURCE_DATA}),
    JobState.DISCOVERING: frozenset({JobState.DOWNLOADING, JobState.PREPARING_DATA, JobState.COMPLETE, JobState.NO_COVERAGE, JobState.QUEUED,
                                     JobState.CANCELLED, JobState.UNAVAILABLE_AFTER_DEADLINE, JobState.INVALID_SOURCE_DATA}),
    JobState.DOWNLOADING: frozenset({JobState.VALIDATING, JobState.PREPARING_DATA, JobState.COMPLETE, JobState.QUEUED, JobState.CANCELLED,
                                     JobState.UNAVAILABLE_AFTER_DEADLINE, JobState.INVALID_SOURCE_DATA}),
    JobState.VALIDATING: frozenset({JobState.DOWNLOADING, JobState.PREPARING_DATA, JobState.COMPLETE, JobState.QUEUED, JobState.CANCELLED,
                                    JobState.UNAVAILABLE_AFTER_DEADLINE, JobState.INVALID_SOURCE_DATA}),
    JobState.PREPARING_DATA: frozenset({JobState.QUEUED, JobState.DISCOVERING, JobState.DOWNLOADING, JobState.COMPLETE, JobState.NO_COVERAGE,
                                        JobState.CANCELLED, JobState.UNAVAILABLE_AFTER_DEADLINE, JobState.INVALID_SOURCE_DATA}),
    **{s: frozenset() for s in TERMINAL},
    JobState.UNAVAILABLE_AFTER_DEADLINE: frozenset({JobState.CANCELLED}),  # a user may stop the automatic recovery policy
}


class Readiness(StrEnum):
    """What the caller is told about data readiness (the job state, when there is one, is reported alongside)."""
    READY = "READY"
    PREPARING = "PREPARING"
    NO_COVERAGE = "NO_COVERAGE"
    UNAVAILABLE_AFTER_DEADLINE = "UNAVAILABLE_AFTER_DEADLINE"
    INVALID_SOURCE_DATA = "INVALID_SOURCE_DATA"
    CANCELLED = "CANCELLED"


JOB_TO_READINESS = {JobState.COMPLETE: Readiness.READY, JobState.NO_COVERAGE: Readiness.NO_COVERAGE,
                    JobState.UNAVAILABLE_AFTER_DEADLINE: Readiness.UNAVAILABLE_AFTER_DEADLINE,
                    JobState.INVALID_SOURCE_DATA: Readiness.INVALID_SOURCE_DATA, JobState.CANCELLED: Readiness.CANCELLED}

# Current searchable releases (documented by IRSA). QR2 is downloadable but not searchable and is never mixed in silently.
CURRENT_COLLECTIONS = ("spherex_dr1", "spherex_dr1_deep", "spherex_qr3", "spherex_qr3_deep")
SCOPE_CURRENT = "current"
SCOPE_SYNTHETIC = "synthetic"  # test fixtures only; never mixed with real inventory

USER_MESSAGES = {
    Readiness.READY: "All required SPHEREx data are verified and stored locally.",
    Readiness.PREPARING: "Your request is accepted. HEIMDALL is preparing the required SPHEREx data and will keep retrying the archive automatically; "
                         "you do not need to resubmit.",
    Readiness.NO_COVERAGE: "The archive was searched successfully and lists no matching SPHEREx observation (as of the stated index time).",
    Readiness.UNAVAILABLE_AFTER_DEADLINE: "The archive could not be reached for long enough to acquire the data within the configured deadline. "
                                          "Nothing was concluded about the sky; the request is recorded with its full attempt history. "
                                          "See auto_recovery: while it is active HEIMDALL re-checks the archive itself and you do not need to resubmit.",
    Readiness.INVALID_SOURCE_DATA: "The archive returned data that failed integrity or format validation, so it was quarantined and not used.",
    Readiness.CANCELLED: "The request was cancelled.",
}


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def digest(obj, n: int = 16) -> str:
    return hashlib.sha256(canonical(obj).encode()).hexdigest()[:n]


def _env_float(env: dict, name: str, default: float) -> float:
    try:
        return float(env[name]) if env.get(name) else default
    except ValueError:
        raise ValueError(f"{name} must be a number") from None


@dataclass(frozen=True)
class ServiceConfig:
    """Operational configuration (environment variables ``HEIMDALL_DS_*``); every limit is finite."""
    deadline_s: float = 6 * 3600.0           # how long the service keeps trying upstream before UNAVAILABLE_AFTER_DEADLINE
    backoff_base_s: float = 30.0
    backoff_cap_s: float = 1800.0
    backoff_jitter: float = 0.25
    poll_s: float = 2.0                       # how soon a parent job re-checks a running child
    lease_s: float = 900.0                    # a running job whose lease expired is resumed (service crash recovery)
    max_download_failures_per_run: int = 3    # stop hammering a failing route within one worker run
    corrupt_retries: int = 2                  # re-download attempts for a corrupt/mismatching product before quarantine
    quota_bytes: int | None = None            # soft quota for the product store; None = unlimited
    index_max_age_s: float = 7 * 86400.0      # older syncs still answer, but trigger a background refresh
    recovery_window_s: float = 7 * 86400.0    # after a deadline, the service keeps re-checking the request for this long (0 disables)
    recovery_interval_s: float = 6 * 3600.0   # low-frequency recovery check interval
    recovery_attempt_s: float = 1800.0        # deadline of each automatic recovery attempt
    scope: str = SCOPE_CURRENT

    @classmethod
    def from_env(cls, env: dict | None = None) -> ServiceConfig:
        e = dict(os.environ if env is None else env)
        q = e.get("HEIMDALL_DS_QUOTA_BYTES")
        return cls(deadline_s=_env_float(e, "HEIMDALL_DS_DEADLINE_S", cls.deadline_s), backoff_base_s=_env_float(e, "HEIMDALL_DS_BACKOFF_BASE_S", cls.backoff_base_s),
                   backoff_cap_s=_env_float(e, "HEIMDALL_DS_BACKOFF_CAP_S", cls.backoff_cap_s), backoff_jitter=_env_float(e, "HEIMDALL_DS_BACKOFF_JITTER", cls.backoff_jitter),
                   poll_s=_env_float(e, "HEIMDALL_DS_POLL_S", cls.poll_s), lease_s=_env_float(e, "HEIMDALL_DS_LEASE_S", cls.lease_s),
                   quota_bytes=int(q) if q else None, index_max_age_s=_env_float(e, "HEIMDALL_DS_INDEX_MAX_AGE_S", cls.index_max_age_s),
                   recovery_window_s=_env_float(e, "HEIMDALL_DS_RECOVERY_WINDOW_S", cls.recovery_window_s),
                   recovery_interval_s=_env_float(e, "HEIMDALL_DS_RECOVERY_INTERVAL_S", cls.recovery_interval_s),
                   recovery_attempt_s=_env_float(e, "HEIMDALL_DS_RECOVERY_ATTEMPT_S", cls.recovery_attempt_s))


@dataclass(frozen=True)
class RegionSpec:
    """A cone plus the filters that define what 'indexed' means. Arbitrary coordinates are first-class."""
    ra_deg: float
    dec_deg: float
    radius_deg: float
    detectors: tuple[int, ...] | None = None
    mjd_start: float | None = None
    mjd_end: float | None = None
    collections: tuple[str, ...] | None = None  # None = all current DR1/QR3 collections

    def validate(self) -> None:
        if not (0.0 <= self.ra_deg < 360.0 and -90.0 <= self.dec_deg <= 90.0 and 0.0 < self.radius_deg <= 5.0):
            raise ValueError("region must have 0<=ra<360, -90<=dec<=90, 0<radius<=5 degrees")
        if self.detectors and not all(d in range(1, 7) for d in self.detectors):
            raise ValueError("detectors must be integers 1-6")
        if self.collections and not all(c in CURRENT_COLLECTIONS for c in self.collections):
            raise ValueError("only current DR1/QR3 collections are searchable; QR2 is never mixed in silently")

    def to_dict(self) -> dict:
        return {"ra_deg": round(self.ra_deg, 8), "dec_deg": round(self.dec_deg, 8), "radius_deg": round(self.radius_deg, 8),
                "detectors": sorted(self.detectors) if self.detectors else None, "mjd_start": self.mjd_start, "mjd_end": self.mjd_end,
                "collections": sorted(self.collections) if self.collections else None}

    @classmethod
    def from_dict(cls, d: dict) -> RegionSpec:
        return cls(d["ra_deg"], d["dec_deg"], d["radius_deg"], tuple(d["detectors"]) if d.get("detectors") else None, d.get("mjd_start"),
                   d.get("mjd_end"), tuple(d["collections"]) if d.get("collections") else None)


@dataclass(frozen=True)
class PlanItem:
    """One science product the analysis needs: an observation (optionally a cutout) at a given release."""
    obs_id: str
    release: str
    detector: int
    access_reference: str
    cutout_ra_deg: float | None = None
    cutout_dec_deg: float | None = None
    cutout_size_deg: float | None = None
    archive_checksum: str | None = None  # "md5:<hex>" when the archive provides one

    @property
    def key(self) -> str:
        cut = "full" if self.cutout_size_deg is None else f"cut:{self.cutout_ra_deg:.8f},{self.cutout_dec_deg:.8f},{self.cutout_size_deg:.6f}"
        return f"obs:{self.obs_id}|{self.release}|{cut}"

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass(frozen=True)
class ProductPlan:
    items: tuple[PlanItem, ...]
    calibrations: tuple[tuple[str, int], ...] = field(default_factory=tuple)  # (kind, detector), e.g. ("epsf", 2)

    def to_dict(self) -> dict:
        return {"items": [i.to_dict() for i in sorted(self.items, key=lambda i: i.key)], "calibrations": sorted([list(c) for c in self.calibrations])}

    @classmethod
    def from_dict(cls, d: dict) -> ProductPlan:
        return cls(tuple(PlanItem(**i) for i in d["items"]), tuple((k, int(det)) for k, det in d["calibrations"]))


def calibration_key(kind: str, detector: int) -> str:
    return f"calib:{kind}|D{detector}"
