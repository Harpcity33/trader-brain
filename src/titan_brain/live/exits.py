"""Oversell-safe exit capacity, cancellation, and managed-closeout decisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Sequence

from .broker.base import (
    BrokerOperationResult,
    BrokerSide,
    EquityOrderType,
    OperationStatus,
    OrderSnapshot,
    PositionSnapshot,
)
from .models import BrokerOrderState
from .money import whole_shares


class ExitError(RuntimeError):
    """Base exit lifecycle error."""


class ExitCapacityError(ExitError):
    """A proposed exit could overlap broker-held/pending sell exposure."""


class ExitAction(str, Enum):
    FLAT = "FLAT"
    CANCEL_ENTRY_ORDERS = "CANCEL_ENTRY_ORDERS"
    CANCEL_EXIT_ORDERS = "CANCEL_EXIT_ORDERS"
    WAIT_CANCEL_CONFIRMATION = "WAIT_CANCEL_CONFIRMATION"
    WAIT_EXISTING_EXIT = "WAIT_EXISTING_EXIT"
    SUBMIT_SAFE_CLOSE = "SUBMIT_SAFE_CLOSE"
    RECONCILE = "RECONCILE"
    BLOCKED = "BLOCKED"


class CancelAction(str, Enum):
    WAIT_CONFIRMATION = "WAIT_CONFIRMATION"
    CANCEL_CONFIRMED_RECALCULATE = "CANCEL_CONFIRMED_RECALCULATE"
    FILL_RACE_RECALCULATE = "FILL_RACE_RECALCULATE"
    CANCEL_REJECTED = "CANCEL_REJECTED"
    UNKNOWN_RECONCILE = "UNKNOWN_RECONCILE"
    CONFLICT_RECONCILE = "CONFLICT_RECONCILE"


@dataclass(frozen=True)
class ExitCapacity:
    symbol: str
    position_quantity: int
    sellable_quantity: int
    broker_held_quantity: int
    working_sell_quantity: int
    pending_sell_quantity: int
    active_sell_quantity: int
    available_to_submit: int
    active_sell_order_ids: tuple[str, ...]
    unknown_sell_order_ids: tuple[str, ...]
    pending_cancel_order_ids: tuple[str, ...]
    overcommitted: bool


@dataclass(frozen=True)
class SafeCloseDecision:
    symbol: str
    action: ExitAction
    quantity: int
    cancel_order_ids: tuple[str, ...]
    pause_new_entries: bool
    requires_newer_snapshot: bool
    reasons: tuple[str, ...]
    capacity: ExitCapacity


@dataclass(frozen=True)
class CancelReconciliation:
    broker_order_id: str
    action: CancelAction
    terminal: bool
    filled_quantity_delta: int
    resulting_state: BrokerOrderState | None
    requires_position_refresh: bool
    reason: str


_WORKING_STATES = frozenset(
    {BrokerOrderState.CONFIRMED, BrokerOrderState.PARTIALLY_FILLED}
)
_UNCERTAIN_STATES = frozenset(
    {
        BrokerOrderState.UNKNOWN,
        BrokerOrderState.PENDING,
        BrokerOrderState.QUEUED,
        BrokerOrderState.UNCONFIRMED,
        BrokerOrderState.LOCATING,
        BrokerOrderState.PENDING_CANCELLED,
    }
)


def _remaining(order: OrderSnapshot) -> int:
    if order.state.terminal:
        return 0
    requested = whole_shares(order.requested_quantity, field="requested_quantity")
    cumulative = whole_shares(
        order.cumulative_filled_quantity,
        field="cumulative_filled_quantity",
        allow_zero=True,
    )
    return requested - cumulative


def calculate_exit_capacity(
    *, position: PositionSnapshot | None, orders: Sequence[OrderSnapshot], symbol: str
) -> ExitCapacity:
    """Calculate safe incremental sell capacity from one broker snapshot.

    Every nonterminal or unknown sell reserves its entire unfilled quantity.
    ``PENDING_CANCELLED`` remains reserved until a newer terminal order state is
    observed.  This is what prevents a replacement/market close from
    overlapping an order whose cancel request merely reached the broker.
    """

    normalized_symbol = str(symbol).strip().upper()
    if not normalized_symbol:
        raise ValueError("symbol is required")
    if position is not None and position.symbol != normalized_symbol:
        raise ExitCapacityError("position symbol differs from requested symbol")
    position_quantity = (
        0
        if position is None
        else whole_shares(position.quantity, field="position quantity", allow_zero=True)
    )
    sellable = (
        0
        if position is None
        else whole_shares(
            position.sellable_quantity, field="sellable quantity", allow_zero=True
        )
    )
    held = (
        0
        if position is None
        else whole_shares(
            position.held_for_sells, field="held-for-sells quantity", allow_zero=True
        )
    )
    active = tuple(
        order
        for order in orders
        if order.symbol == normalized_symbol
        and order.side is BrokerSide.SELL
        and not order.state.terminal
    )
    working = sum(
        _remaining(order) for order in active if order.state in _WORKING_STATES
    )
    pending = sum(
        _remaining(order) for order in active if order.state not in _WORKING_STATES
    )
    active_quantity = working + pending
    unknown = tuple(
        order.broker_order_id
        for order in active
        if order.state in _UNCERTAIN_STATES
    )
    pending_cancel = tuple(
        order.broker_order_id
        for order in active
        if order.state is BrokerOrderState.PENDING_CANCELLED
    )
    # A new order must independently fit currently sellable quantity and must
    # leave aggregate broker sell claims no larger than the actual position.
    available = max(min(sellable, position_quantity - active_quantity), 0)
    return ExitCapacity(
        symbol=normalized_symbol,
        position_quantity=position_quantity,
        sellable_quantity=sellable,
        broker_held_quantity=held,
        working_sell_quantity=working,
        pending_sell_quantity=pending,
        active_sell_quantity=active_quantity,
        available_to_submit=available,
        active_sell_order_ids=tuple(order.broker_order_id for order in active),
        unknown_sell_order_ids=unknown,
        pending_cancel_order_ids=pending_cancel,
        overcommitted=active_quantity > position_quantity,
    )


def require_exit_capacity(capacity: ExitCapacity, proposed_quantity: int) -> int:
    """Validate a whole-share exit before a durable intent may be created."""

    quantity = whole_shares(proposed_quantity, field="proposed exit quantity")
    if capacity.overcommitted:
        raise ExitCapacityError("existing active sells already exceed position quantity")
    if capacity.unknown_sell_order_ids:
        raise ExitCapacityError("unknown or nonterminal sell state must reconcile first")
    if quantity > capacity.sellable_quantity:
        raise ExitCapacityError("proposed exit exceeds broker sellable quantity")
    if capacity.active_sell_quantity + quantity > capacity.position_quantity:
        raise ExitCapacityError("aggregate pending/working exits would exceed position")
    if quantity > capacity.available_to_submit:
        raise ExitCapacityError("proposed exit overlaps broker-held sell quantity")
    return quantity


def plan_safe_close(
    *,
    position: PositionSnapshot | None,
    orders: Sequence[OrderSnapshot],
    symbol: str,
    snapshot_received_at: datetime,
    evidence_floor_at: datetime | None = None,
) -> SafeCloseDecision:
    """Choose the next non-overlapping step of a managed closeout.

    The result is a decision, not an order mutation.  After any cancel request,
    callers must obtain a terminal broker state and a refreshed position before
    asking for another close decision.
    """

    if snapshot_received_at.tzinfo is None:
        raise ValueError("snapshot_received_at must be timezone-aware")
    received = snapshot_received_at.astimezone(timezone.utc)
    if evidence_floor_at is not None:
        if evidence_floor_at.tzinfo is None:
            raise ValueError("evidence_floor_at must be timezone-aware")
        if received <= evidence_floor_at.astimezone(timezone.utc):
            capacity = calculate_exit_capacity(
                position=position, orders=orders, symbol=symbol
            )
            return SafeCloseDecision(
                symbol=capacity.symbol,
                action=ExitAction.RECONCILE,
                quantity=0,
                cancel_order_ids=(),
                pause_new_entries=True,
                requires_newer_snapshot=True,
                reasons=("closeout requires strictly newer broker evidence",),
                capacity=capacity,
            )

    capacity = calculate_exit_capacity(position=position, orders=orders, symbol=symbol)
    active_buys = tuple(
        order
        for order in orders
        if order.symbol == capacity.symbol
        and order.side is BrokerSide.BUY
        and not order.state.terminal
    )
    buy_pending_cancel = tuple(
        order.broker_order_id
        for order in active_buys
        if order.state is BrokerOrderState.PENDING_CANCELLED
    )
    if any(order.state is BrokerOrderState.UNKNOWN for order in active_buys):
        return SafeCloseDecision(
            symbol=capacity.symbol,
            action=ExitAction.RECONCILE,
            quantity=0,
            cancel_order_ids=(),
            pause_new_entries=True,
            requires_newer_snapshot=True,
            reasons=("unknown entry order may still increase exposure",),
            capacity=capacity,
        )
    if buy_pending_cancel:
        return SafeCloseDecision(
            symbol=capacity.symbol,
            action=ExitAction.WAIT_CANCEL_CONFIRMATION,
            quantity=0,
            cancel_order_ids=buy_pending_cancel,
            pause_new_entries=True,
            requires_newer_snapshot=True,
            reasons=("entry cancel acceptance is nonterminal",),
            capacity=capacity,
        )
    if active_buys:
        return SafeCloseDecision(
            symbol=capacity.symbol,
            action=ExitAction.CANCEL_ENTRY_ORDERS,
            quantity=0,
            cancel_order_ids=tuple(order.broker_order_id for order in active_buys),
            pause_new_entries=True,
            requires_newer_snapshot=True,
            reasons=("cancel remaining entry exposure before calculating close quantity",),
            capacity=capacity,
        )

    if capacity.overcommitted:
        return SafeCloseDecision(
            symbol=capacity.symbol,
            action=ExitAction.BLOCKED,
            quantity=0,
            cancel_order_ids=(),
            pause_new_entries=True,
            requires_newer_snapshot=True,
            reasons=("active sell claims exceed the broker position",),
            capacity=capacity,
        )
    if capacity.pending_cancel_order_ids:
        return SafeCloseDecision(
            symbol=capacity.symbol,
            action=ExitAction.WAIT_CANCEL_CONFIRMATION,
            quantity=0,
            cancel_order_ids=capacity.pending_cancel_order_ids,
            pause_new_entries=True,
            requires_newer_snapshot=True,
            reasons=("sell cancel request is not a confirmed cancellation",),
            capacity=capacity,
        )
    if capacity.unknown_sell_order_ids:
        return SafeCloseDecision(
            symbol=capacity.symbol,
            action=ExitAction.RECONCILE,
            quantity=0,
            cancel_order_ids=(),
            pause_new_entries=True,
            requires_newer_snapshot=True,
            reasons=("pending/unknown sell order may still execute",),
            capacity=capacity,
        )

    if capacity.position_quantity == 0:
        if capacity.active_sell_order_ids:
            return SafeCloseDecision(
                symbol=capacity.symbol,
                action=ExitAction.CANCEL_EXIT_ORDERS,
                quantity=0,
                cancel_order_ids=capacity.active_sell_order_ids,
                pause_new_entries=True,
                requires_newer_snapshot=True,
                reasons=("cancel dangling sell orders before declaring flat",),
                capacity=capacity,
            )
        return SafeCloseDecision(
            symbol=capacity.symbol,
            action=ExitAction.FLAT,
            quantity=0,
            cancel_order_ids=(),
            pause_new_entries=False,
            requires_newer_snapshot=False,
            reasons=("broker position and active sell exposure are zero",),
            capacity=capacity,
        )

    if capacity.active_sell_order_ids:
        active_sells = tuple(
            order
            for order in orders
            if order.broker_order_id in capacity.active_sell_order_ids
        )
        if (
            capacity.active_sell_quantity >= capacity.position_quantity
            and all(order.order_type is EquityOrderType.MARKET for order in active_sells)
        ):
            return SafeCloseDecision(
                symbol=capacity.symbol,
                action=ExitAction.WAIT_EXISTING_EXIT,
                quantity=0,
                cancel_order_ids=(),
                pause_new_entries=True,
                requires_newer_snapshot=True,
                reasons=("existing broker-confirmed market exit covers the position",),
                capacity=capacity,
            )
        return SafeCloseDecision(
            symbol=capacity.symbol,
            action=ExitAction.CANCEL_EXIT_ORDERS,
            quantity=0,
            cancel_order_ids=capacity.active_sell_order_ids,
            pause_new_entries=True,
            requires_newer_snapshot=True,
            reasons=(
                "cancel non-marketable protection/targets and reconcile before close",
            ),
            capacity=capacity,
        )

    if capacity.available_to_submit != capacity.position_quantity:
        return SafeCloseDecision(
            symbol=capacity.symbol,
            action=ExitAction.RECONCILE,
            quantity=0,
            cancel_order_ids=(),
            pause_new_entries=True,
            requires_newer_snapshot=True,
            reasons=("full position is not currently broker-sellable",),
            capacity=capacity,
        )
    quantity = require_exit_capacity(capacity, capacity.position_quantity)
    return SafeCloseDecision(
        symbol=capacity.symbol,
        action=ExitAction.SUBMIT_SAFE_CLOSE,
        quantity=quantity,
        cancel_order_ids=(),
        pause_new_entries=True,
        requires_newer_snapshot=False,
        reasons=("no active sell overlaps and full position is sellable",),
        capacity=capacity,
    )


def reconcile_cancel_result(
    *, before: OrderSnapshot, result: BrokerOperationResult
) -> CancelReconciliation:
    """Interpret a cancel response without treating acceptance as completion."""

    after = result.order
    if after is not None:
        if after.broker_order_id != before.broker_order_id:
            return CancelReconciliation(
                broker_order_id=before.broker_order_id,
                action=CancelAction.CONFLICT_RECONCILE,
                terminal=False,
                filled_quantity_delta=0,
                resulting_state=after.state,
                requires_position_refresh=True,
                reason="cancel response references a different broker order",
            )
        before_filled = whole_shares(
            before.cumulative_filled_quantity,
            field="before cumulative fill",
            allow_zero=True,
        )
        after_filled = whole_shares(
            after.cumulative_filled_quantity,
            field="after cumulative fill",
            allow_zero=True,
        )
        if after_filled < before_filled:
            return CancelReconciliation(
                broker_order_id=before.broker_order_id,
                action=CancelAction.CONFLICT_RECONCILE,
                terminal=False,
                filled_quantity_delta=0,
                resulting_state=after.state,
                requires_position_refresh=True,
                reason="cancel evidence decreased cumulative filled quantity",
            )
        delta = after_filled - before_filled
        if delta or after.state is BrokerOrderState.FILLED:
            return CancelReconciliation(
                broker_order_id=before.broker_order_id,
                action=CancelAction.FILL_RACE_RECALCULATE,
                terminal=after.state.terminal,
                filled_quantity_delta=delta,
                resulting_state=after.state,
                requires_position_refresh=True,
                reason="a fill won or overlapped the cancellation request",
            )
        if after.state in {
            BrokerOrderState.CANCELLED,
            BrokerOrderState.PARTIALLY_FILLED_REST_CANCELLED,
        }:
            return CancelReconciliation(
                broker_order_id=before.broker_order_id,
                action=CancelAction.CANCEL_CONFIRMED_RECALCULATE,
                terminal=True,
                filled_quantity_delta=0,
                resulting_state=after.state,
                requires_position_refresh=True,
                reason="broker order is terminal; position must be refreshed",
            )
        if after.state is BrokerOrderState.PENDING_CANCELLED:
            return CancelReconciliation(
                broker_order_id=before.broker_order_id,
                action=CancelAction.WAIT_CONFIRMATION,
                terminal=False,
                filled_quantity_delta=0,
                resulting_state=after.state,
                requires_position_refresh=True,
                reason="cancel request was accepted but remains nonterminal",
            )

    if result.status is OperationStatus.UNKNOWN:
        return CancelReconciliation(
            broker_order_id=before.broker_order_id,
            action=CancelAction.UNKNOWN_RECONCILE,
            terminal=False,
            filled_quantity_delta=0,
            resulting_state=after.state if after else None,
            requires_position_refresh=True,
            reason="cancel outcome is unknown; the order may still execute",
        )
    if result.status is OperationStatus.REJECTED or result.accepted is False:
        return CancelReconciliation(
            broker_order_id=before.broker_order_id,
            action=CancelAction.CANCEL_REJECTED,
            terminal=False,
            filled_quantity_delta=0,
            resulting_state=after.state if after else None,
            requires_position_refresh=True,
            reason="broker did not accept the cancellation",
        )
    # ACKNOWLEDGED, PENDING_CANCEL, or a bare CANCELLED result without a
    # terminal OrderSnapshot all require a subsequent authoritative read.
    return CancelReconciliation(
        broker_order_id=before.broker_order_id,
        action=CancelAction.WAIT_CONFIRMATION,
        terminal=False,
        filled_quantity_delta=0,
        resulting_state=after.state if after else None,
        requires_position_refresh=True,
        reason="cancel acknowledgement is not terminal broker evidence",
    )


__all__ = [
    "CancelAction",
    "CancelReconciliation",
    "ExitAction",
    "ExitCapacity",
    "ExitCapacityError",
    "ExitError",
    "SafeCloseDecision",
    "calculate_exit_capacity",
    "plan_safe_close",
    "reconcile_cancel_result",
    "require_exit_capacity",
]
