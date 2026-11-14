# Requirement -> implementation -> test matrix

Status: **I** implemented and tested; **P** partially implemented; **B** blocked by unavailable data or access;
**NI** not implemented. Evidence: **S** exercised on synthetic data; **R** exercised on genuine SPHEREx data
(`VERIFICATION_REAL_DATA.md`); **L** exercised against a live external service.

"Exercised on real data" means the mechanism ran correctly on genuine products. It does **not** mean a scientific
result was validated. Passing software tests are never scientific validation. Validation outcomes are listed
separately at the end.

Tests (2026-10-30, latest): `python3 -W error -m unittest discover -s tests -t .` ran 344 tests: 339 passed, 5 opt-in live tests
skipped, 0 failed (the 2026-10-29 figure of 253 tests is superseded; the 5 live tests passed when run separately on 2026-10-29 with
`HEIMDALL_REAL_DATA_TESTS=1`, 169 s, and were not re-run since). A final classification of every capability is in `docs/HANDOFF.md`.

## A. Astronomical data infrastructure

| Requirement | Implementation | Tests | Evidence | Status | Limits |
|---|---|---|---|---|---|
| Archive discovery, retrieval, caching, provenance | `data_access/irsa_spherex.py`, `cache.py`, `http.py` | test_adapters, test_data_access, live conformance | S R L | I | |
| Product validation (checksums, FITS layout, QC) | `ProductCache` (SHA-256), `ingest/spherex.py` QC | test_ingest_detection, test_adapters | S R | I | IRSA md5 is for full products, so cutouts are checked by SHA-256 of what was received |
| Identifiers, detectors, release and pipeline versions, extensions, WCS, wavelengths, uncertainty, masks, PSF, calibration ids | `ImageMeta`, tagged `Observation` metadata | test_ingest_detection | S R | I | |
| Six detectors; detector-specific PSF orientation | `DETECTOR_BANDS_UM`, per-detector calibration products | test_ingest_detection | R (D2, D5) | P | ePSF orientation verified on D5 only (`SPHEREX_FIDELITY.md`); D1, D3, D4, D6 science products not inspected |
| QR2 and QR3 compatibility | both flag conventions (`ingest/flags.py`), embedded EPSF, release de-duplication, QR2-calibration-on-QR3 warning | FlagVocabularyTests, AdapterRobustnessTests | S R | I | one QR3 product inspected; QR3 did not yet cover the verification field |
| Wavelength and pixel-scale calibration products | `retrieve_calibration('spectral_wcs'/'solid_angle')`, `--calibration-products full`, shared read-only arrays | e2e, live run | R | I | QR2 versions only |
| Incomplete observations, missing values, corrupt files, duplicates | QC exclusions with reasons, `FailedItem`, verified cache, release de-duplication | test_ingest_detection, test_adapters, FailureModeTests | S R | I | |
| Timeouts, interrupted downloads, rate limits, offline execution | retries with backoff, `Retry-After`, atomic cache writes, `HEIMDALL_OFFLINE` with cached TAP/context answers | AdapterRobustnessTests, ContextServiceTests | S R | I | live rate limiting never provoked |
| Manifests, checksums, versioned configuration, provenance to inputs | `manifest.json`, `config_hash`, `results_hash`, provenance per observation | test_pipeline_e2e | S R | I | |

## B. Image processing and source detection

| Requirement | Implementation | Tests | Evidence | Status | Limits |
|---|---|---|---|---|---|
| Background, noise and uncertainty model, masking | `engine/background.py`, `detection.py` noise diagnostics with standard errors; field-level astrometric and photometric error scales | test_engine_math, test_ingest_detection | S R | I | |
| PSF-aware detection, photometry, sub-pixel centroids, propagated uncertainties | matched filter, ePSF fit, forced photometry | test_ingest_detection, injection | S R | I | bright-source PSF chi-square excess on D2 (crowding/ePSF) |
| Association across observations with ambiguity | `engine/association.py` (1.1.0, brightest-first conflict split) | test_analysis_units (including the real bright-star regression) | S R | I | |
| Crowding, blending, saturation, artefacts, poor PSF fits, grades with reasons | deblending, grades, flag policy, OUTLIER as change flag | test_analysis_units, test_ingest_detection | S R | I | source-specific astrometric error terms not modelled (global scale only) |
| Wavelength-aware measurement, per-pixel wavelengths | WCS-WAVE or CWAVE/CBAND per detection | test_analysis_units | S R | I | spectral-feature detection not implemented (no validated templates) |

## C. Temporal, motion and variability

| Requirement | Implementation | Tests | Evidence | Status | Limits |
|---|---|---|---|---|---|
| Time handling, epochs, ordering, playback | `engine/temporal.py`, `_playback` | test_analysis_units, e2e | S R | I | |
| Motion, proper motion, parallax fits with uncertainty | `engine/motion.py`, astrometric error model, leave-one-image-out | test_engine_math, test_context_and_intelligence | S R | I | |
| Moving-object linking (cadence, gaps, motion bounds) | `engine/linking.py` | test_analysis_units, e2e, moving-object injection | S R | P | tracklets need two detections inside the association gate: synthetic completeness 0/4 at 100-271 AU but 14/16 at 737-2000 AU; real-data completeness only 7/32 |
| Variability with comparable wavelengths and calibration | `variability_test` with leave-one-out, Bonferroni | test_analysis_units, null test | S R | I | |
| Transients and persistence | `transient_test` | e2e | S R | I | |
| Distinguish change from errors, blending, artefacts, PSF and calibration effects | checks and alternatives in `reasoning.py` | test_analysis_units, test_context_and_intelligence | S R | I | |
| Explicit inconclusive states | `INCONCLUSIVE`, `NOT_ASSESSABLE`, `INSUFFICIENT_INPUTS` everywhere | all | S R | I | |

## D. Spatial intelligence and sky-map data

| Capability | Implementation | Tests | Evidence | Status |
|---|---|---|---|---|
| ICRS transformations; gnomonic and Mollweide projections with inverses | `engine/skygeom.py` | round-trip tests | S | I |
| WCS-aware placement (TAN-SIP, crop offsets) | `ingest/spherex.py` | test_ingest_detection | S R | I |
| Footprints, coverage depth, spatial index, viewport queries | `skymap/layers.py`, `skymap/index.py` | e2e, test_performance (100k items) | S R | I |
| Overlays: detections, sources, candidates, uncertainty ellipses, associations, tracks | `build_skymap` | e2e | S R (no real tracks) | I |
| Real reference layers: Gaia DR3 stars, JPL-predicted known bodies | `_external_layers` | ContextOutputTests | R L | I |
| Band and time layers | `build_skymap` | e2e | S R | I |
| Minimal working map (static SVG) | `skymap/svg.py` | e2e | S R | P (static only) |
| **Interactive Stellarium-like workspace** | none | none | none | **NI** (UI scope; the data contract it needs exists) |

## E. Evidence reasoning

| Requirement | Implementation | Tests | Evidence | Status |
|---|---|---|---|---|
| Combine quality, spectra, time, motion, parallax, context, uncertainty, provenance | `reasoning.assess` | test_analysis_units, test_context_and_intelligence | S R | I |
| Distinguish artefacts, known objects, ordinary explanations, unexplained, follow-up | alternatives and attention levels | same | S R L | I |
| Links from conclusions to evidence | `EvidenceRef` (measurement, record, external reference) | same | S R | I |
| Why promoted, downgraded, rejected or inconclusive | `explanation[]` rule trace, `attention_reason`, `cap_reason` | same | S R | I |
| Conflicting and missing evidence, alternatives | `contradictions`, `missing_evidence`, alternatives | same | S R | I |
| No double counting of duplicates or correlated measurements | release de-duplication, one point per image per source, leave-one-image-out | AdapterRobustnessTests, ReasoningWithContextTests | S R | I |
| Versioned rules, calibrated statistics | `reasoning-1.3.0-provisional`, validity registry, error rescaling | same | S R | I |
| Follow-up ranking without pseudo-probabilities | `rank_follow_up` | test_follow_up_ranking_is_ordered_and_explained | S R | I |
| Machine-readable and human-readable output; audit trail | `investigation.json`, statements, events, manifest | e2e | S R | I |
| Machine learning | evaluated, not adopted (`ML_DECISION.md`) | n/a | n/a | decision |

## F. Planet X hypothesis evaluation

| Requirement | Implementation | Tests | Evidence | Status |
|---|---|---|---|---|
| Testable signatures, assumptions, outcomes | `engine/planetx.py` (`planetx-1.1.0-provisional`), 11 requirements | test_analysis_units, PlanetXContextTests | S R | I |
| Known-object and star exclusion | `not_a_known_solar_system_object`, `not_a_star` from context | PlanetXContextTests | S R L | I |
| Completeness-aware region statement, never absence from non-detection | `region_assessment` (`COMPLETENESS_LIMITED`) | e2e, PlanetXContextTests | S R | P (linking completeness is low for closer bodies; cell UNVALIDATED) |
| Brightness plausibility | none | none | none | B (no validated 0.75-5 um model of a distant planet) |

## G. Known-object and catalogue context

| Requirement | Implementation | Tests | Evidence | Status | Limits |
|---|---|---|---|---|---|
| Known small bodies | `context/small_bodies.py` (JPL SB Ident with the spacecraft state vector) | ContextServiceTests, live test | S L R | I | bodies without magnitude parameters (mostly comets) are not queried; second-pass ephemeris error assumed 1.0" |
| Stellar catalogue | `context/gaia.py` (Gaia DR3 at IRSA) | ContextServiceTests, astropy reference test, live test | S L R | I | Gaia incomplete near G = 21; no 2MASS/WISE infrared catalogue yet |
| Match states; failed query never looks like no match | `CONFIRMED_MATCH`/`PROBABLE_MATCH`/`NO_MATCH`/`NOT_ASSESSABLE`/`EXCLUDED_BY_MOTION`; `FAILED`/`OFFLINE_NOT_CACHED` | test_query_failure_is_never_no_match, test_known_object_states | S | I | |
| Caching, timeouts, rate limits, offline behaviour | `CachedService` | ContextServiceTests | S L | I | |

## Statistical trust (order section 3)

| Requirement | Implementation | Evidence | Status |
|---|---|---|---|
| Null tests and negative controls | inverted-image, constant-source variability, stationary-source motion (Gaia controls) | S R | I |
| Injection-recovery (detection) | `injection_recovery` | S R | I |
| Moving-object injection: brightness, distance, motion, cadence, missing observations | `moving_object_injection` | S R | I (sample small by default) |
| Completeness and missed objects with uncertainty | Wilson intervals by flux and distance, coverage fraction | S R | I |
| False positives and threshold sensitivity | inverted-image null, robustness, false links | S R | I |
| PSF error, underestimated positional errors, calibration, artefacts, blending | `psf_mismatch_sensitivity`, astrometric error model, release checks, flags, blend checks | S R | P (calibration systematics not modelled) |
| Regression and reproducibility | `results_hash`, `reproducibility_detection`, `reproduce` | S R | I |
| Independent numerical checks | 8 self-checks; Gaia propagation against astropy (0.17 mas or better) | S | I |
| Performance and memory | `test_performance` (memory and time bounds); shared calibration arrays | S R | I |
| Real-data validation | every procedure ran on genuine images | R | P (see outcomes) |

## Validation outcomes on genuine SPHEREx data

Final run `R20261029T112935-4636a636` (75 QR2 D2 images, `VERIFICATION_REAL_DATA.md` P6): numerical checks,
robustness and reproducibility PASSED; detection injection-recovery and the inverted-image null MISCALIBRATED;
variability null (40 trials), motion null (55 Gaia stationary stars) and moving-object injection (7/32 recovered)
UNVALIDATED; noise calibration NOT_ASSESSABLE; PSF mismatch informational. **No real-data significance is
validated.**


## H. Scientific investigations (added 2026-10-30)

| Capability | Status | Where | Tests |
|---|---|---|---|
| Typed evidence with provenance, admissibility, dependency groups | implemented | `engine/evidence.py` | `tests/test_investigation.py` (synthetic) |
| Hypothesis catalogue with prediction functions and status rules | implemented, provisional | `engine/hypotheses.py` | same |
| Subject-level investigations, interpretation status | implemented | `engine/investigation.py` | same |
| Pipeline stage `investigation`, candidate references, schema 1.1 | implemented | `pipeline/runner.py`, `results.py` | same (end to end, synthetic) |
| `inspect --section investigations`, `reinvestigate` | implemented | `api.py`, `__main__.py` | same; real saved run replayed offline (consistency only) |
| Sky-map candidate metadata | implemented (data only, no visuals) | `skymap/layers.py` | same |
| Thermal/dust, spectral-class, variability-class hypotheses | blocked: no reference | -- | -- |
| Within-epoch light curve, morphology, comet check | not implemented | -- | -- |
| LLM narration | deferred | -- | -- |
