# Validation engine

Validation is executable (`heimdall/engine/validation.py`). Its runs are
stored in the validation registry (`<state_dir>/validation/*.json`) and decide
the validity state of every candidate significance.

## Levels: what a run can establish

| Level | Meaning | Establishes | Never establishes |
|---|---|---|---|
| `MATHEMATICAL` | independent numerical checks | formulas agree with independent references | anything about data |
| `SOFTWARE` | reproducibility and robustness of computation | identical outputs for identical inputs | scientific validity |
| `SYNTHETIC_INJECTION` | procedures run on synthetic images | software behaviour under an idealised model | real-world performance |
| `REAL_DATA_INJECTION` | PSF-model sources injected into real images | completeness and error calibration given real noise and masks, if the PSF model is right | behaviour for sources the model does not describe |
| `REAL_DATA_EMPIRICAL` | statistics of real data under a stated null | false-positive rates and calibration on real data | the null assumption itself (for example that controls are stationary) |

## Scope cells

A significance is validated only for its scope cell:

```
<cell kind> | <data origin: REAL or SYNTHETIC> | <release> | <engine config hash>
```

Cell kinds: `VARIABILITY_SIGNIFICANCE`, `MOTION_SIGNIFICANCE`,
`DETECTION_FALSE_POSITIVE`, `DETECTION_PHOTOMETRY`, `TRANSIENT_SIGNIFICANCE`
(needs both the variability and false-positive cells) and
`MOVING_OBJECT_LINKING` (no procedure yet, so always `NO_CONTROL_RUN`).
Because the data origin is part of the cell, **synthetic runs can never
validate real-data candidates**. Any configuration change produces a new hash,
so earlier validation no longer applies.

## Validity states and the evidence required

| State | Required evidence |
|---|---|
| `VALIDATED_FOR_CONFIG` | the cell's null procedure ran with >= `min_null_trials` (100) trials and met its frozen criteria |
| `MISCALIBRATED` | enough trials, criteria failed (reason `CRITERIA_FAILED`) |
| `UNVALIDATED` | no run (`NO_CONTROL_RUN`), too few trials (`INSUFFICIENT_SAMPLE`) or unrepresentative controls; for a candidate, also a failed (`ASSUMPTION_CHECK_FAILED`) or unresolved (`ASSUMPTION_CHECK_UNRESOLVED`) critical check, listed first |
| `NOT_ASSESSABLE` / `NOT_ASSESSED` | reserved by the contract (not produced by current procedures) |

A candidate with a failed or unresolved critical check is `UNVALIDATED` even
when its cell is validated. `ATTEND` needs `VALIDATED_FOR_CONFIG` and no failed
or unresolved critical check. These invariants are enforced by
`contracts.Candidate`, not only by convention.

## Procedures

| Procedure | Cell | Trials | Criteria (provisional) |
|---|---|---|---|
| `independent_numerical_checks` | none (`MATHEMATICAL`) | 8 checks | all pass |
| `injection_recovery` | `DETECTION_PHOTOMETRY` | PSF-model sources at SNR 2-10 injected into each image, detection re-run | >= 100 recovered at SNR >= 5; flux-pull std and median-based position ratio both in [0.8, 1.25] |
| `null_detection_inverted` | `DETECTION_FALSE_POSITIVE` | detection on sign-inverted images | 95% Poisson interval of false detections per image at the operating threshold: upper <= 0.1 validates, lower > 0.1 is miscalibrated |
| `null_variability_constant_sources` | `VARIABILITY_SIGNIFICANCE` | constant flat-spectrum sources injected at empty positions; forced photometry in every covering image; one constant-flux chi-square per position and detector | >= 100 trials; KS p >= 0.01 against Uniform(0,1); no significant excess of p < alpha_watch (binomial p >= 0.01) |
| `null_motion_stationary_sources` | `MOTION_SIGNIFICANCE` | stationarity test on sources with grade A/B in >= 3 images over >= 2 epochs. With a completed Gaia query the controls are Gaia DR3 stars with catalogued proper motion < 10 mas/yr (`control_definition = GAIA_QUASI_STATIONARY`); otherwise detected sources assumed stationary (`ASSUMED_STATIONARY`) | same as above |
| `moving_object_injection` | `MOVING_OBJECT_LINKING` | distant bodies (log-uniform 100-2000 AU, transverse speed up to circular, flux 8/15/30 x the median forced-flux sigma) placed by exact parallax geometry in every image they cross; detection, association, transient test and linking re-run (`validation.moving_object_*`). Truth attribution (validation-1.3.0): a track detection is the body's if within the association-gate radius of the TRUE position (its own centroid sigma plus the registration floor), ambiguous within `attribution_ambiguous_factor` x that radius. Tracks are pure / mixed / ambiguous / no_body. Recovery = a body has >= 1 pure track spanning >= 2 epochs; further pure tracks of the body are duplicates. Spurious = mixed + no_body + duplicates (ambiguous reported separately). | >= 100 covered bodies; 95% Garwood upper limit of spurious tracks per experiment <= 0.5 (unchanged). VALIDATED needs the bound with ambiguous tracks counted; MISCALIBRATED needs the definite lower bound above 0.5; an interval straddling 0.5 is UNVALIDATED with reason `FALSE_LINK_CRITERION_UNRESOLVED` (a run-level string; candidate-level validity still maps unknown reasons to INSUFFICIENT_SAMPLE until an enum addition is approved). Truncated search -> NOT_ASSESSABLE. Each experiment record also keeps detection sigmas, per-track linking diagnostics (effective dof, absorbed chains), the linker counters and its notes. Truth, per-track classes and the validation-1.2 accounting are stored in the metrics; `reanalyze_injection` recomputes them offline. |
| `psf_mismatch_sensitivity` | none (informational) | sources whose true PSF is the model convolved with a 0.3 px Gaussian, measured with the unmodified model (12 images x 10) | none: reports flux bias, flux-pull spread, chi-square and position calibration |
| `noise_model_calibration` | none | per-image pixel and PSF-scale noise ratios with standard errors | PASSED when all are within [0.8, 1.25]; FAILED when any is more than 2 standard errors outside; otherwise NOT_ASSESSABLE |
| `robustness_detection_settings` | none | background mesh x0.5/x2, no deblending (first 6 images) | >= 95% of SNR >= 6 detections recovered |
| `reproducibility_detection` | none (`SOFTWARE`) | detection re-run (first 6 images) | bit-identical canonical hash |

Each run records its scope, sample size, metrics, criteria, outcome, reasons,
what it establishes and what it does not.

## Results so far

- Synthetic demonstration (24 images, 192 px): injection-recovery and
  constant-source variability validated; motion **MISCALIBRATED**. Synthetic
  images have perfect registration, so the 0.4" registration floor makes the
  errors over-conservative: p-values pile up near 1 and the KS test catches
  it. This shows the procedure detecting a mis-specified error model.
- Synthetic demonstration, moving-object injection: 17 of 31 covered bodies
  recovered with 0 false links; 14/16 at 737-2000 AU but 0/4 at 100-271 AU
  (closer bodies move too far between visits to form tracklets). The
  photometric error scale is 1.016 +/- 0.024, as expected for synthetic noise.
- Real data, Part 1 (16 QR2 detector-2 cutouts of 59x59 px): numerical checks,
  robustness and reproducibility PASSED; everything else UNVALIDATED or
  NOT_ASSESSABLE (too few trials).
- Real data, Part 2 (all 75 QR2 detector-2 images as 0.3-degree cutouts, final
  run `R20261029T112935-4636a636`, `validation-1.2.0`):
  - numerical checks, robustness and reproducibility PASSED;
  - injection-recovery **MISCALIBRATED** (flux pull std 1.31, position ratio
    1.19: uncertainties too small on real sky);
  - inverted-image null **MISCALIBRATED** (0.39 false detections per image at
    5 sigma, about 130 times the Gaussian expectation);
  - variability null (40 trials) and motion null (55 Gaia-confirmed stationary
    stars) UNVALIDATED: fewer than 100 trials;
  - moving-object injection UNVALIDATED: 7/32 recovered (95%: 0.11-0.39), 0
    false links;
  - PSF-mismatch sensitivity: a 0.3 px broadening leaves fluxes unbiased (ratio
    0.999) and raises chi-square to 1.26.

  No real-data significance is validated. Two MISCALIBRATED cells block the
  promotion of any detection-based or transient candidate in this scope.

Moving-object injection correction (validation-1.3.0): the saved 100-experiment real-data run, re-evaluated offline, moves from validation-1.2's 92/100 recovered with 49 'false links' (UNVALIDATED) to 60/100 recovered with 71 definite spurious tracks plus 10 ambiguous (MISCALIBRATED). See `docs/VERIFICATION_REAL_DATA.md` follow-up notes; this is a measurement-method correction, not a linker change.

## Known limits

- Injected sources carry no photon noise, and in `injection_recovery` their PSF
  equals the detection PSF. `psf_mismatch_sensitivity` measures one form of PSF
  error (symmetric broadening) and has no acceptance criterion.
- The inverted-image null cannot reveal positive-only artefacts. These are
  handled by flags and the artefact alternatives.
- Without Gaia, the stationary-source null assumes the controls do not move; a
  genuine high proper-motion star among them inflates the false-positive count.
- Moving-object injection uses constant-flux point sources with the image PSF,
  the real cadence of the analysed images and a field no larger than the
  cutouts. Bodies that leave the analysed area are counted as a coverage
  selection effect, not as missed detections. Known-object rejection is not
  part of the measurement.

## Linker seed comparison (2026-10-30)

A second independent injection sample (seed 20261031) agrees with the first on the false-link criterion and recovery within sampling variation; see `docs/LINKER_SEED_COMPARISON.md`. This is not a general robustness proof.
