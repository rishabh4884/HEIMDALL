"""Does a multivariate outlier model add value over the chi-square variability rule? A SIMULATED evaluation.

The question (docs/ML_DECISION.md): would an ML-style anomaly score improve the ranking of variable sources
among a field's comparable series? This module answers it empirically on *simulated* series (explicitly not
astronomical data), so the answer is a software-evaluation result, not a claim about SPHEREx.

Methods compared at a fixed false-positive rate (set on held-out constant series, not on the evaluation set):

- ``chi2_formal``: reduced chi-square of the constant-flux fit with the stated flux errors;
- ``chi2_scaled``: the same after dividing by the field error scale estimated from the training controls (what the
  production analysis does);
- ``robust_outlier``: Euclidean distance in robustly standardised feature space
  (log10 reduced chi2, fractional peak-to-peak, max |pull|, lag-1 pull autocorrelation) to the k-th nearest
  neighbour among the training controls (k = 5), a density-style outlier score. Median/MAD scaling, no
  learned weights, deterministic.

Leakage controls: the training controls, the threshold-setting set and the evaluation set are drawn from
disjoint random streams; the evaluation constant and injected series are never used to fit scales or thresholds.
Injected signals are sinusoidal or step changes (the two shapes a sparse-epoch light curve can show) with
fractional amplitudes from 2 to 30 percent. Field systematics are modelled as an unmodelled extra scatter
(error underestimation factor), the failure mode the production error scale is designed to absorb.

Output includes recovery fractions with Wilson intervals, the false-positive rate on held-out constants,
and an explicit adoption decision computed from the intervals (adopt only if the lower bound of the
improvement is positive). No threshold is tuned to produce an outcome.
"""

from __future__ import annotations

import numpy as np

from ..engine.stats import mad_std, wilson_interval

N_EPOCHS = 6
K_NEIGHBOURS = 5
TARGET_FPR = 0.05


def _series(rng: np.random.Generator, n: int, amp: float, shape: str, sys_factor: float) -> tuple[np.ndarray, np.ndarray]:
    """n series of N_EPOCHS fluxes (rows) and their stated sigmas."""
    base = rng.uniform(500.0, 5000.0, n)
    sig = base * rng.uniform(0.01, 0.04, n)  # stated fractional error 1-4 percent
    t = np.sort(rng.uniform(0.0, 1.0, (n, N_EPOCHS)), axis=1)
    signal = np.zeros((n, N_EPOCHS))
    if amp > 0:
        if shape == "sine":
            ph = rng.uniform(0, 2 * np.pi, (n, 1))
            signal = amp * np.sin(2 * np.pi * 1.3 * t + ph)
        else:
            signal = amp * (t > rng.uniform(0.3, 0.7, (n, 1))) - amp / 2
    flux = base[:, None] * (1 + signal) + rng.normal(0, 1, (n, N_EPOCHS)) * sig[:, None] * sys_factor
    return flux, np.repeat(sig[:, None], N_EPOCHS, axis=1)


def features(flux: np.ndarray, sig: np.ndarray) -> np.ndarray:
    w = 1.0 / sig ** 2
    mean = (flux * w).sum(axis=1, keepdims=True) / w.sum(axis=1, keepdims=True)
    pull = (flux - mean) / sig
    chi2 = (pull ** 2).sum(axis=1)
    red = chi2 / (N_EPOCHS - 1)
    ptp = (flux.max(axis=1) - flux.min(axis=1)) / mean[:, 0]
    maxp = np.abs(pull).max(axis=1)
    ac = (pull[:, 1:] * pull[:, :-1]).sum(axis=1) / np.maximum((pull ** 2).sum(axis=1), 1e-12)
    return np.column_stack([np.log10(np.maximum(red, 1e-6)), ptp, maxp, ac])


class RobustOutlier:
    """kNN-distance outlier score in median/MAD-standardised feature space; fitted on training controls only."""

    def fit(self, x: np.ndarray) -> RobustOutlier:
        self.med = np.median(x, axis=0)
        self.scale = np.array([max(mad_std(x[:, j]), 1e-9) for j in range(x.shape[1])])
        self.train = (x - self.med) / self.scale
        return self

    def score(self, x: np.ndarray) -> np.ndarray:
        z = (x - self.med) / self.scale
        d = np.sqrt(((z[:, None, :] - self.train[None, :, :]) ** 2).sum(axis=2))
        d.sort(axis=1)
        return d[:, K_NEIGHBOURS - 1]

    def drift(self, x: np.ndarray) -> dict:
        """Out-of-distribution check for new data: fraction of points whose score exceeds the training 99th percentile."""
        s = self.score(x)
        ref = np.sort(self.score(self.train * self.scale + self.med))
        lim = float(ref[int(0.99 * (len(ref) - 1))])
        return {"fraction_beyond_training_p99": float(np.mean(s > lim)), "training_p99": lim}


def _recovery(scores_var: np.ndarray, thr: float) -> dict:
    k, n = int(np.sum(scores_var > thr)), len(scores_var)
    lo, hi = wilson_interval(k, n)
    return {"recovered": k, "n": n, "fraction": k / n, "ci95": [lo, hi]}


def evaluate(seed: int = 20261114, n_train: int = 400, n_thr: int = 400, n_eval: int = 400,
             amplitudes: tuple[float, ...] = (0.02, 0.05, 0.10, 0.30), sys_factor: float = 1.3) -> dict:
    rng_train, rng_thr, rng_eval = (np.random.default_rng([seed, i]) for i in (1, 2, 3))
    f_tr, s_tr = _series(rng_train, n_train, 0.0, "sine", sys_factor)
    x_tr = features(f_tr, s_tr)
    field_scale = float(np.sqrt(np.median(10 ** x_tr[:, 0]) / ((1 - 2 / (9 * (N_EPOCHS - 1))) ** 3)))  # sqrt(median reduced chi2 / null median)
    ro = RobustOutlier().fit(x_tr)

    def scores(flux, sig):
        x = features(flux, sig)
        return {"chi2_formal": 10 ** x[:, 0], "chi2_scaled": 10 ** x[:, 0] / field_scale ** 2, "robust_outlier": ro.score(x)}

    f_t, s_t = _series(rng_thr, n_thr, 0.0, "sine", sys_factor)
    sc_thr = scores(f_t, s_t)
    thresholds = {m: float(np.quantile(v, 1.0 - TARGET_FPR)) for m, v in sc_thr.items()}
    f_c, s_c = _series(rng_eval, n_eval, 0.0, "sine", sys_factor)
    sc_c = scores(f_c, s_c)
    fpr = {m: {"fraction": float(np.mean(v > thresholds[m])), "ci95": list(wilson_interval(int(np.sum(v > thresholds[m])), len(v)))} for m, v in sc_c.items()}
    rows = []
    for shape in ("sine", "step"):
        for a in amplitudes:
            f_v, s_v = _series(rng_eval, n_eval, a, shape, sys_factor)
            sc_v = scores(f_v, s_v)
            rows.append({"shape": shape, "fractional_amplitude": a, **{m: _recovery(v, thresholds[m]) for m, v in sc_v.items()}})
    # Decision: is the robust outlier's recovery better than the scaled chi2 rule, with non-overlapping evidence in at least one stratum and no worse in any?
    better = [r for r in rows if r["robust_outlier"]["ci95"][0] > r["chi2_scaled"]["ci95"][1]]
    worse = [r for r in rows if r["robust_outlier"]["ci95"][1] < r["chi2_scaled"]["ci95"][0]]
    drift_new = ro.drift(features(*_series(np.random.default_rng([seed, 9]), 200, 0.0, "sine", 2.0)))
    drift_same = ro.drift(features(*_series(np.random.default_rng([seed, 10]), 200, 0.0, "sine", sys_factor)))
    return {"data_origin": "SIMULATED (software evaluation; not astronomical data)", "seed": seed,
            "design": {"n_epochs": N_EPOCHS, "n_train_controls": n_train, "n_threshold_set": n_thr, "n_eval_per_stratum": n_eval,
                       "field_systematic_error_factor": sys_factor, "target_false_positive_rate": TARGET_FPR,
                       "knn_k": K_NEIGHBOURS, "disjoint_random_streams": True},
            "field_error_scale_estimated": field_scale, "thresholds": thresholds, "false_positive_rate_heldout": fpr, "strata": rows,
            "robust_outlier_better_strata": len(better), "robust_outlier_worse_strata": len(worse),
            "adopt_robust_outlier_into_scoring": bool(better and not worse),
            "decision_rule": "adopt only if the robust outlier's recovery interval lies entirely above the scaled chi-square rule in at least one stratum and below in none",
            "drift_check_example": {"same_distribution": drift_same, "doubled_systematics": drift_new},
            "calibration_status": "scores are uncalibrated ranks; they are not probabilities",
            "limitations": ["simulated signals (sinusoid, step) and Gaussian noise; real SPHEREx systematics are not modelled",
                            "six epochs; a larger real sample could change the comparison", "no real labelled sample exists (docs/ML_DECISION.md)"]}
