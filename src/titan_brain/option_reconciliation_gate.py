"""Manual-intervention and unknown-activity reconciliation gate (offline).

Milestone 8 of the attended-options plan.

Titan is not assumed to be the only actor on the account: the owner (or another
client) can place orders, close positions, or move cash.  Before ANY further
new risk, the account-wide observed state must reconcile with what Titan
expects; any manual trade, unknown order, or capture gap must BLOCK and force a
refreshed attended ticket (milestone 6) rather than being silently absorbed.

This module is a pure, offline gate.  Given:

* the Titan-expected positions and working orders (what Titan believes it has),
* the account-wide OBSERVED positions and orders (all clients), and
* capture-health flags (reporting-client scope, continuity, subscription
  state, history completeness, duplicate/late-event detection),

it returns the reconciliation blockers.  It performs no broker I/O, holds no
authority, and decides nothing about trading; a clean reconciliation is a
precondition for review, never permission.  Any unresolved delta or degraded
capture is a hard blocker, never a warning.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence


class ReconciliationError(ValueError):
    """A well-formed, non-secret reconciliation-shape error."""


@dataclass(frozen=True)
class CaptureHealth:
    """Health of the account-wide capture feeding this reconciliation."""

    reporting_scope_account_wide: object   # bool; must be exactly True
    capture_continuous: object             # bool; no gap since last reconcile
    subscription_active: object            # bool; not rejected/dropped
    history_complete: object               # bool; no missing backfill
    duplicate_or_late_events_detected: object  # bool; True => block


@dataclass(frozen=True)
class ReconciliationInput:
    """Expected vs observed account-wide state, keyed by contract fingerprint.

    Each mapping is ``fingerprint -> signed net contracts`` (positions) or
    ``fingerprint -> count`` (working orders).  Titan-expected reflects what
    Titan placed/knows; observed reflects the whole account across all clients.
    """

    expected_positions: Mapping[str, int]
    observed_positions: Mapping[str, int]
    expected_orders: Mapping[str, int]
    observed_orders: Mapping[str, int]
    capture: CaptureHealth


@dataclass(frozen=True)
class ReconciliationResult:
    reconciled: bool
    blockers: tuple[str, ...]
    manual_position_symbols: tuple[str, ...]
    unknown_order_symbols: tuple[str, ...]


def _int_map(value: object, field_name: str) -> Mapping[str, int]:
    if not isinstance(value, Mapping):
        raise ReconciliationError(f"{field_name} must be a mapping")
    for key, item in value.items():
        if type(key) is not str:
            raise ReconciliationError(f"{field_name} keys must be strings")
        if type(item) is not int or isinstance(item, bool):
            raise ReconciliationError(f"{field_name} values must be integers")
    return value


def reconcile_account_activity(data: ReconciliationInput) -> ReconciliationResult:
    """Detect manual/unknown activity and capture gaps; block on any of them."""

    if not isinstance(data, ReconciliationInput):
        raise ReconciliationError("data must be a ReconciliationInput")
    if not isinstance(data.capture, CaptureHealth):
        raise ReconciliationError("capture must be a CaptureHealth")

    expected_pos = _int_map(data.expected_positions, "expected_positions")
    observed_pos = _int_map(data.observed_positions, "observed_positions")
    expected_ord = _int_map(data.expected_orders, "expected_orders")
    observed_ord = _int_map(data.observed_orders, "observed_orders")

    blockers: list[str] = []

    # Capture health: any degraded signal blocks (fail closed).
    c = data.capture
    if c.reporting_scope_account_wide is not True:
        blockers.append("REPORTING_SCOPE_NOT_ACCOUNT_WIDE")
    if c.capture_continuous is not True:
        blockers.append("CAPTURE_DISCONTINUITY")
    if c.subscription_active is not True:
        blockers.append("SUBSCRIPTION_INACTIVE_OR_REJECTED")
    if c.history_complete is not True:
        blockers.append("HISTORY_INCOMPLETE")
    if c.duplicate_or_late_events_detected is True:
        blockers.append("DUPLICATE_OR_LATE_EVENTS")

    # Position deltas: any fingerprint whose observed net differs from expected
    # is manual/unknown intervention and must be reconciled before new risk.
    manual_positions: list[str] = []
    for key in sorted(set(expected_pos) | set(observed_pos)):
        if expected_pos.get(key, 0) != observed_pos.get(key, 0):
            manual_positions.append(key)
    if manual_positions:
        blockers.append("MANUAL_POSITION_CHANGE_UNRECONCILED")

    # Order deltas: any working order not expected (or an expected order missing)
    # is an unknown/again manual order.
    unknown_orders: list[str] = []
    for key in sorted(set(expected_ord) | set(observed_ord)):
        if expected_ord.get(key, 0) != observed_ord.get(key, 0):
            unknown_orders.append(key)
    if unknown_orders:
        blockers.append("UNKNOWN_OR_MISSING_WORKING_ORDER")

    unique: list[str] = []
    for code in blockers:
        if code not in unique:
            unique.append(code)
    return ReconciliationResult(
        reconciled=not unique,
        blockers=tuple(unique),
        manual_position_symbols=tuple(manual_positions),
        unknown_order_symbols=tuple(unknown_orders),
    )


__all__ = [
    "CaptureHealth",
    "ReconciliationError",
    "ReconciliationInput",
    "ReconciliationResult",
    "reconcile_account_activity",
]
