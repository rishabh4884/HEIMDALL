"""Programmatic interface to the HEIMDALL scientific engine.

The command line (``python3 -m heimdall``) and the future user interface use
this API; neither recomputes science. Every method returns plain
JSON-compatible data (see docs/OUTPUT_SCHEMA.md) or raises ``HeimdallError``.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

from . import __version__
from .config import Settings, load_settings
from .contracts import ErrorCode, HeimdallError, ResultOutcome
from .data_access.interface import DataSource, DiscoveryQuery, SkyRegion
from .data_access.registry import AdapterRegistry, default_registry
from .engine.config import EngineConfig
from .engine.investigation import build as investigation_build
from .engine.temporal import build_epochs, select_time_window
from .engine.validation import ValidationRegistry, self_checks
from .pipeline.results import plain
from .pipeline.runner import InvestigationPipeline, InvestigationRequest, RunStore
from .skymap.layers import build_skymap
from .skymap.svg import render_svg

SECTIONS = ("summary", "stages", "observations", "epochs", "detections", "sources", "associations", "transients", "tracks",
            "candidates", "investigations", "planet_x", "validation", "playback", "provenance", "unavailable", "events", "failed", "warnings")


class Engine:
    def __init__(self, settings: Settings | None = None, config: EngineConfig | None = None,
                 adapters: AdapterRegistry | None = None) -> None:
        self.settings = settings or load_settings()
        self.config = config or EngineConfig()
        self.adapters = adapters or default_registry
        state = Path(self.settings.state_dir)
        self.store = RunStore(state / "runs")
        self.validation = ValidationRegistry(state / "validation")

    @classmethod
    def from_environment(cls, env_file: Path | None = None, config_path: Path | None = None) -> Engine:
        settings = load_settings(env_file=env_file if env_file and Path(env_file).is_file() else None)
        config = EngineConfig.load(config_path) if config_path else None
        return cls(settings=settings, config=config)

    # -- sources ---------------------------------------------------------------

    def source(self, source_id: str, local_root: str | None = None) -> DataSource:
        if source_id == "local-fits" and local_root:
            from .data_access.local_fits import LocalFitsSource
            return LocalFitsSource(self.settings, root=Path(local_root))
        return self.adapters.create(self.settings, source_id)

    def sources(self) -> list[dict]:
        out = []
        for sid in self.adapters.available():
            try:
                desc = plain(self.adapters.create(self.settings, sid).describe())
            except HeimdallError as exc:
                desc = {"source_id": sid, "error": plain(exc.info)}
            desc["configured_default"] = sid == self.settings.data_source
            out.append(desc)
        return out

    def check_access(self, source_id: str, local_root: str | None = None) -> dict:
        return plain(self.source(source_id, local_root).check_access())

    def coverage(self, source_id: str, ra: float, dec: float, radius: float, *, detectors: list[int] | None = None,
                 collections: list[str] | None = None, mjd_start: float | None = None, mjd_end: float | None = None,
                 local_root: str | None = None, live: bool = False) -> dict:
        """Observations covering a region: discovery only, no pixels are retrieved.

        For ``irsa-spherex`` this is readiness-first: the answer comes from the local index (with its index time), or the call returns a
        durable INDEX job (``outcome: "PREPARING"``) -- an archive outage is never an error here and never ``NO_OBSERVATIONS``.
        ``live=True`` is the explicit operator diagnostic that queries the archive directly (also used for non-current collections such as
        QR2, which the index never holds)."""
        from .dataservice.schemas import CURRENT_COLLECTIONS
        if source_id == "irsa-spherex" and not live and (not collections or all(c in CURRENT_COLLECTIONS for c in collections)):
            return self._indexed_coverage(source_id, ra, dec, radius, detectors, collections, mjd_start, mjd_end)
        filters = {}
        if detectors:
            filters["detectors"] = ",".join(str(int(d)) for d in detectors)
        if collections:
            filters["collections"] = ",".join(collections)
        if mjd_start is not None:
            filters["mjd_start"] = repr(float(mjd_start))
        if mjd_end is not None:
            filters["mjd_end"] = repr(float(mjd_end))
        result = self.source(source_id, local_root).discover(
            DiscoveryQuery(region=SkyRegion(ra_deg=ra % 360.0, dec_deg=dec, radius_deg=radius), filters=filters))
        rows = []
        for o in result.observations:
            md = o.metadata

            def val(key):
                v = md.get(key)
                return v.value if v is not None and v.is_available else None

            mjd = val("mjd_avg_utc") if val("mjd_avg_utc") is not None else val("time_start_mjd")
            rows.append({"obs_id": o.obs_id, "detector": val("detector"), "release": val("release"), "mjd": mjd,
                         "wavelength_min_um": val("wavelength_min"), "wavelength_max_um": val("wavelength_max"),
                         "pipeline_version": val("pipeline_version"), "synthetic": o.is_synthetic})
        epochs, _ = build_epochs([(r["obs_id"], r["mjd"]) for r in rows], self.config.temporal.epoch_gap_days)
        per_detector: dict[str, int] = {}
        for r in rows:
            per_detector[str(r["detector"])] = per_detector.get(str(r["detector"]), 0) + 1
        return {"source_id": source_id, "region": {"ra_deg": ra, "dec_deg": dec, "radius_deg": radius}, "filters": filters,
                "outcome": result.outcome.value, "n_observations": len(rows), "per_detector": per_detector,
                "epochs": plain(epochs), "observations": rows, "failed_items": plain(result.failed_items),
                "note": "NO_OBSERVATIONS means the query succeeded and found none; failures raise errors instead"
                if result.outcome is ResultOutcome.NO_OBSERVATIONS else ""}

    def _indexed_coverage(self, source_id, ra, dec, radius, detectors, collections, mjd_start, mjd_end) -> dict:
        from .dataservice.schemas import RegionSpec
        region = RegionSpec(ra % 360.0, dec, radius, tuple(int(d) for d in detectors) if detectors else None, mjd_start, mjd_end,
                            tuple(collections) if collections else None)
        st = self.data_service(source_id).coverage(region, limit=2000)
        base = {"source_id": source_id, "region": {"ra_deg": ra, "dec_deg": dec, "radius_deg": radius}, "access": "LOCAL_INDEX",
                "readiness": st["readiness"], "data_status": {k: st.get(k) for k in ("schema", "readiness", "message", "job", "must_resubmit")},
                "filters": {"detectors": detectors, "collections": collections, "mjd_start": mjd_start, "mjd_end": mjd_end}, "failed_items": []}
        if st.get("observations") is None:
            return base | {"outcome": "PREPARING" if st["readiness"] == "PREPARING" else st["readiness"], "n_observations": None, "per_detector": {},
                           "epochs": [], "observations": [],
                           "note": "region not indexed yet: a durable INDEX job was created or joined (run a worker); this is not 'no observations'"}
        rows = [{"obs_id": o["obs_id"], "detector": o["detector"], "release": o["release"],
                 "mjd": (o["t_min_mjd"] + o["t_max_mjd"]) / 2.0 if o["t_min_mjd"] is not None and o["t_max_mjd"] is not None else o["t_min_mjd"],
                 "wavelength_min_um": o["wavelength_min_um"], "wavelength_max_um": o["wavelength_max_um"], "pipeline_version": None,
                 "synthetic": o["synthetic"]} for o in st["observations"]]
        epochs, _ = build_epochs([(r["obs_id"], r["mjd"]) for r in rows], self.config.temporal.epoch_gap_days)
        per_detector: dict[str, int] = {}
        for r in rows:
            per_detector[str(r["detector"])] = per_detector.get(str(r["detector"]), 0) + 1
        n = st["n_observations"]
        return base | {"outcome": "OK" if n else "NO_OBSERVATIONS", "n_observations": n, "per_detector": per_detector, "per_release": st["per_release"],
                       "epochs": plain(epochs), "observations": rows, "index_as_of": st["index_as_of"],
                       "note": (f"from the local index as of {st['index_as_of']} (not a live query)"
                                + ("" if n else "; NO_OBSERVATIONS means a successful archive search found none")
                                + (f"; first {len(rows)} of {n} listed" if len(rows) < n else ""))}

    # -- investigations ------------------------------------------------------------

    def investigate(self, request: InvestigationRequest, *, progress=None, save: bool = True, svg: bool = True,
                    out_dir: Path | None = None, live: bool = False) -> tuple[dict, Path | None]:
        """Run the blind investigation pipeline on a region.

        For ``irsa-spherex`` (unless ``live``) this is readiness-first: the region's products are prefetched by the data service, a manifest
        is frozen, and the pipeline runs only on those verified local products with catalogue context read from cache (no network). If the
        data are not yet local, ``(data_status, None)`` is returned with a durable job id instead of a result (never an archive error)."""
        from .dataservice.schemas import CURRENT_COLLECTIONS
        readiness = (request.source_id == "irsa-spherex" and not live
                     and (not request.collections or all(c in CURRENT_COLLECTIONS for c in request.collections)))
        input_manifest = None
        if readiness:
            import dataclasses
            from .dataservice.localsource import LocalStoreSource
            from .dataservice.schemas import RegionSpec
            if request.calibration_products != "epsf":
                raise HeimdallError.of(ErrorCode.INPUT_INVALID, "calibration_products=full is not held by the data service; use live=True (--live)")
            svc = self.data_service(request.source_id)
            region = RegionSpec(request.ra_deg % 360.0, request.dec_deg, request.radius_deg, tuple(request.detectors) if request.detectors else None,
                                request.mjd_start, request.mjd_end, tuple(request.collections) if request.collections else None)
            st = svc.prefetch(region, cutout_size_deg=request.cutout_size_deg, max_observations=request.max_observations or 16)
            if st["readiness"] != "READY":
                return st | {"investigation": None, "note": "no scientific result yet: the region's data are being prepared by a durable job "
                                                             "(run a worker); this is not a non-detection"}, None
            input_manifest = svc.manifest(st["manifest_id"])
            request = dataclasses.replace(request, ra_deg=region.ra_deg, obs_ids=[p["obs_id"] for p in input_manifest["products"]])
            source = LocalStoreSource(svc, input_manifest)
            settings = dataclasses.replace(self.settings, offline=True)  # context from cache only; nothing on this path uses the network
        else:
            source = self.source(request.source_id, request.local_root)
            settings = self.settings
        pipeline = InvestigationPipeline(source, self.config, settings, self.validation, progress)
        result = pipeline.run(request)
        if input_manifest is not None:
            result["input_manifest"] = {"manifest_id": input_manifest["manifest_id"], "products": input_manifest["products"],
                                        "calibrations": input_manifest["calibrations"], "index": input_manifest.get("index")}
            result["data_status"] = {"readiness": "READY", "served_from": "verified local data (no archive request)",
                                     "manifest_id": input_manifest["manifest_id"]}
        run_dir = None
        if save:
            skymap = build_skymap(result)
            run_dir = self.store.save(result, skymap, out_dir)
            if svg:
                (run_dir / "skymap.svg").write_text(render_svg(skymap), encoding="utf-8")
        return result, run_dir

    # -- named and coordinate-addressable targets ---------------------------------------

    def targets(self) -> list[dict]:
        """The registered NASA test targets and any saved target investigation for each."""
        from .targets.registry import NASA_TARGETS
        out = []
        for tid, spec in NASA_TARGETS.items():
            saved = self.target_result(tid, missing_ok=True)
            out.append({"target_id": tid, "name": spec.name, "category": spec.category, "description": spec.description,
                        "saved": None if saved is None else {"investigation_id": saved["investigation_id"], "status": saved["status"],
                                                             "created_at": saved["created_at"]}})
        return out

    def target_result(self, target_id: str, missing_ok: bool = False) -> dict | None:
        path = Path(self.settings.state_dir) / "targets" / target_id / "latest.json"
        if not path.is_file():
            if missing_ok:
                return None
            raise HeimdallError.of(ErrorCode.INPUT_INVALID, f"no saved investigation for target {target_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def resolve_target(self, ref: str | None = None, *, ra: float | None = None, dec: float | None = None, category: str = "MOVING_OBJECT") -> dict:
        """Reference astrometry of a registered target or coordinate: no SPHEREx data is retrieved."""
        from .context import CachedService
        from .targets import astrometry
        from .targets.registry import TargetSpec, get_target
        spec = self._target_spec(ref, ra, dec, category)
        _ = (TargetSpec, get_target)
        gaia = CachedService("gaia", self.settings)
        cat = CachedService("catwise", self.settings)
        return astrometry.resolve(spec, cat if spec.catalogue == "catwise2020" else gaia, gaia_service=gaia).to_dict()

    @staticmethod
    def _target_spec(ref: str | None, ra: float | None, dec: float | None, category: str):
        from .targets.registry import TargetSpec, get_target
        if ref:
            try:
                return get_target(ref)
            except KeyError as exc:
                raise HeimdallError.of(ErrorCode.INPUT_INVALID, str(exc)) from None
        if ra is None or dec is None:
            raise HeimdallError.of(ErrorCode.INPUT_INVALID, "give a target id/name or --ra and --dec")
        return TargetSpec.from_coordinates(ra, dec, category=category)

    def investigate_target(self, ref: str | None = None, *, ra: float | None = None, dec: float | None = None, category: str = "MOVING_OBJECT",
                           source_id: str = "irsa-spherex", local_root: str | None = None, progress=None, cancel=None, **options) -> dict:
        """Run the target investigation (see heimdall.targets.investigate). ``options`` are TargetOptions fields."""
        from .targets.investigate import TargetOptions, investigate_target
        spec = self._target_spec(ref, ra, dec, category)
        wait_s = float(options.pop("wait_s", 0.0) or 0.0)
        analyse_inline = bool(options.pop("analyse_inline", True))
        opts = TargetOptions(**{k: v for k, v in options.items() if v is not None})
        if source_id == "irsa-spherex":
            # Readiness-first: the live archive is never on this call's critical path. Ready -> result now; otherwise a durable job that owns the retries.
            from .targets import prepare
            return prepare.request_investigation(self.data_service(source_id), spec, opts, settings=self.settings, config=self.config, registry=self.validation,
                                                 store=self.store, wait_s=wait_s, progress=progress, analyse_inline=analyse_inline)
        return investigate_target(spec, source=self.source(source_id, local_root), settings=self.settings, config=self.config, registry=self.validation,
                                  store=self.store, options=opts, progress=progress, cancel=cancel)

    # -- data service (readiness-first access to verified local SPHEREx data) -------------------------
    def data_service(self, source_id: str = "irsa-spherex"):
        """The durable data service (inventory, verified store, jobs). Offline settings give a service that never touches the archive."""
        from .dataservice.schemas import ServiceConfig
        from .dataservice.service import DataService
        from .targets import prepare
        if getattr(self, "_ds", None) is None:
            source = None if self.settings.offline else self.source(source_id)
            self._ds = DataService(Path(self.settings.state_dir) / "dataservice", source, ServiceConfig.from_env(), offline=self.settings.offline)
            self._ds.handlers[prepare.KIND_TARGET] = prepare.make_handler(settings=self.settings, config=self.config, registry=self.validation, store=self.store)
        return self._ds

    def investigation_status(self, job_id: str) -> dict:
        from .targets import prepare
        return prepare.investigation_status(self.data_service(), job_id)

    def runs(self) -> list[dict]:
        return self.store.list()

    def load(self, ref: str | Path) -> dict:
        return self.store.load(ref)

    def inspect(self, ref: str | Path, section: str = "summary", item_id: str | None = None) -> Any:
        r = self.load(ref)
        if section not in SECTIONS:
            raise HeimdallError.of(ErrorCode.INPUT_INVALID, f"unknown section; choose one of {', '.join(SECTIONS)}")
        if section == "summary":
            return summarize(r)
        if section == "investigations":
            inv = r.get("investigations") or self.reinvestigate(r)
            if item_id is None:
                return {k: v for k, v in inv.items() if k != "investigations"} | {"summaries": [_investigation_summary(i) for i in inv["investigations"]]}
            for i in inv["investigations"]:
                if item_id in (i["investigation_id"], i["subject"]["id"]) or item_id in i["candidate_ids"]:
                    return i
            raise HeimdallError.of(ErrorCode.INPUT_INVALID, f"no investigation {item_id!r}")
        key = {"unavailable": "unavailable_analyses", "failed": "failed_items"}.get(section, section)
        data = r.get(key)
        if item_id is not None and isinstance(data, list):
            for k in ("candidate_id", "source_id", "det_id", "track_id", "obs_id", "run_id", "subject_id"):
                hits = [x for x in data if isinstance(x, dict) and x.get(k) == item_id]
                if hits:
                    return hits[0]
            raise HeimdallError.of(ErrorCode.INPUT_INVALID, f"no item {item_id!r} in section {section}")
        return data

    def reinvestigate(self, ref: str | Path | dict, out: Path | None = None) -> dict:
        """Rebuild the investigations of a saved run from its stored results. No detection, ingestion or download is run."""
        r = ref if isinstance(ref, dict) else self.load(ref)
        inv = investigation_build(r)
        inv["source_run"] = {"run_id": r.get("run_id"), "results_hash": r.get("results_hash"),
                             "note": "rebuilt offline from the saved structured results; the saved run is not modified"}
        if out is not None:
            Path(out).write_text(json.dumps(inv, indent=1, sort_keys=True, default=str), encoding="utf-8")
        return inv

    def reproduce(self, ref: str | Path) -> dict:
        """Re-run a stored investigation with its recorded request and configuration and compare results hashes."""
        original = self.load(ref)
        request = InvestigationRequest.from_dict(original["request"])
        engine = Engine(self.settings, EngineConfig.from_dict(original["config"]), self.adapters)
        # the original access path (archive adapter with its own cache-first replay), so the observation selection and hash are comparable
        rerun, run_dir = engine.investigate(request, live=True)
        return {"original_run": original["run_id"], "rerun": rerun["run_id"], "rerun_dir": str(run_dir) if run_dir else None,
                "original_hash": original["results_hash"], "rerun_hash": rerun["results_hash"],
                "identical": original["results_hash"] == rerun["results_hash"],
                "note": "the hash covers inputs, configuration, measurements, statistics and categories (not timestamps)"}

    def validate(self, ref: str | Path) -> dict:
        """Re-run a stored investigation with every validation procedure enabled."""
        original = self.load(ref)
        request = InvestigationRequest.from_dict({**original["request"], "run_validation": True})
        engine = Engine(self.settings, EngineConfig.from_dict(original["config"]), self.adapters)
        result, run_dir = engine.investigate(request, live=True)  # same access path as the stored run
        return {"run_id": result["run_id"], "run_dir": str(run_dir) if run_dir else None,
                "validation": result["validation"], "summary": summarize(result)}

    @staticmethod
    def self_check() -> list[dict]:
        return self_checks()

    # -- views ---------------------------------------------------------------------

    def skymap(self, ref: str | Path, viewport: tuple[float, float, float] | None = None) -> dict:
        r = self.load(ref)
        if not r.get("investigations"):  # runs saved before investigation schema 1.1: rebuild offline so the layer metadata is present
            r = {**r, "investigations": self.reinvestigate(r)}
        return build_skymap(r, viewport)

    def playback(self, ref: str | Path, mjd_start: float | None = None, mjd_end: float | None = None) -> dict:
        pb = dict(self.load(ref).get("playback") or {})
        frames = pb.get("frames", [])
        keep, excluded = select_time_window([(f["obs_id"], f["mjd"]) for f in frames], mjd_start, mjd_end)
        pb["frames"] = [f for f in frames if f["obs_id"] in set(keep)]
        pb["window"] = {"mjd_start": mjd_start, "mjd_end": mjd_end, "excluded": [{"obs_id": o, "reason": r} for o, r in excluded]}
        return pb

    def export(self, ref: str | Path, what: str, fmt: str = "json") -> str:
        r = self.load(ref)
        rows = {"candidates": [flatten_candidate(c) for c in r["candidates"]],
                "detections": r["detections"],
                "sources": [{k: v for k, v in s.items() if k in ("source_id", "ra", "dec", "n_obs", "n_epochs", "ambiguous",
                                                                "worst_detection_grade")} for s in r["sources"]],
                "observations": [{"obs_id": o["obs_id"], "included": o["included"], "exclusion_reasons": ";".join(o["exclusion_reasons"]),
                                  **{k: (o.get("meta") or {}).get(k) for k in ("detector", "release", "mjd_avg", "pipeline_version")}}
                                 for o in r["observations"]],
                "validation": [{k: v.get(k) for k in ("run_id", "procedure", "verification_level", "scope_cell", "sample_size",
                                                       "outcome")} for v in r["validation"]["runs"]]}.get(what)
        if rows is None:
            raise HeimdallError.of(ErrorCode.INPUT_INVALID, "export one of: candidates, detections, sources, observations, validation")
        if fmt == "json":
            return json.dumps({"run_id": r["run_id"], "data_origin": r["data_origin"], "what": what, "rows": rows}, indent=1,
                              sort_keys=True)
        if fmt != "csv":
            raise HeimdallError.of(ErrorCode.INPUT_INVALID, "format must be json or csv")
        buf = io.StringIO()
        flat = [{k: (json.dumps(v) if isinstance(v, (list, dict)) else v) for k, v in row.items()} for row in rows]
        fields = sorted({k for row in flat for k in row})
        writer = csv.DictWriter(buf, fieldnames=fields)
        writer.writeheader()
        writer.writerows(flat)
        return buf.getvalue()

    def demo(self, out_dir: Path | None = None, validate: bool = True, small: bool = False) -> tuple[dict, Path | None]:
        """Generate a SYNTHETIC scene and investigate it end to end through the real pipeline."""
        from .synthetic import Scenario, generate
        base = Path(out_dir) if out_dir else Path(self.settings.state_dir) / "demo"
        scenario = Scenario(npix=96, n_stars=30, visits_per_epoch=3) if small else Scenario(npix=192, n_stars=140)
        generated = generate(base / "products", scenario)
        radius = 0.42 * scenario.npix * scenario.pixel_scale_arcsec / 3600.0
        request = InvestigationRequest(ra_deg=scenario.ra0, dec_deg=scenario.dec0, radius_deg=round(radius, 4),
                                       source_id="local-fits", run_validation=validate, label="SYNTHETIC DEMONSTRATION",
                                       local_root=str(generated.directory))
        return self.investigate(request)


def _investigation_summary(i: dict) -> dict:
    return {"investigation_id": i["investigation_id"], "subject": {k: i["subject"][k] for k in ("kind", "id", "ra_deg", "dec_deg")},
            "candidate_ids": i["candidate_ids"], "patterns": [p["pattern"] for p in i["patterns"]], "interpretation_status": i["interpretation"]["status"],
            "viable_hypotheses": i["interpretation"]["viable_hypotheses"], "n_evidence": len(i["evidence"]), "n_independent_groups": len(i["dependency_groups"]),
            "planet_x": i["planet_x"]["status"], "n_missing_evidence": len(i["missing_evidence"])}


def flatten_candidate(c: dict) -> dict:
    cand = c["candidate"]
    return {"candidate_id": c["candidate_id"], "kind": c["kind"], "category": c["category"], "subject_id": c["subject_id"],
            "ra_deg": c["position"]["ra_deg"], "dec_deg": c["position"]["dec_deg"],
            "sigma_equivalent": c["statistic"]["significance"]["sigma_equivalent"], "statistic": c["statistic"]["name"],
            "attention_level": cand["attention_level"], "validity": cand["validity"]["state"],
            "validity_reasons": ";".join(cand["validity"]["reasons"]), "quality_grade": c["quality_grade"],
            "planet_x_status": (c.get("planet_x") or {}).get("status"),
            "failed_checks": ";".join(k["check_id"] for k in cand["checks"] if k["state"] == "FAILED"),
            "unresolved_checks": ";".join(k["check_id"] for k in cand["checks"] if k["state"] == "NOT_ASSESSABLE" and k["critical"]),
            "supported_alternatives": ";".join(a["alt_id"] for a in cand["alternatives"] if a["state"] == "SUPPORTED")}


def summarize(r: dict) -> dict:
    levels: dict[str, int] = {}
    for c in r.get("candidates", []):
        lvl = c["candidate"]["attention_level"]
        levels[lvl] = levels.get(lvl, 0) + 1
    return {
        "run_id": r.get("run_id"), "software_version": __version__, "data_origin": r.get("data_origin"),
        "status": r.get("status"), "request": r.get("request"), "config_hash": r.get("config_hash"),
        "results_hash": r.get("results_hash"),
        "observations": {"total": len(r.get("observations", [])),
                         "included": sum(1 for o in r.get("observations", []) if o.get("included")),
                         "failed_items": len(r.get("failed_items", []))},
        "epochs": len(r.get("epochs", [])), "detections": len(r.get("detections", [])), "sources": len(r.get("sources", [])),
        "tracks": len(r.get("tracks", [])), "candidates_by_attention": levels,
        "planet_x": r.get("planet_x"), "interpretation": (r.get("investigations") or {}).get("interpretation_counts"), "stages": [(s["stage"], s["status"], s["partial"]) for s in r.get("stages", [])],
        "validation": [(v["procedure"], v["outcome"], v["sample_size"]) for v in (r.get("validation") or {}).get("runs", [])],
        "warnings": r.get("warnings", []),
    }
