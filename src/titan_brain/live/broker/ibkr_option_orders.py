"""Canonical IBKR option contract identity and deterministic wire form.

Milestone 2 of the attended-options plan.  This module is intentionally
STANDALONE and READ-ONLY with respect to the running system:

* it imports no broker SDK and opens no socket (like ``ibkr_orders.py`` it is
  pure data + validation);
* it does NOT touch, relax, or remove the stock-only rejection guards in
  ``ibkr_orders.py`` / ``ibkr_sdk.py`` -- those stay exactly as they are and a
  non-STK contract on the equity path is still rejected;
* it is not wired into ``execution.py``/``policy.py``/``reconcile.py`` and it
  carries no live authority.  A well-formed identity is a *description* of a
  contract, never permission to trade it.

It resolves the exact fields the plan requires -- ``con_id``, underlying,
call/put ``right``, ``strike``, ``expiry``, ``trading_class``, ``currency``,
``exchange``, ``multiplier``, ``deliverable``, exercise style and settlement
type -- validates them strictly (rejecting ambiguous / adjusted / unsupported
contracts and NEVER assuming 100 shares per contract), and serializes to a
deterministic, sorted, ``sha256`` wire fingerprint that mirrors the
normalization used by ``ibkr_sdk.sdk_order_fingerprint`` so the two schemes
stay comparable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from types import MappingProxyType
from typing import Mapping


WIRE_SCHEMA = "ibkr-sdk-option-order-v1"

_MAX_ID = 2**31 - 1
# Underlying symbol: same shape the equity path accepts.
_SYMBOL = re.compile(r"[A-Z][A-Z0-9.\-]{0,14}\Z")
# Trading class: IBKR option trading classes are alphanumeric, no spaces.
_TRADING_CLASS = re.compile(r"[A-Z][A-Z0-9.\-]{0,14}\Z")
# Real listing/option exchange or SMART routing is allowed for options; a bare
# routing pseudo-venue that is not a real options exchange is rejected below.
_EXCHANGE = re.compile(r"[A-Z][A-Z0-9.]{0,15}\Z")
_RIGHTS = frozenset({"C", "P"})
_EXERCISE_STYLES = frozenset({"AMERICAN", "EUROPEAN"})
_SETTLEMENT_TYPES = frozenset({"PHYSICAL", "CASH"})
# Standard equity-option deliverable; anything else is treated as adjusted /
# non-standard and rejected until explicitly supported.
_STANDARD_DELIVERABLE = "100_SHARES"
_STANDARD_MULTIPLIER = 100


class OptionContractError(ValueError):
    """A well-formed, non-secret validation error for an option identity."""


def _positive_id(value: object, field: str) -> int:
    if type(value) is not int or isinstance(value, bool) or not 1 <= value <= _MAX_ID:
        raise OptionContractError(f"{field} must be a positive 32-bit integer")
    return value


def _decimal(value: object, field: str) -> Decimal:
    # Money/strike values must arrive as str or int, never float (which would
    # smuggle binary rounding into an exact contract term).
    if isinstance(value, bool) or type(value) not in (str, int):
        raise OptionContractError(f"{field} must be a string or integer, not float/other")
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise OptionContractError(f"{field} is not a valid decimal") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise OptionContractError(f"{field} must be a positive finite decimal")
    return parsed


def _expiry_date(value: object) -> date:
    # Expiry is the option's last-trade / expiration date as YYYYMMDD. We
    # validate it against the real calendar; we do NOT infer it from a symbol.
    if type(value) is not str or not re.fullmatch(r"[0-9]{8}", value):
        raise OptionContractError("expiry must be an 8-digit YYYYMMDD string")
    try:
        return date(int(value[0:4]), int(value[4:6]), int(value[6:8]))
    except ValueError as exc:
        raise OptionContractError("expiry is not a real calendar date") from exc


@dataclass(frozen=True)
class IbkrOptionContractIdentity:
    """A fully-resolved, unambiguous IBKR option contract.

    Every field is required.  There is deliberately no default multiplier: the
    plan forbids assuming 100 shares per contract, so the caller must state the
    real contract multiplier and it must be internally consistent with a
    standard deliverable (a mismatch signals an adjusted contract, which is
    rejected until explicitly supported).
    """

    con_id: int
    symbol: str          # underlying
    right: str           # "C" or "P"
    strike: Decimal
    expiry: str          # YYYYMMDD last-trade/expiration date
    trading_class: str
    multiplier: int
    deliverable: str
    exercise_style: str  # AMERICAN / EUROPEAN
    settlement_type: str  # PHYSICAL / CASH
    currency: str = "USD"
    exchange: str = "SMART"

    def __post_init__(self) -> None:
        _positive_id(self.con_id, "con_id")
        if not isinstance(self.symbol, str) or not _SYMBOL.fullmatch(self.symbol):
            raise OptionContractError("symbol (underlying) is invalid")
        if self.right not in _RIGHTS:
            raise OptionContractError("right must be 'C' or 'P'")
        # Normalize/validate the strike to an exact Decimal.
        object.__setattr__(self, "strike", _decimal(self.strike, "strike"))
        # Validate the expiry against the real calendar (raises if not real).
        _expiry_date(self.expiry)
        if not isinstance(self.trading_class, str) or not _TRADING_CLASS.fullmatch(self.trading_class):
            raise OptionContractError("trading_class is invalid")
        if type(self.multiplier) is not int or isinstance(self.multiplier, bool) or self.multiplier < 1:
            raise OptionContractError("multiplier must be a positive integer (no assumed default)")
        if self.currency != "USD":
            raise OptionContractError("only USD option contracts are supported")
        if not isinstance(self.exchange, str) or not _EXCHANGE.fullmatch(self.exchange):
            raise OptionContractError("exchange is invalid")
        if self.exercise_style not in _EXERCISE_STYLES:
            raise OptionContractError("exercise_style must be AMERICAN or EUROPEAN")
        if self.settlement_type not in _SETTLEMENT_TYPES:
            raise OptionContractError("settlement_type must be PHYSICAL or CASH")
        # Reject ambiguous / adjusted contracts: a standard equity option
        # deliverable pairs with the standard 100 multiplier.  A non-standard
        # deliverable, or a standard deliverable at a non-100 multiplier, is an
        # adjusted contract (e.g. post-split) that this first lane does not
        # support -- rejected rather than silently mispriced.
        if self.deliverable == _STANDARD_DELIVERABLE:
            if self.multiplier != _STANDARD_MULTIPLIER:
                raise OptionContractError(
                    "adjusted contract: standard deliverable requires the 100 multiplier"
                )
        else:
            raise OptionContractError(
                "non-standard or adjusted deliverable is not supported in this lane"
            )

    def canonical_dict(self) -> Mapping[str, object]:
        """Sorted, secret-free canonical representation of the identity.

        The strike is emitted as a normalized decimal string (no float) so that
        equivalent inputs (``"5.00"`` and ``"5"``) fingerprint identically, the
        same rule ``ibkr_sdk._canonical_value`` applies.
        """

        strike = format(self.strike, "f")
        if "." in strike:
            strike = strike.rstrip("0").rstrip(".")
        return MappingProxyType(
            {
                "schema": WIRE_SCHEMA,
                "conId": self.con_id,
                "symbol": self.symbol,
                "secType": "OPT",
                "right": self.right,
                "strike": strike if strike not in ("-0", "") else "0",
                "lastTradeDateOrContractMonth": self.expiry,
                "tradingClass": self.trading_class,
                "multiplier": str(self.multiplier),
                "deliverable": self.deliverable,
                "exerciseStyle": self.exercise_style,
                "settlementType": self.settlement_type,
                "currency": self.currency,
                "exchange": self.exchange,
            }
        )

    def wire_fingerprint(self) -> str:
        """Deterministic sha256 over the canonical identity.

        Mirrors ``sdk_order_fingerprint`` normalization (sorted keys, compact
        separators, ascii) so the option and equity wire schemes stay
        comparable and stable across runs.  This is an identity digest only --
        it carries no order, account, or authority material.
        """

        payload = dict(self.canonical_dict())
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
                "utf-8"
            )
        ).hexdigest()


__all__ = [
    "IbkrOptionContractIdentity",
    "OptionContractError",
    "WIRE_SCHEMA",
]
