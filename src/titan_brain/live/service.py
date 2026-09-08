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
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
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
from .calendar import NEW_YORK
from .control import ControlInbox, ControlResult
from .exits import SafeCloseDecision, plan_safe_close
from .lifecycle_actions import LifecycleReconcileResult
from .latency import LatencyRecorder
from .models import (
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
    error: str | None = None

    @property
    def healthy(self) -> bool:
        return self.error is None and not self.reconciliation_blockers


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
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.policy = policy
        self.state = state
        self.broker = broker
        self.notifications = notifications
        self.notification_route = notification_route
        self.actions = actions or DisabledLifecycleActions()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.account_key = str(policy.config["account"]["masked_identifier"])
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
        blockers.extend(self.policy.activation_blockers)
        if not self.policy.live_entries_configured:
            blockers.append("LIVE_ENTRIES_DISABLED_IN_SIGNED_CONFIG")
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

        actions.append("INGEST_CONFIRMED_FILLS")
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
        actions.append("VERIFY_OR_ESTABLISH_PROTECTION")
        protection = self._assess_protection(snapshot)
        protection_safe = all(item.protected for item in protection)
        if not protection_safe:
            blockers.append("UNPROTECTED_EXPOSURE_PRESENT")
            self._notify_unprotected(protection, durable_snapshot.snapshot_id, now)
            for decision in protection:
                if not decision.protected:
                    if authority_enabled:
                        try:
                            result = self.actions.protect(
                                snapshot=snapshot, decision=decision, now=now
                            )
                            actions.append(f"PROTECT:{decision.symbol}:{result}")
                        except Exception as exc:
                            blockers.append("LIFECYCLE_PROTECTION_FAILED")
                            actions.append(
                                f"PROTECT:{decision.symbol}:FAILED:{type(exc).__name__}"
                            )
                            self._lifecycle_failure("PROTECTION", exc, now)
                    else:
                        actions.append(f"PROTECT:{decision.symbol}:BLOCKED_NO_RUNTIME_AUTHORITY")

        latch = self._update_risk_latch(snapshot, now)
        if latch is not None:
            if latch.loss_lock:
                blockers.append("IRREVERSIBLE_DAILY_NEW_ENTRY_LOCK")
            if latch.hard_kill:
                blockers.append("HARD_DAILY_LOSS_KILL")

        mode_current = EngineMode(str(self.state.runtime_status()["mode"]))
        has_broker_exposure = bool(snapshot.equity_positions)
        should_close = (
            mode_current is EngineMode.MANAGED_CLOSEOUT
            or lane in {"closeout", "flat_deadline"}
            or (lane == "closed" and has_broker_exposure)
            or (mode_current is EngineMode.INCIDENT and has_broker_exposure)
            or bool(latch is not None and latch.hard_kill)
        )
        closeout: tuple[SafeCloseDecision, ...] = ()
        if should_close:
            actions.append("MANAGED_CLOSEOUT")
            if mode_current not in {EngineMode.PAUSED, EngineMode.STOPPED, EngineMode.INCIDENT}:
                self._transition(EngineMode.MANAGED_CLOSEOUT, now, "session or hard-kill closeout")
            closeout = self._plan_closeout(snapshot)
            for decision in closeout:
                if decision.action.value != "FLAT":
                    if authority_enabled:
                        try:
                            result = self.actions.closeout(
                                snapshot=snapshot, decision=decision, now=now
                            )
                            actions.append(f"CLOSEOUT:{decision.symbol}:{result}")
                        except Exception as exc:
                            blockers.append("LIFECYCLE_CLOSEOUT_FAILED")
                            actions.append(
                                f"CLOSEOUT:{decision.symbol}:FAILED:{type(exc).__name__}"
                            )
                            self._lifecycle_failure("CLOSEOUT", exc, now)
                    else:
                        actions.append(f"CLOSEOUT:{decision.symbol}:BLOCKED_NO_RUNTIME_AUTHORITY")
            if any(item.action.value != "FLAT" for item in closeout):
                blockers.append("CLOSEOUT_NOT_FLAT")
        elif blockers and mode_current in {
            EngineMode.ACTIVE,
            EngineMode.RECONCILING,
        }:
            self._transition(EngineMode.PAUSE_NEW_ENTRIES, now, blockers[0])

        if lane == "closed" and snapshot.equity_positions:
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
                            ",".join(
                                sorted(
                                    item.symbol for item in snapshot.equity_positions
                                )
                            ).encode("utf-8")
                        ).hexdigest()[:16]
                    ),
                    "state": "unresolved",
                    "symbol": ",".join(sorted(item.symbol for item in snapshot.equity_positions)),
                    "reason": "broker position remains after session close",
                },
                now,
            )

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
        ):
            actions.append("DISCOVER_AND_EXECUTE")
            try:
                actions.extend(
                    self.actions.discover_and_execute(snapshot=snapshot, now=now)
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
        )

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
        return self.notifications.drain(now)

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

    def _update_risk_latch(
        self, snapshot: AccountSnapshot, now: datetime
    ) -> RiskSessionLatch | None:
        if not snapshot.daily_realized_pnl_ready:
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
        updated = update_session_latch(
            self.policy,
            prior,
            realized_pnl=snapshot.daily_realized_pnl,
            usable_equity=snapshot.funds.total_value,
            observed_at=now.astimezone(NEW_YORK),
        )
        durable = DurableSessionLatch(
            account_key=self.account_key,
            trading_date=local_date,
            loss_locked=updated.loss_lock,
            objective_crossed=updated.profit_goal_crossed,
            pause_new_entries=updated.loss_lock or updated.hard_kill,
            closeout_started=updated.hard_kill,
            hard_kill=updated.hard_kill,
            highest_realized_pnl=updated.highest_realized_pnl,
            first_objective_crossed_at=updated.first_profit_crossed_at,
            revision=revision,
            updated_at=now,
        )
        self.state.apply_session_latch(durable)
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

    def _notify(self, event: str, payload: Mapping[str, Any], now: datetime) -> None:
        self.notifications.enqueue(event, payload, now)

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


class ServiceRunner:
    """Own the kernel/database writer lease and run interruptibly."""

    def __init__(
        self,
        *,
        service: FullLiveService,
        lock: AccountWriterLock,
        interval_seconds: float,
        control_inbox: ControlInbox | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("service interval must be positive")
        self.service = service
        self.lock = lock
        self.interval_seconds = interval_seconds
        self.control_inbox = control_inbox
        self.stop_event = threading.Event()

    def run(self, *, once: bool = False) -> TickResult | None:
        self.lock.acquire(blocking=False)
        acquired_at = self.service._now()
        self.service.state.acquire_writer_lease(
            account_key=self.service.account_key,
            owner_id=self.lock.owner_id,
            acquired_at=acquired_at,
            recover_stale=True,
        )
        prior_handlers: dict[int, Any] = {}
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                prior_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, lambda *_: self.stop_event.set())
        last: TickResult | None = None
        try:
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
                now = self.service._now()
                self.lock.refresh()
                self.service.state.heartbeat_writer_lease(
                    account_key=self.service.account_key,
                    owner_id=self.lock.owner_id,
                    observed_at=now,
                )
                if once:
                    break
                self.stop_event.wait(self.interval_seconds)
            return last
        finally:
            released_at = self.service._now()
            self.service.state.release_writer_lease(
                account_key=self.service.account_key,
                owner_id=self.lock.owner_id,
                released_at=released_at,
            )
            self.lock.release()
            for signum, handler in prior_handlers.items():
                signal.signal(signum, handler)


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
