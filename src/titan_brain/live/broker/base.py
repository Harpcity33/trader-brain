"""Narrow, fail-closed broker boundary for the live coordinator.

The live engine must not depend on connector-specific payloads.  This module
defines the small normalized surface it is allowed to use and makes ambiguous
submission outcomes a first-class error.  It deliberately contains no
credentials, connector discovery, or implicit retry behavior.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import re
from typing import Any, Mapping, Protocol, runtime_checkable
from uuid import UUID

from ..models import BrokerOrderState
from ..money import finite_decimal, positive_decimal, whole_shares


_ACCOUNT_MASK = re.compile(r"^(?:•{4}|\*{4})[0-9]{4}$")
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.\-]{0,14}$")


def _required(value: Any, field_name: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def _utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _account_mask(value: Any) -> str:
    normalized = _required(value, "account_masked")
    if not _ACCOUNT_MASK.fullmatch(normalized):
        raise ValueError("account_masked must expose exactly the last four digits")
    return normalized


def _nonnegative(value: Any, field_name: str) -> Decimal:
    normalized = finite_decimal(value, field=field_name)
    if normalized < 0:
        raise ValueError(f"{field_name} must be nonnegative")
    return normalized


def _enum(value: Any, enum_type: type[Enum], field_name: str) -> Enum:
    try:
        return enum_type(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"unsupported {field_name}: {value!r}") from exc


class BrokerSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class EquityOrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP_MARKET = "stop_market"
    STOP_LIMIT = "stop_limit"


class MarketHours(str, Enum):
    REGULAR = "regular_hours"
    EXTENDED = "extended_hours"
    ALL_DAY = "all_day_hours"


class TimeInForce(str, Enum):
    GFD = "gfd"
    GTC = "gtc"


class OperationStatus(str, Enum):
    ACKNOWLEDGED = "ACKNOWLEDGED"
    REJECTED = "REJECTED"
    PENDING_CANCEL = "PENDING_CANCEL"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class OrderFamily(str, Enum):
    """Every broker order family that can reserve cash/shares or alter exposure."""

    STANDARD_EQUITY = "standard_equity"
    ADVANCED_EQUITY = "advanced_equity"
    OPTION = "option"


class OrderFamilyCoverageStatus(str, Enum):
    COMPLETE_DEDICATED = "complete_dedicated"
    COMPLETE_GENERAL = "complete_general"
    NOT_APPLICABLE = "not_applicable"
    INCOMPLETE = "incomplete"
    UNKNOWN = "unknown"


class ClientRefRecoverySource(str, Enum):
    DEDICATED_LOOKUP = "dedicated_lookup"
    EXHAUSTIVE_ORDER_HISTORY = "exhaustive_order_history"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class OrderFamilyCoverage:
    """Broker-backed proof for one order family, independent of endpoint name."""

    family: OrderFamily
    status: OrderFamilyCoverageStatus
    evidence_id: str
    broker_authoritative: bool
    all_pages_consumed: bool
    includes_working_orders_across_dates: bool
    includes_parent_child_conditional: bool = False
    account_family_disabled: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "family", _enum(self.family, OrderFamily, "order family"))
        object.__setattr__(
            self,
            "status",
            _enum(self.status, OrderFamilyCoverageStatus, "coverage status"),
        )
        object.__setattr__(self, "evidence_id", _required(self.evidence_id, "evidence_id"))
        for name in (
            "broker_authoritative",
            "all_pages_consumed",
            "includes_working_orders_across_dates",
            "includes_parent_child_conditional",
            "account_family_disabled",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be boolean")

        complete = self.status in {
            OrderFamilyCoverageStatus.COMPLETE_DEDICATED,
            OrderFamilyCoverageStatus.COMPLETE_GENERAL,
        }
        if complete and not all(
            (
                self.broker_authoritative,
                self.all_pages_consumed,
                self.includes_working_orders_across_dates,
            )
        ):
            raise ValueError("complete order-family coverage requires authoritative all-page history")
        if complete and self.family is OrderFamily.ADVANCED_EQUITY and not self.includes_parent_child_conditional:
            raise ValueError("advanced-equity coverage must include parent/child/conditional records")
        if self.status is OrderFamilyCoverageStatus.NOT_APPLICABLE and not all(
            (self.broker_authoritative, self.account_family_disabled)
        ):
            raise ValueError("not-applicable coverage requires broker proof that the family is disabled")
        if self.account_family_disabled and self.status is not OrderFamilyCoverageStatus.NOT_APPLICABLE:
            raise ValueError("account_family_disabled is valid only for not-applicable coverage")

    @property
    def proves_complete(self) -> bool:
        return self.status in {
            OrderFamilyCoverageStatus.COMPLETE_DEDICATED,
            OrderFamilyCoverageStatus.COMPLETE_GENERAL,
            OrderFamilyCoverageStatus.NOT_APPLICABLE,
        }


@dataclass(frozen=True)
class OrderCoverageContract:
    """Exhaustive, provenance-bearing order visibility and ref-recovery contract."""

    contract_version: str
    evidence_observed_at: datetime
    families: tuple[OrderFamilyCoverage, ...]
    client_ref_recovery_source: ClientRefRecoverySource
    broker_preserves_client_ref: bool
    negative_client_ref_results_authoritative: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "contract_version", _required(self.contract_version, "contract_version")
        )
        object.__setattr__(
            self,
            "evidence_observed_at",
            _utc(self.evidence_observed_at, "evidence_observed_at"),
        )
        families = tuple(self.families)
        if any(not isinstance(item, OrderFamilyCoverage) for item in families):
            raise ValueError("families must contain OrderFamilyCoverage records")
        if {item.family for item in families} != set(OrderFamily) or len(families) != len(OrderFamily):
            raise ValueError("coverage contract must classify every order family exactly once")
        object.__setattr__(self, "families", families)
        object.__setattr__(
            self,
            "client_ref_recovery_source",
            _enum(
                self.client_ref_recovery_source,
                ClientRefRecoverySource,
                "client-ref recovery source",
            ),
        )
        for name in (
            "broker_preserves_client_ref",
            "negative_client_ref_results_authoritative",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be boolean")
        if self.client_ref_recovery_source is ClientRefRecoverySource.UNAVAILABLE:
            if self.broker_preserves_client_ref or self.negative_client_ref_results_authoritative:
                raise ValueError("unavailable client-ref recovery cannot claim positive guarantees")
        elif not self.broker_preserves_client_ref:
            raise ValueError("client-ref recovery requires broker-preserved IDs")
        if (
            self.client_ref_recovery_source is ClientRefRecoverySource.EXHAUSTIVE_ORDER_HISTORY
            and not all(
                self.family_complete(family)
                for family in (OrderFamily.STANDARD_EQUITY, OrderFamily.ADVANCED_EQUITY)
            )
        ):
            raise ValueError("history-based client-ref recovery requires complete equity-order families")

    def family(self, family: OrderFamily) -> OrderFamilyCoverage:
        normalized = _enum(family, OrderFamily, "order family")
        return next(item for item in self.families if item.family is normalized)

    def family_complete(self, family: OrderFamily) -> bool:
        return self.family(family).proves_complete

    @property
    def proves_whole_account_order_coverage(self) -> bool:
        return all(item.proves_complete for item in self.families)

    @property
    def supports_exact_client_ref_recovery(self) -> bool:
        """Whether exact positive matches can be recovered.

        This deliberately does not imply that a missing record is an
        authoritative rejection. Eventually-consistent order history often
        preserves client IDs while publishing accepted orders after a delay.
        """

        return all(
            (
                self.client_ref_recovery_source
                is not ClientRefRecoverySource.UNAVAILABLE,
                self.broker_preserves_client_ref,
            )
        )


@dataclass(frozen=True)
class BrokerCapabilities:
    """Connector contract plus properties of the configured runtime path.

    ``supports_*`` describes the upstream connector contract.  The separate
    daemon/transport fields prevent a model-mediated capability from being
    mistaken for a capability available to a background process.
    """

    connector: str
    account_masked: str
    supports_account_read: bool
    supports_equity_position_read: bool
    supports_equity_order_read: bool
    supports_option_position_read: bool
    supports_option_order_read: bool
    supports_advanced_order_read: bool
    supports_equity_review: bool
    supports_equity_place: bool
    supports_equity_cancel: bool
    daemon_transport_configured: bool
    supports_daemon_writes: bool
    supports_unattended_writes: bool
    supports_atomic_protection: bool
    supports_equity_replace: bool
    supports_streaming: bool
    supports_auth_refresh: bool
    supports_ref_id_lookup: bool
    review_requires_explicit_confirmation: bool
    cancel_requires_explicit_confirmation: bool
    cancel_is_asynchronous: bool
    supported_order_types: tuple[EquityOrderType, ...]
    supported_market_hours: tuple[MarketHours, ...]
    supported_time_in_force: tuple[TimeInForce, ...]
    order_coverage: OrderCoverageContract
    unsupported_operations: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "connector", _required(self.connector, "connector"))
        object.__setattr__(self, "account_masked", _account_mask(self.account_masked))
        bool_fields = (
            "supports_account_read",
            "supports_equity_position_read",
            "supports_equity_order_read",
            "supports_option_position_read",
            "supports_option_order_read",
            "supports_advanced_order_read",
            "supports_equity_review",
            "supports_equity_place",
            "supports_equity_cancel",
            "daemon_transport_configured",
            "supports_daemon_writes",
            "supports_unattended_writes",
            "supports_atomic_protection",
            "supports_equity_replace",
            "supports_streaming",
            "supports_auth_refresh",
            "supports_ref_id_lookup",
            "review_requires_explicit_confirmation",
            "cancel_requires_explicit_confirmation",
            "cancel_is_asynchronous",
        )
        for name in bool_fields:
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be boolean")
        if self.supports_unattended_writes and not self.supports_daemon_writes:
            raise ValueError("unattended writes require a configured daemon write path")
        if self.supports_daemon_writes and not self.daemon_transport_configured:
            raise ValueError("daemon writes require a configured daemon transport")
        object.__setattr__(
            self,
            "supported_order_types",
            tuple(
                _enum(value, EquityOrderType, "order type")
                for value in self.supported_order_types
            ),
        )
        object.__setattr__(
            self,
            "supported_market_hours",
            tuple(
                _enum(value, MarketHours, "market hours")
                for value in self.supported_market_hours
            ),
        )
        object.__setattr__(
            self,
            "supported_time_in_force",
            tuple(
                _enum(value, TimeInForce, "time in force")
                for value in self.supported_time_in_force
            ),
        )
        object.__setattr__(self, "unsupported_operations", tuple(self.unsupported_operations))
        object.__setattr__(self, "notes", tuple(self.notes))
        if not isinstance(self.order_coverage, OrderCoverageContract):
            raise ValueError("order_coverage must be an OrderCoverageContract")
        if self.supports_ref_id_lookup != self.order_coverage.supports_exact_client_ref_recovery:
            raise ValueError("client-ref capability must match its evidence-backed coverage contract")

    @property
    def can_prove_whole_broker_reconciliation(self) -> bool:
        return all(
            (
                self.supports_account_read,
                self.supports_equity_position_read,
                self.supports_equity_order_read,
                self.supports_option_position_read,
                self.order_coverage.proves_whole_account_order_coverage,
            )
        )


@dataclass(frozen=True)
class FundsSnapshot:
    total_value: Decimal
    cash: Decimal
    buying_power: Decimal
    unleveraged_buying_power: Decimal
    unsettled_funds: Decimal | None = None
    currency: str = "USD"
    unsettled_funds_is_order_gating: bool = False

    def __post_init__(self) -> None:
        for name in ("total_value", "cash", "buying_power", "unleveraged_buying_power"):
            object.__setattr__(self, name, finite_decimal(getattr(self, name), field=name))
        if self.unsettled_funds is not None:
            object.__setattr__(
                self,
                "unsettled_funds",
                _nonnegative(self.unsettled_funds, "unsettled_funds"),
            )
        currency = _required(self.currency, "currency").upper()
        object.__setattr__(self, "currency", currency)
        if not isinstance(self.unsettled_funds_is_order_gating, bool):
            raise ValueError("unsettled_funds_is_order_gating must be boolean")


@dataclass(frozen=True)
class PositionSnapshot:
    symbol: str
    quantity: Decimal
    sellable_quantity: Decimal
    held_for_sells: Decimal = Decimal("0")
    average_price: Decimal | None = None
    asset_class: str = "equity"

    def __post_init__(self) -> None:
        symbol = _required(self.symbol, "symbol").upper()
        if not _SYMBOL.fullmatch(symbol):
            raise ValueError("symbol is invalid")
        object.__setattr__(self, "symbol", symbol)
        for name in ("quantity", "sellable_quantity", "held_for_sells"):
            object.__setattr__(self, name, _nonnegative(getattr(self, name), name))
        if self.sellable_quantity + self.held_for_sells > self.quantity:
            raise ValueError("sellable plus held quantity cannot exceed position quantity")
        if self.average_price is not None:
            object.__setattr__(
                self,
                "average_price",
                positive_decimal(self.average_price, field="average_price"),
            )
        object.__setattr__(self, "asset_class", _required(self.asset_class, "asset_class"))

    @property
    def is_fractional(self) -> bool:
        return self.quantity != self.quantity.to_integral_value()


@dataclass(frozen=True)
class FillSnapshot:
    fill_id: str
    quantity: Decimal
    price: Decimal
    executed_at: datetime
    fee: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        object.__setattr__(self, "fill_id", _required(self.fill_id, "fill_id"))
        object.__setattr__(self, "quantity", positive_decimal(self.quantity, field="quantity"))
        object.__setattr__(self, "price", positive_decimal(self.price, field="price"))
        object.__setattr__(self, "fee", _nonnegative(self.fee, "fee"))
        object.__setattr__(self, "executed_at", _utc(self.executed_at, "executed_at"))


@dataclass(frozen=True)
class OrderSnapshot:
    broker_order_id: str
    account_masked: str
    symbol: str
    side: BrokerSide
    order_type: EquityOrderType
    state: BrokerOrderState
    requested_quantity: Decimal
    cumulative_filled_quantity: Decimal
    market_hours: MarketHours
    time_in_force: TimeInForce
    broker_updated_at: datetime
    received_at: datetime
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    client_ref_id: str | None = None
    fills: tuple[FillSnapshot, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "broker_order_id", _required(self.broker_order_id, "broker_order_id"))
        object.__setattr__(self, "account_masked", _account_mask(self.account_masked))
        symbol = _required(self.symbol, "symbol").upper()
        if not _SYMBOL.fullmatch(symbol):
            raise ValueError("symbol is invalid")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "side", _enum(self.side, BrokerSide, "side"))
        object.__setattr__(
            self,
            "order_type",
            _enum(self.order_type, EquityOrderType, "order type"),
        )
        object.__setattr__(
            self,
            "state",
            _enum(self.state, BrokerOrderState, "broker order state"),
        )
        object.__setattr__(
            self,
            "market_hours",
            _enum(self.market_hours, MarketHours, "market hours"),
        )
        object.__setattr__(
            self,
            "time_in_force",
            _enum(self.time_in_force, TimeInForce, "time in force"),
        )
        requested = positive_decimal(self.requested_quantity, field="requested_quantity")
        cumulative = _nonnegative(self.cumulative_filled_quantity, "cumulative_filled_quantity")
        if cumulative > requested:
            raise ValueError("cumulative fill cannot exceed requested quantity")
        object.__setattr__(self, "requested_quantity", requested)
        object.__setattr__(self, "cumulative_filled_quantity", cumulative)
        updated = _utc(self.broker_updated_at, "broker_updated_at")
        received = _utc(self.received_at, "received_at")
        if received < updated:
            raise ValueError("received_at cannot precede broker_updated_at")
        object.__setattr__(self, "broker_updated_at", updated)
        object.__setattr__(self, "received_at", received)
        for name in ("limit_price", "stop_price"):
            if getattr(self, name) is not None:
                object.__setattr__(
                    self,
                    name,
                    positive_decimal(getattr(self, name), field=name),
                )
        if self.order_type is EquityOrderType.MARKET:
            if self.limit_price is not None or self.stop_price is not None:
                raise ValueError("market orders cannot carry limit or stop prices")
        elif self.order_type is EquityOrderType.LIMIT:
            if self.limit_price is None or self.stop_price is not None:
                raise ValueError("limit orders require only limit_price")
        elif self.order_type is EquityOrderType.STOP_MARKET:
            if self.stop_price is None or self.limit_price is not None:
                raise ValueError("stop-market orders require only stop_price")
        elif self.order_type is EquityOrderType.STOP_LIMIT:
            if self.stop_price is None or self.limit_price is None:
                raise ValueError("stop-limit orders require stop_price and limit_price")
        if self.market_hours is not MarketHours.REGULAR and self.order_type is not EquityOrderType.LIMIT:
            raise ValueError("extended/all-day-hours equity orders must be limit orders")
        if self.client_ref_id is not None:
            try:
                normalized_ref = str(UUID(str(self.client_ref_id)))
            except (ValueError, AttributeError) as exc:
                raise ValueError("client_ref_id must be a UUID") from exc
            object.__setattr__(self, "client_ref_id", normalized_ref)
        object.__setattr__(self, "fills", tuple(self.fills))
        if any(not isinstance(fill, FillSnapshot) for fill in self.fills):
            raise ValueError("fills must contain FillSnapshot records")
        fill_total = sum((fill.quantity for fill in self.fills), Decimal("0"))
        if fill_total != cumulative:
            raise ValueError("fill quantities must equal cumulative_filled_quantity")


@dataclass(frozen=True)
class AccountSnapshot:
    account_masked: str
    observed_at: datetime
    received_at: datetime
    account_state: str
    account_type: str
    funds: FundsSnapshot
    equity_positions: tuple[PositionSnapshot, ...]
    equity_orders: tuple[OrderSnapshot, ...]
    option_position_count: int
    option_order_count: int
    advanced_order_count: int
    standard_equity_positions_complete: bool
    standard_equity_orders_complete: bool
    option_positions_complete: bool
    option_orders_complete: bool
    advanced_orders_complete: bool
    auth_point_in_time: bool
    daily_realized_pnl: Decimal | None = None
    weekly_realized_pnl: Decimal | None = None
    peak_equity: Decimal | None = None
    daily_realized_pnl_complete: bool = False
    weekly_realized_pnl_complete: bool = False
    peak_equity_complete: bool = False
    risk_evidence_authoritative: bool = False
    risk_evidence_source: str | None = None
    risk_evidence_as_of: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "account_masked", _account_mask(self.account_masked))
        observed = _utc(self.observed_at, "observed_at")
        received = _utc(self.received_at, "received_at")
        if received < observed:
            raise ValueError("received_at cannot precede observed_at")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "received_at", received)
        object.__setattr__(self, "account_state", _required(self.account_state, "account_state"))
        object.__setattr__(self, "account_type", _required(self.account_type, "account_type"))
        if not isinstance(self.funds, FundsSnapshot):
            raise ValueError("funds must be a FundsSnapshot")
        object.__setattr__(self, "equity_positions", tuple(self.equity_positions))
        object.__setattr__(self, "equity_orders", tuple(self.equity_orders))
        if any(not isinstance(position, PositionSnapshot) for position in self.equity_positions):
            raise ValueError("equity_positions must contain PositionSnapshot records")
        if any(not isinstance(order, OrderSnapshot) for order in self.equity_orders):
            raise ValueError("equity_orders must contain OrderSnapshot records")
        for name in (
            "option_position_count",
            "option_order_count",
            "advanced_order_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        for name in (
            "standard_equity_positions_complete",
            "standard_equity_orders_complete",
            "option_positions_complete",
            "option_orders_complete",
            "advanced_orders_complete",
            "auth_point_in_time",
            "daily_realized_pnl_complete",
            "weekly_realized_pnl_complete",
            "peak_equity_complete",
            "risk_evidence_authoritative",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be boolean")
        if any(order.account_masked != self.account_masked for order in self.equity_orders):
            raise ValueError("all orders must belong to the snapshot account")
        for name in ("daily_realized_pnl", "weekly_realized_pnl", "peak_equity"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, finite_decimal(value, field=name))
        if self.peak_equity is not None and self.peak_equity <= 0:
            raise ValueError("peak_equity must be positive when supplied")
        if (
            self.peak_equity is not None
            and self.peak_equity < self.funds.total_value
        ):
            raise ValueError("peak_equity cannot be below current total value")
        completeness = (
            (self.daily_realized_pnl_complete, self.daily_realized_pnl, "daily_realized_pnl"),
            (self.weekly_realized_pnl_complete, self.weekly_realized_pnl, "weekly_realized_pnl"),
            (self.peak_equity_complete, self.peak_equity, "peak_equity"),
        )
        for complete, value, name in completeness:
            if complete and value is None:
                raise ValueError(f"{name} cannot be complete without an exact value")
        if self.risk_evidence_source is not None:
            object.__setattr__(
                self,
                "risk_evidence_source",
                _required(self.risk_evidence_source, "risk_evidence_source"),
            )
        if self.risk_evidence_as_of is not None:
            as_of = _utc(self.risk_evidence_as_of, "risk_evidence_as_of")
            if as_of > received:
                raise ValueError("risk_evidence_as_of cannot follow snapshot receipt")
            object.__setattr__(self, "risk_evidence_as_of", as_of)
        if self.risk_evidence_authoritative and (
            self.risk_evidence_source is None or self.risk_evidence_as_of is None
        ):
            raise ValueError(
                "authoritative risk evidence requires source and as-of provenance"
            )

    @property
    def whole_broker_reconciled(self) -> bool:
        return all(
            (
                self.standard_equity_positions_complete,
                self.standard_equity_orders_complete,
                self.option_positions_complete,
                self.option_orders_complete,
                self.advanced_orders_complete,
            )
        )

    @property
    def daily_realized_pnl_ready(self) -> bool:
        return all(
            (
                self.daily_realized_pnl_complete,
                self.daily_realized_pnl is not None,
                self.risk_evidence_authoritative,
                self.risk_evidence_source is not None,
                self.risk_evidence_as_of is not None,
            )
        )

    @property
    def entry_risk_evidence_ready(self) -> bool:
        return all(
            (
                self.daily_realized_pnl_ready,
                self.weekly_realized_pnl_complete,
                self.weekly_realized_pnl is not None,
                self.peak_equity_complete,
                self.peak_equity is not None,
            )
        )


@dataclass(frozen=True)
class OrderRequest:
    account_masked: str
    symbol: str
    side: BrokerSide
    order_type: EquityOrderType
    quantity: int
    market_hours: MarketHours
    time_in_force: TimeInForce
    client_ref_id: str
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "account_masked", _account_mask(self.account_masked))
        symbol = _required(self.symbol, "symbol").upper()
        if not _SYMBOL.fullmatch(symbol):
            raise ValueError("symbol is invalid")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "side", _enum(self.side, BrokerSide, "side"))
        object.__setattr__(
            self,
            "order_type",
            _enum(self.order_type, EquityOrderType, "order type"),
        )
        object.__setattr__(
            self,
            "market_hours",
            _enum(self.market_hours, MarketHours, "market hours"),
        )
        object.__setattr__(
            self,
            "time_in_force",
            _enum(self.time_in_force, TimeInForce, "time in force"),
        )
        object.__setattr__(self, "quantity", whole_shares(self.quantity))
        try:
            normalized_ref = str(UUID(str(self.client_ref_id)))
        except (ValueError, AttributeError) as exc:
            raise ValueError("client_ref_id must be a UUID") from exc
        object.__setattr__(self, "client_ref_id", normalized_ref)
        for name in ("limit_price", "stop_price"):
            if getattr(self, name) is not None:
                object.__setattr__(
                    self,
                    name,
                    positive_decimal(getattr(self, name), field=name),
                )
        if self.order_type is EquityOrderType.MARKET:
            if self.limit_price is not None or self.stop_price is not None:
                raise ValueError("market orders cannot carry limit or stop prices")
        elif self.order_type is EquityOrderType.LIMIT:
            if self.limit_price is None or self.stop_price is not None:
                raise ValueError("limit orders require only limit_price")
        elif self.order_type is EquityOrderType.STOP_MARKET:
            if self.stop_price is None or self.limit_price is not None:
                raise ValueError("stop-market orders require only stop_price")
        elif self.order_type is EquityOrderType.STOP_LIMIT:
            if self.stop_price is None or self.limit_price is None:
                raise ValueError("stop-limit orders require stop_price and limit_price")
        if self.market_hours is not MarketHours.REGULAR and self.order_type is not EquityOrderType.LIMIT:
            raise ValueError("extended/all-day-hours equity orders must be limit orders")

    @property
    def exact_tuple(self) -> tuple[object, ...]:
        return (
            self.account_masked,
            self.symbol,
            self.side.value,
            self.order_type.value,
            self.quantity,
            self.market_hours.value,
            self.time_in_force.value,
            str(self.limit_price) if self.limit_price is not None else None,
            str(self.stop_price) if self.stop_price is not None else None,
            self.client_ref_id,
        )


@dataclass(frozen=True)
class OrderCheck:
    code: str
    severity: str
    message: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _required(self.code, "code"))
        object.__setattr__(self, "severity", _required(self.severity, "severity"))
        object.__setattr__(self, "message", _required(self.message, "message"))


@dataclass(frozen=True)
class ReviewReceipt:
    request: OrderRequest
    reviewed_at: datetime
    expires_at: datetime | None
    disclosure: str
    order_checks: tuple[OrderCheck, ...]
    required_confirmation_phrase: str | None
    broker_review_id: str | None
    broker_bound: bool
    preview: Mapping[str, Any] = field(default_factory=dict)
    received_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.request, OrderRequest):
            raise ValueError("request must be an OrderRequest")
        reviewed = _utc(self.reviewed_at, "reviewed_at")
        object.__setattr__(self, "reviewed_at", reviewed)
        if self.expires_at is not None:
            expires = _utc(self.expires_at, "expires_at")
            if expires <= reviewed:
                raise ValueError("review expiry must follow review time")
            object.__setattr__(self, "expires_at", expires)
        object.__setattr__(self, "disclosure", str(self.disclosure))
        object.__setattr__(self, "order_checks", tuple(self.order_checks))
        if any(not isinstance(check, OrderCheck) for check in self.order_checks):
            raise ValueError("order_checks must contain OrderCheck records")
        if self.required_confirmation_phrase is not None:
            object.__setattr__(
                self,
                "required_confirmation_phrase",
                _required(self.required_confirmation_phrase, "required_confirmation_phrase"),
            )
        if self.broker_review_id is not None:
            object.__setattr__(
                self,
                "broker_review_id",
                _required(self.broker_review_id, "broker_review_id"),
            )
        if not isinstance(self.broker_bound, bool):
            raise ValueError("broker_bound must be boolean")
        if not isinstance(self.preview, Mapping):
            raise ValueError("preview must be a mapping")
        object.__setattr__(self, "preview", dict(self.preview))
        receipt = self.reviewed_at if self.received_at is None else _utc(
            self.received_at, "received_at"
        )
        if receipt < self.reviewed_at:
            raise ValueError("review receipt cannot precede review observation")
        object.__setattr__(self, "received_at", receipt)

    def expired_at(self, now: datetime) -> bool:
        return self.expires_at is not None and _utc(now, "now") >= self.expires_at


@dataclass(frozen=True)
class BrokerNativeReview(ReviewReceipt):
    """A provider-issued review/preview with its native execution binding."""

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.broker_bound or self.broker_review_id is None:
            raise ValueError("broker-native review requires a broker review ID")


@dataclass(frozen=True, kw_only=True)
class LocalPreflightDecision(ReviewReceipt):
    """A local policy decision, never a broker review or approval token.

    This is valid only on a separately verified provider contract that permits
    direct API order submission. A mandatory broker review/confirmation can
    never be represented by this type.
    """

    decision_id: str = ""
    policy_binding_id: str = ""
    evidence_collection_id: str = ""
    provider_contract_id: str = ""

    def __post_init__(self) -> None:
        super().__post_init__()
        for name in (
            "decision_id",
            "policy_binding_id",
            "evidence_collection_id",
            "provider_contract_id",
        ):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        if self.broker_bound or self.broker_review_id is not None:
            raise ValueError("local preflight cannot claim a broker review binding")
        if self.required_confirmation_phrase is not None:
            raise ValueError("local preflight cannot encode broker confirmation")


@dataclass(frozen=True)
class BrokerOperationResult:
    operation: str
    status: OperationStatus
    observed_at: datetime
    received_at: datetime
    accepted: bool | None
    message: str
    order: OrderSnapshot | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "operation", _required(self.operation, "operation"))
        object.__setattr__(
            self,
            "status",
            _enum(self.status, OperationStatus, "operation status"),
        )
        observed_at = _utc(self.observed_at, "observed_at")
        received_at = _utc(self.received_at, "received_at")
        if received_at < observed_at:
            raise ValueError("operation receipt cannot precede provider observation")
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "received_at", received_at)
        if self.accepted is not None and not isinstance(self.accepted, bool):
            raise ValueError("accepted must be boolean or None")
        object.__setattr__(self, "message", str(self.message))
        if self.status is OperationStatus.UNKNOWN and self.accepted is not None:
            raise ValueError("an unknown result cannot claim accepted or rejected")
        if self.order is not None and not isinstance(self.order, OrderSnapshot):
            raise ValueError("order must be an OrderSnapshot or None")
        if self.order is not None and self.order.received_at > received_at:
            raise ValueError("order receipt cannot follow its operation receipt")


@dataclass(frozen=True)
class ClientRefLookupResult:
    """Exact client-ref lookup with explicit unresolved-negative semantics."""

    account_masked: str
    requested_client_refs: tuple[str, ...]
    found_orders: tuple[OrderSnapshot, ...]
    confirmed_absent_client_refs: tuple[str, ...]
    observed_at: datetime
    received_at: datetime
    complete: bool
    not_seen_yet_client_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        account = _account_mask(self.account_masked)
        requested = tuple(_required(item, "requested_client_ref") for item in self.requested_client_refs)
        absent = tuple(
            _required(item, "confirmed_absent_client_ref")
            for item in self.confirmed_absent_client_refs
        )
        not_seen = tuple(
            _required(item, "not_seen_yet_client_ref")
            for item in self.not_seen_yet_client_refs
        )
        found = tuple(self.found_orders)
        if any(
            not isinstance(order, OrderSnapshot) or order.account_masked != account
            for order in found
        ):
            raise ValueError("found orders must belong to lookup account")
        if len(set(requested)) != len(requested):
            raise ValueError("requested client refs must be unique")
        if len(set(absent)) != len(absent) or not set(absent).issubset(requested):
            raise ValueError("absent client refs must be a unique requested subset")
        if len(set(not_seen)) != len(not_seen) or not set(not_seen).issubset(requested):
            raise ValueError("not-seen-yet client refs must be a unique requested subset")
        found_refs = tuple(order.client_ref_id for order in found)
        if any(ref is None or ref not in requested for ref in found_refs):
            raise ValueError("found orders must bind requested client refs")
        if len(set(found_refs)) != len(found_refs):
            raise ValueError("lookup returned duplicate client refs")
        classified = (set(found_refs), set(absent), set(not_seen))
        if any(
            classified[left].intersection(classified[right])
            for left, right in ((0, 1), (0, 2), (1, 2))
        ):
            raise ValueError("client-ref lookup classifications must be disjoint")
        if self.complete is True and set().union(*classified) != set(requested):
            raise ValueError("complete lookup must classify every requested client ref")
        if not isinstance(self.complete, bool):
            raise ValueError("lookup complete must be boolean")
        object.__setattr__(self, "account_masked", account)
        object.__setattr__(self, "requested_client_refs", requested)
        object.__setattr__(self, "found_orders", found)
        object.__setattr__(self, "confirmed_absent_client_refs", absent)
        object.__setattr__(self, "not_seen_yet_client_refs", not_seen)
        observed = _utc(self.observed_at, "observed_at")
        received = _utc(self.received_at, "received_at")
        if received < observed:
            raise ValueError("lookup receipt cannot precede provider observation")
        if any(order.received_at > received for order in found):
            raise ValueError("found order receipt cannot follow lookup receipt")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "received_at", received)


class BrokerError(RuntimeError):
    """Base connector error with explicit retry and side-effect semantics."""

    code = "BROKER_ERROR"
    retry_safe = False
    submission_may_have_reached_broker = False

    def __init__(self, message: str, *, detail: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.detail = dict(detail or {})


class BrokerCapabilityError(BrokerError):
    code = "BROKER_CAPABILITY_UNAVAILABLE"


class BrokerAuthenticationError(BrokerError):
    code = "BROKER_AUTHENTICATION_FAILED"


class BrokerContractViolation(BrokerError):
    code = "BROKER_CONTRACT_VIOLATION"


class BrokerMutationBlocked(BrokerError):
    code = "BROKER_MUTATION_BLOCKED"


class BrokerUnknownSubmission(BrokerError):
    """The request may have reached the broker; never blindly retry it."""

    code = "BROKER_SUBMISSION_UNKNOWN"
    submission_may_have_reached_broker = True


@runtime_checkable
class BrokerClient(Protocol):
    @property
    def capabilities(self) -> BrokerCapabilities:
        ...

    def get_account_snapshot(self, account_masked: str) -> AccountSnapshot:
        ...

    def lookup_equity_orders_by_client_ref(
        self, account_masked: str, client_refs: tuple[str, ...]
    ) -> ClientRefLookupResult:
        ...

    def review_equity_order(self, request: OrderRequest) -> ReviewReceipt:
        ...

    def place_equity_order(
        self,
        request: OrderRequest,
        *,
        review: ReviewReceipt,
        explicit_confirmation: str | None = None,
    ) -> BrokerOperationResult:
        ...

    def cancel_equity_order(
        self,
        account_masked: str,
        broker_order_id: str,
        *,
        explicit_confirmation: str | None = None,
    ) -> BrokerOperationResult:
        ...


__all__ = [
    "AccountSnapshot",
    "BrokerAuthenticationError",
    "BrokerCapabilities",
    "BrokerCapabilityError",
    "BrokerClient",
    "BrokerContractViolation",
    "BrokerError",
    "BrokerMutationBlocked",
    "BrokerNativeReview",
    "BrokerOperationResult",
    "BrokerOrderState",
    "BrokerSide",
    "BrokerUnknownSubmission",
    "ClientRefRecoverySource",
    "ClientRefLookupResult",
    "EquityOrderType",
    "FillSnapshot",
    "FundsSnapshot",
    "LocalPreflightDecision",
    "MarketHours",
    "OperationStatus",
    "OrderCoverageContract",
    "OrderCheck",
    "OrderFamily",
    "OrderFamilyCoverage",
    "OrderFamilyCoverageStatus",
    "OrderRequest",
    "OrderSnapshot",
    "PositionSnapshot",
    "ReviewReceipt",
    "TimeInForce",
]
