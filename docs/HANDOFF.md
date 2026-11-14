# Successor handoff (2026-10-29, after the engine completion order)

## Repository state

- Location: `/Users/rishabhpatel/Workspace/HEIMDALL`. Git is initialised on branch `main` with **no commits**.
  Every file is untracked and uncommitted. No push has been made. Never commit or push without the user's
  explicit approval and a review of `git status` and the staged diff.
- Versions: package 0.2.0; schema `0.1.0-provisional`; engine configuration `1.1.0-provisional`; rules
  `reasoning-1.3.0-provisional`, `planetx-1.3.0-provisional`, `validation-1.3.0`; association method 1.1.0; engine configuration 1.2.0-provisional.
- Dependencies: numpy, scipy, astropy (certifi recommended), all already installed. Nothing new was installed.
  External services used read-only and without credentials: IRSA (SPHEREx archive, Gaia DR3 TAP) and the JPL
  SB Identification API.
- Tests: `python3 -W error -m unittest discover -s tests -t .` ran 253 tests: 248 passed, 5 opt-in live tests
  skipped. The live tests (`HEIMDALL_REAL_DATA_TESTS=1`) were run once separately and all 5 passed (169 s).
- Real data: see `docs/VERIFICATION_REAL_DATA.md`. The final run is `R20261029T112935-4636a636` (all 75 QR2 D2
  images, 0.3-degree cutouts, offline from the cache). Evidence copies (git-ignored):
  `.heimdall/verification/2026-10-29-irsa-qr2/` (Part 1) and `.heimdall/verification/2026-10-29-irsa-qr2-part2/`
  (Part 2 runs, download log and console log).
- Downloaded products and cached service answers are only in the session temporary directory
  (`.../T/opencode/real_cache`: 75 D2 cutouts, 8 D5 cutouts, one QR3 cutout, 4 calibration products, Gaia and
  43 JPL answers). Re-downloading the 75 cutouts takes about 50 minutes.

## Linker state (2026-10-30)

Corrections in place: evaluation bound reported as truncation (200,000); effective-dof criterion; distant-body rate bound on tracklets;
split-tracklet ownership (absorbed chains recorded, not reported as tracks); partial known-object matches are PROBABLE, never CONFIRMED;
validator accounting with truth, detection sigmas and linker diagnostics preserved per experiment. Offline estimates on the saved accepted
tracks (floor-only covariance; not a rerun): mixed tracks 69 -> 15, definite spurious 71 -> 16 (+6 ambiguous), recovered 60 -> 51 of 100.
Not yet validated on a fresh run. See the report of 2026-10-30 for limitations.

## What exists

See `README.md`, `docs/ARCHITECTURE.md` and the matrix in `docs/COMPLETENESS_MATRIX.md`. Added by the engine
completion order:

- offline mode with cached archive and service answers; `Retry-After`; release de-duplication; run manifests;
- QR3 support (flag convention, embedded EPSF); spectral-WCS and solid-angle calibration products, shared in memory;
- `heimdall/context/`: Gaia DR3 matching with epoch propagation, registration against Gaia, Gaia stationary
  controls, JPL known-object identification with the spacecraft state vector, explicit query states;
- reasoning 1.3.0: known-object and catalogue evidence, leave-one-image-out independence, OUTLIER as a change
  flag, photometric error model, follow-up ranking;
- Planet X 1.2.0: context requirements, measurement-reliability requirement, minimum degrees of freedom,
  completeness-limited region statements;
- validation 1.2.0: moving-object injection and linking completeness, PSF-mismatch sensitivity;
- association 1.1.0: brightest-first split of conflicting components;
- sky-map external layers with real Gaia stars and JPL-predicted bodies; performance and memory tests.

## Decisions awaiting the user

1. Review the engine work and the contract additions in `docs/PROVISIONAL.md` (offline setting, request fields
   `context` and `calibration_products`, the `context` stage, the run manifest, candidate `context` and
   `follow_up`).
2. Still pending from before: the SCI-21 clarification, SCI-37, S6 ordering, v1.4 confirmations 4-12 and the
   v1.4 integration choices. The v1.4 contract draft remains in the session temporary directory.
3. Provisional context parameters: JPL magnitude limit V < 21 (comets without magnitude parameters are not
   checked) and the assumed 1.0 arcsec second-pass ephemeris error.
4. How to handle mixed pipeline versions (6.4 and 6.5.6 in the verification field): cross-version relative
   calibration or single-version analysis. Today the check fails and the candidates stay `INCONCLUSIVE`.
5. Real data access arriving on the 14th: inspect the products and documentation first, then add or adapt one
   adapter behind `DataSource`.

## Next necessary actions

1. Velocity-based tracklets inside each epoch, so that bodies closer than about 700 AU can be linked. Then
   re-measure moving-object completeness with at least 100 covered bodies. Real-data completeness is 7/32 today.
2. Source-level error terms: background structure and confusion in flux and position uncertainties (real-data
   flux pulls have a standard deviation of 1.31). Apply Gaia-based per-image registration corrections before the
   motion stages: 46 of 75 images show significant offsets.
3. A cross-version photometric calibration, or single-version analysis.
4. Larger samples (full frames or more cutouts) so the variability and motion nulls reach 100 trials. An
   artefact model for the excess of single-image false detections (0.39 per image at 5 sigma).
5. Check the ePSF orientation on D4 and D6, and QR3 calibration versions.
6. Then build the UI against `docs/OUTPUT_SCHEMA.md` and `heimdall.api`.

## Update 2026-10-30 (later): fresh linker validation and the investigation engine

This file is the project's running handoff. The original, longer handoff text was overwritten in place during an earlier session and is not
recoverable (no Git history exists); `docs/milestones/M01.md`, `ARCHITECTURE.md`, `COMPLETENESS_MATRIX.md` and the other `docs/` files are intact.
The sections above are kept as written; this section supersedes only where it says so.

**Supersedes "Not yet validated on a fresh run" (Linker state).** Fresh bounded injection validation `fresh_20261030_effdof_ownership`
(100 experiments x 1 body, no truncation, `validation-1.3.0`): `VALIDATED_FOR_CONFIG` on the false-link criterion only; recovered 56/100,
definite spurious 16 (upper 95% 0.26 per experiment), conservative 18 (0.284). It used the same seeded bodies the corrections were designed
on, so it is not independent; repeat with another seed. Completeness stays inadequate; Planet X region statements remain completeness-limited.

**Investigation engine (implemented, provisional).** New pipeline stage `investigation` (after `reasoning`, before `outputs`) builds structured
investigations: typed evidence with dependency groups -> hypothesis catalogue with explicit prediction functions -> rule-derived statuses ->
interpretation status per subject. Modules: `engine/evidence.py`, `engine/hypotheses.py`, `engine/investigation.py`. Interfaces:
`python3 -m heimdall inspect RUN --section investigations [--id ID]`, `python3 -m heimdall reinvestigate RUN [--out FILE]`,
`Engine.reinvestigate`, sky-map candidate fields (`investigation_id`, `interpretation_status`, `evidence_refs`, `provenance_refs`).
Schema `heimdall.investigation/1.1` (additive). Design rules and limits: `docs/SCIENCE_METHODS.md` (Investigation section),
`docs/OUTPUT_SCHEMA.md`, `docs/PROVISIONAL.md`, `docs/COMPLETENESS_MATRIX.md` section H.

**Tests:** full suite 339 tests, OK, 5 skipped (opt-in live tests); 25 are new (`tests/test_investigation.py`, synthetic fixtures).
**Offline replay of real run R20261029T112935-4636a636** (consistency only, not validation): 280 investigations; 96 EXPLAINED, 184 INCONCLUSIVE;
no astrophysical hypothesis SUPPORTED; 9 KNOWN_SMALL_BODY SUPPORTED (confirmed JPL matches); every Planet X subject is EXPLAINED or INCONCLUSIVE.
**Still open:** user approval of contract additions (move types into `contracts/`, enum `FALSE_LINK_CRITERION_UNRESOLVED`); photospheric reference
for the thermal hypothesis; LLM narration and the UI (deliberately deferred); credentialed SPHEREx access arrives on the 14th (inspect products first).
No commit has been made; review `git status` before any commit.

## Pre-challenge verification and backend freeze (2026-10-30, final)

Four verification tasks were completed; no feature was added. Earlier sections stay as written; this section supersedes them only where it says so.

### 1. Linker robustness (second seed) — details in `docs/LINKER_SEED_COMPARISON.md`

- The seed controls only the injected bodies (distance, offset, direction, speed). Images, detection, association, baseline links, the linker, fits and the
  acceptance criterion are deterministic.
- Seed 20261031 (config hash `833b30c6...`, 100 experiments x 1 body, 6,953 s, no truncation) vs the original seed 20261029 (`0346f789...`): both
  `VALIDATED_FOR_CONFIG` on the false-link criterion. Recovered 59/100 vs 56/100 (Fisher p = 0.78); definite spurious 12 vs 16 (equal-rate p = 0.57); conservative
  14 vs 18 (p = 0.60). Pooled over 200 experiments: 0.14 definite per experiment (Garwood 95% 0.093-0.202), 0.16 conservative (0.109-0.226).
- Baseline real-field links (0 tracks, 7 inputs) are identical. 10 recurring real-field detections appear in non-pure tracks in both samples: part of the false-link
  behaviour is a deterministic property of this field.
- Interpretation: no instability found; the differences are compatible with sampling variation at n = 100. This is not proof of equality (only differences of about
  0.2 or more in recovery are detectable), the second sample uses the same field and injection type, and completeness remains low (27-30% of bodies get no track).
  The original saved validation files were checksummed before the run and are unchanged. No success threshold was introduced.
- Downstream: the saved real run has no tracks, so its investigations do not change; the seed only changes the numbers in the Planet X region statement (`COMPLETENESS_LIMITED` in both).

### 2. Contract and backend interface review

- A runtime-versus-documentation check (demo run and the saved real run) found these gaps, all corrected in `docs/OUTPUT_SCHEMA.md` without a schema change: the section
  header still said investigation 1.0; the sky-map candidate record, `physical_interpretation`, transient `probe_*`/`reference_sigma_ujy`, detection `amplitude_sigma`/`solid_angle_sr`/
  `n_fit_pixels`/`fit_note`/`inverted`, and hypothesis `instance_id`/`applies_to_candidates`/group counts were undocumented. A "which status means what" table now separates computation,
  review-priority, validity, interpretation, hypothesis and Planet X statuses.
- One code correction: `Engine.skymap` now rebuilds investigation metadata offline for runs saved before schema 1.1 (otherwise those runs showed null `investigation_id`).
  Verified on the real run: 292 candidate records, none with a null position or uncertainty ellipse; 189 INCONCLUSIVE, 103 EXPLAINED; every evidence reference resolves.
- New `tests/test_contract_interfaces.py` (5 tests) pins the documented fields and vocabularies to runtime output (synthetic run).
- New `docs/UI_BACKEND_REFERENCE.md`: entry points, what to draw from where, status vocabularies, empty/partial/unavailable states, provenance traversal and known interface limits.
- Known interface limits (not breaking): a track-subject investigation uses its first candidate's position; the saved real run has no tracks, so the track layer has only been exercised on synthetic data;
  coverage footprints and playback frames exist for every image; there is no morphology or Planet X location, and none should be drawn.

### 3. Thermal-reference feasibility — details in `docs/THERMAL_REFERENCE_FEASIBILITY.md`

Verified against IRSA TAP: `gaia_dr3_astrophys_param`, `allwise_p3as_psd` and `gaia_dr3_allwise_best_neighbour` exist with the columns listed there. In the real field GSP-Phot Teff exists for most Gaia
sources brighter than G = 19 and for none fainter; about 79% (26 of 33) of the Gaia-matched candidate subjects are fainter than G = 19. AllWISE W3/W4 (the usual dust-excess bands) lie outside SPHEREx's range.
All 682 source spectra in the saved run are single-detector. A defensible comparison additionally needs a photospheric model grid, SPHEREx band-response integration and an absolute-calibration mapping, none of which
exist in the repository (absolute calibration is not modelled). **Not implementable and testable now: `THERMAL_DUST_OR_RED_SPECTRUM` stays `NOT_ASSESSABLE`**; only the text of the blocking dependency was made exact.

### 4. Final scientific-engine completion matrix

Classes: IMPLEMENTED_AND_TESTED (IT), IMPLEMENTED_BUT_LIMITED (IL), PARTIALLY_IMPLEMENTED (PI), NOT_IMPLEMENTED (NI), BLOCKED_BY_EXTERNAL_DEPENDENCY (BX), DEFERRED_TO_CHALLENGE_WINDOW (DC).
"Tested" means software tests; real-data runs show the mechanism works, not that a result is validated. Detail per requirement: `docs/COMPLETENESS_MATRIX.md`.

| Capability | Class | Basis and limits |
|---|---|---|
| Ingestion and source adapters (IRSA SPHEREx, local FITS, cache, offline, manifests) | IL | IT on synthetic and live conformance; real QR2 D2 (75) and D5 (8) plus one QR3 cutout. D1, D3, D4, D6 science products never inspected; credentialed access not yet used |
| Observation normalization and QC (flags QR2/QR3, WCS, wavelengths, masks, duplicates) | IT | `test_ingest_detection`, `test_adapters`, `test_boundaries`; QR2 calibration versions only |
| Detection and photometry | IL | tested; real-data detection injection-recovery and inverted-image null are MISCALIBRATED; bright-source PSF chi-square excess |
| Spectral analysis | IL | per-point wavelengths, slope, band means, comparisons, tested; descriptive only; no spectral-feature detection, no multi-detector spectra in real data, no absolute calibration; thermal/dust hypothesis NOT_ASSESSABLE (feasibility documented) |
| Temporal analysis (epochs, variability, transients, playback) | IL | tested; mixed pipeline versions (6.4, 6.5.6) leave most real variables INCONCLUSIVE; variability null UNVALIDATED (40 trials) |
| Spatial association and moving-object linking | IL | tested; false-link criterion VALIDATED_FOR_CONFIG on two independent samples (this field only); completeness 0.56-0.59 given coverage, closer than ~700 AU weak; real-data 0 tracks |
| Motion, proper motion, parallax | IL | tested; motion null UNVALIDATED (55 Gaia stars); 46 of 75 images have significant registration offsets, uncorrected |
| Categorization and scientific reasoning (attention, checks, alternatives, investigations, hypotheses, Planet X) | IL | `test_investigation` (25), `test_candidates`, `test_analysis_units`; provisional rules and catalogue; no variability classes, morphology, within-epoch light curves, comet check; no real-data significance validated |
| Scientific validation | IL | all procedures implemented and run on real data; outcomes: robustness, reproducibility, self-checks PASSED; detection/null MISCALIBRATED; variability, motion nulls UNVALIDATED; moving-object injection VALIDATED_FOR_CONFIG (false-link criterion only) |
| Integrated pipeline (16 stages incl. `investigation`) | IT | `test_pipeline_e2e`, `test_investigation` end to end on synthetic data; real runs on cached products |
| CLI and programmatic interfaces | IT | `__main__.py` (investigate, runs, inspect, reinvestigate, validate, reproduce, skymap, playback, export, demo...) and `heimdall.api.Engine`; tested through `main()` for inspect and reinvestigate |
| Offline reinvestigation | IT | `Engine.reinvestigate`; equals the live stage on a synthetic run (test); real saved run replayed (consistency only) |
| Sky-map data contract (`heimdall.skymap/1.0`) | IT | layers, footprints, coverage depth, index, real Gaia/JPL layers, candidate metadata; `test_contract_interfaces`; no real tracks exercised |
| Provenance and reproducibility | IT | manifests, hashes, `reproduce`, results hash covers investigations; reproduction of a real run was verified earlier (`VERIFICATION_REAL_DATA.md`) |
| UI-facing interfaces | IL | `docs/OUTPUT_SCHEMA.md` and `docs/UI_BACKEND_REFERENCE.md` match runtime; a static SVG exists; no interactive UI |
| Interactive sky-map workspace / UI | DC | not started by design |
| LLM narration | DC | not started by design; the structured output is what it must narrate |
| Real SPHEREx credentialed products | BX | arrive on the 14th |
| Thermal/dust/spectral-class hypotheses | BX | model grid, band-response integration, absolute calibration mapping |
| Brightness plausibility of a distant planet | BX | no validated 0.75-5 um model |
| Contract additions (types in `contracts/`, enum `FALSE_LINK_CRITERION_UNRESOLVED`, v1.4 items) | BX | await the user's decision |
| Velocity-based within-epoch tracklets; cross-version calibration; registration correction | NI | listed under "Next necessary actions" |

### Critical gaps for a challenge demonstration (versus optional improvements)

Critical:
1. No real-data significance cell is validated, so every real candidate is INCONCLUSIVE or EXPLAINED and none is ATTEND. The demonstration must present this honestly as the engine refusing unsupported claims, not as a failure; do not tune thresholds to change it.
2. No UI exists; the UI must be built from `docs/UI_BACKEND_REFERENCE.md` (the contract is stable and tested).
3. Real SPHEREx access (14th) is unverified: products must be inspected before any adapter claim.
4. Real-data runs are slow (about 100 s offline capture; a full real investigation with validation takes hours). Pre-compute and save runs before the demonstration and load them with `inspect`/`skymap`; use `reinvestigate` for fast re-analysis.

Optional: velocity-based tracklets, registration correction, cross-version calibration, larger cutout samples, D4/D6 checks, thermal reference, narration polish.

### Actual tests

`python3 -W error -m unittest discover -s tests -t .` (environment variables unset): **344 tests, OK, 5 skipped** (opt-in live tests, not re-run today; they passed on 2026-10-29). New today: 5 contract tests.
Not run today: the opt-in live suite, the full real-data validation procedures (only the moving-object injection was re-run).

### Exact pre-challenge state

All work is uncommitted and untracked on `main` (no commits, nothing pushed). Backend features are frozen for the challenge except fixes found when real products arrive. Versions: package 0.2.0; investigation schema 1.1;
skymap 1.0; `validation-1.3.0`; `planetx-1.3.0-provisional`; `reasoning-1.3.0-provisional`; `investigation-1.0.0-provisional`; `hypotheses-1.0.0-provisional`; engine config `1.2.0-provisional`.

### November 14-15 execution requirements

1. Inspect the credentialed SPHEREx products and documentation before coding: product type, headers (wavelength, flags, release, pipeline version), access method, rate limits. Record findings in `docs/SPHEREX_FIDELITY.md`. Adapt one adapter behind `DataSource`; run `heimdall.data_access` conformance; do not claim integration until a real product passes ingestion and QC.
2. Run one bounded real investigation first (a small field, validation off), save it, then the longer runs in the background; never present an unsaved long run.
3. Build the UI against `heimdall.api` and the saved runs only (see `docs/UI_BACKEND_REFERENCE.md`); show attention, validity, interpretation and hypothesis statuses separately; show SYNTHETIC watermarks; no recomputation in the UI.
4. Implement LLM narration last, reading only `investigations[]`: it may restate statuses, evidence and missing evidence, and must not add conclusions, probabilities or Planet X claims. Reuse `check_language` in `engine/reasoning.py` as the guard.
5. Keep the `reproduce`/`reinvestigate` commands working on every new run; rerun the full test suite after each adapter change.

### Exact next actions

UI: (a) load a run with `Engine.load`, `skymap`, `playback`, `inspect(..., "investigations", id)`; (b) sky view from `layers.coverage`, `detections`, `sources`, `candidates` with `ellipse_1sigma`; (c) candidate panel from the investigation (evidence with `ref`, dependency groups, hypotheses with predictions, missing evidence, follow-ups, Planet X status); (d) spectra and light curves from `sources[]`.
Real data: credentials in the environment only (`HEIMDALL_SOURCE_CREDENTIAL`, never in files), `python3 -m heimdall access irsa-spherex`, then `coverage`, then `investigate --calibration-products full`, then `inspect` and `reinvestigate`.
Decisions still owed by the user: contract additions (above), the mixed pipeline-version policy, and any commit (review `git status` and the diff first).
