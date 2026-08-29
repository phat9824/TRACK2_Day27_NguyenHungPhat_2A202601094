"""Incident dashboard for the Data Reliability Game Day.

Built to answer the five questions an on-call engineer actually has, in order:
is something broken, how bad is it, what does it touch, who owns it, and what do
I do now. Every number is read from `reports/latest_metrics.json` -- the dashboard
computes nothing of its own, so what it shows is exactly what the pipeline decided.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "reports" / "latest_metrics.json"
HISTORY = ROOT / "data" / "history" / "metrics_history.csv"
SLO_EVENTS = ROOT / "reports" / "slo_events.csv"

# Who to wake up, and what to read first. In a real deployment these come from
# the contract `owner:` field and a service catalogue; here they are pinned to the
# two datasets the lab ships so the dashboard is never a dead end.
OWNERS = {
    "orders": {
        "team": "commerce-data",
        "slack": "#commerce-data-oncall",
        "runbook": "docs/LAB_GUIDE.md#phase-1--contract--validation",
    },
    "kb_documents": {
        "team": "support-ai",
        "slack": "#support-ai-oncall",
        "runbook": "docs/LAB_GUIDE.md#phase-6--mystery-incident",
    },
}

ACTION_STYLE = {
    "quarantine": ("error", "QUARANTINE - hold the batch, do not promote"),
    "block": ("error", "BLOCK - stop the pipeline"),
    "warn": ("warning", "WARN - promote, but investigate"),
    "log": ("info", "LOG - recorded only"),
    "pass": ("success", "PASS - promote to marts"),
}

st.set_page_config(page_title="Data Reliability Control Room", layout="wide")

if not REPORT.exists():
    st.title("Data Reliability Game Day")
    st.warning("No snapshot yet. Run `make baseline` to generate reports/latest_metrics.json.")
    st.stop()

report = json.loads(REPORT.read_text(encoding="utf-8"))
action = report.get("pipeline_action", "pass")
burn = report.get("burn_policy", {})
slo = report.get("contract_slo", {})
orders_contract = report.get("orders_contract", {})
kb_contract = report.get("kb_contract", {})

# ---------------------------------------------------------------- header ----
st.title("Data Reliability Control Room")
st.caption(f"Snapshot: {report.get('timestamp', 'unknown')}")

tone, headline = ACTION_STYLE.get(action, ("info", action.upper()))
getattr(st, tone)(f"**Pipeline decision: {headline}**")
if burn.get("page"):
    st.error(f"**PAGE ({burn.get('severity')})** - {burn.get('reason')}")
elif burn.get("alert_type") == "ticket":
    st.warning(f"**TICKET ({burn.get('severity')})** - {burn.get('reason')}")
else:
    st.caption(f"Alerting policy: no page - {burn.get('reason', 'n/a')}")

# ------------------------------------------------------------------ KPIs ----
freshness = report.get("freshness_minutes", 0.0)
freshness_limit = report.get("freshness_limit_minutes", 30)
kb_freshness = report.get("kb_freshness_minutes", 0.0)
budget_left = slo.get("remaining_error_budget_fraction", 1.0) * 100

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Orders rows", f"{report.get('orders_rows', 0):,}")
c2.metric(
    "Orders freshness",
    f"{freshness:.0f} min",
    delta=f"limit {freshness_limit} min",
    delta_color="inverse" if freshness > freshness_limit else "normal",
)
c3.metric(
    "KB freshness",
    f"{kb_freshness:.0f} min",
    delta="stale" if kb_freshness > 60 else "fresh",
    delta_color="inverse" if kb_freshness > 60 else "normal",
)
c4.metric(
    "Contract failures",
    orders_contract.get("failed_checks", 0) + kb_contract.get("failed_checks", 0),
    delta=f"{orders_contract.get('critical_failures', 0) + kb_contract.get('critical_failures', 0)} critical",
    delta_color="inverse",
)
c5.metric("Error budget left", f"{budget_left:.0f}%")

# ------------------------------------------------------------------- SLO ----
st.subheader("SLO and error budget")
left, right = st.columns([1, 1])

with left:
    st.dataframe(
        pd.DataFrame(
            [
                {"Measure": "SLO target", "Value": f"{slo.get('target', 0) * 100:.1f}% of checks pass"},
                {"Measure": "Allowed bad rate", "Value": f"{slo.get('allowed_bad_rate', 0) * 100:.2f}%"},
                {"Measure": "Actual bad rate", "Value": f"{slo.get('actual_bad_rate', 0) * 100:.2f}%"},
                {"Measure": "Budget remaining", "Value": f"{budget_left:.1f}%"},
                {"Measure": "Breached", "Value": "yes" if slo.get("breached") else "no"},
            ]
        ),
        hide_index=True,
        width="stretch",
    )
    st.progress(min(max(budget_left / 100, 0.0), 1.0), text="error budget remaining")

with right:
    thresholds = burn.get("thresholds", {})
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Window": "Short (most recent run)",
                    "Burn rate": f"{burn.get('short_window_burn', 0):.2f}x",
                    "Page threshold": f"{thresholds.get('fast', 14.4)}x",
                },
                {
                    "Window": "Long (last 12 runs)",
                    "Burn rate": f"{burn.get('long_window_burn', 0):.2f}x",
                    "Page threshold": f"{thresholds.get('fast', 14.4)}x",
                },
            ]
        ),
        hide_index=True,
        width="stretch",
    )
    st.caption(
        "A page needs **both** windows over threshold: the long window proves the "
        "damage is real, the short window proves it is still happening. That is "
        "what stops a recovered blip from waking anyone up."
    )
    if SLO_EVENTS.exists():
        events = pd.read_csv(SLO_EVENTS)
        events["bad_rate"] = events["bad_events"] / events["total_events"].clip(lower=1)
        st.line_chart(events[["bad_rate"]], height=140)

# --------------------------------------------------------------- signals ----
st.subheader("Detection signals")


def signal_row(name: str, payload: dict, extra: str = "") -> dict:
    return {
        "Signal": name,
        "Firing": "YES" if payload.get("is_anomaly") else "no",
        "Score": f"{payload.get('score', 0):.2f}",
        "Method": payload.get("method", "-"),
        "Note": extra,
    }


signals = [
    signal_row("Order volume", report.get("row_count_anomaly", {})),
    signal_row(
        "Order volume vs same weekday",
        report.get("row_count_vs_same_weekday", {}),
        "diagnostic only - the fixture emits a weekday-sized batch every day",
    ),
    signal_row("Amount distribution", report.get("amount_distribution", {})),
    signal_row("KB text length", report.get("kb_text_length_signal", {})),
    signal_row("KB embedding norms", report.get("kb_embedding_signal", {})),
]
st.dataframe(pd.DataFrame(signals), hide_index=True, width="stretch")

with st.expander("Why a signal fired (full detector reasoning)"):
    for name, key in [
        ("Order volume", "row_count_anomaly"),
        ("Amount distribution", "amount_distribution"),
        ("KB text length", "kb_text_length_signal"),
        ("KB embedding norms", "kb_embedding_signal"),
    ]:
        st.markdown(f"**{name}** - `{report.get(key, {}).get('reason', 'n/a')}`")

# ------------------------------------------------------- contract details ----
st.subheader("Contract violations")
failures = [
    {**detail, "dataset": dataset}
    for dataset, summary in (("orders", orders_contract), ("kb_documents", kb_contract))
    for detail in summary.get("failed_details", [])
]
if failures:
    st.dataframe(
        pd.DataFrame(failures)[["dataset", "severity", "check", "column", "details"]],
        hide_index=True,
        width="stretch",
    )
else:
    st.success("All contract checks passed on both datasets.")

# ----------------------------------------------------------- blast radius ----
st.subheader("Blast radius")
b1, b2 = st.columns(2)
with b1:
    st.markdown("**Dataset level** - what breaks")
    st.code(" -> ".join(["stg_orders", *report.get("sample_blast_radius_from_stg_orders", [])]))
    st.code(" -> ".join(["kb_documents", *report.get("kb_blast_radius", [])]))
with b2:
    st.markdown("**Column level** - which numbers are wrong")
    st.code(" -> ".join(["raw_orders.amount", *report.get("column_blast_radius_from_raw_orders_amount", [])]))
    affected = report.get("datasets_affected_by_amount", [])
    st.caption(f"Datasets carrying a wrong amount: {', '.join(affected) if affected else 'none'}")

# ----------------------------------------------------------------- owners ----
st.subheader("Ownership and runbooks")
st.dataframe(
    pd.DataFrame(
        [
            {
                "Dataset": dataset,
                "Owner": meta["team"],
                "Escalate to": meta["slack"],
                "Runbook": meta["runbook"],
                "Status": "IMPACTED"
                if (dataset == "orders" and orders_contract.get("failed_checks"))
                or (dataset == "kb_documents" and kb_contract.get("failed_checks"))
                else "healthy",
            }
            for dataset, meta in OWNERS.items()
        ]
    ),
    hide_index=True,
    width="stretch",
)

# ---------------------------------------------------------------- history ----
st.subheader("Historical volume")
if HISTORY.exists():
    history = pd.read_csv(HISTORY)
    st.line_chart(history.set_index("date")[["row_count"]], height=220)
    st.caption(
        "The weekly saw-tooth is seasonality, not incidents: weekdays run ~600 rows "
        "and weekends ~250. The detector compares a value against the regime it "
        "belongs to, which is why a normal weekend does not alert."
    )
