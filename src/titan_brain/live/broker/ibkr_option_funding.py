"""Option permissions, market-data and funding evidence evaluation (offline).

Milestone 3 of the attended-options plan.

This is the fail-closed EVALUATOR for the evidence milestone 3 is about.  It is
deliberately NOT a broker fetch: it opens no socket and authenticates nothing.
It takes an explicitly supplied evidence bundle (the shape a later authenticated
adapter would populate) and decides whether a defensible funding basis exists,
or returns explicit blockers.  Wiring a real authenticated IBKR adapter that
produces this bundle is a later live-integration step; this module lets that
logic be specified and tested offline first, with synthetic inputs.

Fail-closed conditions required by the plan, each blocking sizing (never a mere
warning):

* option trading permission not explicitly granted for the account;
* market-data entitlement absent, or quote delayed / missing / stale / future;
* incomplete account visibility (positions or pending orders not fully seen);
* stale account balances, or conflicting cash facts;
* unknown settlement of proceeds;
* request-limit / collection failures.

Money is Decimal end-to-end; unknown values are ``None`` and block.  A clean
result exposes the settled, unencumbered cash available for a NEW entry after
subtracting reserved funds and pending-order commitments -- never equity or
margin buying power, and never double-counting a reservation already reflected
in a supplied balance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Mapping, Sequence


# Freshness bounds mirror the session path conventions (account 60s, quote 30s).
_MAX_ACCOUNT_AGE = timedelta(seconds=60)
_MAX_QUOTE_AGE = timedelta(seconds=30)


class OptionEvidenceError(ValueError):
    """A well-formed, non-secret evidence-shape error."""


def _money(value: object, field_name: str) -> Decimal:
    if isinstance(value, bool) or type(value) not in (str, int):
        raise OptionEvidenceError(f"{field_name} must be a string or integer")
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise OptionEvidenceError(f"{field_name} is not a valid decimal") from exc
    if not parsed.is_finite():
        raise OptionEvidenceError(f"{field_name} must be finite")
    return parsed


def _aware(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise OptionEvidenceError(f"{field_name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class OptionFundingEvidence:
    """Supplied (synthetic or, later, adapter-produced) evidence bundle.

    All monetary fields are strings/ints (never float) or ``None`` when the
    broker did not resolve them; ``None`` fails closed rather than assuming 0.
    """

    account_currency: str
    option_permission_granted: object          # bool; must be exactly True
    market_data_entitled: object               # bool; must be exactly True
    positions_complete: object                 # bool
    pending_orders_complete: object            # bool
    settlement_known: object                   # bool
    request_limit_ok: object                   # bool; False => collection failure
    account_observed_at: datetime
    quote_quoted_at: datetime
    quote_received_at: datetime
    settled_cash: object                       # money or None
    reserved_funds: object                     # money or None (holds/margin reqs)
    pending_order_commitments: object          # money or None
    pending_debits_not_in_balances: object     # money or None


@dataclass(frozen=True)
class OptionFundingResult:
    ok: bool
    blockers: tuple[str, ...]
    available_unborrowed_cash: object = None   # Decimal when ok, else None


def evaluate_option_funding(
    evidence: OptionFundingEvidence, *, now: datetime
) -> OptionFundingResult:
    """Evaluate the evidence bundle; return available cash or explicit blockers."""

    if not isinstance(evidence, OptionFundingEvidence):
        raise OptionEvidenceError("evidence must be an OptionFundingEvidence")
    current = _aware(now, "now")
    blockers: list[str] = []

    # Currency scope.
    if evidence.account_currency != "USD":
        blockers.append("ACCOUNT_CURRENCY_UNSUPPORTED")

    # Explicit permission / entitlement / visibility gates (must be exactly True).
    if evidence.option_permission_granted is not True:
        blockers.append("OPTION_PERMISSION_NOT_GRANTED")
    if evidence.market_data_entitled is not True:
        blockers.append("MARKET_DATA_NOT_ENTITLED")
    if evidence.positions_complete is not True:
        blockers.append("POSITIONS_VISIBILITY_INCOMPLETE")
    if evidence.pending_orders_complete is not True:
        blockers.append("PENDING_ORDERS_VISIBILITY_INCOMPLETE")
    if evidence.settlement_known is not True:
        blockers.append("SETTLEMENT_UNKNOWN")
    if evidence.request_limit_ok is not True:
        blockers.append("REQUEST_LIMIT_OR_COLLECTION_FAILED")

    # Freshness: account balances and quote must be recent and not future-dated.
    account_observed = _aware(evidence.account_observed_at, "account_observed_at")
    if account_observed > current:
        blockers.append("ACCOUNT_OBSERVATION_IN_FUTURE")
    elif current - account_observed > _MAX_ACCOUNT_AGE:
        blockers.append("ACCOUNT_BALANCES_STALE")

    quoted = _aware(evidence.quote_quoted_at, "quote_quoted_at")
    received = _aware(evidence.quote_received_at, "quote_received_at")
    if quoted > current or received > current:
        blockers.append("QUOTE_OBSERVATION_IN_FUTURE")
    elif not quoted <= received:
        blockers.append("QUOTE_CHRONOLOGY_INVALID")
    elif current - quoted > _MAX_QUOTE_AGE:
        blockers.append("QUOTE_STALE")

    # Funding facts: any unknown (None) money fails closed.
    settled = _optional_money(evidence.settled_cash, "settled_cash", blockers)
    reserved = _optional_money(evidence.reserved_funds, "reserved_funds", blockers)
    committed = _optional_money(
        evidence.pending_order_commitments, "pending_order_commitments", blockers
    )
    pending_debits = _optional_money(
        evidence.pending_debits_not_in_balances,
        "pending_debits_not_in_balances",
        blockers,
    )

    available = None
    if settled is not None and reserved is not None and committed is not None and pending_debits is not None:
        if settled < 0 or reserved < 0 or committed < 0 or pending_debits < 0:
            blockers.append("NEGATIVE_FUNDING_FACT")
        else:
            # Unborrowed cash available for a NEW entry: settled cash minus
            # everything already committed. Reserved funds and pending-order
            # commitments are subtracted explicitly; pending debits not yet in
            # the balance are also removed. This is settlement-aware spendable
            # cash, NOT equity or margin buying power.
            candidate = settled - reserved - committed - pending_debits
            if candidate < 0:
                blockers.append("INSUFFICIENT_UNBORROWED_CASH")
            else:
                available = candidate

    unique: list[str] = []
    for code in blockers:
        if code not in unique:
            unique.append(code)
    if unique:
        return OptionFundingResult(ok=False, blockers=tuple(unique), available_unborrowed_cash=None)
    return OptionFundingResult(ok=True, blockers=(), available_unborrowed_cash=available)


def _optional_money(value: object, field_name: str, blockers: list[str]):
    if value is None:
        blockers.append(f"{field_name.upper()}_UNKNOWN")
        return None
    return _money(value, field_name)


__all__ = [
    "OptionEvidenceError",
    "OptionFundingEvidence",
    "OptionFundingResult",
    "evaluate_option_funding",
]
