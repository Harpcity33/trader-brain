"""Deterministic broker fake for lifecycle and failure-path tests."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Callable, Iterable, Mapping

from ..models import BrokerOrderState
from ..money import positive_decimal
from .base import (
    AccountSnapshot,
    BrokerAuthenticationError,
    BrokerCapabilities,
    BrokerCapabilityError,
    BrokerContractViolation,
    BrokerOperationResult,
    BrokerSide,
    BrokerUnknownSubmission,
    ClientRefRecoverySource,
    ClientRefLookupResult,
    EquityOrderType,
    FillSnapshot,
    FundsSnapshot,
    MarketHours,
    OperationStatus,
    OrderCoverageContract,
    OrderFamily,
    OrderFamilyCoverage,
    OrderFamilyCoverageStatus,
    OrderRequest,
    OrderSnapshot,
    ReviewReceipt,
    TimeInForce,
)


class FakeFault(str, Enum):
    AUTHENTICATION = "AUTHENTICATION"
    READ_FAILURE = "READ_FAILURE"
    REVIEW_REJECTED = "REVIEW_REJECTED"
    PLACE_REJECTED = "PLACE_REJECTED"
    PLACE_UNKNOWN_BEFORE_ACCEPT = "PLACE_UNKNOWN_BEFORE_ACCEPT"
    PLACE_UNKNOWN_AFTER_ACCEPT = "PLACE_UNKNOWN_AFTER_ACCEPT"
    CANCEL_REJECTED = "CANCEL_REJECTED"
    CANCEL_FILL_RACE = "CANCEL_FILL_RACE"


class FakeReadError(BrokerCapabilityError):
    code = "FAKE_READ_FAILURE"
    retry_safe = True


class FakeBrokerClient:
    """In-memory fake whose faults are consumed in an explicit FIFO order.

    No random source is used.  Tests may inject a clock, initial snapshot, and
    per-operation fault sequence, making incident and reconciliation behavior
    fully reproducible.
    """

    SNAPSHOT = "snapshot"
    LOOKUP = "lookup_client_refs"
    REVIEW = "review"
    PLACE = "place"
    CANCEL = "cancel"

    def __init__(
        self,
        *,
        account_masked: str = "••••7153",
        initial_snapshot: AccountSnapshot | None = None,
        clock: Callable[[], datetime] | None = None,
        require_explicit_confirmation: bool = False,
        faults: Mapping[str, Iterable[FakeFault]] | None = None,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._require_confirmation = require_explicit_confirmation
        self._owns_default_snapshot = initial_snapshot is None
        now = self._now()
        self._snapshot = initial_snapshot or AccountSnapshot(
            account_masked=account_masked,
            observed_at=now,
            received_at=now,
            account_state="active",
            account_type="individual",
            funds=FundsSnapshot(
                total_value=Decimal("1000.00"),
                cash=Decimal("1000.00"),
                buying_power=Decimal("1000.00"),
                unleveraged_buying_power=Decimal("1000.00"),
                unsettled_funds=Decimal("0"),
            ),
            equity_positions=(),
            equity_orders=(),
            option_position_count=0,
            option_order_count=0,
            advanced_order_count=0,
            standard_equity_positions_complete=True,
            standard_equity_orders_complete=True,
            option_positions_complete=True,
            option_orders_complete=True,
            advanced_orders_complete=True,
            auth_point_in_time=True,
            daily_realized_pnl=Decimal("0.00"),
            weekly_realized_pnl=Decimal("0.00"),
            peak_equity=Decimal("1000.00"),
            daily_realized_pnl_complete=True,
            weekly_realized_pnl_complete=True,
            peak_equity_complete=True,
            risk_evidence_authoritative=True,
            risk_evidence_source="deterministic_fake_ledger",
            risk_evidence_as_of=now,
        )
        self._orders = {
            order.broker_order_id: order for order in self._snapshot.equity_orders
        }
        self._faults: dict[str, deque[FakeFault]] = defaultdict(deque)
        for operation, sequence in (faults or {}).items():
            self._faults[operation].extend(sequence)
        self._order_counter = 0
        self._review_counter = 0
        self._fill_counter = 0
        self.calls: list[tuple[str, object]] = []

    @property
    def capabilities(self) -> BrokerCapabilities:
        coverage = OrderCoverageContract(
            contract_version="deterministic-fake-order-coverage-v1",
            evidence_observed_at=self._now(),
            families=tuple(
                OrderFamilyCoverage(
                    family=family,
                    status=OrderFamilyCoverageStatus.COMPLETE_GENERAL,
                    evidence_id=f"fake:{family.value}:all-in-memory",
                    broker_authoritative=True,
                    all_pages_consumed=True,
                    includes_working_orders_across_dates=True,
                    includes_parent_child_conditional=(
                        family is OrderFamily.ADVANCED_EQUITY
                    ),
                )
                for family in OrderFamily
            ),
            client_ref_recovery_source=ClientRefRecoverySource.DEDICATED_LOOKUP,
            broker_preserves_client_ref=True,
            negative_client_ref_results_authoritative=True,
        )
        return BrokerCapabilities(
            connector="deterministic-fake",
            account_masked=self._snapshot.account_masked,
            supports_account_read=True,
            supports_equity_position_read=True,
            supports_equity_order_read=True,
            supports_option_position_read=True,
            supports_option_order_read=True,
            supports_advanced_order_read=True,
            supports_equity_review=True,
            supports_equity_place=True,
            supports_equity_cancel=True,
            daemon_transport_configured=True,
            supports_daemon_writes=True,
            supports_unattended_writes=not self._require_confirmation,
            supports_atomic_protection=False,
            supports_equity_replace=False,
            supports_streaming=False,
            supports_auth_refresh=False,
            supports_ref_id_lookup=True,
            review_requires_explicit_confirmation=self._require_confirmation,
            cancel_requires_explicit_confirmation=self._require_confirmation,
            cancel_is_asynchronous=True,
            supported_order_types=tuple(EquityOrderType),
            supported_market_hours=tuple(MarketHours),
            supported_time_in_force=tuple(TimeInForce),
            order_coverage=coverage,
            unsupported_operations=("atomic_bracket", "oco", "replace"),
            notes=("Test-only transport; never a production broker.",),
        )

    def inject_fault(self, operation: str, fault: FakeFault) -> None:
        if operation not in {self.SNAPSHOT, self.REVIEW, self.PLACE, self.CANCEL}:
            raise ValueError(f"unknown fake operation: {operation}")
        self._faults[operation].append(fault)

    def get_account_snapshot(self, account_masked: str) -> AccountSnapshot:
        self.calls.append((self.SNAPSHOT, account_masked))
        self._assert_account(account_masked)
        fault = self._next_fault(self.SNAPSHOT)
        if fault is FakeFault.AUTHENTICATION:
            raise BrokerAuthenticationError("injected authentication failure")
        if fault is FakeFault.READ_FAILURE:
            raise FakeReadError("injected read failure")
        if fault is not None:
            raise BrokerContractViolation(f"invalid {fault.value} fault for snapshot")
        now = self._now()
        updates = {
            "observed_at": now,
            "received_at": now,
            "equity_orders": tuple(self._orders[key] for key in sorted(self._orders)),
        }
        if self._owns_default_snapshot:
            updates["risk_evidence_as_of"] = now
        return replace(self._snapshot, **updates)

    def lookup_equity_orders_by_client_ref(
        self, account_masked: str, client_refs: tuple[str, ...]
    ) -> ClientRefLookupResult:
        self.calls.append((self.LOOKUP, tuple(client_refs)))
        self._assert_account(account_masked)
        requested = tuple(str(item).strip() for item in client_refs)
        by_ref = {
            order.client_ref_id: order
            for order in self._orders.values()
            if order.client_ref_id is not None
        }
        observed_at = self._now()
        return ClientRefLookupResult(
            account_masked=account_masked,
            requested_client_refs=requested,
            found_orders=tuple(by_ref[ref] for ref in requested if ref in by_ref),
            confirmed_absent_client_refs=tuple(
                ref for ref in requested if ref not in by_ref
            ),
            observed_at=observed_at,
            received_at=observed_at,
            complete=True,
        )

    def review_equity_order(self, request: OrderRequest) -> ReviewReceipt:
        self.calls.append((self.REVIEW, request.exact_tuple))
        self._assert_account(request.account_masked)
        fault = self._next_fault(self.REVIEW)
        if fault is FakeFault.AUTHENTICATION:
            raise BrokerAuthenticationError("injected authentication failure")
        if fault is FakeFault.REVIEW_REJECTED:
            raise BrokerContractViolation("injected review rejection")
        if fault is not None:
            raise BrokerContractViolation(f"invalid {fault.value} fault for review")
        self._review_counter += 1
        now = self._now()
        phrase = None
        if self._require_confirmation:
            phrase = f"CONFIRM FAKE REVIEW {self._review_counter:04d}"
        return ReviewReceipt(
            request=request,
            reviewed_at=now,
            expires_at=now + timedelta(seconds=30),
            disclosure="Deterministic fake preview; no real order will be sent.",
            order_checks=(),
            required_confirmation_phrase=phrase,
            broker_review_id=f"fake-review-{self._review_counter:04d}",
            broker_bound=True,
            preview={
                "symbol": request.symbol,
                "quantity": request.quantity,
                "market_hours": request.market_hours.value,
            },
        )

    def place_equity_order(
        self,
        request: OrderRequest,
        *,
        review: ReviewReceipt,
        explicit_confirmation: str | None = None,
    ) -> BrokerOperationResult:
        self.calls.append((self.PLACE, request.exact_tuple))
        self._assert_review(request, review, explicit_confirmation)
        fault = self._next_fault(self.PLACE)
        if fault is FakeFault.AUTHENTICATION:
            raise BrokerAuthenticationError("injected authentication failure")
        if fault is FakeFault.PLACE_REJECTED:
            return BrokerOperationResult(
                operation="place_equity_order",
                status=OperationStatus.REJECTED,
                observed_at=self._now(),
                received_at=self._now(),
                accepted=False,
                message="injected broker rejection",
            )
        if fault is FakeFault.PLACE_UNKNOWN_BEFORE_ACCEPT:
            raise BrokerUnknownSubmission(
                "injected unknown submission; broker acceptance cannot be excluded",
                detail={"client_ref_id": request.client_ref_id},
            )
        if fault not in {None, FakeFault.PLACE_UNKNOWN_AFTER_ACCEPT}:
            raise BrokerContractViolation(f"invalid {fault.value} fault for place")

        order = self._create_order(request)
        self._orders[order.broker_order_id] = order
        if fault is FakeFault.PLACE_UNKNOWN_AFTER_ACCEPT:
            raise BrokerUnknownSubmission(
                "injected response loss after broker acceptance",
                detail={"client_ref_id": request.client_ref_id},
            )
        return BrokerOperationResult(
            operation="place_equity_order",
            status=OperationStatus.ACKNOWLEDGED,
            observed_at=self._now(),
            received_at=self._now(),
            accepted=True,
            message="fake broker acknowledged submission; this is not fill evidence",
            order=order,
        )

    def cancel_equity_order(
        self,
        account_masked: str,
        broker_order_id: str,
        *,
        explicit_confirmation: str | None = None,
    ) -> BrokerOperationResult:
        self.calls.append((self.CANCEL, broker_order_id))
        self._assert_account(account_masked)
        order_id = str(broker_order_id).strip()
        if order_id not in self._orders:
            raise BrokerContractViolation("order is not known to fake broker")
        if self._require_confirmation:
            required = self.required_cancel_confirmation(order_id)
            if explicit_confirmation != required:
                raise BrokerContractViolation("exact cancel confirmation is required")
        fault = self._next_fault(self.CANCEL)
        if fault is FakeFault.AUTHENTICATION:
            raise BrokerAuthenticationError("injected authentication failure")
        if fault is FakeFault.CANCEL_REJECTED:
            return BrokerOperationResult(
                operation="cancel_equity_order",
                status=OperationStatus.REJECTED,
                observed_at=self._now(),
                received_at=self._now(),
                accepted=False,
                message="injected cancel rejection",
                order=self._orders[order_id],
            )
        if fault is FakeFault.CANCEL_FILL_RACE:
            order = self._orders[order_id]
            remaining = order.requested_quantity - order.cumulative_filled_quantity
            if remaining > 0:
                order = self.fill_order(order_id, remaining, self._fill_price(order))
            return BrokerOperationResult(
                operation="cancel_equity_order",
                status=OperationStatus.REJECTED,
                observed_at=self._now(),
                received_at=self._now(),
                accepted=False,
                message="fill won the cancel race",
                order=order,
            )
        if fault is not None:
            raise BrokerContractViolation(f"invalid {fault.value} fault for cancel")
        now = self._now()
        pending = replace(
            self._orders[order_id],
            state=BrokerOrderState.PENDING_CANCELLED,
            broker_updated_at=now,
            received_at=now,
        )
        self._orders[order_id] = pending
        return BrokerOperationResult(
            operation="cancel_equity_order",
            status=OperationStatus.PENDING_CANCEL,
            observed_at=now,
            received_at=now,
            accepted=True,
            message="cancel request accepted; cancellation is not yet conclusive",
            order=pending,
        )

    def fill_order(
        self,
        broker_order_id: str,
        quantity: Decimal | int | str,
        price: Decimal | int | str,
    ) -> OrderSnapshot:
        order = self._orders[broker_order_id]
        fill_quantity = positive_decimal(quantity, field="fill_quantity")
        remaining = order.requested_quantity - order.cumulative_filled_quantity
        if fill_quantity > remaining:
            raise ValueError("fill quantity exceeds remaining order quantity")
        self._fill_counter += 1
        now = self._now()
        fill = FillSnapshot(
            fill_id=f"fake-fill-{self._fill_counter:04d}",
            quantity=fill_quantity,
            price=positive_decimal(price, field="fill_price"),
            executed_at=now,
        )
        cumulative = order.cumulative_filled_quantity + fill_quantity
        state = (
            BrokerOrderState.FILLED
            if cumulative == order.requested_quantity
            else BrokerOrderState.PARTIALLY_FILLED
        )
        updated = replace(
            order,
            state=state,
            cumulative_filled_quantity=cumulative,
            broker_updated_at=now,
            received_at=now,
            fills=order.fills + (fill,),
        )
        self._orders[broker_order_id] = updated
        return updated

    def settle_cancel(self, broker_order_id: str) -> OrderSnapshot:
        order = self._orders[broker_order_id]
        if order.state is not BrokerOrderState.PENDING_CANCELLED:
            raise BrokerContractViolation("order has no pending cancel request")
        now = self._now()
        state = (
            BrokerOrderState.PARTIALLY_FILLED_REST_CANCELLED
            if order.cumulative_filled_quantity > 0
            else BrokerOrderState.CANCELLED
        )
        updated = replace(
            order,
            state=state,
            broker_updated_at=now,
            received_at=now,
        )
        self._orders[broker_order_id] = updated
        return updated

    @staticmethod
    def required_cancel_confirmation(broker_order_id: str) -> str:
        return f"CONFIRM CANCEL {broker_order_id}"

    def _assert_review(
        self,
        request: OrderRequest,
        review: ReviewReceipt,
        explicit_confirmation: str | None,
    ) -> None:
        self._assert_account(request.account_masked)
        if not review.broker_bound:
            raise BrokerContractViolation("review receipt is not broker-bound")
        if review.request.exact_tuple != request.exact_tuple:
            raise BrokerContractViolation("review does not match the exact order tuple")
        if review.expired_at(self._now()):
            raise BrokerContractViolation("review has expired")
        required = review.required_confirmation_phrase
        if required is not None and explicit_confirmation != required:
            raise BrokerContractViolation("exact review confirmation is required")

    def _create_order(self, request: OrderRequest) -> OrderSnapshot:
        self._order_counter += 1
        now = self._now()
        return OrderSnapshot(
            broker_order_id=f"fake-order-{self._order_counter:04d}",
            account_masked=request.account_masked,
            symbol=request.symbol,
            side=request.side,
            order_type=request.order_type,
            state=BrokerOrderState.CONFIRMED,
            requested_quantity=Decimal(request.quantity),
            cumulative_filled_quantity=Decimal("0"),
            market_hours=request.market_hours,
            time_in_force=request.time_in_force,
            broker_updated_at=now,
            received_at=now,
            limit_price=request.limit_price,
            stop_price=request.stop_price,
            client_ref_id=request.client_ref_id,
            fills=(),
        )

    def _assert_account(self, account_masked: str) -> None:
        if account_masked != self._snapshot.account_masked:
            raise BrokerContractViolation("account does not match fake broker binding")

    def _next_fault(self, operation: str) -> FakeFault | None:
        return self._faults[operation].popleft() if self._faults[operation] else None

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("fake clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _fill_price(order: OrderSnapshot) -> Decimal:
        return order.limit_price or order.stop_price or Decimal("1")


__all__ = ["FakeBrokerClient", "FakeFault", "FakeReadError"]
