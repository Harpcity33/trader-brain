"""Supported production-broker boundary with exhaustive read verification.

This module is intentionally transport-agnostic.  A provider integration must
obtain its own supported authorization and implement ``ProductionTransport``;
the adapter never discovers credentials, calls the attended Robinhood tools,
or turns configuration booleans into write authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
import re
from typing import Callable, Protocol, runtime_checkable
from uuid import UUID

from ..models import BrokerOrderState
from ..money import whole_shares
from .base import (
    AccountSnapshot,
    BrokerCapabilities,
    BrokerCapabilityError,
    BrokerContractViolation,
    BrokerMutationBlocked,
    BrokerOperationResult,
    BrokerUnknownSubmission,
    ClientRefLookupResult,
    ClientRefRecoverySource,
    OperationStatus,
    OrderFamily,
    OrderRequest,
    OrderSnapshot,
    ReviewReceipt,
)


def _required(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} is required")
    return normalized


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


_PROVIDER_CLOCK_SKEW = timedelta(seconds=2)


@dataclass(frozen=True)
class OrderFamilyPage:
    """One normalized provider page; an empty continuation cursor is valid."""

    account_masked: str
    family: OrderFamily
    snapshot_token: str
    page_id: str
    orders: tuple[OrderSnapshot, ...]
    active_order_count: int
    observed_at: datetime
    received_at: datetime
    next_cursor: str | None

    def __post_init__(self) -> None:
        account = _required(self.account_masked, "account_masked")
        family = OrderFamily(self.family)
        orders = tuple(self.orders)
        if any(not isinstance(order, OrderSnapshot) for order in orders):
            raise ValueError("orders must contain OrderSnapshot records")
        if any(order.account_masked != account for order in orders):
            raise ValueError("page orders must match the page account")
        if family is OrderFamily.OPTION and orders:
            raise ValueError("option orders cannot be represented as equity OrderSnapshot records")
        if isinstance(self.active_order_count, bool) or self.active_order_count < 0:
            raise ValueError("active_order_count must be a nonnegative integer")
        if not isinstance(self.active_order_count, int):
            raise ValueError("active_order_count must be a nonnegative integer")
        if family is not OrderFamily.OPTION:
            active = sum(not order.state.terminal for order in orders)
            if active != self.active_order_count:
                raise ValueError("every active equity-family order must be normalized on its page")
        observed = _utc(self.observed_at, "observed_at")
        received = _utc(self.received_at, "received_at")
        if received < observed:
            raise ValueError("page receipt cannot precede provider observation")
        if any(order.received_at > received for order in orders):
            raise ValueError("order receipt cannot follow its containing page receipt")
        if any(
            order.broker_updated_at > observed + _PROVIDER_CLOCK_SKEW
            for order in orders
        ):
            raise ValueError("order fact cannot follow its provider observation")
        if any(
            fill.executed_at > observed + _PROVIDER_CLOCK_SKEW
            for order in orders
            for fill in order.fills
        ):
            raise ValueError("fill fact cannot follow its provider observation")
        if self.next_cursor is not None and not isinstance(self.next_cursor, str):
            raise ValueError("next_cursor must be a string or None")
        object.__setattr__(self, "account_masked", account)
        object.__setattr__(self, "family", family)
        object.__setattr__(
            self, "snapshot_token", _required(self.snapshot_token, "snapshot_token")
        )
        object.__setattr__(self, "page_id", _required(self.page_id, "page_id"))
        object.__setattr__(self, "orders", orders)
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "received_at", received)


@dataclass(frozen=True)
class ProductionAccountBase:
    """Account facts pinned to one provider-issued immutable snapshot token."""

    snapshot: AccountSnapshot
    snapshot_token: str

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, AccountSnapshot):
            raise ValueError("production account base must contain AccountSnapshot")
        if (
            self.snapshot.risk_evidence_as_of is not None
            and self.snapshot.risk_evidence_as_of
            > self.snapshot.observed_at + _PROVIDER_CLOCK_SKEW
        ):
            raise ValueError("risk fact cannot follow its provider observation")
        object.__setattr__(
            self, "snapshot_token", _required(self.snapshot_token, "snapshot_token")
        )


@dataclass(frozen=True)
class ProductionTransportDescriptor:
    """Private account binding and audited capabilities for one transport."""

    transport_id: str
    exact_account_id: str = field(repr=False)
    account_binding_fingerprint: str
    authorization_binding_id: str
    capabilities: BrokerCapabilities
    maximum_order_pages_per_family: int = 1_000

    def __post_init__(self) -> None:
        transport_id = _required(self.transport_id, "transport_id")
        account_id = _required(self.exact_account_id, "exact_account_id")
        account_fingerprint = _required(
            self.account_binding_fingerprint, "account_binding_fingerprint"
        )
        authorization_binding = _required(
            self.authorization_binding_id, "authorization_binding_id"
        )
        if not re.fullmatch(r"[0-9a-f]{64}", account_fingerprint):
            raise ValueError(
                "account_binding_fingerprint must be a nonreversible 256-bit receipt"
            )
        if not re.fullmatch(r"[0-9a-f]{64}", authorization_binding):
            raise ValueError(
                "authorization_binding_id must be a nonsecret 256-bit receipt"
            )
        if not isinstance(self.capabilities, BrokerCapabilities):
            raise ValueError("capabilities must be BrokerCapabilities")
        if (
            account_id == self.capabilities.account_masked
            or "•" in account_id
            or account_id.startswith("****")
        ):
            raise ValueError("production transport requires a private exact broker account ID")
        if (
            isinstance(self.maximum_order_pages_per_family, bool)
            or not isinstance(self.maximum_order_pages_per_family, int)
            or self.maximum_order_pages_per_family <= 0
        ):
            raise ValueError("maximum_order_pages_per_family must be positive")
        object.__setattr__(self, "transport_id", transport_id)
        object.__setattr__(self, "exact_account_id", account_id)
        object.__setattr__(
            self, "account_binding_fingerprint", account_fingerprint
        )
        object.__setattr__(
            self, "authorization_binding_id", authorization_binding
        )


@runtime_checkable
class ProductionTransport(Protocol):
    """Provider-supported transport implemented outside the strategy core."""

    @property
    def descriptor(self) -> ProductionTransportDescriptor: ...

    def release_components(self) -> tuple[tuple[str, object, tuple[str, ...]], ...]:
        """Enumerate injected executable dependencies retained by the transport."""
        ...

    def get_account_base(self, exact_account_id: str) -> ProductionAccountBase: ...

    def list_order_family_page(
        self,
        exact_account_id: str,
        family: OrderFamily,
        cursor: str | None,
    ) -> OrderFamilyPage: ...

    def lookup_equity_orders_by_client_ref(
        self,
        exact_account_id: str,
        client_refs: tuple[str, ...],
    ) -> ClientRefLookupResult: ...

    def review_equity_order(
        self, exact_account_id: str, request: OrderRequest
    ) -> ReviewReceipt: ...

    def place_equity_order(
        self,
        exact_account_id: str,
        request: OrderRequest,
        *,
        review: ReviewReceipt,
        explicit_confirmation: str | None = None,
    ) -> BrokerOperationResult: ...

    def cancel_equity_order(
        self,
        exact_account_id: str,
        broker_order_id: str,
        *,
        explicit_confirmation: str | None = None,
    ) -> BrokerOperationResult: ...


class SupportedProductionBrokerAdapter:
    """Fail-closed adapter for a separately authorized production transport."""

    def __init__(
        self,
        transport: ProductionTransport,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(transport, ProductionTransport):
            raise TypeError("transport does not implement ProductionTransport")
        descriptor = transport.descriptor
        if not isinstance(descriptor, ProductionTransportDescriptor):
            raise TypeError("transport descriptor is not normalized")
        if not descriptor.capabilities.daemon_transport_configured:
            raise BrokerCapabilityError("production transport is not daemon-configured")
        self.transport = transport
        self.descriptor = descriptor
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._issued_reviews: dict[str, ReviewReceipt] = {}

    @property
    def capabilities(self) -> BrokerCapabilities:
        return self.descriptor.capabilities

    def get_account_snapshot(self, account_masked: str) -> AccountSnapshot:
        self._assert_account(account_masked)
        base_envelope = self.transport.get_account_base(
            self.descriptor.exact_account_id
        )
        if not isinstance(base_envelope, ProductionAccountBase):
            raise BrokerContractViolation("production account base is not normalized")
        base = base_envelope.snapshot
        if base.account_masked != account_masked:
            raise BrokerContractViolation("production account base changed account identity")
        if base.equity_orders or base.option_order_count or base.advanced_order_count:
            raise BrokerContractViolation("account base must not bypass paginated order assembly")

        collected = {
            family: self._collect_family(
                family, snapshot_token=base_envelope.snapshot_token
            )
            for family in OrderFamily
            if not self.capabilities.order_coverage.family(family).account_family_disabled
        }
        all_equity_orders = tuple(
            order
            for family in (OrderFamily.STANDARD_EQUITY, OrderFamily.ADVANCED_EQUITY)
            for order in collected.get(family, ((), 0, base.observed_at, base.received_at))[0]
        )
        order_ids = tuple(order.broker_order_id for order in all_equity_orders)
        if len(order_ids) != len(set(order_ids)):
            raise BrokerContractViolation("order families returned duplicate broker order IDs")

        # The snapshot is only as fresh as its earliest required family page;
        # the latest receipt records when exhaustive collection completed.
        observed = min(
            [base.observed_at]
            + [item[2] for item in collected.values()]
        )
        received = max(
            [base.received_at]
            + [item[3] for item in collected.values()]
        )
        coverage = self.capabilities.order_coverage
        return replace(
            base,
            observed_at=observed,
            received_at=received,
            equity_orders=all_equity_orders,
            option_order_count=collected.get(
                OrderFamily.OPTION, ((), 0, observed, received)
            )[1],
            advanced_order_count=collected.get(
                OrderFamily.ADVANCED_EQUITY, ((), 0, observed, received)
            )[1],
            standard_equity_orders_complete=coverage.family_complete(
                OrderFamily.STANDARD_EQUITY
            ),
            option_orders_complete=coverage.family_complete(OrderFamily.OPTION),
            advanced_orders_complete=coverage.family_complete(
                OrderFamily.ADVANCED_EQUITY
            ),
        )

    def lookup_equity_orders_by_client_ref(
        self, account_masked: str, client_refs: tuple[str, ...]
    ) -> ClientRefLookupResult:
        self._assert_account(account_masked)
        requested = self._client_refs(client_refs)
        contract = self.capabilities.order_coverage
        if not contract.supports_exact_client_ref_recovery:
            raise BrokerCapabilityError("transport has no authoritative exact client-ref recovery")

        if contract.client_ref_recovery_source is ClientRefRecoverySource.DEDICATED_LOOKUP:
            result = self.transport.lookup_equity_orders_by_client_ref(
                self.descriptor.exact_account_id, requested
            )
            return self._validate_lookup(result, requested)

        if contract.client_ref_recovery_source is not ClientRefRecoverySource.EXHAUSTIVE_ORDER_HISTORY:
            raise BrokerCapabilityError("unsupported client-ref recovery source")
        base_envelope = self.transport.get_account_base(
            self.descriptor.exact_account_id
        )
        if (
            not isinstance(base_envelope, ProductionAccountBase)
            or base_envelope.snapshot.account_masked != account_masked
        ):
            raise BrokerContractViolation(
                "production account base is not normalized for exact-ref recovery"
            )
        orders: list[OrderSnapshot] = []
        evidence_at: datetime | None = None
        received_at: datetime | None = None
        for family in (OrderFamily.STANDARD_EQUITY, OrderFamily.ADVANCED_EQUITY):
            if contract.family(family).account_family_disabled:
                continue
            family_orders, _, observed_at, family_received_at = self._collect_family(
                family, snapshot_token=base_envelope.snapshot_token
            )
            orders.extend(family_orders)
            evidence_at = (
                observed_at
                if evidence_at is None
                else min(evidence_at, observed_at)
            )
            received_at = (
                family_received_at
                if received_at is None
                else max(received_at, family_received_at)
            )
        by_ref: dict[str, OrderSnapshot] = {}
        for order in orders:
            if order.client_ref_id is None:
                continue
            if order.client_ref_id in by_ref:
                raise BrokerContractViolation("exhaustive history returned duplicate client refs")
            by_ref[order.client_ref_id] = order
        result = ClientRefLookupResult(
            account_masked=account_masked,
            requested_client_refs=requested,
            found_orders=tuple(by_ref[ref] for ref in requested if ref in by_ref),
            confirmed_absent_client_refs=tuple(ref for ref in requested if ref not in by_ref),
            observed_at=evidence_at or contract.evidence_observed_at,
            received_at=received_at or base_envelope.snapshot.received_at,
            complete=True,
        )
        return self._validate_lookup(result, requested)

    def review_equity_order(self, request: OrderRequest) -> ReviewReceipt:
        self._assert_request(request)
        review = self.transport.review_equity_order(
            self.descriptor.exact_account_id, request
        )
        if not isinstance(review, ReviewReceipt):
            raise BrokerContractViolation("production review is not normalized")
        if review.request.exact_tuple != request.exact_tuple:
            raise BrokerContractViolation("production review changed the exact order tuple")
        if not review.broker_bound or not review.broker_review_id:
            raise BrokerContractViolation("production review is not broker-bound")
        if review.required_confirmation_phrase is None and self.capabilities.review_requires_explicit_confirmation:
            raise BrokerContractViolation("connector requires confirmation but supplied no exact phrase")
        now = _utc(self._clock(), "clock")
        if review.reviewed_at > now + timedelta(seconds=2) or review.expired_at(now):
            raise BrokerContractViolation("production review timestamp is unusable")
        if review.broker_review_id in self._issued_reviews:
            raise BrokerContractViolation("production transport reused a broker review ID")
        self._issued_reviews[review.broker_review_id] = review
        return review

    def place_equity_order(
        self,
        request: OrderRequest,
        *,
        review: ReviewReceipt,
        explicit_confirmation: str | None = None,
    ) -> BrokerOperationResult:
        self._assert_request(request)
        now = _utc(self._clock(), "clock")
        if (
            not review.broker_bound
            or not review.broker_review_id
            or self._issued_reviews.get(review.broker_review_id) != review
            or review.request.exact_tuple != request.exact_tuple
            or review.reviewed_at > now + timedelta(seconds=2)
            or review.expired_at(now)
        ):
            raise BrokerContractViolation("place requires the exact unexpired broker review")
        required_phrase = review.required_confirmation_phrase
        if required_phrase is not None and explicit_confirmation != required_phrase:
            raise BrokerMutationBlocked("place requires the exact review confirmation phrase")
        # Consume before crossing the side-effect boundary.  Even an exception
        # may mean the broker accepted the order, so this receipt is one-shot.
        del self._issued_reviews[review.broker_review_id]
        call_started_at = _utc(self._clock(), "clock")
        result = self.transport.place_equity_order(
            self.descriptor.exact_account_id,
            request,
            review=review,
            explicit_confirmation=explicit_confirmation,
        )
        call_completed_at = _utc(self._clock(), "clock")
        try:
            self._validate_operation_result(
                result,
                operation="place_equity_order",
                call_started_at=call_started_at,
                call_completed_at=call_completed_at,
            )
            if result.status is OperationStatus.ACKNOWLEDGED and result.order is None:
                raise BrokerContractViolation(
                    "acknowledged place response omitted exact order evidence"
                )
            if result.order is not None:
                self._assert_order_matches_request(result.order, request)
        except BrokerContractViolation as exc:
            # Validation happens after the transport call.  A malformed response
            # can never be downgraded to known-no-accept because the request may
            # already have crossed the broker side-effect boundary.
            raise BrokerUnknownSubmission(
                "production place response was not authoritative; broker acceptance cannot be determined",
                detail={"operation": "place_equity_order", "code": exc.code},
            ) from exc
        return result

    def cancel_equity_order(
        self,
        account_masked: str,
        broker_order_id: str,
        *,
        explicit_confirmation: str | None = None,
    ) -> BrokerOperationResult:
        self._assert_account(account_masked)
        order_id = _required(broker_order_id, "broker_order_id")
        if self.capabilities.cancel_requires_explicit_confirmation and not str(
            explicit_confirmation or ""
        ).strip():
            raise BrokerMutationBlocked("cancel requires explicit user confirmation")
        call_started_at = _utc(self._clock(), "clock")
        result = self.transport.cancel_equity_order(
            self.descriptor.exact_account_id,
            order_id,
            explicit_confirmation=explicit_confirmation,
        )
        call_completed_at = _utc(self._clock(), "clock")
        try:
            self._validate_operation_result(
                result,
                operation="cancel_equity_order",
                call_started_at=call_started_at,
                call_completed_at=call_completed_at,
            )
            if result.order is not None and (
                result.order.account_masked != account_masked
                or result.order.broker_order_id != order_id
            ):
                raise BrokerContractViolation(
                    "cancel response changed account or target order"
                )
        except BrokerContractViolation as exc:
            raise BrokerUnknownSubmission(
                "production cancel response was not authoritative; broker outcome cannot be determined",
                detail={"operation": "cancel_equity_order", "code": exc.code},
            ) from exc
        return result

    def _collect_family(
        self, family: OrderFamily, *, snapshot_token: str
    ) -> tuple[tuple[OrderSnapshot, ...], int, datetime, datetime]:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        seen_pages: set[str] = set()
        orders: list[OrderSnapshot] = []
        active_count = 0
        observed_at: datetime | None = None
        received_at: datetime | None = None
        for _ in range(self.descriptor.maximum_order_pages_per_family):
            page = self.transport.list_order_family_page(
                self.descriptor.exact_account_id, family, cursor
            )
            if not isinstance(page, OrderFamilyPage):
                raise BrokerContractViolation("order-family page is not normalized")
            if page.account_masked != self.capabilities.account_masked or page.family is not family:
                raise BrokerContractViolation("order-family page changed account or family")
            if page.snapshot_token != snapshot_token:
                raise BrokerContractViolation(
                    "order-family pagination crossed provider snapshot tokens"
                )
            if page.page_id in seen_pages:
                raise BrokerContractViolation("order-family pagination repeated a page")
            seen_pages.add(page.page_id)
            orders.extend(page.orders)
            active_count += page.active_order_count
            observed_at = (
                page.observed_at
                if observed_at is None
                else min(observed_at, page.observed_at)
            )
            received_at = (
                page.received_at
                if received_at is None
                else max(received_at, page.received_at)
            )
            if page.next_cursor is None:
                break
            if page.next_cursor in seen_cursors:
                raise BrokerContractViolation("order-family pagination cursor loop")
            seen_cursors.add(page.next_cursor)
            cursor = page.next_cursor
        else:
            raise BrokerContractViolation("order-family pagination exceeded its page limit")
        order_ids = tuple(order.broker_order_id for order in orders)
        if len(order_ids) != len(set(order_ids)):
            raise BrokerContractViolation("order-family history returned duplicate order IDs")
        assert observed_at is not None and received_at is not None
        return tuple(orders), active_count, observed_at, received_at

    def _validate_lookup(
        self, result: ClientRefLookupResult, requested: tuple[str, ...]
    ) -> ClientRefLookupResult:
        if not isinstance(result, ClientRefLookupResult):
            raise BrokerContractViolation("client-ref lookup is not normalized")
        if (
            result.account_masked != self.capabilities.account_masked
            or result.requested_client_refs != requested
            or result.complete is not True
        ):
            raise BrokerContractViolation("client-ref lookup was incomplete or changed its request")
        if any(
            order.broker_updated_at > result.observed_at + _PROVIDER_CLOCK_SKEW
            for order in result.found_orders
        ):
            raise BrokerContractViolation("client-ref lookup contains future broker facts")
        if any(order.received_at > result.received_at for order in result.found_orders):
            raise BrokerContractViolation("client-ref lookup contains post-receipt orders")
        now = _utc(self._clock(), "clock")
        if result.received_at > now + _PROVIDER_CLOCK_SKEW:
            raise BrokerContractViolation("client-ref lookup receipt is in the future")
        if any(
            fill.executed_at > result.observed_at + _PROVIDER_CLOCK_SKEW
            for order in result.found_orders
            for fill in order.fills
        ):
            raise BrokerContractViolation("client-ref lookup contains future fill facts")
        return result

    def _assert_account(self, account_masked: str) -> None:
        if account_masked != self.capabilities.account_masked:
            raise BrokerContractViolation("account does not match production transport binding")

    def _assert_request(self, request: OrderRequest) -> None:
        if not isinstance(request, OrderRequest):
            raise BrokerContractViolation("order request is not normalized")
        self._assert_account(request.account_masked)

    @staticmethod
    def _client_refs(client_refs: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for value in client_refs:
            try:
                normalized.append(str(UUID(str(value))))
            except (ValueError, AttributeError) as exc:
                raise BrokerContractViolation("client-ref lookup requires UUIDs") from exc
        if len(normalized) != len(set(normalized)):
            raise BrokerContractViolation("client-ref lookup request contains duplicates")
        return tuple(normalized)

    @staticmethod
    def _validate_operation_result(
        result: BrokerOperationResult,
        *,
        operation: str,
        call_started_at: datetime,
        call_completed_at: datetime,
    ) -> None:
        if not isinstance(result, BrokerOperationResult) or result.operation != operation:
            raise BrokerContractViolation("broker operation result is not exact and normalized")
        if (
            result.observed_at < call_started_at - _PROVIDER_CLOCK_SKEW
            or result.received_at < call_started_at - _PROVIDER_CLOCK_SKEW
        ):
            raise BrokerContractViolation("broker operation result predates its transport call")
        if result.received_at > call_completed_at + _PROVIDER_CLOCK_SKEW:
            raise BrokerContractViolation("broker operation receipt is in the future")
        if result.order is not None:
            if result.order.received_at > result.received_at:
                raise BrokerContractViolation("broker order evidence follows operation receipt")
            if result.order.broker_updated_at > result.observed_at + _PROVIDER_CLOCK_SKEW:
                raise BrokerContractViolation("broker operation contains future order evidence")
            if any(
                fill.executed_at > result.observed_at + _PROVIDER_CLOCK_SKEW
                for fill in result.order.fills
            ):
                raise BrokerContractViolation("broker operation contains future fill evidence")
        if result.status in {
            OperationStatus.ACKNOWLEDGED,
            OperationStatus.PENDING_CANCEL,
            OperationStatus.CANCELLED,
        } and result.accepted is not True:
            raise BrokerContractViolation("accepted operation state must state accepted=true")
        if result.status is OperationStatus.REJECTED and result.accepted is not False:
            raise BrokerContractViolation("rejected operation must state accepted=false")
        if (
            operation == "place_equity_order"
            and result.status is OperationStatus.REJECTED
            and result.order is not None
            and (
                result.order.state
                not in {
                    BrokerOrderState.REJECTED,
                    BrokerOrderState.FAILED,
                    BrokerOrderState.VOIDED,
                    BrokerOrderState.LOCATE_FAILED,
                }
                or result.order.cumulative_filled_quantity != 0
                or bool(result.order.fills)
            )
        ):
            raise BrokerContractViolation(
                "rejected place result contains possible broker exposure"
            )

    @staticmethod
    def _assert_order_matches_request(order: OrderSnapshot, request: OrderRequest) -> None:
        actual = (
            order.account_masked,
            order.symbol,
            order.side.value,
            order.order_type.value,
            whole_shares(order.requested_quantity, field="requested_quantity"),
            order.market_hours.value,
            order.time_in_force.value,
            str(order.limit_price) if order.limit_price is not None else None,
            str(order.stop_price) if order.stop_price is not None else None,
            order.client_ref_id,
        )
        if actual != request.exact_tuple:
            raise BrokerContractViolation("broker order differs from the exact submitted tuple")


__all__ = [
    "OrderFamilyPage",
    "ProductionAccountBase",
    "ProductionTransport",
    "ProductionTransportDescriptor",
    "SupportedProductionBrokerAdapter",
]
