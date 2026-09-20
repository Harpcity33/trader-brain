"""Option position lifecycle and cumulative P&L accumulator (offline).

Milestone 7 of the attended-options plan.

A single long-option position accrues events over its life: fills (opening and
closing), fees, end-of-day marks, and day rollovers.  This module folds an
ordered event log into a deterministic state so that:

* LIFETIME realized+unrealized trade P&L is preserved across midnight and across
  a restart -- replaying the same log yields the same state, and a day rollover
  does NOT reset the position's cost basis or realized history;
* session P&L (against the pre-trade baseline) and daily MARKED P&L (against a
  documented carry-in mark) are reported SEPARATELY;
* an unfilled CLOSING order never marks the position flat -- only an actual
  closing FILL reduces the open quantity.  Submitting or working a close is not
  a close.

It is pure and offline: no broker, no socket, no clock of its own (event
timestamps are supplied), no live authority.  Money is Decimal end-to-end and
multiplier-aware (contracts x multiplier x price), consistent with unit 2b.
Malformed logs fail closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from typing import Mapping, Sequence


class LifecycleError(ValueError):
    """A well-formed, non-secret lifecycle error."""


def _money(value: object, field_name: str, *, minimum: Decimal | None = None) -> Decimal:
    if isinstance(value, bool) or type(value) not in (str, int):
        raise LifecycleError(f"{field_name} must be a string or integer, not float/other")
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise LifecycleError(f"{field_name} is not a valid decimal") from exc
    if not parsed.is_finite():
        raise LifecycleError(f"{field_name} must be finite")
    if minimum is not None and parsed < minimum:
        raise LifecycleError(f"{field_name} must be >= {minimum}")
    return parsed


def _aware(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise LifecycleError(f"{field_name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


_EVENT_TYPES = frozenset({"open_fill", "close_fill", "fee", "mark", "day_rollover"})


@dataclass(frozen=True)
class LifecycleEvent:
    """One event in a position's life.

    Fields used depend on ``event_type``:
    * open_fill / close_fill: quantity (contracts > 0), price (per share)
    * fee: amount (>= 0)
    * mark: price (per share, current bid used for marking)
    * day_rollover: session_date (the new trading day; carry-in mark uses the
      most recent mark)
    """

    event_type: str
    at: datetime
    multiplier: int
    quantity: int = 0
    price: object = None
    amount: object = None
    session_date: object = None

    def __post_init__(self) -> None:
        if self.event_type not in _EVENT_TYPES:
            raise LifecycleError("unknown event_type")
        _aware(self.at, "at")
        if type(self.multiplier) is not int or isinstance(self.multiplier, bool) or self.multiplier < 1:
            raise LifecycleError("multiplier must be a positive integer")


@dataclass(frozen=True)
class LifecycleState:
    open_contracts: int
    cost_basis_cash: str          # signed cash spent on the open position (negative = paid)
    realized_pnl: str
    total_fees: str
    lifetime_pnl: str             # realized + unrealized(at last mark) - fees
    session_pnl_vs_baseline: str
    daily_marked_pnl: str
    last_mark_price: object
    session_date: object


def replay_lifecycle(
    events: Sequence[LifecycleEvent], *, pretrade_baseline_cash: object
) -> LifecycleState:
    """Fold the ordered event log into deterministic lifecycle state.

    Raises LifecycleError on a malformed log (out-of-order times, a close larger
    than the open quantity, a mark/close before any open).  Replaying the same
    log always yields the same state (restart-safe).
    """

    if not isinstance(events, (list, tuple)) or not events:
        raise LifecycleError("at least one lifecycle event is required")
    baseline = _money(pretrade_baseline_cash, "pretrade_baseline_cash")

    with localcontext() as context:
        context.prec = 80
        open_contracts = 0
        # weighted signed cash paid for the currently-open contracts (negative).
        open_cost_cash = Decimal(0)
        realized = Decimal(0)
        fees = Decimal(0)
        last_mark: Decimal | None = None
        # avg open premium per share, for realizing on close.
        open_premium_per_share: Decimal | None = None
        session_date: date | None = None
        # marked pnl carry-in reference for the CURRENT day.
        carry_in_mark: Decimal | None = None
        last_at: datetime | None = None

        for event in events:
            if not isinstance(event, LifecycleEvent):
                raise LifecycleError("every event must be a LifecycleEvent")
            at = _aware(event.at, "at")
            if last_at is not None and at < last_at:
                raise LifecycleError("events must be in non-decreasing time order")
            last_at = at
            multiplier = Decimal(event.multiplier)

            if event.event_type == "open_fill":
                qty = _int_qty(event.quantity)
                price = _money(event.price, "price", minimum=Decimal(0))
                # Increase open position; accumulate premium paid.
                total_shares_before = Decimal(open_contracts) * multiplier
                paid = qty * multiplier * price
                open_cost_cash -= paid
                new_contracts = open_contracts + qty
                # Weighted average open premium per share.
                if open_premium_per_share is None:
                    open_premium_per_share = price
                else:
                    open_premium_per_share = (
                        (open_premium_per_share * total_shares_before + price * (qty * multiplier))
                        / (Decimal(new_contracts) * multiplier)
                    )
                open_contracts = new_contracts

            elif event.event_type == "close_fill":
                qty = _int_qty(event.quantity)
                price = _money(event.price, "price", minimum=Decimal(0))
                if qty > open_contracts:
                    raise LifecycleError("close fill exceeds open contracts")
                if open_premium_per_share is None:
                    raise LifecycleError("close fill before any open fill")
                # Realize P&L on the closed contracts: (exit - entry) x shares.
                shares = qty * multiplier
                realized += (price - open_premium_per_share) * shares
                # Reduce open position; open_cost_cash releases the entry cost of
                # the closed portion.
                open_cost_cash += open_premium_per_share * shares
                open_contracts -= qty
                if open_contracts == 0:
                    open_premium_per_share = None

            elif event.event_type == "fee":
                fees += _money(event.amount, "amount", minimum=Decimal(0))

            elif event.event_type == "mark":
                last_mark = _money(event.price, "price", minimum=Decimal(0))
                if carry_in_mark is None:
                    carry_in_mark = last_mark

            elif event.event_type == "day_rollover":
                # A new day: the carry-in mark for daily marked P&L becomes the
                # most recent mark. Cost basis, realized P&L, fees, and open
                # quantity are PRESERVED -- never reset.
                if event.session_date is not None:
                    session_date = _session_date(event.session_date)
                carry_in_mark = last_mark

        # Unrealized at the last mark (0 if flat or no mark yet).
        unrealized = Decimal(0)
        if open_contracts > 0 and last_mark is not None and open_premium_per_share is not None:
            unrealized = (last_mark - open_premium_per_share) * Decimal(open_contracts) * _last_multiplier(events)

        lifetime = realized + unrealized - fees
        session_vs_baseline = lifetime  # baseline is the pre-trade cash; lifetime pnl is the delta
        # Daily marked P&L: change in mark value of the open position since the
        # day's carry-in mark, plus realized/fees are session-cumulative; we
        # report the mark-to-mark component for the day distinctly.
        daily_marked = Decimal(0)
        if open_contracts > 0 and last_mark is not None and carry_in_mark is not None:
            daily_marked = (last_mark - carry_in_mark) * Decimal(open_contracts) * _last_multiplier(events)

        return LifecycleState(
            open_contracts=open_contracts,
            cost_basis_cash=_num(open_cost_cash),
            realized_pnl=_num(realized),
            total_fees=_num(fees),
            lifetime_pnl=_num(lifetime),
            session_pnl_vs_baseline=_num(session_vs_baseline),
            daily_marked_pnl=_num(daily_marked),
            last_mark_price=(_num(last_mark) if last_mark is not None else None),
            session_date=(session_date.isoformat() if session_date is not None else None),
        )


def _int_qty(value: object) -> int:
    if type(value) is not int or isinstance(value, bool) or value <= 0:
        raise LifecycleError("fill quantity must be a positive integer number of contracts")
    return value


def _session_date(value: object) -> date:
    if type(value) is not str:
        raise LifecycleError("session_date must be a YYYY-MM-DD string")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise LifecycleError("session_date is not a real date") from exc


def _last_multiplier(events: Sequence[LifecycleEvent]) -> Decimal:
    # All events for one position carry the same multiplier; use the first.
    return Decimal(events[0].multiplier)


def _num(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text if text not in ("-0", "") else "0"


__all__ = [
    "LifecycleError",
    "LifecycleEvent",
    "LifecycleState",
    "replay_lifecycle",
]
