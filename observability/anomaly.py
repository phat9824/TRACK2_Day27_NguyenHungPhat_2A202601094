"""Anomaly detection.

Every detector returns the same shape (`is_anomaly`, `score`, `method`, `reason`):

- `zscore_detector`: mean/std baseline. Cheap, but one past outlier inflates the
  std and a seasonal metric makes the mean meaningless.
- `mad_detector`: median/MAD baseline. Robust to outliers.
- `trend_detector`: residuals around a forecast, for metrics that grow.
- `detect_anomaly(method="auto")`: picks the baseline and the detector from the
  context the caller supplies, then checks the alert against the historical
  regimes before raising it.
"""
from __future__ import annotations

import math
from typing import Any, Iterable

import numpy as np

# Iglewicz-Hoaglin convention: a modified z-score is comparable to a z-score.
_MAD_CONSTANT = 0.6745
# 1 / (2 * ppf(0.75)) -- turns an IQR into a standard-deviation-like scale.
_IQR_CONSTANT = 0.7413
# Smallest deviation worth a page, as a fraction of the baseline level. Without
# this floor a quantized metric (integer row counts, a near-perfect trend) has a
# spread of ~0, so a 0.1% wobble scores in the hundreds and pages the on-call.
_MIN_RELATIVE_SCALE = 0.01
# History needed before "this value has happened before, regularly" is credible.
_MIN_REGIME_HISTORY = 8


def _finite(values: Iterable[float]) -> np.ndarray:
    array = np.asarray(list(values), dtype=float)
    return array[np.isfinite(array)]


def _robust_scale(values: np.ndarray, center: float) -> tuple[float, str]:
    """Spread estimate plus the name of the estimator that produced it.

    MAD is preferred, but quantized metrics routinely have MAD == 0. Falling back
    to the IQR and then to the standard deviation avoids the degenerate "every
    deviation is infinite" behaviour of the starter code.
    """
    mad = float(np.median(np.abs(values - center)))
    if mad > 0:
        return mad / _MAD_CONSTANT, "mad"
    iqr = float(np.percentile(values, 75) - np.percentile(values, 25))
    if iqr > 0:
        return iqr * _IQR_CONSTANT, "iqr"
    std = float(np.std(values))
    if std > 0:
        return std, "std"
    return 0.0, "degenerate"


def _scale_with_floor(values: np.ndarray, center: float, level: float) -> tuple[float, str]:
    """Robust scale, floored at `_MIN_RELATIVE_SCALE` of the metric's level."""
    scale, estimator = _robust_scale(values, center)
    floor = _MIN_RELATIVE_SCALE * abs(level)
    if floor > scale:
        return floor, f"{estimator}->floor"
    return scale, estimator


def _score(deviation: float, scale: float) -> float:
    if scale > 0:
        return abs(deviation) / scale
    return 0.0 if deviation == 0 else float("inf")


def zscore_detector(current: float, history: Iterable[float], threshold: float = 3.0) -> dict[str, Any]:
    values = _finite(history)
    current = float(current)
    if not np.isfinite(current):
        return {"is_anomaly": True, "score": float("inf"), "method": "zscore", "reason": "current_not_finite"}
    if values.size < 3:
        return {"is_anomaly": False, "score": 0.0, "method": "zscore", "reason": "insufficient_history"}
    mean = float(np.mean(values))
    std = float(np.std(values))
    if std == 0:
        score = float("inf") if current != mean else 0.0
    else:
        score = abs(current - mean) / std
    return {
        "is_anomaly": bool(score > threshold),
        "score": float(score),
        "method": "zscore",
        "reason": f"mean={mean:.3f}, std={std:.3f}, threshold={threshold}",
    }


def mad_detector(current: float, history: Iterable[float], threshold: float = 3.5) -> dict[str, Any]:
    """Modified z-score around the median, with a scale-estimator cascade.

    A zero MAD no longer short-circuits to "not an anomaly": that hid every
    incident on metrics whose history is mostly one repeated value.
    """
    values = _finite(history)
    current = float(current)
    if not np.isfinite(current):
        return {"is_anomaly": True, "score": float("inf"), "method": "mad", "reason": "current_not_finite"}
    if values.size < 5:
        return {"is_anomaly": False, "score": 0.0, "method": "mad", "reason": "insufficient_history"}

    median = float(np.median(values))
    scale, estimator = _scale_with_floor(values, median, median)
    score = _score(current - median, scale)
    return {
        "is_anomaly": bool(score > threshold),
        "score": float(score),
        "method": "mad",
        "reason": f"median={median:.3f}, scale={scale:.3f} ({estimator}), threshold={threshold}",
    }


def _forecast(series: np.ndarray, level: float) -> tuple[float, float, str]:
    """Least-squares forecast of the next point, plus its residual scale."""
    x = np.arange(series.size, dtype=float)
    slope, intercept = np.polyfit(x, series, 1)
    predicted = float(slope * series.size + intercept)
    residuals = series - (slope * x + intercept)
    scale, estimator = _scale_with_floor(residuals, float(np.median(residuals)), level)
    return predicted, scale, estimator


def trend_detector(current: float, history: Iterable[float], threshold: float = 3.5) -> dict[str, Any]:
    """Compare `current` against a forecast instead of a flat baseline.

    A metric that grows every day is not anomalous just because today is the
    largest value ever seen; the residuals of the fit, not the raw values, define
    the expected spread. Two models are fitted -- linear and log-linear -- and the
    one whose residuals are relatively tighter wins, so steady compounding growth
    is modelled as growth rather than as an ever-worsening anomaly.
    """
    values = _finite(history)
    current = float(current)
    if not np.isfinite(current):
        return {"is_anomaly": True, "score": float("inf"), "method": "trend", "reason": "current_not_finite"}
    if values.size < 4:
        return {"is_anomaly": False, "score": 0.0, "method": "trend", "reason": "insufficient_history"}

    level = float(np.mean(np.abs(values))) or 1.0
    predicted, scale, estimator = _forecast(values, level)
    # Relative residual spread, so linear and log-linear fits are comparable.
    best = (scale / level, "linear", _score(current - predicted, scale), predicted, scale, estimator)

    if np.all(values > 0) and current > 0:
        log_predicted, log_scale, log_estimator = _forecast(np.log(values), 1.0)
        log_relative = log_scale  # residuals in log space are already relative
        if log_relative < best[0]:
            best = (
                log_relative,
                "log_linear",
                _score(math.log(current) - log_predicted, log_scale),
                math.exp(log_predicted),
                log_scale,
                log_estimator,
            )

    _, model, score, predicted, scale, estimator = best
    return {
        "is_anomaly": bool(score > threshold),
        "score": float(score),
        "method": "trend",
        "reason": (
            f"model={model}, predicted={predicted:.3f}, "
            f"scale={scale:.4f} ({estimator}), threshold={threshold}"
        ),
    }


def _has_strong_trend(values: np.ndarray) -> bool:
    """True when the history is close to monotonic, so a flat baseline lies."""
    if values.size < 6 or float(np.std(values)) == 0:
        return False
    correlation = float(np.corrcoef(np.arange(values.size, dtype=float), values)[0, 1])
    return abs(correlation) >= 0.9


def _matching_regime(current: float, values: np.ndarray, threshold: float) -> tuple[bool, str]:
    """Does `current` land inside a *recurring* cluster of the history?

    A seasonal metric is multi-modal: weekday traffic and weekend traffic are two
    regimes, and the global median sits between them, so a perfectly normal
    Saturday scores as a huge deviation. Instead of comparing against the whole
    history, compare against the k nearest historical observations: if they form a
    tight cluster around `current`, this level is something the metric reaches
    regularly. A one-off past dip cannot fake a regime -- with too few near
    neighbours, k reaches into the far values and the cluster is not tight.
    """
    n = values.size
    if n < _MIN_REGIME_HISTORY:
        return False, ""
    k = max(3, math.ceil(0.15 * n))
    neighbours = values[np.argsort(np.abs(values - current))[:k]]
    center = float(np.median(neighbours))
    scale, _ = _scale_with_floor(neighbours, center, center)
    score = _score(current - center, scale)
    if score <= threshold:
        return True, f"matches_recurring_regime(center={center:.3f}, k={k}, local_score={score:.2f})"
    return False, ""


def detect_anomaly(
    current: float,
    history: Iterable[float],
    *,
    method: str = "auto",
    threshold: float = 3.0,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Stable lab API.

    - `zscore` / `mad` / `trend`: run that detector directly.
    - `auto`: choose the baseline and detector from `context`:
        * `same_segment_history` -- compare like with like (same weekday, same
          region...). Seasonality is a baseline problem before it is a statistics
          problem, so a caller-supplied segment always wins.
        * `trend`, or a near-monotonic history, switches to a forecast.
        * `known_event` -- a planned deviation (sale, migration, backfill) is
          recorded but never raised as an anomaly.
        * `metric_name`, `day_of_week` -- annotation carried into `reason`.

      With no segment supplied, a positive detection is checked against the
      recurring regimes in the history before it is allowed to page.
    """
    context = context or {}
    history_values = list(history)

    if method == "zscore":
        return zscore_detector(current, history_values, threshold=threshold)
    if method == "mad":
        return mad_detector(current, history_values, threshold=threshold)
    if method == "trend":
        return trend_detector(current, history_values, threshold=threshold)
    if method != "auto":
        raise ValueError(f"Unsupported method: {method}")

    segment = context.get("same_segment_history")
    segment_values = _finite(segment) if segment is not None else np.empty(0)
    if segment_values.size >= 3:
        baseline, baseline_name = segment_values, "same_segment"
    else:
        baseline, baseline_name = _finite(history_values), "all_history"

    use_trend = bool(context.get("trend")) or _has_strong_trend(baseline)
    if use_trend and baseline.size >= 4:
        result = trend_detector(current, baseline, threshold=threshold)
    else:
        result = mad_detector(current, baseline, threshold=threshold)
        if result["reason"] == "insufficient_history":
            # Fewer than 5 points: the median has nothing to stand on.
            result = zscore_detector(current, baseline, threshold=threshold)

    result["method"] = f"auto:{baseline_name}:{result['method']}"
    metric_name = context.get("metric_name")
    if metric_name:
        result["reason"] += f"; metric={metric_name}"
    result["reason"] += f"; baseline={baseline_name}(n={baseline.size})"
    if "day_of_week" in context:
        result["reason"] += f"; day_of_week={context['day_of_week']}"

    # The caller who supplies a segment has already defined "like for like";
    # second-guessing it there would hide real drops inside the segment.
    if result["is_anomaly"] and baseline_name == "all_history" and not use_trend:
        in_regime, regime_reason = _matching_regime(float(current), baseline, threshold)
        if in_regime:
            result["raw_is_anomaly"] = True
            result["is_anomaly"] = False
            result["reason"] += f"; {regime_reason}"

    known_event = context.get("known_event")
    if known_event and result["is_anomaly"]:
        # Keep the evidence, drop the page: an expected deviation is not an incident.
        result["raw_is_anomaly"] = True
        result["is_anomaly"] = False
        result["reason"] += f"; suppressed_by_known_event={known_event}"

    return result
