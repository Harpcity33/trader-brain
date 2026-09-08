"""Fail-closed lifecycle adapter for broker protection, exits, and cancels.

The service owns sequencing and reconciliation.  This adapter turns those
decisions into calls to :class:`SafetyExecutionCoordinator` while preserving
three independent authority barriers: an explicitly enabled adapter, the
durable runtime activation, and the account-scoped kernel/database writer
lease.  Its default is intentionally non-mutating.

No connector is discovered here and no credentials are loaded.  The broker
object is injected, and all broker writes still pass through the durable
review/submission boundary in ``execution.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, time, timezone
from decimal import Decimal
import hashlib
import json
import os
from typing import Callable, Mapping, Protocol
from uuid import UUID

from .broker import (
    AccountSnapshot,
    BrokerClient,
    BrokerSide,
    EquityOrderType,
    MarketHours,
    OrderRequest,
    OrderFamily,
    OrderSnapshot,
    PositionSnapshot,
    TimeInForce,
)
from .authority import (
    MutationAuthorityDenied,
    MutationOperation,
    MutationPhase,
)
from .calendar import NEW_YORK
from .execution import (
    ExecutionStatus,
    SafetyExecutionCoordinator,
    SafetyExecutionOutcome,
)
from .latency import LatencyRecorder
from .exits import (
    ExitAction,
    ExitCapacityError,
    SafeCloseDecision,
    calculate_exit_capacity,
    plan_safe_close,
    require_exit_capacity,
)
from .models import (
    BrokerOrderState,
    EngineMode,
    IntentKind,
    IntentState,
    ProtectionObligation,
    ProtectionState,
)
from .money import whole_shares
from .plans import ExpiringPlan as SignedPlan
from .policy import PolicyBundle
from .protection import (
    ProtectionAction,
    ProtectionDecision,
    assess_protection,
    is_verified_working_protection,
    load_open_obligations,
)
from .risk_runtime import (
    RiskDecision as RuntimeRiskDecision,
    SessionLatch as RuntimeSessionLatch,
    evaluate_entry,
)
from .state import LiveStateStore
from .writer_lock import AccountWriterLock, account_writer_fingerprint


_PLACEHOLDER_CLIENT_REF = str(UUID(int=0))
_MUTATION_MODES = frozenset(
    {
        EngineMode.RECONCILING,
        EngineMode.ACTIVE,
        EngineMode.PAUSE_NEW_ENTRIES,
        EngineMode.MANAGED_CLOSEOUT,
        EngineMode.INCIDENT,
    }
)
_PENDING_ORDER_STATES = frozenset(
    {
        BrokerOrderState.UNKNOWN,
        BrokerOrderState.PENDING,
        BrokerOrderState.QUEUED,
        BrokerOrderState.UNCONFIRMED,
        BrokerOrderState.LOCATING,
    }
)
_FAILED_ORDER_STATES = frozenset(
    {
        BrokerOrderState.CANCELLED,
        BrokerOrderState.PARTIALLY_FILLED_REST_CANCELLED,
        BrokerOrderState.REJECTED,
        BrokerOrderState.FAILED,
        BrokerOrderState.VOIDED,
        BrokerOrderState.LOCATE_FAILED,
    }
)


class DiscoveryExecutor(Protocol):
    """Optional entry pipeline kept behind the same activation/lease gate."""

    def execute(
        self, *, snapshot: AccountSnapshot, now: datetime
    ) -> tuple[str, ...]: ...

    def final_entry_evidence_failures(
        self, *, plan: object, request: OrderRequest, now: datetime
    ) -> tuple[str, ...]: ...


@dataclass(frozen=True)
class LifecycleReconcileResult:
    """Read-only-broker lifecycle convergence result for one service tick."""

    actions: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()


@dataclass(frozen=True)
class _OwnedExposure:
    plan_id: str
    quantity: int
    fingerprint: str


class ProductionLifecycleActions:
    """Concrete lifecycle actions with zero broker mutations by default.

    ``allow_mutations`` is a local deployment interlock, not authority by
    itself.  Even when true, every action requires a matching armed runtime,
    held kernel lock and database lease, fresh complete broker evidence, an
    unattended-capable connector, and an exact broker review.
    """

    def __init__(
        self,
        *,
        policy: PolicyBundle,
        state: LiveStateStore,
        broker: BrokerClient,
        writer_lock: AccountWriterLock | None = None,
        discovery: DiscoveryExecutor | None = None,
        clock: Callable[[], datetime] | None = None,
        latency: LatencyRecorder | None = None,
        allow_mutations: bool = False,
    ) -> AccountSnapshot:
        if not isinstance(allow_mutations, bool):
            raise ValueError("allow_mutations must be boolean")
        self.policy = policy
        self.state = state
        self.broker = broker
        self.writer_lock = writer_lock
        self.discovery = discovery
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.allow_mutations = allow_mutations
        self.account_key = str(policy.config["account"]["masked_identifier"])
        self.account_masked = f"••••{policy.account_last4}"
        self.safety = SafetyExecutionCoordinator(
            policy=policy,
            state=state,
            broker=broker,
            authority=self,
            clock=self._clock,
            latency=latency,
        )
        # A second action from the same account snapshot is unsafe: the first
        # mutation is not represented in that evidence envelope.
        self._mutation_snapshots: set[str] = set()

    def require_mutation_authority(
        self,
        *,
        snapshot: AccountSnapshot,
        operation: MutationOperation,
        phase: MutationPhase,
        now: object,
        plan_id: str,
        kind: object,
        request: OrderRequest | None = None,
        target: OrderSnapshot | None = None,
        plan: object | None = None,
        risk_decision: object | None = None,
    ) -> AccountSnapshot:
        """Revalidate live authority at the final broker transport boundary."""

        if not isinstance(now, datetime):
            raise MutationAuthorityDenied("MUTATION_AUTHORITY_TIME_INVALID")
        current = self._aware(now, "mutation authority time")
        try:
            normalized_operation = MutationOperation(operation)
            normalized_phase = MutationPhase(phase)
        except ValueError as exc:
            raise MutationAuthorityDenied(
                "MUTATION_AUTHORITY_OPERATION_INVALID"
            ) from exc
        allowed_phases = {
            MutationOperation.ENTRY_PLACE: {
                MutationPhase.BEFORE_PREPARE,
                MutationPhase.BEFORE_REVIEW,
                MutationPhase.BEFORE_PLACE,
            },
            MutationOperation.SAFETY_PLACE: {
                MutationPhase.BEFORE_PREPARE,
                MutationPhase.BEFORE_REVIEW,
                MutationPhase.BEFORE_PLACE,
            },
            MutationOperation.CANCEL: {
                MutationPhase.BEFORE_PREPARE,
                MutationPhase.BEFORE_CANCEL,
            },
        }
        failures: list[str] = []
        if normalized_phase not in allowed_phases[normalized_operation]:
            failures.append("MUTATION_AUTHORITY_PHASE_INVALID")
        # The snapshot passed into the coordinator is useful for planning and
        # broker review, but it is never sufficient authority for the actual
        # transport mutation.  Cash, restrictions, positions, orders, or
        # fills may have changed while review was in flight.  Refetch at the
        # final boundary and run every exact-tuple/risk/capacity check below
        # against that broker response.  Equality is accepted because a
        # connector can legitimately return the same canonical envelope when
        # its clock is frozen in deterministic tests; the envelope is still
        # consumed exactly once below.
        if (
            normalized_phase
            in {MutationPhase.BEFORE_PLACE, MutationPhase.BEFORE_CANCEL}
            and isinstance(snapshot, AccountSnapshot)
        ):
            supplied_snapshot = snapshot
            try:
                refreshed = self.broker.get_account_snapshot(self.account_masked)
            except Exception as exc:
                failures.append(
                    f"FINAL_BROKER_SNAPSHOT_REFRESH_FAILED:{type(exc).__name__}"
                )
            else:
                if not isinstance(refreshed, AccountSnapshot):
                    failures.append("FINAL_BROKER_SNAPSHOT_TYPE_INVALID")
                elif refreshed.account_masked != self.account_masked:
                    failures.append("FINAL_BROKER_SNAPSHOT_ACCOUNT_MISMATCH")
                else:
                    try:
                        supplied_observed = self._aware(
                            supplied_snapshot.observed_at,
                            "supplied mutation snapshot observed_at",
                        )
                        supplied_received = self._aware(
                            supplied_snapshot.received_at,
                            "supplied mutation snapshot received_at",
                        )
                        refreshed_observed = self._aware(
                            refreshed.observed_at,
                            "refreshed mutation snapshot observed_at",
                        )
                        refreshed_received = self._aware(
                            refreshed.received_at,
                            "refreshed mutation snapshot received_at",
                        )
                    except (TypeError, ValueError):
                        failures.append("FINAL_BROKER_SNAPSHOT_TIME_INVALID")
                    else:
                        if refreshed_observed < supplied_observed:
                            failures.append(
                                "FINAL_BROKER_SNAPSHOT_OBSERVED_REGRESSED"
                            )
                        if refreshed_received < supplied_received:
                            failures.append(
                                "FINAL_BROKER_SNAPSHOT_RECEIPT_REGRESSED"
                            )
                        if (
                            normalized_operation is MutationOperation.ENTRY_PLACE
                            and supplied_snapshot.risk_evidence_as_of is not None
                            and refreshed.risk_evidence_as_of is not None
                            and refreshed.risk_evidence_as_of
                            < supplied_snapshot.risk_evidence_as_of
                        ):
                            failures.append("ENTRY_RISK_EVIDENCE_REGRESSED")
                        # Even a regressed envelope is the latest broker response
                        # available at this boundary.  Inspect it for additional
                        # position/order/capacity blockers, while the regression
                        # above independently makes the mutation fail closed.
                        snapshot = refreshed
                try:
                    refreshed_clock = self._aware(
                        self._clock(), "post-refresh mutation authority time"
                    )
                except (TypeError, ValueError):
                    failures.append("MUTATION_AUTHORITY_CLOCK_INVALID")
                else:
                    if refreshed_clock < current:
                        failures.append("MUTATION_AUTHORITY_CLOCK_REGRESSED")
                    else:
                        current = refreshed_clock
        if not isinstance(snapshot, AccountSnapshot):
            failures.append("MUTATION_AUTHORITY_SNAPSHOT_MISSING")
        else:
            failures.extend(
                self._mutation_guard(
                    snapshot=snapshot,
                    now=current,
                    cancel=normalized_operation is MutationOperation.CANCEL,
                )
            )
        try:
            normalized_kind = IntentKind(kind)
        except ValueError:
            normalized_kind = None
            failures.append("MUTATION_AUTHORITY_INTENT_KIND_INVALID")
        normalized_plan = str(plan_id).strip()
        if not normalized_plan:
            failures.append("MUTATION_AUTHORITY_PLAN_ID_MISSING")
        if normalized_operation is MutationOperation.ENTRY_PLACE:
            runtime = self.state.runtime_status()
            if (
                runtime is None
                or EngineMode(str(runtime["mode"])) is not EngineMode.ACTIVE
            ):
                failures.append("RUNTIME_NOT_ACTIVE_FOR_ENTRY_MUTATION")
            if self.policy.calendar.lane(current) != "regular_entry":
                failures.append("ENTRY_MUTATION_OUTSIDE_REGULAR_ENTRY_LANE")
            if normalized_kind is not IntentKind.ENTRY:
                failures.append("ENTRY_MUTATION_KIND_MISMATCH")
            if not isinstance(request, OrderRequest):
                failures.append("ENTRY_MUTATION_REQUEST_MISSING")
            elif isinstance(snapshot, AccountSnapshot):
                if request.side is not BrokerSide.BUY or request.order_type is not EquityOrderType.LIMIT:
                    failures.append("ENTRY_MUTATION_TUPLE_INVALID")
                if any(
                    position.symbol == request.symbol and position.quantity > 0
                    for position in snapshot.equity_positions
                ):
                    failures.append("ADD_OR_REENTRY_POSITION_PRESENT")
                if any(
                    order.symbol == request.symbol and not order.state.terminal
                    for order in snapshot.equity_orders
                ):
                    failures.append("OVERLAPPING_ENTRY_OR_EXIT_ORDER_PRESENT")
                notional = Decimal(request.quantity) * (request.limit_price or Decimal("0"))
                if (
                    request.limit_price is None
                    or notional > snapshot.funds.cash
                    or notional > snapshot.funds.unleveraged_buying_power
                ):
                    failures.append("ENTRY_EXCEEDS_UNLEVERAGED_FUNDS")
                prior = self.state.rows(
                    "SELECT 1 FROM fills f JOIN broker_orders o ON o.broker_order_id=f.broker_order_id "
                    "JOIN order_intents i ON i.intent_id=o.intent_id "
                    "JOIN plans p ON p.plan_id=i.plan_id WHERE i.account_key=? "
                    "AND i.kind='ENTRY' AND p.symbol=? LIMIT 1",
                    (self.account_key, request.symbol),
                )
                if prior:
                    failures.append("REENTRY_FORBIDDEN_BY_DURABLE_HISTORY")
                failures.extend(
                    self._entry_risk_failures(
                        snapshot=snapshot,
                        plan=plan,
                        risk_decision=risk_decision,
                        request=request,
                        phase=normalized_phase,
                        now=current,
                    )
                )
        elif normalized_operation is MutationOperation.SAFETY_PLACE:
            if normalized_kind not in {IntentKind.PROTECTION, IntentKind.EXIT}:
                failures.append("SAFETY_MUTATION_KIND_MISMATCH")
            if not isinstance(request, OrderRequest):
                failures.append("SAFETY_MUTATION_REQUEST_MISSING")
            elif isinstance(snapshot, AccountSnapshot):
                if request.side is not BrokerSide.SELL:
                    failures.append("SAFETY_MUTATION_NOT_SELL")
                position = self._position(snapshot, request.symbol)
                if position is None or position.quantity <= 0:
                    failures.append("NO_BROKER_POSITION_TO_EXIT")
                else:
                    try:
                        owned = self._require_owned_exposure(
                            position=position,
                            symbol=request.symbol,
                        )
                        if owned.plan_id != normalized_plan:
                            failures.append("SAFETY_PLAN_OWNERSHIP_MISMATCH")
                        capacity = calculate_exit_capacity(
                            position=position,
                            orders=snapshot.equity_orders,
                            symbol=request.symbol,
                        )
                        require_exit_capacity(capacity, request.quantity)
                    except (ExitCapacityError, ValueError):
                        failures.append("INSUFFICIENT_OR_UNCERTAIN_EXIT_CAPACITY")
                if normalized_kind is IntentKind.PROTECTION:
                    plans = self.state.rows(
                        "SELECT structural_stop FROM plans WHERE plan_id=? AND account_key=?",
                        (normalized_plan, self.account_key),
                    )
                    if (
                        len(plans) != 1
                        or request.order_type is not EquityOrderType.STOP_MARKET
                        or request.market_hours is not MarketHours.REGULAR
                        or request.time_in_force is not TimeInForce.GTC
                        or request.stop_price != Decimal(str(plans[0]["structural_stop"]))
                    ):
                        failures.append("PROTECTION_TUPLE_DIFFERS_FROM_PLAN")
                elif normalized_kind is IntentKind.EXIT and (
                    request.order_type is not EquityOrderType.MARKET
                    or request.market_hours is not MarketHours.REGULAR
                    or request.time_in_force is not TimeInForce.GFD
                ):
                    failures.append("SAFE_CLOSE_TUPLE_INVALID")
        elif normalized_operation is MutationOperation.CANCEL:
            if normalized_kind is not IntentKind.CANCEL:
                failures.append("CANCEL_MUTATION_KIND_MISMATCH")
            if not isinstance(target, OrderSnapshot) or not isinstance(snapshot, AccountSnapshot):
                failures.append("CANCEL_TARGET_OR_SNAPSHOT_MISSING")
            else:
                matches = tuple(
                    order
                    for order in snapshot.equity_orders
                    if order.broker_order_id == target.broker_order_id
                )
                if len(matches) != 1:
                    failures.append("CANCEL_TARGET_NOT_CURRENT_AND_ACTIVE")
                else:
                    current_target = matches[0]
                    immutable_identity = (
                        current_target.account_masked == target.account_masked,
                        current_target.symbol == target.symbol,
                        current_target.side is target.side,
                        current_target.order_type is target.order_type,
                        current_target.requested_quantity == target.requested_quantity,
                        current_target.market_hours is target.market_hours,
                        current_target.time_in_force is target.time_in_force,
                        current_target.limit_price == target.limit_price,
                        current_target.stop_price == target.stop_price,
                        current_target.client_ref_id == target.client_ref_id,
                    )
                    monotone_evidence = (
                        current_target.broker_updated_at >= target.broker_updated_at
                        and current_target.received_at >= target.received_at
                        and current_target.cumulative_filled_quantity
                        >= target.cumulative_filled_quantity
                    )
                    if (
                        not all(immutable_identity)
                        or not monotone_evidence
                        or current_target.state.terminal
                    ):
                        failures.append("CANCEL_TARGET_NOT_CURRENT_AND_ACTIVE")
                try:
                    if self._plan_for_local_order(target.broker_order_id) != normalized_plan:
                        failures.append("CANCEL_PLAN_OWNERSHIP_MISMATCH")
                except ValueError:
                    failures.append("CANCEL_TARGET_NOT_LOCALLY_OWNED")
        if isinstance(snapshot, AccountSnapshot) and self._snapshot_key(snapshot) in self._mutation_snapshots:
            failures.append("SNAPSHOT_ALREADY_USED_FOR_MUTATION")
        if failures:
            raise MutationAuthorityDenied(*tuple(dict.fromkeys(failures)))
        if normalized_phase in {
            MutationPhase.BEFORE_PLACE,
            MutationPhase.BEFORE_CANCEL,
        }:
            # Consume broker evidence at the last pre-transport boundary, not
            # after a response.  A lost response or second caller can never
            # reuse the same account snapshot for another logical mutation.
            assert isinstance(snapshot, AccountSnapshot)
            self._mutation_snapshots.add(self._snapshot_key(snapshot))
        assert isinstance(snapshot, AccountSnapshot)
        return snapshot

    def _entry_risk_failures(
        self,
        *,
        snapshot: AccountSnapshot,
        plan: object,
        risk_decision: object,
        request: OrderRequest,
        phase: MutationPhase,
        now: datetime,
    ) -> tuple[str, ...]:
        """Independently recompute account-wide risk at every entry boundary.

        A caller-supplied ``RiskDecision`` is never authority.  The production
        capability rebuilds the exposure set from current broker evidence plus
        durable reservations and verifies byte-for-byte-equivalent decision
        facts.  The current plan is excluded only if its exact reservation is
        already durable, avoiding both double counting and a replay loophole.
        """

        failures: list[str] = []
        if phase is MutationPhase.BEFORE_PLACE:
            final_market_check = getattr(
                self.discovery, "final_entry_evidence_failures", None
            )
            if not callable(final_market_check):
                failures.append("FINAL_ENTRY_MARKET_RECHECK_UNAVAILABLE")
            else:
                try:
                    market_failures = final_market_check(
                        plan=plan, request=request, now=now
                    )
                    if not isinstance(market_failures, tuple) or any(
                        not isinstance(item, str) or not item
                        for item in market_failures
                    ):
                        failures.append("FINAL_ENTRY_MARKET_RECHECK_INVALID")
                    else:
                        failures.extend(market_failures)
                except Exception as exc:
                    failures.append(
                        f"FINAL_ENTRY_MARKET_RECHECK_FAILED:{type(exc).__name__}"
                    )
        if not snapshot.entry_risk_evidence_ready:
            failures.append("ENTRY_RISK_EVIDENCE_INCOMPLETE")
        if snapshot.risk_evidence_as_of is not None:
            risk_age = (now - snapshot.risk_evidence_as_of).total_seconds()
            max_age = int(
                self.policy.config["evidence"]["broker_snapshot_max_age_seconds"]
            )
            if risk_age < -1:
                failures.append("ENTRY_RISK_EVIDENCE_FROM_FUTURE")
            elif risk_age > max_age:
                failures.append("STALE_ENTRY_RISK_EVIDENCE")
        if not isinstance(plan, SignedPlan):
            return tuple(failures + ["ENTRY_SIGNED_PLAN_MISSING"])
        if not isinstance(risk_decision, RuntimeRiskDecision):
            return tuple(failures + ["ENTRY_RISK_DECISION_MISSING"])
        try:
            plan.validate(self.policy, now)
        except (TypeError, ValueError) as exc:
            failures.append(f"ENTRY_SIGNED_PLAN_INVALID:{type(exc).__name__}")
        zone_date = now.astimezone(NEW_YORK).date()
        latch_rows = self.state.rows(
            "SELECT * FROM session_latches WHERE account_key=? AND trading_date=?",
            (self.account_key, zone_date.isoformat()),
        )
        if len(latch_rows) != 1:
            return tuple(failures + ["CURRENT_SESSION_LATCH_MISSING_OR_AMBIGUOUS"])
        row = latch_rows[0]
        try:
            latch_updated = self._parse_time(
                str(row["updated_at"]), "session_latch.updated_at"
            )
            age = (now - latch_updated).total_seconds()
            if age < -1 or age > int(
                self.policy.config["evidence"]["broker_snapshot_max_age_seconds"]
            ):
                failures.append("CURRENT_SESSION_LATCH_STALE_OR_FUTURE")
            if bool(row["pause_new_entries"]) or bool(row["closeout_started"]):
                failures.append("CURRENT_SESSION_LATCH_BLOCKS_NEW_ENTRIES")
            crossed_at = (
                self._parse_time(
                    str(row["first_objective_crossed_at"]),
                    "session_latch.first_objective_crossed_at",
                )
                if row["first_objective_crossed_at"] is not None
                else None
            )
            latch = RuntimeSessionLatch(
                trading_date=zone_date,
                loss_lock=bool(row["loss_locked"]),
                hard_kill=bool(row["hard_kill"]),
                profit_goal_crossed=bool(row["objective_crossed"]),
                first_profit_crossed_at=crossed_at,
                highest_realized_pnl=Decimal(
                    int(row["highest_realized_pnl_cents"])
                )
                / Decimal("100"),
            )
        except (TypeError, ValueError) as exc:
            return tuple(
                failures
                + [f"CURRENT_SESSION_LATCH_INVALID:{type(exc).__name__}"]
            )

        try:
            # Local import keeps the authority protocol independent from the
            # discovery composition while reusing its conservative exposure
            # accounting implementation.
            from .pipeline import build_account_risk_snapshot

            existing_reservation = self.state.rows(
                "SELECT reservation_id FROM risk_reservations "
                "WHERE account_key=? AND plan_id=? AND state<>'RELEASED'",
                (self.account_key, plan.plan_id),
            )
            exclude = plan.plan_id if len(existing_reservation) == 1 else None
            rebuilt, rebuild_failures = build_account_risk_snapshot(
                policy=self.policy,
                state=self.state,
                broker_snapshot=snapshot,
                now=now,
                prices={plan.symbol: plan.entry_limit},
                exclude_plan_id=exclude,
            )
            failures.extend(rebuild_failures)
            if rebuilt is None:
                failures.append("ENTRY_RISK_SNAPSHOT_REBUILD_FAILED")
                return tuple(dict.fromkeys(failures))
            fresh = evaluate_entry(
                policy=self.policy,
                snapshot=rebuilt,
                plan=plan,
                latch=latch,
                now=now,
            )
            failures.extend(fresh.failures)
            if fresh != risk_decision:
                failures.append("RISK_DECISION_NOT_BOUND_TO_CURRENT_SNAPSHOT")
            if not fresh.allowed:
                failures.append("CURRENT_ACCOUNT_RISK_DENIES_ENTRY")
        except Exception as exc:
            failures.append(f"ENTRY_RISK_RECOMPUTE_FAILED:{type(exc).__name__}")
        return tuple(dict.fromkeys(failures))

    def reconcile(
        self, *, snapshot: AccountSnapshot, now: datetime
    ) -> LifecycleReconcileResult:
        """Converge durable safety state from the supplied broker snapshot.

        This method performs no broker transport call.  It is intended to run
        unconditionally after entry fills create obligations and before the
        service assesses protection.  Durable writes still require the sole
        writer lock/lease and matching release identity.
        """

        current = self._aware(now, "now")
        failures = self._state_write_guard(snapshot=snapshot, now=current)
        if failures:
            return LifecycleReconcileResult(blockers=failures)
        actions: list[str] = []
        blockers: list[str] = []
        try:
            by_ref = {
                order.client_ref_id: order
                for order in snapshot.equity_orders
                if order.client_ref_id is not None
            }
            by_id = {order.broker_order_id: order for order in snapshot.equity_orders}
            safety_rows = self.state.rows(
                "SELECT * FROM order_intents WHERE account_key=? "
                "AND kind IN ('PROTECTION','EXIT','CANCEL') ORDER BY created_at,intent_id",
                (self.account_key,),
            )
            for row in safety_rows:
                kind = IntentKind(row["kind"])
                if kind in {IntentKind.PROTECTION, IntentKind.EXIT}:
                    intent_state = IntentState(row["state"])
                    if intent_state.terminal:
                        continue
                    if intent_state is IntentState.PREPARED:
                        blockers.append(f"UNRESOLVED_{kind.value}_INTENT")
                        continue
                    order = by_ref.get(str(row["client_ref"]))
                    if order is not None:
                        outcome = self._reconcile_sell_intent(row=row, order=order)
                        actions.append(
                            f"RECONCILE_{kind.value}:{outcome}"
                        )
                        if outcome == "WAIT_STRICTLY_NEWER_EVIDENCE":
                            blockers.append(f"UNRESOLVED_{kind.value}_INTENT")
                    elif IntentState(row["state"]) in {
                        IntentState.SUBMITTING,
                        IntentState.UNKNOWN,
                        IntentState.ACKNOWLEDGED,
                    }:
                        blockers.append(
                            f"ACKNOWLEDGED_{kind.value}_ORDER_MISSING"
                            if IntentState(row["state"]) is IntentState.ACKNOWLEDGED
                            else f"UNRESOLVED_{kind.value}_INTENT"
                        )
                else:
                    cancel_tuple = json.loads(str(row["order_tuple_json"]))
                    target_id = str(cancel_tuple["target_broker_order_id"])
                    target = by_id.get(target_id)
                    state = IntentState(row["state"])
                    if target is not None and state in {
                        IntentState.SUBMITTING,
                        IntentState.UNKNOWN,
                        IntentState.ACKNOWLEDGED,
                    }:
                        floor = self._parse_time(
                            str(cancel_tuple["target_evidence_floor_at"]),
                            "cancel evidence floor",
                        )
                        if target.broker_updated_at > floor:
                            result = self.safety.reconcile_cancel(
                                intent_id=str(row["intent_id"]), order=target
                            )
                            actions.append(
                                f"RECONCILE_CANCEL:{result.status.value}"
                            )
                            state = IntentState(
                                self.state.row(
                                    "order_intents", "intent_id", row["intent_id"]
                                )["state"]
                            )
                    if state in {
                        IntentState.SUBMITTING,
                        IntentState.UNKNOWN,
                        IntentState.ACKNOWLEDGED,
                    }:
                        blockers.append("CANCEL_PENDING_RECONCILIATION")

            for symbol in self._obligation_symbols():
                actions.extend(
                    self._sync_obligations(
                        snapshot=snapshot,
                        symbol=symbol,
                        now=current,
                    )
                )
        except Exception as exc:
            blockers.append(f"LIFECYCLE_RECONCILIATION_FAILED:{type(exc).__name__}")
        return LifecycleReconcileResult(
            actions=tuple(actions), blockers=tuple(dict.fromkeys(blockers))
        )

    def protect(
        self,
        *,
        snapshot: AccountSnapshot,
        decision: ProtectionDecision,
        now: datetime,
    ) -> str:
        current = self._aware(now, "now")
        failures = self._mutation_guard(snapshot=snapshot, now=current, cancel=False)
        if failures:
            return self._blocked(failures)
        if self._snapshot_key(snapshot) in self._mutation_snapshots:
            return "BLOCKED:SNAPSHOT_ALREADY_USED_FOR_MUTATION"
        try:
            sync = self.reconcile(snapshot=snapshot, now=current)
            if sync.blockers:
                return self._blocked(sync.blockers)
            position = self._position(snapshot, decision.symbol)
            obligations = load_open_obligations(
                self.state, account_key=self.account_key, symbol=decision.symbol
            )
            expected = assess_protection(
                position=position,
                orders=snapshot.equity_orders,
                obligations=obligations,
                symbol=decision.symbol,
            )
            if expected != decision:
                return "BLOCKED:STALE_OR_MISMATCHED_PROTECTION_DECISION"
            owned = self._require_owned_exposure(position=position, symbol=decision.symbol)
            unresolved = self._unresolved_safety_failures(
                snapshot=snapshot, symbol=decision.symbol
            )
            if unresolved:
                return self._blocked(unresolved)

            if ProtectionAction.CREATE_PROTECTION_INTENT in decision.actions:
                candidates = tuple(
                    obligation
                    for obligation in obligations
                    if obligation.state is ProtectionState.REQUIRED
                )
                if not candidates:
                    return "BLOCKED:NO_REQUIRED_FILL_OBLIGATION"
                obligation = candidates[0]
                plan_id = self._plan_for_obligation(obligation.obligation_id)
                if plan_id != owned.plan_id:
                    return "BLOCKED:PROTECTION_PLAN_OWNERSHIP_MISMATCH"
                capacity = calculate_exit_capacity(
                    position=position,
                    orders=snapshot.equity_orders,
                    symbol=decision.symbol,
                )
                require_exit_capacity(capacity, obligation.required_quantity)
                request = OrderRequest(
                    account_masked=self.account_masked,
                    symbol=decision.symbol,
                    side=BrokerSide.SELL,
                    order_type=EquityOrderType.STOP_MARKET,
                    quantity=obligation.required_quantity,
                    market_hours=MarketHours.REGULAR,
                    time_in_force=TimeInForce.GTC,
                    client_ref_id=_PLACEHOLDER_CLIENT_REF,
                    stop_price=obligation.stop_price,
                )
                outcome = self.safety.submit_sell(
                    plan_id=plan_id,
                    kind=IntentKind.PROTECTION,
                    operation_key=obligation.obligation_id,
                    request=request,
                    broker_snapshot=snapshot,
                    now=current,
                )
                self._apply_protection_outcome(
                    obligation=obligation, outcome=outcome, now=current
                )
                self._reserve_snapshot_if_needed(snapshot, outcome)
                if outcome.status in {ExecutionStatus.REJECTED, ExecutionStatus.FAILED}:
                    close_decision = plan_safe_close(
                        position=position,
                        orders=snapshot.equity_orders,
                        symbol=decision.symbol,
                        snapshot_received_at=snapshot.observed_at,
                    )
                    if close_decision.action is ExitAction.SUBMIT_SAFE_CLOSE:
                        return self._advance_closeout(
                            snapshot=snapshot,
                            decision=close_decision,
                            now=current,
                            owned=owned,
                        )
                return self._format_outcome("PROTECTION", outcome)

            if ProtectionAction.SAFE_CLOSE in decision.actions:
                close_decision = plan_safe_close(
                    position=position,
                    orders=snapshot.equity_orders,
                    symbol=decision.symbol,
                    snapshot_received_at=snapshot.observed_at,
                )
                return self._advance_closeout(
                    snapshot=snapshot,
                    decision=close_decision,
                    now=current,
                    owned=owned,
                )
            return "NO_MUTATION:PROTECTION_RECONCILIATION_REQUIRED"
        except ExitCapacityError:
            return "BLOCKED:INSUFFICIENT_OR_UNCERTAIN_EXIT_CAPACITY"
        except Exception as exc:
            return f"BLOCKED:LIFECYCLE_PROTECTION_FAILED:{type(exc).__name__}"

    def closeout(
        self,
        *,
        snapshot: AccountSnapshot,
        decision: SafeCloseDecision,
        now: datetime,
    ) -> str:
        current = self._aware(now, "now")
        failures = self._mutation_guard(snapshot=snapshot, now=current, cancel=False)
        if failures:
            return self._blocked(failures)
        if self._snapshot_key(snapshot) in self._mutation_snapshots:
            return "BLOCKED:SNAPSHOT_ALREADY_USED_FOR_MUTATION"
        try:
            sync = self.reconcile(snapshot=snapshot, now=current)
            if sync.blockers:
                return self._blocked(sync.blockers)
            position = self._position(snapshot, decision.symbol)
            expected = plan_safe_close(
                position=position,
                orders=snapshot.equity_orders,
                symbol=decision.symbol,
                snapshot_received_at=snapshot.observed_at,
            )
            if expected != decision:
                return "BLOCKED:STALE_OR_MISMATCHED_CLOSEOUT_DECISION"
            owned = self._require_owned_exposure(
                position=position, symbol=decision.symbol, allow_flat=True
            )
            return self._advance_closeout(
                snapshot=snapshot, decision=decision, now=current, owned=owned
            )
        except ExitCapacityError:
            return "BLOCKED:INSUFFICIENT_OR_UNCERTAIN_EXIT_CAPACITY"
        except Exception as exc:
            return f"BLOCKED:LIFECYCLE_CLOSEOUT_FAILED:{type(exc).__name__}"

    def discover_and_execute(
        self, *, snapshot: AccountSnapshot, now: datetime
    ) -> tuple[str, ...]:
        current = self._aware(now, "now")
        failures = list(
            self._mutation_guard(snapshot=snapshot, now=current, cancel=False)
        )
        runtime = self.state.runtime_status()
        if runtime is None or EngineMode(str(runtime["mode"])) is not EngineMode.ACTIVE:
            failures.append("RUNTIME_NOT_ACTIVE_FOR_DISCOVERY")
        if self.policy.calendar.lane(current) != "regular_entry":
            failures.append("DISCOVERY_OUTSIDE_REGULAR_ENTRY_LANE")
        if failures:
            return (self._blocked(tuple(dict.fromkeys(failures))),)
        if self.discovery is None:
            return ("DISCOVERY_EXECUTION_PATH_NOT_CONFIGURED",)
        return self.discovery.execute(snapshot=snapshot, now=current)

    def _advance_closeout(
        self,
        *,
        snapshot: AccountSnapshot,
        decision: SafeCloseDecision,
        now: datetime,
        owned: _OwnedExposure,
    ) -> str:
        if decision.action in {
            ExitAction.FLAT,
            ExitAction.RECONCILE,
            ExitAction.BLOCKED,
            ExitAction.WAIT_CANCEL_CONFIRMATION,
            ExitAction.WAIT_EXISTING_EXIT,
        }:
            return f"NO_MUTATION:{decision.action.value}"

        if decision.action in {
            ExitAction.CANCEL_ENTRY_ORDERS,
            ExitAction.CANCEL_EXIT_ORDERS,
        }:
            pending = self._pending_cancel_failure(decision.symbol)
            if pending:
                return self._blocked(pending)
            target_id = decision.cancel_order_ids[0] if decision.cancel_order_ids else None
            target = next(
                (
                    order
                    for order in snapshot.equity_orders
                    if order.broker_order_id == target_id
                ),
                None,
            )
            if target is None:
                return "BLOCKED:CANCEL_TARGET_NOT_IN_SNAPSHOT"
            plan_id = self._plan_for_local_order(target.broker_order_id)
            # Cancellation uses a different capability subset, but still
            # shares the same activation/lease/evidence guard.
            cancel_failures = self._mutation_guard(
                snapshot=snapshot, now=now, cancel=True
            )
            if cancel_failures:
                return self._blocked(cancel_failures)
            outcome = self.safety.cancel_order(
                plan_id=plan_id,
                target=target,
                broker_snapshot=snapshot,
                now=now,
            )
            self._reserve_snapshot_if_needed(snapshot, outcome)
            return self._format_outcome("CANCEL", outcome)

        if decision.action is not ExitAction.SUBMIT_SAFE_CLOSE:
            return f"BLOCKED:UNSUPPORTED_CLOSEOUT_ACTION:{decision.action.value}"
        if owned.quantity <= 0 or decision.quantity != owned.quantity:
            return "BLOCKED:CLOSE_QUANTITY_DIFFERS_FROM_LOCALLY_OWNED_POSITION"
        unresolved = self._unresolved_safety_failures(
            snapshot=snapshot, symbol=decision.symbol
        )
        if unresolved:
            return self._blocked(unresolved)
        floor = self._latest_terminal_action_floor(decision.symbol)
        if floor is not None and snapshot.observed_at <= floor:
            return "BLOCKED:STRICTLY_NEWER_POST_MUTATION_SNAPSHOT_REQUIRED"
        capacity = calculate_exit_capacity(
            position=self._position(snapshot, decision.symbol),
            orders=snapshot.equity_orders,
            symbol=decision.symbol,
        )
        require_exit_capacity(capacity, decision.quantity)
        ordinal = self._next_exit_ordinal(decision.symbol)
        request = OrderRequest(
            account_masked=self.account_masked,
            symbol=decision.symbol,
            side=BrokerSide.SELL,
            order_type=EquityOrderType.MARKET,
            quantity=decision.quantity,
            market_hours=MarketHours.REGULAR,
            time_in_force=TimeInForce.GFD,
            client_ref_id=_PLACEHOLDER_CLIENT_REF,
        )
        outcome = self.safety.submit_sell(
            plan_id=owned.plan_id,
            kind=IntentKind.EXIT,
            operation_key=f"safe-close:{owned.plan_id}:{ordinal}",
            request=request,
            broker_snapshot=snapshot,
            now=now,
        )
        self._reserve_snapshot_if_needed(snapshot, outcome)
        return self._format_outcome("EXIT", outcome)

    def _reconcile_sell_intent(
        self, *, row: Mapping[str, object], order: OrderSnapshot
    ) -> str:
        before = IntentState(row["state"])
        updated_at = self._parse_time(str(row["updated_at"]), "intent updated_at")
        if (
            before in {IntentState.SUBMITTING, IntentState.UNKNOWN}
            and order.broker_updated_at <= updated_at
        ):
            return "WAIT_STRICTLY_NEWER_EVIDENCE"
        durable_orders = self.state.rows(
            "SELECT * FROM broker_orders WHERE intent_id=?",
            (row["intent_id"],),
        )
        if durable_orders:
            durable = durable_orders[0]
            if durable["broker_order_id"] != order.broker_order_id:
                raise ValueError("one safety intent resolved to multiple broker orders")
            durable_received = self._parse_time(
                str(durable["received_at"]), "durable order received_at"
            )
            durable_updated = self._parse_time(
                str(durable["broker_updated_at"]), "durable order broker_updated_at"
            )
            if (
                order.received_at < durable_received
                or order.broker_updated_at < durable_updated
                or whole_shares(
                    order.cumulative_filled_quantity, allow_zero=True
                )
                < int(durable["cumulative_filled_quantity"])
            ):
                return "WAIT_STRICTLY_NEWER_EVIDENCE"
        self.safety.persist_order_evidence(intent_id=str(row["intent_id"]), order=order)
        current_row = self.state.row("order_intents", "intent_id", row["intent_id"])
        current = IntentState(current_row["state"])
        if order.state.terminal and current is IntentState.ACKNOWLEDGED:
            self.state.transition_intent(
                str(row["intent_id"]),
                IntentState.RECONCILED,
                occurred_at=order.received_at,
                detail={
                    "terminal_broker_state": order.state.value,
                    "broker_order_id": order.broker_order_id,
                },
            )
            self._resolve_unknown_incidents(str(row["intent_id"]), order.received_at)
            return "TERMINAL"
        if before is IntentState.UNKNOWN and current is IntentState.ACKNOWLEDGED:
            self._resolve_unknown_incidents(str(row["intent_id"]), order.received_at)
        return order.state.value

    def _sync_obligations(
        self, *, snapshot: AccountSnapshot, symbol: str, now: datetime
    ) -> tuple[str, ...]:
        position = self._position(snapshot, symbol)
        position_quantity = (
            0
            if position is None
            else whole_shares(position.quantity, allow_zero=True)
        )
        rows = self.state.rows(
            "SELECT * FROM order_intents WHERE account_key=? AND kind='PROTECTION'",
            (self.account_key,),
        )
        intents: dict[str, Mapping[str, object]] = {}
        for row in rows:
            payload = json.loads(str(row["order_tuple_json"]))
            key = str(payload.get("operation_key", ""))
            if key:
                if key in intents:
                    raise ValueError("multiple protection intents share an operation key")
                intents[key] = row
        orders_by_ref = {
            order.client_ref_id: order
            for order in snapshot.equity_orders
            if order.client_ref_id is not None
        }
        actions: list[str] = []
        for obligation in load_open_obligations(
            self.state, account_key=self.account_key, symbol=symbol
        ):
            intent = intents.get(obligation.obligation_id)
            if intent is None:
                continue
            order = orders_by_ref.get(str(intent["client_ref"]))
            if order is not None:
                durable = self.state.rows(
                    "SELECT broker_order_id FROM broker_orders "
                    "WHERE intent_id=? AND broker_order_id=?",
                    (intent["intent_id"], order.broker_order_id),
                )
                # A matching client reference is not enough to bind an
                # UNKNOWN mutation.  It must first pass strict-newer evidence
                # reconciliation and exist in the durable broker-order table.
                if not durable:
                    order = None
            target_state = obligation.state
            working = obligation.working_quantity
            broker_order_id = obligation.broker_order_id
            intent_state = IntentState(intent["state"])
            if order is None:
                if intent_state in {IntentState.SUBMITTING, IntentState.UNKNOWN}:
                    target_state = ProtectionState.PENDING_SUBMIT
                    working = 0
                elif intent_state in {IntentState.REJECTED, IntentState.FAILED}:
                    target_state = ProtectionState.FAILED
                    working = 0
            else:
                broker_order_id = order.broker_order_id
                remaining = whole_shares(
                    order.requested_quantity - order.cumulative_filled_quantity,
                    field="protection remaining quantity",
                    allow_zero=True,
                )
                if order.state is BrokerOrderState.FILLED:
                    target_state = ProtectionState.SATISFIED
                    working = 0
                elif order.state is BrokerOrderState.PENDING_CANCELLED:
                    target_state = ProtectionState.CANCEL_REQUESTED
                    working = 0
                elif order.state in _FAILED_ORDER_STATES:
                    target_state = ProtectionState.FAILED
                    working = 0
                elif is_verified_working_protection(order):
                    if order.stop_price != obligation.stop_price:
                        target_state = ProtectionState.FAILED
                        working = 0
                    else:
                        target_state = ProtectionState.WORKING
                        working = min(remaining, obligation.required_quantity)
                elif order.state in _PENDING_ORDER_STATES:
                    target_state = ProtectionState.SUBMITTED
                    working = 0
            if position_quantity == 0 and order is not None and order.state.terminal:
                target_state = ProtectionState.SATISFIED
                working = 0
            changed = (
                target_state is not obligation.state
                or working != obligation.working_quantity
                or broker_order_id != obligation.broker_order_id
            )
            if changed:
                updated = replace(
                    obligation,
                    state=target_state,
                    working_quantity=working,
                    broker_order_id=broker_order_id,
                    revision=obligation.revision + 1,
                    updated_at=max(now, obligation.updated_at),
                )
                self.state.record_protection_obligation(updated)
                actions.append(
                    f"OBLIGATION:{obligation.obligation_id}:{target_state.value}"
                )
        return tuple(actions)

    def _apply_protection_outcome(
        self,
        *,
        obligation: ProtectionObligation,
        outcome: SafetyExecutionOutcome,
        now: datetime,
    ) -> None:
        state = obligation.state
        working = obligation.working_quantity
        broker_order_id = obligation.broker_order_id
        if outcome.status is ExecutionStatus.UNKNOWN:
            state = ProtectionState.PENDING_SUBMIT
            working = 0
        elif outcome.status in {ExecutionStatus.REJECTED, ExecutionStatus.FAILED}:
            state = ProtectionState.FAILED
            working = 0
        elif outcome.status is ExecutionStatus.ACKNOWLEDGED:
            state = ProtectionState.SUBMITTED
            broker_order_id = outcome.broker_order_id
            if broker_order_id is not None:
                durable = self.state.row(
                    "broker_orders", "broker_order_id", broker_order_id
                )
                if durable is not None:
                    broker_state = BrokerOrderState(durable["state"])
                    remaining = int(durable["quantity"]) - int(
                        durable["cumulative_filled_quantity"]
                    )
                    if broker_state is BrokerOrderState.FILLED:
                        state = ProtectionState.SATISFIED
                        working = 0
                    elif broker_state in _FAILED_ORDER_STATES:
                        state = ProtectionState.FAILED
                        working = 0
                    elif broker_state in {
                        BrokerOrderState.CONFIRMED,
                        BrokerOrderState.PARTIALLY_FILLED,
                    } and remaining > 0:
                        state = ProtectionState.WORKING
                        working = min(remaining, obligation.required_quantity)
        if (
            state is obligation.state
            and working == obligation.working_quantity
            and broker_order_id == obligation.broker_order_id
        ):
            return
        self.state.record_protection_obligation(
            replace(
                obligation,
                state=state,
                working_quantity=working,
                broker_order_id=broker_order_id,
                revision=obligation.revision + 1,
                updated_at=max(now, obligation.updated_at),
            )
        )

    def _state_write_guard(
        self, *, snapshot: AccountSnapshot, now: datetime
    ) -> tuple[str, ...]:
        failures = list(self._runtime_identity_failures(require_authority=False))
        failures.extend(self._writer_failures())
        failures.extend(self._snapshot_failures(snapshot=snapshot, now=now))
        try:
            capabilities = self.broker.capabilities
            if capabilities.account_masked != self.account_masked:
                failures.append("BROKER_ACCOUNT_MISMATCH")
            if not capabilities.can_prove_whole_broker_reconciliation:
                failures.append("CONNECTOR_CANNOT_RECONCILE_WHOLE_BROKER")
        except Exception as exc:
            failures.append(f"BROKER_CAPABILITY_PROBE_FAILED:{type(exc).__name__}")
        return tuple(dict.fromkeys(failures))

    def _mutation_guard(
        self, *, snapshot: AccountSnapshot, now: datetime, cancel: bool
    ) -> tuple[str, ...]:
        failures = list(self._runtime_identity_failures(require_authority=True))
        if not self.allow_mutations:
            failures.append("LOCAL_MUTATION_INTERLOCK_DISABLED")
        failures.extend(self._writer_failures())
        failures.extend(self._snapshot_failures(snapshot=snapshot, now=now))
        failures.extend(self._session_failures(now))
        execution = self.policy.config["execution"]
        if execution.get("supported_unattended_mutation") is not True:
            failures.append("SUPPORTED_UNATTENDED_MUTATION_NOT_ATTESTED")
        if execution.get("per_mutation_user_confirmation_required") is not False:
            failures.append("PER_MUTATION_CONFIRMATION_STILL_REQUIRED")
        try:
            capabilities = self.broker.capabilities
            capability_checks = (
                (capabilities.account_masked == self.account_masked, "BROKER_ACCOUNT_MISMATCH"),
                (capabilities.daemon_transport_configured, "DAEMON_BROKER_TRANSPORT_UNAVAILABLE"),
                (capabilities.supports_daemon_writes, "DAEMON_BROKER_WRITES_UNSUPPORTED"),
                (capabilities.supports_unattended_writes, "UNATTENDED_BROKER_WRITES_UNSUPPORTED"),
                (capabilities.supports_equity_order_read, "EQUITY_ORDER_READ_UNSUPPORTED"),
                (
                    capabilities.can_prove_whole_broker_reconciliation,
                    "CONNECTOR_CANNOT_RECONCILE_WHOLE_BROKER",
                ),
                (capabilities.supports_equity_cancel, "EQUITY_CANCEL_UNSUPPORTED"),
                (
                    not capabilities.cancel_requires_explicit_confirmation,
                    "CANCEL_CONFIRMATION_REQUIRED",
                ),
            ) if cancel else (
                (capabilities.account_masked == self.account_masked, "BROKER_ACCOUNT_MISMATCH"),
                (capabilities.daemon_transport_configured, "DAEMON_BROKER_TRANSPORT_UNAVAILABLE"),
                (capabilities.supports_daemon_writes, "DAEMON_BROKER_WRITES_UNSUPPORTED"),
                (capabilities.supports_unattended_writes, "UNATTENDED_BROKER_WRITES_UNSUPPORTED"),
                (capabilities.supports_equity_order_read, "EQUITY_ORDER_READ_UNSUPPORTED"),
                (
                    capabilities.can_prove_whole_broker_reconciliation,
                    "CONNECTOR_CANNOT_RECONCILE_WHOLE_BROKER",
                ),
                (capabilities.supports_equity_review, "EQUITY_REVIEW_UNSUPPORTED"),
                (capabilities.supports_equity_place, "EQUITY_PLACE_UNSUPPORTED"),
                (capabilities.supports_ref_id_lookup, "CLIENT_REF_LOOKUP_UNSUPPORTED"),
                (
                    not capabilities.review_requires_explicit_confirmation,
                    "REVIEW_CONFIRMATION_REQUIRED",
                ),
            )
            failures.extend(code for passed, code in capability_checks if not passed)
            if (
                self.policy.config["evidence"].get(
                    "require_advanced_order_reconciliation"
                )
                is True
                and not capabilities.order_coverage.family_complete(
                    OrderFamily.ADVANCED_EQUITY
                )
            ):
                failures.append("ADVANCED_ORDER_READ_UNSUPPORTED")
        except Exception as exc:
            failures.append(f"BROKER_CAPABILITY_PROBE_FAILED:{type(exc).__name__}")
        return tuple(dict.fromkeys(failures))

    def _runtime_identity_failures(self, *, require_authority: bool) -> tuple[str, ...]:
        row = self.state.runtime_status()
        if row is None:
            return ("RUNTIME_NOT_INITIALIZED",)
        checks = (
            (row["runtime_id"] == self.policy.runtime_id, "RUNTIME_ID_MISMATCH"),
            (row["account_key"] == self.account_key, "RUNTIME_ACCOUNT_MISMATCH"),
            (row["config_hash"] == self.policy.config_hash, "RUNTIME_CONFIG_HASH_MISMATCH"),
            (row["policy_hash"] == self.policy.policy_hash, "RUNTIME_POLICY_HASH_MISMATCH"),
        )
        failures = [code for passed, code in checks if not passed]
        if require_authority:
            if not bool(row["authority_enabled"]):
                failures.append("RUNTIME_AUTHORITY_DISABLED")
            try:
                if EngineMode(str(row["mode"])) not in _MUTATION_MODES:
                    failures.append("RUNTIME_MODE_BLOCKS_SAFETY_MUTATION")
            except ValueError:
                failures.append("RUNTIME_MODE_UNKNOWN")
        return tuple(failures)

    def _writer_failures(self) -> tuple[str, ...]:
        if self.writer_lock is None or not self.writer_lock.held:
            return ("ACCOUNT_WRITER_KERNEL_LOCK_NOT_HELD",)
        execution = self.policy.config["execution"]
        production = (
            execution.get("broker_adapter") == "supported_production_transport"
        )
        account_binding = (
            str(execution.get("production_account_binding_fingerprint", ""))
            if production
            else None
        )
        authorization_binding = (
            str(execution.get("production_authorization_binding_id", ""))
            if production
            else None
        )
        try:
            expected_fingerprint = account_writer_fingerprint(
                self.account_key,
                broker_account_binding_fingerprint=account_binding,
                authorization_binding_id=authorization_binding,
            )
        except ValueError:
            return ("ACCOUNT_WRITER_PRODUCTION_BINDING_INVALID",)
        failures: list[str] = []
        if self.writer_lock.account_fingerprint != expected_fingerprint:
            failures.append("ACCOUNT_WRITER_LOCK_ACCOUNT_MISMATCH")
        if production and (
            self.writer_lock.broker_account_binding_fingerprint != account_binding
            or self.writer_lock.authorization_binding_id != authorization_binding
        ):
            failures.append("ACCOUNT_WRITER_LOCK_BROKER_BINDING_MISMATCH")
        rows = self.state.rows(
            "SELECT * FROM account_writer_lease WHERE account_key=?",
            (self.account_key,),
        )
        if len(rows) != 1:
            failures.append("ACCOUNT_WRITER_DATABASE_LEASE_MISSING")
        else:
            lease = rows[0]
            if lease["released_at"] is not None:
                failures.append("ACCOUNT_WRITER_DATABASE_LEASE_RELEASED")
            if lease["owner_id"] != self.writer_lock.owner_id:
                failures.append("ACCOUNT_WRITER_DATABASE_LEASE_OWNER_MISMATCH")
            if int(lease["process_id"]) != os.getpid():
                failures.append("ACCOUNT_WRITER_DATABASE_LEASE_PROCESS_MISMATCH")
        return tuple(failures)

    def _snapshot_failures(
        self, *, snapshot: AccountSnapshot, now: datetime
    ) -> tuple[str, ...]:
        if not isinstance(snapshot, AccountSnapshot):
            return ("ACCOUNT_SNAPSHOT_NOT_NORMALIZED",)
        failures: list[str] = []
        try:
            self.policy.require_account(snapshot.account_masked, snapshot.account_type)
        except (TypeError, ValueError):
            failures.append("SNAPSHOT_ACCOUNT_OR_TYPE_MISMATCH")
        if snapshot.account_state.strip().lower() not in {"active", "open"}:
            failures.append("ACCOUNT_NOT_ACTIVE")
        if not snapshot.auth_point_in_time:
            failures.append("AUTH_NOT_CURRENT")
        # Receipt time only says when collection completed.  Authority is as
        # old as the earliest provider observation in the exhaustive envelope.
        age = (now - snapshot.observed_at).total_seconds()
        max_age = int(
            self.policy.config["evidence"]["broker_snapshot_max_age_seconds"]
        )
        if age < -1:
            failures.append("SNAPSHOT_FROM_FUTURE")
        elif age > max_age:
            failures.append("STALE_SNAPSHOT")
        completeness = (
            (snapshot.standard_equity_positions_complete, "STANDARD_POSITIONS_INCOMPLETE"),
            (snapshot.standard_equity_orders_complete, "STANDARD_ORDERS_INCOMPLETE"),
            (snapshot.option_positions_complete, "OPTION_POSITIONS_INCOMPLETE"),
            (snapshot.option_orders_complete, "OPTION_ORDERS_INCOMPLETE"),
            (snapshot.advanced_orders_complete, "ADVANCED_RECONCILIATION_INCOMPLETE"),
        )
        failures.extend(code for complete, code in completeness if not complete)
        if snapshot.option_position_count:
            failures.append("OPTION_EXPOSURE_PRESENT")
        if snapshot.option_order_count:
            failures.append("OPTION_ORDERS_PRESENT")
        if snapshot.advanced_order_count:
            failures.append("ADVANCED_ORDERS_PRESENT")
        if len({position.symbol for position in snapshot.equity_positions}) != len(
            snapshot.equity_positions
        ):
            failures.append("DUPLICATE_POSITION_SYMBOL")
        if len({order.broker_order_id for order in snapshot.equity_orders}) != len(
            snapshot.equity_orders
        ):
            failures.append("DUPLICATE_BROKER_ORDER_ID")
        client_refs = tuple(
            order.client_ref_id
            for order in snapshot.equity_orders
            if order.client_ref_id is not None
        )
        if len(set(client_refs)) != len(client_refs):
            failures.append("DUPLICATE_CLIENT_REFERENCE")
        if any(
            position.asset_class.lower() != "equity" or position.is_fractional
            for position in snapshot.equity_positions
        ):
            failures.append("UNSUPPORTED_OR_FRACTIONAL_POSITION")
        if any(
            order.requested_quantity != order.requested_quantity.to_integral_value()
            or order.cumulative_filled_quantity
            != order.cumulative_filled_quantity.to_integral_value()
            for order in snapshot.equity_orders
        ):
            failures.append("FRACTIONAL_ORDER_PRESENT")
        return tuple(dict.fromkeys(failures))

    def _session_failures(self, now: datetime) -> tuple[str, ...]:
        lane = self.policy.calendar.lane(now)
        if lane in {"regular_entry", "manage_only", "closeout", "flat_deadline"}:
            return ()
        if lane == "transition" and now.astimezone(NEW_YORK).timetz().replace(
            tzinfo=None
        ) >= time(9, 30):
            return ()
        return (f"UNATTENDED_SAFETY_MUTATION_CLOSED_IN_{lane.upper()}",)

    def _require_owned_exposure(
        self,
        *,
        position: PositionSnapshot | None,
        symbol: str,
        allow_flat: bool = False,
    ) -> _OwnedExposure:
        rows = self.state.rows(
            "SELECT f.fill_id,f.quantity,i.plan_id,i.order_tuple_json "
            "FROM fills f JOIN broker_orders o ON o.broker_order_id=f.broker_order_id "
            "JOIN order_intents i ON i.intent_id=o.intent_id "
            "WHERE f.account_key=? ORDER BY f.received_at,f.fill_id",
            (self.account_key,),
        )
        quantity = 0
        plan_ids: set[str] = set()
        facts: list[str] = []
        for row in rows:
            payload = json.loads(str(row["order_tuple_json"]))
            if str(payload.get("symbol", "")).upper() != symbol.upper():
                continue
            side = str(payload.get("side", "")).lower()
            if side not in {"buy", "sell"}:
                raise ValueError("durable fill has no normalized side")
            amount = int(row["quantity"])
            quantity += amount if side == "buy" else -amount
            if side == "buy":
                plan_ids.add(str(row["plan_id"]))
            facts.append(f"{row['fill_id']}:{side}:{amount}")
        if quantity < 0:
            raise ValueError("durable fills imply a short position")
        actual = (
            0
            if position is None
            else whole_shares(position.quantity, allow_zero=True)
        )
        if actual != quantity:
            raise ValueError("broker position differs from durable local fills")
        if actual == 0:
            if not allow_flat:
                raise ValueError("no locally owned exposure remains")
            plan_id = next(iter(plan_ids), "")
            return _OwnedExposure(
                plan_id=plan_id,
                quantity=0,
                fingerprint=self._fingerprint(facts),
            )
        if len(plan_ids) != 1:
            raise ValueError("position ownership does not resolve to one entry plan")
        return _OwnedExposure(
            plan_id=next(iter(plan_ids)),
            quantity=quantity,
            fingerprint=self._fingerprint(facts),
        )

    def _unresolved_safety_failures(
        self, *, snapshot: AccountSnapshot, symbol: str
    ) -> tuple[str, ...]:
        broker_by_ref = {
            order.client_ref_id: order
            for order in snapshot.equity_orders
            if order.client_ref_id is not None
        }
        failures: list[str] = []
        rows = self.state.rows(
            "SELECT * FROM order_intents WHERE account_key=? "
            "AND kind IN ('PROTECTION','EXIT','CANCEL') ORDER BY created_at",
            (self.account_key,),
        )
        for row in rows:
            payload = json.loads(str(row["order_tuple_json"]))
            row_symbol = str(payload.get("symbol", "")).upper()
            if IntentKind(row["kind"]) is IntentKind.CANCEL:
                target_id = str(payload.get("target_broker_order_id", ""))
                target_rows = self.state.rows(
                    "SELECT i.order_tuple_json FROM broker_orders o "
                    "JOIN order_intents i ON i.intent_id=o.intent_id "
                    "WHERE o.broker_order_id=?",
                    (target_id,),
                )
                if target_rows:
                    target_tuple = json.loads(str(target_rows[0]["order_tuple_json"]))
                    row_symbol = str(target_tuple.get("symbol", "")).upper()
                if row_symbol == symbol.upper() and IntentState(row["state"]) in {
                    IntentState.PREPARED,
                    IntentState.SUBMITTING,
                    IntentState.UNKNOWN,
                    IntentState.ACKNOWLEDGED,
                }:
                    failures.append("CANCEL_PENDING_RECONCILIATION")
                continue
            if row_symbol != symbol.upper():
                continue
            state = IntentState(row["state"])
            order = broker_by_ref.get(str(row["client_ref"]))
            if state in {IntentState.PREPARED, IntentState.SUBMITTING, IntentState.UNKNOWN}:
                failures.append(f"UNRESOLVED_{row['kind']}_INTENT")
            elif state is IntentState.ACKNOWLEDGED and order is None:
                failures.append(f"ACKNOWLEDGED_{row['kind']}_ORDER_MISSING")
            elif state is IntentState.ACKNOWLEDGED and order is not None and order.state is BrokerOrderState.UNKNOWN:
                failures.append(f"UNKNOWN_{row['kind']}_BROKER_ORDER")
            elif (
                state in {IntentState.REJECTED, IntentState.FAILED}
                and IntentKind(row["kind"]) is IntentKind.EXIT
            ):
                durable_orders = self.state.rows(
                    "SELECT state FROM broker_orders WHERE intent_id=?",
                    (row["intent_id"],),
                )
                if not durable_orders:
                    failures.append(f"FAILED_{row['kind']}_REQUIRES_OPERATOR_REVIEW")
        return tuple(dict.fromkeys(failures))

    def _pending_cancel_failure(self, symbol: str) -> tuple[str, ...]:
        failures = self._unresolved_cancel_rows(symbol)
        return ("CANCEL_PENDING_RECONCILIATION",) if failures else ()

    def _unresolved_cancel_rows(self, symbol: str) -> tuple[Mapping[str, object], ...]:
        matches: list[Mapping[str, object]] = []
        for row in self.state.rows(
            "SELECT * FROM order_intents WHERE account_key=? AND kind='CANCEL'",
            (self.account_key,),
        ):
            if IntentState(row["state"]) not in {
                IntentState.PREPARED,
                IntentState.SUBMITTING,
                IntentState.UNKNOWN,
                IntentState.ACKNOWLEDGED,
            }:
                continue
            payload = json.loads(str(row["order_tuple_json"]))
            target = self.state.rows(
                "SELECT i.order_tuple_json FROM broker_orders o "
                "JOIN order_intents i ON i.intent_id=o.intent_id "
                "WHERE o.broker_order_id=?",
                (payload.get("target_broker_order_id"),),
            )
            if target:
                target_tuple = json.loads(str(target[0]["order_tuple_json"]))
                if str(target_tuple.get("symbol", "")).upper() == symbol.upper():
                    matches.append(row)
        return tuple(matches)

    def _latest_terminal_action_floor(self, symbol: str) -> datetime | None:
        floors: list[datetime] = []
        rows = self.state.rows(
            "SELECT * FROM order_intents WHERE account_key=? "
            "AND kind IN ('EXIT','CANCEL')",
            (self.account_key,),
        )
        for row in rows:
            payload = json.loads(str(row["order_tuple_json"]))
            row_symbol = str(payload.get("symbol", "")).upper()
            if IntentKind(row["kind"]) is IntentKind.CANCEL:
                target = self.state.rows(
                    "SELECT i.order_tuple_json FROM broker_orders o "
                    "JOIN order_intents i ON i.intent_id=o.intent_id "
                    "WHERE o.broker_order_id=?",
                    (payload.get("target_broker_order_id"),),
                )
                if target:
                    target_tuple = json.loads(str(target[0]["order_tuple_json"]))
                    row_symbol = str(target_tuple.get("symbol", "")).upper()
            if row_symbol != symbol.upper() or IntentState(row["state"]) is not IntentState.RECONCILED:
                continue
            floors.append(self._parse_time(str(row["updated_at"]), "intent updated_at"))
        return max(floors, default=None)

    def _next_exit_ordinal(self, symbol: str) -> int:
        rows = self.state.rows(
            "SELECT order_tuple_json FROM order_intents WHERE account_key=? AND kind='EXIT'",
            (self.account_key,),
        )
        count = 0
        for row in rows:
            payload = json.loads(str(row["order_tuple_json"]))
            if str(payload.get("symbol", "")).upper() == symbol.upper():
                count += 1
        return count + 1

    def _plan_for_obligation(self, obligation_id: str) -> str:
        rows = self.state.rows(
            "SELECT i.plan_id FROM protection_obligations po "
            "JOIN fills f ON f.fill_id=po.source_fill_id "
            "JOIN broker_orders o ON o.broker_order_id=f.broker_order_id "
            "JOIN order_intents i ON i.intent_id=o.intent_id "
            "WHERE po.obligation_id=? AND i.kind='ENTRY'",
            (obligation_id,),
        )
        if len(rows) != 1:
            raise ValueError("protection obligation has no unique entry plan")
        return str(rows[0]["plan_id"])

    def _plan_for_local_order(self, broker_order_id: str) -> str:
        rows = self.state.rows(
            "SELECT i.plan_id FROM broker_orders o "
            "JOIN order_intents i ON i.intent_id=o.intent_id "
            "WHERE o.broker_order_id=? AND o.account_key=?",
            (broker_order_id, self.account_key),
        )
        if len(rows) != 1:
            raise ValueError("cancel target is not one locally owned broker order")
        return str(rows[0]["plan_id"])

    def _resolve_unknown_incidents(self, intent_id: str, resolved_at: datetime) -> None:
        rows = self.state.rows(
            "SELECT incident_id,detail_json FROM incidents "
            "WHERE account_key=? AND resolved_at IS NULL",
            (self.account_key,),
        )
        for row in rows:
            try:
                detail = json.loads(str(row["detail_json"]))
            except (TypeError, ValueError):
                continue
            if detail.get("intent_id") == intent_id:
                self.state.resolve_incident(
                    str(row["incident_id"]), resolved_at=resolved_at
                )

    def _obligation_symbols(self) -> tuple[str, ...]:
        return tuple(
            str(row["symbol"])
            for row in self.state.rows(
                "SELECT DISTINCT symbol FROM protection_obligations "
                "WHERE account_key=? ORDER BY symbol",
                (self.account_key,),
            )
        )

    @staticmethod
    def _position(snapshot: AccountSnapshot, symbol: str) -> PositionSnapshot | None:
        matches = tuple(
            position for position in snapshot.equity_positions if position.symbol == symbol
        )
        if len(matches) > 1:
            raise ValueError("snapshot contains duplicate position symbols")
        return matches[0] if matches else None

    @staticmethod
    def _fingerprint(facts: list[str]) -> str:
        return hashlib.sha256("\n".join(facts).encode("utf-8")).hexdigest()

    @staticmethod
    def _snapshot_key(snapshot: AccountSnapshot) -> str:
        # A re-receipt of unchanged provider facts is not new mutation
        # authority. Consume by the authoritative observation point.
        return f"{snapshot.account_masked}:{snapshot.observed_at.isoformat()}"

    def _reserve_snapshot_if_needed(
        self, snapshot: AccountSnapshot, outcome: SafetyExecutionOutcome
    ) -> None:
        if outcome.status in {ExecutionStatus.ACKNOWLEDGED, ExecutionStatus.UNKNOWN}:
            self._mutation_snapshots.add(self._snapshot_key(snapshot))

    @staticmethod
    def _format_outcome(prefix: str, outcome: SafetyExecutionOutcome) -> str:
        suffix = (
            ":" + ",".join(outcome.failure_codes)
            if outcome.failure_codes
            else ""
        )
        return f"{prefix}:{outcome.status.value}{suffix}"

    @staticmethod
    def _blocked(failures: tuple[str, ...]) -> str:
        return "BLOCKED:" + ",".join(dict.fromkeys(failures))

    @staticmethod
    def _parse_time(value: str, field: str) -> datetime:
        return ProductionLifecycleActions._aware(datetime.fromisoformat(value), field)

    @staticmethod
    def _aware(value: datetime, field: str) -> datetime:
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError(f"{field} must be timezone-aware")
        return value.astimezone(timezone.utc)


__all__ = [
    "DiscoveryExecutor",
    "LifecycleReconcileResult",
    "ProductionLifecycleActions",
]
