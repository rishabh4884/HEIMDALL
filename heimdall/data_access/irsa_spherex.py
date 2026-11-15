"""Adapter for the public NASA/IPAC IRSA SPHEREx archive.

The adapter uses the public live service without credentials:
- TAP sync endpoint ``https://irsa.ipac.caltech.edu/TAP/sync`` (ADQL, CSV);
  tables ``spherex.plane`` (``poly``, ``time_bounds_*``, ``energy_bounds_*`` in
  metres, ``energy_bandpassname`` = ``SPHEREx-D<n>``, ``provenance_version``),
  ``spherex.artifact`` (``uri``, ``contentchecksum`` = ``md5:<hex>``,
  ``contentlength``, ``producttype``) and ``spherex.observation``
  (``collection``, ``observationid``).
- Level 2 images at ``https://irsa.ipac.caltech.edu/<uri>``; the cutout service
  takes ``?center=<ra>,<dec>d&size=<deg>`` and returns the same MEF layout.
- The documented SIA2 endpoint ``https://irsa.ipac.caltech.edu/SIA`` is used
  as a discovery fallback for current DR1/QR3 collections when TAP has a
  transient network failure.  Its standard CSV fields are validated before
  becoming observations; a failed fallback never becomes an empty search.
- QR3 ePSF calibration product (documented in IRSA's PSF tutorial) on the
  public S3 bucket.

Identifiers: IRSA's ``observationid`` names one exposure of all six detectors;
HEIMDALL's ``obs_id`` is ``<observationid>D<detector>`` (one image), matching
the product file names.
"""

from __future__ import annotations

import csv
import hashlib
import io
import random
import re
import time
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path

from .. import __version__
from ..config import Settings
from ..contracts import (DataOrigin, ErrorCode, ErrorInfo, FailedItem, HeimdallError, Observation, Origin, OriginInfo,
                         Provenance, ResultOutcome, SourceProduct, Uncertainty, ValueClass, measured)
from .cache import ProductCache
from .http import HttpClient, Transport
from .resilience import CircuitBreaker
from .interface import (AccessReport, DataSource, DiscoveryQuery, DiscoveryResult, RetrievalRequest, RetrievalResult,
                        RetrievedProduct, SourceDescription)

SOURCE_ID = "irsa-spherex"
IRSA_BASE = "https://irsa.ipac.caltech.edu/"
TAP_SYNC = IRSA_BASE + "TAP/sync"
SIA = IRSA_BASE + "SIA"
S3 = "https://nasa-irsa-spherex.s3.us-east-1.amazonaws.com/"
# Calibration products hosted on the public bucket. The ePSF is IRSA's
# recommended PSF for all releases; the spectral WCS and solid-angle map are the QR2 versions.
CALIBRATIONS = {
    "epsf": ("qr3/epsf/cal-epsf-v1-2026-191/{det}/epsf_D{det}_spx_cal-epsf-v1-2026-191.fits", "spherex_qr3_calibration"),
    "spectral_wcs": ("qr2/spectral_wcs/cal-wcs-v4-2025-254/{det}/spectral_wcs_D{det}_spx_cal-wcs-v4-2025-254.fits",
                     "spherex_qr2_calibration"),
    "solid_angle": ("qr2/solid_angle_pixel_map/cal-sapm-v2-2025-164/{det}/solid_angle_pixel_map_D{det}_spx_cal-sapm-v2-2025-164.fits",
                    "spherex_qr2_calibration"),
}
EPSF_URL = S3 + CALIBRATIONS["epsf"][0]


def _release_rank(collection: str) -> int:
    m = re.search(r"(qr|dr)(\d+)", collection)
    return (100 if m.group(1) == "dr" else 0) + int(m.group(2)) if m else -1
TAP_SOURCE = "IRSA TAP (spherex.plane/artifact/observation)"
SIA_SOURCE = "IRSA SIA2 (current DR1/QR3 image collections)"
ROUTE_SOURCE = {"tap": TAP_SOURCE, "sia2": SIA_SOURCE}
SIA_REQUIRED = ("obs_id", "access_url", "t_min", "t_max", "em_min", "em_max")
# Pinned public DR1 image (detector 6) used only to probe the direct product service; position taken from its own SIA2 row.
PROBE_PRODUCT = ("ibe/data/spherex/dr1/level2/2025W33_2C/l2b-v27-2026-228/6/"
                 "level2_2025W33_2C_0321_1D6_spx_l2b-v27-2026-228.fits")
PROBE_RA, PROBE_DEC = 269.4485, 4.7394
PROBE_PRODUCT_RA, PROBE_PRODUCT_DEC = PROBE_RA, PROBE_DEC


def summary_of(attempts: list[dict]) -> str:
    return ", ".join(f"{a['route']}={a.get('error_code') or a['outcome']}" for a in attempts if a["outcome"] != "OK")

REQUIRED_COLUMNS = ("collection", "observationid", "energy_bandpassname", "energy_bounds_lower", "energy_bounds_upper",
                    "time_bounds_lower", "time_bounds_upper", "provenance_version", "uri", "contentchecksum")
_NAME = re.compile(r"^[a-z0-9_]+$")
_OBS = re.compile(r"^(?P<obs>[A-Za-z0-9_]+)D(?P<det>[1-6])$")
_SIA_PRODUCT = re.compile(r"(?:^|/)level2_(?P<obs>[A-Za-z0-9_]+)D(?P<det>[1-6])_spx[^/]*\.fits(?:$|[?#])", re.I)
# Current public image collections documented by IRSA.  QR2 remains directly
# downloadable, but is no longer searchable, so it is intentionally excluded.
SIA_COLLECTIONS = ("spherex_dr1", "spherex_dr1_deep", "spherex_qr3", "spherex_qr3_deep")


def _floats(text: str, name: str) -> float:
    try:
        return float(text)
    except ValueError:
        raise HeimdallError.of(ErrorCode.INPUT_INVALID, f"filter {name} must be a number", filter=name) from None


class IrsaSpherexSource(DataSource):
    """IRSA SPHEREx source.

    Discovery routes, in preference order (``route_order``):

    1. ``sia2`` - IRSA's documented Simple Image Access v2 service, which IRSA's SPHEREx documentation names as the way to search
       the current DR1 and QR3 collections (QR2 is no longer searchable there);
    2. ``tap`` - the legacy TAP service, kept as a bounded fallback and for explicitly requested legacy collections.

    A route failing for a transient reason (timeout, HTTP 5xx, malformed answer) moves on to the next route; every attempt is recorded
    in ``last_discovery`` and in the result provenance. A route that fails repeatedly is skipped for a cooldown (circuit breaker).
    If no route answers, ``discover`` raises; it never returns an empty result for a failed search.
    """

    def __init__(self, settings: Settings, transport: Transport | None = None, sleep=time.sleep, *,
                 route_order: tuple[str, ...] = ("sia2", "tap"), jitter: float = 0.25, rng: random.Random | None = None,
                 clock=time.monotonic, breaker_threshold: int = 3, breaker_cooldown_s: float = 120.0,
                 route_timeouts: dict[str, float] | None = None) -> None:
        if not route_order or set(route_order) - {"sia2", "tap"}:
            raise ValueError("route_order must be a non-empty tuple drawn from 'sia2', 'tap'")
        self._settings = settings
        self._http = HttpClient(SOURCE_ID, settings.request_timeout_s, settings.max_retries, transport, sleep,
                                offline=settings.offline, jitter=jitter, rng=rng)
        self.cache = ProductCache(Path(settings.cache_dir) / SOURCE_ID)
        self.route_order = tuple(route_order)
        self.route_timeouts = {"tap": min(settings.request_timeout_s, 45.0), "sia2": max(settings.request_timeout_s, 60.0)}
        self.route_timeouts.update(route_timeouts or {})
        self.breakers = {r: CircuitBreaker(breaker_threshold, breaker_cooldown_s, clock) for r in ("sia2", "tap")}
        self.last_tap_from_cache = False
        self._from_cache = False
        self.last_discovery: dict | None = None

    @property
    def source_id(self) -> str:
        return SOURCE_ID

    def describe(self) -> SourceDescription:
        return SourceDescription(
            source_id=SOURCE_ID, display_name="NASA/IPAC IRSA: SPHEREx Level 2 spectral images (public archive)",
            requires_credentials=False, capabilities_verified=False,
            notes="SIA2 discovery (current DR1/QR3) with bounded TAP fallback, cutout retrieval and QR3 ePSF access are supported; "
                  "capability verification remains conservative until automated live conformance completes.")

    # -- TAP -------------------------------------------------------------------

    def _tap(self, adql: str) -> list[dict[str, str]]:
        """Run a TAP query. Responses are cached so offline reruns use exactly the same archive answer."""
        body = urllib.parse.urlencode({"REQUEST": "doQuery", "LANG": "ADQL", "FORMAT": "csv", "QUERY": adql}).encode()
        key = TAP_SYNC + "?" + body.decode()
        self.last_tap_from_cache = False
        if self._settings.offline:
            entry = self.cache.get(key)
            if entry is None:
                raise HeimdallError.of(ErrorCode.ARCHIVE_UNREACHABLE, "offline mode: this TAP query has no cached response",
                                       offline="true")
            text = entry.path.read_bytes().decode("utf-8", errors="replace")
            self.last_tap_from_cache = True
        else:
            resp = self._http.request("POST", TAP_SYNC, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"},
                                      timeout_s=self.route_timeouts["tap"])
            text = resp.body.decode("utf-8", errors="replace")
            if not text.lstrip().startswith("<"):
                self.cache.put(key, resp.body, {"kind": "tap_response"})
        if text.lstrip().startswith("<"):
            raise HeimdallError.of(ErrorCode.DATA_MALFORMED, "TAP returned an XML document instead of CSV (query error)")
        try:
            reader = csv.DictReader(io.StringIO(text))
            rows = list(reader)
        except csv.Error as exc:
            raise HeimdallError.of(ErrorCode.DATA_MALFORMED, f"TAP CSV could not be parsed: {exc}") from None
        if reader.fieldnames is None:
            raise HeimdallError.of(ErrorCode.DATA_MALFORMED, "TAP response has no header row")
        self._tap_fields = list(reader.fieldnames)
        return rows

    # -- route health ------------------------------------------------------------

    def route_health(self) -> list[dict]:
        """Probe each access route independently with one bounded attempt (no retries, no cache, never raises).

        Components: ``tap`` and ``sia2`` (discovery), ``product`` (a pinned public DR1 image cutout fetched from the IRSA product service)
        and ``calibration`` (the QR3 ePSF on the public bucket). A route is ``OK`` only if its answer was usable, not merely HTTP 200.
        """
        def probe(name: str, fn) -> dict:
            t0 = time.monotonic()
            rec = {"route": name, "checked_at": datetime.now(UTC).isoformat(timespec="seconds"), "status": "OK", "error_code": None,
                   "http_status": None, "detail": ""}
            try:
                rec["detail"] = fn()
            except HeimdallError as exc:
                rec.update(status="FAILED", error_code=exc.code.value, http_status=exc.info.detail.get("http_status"),
                           detail=exc.info.message)
            except Exception as exc:  # noqa: BLE001 - a probe must report, never raise
                rec.update(status="FAILED", error_code="UNEXPECTED", detail=f"{type(exc).__name__}: {exc}")
            rec["elapsed_s"] = round(time.monotonic() - t0, 2)
            return rec

        def sia() -> str:
            text = self._sia_get(SIA + "?" + urllib.parse.urlencode(
                {"COLLECTION": "spherex_dr1", "POS": f"CIRCLE {PROBE_RA:.4f} {PROBE_DEC:.4f} 0.02", "RESPONSEFORMAT": "CSV", "MAXREC": "1"}),
                cache=False, retries=0)
            return f"SIA2 answered with a valid ObsCore header ({len(text)} bytes)"

        def tap() -> str:
            resp = self._http.request("POST", TAP_SYNC, headers={"Content-Type": "application/x-www-form-urlencoded"},
                                      data=urllib.parse.urlencode({"REQUEST": "doQuery", "LANG": "ADQL", "FORMAT": "csv",
                                                                   "QUERY": "SELECT TOP 1 observationid FROM spherex.observation"}).encode(),
                                      timeout_s=self.route_timeouts["tap"], max_retries=0)
            if not resp.body.lstrip().lower().startswith(b"observationid"):
                raise HeimdallError.of(ErrorCode.DATA_MALFORMED, "TAP answered but not with the expected CSV")
            return "TAP answered a one-row query"

        def product() -> str:
            url = IRSA_BASE + PROBE_PRODUCT + f"?center={PROBE_PRODUCT_RA:.6f},{PROBE_PRODUCT_DEC:.6f}d&size=0.02"
            resp = self._http.request("GET", url, timeout_s=self.route_timeouts["sia2"], max_retries=0)
            if not resp.body.startswith(b"SIMPLE  ="):
                raise HeimdallError.of(ErrorCode.DATA_MALFORMED, "product service did not return FITS")
            return f"pinned DR1 image cutout returned {len(resp.body)} bytes of FITS"

        def calibration() -> str:
            resp = self._http.request("HEAD", S3 + CALIBRATIONS["epsf"][0].format(det=2), max_retries=0)
            return f"QR3 ePSF object reachable (HTTP {resp.status})"

        return [probe("tap", tap), probe("sia2", sia), probe("product", product), probe("calibration", calibration)]

    def check_access(self) -> AccessReport:
        """Healthy when at least one discovery route answers. Both routes are tried; the detail records each."""
        if self._settings.offline:
            raise HeimdallError.of(ErrorCode.ARCHIVE_UNREACHABLE, "offline mode: no network request is made", offline="true")
        health = {h["route"]: h for h in self.route_health()[:2]}
        if all(h["status"] != "OK" for h in health.values()):
            first = next(iter(health.values()))
            raise HeimdallError.of(ErrorCode.ARCHIVE_UNREACHABLE if first["error_code"] == "ARCHIVE_UNREACHABLE" else ErrorCode.RETRIEVAL_FAILED,
                                   "no IRSA discovery route answered: " + "; ".join(f"{k}: {v['detail']}" for k, v in health.items()))
        detail = "IRSA discovery reachable via " + ", ".join(k for k, v in health.items() if v["status"] == "OK") + \
                 ("; unavailable: " + ", ".join(k for k, v in health.items() if v["status"] != "OK") if any(v["status"] != "OK" for v in health.values()) else "") + \
                 "; no credentials required"
        return AccessReport(source_id=SOURCE_ID, checked_at=datetime.now(UTC), detail=detail)

    # -- SIA2 ---------------------------------------------------------------------

    @staticmethod
    def _sia_uri(access_url: str) -> str:
        """Accept only the documented on-premises FITS URLs returned by SIA2."""
        parsed = urllib.parse.urlsplit(access_url)
        if parsed.scheme != "https" or parsed.netloc != urllib.parse.urlsplit(IRSA_BASE).netloc:
            raise ValueError("SIA2 access_url is not an IRSA on-premises HTTPS URL")
        uri = parsed.path.lstrip("/")
        if not uri.startswith("ibe/data/spherex/") or ".." in uri:
            raise ValueError("SIA2 access_url is not a SPHEREx IBE product path")
        return uri

    @staticmethod
    def _requested_collections(query: DiscoveryQuery) -> list[str]:
        filters = dict(query.filters)
        return sorted({c for c in (filters.get("collections", "").split(",") + ([query.release] if query.release else [])) if c})

    def _sia_collections(self, query: DiscoveryQuery) -> tuple[str, ...]:
        requested = self._requested_collections(query)
        if not requested:
            return SIA_COLLECTIONS
        unknown = sorted(set(requested) - set(SIA_COLLECTIONS))
        if unknown:
            raise HeimdallError.of(ErrorCode.RELEASE_UNAVAILABLE,
                                   "SIA2 searches current DR1/QR3 collections only", collections=",".join(unknown))
        return tuple(requested)

    def _sia_get(self, url: str, *, cache: bool = True, retries: int | None = None) -> str:
        """One SIA2 CSV answer, validated and cached. Offline mode reads the cache. An error page is never cached."""
        self._from_cache = False
        if self._settings.offline:
            entry = self.cache.get(url)
            if entry is None:
                raise HeimdallError.of(ErrorCode.ARCHIVE_UNREACHABLE, "offline mode: this SIA2 query has no cached response",
                                       offline="true")
            self._from_cache = True
            return entry.path.read_bytes().decode("utf-8", errors="replace")
        resp = self._http.request("GET", url, timeout_s=self.route_timeouts["sia2"], max_retries=retries)
        text = resp.body.decode("utf-8", errors="replace")
        if text.lstrip().startswith("<") or not text.strip():
            raise HeimdallError.of(ErrorCode.DATA_MALFORMED, "SIA2 returned XML/empty instead of the requested CSV")
        header = text.splitlines()[0].split(",")
        missing = [c for c in SIA_REQUIRED if c not in header]
        if missing:
            raise HeimdallError.of(ErrorCode.DATA_MALFORMED, f"SIA2 response lacks standard ObsCore columns: {missing}")
        if cache:
            self.cache.put(url, resp.body, {"kind": "sia2_response"})
        return text

    def _sia(self, query: DiscoveryQuery) -> list[dict[str, str]]:
        """Search current SPHEREx images through IRSA's documented SIA2 CSV API.

        Results are normalised to the internal TAP-row shape so all filtering, de-duplication and provenance logic is shared.
        Each row must carry standard ObsCore fields and an IRSA SPHEREx product URL; anything else makes the answer malformed.
        """
        filters = dict(query.filters)
        self._where(query)  # validates common filters before any network request
        dets = {d.strip() for d in filters.get("detectors", "1,2,3,4,5,6").split(",")}
        mjd_start = float(filters["mjd_start"]) if filters.get("mjd_start") else None
        mjd_end = float(filters["mjd_end"]) if filters.get("mjd_end") else None
        region = query.region
        rows: list[dict[str, str]] = []
        from_cache = []
        for collection in self._sia_collections(query):
            params = {"COLLECTION": collection,
                      "POS": f"CIRCLE {region.ra_deg:.10f} {region.dec_deg:.10f} {region.radius_deg:.10f}",
                      "RESPONSEFORMAT": "CSV"}
            text = self._sia_get(SIA + "?" + urllib.parse.urlencode(params))
            from_cache.append(self._from_cache)
            try:
                raw_rows = list(csv.DictReader(io.StringIO(text)))
            except csv.Error as exc:
                raise HeimdallError.of(ErrorCode.DATA_MALFORMED, f"SIA2 CSV could not be parsed: {exc}") from None
            for raw in raw_rows:
                access_url = (raw.get("access_url") or "").strip()
                match = _SIA_PRODUCT.search(urllib.parse.urlsplit(access_url).path)
                if not match:
                    raise HeimdallError.of(ErrorCode.DATA_MALFORMED,
                                           "SIA2 row has no recognisable SPHEREx Level-2 FITS identifier")
                try:
                    uri = self._sia_uri(access_url)
                except ValueError as exc:
                    raise HeimdallError.of(ErrorCode.DATA_MALFORMED, str(exc)) from None
                row_collection = (raw.get("obs_collection") or collection).strip()
                if row_collection != collection:
                    raise HeimdallError.of(ErrorCode.DATA_MALFORMED,
                                           f"SIA2 row belongs to {row_collection!r}, not the requested {collection!r}")
                subtype = (raw.get("dataproduct_subtype") or "science").strip().lower()
                if subtype != "science":
                    continue  # only science images are observations; this is a filter, not a failure
                detector = match.group("det")
                if detector not in dets:
                    continue
                try:
                    t_min, t_max = float(raw["t_min"]), float(raw["t_max"])
                    em_min, em_max = float(raw["em_min"]), float(raw["em_max"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise HeimdallError.of(ErrorCode.DATA_MALFORMED,
                                           "SIA2 row lacks valid ObsCore time or spectral bounds") from exc
                if mjd_start is not None and t_max < mjd_start:
                    continue
                if mjd_end is not None and t_min > mjd_end:
                    continue
                # The product filename, not an archive-specific ObsCore obs_id convention, is the stable exposure/detector
                # identifier used by direct retrieval.
                version = re.search(r"/l2b-v([^/]+)/", urllib.parse.urlsplit(access_url).path)
                rows.append({"collection": row_collection,
                             "observationid": match.group("obs"), "energy_bandpassname": f"SPHEREx-D{detector}",
                             "energy_bounds_lower": str(em_min), "energy_bounds_upper": str(em_max),
                             "time_bounds_lower": str(t_min), "time_bounds_upper": str(t_max),
                             "provenance_version": version.group(1) if version else "",
                             "uri": uri, "contentchecksum": "", "contentlength": ""})
        self._from_cache = bool(from_cache) and all(from_cache)
        return rows

    def _where(self, query: DiscoveryQuery, *, current_only: bool = False) -> list[str]:
        r = query.region
        clauses = [f"1 = CONTAINS(POINT('ICRS', {r.ra_deg:.10f}, {r.dec_deg:.10f}), p.poly)", "a.producttype = 'science'"]
        f = dict(query.filters)
        collections = [c for c in (f.get("collections", "").split(",") + ([query.release] if query.release else [])) if c]
        for c in collections:
            if not _NAME.match(c):
                raise HeimdallError.of(ErrorCode.INPUT_INVALID, "collection names may contain only a-z, 0-9 and _", filter="collections")
        if not collections and current_only:
            collections = list(SIA_COLLECTIONS)  # same scope as SIA2: current DR1/QR3 images, never legacy releases
        if collections:
            clauses.append("o.collection IN (" + ", ".join(f"'{c}'" for c in sorted(set(collections))) + ")")
        if f.get("detectors"):
            dets = [d.strip() for d in f["detectors"].split(",") if d.strip()]
            if not all(d in {"1", "2", "3", "4", "5", "6"} for d in dets):
                raise HeimdallError.of(ErrorCode.INPUT_INVALID, "detectors must be integers 1-6", filter="detectors")
            clauses.append("p.energy_bandpassname IN (" + ", ".join(f"'SPHEREx-D{d}'" for d in sorted(set(dets))) + ")")
        if f.get("mjd_start"):
            clauses.append(f"p.time_bounds_upper >= {_floats(f['mjd_start'], 'mjd_start'):.6f}")
        if f.get("mjd_end"):
            clauses.append(f"p.time_bounds_lower <= {_floats(f['mjd_end'], 'mjd_end'):.6f}")
        return clauses

    # -- discovery with route failover ---------------------------------------------

    def _tap_discovery_rows(self, query: DiscoveryQuery) -> list[dict[str, str]]:
        adql = ("SELECT o.collection, o.observationid, p.energy_bandpassname, p.energy_bounds_lower, p.energy_bounds_upper, "
                "p.time_bounds_lower, p.time_bounds_upper, p.provenance_version, a.uri, a.contentchecksum, a.contentlength "
                "FROM spherex.plane p JOIN spherex.artifact a ON a.planeid = p.planeid "
                "JOIN spherex.observation o ON o.obsid = p.obsid WHERE " + " AND ".join(self._where(query, current_only=True)) +
                " ORDER BY p.time_bounds_lower, p.energy_bandpassname")
        rows = self._tap(adql)
        missing = [c for c in REQUIRED_COLUMNS if c not in self._tap_fields]
        if missing:
            raise HeimdallError.of(ErrorCode.DATA_MALFORMED, f"discovery response lacks columns: {missing}")
        self._from_cache = self.last_tap_from_cache
        return rows

    @staticmethod
    def _failover_ok(exc: HeimdallError) -> bool:
        """Failures that say 'this route is unhealthy' (so another route may help). Auth, bad input and a dead host are not."""
        status = exc.info.detail.get("http_status")
        return (exc.code in (ErrorCode.ARCHIVE_TIMEOUT, ErrorCode.DATA_MALFORMED) or status in {"500", "502", "503"}
                or exc.info.detail.get("offline") == "true")

    def _plan_routes(self, query: DiscoveryQuery) -> tuple[list[str], str | None]:
        requested = self._requested_collections(query)
        if requested and set(requested) - set(SIA_COLLECTIONS):
            return ["tap"], ("legacy collections " + ",".join(sorted(set(requested) - set(SIA_COLLECTIONS))) +
                             " requested explicitly; SIA2 cannot search them, so TAP only (legacy, not current data)")
        return list(self.route_order), None

    def discover(self, query: DiscoveryQuery) -> DiscoveryResult:
        self._where(query)  # input validation first: bad filters are INPUT_INVALID on every route
        routes, plan_note = self._plan_routes(query)
        attempts: list[dict] = []
        rows: list[dict[str, str]] | None = None
        used = ""
        errors: list[HeimdallError] = []
        for route in routes:
            br = self.breakers[route]
            if not br.allow():
                attempts.append({"route": route, "outcome": "SKIPPED_CIRCUIT_OPEN", "retry_in_s": round(br.retry_in_s(), 1)})
                continue
            try:
                rows = self._sia(query) if route == "sia2" else self._tap_discovery_rows(query)
            except HeimdallError as exc:
                attempts.append({"route": route, "outcome": "FAILED", "error_code": exc.code.value,
                                 "http_status": exc.info.detail.get("http_status"), "message": exc.info.message})
                errors.append(exc)
                if not self._failover_ok(exc):
                    raise
                if not self._settings.offline:
                    br.failure()
                continue
            br.success()
            used = route
            attempts.append({"route": route, "outcome": "OK", "from_cache": self._from_cache})
            break
        self.last_discovery = {"route_used": used or None, "attempts": attempts, "from_cache": self._from_cache if used else False,
                               "plan_note": plan_note}
        if rows is None:
            # No route produced an answer. This is a failure, never an empty result.
            summary = "; ".join(f"{a['route']}: {a.get('error_code') or a['outcome']}" +
                                (f" (HTTP {a['http_status']})" if a.get("http_status") else "") for a in attempts)
            if errors:
                first = errors[0]
                raise HeimdallError.of(first.code, f"IRSA discovery failed on every route ({summary})",
                                       **{k: v for k, v in first.info.detail.items() if k in ("http_status", "host", "offline")})
            raise HeimdallError.of(ErrorCode.ARCHIVE_UNREACHABLE,
                                   f"IRSA discovery routes are in circuit-open cooldown ({summary}); retry later")
        discovery_source = ROUTE_SOURCE[used] + (f"; earlier route(s) failed: {summary_of(attempts)}" if len(attempts) > 1 else "")
        now = datetime.now(UTC)
        self.last_tap_from_cache = self._from_cache
        origin = DataOrigin.CACHED if self._from_cache else DataOrigin.LIVE
        prov = Provenance(origin=origin, retrieved_at=now, software_version=__version__,
                          notes="discovery metadata from " + discovery_source + (" (cached response, offline)" if self._from_cache else "") +
                                (f"; {plan_note}" if plan_note else ""))
        if not rows:
            return DiscoveryResult(outcome=ResultOutcome.NO_OBSERVATIONS, observations=(), provenance=prov)
        missing = [c for c in REQUIRED_COLUMNS if c not in rows[0]]
        if missing:
            raise HeimdallError.of(ErrorCode.DATA_MALFORMED, f"discovery response lacks columns: {missing}")
        failed = []
        # One exposure of one detector may be listed by several releases (e.g. QR2 and QR3 reprocessing). Counting
        # both would double the evidence, so the newest release is kept and the others are recorded on it.
        chosen: dict[str, tuple[int, Observation, list[str]]] = {}
        for i, row in enumerate(rows):
            try:
                obs = self._observation(row, now, discovery_source)
            except (ValueError, KeyError) as exc:
                failed.append(FailedItem(ref=f"row {i}", error=ErrorInfo(code=ErrorCode.DATA_MALFORMED, message=str(exc))))
                continue
            rank = _release_rank(row["collection"])
            if obs.obs_id not in chosen:
                chosen[obs.obs_id] = (rank, obs, [])
            elif rank > chosen[obs.obs_id][0]:
                old_rank, old, superseded = chosen[obs.obs_id]
                chosen[obs.obs_id] = (rank, obs, superseded + [str(old.metadata["release"].value)])
            else:
                chosen[obs.obs_id][2].append(row["collection"].strip())
        observations = []
        for rank, obs, superseded in chosen.values():
            if superseded:
                md = dict(obs.metadata)
                md["superseded_listings"] = measured(",".join(sorted(set(superseded))), value_class=ValueClass.CATEGORICAL,
                                                     origin=OriginInfo(kind=Origin.SOURCE_REPORTED, source=discovery_source))
                obs = Observation(obs_id=obs.obs_id, source_id=obs.source_id, provenance=obs.provenance, metadata=md)
            observations.append(obs)
        if failed:
            return DiscoveryResult(outcome=ResultOutcome.PARTIAL, observations=tuple(observations), provenance=prov,
                                   failed_items=tuple(failed))
        return DiscoveryResult(outcome=ResultOutcome.OK, observations=tuple(observations), provenance=prov)

    def _observation(self, row: dict[str, str], now: datetime, source: str = TAP_SOURCE) -> Observation:
        band = row["energy_bandpassname"].strip()
        m = re.fullmatch(r"SPHEREx-D([1-6])", band)
        if not m or not row["observationid"].strip() or not row["uri"].strip():
            raise ValueError(f"unexpected band or empty identifier in row ({band!r})")
        det = m.group(1)
        obs_id = f"{row['observationid'].strip()}D{det}"
        algo, _, digest = row["contentchecksum"].partition(":")
        product = SourceProduct(source_id=SOURCE_ID, product_id=obs_id, release=row["collection"].strip(),
                                product_version=row["provenance_version"].strip() or None,
                                checksum=digest or None, checksum_algorithm=(algo or None) if digest else None,
                                access_reference=row["uri"].strip())
        prov = Provenance(origin=DataOrigin.LIVE, retrieved_at=now, software_version=__version__, products=(product,))

        def src(value, vc, unit=None, unc=None):
            return measured(value, value_class=vc, unit=unit, uncertainty=unc,
                            origin=OriginInfo(kind=Origin.SOURCE_REPORTED, source=source))

        md = {
            "observation_id": src(row["observationid"].strip(), ValueClass.IDENTIFIER),
            "detector": src(det, ValueClass.CATEGORICAL),
            "release": src(row["collection"].strip(), ValueClass.CATEGORICAL),
            "time_start_mjd": src(float(row["time_bounds_lower"]), ValueClass.TIME, "d (MJD)", Uncertainty.not_applicable()),
            "time_end_mjd": src(float(row["time_bounds_upper"]), ValueClass.TIME, "d (MJD)", Uncertainty.not_applicable()),
            "wavelength_min": src(float(row["energy_bounds_lower"]) * 1e6, ValueClass.QUANTITY, "um", Uncertainty.unavailable()),
            "wavelength_max": src(float(row["energy_bounds_upper"]) * 1e6, ValueClass.QUANTITY, "um", Uncertainty.unavailable()),
        }
        if row.get("provenance_version", "").strip():
            md["pipeline_version"] = src(row["provenance_version"].strip(), ValueClass.CATEGORICAL)
        if row.get("contentlength", "").strip():
            md["file_size_bytes"] = src(int(float(row["contentlength"])), ValueClass.COUNT)
        return Observation(obs_id=obs_id, source_id=SOURCE_ID, provenance=prov, metadata=md)

    # -- retrieval ---------------------------------------------------------------

    def _lookup_uri(self, obs_id: str) -> str:
        m = _OBS.match(obs_id)
        if not m:
            raise HeimdallError.of(ErrorCode.INPUT_INVALID, "obs_id must look like <observationid>D<detector>", obs_id=obs_id)
        rows = self._tap("SELECT a.uri FROM spherex.plane p JOIN spherex.artifact a ON a.planeid = p.planeid "
                         "JOIN spherex.observation o ON o.obsid = p.obsid "
                         f"WHERE o.observationid = '{m.group('obs')}' AND p.energy_bandpassname = 'SPHEREx-D{m.group('det')}' "
                         "AND a.producttype = 'science'")
        if not rows:
            raise HeimdallError.of(ErrorCode.RETRIEVAL_FAILED, "no science product is listed for this obs_id", obs_id=obs_id)
        return rows[0]["uri"].strip()

    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        options = dict(request.options)
        uri = options.get("access_reference") or self._lookup_uri(request.obs_id)
        if uri.startswith(("http://", "https://")) or ".." in uri:
            raise HeimdallError.of(ErrorCode.INPUT_INVALID, "access_reference must be an archive-relative path")
        url = IRSA_BASE + uri.lstrip("/")
        size = options.get("cutout_size_deg")
        if size is None and request.region is not None:
            size = str(min(2.0 * request.region.radius_deg, 1.0))
        if size is not None:
            if request.region is None:
                raise HeimdallError.of(ErrorCode.INPUT_INVALID, "a cutout needs a region centre")
            s = _floats(str(size), "cutout_size_deg")
            url += f"?center={request.region.ra_deg:.8f},{request.region.dec_deg:.8f}d&size={s:.6f}"
        entry = self.cache.get(url)
        origin = DataOrigin.CACHED
        if entry is None:
            resp = self._http.request("GET", url)
            if not resp.body.startswith(b"SIMPLE  ="):
                raise HeimdallError.of(ErrorCode.DATA_MALFORMED, "archive response is not a FITS file", obs_id=request.obs_id)
            archive = options.get("archive_checksum")
            if size is None and archive and archive.startswith("md5:"):
                if hashlib.md5(resp.body).hexdigest() != archive.split(":", 1)[1]:
                    raise HeimdallError.of(ErrorCode.DATA_MALFORMED, "downloaded product fails the archive MD5 checksum")
            entry = self.cache.put(url, resp.body, {"obs_id": request.obs_id, "uri": uri, "cutout_size_deg": size})
            origin = DataOrigin.LIVE
        product = SourceProduct(source_id=SOURCE_ID, product_id=request.obs_id, release=options.get("release"),
                                product_version=options.get("product_version"), checksum=entry.sha256,
                                checksum_algorithm="sha256", access_reference=uri)
        prov = Provenance(origin=origin, retrieved_at=datetime.now(UTC), software_version=__version__, products=(product,),
                          notes=f"cutout size {size} deg" if size else "full product")
        return RetrievalResult(outcome=ResultOutcome.OK, obs_id=request.obs_id, provenance=prov,
                               products=(RetrievedProduct(product=product, location=str(entry.path), media_type="application/fits"),))

    def retrieve_calibration(self, kind: str, detector: int) -> tuple[Path, SourceProduct]:
        """Fetch (or reuse) a calibration product: 'epsf', 'spectral_wcs' (CWAVE/CBAND) or 'solid_angle'."""
        if kind not in CALIBRATIONS or detector not in range(1, 7):
            raise HeimdallError.of(ErrorCode.INPUT_INVALID, f"supported calibrations: {sorted(CALIBRATIONS)}, detector 1-6")
        path_template, release = CALIBRATIONS[kind]
        ref = path_template.format(det=detector)
        url = S3 + ref
        entry = self.cache.get(url)
        if entry is None:
            resp = self._http.request("GET", url)
            if not resp.body.startswith(b"SIMPLE  ="):
                raise HeimdallError.of(ErrorCode.DATA_MALFORMED, "calibration response is not a FITS file")
            entry = self.cache.put(url, resp.body, {"calibration": kind, "detector": detector})
        return entry.path, SourceProduct(source_id=SOURCE_ID, product_id=Path(ref).stem, release=release,
                                         checksum=entry.sha256, checksum_algorithm="sha256", access_reference=ref)
