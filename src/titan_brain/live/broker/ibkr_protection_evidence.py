"""Narrow evidence for an IBKR simulated stop awaiting election.

IBKR documents PreSubmitted as accepted but not yet elected, not as a generic
working order. Only a joined, current openOrder/orderStatus standalone long
stop is eligible here. This is not a fill-price guarantee or extended-hours
protection. No message text, full account ID, SDK object or authority is kept.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import OrderSnapshot


_STATES = frozenset({
    "PendingSubmit", "ApiPending", "PreSubmitted", "Submitted", "PendingCancel",
    "ApiCancelled", "Cancelled", "Filled", "Inactive",
})
_FALSE_FLAGS = (
    "outsideRth", "includeOvernight", "notHeld",
)
_EMPTY_FIELDS = (
    "ocaGroup", "algoStrategy", "hedgeType", "goodAfterTime", "goodTillDate",
)


def _time(value: object) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def _int(value: object, *, positive: bool = False) -> bool:
    return type(value) is int and (1 if positive else 0) <= value <= 9_223_372_036_854_775_807


def _amount(value: object) -> Decimal | None:
    # SDK Decimal/int/plain decimal text only, not arbitrary __str__/float.
    if type(value) not in (Decimal, int, str):
        return None
    if type(value) is str and (len(value) > 64 or re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value) is None):
        return None
    try:
        result = Decimal(value)
        return result if result.is_finite() and 0 <= result <= Decimal("1e15") else None
    except (InvalidOperation, ValueError):
        return None


def _whole(value: object, *, positive: bool = False) -> bool:
    return (
        type(value) is Decimal and value.is_finite()
        and (Decimal(1) if positive else Decimal(0)) <= value <= Decimal(2_147_483_647)
        and value == value.to_integral_value()
    )


@dataclass(frozen=True)
class IbkrOrderStatusFact:
    order_id: int
    client_id: int
    perm_id: int
    parent_id: int
    status: str
    filled: Decimal
    remaining: Decimal
    why_held_present: bool | None
    received_at: datetime


def capture_ibkr_order_status_fact(
    *, order_id: object, client_id: object, perm_id: object, parent_id: object,
    status: object, filled: object, remaining: object, why_held: object,
    received_at: datetime,
) -> IbkrOrderStatusFact | None:
    """Copy bounded callback primitives; unavailable hold text is not empty."""
    normalized_filled, normalized_remaining = _amount(filled), _amount(remaining)
    if (
        not all(_int(value) for value in (order_id, client_id, perm_id, parent_id))
        or type(status) is not str or status not in _STATES
        or normalized_filled is None or normalized_remaining is None
        or not _time(received_at)
    ):
        return None
    held = bool(why_held.strip()) if type(why_held) is str and len(why_held) <= 4096 else None
    return IbkrOrderStatusFact(
        order_id, client_id, perm_id, parent_id, status,
        normalized_filled, normalized_remaining, held, received_at,
    )


@dataclass(frozen=True)
class IbkrProtectionEvidence:
    broker_order_id: str
    account_masked: str
    contract_id: int
    symbol: str
    security_type: str
    currency: str
    client_id: int
    order_id: int
    perm_id: int
    parent_id: int
    action: str
    order_type: str
    time_in_force: str
    requested_quantity: Decimal
    stop_price: Decimal
    transmit: bool | None
    outside_rth: bool | None
    include_overnight: bool | None
    not_held: bool | None
    oca_group_present: bool | None
    conditions_present: bool | None
    algo_present: bool | None
    hedge_present: bool | None
    good_after_time_present: bool | None
    good_till_date_present: bool | None
    source: str
    open_order_status: str
    blocking_warning_present: bool
    open_order_received_at: datetime
    status: IbkrOrderStatusFact | None
    collection_started_at: datetime
    collection_completed_at: datetime


def protection_evidence_facts(evidence: IbkrProtectionEvidence | None) -> tuple | None:
    """Immutable material facts, excluding per-read receipt timestamps."""
    if evidence is None:
        return None
    return (
        tuple((key, value) for key, value in vars(evidence).items() if key not in {
            "status", "open_order_received_at", "collection_started_at", "collection_completed_at",
        }),
        None if evidence.status is None else tuple(
            (key, value) for key, value in vars(evidence.status).items() if key != "received_at"
        ),
    )


def capture_ibkr_protection_evidence(
    *, contract: object, order: object, broker_order_id: str,
    expected_account_id: str, account_masked: str, source: str,
    open_order_status: str, blocking_warning_present: bool,
    open_order_received_at: datetime, status: IbkrOrderStatusFact | None,
    collection_started_at: datetime, collection_completed_at: datetime,
) -> IbkrProtectionEvidence | None:
    """Join exact-account raw order/contract with one same-collection status.

    Missing SDK fields stay unknown. They are never replaced by permissive
    defaults. A caller must discard status evidence after a malformed/conflicted
    callback, generation change, or collection boundary.
    """
    try:
        account = getattr(order, "account", None)
        if type(account) is not str or type(expected_account_id) is not str or not expected_account_id or account != expected_account_id:
            return None
        if type(account_masked) is not str or re.fullmatch(r"(?:••••|\*\*\*\*)[0-9]{4}", account_masked) is None or account_masked[-4:] != account[-4:]:
            return None
        identifiers = tuple(getattr(order, name, None) for name in ("clientId", "orderId", "permId", "parentId"))
        contract_id = getattr(contract, "conId", None)
        if not all(_int(value) for value in identifiers) or not _int(contract_id, positive=True):
            return None
        symbol, security_type, currency = (getattr(contract, name, None) for name in ("symbol", "secType", "currency"))
        action, order_type, tif = (getattr(order, name, None) for name in ("action", "orderType", "tif"))
        if (
            type(symbol) is not str or re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", symbol) is None
            or security_type not in ("STK", "OPT", "FOP") or currency not in ("USD", "CAD", "EUR", "GBP", "")
            or action not in ("BUY", "SELL") or order_type not in ("STP", "STP LMT", "LMT", "MKT")
            or tif not in ("DAY", "GTC") or open_order_status not in _STATES
            or source not in ("open", "completed") or type(blocking_warning_present) is not bool
        ):
            return None
        raw_stop = getattr(order, "auxPrice", None)
        # IB's order price field is an SDK double, unlike share quantities.
        if type(raw_stop) is float:
            raw_stop = str(raw_stop)
        requested, stop = _amount(getattr(order, "totalQuantity", None)), _amount(raw_stop)
        if requested is None or stop is None:
            return None
        flags = {}
        for name in ("transmit", *_FALSE_FLAGS):
            value = getattr(order, name, None)
            flags[name] = value if type(value) is bool else None
        presence = {}
        for name in _EMPTY_FIELDS:
            value = getattr(order, name, None)
            presence[name] = bool(value.strip()) if type(value) is str and len(value) <= 4096 else None
        conditions = getattr(order, "conditions", None)
        conditions_present = bool(conditions) if type(conditions) in (list, tuple) else None
        if not all(_time(value) for value in (open_order_received_at, collection_started_at, collection_completed_at)):
            return None
        return IbkrProtectionEvidence(
            broker_order_id, account_masked, contract_id, symbol, security_type, currency,
            *identifiers, action, order_type, tif, requested, stop,
            flags["transmit"], flags["outsideRth"], flags["includeOvernight"], flags["notHeld"],
            presence["ocaGroup"], conditions_present, presence["algoStrategy"], presence["hedgeType"],
            presence["goodAfterTime"], presence["goodTillDate"], source, open_order_status,
            blocking_warning_present, open_order_received_at, status,
            collection_started_at, collection_completed_at,
        )
    except Exception:
        return None


def is_eligible_presubmitted_stop(order: "OrderSnapshot") -> bool:
    """Recognize accepted simulated stops, never generic queued orders.

    Receipts must come from one <=5-second finite collection and match the
    snapshot. The surrounding authoritative snapshot freshness check remains
    necessary; this function does not compare historical observations to now.
    """
    try:
        return _eligible_presubmitted_stop(order)
    except Exception:
        return False


def _eligible_presubmitted_stop(order: "OrderSnapshot") -> bool:
    from .base import BrokerSide, EquityOrderType, MarketHours, OrderSnapshot, TimeInForce
    from ..models import BrokerOrderState

    if type(order) is not OrderSnapshot or order.state is not BrokerOrderState.QUEUED:
        return False
    evidence = order.ibkr_protection_evidence
    if type(evidence) is not IbkrProtectionEvidence or type(evidence.status) is not IbkrOrderStatusFact:
        return False
    status = evidence.status
    if not all(_time(value) for value in (evidence.collection_started_at, evidence.collection_completed_at, evidence.open_order_received_at, status.received_at)):
        return False
    if not (
        evidence.collection_started_at <= evidence.open_order_received_at <= evidence.collection_completed_at
        and evidence.collection_started_at <= status.received_at <= evidence.collection_completed_at
        and evidence.collection_completed_at - evidence.collection_started_at <= timedelta(seconds=5)
        and max(evidence.open_order_received_at, status.received_at) == order.received_at
        and order.received_at <= evidence.collection_completed_at
    ):
        return False
    if not all((
        evidence.source == "open", evidence.open_order_status == "PreSubmitted", status.status == "PreSubmitted",
        evidence.security_type == "STK", evidence.currency == "USD",
        evidence.action == "SELL", evidence.order_type == "STP", evidence.time_in_force == "GTC",
        order.side is BrokerSide.SELL, order.order_type is EquityOrderType.STOP_MARKET,
        order.market_hours is MarketHours.REGULAR, order.time_in_force is TimeInForce.GTC,
        evidence.broker_order_id == order.broker_order_id,
        evidence.account_masked == order.account_masked, evidence.symbol == order.symbol,
        _int(evidence.contract_id, positive=True), evidence.contract_id == order.broker_contract_id,
        _int(evidence.perm_id, positive=True), evidence.perm_id == status.perm_id == order.broker_perm_id,
        _int(status.perm_id, positive=True), _int(status.client_id), _int(status.order_id, positive=True),
        _int(evidence.client_id), evidence.client_id == status.client_id,
        _int(evidence.order_id, positive=True), evidence.order_id == status.order_id,
        type(evidence.parent_id) is int, evidence.parent_id == 0,
        type(status.parent_id) is int, status.parent_id == 0,
        evidence.broker_order_id == f"ibkr:{evidence.client_id}:{evidence.order_id}",
        evidence.transmit is True, evidence.blocking_warning_present is False,
        status.why_held_present is False,
        _whole(evidence.requested_quantity, positive=True),
        _whole(status.filled), _whole(status.remaining, positive=True),
        evidence.requested_quantity == order.requested_quantity,
        status.filled == order.cumulative_filled_quantity,
        status.remaining == order.requested_quantity - order.cumulative_filled_quantity,
        type(evidence.stop_price) is Decimal, evidence.stop_price == order.stop_price,
    )):
        return False
    return all(getattr(evidence, name) is False for name in (
        "outside_rth", "include_overnight", "not_held", "oca_group_present",
        "conditions_present", "algo_present", "hedge_present",
        "good_after_time_present", "good_till_date_present",
    ))
