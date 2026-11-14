# Provisional decisions

Everything here may change through a versioned change (engine configuration
version, rules versions or schema version). Changing a configuration value
changes the configuration hash, so earlier validation no longer applies.

## Provisional engine parameters (`heimdall/engine/config.py`, version 1.0.0-provisional)

| Parameter | Default | Basis |
|---|---|---|
| detection threshold | 5 sigma (matched filter) | convention; completeness measured by injection |
| PSF stamp, min separation, edge margin | 11 px, 2 px, 3 px | engineering |
| max centroid shift, max masked fraction | 1.5 px, 25% | engineering |
| deblending passes | 2 | engineering |
| background mesh, clip | 32 px, 3 sigma | engineering |
| excluded / warning flags | see SCIENCE_METHODS section 2 | IRSA spectrophotometry tool ignore list |
| grades | D saturation; C warning at peak, fallback, chi2_red > 3, SNR < 10; B neighbour < 3 px or warnings in footprint | engineering |
| noise-ratio bounds | 0.8-1.25 | engineering |
| registration floor | 0.4 arcsec | upper end of documented median precision for FINAST = 0 |
| association gate | chi2_2 at 0.9973 | convention |
| epoch gap | 5 days | real QR2 coverage clustering |
| wavelength tolerance | 0.5 x bandwidth | engineering |
| variability minimum | 3 points, 2 epochs | engineering |
| watch / attend / transient | 3 / 5 / 5 sigma (two-sided equivalent) | convention |
| minimum median grade | B | engineering |
| Planet X scenario | 100-2000 AU | exploratory range; set explicitly for a specific hypothesis |
| linking | p >= 0.01, dof >= 3, tracklets >= 2 points, max 500 evaluations | engineering |
| validation | >= 100 trials, KS p >= 0.01, binomial p >= 0.01, <= 0.1 false detections per image | engineering |

Added in configuration `1.1.0-provisional`:

| Parameter | Default | Basis |
|---|---|---|
| Gaia DR3 magnitude limit | G < 21 | Gaia completeness limit |
| context match gate | chi2_2 at 0.9973 | convention (same as association) |
| JPL query magnitude limit | V < 21 | single-image 5-sigma depth about AB 18.4 at 1.4 um, plus a conservative 1.5 mag near-infrared colour allowance and margin |
| assumed ephemeris error (second-pass positions) | 1.0 arcsec | engineering; the API gives no second-pass error; recorded as assumed |
| interval between live context calls | 1 s | courtesy rate limit |
| Gaia stationary-control limit | proper motion < 10 mas/yr | about 0.01 arcsec per year, far below SPHEREx centroid precision |
| moving-object injection | 4 experiments x 8 bodies; fluxes 8, 15 and 30 x the median forced-flux sigma; recovery within 2 px in >= 2 epochs | engineering; increase `validation.moving_object_experiments` for validation-grade samples |
| linking false-link criterion | 95% upper limit <= 0.5 per experiment | engineering |
| single-image independence | lowest leave-one-image-out significance >= watch level (3 sigma) | convention |
| injection truth attribution | association-gate radius around the true position; ambiguous band 2 x radius (`validation.attribution_ambiguous_factor`) | same gate as association; band is an explicit, reported-sensitivity parameter |
| link rate-information fraction | 1.0 x the annual-parallax rate at r_max (about 1.8 arcsec/day) | physical scale of the weakest signal the fit must test; sensitivity 0.5-5 reported in the offline re-evaluation |
| follow-up order | attention level, validity, failed then unresolved critical checks, rescaled significance | transparent ordering, not a probability |

## Rules versions

- `reasoning-1.3.0-provisional`: check applicability and criticality table and
  alternative rules (`engine/reasoning.py`). Version 1.1.0 added the
  precision-aware noise check and the `astrometric_error_model` check after
  the first real-data run. Version 1.2.0 added the known-object and Gaia
  catalogue evidence, the leave-one-image-out check, OUTLIER as a change flag
  (not an artefact) and the follow-up rank. Version 1.3.0 added the
  photometric error model check and alternative after the 0.3-degree real-data run.
- `planetx-1.3.0-provisional`: requirements and status rules. The linker (engine configuration 1.2.0) additionally requires effective_dof >= min_link_dof (1.2.0), a distant-body rate bound on tracklets, split-tracklet ownership and non-confirmation of partial known-object matches (1.3.0). Version 1.1.0
  added the known-object and catalogue requirements and the
  completeness-limited region statement.
- `validation-1.3.0`: moving-object injection with explicit truth attribution (see VALIDATION.md). `validation-1.2.0`: procedures and criteria. Version 1.1.0 added the
  precision-aware noise calibration and SNR bins shared across images for
  injection. Version 1.2.0 added moving-object injection, PSF-mismatch
  sensitivity and Gaia stationary controls.

## Contract additions (beyond the master contract)

From Milestone 01:
- `DataOrigin.SYNTHETIC_TEST_FIXTURE`;
- error codes `CONFIG_INVALID`, `SOURCE_NOT_CONFIGURED`, `CREDENTIALS_MISSING`,
  `AUTHENTICATION_FAILED` and `ACCESS_DENIED`;
- `InputRef.name`, `Alternative.ordinary` and `ComputationStatus`.

From the engine work:
- `CandidateKind.TRANSIENT`;
- `AdapterRegistry.create(settings, source_id=None)`;
- `Settings.state_dir` and `Settings.local_fits_dir` (`HEIMDALL_STATE_DIR`,
  `HEIMDALL_LOCAL_FITS_DIR`);
- the registered methods in `engine/methods.py`.

From the engine completion work:
- `Settings.offline` (`HEIMDALL_OFFLINE`);
- `InvestigationRequest.context` and `InvestigationRequest.calibration_products`;
- the `context` pipeline stage, the `heimdall.manifest/1.0` run manifest, and the
  candidate `context` and `follow_up` fields (`docs/OUTPUT_SCHEMA.md`).

Still awaiting the user's approval (unchanged): the SCI-21 clarification,
SCI-37, S6 ordering, confirmations 4-12 of the v1.4 register, and the v1.4
integration choices.

## Still provisional at the data level

See `docs/SPHEREX_FIDELITY.md` section 4: ePSF orientation for D4 and D6,
QR3 versions of the spectral-WCS and solid-angle products, `X_SC` axes, legacy
PSF convention and absolute calibration systematics.


## Investigation engine (added 2026-10-30)

- Versions: `investigation-1.0.0-provisional`, `hypotheses-1.0.0-provisional`, `evidence-1.0.0-provisional`, schema `heimdall.investigations/1.0` inside `heimdall.investigation/1.1`.
- Provisional by construction: the hypothesis catalogue and its triggers, the star proper-motion/parallax significance cut (3 sigma) and the 1 arcsec maximum stellar parallax, the candidate gate, and the status-rule precedence. None is calibrated against real labelled cases; none is validated.
- The new types live in the engine (not in `contracts/`); moving them there still needs the user's approval.
- Known limitations: Gaia is incomplete near G = 21; comets and bodies fainter than V = 21 are not queried; absolute calibration is not modelled; the thermal hypothesis has no reference; the Gaia astrophysical-parameter and AllWISE tables were never inspected.
