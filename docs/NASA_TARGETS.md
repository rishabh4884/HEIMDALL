# NASA target investigations (NASA-01 … NASA-05)

Module: `heimdall/targets/`. Entry points: `python3 -m heimdall target {list|resolve|run|show|matrix}` and
`Engine.targets / resolve_target / investigate_target / target_result`. Result schema: `heimdall.target_investigation/1.0`
(saved at `<state_dir>/targets/<target_id>/latest.json`). The measured outcomes live in the generated
`docs/NASA_TARGET_MATRIX.md` (`python3 -m heimdall target matrix --write docs/NASA_TARGET_MATRIX.md`); this file describes the method.

The five targets are configuration (`targets/registry.py`), not code paths: a coordinate (`--ra --dec --category`) runs the same engine.

## Pipeline

1. **Resolve** (`astrometry.resolve`): a seed position (arcminute accuracy, never used as truth) plus a selection rule picks the catalogue
   entry: Gaia DR3 (IRSA TAP, fallback ESA Gaia archive) for NASA-01/02/04/05, CatWISE2020 (IRSA) for NASA-03. The rule is applied
   server-side (parallax window, G range, total proper motion). No match, a failed service or an offline cache miss gives
   `UNRESOLVED` / `SERVICE_FAILED` and the investigation is `BLOCKED`; it never falls back to the seed. Several rule-satisfying entries give
   `AMBIGUOUS` with the alternatives listed.
2. **Expected position** (`ReferenceAstrometry.at_mjd`): catalogue position at its reference epoch, proper motion (astropy space-motion with
   distance when the parallax is ≥ 1 mas), annual parallax from the Earth's barycentric position, and position/proper-motion covariance.
   Radial velocity is assumed 0 (stated). NASA's WISE 0855 coordinate is *not* assumed current: `seed_position_epoch_check` infers which epoch
   it corresponds to from the CatWISE proper motion and says so as an inference.
3. **Discovery** (IRSA SIA2 for current DR1/QR3, bounded fallback to IRSA TAP `spherex.plane`; the route used is saved in `discovery.route`), **selection** (`select_observations`): deterministic, epoch-balanced, per-detector, bounded by
   `--max-observations`; first and last epochs kept. The rule and the counts of what was *not* selected are in the result.
4. **Retrieval**: IRSA cutout service, cutout side `max(spec, 2 × motion span + 0.04°)` ≤ 0.3°, cache-first, resumable, time-budgeted
   (`--max-seconds`; PARTIAL if exceeded). The cache key includes the cutout centre/size and the archive path (release/version).
5. **Per-image measurement** (`measure.py`): ePSF forced photometry at the expected position; free-position PSF centroid with sky covariance;
   translation registration from isolated Gaia control stars (3σ-clipped, scale-inflated, ≥ 3 controls else `UNCONSTRAINED`); Gaia
   neighbours inside the PSF stamp; saturation/non-linearity flags; wavelength/bandwidth at the pixel.
6. **Motion** (`motion.py`) for moving targets: catalogue χ², stationary and constant-velocity fits on registered centroids, fitted vs catalogue
   angular velocity, measurability (expected displacement / its uncertainty), and a leave-one-out control-star null test whose pull scatter inflates
   the target's errors. Verdicts: `MOTION_DETECTED_CONSISTENT_WITH_CATALOGUE`, `…_INCONSISTENT_…`, `NO_SIGNIFICANT_MOTION`,
   `NO_SIGNIFICANT_MOTION_BUT_CATALOGUE_REJECTED`, `MOTION_NOT_MEASURABLE_WITH_THESE_OBSERVATIONS`, `NOT_ASSESSABLE`.
7. **Variability** (`variability.py`) for variable targets: forced flux grouped into *comparable series* (detector, release, wavelength within half a
   bandwidth); constant-flux χ² per series; field error scale and an empirical false-alarm fraction from the same test applied to Gaia control stars;
   censored 3σ upper limits; straight-line trend (≥ 3 epochs); cross-epoch spectral comparison at matched wavelengths; wavelength-dependent
   amplitude table; caveats (saturation, neighbours, mixed pipeline versions, single detector). **No period is ever fitted**
   (`period_assessment` explains the sampling requirement).
8. **Blind pipeline**: the ordinary HEIMDALL pipeline (`InvestigationRequest.obs_ids` — new field) runs on the *same exposures* without knowing the
   target; the result reports whether it independently detected something at the expected position (one pixel + 3σ), with its run id.
9. **Context**: CatWISE2020 neighbour (cross-catalogue astrometry and W1/W2 → implied AB flux → implied SNR near the band; order-of-magnitude only),
   Gaia neighbours, and for NASA-02 a component/resolution assessment. JPL small-body identification runs inside the blind pipeline for real
   data (these targets are stars/brown dwarfs, not minor planets).
10. **Hypotheses and evidence** (`hypotheses.py`): known/catalogued motion or intrinsic variability, stationary/constant, registration or calibration
    artefact, blend/confusion, uncatalogued mover (disfavoured *because* a catalogue match exists; a missing match would not be evidence),
    insufficient evidence. Each has status, supporting/contradicting evidence IDs, tests run, tests unavailable with reasons, assumptions,
    limitations and a discriminating next step. Evidence items carry a `dependency_group` so correlated results are not counted as independent.
11. **Test status** — `PASSED`: analysis completed with a reported verdict (a non-detection is a valid verdict); `LIMITED_BY_DATA`: observations
    cannot support the test; `FAILED`: could not run (unresolved target, no archive coverage handled separately).

## Stated limits

- Angular motion only; no distance, orbit or physical velocity. No Planet X statement is produced by this module.
- Registration is translation-only; control stars are assumed stationary after Gaia propagation.
- Centroids are not deblended from neighbours; a neighbour inside the stamp is a validity caveat, not a correction.
- SPHEREx samples different wavelengths per exposure; a flux-versus-time list is never treated as one light curve.
- Saturated bright targets (Barnard's Star, Epsilon Indi A, R Dra at maximum) can be unusable; that is reported, not repaired.
- The CatWISE reference epoch is taken as MJD 57170 (documented CatWISE2020 PM-fit epoch); the NASA WISE 0855 position check supports it.
- Epsilon Indi Ba/Bb: Gaia lists one entry, the PSF FWHM is several arcsec; resolution of both components is not claimed.

## Reproduce

```
python3 -m heimdall target resolve NASA-01          # catalogue identification only (no SPHEREx data)
python3 -m heimdall target run NASA-01              # full investigation (network; resumable from .heimdall/cache)
python3 -m heimdall target run --ra 150.78 --dec 3.35 --category VARIABLE_STAR   # any coordinate
python3 -m heimdall target matrix --write docs/NASA_TARGET_MATRIX.md
```
