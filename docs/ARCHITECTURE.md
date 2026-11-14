# HEIMDALL architecture

One codebase, one shared data model, explicit boundaries. The scientific
engine never depends on a UI. Source-specific details stay inside adapters and
ingestion.

## Modules and dependency rules

```
contracts      standard library only (tagged values, validity, candidates, outcomes, provenance)
config         settings, Secret
data_access    contracts, config            (never pipeline or presentation; enforced by a test)
  interface, registry, http, cache, irsa_spherex, local_fits
ingest         contracts, engine.{config,methods,psf,skygeom}      (SPHEREx L2 parsing and QC)
engine         contracts, ingest types, numpy/scipy/astropy        (all scientific mechanisms)
context        data_access.{http,cache}, engine.skygeom, config    (Gaia DR3 via IRSA TAP, JPL SB Ident; cached)
pipeline       engine, ingest, data_access, context, contracts     (runner, results, interfaces)
skymap         engine.skygeom                                      (data contract, index, SVG)
api            everything above                                    (programmatic interface)
__main__       api                                                 (CLI)
synthetic      engine                                              (labelled synthetic products)
```

`tests/test_boundaries.py` enforces that contracts stay stdlib-only, that
data access never imports the pipeline or presentation, and that application
code never imports test fixtures.

## Pipeline

```
DataSource.discover -> selection -> DataSource.retrieve (+ calibration products)
  -> read_spherex_mef (QC, suitability, crop) -> prepare/detect (background, matched filter, PSF fit, deblend)
  -> build_epochs -> associate -> forced_photometry
  -> per source: assemble_spectrum, build_series + variability_test, compare_point_sets, motion_test, parallax_fit
  -> transient_test -> link_moving
  -> context (real data only): Gaia DR3 cone + matching + registration check; JPL SB Ident for images with
     transients, tracks or single-epoch movers
  -> validation procedures (optional; Gaia stationary stars as motion controls) -> ValidationRegistry
  -> reasoning.assess (+ planetx.evaluate) -> rank_follow_up -> results (plain JSON) + manifest + playback + skymap
```

Every stage records a `ComputationStatus`. Item failures become `FailedItem`s.
Discovery failure gives `execution = FAILED`. A successful empty query gives
`NO_OBSERVATIONS`. No usable images gives `INSUFFICIENT_OBSERVATIONS`.

## Significant design decisions

1. **Numerical libraries.** numpy, scipy and astropy are required by the
   engine. The justification is FITS and WCS with
   SIP and `-TAB` lookups, coordinate and time scales, the ephemeris, spatial
   trees, interpolation and special functions. Reimplementing these would be
   error-prone. `contracts` remains stdlib-only.
2. **Engine records are plain dataclasses** (floats, lists), converted at the
   edge by `pipeline.results.plain`. Candidates carry the contract `Candidate`,
   whose invariants make an unsupported state unrepresentable. Statistics enter
   candidates as tagged `D` values whose methods are registered
   (`engine/methods.py`).
3. **Image coordinates** are 0-based pixels of the file's IMAGE array. Full
   frames are cropped on read; arrays carry an origin. Parent (full-detector)
   coordinates are used for PSF zones and calibration maps.
4. **Validation is a registry of runs** keyed by scope cell. Candidates query
   it, so running validation changes validity (and possibly attention) in later
   runs without changing measurements.
5. **Reproducibility.** Deterministic ordering and seeds, and a configuration
   hash in every result. `results_hash` covers the scientific core, and
   `reproduce` re-runs and compares it.
6. **Contract changes in this phase:** `CandidateKind.TRANSIENT` was added.
   `AdapterRegistry.create` takes an optional `source_id`. `Settings` gained
   `state_dir` and `local_fits_dir`. The default registry now contains the
   real adapters `irsa-spherex` and `local-fits`.

## Extending to new sources or credentials

1. Implement `DataSource` (discover, retrieve, check_access, describe) in one
   module, reading any credential from `Settings.source_credential`.
2. Register it in `data_access/registry.py`.
3. Run `tests/contract/data_source_contract.py` with `expect_synthetic = False`.
4. If the products are not SPHEREx L2 MEF files, add an ingestion module
   producing `SpectralImage` objects; the rest of the engine is unchanged.
