"""SLI / SLO / error-budget math and multi-window burn-rate alerting."""
from __future__ import annotations

from typing import Any

# Google SRE Workbook, "Alerting on SLOs" -- multiwindow, multi-burn-rate alerts.
# 14.4x burns 2% of a 30-day budget in 1 hour; 6x burns 5% in 6 hours;
# 1x is exactly the budget-consumption pace the SLO allows.
FAST_BURN = 14.4
SLOW_BURN = 6.0
BUDGET_PACE = 1.0


def calculate_slo(target: float, bad_events: int, total_events: int) -> dict[str, Any]:
    if not 0 < target < 1:
        raise ValueError("target must be between 0 and 1 (exclusive)")
    if bad_events < 0 or total_events < 0 or bad_events > total_events:
        raise ValueError("invalid event counts")
    allowed_bad_rate = 1.0 - target
    if total_events == 0:
        return {
            "target": target,
            "actual_bad_rate": 0.0,
            "allowed_bad_rate": allowed_bad_rate,
            "burn_rate": 0.0,
            "remaining_error_budget_fraction": 1.0,
            "breached": False,
        }
    actual_bad_rate = bad_events / total_events
    burn_rate = actual_bad_rate / allowed_bad_rate
    consumed_fraction = min(1.0, actual_bad_rate / allowed_bad_rate)
    return {
        "target": target,
        "actual_bad_rate": actual_bad_rate,
        "allowed_bad_rate": allowed_bad_rate,
        "burn_rate": burn_rate,
        "remaining_error_budget_fraction": max(0.0, 1.0 - consumed_fraction),
        "breached": bool(actual_bad_rate > allowed_bad_rate),
    }


def evaluate_multiwindow_burn(
    *,
    short_window_burn: float,
    long_window_burn: float,
    policy: str = "google_sre_multiwindow",
    fast_burn_threshold: float = FAST_BURN,
    slow_burn_threshold: float = SLOW_BURN,
) -> dict[str, Any]:
    """Decide whether a burn rate deserves a page, a ticket, or silence.

    The long window answers "is enough error budget actually being destroyed to
    matter?"; the short window answers "is it still happening right now?". A page
    requires *both*, which is what kills the two classic failure modes:

    - a 60-second spike that has already recovered never wakes anyone up,
    - a sustained burn is not silenced just because the long window is still
      averaging over mostly-healthy minutes.

    Ladder (highest severity first):
      short & long >= 14.4x -> page, critical   (budget gone in hours)
      short & long >=  6.0x -> page, warning    (budget gone in ~a day)
      short high, long low  -> silence, info    (transient spike)
      short & long >=  1.0x -> ticket, warning  (over pace, not urgent)
      long high, short low  -> ticket, warning  (burn stopped; verify recovery)

    The transient-spike rule is checked *before* the over-pace rule on purpose: a
    spiking short window with a long window below the page threshold is the
    textbook blip, and calling it a ticket would reintroduce exactly the noise the
    two-window design exists to remove.
    """
    short_burn = float(short_window_burn)
    long_burn = float(long_window_burn)
    sustained = min(short_burn, long_burn)

    def verdict(page: bool, severity: str, alert_type: str, reason: str) -> dict[str, Any]:
        return {
            "page": page,
            "severity": severity,
            "alert_type": alert_type,
            "reason": f"{reason} (short={short_burn:.2f}x, long={long_burn:.2f}x)",
            "short_window_burn": short_burn,
            "long_window_burn": long_burn,
            "policy": policy,
            "thresholds": {"fast": fast_burn_threshold, "slow": slow_burn_threshold},
        }

    if sustained >= fast_burn_threshold:
        return verdict(True, "critical", "page", "sustained_fast_burn")
    if sustained >= slow_burn_threshold:
        return verdict(True, "warning", "page", "sustained_slow_burn")
    if short_burn >= slow_burn_threshold:
        # Loud now, invisible over the long window: too short to be real.
        return verdict(False, "info", "none", "transient_spike_suppressed")
    if long_burn >= slow_burn_threshold:
        # Real damage already done, but the bleeding has stopped.
        return verdict(False, "warning", "ticket", "burn_recovered_verify_budget")
    if sustained >= BUDGET_PACE:
        return verdict(False, "warning", "ticket", "budget_burning_above_pace")
    return verdict(False, "info", "none", "healthy_error_budget")
