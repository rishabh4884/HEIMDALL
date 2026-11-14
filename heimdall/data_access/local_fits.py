"""Adapter for SPHEREx Level 2 FITS files in a local directory.

Use it for offline, reproducible runs on files downloaded earlier (for example
from IRSA) and for synthetic demonstration products. Discovery reads headers
only: ``OBSID``/``DETECTOR``/``MJD-AVG`` and the celestial WCS footprint of the
IMAGE extension. A file is reported when the query region overlaps its
footprint.

Products marked synthetic (``SYNTHETC = T`` or ``OBSID`` starting with
``SYNTHETIC``) get origin ``SYNTHETIC_TEST_FIXTURE``; real files get
``CACHED``. A directory that mixes both is rejected, so synthetic data can
never be presented next to, or as, real observations.
"""

from __future__ import annotations

import logging
import math
import warnings
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS, FITSFixedWarning

from .. import __version__
from ..config import Settings
from ..contracts import (DataOrigin, ErrorCode, ErrorInfo, FailedItem, HeimdallError, Observation, Origin, OriginInfo,
                         Provenance, ResultOutcome, SourceProduct, ValueClass, measured)
from .cache import sha256_file
from .interface import (AccessReport, DataSource, DiscoveryQuery, DiscoveryResult, RetrievalRequest, RetrievalResult,
                        RetrievedProduct, SourceDescription)

SOURCE_ID = "local-fits"
HEADER_SOURCE = "local SPHEREx L2 FITS header"


def _quiet_wcs(header) -> WCS:
    logger = logging.getLogger("astropy")
    level = logger.level
    logger.setLevel(logging.ERROR)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FITSFixedWarning)
            return WCS(header).celestial
    finally:
        logger.setLevel(level)


def _sep_deg(ra1, dec1, ra2, dec2):
    r1, d1, r2, d2 = map(np.radians, (ra1, dec1, ra2, dec2))
    c = np.sin(d1) * np.sin(d2) + np.cos(d1) * np.cos(d2) * np.cos(r2 - r1)
    return np.degrees(np.arccos(np.clip(c, -1.0, 1.0)))


def read_header_summary(path: Path) -> dict:
    """Header facts needed for discovery. Raises ValueError for unusable files."""
    with fits.open(path, memmap=True) as hdul:
        names = {h.name for h in hdul}
        if "IMAGE" not in names:
            raise ValueError("no IMAGE extension")
        primary, ih = hdul[0].header, hdul["IMAGE"].header
        nx, ny = int(ih.get("NAXIS1", 0)), int(ih.get("NAXIS2", 0))
        if nx <= 0 or ny <= 0:
            raise ValueError("IMAGE has no data")
        wcs = _quiet_wcs(ih)
        if wcs.naxis != 2:
            raise ValueError("IMAGE has no celestial WCS")
        t = np.linspace(0.0, 1.0, 12, endpoint=False)
        xs = np.concatenate([-0.5 + t * nx, np.full(12, nx - 0.5), nx - 0.5 - t * nx, np.full(12, -0.5)])
        ys = np.concatenate([np.full(12, -0.5), -0.5 + t * ny, np.full(12, ny - 0.5), ny - 0.5 - t * ny])
        ra, dec = wcs.pixel_to_world_values(xs, ys)
        cra, cdec = wcs.pixel_to_world_values((nx - 1) / 2.0, (ny - 1) / 2.0)
        obsid = str(ih.get("OBSID", "")).strip()
        det = ih.get("DETECTOR")
        mjd = ih.get("MJD-AVG", ih.get("MJD-OBS"))
        return {
            "observation_id": obsid or None, "detector": int(det) if isinstance(det, (int, np.integer)) else None,
            "mjd": float(mjd) if isinstance(mjd, (int, float)) and math.isfinite(float(mjd)) else None,
            "synthetic": bool(primary.get("SYNTHETC", False)) or obsid.startswith("SYNTHETIC"),
            "version": str(primary.get("VERSION")) if primary.get("VERSION") else None,
            "boundary_ra": np.asarray(ra) % 360.0, "boundary_dec": np.asarray(dec),
            "centre": (float(cra) % 360.0, float(cdec)),
            "radius_deg": float(np.max(_sep_deg(cra, cdec, ra, dec))),
        }


class LocalFitsSource(DataSource):
    def __init__(self, settings: Settings, root: Path | None = None) -> None:
        self._root = Path(root) if root is not None else (Path(settings.local_fits_dir) if settings.local_fits_dir else None)
        self._paths: dict[str, Path] = {}

    @property
    def source_id(self) -> str:
        return SOURCE_ID

    def describe(self) -> SourceDescription:
        return SourceDescription(source_id=SOURCE_ID, display_name="Local SPHEREx L2 FITS directory",
                                 requires_credentials=False, capabilities_verified=False,
                                 notes=f"root: {self._root or '(not set; HEIMDALL_LOCAL_FITS_DIR)'}")

    def _require_root(self) -> Path:
        if self._root is None:
            raise HeimdallError.of(ErrorCode.SOURCE_NOT_CONFIGURED, "no local FITS directory configured",
                                   variable="HEIMDALL_LOCAL_FITS_DIR")
        if not self._root.is_dir():
            raise HeimdallError.of(ErrorCode.ARCHIVE_UNREACHABLE, "local FITS directory does not exist", path=str(self._root))
        return self._root

    def check_access(self) -> AccessReport:
        root = self._require_root()
        n = len(list(root.glob("*.fits")))
        return AccessReport(source_id=SOURCE_ID, checked_at=datetime.now(UTC), detail=f"{n} FITS file(s) in {root}")

    def discover(self, query: DiscoveryQuery) -> DiscoveryResult:
        root = self._require_root()
        r = query.region
        f = dict(query.filters)
        dets = {int(d) for d in f.get("detectors", "").split(",") if d.strip()} if f.get("detectors") else None
        mjd_start = float(f["mjd_start"]) if f.get("mjd_start") else None
        mjd_end = float(f["mjd_end"]) if f.get("mjd_end") else None
        now = datetime.now(UTC)
        observations, failed, origins = [], [], set()
        for path in sorted(root.glob("*.fits")):
            try:
                h = read_header_summary(path)
            except (OSError, ValueError, TypeError) as exc:
                failed.append(FailedItem(ref=path.name, error=ErrorInfo(code=ErrorCode.DATA_MALFORMED, message=f"unreadable: {exc}")))
                continue
            if _sep_deg(r.ra_deg, r.dec_deg, *h["centre"]) > r.radius_deg + h["radius_deg"]:
                continue
            inside = float(np.min(_sep_deg(r.ra_deg, r.dec_deg, h["boundary_ra"], h["boundary_dec"]))) <= r.radius_deg
            centre_in = _sep_deg(r.ra_deg, r.dec_deg, *h["centre"]) <= h["radius_deg"]
            if not (inside or centre_in):
                continue
            if dets is not None and h["detector"] not in dets:
                continue
            if h["mjd"] is not None and ((mjd_start is not None and h["mjd"] < mjd_start) or (mjd_end is not None and h["mjd"] > mjd_end)):
                continue
            obs_id = f"{h['observation_id']}D{h['detector']}" if h["observation_id"] and h["detector"] else path.stem
            origin = DataOrigin.SYNTHETIC_TEST_FIXTURE if h["synthetic"] else DataOrigin.CACHED
            origins.add(origin)
            product = SourceProduct(source_id=SOURCE_ID, product_id=obs_id, product_version=h["version"], access_reference=path.name)
            prov = Provenance(origin=origin, retrieved_at=now, software_version=__version__, products=(product,),
                              notes="SYNTHETIC: not astronomical data" if h["synthetic"] else "local copy of an archive product")
            md = {}
            if h["detector"] is not None:
                md["detector"] = measured(str(h["detector"]), value_class=ValueClass.CATEGORICAL,
                                          origin=OriginInfo(kind=Origin.SOURCE_REPORTED, source=HEADER_SOURCE))
            if h["mjd"] is not None:
                md["mjd_avg_utc"] = measured(h["mjd"], value_class=ValueClass.TIME, unit="d (MJD, UTC)",
                                             origin=OriginInfo(kind=Origin.SOURCE_REPORTED, source=HEADER_SOURCE))
            self._paths[obs_id] = path
            observations.append(Observation(obs_id=obs_id, source_id=SOURCE_ID, provenance=prov, metadata=md))
        if len(origins) > 1:
            raise HeimdallError.of(ErrorCode.INPUT_INVALID, "directory mixes synthetic and real products; keep them apart",
                                   path=str(root))
        origin = origins.pop() if origins else DataOrigin.CACHED
        prov = Provenance(origin=origin, retrieved_at=now, software_version=__version__, notes=f"scan of {root}")
        if failed:
            return DiscoveryResult(outcome=ResultOutcome.PARTIAL, observations=tuple(observations), provenance=prov,
                                   failed_items=tuple(failed))
        if not observations:
            return DiscoveryResult(outcome=ResultOutcome.NO_OBSERVATIONS, observations=(), provenance=prov)
        return DiscoveryResult(outcome=ResultOutcome.OK, observations=tuple(observations), provenance=prov)

    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        root = self._require_root()
        path = self._paths.get(request.obs_id)
        if path is None:
            ref = dict(request.options).get("access_reference")
            candidate = root / ref if ref else None
            if candidate is None or not candidate.is_file() or candidate.resolve().parent != root.resolve():
                raise HeimdallError.of(ErrorCode.RETRIEVAL_FAILED, "file for this obs_id is not known; run discovery first",
                                       obs_id=request.obs_id)
            path = candidate
        try:
            h = read_header_summary(path)
        except (OSError, ValueError, TypeError) as exc:
            raise HeimdallError.of(ErrorCode.DATA_MALFORMED, f"unreadable product: {exc}", obs_id=request.obs_id) from None
        origin = DataOrigin.SYNTHETIC_TEST_FIXTURE if h["synthetic"] else DataOrigin.CACHED
        product = SourceProduct(source_id=SOURCE_ID, product_id=request.obs_id, product_version=h["version"],
                                checksum=sha256_file(path), checksum_algorithm="sha256", access_reference=path.name)
        prov = Provenance(origin=origin, retrieved_at=datetime.now(UTC), software_version=__version__, products=(product,))
        return RetrievalResult(outcome=ResultOutcome.OK, obs_id=request.obs_id, provenance=prov,
                               products=(RetrievedProduct(product=product, location=str(path), media_type="application/fits"),))
