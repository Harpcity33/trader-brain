"""Durable, idempotent live-order submission coordinators.

This module is the only bridge from validated plans to the narrow broker
protocol.  It persists the logical order and its risk reservation before any
review, changes the intent to ``SUBMITTING`` before the mutation transport,
and treats every ambiguous outcome as possible exposure.  It never retries an
unknown submission.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
import json
from typing import Callable, Mapping, Protocol
from uuid import UUID, uuid5

from .broker import (
    AccountSnapshot,
    BrokerCapabilities,
    BrokerClient,
    BrokerError,
    BrokerOperationResult,
    BrokerSide,
    BrokerUnknownSubmission,
    EquityOrderType,
    LocalPreflightDecision,
    MarketHours,
    OperationStatus,
    OrderFamily,
    OrderRequest,
    OrderSnapshot,
    ReviewReceipt,
    TimeInForce,
)
from .authority import (
    MutationAuthority,
    MutationAuthorityDenied,
    MutationOperation,
    MutationPhase,
)
from .market_data import EvidenceDecision, MarketDataCache
from .latency import LatencyMeasurement, LatencyRecorder, LatencySpan
from .plan_freshness import (
    PlanFreshnessError,
    SymbolOpenOrder,
    SymbolPosition,
    account_exposure_fingerprint,
    evaluate_plan_freshness,
)
from .models import (
    BrokerOrder,
    BrokerOrderState,
    ExpiringPlan as DurablePlan,
    Fill,
    Incident,
    IncidentSeverity,
    IntentKind,
    IntentState,
    OrderIntent,
    OutboxMessage,
    ReservationState,
    RiskReservation,
)
from .money import whole_shares
from .plans import ExpiringPlan
from .policy import PolicyBundle
from .risk_runtime import RiskDecision, entry_lifecycle_fee_reserve
from .state import LiveStateStore, object_hash


_EXECUTION_NAMESPACE = UUID("f88ff54e-d86f-4e97-977f-b5b2d86ed995")
_MAX_KNOWN_NO_ACCEPT_CANCEL_ATTEMPTS = 3


def _start_latency(
    recorder: LatencyRecorder | None,
    stage: str,
    *,
    observed_at: datetime,
    correlation_id: str,
    metadata: Mapping[str, object],
) -> LatencySpan | None:
    """Keep observability failures outside the order state machine."""

    if recorder is None:
        return None
    try:
        return recorder.start(
            stage,
            observed_at=observed_at,
            correlation_id=correlation_id,
            metadata=metadata,
        )
    except Exception:
        return None


def _finish_latency(
    recorder: LatencyRecorder | None,
    span: LatencySpan | None,
    *,
    observed_at: datetime,
    metadata: Mapping[str, object],
) -> None:
    """Latency telemetry must never reinterpret or interrupt a broker result."""

    if recorder is None or span is None:
        return
    try:
        recorder.finish(span, observed_at=observed_at, metadata=metadata)
    except Exception:
        return


def _stop_latency(
    recorder: LatencyRecorder | None,
    span: LatencySpan | None,
    *,
    observed_at: datetime,
    metadata: Mapping[str, object],
) -> LatencyMeasurement | None:
    if recorder is None or span is None:
        return None
    try:
        return recorder.stop(span, observed_at=observed_at, metadata=metadata)
    except Exception:
        return None


def _persist_latency(
    recorder: LatencyRecorder | None,
    measurement: LatencyMeasurement | None,
) -> None:
    if recorder is None or measurement is None:
        return
    try:
        recorder.record_measurement(measurement)
    except Exception:
        return


def _is_exact_place_ack(
    result: object,
    request: OrderRequest,
) -> bool:
    """Recognize a broker acknowledgement without claiming a fill."""

    if (
        not isinstance(result, BrokerOperationResult)
        or result.operation != "place_equity_order"
        or result.status is not OperationStatus.ACKNOWLEDGED
        or result.accepted is not True
        or result.order is None
    ):
        return False
    order = result.order
    try:
        checks = (
            order.account_masked == request.account_masked,
            order.symbol == request.symbol,
            order.side is request.side,
            order.order_type is request.order_type,
            whole_shares(order.requested_quantity) == request.quantity,
            order.market_hours is request.market_hours,
            order.time_in_force is request.time_in_force,
            order.limit_price == request.limit_price,
            order.stop_price == request.stop_price,
            order.client_ref_id == request.client_ref_id,
        )
    except Exception:
        return False
    return all(checks)


def _is_exact_cancel_ack(
    result: object,
    target: OrderSnapshot,
) -> bool:
    if (
        not isinstance(result, BrokerOperationResult)
        or result.operation != "cancel_equity_order"
        or result.accepted is not True
        or result.order is None
        or result.order.broker_order_id != target.broker_order_id
        or result.order.account_masked != target.account_masked
    ):
        return False
    return (
        result.status is OperationStatus.CANCELLED and result.order.state.terminal
    ) or (
        result.status is OperationStatus.PENDING_CANCEL
        and result.order.state is BrokerOrderState.PENDING_CANCELLED
    )


def _review_provenance_failures(
    review: ReviewReceipt,
    capabilities: BrokerCapabilities,
) -> tuple[str, ...]:
    """Keep local preflight distinct from mandatory provider review."""

    if isinstance(review, LocalPreflightDecision):
        checks = (
            (
                capabilities.supports_daemon_writes,
                "LOCAL_PREFLIGHT_DAEMON_WRITE_UNSUPPORTED",
            ),
            (
                capabilities.supports_unattended_writes,
                "LOCAL_PREFLIGHT_UNATTENDED_WRITE_UNSUPPORTED",
            ),
            (
                not capabilities.review_requires_explicit_confirmation,
                "LOCAL_PREFLIGHT_CANNOT_REPLACE_REQUIRED_CONFIRMATION",
            ),
            (
                not review.broker_bound and review.broker_review_id is None,
                "LOCAL_PREFLIGHT_FALSE_BROKER_BINDING",
            ),
        )
        return tuple(code for passed, code in checks if not passed)
    if not review.broker_bound or not review.broker_review_id:
        return ("BROKER_REVIEW_NOT_BOUND",)
    return ()


class ExecutionStatus(str, Enum):
    BLOCKED = "BLOCKED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ExecutionOutcome:
    status: ExecutionStatus
    plan_id: str
    reservation_id: str
    intent_id: str
    client_ref_id: str
    risk_reserved: bool
    replay: bool
    message: str
    failure_codes: tuple[str, ...] = ()
    broker_order_id: str | None = None


@dataclass(frozen=True)
class SafetyExecutionOutcome:
    """Result of a protection, exit, or cancellation mutation boundary.

    ``exposure_reserved`` is deliberately separate from an entry risk
    reservation.  Safety intents consume no incremental risk reservation, but
    an ambiguous or pending result must continue to reserve the affected
    exposure/order capacity until authoritative broker reconciliation.
    """

    status: ExecutionStatus
    plan_id: str
    kind: IntentKind
    intent_id: str
    client_ref_id: str
    exposure_reserved: bool
    replay: bool
    requires_reconciliation: bool
    message: str
    failure_codes: tuple[str, ...] = ()
    broker_order_id: str | None = None


class PreparedOrderPlanSealer(Protocol):
    """Persist the exact broker-local plan for one prepared durable intent.

    The callback is a data-integrity boundary, not mutation authority.  It
    must raise on denial and return ``None`` on success.  Implementations may
    be called again after a process restart while the same intent remains
    PREPARED, so sealing must be exact and idempotent.
    """

    def __call__(
        self,
        *,
        intent_id: str,
        plan_id: str,
        kind: IntentKind,
        request: OrderRequest,
    ) -> None: ...


class EntryExecutionCoordinator:
    """Submit one validated long-equity entry through a supported broker."""

    def __init__(
        self,
        *,
        policy: PolicyBundle,
        market_data: MarketDataCache,
        state: LiveStateStore,
        broker: BrokerClient,
        authority: MutationAuthority,
        plan_sealer: PreparedOrderPlanSealer | None = None,
        clock: Callable[[], datetime] | None = None,
        latency: LatencyRecorder | None = None,
    ) -> None:
        self.policy = policy
        self.market_data = market_data
        self.state = state
        self.broker = broker
        self.authority = authority
        self.plan_sealer = plan_sealer
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.latency = latency

    def _plan_freshness_failures(
        self,
        *,
        plan_bound_fingerprint: str,
        broker_snapshot: "AccountSnapshot | None",
        created_at: datetime,
        expires_at: datetime,
        now: datetime,
    ) -> tuple[str, ...]:
        """Return freshness blockers for the entry, or () if fresh. Fail closed.

        Maps the current broker AccountSnapshot to symbol-keyed exposure, then
        compares its fingerprint to the plan-bound fingerprint and checks the
        validity window. A missing snapshot, an unmappable row, or any
        PlanFreshnessError becomes a blocker (never a silent pass). Pure — no
        broker or network access.
        """
        if broker_snapshot is None:
            return ("PLAN_FRESHNESS_SNAPSHOT_UNAVAILABLE",)
        try:
            positions = tuple(
                SymbolPosition(symbol=p.symbol, quantity=int(p.quantity))
                for p in broker_snapshot.equity_positions
                if p.quantity == p.quantity.to_integral_value()
            )
            if len(positions) != len(broker_snapshot.equity_positions):
                # A fractional position cannot be represented as whole shares.
                return ("PLAN_FRESHNESS_UNMAPPABLE_POSITION",)
            open_orders = tuple(
                SymbolOpenOrder(
                    order_identity=o.broker_order_id,
                    symbol=o.symbol,
                    side="BUY" if o.side is BrokerSide.BUY else "SELL",
                    quantity=int(o.requested_quantity),
                    limit_price=o.limit_price,
                )
                for o in broker_snapshot.equity_orders
                if o.requested_quantity == o.requested_quantity.to_integral_value()
            )
            if len(open_orders) != len(broker_snapshot.equity_orders):
                return ("PLAN_FRESHNESS_UNMAPPABLE_ORDER",)
            observed = account_exposure_fingerprint(positions, open_orders)
            return evaluate_plan_freshness(
                plan_bound_fingerprint=plan_bound_fingerprint,
                observed_fingerprint=observed,
                created_at=created_at,
                expires_at=expires_at,
                now=now,
            )
        except PlanFreshnessError as exc:
            return (str(exc),)

    def submit_entry(
        self,
        *,
        plan: ExpiringPlan,
        risk_decision: RiskDecision,
        broker_snapshot: AccountSnapshot | None = None,
        plan_bound_fingerprint: str | None = None,
        now: datetime | None = None,
    ) -> ExecutionOutcome:
        """Validate, reserve, review, and submit exactly one logical entry.

        Replaying the same plan reuses deterministic aggregate IDs and UUID
        client reference.  A replay of ``SUBMITTING`` or ``UNKNOWN`` never
        calls the broker and remains reserved pending reconciliation.
        """

        current = self._aware(now or self._clock(), "now")
        reservation_id, intent_id = self._stable_ids(plan)
        client_ref_id = self._client_ref(plan)
        try:
            request = self._request_for(plan)
        except (TypeError, ValueError) as exc:
            return ExecutionOutcome(
                status=ExecutionStatus.BLOCKED,
                plan_id=plan.plan_id,
                reservation_id=reservation_id,
                intent_id=intent_id,
                client_ref_id=client_ref_id,
                risk_reserved=False,
                replay=False,
                message="invalid order request was rejected before durable state or broker access",
                failure_codes=("ORDER_REQUEST_INVALID",),
            )
        existing = self.state.row("order_intents", "intent_id", intent_id)
        if existing is not None:
            self._validate_existing_intent(existing, plan, request, reservation_id)
            prior = IntentState(existing["state"])
            if prior is IntentState.SUBMITTING:
                return self._mark_unknown(
                    plan=plan,
                    intent_id=intent_id,
                    reservation_id=reservation_id,
                    client_ref_id=request.client_ref_id,
                    occurred_at=current,
                    code="RESTART_OR_REPLAY_WHILE_SUBMITTING",
                    message="prior submission may have reached the broker; reconciliation is required",
                    replay=True,
                )
            if prior is IntentState.UNKNOWN:
                self._ensure_unknown_artifacts(
                    plan=plan,
                    intent_id=intent_id,
                    client_ref_id=request.client_ref_id,
                    occurred_at=current,
                    code="UNKNOWN_SUBMISSION_REPLAY",
                    message="unknown submission remains reserved and was not retried",
                )
                return self._outcome_from_existing(
                    plan=plan,
                    reservation_id=reservation_id,
                    intent_id=intent_id,
                    client_ref_id=request.client_ref_id,
                    state=prior,
                )
            if prior is not IntentState.PREPARED:
                return self._outcome_from_existing(
                    plan=plan,
                    reservation_id=reservation_id,
                    intent_id=intent_id,
                    client_ref_id=request.client_ref_id,
                    state=prior,
                )

        failures, evidence = self._preflight(plan, risk_decision, request, current)
        if failures:
            return ExecutionOutcome(
                status=ExecutionStatus.BLOCKED,
                plan_id=plan.plan_id,
                reservation_id=reservation_id,
                intent_id=intent_id,
                client_ref_id=request.client_ref_id,
                risk_reserved=existing is not None,
                replay=existing is not None,
                message="entry preflight failed; no broker review or mutation was called",
                failure_codes=failures,
            )
        assert evidence is not None

        authority_failures = self._authority_failures(
            snapshot=broker_snapshot,
            operation=MutationOperation.ENTRY_PLACE,
            phase=MutationPhase.BEFORE_PREPARE,
            now=current,
            plan_id=plan.plan_id,
            kind=IntentKind.ENTRY,
            request=request,
            plan=plan,
            risk_decision=risk_decision,
        )
        if authority_failures:
            return ExecutionOutcome(
                status=ExecutionStatus.BLOCKED,
                plan_id=plan.plan_id,
                reservation_id=reservation_id,
                intent_id=intent_id,
                client_ref_id=request.client_ref_id,
                risk_reserved=existing is not None,
                replay=existing is not None,
                message="runtime mutation authority failed before durable submission preparation",
                failure_codes=authority_failures,
            )

        try:
            durable_plan, reservation, intent = self._submission_aggregate(
                plan=plan,
                request=request,
                risk_decision=risk_decision,
                evidence=evidence,
            )
        except (TypeError, ValueError) as exc:
            return ExecutionOutcome(
                status=ExecutionStatus.BLOCKED,
                plan_id=plan.plan_id,
                reservation_id=reservation_id,
                intent_id=intent_id,
                client_ref_id=request.client_ref_id,
                risk_reserved=False,
                replay=False,
                message="durable aggregate validation failed before broker review",
                failure_codes=("DURABLE_AGGREGATE_INVALID",),
            )
        durable_span = _start_latency(
            self.latency,
            "durable_intent_write",
            observed_at=current,
            correlation_id=intent_id,
            metadata={"kind": IntentKind.ENTRY.value, "plan_id": plan.plan_id},
        )
        try:
            inserted = self.state.prepare_submission(
                plan=durable_plan,
                reservation=reservation,
                intent=intent,
            )
        except (TypeError, ValueError) as exc:
            return ExecutionOutcome(
                status=ExecutionStatus.BLOCKED,
                plan_id=plan.plan_id,
                reservation_id=reservation_id,
                intent_id=intent_id,
                client_ref_id=request.client_ref_id,
                risk_reserved=False,
                replay=False,
                message="durable aggregate validation failed before broker review",
                failure_codes=("DURABLE_AGGREGATE_INVALID",),
            )
        _finish_latency(
            self.latency,
            durable_span,
            observed_at=self._now(),
            metadata={"inserted": inserted},
        )

        seal_failure = self._seal_prepared_entry(
            plan=plan,
            request=request,
            intent_id=intent_id,
            reservation_id=reservation_id,
            replay=not inserted,
        )
        if seal_failure is not None:
            return seal_failure

        authority_failures = self._authority_failures(
            snapshot=broker_snapshot,
            operation=MutationOperation.ENTRY_PLACE,
            phase=MutationPhase.BEFORE_REVIEW,
            now=self._now(),
            plan_id=plan.plan_id,
            kind=IntentKind.ENTRY,
            request=request,
            plan=plan,
            risk_decision=risk_decision,
        )
        if authority_failures:
            return ExecutionOutcome(
                status=ExecutionStatus.BLOCKED,
                plan_id=plan.plan_id,
                reservation_id=reservation_id,
                intent_id=intent_id,
                client_ref_id=request.client_ref_id,
                risk_reserved=True,
                replay=not inserted,
                message="runtime mutation authority failed immediately before broker review",
                failure_codes=authority_failures,
            )

        try:
            review = self.broker.review_equity_order(request)
        except BrokerError as exc:
            failed_at = self._now()
            self.state.transition_intent(
                intent_id,
                IntentState.FAILED,
                occurred_at=failed_at,
                detail={"phase": "review", "code": exc.code},
            )
            risk_reserved = self._release_known_no_exposure(
                reservation_id=reservation_id,
                occurred_at=failed_at,
                reason=f"BROKER_REVIEW_FAILED:{exc.code}",
            )
            return ExecutionOutcome(
                status=ExecutionStatus.FAILED,
                plan_id=plan.plan_id,
                reservation_id=reservation_id,
                intent_id=intent_id,
                client_ref_id=request.client_ref_id,
                risk_reserved=risk_reserved,
                replay=not inserted,
                message="broker review failed before submission",
                failure_codes=(exc.code,),
            )
        except Exception as exc:
            failed_at = self._now()
            self.state.transition_intent(
                intent_id,
                IntentState.FAILED,
                occurred_at=failed_at,
                detail={"phase": "review", "error_type": type(exc).__name__},
            )
            risk_reserved = self._release_known_no_exposure(
                reservation_id=reservation_id,
                occurred_at=failed_at,
                reason=f"BROKER_REVIEW_EXCEPTION:{type(exc).__name__}",
            )
            return ExecutionOutcome(
                status=ExecutionStatus.FAILED,
                plan_id=plan.plan_id,
                reservation_id=reservation_id,
                intent_id=intent_id,
                client_ref_id=request.client_ref_id,
                risk_reserved=risk_reserved,
                replay=not inserted,
                message="unexpected broker review failure before submission",
                failure_codes=("BROKER_REVIEW_FAILURE",),
            )

        checked_at = self._now()
        review_failures = self._validate_review(
            review=review,
            request=request,
            plan=plan,
            checked_at=checked_at,
        )
        refreshed = self._market_evidence(plan, checked_at)
        if not refreshed.eligible:
            review_failures.extend(refreshed.failures)
        if review_failures:
            failures_tuple = tuple(dict.fromkeys(review_failures))
            self.state.transition_intent(
                intent_id,
                IntentState.FAILED,
                occurred_at=checked_at,
                detail={"phase": "review_validation", "failures": failures_tuple},
            )
            risk_reserved = self._release_known_no_exposure(
                reservation_id=reservation_id,
                occurred_at=checked_at,
                reason="BROKER_REVIEW_VALIDATION_FAILED",
            )
            return ExecutionOutcome(
                status=ExecutionStatus.FAILED,
                plan_id=plan.plan_id,
                reservation_id=reservation_id,
                intent_id=intent_id,
                client_ref_id=request.client_ref_id,
                risk_reserved=risk_reserved,
                replay=not inserted,
                message="review or refreshed execution evidence failed closed",
                failure_codes=failures_tuple,
            )

        submitting_at = self._now()
        authority_failures = self._authority_failures(
            snapshot=broker_snapshot,
            operation=MutationOperation.ENTRY_PLACE,
            phase=MutationPhase.BEFORE_PLACE,
            now=submitting_at,
            plan_id=plan.plan_id,
            kind=IntentKind.ENTRY,
            request=request,
            plan=plan,
            risk_decision=risk_decision,
        )
        if authority_failures:
            return ExecutionOutcome(
                status=ExecutionStatus.BLOCKED,
                plan_id=plan.plan_id,
                reservation_id=reservation_id,
                intent_id=intent_id,
                client_ref_id=request.client_ref_id,
                risk_reserved=True,
                replay=not inserted,
                message="runtime mutation authority failed immediately before broker place",
                failure_codes=authority_failures,
            )
        # External-change / expiry gate (fail-closed). Active only when the
        # caller supplied the fingerprint the plan was bound to at sizing time.
        # Immediately before the broker place, refuse if the observed account
        # exposure changed out-of-band or the plan is outside its validity
        # window. When no fingerprint is supplied (all current callers) this is
        # skipped and behaviour is unchanged; it lifts no gate and never reaches
        # the broker on refusal.
        if plan_bound_fingerprint is not None:
            freshness_failures = self._plan_freshness_failures(
                plan_bound_fingerprint=plan_bound_fingerprint,
                broker_snapshot=broker_snapshot,
                created_at=plan.created_at,
                expires_at=plan.expires_at,
                now=submitting_at,
            )
            if freshness_failures:
                return ExecutionOutcome(
                    status=ExecutionStatus.BLOCKED,
                    plan_id=plan.plan_id,
                    reservation_id=reservation_id,
                    intent_id=intent_id,
                    client_ref_id=request.client_ref_id,
                    risk_reserved=True,
                    replay=not inserted,
                    message="plan freshness gate refused the entry immediately before broker place",
                    failure_codes=freshness_failures,
                )
        self.state.transition_intent(
            intent_id,
            IntentState.SUBMITTING,
            occurred_at=submitting_at,
            detail={
                "broker_review_id": review.broker_review_id,
                "reviewed_at": review.reviewed_at.isoformat(),
                "tuple_hash": object_hash(intent.order_tuple),
            },
        )

        submit_span = _start_latency(
            self.latency,
            "submit_to_ack",
            observed_at=submitting_at,
            correlation_id=intent_id,
            metadata={"kind": IntentKind.ENTRY.value, "plan_id": plan.plan_id},
        )
        try:
            result = self.broker.place_equity_order(request, review=review)
        except BrokerUnknownSubmission as exc:
            return self._mark_unknown(
                plan=plan,
                intent_id=intent_id,
                reservation_id=reservation_id,
                client_ref_id=request.client_ref_id,
                occurred_at=self._now(),
                code=exc.code,
                message=f"{exc.code}: broker acceptance cannot be determined",
                replay=False,
            )
        except BrokerError as exc:
            if exc.submission_may_have_reached_broker:
                return self._mark_unknown(
                    plan=plan,
                    intent_id=intent_id,
                    reservation_id=reservation_id,
                    client_ref_id=request.client_ref_id,
                    occurred_at=self._now(),
                    code=exc.code,
                    message=f"{exc.code}: broker acceptance cannot be determined",
                    replay=False,
                )
            failed_at = self._now()
            self.state.transition_intent(
                intent_id,
                IntentState.FAILED,
                occurred_at=failed_at,
                detail={"phase": "place", "code": exc.code, "known_no_accept": True},
            )
            risk_reserved = self._release_known_no_exposure(
                reservation_id=reservation_id,
                occurred_at=failed_at,
                reason=f"BROKER_PLACE_KNOWN_NO_ACCEPT:{exc.code}",
            )
            return ExecutionOutcome(
                status=ExecutionStatus.FAILED,
                plan_id=plan.plan_id,
                reservation_id=reservation_id,
                intent_id=intent_id,
                client_ref_id=request.client_ref_id,
                risk_reserved=risk_reserved,
                replay=False,
                message="broker mutation failed with a known no-accept result",
                failure_codes=(exc.code,),
            )
        except Exception as exc:
            return self._mark_unknown(
                plan=plan,
                intent_id=intent_id,
                reservation_id=reservation_id,
                client_ref_id=request.client_ref_id,
                occurred_at=self._now(),
                code="UNCLASSIFIED_PLACE_TRANSPORT_FAILURE",
                message=f"{type(exc).__name__}: broker acceptance cannot be excluded",
                replay=False,
            )

        ack_measurement = (
            _stop_latency(
                self.latency,
                submit_span,
                observed_at=result.observed_at,
                metadata={"broker_order_id": result.order.broker_order_id},
            )
            if _is_exact_place_ack(result, request)
            else None
        )
        outcome = self._handle_place_result(
            plan=plan,
            request=request,
            reservation_id=reservation_id,
            intent_id=intent_id,
            result=result,
        )
        _persist_latency(self.latency, ack_measurement)
        return outcome

    def persist_order_evidence(
        self,
        *,
        intent_id: str,
        order: OrderSnapshot,
    ) -> tuple[bool, int]:
        """Persist one normalized broker order and all included fills.

        This is also the reconciliation entry point for an ``UNKNOWN`` intent.
        The stored order tuple is authoritative; mismatched broker evidence is
        rejected instead of being attached to the logical order.
        """

        intent_row = self.state.row("order_intents", "intent_id", intent_id)
        if intent_row is None:
            raise ValueError("broker evidence has no durable local intent")
        order_tuple = json.loads(intent_row["order_tuple_json"])
        self._validate_order_identity(order, order_tuple)
        existing_for_intent = self.state.rows(
            "SELECT broker_order_id FROM broker_orders WHERE intent_id = ?",
            (intent_id,),
        )
        if existing_for_intent and existing_for_intent[0]["broker_order_id"] != order.broker_order_id:
            raise ValueError("a logical intent cannot acquire a different broker order id")

        raw_payload = self._order_payload(order)
        raw_hash = object_hash(raw_payload)
        existing = self.state.row("broker_orders", "broker_order_id", order.broker_order_id)
        revision = 0 if existing is None else int(existing["revision"]) + 1
        durable_order = BrokerOrder(
            broker_order_id=order.broker_order_id,
            intent_id=intent_id,
            account_key=str(intent_row["account_key"]),
            state=order.state,
            quantity=whole_shares(order.requested_quantity),
            cumulative_filled_quantity=whole_shares(
                order.cumulative_filled_quantity,
                allow_zero=True,
            ),
            revision=revision,
            broker_updated_at=order.broker_updated_at,
            received_at=order.received_at,
            raw_hash=raw_hash,
        )
        # Normalize every fill before the order write.  A malformed fractional
        # fill must not leave the intent acknowledged with only half of the
        # response persisted.
        durable_fills = tuple(
            Fill(
                fill_id=broker_fill.fill_id,
                broker_order_id=order.broker_order_id,
                account_key=str(intent_row["account_key"]),
                quantity=whole_shares(broker_fill.quantity),
                price=broker_fill.price,
                executed_at=broker_fill.executed_at,
                received_at=max(order.received_at, broker_fill.executed_at),
            )
            for broker_fill in order.fills
        )
        if existing is not None and existing["raw_hash"] == raw_hash:
            order_inserted = False
        else:
            order_inserted = self.state.record_broker_order(durable_order)

        fill_count = 0
        for durable_fill in durable_fills:
            if self.state.record_fill(durable_fill):
                fill_count += 1
        return order_inserted, fill_count

    def _preflight(
        self,
        plan: ExpiringPlan,
        risk_decision: RiskDecision,
        request: OrderRequest,
        now: datetime,
    ) -> tuple[tuple[str, ...], EvidenceDecision | None]:
        failures: list[str] = []
        try:
            plan.validate(self.policy, now)
        except (TypeError, ValueError) as exc:
            failures.append("PLAN_INVALID")

        if not self.policy.live_entries_configured:
            failures.append("LIVE_ENTRIES_DISABLED_IN_SIGNED_CONFIG")
        failures.extend(self.policy.activation_blockers)
        try:
            failures.extend(self._capability_failures(self.broker.capabilities, request))
        except BrokerError as exc:
            failures.append(f"BROKER_CAPABILITY_PROBE_FAILED:{exc.code}")
        except Exception as exc:
            failures.append(f"BROKER_CAPABILITY_PROBE_FAILED:{type(exc).__name__}")

        if not isinstance(risk_decision, RiskDecision):
            failures.append("RISK_DECISION_MISSING_OR_INVALID")
        else:
            if not risk_decision.allowed:
                failures.extend(risk_decision.failures or ("RISK_DECISION_DENIED",))
            try:
                expected_stress = (
                    plan.stress_risk
                    + entry_lifecycle_fee_reserve(
                        self.policy,
                        quantity=plan.quantity,
                    )
                )
            except (TypeError, ValueError):
                expected_stress = None
                failures.append("COMMISSION_FEE_RESERVE_INVALID")
            expected = (
                (risk_decision.proposal_planned_risk, plan.planned_risk, "RISK_PLANNED_MISMATCH"),
                (risk_decision.proposal_reserve, plan.execution_reserve, "RISK_RESERVE_MISMATCH"),
            )
            failures.extend(code for actual, wanted, code in expected if actual != wanted)
            if (
                expected_stress is not None
                and risk_decision.proposal_stress_risk != expected_stress
            ):
                failures.append("RISK_STRESS_MISMATCH")
            if any(
                value < 0
                for value in (
                    risk_decision.remaining_daily_headroom,
                    risk_decision.remaining_portfolio_headroom,
                    risk_decision.remaining_stress_headroom,
                    risk_decision.remaining_buying_power,
                    risk_decision.remaining_cash_headroom,
                )
            ):
                failures.append("RISK_HEADROOM_NEGATIVE")

        evidence: EvidenceDecision | None = None
        try:
            evidence = self._market_evidence(plan, now)
            failures.extend(evidence.failures)
            if evidence.quote is not None and evidence.quote.observed_at < plan.quote_observed_at:
                failures.append("QUOTE_PREDATES_SIGNED_PLAN_EVIDENCE")
            if (
                evidence.causal_bar is not None
                and evidence.causal_bar.source_event_id not in plan.source_event_ids
            ):
                failures.append("CAUSAL_BAR_EVENT_NOT_BOUND_TO_PLAN")
        except (KeyError, TypeError, ValueError) as exc:
            failures.append("MARKET_EVIDENCE_INVALID")
        return tuple(dict.fromkeys(failures)), evidence

    def _market_evidence(self, plan: ExpiringPlan, now: datetime) -> EvidenceDecision:
        config = self.policy.config
        return self.market_data.validate_entry_evidence(
            symbol=plan.symbol,
            now=now,
            plan_created_at=plan.created_at,
            plan_expires_at=plan.expires_at,
            causal_bar_end=plan.completed_bar_end,
            quote_max_age_seconds=int(config["evidence"]["quote_max_age_seconds"]),
            completed_bar_max_age_seconds=int(
                config["evidence"]["completed_bar_max_age_seconds"]
            ),
            minimum_session_volume=int(
                config["scope"]["minimum_session_volume_inclusive"]
            ),
            max_spread_bps=config["evidence"].get("max_spread_bps"),
            minimum_depth_multiple=config["evidence"].get("minimum_depth_multiple"),
            quantity=plan.quantity,
        )

    def _capability_failures(
        self,
        capabilities: BrokerCapabilities,
        request: OrderRequest,
    ) -> tuple[str, ...]:
        checks = (
            (capabilities.account_masked == request.account_masked, "BROKER_ACCOUNT_MISMATCH"),
            (capabilities.daemon_transport_configured, "DAEMON_BROKER_TRANSPORT_UNAVAILABLE"),
            (capabilities.supports_daemon_writes, "DAEMON_BROKER_WRITES_UNSUPPORTED"),
            (capabilities.supports_unattended_writes, "UNATTENDED_BROKER_WRITES_UNSUPPORTED"),
            (capabilities.supports_equity_review, "EQUITY_REVIEW_UNSUPPORTED"),
            (capabilities.supports_equity_place, "EQUITY_PLACE_UNSUPPORTED"),
            (capabilities.supports_equity_cancel, "EQUITY_CANCEL_UNSUPPORTED"),
            (not capabilities.review_requires_explicit_confirmation, "REVIEW_CONFIRMATION_REQUIRED"),
            (not capabilities.cancel_requires_explicit_confirmation, "CANCEL_CONFIRMATION_REQUIRED"),
            (capabilities.supports_ref_id_lookup, "CLIENT_REF_LOOKUP_UNSUPPORTED"),
            (request.order_type in capabilities.supported_order_types, "ORDER_TYPE_UNSUPPORTED"),
            (request.market_hours in capabilities.supported_market_hours, "MARKET_HOURS_UNSUPPORTED"),
            (request.time_in_force in capabilities.supported_time_in_force, "TIME_IN_FORCE_UNSUPPORTED"),
        )
        failures = [code for passed, code in checks if not passed]
        if (
            self.policy.config["evidence"].get("require_advanced_order_reconciliation") is True
            and not capabilities.order_coverage.family_complete(
                OrderFamily.ADVANCED_EQUITY
            )
        ):
            failures.append("ADVANCED_ORDER_READ_UNSUPPORTED")
        return tuple(failures)

    def _submission_aggregate(
        self,
        *,
        plan: ExpiringPlan,
        request: OrderRequest,
        risk_decision: RiskDecision,
        evidence: EvidenceDecision,
    ) -> tuple[DurablePlan, RiskReservation, OrderIntent]:
        reservation_id, intent_id = self._stable_ids(plan)
        account_key = self.policy.account_key
        evidence_body = {
            "plan_id": plan.plan_id,
            "completed_bar_end": plan.completed_bar_end.isoformat(),
            "quote_observed_at": plan.quote_observed_at.isoformat(),
            "source_event_ids": list(plan.source_event_ids),
            "causal_bar_source_event_id": (
                evidence.causal_bar.source_event_id if evidence.causal_bar is not None else None
            ),
        }
        durable_plan = DurablePlan(
            plan_id=plan.plan_id,
            account_key=account_key,
            strategy_id=plan.strategy_id,
            symbol=plan.symbol,
            setup_id=plan.setup_id,
            quantity=plan.quantity,
            limit_price=plan.entry_limit,
            structural_stop=plan.structural_stop,
            market_hours=plan.market_hours,
            time_in_force=plan.time_in_force,
            evidence_cutoff_at=max(plan.completed_bar_end, plan.quote_observed_at),
            created_at=plan.created_at,
            expires_at=plan.expires_at,
            policy_hash=plan.policy_hash,
            config_hash=plan.config_hash,
            evidence_hash=object_hash(evidence_body),
            targets=plan.targets,
        )
        reservation = RiskReservation(
            reservation_id=reservation_id,
            plan_id=plan.plan_id,
            account_key=account_key,
            planned_risk=risk_decision.proposal_planned_risk,
            stress_risk=risk_decision.proposal_stress_risk,
            execution_reserve=risk_decision.proposal_reserve,
            notional=plan.notional,
            created_at=plan.created_at,
        )
        order_tuple = self._order_tuple(account_key, request)
        intent = OrderIntent(
            intent_id=intent_id,
            plan_id=plan.plan_id,
            reservation_id=reservation_id,
            account_key=account_key,
            kind=IntentKind.ENTRY,
            client_ref=request.client_ref_id,
            order_tuple=order_tuple,
            tuple_hash=object_hash(order_tuple),
            created_at=plan.created_at,
            acknowledgement_deadline_at=min(
                plan.expires_at,
                plan.created_at
                + timedelta(
                    seconds=int(self.policy.config["execution"]["order_ack_timeout_seconds"])
                ),
            ),
        )
        return durable_plan, reservation, intent

    def _validate_review(
        self,
        *,
        review: ReviewReceipt,
        request: OrderRequest,
        plan: ExpiringPlan,
        checked_at: datetime,
    ) -> list[str]:
        failures: list[str] = []
        if not isinstance(review, ReviewReceipt):
            return ["BROKER_REVIEW_NOT_NORMALIZED"]
        if review.request.exact_tuple != request.exact_tuple:
            failures.append("BROKER_REVIEW_TUPLE_MISMATCH")
        failures.extend(
            _review_provenance_failures(review, self.broker.capabilities)
        )
        if review.expires_at is None or review.expired_at(checked_at):
            failures.append("BROKER_REVIEW_EXPIRED_OR_UNBOUNDED")
        review_age = (checked_at - review.reviewed_at).total_seconds()
        max_age = int(self.policy.config["evidence"]["quote_max_age_seconds"])
        if review_age < -1 or review_age > max_age:
            failures.append("BROKER_REVIEW_STALE_OR_FUTURE")
        if checked_at > plan.expires_at:
            failures.append("PLAN_EXPIRED_DURING_REVIEW")
        if review.required_confirmation_phrase is not None:
            failures.append("BROKER_REVIEW_REQUIRES_ATTENDED_CONFIRMATION")
        if not review.disclosure.strip():
            failures.append("BROKER_REVIEW_DISCLOSURE_MISSING")
        if review.order_checks:
            failures.extend(f"BROKER_REVIEW_CHECK:{check.code}" for check in review.order_checks)
        return failures

    def _handle_place_result(
        self,
        *,
        plan: ExpiringPlan,
        request: OrderRequest,
        reservation_id: str,
        intent_id: str,
        result: BrokerOperationResult,
    ) -> ExecutionOutcome:
        if not isinstance(result, BrokerOperationResult):
            return self._mark_unknown(
                plan=plan,
                intent_id=intent_id,
                reservation_id=reservation_id,
                client_ref_id=request.client_ref_id,
                occurred_at=self._now(),
                code="UNNORMALIZED_PLACE_RESULT",
                message="place transport returned an unnormalized result",
                replay=False,
            )
        if result.status is OperationStatus.REJECTED and result.accepted is False:
            if result.order is not None:
                self.persist_order_evidence(intent_id=intent_id, order=result.order)
            self.state.transition_intent(
                intent_id,
                IntentState.REJECTED,
                occurred_at=result.observed_at,
                detail={"message": result.message, "known_reject": True},
            )
            risk_reserved = self._release_known_no_exposure(
                reservation_id=reservation_id,
                occurred_at=result.observed_at,
                reason="BROKER_KNOWN_REJECTION",
            )
            return ExecutionOutcome(
                status=ExecutionStatus.REJECTED,
                plan_id=plan.plan_id,
                reservation_id=reservation_id,
                intent_id=intent_id,
                client_ref_id=request.client_ref_id,
                risk_reserved=risk_reserved,
                replay=False,
                message=result.message,
                failure_codes=("BROKER_KNOWN_REJECTION",),
                broker_order_id=(result.order.broker_order_id if result.order else None),
            )
        if (
            result.status is not OperationStatus.ACKNOWLEDGED
            or result.accepted is not True
            or result.order is None
        ):
            return self._mark_unknown(
                plan=plan,
                intent_id=intent_id,
                reservation_id=reservation_id,
                client_ref_id=request.client_ref_id,
                occurred_at=result.observed_at,
                code="AMBIGUOUS_PLACE_RESULT",
                message="place result did not conclusively prove acknowledgement or rejection",
                replay=False,
            )
        try:
            self.persist_order_evidence(intent_id=intent_id, order=result.order)
        except Exception as exc:
            return self._mark_unknown(
                plan=plan,
                intent_id=intent_id,
                reservation_id=reservation_id,
                client_ref_id=request.client_ref_id,
                occurred_at=self._now(),
                code="ACKNOWLEDGEMENT_PERSISTENCE_FAILED",
                message=f"broker acknowledged but local persistence failed: {type(exc).__name__}",
                replay=False,
            )
        return ExecutionOutcome(
            status=ExecutionStatus.ACKNOWLEDGED,
            plan_id=plan.plan_id,
            reservation_id=reservation_id,
            intent_id=intent_id,
            client_ref_id=request.client_ref_id,
            risk_reserved=True,
            replay=False,
            message="broker acknowledgement and included fills were durably recorded",
            broker_order_id=result.order.broker_order_id,
        )

    def _mark_unknown(
        self,
        *,
        plan: ExpiringPlan,
        intent_id: str,
        reservation_id: str,
        client_ref_id: str,
        occurred_at: datetime,
        code: str,
        message: str,
        replay: bool,
    ) -> ExecutionOutcome:
        row = self.state.row("order_intents", "intent_id", intent_id)
        if row is None:
            raise ValueError("cannot mark an undurable submission UNKNOWN")
        state = IntentState(row["state"])
        if state is IntentState.SUBMITTING:
            self.state.transition_intent(
                intent_id,
                IntentState.UNKNOWN,
                occurred_at=occurred_at,
                detail={"code": code, "automatic_retry_allowed": False},
            )
        elif state is not IntentState.UNKNOWN:
            raise ValueError(f"cannot mark {state.value} intent UNKNOWN")
        self._ensure_unknown_artifacts(
            plan=plan,
            intent_id=intent_id,
            client_ref_id=client_ref_id,
            occurred_at=occurred_at,
            code=code,
            message=message,
        )
        return ExecutionOutcome(
            status=ExecutionStatus.UNKNOWN,
            plan_id=plan.plan_id,
            reservation_id=reservation_id,
            intent_id=intent_id,
            client_ref_id=client_ref_id,
            risk_reserved=True,
            replay=replay,
            message="submission may have reached the broker; risk remains reserved and no retry was attempted",
            failure_codes=(code,),
        )

    def _release_known_no_exposure(
        self,
        *,
        reservation_id: str,
        occurred_at: datetime,
        reason: str,
    ) -> bool:
        """Return whether risk remains reserved after a guarded release attempt.

        A persistence or proof failure is deliberately converted into
        ``risk_reserved=True``.  The broker failure outcome still reaches the
        caller while risk accounting fails closed instead of pretending that
        capacity was restored.
        """

        try:
            self.state.release_reservation(
                reservation_id,
                occurred_at=occurred_at,
                reason=reason,
            )
        except Exception:
            return True
        reservation = self.state.row(
            "risk_reservations", "reservation_id", reservation_id
        )
        return (
            reservation is None
            or reservation["state"] != ReservationState.RELEASED.value
        )

    def _seal_prepared_entry(
        self,
        *,
        plan: ExpiringPlan,
        request: OrderRequest,
        intent_id: str,
        reservation_id: str,
        replay: bool,
    ) -> ExecutionOutcome | None:
        """Seal after durable prepare and before any broker review.

        An ordinary callback failure is conclusively pre-broker and can
        release the entry reservation.  ``BaseException`` is deliberately not
        caught: a process death leaves PREPARED state (and possibly an already
        appended idempotent seal), which a restart can safely resume without a
        broker retry ambiguity.
        """

        if self.plan_sealer is None:
            return None
        try:
            result = self.plan_sealer(
                intent_id=intent_id,
                plan_id=plan.plan_id,
                kind=IntentKind.ENTRY,
                request=request,
            )
            if result is not None:
                raise TypeError("prepared plan sealer must return None")
        except Exception as exc:
            failed_at = self._now()
            transition_failed = False
            try:
                self.state.transition_intent(
                    intent_id,
                    IntentState.FAILED,
                    occurred_at=failed_at,
                    detail={
                        "phase": "review",
                        "code": "AUTONOMOUS_PLAN_SEAL_FAILED",
                        "error_type": type(exc).__name__,
                        "known_no_accept": True,
                    },
                )
            except Exception:
                transition_failed = True
            risk_reserved = self._release_known_no_exposure(
                reservation_id=reservation_id,
                occurred_at=failed_at,
                reason="AUTONOMOUS_PLAN_SEAL_FAILED",
            )
            failures = ["AUTONOMOUS_PLAN_SEAL_FAILED"]
            if transition_failed:
                failures.append("AUTONOMOUS_PLAN_SEAL_FAILURE_NOT_DURABLE")
            return ExecutionOutcome(
                status=ExecutionStatus.FAILED,
                plan_id=plan.plan_id,
                reservation_id=reservation_id,
                intent_id=intent_id,
                client_ref_id=request.client_ref_id,
                risk_reserved=risk_reserved,
                replay=replay,
                message=(
                    "exact autonomous plan sealing failed before broker review; "
                    "no broker mutation was attempted"
                ),
                failure_codes=tuple(failures),
            )
        return None

    def _ensure_unknown_artifacts(
        self,
        *,
        plan: ExpiringPlan,
        intent_id: str,
        client_ref_id: str,
        occurred_at: datetime,
        code: str,
        message: str,
    ) -> None:
        account_key = self.policy.account_key
        incident_id = str(uuid5(_EXECUTION_NAMESPACE, f"unknown-incident:{intent_id}"))
        if self.state.row("incidents", "incident_id", incident_id) is None:
            self.state.record_incident(
                Incident(
                    incident_id=incident_id,
                    account_key=account_key,
                    category="BROKER_SUBMISSION_UNKNOWN",
                    severity=IncidentSeverity.CRITICAL,
                    opened_at=occurred_at,
                    detail={
                        "plan_id": plan.plan_id,
                        "intent_id": intent_id,
                        "client_ref_id": client_ref_id,
                        "code": code,
                        "message": message,
                        "risk_reserved": True,
                        "automatic_retry_allowed": False,
                    },
                )
            )
        message_id = str(uuid5(_EXECUTION_NAMESPACE, f"unknown-notification:{intent_id}"))
        if self.state.row("notification_outbox", "message_id", message_id) is None:
            self.state.enqueue_notification(
                OutboxMessage(
                    message_id=message_id,
                    event_key=f"broker-submission-unknown:{intent_id}",
                    account_key=account_key,
                    template="BROKER_SUBMISSION_UNKNOWN",
                    payload={
                        "account": f"ending-{self.policy.account_last4}",
                        "symbol": plan.symbol,
                        "intent_id": intent_id,
                        "state": "UNKNOWN",
                        "risk_reserved": True,
                        "retry": "BLOCKED_PENDING_RECONCILIATION",
                    },
                    created_at=occurred_at,
                )
            )

    def _outcome_from_existing(
        self,
        *,
        plan: ExpiringPlan,
        reservation_id: str,
        intent_id: str,
        client_ref_id: str,
        state: IntentState,
    ) -> ExecutionOutcome:
        status_map = {
            IntentState.ACKNOWLEDGED: ExecutionStatus.ACKNOWLEDGED,
            IntentState.RECONCILED: ExecutionStatus.ACKNOWLEDGED,
            IntentState.REJECTED: ExecutionStatus.REJECTED,
            IntentState.FAILED: ExecutionStatus.FAILED,
            IntentState.CANCELLED: ExecutionStatus.FAILED,
            IntentState.UNKNOWN: ExecutionStatus.UNKNOWN,
        }
        orders = self.state.rows(
            "SELECT broker_order_id FROM broker_orders WHERE intent_id = ?",
            (intent_id,),
        )
        reservation = self.state.row(
            "risk_reservations", "reservation_id", reservation_id
        )
        return ExecutionOutcome(
            status=status_map[state],
            plan_id=plan.plan_id,
            reservation_id=reservation_id,
            intent_id=intent_id,
            client_ref_id=client_ref_id,
            risk_reserved=(
                reservation is None
                or reservation["state"] != ReservationState.RELEASED.value
            ),
            replay=True,
            message=f"logical order already exists in durable state {state.value}; broker was not called",
            broker_order_id=(str(orders[0]["broker_order_id"]) if orders else None),
        )

    def _validate_existing_intent(
        self,
        row: Mapping[str, object],
        plan: ExpiringPlan,
        request: OrderRequest,
        reservation_id: str,
    ) -> None:
        expected = self._order_tuple(
            self.policy.account_key,
            request,
        )
        if (
            row["plan_id"] != plan.plan_id
            or row["reservation_id"] != reservation_id
            or row["client_ref"] != request.client_ref_id
            or json.loads(str(row["order_tuple_json"])) != expected
            or row["tuple_hash"] != object_hash(expected)
        ):
            raise ValueError("durable logical-order replay conflicts with the signed plan")

    def _validate_order_identity(
        self,
        order: OrderSnapshot,
        expected: Mapping[str, object],
    ) -> None:
        checks = (
            (order.account_masked, expected["account_masked"]),
            (order.symbol, expected["symbol"]),
            (order.side.value, expected["side"]),
            (order.order_type.value, expected["order_type"]),
            (whole_shares(order.requested_quantity), expected["quantity"]),
            (order.market_hours.value, expected["market_hours"]),
            (order.time_in_force.value, expected["time_in_force"]),
            (
                format(order.limit_price, "f") if order.limit_price is not None else None,
                expected["limit_price"],
            ),
            (
                format(order.stop_price, "f") if order.stop_price is not None else None,
                expected["stop_price"],
            ),
            (order.client_ref_id, expected["client_ref_id"]),
        )
        if any(actual != wanted for actual, wanted in checks):
            raise ValueError("broker order does not match the durable exact order tuple")

    @staticmethod
    def _order_payload(order: OrderSnapshot) -> Mapping[str, object]:
        return {
            "broker_order_id": order.broker_order_id,
            "account_masked": order.account_masked,
            "symbol": order.symbol,
            "side": order.side.value,
            "order_type": order.order_type.value,
            "state": order.state.value,
            "requested_quantity": format(order.requested_quantity, "f"),
            "cumulative_filled_quantity": format(
                order.cumulative_filled_quantity, "f"
            ),
            "market_hours": order.market_hours.value,
            "time_in_force": order.time_in_force.value,
            "limit_price": (
                format(order.limit_price, "f") if order.limit_price is not None else None
            ),
            "stop_price": (
                format(order.stop_price, "f") if order.stop_price is not None else None
            ),
            "client_ref_id": order.client_ref_id,
            "broker_updated_at": order.broker_updated_at.isoformat(),
            "received_at": order.received_at.isoformat(),
            "fills": [
                {
                    "fill_id": fill.fill_id,
                    "quantity": format(fill.quantity, "f"),
                    "price": format(fill.price, "f"),
                    "fee": format(fill.fee, "f"),
                    "executed_at": fill.executed_at.isoformat(),
                }
                for fill in order.fills
            ],
        }

    def _request_for(self, plan: ExpiringPlan) -> OrderRequest:
        return OrderRequest(
            account_masked=f"••••{self.policy.account_last4}",
            symbol=plan.symbol,
            side=BrokerSide.BUY,
            order_type=EquityOrderType.LIMIT,
            quantity=plan.quantity,
            market_hours=MarketHours(plan.market_hours),
            time_in_force=TimeInForce(plan.time_in_force),
            client_ref_id=self._client_ref(plan),
            limit_price=plan.entry_limit,
        )

    def _client_ref(self, plan: ExpiringPlan) -> str:
        return str(
            uuid5(
                _EXECUTION_NAMESPACE,
                f"entry:{self.policy.account_last4}:{plan.plan_id}",
            )
        )

    def _stable_ids(self, plan: ExpiringPlan) -> tuple[str, str]:
        reservation_id = str(
            uuid5(
                _EXECUTION_NAMESPACE,
                f"reservation:{self.policy.account_last4}:{plan.plan_id}",
            )
        )
        intent_id = str(
            uuid5(
                _EXECUTION_NAMESPACE,
                f"intent:{self.policy.account_last4}:{plan.plan_id}:entry",
            )
        )
        return reservation_id, intent_id

    @staticmethod
    def _order_tuple(account_key: str, request: OrderRequest) -> dict[str, object]:
        return {
            "account_key": account_key,
            "account_masked": request.account_masked,
            "symbol": request.symbol,
            "side": request.side.value,
            "order_type": request.order_type.value,
            "quantity": request.quantity,
            "market_hours": request.market_hours.value,
            "time_in_force": request.time_in_force.value,
            "limit_price": (
                format(request.limit_price, "f") if request.limit_price is not None else None
            ),
            "stop_price": (
                format(request.stop_price, "f") if request.stop_price is not None else None
            ),
            "client_ref_id": request.client_ref_id,
        }

    def _now(self) -> datetime:
        return self._aware(self._clock(), "clock")

    def _authority_failures(
        self,
        *,
        snapshot: AccountSnapshot | None,
        operation: MutationOperation,
        phase: MutationPhase,
        now: datetime,
        plan_id: str,
        kind: IntentKind,
        request: OrderRequest | None = None,
        target: OrderSnapshot | None = None,
        plan: ExpiringPlan | None = None,
        risk_decision: RiskDecision | None = None,
    ) -> tuple[str, ...]:
        try:
            self.authority.require_mutation_authority(
                snapshot=snapshot,
                operation=operation,
                phase=phase,
                now=now,
                plan_id=plan_id,
                kind=kind,
                request=request,
                target=target,
                plan=plan,
                risk_decision=risk_decision,
            )
            return ()
        except MutationAuthorityDenied as exc:
            return exc.failure_codes
        except Exception as exc:
            return (f"MUTATION_AUTHORITY_CAPABILITY_FAILED:{type(exc).__name__}",)

    @staticmethod
    def _aware(value: datetime, field: str) -> datetime:
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError(f"{field} must be timezone-aware")
        return value.astimezone(timezone.utc)


class SafetyExecutionCoordinator:
    """Durable mutation boundary for protection, exits, and cancellation.

    The coordinator does not decide *whether* an exit is required or calculate
    sell capacity.  Those decisions belong to the protection/exit planners and
    must be made from current broker evidence.  This class owns the final
    transport invariants: a deterministic logical identity, durable intent
    before mutation, an exact unexpired review for sell orders, fail-closed
    unattended capability checks, and no retry after an ambiguous result.
    """

    def __init__(
        self,
        *,
        policy: PolicyBundle,
        state: LiveStateStore,
        broker: BrokerClient,
        authority: MutationAuthority,
        plan_sealer: PreparedOrderPlanSealer | None = None,
        clock: Callable[[], datetime] | None = None,
        latency: LatencyRecorder | None = None,
    ) -> None:
        self.policy = policy
        self.state = state
        self.broker = broker
        self.authority = authority
        self.plan_sealer = plan_sealer
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.latency = latency

    def submit_sell(
        self,
        *,
        plan_id: str,
        kind: IntentKind,
        operation_key: str,
        request: OrderRequest,
        broker_snapshot: AccountSnapshot | None = None,
        now: datetime | None = None,
    ) -> SafetyExecutionOutcome:
        """Review and place one deterministic PROTECTION or EXIT sell.

        ``operation_key`` is the stable upstream identity of the obligation or
        closeout action.  The caller-supplied request's UUID is replaced with a
        UUID5 derived from that identity, so a process restart cannot create a
        second logical order merely by generating a new random reference.
        """

        current = self._aware(now or self._clock(), "now")
        normalized_plan = self._required(plan_id, "plan_id")
        normalized_kind = self._sell_kind(kind)
        normalized_key = self._required(operation_key, "operation_key")
        client_ref_id = self._stable_client_ref(
            plan_id=normalized_plan,
            kind=normalized_kind,
            operation_key=normalized_key,
        )
        intent_id = self._stable_intent_id(
            plan_id=normalized_plan,
            kind=normalized_kind,
            operation_key=normalized_key,
        )
        if not isinstance(request, OrderRequest):
            return self._blocked(
                plan_id=normalized_plan,
                kind=normalized_kind,
                intent_id=intent_id,
                client_ref_id=client_ref_id,
                message="sell request was not normalized",
                failures=("SELL_REQUEST_NOT_NORMALIZED",),
            )
        exact_request = replace(request, client_ref_id=client_ref_id)
        order_tuple = self._sell_tuple(
            plan_id=normalized_plan,
            kind=normalized_kind,
            operation_key=normalized_key,
            request=exact_request,
        )

        existing = self.state.row("order_intents", "intent_id", intent_id)
        if existing is not None:
            self._validate_existing_safety_intent(
                existing,
                plan_id=normalized_plan,
                kind=normalized_kind,
                client_ref_id=client_ref_id,
                order_tuple=order_tuple,
            )
            prior = IntentState(existing["state"])
            if prior is IntentState.SUBMITTING:
                return self._mark_unknown(
                    plan_id=normalized_plan,
                    kind=normalized_kind,
                    intent_id=intent_id,
                    client_ref_id=client_ref_id,
                    occurred_at=current,
                    code="RESTART_OR_REPLAY_WHILE_SAFETY_SUBMITTING",
                    message="prior safety mutation may have reached the broker",
                    replay=True,
                )
            if prior is IntentState.UNKNOWN:
                self._ensure_unknown_artifacts(
                    plan_id=normalized_plan,
                    kind=normalized_kind,
                    intent_id=intent_id,
                    client_ref_id=client_ref_id,
                    occurred_at=current,
                    code="UNKNOWN_SAFETY_SUBMISSION_REPLAY",
                    message="unknown safety mutation was not retried",
                )
                return self._outcome_from_existing(existing, replay=True)
            if prior is not IntentState.PREPARED:
                return self._outcome_from_existing(existing, replay=True)

        failures, plan_row = self._sell_preflight(
            plan_id=normalized_plan,
            kind=normalized_kind,
            request=exact_request,
        )
        if failures:
            return self._blocked(
                plan_id=normalized_plan,
                kind=normalized_kind,
                intent_id=intent_id,
                client_ref_id=client_ref_id,
                message="safety sell preflight failed; broker was not called",
                failures=failures,
                replay=existing is not None,
            )
        assert plan_row is not None

        authority_failures = self._authority_failures(
            snapshot=broker_snapshot,
            operation=MutationOperation.SAFETY_PLACE,
            phase=MutationPhase.BEFORE_PREPARE,
            now=current,
            plan_id=normalized_plan,
            kind=normalized_kind,
            request=exact_request,
        )
        if authority_failures:
            return self._blocked(
                plan_id=normalized_plan,
                kind=normalized_kind,
                intent_id=intent_id,
                client_ref_id=client_ref_id,
                message="runtime mutation authority failed before durable safety preparation",
                failures=authority_failures,
                replay=existing is not None,
            )

        intent = OrderIntent(
            intent_id=intent_id,
            plan_id=normalized_plan,
            reservation_id=None,
            account_key=str(plan_row["account_key"]),
            kind=normalized_kind,
            client_ref=client_ref_id,
            order_tuple=order_tuple,
            tuple_hash=object_hash(order_tuple),
            created_at=current,
            acknowledgement_deadline_at=current
            + timedelta(
                seconds=int(
                    self.policy.config["execution"]["order_ack_timeout_seconds"]
                )
            ),
        )
        durable_span = _start_latency(
            self.latency,
            "durable_intent_write",
            observed_at=current,
            correlation_id=intent_id,
            metadata={"kind": normalized_kind.value, "plan_id": normalized_plan},
        )
        inserted = self.state.prepare_safety_intent(intent)
        _finish_latency(
            self.latency,
            durable_span,
            observed_at=self._now(),
            metadata={"inserted": inserted},
        )

        seal_failure = self._seal_prepared_sell(
            plan_id=normalized_plan,
            kind=normalized_kind,
            request=exact_request,
            intent_id=intent_id,
            replay=not inserted,
        )
        if seal_failure is not None:
            return seal_failure

        authority_failures = self._authority_failures(
            snapshot=broker_snapshot,
            operation=MutationOperation.SAFETY_PLACE,
            phase=MutationPhase.BEFORE_REVIEW,
            now=self._now(),
            plan_id=normalized_plan,
            kind=normalized_kind,
            request=exact_request,
        )
        if authority_failures:
            return self._blocked(
                plan_id=normalized_plan,
                kind=normalized_kind,
                intent_id=intent_id,
                client_ref_id=client_ref_id,
                message="runtime mutation authority failed immediately before broker review",
                failures=authority_failures,
                replay=not inserted,
            )

        try:
            review = self.broker.review_equity_order(exact_request)
        except BrokerError as exc:
            self.state.transition_intent(
                intent_id,
                IntentState.FAILED,
                occurred_at=self._now(),
                detail={"phase": "review", "code": exc.code},
            )
            return self._failed(
                plan_id=normalized_plan,
                kind=normalized_kind,
                intent_id=intent_id,
                client_ref_id=client_ref_id,
                message="broker safety-order review failed before mutation",
                failures=(exc.code,),
                replay=not inserted,
            )
        except Exception as exc:
            self.state.transition_intent(
                intent_id,
                IntentState.FAILED,
                occurred_at=self._now(),
                detail={"phase": "review", "error_type": type(exc).__name__},
            )
            return self._failed(
                plan_id=normalized_plan,
                kind=normalized_kind,
                intent_id=intent_id,
                client_ref_id=client_ref_id,
                message="unexpected safety-order review failure before mutation",
                failures=("BROKER_REVIEW_FAILURE",),
                replay=not inserted,
            )

        checked_at = self._now()
        review_failures = self._validate_review(
            review=review,
            request=exact_request,
            checked_at=checked_at,
        )
        if review_failures:
            self.state.transition_intent(
                intent_id,
                IntentState.FAILED,
                occurred_at=checked_at,
                detail={"phase": "review_validation", "failures": review_failures},
            )
            return self._failed(
                plan_id=normalized_plan,
                kind=normalized_kind,
                intent_id=intent_id,
                client_ref_id=client_ref_id,
                message="safety-order review failed exactness or freshness checks",
                failures=review_failures,
                replay=not inserted,
            )

        submitting_at = self._now()
        authority_failures = self._authority_failures(
            snapshot=broker_snapshot,
            operation=MutationOperation.SAFETY_PLACE,
            phase=MutationPhase.BEFORE_PLACE,
            now=submitting_at,
            plan_id=normalized_plan,
            kind=normalized_kind,
            request=exact_request,
        )
        if authority_failures:
            return self._blocked(
                plan_id=normalized_plan,
                kind=normalized_kind,
                intent_id=intent_id,
                client_ref_id=client_ref_id,
                message="runtime mutation authority failed immediately before broker place",
                failures=authority_failures,
                replay=not inserted,
            )
        self.state.transition_intent(
            intent_id,
            IntentState.SUBMITTING,
            occurred_at=submitting_at,
            detail={
                "broker_review_id": review.broker_review_id,
                "reviewed_at": review.reviewed_at.isoformat(),
                "tuple_hash": object_hash(order_tuple),
                "kind": normalized_kind.value,
            },
        )
        submit_span = _start_latency(
            self.latency,
            "submit_to_ack",
            observed_at=submitting_at,
            correlation_id=intent_id,
            metadata={"kind": normalized_kind.value, "plan_id": normalized_plan},
        )
        try:
            result = self.broker.place_equity_order(exact_request, review=review)
        except BrokerUnknownSubmission as exc:
            return self._mark_unknown(
                plan_id=normalized_plan,
                kind=normalized_kind,
                intent_id=intent_id,
                client_ref_id=client_ref_id,
                occurred_at=self._now(),
                code=exc.code,
                message=f"{exc.code}: broker acceptance cannot be determined",
                replay=False,
            )
        except BrokerError as exc:
            if exc.submission_may_have_reached_broker:
                return self._mark_unknown(
                    plan_id=normalized_plan,
                    kind=normalized_kind,
                    intent_id=intent_id,
                    client_ref_id=client_ref_id,
                    occurred_at=self._now(),
                    code=exc.code,
                    message=f"{exc.code}: broker acceptance cannot be determined",
                    replay=False,
                )
            self.state.transition_intent(
                intent_id,
                IntentState.FAILED,
                occurred_at=self._now(),
                detail={"phase": "place", "code": exc.code, "known_no_accept": True},
            )
            return self._failed(
                plan_id=normalized_plan,
                kind=normalized_kind,
                intent_id=intent_id,
                client_ref_id=client_ref_id,
                message="safety mutation failed with a known no-accept result",
                failures=(exc.code,),
            )
        except Exception as exc:
            return self._mark_unknown(
                plan_id=normalized_plan,
                kind=normalized_kind,
                intent_id=intent_id,
                client_ref_id=client_ref_id,
                occurred_at=self._now(),
                code="UNCLASSIFIED_SAFETY_PLACE_TRANSPORT_FAILURE",
                message=f"{type(exc).__name__}: broker acceptance cannot be excluded",
                replay=False,
            )
        ack_measurement = (
            _stop_latency(
                self.latency,
                submit_span,
                observed_at=result.observed_at,
                metadata={"broker_order_id": result.order.broker_order_id},
            )
            if _is_exact_place_ack(result, exact_request)
            else None
        )
        outcome = self._handle_sell_result(
            intent=intent,
            request=exact_request,
            result=result,
        )
        _persist_latency(self.latency, ack_measurement)
        return outcome

    def cancel_order(
        self,
        *,
        plan_id: str,
        target: OrderSnapshot,
        broker_snapshot: AccountSnapshot | None = None,
        now: datetime | None = None,
    ) -> SafetyExecutionOutcome:
        """Request one durable cancel without equating acceptance to completion."""

        current = self._aware(now or self._clock(), "now")
        normalized_plan = self._required(plan_id, "plan_id")
        if not isinstance(target, OrderSnapshot):
            raise TypeError("target must be an OrderSnapshot")
        operation_key, cancel_attempt, attempt_failure = self._cancel_operation_identity(
            plan_id=normalized_plan,
            target=target,
        )
        kind = IntentKind.CANCEL
        client_ref_id = self._stable_client_ref(
            plan_id=normalized_plan,
            kind=kind,
            operation_key=operation_key,
        )
        intent_id = self._stable_intent_id(
            plan_id=normalized_plan,
            kind=kind,
            operation_key=operation_key,
        )
        if attempt_failure is not None:
            return self._blocked(
                plan_id=normalized_plan,
                kind=kind,
                intent_id=intent_id,
                client_ref_id=client_ref_id,
                message="cancel retry is not authorized by conclusive newer evidence",
                failures=(attempt_failure,),
                replay=True,
                broker_order_id=target.broker_order_id,
            )

        existing = self.state.row("order_intents", "intent_id", intent_id)
        if existing is not None:
            self._validate_existing_cancel(
                existing,
                plan_id=normalized_plan,
                target=target,
                client_ref_id=client_ref_id,
            )
            prior = IntentState(existing["state"])
            if prior is IntentState.SUBMITTING:
                return self._mark_unknown(
                    plan_id=normalized_plan,
                    kind=kind,
                    intent_id=intent_id,
                    client_ref_id=client_ref_id,
                    occurred_at=current,
                    code="RESTART_OR_REPLAY_WHILE_CANCEL_SUBMITTING",
                    message="prior cancel may have reached the broker",
                    replay=True,
                    broker_order_id=target.broker_order_id,
                )
            if prior is IntentState.UNKNOWN:
                self._ensure_unknown_artifacts(
                    plan_id=normalized_plan,
                    kind=kind,
                    intent_id=intent_id,
                    client_ref_id=client_ref_id,
                    occurred_at=current,
                    code="UNKNOWN_CANCEL_REPLAY",
                    message="unknown cancel was not retried",
                    broker_order_id=target.broker_order_id,
                )
                return self._outcome_from_existing(existing, replay=True)
            if prior is not IntentState.PREPARED:
                return self._outcome_from_existing(existing, replay=True)

        failures, plan_row, owner_intent_id = self._cancel_preflight(
            plan_id=normalized_plan,
            target=target,
            now=current,
        )
        if failures:
            return self._blocked(
                plan_id=normalized_plan,
                kind=kind,
                intent_id=intent_id,
                client_ref_id=client_ref_id,
                message="cancel preflight failed; broker was not called",
                failures=failures,
                replay=existing is not None,
                broker_order_id=target.broker_order_id,
            )
        assert plan_row is not None and owner_intent_id is not None
        authority_failures = self._authority_failures(
            snapshot=broker_snapshot,
            operation=MutationOperation.CANCEL,
            phase=MutationPhase.BEFORE_PREPARE,
            now=current,
            plan_id=normalized_plan,
            kind=kind,
            target=target,
        )
        if authority_failures:
            return self._blocked(
                plan_id=normalized_plan,
                kind=kind,
                intent_id=intent_id,
                client_ref_id=client_ref_id,
                message="runtime mutation authority failed before durable cancel preparation",
                failures=authority_failures,
                replay=existing is not None,
                broker_order_id=target.broker_order_id,
            )
        cancel_tuple = {
            "operation": "cancel_equity_order",
            "account_key": str(plan_row["account_key"]),
            "account_masked": target.account_masked,
            "plan_id": normalized_plan,
            "target_broker_order_id": target.broker_order_id,
            "target_owner_intent_id": owner_intent_id,
            "target_client_ref_id": target.client_ref_id,
            # Provider update time is the cancel causality floor. A later
            # local receipt of unchanged broker facts is telemetry only.
            "target_evidence_floor_at": target.broker_updated_at.isoformat(),
            "operation_key": operation_key,
            "cancel_attempt": cancel_attempt,
            "client_ref_id": client_ref_id,
        }
        intent = OrderIntent(
            intent_id=intent_id,
            plan_id=normalized_plan,
            reservation_id=None,
            account_key=str(plan_row["account_key"]),
            kind=kind,
            client_ref=client_ref_id,
            order_tuple=cancel_tuple,
            tuple_hash=object_hash(cancel_tuple),
            created_at=current,
            acknowledgement_deadline_at=current
            + timedelta(
                seconds=int(
                    self.policy.config["execution"]["order_ack_timeout_seconds"]
                )
            ),
        )
        durable_span = _start_latency(
            self.latency,
            "durable_intent_write",
            observed_at=current,
            correlation_id=intent_id,
            metadata={"kind": kind.value, "plan_id": normalized_plan},
        )
        inserted = self.state.prepare_safety_intent(intent)
        _finish_latency(
            self.latency,
            durable_span,
            observed_at=self._now(),
            metadata={"inserted": inserted},
        )
        submitting_at = self._now()
        authority_failures, authority_snapshot = self._authority_result(
            snapshot=broker_snapshot,
            operation=MutationOperation.CANCEL,
            phase=MutationPhase.BEFORE_CANCEL,
            now=submitting_at,
            plan_id=normalized_plan,
            kind=kind,
            target=target,
        )
        if authority_failures:
            return self._blocked(
                plan_id=normalized_plan,
                kind=kind,
                intent_id=intent_id,
                client_ref_id=client_ref_id,
                message="runtime mutation authority failed immediately before broker cancel",
                failures=authority_failures,
                replay=not inserted,
                broker_order_id=target.broker_order_id,
            )
        if authority_snapshot is not None:
            refreshed_targets = tuple(
                order
                for order in authority_snapshot.equity_orders
                if order.broker_order_id == target.broker_order_id
            )
            if len(refreshed_targets) != 1:
                return self._blocked(
                    plan_id=normalized_plan,
                    kind=kind,
                    intent_id=intent_id,
                    client_ref_id=client_ref_id,
                    message="final broker snapshot did not carry one exact cancel target",
                    failures=("CANCEL_TARGET_NOT_CURRENT_AND_ACTIVE",),
                    replay=not inserted,
                    broker_order_id=target.broker_order_id,
                )
            target = refreshed_targets[0]
        self.state.transition_intent(
            intent_id,
            IntentState.SUBMITTING,
            occurred_at=submitting_at,
            detail={
                "operation": "cancel_equity_order",
                "target_broker_order_id": target.broker_order_id,
                "tuple_hash": intent.tuple_hash,
            },
        )
        submit_span = _start_latency(
            self.latency,
            "submit_to_ack",
            observed_at=submitting_at,
            correlation_id=intent_id,
            metadata={"kind": kind.value, "plan_id": normalized_plan},
        )
        try:
            result = self.broker.cancel_equity_order(
                target.account_masked,
                target.broker_order_id,
            )
        except BrokerUnknownSubmission as exc:
            return self._mark_unknown(
                plan_id=normalized_plan,
                kind=kind,
                intent_id=intent_id,
                client_ref_id=client_ref_id,
                occurred_at=self._now(),
                code=exc.code,
                message=f"{exc.code}: broker acceptance cannot be determined",
                replay=False,
                broker_order_id=target.broker_order_id,
            )
        except BrokerError as exc:
            if exc.submission_may_have_reached_broker:
                return self._mark_unknown(
                    plan_id=normalized_plan,
                    kind=kind,
                    intent_id=intent_id,
                    client_ref_id=client_ref_id,
                    occurred_at=self._now(),
                    code=exc.code,
                    message=f"{exc.code}: broker acceptance cannot be determined",
                    replay=False,
                    broker_order_id=target.broker_order_id,
                )
            self.state.transition_intent(
                intent_id,
                IntentState.FAILED,
                occurred_at=self._now(),
                detail={"phase": "cancel", "code": exc.code, "known_no_accept": True},
            )
            return self._failed(
                plan_id=normalized_plan,
                kind=kind,
                intent_id=intent_id,
                client_ref_id=client_ref_id,
                message="cancel transport failed with a known no-accept result",
                failures=(exc.code,),
                replay=not inserted,
                broker_order_id=target.broker_order_id,
            )
        except Exception as exc:
            return self._mark_unknown(
                plan_id=normalized_plan,
                kind=kind,
                intent_id=intent_id,
                client_ref_id=client_ref_id,
                occurred_at=self._now(),
                code="UNCLASSIFIED_CANCEL_TRANSPORT_FAILURE",
                message=f"{type(exc).__name__}: broker acceptance cannot be excluded",
                replay=False,
                broker_order_id=target.broker_order_id,
            )
        ack_measurement = (
            _stop_latency(
                self.latency,
                submit_span,
                observed_at=result.observed_at,
                metadata={"broker_order_id": target.broker_order_id},
            )
            if _is_exact_cancel_ack(result, target)
            else None
        )
        outcome = self._handle_cancel_result(intent=intent, target=target, result=result)
        _persist_latency(self.latency, ack_measurement)
        return outcome

    def reconcile_cancel(
        self,
        *,
        intent_id: str,
        order: OrderSnapshot,
    ) -> SafetyExecutionOutcome:
        """Resolve a pending/unknown cancel only from newer broker evidence."""

        row = self.state.row("order_intents", "intent_id", intent_id)
        if row is None or IntentKind(row["kind"]) is not IntentKind.CANCEL:
            raise ValueError("cancel reconciliation requires a durable CANCEL intent")
        order_tuple = json.loads(str(row["order_tuple_json"]))
        if order.broker_order_id != order_tuple["target_broker_order_id"]:
            raise ValueError("cancel reconciliation references a different broker order")
        floor = self._aware(
            datetime.fromisoformat(str(order_tuple["target_evidence_floor_at"])),
            "target evidence floor",
        )
        if order.broker_updated_at <= floor:
            return SafetyExecutionOutcome(
                status=ExecutionStatus.UNKNOWN,
                plan_id=str(row["plan_id"]),
                kind=IntentKind.CANCEL,
                intent_id=str(row["intent_id"]),
                client_ref_id=str(row["client_ref"]),
                exposure_reserved=True,
                replay=True,
                requires_reconciliation=True,
                message="cancel remains unresolved because broker evidence is not strictly newer",
                failure_codes=("CANCEL_EVIDENCE_NOT_STRICTLY_NEWER",),
                broker_order_id=order.broker_order_id,
            )
        owner_intent_id = str(order_tuple["target_owner_intent_id"])
        self.persist_order_evidence(intent_id=owner_intent_id, order=order)
        current = IntentState(
            self.state.row("order_intents", "intent_id", intent_id)["state"]
        )
        if current is IntentState.SUBMITTING:
            self.state.transition_intent(
                intent_id,
                IntentState.UNKNOWN,
                occurred_at=order.received_at,
                detail={"code": "RECONCILED_AFTER_INTERRUPTED_CANCEL"},
            )
            current = IntentState.UNKNOWN
        if order.state is BrokerOrderState.PENDING_CANCELLED:
            if current is IntentState.UNKNOWN:
                self.state.transition_intent(
                    intent_id,
                    IntentState.ACKNOWLEDGED,
                    occurred_at=order.received_at,
                    detail={"broker_state": order.state.value},
                )
            self._resolve_unknown_incident(intent_id, order.received_at)
            refreshed = self.state.row("order_intents", "intent_id", intent_id)
            return self._outcome_from_existing(refreshed, replay=True)
        if order.state.terminal:
            if current is IntentState.UNKNOWN:
                self.state.transition_intent(
                    intent_id,
                    IntentState.ACKNOWLEDGED,
                    occurred_at=order.received_at,
                    detail={"broker_state": order.state.value},
                )
                current = IntentState.ACKNOWLEDGED
            if current is IntentState.ACKNOWLEDGED:
                self.state.transition_intent(
                    intent_id,
                    IntentState.RECONCILED,
                    occurred_at=order.received_at,
                    detail={
                        "target_broker_order_id": order.broker_order_id,
                        "terminal_broker_state": order.state.value,
                    },
                )
            self._resolve_unknown_incident(intent_id, order.received_at)
            refreshed = self.state.row("order_intents", "intent_id", intent_id)
            outcome = self._outcome_from_existing(refreshed, replay=True)
            return replace(
                outcome,
                requires_reconciliation=True,
                message=(
                    "cancel reached a terminal broker state; refresh positions and exit capacity"
                ),
            )
        if current is IntentState.UNKNOWN and order.state.working:
            # This is positive evidence, not an absence inference: the exact
            # order is still present in a strictly newer broker fact and is
            # not pending cancellation.  Resolve this attempt as ineffective
            # so a later, independently fresh receipt may authorize one
            # bounded new cancel identity.  The same receipt cannot dispatch.
            self.state.transition_intent(
                intent_id,
                IntentState.FAILED,
                occurred_at=order.received_at,
                detail={
                    "phase": "cancel_reconciliation",
                    "known_no_accept": True,
                    "positive_working_order_evidence": True,
                    "broker_state": order.state.value,
                    "broker_updated_at": order.broker_updated_at.isoformat(),
                },
            )
            self._resolve_unknown_incident(intent_id, order.received_at)
            refreshed = self.state.row("order_intents", "intent_id", intent_id)
            return replace(
                self._outcome_from_existing(refreshed, replay=True),
                requires_reconciliation=True,
                message=(
                    "strictly newer positive broker evidence proves the prior "
                    "cancel ineffective; a later fresh receipt may authorize "
                    "one bounded cancel attempt"
                ),
                failure_codes=("CANCEL_PREVIOUS_ATTEMPT_INEFFECTIVE",),
            )
        return SafetyExecutionOutcome(
            status=(
                ExecutionStatus.ACKNOWLEDGED
                if current is IntentState.ACKNOWLEDGED
                else ExecutionStatus.UNKNOWN
            ),
            plan_id=str(row["plan_id"]),
            kind=IntentKind.CANCEL,
            intent_id=str(row["intent_id"]),
            client_ref_id=str(row["client_ref"]),
            exposure_reserved=True,
            replay=True,
            requires_reconciliation=True,
            message="newer evidence is still nonterminal; cancel remains reserved",
            failure_codes=("CANCEL_NOT_TERMINAL",),
            broker_order_id=order.broker_order_id,
        )

    def persist_order_evidence(
        self,
        *,
        intent_id: str,
        order: OrderSnapshot,
    ) -> tuple[bool, int]:
        """Persist exact normalized order/fill evidence for any local intent."""

        intent_row = self.state.row("order_intents", "intent_id", intent_id)
        if intent_row is None:
            raise ValueError("broker evidence has no durable local intent")
        order_tuple = json.loads(str(intent_row["order_tuple_json"]))
        EntryExecutionCoordinator._validate_order_identity(self, order, order_tuple)
        existing_for_intent = self.state.rows(
            "SELECT broker_order_id FROM broker_orders WHERE intent_id = ?",
            (intent_id,),
        )
        if (
            existing_for_intent
            and existing_for_intent[0]["broker_order_id"] != order.broker_order_id
        ):
            raise ValueError("a logical intent cannot acquire a different broker order id")
        raw_hash = object_hash(EntryExecutionCoordinator._order_payload(order))
        existing = self.state.row("broker_orders", "broker_order_id", order.broker_order_id)
        revision = 0 if existing is None else int(existing["revision"]) + 1
        durable_order = BrokerOrder(
            broker_order_id=order.broker_order_id,
            intent_id=intent_id,
            account_key=str(intent_row["account_key"]),
            state=order.state,
            quantity=whole_shares(order.requested_quantity),
            cumulative_filled_quantity=whole_shares(
                order.cumulative_filled_quantity,
                allow_zero=True,
            ),
            revision=revision,
            broker_updated_at=order.broker_updated_at,
            received_at=order.received_at,
            raw_hash=raw_hash,
        )
        durable_fills = tuple(
            Fill(
                fill_id=broker_fill.fill_id,
                broker_order_id=order.broker_order_id,
                account_key=str(intent_row["account_key"]),
                quantity=whole_shares(broker_fill.quantity),
                price=broker_fill.price,
                executed_at=broker_fill.executed_at,
                received_at=max(order.received_at, broker_fill.executed_at),
            )
            for broker_fill in order.fills
        )
        order_inserted = (
            False
            if existing is not None and existing["raw_hash"] == raw_hash
            else self.state.record_broker_order(durable_order)
        )
        fill_count = sum(
            1 for fill in durable_fills if self.state.record_fill(fill)
        )
        return order_inserted, fill_count

    def _sell_preflight(
        self,
        *,
        plan_id: str,
        kind: IntentKind,
        request: OrderRequest,
    ) -> tuple[tuple[str, ...], Mapping[str, object] | None]:
        failures: list[str] = []
        plan_row = self.state.row("plans", "plan_id", plan_id)
        if plan_row is None:
            failures.append("ORIGINATING_PLAN_NOT_DURABLE")
        else:
            if plan_row["account_key"] != self._account_key:
                failures.append("ORIGINATING_PLAN_ACCOUNT_MISMATCH")
            if plan_row["symbol"] != request.symbol:
                failures.append("SAFETY_ORDER_SYMBOL_MISMATCH")
            if request.quantity > int(plan_row["quantity"]):
                failures.append("SAFETY_ORDER_EXCEEDS_ORIGINATING_PLAN_QUANTITY")
        if request.account_masked != self._account_masked:
            failures.append("SAFETY_ORDER_ACCOUNT_MISMATCH")
        if request.side is not BrokerSide.SELL:
            failures.append("SAFETY_ORDER_MUST_BE_SELL")
        if kind is IntentKind.PROTECTION and plan_row is not None:
            try:
                self.policy.require_protection_tuple(
                    quantity=request.quantity,
                    stop_price=request.stop_price,
                    original_stop=str(plan_row["structural_stop"]),
                    entry_price=str(plan_row["limit_price"]),
                    market_hours=request.market_hours.value,
                    order_type=request.order_type.value,
                    time_in_force=request.time_in_force.value,
                )
            except (TypeError, ValueError) as exc:
                failures.append("PROTECTION_TUPLE_INVALID")
        failures.extend(self._policy_unattended_failures())
        try:
            failures.extend(self._order_capability_failures(self.broker.capabilities, request))
        except BrokerError as exc:
            failures.append(f"BROKER_CAPABILITY_PROBE_FAILED:{exc.code}")
        except Exception as exc:
            failures.append(f"BROKER_CAPABILITY_PROBE_FAILED:{type(exc).__name__}")
        return tuple(dict.fromkeys(failures)), plan_row

    def _seal_prepared_sell(
        self,
        *,
        plan_id: str,
        kind: IntentKind,
        request: OrderRequest,
        intent_id: str,
        replay: bool,
    ) -> SafetyExecutionOutcome | None:
        """Seal an exact prepared protection/exit before broker review."""

        if self.plan_sealer is None:
            return None
        try:
            result = self.plan_sealer(
                intent_id=intent_id,
                plan_id=plan_id,
                kind=kind,
                request=request,
            )
            if result is not None:
                raise TypeError("prepared plan sealer must return None")
        except Exception as exc:
            failed_at = self._now()
            transition_failed = False
            try:
                self.state.transition_intent(
                    intent_id,
                    IntentState.FAILED,
                    occurred_at=failed_at,
                    detail={
                        "phase": "review",
                        "code": "AUTONOMOUS_PLAN_SEAL_FAILED",
                        "error_type": type(exc).__name__,
                        "known_no_accept": True,
                        "kind": kind.value,
                    },
                )
            except Exception:
                transition_failed = True
            failures = ["AUTONOMOUS_PLAN_SEAL_FAILED"]
            if transition_failed:
                failures.append("AUTONOMOUS_PLAN_SEAL_FAILURE_NOT_DURABLE")
            return self._failed(
                plan_id=plan_id,
                kind=kind,
                intent_id=intent_id,
                client_ref_id=request.client_ref_id,
                message=(
                    "exact autonomous safety plan sealing failed before broker "
                    "review; no broker mutation was attempted"
                ),
                failures=tuple(failures),
                replay=replay,
            )
        return None

    def _cancel_preflight(
        self,
        *,
        plan_id: str,
        target: OrderSnapshot,
        now: datetime,
    ) -> tuple[tuple[str, ...], Mapping[str, object] | None, str | None]:
        failures: list[str] = []
        plan_row = self.state.row("plans", "plan_id", plan_id)
        owner_intent_id: str | None = None
        if plan_row is None:
            failures.append("ORIGINATING_PLAN_NOT_DURABLE")
        elif plan_row["account_key"] != self._account_key:
            failures.append("ORIGINATING_PLAN_ACCOUNT_MISMATCH")
        if target.account_masked != self._account_masked:
            failures.append("CANCEL_TARGET_ACCOUNT_MISMATCH")
        if plan_row is not None and target.symbol != plan_row["symbol"]:
            failures.append("CANCEL_TARGET_SYMBOL_MISMATCH")
        if target.state.terminal:
            failures.append("CANCEL_TARGET_ALREADY_TERMINAL")
        age = (now - target.broker_updated_at).total_seconds()
        max_age = int(self.policy.config["evidence"]["broker_snapshot_max_age_seconds"])
        if age < -1 or age > max_age:
            failures.append("CANCEL_TARGET_EVIDENCE_STALE_OR_FUTURE")
        local_order = self.state.row(
            "broker_orders", "broker_order_id", target.broker_order_id
        )
        if local_order is None:
            failures.append("CANCEL_TARGET_NOT_LOCALLY_OWNED")
        else:
            owner_intent_id = str(local_order["intent_id"])
            local_state = BrokerOrderState(local_order["state"])
            if local_state.terminal:
                failures.append("CANCEL_TARGET_ALREADY_TERMINAL_IN_DURABLE_STATE")
            local_received_at = self._aware(
                datetime.fromisoformat(str(local_order["received_at"])),
                "durable cancel target received_at",
            )
            local_updated_at = self._aware(
                datetime.fromisoformat(str(local_order["broker_updated_at"])),
                "durable cancel target broker_updated_at",
            )
            if (
                target.received_at < local_received_at
                or target.broker_updated_at < local_updated_at
                or whole_shares(
                    target.cumulative_filled_quantity,
                    allow_zero=True,
                )
                < int(local_order["cumulative_filled_quantity"])
            ):
                failures.append("CANCEL_TARGET_PREDATES_DURABLE_BROKER_EVIDENCE")
            owner_intent = self.state.row(
                "order_intents", "intent_id", owner_intent_id
            )
            if owner_intent is None or owner_intent["plan_id"] != plan_id:
                failures.append("CANCEL_TARGET_PLAN_MISMATCH")
            else:
                try:
                    expected = json.loads(str(owner_intent["order_tuple_json"]))
                    EntryExecutionCoordinator._validate_order_identity(
                        self, target, expected
                    )
                except (TypeError, ValueError, KeyError) as exc:
                    failures.append("CANCEL_TARGET_IDENTITY_MISMATCH")
        failures.extend(self._policy_unattended_failures())
        try:
            failures.extend(self._cancel_capability_failures(self.broker.capabilities))
        except BrokerError as exc:
            failures.append(f"BROKER_CAPABILITY_PROBE_FAILED:{exc.code}")
        except Exception as exc:
            failures.append(f"BROKER_CAPABILITY_PROBE_FAILED:{type(exc).__name__}")
        return tuple(dict.fromkeys(failures)), plan_row, owner_intent_id

    def _policy_unattended_failures(self) -> tuple[str, ...]:
        execution = self.policy.config["execution"]
        checks = (
            (
                execution.get("supported_unattended_mutation") is True,
                "SUPPORTED_UNATTENDED_MUTATION_NOT_ATTESTED",
            ),
            (
                execution.get("per_mutation_user_confirmation_required") is False,
                "PER_MUTATION_CONFIRMATION_STILL_REQUIRED",
            ),
        )
        return tuple(code for passed, code in checks if not passed)

    def _order_capability_failures(
        self,
        capabilities: BrokerCapabilities,
        request: OrderRequest,
    ) -> tuple[str, ...]:
        checks = (
            (capabilities.account_masked == request.account_masked, "BROKER_ACCOUNT_MISMATCH"),
            (capabilities.daemon_transport_configured, "DAEMON_BROKER_TRANSPORT_UNAVAILABLE"),
            (capabilities.supports_daemon_writes, "DAEMON_BROKER_WRITES_UNSUPPORTED"),
            (capabilities.supports_unattended_writes, "UNATTENDED_BROKER_WRITES_UNSUPPORTED"),
            (capabilities.supports_equity_order_read, "EQUITY_ORDER_READ_UNSUPPORTED"),
            (capabilities.supports_equity_review, "EQUITY_REVIEW_UNSUPPORTED"),
            (capabilities.supports_equity_place, "EQUITY_PLACE_UNSUPPORTED"),
            (capabilities.supports_equity_cancel, "EQUITY_CANCEL_UNSUPPORTED"),
            (capabilities.supports_ref_id_lookup, "CLIENT_REF_LOOKUP_UNSUPPORTED"),
            (not capabilities.review_requires_explicit_confirmation, "REVIEW_CONFIRMATION_REQUIRED"),
            (request.order_type in capabilities.supported_order_types, "ORDER_TYPE_UNSUPPORTED"),
            (request.market_hours in capabilities.supported_market_hours, "MARKET_HOURS_UNSUPPORTED"),
            (request.time_in_force in capabilities.supported_time_in_force, "TIME_IN_FORCE_UNSUPPORTED"),
        )
        failures = [code for passed, code in checks if not passed]
        if (
            self.policy.config["evidence"].get("require_advanced_order_reconciliation")
            is True
            and not capabilities.order_coverage.family_complete(
                OrderFamily.ADVANCED_EQUITY
            )
        ):
            failures.append("ADVANCED_ORDER_READ_UNSUPPORTED")
        return tuple(failures)

    def _cancel_capability_failures(
        self, capabilities: BrokerCapabilities
    ) -> tuple[str, ...]:
        checks = (
            (capabilities.account_masked == self._account_masked, "BROKER_ACCOUNT_MISMATCH"),
            (capabilities.daemon_transport_configured, "DAEMON_BROKER_TRANSPORT_UNAVAILABLE"),
            (capabilities.supports_daemon_writes, "DAEMON_BROKER_WRITES_UNSUPPORTED"),
            (capabilities.supports_unattended_writes, "UNATTENDED_BROKER_WRITES_UNSUPPORTED"),
            (capabilities.supports_equity_order_read, "EQUITY_ORDER_READ_UNSUPPORTED"),
            (capabilities.supports_equity_cancel, "EQUITY_CANCEL_UNSUPPORTED"),
            (not capabilities.cancel_requires_explicit_confirmation, "CANCEL_CONFIRMATION_REQUIRED"),
        )
        failures = [code for passed, code in checks if not passed]
        if (
            self.policy.config["evidence"].get("require_advanced_order_reconciliation")
            is True
            and not capabilities.order_coverage.family_complete(
                OrderFamily.ADVANCED_EQUITY
            )
        ):
            failures.append("ADVANCED_ORDER_READ_UNSUPPORTED")
        return tuple(failures)

    def _validate_review(
        self,
        *,
        review: ReviewReceipt,
        request: OrderRequest,
        checked_at: datetime,
    ) -> tuple[str, ...]:
        failures: list[str] = []
        if not isinstance(review, ReviewReceipt):
            return ("BROKER_REVIEW_NOT_NORMALIZED",)
        if review.request.exact_tuple != request.exact_tuple:
            failures.append("BROKER_REVIEW_TUPLE_MISMATCH")
        failures.extend(
            _review_provenance_failures(review, self.broker.capabilities)
        )
        if review.expires_at is None or review.expired_at(checked_at):
            failures.append("BROKER_REVIEW_EXPIRED_OR_UNBOUNDED")
        age = (checked_at - review.reviewed_at).total_seconds()
        if age < -1 or age > int(
            self.policy.config["evidence"]["quote_max_age_seconds"]
        ):
            failures.append("BROKER_REVIEW_STALE_OR_FUTURE")
        if review.required_confirmation_phrase is not None:
            failures.append("BROKER_REVIEW_REQUIRES_ATTENDED_CONFIRMATION")
        if not review.disclosure.strip():
            failures.append("BROKER_REVIEW_DISCLOSURE_MISSING")
        failures.extend(
            f"BROKER_REVIEW_CHECK:{check.code}" for check in review.order_checks
        )
        return tuple(dict.fromkeys(failures))

    def _handle_sell_result(
        self,
        *,
        intent: OrderIntent,
        request: OrderRequest,
        result: BrokerOperationResult,
    ) -> SafetyExecutionOutcome:
        if not isinstance(result, BrokerOperationResult):
            return self._mark_unknown(
                plan_id=intent.plan_id,
                kind=intent.kind,
                intent_id=intent.intent_id,
                client_ref_id=request.client_ref_id,
                occurred_at=self._now(),
                code="UNNORMALIZED_SAFETY_PLACE_RESULT",
                message="sell transport returned an unnormalized result",
                replay=False,
            )
        if result.status is OperationStatus.REJECTED and result.accepted is False:
            evidence_failures: tuple[str, ...] = ()
            if result.order is not None:
                try:
                    self.persist_order_evidence(
                        intent_id=intent.intent_id,
                        order=result.order,
                    )
                except Exception as exc:
                    evidence_failures = (
                        f"REJECT_EVIDENCE_INVALID:{type(exc).__name__}",
                    )
            self.state.transition_intent(
                intent.intent_id,
                IntentState.REJECTED,
                occurred_at=result.observed_at,
                detail={"message": result.message, "known_reject": True},
            )
            return SafetyExecutionOutcome(
                status=ExecutionStatus.REJECTED,
                plan_id=intent.plan_id,
                kind=intent.kind,
                intent_id=intent.intent_id,
                client_ref_id=request.client_ref_id,
                exposure_reserved=True,
                replay=False,
                requires_reconciliation=True,
                message=result.message,
                failure_codes=("BROKER_KNOWN_REJECTION",) + evidence_failures,
                broker_order_id=(
                    result.order.broker_order_id if result.order is not None else None
                ),
            )
        if result.accepted is False:
            self.state.transition_intent(
                intent.intent_id,
                IntentState.FAILED,
                occurred_at=result.observed_at,
                detail={"message": result.message, "known_no_accept": True},
            )
            return self._failed(
                plan_id=intent.plan_id,
                kind=intent.kind,
                intent_id=intent.intent_id,
                client_ref_id=request.client_ref_id,
                message="broker returned a known no-accept safety result",
                failures=("BROKER_KNOWN_NO_ACCEPT",),
            )
        if (
            result.status is not OperationStatus.ACKNOWLEDGED
            or result.accepted is not True
            or result.order is None
        ):
            return self._mark_unknown(
                plan_id=intent.plan_id,
                kind=intent.kind,
                intent_id=intent.intent_id,
                client_ref_id=request.client_ref_id,
                occurred_at=result.observed_at,
                code="AMBIGUOUS_SAFETY_PLACE_RESULT",
                message="sell result did not contain exact broker order evidence",
                replay=False,
            )
        try:
            self.persist_order_evidence(intent_id=intent.intent_id, order=result.order)
        except Exception as exc:
            current = IntentState(
                self.state.row("order_intents", "intent_id", intent.intent_id)["state"]
            )
            if current is IntentState.SUBMITTING:
                return self._mark_unknown(
                    plan_id=intent.plan_id,
                    kind=intent.kind,
                    intent_id=intent.intent_id,
                    client_ref_id=request.client_ref_id,
                    occurred_at=self._now(),
                    code="SAFETY_ACKNOWLEDGEMENT_PERSISTENCE_FAILED",
                    message=f"broker acknowledged but persistence failed: {type(exc).__name__}",
                    replay=False,
                )
            self._ensure_unknown_artifacts(
                plan_id=intent.plan_id,
                kind=intent.kind,
                intent_id=intent.intent_id,
                client_ref_id=request.client_ref_id,
                occurred_at=self._now(),
                code="SAFETY_FILL_PERSISTENCE_INCOMPLETE",
                message=f"order acknowledgement persisted but fill persistence failed: {type(exc).__name__}",
                broker_order_id=result.order.broker_order_id,
            )
            return SafetyExecutionOutcome(
                status=ExecutionStatus.ACKNOWLEDGED,
                plan_id=intent.plan_id,
                kind=intent.kind,
                intent_id=intent.intent_id,
                client_ref_id=request.client_ref_id,
                exposure_reserved=True,
                replay=False,
                requires_reconciliation=True,
                message="broker order acknowledgement is durable but fill evidence must reconcile",
                failure_codes=("SAFETY_FILL_PERSISTENCE_INCOMPLETE",),
                broker_order_id=result.order.broker_order_id,
            )
        return SafetyExecutionOutcome(
            status=ExecutionStatus.ACKNOWLEDGED,
            plan_id=intent.plan_id,
            kind=intent.kind,
            intent_id=intent.intent_id,
            client_ref_id=request.client_ref_id,
            exposure_reserved=True,
            replay=False,
            requires_reconciliation=True,
            message="exact broker order acknowledgement was durably recorded; working/filled state must reconcile",
            broker_order_id=result.order.broker_order_id,
        )

    def _handle_cancel_result(
        self,
        *,
        intent: OrderIntent,
        target: OrderSnapshot,
        result: BrokerOperationResult,
    ) -> SafetyExecutionOutcome:
        if not isinstance(result, BrokerOperationResult):
            return self._mark_unknown(
                plan_id=intent.plan_id,
                kind=IntentKind.CANCEL,
                intent_id=intent.intent_id,
                client_ref_id=intent.client_ref,
                occurred_at=self._now(),
                code="UNNORMALIZED_CANCEL_RESULT",
                message="cancel transport returned an unnormalized result",
                replay=False,
                broker_order_id=target.broker_order_id,
            )
        if result.order is not None:
            if result.order.broker_order_id != target.broker_order_id:
                return self._mark_unknown(
                    plan_id=intent.plan_id,
                    kind=IntentKind.CANCEL,
                    intent_id=intent.intent_id,
                    client_ref_id=intent.client_ref,
                    occurred_at=result.observed_at,
                    code="CANCEL_RESULT_ORDER_MISMATCH",
                    message="cancel response referenced a different broker order",
                    replay=False,
                    broker_order_id=target.broker_order_id,
                )
            owner_intent_id = str(intent.order_tuple["target_owner_intent_id"])
            try:
                self.persist_order_evidence(
                    intent_id=owner_intent_id,
                    order=result.order,
                )
            except Exception as exc:
                return self._mark_unknown(
                    plan_id=intent.plan_id,
                    kind=IntentKind.CANCEL,
                    intent_id=intent.intent_id,
                    client_ref_id=intent.client_ref,
                    occurred_at=result.observed_at,
                    code="CANCEL_EVIDENCE_PERSISTENCE_FAILED",
                    message=f"cancel evidence could not be persisted: {type(exc).__name__}",
                    replay=False,
                    broker_order_id=target.broker_order_id,
                )
        if result.status is OperationStatus.REJECTED and result.accepted is False:
            self.state.transition_intent(
                intent.intent_id,
                IntentState.REJECTED,
                occurred_at=result.observed_at,
                detail={"message": result.message, "known_reject": True},
            )
            return SafetyExecutionOutcome(
                status=ExecutionStatus.REJECTED,
                plan_id=intent.plan_id,
                kind=IntentKind.CANCEL,
                intent_id=intent.intent_id,
                client_ref_id=intent.client_ref,
                exposure_reserved=True,
                replay=False,
                requires_reconciliation=True,
                message="cancel was rejected; refresh order, fills, position, and exit capacity",
                failure_codes=("BROKER_CANCEL_REJECTED",),
                broker_order_id=target.broker_order_id,
            )
        if (
            result.status is OperationStatus.CANCELLED
            and result.accepted is True
            and result.order is not None
            and result.order.state.terminal
        ):
            self.state.transition_intent(
                intent.intent_id,
                IntentState.ACKNOWLEDGED,
                occurred_at=result.observed_at,
                detail={"broker_state": result.order.state.value},
            )
            self.state.transition_intent(
                intent.intent_id,
                IntentState.RECONCILED,
                occurred_at=result.observed_at,
                detail={"terminal_broker_state": result.order.state.value},
            )
            return SafetyExecutionOutcome(
                status=ExecutionStatus.ACKNOWLEDGED,
                plan_id=intent.plan_id,
                kind=IntentKind.CANCEL,
                intent_id=intent.intent_id,
                client_ref_id=intent.client_ref,
                exposure_reserved=True,
                replay=False,
                requires_reconciliation=True,
                message="cancel is terminal at the broker; refresh position and capacity before any replacement",
                broker_order_id=target.broker_order_id,
            )
        if (
            result.status is OperationStatus.PENDING_CANCEL
            and result.accepted is True
            and result.order is not None
            and result.order.state is BrokerOrderState.PENDING_CANCELLED
        ):
            self.state.transition_intent(
                intent.intent_id,
                IntentState.ACKNOWLEDGED,
                occurred_at=result.observed_at,
                detail={
                    "broker_state": result.order.state.value,
                    "terminal": False,
                },
            )
            return SafetyExecutionOutcome(
                status=ExecutionStatus.ACKNOWLEDGED,
                plan_id=intent.plan_id,
                kind=IntentKind.CANCEL,
                intent_id=intent.intent_id,
                client_ref_id=intent.client_ref,
                exposure_reserved=True,
                replay=False,
                requires_reconciliation=True,
                message="cancel request was accepted but is nonterminal; replacement is blocked pending reconciliation",
                failure_codes=("CANCEL_ACCEPTED_PENDING_RECONCILIATION",),
                broker_order_id=target.broker_order_id,
            )
        if result.accepted is False:
            self.state.transition_intent(
                intent.intent_id,
                IntentState.FAILED,
                occurred_at=result.observed_at,
                detail={"message": result.message, "known_no_accept": True},
            )
            return self._failed(
                plan_id=intent.plan_id,
                kind=IntentKind.CANCEL,
                intent_id=intent.intent_id,
                client_ref_id=intent.client_ref,
                message="broker returned a known no-accept cancel result",
                failures=("BROKER_CANCEL_KNOWN_NO_ACCEPT",),
                broker_order_id=target.broker_order_id,
            )
        return self._mark_unknown(
            plan_id=intent.plan_id,
            kind=IntentKind.CANCEL,
            intent_id=intent.intent_id,
            client_ref_id=intent.client_ref,
            occurred_at=result.observed_at,
            code="AMBIGUOUS_CANCEL_RESULT",
            message="cancel result did not prove either rejection or broker state",
            replay=False,
            broker_order_id=target.broker_order_id,
        )

    def _mark_unknown(
        self,
        *,
        plan_id: str,
        kind: IntentKind,
        intent_id: str,
        client_ref_id: str,
        occurred_at: datetime,
        code: str,
        message: str,
        replay: bool,
        broker_order_id: str | None = None,
    ) -> SafetyExecutionOutcome:
        row = self.state.row("order_intents", "intent_id", intent_id)
        if row is None:
            raise ValueError("cannot mark an undurable safety mutation UNKNOWN")
        current = IntentState(row["state"])
        if current is IntentState.SUBMITTING:
            self.state.transition_intent(
                intent_id,
                IntentState.UNKNOWN,
                occurred_at=occurred_at,
                detail={"code": code, "automatic_retry_allowed": False},
            )
        elif current is not IntentState.UNKNOWN:
            raise ValueError(f"cannot mark {current.value} safety intent UNKNOWN")
        self._ensure_unknown_artifacts(
            plan_id=plan_id,
            kind=kind,
            intent_id=intent_id,
            client_ref_id=client_ref_id,
            occurred_at=occurred_at,
            code=code,
            message=message,
            broker_order_id=broker_order_id,
        )
        return SafetyExecutionOutcome(
            status=ExecutionStatus.UNKNOWN,
            plan_id=plan_id,
            kind=kind,
            intent_id=intent_id,
            client_ref_id=client_ref_id,
            exposure_reserved=True,
            replay=replay,
            requires_reconciliation=True,
            message="safety mutation may have reached the broker; exposure remains reserved and no retry was attempted",
            failure_codes=(code,),
            broker_order_id=broker_order_id,
        )

    def _ensure_unknown_artifacts(
        self,
        *,
        plan_id: str,
        kind: IntentKind,
        intent_id: str,
        client_ref_id: str,
        occurred_at: datetime,
        code: str,
        message: str,
        broker_order_id: str | None = None,
    ) -> None:
        incident_id = self._unknown_incident_id(intent_id)
        plan_row = self.state.row("plans", "plan_id", plan_id)
        symbol = str(plan_row["symbol"]) if plan_row is not None else "UNKNOWN"
        category = f"BROKER_{kind.value}_SUBMISSION_UNKNOWN"
        if self.state.row("incidents", "incident_id", incident_id) is None:
            self.state.record_incident(
                Incident(
                    incident_id=incident_id,
                    account_key=self._account_key,
                    category=category,
                    severity=IncidentSeverity.CRITICAL,
                    opened_at=occurred_at,
                    detail={
                        "plan_id": plan_id,
                        "intent_id": intent_id,
                        "kind": kind.value,
                        "client_ref_id": client_ref_id,
                        "broker_order_id": broker_order_id,
                        "code": code,
                        "message": message,
                        "exposure_reserved": True,
                        "automatic_retry_allowed": False,
                    },
                )
            )
        message_id = str(
            uuid5(_EXECUTION_NAMESPACE, f"safety-unknown-notification:{intent_id}")
        )
        if self.state.row("notification_outbox", "message_id", message_id) is None:
            self.state.enqueue_notification(
                OutboxMessage(
                    message_id=message_id,
                    event_key=f"broker-{kind.value.lower()}-unknown:{intent_id}",
                    account_key=self._account_key,
                    template=category,
                    payload={
                        "account": f"ending-{self.policy.account_last4}",
                        "symbol": symbol,
                        "intent_id": intent_id,
                        "kind": kind.value,
                        "state": "UNKNOWN",
                        "exposure_reserved": True,
                        "retry": "BLOCKED_PENDING_RECONCILIATION",
                    },
                    created_at=occurred_at,
                )
            )

    def _resolve_unknown_incident(self, intent_id: str, resolved_at: datetime) -> None:
        incident_id = self._unknown_incident_id(intent_id)
        if self.state.row("incidents", "incident_id", incident_id) is not None:
            self.state.resolve_incident(incident_id, resolved_at=resolved_at)

    def _outcome_from_existing(
        self, row: Mapping[str, object], *, replay: bool
    ) -> SafetyExecutionOutcome:
        state = IntentState(row["state"])
        status_map = {
            IntentState.ACKNOWLEDGED: ExecutionStatus.ACKNOWLEDGED,
            IntentState.RECONCILED: ExecutionStatus.ACKNOWLEDGED,
            IntentState.REJECTED: ExecutionStatus.REJECTED,
            IntentState.FAILED: ExecutionStatus.FAILED,
            IntentState.CANCELLED: ExecutionStatus.FAILED,
            IntentState.UNKNOWN: ExecutionStatus.UNKNOWN,
        }
        order_tuple = json.loads(str(row["order_tuple_json"]))
        orders = self.state.rows(
            "SELECT broker_order_id FROM broker_orders WHERE intent_id = ?",
            (row["intent_id"],),
        )
        broker_order_id = (
            str(orders[0]["broker_order_id"])
            if orders
            else order_tuple.get("target_broker_order_id")
        )
        return SafetyExecutionOutcome(
            status=status_map[state],
            plan_id=str(row["plan_id"]),
            kind=IntentKind(row["kind"]),
            intent_id=str(row["intent_id"]),
            client_ref_id=str(row["client_ref"]),
            exposure_reserved=True,
            replay=replay,
            requires_reconciliation=state is not IntentState.RECONCILED,
            message=f"safety intent already exists in durable state {state.value}; broker was not called",
            broker_order_id=(str(broker_order_id) if broker_order_id else None),
        )

    def _blocked(
        self,
        *,
        plan_id: str,
        kind: IntentKind,
        intent_id: str,
        client_ref_id: str,
        message: str,
        failures: tuple[str, ...],
        replay: bool = False,
        broker_order_id: str | None = None,
    ) -> SafetyExecutionOutcome:
        return SafetyExecutionOutcome(
            status=ExecutionStatus.BLOCKED,
            plan_id=plan_id,
            kind=kind,
            intent_id=intent_id,
            client_ref_id=client_ref_id,
            exposure_reserved=True,
            replay=replay,
            requires_reconciliation=True,
            message=message,
            failure_codes=failures,
            broker_order_id=broker_order_id,
        )

    def _failed(
        self,
        *,
        plan_id: str,
        kind: IntentKind,
        intent_id: str,
        client_ref_id: str,
        message: str,
        failures: tuple[str, ...],
        replay: bool = False,
        broker_order_id: str | None = None,
    ) -> SafetyExecutionOutcome:
        return SafetyExecutionOutcome(
            status=ExecutionStatus.FAILED,
            plan_id=plan_id,
            kind=kind,
            intent_id=intent_id,
            client_ref_id=client_ref_id,
            exposure_reserved=True,
            replay=replay,
            requires_reconciliation=True,
            message=message,
            failure_codes=failures,
            broker_order_id=broker_order_id,
        )

    def _validate_existing_safety_intent(
        self,
        row: Mapping[str, object],
        *,
        plan_id: str,
        kind: IntentKind,
        client_ref_id: str,
        order_tuple: Mapping[str, object],
    ) -> None:
        if (
            row["plan_id"] != plan_id
            or row["reservation_id"] is not None
            or row["kind"] != kind.value
            or row["client_ref"] != client_ref_id
            or json.loads(str(row["order_tuple_json"])) != order_tuple
            or row["tuple_hash"] != object_hash(order_tuple)
        ):
            raise ValueError("durable safety-order replay conflicts with exact tuple")

    def _validate_existing_cancel(
        self,
        row: Mapping[str, object],
        *,
        plan_id: str,
        target: OrderSnapshot,
        client_ref_id: str,
    ) -> None:
        order_tuple = json.loads(str(row["order_tuple_json"]))
        if (
            row["plan_id"] != plan_id
            or row["reservation_id"] is not None
            or row["kind"] != IntentKind.CANCEL.value
            or row["client_ref"] != client_ref_id
            or order_tuple.get("operation") != "cancel_equity_order"
            or order_tuple.get("target_broker_order_id") != target.broker_order_id
            or order_tuple.get("target_client_ref_id") != target.client_ref_id
            or row["tuple_hash"] != object_hash(order_tuple)
        ):
            raise ValueError("durable cancel replay conflicts with exact target")

    def _cancel_operation_identity(
        self,
        *,
        plan_id: str,
        target: OrderSnapshot,
    ) -> tuple[str, int, str | None]:
        """Choose one bounded cancel identity without retrying ambiguity.

        The first logical cancel keeps the historical broker-order-id identity.
        A later identity is minted only when the immediately prior attempt has
        durable terminal proof of ``known_no_accept`` and the target arrived in
        a strictly newer broker receipt.  Accepted, pending, SUBMITTING,
        UNKNOWN, or otherwise ambiguous attempts always replay their old
        identity and never grant another broker call.
        """

        rows = self.state.rows(
            "SELECT * FROM order_intents WHERE account_key=? AND plan_id=? "
            "AND kind='CANCEL' ORDER BY created_at, intent_id",
            (self._account_key, plan_id),
        )
        attempts: list[tuple[int, Mapping[str, object], Mapping[str, object]]] = []
        for row in rows:
            try:
                payload = json.loads(str(row["order_tuple_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if payload.get("target_broker_order_id") != target.broker_order_id:
                continue
            raw_attempt = payload.get("cancel_attempt", 1)
            if type(raw_attempt) is not int or raw_attempt <= 0:
                continue
            attempts.append((raw_attempt, row, payload))
        if not attempts:
            return target.broker_order_id, 1, None

        attempt, row, payload = max(attempts, key=lambda item: item[0])
        operation_key = str(
            payload.get("operation_key")
            or (
                target.broker_order_id
                if attempt == 1
                else f"{target.broker_order_id}:attempt:{attempt}"
            )
        )
        state = IntentState(str(row["state"]))
        if state not in {IntentState.FAILED, IntentState.REJECTED}:
            return operation_key, attempt, None
        if not self._intent_has_known_no_accept(row):
            return operation_key, attempt, None
        if attempt >= _MAX_KNOWN_NO_ACCEPT_CANCEL_ATTEMPTS:
            return (
                operation_key,
                attempt,
                "CANCEL_KNOWN_NO_ACCEPT_ATTEMPTS_EXHAUSTED",
            )
        prior_updated = self._aware(
            datetime.fromisoformat(str(row["updated_at"])),
            "prior cancel updated_at",
        )
        if target.received_at <= prior_updated:
            return (
                operation_key,
                attempt,
                "CANCEL_RETRY_REQUIRES_STRICTLY_NEWER_BROKER_RECEIPT",
            )
        next_attempt = attempt + 1
        return (
            f"{target.broker_order_id}:attempt:{next_attempt}",
            next_attempt,
            None,
        )

    def _intent_has_known_no_accept(self, row: Mapping[str, object]) -> bool:
        state = IntentState(str(row["state"]))
        if state not in {IntentState.FAILED, IntentState.REJECTED}:
            return False
        events = self.state.rows(
            "SELECT payload_json FROM audit_events "
            "WHERE entity_type='order_intent' AND entity_id=? AND event_type=? "
            "ORDER BY sequence DESC LIMIT 1",
            (row["intent_id"], f"INTENT_{state.value}"),
        )
        if len(events) != 1:
            return False
        try:
            detail = json.loads(str(events[0]["payload_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        if state is IntentState.REJECTED:
            return detail.get("known_reject") is True
        return bool(
            detail.get("known_no_accept") is True
            or detail.get("phase") in {"review", "review_validation"}
        )

    def _sell_tuple(
        self,
        *,
        plan_id: str,
        kind: IntentKind,
        operation_key: str,
        request: OrderRequest,
    ) -> dict[str, object]:
        body = EntryExecutionCoordinator._order_tuple(self._account_key, request)
        body.update(
            {
                "operation": "place_equity_order",
                "plan_id": plan_id,
                "kind": kind.value,
                "operation_key": operation_key,
            }
        )
        return body

    def _stable_client_ref(
        self, *, plan_id: str, kind: IntentKind, operation_key: str
    ) -> str:
        return str(
            uuid5(
                _EXECUTION_NAMESPACE,
                f"safety-ref:{self.policy.account_last4}:{plan_id}:{kind.value}:{operation_key}",
            )
        )

    def _stable_intent_id(
        self, *, plan_id: str, kind: IntentKind, operation_key: str
    ) -> str:
        return str(
            uuid5(
                _EXECUTION_NAMESPACE,
                f"safety-intent:{self.policy.account_last4}:{plan_id}:{kind.value}:{operation_key}",
            )
        )

    @staticmethod
    def _unknown_incident_id(intent_id: str) -> str:
        return str(uuid5(_EXECUTION_NAMESPACE, f"safety-unknown-incident:{intent_id}"))

    @staticmethod
    def _sell_kind(kind: IntentKind) -> IntentKind:
        try:
            normalized = IntentKind(kind)
        except (TypeError, ValueError) as exc:
            raise ValueError("kind must be PROTECTION or EXIT") from exc
        if normalized not in {IntentKind.PROTECTION, IntentKind.EXIT}:
            raise ValueError("kind must be PROTECTION or EXIT")
        return normalized

    @staticmethod
    def _required(value: object, field: str) -> str:
        normalized = str(value).strip()
        if not normalized:
            raise ValueError(f"{field} is required")
        return normalized

    @property
    def _account_key(self) -> str:
        return self.policy.account_key

    @property
    def _account_masked(self) -> str:
        return f"••••{self.policy.account_last4}"

    def _now(self) -> datetime:
        return self._aware(self._clock(), "clock")

    def _authority_failures(
        self,
        *,
        snapshot: AccountSnapshot | None,
        operation: MutationOperation,
        phase: MutationPhase,
        now: datetime,
        plan_id: str,
        kind: IntentKind,
        request: OrderRequest | None = None,
        target: OrderSnapshot | None = None,
        plan: object | None = None,
        risk_decision: object | None = None,
    ) -> tuple[str, ...]:
        failures, _ = self._authority_result(
            snapshot=snapshot,
            operation=operation,
            phase=phase,
            now=now,
            plan_id=plan_id,
            kind=kind,
            request=request,
            target=target,
            plan=plan,
            risk_decision=risk_decision,
        )
        return failures

    def _authority_result(
        self,
        *,
        snapshot: AccountSnapshot | None,
        operation: MutationOperation,
        phase: MutationPhase,
        now: datetime,
        plan_id: str,
        kind: IntentKind,
        request: OrderRequest | None = None,
        target: OrderSnapshot | None = None,
        plan: object | None = None,
        risk_decision: object | None = None,
    ) -> tuple[tuple[str, ...], AccountSnapshot | None]:
        try:
            result = self.authority.require_mutation_authority(
                snapshot=snapshot,
                operation=operation,
                phase=phase,
                now=now,
                plan_id=plan_id,
                kind=kind,
                request=request,
                target=target,
                plan=plan,
                risk_decision=risk_decision,
            )
            return (), result if isinstance(result, AccountSnapshot) else None
        except MutationAuthorityDenied as exc:
            return exc.failure_codes, None
        except Exception as exc:
            return (
                (f"MUTATION_AUTHORITY_CAPABILITY_FAILED:{type(exc).__name__}",),
                None,
            )

    @staticmethod
    def _aware(value: datetime, field: str) -> datetime:
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError(f"{field} must be timezone-aware")
        return value.astimezone(timezone.utc)


__all__ = [
    "EntryExecutionCoordinator",
    "ExecutionOutcome",
    "ExecutionStatus",
    "PreparedOrderPlanSealer",
    "SafetyExecutionCoordinator",
    "SafetyExecutionOutcome",
]
