"""Offline first-tier U.S. IBKR Pro SMART base-commission illustrations.

Source checked 2026-09-13:
https://www.interactivebrokers.com/en/pricing/commissions-stocks.php

The U.S. table lists $0.0035/share, a $0.35 order minimum, and a 1% trade-value
cap; its notes also contain a broader 0.5% tiered-cap statement. This module
does not resolve that discrepancy: differing results raise an explicit error.

Scope is direct-client, USD, whole-share, ordinary SMART orders entirely within
the first 300,000 monthly tier-counted shares. An order means one independently
commissioned execution order, not an individual partial fill. Modifications,
overnight carry, directed/API alternate routes, fractional shares, dividend
reinvestment, special arrangements, and later volume tiers are excluded.

Results exclude regulatory, exchange, clearing, pass-through and other fees,
rebates, spread and slippage. No broker rounding rule is assumed. These are
unrounded base estimates, NOT all-in costs, cash/risk reserves, price guarantees
or a verified account billing contract. Nothing here connects to or configures
a broker, requests quotes, or participates in the production execution path.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import (
    Context, Decimal, DecimalException, Inexact, MAX_EMAX, MIN_EMIN, localcontext,
)

from ..money import NumericPolicyError, finite_decimal


RATE_PER_SHARE = Decimal("0.0035")
MINIMUM_PER_ORDER = Decimal("0.35")
TABLE_TRADE_VALUE_CAP = Decimal("0.01")
DISCLOSURE_TRADE_VALUE_CAP = Decimal("0.005")
FIRST_TIER_SHARE_LIMIT = 300_000

ExactInput = Decimal | int | str


class UnverifiedCommissionCap(ValueError):
    """The published cap discrepancy changes this offline base estimate."""


def _exact_decimal(value: ExactInput, *, field: str) -> Decimal:
    # Do not hide a binary-float approximation behind Decimal(str(value)).
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, str)):
        raise NumericPolicyError(f"{field} must be Decimal, integer or decimal text")
    return finite_decimal(value, field=field)


def _tier_quantity(value: ExactInput, *, field: str, allow_zero: bool) -> int:
    numeric = _exact_decimal(value, field=field)
    minimum = 0 if allow_zero else 1
    # Bound before int conversion, including enormous scientific notation.
    if not minimum <= numeric <= FIRST_TIER_SHARE_LIMIT:
        raise NumericPolicyError(
            f"{field} must be between {minimum} and {FIRST_TIER_SHARE_LIMIT} shares"
        )
    if numeric != numeric.to_integral_value():
        raise NumericPolicyError(f"{field} must be a whole number of shares")
    return int(numeric)


def estimate_us_smart_tier1_base_commission(
    quantity: ExactInput,
    price: ExactInput,
    *,
    monthly_volume_before: ExactInput = 0,
) -> Decimal:
    """Estimate one ordinary order's base commission, subject to module scope.

    ``price`` is the assumed execution price, not a promise of a limit fill.
    ``monthly_volume_before`` must include all relevant tier-counted volume;
    this pure helper cannot discover an account's actual volume or pricing.
    The default explicitly models the beginning of the first tier. Crossing
    its boundary or encountering conflicting published caps fails closed.
    """
    shares = _tier_quantity(quantity, field="quantity", allow_zero=False)
    before = _tier_quantity(
        monthly_volume_before, field="monthly_volume_before", allow_zero=True
    )
    if before + shares > FIRST_TIER_SHARE_LIMIT:
        raise NumericPolicyError("order exceeds the modeled first monthly volume tier")
    execution_price = _exact_decimal(price, field="price")
    if execution_price <= 0:
        raise NumericPolicyError("price must be positive")

    # Independent of a caller's low precision/rounding context. Product digit
    # counts are bounded by the price coefficient plus the quantity and rates.
    precision = max(28, len(execution_price.as_tuple().digits) + len(str(shares)) + 8)
    calculation_context = Context(prec=precision, Emax=MAX_EMAX, Emin=MIN_EMIN)
    calculation_context.traps[Inexact] = True
    try:
        with localcontext(calculation_context):
            trade_value = Decimal(shares) * execution_price
            uncapped = max(MINIMUM_PER_ORDER, Decimal(shares) * RATE_PER_SHARE)
            table_estimate = min(uncapped, trade_value * TABLE_TRADE_VALUE_CAP)
            disclosure_estimate = min(
                uncapped, trade_value * DISCLOSURE_TRADE_VALUE_CAP
            )
    except DecimalException as exc:
        raise NumericPolicyError("price is outside the supported decimal range") from exc
    if table_estimate != disclosure_estimate:
        raise UnverifiedCommissionCap(
            "published 1% table and 0.5% disclosure caps differ for this order; "
            "broker billing clarification is required"
        )
    return table_estimate


def estimate_us_smart_tier1_base_commission_total(
    orders: Iterable[tuple[ExactInput, ExactInput]],
    *,
    monthly_volume_before: ExactInput = 0,
) -> Decimal:
    """Sum (quantity, assumed execution price) orders with separate minimums.

    Use each executed entry/exit order once, not each partial-fill event. Do
    not include an unexecuted bracket sibling as if it incurred a commission.
    Empty input is zero; malformed or out-of-scope input raises, never returns
    a partial total. Orders must share this module's ordinary-order scope.
    """
    before = _tier_quantity(
        monthly_volume_before, field="monthly_volume_before", allow_zero=True
    )
    total = Decimal("0")
    with localcontext(Context(prec=28)):
        for quantity, price in orders:
            shares = _tier_quantity(quantity, field="quantity", allow_zero=False)
            total += estimate_us_smart_tier1_base_commission(
                shares, price, monthly_volume_before=before
            )
            before += shares
    return total


__all__ = [
    "UnverifiedCommissionCap",
    "estimate_us_smart_tier1_base_commission",
    "estimate_us_smart_tier1_base_commission_total",
]
