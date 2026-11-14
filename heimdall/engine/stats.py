"""Statistical primitives (contract §10.2, SCI-31).

- p-values are upper-tail probabilities of the stated reference distribution,
  computed in log space (regularised upper incomplete gamma by series or
  Lentz continued fraction), so very small values never underflow.
- The Gaussian-equivalent significance uses the two-sided convention
  p = 2 Q(Z), i.e. Z = Q^-1(p/2), with Q the standard-normal survival function.
  With one degree of freedom this gives Z = sqrt(chi2).
- A computed statistic is not a validated significance. Validity is assigned
  by the validation engine.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy import special
from scipy import stats as _st

_LN2 = math.log(2.0)
_MAD_TO_SIGMA = 1.482602218505602  # 1 / Phi^-1(3/4)


def _log_gamma_q_series(a: float, x: float) -> float:
    """log Q(a, x) for x < a + 1, via the series for P(a, x)."""
    term = 1.0 / a
    total = term
    ap = a
    for _ in range(100000):
        ap += 1.0
        term *= x / ap
        total += term
        if abs(term) < abs(total) * 1e-17:
            break
    log_p = math.log(total) - x + a * math.log(x) - math.lgamma(a)
    p = math.exp(log_p)
    return math.log1p(-p) if p < 1.0 else -math.inf


def _log_gamma_q_cf(a: float, x: float) -> float:
    """log Q(a, x) for x >= a + 1, via the Lentz continued fraction."""
    tiny = 1e-300
    b = x + 1.0 - a
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for i in range(1, 100000):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-16:
            break
    return -x + a * math.log(x) - math.lgamma(a) + math.log(h)


def chi2_logsf(statistic: float, dof: float) -> float:
    """Natural log of P(chi2_dof >= statistic)."""
    if not math.isfinite(statistic) or statistic < 0:
        raise ValueError("chi-square statistic must be finite and non-negative")
    if not dof > 0:
        raise ValueError("degrees of freedom must be positive")
    if statistic == 0.0:
        return 0.0
    a = dof / 2.0
    x = statistic / 2.0
    return _log_gamma_q_series(a, x) if x < a + 1.0 else _log_gamma_q_cf(a, x)


def two_sided_sigma_from_logp(log_p: float) -> float:
    """Gaussian-equivalent Z with p = 2 Q(Z), computed from log p (tail-accurate)."""
    if log_p >= 0.0:
        return 0.0
    log_q = log_p - _LN2
    return float(-special.ndtri_exp(log_q))


@dataclass(frozen=True)
class Significance:
    """A test statistic, its p-value and its two-sided Gaussian-equivalent conversion."""

    statistic: float
    dof: int
    reference: str
    log_p: float
    p_value: float
    log10_p: float
    sigma_equivalent: float
    convention: str = "two_sided"
    conversion_method: str = "log-space incomplete gamma (series/Lentz CF); Z = -ndtri_exp(log(p/2))"


def chi2_significance(statistic: float, dof: int) -> Significance:
    log_p = chi2_logsf(statistic, dof)
    return Significance(
        statistic=float(statistic),
        dof=int(dof),
        reference=f"chi2({dof})",
        log_p=log_p,
        p_value=math.exp(log_p),
        log10_p=log_p / math.log(10.0),
        sigma_equivalent=two_sided_sigma_from_logp(log_p),
    )


def bonferroni(sig: Significance, n_tests: int) -> Significance:
    """Correct the most significant of ``n_tests`` tests for the number of tests: p' = min(1, n p)."""
    if n_tests <= 1:
        return sig
    log_p = min(0.0, sig.log_p + math.log(n_tests))
    return Significance(statistic=sig.statistic, dof=sig.dof, reference=f"{sig.reference}, Bonferroni x{n_tests}",
                        log_p=log_p, p_value=math.exp(log_p), log10_p=log_p / math.log(10.0),
                        sigma_equivalent=two_sided_sigma_from_logp(log_p))


def mad_std(values: np.ndarray) -> float:
    v = np.asarray(values, dtype=float)
    v = v[np.isfinite(v)]
    if v.size == 0:
        return math.nan
    return float(_MAD_TO_SIGMA * np.median(np.abs(v - np.median(v))))


def sigma_clipped_stats(values: np.ndarray, sigma: float = 3.0, max_iter: int = 5) -> tuple[float, float, int]:
    """Return (median, robust std, n_used) after iterative clipping."""
    v = np.asarray(values, dtype=float)
    v = v[np.isfinite(v)]
    if v.size == 0:
        return math.nan, math.nan, 0
    for _ in range(max_iter):
        med = float(np.median(v))
        std = mad_std(v)
        if not std > 0:
            break
        keep = np.abs(v - med) <= sigma * std
        if keep.all():
            break
        v = v[keep]
        if v.size < 3:
            break
    return float(np.median(v)), mad_std(v), int(v.size)


@dataclass(frozen=True)
class WeightedMean:
    mean: float
    sigma: float
    chi2: float
    dof: int


def weighted_mean(values: np.ndarray, sigmas: np.ndarray) -> WeightedMean:
    v = np.asarray(values, dtype=float)
    s = np.asarray(sigmas, dtype=float)
    if v.size == 0 or v.shape != s.shape or np.any(~np.isfinite(s)) or np.any(s <= 0):
        raise ValueError("weighted_mean needs matching, finite, positive uncertainties")
    w = 1.0 / s**2
    mean = float(np.sum(w * v) / np.sum(w))
    chi2 = float(np.sum(w * (v - mean) ** 2))
    return WeightedMean(mean=mean, sigma=float(1.0 / math.sqrt(np.sum(w))), chi2=chi2, dof=int(v.size - 1))


def wilson_interval(successes: int, trials: int, confidence: float = 0.95) -> tuple[float, float]:
    if trials <= 0:
        return 0.0, 1.0
    z = float(_st.norm.ppf(0.5 + confidence / 2.0))
    phat = successes / trials
    denom = 1.0 + z * z / trials
    centre = (phat + z * z / (2 * trials)) / denom
    half = z * math.sqrt(phat * (1 - phat) / trials + z * z / (4 * trials * trials)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def poisson_interval(count: int, confidence: float = 0.95) -> tuple[float, float]:
    """Garwood exact interval for a Poisson mean."""
    alpha = 1.0 - confidence
    low = 0.0 if count == 0 else float(_st.chi2.ppf(alpha / 2, 2 * count) / 2)
    high = float(_st.chi2.ppf(1 - alpha / 2, 2 * count + 2) / 2)
    return low, high


def ks_uniform(pvalues: np.ndarray) -> tuple[float, float]:
    """Kolmogorov-Smirnov test of p-values against Uniform(0, 1). Returns (D, p)."""
    p = np.asarray(pvalues, dtype=float)
    p = p[np.isfinite(p)]
    if p.size == 0:
        return math.nan, math.nan
    result = _st.kstest(p, "uniform")
    return float(result.statistic), float(result.pvalue)


def binomial_excess_pvalue(failures: int, trials: int, rate: float) -> float:
    """P(X >= failures) for X ~ Binomial(trials, rate): evidence of an excess false-positive rate."""
    if trials <= 0:
        return math.nan
    return float(_st.binom.sf(failures - 1, trials, rate))


@dataclass(frozen=True)
class PullSummary:
    n: int
    mean: float
    std: float
    fraction_within_1sigma: float


def pull_summary(pulls: np.ndarray) -> PullSummary:
    p = np.asarray(pulls, dtype=float)
    p = p[np.isfinite(p)]
    if p.size == 0:
        return PullSummary(n=0, mean=math.nan, std=math.nan, fraction_within_1sigma=math.nan)
    return PullSummary(
        n=int(p.size),
        mean=float(np.mean(p)),
        std=float(np.std(p, ddof=1)) if p.size > 1 else math.nan,
        fraction_within_1sigma=float(np.mean(np.abs(p) <= 1.0)),
    )
