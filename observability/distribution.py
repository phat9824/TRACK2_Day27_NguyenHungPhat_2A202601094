"""Distribution drift detection.

The starter compared means only, so it was blind to any shift that keeps the
mean roughly constant (variance blow-up, a bimodal split, a currency mix change).
Three complementary signals are combined here:

- **mean/median ratio** -- catches the loud, order-of-magnitude shifts.
- **two-sample Kolmogorov-Smirnov** -- catches shape changes at equal means.
- **PSI** -- the industry drift metric; stable enough to alert on.

KS is implemented on numpy directly: `scipy` is not in `requirements.txt`, it
only arrives as a transitive dependency of Great Expectations.
"""
from __future__ import annotations

from typing import Any, Iterable

import numpy as np

# Conventional PSI reading: < 0.1 stable, 0.1-0.25 moderate, > 0.25 significant.
_PSI_THRESHOLD = 0.25
_KS_MIN_SAMPLES = 8
_PSI_MIN_SAMPLES = 20


def _ks_2samp(current: np.ndarray, baseline: np.ndarray) -> tuple[float, float]:
    """Two-sample KS statistic and asymptotic p-value.

    The p-value uses the Kolmogorov distribution with the Stephens small-sample
    correction, which is what `scipy.stats.ks_2samp(method="asymp")` computes.
    """
    n, m = current.size, baseline.size
    if n == 0 or m == 0:
        return 0.0, 1.0
    grid = np.sort(np.concatenate([current, baseline]))
    cdf_current = np.searchsorted(np.sort(current), grid, side="right") / n
    cdf_baseline = np.searchsorted(np.sort(baseline), grid, side="right") / m
    statistic = float(np.max(np.abs(cdf_current - cdf_baseline)))

    effective_n = np.sqrt(n * m / (n + m))
    lam = (effective_n + 0.12 + 0.11 / effective_n) * statistic
    if lam <= 0:
        return statistic, 1.0
    k = np.arange(1, 101, dtype=float)
    p_value = 2.0 * float(np.sum((-1.0) ** (k - 1) * np.exp(-2.0 * k**2 * lam**2)))
    return statistic, float(min(max(p_value, 0.0), 1.0))


def _psi(current: np.ndarray, baseline: np.ndarray, bins: int = 10) -> float:
    """Population Stability Index over baseline quantile bins."""
    quantiles = np.unique(np.quantile(baseline, np.linspace(0, 1, bins + 1)))
    if quantiles.size < 3:
        return 0.0
    edges = np.concatenate([[-np.inf], quantiles[1:-1], [np.inf]])
    baseline_share = np.histogram(baseline, bins=edges)[0] / baseline.size
    current_share = np.histogram(current, bins=edges)[0] / current.size
    # Smooth empty bins so a single missing bucket does not produce infinity.
    epsilon = 1e-4
    baseline_share = np.clip(baseline_share, epsilon, None)
    current_share = np.clip(current_share, epsilon, None)
    return float(np.sum((current_share - baseline_share) * np.log(current_share / baseline_share)))


def _ratio(current_center: float, baseline_center: float) -> float:
    """Symmetric ratio: 1.0 means identical, larger means further apart."""
    if baseline_center == 0 and current_center == 0:
        return 1.0
    if baseline_center == 0 or current_center == 0:
        return float("inf")
    return float(max(abs(current_center / baseline_center), abs(baseline_center / current_center)))


def detect_distribution_shift(
    current_values: Iterable[float],
    baseline_values: Iterable[float],
    *,
    ratio_threshold: float = 3.0,
    ks_pvalue_threshold: float = 0.01,
    ks_statistic_threshold: float = 0.3,
    psi_threshold: float = _PSI_THRESHOLD,
) -> dict[str, Any]:
    """Return a drift verdict combining ratio, KS and PSI evidence.

    `score` stays comparable to the starter's ratio score so existing callers and
    dashboards keep working; the individual signals are returned alongside it so
    an on-call engineer can see *which* test fired.
    """
    cur = np.asarray(list(current_values), dtype=float)
    base = np.asarray(list(baseline_values), dtype=float)
    cur = cur[np.isfinite(cur)]
    base = base[np.isfinite(base)]
    if cur.size == 0 or base.size == 0:
        return {
            "is_anomaly": False,
            "score": 0.0,
            "method": "ratio+ks+psi",
            "reason": "empty_input",
            "signals": {},
        }

    cur_mean, base_mean = float(np.mean(cur)), float(np.mean(base))
    cur_median, base_median = float(np.median(cur)), float(np.median(base))
    mean_ratio = _ratio(cur_mean, base_mean)
    median_ratio = _ratio(cur_median, base_median)

    ks_statistic, ks_pvalue = 0.0, 1.0
    if cur.size >= _KS_MIN_SAMPLES and base.size >= _KS_MIN_SAMPLES:
        ks_statistic, ks_pvalue = _ks_2samp(cur, base)

    psi = _psi(cur, base) if cur.size >= _PSI_MIN_SAMPLES and base.size >= _PSI_MIN_SAMPLES else 0.0

    ratio_fired = mean_ratio >= ratio_threshold or median_ratio >= ratio_threshold
    ks_fired = ks_pvalue < ks_pvalue_threshold and ks_statistic >= ks_statistic_threshold
    psi_fired = psi >= psi_threshold

    fired = [
        name
        for name, hit in (("ratio", ratio_fired), ("ks", ks_fired), ("psi", psi_fired))
        if hit
    ]
    # Keep the ratio as the headline score; fall back to a KS/PSI-derived score
    # when the shift is one the ratio alone cannot see.
    finite_ratio = mean_ratio if np.isfinite(mean_ratio) else 999.0
    score = float(max(finite_ratio, ks_statistic * 10.0, psi * 10.0))

    return {
        "is_anomaly": bool(fired),
        "score": score,
        "method": "ratio+ks+psi",
        "reason": (
            f"baseline_mean={base_mean:.3f}, current_mean={cur_mean:.3f}, "
            f"mean_ratio={mean_ratio:.2f}, median_ratio={median_ratio:.2f}, "
            f"ks_stat={ks_statistic:.3f}, ks_p={ks_pvalue:.3e}, psi={psi:.3f}, "
            f"fired={fired or 'none'}"
        ),
        "signals": {
            "mean_ratio": mean_ratio,
            "median_ratio": median_ratio,
            "ks_statistic": ks_statistic,
            "ks_pvalue": ks_pvalue,
            "psi": psi,
            "fired": fired,
        },
    }
