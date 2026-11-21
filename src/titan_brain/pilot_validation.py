"""Non-executing validation for a proposed $500 pilot.

This module has no broker connection, order submission, cancellation, funding,
authorization override, activation function, or background scheduler. It accepts
caller-supplied broker observations for validation only. A passing report is NOT
a trade instruction and is NOT permission to activate live trading.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

VERSION = "pilot-validation-500-v1"


class EvidenceError(ValueError):
    """Fixed, credential-free diagnostic."""


def number(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise EvidenceError("INVALID_NUMBER")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise EvidenceError("INVALID_NUMBER") from None
    if not result.is_finite():
        raise EvidenceError("INVALID_NUMBER")
    return result


def nonnegative(value: Any) -> Decimal:
    result = number(value)
    if result < 0:
        raise EvidenceError("NEGATIVE_VALUE")
    return result


def fresh(observed_at: datetime, now: datetime, maximum_age: int) -> bool:
    if not isinstance(observed_at, datetime) or not isinstance(now, datetime):
        return False
    if observed_at.tzinfo is None or now.tzinfo is None:
        return False
    return 0 <= (now - observed_at).total_seconds() <= maximum_age


@dataclass(frozen=True)
class PilotLimits:
    """Draft limits derived from the user's $500 capital and earlier 10% cap.

    These do not update the existing paper policy or authorize live operation.
    """
    capital: str = "500.00"
    weekly_loss: str = "50.00"
    planned_trade_loss: str = "15.00"
    modeled_round_trip_cost: str = "1.30"
    maximum_contracts: int = 1

    def __post_init__(self) -> None:
        capital = number(self.capital)
        weekly = number(self.weekly_loss)
        if not 0 < capital <= 500 or not 0 < weekly <= capital * Decimal(".10"):
            raise EvidenceError("PILOT_CAP_EXCEEDED")
        if not 0 < number(self.planned_trade_loss) <= min(weekly, capital * Decimal(".03")):
            raise EvidenceError("PLANNED_LOSS_CAP_EXCEEDED")
        if not 0 < number(self.modeled_round_trip_cost) < weekly:
            raise EvidenceError("COST_RESERVE_REQUIRED")
        if type(self.maximum_contracts) is not int or self.maximum_contracts != 1:
            raise EvidenceError("SINGLE_CONTRACT_PILOT_ONLY")


@dataclass(frozen=True)
class AccountObservation:
    account_number: str
    observed_at: datetime
    accessible: bool
    active: bool
    options_approved: bool
    buying_power: str
    unleveraged_buying_power: str
    account_equity: str
    realized_week_pnl: str
    open_and_pending_premium_risk: str
    all_assets_reconciled: bool
    cash_flows_reconciled: bool
    all_pages_read: bool
    unknown_order_delivery: bool


@dataclass(frozen=True)
class CandidateObservation:
    option_id: str
    quote_option_id: str
    quote_at: datetime
    bid: str
    ask: str
    limit_price: str
    planned_stop: str
    quantity: int
    multiplier: int
    bid_size: int
    ask_size: int
    days_to_expiration: int
    source: str
    quote_is_real_time: bool
    contract_is_tradable: bool
    contract_type: str
    signal_at: datetime
    signal_confirmed: bool
    market_confirmed_open: bool


@dataclass(frozen=True)
class ReadinessReport:
    status: str
    reasons: tuple[str, ...]
    capital_ceiling: str
    weekly_loss_ceiling: str
    remaining_weekly_capacity: str
    full_premium_and_cost_reserve: str
    planned_stop_loss_and_cost: str
    live_submission_enabled: bool = False


def evaluate(expected_account: str, account: AccountObservation,
             candidate: CandidateObservation, now: datetime,
             limits: PilotLimits = PilotLimits()) -> ReadinessReport:
    """Validate observations, never infer a fill, select a trade or place an order.

    Evidence freshness/completeness must first be established by a read-only
    integration. Cash deposits do not expand the fixed $500/$50 test ceilings.
    Full premium (not merely a stop) is reserved against the remaining budget.
    Costs here are a disclosed simulation assumption, not a verified broker fee.
    """
    reasons: list[str] = []
    def check(condition: bool, reason: str) -> None:
        if not condition:
            reasons.append(reason)

    check(bool(expected_account) and account.account_number == expected_account, "ACCOUNT_MISMATCH")
    check(fresh(account.observed_at, now, 15), "ACCOUNT_OBSERVATION_STALE")
    check(account.accessible is True and account.active is True, "ACCOUNT_NOT_ACCESSIBLE")
    check(account.options_approved is True, "OPTIONS_APPROVAL_REQUIRED")
    check(account.all_assets_reconciled is True and account.cash_flows_reconciled is True
          and account.all_pages_read is True, "RECONCILIATION_INCOMPLETE")
    check(account.unknown_order_delivery is False, "UNKNOWN_ORDER_DELIVERY")
    check(bool(candidate.option_id) and candidate.option_id == candidate.quote_option_id, "CONTRACT_IDENTITY_MISMATCH")
    check(fresh(candidate.quote_at, now, 30), "OPTION_QUOTE_STALE")
    check(candidate.source == "robinhood_mcp" and candidate.quote_is_real_time is True, "EXECUTABLE_QUOTE_NOT_VERIFIED")
    check(candidate.contract_is_tradable is True and candidate.contract_type in {"call", "put"}, "UNSUPPORTED_CONTRACT")
    check(type(candidate.quantity) is int and candidate.quantity == limits.maximum_contracts, "SINGLE_CONTRACT_REQUIRED")
    check(type(candidate.multiplier) is int and candidate.multiplier == 100, "ADJUSTED_CONTRACT_BLOCKED")
    check(type(candidate.days_to_expiration) is int and 7 <= candidate.days_to_expiration <= 21, "EXPIRY_OUTSIDE_PILOT")
    check(type(candidate.bid_size) is int and type(candidate.ask_size) is int
          and candidate.bid_size >= 1 and candidate.ask_size >= 1, "OPTION_DEPTH_MISSING")
    check(candidate.signal_confirmed is True and fresh(candidate.signal_at, now, 180), "SIGNAL_NOT_FRESH_AND_CONFIRMED")
    check(candidate.market_confirmed_open is True, "MARKET_NOT_CONFIRMED_OPEN")
    remaining = full = planned = Decimal("0")
    try:
        buying_power = nonnegative(account.buying_power)
        unleveraged = nonnegative(account.unleveraged_buying_power)
        equity = nonnegative(account.account_equity)
        realized = number(account.realized_week_pnl)
        reserved = nonnegative(account.open_and_pending_premium_risk)
        weekly = number(limits.weekly_loss)
        # Gains cannot enlarge the initial weekly ceiling. Pending/open premiums
        # continue to consume capacity until actual closing fills are reconciled.
        remaining = max(Decimal("0"), weekly + min(Decimal("0"), realized) - reserved)
        bid, ask, limit, stop = map(number, (candidate.bid, candidate.ask, candidate.limit_price, candidate.planned_stop))
        costs = number(limits.modeled_round_trip_cost)
        check(0 < bid <= ask, "INVALID_OPTION_BOOK")
        check(0 < stop < limit, "INVALID_PLANNED_STOP")
        check(limit >= ask > 0, "LIMIT_NOT_MARKETABLE")
        check(limit > 0 and limit * 100 == (limit * 100).to_integral_value(), "INVALID_PRICE_PRECISION")
        if bid > 0 and ask >= bid:
            check((ask - bid) / ((ask + bid) / 2) <= Decimal(".15"), "SPREAD_TOO_WIDE")
        if type(candidate.quantity) is int and candidate.quantity > 0 and candidate.multiplier == 100:
            full = limit * candidate.quantity * 100 + costs
            planned = (limit - stop) * candidate.quantity * 100 + costs
            check(full <= remaining, "FULL_PREMIUM_EXCEEDS_WEEKLY_CAPACITY")
            check(full <= min(number(limits.capital), buying_power, unleveraged, equity), "INSUFFICIENT_VERIFIED_FUNDS")
            check(0 < planned <= number(limits.planned_trade_loss), "PLANNED_LOSS_EXCEEDS_CAP")
        check(min(buying_power, unleveraged, equity) > 0, "ACCOUNT_UNFUNDED")
    except EvidenceError as exc:
        reasons.append(str(exc))
    return ReadinessReport(
        status="BLOCKED" if reasons else "OBSERVATIONS_PASS_NOT_LIVE_READY",
        reasons=tuple(dict.fromkeys(reasons)), capital_ceiling=limits.capital,
        weekly_loss_ceiling=limits.weekly_loss, remaining_weekly_capacity=str(remaining),
        full_premium_and_cost_reserve=str(full), planned_stop_loss_and_cost=str(planned),
    )


TERMINAL_STATES = frozenset({"filled", "cancelled", "rejected", "failed", "voided"})
KNOWN_STATES = TERMINAL_STATES | {"queued", "confirmed", "partially_filled", "pending_cancelled"}


def reconcile_order(previous: Mapping[str, Any] | None, observation: Mapping[str, Any],
                    *, expected_account: str, expected_option_id: str,
                    expected_ref_id: str) -> dict[str, Any]:
    """Validate broker cumulative evidence without performing a broker action.

    Inputs are normalized upstream observations, NOT unverified submission
    requests. The normalizer must bind the real broker order ID to its ref_id,
    account and contract. Missing evidence remains an error; it is not filled in.
    A cancel request is never interpreted as successful cancellation.
    """
    try:
        if (observation["account_number"] != expected_account
                or observation["option_id"] != expected_option_id
                or observation["ref_id"] != expected_ref_id):
            raise EvidenceError("ORDER_IDENTITY_MISMATCH")
        if not isinstance(observation["order_id"], str) or not observation["order_id"]:
            raise EvidenceError("BROKER_ORDER_ID_REQUIRED")
        state = observation["state"]
        if state not in KNOWN_STATES:
            raise EvidenceError("UNKNOWN_ORDER_STATE")
        quantity, filled = number(observation["quantity"]), number(observation["filled_quantity"])
        average = nonnegative(observation["average_price"])
        if quantity <= 0 or quantity != quantity.to_integral_value():
            raise EvidenceError("INVALID_ORDER_QUANTITY")
        if not 0 <= filled <= quantity or filled != filled.to_integral_value():
            raise EvidenceError("INVALID_FILL_QUANTITY")
        if (filled == 0 and average != 0) or (filled > 0 and average <= 0):
            raise EvidenceError("INVALID_CUMULATIVE_FILL_PRICE")
        if state == "filled" and filled != quantity:
            raise EvidenceError("FILL_NOT_CONFIRMED")
        if state == "partially_filled" and not 0 < filled < quantity:
            raise EvidenceError("INVALID_PARTIAL_FILL")
        previous_filled = Decimal("0")
        if previous:
            for field in ("account_number", "option_id", "ref_id", "order_id", "quantity"):
                if str(previous[field]) != str(observation[field]):
                    raise EvidenceError("ORDER_IDENTITY_CHANGED")
            previous_filled = number(previous["filled_quantity"])
            if filled < previous_filled:
                raise EvidenceError("CUMULATIVE_FILL_REGRESSION")
            if previous["state"] in TERMINAL_STATES:
                if (state != previous["state"] or filled != previous_filled
                        or average != number(previous["average_price"])):
                    raise EvidenceError("TERMINAL_EVIDENCE_CHANGED")
        result = {key: observation[key] for key in ("account_number", "option_id", "ref_id", "order_id", "state", "quantity", "filled_quantity", "average_price")}
        result["incremental_filled_quantity"] = str(filled - previous_filled)
        result["terminal_confirmed"] = state in TERMINAL_STATES
        result["remaining_quantity"] = str(quantity - filled)
        result["retain_pending_reserve"] = state not in TERMINAL_STATES
        result["requires_position_reconciliation"] = filled > 0
        return result
    except (KeyError, TypeError):
        raise EvidenceError("INCOMPLETE_ORDER_EVIDENCE") from None
