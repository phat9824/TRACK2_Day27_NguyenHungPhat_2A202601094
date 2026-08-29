#!/usr/bin/env python3
from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "data" / "baseline"
INCOMING = ROOT / "data" / "incoming"


def shift_dataframe_timestamps(df: pd.DataFrame, columns: list[str], target_age_minutes: int = 5) -> pd.DataFrame:
    parsed = []
    for col in columns:
        if col in df.columns:
            parsed.append(pd.to_datetime(df[col], utc=True, errors="coerce"))
    if not parsed:
        return df
    latest = max(s.max() for s in parsed if s.notna().any())
    target = pd.Timestamp(datetime.now(timezone.utc) - timedelta(minutes=target_age_minutes))
    delta = target - latest
    for col in columns:
        if col in df.columns:
            s = pd.to_datetime(df[col], utc=True, errors="coerce")
            df[col] = (s + delta).dt.strftime("%Y-%m-%dT%H:%M:%S%z")
    return df


def main() -> None:
    INCOMING.mkdir(parents=True, exist_ok=True)
    orders = pd.read_csv(BASE / "orders.csv")
    orders = shift_dataframe_timestamps(orders, ["created_at", "updated_at"], target_age_minutes=5)
    orders.to_csv(INCOMING / "orders.csv", index=False)

    shutil.copy2(BASE / "customers.csv", INCOMING / "customers.csv")

    docs = []
    with open(BASE / "kb_documents.jsonl", "r", encoding="utf-8") as f:
        for line in f:
            docs.append(json.loads(line))
    # Re-anchor publish times so the starter dataset is always fresh when class runs.
    now = datetime.now(timezone.utc)
    for i, doc in enumerate(docs):
        doc["published_at"] = (now - timedelta(minutes=10 + i * 2)).isoformat()
    with open(INCOMING / "kb_documents.jsonl", "w", encoding="utf-8") as f:
        for row in docs:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    # Keep dbt seeds synchronized with current incoming data.
    seeds = ROOT / "dbt_project" / "seeds"
    seeds.mkdir(parents=True, exist_ok=True)
    shutil.copy2(INCOMING / "orders.csv", seeds / "orders.csv")
    shutil.copy2(INCOMING / "customers.csv", seeds / "customers.csv")

    metrics = ROOT / "reports" / "latest_metrics.json"
    if metrics.exists():
        metrics.unlink()
    # The burn-rate policy reads this run log; a stale one would carry a previous
    # incident's error budget into a freshly reset lab.
    slo_events = ROOT / "reports" / "slo_events.csv"
    if slo_events.exists():
        slo_events.unlink()
    print("Lab reset to a healthy baseline.")


if __name__ == "__main__":
    main()
