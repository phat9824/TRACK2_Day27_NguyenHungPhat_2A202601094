"""Tests for the behaviour added on top of the starter.

Kept in a separate file on purpose: the instructor's own test files are left
byte-for-byte as issued, so `pytest tests_public -q` still proves the original
suite passes rather than a suite that was edited until it agreed with the code.

Every case here failed against the starter implementation before it passed.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from student_api import (
    column_downstream,
    detect_distribution,
    detect_metric,
    multiwindow_burn,
    rag_embedding_shift,
    rag_length_shift,
    validate_orders,
)

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "contracts" / "orders_contract.yaml"


def healthy_df():
    return pd.DataFrame(
        [
            {
                "order_id": 1,
                "customer_id": "C1",
                "amount": 10.0,
                "currency": "USD",
                "status": "completed",
                "created_at": "2026-08-28T10:00:00Z",
                "updated_at": "2026-08-28T10:05:00Z",
            },
            {
                "order_id": 2,
                "customer_id": "C2",
                "amount": 20.0,
                "currency": "USD",
                "status": "pending",
                "created_at": "2026-08-28T10:01:00Z",
                "updated_at": "2026-08-28T10:06:00Z",
            },
        ]
    )


def failed(issues):
    return [i for i in issues if not i["passed"]]


# --------------------------------------------------------------- contracts ---
def test_type_drift_is_detected():
    df = healthy_df()
    df["order_id"] = ["not_an_int", "invalid_id"]
    issues = failed(validate_orders(df, CONTRACT))
    assert any(i["check"] == "type" and i["column"] == "order_id" for i in issues)


def test_numeric_strings_are_type_drift_not_valid_integers():
    """A CSV that starts quoting its ids is a schema regression, not a detail."""
    df = healthy_df()
    df["order_id"] = ["1", "2"]
    issues = failed(validate_orders(df, CONTRACT))
    assert any(i["check"] == "type" and i["column"] == "order_id" for i in issues)


def test_stale_data_is_detected_with_an_explicit_clock():
    df = healthy_df()
    stale = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    df["updated_at"] = [stale, stale]
    issues = failed(validate_orders(df, CONTRACT, reference_time=datetime.now(timezone.utc)))
    assert any(i["check"] == "freshness" and i["column"] == "updated_at" for i in issues)


def test_fresh_data_passes_freshness_with_an_explicit_clock():
    df = healthy_df()
    fresh = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    df["updated_at"] = [fresh, fresh]
    assert not failed(validate_orders(df, CONTRACT, reference_time=datetime.now(timezone.utc)))


def test_historical_fixture_is_not_judged_by_the_wall_clock():
    """The instructor's fixture is dated; running the suite later must not fail it."""
    assert not failed(validate_orders(healthy_df(), CONTRACT))


def test_every_issue_carries_an_action():
    for issue in validate_orders(healthy_df(), CONTRACT):
        assert issue["action"] in {"block", "warn", "log"}


# ----------------------------------------------------------------- anomaly ---
def test_mad_handles_outliers_and_zero_mad():
    identical = [500, 500, 500, 500, 500]
    assert detect_metric(500, identical, method="mad")["is_anomaly"] is False
    assert detect_metric(100, identical, method="mad")["is_anomaly"] is True


def test_quantized_history_does_not_alert_on_noise():
    quantized = [100] * 7
    assert detect_metric(101, quantized, method="mad")["is_anomaly"] is False
    assert detect_metric(30, quantized, method="mad")["is_anomaly"] is True


def test_auto_uses_context_segmentation():
    history_all = [100, 105, 500, 510, 102, 520, 104]
    same_dow = [100, 105, 102, 104, 101]
    result = detect_metric(
        103, history_all, method="auto",
        context={"day_of_week": 5, "same_segment_history": same_dow},
    )
    assert result["is_anomaly"] is False


def test_auto_survives_seasonality_without_a_segment():
    """4 weeks of weekday ~600 / weekend ~250: a normal weekend must not page."""
    history = [600, 610, 595, 605, 598, 250, 245] * 4
    assert detect_metric(250, history, method="auto")["is_anomaly"] is False
    assert detect_metric(20, history, method="auto")["is_anomaly"] is True
    assert detect_metric(150, history, method="auto")["is_anomaly"] is True


def test_auto_tolerates_a_growth_trend():
    growing = [100, 110, 121, 133, 146, 161, 177, 195]
    assert detect_metric(214, growing, method="auto")["is_anomaly"] is False
    assert detect_metric(40, growing, method="auto")["is_anomaly"] is True


def test_outlier_in_history_does_not_hide_a_collapse():
    spiky = [1000, 1005, 995, 1002, 20000, 998, 1004, 1001]
    assert detect_metric(300, spiky, method="auto")["is_anomaly"] is True
    # The starter's z-score is blinded by the outlier-inflated std.
    assert detect_metric(300, spiky, method="zscore")["is_anomaly"] is False


def test_known_event_suppresses_the_page_but_keeps_the_signal():
    history = [100, 102, 101, 99, 103, 98, 100]
    result = detect_metric(
        500, history, method="auto",
        context={"known_event": "flash_sale", "metric_name": "row_count"},
    )
    assert result["is_anomaly"] is False
    assert "suppressed_by_known_event" in result["reason"]
    assert result["raw_is_anomaly"] is True


def test_nonfinite_inputs_are_flagged_not_crashed():
    assert detect_metric(float("nan"), [100, 102, 101, 99, 100], method="mad")["is_anomaly"] is True
    assert detect_metric(float("inf"), [100, 102, 101, 99, 100], method="zscore")["is_anomaly"] is True


# ------------------------------------------------------------ distribution ---
def test_distribution_catches_shape_shifts_at_a_stable_mean():
    rng = np.random.default_rng(27)
    base = rng.normal(100, 5, 300)
    assert detect_distribution(rng.normal(100, 5, 300), base)["is_anomaly"] is False
    assert detect_distribution(rng.normal(100, 30, 300), base)["is_anomaly"] is True
    bimodal = np.concatenate([rng.normal(70, 3, 150), rng.normal(130, 3, 150)])
    assert detect_distribution(bimodal, base)["is_anomaly"] is True


# ---------------------------------------------------------------- SLO burn ---
def test_sustained_fast_burn_pages():
    result = multiwindow_burn(short_window_burn=15.0, long_window_burn=14.8)
    assert result["page"] is True
    assert result["severity"] == "critical"


def test_transient_spike_is_suppressed():
    result = multiwindow_burn(short_window_burn=20.0, long_window_burn=2.0)
    assert result["page"] is False
    assert result["severity"] == "info"


def test_sustained_moderate_burn_pages():
    result = multiwindow_burn(short_window_burn=8.0, long_window_burn=7.0)
    assert result["page"] is True
    assert result["severity"] == "warning"


def test_healthy_budget_is_silent():
    assert multiwindow_burn(short_window_burn=0.3, long_window_burn=0.2)["page"] is False


# ----------------------------------------------------------------- lineage ---
def test_column_lineage_is_transitive_and_cycle_safe():
    graph = {
        "raw_orders.amount": ["stg_orders.amount_usd"],
        "stg_orders.amount_usd": ["fct_daily_revenue.daily_revenue"],
        "fct_daily_revenue.daily_revenue": ["ceo_revenue_dashboard.revenue"],
    }
    assert column_downstream(graph, "raw_orders.amount") == [
        "stg_orders.amount_usd",
        "fct_daily_revenue.daily_revenue",
        "ceo_revenue_dashboard.revenue",
    ]
    assert column_downstream(graph, "nothing.here") == []
    assert column_downstream({"a.x": ["b.x"], "b.x": ["a.x"]}, "a.x") == ["b.x"]


# --------------------------------------------------------------------- RAG ---
def test_embedding_norm_drift():
    rng = np.random.default_rng(27)
    base = list(rng.normal(1.0, 0.02, 200))
    assert rag_embedding_shift(list(rng.normal(1.0, 0.02, 100)), base)["is_anomaly"] is False
    assert rag_embedding_shift(list(rng.normal(0.55, 0.02, 100)), base)["is_anomaly"] is True
    assert rag_embedding_shift([], base)["is_anomaly"] is False


def test_text_length_signals():
    baseline = [40, 42, 39, 41, 43, 40, 42]
    assert rag_length_shift(["x y", "a b c", "one two"], baseline)["is_anomaly"] is True
    assert rag_length_shift(["w " * 41, "w " * 40, "w " * 42], baseline)["is_anomaly"] is False
    assert rag_length_shift([], baseline)["is_anomaly"] is True
