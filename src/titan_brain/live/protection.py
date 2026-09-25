"""Per-fill protection obligations and broker-authoritative coverage checks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
import hashlib
import json
from typing import Sequence

from .broker.base import (
    BrokerSide,
    EquityOrderType,
    MarketHours,
    OrderSnapshot,
    PositionSnapshot,
    TimeInForce,
)
from .models import (
    BrokerOrderState,
    IntentKind,
    ProtectionObligation,
    ProtectionState,
)
from .money import whole_shares
from .reconcile import IngestedOrder, ingest_local_order
from .state import LiveStateStore


class ProtectionError(RuntimeError):
    """Base protection lifecycle error."""


class ProtectionInvariantError(ProtectionError):
    """Raised when protection cannot be tied to authorized entry facts."""


class ProtectionAction(str, Enum):
    NONE = "NONE"
    PAUSE_NEW_ENTRIES = "PAUSE_NEW_ENTRIES"
    VERIFY_PENDING_PROTECTION = "VERIFY_PENDING_PROTECTION"
    CREATE_PROTECTION_INTENT = "CREATE_PROTECTION_INTENT"
    RECONCILE_OBLIGATIONS = "RECONCILE_OBLIGATIONS"
    RECONCILE_CANCEL = "RECONCILE_CANCEL"
    RECONCILE_EXIT_CAPACITY = "RECONCILE_EXIT_CAPACITY"
    SAFE_CLOSE = "SAFE_CLOSE"


@dataclass(frozen=True)
class FillProtectionResult:
    order: IngestedOrder
    new_obligation_ids: tuple[str, ...]
    existing_obligation_ids: tuple[str, ...]


@dataclass(frozen=True)
class ProtectionDecision:
    symbol: str
    position_quantity: int
    obligated_quantity: int
    working_quantity: int
    pending_quantity: int
    uncovered_quantity: int
    active_sell_quantity: int
    failed_order_ids: tuple[str, ...]
    pending_cancel_order_ids: tuple[str, ...]
    actions: tuple[ProtectionAction, ...]
    reasons: tuple[str, ...]

    @property
    def protected(self) -> bool:
        return (
            (self.position_quantity == 0 and self.active_sell_quantity == 0)
            or (
                self.uncovered_quantity == 0
                and self.obligated_quantity >= self.position_quantity
                and self.active_sell_quantity <= self.position_quantity
            )
        )

    @property
    def pause_new_entries(self) -> bool:
        return ProtectionAction.PAUSE_NEW_ENTRIES in self.actions

    @property
    def safe_close_required(self) -> bool:
        return ProtectionAction.SAFE_CLOSE in self.actions


_VERIFIED_WORKING = frozenset(
    {BrokerOrderState.CONFIRMED, BrokerOrderState.PARTIALLY_FILLED}
)
_PENDING_PROTECTION = frozenset(
    {
        BrokerOrderState.UNKNOWN,
        BrokerOrderState.PENDING,
        BrokerOrderState.QUEUED,
        BrokerOrderState.UNCONFIRMED,
        BrokerOrderState.LOCATING,
    }
)
_FAILED_PROTECTION = frozenset(
    {
        BrokerOrderState.REJECTED,
        BrokerOrderState.FAILED,
        BrokerOrderState.VOIDED,
        BrokerOrderState.CANCELLED,
        BrokerOrderState.PARTIALLY_FILLED_REST_CANCELLED,
        BrokerOrderState.LOCATE_FAILED,
    }
)


def _obligation_id(account_key: str, fill_id: str) -> str:
    digest = hashlib.sha256(
        f"{account_key}\n{fill_id}\nprotection-v1".encode("utf-8")
    ).hexdigest()
    return f"protect-{digest}"


def _db_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ProtectionInvariantError("durable timestamp is not timezone-aware")
    return parsed


def _remaining(order: OrderSnapshot) -> int:
    if order.state.terminal:
        return 0
    requested = whole_shares(order.requested_quantity, field="requested_quantity")
    filled = whole_shares(
        order.cumulative_filled_quantity,
        field="cumulative_filled_quantity",
        allow_zero=True,
    )
    return requested - filled


def is_verified_working_protection(order: OrderSnapshot) -> bool:
    """Return true only for a broker-confirmed regular-hours GTC stop-market.

    PENDING/QUEUED/UNCONFIRMED submissions intentionally return false.  An API
    submission acknowledgement is not evidence that protection is working.
    """

    return all(
        (
            order.side is BrokerSide.SELL,
            order.order_type is EquityOrderType.STOP_MARKET,
            order.market_hours is MarketHours.REGULAR,
            order.time_in_force is TimeInForce.GTC,
            order.state in _VERIFIED_WORKING,
            _remaining(order) > 0,
        )
    )


def ensure_entry_fill_obligations(
    store: LiveStateStore,
    *,
    order: OrderSnapshot,
    intent_id: str,
    account_key: str,
) -> FillProtectionResult:
    """Persist exactly one protection obligation per confirmed entry fill.

    The broker order and fills are ingested first.  Replays are idempotent;
    duplicate fill IDs with different facts fail in the state store.
    """

    intent = store.row("order_intents", "intent_id", intent_id)
    if intent is None:
        raise ProtectionInvariantError("entry fill has no durable intent")
    if IntentKind(intent["kind"]) is not IntentKind.ENTRY:
        raise ProtectionInvariantError("only ENTRY intents create protection obligations")
    if order.side is not BrokerSide.BUY:
        raise ProtectionInvariantError("only confirmed buy fills create long protection")

    ingested = ingest_local_order(
        store,
        order=order,
        intent_id=intent_id,
        account_key=account_key,
    )
    return _ensure_durable_entry_fill_obligations(
        store,
        intent_id=intent_id,
        account_key=account_key,
        ingested=ingested,
        expected_symbol=order.symbol,
    )


def ensure_durable_entry_fill_obligations(
    store: LiveStateStore,
    *,
    intent_id: str,
    account_key: str,
) -> FillProtectionResult:
    """Create obligations only from fills already accepted into durable state.

    This is the service/restart-safe entry point.  It never reads a raw broker
    envelope and therefore cannot bypass the authoritative snapshot quarantine.
    A sweep is intentionally idempotent so a crash after fill persistence but
    before obligation creation is repaired on the next ingestible snapshot.
    """

    intent = store.row("order_intents", "intent_id", intent_id)
    if intent is None or intent["account_key"] != account_key:
        raise ProtectionInvariantError("entry fill has no matching durable intent")
    if IntentKind(intent["kind"]) is not IntentKind.ENTRY:
        raise ProtectionInvariantError("only ENTRY intents create protection obligations")
    order_tuple = json.loads(str(intent["order_tuple_json"]))
    if str(order_tuple.get("side", "")).lower() != BrokerSide.BUY.value:
        raise ProtectionInvariantError("durable entry intent is not a buy")
    orders = store.rows(
        "SELECT broker_order_id FROM broker_orders WHERE intent_id=?",
        (intent_id,),
    )
    if len(orders) != 1:
        raise ProtectionInvariantError("entry intent does not resolve to one broker order")
    fill_rows = store.rows(
        "SELECT fill_id FROM fills WHERE broker_order_id=? ORDER BY received_at,fill_id",
        (orders[0]["broker_order_id"],),
    )
    ingested = IngestedOrder(
        broker_order_id=str(orders[0]["broker_order_id"]),
        intent_id=intent_id,
        order_revision_recorded=False,
        new_fill_ids=(),
        duplicate_fill_ids=tuple(str(row["fill_id"]) for row in fill_rows),
    )
    return _ensure_durable_entry_fill_obligations(
        store,
        intent_id=intent_id,
        account_key=account_key,
        ingested=ingested,
        expected_symbol=str(order_tuple.get("symbol", "")).upper(),
    )


def _ensure_durable_entry_fill_obligations(
    store: LiveStateStore,
    *,
    intent_id: str,
    account_key: str,
    ingested: IngestedOrder,
    expected_symbol: str,
) -> FillProtectionResult:
    plan_rows = store.rows(
        "SELECT p.symbol, p.structural_stop, i.account_key, i.kind, i.order_tuple_json "
        "FROM plans p JOIN order_intents i ON i.plan_id = p.plan_id "
        "WHERE i.intent_id = ?",
        (intent_id,),
    )
    if len(plan_rows) != 1:
        raise ProtectionInvariantError("entry intent does not resolve to one plan")
    plan = plan_rows[0]
    if plan["account_key"] != account_key or IntentKind(plan["kind"]) is not IntentKind.ENTRY:
        raise ProtectionInvariantError("entry plan ownership differs from durable intent")
    order_tuple = json.loads(str(plan["order_tuple_json"]))
    durable_symbol = str(order_tuple.get("symbol", "")).upper()
    if (
        not expected_symbol
        or plan["symbol"] != expected_symbol
        or durable_symbol != expected_symbol
        or str(order_tuple.get("side", "")).lower() != BrokerSide.BUY.value
    ):
        raise ProtectionInvariantError("durable fill symbol or side differs from entry plan")

    fill_rows = store.rows(
        "SELECT f.* FROM fills f JOIN broker_orders o "
        "ON o.broker_order_id=f.broker_order_id "
        "WHERE o.intent_id=? ORDER BY f.received_at,f.fill_id",
        (intent_id,),
    )

    new_ids: list[str] = []
    existing_ids: list[str] = []
    for fill in fill_rows:
        obligation_id = _obligation_id(account_key, str(fill["fill_id"]))
        obligation = ProtectionObligation(
            obligation_id=obligation_id,
            source_fill_id=str(fill["fill_id"]),
            account_key=account_key,
            symbol=expected_symbol,
            required_quantity=int(fill["quantity"]),
            working_quantity=0,
            stop_price=Decimal(plan["structural_stop"]),
            state=ProtectionState.REQUIRED,
            revision=0,
            # Use the durable first-seen time so a later envelope replay is
            # byte-for-byte idempotent at revision zero.
            updated_at=_db_time(fill["received_at"]),
        )
        if store.record_protection_obligation(obligation):
            new_ids.append(obligation_id)
        else:
            existing_ids.append(obligation_id)

    return FillProtectionResult(
        order=ingested,
        new_obligation_ids=tuple(new_ids),
        existing_obligation_ids=tuple(existing_ids),
    )


def load_open_obligations(
    store: LiveStateStore, *, account_key: str, symbol: str
) -> tuple[ProtectionObligation, ...]:
    rows = store.rows(
        "SELECT * FROM protection_obligations "
        "WHERE account_key = ? AND symbol = ? AND state NOT IN (?, ?) "
        "ORDER BY updated_at, obligation_id",
        (
            account_key,
            symbol.upper(),
            ProtectionState.SATISFIED.value,
            ProtectionState.CANCELLED.value,
        ),
    )
    return tuple(
        ProtectionObligation(
            obligation_id=row["obligation_id"],
            source_fill_id=row["source_fill_id"],
            account_key=row["account_key"],
            symbol=row["symbol"],
            required_quantity=int(row["required_quantity"]),
            working_quantity=int(row["working_quantity"]),
            stop_price=Decimal(row["stop_price"]),
            state=ProtectionState(row["state"]),
            revision=int(row["revision"]),
            updated_at=_db_time(row["updated_at"]),
            broker_order_id=row["broker_order_id"],
        )
        for row in rows
    )


def assess_protection(
    *,
    position: PositionSnapshot | None,
    orders: Sequence[OrderSnapshot],
    obligations: Sequence[ProtectionObligation],
    symbol: str | None = None,
) -> ProtectionDecision:
    """Assess coverage from current broker evidence, never local submission state."""

    resolved_symbol = (symbol or (position.symbol if position else "")).strip().upper()
    if not resolved_symbol:
        raise ProtectionInvariantError("symbol is required")
    if position is not None and position.symbol != resolved_symbol:
        raise ProtectionInvariantError("position symbol differs from assessment symbol")

    position_quantity = (
        0
        if position is None
        else whole_shares(position.quantity, field="position quantity", allow_zero=True)
    )
    relevant_obligations = tuple(
        obligation
        for obligation in obligations
        if obligation.symbol == resolved_symbol
        and obligation.state not in {ProtectionState.SATISFIED, ProtectionState.CANCELLED}
    )
    obligated = sum(item.required_quantity for item in relevant_obligations)
    ready_to_submit = sum(
        item.required_quantity
        for item in relevant_obligations
        if item.state is ProtectionState.REQUIRED
    )
    strictest_stop = max(
        (item.stop_price for item in relevant_obligations), default=None
    )

    symbol_orders = tuple(order for order in orders if order.symbol == resolved_symbol)
    active_sells = tuple(
        order
        for order in symbol_orders
        if order.side is BrokerSide.SELL and not order.state.terminal
    )
    active_sell_quantity = sum(_remaining(order) for order in active_sells)
    working = sum(
        _remaining(order)
        for order in active_sells
        if is_verified_working_protection(order)
        and strictest_stop is not None
        and order.stop_price is not None
        and order.stop_price >= strictest_stop
    )
    pending_orders = tuple(
        order
        for order in active_sells
        if order.order_type is EquityOrderType.STOP_MARKET
        and order.state in _PENDING_PROTECTION
    )
    pending = sum(_remaining(order) for order in pending_orders)
    pending_cancel = tuple(
        order.broker_order_id
        for order in active_sells
        if order.state is BrokerOrderState.PENDING_CANCELLED
    )
    failed = tuple(
        order.broker_order_id
        for order in symbol_orders
        if order.side is BrokerSide.SELL
        and order.order_type is EquityOrderType.STOP_MARKET
        and order.state in _FAILED_PROTECTION
    )
    widened = tuple(
        order.broker_order_id
        for order in active_sells
        if is_verified_working_protection(order)
        and strictest_stop is not None
        and order.stop_price is not None
        and order.stop_price < strictest_stop
    )
    failed = tuple(dict.fromkeys(failed + widened))
    uncovered = max(position_quantity - working, 0)

    actions: list[ProtectionAction] = []
    reasons: list[str] = []
    if active_sell_quantity > position_quantity:
        actions.extend(
            (
                ProtectionAction.PAUSE_NEW_ENTRIES,
                ProtectionAction.RECONCILE_EXIT_CAPACITY,
            )
        )
        reasons.append("aggregate active sell quantity exceeds broker position")
    if obligated < position_quantity:
        actions.extend(
            (
                ProtectionAction.PAUSE_NEW_ENTRIES,
                ProtectionAction.RECONCILE_OBLIGATIONS,
            )
        )
        reasons.append("position quantity lacks per-fill durable obligations")

    if uncovered:
        actions.append(ProtectionAction.PAUSE_NEW_ENTRIES)
        if pending_cancel:
            actions.extend(
                (ProtectionAction.RECONCILE_CANCEL, ProtectionAction.SAFE_CLOSE)
            )
            reasons.append("protection cancellation is nonterminal; close must wait")
        elif pending:
            actions.append(ProtectionAction.VERIFY_PENDING_PROTECTION)
            reasons.append("submitted protection has not reached a working broker state")
        elif ready_to_submit and not failed:
            actions.append(ProtectionAction.CREATE_PROTECTION_INTENT)
            reasons.append("confirmed entry fill requires immediate stop protection")
        else:
            # Rejection, disappearance, or no protective order at all requires
            # immediate risk reduction within the coordinator's authority.
            actions.append(ProtectionAction.SAFE_CLOSE)
            if failed:
                reasons.append(
                    "protective order failed, was cancelled, or widened below structural stop"
                )
            else:
                reasons.append("previously submitted working protection is missing")
    elif not actions:
        actions.append(ProtectionAction.NONE)

    return ProtectionDecision(
        symbol=resolved_symbol,
        position_quantity=position_quantity,
        obligated_quantity=obligated,
        working_quantity=working,
        pending_quantity=pending,
        uncovered_quantity=uncovered,
        active_sell_quantity=active_sell_quantity,
        failed_order_ids=failed,
        pending_cancel_order_ids=pending_cancel,
        actions=tuple(dict.fromkeys(actions)),
        reasons=tuple(reasons),
    )


__all__ = [
    "FillProtectionResult",
    "ProtectionAction",
    "ProtectionDecision",
    "ProtectionError",
    "ProtectionInvariantError",
    "assess_protection",
    "ensure_entry_fill_obligations",
    "ensure_durable_entry_fill_obligations",
    "is_verified_working_protection",
    "load_open_obligations",
]
