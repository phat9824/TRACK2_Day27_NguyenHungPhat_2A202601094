#!/usr/bin/env python3
"""Produce one reliability snapshot of the incoming data.

Everything the dashboard, the SLO policy and the incident report need comes from
this single run. What the starter left undone and is wired up here:

- the knowledge base was never validated at all (its contract uses `fields:`,
  which the starter validator did not read), so `stale_kb` was invisible;
- freshness now has an explicit reference clock, so stale data is caught;
- the SLI is the whole contract-check population, not a single synthetic event;
- burn rate is evaluated over two windows from a persisted run log, so a one-off
  bad run is distinguishable from sustained damage;
- blast radius is reported at column level as well as dataset level.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from observability.anomaly import detect_anomaly
from observability.distribution import detect_distribution_shift
from observability.lineage import (
    get_affected_datasets,
    get_column_downstream,
    get_downstream_assets,
)
from observability.rag_metrics import (
    approximate_embedding_norms,
    detect_embedding_norm_shift,
    detect_text_length_shift,
)
from observability.slo import calculate_slo, evaluate_multiwindow_burn
from src.contract_validator import (
    determine_action,
    failed_issues,
    load_contract,
    summarize,
    validate_dataframe,
)
from src.io_utils import load_jsonl, load_yaml

SLO_EVENT_LOG = ROOT / "reports" / "slo_events.csv"
# Windows for the burn-rate policy, in runs rather than hours: this lab produces
# one pipeline run at a time, so "recent" and "sustained" are counted in runs.
SHORT_WINDOW_RUNS = 1
LONG_WINDOW_RUNS = 12


def _append_slo_event(bad_events: int, total_events: int, timestamp: str) -> pd.DataFrame:
    """Append this run to the SLO log and return the full history."""
    SLO_EVENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    row = pd.DataFrame([{"timestamp": timestamp, "bad_events": bad_events, "total_events": total_events}])
    history = pd.concat([pd.read_csv(SLO_EVENT_LOG), row], ignore_index=True) if SLO_EVENT_LOG.exists() else row
    history.to_csv(SLO_EVENT_LOG, index=False)
    return history


def _window_burn(history: pd.DataFrame, runs: int, target: float) -> float:
    window = history.tail(runs)
    return calculate_slo(
        target,
        bad_events=int(window["bad_events"].sum()),
        total_events=int(window["total_events"].sum()),
    )["burn_rate"]


def main() -> None:
    now = datetime.now(timezone.utc)
    config = load_yaml(ROOT / "lab_config.yaml")
    contract_target = config["slo"]["critical_contract_pass"]["target"]
    freshness_limit = config["slo"]["revenue_freshness"]["threshold_minutes"]

    # ---- orders contract -------------------------------------------------
    orders = pd.read_csv(ROOT / "data" / "incoming" / "orders.csv")
    orders_contract = load_contract(ROOT / "contracts" / "orders_contract.yaml")
    orders_issues = validate_dataframe(orders, orders_contract, reference_time=now)
    orders_summary = summarize(orders_issues)

    # ---- knowledge-base contract (never validated by the starter) --------
    kb_docs = load_jsonl(ROOT / "data" / "incoming" / "kb_documents.jsonl")
    kb_df = pd.DataFrame(kb_docs)
    kb_contract = load_contract(ROOT / "contracts" / "kb_contract.yaml")
    kb_issues = validate_dataframe(kb_df, kb_contract, reference_time=now)
    kb_summary = summarize(kb_issues)

    # ---- freshness -------------------------------------------------------
    updated = pd.to_datetime(orders["updated_at"], utc=True, errors="coerce")
    freshness_minutes = (pd.Timestamp(now) - updated.max()).total_seconds() / 60.0
    kb_published = pd.to_datetime(kb_df["published_at"], utc=True, errors="coerce")
    kb_freshness_minutes = (pd.Timestamp(now) - kb_published.max()).total_seconds() / 60.0

    # ---- volume anomaly --------------------------------------------------
    # The alert runs against the full 28-day history: the history is seasonal
    # (weekdays ~600, weekends ~250) and `auto` resolves that by checking the value
    # against the recurring regimes rather than against a single flat mean.
    #
    # The same-weekday comparison is kept as a *diagnostic only*. The lab fixture
    # emits a weekday-sized batch every day, so on a weekend it disagrees with the
    # segment by construction -- pointing the alert at it would page the on-call
    # every Saturday. It is still worth showing: "normal overall, high for a
    # Saturday" is exactly the sentence an on-call engineer wants.
    history = pd.read_csv(ROOT / "data" / "history" / "metrics_history.csv")
    current_dow = now.weekday()
    row_history = history["row_count"].tail(28).tolist()
    row_result = detect_anomaly(
        len(orders),
        row_history,
        method="auto",
        context={"metric_name": "orders_row_count", "day_of_week": current_dow},
    )
    same_weekday = history.loc[history["day_of_week"] == current_dow, "row_count"].tail(8).tolist()
    row_result_same_weekday = detect_anomaly(
        len(orders),
        row_history,
        method="auto",
        context={
            "metric_name": "orders_row_count",
            "day_of_week": current_dow,
            "same_segment_history": same_weekday,
        },
    )

    # ---- amount distribution vs the healthy baseline ---------------------
    baseline_orders = pd.read_csv(ROOT / "data" / "baseline" / "orders.csv")
    amount_result = detect_distribution_shift(orders["amount"], baseline_orders["amount"])

    # ---- RAG / knowledge-base signals ------------------------------------
    kb_texts = [doc.get("content", "") for doc in kb_docs]
    text_result = detect_text_length_shift(kb_texts, history["mean_text_length"].tail(14).tolist())
    baseline_texts = [
        doc.get("content", "")
        for doc in load_jsonl(ROOT / "data" / "baseline" / "kb_documents.jsonl")
    ]
    embedding_result = detect_embedding_norm_shift(
        approximate_embedding_norms(kb_texts),
        approximate_embedding_norms(baseline_texts),
    )

    # ---- SLO and multi-window burn rate ----------------------------------
    all_issues = orders_issues + kb_issues
    bad_events = len(failed_issues(all_issues, min_severity="critical"))
    total_events = len(all_issues)
    contract_slo = calculate_slo(contract_target, bad_events=bad_events, total_events=total_events)

    event_history = _append_slo_event(bad_events, total_events, now.isoformat())
    short_burn = _window_burn(event_history, SHORT_WINDOW_RUNS, contract_target)
    long_burn = _window_burn(event_history, LONG_WINDOW_RUNS, contract_target)
    burn_policy = evaluate_multiwindow_burn(short_window_burn=short_burn, long_window_burn=long_burn)

    # ---- blast radius ----------------------------------------------------
    lineage = json.loads((ROOT / "data" / "baseline" / "lineage_graph.json").read_text(encoding="utf-8"))
    blast_radius = get_downstream_assets(lineage["dataset_lineage"], "stg_orders")
    column_graph = lineage.get("column_lineage", {})
    amount_blast = get_column_downstream(column_graph, "raw_orders.amount")
    kb_blast = get_downstream_assets(lineage["dataset_lineage"], "kb_documents")

    action = determine_action(all_issues)
    report = {
        "timestamp": now.isoformat(),
        "orders_rows": int(len(orders)),
        "kb_docs": int(len(kb_df)),
        "failed_contract_checks": orders_summary["failed_checks"],
        "critical_contract_failures": orders_summary["critical_failures"],
        "orders_contract": orders_summary,
        "kb_contract": kb_summary,
        "pipeline_action": action,
        "freshness_minutes": freshness_minutes,
        "freshness_limit_minutes": freshness_limit,
        "kb_freshness_minutes": kb_freshness_minutes,
        "row_count_anomaly": row_result,
        "row_count_vs_same_weekday": row_result_same_weekday,
        "amount_distribution": amount_result,
        "kb_text_length_signal": text_result,
        "kb_embedding_signal": embedding_result,
        "contract_slo": contract_slo,
        "burn_policy": burn_policy,
        "sample_blast_radius_from_stg_orders": blast_radius,
        "column_blast_radius_from_raw_orders_amount": amount_blast,
        "datasets_affected_by_amount": get_affected_datasets(column_graph, "raw_orders.amount"),
        "kb_blast_radius": kb_blast,
    }
    out = ROOT / "reports" / "latest_metrics.json"
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    print("=== DATA RELIABILITY BASELINE ===")
    print(f"orders rows              : {len(orders)}")
    print(f"orders contract          : {orders_summary['failed_checks']} failed "
          f"({orders_summary['critical_failures']} critical) of {orders_summary['total_checks']}")
    print(f"kb contract              : {kb_summary['failed_checks']} failed "
          f"({kb_summary['critical_failures']} critical) of {kb_summary['total_checks']}")
    print(f"pipeline action          : {action.upper()}")
    print(f"row-count anomaly        : {row_result['is_anomaly']} "
          f"({row_result['method']}, score={row_result['score']:.2f})")
    print(f"  vs same weekday (dow {current_dow})  : {row_result_same_weekday['is_anomaly']} "
          f"(score={row_result_same_weekday['score']:.2f}, diagnostic only)")
    print(f"amount distribution      : {amount_result['is_anomaly']} "
          f"(fired={amount_result['signals'].get('fired')})")
    print(f"orders freshness minutes : {freshness_minutes:.1f} (limit {freshness_limit})")
    print(f"kb freshness minutes     : {kb_freshness_minutes:.1f}")
    print(f"KB length anomaly        : {text_result['is_anomaly']}")
    print(f"KB embedding drift       : {embedding_result['is_anomaly']} (score={embedding_result['score']:.2f})")
    print(f"error budget remaining   : {contract_slo['remaining_error_budget_fraction'] * 100:.1f}%")
    print(f"burn rate short/long     : {short_burn:.2f}x / {long_burn:.2f}x -> "
          f"{burn_policy['alert_type'].upper()} ({burn_policy['severity']})")
    print(f"blast radius (dataset)   : {', '.join(blast_radius)}")
    print(f"blast radius (column)    : {', '.join(amount_blast)}")
    print(f"report                   : {out.relative_to(ROOT)}")

    for issue in failed_issues(all_issues):
        print(f"  ! {issue['severity']:<8} {issue['check']:<16} {issue['column']}: {issue['details']}")


if __name__ == "__main__":
    main()
