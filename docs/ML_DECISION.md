# Machine learning: evaluated, not adopted (yet)

Decision date: 2026-10-29. Status: **not justified with current evidence**.

## Questions where ML could help

1. Real/bogus classification of detections (artefact versus astrophysical source).
2. Anomaly ranking of light curves or spectra.
3. Moving-object linking.

## Why not now

| Requirement for a defensible model | Current state |
|---|---|
| Labelled training data from SPHEREx | None. No labelled artefact or real-source sample exists for SPHEREx L2 images; synthetic injections give positive examples only, so a classifier would learn the injection model rather than the instrument. |
| Enough independent samples for train/validation/test without leakage | The real-data verification used 16 cutouts of one field. Splits by sky region and epoch would leave a handful of fields. |
| A baseline to beat | The interpretable baselines (matched filter, PSF chi-square, flag rules, null-test-calibrated statistics) are implemented, but their real-data error rates are not yet measured, so an ML gain could not be shown. |
| Honest false-positive estimation | Requires a held-out labelled set; not available. |
| Interpretability for the evidence panel | Rule-based checks give per-candidate reasons; an ML score would need its own validation before it could enter the attention rules. |

An ML score would also conflict with the contract's rule that no anomaly score
exists in v1 (`Candidate.anomaly` must be `U/NOT_APPLICABLE`) unless its exact
meaning and validation are defined.

## Conditions to revisit

- At least several thousand real detections labelled by independent vetting
  (cross-match to an external catalogue for real sources, flags and visual
  vetting for artefacts), spanning many fields, detectors and epochs.
- Splits by field and epoch, never by detection, to prevent leakage.
- A comparison with the rule-based baseline on the same held-out set,
  reporting false-positive rate at fixed completeness, calibration and
  robustness to detector or release changes.
- Interpretable diagnostics (feature importance, failure examples), a defined
  meaning for any score, and a validation scope cell like any other statistic.

## Update 2026-11-14: a simulated evaluation of one candidate method (`heimdall/targets/ml_eval.py`)

Question: for ranking variable sources among a field's comparable series, does a robust multivariate outlier model (kNN distance in
median/MAD-standardised feature space: log reduced chi2, fractional peak-to-peak, max |pull|, lag-1 autocorrelation) beat the chi-square rule
the production analysis uses? Run: `python3 -c "from heimdall.targets.ml_eval import evaluate; import json; print(json.dumps(evaluate(), indent=1))"`.

Design (all SIMULATED, deterministic seed 20261114): six epochs; 400 constant control series to fit scales/neighbours, a disjoint set of 400 to set a 5%
false-positive threshold, a third disjoint set of 400 per stratum to evaluate; injected sinusoid and step signals of 2, 5, 10 and 30 percent; stated errors
underestimated by 1.3 (field systematics). No threshold was tuned toward an outcome.

Result (400 per stratum; recovered fraction at 5% held-out false-positive rate, chi2 rule / robust outlier): sine 2%: 0.13 / 0.10; 5%: 0.46 / 0.39;
10%: 0.85 / 0.81; 30%: 0.99 / 0.99. Step 2%: 0.10 / 0.09; 5%: 0.27 / 0.25; 10%: 0.66 / 0.61; 30%: 0.94 / 0.94. Held-out false-positive rate 0.050 (chi2) and
0.038 (robust outlier). The robust outlier was never significantly better and never significantly worse than the chi2 rule; the pre-stated adoption
rule (interval strictly above in at least one stratum, below in none) is **not met**. `chi2_formal` and `chi2_scaled` give identical rankings because the
error scale is a monotone rescaling; it matters for the *significance* the production analysis reports, not for ranking.

Decision: **not adopted into scoring.** It adds no measurable value over the interpretable statistic on this simulated problem, and no labelled real
sample exists to show otherwise. What was kept: the evaluation harness (reproducible), and the out-of-distribution check idea (fraction of new points beyond the
training 99th percentile: 0% for same-distribution controls versus 23% when systematics are doubled) as a candidate drift diagnostic. Scores are
uncalibrated ranks, not probabilities. The target investigations use the chi2 rule with the Gaia-control field error scale and false-alarm reference
instead. The conditions in the previous section still apply before any real-data model is trained.
