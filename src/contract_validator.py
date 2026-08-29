"""Deterministic data-contract validation.

What the starter did: not-null, unique, accepted values, numeric range.
What was added here, in the order an on-call engineer needs it:

1. **type validation without silent coercion** -- `pd.to_numeric(..., "coerce")`
   turns a schema regression into a pile of NaNs, which is exactly how type drift
   reaches a dashboard unnoticed.
2. **freshness**, evaluated against an explicit reference time so a replayed or
   historical DataFrame is never judged by the wall clock of the machine that
   happens to run the validator.
3. **severity -> action**: every issue carries what the pipeline should *do*
   (`block`, `quarantine`, `warn`, `log`), and `determine_action` reduces a whole
   run to the single decision the orchestrator needs.
4. **`columns:` or `fields:`** -- `contracts/kb_contract.yaml` uses `fields:`, so
   the starter validated nothing at all for the knowledge base.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

# What the pipeline does when a check of this severity fails.
_ACTIONS = {"critical": "block", "warning": "warn", "info": "log"}
_SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}

# A critical failure of one of these means the rows themselves are unusable:
# hold the batch aside instead of only blocking the run.
_QUARANTINE_CHECKS = {"unique", "not_null", "type", "required_column"}


def load_contract(path: str | Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _issue(
    check: str,
    *,
    column: str | None,
    severity: str,
    passed: bool,
    details: str,
) -> dict[str, Any]:
    return {
        "check": check,
        "column": column,
        "severity": severity,
        "action": _ACTIONS.get(severity, "warn"),
        "passed": bool(passed),
        "details": details,
    }


def _is_integer(value: Any) -> bool:
    if isinstance(value, (bool, np.bool_)):
        return False
    if isinstance(value, (int, np.integer)):
        return True
    # A CSV column holding integers plus one null is read back as float64, so an
    # integral float is accepted; a fractional one is genuine type drift.
    if isinstance(value, (float, np.floating)):
        return bool(np.isfinite(value) and float(value).is_integer())
    return False


def _is_number(value: Any) -> bool:
    if isinstance(value, (bool, np.bool_)):
        return False
    if isinstance(value, (int, float, np.number)):
        return bool(np.isfinite(value))
    return False


def _is_datetime(value: Any) -> bool:
    if isinstance(value, (datetime, pd.Timestamp, np.datetime64)):
        return True
    if isinstance(value, str):
        return not pd.isna(pd.to_datetime(value, utc=True, errors="coerce"))
    # Bare numbers are not timestamps here: accepting them would let an epoch-vs-
    # ISO format change pass as valid.
    return False


def _is_boolean(value: Any) -> bool:
    if isinstance(value, (bool, np.bool_)):
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"true", "false"}
    return False


_TYPE_PREDICATES = {
    "integer": _is_integer,
    "int": _is_integer,
    "number": _is_number,
    "float": _is_number,
    "numeric": _is_number,
    "string": lambda value: isinstance(value, str),
    "str": lambda value: isinstance(value, str),
    "text": lambda value: isinstance(value, str),
    "datetime": _is_datetime,
    "timestamp": _is_datetime,
    "date": _is_datetime,
    "boolean": _is_boolean,
    "bool": _is_boolean,
}


def _type_invalid_count(series: pd.Series, declared_type: str) -> int:
    """Count non-null values incompatible with the declared contract type."""
    predicate = _TYPE_PREDICATES.get(str(declared_type).strip().lower())
    if predicate is None:
        return 0
    non_null = series.dropna()
    if non_null.empty:
        return 0
    return int((~non_null.map(predicate).astype(bool)).sum())


def _resolve_reference_time(
    contract: dict[str, Any], reference_time: datetime | str | None
) -> pd.Timestamp | None:
    """Explicit argument wins, then `freshness.reference_time` in the contract.

    Returning `None` means "no trustworthy clock for this run", and freshness is
    skipped rather than evaluated against whatever time the test suite happens to
    run at. `scripts/run_baseline.py` passes the wall clock explicitly, so the
    real pipeline still catches stale data.
    """
    candidate = reference_time
    if candidate is None:
        candidate = (contract.get("freshness") or {}).get("reference_time")
    if candidate is None:
        return None
    parsed = pd.to_datetime(candidate, utc=True, errors="coerce")
    return None if pd.isna(parsed) else parsed


def validate_dataframe(
    df: pd.DataFrame,
    contract: dict[str, Any],
    *,
    reference_time: datetime | str | None = None,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    columns = contract.get("columns") or contract.get("fields") or {}

    for column, rules in columns.items():
        rules = rules or {}
        severity = rules.get("severity", "warning")
        required = bool(rules.get("required", False))

        if column not in df.columns:
            if required:
                issues.append(
                    _issue(
                        "required_column",
                        column=column,
                        severity=severity,
                        passed=False,
                        details=f"Missing required column: {column}",
                    )
                )
            continue

        series = df[column]

        declared_type = rules.get("type")
        if declared_type:
            invalid_count = _type_invalid_count(series, declared_type)
            issues.append(
                _issue(
                    "type",
                    column=column,
                    severity=severity,
                    passed=(invalid_count == 0),
                    details=f"declared_type={declared_type}; invalid_count={invalid_count}",
                )
            )

        if required:
            null_count = int(series.isna().sum())
            issues.append(
                _issue(
                    "not_null",
                    column=column,
                    severity=severity,
                    passed=(null_count == 0),
                    details=f"null_count={null_count}",
                )
            )

        if rules.get("unique"):
            duplicate_count = int(series.duplicated(keep=False).sum())
            issues.append(
                _issue(
                    "unique",
                    column=column,
                    severity=severity,
                    passed=(duplicate_count == 0),
                    details=f"duplicate_rows={duplicate_count}",
                )
            )

        accepted = rules.get("accepted_values")
        if accepted is not None:
            invalid_count = int((series.notna() & ~series.isin(accepted)).sum())
            issues.append(
                _issue(
                    "accepted_values",
                    column=column,
                    severity=severity,
                    passed=(invalid_count == 0),
                    details=f"invalid_count={invalid_count}; accepted={accepted}",
                )
            )

        if "min" in rules or "max" in rules:
            numeric = pd.to_numeric(series, errors="coerce")
            invalid = pd.Series(False, index=series.index)
            if "min" in rules:
                invalid |= numeric < rules["min"]
            if "max" in rules:
                invalid |= numeric > rules["max"]
            invalid_count = int(invalid.fillna(False).sum())
            issues.append(
                _issue(
                    "range",
                    column=column,
                    severity=severity,
                    passed=(invalid_count == 0),
                    details=f"invalid_count={invalid_count}",
                )
            )

        if "min_length" in rules or "max_length" in rules:
            lengths = series.dropna().astype(str).str.len()
            invalid = pd.Series(False, index=lengths.index)
            if "min_length" in rules:
                invalid |= lengths < rules["min_length"]
            if "max_length" in rules:
                invalid |= lengths > rules["max_length"]
            invalid_count = int(invalid.sum())
            issues.append(
                _issue(
                    "length",
                    column=column,
                    severity=severity,
                    passed=(invalid_count == 0),
                    details=(
                        f"invalid_count={invalid_count}; "
                        f"min_length={rules.get('min_length')}; max_length={rules.get('max_length')}"
                    ),
                )
            )

    issues.extend(_validate_freshness(df, contract, reference_time))
    return issues


def _validate_freshness(
    df: pd.DataFrame,
    contract: dict[str, Any],
    reference_time: datetime | str | None,
) -> list[dict[str, Any]]:
    freshness = contract.get("freshness") or {}
    column = freshness.get("column")
    if not column:
        return []

    severity = freshness.get("severity", "warning")
    reference = _resolve_reference_time(contract, reference_time)
    if reference is None:
        return []

    if column not in df.columns:
        return [
            _issue(
                "freshness",
                column=column,
                severity=severity,
                passed=False,
                details=f"freshness column missing: {column}",
            )
        ]

    timestamps = pd.to_datetime(df[column], utc=True, errors="coerce").dropna()
    if timestamps.empty:
        return [
            _issue(
                "freshness",
                column=column,
                severity=severity,
                passed=False,
                details="no parseable timestamps in freshness column",
            )
        ]

    latest = timestamps.max()
    max_delay = float(freshness.get("max_delay_minutes", 60))
    age_minutes = (reference - latest).total_seconds() / 60.0
    return [
        _issue(
            "freshness",
            column=column,
            severity=severity,
            # A future timestamp is a clock/timezone bug, not freshness -- flag it too.
            passed=bool(-1.0 <= age_minutes <= max_delay),
            details=(
                f"age_minutes={age_minutes:.2f}; max_delay_minutes={max_delay:.2f}; "
                f"latest={latest.isoformat()}; reference={reference.isoformat()}"
            ),
        )
    ]


def failed_issues(issues: list[dict[str, Any]], min_severity: str | None = None) -> list[dict[str, Any]]:
    failed = [issue for issue in issues if not issue.get("passed", False)]
    if min_severity is None:
        return failed
    threshold = _SEVERITY_ORDER[min_severity]
    return [
        issue
        for issue in failed
        if _SEVERITY_ORDER.get(issue.get("severity", "warning"), 1) >= threshold
    ]


def determine_action(issues: list[dict[str, Any]]) -> str:
    """Reduce a validation run to one pipeline decision.

    `quarantine` is deliberately distinct from `block`: a duplicate key or a type
    regression means the rows are unusable and must be held aside for a producer
    to fix, while a critical range/value violation blocks promotion but leaves the
    batch intact for a re-run.
    """
    failed = failed_issues(issues)
    if not failed:
        return "pass"
    critical_checks = {i["check"] for i in failed if i.get("severity") == "critical"}
    if critical_checks & _QUARANTINE_CHECKS:
        return "quarantine"
    if critical_checks:
        return "block"
    if any(i.get("severity") == "warning" for i in failed):
        return "warn"
    return "log"


def summarize(issues: list[dict[str, Any]]) -> dict[str, Any]:
    """Compact result for dashboards, reports and the baseline JSON."""
    failed = failed_issues(issues)
    return {
        "total_checks": len(issues),
        "failed_checks": len(failed),
        "critical_failures": len(failed_issues(issues, "critical")),
        "warning_failures": len([i for i in failed if i.get("severity") == "warning"]),
        "action": determine_action(issues),
        "failed_details": [
            {"check": i["check"], "column": i["column"], "severity": i["severity"], "details": i["details"]}
            for i in failed
        ],
    }
