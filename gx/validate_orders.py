#!/usr/bin/env python3
"""Great Expectations Core 1.21 validation flow for the orders dataset.

The starter validated four hand-written expectations one at a time. This builds
the full object chain GX is designed around:

    contract YAML -> ExpectationSuite -> ValidationDefinition -> Checkpoint -> action

Two decisions worth defending:

1. **The suite is generated from `contracts/orders_contract.yaml`.** The contract
   is the single source of truth; the Python validator and the GX suite are two
   engines reading it. A rule added to the YAML shows up in both, so they cannot
   drift apart and disagree about what "valid" means.
2. **Severity drives an action, not just a boolean.** GX tells you *what* failed;
   the pipeline still has to decide whether to block, quarantine or merely warn.
   That policy lives in `src/contract_validator.determine_action`, so the GX path
   and the pandas path reach the same verdict on the same data.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    import great_expectations as gx
except ImportError as exc:  # friendlier classroom failure
    raise SystemExit("great_expectations is not installed. Run: pip install -r requirements.txt") from exc

from src.contract_validator import determine_action, load_contract

CONTRACT_PATH = ROOT / "contracts" / "orders_contract.yaml"

# Contract type -> accepted pandas/python types. GX resolves these against the
# *values* in an object column, not the column dtype, so `str` has to be listed
# explicitly: a plain "object" entry passes for anything at all.
_TYPE_LISTS = {
    "integer": ["int", "int64", "int32", "Int64"],
    "number": ["float", "float64", "float32", "int", "int64"],
    "string": ["str", "string", "object"],
}


def _type_expectation(column: str, declared_type: str, meta: dict[str, Any]) -> Any | None:
    """Pick the expectation that actually enforces a declared contract type.

    Timestamps arrive from CSV as ISO-8601 strings, so a type-list check would
    only prove they are strings -- "banana" would pass. Parseability is the real
    invariant, and it survives the column later becoming a true datetime dtype.
    """
    declared = str(declared_type).lower()
    if declared in {"datetime", "timestamp", "date"}:
        return gx.expectations.ExpectColumnValuesToBeDateutilParseable(column=column, meta=meta)
    type_list = _TYPE_LISTS.get(declared)
    if type_list:
        return gx.expectations.ExpectColumnValuesToBeInTypeList(
            column=column, type_list=type_list, meta=meta
        )
    return None


def build_expectations(contract: dict[str, Any]) -> list[Any]:
    """Translate every contract rule into a GX expectation carrying its severity."""
    expectations: list[Any] = []
    columns = contract.get("columns") or contract.get("fields") or {}

    for column, rules in columns.items():
        rules = rules or {}
        severity = rules.get("severity", "warning")
        meta = {"severity": severity, "column": column}

        if rules.get("required"):
            expectations.append(
                gx.expectations.ExpectColumnToExist(column=column, meta=dict(meta, check="required_column"))
            )
            expectations.append(
                gx.expectations.ExpectColumnValuesToNotBeNull(column=column, meta=dict(meta, check="not_null"))
            )
        if rules.get("unique"):
            expectations.append(
                gx.expectations.ExpectColumnValuesToBeUnique(column=column, meta=dict(meta, check="unique"))
            )
        if rules.get("accepted_values") is not None:
            expectations.append(
                gx.expectations.ExpectColumnValuesToBeInSet(
                    column=column,
                    value_set=list(rules["accepted_values"]),
                    meta=dict(meta, check="accepted_values"),
                )
            )
        if "min" in rules or "max" in rules:
            expectations.append(
                gx.expectations.ExpectColumnValuesToBeBetween(
                    column=column,
                    min_value=rules.get("min"),
                    max_value=rules.get("max"),
                    meta=dict(meta, check="range"),
                )
            )
        if rules.get("type"):
            type_expectation = _type_expectation(column, rules["type"], dict(meta, check="type"))
            if type_expectation is not None:
                expectations.append(type_expectation)

    return expectations


def run_orders_checkpoint(df: pd.DataFrame, contract: dict[str, Any]) -> dict[str, Any]:
    """Run the full Suite -> ValidationDefinition -> Checkpoint chain over `df`."""
    context = gx.get_context(mode="ephemeral")

    data_source = context.data_sources.add_pandas("orders_source")
    asset = data_source.add_dataframe_asset(name="orders_asset")
    batch_definition = asset.add_batch_definition_whole_dataframe("orders_batch")

    suite = context.suites.add(gx.ExpectationSuite(name="orders_contract_suite"))
    for expectation in build_expectations(contract):
        suite.add_expectation(expectation)

    validation_definition = context.validation_definitions.add(
        gx.ValidationDefinition(name="orders_validation", data=batch_definition, suite=suite)
    )
    checkpoint = context.checkpoints.add(
        gx.Checkpoint(name="orders_checkpoint", validation_definitions=[validation_definition])
    )

    result = checkpoint.run(batch_parameters={"dataframe": df})

    # Re-shape GX results into the same issue dicts the pandas validator emits, so
    # one severity policy decides the action for both engines.
    issues: list[dict[str, Any]] = []
    for validation_result in result.run_results.values():
        for outcome in validation_result.results:
            meta = outcome.expectation_config.meta or {}
            issues.append(
                {
                    "check": meta.get("check", outcome.expectation_config.type),
                    "column": meta.get("column") or outcome.expectation_config.kwargs.get("column"),
                    "severity": meta.get("severity", "warning"),
                    "passed": bool(outcome.success),
                    "expectation": outcome.expectation_config.type,
                    "unexpected_count": (outcome.result or {}).get("unexpected_count"),
                }
            )

    return {
        "success": bool(result.success),
        "issues": issues,
        "action": determine_action(issues),
    }


def main() -> None:
    orders_path = ROOT / "data" / "incoming" / "orders.csv"
    if not orders_path.exists():
        orders_path = ROOT / "data" / "baseline" / "orders.csv"

    contract = load_contract(CONTRACT_PATH)
    outcome = run_orders_checkpoint(pd.read_csv(orders_path), contract)

    print("=== GREAT EXPECTATIONS CHECKPOINT: orders_contract_suite ===")
    print(f"source: {orders_path.relative_to(ROOT)}\n")
    for issue in outcome["issues"]:
        status = "PASS" if issue["passed"] else "FAIL"
        unexpected = "" if issue["unexpected_count"] in (None, 0) else f"  unexpected={issue['unexpected_count']}"
        print(f"[{status}] {issue['severity']:<8} {issue['check']:<16} {issue['column'] or '-':<14}{unexpected}")

    failed = [i for i in outcome["issues"] if not i["passed"]]
    critical = [i for i in failed if i["severity"] == "critical"]
    print(f"\nexpectations   : {len(outcome['issues'])}")
    print(f"failed         : {len(failed)} ({len(critical)} critical)")
    print(f"checkpoint     : {'SUCCESS' if outcome['success'] else 'FAILED'}")
    print(f"pipeline action: {outcome['action'].upper()}")

    # Only a critical failure stops the pipeline. A warning is recorded and the
    # run continues -- alert fatigue is a reliability problem of its own.
    if outcome["action"] in {"block", "quarantine"}:
        sys.exit(1)


if __name__ == "__main__":
    main()
