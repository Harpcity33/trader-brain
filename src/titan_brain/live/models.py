"""Typed records for the durable live-order lifecycle.

These records contain no transport methods.  They make unsafe or ambiguous
states explicit so the persistent coordinator can reconcile them before any
new risk is considered.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
import re
from typing import Any, Mapping
from uuid import UUID

from .money import finite_decimal, money, positive_decimal, whole_shares


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class EngineMode(str, Enum):
    PAUSED = "PAUSED"
    RECONCILING = "RECONCILING"
    ACTIVE = "ACTIVE"
    PAUSE_NEW_ENTRIES = "PAUSE_NEW_ENTRIES"
    MANAGED_CLOSEOUT = "MANAGED_CLOSEOUT"
    INCIDENT = "INCIDENT"
    STOPPED = "STOPPED"


class IntentKind(str, Enum):
    ENTRY = "ENTRY"
    PROTECTION = "PROTECTION"
    EXIT = "EXIT"
    CANCEL = "CANCEL"


class IntentState(str, Enum):
    PREPARED = "PREPARED"
    SUBMITTING = "SUBMITTING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    UNKNOWN = "UNKNOWN"
    RECONCILED = "RECONCILED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"

    @property
    def terminal(self) -> bool:
        return self in {
            IntentState.RECONCILED,
            IntentState.REJECTED,
            IntentState.CANCELLED,
            IntentState.FAILED,
        }


class BrokerOrderState(str, Enum):
    UNKNOWN = "UNKNOWN"
    PENDING = "PENDING"
    QUEUED = "QUEUED"
    UNCONFIRMED = "UNCONFIRMED"
    CONFIRMED = "CONFIRMED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    PENDING_CANCELLED = "PENDING_CANCELLED"
    CANCELLED = "CANCELLED"
    PARTIALLY_FILLED_REST_CANCELLED = "PARTIALLY_FILLED_REST_CANCELLED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    VOIDED = "VOIDED"
    LOCATING = "LOCATING"
    LOCATE_FAILED = "LOCATE_FAILED"

    @property
    def terminal(self) -> bool:
        return self in {
            BrokerOrderState.FILLED,
            BrokerOrderState.CANCELLED,
            BrokerOrderState.PARTIALLY_FILLED_REST_CANCELLED,
            BrokerOrderState.REJECTED,
            BrokerOrderState.FAILED,
            BrokerOrderState.VOIDED,
            BrokerOrderState.LOCATE_FAILED,
        }

    @property
    def working(self) -> bool:
        return self in {
            BrokerOrderState.PENDING,
            BrokerOrderState.QUEUED,
            BrokerOrderState.UNCONFIRMED,
            BrokerOrderState.CONFIRMED,
            BrokerOrderState.PARTIALLY_FILLED,
            BrokerOrderState.PENDING_CANCELLED,
            BrokerOrderState.LOCATING,
        }


class ProtectionState(str, Enum):
    REQUIRED = "REQUIRED"
    PENDING_SUBMIT = "PENDING_SUBMIT"
    SUBMITTED = "SUBMITTED"
    WORKING = "WORKING"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    SATISFIED = "SATISFIED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class PlanState(str, Enum):
    VALIDATED = "VALIDATED"
    CONSUMED = "CONSUMED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"


class ReservationState(str, Enum):
    RESERVED = "RESERVED"
    BOUND = "BOUND"
    RELEASED = "RELEASED"


class IncidentSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class OutboxState(str, Enum):
    PENDING = "PENDING"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"


def _required(value: Any, field_name: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def _sha256(value: Any, field_name: str) -> str:
    normalized = _required(value, field_name).lower()
    if not _SHA256.fullmatch(normalized):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return normalized


def _utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _strict_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be boolean")
    return value


def _enum(value: Any, enum_type: type[Enum], field_name: str) -> Enum:
    try:
        return value if isinstance(value, enum_type) else enum_type(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{field_name} is invalid") from exc


@dataclass(frozen=True)
class BrokerSnapshot:
    snapshot_id: str
    account_key: str
    evidence_revision: str
    observed_at: datetime
    received_at: datetime
    account_state: str
    equity: Decimal
    cash: Decimal
    unleveraged_buying_power: Decimal
    realized_pnl: Decimal
    equity_position_count: int
    equity_order_count: int
    equity_nonterminal_order_count: int
    external_material_order_count: int
    option_position_count: int
    option_order_count: int
    advanced_order_count: int
    reconciliation_blocker_count: int
    positions_reconciled: bool
    equity_orders_reconciled: bool
    option_positions_reconciled: bool
    option_orders_reconciled: bool
    positions_digest: str
    orders_digest: str
    advanced_orders_reconciled: bool = False
    realized_pnl_reconciled: bool = False

    def __post_init__(self) -> None:
        for name in ("snapshot_id", "account_key", "evidence_revision", "account_state"):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        observed = _utc(self.observed_at, "observed_at")
        received = _utc(self.received_at, "received_at")
        if received < observed:
            raise ValueError("received_at cannot precede observed_at")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "received_at", received)
        for name in ("equity", "cash", "unleveraged_buying_power", "realized_pnl"):
            object.__setattr__(self, name, money(getattr(self, name), field=name))
        if self.equity <= 0:
            raise ValueError("equity must be positive")
        for name in (
            "equity_position_count",
            "equity_order_count",
            "equity_nonterminal_order_count",
            "external_material_order_count",
            "option_position_count",
            "option_order_count",
            "advanced_order_count",
            "reconciliation_blocker_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        for name in (
            "positions_reconciled",
            "equity_orders_reconciled",
            "option_positions_reconciled",
            "option_orders_reconciled",
            "advanced_orders_reconciled",
            "realized_pnl_reconciled",
        ):
            _strict_bool(getattr(self, name), name)
        object.__setattr__(self, "positions_digest", _sha256(self.positions_digest, "positions_digest"))
        object.__setattr__(self, "orders_digest", _sha256(self.orders_digest, "orders_digest"))

    @property
    def fully_reconciled(self) -> bool:
        return all(
            (
                self.positions_reconciled,
                self.equity_orders_reconciled,
                self.option_positions_reconciled,
                self.option_orders_reconciled,
                self.advanced_orders_reconciled,
                self.realized_pnl_reconciled,
                self.reconciliation_blocker_count == 0,
            )
        )


@dataclass(frozen=True)
class PositionRecord:
    """Latest broker-authoritative position, including manual/fractional facts."""

    account_key: str
    symbol: str
    quantity: Decimal
    sellable_quantity: Decimal
    held_for_sells: Decimal
    average_price: Decimal | None
    source: str
    broker_updated_at: datetime
    received_at: datetime
    revision: int
    raw_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "account_key", _required(self.account_key, "account_key"))
        symbol = _required(self.symbol, "symbol").upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", symbol):
            raise ValueError("symbol is invalid")
        object.__setattr__(self, "symbol", symbol)
        for name in ("quantity", "sellable_quantity", "held_for_sells"):
            value = finite_decimal(getattr(self, name), field=name)
            if value < 0:
                raise ValueError(f"{name} must be nonnegative")
            object.__setattr__(self, name, value)
        if self.sellable_quantity + self.held_for_sells > self.quantity:
            raise ValueError("sellable plus held quantity exceeds position")
        if self.average_price is not None:
            object.__setattr__(
                self,
                "average_price",
                positive_decimal(self.average_price, field="average_price"),
            )
        object.__setattr__(self, "source", _required(self.source, "source"))
        updated = _utc(self.broker_updated_at, "broker_updated_at")
        received = _utc(self.received_at, "received_at")
        if received < updated:
            raise ValueError("received_at cannot precede broker_updated_at")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 0:
            raise ValueError("revision must be a nonnegative integer")
        object.__setattr__(self, "broker_updated_at", updated)
        object.__setattr__(self, "received_at", received)
        object.__setattr__(self, "raw_hash", _sha256(self.raw_hash, "raw_hash"))


@dataclass(frozen=True)
class ExpiringPlan:
    plan_id: str
    account_key: str
    strategy_id: str
    symbol: str
    setup_id: str
    quantity: int
    limit_price: Decimal
    structural_stop: Decimal
    market_hours: str
    time_in_force: str
    evidence_cutoff_at: datetime
    created_at: datetime
    expires_at: datetime
    policy_hash: str
    config_hash: str
    evidence_hash: str
    state: PlanState = PlanState.VALIDATED
    targets: tuple[Decimal, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        for name in ("plan_id", "account_key", "strategy_id", "setup_id"):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        symbol = _required(self.symbol, "symbol").upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", symbol):
            raise ValueError("symbol is invalid")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "quantity", whole_shares(self.quantity))
        limit_price = positive_decimal(self.limit_price, field="limit_price")
        stop = positive_decimal(self.structural_stop, field="structural_stop")
        if stop >= limit_price:
            raise ValueError("long-equity structural_stop must be below limit_price")
        object.__setattr__(self, "limit_price", limit_price)
        object.__setattr__(self, "structural_stop", stop)
        if self.market_hours not in {"regular_hours", "extended_hours"}:
            raise ValueError("unsupported market_hours")
        if self.time_in_force not in {"gfd", "gtc"}:
            raise ValueError("unsupported time_in_force")
        cutoff = _utc(self.evidence_cutoff_at, "evidence_cutoff_at")
        created = _utc(self.created_at, "created_at")
        expires = _utc(self.expires_at, "expires_at")
        if cutoff > created or expires <= created:
            raise ValueError("plan timestamps are not causally ordered")
        object.__setattr__(self, "evidence_cutoff_at", cutoff)
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "expires_at", expires)
        for name in ("policy_hash", "config_hash", "evidence_hash"):
            object.__setattr__(self, name, _sha256(getattr(self, name), name))
        object.__setattr__(self, "state", _enum(self.state, PlanState, "state"))
        object.__setattr__(
            self,
            "targets",
            tuple(positive_decimal(value, field="target") for value in self.targets),
        )


@dataclass(frozen=True)
class RiskReservation:
    reservation_id: str
    plan_id: str
    account_key: str
    planned_risk: Decimal
    stress_risk: Decimal
    execution_reserve: Decimal
    notional: Decimal
    created_at: datetime
    state: ReservationState = ReservationState.RESERVED

    def __post_init__(self) -> None:
        for name in ("reservation_id", "plan_id", "account_key"):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        for name in ("planned_risk", "stress_risk", "execution_reserve", "notional"):
            object.__setattr__(self, name, money(getattr(self, name), field=name))
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be nonnegative")
        if self.stress_risk < self.planned_risk:
            raise ValueError("stress_risk cannot be below planned_risk")
        if self.planned_risk <= 0:
            raise ValueError("planned_risk must be positive")
        if self.execution_reserve <= 0:
            raise ValueError("execution_reserve must be positive")
        if self.notional <= 0:
            raise ValueError("notional must be positive")
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))
        object.__setattr__(self, "state", _enum(self.state, ReservationState, "state"))


@dataclass(frozen=True)
class OrderIntent:
    intent_id: str
    plan_id: str
    reservation_id: str | None
    account_key: str
    kind: IntentKind
    client_ref: str
    order_tuple: Mapping[str, Any]
    tuple_hash: str
    created_at: datetime
    acknowledgement_deadline_at: datetime
    state: IntentState = IntentState.PREPARED

    def __post_init__(self) -> None:
        for name in ("intent_id", "plan_id", "account_key"):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        try:
            UUID(str(self.client_ref))
        except (ValueError, AttributeError) as exc:
            raise ValueError("client_ref must be a UUID") from exc
        object.__setattr__(self, "client_ref", str(self.client_ref).lower())
        object.__setattr__(self, "kind", _enum(self.kind, IntentKind, "kind"))
        object.__setattr__(self, "state", _enum(self.state, IntentState, "state"))
        if self.kind is IntentKind.ENTRY:
            object.__setattr__(
                self,
                "reservation_id",
                _required(self.reservation_id, "reservation_id"),
            )
        elif self.reservation_id is not None:
            raise ValueError("safety intents cannot consume incremental entry risk")
        if not isinstance(self.order_tuple, Mapping) or not self.order_tuple:
            raise ValueError("order_tuple must be a nonempty mapping")
        object.__setattr__(self, "order_tuple", dict(self.order_tuple))
        object.__setattr__(self, "tuple_hash", _sha256(self.tuple_hash, "tuple_hash"))
        created = _utc(self.created_at, "created_at")
        deadline = _utc(self.acknowledgement_deadline_at, "acknowledgement_deadline_at")
        if deadline <= created:
            raise ValueError("acknowledgement deadline must follow intent creation")
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "acknowledgement_deadline_at", deadline)


@dataclass(frozen=True)
class BrokerOrder:
    broker_order_id: str
    intent_id: str
    account_key: str
    state: BrokerOrderState
    quantity: int
    cumulative_filled_quantity: int
    revision: int
    broker_updated_at: datetime
    received_at: datetime
    raw_hash: str

    def __post_init__(self) -> None:
        for name in ("broker_order_id", "intent_id", "account_key"):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        object.__setattr__(self, "state", _enum(self.state, BrokerOrderState, "state"))
        quantity = whole_shares(self.quantity)
        cumulative = whole_shares(
            self.cumulative_filled_quantity,
            field="cumulative_filled_quantity",
            allow_zero=True,
        )
        if cumulative > quantity:
            raise ValueError("cumulative fill cannot exceed order quantity")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 0:
            raise ValueError("revision must be a nonnegative integer")
        updated = _utc(self.broker_updated_at, "broker_updated_at")
        received = _utc(self.received_at, "received_at")
        if received < updated:
            raise ValueError("received_at cannot precede broker_updated_at")
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "cumulative_filled_quantity", cumulative)
        object.__setattr__(self, "broker_updated_at", updated)
        object.__setattr__(self, "received_at", received)
        object.__setattr__(self, "raw_hash", _sha256(self.raw_hash, "raw_hash"))


@dataclass(frozen=True)
class Fill:
    fill_id: str
    broker_order_id: str
    account_key: str
    quantity: int
    price: Decimal
    executed_at: datetime
    received_at: datetime

    def __post_init__(self) -> None:
        for name in ("fill_id", "broker_order_id", "account_key"):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        object.__setattr__(self, "quantity", whole_shares(self.quantity))
        object.__setattr__(self, "price", positive_decimal(self.price, field="price"))
        executed = _utc(self.executed_at, "executed_at")
        received = _utc(self.received_at, "received_at")
        if received < executed:
            raise ValueError("received_at cannot precede executed_at")
        object.__setattr__(self, "executed_at", executed)
        object.__setattr__(self, "received_at", received)


@dataclass(frozen=True)
class ProtectionObligation:
    obligation_id: str
    source_fill_id: str
    account_key: str
    symbol: str
    required_quantity: int
    working_quantity: int
    stop_price: Decimal
    state: ProtectionState
    revision: int
    updated_at: datetime
    broker_order_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("obligation_id", "source_fill_id", "account_key", "symbol"):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        object.__setattr__(self, "state", _enum(self.state, ProtectionState, "state"))
        required = whole_shares(self.required_quantity, field="required_quantity")
        working = whole_shares(self.working_quantity, field="working_quantity", allow_zero=True)
        if working > required:
            raise ValueError("working protection cannot exceed required quantity")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 0:
            raise ValueError("revision must be a nonnegative integer")
        object.__setattr__(self, "required_quantity", required)
        object.__setattr__(self, "working_quantity", working)
        object.__setattr__(self, "stop_price", positive_decimal(self.stop_price, field="stop_price"))
        object.__setattr__(self, "updated_at", _utc(self.updated_at, "updated_at"))
        if self.broker_order_id is not None:
            object.__setattr__(self, "broker_order_id", _required(self.broker_order_id, "broker_order_id"))

    @property
    def uncovered_quantity(self) -> int:
        return self.required_quantity - self.working_quantity


@dataclass(frozen=True)
class SessionLatch:
    account_key: str
    trading_date: date
    loss_locked: bool
    objective_crossed: bool
    pause_new_entries: bool
    closeout_started: bool
    revision: int
    updated_at: datetime
    hard_kill: bool = False
    highest_realized_pnl: Decimal = Decimal("0.00")
    first_objective_crossed_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "account_key", _required(self.account_key, "account_key"))
        if not isinstance(self.trading_date, date):
            raise ValueError("trading_date must be a date")
        for name in (
            "loss_locked",
            "objective_crossed",
            "pause_new_entries",
            "closeout_started",
            "hard_kill",
        ):
            _strict_bool(getattr(self, name), name)
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 0:
            raise ValueError("revision must be a nonnegative integer")
        object.__setattr__(self, "updated_at", _utc(self.updated_at, "updated_at"))
        object.__setattr__(
            self,
            "highest_realized_pnl",
            money(self.highest_realized_pnl, field="highest_realized_pnl"),
        )
        if self.first_objective_crossed_at is not None:
            object.__setattr__(
                self,
                "first_objective_crossed_at",
                _utc(self.first_objective_crossed_at, "first_objective_crossed_at"),
            )
        if self.objective_crossed and self.first_objective_crossed_at is None:
            raise ValueError("objective crossing requires its first timestamp")


@dataclass(frozen=True)
class Incident:
    incident_id: str
    account_key: str
    category: str
    severity: IncidentSeverity
    opened_at: datetime
    detail: Mapping[str, Any]

    def __post_init__(self) -> None:
        for name in ("incident_id", "account_key", "category"):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        object.__setattr__(self, "severity", _enum(self.severity, IncidentSeverity, "severity"))
        object.__setattr__(self, "opened_at", _utc(self.opened_at, "opened_at"))
        if not isinstance(self.detail, Mapping):
            raise ValueError("detail must be a mapping")
        object.__setattr__(self, "detail", dict(self.detail))


@dataclass(frozen=True)
class OutboxMessage:
    message_id: str
    event_key: str
    account_key: str
    template: str
    payload: Mapping[str, Any]
    created_at: datetime
    state: OutboxState = OutboxState.PENDING

    def __post_init__(self) -> None:
        for name in ("message_id", "event_key", "account_key", "template"):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        object.__setattr__(self, "state", _enum(self.state, OutboxState, "state"))
        if not isinstance(self.payload, Mapping):
            raise ValueError("payload must be a mapping")
        object.__setattr__(self, "payload", dict(self.payload))
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))


@dataclass(frozen=True)
class LatencySample:
    sample_id: str
    account_key: str
    stage: str
    duration_microseconds: int
    observed_at: datetime
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("sample_id", "account_key", "stage"):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        if (
            isinstance(self.duration_microseconds, bool)
            or not isinstance(self.duration_microseconds, int)
            or self.duration_microseconds < 0
        ):
            raise ValueError("duration_microseconds must be a nonnegative integer")
        object.__setattr__(self, "observed_at", _utc(self.observed_at, "observed_at"))
        if self.correlation_id is not None:
            object.__setattr__(self, "correlation_id", _required(self.correlation_id, "correlation_id"))


__all__ = [
    "BrokerOrder",
    "BrokerOrderState",
    "BrokerSnapshot",
    "EngineMode",
    "ExpiringPlan",
    "Fill",
    "Incident",
    "IncidentSeverity",
    "IntentKind",
    "IntentState",
    "LatencySample",
    "OrderIntent",
    "OutboxMessage",
    "OutboxState",
    "PlanState",
    "PositionRecord",
    "ProtectionObligation",
    "ProtectionState",
    "ReservationState",
    "RiskReservation",
    "SessionLatch",
]
