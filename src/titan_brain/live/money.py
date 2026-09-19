"""Exact numeric primitives for the live execution boundary.

Broker payloads are text-like and financial controls are exact.  The live
runtime therefore converts values to finite :class:`~decimal.Decimal` objects
at the boundary and stores dollar amounts as integer cents.  Booleans are
rejected explicitly because ``bool`` is an ``int`` subclass in Python.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


CENT = Decimal("0.01")


class NumericPolicyError(ValueError):
    """Raised when execution-critical numeric input is ambiguous or unsafe."""


def finite_decimal(value: Any, *, field: str = "value") -> Decimal:
    """Return ``value`` as a finite Decimal without binary-float arithmetic."""

    if value is None or isinstance(value, bool):
        raise NumericPolicyError(f"{field} must be a finite decimal")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise NumericPolicyError(f"{field} must be a finite decimal") from exc
    if not result.is_finite():
        raise NumericPolicyError(f"{field} must be finite")
    return result


def decimal_value(value: Any, field: str = "value") -> Decimal:
    """Compatibility spelling for boundary parsers that pass ``field`` positionally."""

    return finite_decimal(value, field=field)


def nonnegative_decimal(value: Any, *, field: str = "value") -> Decimal:
    result = finite_decimal(value, field=field)
    if result < 0:
        raise NumericPolicyError(f"{field} must be nonnegative")
    return result


def positive_decimal(value: Any, *, field: str = "value") -> Decimal:
    result = finite_decimal(value, field=field)
    if result <= 0:
        raise NumericPolicyError(f"{field} must be positive")
    return result


def money(value: Any, *, field: str = "amount") -> Decimal:
    """Return an exact, cent-denominated monetary amount.

    Execution policy must choose any rounding before this boundary.  Silently
    rounding an over-precise risk amount could increase authorized exposure.
    """

    result = finite_decimal(value, field=field)
    normalized = result.quantize(CENT)
    if normalized != result:
        raise NumericPolicyError(f"{field} must be exact to one cent")
    return normalized


def nonnegative_money(value: Any, *, field: str = "amount") -> Decimal:
    result = money(value, field=field)
    if result < 0:
        raise NumericPolicyError(f"{field} must be nonnegative")
    return result


def positive_money(value: Any, *, field: str = "amount") -> Decimal:
    result = money(value, field=field)
    if result <= 0:
        raise NumericPolicyError(f"{field} must be positive")
    return result


def to_cents(value: Any, *, field: str = "amount") -> int:
    """Convert exact money to signed integer cents."""

    return int(money(value, field=field) / CENT)


def from_cents(value: Any, *, field: str = "cents") -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int):
        raise NumericPolicyError(f"{field} must be an integer")
    return (Decimal(value) * CENT).quantize(CENT)


def whole_shares(value: Any, *, field: str = "quantity", allow_zero: bool = False) -> int:
    """Return a strict integral share quantity.

    Integral broker strings/Decimals are accepted, while fractional values,
    booleans, non-finite values, and non-positive quantities fail closed.
    """

    numeric = finite_decimal(value, field=field)
    integral = numeric.to_integral_value()
    if numeric != integral:
        raise NumericPolicyError(f"{field} must be a whole number of shares")
    quantity = int(integral)
    if quantity < 0 or (quantity == 0 and not allow_zero):
        qualifier = "nonnegative" if allow_zero else "positive"
        raise NumericPolicyError(f"{field} must be {qualifier}")
    return quantity


__all__ = [
    "CENT",
    "NumericPolicyError",
    "decimal_value",
    "finite_decimal",
    "from_cents",
    "money",
    "nonnegative_decimal",
    "nonnegative_money",
    "positive_decimal",
    "positive_money",
    "to_cents",
    "whole_shares",
]
