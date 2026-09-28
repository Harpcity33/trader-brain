"""Pure, narrow order encoding for an explicitly bound IBKR stock contract.

This module neither imports the SDK nor connects or submits anything. Creating
an order plan (including one with ``transmit=False``) is not a broker preview,
an authorization, proof of available funds, or proof of protective coverage.
Calling ``placeOrder`` with an untransmitted order would still be a mutation.

The caller must obtain and verify the contract identity from an unambiguous
broker lookup, bind the *full* account and live/paper environment, allocate IDs
from the connected client's sequence, and enforce holdings/funds/risk policy.
In particular, a SELL is not inherently reduce-only at IBKR. A masked account
suffix check is an additional check, not an identity or environment proof.

Mapping references, checked against the official Python SDK 10.50.2:
https://www.interactivebrokers.com/docs/tws-api/ref/order
https://www.interactivebrokers.com/docs/tws-api/ref/contract
https://interactivebrokers.github.io/tws-api/basic_orders.html

Only regular-hours MARKET/LIMIT/STOP_MARKET/STOP_LIMIT orders are encoded.
Extended and ALL_DAY execution are deliberately unavailable in the current
owner policy. No bracket, OCA, stop-trigger customization, order preset, or
permission bypass is synthesized here.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
import math
import re
import sys
from types import MappingProxyType
from typing import Any, Callable, Mapping

from .base import (
    BrokerCapabilityError,
    BrokerContractViolation,
    BrokerSide,
    EquityOrderType,
    MarketHours,
    OrderRequest,
    TimeInForce,
)


_MAX_ID = 2**31 - 1
_SYMBOL = re.compile(r"[A-Z][A-Z0-9.\-]{0,14}\Z")
_PRIMARY_EXCHANGE = re.compile(r"[A-Z][A-Z0-9.]{0,15}\Z")
_RETAIL_ACCOUNT = re.compile(r"(?:DU|U)[0-9]{5,16}\Z")
_ORDER_TYPES = {
    EquityOrderType.MARKET: "MKT",
    EquityOrderType.LIMIT: "LMT",
    EquityOrderType.STOP_MARKET: "STP",
    EquityOrderType.STOP_LIMIT: "STP LMT",
}


def attended_confirmation_phrase(request: OrderRequest) -> str:
    """Return the one canonical owner phrase for an attended IBKR order.

    Both the local preflight and the socket transport use this function.  A
    provider-specific copy would let the displayed phrase and the dispatch
    phrase drift while all of the individual objects still looked valid.
    """

    if not isinstance(request, OrderRequest):
        raise BrokerContractViolation("normalized IBKR order request required")
    if request.market_hours is not MarketHours.REGULAR:
        raise BrokerCapabilityError("IBKR attended execution is regular-hours-only")
    parts = [
        "CONFIRM",
        request.side.value.upper(),
        str(request.quantity),
        request.symbol,
        request.order_type.value.upper(),
    ]
    if request.order_type is EquityOrderType.LIMIT:
        parts.append(str(request.limit_price))
    elif request.order_type is EquityOrderType.STOP_MARKET:
        parts.append(str(request.stop_price))
    elif request.order_type is EquityOrderType.STOP_LIMIT:
        parts.extend(("STOP", str(request.stop_price), "LIMIT", str(request.limit_price)))
    parts.extend((request.time_in_force.value.upper(), MarketHours.REGULAR.value.upper()))
    return " ".join(parts)


def attended_order_preview(request: OrderRequest) -> Mapping[str, object]:
    """Canonical non-secret order fields bound by every attended preview."""

    if not isinstance(request, OrderRequest):
        raise BrokerContractViolation("normalized IBKR order request required")
    return MappingProxyType(
        {
            "account_masked": request.account_masked,
            "symbol": request.symbol,
            "side": request.side.value,
            "order_type": request.order_type.value,
            "quantity": request.quantity,
            "market_hours": request.market_hours.value,
            "time_in_force": request.time_in_force.value,
            "limit_price": (
                str(request.limit_price) if request.limit_price is not None else None
            ),
            "stop_price": (
                str(request.stop_price) if request.stop_price is not None else None
            ),
            "client_ref_id": request.client_ref_id,
        }
    )


def _positive_id(value: object, name: str) -> None:
    if type(value) is not int or not 0 < value <= _MAX_ID:
        raise ValueError(f"{name} must be a positive signed 32-bit integer")


def _price_for_sdk(price: Decimal, name: str) -> float:
    """Refuse decimal digits that SDK double conversion would silently lose.

The SDK's protobuf prices are IEEE doubles. Ordinary decimal prices need not
be exact binary fractions; the guarantee here is exact *decimal round-trip*
through Python's shortest double representation, not binary-exact cents.
Tick-size and broker price-band validation remain the caller's responsibility.
"""
    converted = float(price)
    if (
        not math.isfinite(converted)
        or not 0 < converted < sys.float_info.max
        or Decimal(str(converted)) != price
    ):
        raise ValueError(f"{name} cannot round-trip through the IBKR price field")
    return converted


@dataclass(frozen=True)
class IbkrContractIdentity:
    """Identity supplied by the caller's verified, unique contract lookup.

Construction validates the restricted wire shape, not the truth of a lookup.
No symbol inference, symbol alias replacement, or default listing exchange is
allowed. A broker lookup must establish this exact conId/symbol/listing tuple.
"""

    con_id: int
    symbol: str
    primary_exchange: str
    sec_type: str = "STK"
    currency: str = "USD"
    exchange: str = "SMART"

    def __post_init__(self) -> None:
        _positive_id(self.con_id, "con_id")
        if not isinstance(self.symbol, str) or not _SYMBOL.fullmatch(self.symbol):
            raise ValueError("contract symbol must be an exact normalized equity symbol")
        if (
            not isinstance(self.primary_exchange, str)
            or not _PRIMARY_EXCHANGE.fullmatch(self.primary_exchange)
            or self.primary_exchange in {"SMART", "BEST", "OVERNIGHT", "IBKRATS"}
        ):
            raise ValueError("primary_exchange must identify the actual listing exchange")
        if self.sec_type != "STK" or self.currency != "USD" or self.exchange != "SMART":
            raise ValueError("only STK USD SMART contracts are supported")


@dataclass(frozen=True)
class IbkrOrderPlan:
    """Immutable normalized request plus explicit, repr-redacted wire binding.

Do not serialize this dataclass, its ``__dict__``, or SDK objects to logs:
``repr=False`` only keeps the account identifier out of ordinary repr/str.
SDK factories supplied to ``to_sdk`` must construct fresh official SDK objects.
"""

    request: OrderRequest
    contract: IbkrContractIdentity
    account_id: str = field(repr=False)
    client_id: int
    order_id: int
    transmit: bool

    def __post_init__(self) -> None:
        if type(self.request) is not OrderRequest:
            raise ValueError("request must be an OrderRequest")
        if type(self.contract) is not IbkrContractIdentity:
            raise ValueError("contract must be an IbkrContractIdentity")
        # Re-run the shared boundary on a private frozen copy; direct plan
        # construction has exactly the same validation as the builder.
        object.__setattr__(self, "request", replace(self.request))
        object.__setattr__(self, "contract", replace(self.contract))
        if not isinstance(self.account_id, str) or not _RETAIL_ACCOUNT.fullmatch(self.account_id):
            raise ValueError("account_id must be an exact individual IBKR account identifier")
        if self.request.account_masked[-4:] != self.account_id[-4:]:
            raise ValueError("request account mask does not match the explicit account binding")
        _positive_id(self.client_id, "client_id")
        _positive_id(self.order_id, "order_id")
        if type(self.transmit) is not bool:
            raise ValueError("transmit must be an explicit boolean")
        if self.request.symbol != self.contract.symbol:
            raise ValueError("request symbol does not match the verified contract identity")
        if self.request.market_hours is not MarketHours.REGULAR:
            raise ValueError("IBKR execution is restricted to regular hours")
        for name in ("limit_price", "stop_price"):
            price = getattr(self.request, name)
            if price is not None:
                _price_for_sdk(price, name)

    @property
    def action(self) -> str:
        return "BUY" if self.request.side is BrokerSide.BUY else "SELL"

    @property
    def order_type(self) -> str:
        return _ORDER_TYPES[self.request.order_type]

    @property
    def quantity(self) -> int:
        return self.request.quantity

    @property
    def tif(self) -> str:
        return "DAY" if self.request.time_in_force is TimeInForce.GFD else "GTC"

    @property
    def outside_rth(self) -> bool:
        return False

    @property
    def order_ref(self) -> str:
        return self.request.client_ref_id

    @property
    def limit_price(self) -> Decimal | None:
        return self.request.limit_price

    @property
    def stop_price(self) -> Decimal | None:
        return self.request.stop_price

    @property
    def what_if(self) -> bool:
        return False

    def to_sdk(
        self,
        *,
        contract_factory: Callable[[], Any],
        order_factory: Callable[[], Any],
    ) -> tuple[Any, Any]:
        """Construct SDK data objects only; this method never sends them.

        Absent prices keep the SDK's UNSET_DOUBLE sentinel, never an invented
        zero price. Constructors must be fresh official SDK constructors (or
        inert test doubles), not factories that return cached mutable orders.
        """
        # Validate again before allocating mutable SDK objects.
        plan = replace(self)
        try:
            sdk_contract = contract_factory()
            sdk_order = order_factory()
            if sdk_contract is sdk_order:
                raise ValueError("SDK factories must produce separate objects")
            for name, value in (
                ("conId", plan.contract.con_id),
                ("symbol", plan.contract.symbol),
                ("secType", plan.contract.sec_type),
                ("currency", plan.contract.currency),
                ("exchange", plan.contract.exchange),
                ("primaryExchange", plan.contract.primary_exchange),
            ):
                setattr(sdk_contract, name, value)
            for name, value in (
                ("orderId", plan.order_id),
                ("clientId", plan.client_id),
                ("account", plan.account_id),
                ("action", plan.action),
                ("totalQuantity", Decimal(plan.quantity)),
                ("orderType", plan.order_type),
                ("tif", plan.tif),
                ("outsideRth", plan.outside_rth),
                ("includeOvernight", False),
                ("orderRef", plan.order_ref),
                ("transmit", plan.transmit),
                ("whatIf", False),
                ("parentId", 0),
                ("ocaGroup", ""),
                ("ocaType", 0),
                ("conditions", []),
                ("conditionsCancelOrder", False),
                ("conditionsIgnoreRth", False),
                ("triggerMethod", 0),
                ("overridePercentageConstraints", False),
                ("advancedErrorOverride", ""),
            ):
                setattr(sdk_order, name, value)
            if plan.limit_price is not None:
                sdk_order.lmtPrice = _price_for_sdk(plan.limit_price, "limit_price")
            if plan.stop_price is not None:
                sdk_order.auxPrice = _price_for_sdk(plan.stop_price, "stop_price")
        except Exception:
            # An injected factory or setter can include private account data
            # in its exception. Do not relay or chain it into normal logs.
            raise ValueError("IBKR SDK object construction failed") from None
        return sdk_contract, sdk_order


def build_ibkr_order_plan(
    request: OrderRequest,
    *,
    contract: IbkrContractIdentity,
    account_id: str,
    client_id: int,
    order_id: int,
    transmit: bool,
) -> IbkrOrderPlan:
    """Encode one exact request; never reserve an ID or contact a broker."""
    return IbkrOrderPlan(
        request=request,
        contract=contract,
        account_id=account_id,
        client_id=client_id,
        order_id=order_id,
        transmit=transmit,
    )


__all__ = [
    "IbkrContractIdentity",
    "IbkrOrderPlan",
    "attended_confirmation_phrase",
    "attended_order_preview",
    "build_ibkr_order_plan",
]
