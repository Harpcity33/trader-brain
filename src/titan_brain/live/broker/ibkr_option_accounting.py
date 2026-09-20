"""Multiplier-aware option cash/exposure accounting (standalone, offline).

Milestone 2, unit 2b of the attended-options plan.

The existing session accounting in ``session_trading_calculation.py`` is
stock-only by construction: it rejects non-STK scope and computes cash as
``sign * quantity * price`` with NO contract multiplier (one contract == one
share).  Options are not one share -- a standard equity option controls
``multiplier`` shares -- so reusing that arithmetic would misprice every option
fill.  Per the plan we do NOT modify or relax the stock/session path; we add a
DISTINCT, reviewed options-aware accounting helper here, keyed on the canonical
``IbkrOptionContractIdentity`` from unit 2a.

This module is pure and offline: no broker SDK, no socket, no production config,
no live authority.  It computes accounting facts from explicitly supplied
fills; it does not fetch data, decide budgets, or authorize anything.  Money is
Decimal end-to-end; a missing multiplier or malformed fill fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from typing import Mapping, Sequence

from .ibkr_option_orders import IbkrOptionContractIdentity, OptionContractError


class OptionAccountingError(ValueError):
    """A well-formed, non-secret option accounting error."""


def _money(value: object, field: str) -> Decimal:
    # Money arrives as str or int only -- never float.
    if isinstance(value, bool) or type(value) not in (str, int):
        raise OptionAccountingError(f"{field} must be a string or integer")
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise OptionAccountingError(f"{field} is not a valid decimal") from exc
    if not parsed.is_finite():
        raise OptionAccountingError(f"{field} must be finite")
    return parsed


@dataclass(frozen=True)
class OptionFill:
    """One option execution against a resolved contract identity.

    ``price`` is the per-contract-share premium (as quoted); the multiplier is
    taken from the contract identity, never assumed.  ``commission`` is the
    signed cash cost of the fill's fees (>= 0).
    """

    identity: IbkrOptionContractIdentity
    side: str      # "BUY" or "SELL"
    quantity: int  # number of contracts, positive
    price: Decimal
    commission: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.identity, IbkrOptionContractIdentity):
            raise OptionAccountingError("identity must be an IbkrOptionContractIdentity")
        if self.side not in ("BUY", "SELL"):
            raise OptionAccountingError("side must be BUY or SELL")
        if type(self.quantity) is not int or isinstance(self.quantity, bool) or self.quantity <= 0:
            raise OptionAccountingError("quantity must be a positive integer number of contracts")
        object.__setattr__(self, "price", _money(self.price, "price"))
        object.__setattr__(self, "commission", _money(self.commission, "commission"))
        if self.price < 0:
            raise OptionAccountingError("price must be non-negative")
        if self.commission < 0:
            raise OptionAccountingError("commission must be non-negative")


@dataclass(frozen=True)
class OptionAccountingResult:
    """Cash delta and per-contract net position from a set of option fills."""

    cash_delta: Decimal            # signed cash change (buys reduce cash)
    total_commission: Decimal
    net_contracts: Mapping[str, int]  # wire_fingerprint -> signed net contracts


def account_option_fills(fills: Sequence[OptionFill]) -> OptionAccountingResult:
    """Compute multiplier-aware cash delta and net contracts from fills.

    Cash for one fill is ``sign * quantity * multiplier * price`` plus the
    (always subtracted) commission -- the multiplier is what distinguishes this
    from the one-share stock arithmetic.  Positions are netted per exact
    contract identity (by its wire fingerprint), so a call and a put, or two
    different strikes, never net against each other.
    """

    if not isinstance(fills, (list, tuple)) or not fills:
        raise OptionAccountingError("at least one option fill is required")
    with localcontext() as context:
        context.prec = 80
        cash = Decimal(0)
        commission_total = Decimal(0)
        net: dict[str, int] = {}
        for fill in fills:
            if not isinstance(fill, OptionFill):
                raise OptionAccountingError("every fill must be an OptionFill")
            key = fill.identity.wire_fingerprint()
            multiplier = Decimal(fill.identity.multiplier)
            sign = 1 if fill.side == "BUY" else -1
            # Multiplier-aware premium: contracts x shares-per-contract x price.
            cash -= sign * Decimal(fill.quantity) * multiplier * fill.price
            cash -= fill.commission
            commission_total += fill.commission
            net[key] = net.get(key, 0) + sign * fill.quantity
        # Drop fully-closed contracts from the reported net map.
        net_nonzero = {key: qty for key, qty in net.items() if qty != 0}
        return OptionAccountingResult(
            cash_delta=cash,
            total_commission=commission_total,
            net_contracts=net_nonzero,
        )


__all__ = [
    "OptionAccountingError",
    "OptionAccountingResult",
    "OptionFill",
    "account_option_fills",
]
