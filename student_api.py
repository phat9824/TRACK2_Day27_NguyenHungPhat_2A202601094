"""Stable interface used by public and instructor-side hidden evaluation.

Students may refactor internals, but keep these function names and return shapes.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from observability.anomaly import detect_anomaly
from observability.distribution import detect_distribution_shift
from observability.lineage import get_column_downstream, get_downstream_assets
from observability.rag_metrics import detect_embedding_norm_shift, detect_text_length_shift
from observability.slo import calculate_slo, evaluate_multiwindow_burn
from src.contract_validator import load_contract, validate_dataframe


def validate_orders(
    df: pd.DataFrame,
    contract_path: str | Path,
    *,
    reference_time: Any = None,
) -> list[dict[str, Any]]:
    """Validate `df` against the contract at `contract_path`.

    `reference_time` is optional and additive: callers that pass a clock get a
    freshness verdict, callers that do not keep the original two-argument
    behaviour. Freshness is skipped when no clock is available from either the
    caller or the contract, so a historical fixture is never judged stale merely
    because the suite ran on a later day.
    """
    return validate_dataframe(df, load_contract(contract_path), reference_time=reference_time)


def detect_metric(
    current: float,
    history: Iterable[float],
    *,
    method: str = "auto",
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return detect_anomaly(current, history, method=method, context=context)


def detect_distribution(current_values: Iterable[float], baseline_values: Iterable[float]) -> dict[str, Any]:
    return detect_distribution_shift(current_values, baseline_values)


def slo_status(target: float, bad_events: int, total_events: int) -> dict[str, Any]:
    return calculate_slo(target, bad_events, total_events)


def multiwindow_burn(short_window_burn: float, long_window_burn: float) -> dict[str, Any]:
    return evaluate_multiwindow_burn(
        short_window_burn=short_window_burn,
        long_window_burn=long_window_burn,
    )


def downstream_assets(graph: dict[str, list[str]], start: str) -> list[str]:
    return get_downstream_assets(graph, start)


def column_downstream(graph: dict[str, list[str]], start: str) -> list[str]:
    return get_column_downstream(graph, start)


def rag_length_shift(current_texts: Iterable[str], baseline_batch_means: Iterable[float]) -> dict[str, Any]:
    return detect_text_length_shift(current_texts, baseline_batch_means)


def rag_embedding_shift(current_norms: Iterable[float], baseline_norms: Iterable[float]) -> dict[str, Any]:
    return detect_embedding_norm_shift(current_norms, baseline_norms)
