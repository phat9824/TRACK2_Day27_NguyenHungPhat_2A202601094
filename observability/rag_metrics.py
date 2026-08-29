"""RAG / knowledge-base quality signals.

A retrieval pipeline fails quietly: the index still returns k documents, the
agent still answers, and nothing raises an exception. These detectors watch the
two cheap proxies that move first when the KB breaks -- how long documents are,
and how the embedding vectors are distributed.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Iterable

import numpy as np

from observability.anomaly import detect_anomaly, mad_detector
from observability.distribution import detect_distribution_shift


def approximate_token_lengths(texts: Iterable[str]) -> list[int]:
    # Deliberately simple proxy; no tokenizer/model download needed.
    return [0 if text is None else len(str(text).split()) for text in texts]


def approximate_embedding_norms(texts: Iterable[str]) -> list[float]:
    """L2 norm of each document's normalised term-frequency vector.

    A stand-in for a real embedding, chosen because it needs no model download
    and still moves for the failures this lab cares about: truncation, boilerplate
    duplication, and a document collapsing to a handful of repeated tokens all
    change how concentrated the vector is. A document with all-distinct tokens
    tends towards 1/sqrt(n); one repeated token gives exactly 1.0.

    Swap this for real `model.encode(...)` norms and every detector downstream
    keeps working unchanged -- that is the point of measuring norms rather than
    vectors.
    """
    norms: list[float] = []
    for text in texts:
        tokens = str(text or "").lower().split()
        if not tokens:
            norms.append(0.0)
            continue
        counts = np.array(list(Counter(tokens).values()), dtype=float)
        norms.append(float(np.linalg.norm(counts) / len(tokens)))
    return norms


def detect_text_length_shift(
    current_texts: Iterable[str],
    baseline_batch_means: Iterable[float],
    *,
    threshold: float = 3.0,
) -> dict[str, Any]:
    """Flag a batch whose mean document length left the historical envelope.

    Truncated ingestion, a broken HTML parser, or a chunker regression all show
    up here long before anyone notices the answers got worse. The robust `auto`
    detector is used so one unusually long historical batch cannot widen the
    baseline enough to hide a collapse.
    """
    lengths = approximate_token_lengths(current_texts)
    current_mean = float(np.mean(lengths)) if lengths else 0.0
    result = detect_anomaly(
        current_mean,
        baseline_batch_means,
        method="auto",
        threshold=threshold,
        context={"metric_name": "mean_text_length"},
    )
    result["metric"] = "mean_text_length"
    result["current_mean"] = current_mean
    result["current_batch_size"] = len(lengths)
    if not lengths:
        # An empty KB batch is an incident on its own, whatever the history says.
        result["is_anomaly"] = True
        result["reason"] = "empty_current_batch"
    return result


def detect_embedding_norm_shift(
    current_norms: Iterable[float],
    baseline_norms: Iterable[float],
    *,
    threshold: float = 3.5,
) -> dict[str, Any]:
    """Detect embedding-space drift from precomputed vector norms.

    No embedding model is needed: the norms are enough to catch the failures that
    matter operationally -- a swapped/renormalised embedding model, an index
    rebuilt from truncated text, or a batch of near-empty vectors. Two signals:

    - **centre shift**: the current median norm scored against the baseline's own
      robust spread (median/MAD), so a tight baseline is not loosened by outliers.
    - **shape shift**: a KS/PSI comparison of the two norm distributions, which
      fires when the spread splits or collapses while the mean barely moves.
    """
    cur = np.asarray(list(current_norms), dtype=float)
    base = np.asarray(list(baseline_norms), dtype=float)
    cur = cur[np.isfinite(cur)]
    base = base[np.isfinite(base)]
    if cur.size == 0 or base.size == 0:
        return {
            "is_anomaly": False,
            "score": 0.0,
            "method": "embedding_norm_drift",
            "reason": "empty_input",
            "signals": {},
        }

    current_median = float(np.median(cur))
    centre = mad_detector(current_median, base, threshold=threshold)
    if centre["reason"] == "insufficient_history":
        # Too few baseline norms for a median: fall back to a mean/std z-score.
        base_mean, base_std = float(np.mean(base)), float(np.std(base))
        centre_score = abs(current_median - base_mean) / base_std if base_std > 0 else (
            0.0 if current_median == base_mean else float("inf")
        )
        centre = {
            "is_anomaly": bool(centre_score > threshold),
            "score": float(centre_score),
            "reason": f"small_baseline mean={base_mean:.4f}, std={base_std:.4f}",
        }

    shape = detect_distribution_shift(cur, base)

    is_anomaly = bool(centre["is_anomaly"] or shape["is_anomaly"])
    score = float(max(centre["score"] if np.isfinite(centre["score"]) else 999.0, shape["score"]))
    return {
        "is_anomaly": is_anomaly,
        "score": score,
        "method": "embedding_norm_drift",
        "reason": (
            f"baseline_median={float(np.median(base)):.4f}, current_median={current_median:.4f}; "
            f"centre[{centre['reason']}]; shape[{shape['reason']}]"
        ),
        "signals": {
            "centre_shift_score": float(centre["score"]),
            "centre_shift_fired": bool(centre["is_anomaly"]),
            "distribution_fired": bool(shape["is_anomaly"]),
            "distribution_signals": shape.get("signals", {}),
        },
    }
