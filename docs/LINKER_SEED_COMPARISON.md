# Linker robustness: second-seed validation (2026-10-30)

## What the seed controls (from the code)

`ValidationParams.seed` feeds `validation._rng(config, label)` (a SHA-256 of `seed:label`). In `moving_object_injection` it draws **only the injected bodies**:
heliocentric distance (log-uniform 100-2000 AU), sky offset, direction and speed. Everything else is deterministic and seed-independent: the real
images, detection, association (`association-1.1.0`), the baseline real-field link inputs, the linker (`linking.py`, no random numbers), the motion fits,
the attribution and the acceptance criterion. The seed is part of the engine configuration, so the config hash differs between the two runs
(`0346f789...` vs `833b30c6...`); `linking.py`, `validation.py`, `config.py`, `planetx.py` and `small_bodies.py` have identical SHA-256 in both manifests
(only `runner.py` differs, by the investigation stage, which is not used by this procedure).

So "seed robustness" here means: do two **independent random samples of 100 injected bodies** on the same field give statistically compatible results?
It does not test other fields, cadences, source types or the linker's behaviour on real moving objects.

## Runs

Same procedure, defaults and inputs (75 cached QR2 D2 cutouts, 0.3 deg field, offline, `validation-1.3.0`, config 1.2.0-provisional, 100 experiments x 1 body,
`link_rate_info_fraction` 1.0, `min_link_dof` 3, `link_gof_min_p` 0.01, `max_link_evaluations` 200,000). Only `seed` differs.

| | original, seed 20261029 | new, seed 20261031 |
|---|---|---|
| started / runtime | 2026-10-29, 10,168 s | 2026-10-30 05:59 UTC, 6,953 s |
| validator outcome | VALIDATED_FOR_CONFIG | VALIDATED_FOR_CONFIG |
| truncated experiments | 0 | 0 |
| covered / recovered | 100 / 56 (Wilson 95% 0.46-0.65) | 100 / 59 (0.49-0.68) |
| track classes | pure 56, mixed 15, ambiguous 2, no-body 1 | pure 59, mixed 12, ambiguous 2, no-body 0 |
| definite spurious (per experiment, Garwood upper 95%) | 16 (0.16, 0.26) | 12 (0.12, 0.21) |
| conservative spurious (incl. ambiguous) | 18 (upper 0.284) | 14 (upper 0.235) |
| body outcomes | recovered 56, mixed-only 13, ambiguous-only 1, no track 30 | recovered 59, mixed-only 12, ambiguous-only 2, no track 27 |
| chains rejected by effective dof / absorbed | 512 / 10 | 479 / 6 |
| baseline tracks without injection | 0 | 0 |
| baseline link inputs (real field, no injection) | 7 | 7 |

## Comparison (descriptive; no success threshold was invented)

- Recovery 56/100 vs 59/100: Fisher exact two-sided p = 0.78.
- Definite spurious tracks 16 vs 12: conditional binomial test for equal rates p = 0.57. Conservative 18 vs 14: p = 0.60.
- Pooled over 200 experiments: definite 0.14 per experiment (Garwood 95% 0.093-0.202); conservative 0.16 (0.109-0.226). Both are below the existing acceptance
  bound (upper 95% <= 0.5 per experiment), which is the criterion the validator already applies; it was not changed.
- Per distance bin (100-271 / 271-737 / 737-2000 AU) recovered 17/34, 22/35, 17/31 vs 17/37, 22/29, 20/34; per flux level 13/34, 20/33, 23/33 vs 14/34, 21/33, 24/33. Bin sizes differ because distances are random.
- The share of bodies with no track at all is similar (30 vs 27), as is the effective-dof rejection count (512 vs 479).
- Recurring real-field detections in mixed/ambiguous tracks (keyed by image and 0.5 arcsec position): 23 distinct in the original run, 13 in the new run, **10 shared**.
  The same few real detections (for example two at about RA 150.85, Dec 3.355 in images 2025W47_1A_0657 and 2025W47_2A_0507) produce false partners in both
  samples. That part of the false-link behaviour is a deterministic property of this field, not noise from the seed.
- Downstream: the saved real run has 0 tracks, so its investigations contain no track subject and are unaffected. The seed enters the investigations only through the
  Planet X region statement, which is `COMPLETENESS_LIMITED` in both cases with different recovery numbers (56/100 vs 59/100).

## Interpretation

No instability was found: the two independent samples are statistically compatible on recovery and on false links, the criterion-level outcome is the same, and the
deterministic parts (baseline links, effective-dof rejections' scale) match. The differences (3 more recoveries, 4 fewer spurious tracks) are what two 100-body
samples are expected to differ by under the binomial/Poisson tests above.

What this does **not** establish:

- A non-significant test does not prove equality. With 100 bodies per sample, only large differences (roughly 0.2 or more in recovery fraction, at 80% power) are detectable.
- Both samples use the same field, cadence, constant-flux point-source injections and the same linker design decisions (made while examining the first sample's bodies;
  the second sample is independent of that design, which is its value). Other fields, variable or extended sources, and real moving objects are untested.
- `VALIDATED_FOR_CONFIG` refers to the false-link criterion only. Completeness stays low (about 0.56-0.59 given two-epoch coverage, 27-30% of bodies with no track),
  so absence of a track is still never evidence of absence of a body.
- The remaining known issues are unchanged: mixed tracks from recurring real-field partner detections, bodies with no track (split tracklets), and legitimate tracks lost to the effective-dof rule.

## Records

Artifacts (not in the repository, in the session temp directory): `.../opencode/validation_runs/seed2_20261030/` (`validation_run.json`, `run_manifest.json`, `console.log`,
`fresh_moi.py`, `compare.py`, `compare_output.json`, `original_md5_before.txt`). The original `fresh_20261030_effdof_ownership/` files were checksummed before
the run and are unchanged. Reproduce: run `rca/run_analysis.py` with `fresh_moi.py` (it captures the offline pipeline inputs, about 100 s, then runs the validator).
