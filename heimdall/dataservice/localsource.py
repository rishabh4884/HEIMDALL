"""A ``DataSource`` that serves ONLY verified local data: the science code's view of the data service. It never touches the network.

``discover`` answers from the inventory (refusing if the region is not indexed -- an unindexed region is not an empty one); ``retrieve`` and
``retrieve_calibration`` return files from the verified product store. Provenance reports origin ``CACHED`` ("stored locally"), with the
original acquisition origin and route kept in the notes, so stored data are never called live. Synthetic fixtures keep their synthetic origin.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from .. import __version__

from ..contracts import (DataOrigin, ErrorCode, HeimdallError, Observation, Origin, OriginInfo, Provenance, ResultOutcome, SourceProduct, Uncertainty,
                         ValueClass, measured)
from ..data_access.interface import (AccessReport, DataSource, DiscoveryQuery, DiscoveryResult, RetrievalRequest, RetrievalResult, RetrievedProduct,
                                     SourceDescription)
from .inventory import ObsRecord
from .schemas import PlanItem, RegionSpec, calibration_key
from .service import DataService

SOURCE_ID = "heimdall-local-store"


class LocalStoreSource(DataSource):
    def __init__(self, service: DataService, manifest: dict | None = None) -> None:
        self.service = service
        self.manifest = manifest  # when given, retrieval is restricted to (and verified against) this frozen manifest

    @property
    def source_id(self) -> str:
        return SOURCE_ID

    def describe(self) -> SourceDescription:
        return SourceDescription(source_id=SOURCE_ID, display_name="HEIMDALL verified local SPHEREx store (no network)", requires_credentials=False,
                                 capabilities_verified=True, notes="serves only inventory-indexed observations and SHA-256-verified local products")

    def check_access(self) -> AccessReport:
        return AccessReport(source_id=SOURCE_ID, checked_at=datetime.now(UTC), detail="local store; no network")

    # -- discovery ------------------------------------------------------------------------------
    def discover(self, query: DiscoveryQuery) -> DiscoveryResult:
        f = dict(query.filters)
        region = RegionSpec(query.region.ra_deg, query.region.dec_deg, query.region.radius_deg,
                            tuple(int(d) for d in f["detectors"].split(",")) if f.get("detectors") else None,
                            float(f["mjd_start"]) if f.get("mjd_start") else None, float(f["mjd_end"]) if f.get("mjd_end") else None, None)
        obs = self.service.observations(region)
        if obs is None:
            raise HeimdallError.of(ErrorCode.ARCHIVE_UNREACHABLE, "this region is not indexed in the local store (not the same as 'no observations')", offline="true")
        now = datetime.now(UTC)
        prov = Provenance(origin=DataOrigin.CACHED, retrieved_at=now, software_version=__version__, notes="local inventory (cached archive discovery), not a live query")
        if not obs:
            return DiscoveryResult(outcome=ResultOutcome.NO_OBSERVATIONS, observations=(), provenance=prov)
        return DiscoveryResult(outcome=ResultOutcome.OK, observations=tuple(self._observation(o, now) for o in obs), provenance=prov)

    def _observation(self, o: ObsRecord, now: datetime) -> Observation:
        def src(v, vc, unit=None, unc=None):
            return measured(v, value_class=vc, unit=unit, uncertainty=unc, origin=OriginInfo(kind=Origin.SOURCE_REPORTED, source=f"local inventory ({o.route or 'unknown route'})"))

        algo, _, digest_ = (o.archive_checksum or "").partition(":")
        prod = SourceProduct(source_id=SOURCE_ID, product_id=o.obs_id, release=o.release, product_version=o.product_version, checksum=digest_ or None,
                             checksum_algorithm=algo or None, access_reference=o.access_reference)
        origin = DataOrigin.SYNTHETIC_TEST_FIXTURE if o.synthetic else DataOrigin.CACHED
        md = {"observation_id": src(o.obs_id.rsplit("D", 1)[0], ValueClass.IDENTIFIER), "detector": src(str(o.detector), ValueClass.CATEGORICAL),
              "release": src(o.release, ValueClass.CATEGORICAL)}
        if o.t_min is not None:
            md["time_start_mjd"] = src(o.t_min, ValueClass.TIME, "d (MJD)", Uncertainty.not_applicable())
            md["time_end_mjd"] = src(o.t_max if o.t_max is not None else o.t_min, ValueClass.TIME, "d (MJD)", Uncertainty.not_applicable())
            if o.synthetic:
                md["mjd_avg_utc"] = src(0.5 * (o.t_min + (o.t_max if o.t_max is not None else o.t_min)), ValueClass.TIME, "d (MJD, UTC)", Uncertainty.not_applicable())
        if o.wl_min_um is not None:
            md["wavelength_min"] = src(o.wl_min_um, ValueClass.QUANTITY, "um", Uncertainty.unavailable())
            md["wavelength_max"] = src(o.wl_max_um, ValueClass.QUANTITY, "um", Uncertainty.unavailable())
        if o.source_version:
            md["pipeline_version"] = src(o.source_version, ValueClass.CATEGORICAL)
        return Observation(obs_id=o.obs_id, source_id=SOURCE_ID, provenance=Provenance(origin=origin, retrieved_at=now, software_version=__version__, products=(prod,)), metadata=md)

    # -- retrieval ------------------------------------------------------------------------------
    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        opts = dict(request.options)
        size = opts.get("cutout_size_deg")
        item = PlanItem(request.obs_id, opts.get("release", ""), 0, opts.get("access_reference", ""),
                        request.region.ra_deg if size and request.region else None, request.region.dec_deg if size and request.region else None,
                        float(size) if size else None)
        stored = self.service.store.get(item.key, count=True) if item.release else None
        if stored is None:
            stored = self.service.store.find(request.obs_id, item.key.split("|", 2)[2])
        if stored is None:
            raise HeimdallError.of(ErrorCode.RETRIEVAL_FAILED, "product is not in the verified local store", obs_id=request.obs_id, offline="true")
        if self.manifest is not None and stored.sha256 not in {p["sha256"] for p in self.manifest["products"]}:
            raise HeimdallError.of(ErrorCode.INPUT_INVALID, "product is not part of the frozen input manifest", obs_id=request.obs_id)
        origin = DataOrigin.SYNTHETIC_TEST_FIXTURE if stored.origin == "SYNTHETIC_TEST_FIXTURE" else DataOrigin.CACHED
        prod = SourceProduct(source_id=SOURCE_ID, product_id=request.obs_id, release=stored.release, checksum=stored.sha256, checksum_algorithm="sha256",
                             access_reference=stored.access_reference)
        prov = Provenance(origin=origin, retrieved_at=datetime.fromisoformat(stored.retrieved_at), software_version=__version__, products=(prod,),
                          notes=f"verified local store object; originally acquired {stored.origin} via {stored.route}; manifest-pinned SHA-256")
        return RetrievalResult(outcome=ResultOutcome.OK, obs_id=request.obs_id, provenance=prov,
                               products=(RetrievedProduct(product=prod, location=str(stored.path), media_type="application/fits"),))

    def retrieve_calibration(self, kind: str, detector: int) -> tuple[Path, SourceProduct]:
        stored = self.service.store.get(calibration_key(kind, detector), count=True)
        if stored is None:
            raise HeimdallError.of(ErrorCode.RETRIEVAL_FAILED, f"calibration {kind} D{detector} is not in the verified local store", offline="true")
        return stored.path, SourceProduct(source_id=SOURCE_ID, product_id=Path(stored.access_reference or kind).stem, release=None, checksum=stored.sha256,
                                          checksum_algorithm="sha256", access_reference=stored.access_reference)
