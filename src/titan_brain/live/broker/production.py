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
from typing import Callable, Protocol, TypeAlias, runtime_checkable
from uuid import UUID

from ..models import BrokerOrderState
from ..money import whole_shares
from .base import (
    AccountSnapshot,
    BrokerCapabilities,
    BrokerCapabilityError,
    BrokerContractViolation,
    BrokerMutationBlocked,
    BrokerNativeReview,
    BrokerOperationResult,
    BrokerUnknownSubmission,
    ClientRefLookupResult,
    ClientRefRecoverySource,
    LocalPreflightDecision,
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
    snapshot_token: str | None
    page_id: str
    orders: tuple[OrderSnapshot, ...]
    active_order_count: int
    observed_at: datetime
    received_at: datetime
    next_cursor: str | None
    collection_id: str | None = None
    page_index: int = 0
    page_complete: bool = True
    provider_watermark: str | None = None

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
        token = (
            _required(self.snapshot_token, "snapshot_token")
            if self.snapshot_token is not None
            else None
        )
        collection = (
            _required(self.collection_id, "collection_id")
            if self.collection_id is not None
            else None
        )
        if (token is None) == (collection is None):
            raise ValueError(
                "page requires exactly one provider snapshot token or collection ID"
            )
        if collection is not None and not re.fullmatch(r"[0-9a-f]{64}", collection):
            raise ValueError("collection_id must be a nonsecret SHA-256 receipt")
        if isinstance(self.page_index, bool) or not isinstance(self.page_index, int) or self.page_index < 0:
            raise ValueError("page_index must be a nonnegative integer")
        if not isinstance(self.page_complete, bool):
            raise ValueError("page_complete must be boolean")
        if self.provider_watermark is not None:
            object.__setattr__(
                self,
                "provider_watermark",
                _required(self.provider_watermark, "provider_watermark"),
            )
        object.__setattr__(self, "snapshot_token", token)
        object.__setattr__(self, "collection_id", collection)
        object.__setattr__(self, "page_id", _required(self.page_id, "page_id"))
        object.__setattr__(self, "orders", orders)
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "received_at", received)


@dataclass(frozen=True)
class ProviderSnapshot:
    """Account facts pinned to a genuine provider snapshot/version token."""

    snapshot: AccountSnapshot
    snapshot_token: str
    snapshot_token_source: str

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
        object.__setattr__(
            self,
            "snapshot_token_source",
            _required(self.snapshot_token_source, "snapshot_token_source"),
        )


# Backward-compatible name for existing provider implementations.  The strong
# type remains explicit and is never used for locally generated hashes.
ProductionAccountBase = ProviderSnapshot


@dataclass(frozen=True)
class CollectedObservation:
    """Non-atomic account collection with honest request provenance.

    ``collection_id`` is a local correlation/hash receipt.  It identifies one
    collection attempt but never claims an immutable provider snapshot.
    """

    snapshot: AccountSnapshot
    collection_id: str
    request_started_at: datetime
    request_completed_at: datetime
    order_event_watermark: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, AccountSnapshot):
            raise ValueError("collected observation must contain AccountSnapshot")
        collection = _required(self.collection_id, "collection_id")
        if not re.fullmatch(r"[0-9a-f]{64}", collection):
            raise ValueError("collection_id must be a nonsecret SHA-256 receipt")
        started = _utc(self.request_started_at, "request_started_at")
        completed = _utc(self.request_completed_at, "request_completed_at")
        if completed < started:
            raise ValueError("collection completion cannot precede request start")
        if self.snapshot.received_at < started - _PROVIDER_CLOCK_SKEW:
            raise ValueError("account receipt predates its collection request")
        if self.snapshot.received_at > completed + _PROVIDER_CLOCK_SKEW:
            raise ValueError("account receipt follows collection completion")
        if (
            self.snapshot.risk_evidence_as_of is not None
            and self.snapshot.risk_evidence_as_of
            > self.snapshot.observed_at + _PROVIDER_CLOCK_SKEW
        ):
            raise ValueError("risk fact cannot follow its provider observation")
        watermark = (
            _required(self.order_event_watermark, "order_event_watermark")
            if self.order_event_watermark is not None
            else None
        )
        object.__setattr__(self, "collection_id", collection)
        object.__setattr__(self, "request_started_at", started)
        object.__setattr__(self, "request_completed_at", completed)
        object.__setattr__(self, "order_event_watermark", watermark)


AccountEvidence: TypeAlias = ProviderSnapshot | CollectedObservation


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

    def get_account_base(self, exact_account_id: str) -> AccountEvidence: ...

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
    ) -> BrokerNativeReview | LocalPreflightDecision: ...

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
        first_evidence = self._get_account_evidence(account_masked)
        first, first_watermarks = self._assemble_account_evidence(first_evidence)
        if isinstance(first_evidence, ProviderSnapshot):
            return first

        # A collection identifier is not an atomic broker snapshot. Re-read
        # all material account/order state around the critical boundary and
        # accept only a stable second collection. Any motion is unresolved and
        # causes the caller to reconcile again rather than manufacture a token.
        second_evidence = self._get_account_evidence(account_masked)
        if not isinstance(second_evidence, CollectedObservation):
            raise BrokerContractViolation(
                "production transport changed account consistency models"
            )
        if (
            second_evidence.request_started_at
            < first_evidence.request_completed_at - _PROVIDER_CLOCK_SKEW
        ):
            raise BrokerContractViolation("collected account requests overlapped")
        second, second_watermarks = self._assemble_account_evidence(second_evidence)
        if (
            self._material_snapshot(first) != self._material_snapshot(second)
            or first_watermarks != second_watermarks
            or (
                first_evidence.order_event_watermark is not None
                and second_evidence.order_event_watermark is not None
                and first_evidence.order_event_watermark
                != second_evidence.order_event_watermark
            )
        ):
            raise BrokerContractViolation(
                "collected account state moved during non-atomic reconciliation"
            )
        return second

    def _get_account_evidence(self, account_masked: str) -> AccountEvidence:
        call_started_at = _utc(self._clock(), "clock")
        evidence = self.transport.get_account_base(self.descriptor.exact_account_id)
        call_completed_at = _utc(self._clock(), "clock")
        if not isinstance(evidence, (ProviderSnapshot, CollectedObservation)):
            raise BrokerContractViolation("production account evidence is not normalized")
        self._validate_read_receipt(
            evidence.snapshot.received_at,
            operation="get_account_base",
            call_started_at=call_started_at,
            call_completed_at=call_completed_at,
        )
        if isinstance(evidence, CollectedObservation) and (
            evidence.request_started_at < call_started_at - _PROVIDER_CLOCK_SKEW
            or evidence.request_completed_at
            > call_completed_at + _PROVIDER_CLOCK_SKEW
        ):
            raise BrokerContractViolation(
                "collected account request timestamps escape the transport call"
            )
        base = evidence.snapshot
        if base.account_masked != account_masked:
            raise BrokerContractViolation("production account base changed account identity")
        if base.equity_orders or base.option_order_count or base.advanced_order_count:
            raise BrokerContractViolation("account base must not bypass paginated order assembly")
        return evidence

    def _assemble_account_evidence(
        self, evidence: AccountEvidence
    ) -> tuple[AccountSnapshot, tuple[tuple[str, tuple[str, ...]], ...]]:
        base = evidence.snapshot
        collected = {
            family: self._collect_family(family, evidence=evidence)
            for family in OrderFamily
            if not self.capabilities.order_coverage.family(family).account_family_disabled
        }
        all_equity_orders = tuple(
            order
            for family in (OrderFamily.STANDARD_EQUITY, OrderFamily.ADVANCED_EQUITY)
            for order in collected.get(
                family, ((), 0, base.observed_at, base.received_at, ())
            )[0]
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
        snapshot = replace(
            base,
            observed_at=observed,
            received_at=received,
            equity_orders=all_equity_orders,
            option_order_count=collected.get(
                OrderFamily.OPTION, ((), 0, observed, received, ())
            )[1],
            advanced_order_count=collected.get(
                OrderFamily.ADVANCED_EQUITY, ((), 0, observed, received, ())
            )[1],
            standard_equity_orders_complete=coverage.family_complete(
                OrderFamily.STANDARD_EQUITY
            ),
            option_orders_complete=coverage.family_complete(OrderFamily.OPTION),
            advanced_orders_complete=coverage.family_complete(
                OrderFamily.ADVANCED_EQUITY
            ),
        )
        watermarks = tuple(
            sorted(
                (
                    family.value,
                    tuple(item[4]),
                )
                for family, item in collected.items()
            )
        )
        return snapshot, watermarks

    @staticmethod
    def _material_snapshot(snapshot: AccountSnapshot) -> tuple[object, ...]:
        def order_fact(order: OrderSnapshot) -> tuple[object, ...]:
            return (
                order.broker_order_id,
                order.account_masked,
                order.symbol,
                order.side,
                order.order_type,
                order.state,
                order.requested_quantity,
                order.cumulative_filled_quantity,
                order.market_hours,
                order.time_in_force,
                order.limit_price,
                order.stop_price,
                order.client_ref_id,
                order.broker_updated_at,
                order.fills,
            )

        return (
            snapshot.account_masked,
            snapshot.account_state,
            snapshot.account_type,
            snapshot.funds,
            tuple(snapshot.equity_positions),
            tuple(sorted((order_fact(order) for order in snapshot.equity_orders))),
            snapshot.option_position_count,
            snapshot.option_order_count,
            snapshot.advanced_order_count,
            snapshot.standard_equity_positions_complete,
            snapshot.standard_equity_orders_complete,
            snapshot.option_positions_complete,
            snapshot.option_orders_complete,
            snapshot.advanced_orders_complete,
            snapshot.daily_realized_pnl,
            snapshot.weekly_realized_pnl,
            snapshot.peak_equity,
            snapshot.risk_evidence_authoritative,
            snapshot.risk_evidence_source,
        )

    def lookup_equity_orders_by_client_ref(
        self, account_masked: str, client_refs: tuple[str, ...]
    ) -> ClientRefLookupResult:
        self._assert_account(account_masked)
        requested = self._client_refs(client_refs)
        call_started_at = _utc(self._clock(), "clock")
        contract = self.capabilities.order_coverage
        if not contract.supports_exact_client_ref_recovery:
            raise BrokerCapabilityError("transport has no authoritative exact client-ref recovery")

        if contract.client_ref_recovery_source is ClientRefRecoverySource.DEDICATED_LOOKUP:
            result = self.transport.lookup_equity_orders_by_client_ref(
                self.descriptor.exact_account_id, requested
            )
            call_completed_at = _utc(self._clock(), "clock")
            return self._validate_lookup(
                result,
                requested,
                call_started_at=call_started_at,
                call_completed_at=call_completed_at,
            )

        if contract.client_ref_recovery_source is not ClientRefRecoverySource.EXHAUSTIVE_ORDER_HISTORY:
            raise BrokerCapabilityError("unsupported client-ref recovery source")
        snapshot = self.get_account_snapshot(account_masked)
        orders = list(snapshot.equity_orders)
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
            confirmed_absent_client_refs=(
                tuple(ref for ref in requested if ref not in by_ref)
                if contract.negative_client_ref_results_authoritative
                else ()
            ),
            not_seen_yet_client_refs=(
                ()
                if contract.negative_client_ref_results_authoritative
                else tuple(ref for ref in requested if ref not in by_ref)
            ),
            observed_at=snapshot.observed_at,
            received_at=snapshot.received_at,
            complete=True,
        )
        call_completed_at = _utc(self._clock(), "clock")
        return self._validate_lookup(
            result,
            requested,
            call_started_at=call_started_at,
            call_completed_at=call_completed_at,
        )

    def review_equity_order(self, request: OrderRequest) -> ReviewReceipt:
        self._assert_request(request)
        call_started_at = _utc(self._clock(), "clock")
        review = self.transport.review_equity_order(
            self.descriptor.exact_account_id, request
        )
        call_completed_at = _utc(self._clock(), "clock")
        if not isinstance(review, (BrokerNativeReview, LocalPreflightDecision)):
            raise BrokerContractViolation(
                "production review must declare broker-native or local-preflight provenance"
            )
        if review.request.exact_tuple != request.exact_tuple:
            raise BrokerContractViolation("production review changed the exact order tuple")
        if isinstance(review, BrokerNativeReview):
            if (
                review.required_confirmation_phrase is None
                and self.capabilities.review_requires_explicit_confirmation
            ):
                raise BrokerContractViolation(
                    "connector requires confirmation but supplied no exact phrase"
                )
            if (
                review.required_confirmation_phrase is not None
                and not self.capabilities.review_requires_explicit_confirmation
            ):
                raise BrokerContractViolation(
                    "broker review requires confirmation contrary to transport capabilities"
                )
        else:
            if self.capabilities.review_requires_explicit_confirmation:
                raise BrokerContractViolation(
                    "local preflight cannot replace mandatory broker review or confirmation"
                )
            if not all(
                (
                    self.capabilities.supports_daemon_writes,
                    self.capabilities.supports_unattended_writes,
                )
            ):
                raise BrokerContractViolation(
                    "local preflight requires a verified direct-write provider contract"
                )
        now = _utc(self._clock(), "clock")
        if (
            review.reviewed_at > now + _PROVIDER_CLOCK_SKEW
            or review.received_at < call_started_at - _PROVIDER_CLOCK_SKEW
            or review.received_at > call_completed_at + _PROVIDER_CLOCK_SKEW
            or review.expired_at(now)
        ):
            raise BrokerContractViolation("production review timestamp is unusable")
        review_id = self._review_identity(review)
        if review_id in self._issued_reviews:
            raise BrokerContractViolation("production transport reused a review identity")
        self._issued_reviews[review_id] = review
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
        review_id = self._review_identity(review)
        if (
            self._issued_reviews.get(review_id) != review
            or review.request.exact_tuple != request.exact_tuple
            or review.reviewed_at > now + timedelta(seconds=2)
            or review.expired_at(now)
        ):
            raise BrokerContractViolation("place requires the exact unexpired review decision")
        if (
            isinstance(review, LocalPreflightDecision)
            and self.capabilities.review_requires_explicit_confirmation
        ):
            raise BrokerMutationBlocked(
                "local preflight cannot replace mandatory broker confirmation"
            )
        required_phrase = review.required_confirmation_phrase
        if required_phrase is not None and explicit_confirmation != required_phrase:
            raise BrokerMutationBlocked("place requires the exact review confirmation phrase")
        # Consume before crossing the side-effect boundary.  Even an exception
        # may mean the broker accepted the order, so this receipt is one-shot.
        del self._issued_reviews[review_id]
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
        self, family: OrderFamily, *, evidence: AccountEvidence
    ) -> tuple[tuple[OrderSnapshot, ...], int, datetime, datetime, tuple[str, ...]]:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        seen_pages: set[str] = set()
        orders: list[OrderSnapshot] = []
        active_count = 0
        observed_at: datetime | None = None
        received_at: datetime | None = None
        watermarks: list[str] = []
        for expected_index in range(self.descriptor.maximum_order_pages_per_family):
            call_started_at = _utc(self._clock(), "clock")
            page = self.transport.list_order_family_page(
                self.descriptor.exact_account_id, family, cursor
            )
            call_completed_at = _utc(self._clock(), "clock")
            if not isinstance(page, OrderFamilyPage):
                raise BrokerContractViolation("order-family page is not normalized")
            self._validate_read_receipt(
                page.received_at,
                operation="list_order_family_page",
                call_started_at=call_started_at,
                call_completed_at=call_completed_at,
            )
            if page.account_masked != self.capabilities.account_masked or page.family is not family:
                raise BrokerContractViolation("order-family page changed account or family")
            if page.page_index != expected_index:
                raise BrokerContractViolation(
                    "order-family pagination skipped or reordered a page"
                )
            if not page.page_complete:
                raise BrokerContractViolation("order-family provider reported an incomplete page")
            if isinstance(evidence, ProviderSnapshot):
                if (
                    page.snapshot_token != evidence.snapshot_token
                    or page.collection_id is not None
                ):
                    raise BrokerContractViolation(
                        "order-family pagination crossed provider snapshot tokens"
                    )
            elif (
                page.collection_id != evidence.collection_id
                or page.snapshot_token is not None
            ):
                raise BrokerContractViolation(
                    "order-family page changed its collected-observation binding"
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
            if page.provider_watermark is not None:
                watermarks.append(page.provider_watermark)
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
        return tuple(orders), active_count, observed_at, received_at, tuple(watermarks)

    def _validate_lookup(
        self,
        result: ClientRefLookupResult,
        requested: tuple[str, ...],
        *,
        call_started_at: datetime,
        call_completed_at: datetime,
    ) -> ClientRefLookupResult:
        if not isinstance(result, ClientRefLookupResult):
            raise BrokerContractViolation("client-ref lookup is not normalized")
        if (
            result.account_masked != self.capabilities.account_masked
            or result.requested_client_refs != requested
            or result.complete is not True
        ):
            raise BrokerContractViolation("client-ref lookup was incomplete or changed its request")
        if (
            result.confirmed_absent_client_refs
            and not self.capabilities.order_coverage.negative_client_ref_results_authoritative
        ):
            raise BrokerContractViolation(
                "provider claimed authoritative absence without negative-result semantics"
            )
        if any(
            order.broker_updated_at > result.observed_at + _PROVIDER_CLOCK_SKEW
            for order in result.found_orders
        ):
            raise BrokerContractViolation("client-ref lookup contains future broker facts")
        if any(order.received_at > result.received_at for order in result.found_orders):
            raise BrokerContractViolation("client-ref lookup contains post-receipt orders")
        self._validate_read_receipt(
            result.received_at,
            operation="lookup_equity_orders_by_client_ref",
            call_started_at=call_started_at,
            call_completed_at=call_completed_at,
        )
        if any(
            fill.executed_at > result.observed_at + _PROVIDER_CLOCK_SKEW
            for order in result.found_orders
            for fill in order.fills
        ):
            raise BrokerContractViolation("client-ref lookup contains future fill facts")
        return result

    @staticmethod
    def _review_identity(review: ReviewReceipt) -> str:
        if isinstance(review, BrokerNativeReview):
            return f"broker:{review.broker_review_id}"
        if isinstance(review, LocalPreflightDecision):
            return f"local:{review.decision_id}"
        raise BrokerContractViolation(
            "production review must declare broker-native or local-preflight provenance"
        )

    @staticmethod
    def _validate_read_receipt(
        received_at: datetime,
        *,
        operation: str,
        call_started_at: datetime,
        call_completed_at: datetime,
    ) -> None:
        receipt = _utc(received_at, "received_at")
        if call_completed_at < call_started_at:
            raise BrokerContractViolation(
                f"clock regressed during {operation}"
            )
        if receipt < call_started_at - _PROVIDER_CLOCK_SKEW:
            raise BrokerContractViolation(
                f"{operation} receipt predates its transport call"
            )
        if receipt > call_completed_at + _PROVIDER_CLOCK_SKEW:
            raise BrokerContractViolation(f"{operation} receipt is in the future")

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
    "CollectedObservation",
    "OrderFamilyPage",
    "ProviderSnapshot",
    "ProductionAccountBase",
    "ProductionTransport",
    "ProductionTransportDescriptor",
    "SupportedProductionBrokerAdapter",
]
