"""Professional equity-universe eligibility with explicit boundary evidence.

News is context, never a hard eligibility condition.  Missing price, volume,
tradability, or quote freshness fails closed and is recorded rather than
inferred.  This module is pure and has no broker authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping


MIN_PRICE_EXCLUSIVE = Decimal("5.00")
MIN_SESSION_VOLUME_INCLUSIVE = 750_000


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


@dataclass(frozen=True)
class EquityEligibilityEvidence:
    ticker: str
    price: Decimal | str | float | int | None
    session_volume_shares: int | None
    robinhood_tradable: bool | None
    quote_fresh: bool | None
    bid: Decimal | str | float | int | None = None
    ask: Decimal | str | float | int | None = None
    displayed_bid_depth: int | None = None
    displayed_ask_depth: int | None = None
    median_20d_dollar_volume: Decimal | str | float | int | None = None
    relative_volume: Decimal | str | float | int | None = None
    current_dollar_volume: Decimal | str | float | int | None = None
    historical_spread_behavior: Mapping[str, Any] | None = None
    fresh_news_available: bool | None = None


@dataclass(frozen=True)
class EquityEligibilityDecision:
    eligible: bool
    hard_gate_failures: tuple[str, ...]
    price: Decimal | None
    session_volume_shares: int | None
    spread_dollars: Decimal | None
    spread_pct: Decimal | None
    context: Mapping[str, Any]


def evaluate_professional_equity_eligibility(
    evidence: EquityEligibilityEvidence,
) -> EquityEligibilityDecision:
    """Apply the live-core universe gates before SETUP_SCORE is considered."""

    failures: list[str] = []
    ticker = str(evidence.ticker).strip().upper()
    if not ticker:
        failures.append("TICKER_UNAVAILABLE")

    price = _decimal(evidence.price)
    if price is None:
        failures.append("PRICE_UNAVAILABLE")
    elif price <= MIN_PRICE_EXCLUSIVE:
        failures.append("PRICE_NOT_STRICTLY_ABOVE_5")

    volume = evidence.session_volume_shares
    if isinstance(volume, bool) or not isinstance(volume, int):
        failures.append("VOLUME_UNAVAILABLE")
        normalized_volume = None
    else:
        normalized_volume = volume
        if volume < MIN_SESSION_VOLUME_INCLUSIVE:
            failures.append("VOLUME_BELOW_750000")

    if evidence.robinhood_tradable is not True:
        failures.append(
            "TRADABILITY_UNAVAILABLE"
            if evidence.robinhood_tradable is None
            else "NOT_ROBINHOOD_TRADABLE"
        )
    if evidence.quote_fresh is not True:
        failures.append("QUOTE_NOT_FRESH")

    bid = _decimal(evidence.bid)
    ask = _decimal(evidence.ask)
    spread_dollars: Decimal | None = None
    spread_pct: Decimal | None = None
    if bid is not None and ask is not None and bid > 0 and ask >= bid:
        spread_dollars = ask - bid
        midpoint = (ask + bid) / Decimal("2")
        spread_pct = spread_dollars / midpoint if midpoint > 0 else None

    context = {
        "median_20d_dollar_volume": _decimal(evidence.median_20d_dollar_volume),
        "relative_volume": _decimal(evidence.relative_volume),
        "current_dollar_volume": _decimal(evidence.current_dollar_volume),
        "displayed_bid_depth": evidence.displayed_bid_depth,
        "displayed_ask_depth": evidence.displayed_ask_depth,
        "historical_spread_behavior": evidence.historical_spread_behavior,
        # Explicitly informational: False/None never produces a hard failure.
        "fresh_news_available": evidence.fresh_news_available,
    }
    return EquityEligibilityDecision(
        eligible=not failures,
        hard_gate_failures=tuple(dict.fromkeys(failures)),
        price=price,
        session_volume_shares=normalized_volume,
        spread_dollars=spread_dollars,
        spread_pct=spread_pct,
        context=context,
    )


__all__ = [
    "EquityEligibilityDecision",
    "EquityEligibilityEvidence",
    "MIN_PRICE_EXCLUSIVE",
    "MIN_SESSION_VOLUME_INCLUSIVE",
    "evaluate_professional_equity_eligibility",
]

