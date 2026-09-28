"""Persistent account-first coordinator for the full-live production lifecycle.

The service deliberately keeps transport-specific discovery and broker
mutations behind injected interfaces.  Its ordering is fixed: reconcile the
whole account, ingest fills, establish/verify protection, manage exits and
closeout, flush confirmed-event notifications, and only then consider a new
entry.  A missing capability makes the last step unreachable rather than
degrading into a partial-autonomy claim.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
import signal
import threading
from typing import Any, Callable, Mapping, Protocol, Sequence

from .broker import (
    AccountSnapshot,
    BrokerClient,
    ClientRefLookupResult,
    OrderSnapshot,
    PositionSnapshot,
)
from .calendar import NEW_YORK, SessionTimes
from .control import ControlInbox, ControlResult
from .exits import SafeCloseDecision, plan_safe_close
from .lifecycle_actions import LifecycleReconcileResult, TargetExitAssessment
from .latency import LatencyRecorder
from .models import (
    BrokerOrderState,
    BrokerSnapshot,
    EngineMode,
    Incident,
    IncidentSeverity,
    IntentKind,
    PositionRecord,
    SessionLatch as DurableSessionLatch,
)
from .notification_worker import notification_worker_health
from .notifications import (
    EnqueueOnlyOutbox,
    LiveStateOutboxAdapter,
    NotificationRoute,
    OutboxDispatcher,
)
from .policy import PolicyBundle
from .protection import (
    ProtectionDecision,
    assess_protection,
    ensure_durable_entry_fill_obligations,
    load_open_obligations,
)
from .reconcile import (
    NON_INGESTIBLE_SNAPSHOT_BLOCKERS,
    AuthoritativeReconciler,
    ReconciliationPhase,
    ReconciliationReport,
)
from .risk_runtime import SessionLatch as RiskSessionLatch, update_session_latch
from .state import LiveStateStore, StateConflict, object_hash
from .writer_lock import AccountWriterLock


_PREMARKET_RETRY_LIMIT = 3
_PREMARKET_RETRY_BASE = timedelta(minutes=5)
_PREMARKET_RETRY_MAX = timedelta(minutes=10)


class LifecycleActions(Protocol):
    """Concrete broker/market pipeline called only after the account gate."""

    def reconcile(
        self,
        *,
        snapshot: AccountSnapshot,
        now: datetime,
    ) -> LifecycleReconcileResult: ...

    def protect(
        self,
        *,
        snapshot: AccountSnapshot,
        decision: ProtectionDecision,
        now: datetime,
    ) -> str: ...

    def closeout(
        self,
        *,
        snapshot: AccountSnapshot,
        decision: SafeCloseDecision,
        now: datetime,
    ) -> str: ...

    def target_exits(
        self,
        *,
        snapshot: AccountSnapshot,
        now: datetime,
    ) -> TargetExitAssessment: ...

    def analyze_premarket(
        self,
        *,
        now: datetime,
        last_completed_slot: datetime | None,
    ) -> object: ...

    def discover_and_execute(
        self,
        *,
        snapshot: AccountSnapshot,
        now: datetime,
    ) -> tuple[str, ...]: ...


class DisabledLifecycleActions:
    """Explicit non-authority implementation used by an installed paused build."""

    def reconcile(self, **_: Any) -> LifecycleReconcileResult:
        return LifecycleReconcileResult()

    def protect(self, **_: Any) -> str:
        return "PROTECTION_MUTATION_PATH_NOT_CONFIGURED"

    def closeout(self, **_: Any) -> str:
        return "EXIT_MUTATION_PATH_NOT_CONFIGURED"

    def target_exits(self, **_: Any) -> TargetExitAssessment:
        return TargetExitAssessment()

    def analyze_premarket(self, **_: Any) -> object | None:
        return None

    def discover_and_execute(self, **_: Any) -> tuple[str, ...]:
        return ("DISCOVERY_EXECUTION_PATH_NOT_CONFIGURED",)


@dataclass(frozen=True)
class TickResult:
    observed_at: datetime
    mode_before: str
    mode_after: str
    lane: str
    snapshot_id: str | None
    reconciliation_blockers: tuple[str, ...]
    protection: tuple[ProtectionDecision, ...]
    closeout: tuple[SafeCloseDecision, ...]
    actions: tuple[str, ...]
    entries_considered: bool
    notification_sent: int
    notification_failed: int
    # ``None`` means the ordinary configured cadence.  Zero is an explicit
    # priority handoff: the runner must collect a fresh broker envelope before
    # sleeping.  A positive value is a bounded recovery cadence for exposure
    # that is still awaiting conclusive broker evidence.
    next_poll_delay_seconds: float | None = None
    error: str | None = None

    @property
    def healthy(self) -> bool:
        return self.error is None and not self.reconciliation_blockers


@dataclass(frozen=True)
class CloseoutFeasibility:
    """Conservative latency budget for closeout, never a fill guarantee."""

    obligation_count: int
    required_cycles: int
    cycle_budget_seconds: float
    estimated_seconds: float
    seconds_to_flat_deadline: float | None
    start_early: bool
    margin_exhausted: bool
    obligation_facts: tuple[str, ...] = ()


@dataclass(frozen=True)
class _MissedSessionExposure:
    """Durable evidence that broker exposure crossed a required flat deadline."""

    prior_snapshot_id: str
    prior_observed_at: datetime
    prior_trading_date: date
    required_flat_at: datetime


def _order_payload(order: OrderSnapshot) -> Mapping[str, Any]:
    return {
        "broker_order_id": order.broker_order_id,
        "symbol": order.symbol,
        "side": order.side.value,
        "order_type": order.order_type.value,
        "state": order.state.value,
        "requested_quantity": format(order.requested_quantity, "f"),
        "cumulative_filled_quantity": format(order.cumulative_filled_quantity, "f"),
        "market_hours": order.market_hours.value,
        "time_in_force": order.time_in_force.value,
        "limit_price": format(order.limit_price, "f") if order.limit_price is not None else None,
        "stop_price": format(order.stop_price, "f") if order.stop_price is not None else None,
        "client_ref_id": order.client_ref_id,
        "broker_updated_at": order.broker_updated_at,
        "fills": [
            {
                "fill_id": fill.fill_id,
                "quantity": format(fill.quantity, "f"),
                "price": format(fill.price, "f"),
                "fee": format(fill.fee, "f"),
                "executed_at": fill.executed_at,
            }
            for fill in order.fills
        ],
    }


def persist_account_snapshot(
    store: LiveStateStore,
    *,
    account_key: str,
    snapshot: AccountSnapshot,
    reconciliation_report: ReconciliationReport,
    accept_reconciliation_envelope: bool = True,
) -> BrokerSnapshot:
    """Persist normalized broker evidence and its complete position replacement."""

    positions_payload = [
        {
            "symbol": position.symbol,
            "quantity": format(position.quantity, "f"),
            "sellable_quantity": format(position.sellable_quantity, "f"),
            "held_for_sells": format(position.held_for_sells, "f"),
            "average_price": (
                format(position.average_price, "f")
                if position.average_price is not None
                else None
            ),
            "asset_class": position.asset_class,
        }
        for position in snapshot.equity_positions
    ]
    orders_payload = [_order_payload(order) for order in snapshot.equity_orders]
    envelope = {
        "account_masked": snapshot.account_masked,
        "observed_at": snapshot.observed_at,
        "received_at": snapshot.received_at,
        "account_state": snapshot.account_state,
        "account_type": snapshot.account_type,
        "funds": {
            "total_value": snapshot.funds.total_value,
            "cash": snapshot.funds.cash,
            "buying_power": snapshot.funds.buying_power,
            "unleveraged_buying_power": snapshot.funds.unleveraged_buying_power,
            "unsettled_funds": snapshot.funds.unsettled_funds,
        },
        "daily_realized_pnl": snapshot.daily_realized_pnl,
        "daily_starting_equity": snapshot.daily_starting_equity,
        "daily_external_cash_flow": snapshot.daily_external_cash_flow,
        "daily_starting_equity_receipt_hash": snapshot.daily_starting_equity_receipt_hash,
        "positions": positions_payload,
        "orders": orders_payload,
        "option_position_count": snapshot.option_position_count,
        "option_order_count": snapshot.option_order_count,
        "completeness": {
            "positions": snapshot.standard_equity_positions_complete,
            "equity_orders": snapshot.standard_equity_orders_complete,
            "option_positions": snapshot.option_positions_complete,
            "option_orders": snapshot.option_orders_complete,
            "advanced_orders": snapshot.advanced_orders_complete,
            "realized_pnl": snapshot.daily_realized_pnl_ready,
        },
    }
    evidence_revision = object_hash(envelope)
    broker_snapshot = BrokerSnapshot(
        snapshot_id=f"broker-{evidence_revision}",
        account_key=account_key,
        evidence_revision=evidence_revision,
        observed_at=snapshot.observed_at,
        received_at=snapshot.received_at,
        account_state=snapshot.account_state,
        equity=snapshot.funds.total_value,
        cash=snapshot.funds.cash,
        unleveraged_buying_power=snapshot.funds.unleveraged_buying_power,
        realized_pnl=(
            snapshot.daily_realized_pnl
            if snapshot.daily_realized_pnl is not None
            else Decimal("0")
        ),
        equity_position_count=sum(
            1 for position in snapshot.equity_positions if position.quantity != 0
        ),
        equity_order_count=len(snapshot.equity_orders),
        equity_nonterminal_order_count=sum(
            1 for order in snapshot.equity_orders if not order.state.terminal
        ),
        external_material_order_count=sum(
            1 for order in reconciliation_report.external_activity if order.blocks_entries
        ),
        option_position_count=snapshot.option_position_count,
        option_order_count=snapshot.option_order_count,
        advanced_order_count=snapshot.advanced_order_count,
        reconciliation_blocker_count=len(reconciliation_report.blockers),
        positions_reconciled=(
            accept_reconciliation_envelope
            and snapshot.standard_equity_positions_complete
        ),
        equity_orders_reconciled=(
            accept_reconciliation_envelope and snapshot.standard_equity_orders_complete
        ),
        option_positions_reconciled=(
            accept_reconciliation_envelope and snapshot.option_positions_complete
        ),
        option_orders_reconciled=(
            accept_reconciliation_envelope and snapshot.option_orders_complete
        ),
        advanced_orders_reconciled=(
            accept_reconciliation_envelope and snapshot.advanced_orders_complete
        ),
        realized_pnl_reconciled=(
            accept_reconciliation_envelope and snapshot.daily_realized_pnl_ready
        ),
        positions_digest=object_hash(positions_payload),
        orders_digest=object_hash(orders_payload),
    )
    store.record_broker_snapshot(broker_snapshot)
    if accept_reconciliation_envelope and snapshot.standard_equity_positions_complete:
        revision = round(snapshot.observed_at.timestamp() * 1_000_000)
        records = tuple(
            PositionRecord(
                account_key=account_key,
                symbol=position.symbol,
                quantity=position.quantity,
                sellable_quantity=position.sellable_quantity,
                held_for_sells=position.held_for_sells,
                average_price=position.average_price,
                source="broker_account_snapshot",
                broker_updated_at=snapshot.observed_at,
                received_at=snapshot.received_at,
                revision=revision,
                raw_hash=object_hash(positions_payload[index]),
            )
            for index, position in enumerate(snapshot.equity_positions)
        )
        store.reconcile_positions(
            snapshot_id=broker_snapshot.snapshot_id,
            account_key=account_key,
            positions=records,
            reconciled_at=snapshot.received_at,
        )
    return broker_snapshot


class FullLiveService:
    """Single-account lifecycle coordinator; safe to run continuously while paused."""

    def __init__(
        self,
        *,
        policy: PolicyBundle,
        state: LiveStateStore,
        broker: BrokerClient,
        notifications: OutboxDispatcher | EnqueueOnlyOutbox,
        notification_route: NotificationRoute | None = None,
        actions: LifecycleActions | None = None,
        entry_path_blockers: Sequence[str] = (),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.policy = policy
        self.state = state
        self.broker = broker
        self.notifications = notifications
        self.notification_route = notification_route
        self.actions = actions or DisabledLifecycleActions()
        normalized_entry_blockers = tuple(dict.fromkeys(entry_path_blockers))
        if any(
            not isinstance(item, str) or not item.strip()
            for item in normalized_entry_blockers
        ):
            raise ValueError("entry path blockers must be non-empty strings")
        self.entry_path_blockers = normalized_entry_blockers
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.account_key = policy.account_key
        self.account_masked = f"••••{policy.account_last4}"
        self.reconciler = AuthoritativeReconciler(
            account_masked=self.account_masked,
            account_key=self.account_key,
            max_snapshot_age=timedelta(
                seconds=int(policy.config["evidence"]["broker_snapshot_max_age_seconds"])
            ),
        )
        self._startup = True

    def run_once(self) -> TickResult:
        tick_started_at = self._now()
        now = tick_started_at
        runtime = self.state.runtime_status()
        if runtime is None:
            raise RuntimeError("runtime state must be initialized before service start")
        mode_before = EngineMode(str(runtime["mode"]))
        authority_enabled = bool(runtime["authority_enabled"])
        lane = self.policy.calendar.lane(now)
        actions: list[str] = ["RECONCILE_ACCOUNT"]
        try:
            snapshot = self.broker.get_account_snapshot(self.account_masked)
            self.policy.require_account(snapshot.account_masked, snapshot.account_type)
            lookup = self._lookup_unknown_client_refs(snapshot=snapshot)
            if lookup is not None:
                snapshot = self._merge_client_ref_lookup(snapshot, lookup)
                confirmed_absent = frozenset(
                    lookup.confirmed_absent_client_refs
                )
            else:
                confirmed_absent = frozenset()
            # Account and exact-ref reads can span seconds.  All freshness,
            # calendar, risk, and later market checks use a clock sample taken
            # after those reads, never the tick-start timestamp.
            now = self._now()
            if now < tick_started_at:
                raise RuntimeError("SERVICE_CLOCK_REGRESSED_DURING_ACCOUNT_READ")
            lane = self.policy.calendar.lane(now)
            report = self.reconciler.reconcile_snapshot(
                self.state,
                snapshot=snapshot,
                capabilities=self.broker.capabilities,
                now=now,
                phase=(
                    ReconciliationPhase.STARTUP
                    if self._startup
                    else ReconciliationPhase.CONTINUOUS
                ),
                confirmed_absent_client_refs=confirmed_absent,
            )
            invalid_envelope = NON_INGESTIBLE_SNAPSHOT_BLOCKERS.intersection(
                report.blockers
            )

            durable_snapshot = persist_account_snapshot(
                self.state,
                account_key=self.account_key,
                snapshot=snapshot,
                reconciliation_report=report,
                accept_reconciliation_envelope=not invalid_envelope,
            )
            self._startup = False
        except Exception as exc:
            post_failure = self._now()
            if post_failure >= tick_started_at:
                now = post_failure
                lane = self.policy.calendar.lane(now)
            category = (
                "AUTHENTICATION_INCIDENT"
                if "auth" in type(exc).__name__.lower()
                else "RECONCILIATION_INCIDENT"
            )
            self._open_incident(category, exc, now)
            self._notify(
                (
                    "AUTHENTICATION_INCIDENT"
                    if category == "AUTHENTICATION_INCIDENT"
                    else "RUNTIME_INCIDENT"
                ),
                {
                    "event_id": f"{now.astimezone(NEW_YORK).date()}:{category}",
                    "state": "blocked",
                    "symbol": "ACCOUNT",
                    "reason": type(exc).__name__,
                },
                now,
            )
            if mode_before not in {EngineMode.PAUSED, EngineMode.STOPPED}:
                self._transition(EngineMode.INCIDENT, now, category)
            sent, failed = self._deliver_notifications(now)
            # The account read was attempted and the incident notification was
            # handed off first. Analysis remains read-only and can report its
            # own provider blocker without delaying the safety notification.
            actions.extend(self._premarket_analysis_actions(now=now))
            mode_after = str(self.state.runtime_status()["mode"])
            return TickResult(
                observed_at=now,
                mode_before=mode_before.value,
                mode_after=mode_after,
                lane=lane,
                snapshot_id=None,
                reconciliation_blockers=(category,),
                protection=(),
                closeout=(),
                actions=tuple(
                    actions + ["BLOCK_DISCOVERY", self._notification_action()]
                ),
                entries_considered=False,
                notification_sent=sent,
                notification_failed=failed,
                error=type(exc).__name__,
            )

        blockers = list(report.blockers)
        if not snapshot.daily_realized_pnl_ready:
            blockers.append("DAILY_REALIZED_PNL_NOT_READY")
        if self.policy.daily_starting_equity_risk and not snapshot.daily_starting_equity_ready:
            blockers.append("DAILY_STARTING_EQUITY_EVIDENCE_NOT_READY")
        blockers.extend(self.policy.activation_blockers)
        if not self.policy.live_entries_configured:
            blockers.append("LIVE_ENTRIES_DISABLED_IN_SIGNED_CONFIG")
        blockers.extend(self.entry_path_blockers)
        blockers = list(dict.fromkeys(blockers))

        if invalid_envelope:
            # A rejected account envelope is diagnostic evidence only.  None
            # of its positions, orders, fills, risk fields, or timestamps may
            # drive a durable lifecycle transition or a broker mutation.
            # This branch intentionally precedes fill obligations, lifecycle
            # reconciliation, protection, latches, closeout, and discovery.
            actions.extend(
                (
                    "QUARANTINE_NON_INGESTIBLE_SNAPSHOT",
                    "BLOCK_LIFECYCLE_MUTATIONS",
                    "BLOCK_DISCOVERY",
                )
            )
            detail = ",".join(sorted(invalid_envelope))
            error = RuntimeError(f"non-ingestible broker snapshot: {detail}")
            self._open_incident("NON_INGESTIBLE_BROKER_SNAPSHOT", error, now)
            self._notify(
                "RUNTIME_INCIDENT",
                {
                    "event_id": (
                        f"{now.astimezone(NEW_YORK).date()}:"
                        f"non-ingestible:{hashlib.sha256(detail.encode()).hexdigest()[:16]}"
                    ),
                    "state": "quarantined",
                    "symbol": "ACCOUNT",
                    "reason": detail,
                },
                now,
            )
            mode_current = EngineMode(str(self.state.runtime_status()["mode"]))
            if mode_current in {EngineMode.ACTIVE, EngineMode.RECONCILING}:
                self._transition(
                    EngineMode.PAUSE_NEW_ENTRIES,
                    now,
                    "non-ingestible broker snapshot quarantined",
                )
            actions.append(self._notification_action())
            sent, failed = self._deliver_notifications(now)
            actions.extend(self._premarket_analysis_actions(now=now))
            return TickResult(
                observed_at=now,
                mode_before=mode_before.value,
                mode_after=str(self.state.runtime_status()["mode"]),
                lane=lane,
                snapshot_id=durable_snapshot.snapshot_id,
                reconciliation_blockers=tuple(blockers),
                protection=(),
                closeout=(),
                actions=tuple(actions),
                entries_considered=False,
                notification_sent=sent,
                notification_failed=failed,
            )

        # A timed closeout, a durable prior-session exposure, or a hard-kill
        # owns the risk-reduction lane before any new stop obligation or stop
        # order is created.  Confirmed fills are already durable from account
        # reconciliation above; when an immediate close is due, creating
        # another contingent sell only adds a cancel race and consumes a fresh
        # snapshot mutation that the close needs instead.
        missed_session = self._missed_session_exposure(
            snapshot=snapshot,
            durable_snapshot=durable_snapshot,
            now=now,
        )
        if missed_session is not None:
            blockers.append("MISSED_SESSION_CLOSEOUT_EXPOSURE")
            actions.append(
                "MISSED_SESSION_CLOSEOUT_EXPOSURE:"
                f"session={missed_session.prior_trading_date.isoformat()}:"
                f"snapshot={missed_session.prior_snapshot_id}"
            )
            self._record_missed_session_exposure(
                evidence=missed_session,
                snapshot=snapshot,
                durable_snapshot=durable_snapshot,
                now=now,
            )
            current_mode = EngineMode(str(self.state.runtime_status()["mode"]))
            if current_mode is not EngineMode.STOPPED:
                safety_session_open = self._safety_session_open(now)
                ownership_mode = (
                    EngineMode.MANAGED_CLOSEOUT
                    if safety_session_open
                    and current_mode is not EngineMode.PAUSED
                    else EngineMode.INCIDENT
                )
                self._transition(
                    ownership_mode,
                    now,
                    "broker exposure survived a prior session flat deadline",
                )
                # MANAGED_CLOSEOUT requires the armed writer lease.  If that
                # proof is unexpectedly unavailable, retain durable incident
                # ownership instead of leaving an entry-capable mode in force.
                post_transition_mode = EngineMode(
                    str(self.state.runtime_status()["mode"])
                )
                if post_transition_mode not in {
                    EngineMode.MANAGED_CLOSEOUT,
                    EngineMode.INCIDENT,
                    EngineMode.STOPPED,
                }:
                    self._transition(
                        EngineMode.INCIDENT,
                        now,
                        "missed-session closeout ownership could not be armed",
                    )

        latch = self._update_risk_latch(snapshot, now)
        if latch is not None:
            if latch.loss_lock:
                blockers.append("IRREVERSIBLE_DAILY_NEW_ENTRY_LOCK")
            if latch.hard_kill:
                blockers.append("HARD_DAILY_LOSS_KILL")

        mode_current = EngineMode(str(self.state.runtime_status()["mode"]))
        has_broker_exposure = any(
            position.quantity != 0 for position in snapshot.equity_positions
        ) or any(
            not order.state.terminal for order in snapshot.equity_orders
        )
        closeout_owns_exposure = (
            mode_current is EngineMode.MANAGED_CLOSEOUT
            or lane in {"closeout", "flat_deadline"}
            or (lane == "closed" and has_broker_exposure)
            or (mode_current is EngineMode.INCIDENT and has_broker_exposure)
            or missed_session is not None
            or bool(latch is not None and latch.hard_kill)
        )

        actions.append("INGEST_CONFIRMED_FILLS")
        if closeout_owns_exposure:
            actions.append("DEFER_NEW_PROTECTION_OBLIGATIONS_TO_CLOSEOUT")
        else:
            try:
                self._ensure_fill_obligations()
            except Exception as exc:
                blockers.append("FILL_OBLIGATION_INGESTION_FAILED")
                actions.append(
                    f"INGEST_CONFIRMED_FILLS:FAILED:{type(exc).__name__}"
                )
                self._lifecycle_failure("FILL_OBLIGATION_INGESTION", exc, now)
        try:
            lifecycle_update = self.actions.reconcile(snapshot=snapshot, now=now)
            actions.extend(
                f"RECONCILE_LIFECYCLE:{item}" for item in lifecycle_update.actions
            )
            blockers.extend(lifecycle_update.blockers)
        except Exception as exc:
            blockers.append("LIFECYCLE_RECONCILIATION_FAILED")
            actions.append(
                f"RECONCILE_LIFECYCLE:FAILED:{type(exc).__name__}"
            )
            self._lifecycle_failure("RECONCILIATION", exc, now)
        # Free previously consumed entry capacity only from an ingestible
        # broker envelope.  The store independently re-reads all complete,
        # account-wide and durable-flat proof inside its write transaction;
        # ordinary non-flat snapshots simply release nothing.
        if not invalid_envelope:
            try:
                released = self.state.release_reservations_after_flat_snapshot(
                    account_key=self.account_key,
                    snapshot_id=durable_snapshot.snapshot_id,
                    occurred_at=max(now, snapshot.received_at),
                )
                actions.extend(
                    f"RELEASE_FLAT_RISK:{reservation_id}"
                    for reservation_id in released
                )
            except Exception as exc:
                blockers.append("RISK_RESERVATION_RELEASE_GUARD_FAILED")
                actions.append(
                    f"RELEASE_FLAT_RISK:FAILED:{type(exc).__name__}"
                )
                self._lifecycle_failure("RISK_RESERVATION_RELEASE", exc, now)
        mode_current = EngineMode(str(self.state.runtime_status()["mode"]))
        # A lifecycle reconciliation failure can change durable mode, so the
        # direct owner is refreshed before estimating remaining work.
        closeout_owns_exposure = (
            closeout_owns_exposure
            or mode_current is EngineMode.MANAGED_CLOSEOUT
            or (mode_current is EngineMode.INCIDENT and has_broker_exposure)
        )
        feasibility = self._closeout_feasibility(
            snapshot=snapshot,
            now=now,
            closeout_owns_exposure=closeout_owns_exposure,
        )
        if feasibility.obligation_count:
            actions.append(
                "CLOSEOUT_FEASIBILITY:"
                f"obligations={feasibility.obligation_count}:"
                f"cycles={feasibility.required_cycles}:"
                f"cycle_budget_seconds={feasibility.cycle_budget_seconds:.3f}:"
                f"estimated_seconds={feasibility.estimated_seconds:.3f}:"
                "seconds_to_deadline="
                + (
                    f"{feasibility.seconds_to_flat_deadline:.3f}"
                    if feasibility.seconds_to_flat_deadline is not None
                    else "unavailable"
                )
            )
        if feasibility.margin_exhausted:
            blockers.append("CLOSEOUT_DEADLINE_FEASIBILITY_MARGIN_EXHAUSTED")
            self._record_closeout_feasibility_incident(feasibility, now)
        elif feasibility.start_early:
            actions.append("CLOSEOUT_FEASIBILITY_EARLY_START")
        elif feasibility.obligation_count == 0:
            if self._resolve_closeout_feasibility_incident(now):
                actions.append("CLOSEOUT_FEASIBILITY_INCIDENT_RESOLVED_FLAT")
        should_close = closeout_owns_exposure or feasibility.start_early

        actions.append("VERIFY_OR_ESTABLISH_PROTECTION")
        protection = self._assess_protection(snapshot)
        protection_safe = all(item.protected for item in protection)
        if not protection_safe:
            blockers.append("UNPROTECTED_EXPOSURE_PRESENT")
            self._notify_unprotected(protection, durable_snapshot.snapshot_id, now)
            unprotected = tuple(item for item in protection if not item.protected)
            if should_close:
                actions.append(
                    "PROTECTION_DEFERRED_TO_CLOSEOUT:"
                    f"symbols={len(unprotected)}"
                )
            elif unprotected:
                # One broker consequence per exhaustive account snapshot.  Any
                # additional symbol waits for the immediately requested fresh
                # snapshot rather than sharing stale sell capacity.
                decision = unprotected[0]
                if authority_enabled:
                    try:
                        result = self.actions.protect(
                            snapshot=snapshot, decision=decision, now=now
                        )
                        actions.append(f"PROTECT:{decision.symbol}:{result}")
                        self._promote_lifecycle_outcome(
                            stage="PROTECTION",
                            symbol=decision.symbol,
                            outcome=result,
                            blockers=blockers,
                            now=now,
                        )
                    except Exception as exc:
                        blockers.append("LIFECYCLE_PROTECTION_FAILED")
                        actions.append(
                            f"PROTECT:{decision.symbol}:FAILED:{type(exc).__name__}"
                        )
                        self._lifecycle_failure("PROTECTION", exc, now)
                else:
                    actions.append(
                        f"PROTECT:{decision.symbol}:BLOCKED_NO_RUNTIME_AUTHORITY"
                    )
                if len(unprotected) > 1:
                    actions.append(
                        "PROTECTION_DEFER_ADDITIONAL_SYMBOLS:"
                        f"{len(unprotected) - 1}"
                    )

        target_assessment = TargetExitAssessment()
        target_exit_pending = False
        target_method = getattr(self.actions, "target_exits", None)
        if should_close:
            actions.append("TARGET_EXIT_DEFERRED_TO_MANDATORY_CLOSEOUT")
        elif callable(target_method):
            try:
                target_assessment = target_method(snapshot=snapshot, now=now)
                actions.extend(
                    f"TARGET_EXIT:{item}" for item in target_assessment.actions
                )
                blockers.extend(target_assessment.blockers)
                target_exit_pending = any(
                    item.action.value != "FLAT"
                    for item in target_assessment.decisions
                )
            except Exception as exc:
                blockers.append("TARGET_EXIT_ASSESSMENT_FAILED")
                actions.append(
                    f"TARGET_EXIT:ASSESSMENT_FAILED:{type(exc).__name__}"
                )
                self._lifecycle_failure("TARGET_EXIT_ASSESSMENT", exc, now)
        elif has_broker_exposure and not should_close:
            blockers.append("TARGET_EXIT_PATH_NOT_CONFIGURED")

        closeout: tuple[SafeCloseDecision, ...] = ()
        if should_close:
            actions.append("MANAGED_CLOSEOUT")
            if mode_current not in {EngineMode.PAUSED, EngineMode.STOPPED, EngineMode.INCIDENT}:
                reason = (
                    "conservative closeout deadline feasibility escalation"
                    if feasibility.start_early
                    else "session or hard-kill closeout"
                )
                self._transition(EngineMode.MANAGED_CLOSEOUT, now, reason)
            closeout = self._plan_closeout(snapshot)
            pending_closeout = tuple(
                item for item in closeout if item.action.value != "FLAT"
            )
            if pending_closeout:
                decision = pending_closeout[0]
                if authority_enabled:
                    try:
                        result = self.actions.closeout(
                            snapshot=snapshot, decision=decision, now=now
                        )
                        actions.append(f"CLOSEOUT:{decision.symbol}:{result}")
                        self._promote_lifecycle_outcome(
                            stage="CLOSEOUT",
                            symbol=decision.symbol,
                            outcome=result,
                            blockers=blockers,
                            now=now,
                        )
                    except Exception as exc:
                        blockers.append("LIFECYCLE_CLOSEOUT_FAILED")
                        actions.append(
                            f"CLOSEOUT:{decision.symbol}:FAILED:{type(exc).__name__}"
                        )
                        self._lifecycle_failure("CLOSEOUT", exc, now)
                else:
                    actions.append(
                        f"CLOSEOUT:{decision.symbol}:BLOCKED_NO_RUNTIME_AUTHORITY"
                    )
                if len(pending_closeout) > 1:
                    actions.append(
                        "CLOSEOUT_DEFER_ADDITIONAL_SYMBOLS:"
                        f"{len(pending_closeout) - 1}"
                    )
                blockers.append("CLOSEOUT_NOT_FLAT")
        elif target_exit_pending:
            # The target latch persists independently of later price.  Execute
            # only the first deterministic symbol step from this broker
            # envelope; all remaining symbols wait for a fresh exhaustive
            # snapshot so no two mutations share stale capacity evidence.
            closeout = target_assessment.decisions
            decision = next(
                (
                    item
                    for item in target_assessment.decisions
                    if item.action.value != "FLAT"
                ),
                None,
            )
            if decision is not None:
                prior_broker_consequence = any(
                    action.startswith("PROTECT:")
                    and any(
                        marker in action
                        for marker in (
                            ":ACKNOWLEDGED",
                            ":UNKNOWN",
                            ":REJECTED",
                            ":FAILED",
                        )
                    )
                    for action in actions
                )
                if prior_broker_consequence:
                    actions.append(
                        "TARGET_EXIT_ACTION:"
                        f"{decision.symbol}:DEFER_FRESH_BROKER_SNAPSHOT"
                    )
                elif authority_enabled:
                    try:
                        result = self.actions.closeout(
                            snapshot=snapshot, decision=decision, now=now
                        )
                        actions.append(
                            f"TARGET_EXIT_ACTION:{decision.symbol}:{result}"
                        )
                        self._promote_lifecycle_outcome(
                            stage="TARGET_EXIT",
                            symbol=decision.symbol,
                            outcome=result,
                            blockers=blockers,
                            now=now,
                        )
                    except Exception as exc:
                        blockers.append("LIFECYCLE_TARGET_EXIT_FAILED")
                        actions.append(
                            "TARGET_EXIT_ACTION:"
                            f"{decision.symbol}:FAILED:{type(exc).__name__}"
                        )
                        self._lifecycle_failure("TARGET_EXIT", exc, now)
                else:
                    blockers.append("TARGET_EXIT_BLOCKED_NO_RUNTIME_AUTHORITY")
                    actions.append(
                        f"TARGET_EXIT_ACTION:{decision.symbol}:BLOCKED_NO_RUNTIME_AUTHORITY"
                    )
            if len(target_assessment.decisions) > 1:
                actions.append(
                    "TARGET_EXIT_DEFER_ADDITIONAL_SYMBOLS:"
                    f"{len(target_assessment.decisions) - 1}"
                )
        elif blockers and mode_current in {
            EngineMode.ACTIVE,
            EngineMode.RECONCILING,
        }:
            self._transition(EngineMode.PAUSE_NEW_ENTRIES, now, blockers[0])

        mode_current = EngineMode(str(self.state.runtime_status()["mode"]))
        if (
            authority_enabled
            and self.entry_path_blockers
            and mode_current in {EngineMode.ACTIVE, EngineMode.RECONCILING}
        ):
            actions.append("BLOCK_DISCOVERY_ENTRY_PATH_UNAVAILABLE")
            self._transition(
                EngineMode.PAUSE_NEW_ENTRIES,
                now,
                self.entry_path_blockers[0],
            )

        if lane == "closed" and has_broker_exposure:
            blockers.append("OVERNIGHT_EXPOSURE_INCIDENT")
            if EngineMode(str(self.state.runtime_status()["mode"])) not in {
                EngineMode.PAUSED,
                EngineMode.STOPPED,
                EngineMode.INCIDENT,
            }:
                self._transition(
                    EngineMode.INCIDENT,
                    now,
                    "broker exposure remains outside a supported exit session",
                )
            self._notify(
                "CLOSEOUT_INCIDENT",
                {
                    "event_id": (
                        f"{now.astimezone(NEW_YORK).date()}:overnight:"
                        + hashlib.sha256(
                            ",".join(self._exposure_symbols(snapshot)).encode("utf-8")
                        ).hexdigest()[:16]
                    ),
                    "state": "unresolved",
                    "symbol": ",".join(self._exposure_symbols(snapshot)),
                    "reason": "broker position or working order remains after session close",
                },
                now,
            )

        # Analysis is deliberately last among account/risk/lifecycle work.
        # It is read-only, never delays protection or an exit, and a completed
        # slot is hash-chained so a process restart cannot repeat it.
        actions.extend(self._premarket_analysis_actions(now=now))

        entries_considered = False
        mode_current = EngineMode(str(self.state.runtime_status()["mode"]))
        independent_notification_blockers = self._notification_entry_blockers(now)
        if (
            authority_enabled
            and mode_current in {EngineMode.ACTIVE, EngineMode.RECONCILING}
            and independent_notification_blockers
        ):
            blockers.extend(independent_notification_blockers)
            actions.append("BLOCK_DISCOVERY_NOTIFICATION_WORKER_UNHEALTHY")
            self._transition(
                EngineMode.PAUSE_NEW_ENTRIES,
                now,
                independent_notification_blockers[0],
            )
            mode_current = EngineMode(str(self.state.runtime_status()["mode"]))
        activated_this_tick = False
        if (
            mode_current is EngineMode.RECONCILING
            and authority_enabled
            and not blockers
            and protection_safe
            and lane == "regular_entry"
        ):
            self._transition(EngineMode.ACTIVE, now, "fresh startup reconciliation complete")
            mode_current = EngineMode.ACTIVE
            # ACTIVE emits a durable RECOVERED event. The independent worker
            # must deliver it before any later tick may consider an entry.
            activated_this_tick = isinstance(self.notifications, EnqueueOnlyOutbox)
        if (
            mode_current is EngineMode.ACTIVE
            and authority_enabled
            and lane == "regular_entry"
            and not blockers
            and report.entries_allowed
            and protection_safe
            and not activated_this_tick
            and not target_exit_pending
        ):
            actions.append("DISCOVER_AND_EXECUTE")
            try:
                discovery_actions = self.actions.discover_and_execute(
                    snapshot=snapshot, now=now
                )
                actions.extend(discovery_actions)
                for outcome in discovery_actions:
                    if outcome.startswith(("ENTRY:", "BLOCKED:")):
                        self._promote_lifecycle_outcome(
                            stage="ENTRY",
                            symbol="ACCOUNT",
                            outcome=outcome,
                            blockers=blockers,
                            now=now,
                        )
                entries_considered = True
            except Exception as exc:
                blockers.append("LIFECYCLE_DISCOVERY_FAILED")
                actions.append(f"DISCOVERY:FAILED:{type(exc).__name__}")
                self._lifecycle_failure("DISCOVERY", exc, now)
                self._transition(
                    EngineMode.PAUSE_NEW_ENTRIES,
                    now,
                    "lifecycle discovery failure",
                )
        else:
            actions.append("BLOCK_DISCOVERY")

        self._notify_reconciled_fills(report, now)
        self._notify_working_protection(now)
        if lane in {"flat_deadline", "closed"}:
            self._notify_end_of_day_if_flat(durable_snapshot, now)

        # Hand off after every broker-side consequence. Production only
        # enqueues; an independently supervised worker owns provider delivery.
        actions.append(self._notification_action())
        sent, failed = self._deliver_notifications(now)

        next_poll_delay = self._priority_poll_delay(
            actions=actions,
            blockers=blockers,
            protection=protection,
            closeout=closeout,
        )

        return TickResult(
            observed_at=now,
            mode_before=mode_before.value,
            mode_after=str(self.state.runtime_status()["mode"]),
            lane=lane,
            snapshot_id=durable_snapshot.snapshot_id,
            reconciliation_blockers=tuple(dict.fromkeys(blockers)),
            protection=protection,
            closeout=closeout,
            actions=tuple(actions),
            entries_considered=entries_considered,
            notification_sent=sent,
            notification_failed=failed,
            next_poll_delay_seconds=next_poll_delay,
        )

    def _premarket_analysis_actions(self, *, now: datetime) -> tuple[str, ...]:
        """Run at most one durable analysis-only premarket slot.

        This helper is intentionally outside every mutation-authority path.
        It accepts only a result that proves both execution flags false and
        records completion in the append-only audit chain.  Provider failures
        are reported as local analysis facts; they never become a regular-hours
        entry blocker and never enqueue a trading notification.
        """

        if self.policy.calendar.lane(now) != "premarket_attended":
            return ()
        try:
            slot = self._current_premarket_slot(now)
            last_completed = self._last_completed_premarket_slot()
            if last_completed == slot:
                return ("PREMARKET_ANALYSIS:CURRENT_SLOT_ALREADY_COMPLETE",)
            if last_completed is not None and last_completed > slot:
                raise ValueError("durable premarket completion is ahead of current slot")
            retry_action = self._premarket_retry_action(slot=slot, now=now)
            if retry_action is not None:
                return (retry_action,)
        except Exception as exc:
            return (f"PREMARKET_ANALYSIS:FAILED:{type(exc).__name__}",)

        try:
            method = getattr(self.actions, "analyze_premarket", None)
            if not callable(method):
                raise RuntimeError("PREMARKET_ANALYSIS_PATH_NOT_CONFIGURED")
            result = method(now=now, last_completed_slot=last_completed)
            if result is None:
                raise RuntimeError("PREMARKET_ANALYSIS_PATH_NOT_CONFIGURED")
            if (
                getattr(result, "execution_authority", None) is not False
                or getattr(result, "approved_to_buy", None) is not False
            ):
                raise ValueError("premarket analysis result claimed order authority")
            status_object = getattr(result, "status", None)
            status = str(getattr(status_object, "value", status_object))
            schedule = getattr(result, "schedule", None)
            if schedule is None:
                raise ValueError("premarket analysis schedule is missing")
            if (
                getattr(schedule, "execution_authority", None) is not False
                or getattr(schedule, "approved_to_buy", None) is not False
            ):
                raise ValueError("premarket analysis schedule claimed order authority")

            scheduled_for = self._aware_service_time(
                getattr(schedule, "scheduled_for", None),
                "premarket scheduled_for",
            )
            if scheduled_for != slot:
                raise ValueError("premarket analyzer returned a different slot")

            if status == "BLOCKED":
                reasons = tuple(str(item) for item in getattr(result, "blockers", ()))
                self._record_premarket_attempt(
                    slot=slot,
                    attempted_at=now,
                    status="BLOCKED",
                    blockers=reasons,
                )
                detail = ",".join(reasons) if reasons else "UNSPECIFIED"
                return (f"PREMARKET_ANALYSIS:BLOCKED:{detail}",)
            if status == "NOT_DUE":
                raise ValueError("analyzer reported not-due without durable completion")
            if status == "OUTSIDE_LANE":
                raise ValueError("premarket analyzer disagreed with service lane")
            if status != "COMPLETED":
                raise ValueError("premarket analysis status is unsupported")

            observed_at = self._aware_service_time(
                getattr(result, "observed_at", None),
                "premarket observed_at",
            )
            if (
                getattr(schedule, "due", None) is not True
                or scheduled_for > now
                or observed_at != now
            ):
                raise ValueError("premarket analysis completion has invalid timing")
            analysis_id = str(getattr(result, "analysis_id", "")).strip()
            if not analysis_id:
                raise ValueError("premarket analysis completion has no identity")

            candidates = tuple(getattr(result, "candidates", ()))
            candidate_payload: list[Mapping[str, Any]] = []
            for expected_rank, candidate in enumerate(candidates, 1):
                if (
                    getattr(candidate, "execution_authority", None) is not False
                    or getattr(candidate, "approved_to_buy", None) is not False
                ):
                    raise ValueError("premarket candidate claimed order authority")
                rank = int(getattr(candidate, "rank", 0))
                symbol = str(getattr(candidate, "symbol", "")).strip().upper()
                source_plan_id = str(
                    getattr(candidate, "source_plan_id", "")
                ).strip()
                if rank != expected_rank or not symbol or not source_plan_id:
                    raise ValueError("premarket candidate identity/rank is invalid")
                candidate_payload.append(
                    {
                        "rank": rank,
                        "symbol": symbol,
                        "source_plan_id": source_plan_id,
                        "instrument_evidence_id": getattr(
                            candidate, "instrument_evidence_id", None
                        ),
                        "quote_observed_at": getattr(
                            candidate, "quote_observed_at", None
                        ),
                        "latest_completed_bar_end": getattr(
                            candidate, "latest_completed_bar_end", None
                        ),
                        "hard_gate_failures": list(
                            getattr(candidate, "hard_gate_failures", ())
                        ),
                        "deferred_execution_gates": list(
                            getattr(candidate, "deferred_execution_gates", ())
                        ),
                        "execution_authority": False,
                        "approved_to_buy": False,
                    }
                )

            completion_id = "premarket-analysis-completed-" + hashlib.sha256(
                (
                    f"{self.account_key}\n{scheduled_for.isoformat()}\n"
                    f"{analysis_id}"
                ).encode("utf-8")
            ).hexdigest()
            self.state.append_event(
                stream=self.account_key,
                event_type="PREMARKET_ANALYSIS_COMPLETED",
                entity_type="premarket_analysis_slot",
                entity_id=scheduled_for.isoformat(),
                occurred_at=observed_at,
                event_id=completion_id,
                payload={
                    "schema": "titan_premarket_analysis_completion_2026-09-14_v1",
                    "analysis_id": analysis_id,
                    "scheduled_for": scheduled_for,
                    "observed_at": observed_at,
                    "candidate_count": len(candidate_payload),
                    "candidates": candidate_payload,
                    "blockers": [
                        str(item) for item in getattr(result, "blockers", ())
                    ],
                    "execution_authority": False,
                    "approved_to_buy": False,
                },
            )
            self._record_premarket_attempt(
                slot=slot,
                attempted_at=observed_at,
                status="COMPLETED",
                blockers=tuple(
                    str(item) for item in getattr(result, "blockers", ())
                ),
            )
            return (
                "PREMARKET_ANALYSIS:COMPLETED:"
                f"slot={scheduled_for.isoformat()}:"
                f"analysis={analysis_id}:candidates={len(candidate_payload)}",
            )
        except Exception as exc:
            try:
                self._record_premarket_attempt(
                    slot=slot,
                    attempted_at=now,
                    status="FAILED",
                    blockers=(type(exc).__name__,),
                )
            except Exception:
                return (
                    "PREMARKET_ANALYSIS:FAILED:"
                    "DURABLE_RETRY_STATE_WRITE_FAILED",
                )
            return (f"PREMARKET_ANALYSIS:FAILED:{type(exc).__name__}",)

    def _current_premarket_slot(self, now: datetime) -> datetime:
        current = self._aware_service_time(now, "premarket slot time")
        sessions = self.policy.config.get("sessions", {})
        try:
            interval = int(
                sessions.get("premarket_analysis_interval_minutes", 30)
            )
            start_clock = time.fromisoformat(str(sessions.get("premarket_start")))
        except (TypeError, ValueError) as exc:
            raise ValueError("premarket schedule is invalid") from exc
        if interval <= 0:
            raise ValueError("premarket interval must be positive")
        local = current.astimezone(NEW_YORK)
        if self.policy.calendar.session_times(local.date()) is None:
            raise ValueError("premarket slot has no verified trading session")
        start = datetime.combine(local.date(), start_clock, NEW_YORK)
        elapsed_seconds = (local - start).total_seconds()
        if elapsed_seconds < 0:
            raise ValueError("premarket slot precedes configured start")
        slot_local = start + timedelta(
            minutes=(int(elapsed_seconds // 60) // interval) * interval
        )
        slot = slot_local.astimezone(timezone.utc)
        if self.policy.calendar.lane(slot) != "premarket_attended":
            raise ValueError("computed premarket slot is outside analysis lane")
        return slot

    def _premarket_attempt_rows(self, slot: datetime) -> list[Any]:
        return self.state.rows(
            "SELECT occurred_at,payload_json FROM audit_events WHERE stream=? "
            "AND event_type='PREMARKET_ANALYSIS_ATTEMPT' "
            "AND entity_type='premarket_analysis_slot' AND entity_id=? "
            "ORDER BY sequence",
            (self.account_key, slot.isoformat()),
        )

    def _premarket_retry_action(
        self, *, slot: datetime, now: datetime
    ) -> str | None:
        rows = self._premarket_attempt_rows(slot)
        attempts = len(rows)
        if attempts >= _PREMARKET_RETRY_LIMIT:
            return (
                "PREMARKET_ANALYSIS:RETRY_EXHAUSTED:"
                f"slot={slot.isoformat()}:attempts={attempts}"
            )
        if not rows:
            return None
        last_attempt = self._aware_service_time(
            datetime.fromisoformat(str(rows[-1]["occurred_at"])),
            "premarket last attempt",
        )
        multiplier = 2 ** max(attempts - 1, 0)
        backoff = min(_PREMARKET_RETRY_BASE * multiplier, _PREMARKET_RETRY_MAX)
        next_attempt = last_attempt + backoff
        current = self._aware_service_time(now, "premarket retry time")
        if current < next_attempt:
            return (
                "PREMARKET_ANALYSIS:RETRY_BACKOFF:"
                f"slot={slot.isoformat()}:attempts={attempts}:"
                f"next={next_attempt.isoformat()}"
            )
        return None

    def _record_premarket_attempt(
        self,
        *,
        slot: datetime,
        attempted_at: datetime,
        status: str,
        blockers: Sequence[str],
    ) -> int:
        normalized_status = str(status).strip().upper()
        if normalized_status not in {"BLOCKED", "FAILED", "COMPLETED"}:
            raise ValueError("premarket attempt status is invalid")
        rows = self._premarket_attempt_rows(slot)
        ordinal = len(rows) + 1
        if ordinal > _PREMARKET_RETRY_LIMIT:
            raise StateConflict("premarket retry limit is already exhausted")
        event_id = "premarket-analysis-attempt-" + hashlib.sha256(
            (
                f"{self.account_key}\n{slot.isoformat()}\n{ordinal}"
            ).encode("utf-8")
        ).hexdigest()
        self.state.append_event(
            stream=self.account_key,
            event_type="PREMARKET_ANALYSIS_ATTEMPT",
            entity_type="premarket_analysis_slot",
            entity_id=slot.isoformat(),
            occurred_at=attempted_at,
            event_id=event_id,
            payload={
                "schema": "titan_premarket_analysis_attempt_2026-09-14_v1",
                "scheduled_for": slot,
                "attempt": ordinal,
                "status": normalized_status,
                "blockers": list(blockers),
                "execution_authority": False,
                "approved_to_buy": False,
            },
        )
        return ordinal

    def _last_completed_premarket_slot(self) -> datetime | None:
        rows = self.state.rows(
            "SELECT entity_id FROM audit_events WHERE stream=? "
            "AND event_type='PREMARKET_ANALYSIS_COMPLETED' "
            "AND entity_type='premarket_analysis_slot' "
            "ORDER BY sequence DESC LIMIT 1",
            (self.account_key,),
        )
        if not rows:
            return None
        return self._aware_service_time(
            datetime.fromisoformat(str(rows[0]["entity_id"])),
            "durable premarket completed slot",
        )

    @staticmethod
    def _aware_service_time(value: object, field: str) -> datetime:
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError(f"{field} must be timezone-aware")
        return value.astimezone(timezone.utc)

    def _lookup_unknown_client_refs(
        self, *, snapshot: AccountSnapshot
    ) -> ClientRefLookupResult | None:
        """Obtain exact positive and negative evidence for ambiguous refs."""

        if not self.broker.capabilities.supports_ref_id_lookup:
            return None
        rows = self.state.rows(
            "SELECT client_ref FROM order_intents "
            "WHERE account_key=? AND state IN ('SUBMITTING','UNKNOWN') "
            "AND kind IN ('ENTRY','PROTECTION','EXIT') ORDER BY client_ref",
            (self.account_key,),
        )
        requested = tuple(str(row["client_ref"]) for row in rows)
        if not requested:
            return None
        result = self.broker.lookup_equity_orders_by_client_ref(
            self.account_masked, requested
        )
        if not isinstance(result, ClientRefLookupResult):
            raise ValueError("broker client-ref lookup was not normalized")
        observed_now = self._now()
        age = (observed_now - result.observed_at).total_seconds()
        maximum = int(
            self.policy.config["evidence"]["broker_snapshot_max_age_seconds"]
        )
        if (
            result.account_masked != self.account_masked
            or result.requested_client_refs != requested
            or result.complete is not True
            or age < -1
            or age > maximum
            or result.received_at < result.observed_at
            or result.received_at > observed_now + timedelta(seconds=2)
            or result.observed_at < snapshot.observed_at
        ):
            raise ValueError(
                "client-ref lookup evidence is incomplete, stale, or mismatched"
            )
        if (
            result.confirmed_absent_client_refs
            and not self.broker.capabilities.order_coverage.negative_client_ref_results_authoritative
        ):
            raise ValueError(
                "client-ref lookup claimed absence without authoritative negative semantics"
            )
        if any(
            order.broker_updated_at > result.observed_at + timedelta(seconds=2)
            for order in result.found_orders
        ):
            raise ValueError("client-ref lookup contains future broker facts")
        return result

    @staticmethod
    def _merge_client_ref_lookup(
        snapshot: AccountSnapshot, result: ClientRefLookupResult
    ) -> AccountSnapshot:
        """Carry authoritative recovered orders into reconciliation.

        A complete point lookup may include a terminal order outside the
        account endpoint's normal history window.  Never discard that positive
        evidence; merge it by immutable broker/client identity while rejecting
        contradictions or evidence regression.
        """

        orders = list(snapshot.equity_orders)
        by_id = {order.broker_order_id: index for index, order in enumerate(orders)}
        by_ref = {
            order.client_ref_id: order.broker_order_id
            for order in orders
            if order.client_ref_id is not None
        }

        def identity(order: OrderSnapshot) -> tuple[object, ...]:
            return (
                order.account_masked,
                order.broker_order_id,
                order.client_ref_id,
                order.symbol,
                order.side,
                order.order_type,
                order.requested_quantity,
                order.market_hours,
                order.time_in_force,
                order.limit_price,
                order.stop_price,
            )

        def mutable_facts(order: OrderSnapshot) -> tuple[object, ...]:
            return (
                order.state,
                order.cumulative_filled_quantity,
                order.fills,
            )

        for recovered in result.found_orders:
            other_id = by_ref.get(recovered.client_ref_id)
            if other_id is not None and other_id != recovered.broker_order_id:
                raise ValueError(
                    "client-ref lookup conflicts with account snapshot order identity"
                )
            index = by_id.get(recovered.broker_order_id)
            if index is None:
                by_id[recovered.broker_order_id] = len(orders)
                if recovered.client_ref_id is not None:
                    by_ref[recovered.client_ref_id] = recovered.broker_order_id
                orders.append(recovered)
                continue
            current = orders[index]
            if identity(current) != identity(recovered):
                raise ValueError(
                    "client-ref lookup changed immutable account-snapshot order facts"
                )
            if recovered.broker_updated_at < current.broker_updated_at:
                continue
            if (
                recovered.broker_updated_at == current.broker_updated_at
                and mutable_facts(recovered) != mutable_facts(current)
            ):
                raise ValueError(
                    "client-ref lookup conflicts at the same broker revision"
                )
            if (
                recovered.cumulative_filled_quantity
                < current.cumulative_filled_quantity
            ):
                raise ValueError("client-ref lookup cumulative fill regressed")
            orders[index] = recovered

        latest_receipt = max(snapshot.received_at, result.received_at)
        return replace(
            snapshot,
            equity_orders=tuple(orders),
            received_at=latest_receipt,
        )

    def _notification_action(self) -> str:
        if isinstance(self.notifications, EnqueueOnlyOutbox):
            return "DEFER_OUTBOX_TO_INDEPENDENT_NOTIFICATION_WORKER"
        return "FLUSH_OUTBOX"

    def _notification_entry_blockers(self, now: datetime) -> tuple[str, ...]:
        """Return independent-delivery blockers without affecting exits."""

        local_failures = self.state.rows(
            "SELECT category FROM incidents WHERE account_key=? "
            "AND resolved_at IS NULL AND category IN "
            "('NOTIFICATION_ENQUEUE_FAILED','NOTIFICATION_DELIVERY_FAILED') "
            "ORDER BY category",
            (self.account_key,),
        )
        if local_failures:
            return tuple(str(row["category"]) for row in local_failures)

        if not isinstance(self.notifications, EnqueueOnlyOutbox):
            return ()
        if self.notification_route is None:
            return ("NOTIFICATION_WORKER_ROUTE_UNCONFIGURED",)
        if self.notification_route.provider == "local_jsonl":
            return ("NOTIFICATION_PROVIDER_DESTINATION_REQUIRED",)
        try:
            healthy, errors = notification_worker_health(
                self.state,
                account_key=self.account_key,
                route=self.notification_route,
                now=now,
            )
        except Exception as exc:
            return (f"NOTIFICATION_WORKER_PROBE_{type(exc).__name__.upper()}",)
        if healthy:
            return ()
        return tuple(
            "NOTIFICATION_WORKER_" + error.partition(":")[2]
            if error.startswith("notification_worker:")
            else "NOTIFICATION_WORKER_PROBE_FAILED"
            for error in errors
        )

    def _deliver_notifications(self, now: datetime) -> tuple[int, int]:
        if isinstance(self.notifications, EnqueueOnlyOutbox):
            return 0, 0
        try:
            sent, failed = self.notifications.drain(now)
        except Exception as exc:
            self._record_notification_failure(
                "NOTIFICATION_DELIVERY_FAILED", exc, now
            )
            return 0, 1
        if failed:
            self._record_notification_failure(
                "NOTIFICATION_DELIVERY_FAILED",
                RuntimeError("notification outbox delivery was not provider-confirmed"),
                now,
            )
        return sent, failed

    @staticmethod
    def _priority_poll_delay(
        *,
        actions: Sequence[str],
        blockers: Sequence[str],
        protection: Sequence[ProtectionDecision],
        closeout: Sequence[SafeCloseDecision],
    ) -> float | None:
        """Choose a fail-closed broker refresh cadence after this tick.

        A broker-side acknowledgement, rejection, or ambiguous submission can
        immediately change fills, capacity, and protection.  The next action
        therefore starts with a new account snapshot without the ordinary
        heartbeat sleep.  Persistent uncertainty is polled at one second so a
        broker outage cannot create a CPU spin.
        """

        consequence_prefixes = (
            "ENTRY:",
            "PROTECT:",
            "CLOSEOUT:",
            "RECONCILE_LIFECYCLE:",
        )
        consequence_states = (
            ":ACKNOWLEDGED",
            ":UNKNOWN",
            ":REJECTED",
            ":FAILED",
        )
        if any(
            action.startswith(consequence_prefixes)
            and any(state in action for state in consequence_states)
            for action in actions
        ):
            return 0.0
        urgent_blocker_fragments = (
            "UNRESOLVED_",
            "ACKNOWLEDGED_",
            "UNKNOWN_",
            "UNPROTECTED_EXPOSURE_PRESENT",
            "CLOSEOUT_NOT_FLAT",
            "LIFECYCLE_",
            "CANCEL_PENDING_RECONCILIATION",
        )
        if (
            any(
                any(fragment in blocker for fragment in urgent_blocker_fragments)
                for blocker in blockers
            )
            or any(not item.protected for item in protection)
            or any(item.action.value != "FLAT" for item in closeout)
        ):
            return 1.0
        return None

    def _ensure_fill_obligations(self) -> None:
        rows = self.state.rows(
            "SELECT DISTINCT i.intent_id FROM order_intents i "
            "JOIN broker_orders o ON o.intent_id=i.intent_id "
            "JOIN fills f ON f.broker_order_id=o.broker_order_id "
            "WHERE i.account_key=? AND i.kind=? ORDER BY i.intent_id",
            (self.account_key, IntentKind.ENTRY.value),
        )
        for row in rows:
            ensure_durable_entry_fill_obligations(
                self.state,
                intent_id=str(row["intent_id"]),
                account_key=self.account_key,
            )

    def _assess_protection(self, snapshot: AccountSnapshot) -> tuple[ProtectionDecision, ...]:
        positions = {position.symbol: position for position in snapshot.equity_positions}
        symbols = set(positions)
        symbols.update(
            row["symbol"]
            for row in self.state.rows(
                "SELECT DISTINCT symbol FROM protection_obligations WHERE account_key=?",
                (self.account_key,),
            )
        )
        return tuple(
            assess_protection(
                position=positions.get(symbol),
                orders=snapshot.equity_orders,
                obligations=load_open_obligations(
                    self.state, account_key=self.account_key, symbol=symbol
                ),
                symbol=symbol,
            )
            for symbol in sorted(symbols)
        )

    def _plan_closeout(self, snapshot: AccountSnapshot) -> tuple[SafeCloseDecision, ...]:
        symbols = {position.symbol for position in snapshot.equity_positions}
        symbols.update(
            order.symbol for order in snapshot.equity_orders if not order.state.terminal
        )
        positions = {position.symbol: position for position in snapshot.equity_positions}
        return tuple(
            plan_safe_close(
                position=positions.get(symbol),
                orders=snapshot.equity_orders,
                symbol=symbol,
                snapshot_received_at=snapshot.observed_at,
            )
            for symbol in sorted(symbols)
        )

    @staticmethod
    def _exposure_symbols(snapshot: AccountSnapshot) -> tuple[str, ...]:
        symbols = {
            position.symbol
            for position in snapshot.equity_positions
            if position.quantity != 0
        }
        symbols.update(
            order.symbol
            for order in snapshot.equity_orders
            if not order.state.terminal
        )
        return tuple(sorted(symbols))

    def _safety_session_open(self, now: datetime) -> bool:
        local = now.astimezone(NEW_YORK)
        try:
            session = self.policy.calendar.session_times(local.date())
        except (TypeError, ValueError):
            return False
        return bool(
            session is not None
            and session.open_at <= local < session.close_at
        )

    def _session_at_or_before(self, observed_at: datetime) -> SessionTimes | None:
        """Return the latest evidence-backed exchange session for a timestamp."""

        local = observed_at.astimezone(NEW_YORK)
        candidate = local.date()
        calendar_year = int(self.policy.calendar.year)
        while candidate.year == calendar_year:
            try:
                session = self.policy.calendar.session_times(candidate)
            except (TypeError, ValueError):
                return None
            # A pre-open observation belongs to the exposure carried out of the
            # preceding completed session, not to the still-future session on
            # the same civil date.
            if session is not None and session.open_at <= local:
                return session
            candidate -= timedelta(days=1)
        return None

    def _session_before(self, trading_date: date) -> SessionTimes | None:
        """Return the preceding verified exchange session in this calendar."""

        candidate = trading_date - timedelta(days=1)
        calendar_year = int(self.policy.calendar.year)
        while candidate.year == calendar_year:
            try:
                session = self.policy.calendar.session_times(candidate)
            except (TypeError, ValueError):
                return None
            if session is not None:
                return session
            candidate -= timedelta(days=1)
        return None

    @staticmethod
    def _durable_origin_time(value: object) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)

    def _current_session_origin_proven(
        self,
        *,
        snapshot: AccountSnapshot,
        session: SessionTimes,
    ) -> bool:
        """Prove every current exposure from exact durable same-session facts.

        A position is current-session-owned only when the signed inventory from
        every durable fill exactly equals the broker position and the net
        inventory contributed outside this session is zero.  A working order
        additionally needs its exact broker-order/client-reference/tuple join
        and both its intent creation and broker observation inside this session.
        Any absent, malformed, external, or temporally ambiguous fact fails
        closed so a first-seen carried position cannot become ordinary managed
        exposure merely by being observed after a restart.
        """

        observed_at = snapshot.observed_at.astimezone(timezone.utc)
        received_at = snapshot.received_at.astimezone(timezone.utc)
        open_at = session.open_at.astimezone(timezone.utc)
        close_at = session.close_at.astimezone(timezone.utc)
        if observed_at < open_at or received_at < observed_at:
            return False

        current_positions: dict[str, Decimal] = {}
        for position in snapshot.equity_positions:
            if position.quantity == 0:
                continue
            if (
                position.quantity <= 0
                or position.quantity != position.quantity.to_integral_value()
                or position.symbol in current_positions
            ):
                return False
            current_positions[position.symbol] = position.quantity

        try:
            fill_rows = self.state.rows(
                """SELECT f.fill_id,f.quantity,f.executed_at,f.received_at,
                          i.kind,i.client_ref,i.created_at,
                          i.order_tuple_json,i.tuple_hash
                     FROM fills f
                     JOIN broker_orders o ON o.broker_order_id=f.broker_order_id
                     JOIN order_intents i ON i.intent_id=o.intent_id
                    WHERE f.account_key=?
                    ORDER BY f.executed_at,f.fill_id""",
                (self.account_key,),
            )
        except Exception:
            return False

        durable_inventory: dict[str, Decimal] = {}
        outside_session_inventory: dict[str, Decimal] = {}
        for row in fill_rows:
            try:
                order_tuple = json.loads(str(row["order_tuple_json"]))
                if (
                    not isinstance(order_tuple, Mapping)
                    or str(row["tuple_hash"]) != object_hash(order_tuple)
                    or order_tuple.get("account_key") != self.account_key
                    or order_tuple.get("account_masked") != snapshot.account_masked
                ):
                    return False
                symbol = str(order_tuple["symbol"]).strip().upper()
                side = str(order_tuple["side"]).strip().lower()
                kind = str(row["kind"])
                quantity = Decimal(int(row["quantity"]))
                intent_created_at = self._durable_origin_time(row["created_at"])
                executed_at = self._durable_origin_time(row["executed_at"])
                fill_received_at = self._durable_origin_time(row["received_at"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                return False
            if (
                not symbol
                or side not in {"buy", "sell"}
                or (side == "buy" and kind != IntentKind.ENTRY.value)
                or (
                    side == "sell"
                    and kind not in {IntentKind.PROTECTION.value, IntentKind.EXIT.value}
                )
                or order_tuple.get("client_ref_id") != str(row["client_ref"])
                or intent_created_at is None
                or executed_at is None
                or fill_received_at is None
                or fill_received_at < executed_at
                or fill_received_at > received_at
                or executed_at > received_at
                or (
                    open_at <= executed_at < close_at
                    and not open_at <= intent_created_at < close_at
                )
            ):
                return False
            signed = quantity if side == "buy" else -quantity
            durable_inventory[symbol] = durable_inventory.get(
                symbol, Decimal("0")
            ) + signed
            if not open_at <= executed_at < close_at:
                outside_session_inventory[symbol] = outside_session_inventory.get(
                    symbol, Decimal("0")
                ) + signed

        durable_positions = {
            symbol: quantity
            for symbol, quantity in durable_inventory.items()
            if quantity != 0
        }
        if durable_positions != current_positions or any(
            outside_session_inventory.get(symbol, Decimal("0")) != 0
            for symbol in current_positions
        ):
            return False

        working_orders = tuple(
            order for order in snapshot.equity_orders if not order.state.terminal
        )
        try:
            order_rows = self.state.rows(
                """SELECT o.broker_order_id,o.state,o.quantity,
                          o.cumulative_filled_quantity,i.kind,i.client_ref,i.created_at,
                          i.order_tuple_json,i.tuple_hash
                     FROM broker_orders o
                     JOIN order_intents i ON i.intent_id=o.intent_id
                    WHERE o.account_key=?""",
                (self.account_key,),
            )
        except Exception:
            return False
        orders_by_id = {str(row["broker_order_id"]): row for row in order_rows}
        for order in working_orders:
            row = orders_by_id.get(order.broker_order_id)
            if row is None or order.client_ref_id is None:
                return False
            try:
                order_tuple = json.loads(str(row["order_tuple_json"]))
                intent_created_at = self._durable_origin_time(row["created_at"])
            except (TypeError, ValueError, json.JSONDecodeError):
                return False
            expected = {
                "account_masked": order.account_masked,
                "symbol": order.symbol,
                "side": order.side.value,
                "order_type": order.order_type.value,
                "quantity": int(order.requested_quantity),
                "market_hours": order.market_hours.value,
                "time_in_force": order.time_in_force.value,
                "limit_price": (
                    format(order.limit_price, "f")
                    if order.limit_price is not None
                    else None
                ),
                "stop_price": (
                    format(order.stop_price, "f")
                    if order.stop_price is not None
                    else None
                ),
                "client_ref_id": order.client_ref_id,
            }
            if (
                not isinstance(order_tuple, Mapping)
                or str(row["tuple_hash"]) != object_hash(order_tuple)
                or order_tuple.get("account_key") != self.account_key
                or any(
                    order_tuple.get(field) != value
                    for field, value in expected.items()
                )
                or str(row["client_ref"]) != order.client_ref_id
                or (
                    order.side.value == "buy"
                    and str(row["kind"]) != IntentKind.ENTRY.value
                )
                or (
                    order.side.value == "sell"
                    and str(row["kind"])
                    not in {IntentKind.PROTECTION.value, IntentKind.EXIT.value}
                )
                or str(row["state"]) != order.state.value
                or int(row["quantity"]) != int(order.requested_quantity)
                or int(row["cumulative_filled_quantity"])
                != int(order.cumulative_filled_quantity)
                or intent_created_at is None
                or not open_at <= intent_created_at < close_at
                or not open_at
                <= order.broker_updated_at.astimezone(timezone.utc)
                < close_at
            ):
                return False
        return True

    def _missed_session_exposure(
        self,
        *,
        snapshot: AccountSnapshot,
        durable_snapshot: BrokerSnapshot,
        now: datetime,
    ) -> _MissedSessionExposure | None:
        """Detect exposure that lacks later durable flat proof after its session.

        The current broker envelope alone cannot distinguish a same-session
        position from one carried across a process outage.  Append-only prior
        broker snapshots can: the latest earlier exposure remains causal until
        a later, fully reconciled account-wide flat snapshot supersedes it.  If
        that exposure's evidence-backed flat deadline has passed, the position
        or working order belongs to closeout, not ordinary management.
        """

        if not self._exposure_symbols(snapshot):
            return None

        prior_rows = self.state.rows(
            """SELECT b.snapshot_id,b.observed_at,b.received_at
                 FROM broker_snapshots b
                WHERE b.account_key=? AND b.snapshot_id<>?
                  AND b.observed_at<?
                  AND b.positions_reconciled=1
                  AND b.equity_orders_reconciled=1
                  AND (b.equity_position_count<>0
                       OR b.equity_nonterminal_order_count<>0)
                  AND EXISTS (
                      SELECT 1 FROM audit_events e
                       WHERE e.stream=b.account_key
                         AND e.event_type='POSITIONS_RECONCILED'
                         AND e.entity_type='broker_snapshot'
                         AND e.entity_id=b.snapshot_id
                  )
                ORDER BY b.observed_at DESC,b.received_at DESC,b.snapshot_id DESC
                LIMIT 1""",
            (
                self.account_key,
                durable_snapshot.snapshot_id,
                durable_snapshot.observed_at.isoformat(),
            ),
        )
        if prior_rows:
            prior = prior_rows[0]

            # Only complete account-wide flatness can break the causal exposure
            # chain.  A partial/blocked empty response must never turn yesterday's
            # known position into today's ordinary managed position.
            later_flat = self.state.rows(
                """SELECT b.snapshot_id
                     FROM broker_snapshots b
                    WHERE b.account_key=?
                      AND b.observed_at>? AND b.observed_at<?
                      AND b.positions_reconciled=1
                      AND b.equity_orders_reconciled=1
                      AND b.option_positions_reconciled=1
                      AND b.option_orders_reconciled=1
                      AND b.advanced_orders_reconciled=1
                      AND b.realized_pnl_reconciled=1
                      AND b.equity_position_count=0
                      AND b.equity_nonterminal_order_count=0
                      AND b.external_material_order_count=0
                      AND b.option_position_count=0
                      AND b.option_order_count=0
                      AND b.advanced_order_count=0
                      AND b.reconciliation_blocker_count=0
                      AND EXISTS (
                          SELECT 1 FROM audit_events e
                           WHERE e.stream=b.account_key
                             AND e.event_type='POSITIONS_RECONCILED'
                             AND e.entity_type='broker_snapshot'
                             AND e.entity_id=b.snapshot_id
                      )
                    ORDER BY b.observed_at DESC,b.received_at DESC,b.snapshot_id DESC
                    LIMIT 1""",
                (
                    self.account_key,
                    str(prior["observed_at"]),
                    durable_snapshot.observed_at.isoformat(),
                ),
            )
            if not later_flat:
                prior_observed_at = self._durable_origin_time(prior["observed_at"])
                prior_session = (
                    self._session_at_or_before(prior_observed_at)
                    if prior_observed_at is not None
                    else None
                )
                if (
                    prior_session is not None
                    and now >= prior_session.flat_deadline_at
                ):
                    return _MissedSessionExposure(
                        prior_snapshot_id=str(prior["snapshot_id"]),
                        prior_observed_at=prior_observed_at,
                        prior_trading_date=prior_session.trading_date,
                        required_flat_at=prior_session.flat_deadline_at,
                    )

        # A current snapshot is not evidence that its exposure originated in
        # this session.  When no unresolved prior exposure row exists (for
        # example, a crash immediately after a fill), require the exact durable
        # current-session fill/order proof above.  Otherwise conservatively bind
        # the first-seen exposure to the preceding completed exchange session.
        local = now.astimezone(NEW_YORK)
        try:
            current_session = self.policy.calendar.session_times(local.date())
        except (TypeError, ValueError):
            current_session = None
        if current_session is None:
            prior_session = self._session_at_or_before(now)
            history_cutoff = durable_snapshot.observed_at
        else:
            if self._current_session_origin_proven(
                snapshot=snapshot,
                session=current_session,
            ):
                return None
            prior_session = self._session_before(current_session.trading_date)
            history_cutoff = current_session.open_at
        if prior_session is None:
            return None
        # "First seen" is a restart boundary only when durable broker history
        # predates today's session.  A newly initialized same-session test or
        # runtime still fails ordinary ownership/protection gates, but is not
        # mislabeled as exposure carried from yesterday.
        prior_history = self.state.rows(
            """SELECT b.snapshot_id
                 FROM broker_snapshots b
                WHERE b.account_key=? AND b.snapshot_id<>?
                  AND b.observed_at<?
                  AND b.positions_reconciled=1
                  AND b.equity_orders_reconciled=1
                  AND EXISTS (
                      SELECT 1 FROM audit_events e
                       WHERE e.stream=b.account_key
                         AND e.event_type='POSITIONS_RECONCILED'
                         AND e.entity_type='broker_snapshot'
                         AND e.entity_id=b.snapshot_id
                  )
                ORDER BY b.observed_at DESC,b.received_at DESC,b.snapshot_id DESC
                LIMIT 1""",
            (
                self.account_key,
                durable_snapshot.snapshot_id,
                history_cutoff.astimezone(timezone.utc).isoformat(),
            ),
        )
        if not prior_history:
            return None
        if now < prior_session.flat_deadline_at:
            return None
        return _MissedSessionExposure(
            prior_snapshot_id=durable_snapshot.snapshot_id,
            prior_observed_at=snapshot.observed_at,
            prior_trading_date=prior_session.trading_date,
            required_flat_at=prior_session.flat_deadline_at,
        )

    def _record_missed_session_exposure(
        self,
        *,
        evidence: _MissedSessionExposure,
        snapshot: AccountSnapshot,
        durable_snapshot: BrokerSnapshot,
        now: datetime,
    ) -> None:
        category = "MISSED_SESSION_CLOSEOUT_EXPOSURE"
        existing = self.state.rows(
            "SELECT incident_id FROM incidents WHERE account_key=? "
            "AND category=? AND resolved_at IS NULL",
            (self.account_key, category),
        )
        symbols = self._exposure_symbols(snapshot)
        if not existing:
            fingerprint = hashlib.sha256(
                (
                    f"{self.account_key}\n{category}\n"
                    f"{evidence.prior_snapshot_id}\n"
                    f"{durable_snapshot.snapshot_id}"
                ).encode("utf-8")
            ).hexdigest()
            self.state.record_incident(
                Incident(
                    incident_id=f"incident-{fingerprint}",
                    account_key=self.account_key,
                    category=category,
                    severity=IncidentSeverity.CRITICAL,
                    opened_at=now,
                    detail={
                        "prior_snapshot_id": evidence.prior_snapshot_id,
                        "prior_observed_at": evidence.prior_observed_at,
                        "prior_trading_date": evidence.prior_trading_date,
                        "required_flat_at": evidence.required_flat_at,
                        "current_snapshot_id": durable_snapshot.snapshot_id,
                        "symbols": list(symbols),
                    },
                )
            )
        self._notify(
            "CLOSEOUT_INCIDENT",
            {
                "event_id": (
                    f"{evidence.prior_trading_date.isoformat()}:"
                    "missed-session-closeout:"
                    + hashlib.sha256(
                        ",".join(symbols).encode("utf-8")
                    ).hexdigest()[:16]
                ),
                "state": "unresolved",
                "symbol": ",".join(symbols),
                "reason": (
                    "broker exposure survived a prior session flat deadline"
                ),
            },
            now,
        )

    def _closeout_feasibility(
        self,
        *,
        snapshot: AccountSnapshot,
        now: datetime,
        closeout_owns_exposure: bool = False,
    ) -> CloseoutFeasibility:
        """Budget conservative convergence cycles against the flat deadline.

        The estimate is deliberately monotone in outstanding positions,
        broker orders, unresolved local intents, and observed latency.  It is
        an escalation trigger—not a representation that an order will fill
        within the budget.  Each broker mutation and each conclusive follow-up
        read gets its own cycle; no cycle is shared between symbols.
        """

        position_facts = tuple(
            f"position:{item.symbol}:{item.quantity}"
            for item in snapshot.equity_positions
            if item.quantity != 0
        )
        order_facts = tuple(
            f"order:{item.broker_order_id}:{item.symbol}:{item.state.value}"
            for item in snapshot.equity_orders
            if not item.state.terminal
        )
        active_snapshot_order_ids = {
            item.broker_order_id
            for item in snapshot.equity_orders
            if not item.state.terminal
        }
        intent_rows = self.state.rows(
            "SELECT i.intent_id,i.kind,i.state,i.order_tuple_json,"
            "o.broker_order_id,o.state AS broker_state "
            "FROM order_intents i LEFT JOIN broker_orders o "
            "ON o.intent_id=i.intent_id WHERE i.account_key=? "
            "AND i.state IN ('PREPARED','SUBMITTING','UNKNOWN','ACKNOWLEDGED') "
            "ORDER BY i.intent_id,o.broker_order_id",
            (self.account_key,),
        )
        intent_groups: dict[str, dict[str, Any]] = {}
        for row in intent_rows:
            intent_id = str(row["intent_id"])
            group = intent_groups.setdefault(
                intent_id,
                {
                    "kind": str(row["kind"]),
                    "state": str(row["state"]),
                    "order_tuple_json": str(row["order_tuple_json"]),
                    "broker_orders": [],
                },
            )
            if row["broker_order_id"] is not None:
                group["broker_orders"].append(
                    (str(row["broker_order_id"]), str(row["broker_state"]))
                )

        unresolved_facts_list: list[str] = []
        pending_protection_keys: set[str] = set()
        for intent_id in sorted(intent_groups):
            group = intent_groups[intent_id]
            kind = str(group["kind"])
            intent_state = str(group["state"])
            broker_orders = tuple(group["broker_orders"])
            broker_states = tuple(
                BrokerOrderState(state) for _broker_order_id, state in broker_orders
            )
            terminal_broker_backing = bool(broker_states) and all(
                state.terminal for state in broker_states
            )
            active_order_already_counted = any(
                broker_order_id in active_snapshot_order_ids
                and not BrokerOrderState(state).terminal
                for broker_order_id, state in broker_orders
            )

            operation_key: str | None = None
            if kind == IntentKind.PROTECTION.value:
                try:
                    payload = json.loads(str(group["order_tuple_json"]))
                    candidate = payload.get("operation_key")
                    if isinstance(candidate, str) and candidate:
                        operation_key = candidate
                except (TypeError, ValueError, json.JSONDecodeError):
                    operation_key = None
                if operation_key is not None and not terminal_broker_backing:
                    pending_protection_keys.add(operation_key)

            # A PREPARED entry has never crossed the send boundary and creates
            # no broker closeout work.  An ACK entry backed only by terminal
            # broker orders is complete even if its local intent remains ACK.
            if kind == IntentKind.ENTRY.value and intent_state == "PREPARED":
                continue
            if terminal_broker_backing:
                continue
            # An active broker order is already one explicit order fact.  Do
            # not inflate the cycle budget by counting its local envelope too.
            if active_order_already_counted:
                continue
            unresolved_facts_list.append(
                f"intent:{intent_id}:{kind}:{intent_state}"
            )

        unresolved_facts = tuple(unresolved_facts_list)
        uncreated_protection_facts: tuple[str, ...] = ()
        if not closeout_owns_exposure:
            position_symbols = {
                item.symbol
                for item in snapshot.equity_positions
                if item.quantity != 0
            }
            obligation_rows = self.state.rows(
                "SELECT obligation_id,symbol,required_quantity FROM "
                "protection_obligations WHERE account_key=? AND state='REQUIRED' "
                "AND broker_order_id IS NULL ORDER BY obligation_id",
                (self.account_key,),
            )
            uncreated_protection_facts = tuple(
                "uncreated_protection:"
                f"{row['obligation_id']}:{row['symbol']}:{row['required_quantity']}"
                for row in obligation_rows
                if str(row["symbol"]) in position_symbols
                and str(row["obligation_id"]) not in pending_protection_keys
            )

        facts = (
            position_facts
            + order_facts
            + unresolved_facts
            + uncreated_protection_facts
        )
        obligation_count = len(facts)
        if obligation_count == 0:
            return CloseoutFeasibility(
                obligation_count=0,
                required_cycles=0,
                cycle_budget_seconds=self._closeout_cycle_budget_seconds(),
                estimated_seconds=0.0,
                seconds_to_flat_deadline=self._seconds_to_flat_deadline(now),
                start_early=False,
                margin_exhausted=False,
                obligation_facts=(),
            )

        # Every obligation gets a mutation/recovery cycle and a separate
        # broker-proof cycle, followed by one final account-wide flat read.
        required_cycles = 1 + (2 * obligation_count)
        cycle_budget = self._closeout_cycle_budget_seconds()
        estimated = required_cycles * cycle_budget
        remaining = self._seconds_to_flat_deadline(now)
        unavailable = remaining is None
        margin_exhausted = unavailable or remaining <= estimated
        start_early = bool(
            margin_exhausted
            or (
                remaining is not None
                and remaining > 0
                and remaining <= estimated + 60.0
            )
        )
        return CloseoutFeasibility(
            obligation_count=obligation_count,
            required_cycles=required_cycles,
            cycle_budget_seconds=cycle_budget,
            estimated_seconds=estimated,
            seconds_to_flat_deadline=remaining,
            start_early=start_early,
            margin_exhausted=margin_exhausted,
            obligation_facts=facts,
        )

    def _closeout_cycle_budget_seconds(self) -> float:
        execution = self.policy.config["execution"]
        floor = max(
            30.0,
            float(execution.get("order_ack_timeout_seconds", 0))
            + 2
            * max(
                float(execution.get("reconcile_interval_seconds", 0)),
                float(execution.get("unknown_reconcile_interval_seconds", 0)),
            ),
        )
        rows = self.state.rows(
            "SELECT stage,duration_microseconds FROM latency_samples "
            "WHERE account_key=? AND stage IN "
            "('durable_intent_write','submit_to_ack','ack_to_fill') "
            "ORDER BY observed_at DESC LIMIT 600",
            (self.account_key,),
        )
        maxima: dict[str, int] = {}
        for row in rows:
            stage = str(row["stage"])
            duration = int(row["duration_microseconds"])
            maxima[stage] = max(maxima.get(stage, 0), duration)
        observed = sum(maxima.values()) / 1_000_000
        poll_reserve = 2 * max(
            float(execution.get("reconcile_interval_seconds", 0)),
            float(execution.get("unknown_reconcile_interval_seconds", 0)),
        )
        return max(floor, observed + poll_reserve)

    def _seconds_to_flat_deadline(self, now: datetime) -> float | None:
        try:
            local = now.astimezone(NEW_YORK)
            session = self.policy.calendar.session_times(local.date())
        except (TypeError, ValueError):
            return None
        if session is None:
            return None
        return (session.flat_deadline_at - local).total_seconds()

    def _record_closeout_feasibility_incident(
        self, feasibility: CloseoutFeasibility, now: datetime
    ) -> None:
        category = "CLOSEOUT_DEADLINE_FEASIBILITY_MARGIN_EXHAUSTED"
        rows = self.state.rows(
            "SELECT incident_id FROM incidents WHERE account_key=? AND category=? "
            "AND resolved_at IS NULL",
            (self.account_key, category),
        )
        if not rows:
            day = now.astimezone(NEW_YORK).date().isoformat()
            digest = hashlib.sha256(
                f"{self.account_key}\n{day}\n{category}".encode("utf-8")
            ).hexdigest()
            self.state.record_incident(
                Incident(
                    incident_id=f"incident-{digest}",
                    account_key=self.account_key,
                    category=category,
                    severity=IncidentSeverity.CRITICAL,
                    opened_at=now,
                    detail={
                        "assessment": "conservative_feasibility_not_fill_guarantee",
                        "obligation_count": feasibility.obligation_count,
                        "required_cycles": feasibility.required_cycles,
                        "cycle_budget_seconds": feasibility.cycle_budget_seconds,
                        "estimated_seconds": feasibility.estimated_seconds,
                        "seconds_to_flat_deadline": (
                            feasibility.seconds_to_flat_deadline
                        ),
                        "obligation_facts": list(feasibility.obligation_facts),
                    },
                )
            )
        self._notify(
            "CLOSEOUT_INCIDENT",
            {
                "event_id": f"{now.astimezone(NEW_YORK).date()}:{category}",
                "state": "latency_margin_exhausted",
                "symbol": "ACCOUNT",
                "reason": (
                    "conservative closeout feasibility margin is exhausted; "
                    "this is not a fill guarantee"
                ),
            },
            now,
        )

    def _resolve_closeout_feasibility_incident(self, now: datetime) -> bool:
        rows = self.state.rows(
            "SELECT incident_id FROM incidents WHERE account_key=? AND category=? "
            "AND resolved_at IS NULL ORDER BY opened_at,incident_id",
            (self.account_key, "CLOSEOUT_DEADLINE_FEASIBILITY_MARGIN_EXHAUSTED"),
        )
        changed = False
        for row in rows:
            changed = (
                self.state.resolve_incident(
                    str(row["incident_id"]), resolved_at=now
                )
                or changed
            )
        return changed

    def _update_risk_latch(
        self, snapshot: AccountSnapshot, now: datetime
    ) -> RiskSessionLatch | None:
        if not snapshot.daily_realized_pnl_ready and not self.policy.daily_starting_equity_risk:
            return None
        local_date = now.astimezone(NEW_YORK).date()
        if not self.policy.calendar.is_trading_day(local_date):
            return None
        rows = self.state.rows(
            "SELECT * FROM session_latches WHERE account_key=? AND trading_date=?",
            (self.account_key, local_date.isoformat()),
        )
        if rows:
            row = rows[0]
            prior = RiskSessionLatch(
                trading_date=local_date,
                loss_lock=bool(row["loss_locked"]),
                hard_kill=bool(row["hard_kill"]),
                profit_goal_crossed=bool(row["objective_crossed"]),
                first_profit_crossed_at=(
                    datetime.fromisoformat(row["first_objective_crossed_at"])
                    if row["first_objective_crossed_at"] is not None
                    else None
                ),
                highest_realized_pnl=Decimal(row["highest_realized_pnl_cents"]) / 100,
            )
            revision = int(row["revision"]) + 1
        else:
            prior = RiskSessionLatch(trading_date=local_date)
            revision = 0
        if self.policy.daily_starting_equity_risk and (
            not snapshot.daily_starting_equity_ready
            or not snapshot.entry_risk_evidence_ready
            or snapshot.daily_starting_equity_as_of != datetime.combine(
                local_date, datetime.min.time(), NEW_YORK
            )
            or snapshot.observed_at.astimezone(NEW_YORK).date() != local_date
            or not 0 <= (now - snapshot.observed_at).total_seconds() <= int(
                self.policy.config["evidence"]["broker_snapshot_max_age_seconds"]
            )
        ):
            # An incomplete new observation cannot clear a prior durable
            # breach or silently restore entry eligibility.
            return prior if rows else None
        updated = update_session_latch(
            self.policy,
            prior,
            realized_pnl=snapshot.daily_realized_pnl,
            usable_equity=snapshot.funds.total_value,
            observed_at=now.astimezone(NEW_YORK),
            daily_starting_equity=snapshot.daily_starting_equity,
            daily_external_cash_flow=snapshot.daily_external_cash_flow,
            total_equity=snapshot.funds.total_value,
        )
        durable = DurableSessionLatch(
            account_key=self.account_key,
            trading_date=local_date,
            loss_locked=updated.loss_lock,
            objective_crossed=updated.profit_goal_crossed,
            pause_new_entries=bool(rows and row["pause_new_entries"]) or updated.loss_lock or updated.hard_kill,
            closeout_started=bool(rows and row["closeout_started"]) or updated.hard_kill,
            hard_kill=updated.hard_kill,
            highest_realized_pnl=updated.highest_realized_pnl,
            first_objective_crossed_at=updated.first_profit_crossed_at,
            revision=revision,
            updated_at=now,
        )
        if not self.state.apply_session_latch(durable):
            raise RuntimeError("RISK_LATCH_PERSISTENCE_NOT_CONFIRMED")
        return updated

    def _notify_unprotected(
        self,
        decisions: Sequence[ProtectionDecision],
        snapshot_id: str,
        now: datetime,
    ) -> None:
        for decision in decisions:
            if decision.protected:
                continue
            condition = hashlib.sha256(
                (
                    f"{decision.symbol}|{decision.uncovered_quantity}|"
                    + "|".join(sorted(decision.reasons))
                ).encode("utf-8")
            ).hexdigest()[:20]
            self._notify(
                "UNPROTECTED_EXPOSURE",
                {
                    # Stable while the same condition persists; a changing
                    # broker snapshot alone never creates alert spam.
                    "event_id": f"{decision.symbol}:unprotected:{condition}",
                    "state": "unprotected",
                    "symbol": decision.symbol,
                    "quantity": decision.uncovered_quantity,
                    "protection_state": "missing_or_unverified",
                    "reason": "; ".join(decision.reasons),
                },
                now,
            )

    def _notify_reconciled_fills(
        self, report: ReconciliationReport, now: datetime
    ) -> None:
        fill_ids = tuple(
            dict.fromkeys(
                fill_id
                for item in (*report.ingested_orders, *report.unknown_resolutions)
                for fill_id in item.new_fill_ids
            )
        )
        for fill_id in fill_ids:
            rows = self.state.rows(
                """SELECT f.fill_id,f.quantity,f.price,i.kind,i.intent_id,p.symbol
                     FROM fills f JOIN broker_orders o
                       ON o.broker_order_id=f.broker_order_id
                     JOIN order_intents i ON i.intent_id=o.intent_id
                     JOIN plans p ON p.plan_id=i.plan_id
                    WHERE f.fill_id=? AND f.account_key=?""",
                (fill_id, self.account_key),
            )
            if len(rows) != 1:
                self._lifecycle_failure(
                    "FILL_NOTIFICATION",
                    RuntimeError("confirmed fill has no unique durable ownership"),
                    now,
                )
                continue
            row = rows[0]
            kind = IntentKind(str(row["kind"]))
            if kind is IntentKind.ENTRY:
                obligations = self.state.rows(
                    "SELECT state,working_quantity,required_quantity "
                    "FROM protection_obligations WHERE source_fill_id=?",
                    (fill_id,),
                )
                protection_state = (
                    str(obligations[0]["state"]).lower()
                    if len(obligations) == 1
                    else "missing"
                )
                event = "ENTRY_FILLED"
            elif kind in {IntentKind.EXIT, IntentKind.PROTECTION}:
                protection_state = "not_applicable"
                event = "EXIT_FILLED"
            else:
                continue
            self._notify(
                event,
                {
                    "event_id": fill_id,
                    "intent_id": str(row["intent_id"]),
                    "state": "broker_confirmed_fill",
                    "symbol": str(row["symbol"]),
                    "quantity": int(row["quantity"]),
                    "price": str(row["price"]),
                    "protection_state": protection_state,
                },
                now,
            )

    def _notify_working_protection(self, now: datetime) -> None:
        rows = self.state.rows(
            """SELECT obligation_id,symbol,required_quantity,working_quantity,
                      broker_order_id,revision
                 FROM protection_obligations
                WHERE account_key=? AND state='WORKING'
                ORDER BY obligation_id""",
            (self.account_key,),
        )
        for row in rows:
            event_id = (
                f"{row['obligation_id']}:{row['broker_order_id']}:"
                f"{row['working_quantity']}"
            )
            self._notify(
                "PROTECTION_WORKING",
                {
                    "event_id": event_id,
                    "state": "broker_confirmed_working",
                    "symbol": str(row["symbol"]),
                    "quantity": int(row["working_quantity"]),
                    "protection_state": "working",
                },
                now,
            )

    def _notify_end_of_day_if_flat(
        self, snapshot: BrokerSnapshot, now: datetime
    ) -> None:
        if not snapshot.fully_reconciled:
            return
        scope_counts = (
            snapshot.equity_position_count,
            snapshot.equity_nonterminal_order_count,
            snapshot.external_material_order_count,
            snapshot.option_position_count,
            snapshot.option_order_count,
            snapshot.advanced_order_count,
            snapshot.reconciliation_blocker_count,
        )
        if any(scope_counts):
            return
        local_counts = (
            self.state.rows(
                "SELECT COUNT(*) AS n FROM positions "
                "WHERE account_key=? AND CAST(quantity AS REAL)<>0",
                (self.account_key,),
            )[0]["n"],
            self.state.rows(
                "SELECT COUNT(*) AS n FROM order_intents "
                "WHERE account_key=? AND state IN ('SUBMITTING','UNKNOWN')",
                (self.account_key,),
            )[0]["n"],
        )
        if any(int(value) for value in local_counts):
            return
        trading_date = now.astimezone(NEW_YORK).date().isoformat()
        self._notify(
            "END_OF_DAY",
            {
                "event_id": f"{trading_date}:flat",
                "session_date": trading_date,
                "state": "broker_confirmed_flat",
                "symbol": "ACCOUNT",
                "reason": "complete account-wide reconciliation proves flatness",
            },
            now,
        )

    def _notify(self, event: str, payload: Mapping[str, Any], now: datetime) -> bool:
        """Durably enqueue without ever interrupting an exposure action.

        Notification health is an entry interlock, not an exit interlock.  A
        provider/outbox fault is made durable and pauses new entries, while
        the caller continues through reconciliation, protection, and closeout.
        """

        try:
            self.notifications.enqueue(event, payload, now)
            return True
        except Exception as exc:
            self._record_notification_failure(
                "NOTIFICATION_ENQUEUE_FAILED", exc, now
            )
            return False

    def _record_notification_failure(
        self, category: str, exc: Exception, now: datetime
    ) -> None:
        # This path must itself be best-effort: a damaged outbox may coincide
        # with a state-store error, and neither may suppress a required exit.
        try:
            self._open_incident(category, exc, now)
        except Exception:
            pass
        try:
            runtime = self.state.runtime_status()
            if runtime is None:
                return
            mode = EngineMode(str(runtime["mode"]))
            if mode in {EngineMode.ACTIVE, EngineMode.RECONCILING}:
                self.state.set_runtime_mode(
                    EngineMode.PAUSE_NEW_ENTRIES.value,
                    occurred_at=now,
                    reason=category,
                )
        except Exception:
            pass

    def _promote_lifecycle_outcome(
        self,
        *,
        stage: str,
        symbol: str,
        outcome: str,
        blockers: list[str],
        now: datetime,
    ) -> None:
        """Promote non-exception safety failures into durable health state."""

        status = next(
            (
                candidate
                for candidate in ("BLOCKED", "FAILED", "REJECTED")
                if outcome.startswith(f"{candidate}:")
                or f":{candidate}" in outcome
            ),
            None,
        )
        if status is None:
            return
        category = f"LIFECYCLE_{stage}_{status}"
        blockers.append(category)
        error = RuntimeError(outcome)
        self._open_incident(category, error, now)
        self._notify(
            "RUNTIME_INCIDENT",
            {
                "event_id": f"{now.astimezone(NEW_YORK).date()}:{category}:{symbol}",
                "state": "blocked",
                "symbol": symbol,
                "reason": outcome,
            },
            now,
        )
        runtime = self.state.runtime_status()
        if runtime is not None and EngineMode(str(runtime["mode"])) in {
            EngineMode.ACTIVE,
            EngineMode.RECONCILING,
        }:
            self._transition(
                EngineMode.PAUSE_NEW_ENTRIES,
                now,
                f"{stage.lower()} returned {status.lower()}",
            )

    def _open_incident(self, category: str, exc: Exception, now: datetime) -> None:
        existing = self.state.rows(
            "SELECT incident_id FROM incidents WHERE account_key=? AND category=? AND resolved_at IS NULL",
            (self.account_key, category),
        )
        if existing:
            return
        fingerprint = hashlib.sha256(
            f"{self.account_key}\n{category}\n{now.isoformat()}".encode("utf-8")
        ).hexdigest()
        self.state.record_incident(
            Incident(
                incident_id=f"incident-{fingerprint}",
                account_key=self.account_key,
                category=category,
                severity=IncidentSeverity.CRITICAL,
                opened_at=now,
                detail={"error_type": type(exc).__name__, "error_code": category},
            )
        )

    def _lifecycle_failure(self, stage: str, exc: Exception, now: datetime) -> None:
        category = f"LIFECYCLE_{stage}_FAILED"
        self._open_incident(category, exc, now)
        self._notify(
            "RUNTIME_INCIDENT",
            {
                "event_id": f"{now.astimezone(NEW_YORK).date()}:{category}",
                "state": "blocked",
                "symbol": "ACCOUNT",
                "reason": type(exc).__name__,
            },
            now,
        )

    def _transition(self, target: EngineMode, now: datetime, reason: str) -> None:
        before = self.state.runtime_status()
        try:
            changed = self.state.set_runtime_mode(
                target.value, occurred_at=now, reason=reason
            )
        except StateConflict:
            # A more conservative concurrent/preceding transition remains in force.
            return
        if not changed or before is None:
            return
        if target in {
            EngineMode.PAUSE_NEW_ENTRIES,
            EngineMode.MANAGED_CLOSEOUT,
            EngineMode.INCIDENT,
        }:
            self._notify(
                "RISK_PAUSED",
                {
                    "event_id": f"{now.isoformat()}:{target.value}",
                    "state": target.value.lower(),
                    "symbol": "ACCOUNT",
                    "reason": reason,
                },
                now,
            )
        elif target is EngineMode.ACTIVE:
            self._notify(
                "RECOVERED",
                {
                    "event_id": f"{now.isoformat()}:ACTIVE",
                    "state": "active",
                    "symbol": "ACCOUNT",
                    "reason": reason,
                },
                now,
            )

    def record_control_results(
        self, results: Sequence[ControlResult], *, now: datetime
    ) -> None:
        """Fail closed and surface any operator control that was rejected."""

        for result in results:
            if result.status != "REJECTED":
                continue
            error = ControlErrorProxy(result.detail)
            self._open_incident("CONTROL_REQUEST_REJECTED", error, now)
            self._notify(
                "RUNTIME_INCIDENT",
                {
                    "event_id": f"{result.request_id}:control-rejected",
                    "state": "blocked",
                    "symbol": "ACCOUNT",
                    "reason": f"{result.command}: {result.detail}",
                },
                now,
            )
            runtime = self.state.runtime_status()
            if runtime is not None and bool(runtime["authority_enabled"]):
                mode = EngineMode(str(runtime["mode"]))
                if mode in {EngineMode.ACTIVE, EngineMode.RECONCILING}:
                    self._transition(
                        EngineMode.PAUSE_NEW_ENTRIES,
                        now,
                        "operator control request rejected",
                    )

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None:
            raise ValueError("service clock must be timezone-aware")
        return value.astimezone(timezone.utc)


@dataclass
class AcquiredServiceWriterAuthority:
    """One exact kernel-lock/database-lease pair owned by a service process.

    Provider composition may open fixed broker client IDs, so the CLI acquires
    this pair before constructing that graph and transfers it to
    :class:`ServiceRunner`.  The same helper is also used by direct runner
    callers, keeping acquisition, heartbeat, and release semantics identical.
    """

    state: LiveStateStore
    account_key: str
    lock: AccountWriterLock
    generation: int
    process_id: int
    released: bool = False

    @classmethod
    def acquire(
        cls,
        *,
        state: LiveStateStore,
        account_key: str,
        lock: AccountWriterLock,
        acquired_at: datetime,
    ) -> "AcquiredServiceWriterAuthority":
        if not isinstance(state, LiveStateStore):
            raise TypeError("service writer authority requires LiveStateStore")
        if not isinstance(lock, AccountWriterLock):
            raise TypeError("service writer authority requires AccountWriterLock")
        if not isinstance(account_key, str) or not account_key.strip():
            raise ValueError("service writer account key is required")
        if not isinstance(acquired_at, datetime) or acquired_at.tzinfo is None:
            raise ValueError("service writer acquisition time must be aware")
        current = acquired_at.astimezone(timezone.utc)
        generation: int | None = None
        lock.acquire(blocking=False, acquired_at=current)
        try:
            generation = state.acquire_writer_lease(
                account_key=account_key,
                owner_id=lock.owner_id,
                acquired_at=current,
                recover_stale=True,
            )
            lock.bind_writer_lease(generation)
            return cls(
                state=state,
                account_key=account_key,
                lock=lock,
                generation=generation,
                process_id=os.getpid(),
            )
        except BaseException:
            try:
                if generation is not None:
                    state.release_writer_lease(
                        account_key=account_key,
                        owner_id=lock.owner_id,
                        released_at=current,
                        generation=generation,
                        process_id=os.getpid(),
                    )
            finally:
                lock.release()
            raise

    def assert_matches(
        self,
        *,
        state: LiveStateStore,
        account_key: str,
        lock: AccountWriterLock,
    ) -> None:
        if self.released:
            raise StateConflict("service writer authority was already released")
        if self.state is not state or self.lock is not lock or self.account_key != account_key:
            raise StateConflict("service writer authority binding mismatch")
        if (
            not self.lock.held
            or self.lock.writer_lease_generation != self.generation
            or self.process_id != os.getpid()
        ):
            raise StateConflict("service writer authority is not held")

    def heartbeat(self, observed_at: datetime) -> None:
        self.assert_matches(
            state=self.state,
            account_key=self.account_key,
            lock=self.lock,
        )
        self.lock.refresh()
        self.state.heartbeat_writer_lease(
            account_key=self.account_key,
            owner_id=self.lock.owner_id,
            observed_at=observed_at,
            generation=self.generation,
            process_id=self.process_id,
        )

    def release(self, released_at: datetime) -> None:
        if self.released:
            return
        error: BaseException | None = None
        try:
            self.state.release_writer_lease(
                account_key=self.account_key,
                owner_id=self.lock.owner_id,
                released_at=released_at,
                generation=self.generation,
                process_id=self.process_id,
            )
        except BaseException as exc:  # always release the kernel authority
            error = exc
        finally:
            self.lock.release()
            self.released = True
        if error is not None:
            raise error


class ServiceRunner:
    """Own the kernel/database writer lease and run interruptibly."""

    def __init__(
        self,
        *,
        service: FullLiveService,
        lock: AccountWriterLock,
        interval_seconds: float,
        control_inbox: ControlInbox | None = None,
        writer_authority: AcquiredServiceWriterAuthority | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("service interval must be positive")
        self.service = service
        self.lock = lock
        self.interval_seconds = interval_seconds
        self.control_inbox = control_inbox
        if writer_authority is not None and not isinstance(
            writer_authority, AcquiredServiceWriterAuthority
        ):
            raise TypeError("writer_authority must be acquired service authority")
        self.writer_authority = writer_authority
        self.stop_event = threading.Event()
        self._writer_heartbeat_lock = threading.Lock()

    def _heartbeat_writer_authority(
        self, authority: AcquiredServiceWriterAuthority
    ) -> None:
        """Capture and commit one lease timestamp as one ordered operation."""

        with self._writer_heartbeat_lock:
            authority.heartbeat(self.service._now())

    def _lease_heartbeat_loop(
        self,
        authority: AcquiredServiceWriterAuthority,
        stopped: threading.Event,
        failures: list[BaseException],
    ) -> None:
        """Keep the durable writer lease current during slow provider calls.

        A broker snapshot can legitimately take longer than the ordinary
        reconciliation interval.  Tying lease renewal only to the end of a
        service iteration would therefore revoke the exact writer while it is
        still healthy and holding the kernel lock.  This narrow watchdog owns
        no provider and performs no broker work; loss of the already-acquired
        lease stops the runner and is surfaced to the main thread.
        """

        interval = min(1.0, max(0.1, self.interval_seconds / 2.0))
        while not stopped.wait(interval):
            try:
                self._heartbeat_writer_authority(authority)
            except BaseException as exc:
                failures.append(exc)
                self.stop_event.set()
                return

    def run(self, *, once: bool = False) -> TickResult | None:
        acquired_at = self.service._now()
        authority = self.writer_authority
        prior_handlers: dict[int, Any] = {}
        last: TickResult | None = None
        heartbeat_stop = threading.Event()
        heartbeat_failures: list[BaseException] = []
        heartbeat_thread: threading.Thread | None = None
        try:
            if authority is None:
                authority = AcquiredServiceWriterAuthority.acquire(
                    state=self.service.state,
                    account_key=self.service.account_key,
                    lock=self.lock,
                    acquired_at=acquired_at,
                )
            authority.assert_matches(
                state=self.service.state,
                account_key=self.service.account_key,
                lock=self.lock,
            )
            # Establish a fresh lease edge before any control or provider
            # call, then renew independently while that call is in flight.
            authority.heartbeat(acquired_at)
            heartbeat_thread = threading.Thread(
                target=self._lease_heartbeat_loop,
                args=(authority, heartbeat_stop, heartbeat_failures),
                name="titan-account-writer-lease-heartbeat",
                daemon=True,
            )
            heartbeat_thread.start()
            if threading.current_thread() is threading.main_thread():
                for signum in (signal.SIGINT, signal.SIGTERM):
                    prior_handlers[signum] = signal.getsignal(signum)
                    signal.signal(signum, lambda *_: self.stop_event.set())
            while not self.stop_event.is_set():
                if self.control_inbox is not None:
                    control_now = self.service._now()
                    control_results = self.control_inbox.drain(
                        self.service.state, now=control_now
                    )
                    self.service.record_control_results(
                        control_results, now=control_now
                    )
                last = self.service.run_once()
                if heartbeat_failures:
                    raise StateConflict(
                        "service writer lease heartbeat failed"
                    ) from heartbeat_failures[0]
                self._heartbeat_writer_authority(authority)
                if once:
                    break
                delay = last.next_poll_delay_seconds
                if delay is None:
                    delay = self.interval_seconds
                elif delay > 0:
                    delay = min(delay, self.interval_seconds)
                if delay > 0:
                    self.stop_event.wait(delay)
        finally:
            try:
                heartbeat_stop.set()
                if heartbeat_thread is not None:
                    heartbeat_thread.join()
                if authority is not None:
                    authority.release(self.service._now())
            finally:
                for signum, handler in prior_handlers.items():
                    signal.signal(signum, handler)
        if heartbeat_failures:
            raise StateConflict(
                "service writer lease heartbeat failed"
            ) from heartbeat_failures[0]
        return last


class ControlErrorProxy(RuntimeError):
    """Stable incident error type for a rejected filesystem control."""


def build_local_outbox(
    store: LiveStateStore,
    account_key: str,
    sink: Any,
    *,
    latency: LatencyRecorder | None = None,
) -> OutboxDispatcher:
    return OutboxDispatcher(
        LiveStateOutboxAdapter(store, account_key),
        sink,
        latency=latency,
    )


def build_enqueue_only_outbox(
    store: LiveStateStore,
    account_key: str,
) -> EnqueueOnlyOutbox:
    """Build the service-side publisher without provider delivery authority."""

    return EnqueueOnlyOutbox(LiveStateOutboxAdapter(store, account_key))


__all__ = [
    "DisabledLifecycleActions",
    "FullLiveService",
    "LifecycleActions",
    "ServiceRunner",
    "TickResult",
    "build_local_outbox",
    "build_enqueue_only_outbox",
    "persist_account_snapshot",
]
