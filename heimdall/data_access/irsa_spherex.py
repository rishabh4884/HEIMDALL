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
from .interface import (AccessReport, DataSource, DiscoveryQuery, DiscoveryResult, RetrievalRequest, RetrievalResult,
                        RetrievedProduct, SourceDescription)

SOURCE_ID = "irsa-spherex"
IRSA_BASE = "https://irsa.ipac.caltech.edu/"
TAP_SYNC = IRSA_BASE + "TAP/sync"
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
REQUIRED_COLUMNS = ("collection", "observationid", "energy_bandpassname", "energy_bounds_lower", "energy_bounds_upper",
                    "time_bounds_lower", "time_bounds_upper", "provenance_version", "uri", "contentchecksum")
_NAME = re.compile(r"^[a-z0-9_]+$")
_OBS = re.compile(r"^(?P<obs>[A-Za-z0-9_]+)D(?P<det>[1-6])$")


def _floats(text: str, name: str) -> float:
    try:
        return float(text)
    except ValueError:
        raise HeimdallError.of(ErrorCode.INPUT_INVALID, f"filter {name} must be a number", filter=name) from None


class IrsaSpherexSource(DataSource):
    def __init__(self, settings: Settings, transport: Transport | None = None, sleep=time.sleep) -> None:
        self._settings = settings
        self._http = HttpClient(SOURCE_ID, settings.request_timeout_s, settings.max_retries, transport, sleep,
                                offline=settings.offline)
        self.cache = ProductCache(Path(settings.cache_dir) / SOURCE_ID)
        self.last_tap_from_cache = False

    @property
    def source_id(self) -> str:
        return SOURCE_ID

    def describe(self) -> SourceDescription:
        return SourceDescription(
            source_id=SOURCE_ID, display_name="NASA/IPAC IRSA: SPHEREx Level 2 spectral images (public archive)",
            requires_credentials=False, capabilities_verified=False,
            notes="TAP discovery, cutout retrieval and QR3 ePSF access are supported; capability verification remains "
                  "conservative until automated live conformance completes.")

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
            resp = self._http.request("POST", TAP_SYNC, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
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
        return rows

    def check_access(self) -> AccessReport:
        rows = self._tap("SELECT table_name FROM TAP_SCHEMA.tables WHERE table_name = 'spherex.plane'")
        if not rows:
            raise HeimdallError.of(ErrorCode.ACCESS_DENIED, "spherex.plane is not visible through IRSA TAP")
        return AccessReport(source_id=SOURCE_ID, checked_at=datetime.now(UTC),
                            detail="IRSA TAP reachable; spherex.plane visible; no credentials required")

    def _where(self, query: DiscoveryQuery) -> list[str]:
        r = query.region
        clauses = [f"1 = CONTAINS(POINT('ICRS', {r.ra_deg:.10f}, {r.dec_deg:.10f}), p.poly)", "a.producttype = 'science'"]
        f = dict(query.filters)
        collections = [c for c in (f.get("collections", "").split(",") + ([query.release] if query.release else [])) if c]
        for c in collections:
            if not _NAME.match(c):
                raise HeimdallError.of(ErrorCode.INPUT_INVALID, "collection names may contain only a-z, 0-9 and _", filter="collections")
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

    def discover(self, query: DiscoveryQuery) -> DiscoveryResult:
        adql = ("SELECT o.collection, o.observationid, p.energy_bandpassname, p.energy_bounds_lower, p.energy_bounds_upper, "
                "p.time_bounds_lower, p.time_bounds_upper, p.provenance_version, a.uri, a.contentchecksum, a.contentlength "
                "FROM spherex.plane p JOIN spherex.artifact a ON a.planeid = p.planeid "
                "JOIN spherex.observation o ON o.obsid = p.obsid WHERE " + " AND ".join(self._where(query)) +
                " ORDER BY p.time_bounds_lower, p.energy_bandpassname")
        rows = self._tap(adql)
        now = datetime.now(UTC)
        origin = DataOrigin.CACHED if self.last_tap_from_cache else DataOrigin.LIVE
        prov = Provenance(origin=origin, retrieved_at=now, software_version=__version__,
                          notes="discovery metadata from IRSA TAP" + (" (cached response, offline)" if self.last_tap_from_cache else ""))
        if not rows:
            return DiscoveryResult(outcome=ResultOutcome.NO_OBSERVATIONS, observations=(), provenance=prov)
        missing = [c for c in REQUIRED_COLUMNS if c not in rows[0]]
        if missing:
            raise HeimdallError.of(ErrorCode.DATA_MALFORMED, f"TAP response lacks columns: {missing}")
        failed = []
        # One exposure of one detector may be listed by several releases (e.g. QR2 and QR3 reprocessing). Counting
        # both would double the evidence, so the newest release is kept and the others are recorded on it.
        chosen: dict[str, tuple[int, Observation, list[str]]] = {}
        for i, row in enumerate(rows):
            try:
                obs = self._observation(row, now)
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
                                                     origin=OriginInfo(kind=Origin.SOURCE_REPORTED, source=TAP_SOURCE))
                obs = Observation(obs_id=obs.obs_id, source_id=obs.source_id, provenance=obs.provenance, metadata=md)
            observations.append(obs)
        if failed:
            return DiscoveryResult(outcome=ResultOutcome.PARTIAL, observations=tuple(observations), provenance=prov,
                                   failed_items=tuple(failed))
        return DiscoveryResult(outcome=ResultOutcome.OK, observations=tuple(observations), provenance=prov)

    def _observation(self, row: dict[str, str], now: datetime) -> Observation:
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
                            origin=OriginInfo(kind=Origin.SOURCE_REPORTED, source=TAP_SOURCE))

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
