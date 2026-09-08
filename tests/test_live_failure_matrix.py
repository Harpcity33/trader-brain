"""Synthetic failure simulations for the September 8 full-live contract.

These tests exercise deterministic fakes and local storage only.  They are
not broker certification, live-market evidence, or proof that an unattended
Robinhood transport exists.  The purpose is to pin the fail-closed behavior
expected at each failure boundary before an owner-controlled activation.
"""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from titan_brain.live.broker import (
    AccountSnapshot,
    BrokerError,
    BrokerSide,
    EquityOrderType,
    FakeBrokerClient,
    FakeFault,
    MarketHours,
    OrderRequest,
    OrderSnapshot,
    PositionSnapshot,
    TimeInForce,
)
from titan_brain.live.execution import EntryExecutionCoordinator, ExecutionStatus
from titan_brain.live.lifecycle_actions import LifecycleReconcileResult
from titan_brain.live.market_data import CompletedBar, MarketDataCache, Quote
from titan_brain.live.models import BrokerOrderState
from titan_brain.live.notifications import (
    JsonlNotificationSink,
    LiveStateOutboxAdapter,
    OutboxDispatcher,
)
from titan_brain.live.policy import PolicyBundle, sha256_json
from titan_brain.live.reconcile import (
    ActivityOwner,
    AuthoritativeReconciler,
    ReconciliationPhase,
)
from titan_brain.live.service import FullLiveService, build_local_outbox
from titan_brain.live.state import LiveStateStore
from titan_brain.live.writer_lock import AccountWriterLock, WriterLockBusy

from tests.test_live_execution import (
    ExplicitTestMutationAuthority,
    NOW as EXECUTION_NOW,
    allowed_risk,
    market_cache,
    plan_for,
)
from tests.test_live_service import account_snapshot
from tests.live_activation_support import activate_canonical_runtime


UTC = timezone.utc
ET = ZoneInfo("America/New_York")
ACCOUNT_KEY = "ending-7153"
ACCOUNT_MASKED = "••••7153"
HASH_A = "a" * 64


class SyntheticKnownNoAccept429(BrokerError):
    """Synthetic 429 whose adapter proves no mutation left the client."""

    code = "SYNTHETIC_HTTP_429_KNOWN_NO_ACCEPT"
    retry_safe = True
    submission_may_have_reached_broker = False


class SyntheticAmbiguous503(BrokerError):
    """Synthetic 503 after send: broker acceptance cannot be excluded."""

    code = "SYNTHETIC_HTTP_503_SIDE_EFFECT_UNKNOWN"
    retry_safe = False
    submission_may_have_reached_broker = True


class SyntheticPlaceFaultBroker(FakeBrokerClient):
    def __init__(self, fault_type: type[BrokerError]) -> None:
        super().__init__(clock=lambda: EXECUTION_NOW)
        self.fault_type = fault_type

    def place_equity_order(self, request, *, review, explicit_confirmation=None):
        # This is deliberately after exact review and after the coordinator has
        # persisted SUBMITTING, matching the real mutation ambiguity boundary.
        self.calls.append((self.PLACE, request.exact_tuple))
        raise self.fault_type("synthetic HTTP failure at place boundary")


class SyntheticLifecycleActions:
    """Non-broker action spy; it never places or cancels an order."""

    def __init__(
        self,
        *,
        store: LiveStateStore,
        research_unavailable: bool = False,
    ) -> None:
        self.store = store
        self.research_unavailable = research_unavailable
        self.calls: list[str] = []
        self.risk_latch_seen_before_research = False

    def reconcile(self, **_) -> LifecycleReconcileResult:
        self.calls.append("reconcile")
        return LifecycleReconcileResult(
            actions=("synthetic-lifecycle-reconciled",)
        )

    def protect(self, **_) -> str:
        self.calls.append("protect")
        return "synthetic-protection-path-invoked"

    def closeout(self, **_) -> str:
        self.calls.append("closeout")
        return "synthetic-closeout-path-invoked"

    def discover_and_execute(self, **_) -> tuple[str, ...]:
        self.calls.append("discover")
        self.risk_latch_seen_before_research = bool(
            self.store.rows(
                "SELECT account_key FROM session_latches WHERE account_key=?",
                (ACCOUNT_KEY,),
            )
        )
        if self.research_unavailable:
            raise RuntimeError("synthetic model/research service unavailable")
        return ("synthetic-discovery-complete",)


class FailOnceSink:
    def __init__(self) -> None:
        self.calls = 0
        self.delivered_keys: list[str] = []

    def send(self, notification) -> str:
        self.calls += 1
        if self.calls == 1:
            raise OSError("synthetic notification transport outage")
        self.delivered_keys.append(notification.dedupe_key)
        return "synthetic-receipt-0001"


def synthetic_enabled_policy() -> PolicyBundle:
    """Enable otherwise-blocked gates only inside an offline simulation.

    This never writes the checked-in policy.  It lets failure tests reach the
    post-activation branches while production remains blocked on real broker,
    risk-provenance, notification, spread, and depth evidence.
    """

    root = Path(__file__).resolve().parents[1]
    base = PolicyBundle.load(root)
    config = copy.deepcopy(base.config)
    config["authority"]["live_entries_enabled"] = True
    config["authority"]["blockers"] = []
    config["execution"]["supported_unattended_mutation"] = True
    config["execution"]["per_mutation_user_confirmation_required"] = False
    config["execution"]["local_mutation_interlock_enabled"] = True
    config["execution"]["local_mutation_interlock_enabled"] = True
    config["evidence"]["max_spread_bps"] = "25"
    config["evidence"]["minimum_depth_multiple"] = "5"
    config["risk"]["limits_live_provenance_verified"] = True
    config["notifications"]["destination_bridge_configured"] = True
    config["discovery"].update(
        {
            "pipeline_configured": True,
            "instrument_evidence_provider": "synthetic_test_only",
            "quality_revalidation_provider": "synthetic_test_only",
            "minimum_setup_score": 70,
            "minimum_execution_score": 65,
            "a_plus_setup_score": 95,
            "a_plus_execution_score": 90,
        }
    )
    config_hash = sha256_json(config)
    policy_hash = sha256_json(
        {
            "account": config["account"],
            "scope": config["scope"],
            "sessions": config["sessions"],
            "risk": config["risk"],
            "risk_hash": base.risk_hash,
            "strategy_id": config["strategy_id"],
        }
    )
    result = replace(
        base,
        config=config,
        config_hash=config_hash,
        policy_hash=policy_hash,
    )
    result.validate()
    result.require_activation_ready()
    return result


def snapshot_at(
    now: datetime,
    *,
    positions: tuple[PositionSnapshot, ...] = (),
    orders: tuple[OrderSnapshot, ...] = (),
) -> AccountSnapshot:
    base = account_snapshot()
    current = now.astimezone(UTC)
    return replace(
        base,
        observed_at=current,
        received_at=current,
        equity_positions=positions,
        equity_orders=orders,
        risk_evidence_as_of=current,
    )


def arm_runtime(store: LiveStateStore, now: datetime) -> None:
    initialized = now.astimezone(UTC) - timedelta(minutes=2)
    store.initialize_runtime(
        runtime_id="synthetic-full-live-failure-matrix",
        account_key=ACCOUNT_KEY,
        release_manifest_hash=HASH_A,
        config_hash=HASH_A,
        policy_hash=HASH_A,
        initialized_at=initialized,
    )
    activate_canonical_runtime(
        store,
        created_at=now.astimezone(UTC) - timedelta(seconds=2),
        expires_at=now.astimezone(UTC) + timedelta(minutes=5),
        activated_at=now.astimezone(UTC) - timedelta(seconds=1),
    )


def manual_pending_order(now: datetime) -> OrderSnapshot:
    current = now.astimezone(UTC)
    return OrderSnapshot(
        broker_order_id="synthetic-manual-order-1",
        account_masked=ACCOUNT_MASKED,
        symbol="XYZ",
        side=BrokerSide.BUY,
        order_type=EquityOrderType.LIMIT,
        state=BrokerOrderState.PENDING,
        requested_quantity=Decimal("2"),
        cumulative_filled_quantity=Decimal("0"),
        market_hours=MarketHours.REGULAR,
        time_in_force=TimeInForce.GFD,
        broker_updated_at=current,
        received_at=current,
        limit_price=Decimal("10.00"),
        client_ref_id=None,
    )


def completed_bar(now: datetime, *, sequence: int = 1) -> CompletedBar:
    end = now - timedelta(minutes=1)
    return CompletedBar.build(
        symbol="XYZ",
        start_at=end - timedelta(minutes=1),
        end_at=end,
        open="9.90",
        high="10.05",
        low="9.85",
        close="10.01",
        volume=800_000,
        sequence=sequence,
        source_event_id=f"synthetic-bar-{sequence}",
    )


def quote_at(
    now: datetime,
    *,
    bid: str = "10.00",
    ask: str = "10.02",
    venue_age_seconds: int = 1,
    halted: bool = False,
) -> Quote:
    venue_at = now - timedelta(seconds=venue_age_seconds)
    return Quote.build(
        symbol="XYZ",
        bid=bid,
        ask=ask,
        bid_size=500,
        ask_size=500,
        venue_bid_at=venue_at,
        venue_ask_at=venue_at,
        observed_at=now,
        source="synthetic-robinhood-quote",
        tradable=True,
        halted=halted,
    )


def evidence_decision(cache: MarketDataCache, now: datetime):
    bar = completed_bar(now)
    return cache.validate_entry_evidence(
        symbol="XYZ",
        now=now,
        plan_created_at=now - timedelta(seconds=2),
        plan_expires_at=now + timedelta(seconds=20),
        causal_bar_end=bar.end_at,
        quote_max_age_seconds=3,
        completed_bar_max_age_seconds=120,
        minimum_session_volume=750_000,
        max_spread_bps="25",
        minimum_depth_multiple="5",
        quantity=2,
    )


class FullLiveFailureMatrixTests(unittest.TestCase):
    """All cases are deterministic, offline, and explicitly synthetic."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.temp = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_broker_429_known_no_accept_is_not_confused_with_503_unknown_side_effect(self) -> None:
        policy = synthetic_enabled_policy()
        plan = plan_for(policy)
        risk = allowed_risk(plan)

        with LiveStateStore(self.temp / "known-no-accept.sqlite3") as store:
            rate_limited = SyntheticPlaceFaultBroker(SyntheticKnownNoAccept429)
            result = EntryExecutionCoordinator(
                policy=policy,
                market_data=market_cache(),
                state=store,
                broker=rate_limited,
                authority=ExplicitTestMutationAuthority(),
                clock=lambda: EXECUTION_NOW,
            ).submit_entry(plan=plan, risk_decision=risk)
            self.assertEqual(result.status, ExecutionStatus.FAILED)
            self.assertIn(SyntheticKnownNoAccept429.code, result.failure_codes)
            self.assertEqual(
                store.row("order_intents", "intent_id", result.intent_id)["state"],
                "FAILED",
            )
            self.assertEqual(store.rows("SELECT * FROM incidents"), [])

        with LiveStateStore(self.temp / "unknown-side-effect.sqlite3") as store:
            unavailable = SyntheticPlaceFaultBroker(SyntheticAmbiguous503)
            coordinator = EntryExecutionCoordinator(
                policy=policy,
                market_data=market_cache(),
                state=store,
                broker=unavailable,
                authority=ExplicitTestMutationAuthority(),
                clock=lambda: EXECUTION_NOW,
            )
            unknown = coordinator.submit_entry(plan=plan, risk_decision=risk)
            self.assertEqual(unknown.status, ExecutionStatus.UNKNOWN)
            self.assertTrue(unknown.risk_reserved)
            self.assertIn(SyntheticAmbiguous503.code, unknown.failure_codes)
            place_calls = sum(
                operation == FakeBrokerClient.PLACE
                for operation, _ in unavailable.calls
            )
            replay = coordinator.submit_entry(plan=plan, risk_decision=risk)
            self.assertEqual(replay.status, ExecutionStatus.UNKNOWN)
            self.assertTrue(replay.replay)
            self.assertEqual(
                sum(
                    operation == FakeBrokerClient.PLACE
                    for operation, _ in unavailable.calls
                ),
                place_calls,
            )
            self.assertEqual(len(store.rows("SELECT * FROM incidents")), 1)

    def test_revoked_credentials_force_armed_runtime_into_incident_without_discovery(self) -> None:
        now = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
        policy = synthetic_enabled_policy()
        with LiveStateStore(self.temp / "revoked.sqlite3") as store:
            arm_runtime(store, now)
            actions = SyntheticLifecycleActions(store=store)
            broker = FakeBrokerClient(clock=lambda: now)
            broker.inject_fault(FakeBrokerClient.SNAPSHOT, FakeFault.AUTHENTICATION)
            service = FullLiveService(
                policy=policy,
                state=store,
                broker=broker,
                notifications=build_local_outbox(
                    store,
                    ACCOUNT_KEY,
                    JsonlNotificationSink(self.temp / "revoked-notifications.jsonl"),
                ),
                actions=actions,
                clock=lambda: now,
            )
            result = service.run_once()
            self.assertEqual(result.mode_after, "INCIDENT")
            self.assertIn("AUTHENTICATION_INCIDENT", result.reconciliation_blockers)
            self.assertFalse(result.entries_considered)
            self.assertNotIn("discover", actions.calls)
            incident = store.rows("SELECT category FROM incidents")
            self.assertEqual([row["category"] for row in incident], ["AUTHENTICATION_INCIDENT"])

    def test_restart_with_unowned_exposure_reconciles_then_invokes_protection_before_discovery(self) -> None:
        now = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
        state_path = self.temp / "restart-exposure.sqlite3"
        first = LiveStateStore(state_path)
        arm_runtime(first, now)
        first.close()

        position = PositionSnapshot(
            symbol="XYZ",
            quantity=Decimal("5"),
            sellable_quantity=Decimal("5"),
            average_price=Decimal("10.00"),
        )
        broker = FakeBrokerClient(
            initial_snapshot=snapshot_at(now, positions=(position,)),
            clock=lambda: now,
        )
        with LiveStateStore(state_path) as restarted:
            actions = SyntheticLifecycleActions(store=restarted)
            service = FullLiveService(
                policy=synthetic_enabled_policy(),
                state=restarted,
                broker=broker,
                notifications=build_local_outbox(
                    restarted,
                    ACCOUNT_KEY,
                    JsonlNotificationSink(self.temp / "restart-notifications.jsonl"),
                ),
                actions=actions,
                clock=lambda: now,
            )
            result = service.run_once()
            self.assertIn("POSITION_OWNERSHIP_MISMATCH", result.reconciliation_blockers)
            self.assertIn("UNPROTECTED_EXPOSURE_PRESENT", result.reconciliation_blockers)
            self.assertIn("protect", actions.calls)
            self.assertNotIn("discover", actions.calls)
            protect_index = next(
                index for index, action in enumerate(result.actions) if action.startswith("PROTECT:XYZ:")
            )
            self.assertLess(result.actions.index("RECONCILE_ACCOUNT"), protect_index)
            self.assertLess(protect_index, result.actions.index("BLOCK_DISCOVERY"))

    def test_manual_broker_activity_is_identified_and_blocks_entries(self) -> None:
        now = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
        snapshot = snapshot_at(now, orders=(manual_pending_order(now),))
        broker = FakeBrokerClient(initial_snapshot=snapshot, clock=lambda: now)
        with LiveStateStore(self.temp / "manual.sqlite3") as store:
            report = AuthoritativeReconciler(
                account_masked=ACCOUNT_MASKED,
                account_key=ACCOUNT_KEY,
            ).reconcile_snapshot(
                store,
                snapshot=broker.get_account_snapshot(ACCOUNT_MASKED),
                capabilities=broker.capabilities,
                now=now,
                phase=ReconciliationPhase.CONTINUOUS,
            )
            self.assertFalse(report.entries_allowed)
            self.assertIn("EXTERNAL_BROKER_ACTIVITY", report.blockers)
            self.assertEqual(len(report.external_activity), 1)
            self.assertEqual(report.external_activity[0].owner, ActivityOwner.MANUAL)
            self.assertTrue(report.external_activity[0].blocks_entries)

    def test_storage_write_and_notification_fsync_failures_leave_no_false_success(self) -> None:
        now = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
        with LiveStateStore(self.temp / "storage.sqlite3") as store:
            baseline_id = "synthetic-baseline-event"
            store.append_event(
                stream=ACCOUNT_KEY,
                event_type="SYNTHETIC_BASELINE",
                entity_type="simulation",
                entity_id="baseline",
                occurred_at=now,
                payload={"environment": "synthetic"},
                event_id=baseline_id,
            )
            store._conn.execute("PRAGMA query_only=ON")
            with self.assertRaises(sqlite3.OperationalError):
                store.append_event(
                    stream=ACCOUNT_KEY,
                    event_type="MUST_NOT_PARTIALLY_PERSIST",
                    entity_type="simulation",
                    entity_id="write-failure",
                    occurred_at=now + timedelta(seconds=1),
                    payload={"environment": "synthetic"},
                )
            store._conn.execute("PRAGMA query_only=OFF")
            events = store.rows("SELECT event_id,event_type FROM audit_events")
            self.assertEqual([(row["event_id"], row["event_type"]) for row in events], [(baseline_id, "SYNTHETIC_BASELINE")])
            self.assertTrue(store.verify_event_chain()[0])

            dispatcher = OutboxDispatcher(
                LiveStateOutboxAdapter(store, ACCOUNT_KEY),
                JsonlNotificationSink(self.temp / "fsync-failure.jsonl"),
            )
            message_id = dispatcher.enqueue(
                "RUNTIME_INCIDENT",
                {
                    "event_id": "synthetic-fsync-failure",
                    "state": "blocked",
                    "reason": "synthetic storage durability failure",
                },
                now,
            )
            with patch("titan_brain.live.notifications.os.fsync", side_effect=OSError("synthetic fsync failure")):
                sent, failed = dispatcher.drain(now)
            self.assertEqual((sent, failed), (0, 1))
            outbox = store.row("notification_outbox", "message_id", message_id)
            self.assertEqual(outbox["state"], "PENDING")
            self.assertEqual(outbox["attempt_count"], 1)
            self.assertIn("synthetic fsync failure", outbox["last_error"])
            self.assertIsNone(outbox["delivered_at"])

    def test_notification_failure_retries_after_backoff_and_dedupes_delivery(self) -> None:
        now = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
        with LiveStateStore(self.temp / "notification-retry.sqlite3") as store:
            sink = FailOnceSink()
            dispatcher = OutboxDispatcher(
                LiveStateOutboxAdapter(store, ACCOUNT_KEY), sink
            )
            message_id = dispatcher.enqueue(
                "UNPROTECTED_EXPOSURE",
                {
                    "event_id": "synthetic-exposure-1",
                    "state": "unprotected",
                    "symbol": "XYZ",
                    "quantity": 2,
                    "protection_state": "missing",
                },
                now,
            )
            self.assertEqual(dispatcher.drain(now), (0, 1))
            self.assertEqual(dispatcher.drain(now + timedelta(seconds=1)), (0, 0))
            self.assertEqual(dispatcher.drain(now + timedelta(seconds=2)), (1, 0))
            row = store.row("notification_outbox", "message_id", message_id)
            self.assertEqual(row["state"], "DELIVERED")
            self.assertEqual(row["attempt_count"], 2)
            self.assertEqual(row["delivery_receipt"], "synthetic-receipt-0001")
            self.assertEqual(
                sink.delivered_keys,
                ["UNPROTECTED_EXPOSURE:synthetic-exposure-1"],
            )

    def test_model_or_research_failure_occurs_only_after_reconciliation_and_risk_latch(self) -> None:
        now = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
        with LiveStateStore(self.temp / "research-unavailable.sqlite3") as store:
            arm_runtime(store, now)
            actions = SyntheticLifecycleActions(
                store=store, research_unavailable=True
            )
            broker = FakeBrokerClient(
                initial_snapshot=snapshot_at(now), clock=lambda: now
            )
            service = FullLiveService(
                policy=synthetic_enabled_policy(),
                state=store,
                broker=broker,
                notifications=build_local_outbox(
                    store,
                    ACCOUNT_KEY,
                    JsonlNotificationSink(self.temp / "research-notifications.jsonl"),
                ),
                actions=actions,
                clock=lambda: now,
            )
            result = service.run_once()
            self.assertEqual(actions.calls[:2], ["reconcile", "discover"])
            self.assertTrue(actions.risk_latch_seen_before_research)
            self.assertIn("LIFECYCLE_DISCOVERY_FAILED", result.reconciliation_blockers)
            self.assertEqual(result.mode_after, "PAUSE_NEW_ENTRIES")
            self.assertFalse(result.entries_considered)
            self.assertEqual(
                [call[0] for call in broker.calls], [FakeBrokerClient.SNAPSHOT]
            )

    def test_market_halt_is_a_hard_entry_gate(self) -> None:
        now = datetime(2026, 9, 8, 10, 1, tzinfo=ET)
        cache = MarketDataCache()
        cache.record_completed_bar(completed_bar(now), received_at=now)
        cache.record_quote(quote_at(now, halted=True))
        decision = evidence_decision(cache, now)
        self.assertFalse(decision.eligible)
        self.assertIn("MARKET_HALTED", decision.failures)

    def test_early_close_and_dst_change_lanes_from_verified_calendar(self) -> None:
        calendar = synthetic_enabled_policy().calendar
        early = calendar.session_times(datetime(2026, 11, 27, tzinfo=ET).date())
        self.assertIsNotNone(early)
        assert early is not None
        self.assertEqual(early.entry_cutoff_at.hour, 12)
        self.assertEqual(early.entry_cutoff_at.minute, 30)
        self.assertEqual(early.closeout_start_at.hour, 12)
        self.assertEqual(early.closeout_start_at.minute, 50)
        self.assertEqual(early.flat_deadline_at.hour, 12)
        self.assertEqual(early.flat_deadline_at.minute, 55)
        self.assertEqual(calendar.lane(datetime(2026, 11, 27, 12, 51, tzinfo=ET)), "closeout")
        self.assertEqual(calendar.lane(datetime(2026, 11, 27, 12, 56, tzinfo=ET)), "flat_deadline")
        summer = calendar.session_times(datetime(2026, 9, 8, tzinfo=ET).date())
        winter = calendar.session_times(datetime(2026, 12, 1, tzinfo=ET).date())
        assert summer is not None and winter is not None
        self.assertEqual(summer.open_at.astimezone(UTC).hour, 13)
        self.assertEqual(winter.open_at.astimezone(UTC).hour, 14)

    def test_duplicate_process_and_market_events_cannot_create_duplicate_authority(self) -> None:
        locks = self.temp / "locks"
        first = AccountWriterLock(locks, ACCOUNT_KEY, owner_id="synthetic-owner-1")
        second = AccountWriterLock(locks, ACCOUNT_KEY, owner_id="synthetic-owner-2")
        first.acquire()
        try:
            with self.assertRaises(WriterLockBusy):
                second.acquire()
        finally:
            first.release()

        now = datetime(2026, 9, 8, 10, 1, tzinfo=ET)
        cache = MarketDataCache()
        event = completed_bar(now)
        self.assertEqual(cache.record_completed_bar(event, received_at=now), "inserted")
        self.assertEqual(cache.record_completed_bar(event, received_at=now), "duplicate")
        self.assertEqual(len(cache.bars["XYZ"]), 1)

    def test_stale_crossed_quotes_and_market_data_loss_all_block_entry(self) -> None:
        now = datetime(2026, 9, 8, 10, 1, tzinfo=ET)
        with self.assertRaisesRegex(ValueError, "crossed quote"):
            quote_at(now, bid="10.03", ask="10.02")

        stale = MarketDataCache()
        stale.record_completed_bar(completed_bar(now), received_at=now)
        stale.record_quote(quote_at(now, venue_age_seconds=10))
        stale_decision = evidence_decision(stale, now)
        self.assertFalse(stale_decision.eligible)
        self.assertIn("QUOTE_STALE", stale_decision.failures)

        disconnected = MarketDataCache()
        disconnected.record_completed_bar(completed_bar(now), received_at=now)
        disconnected.record_quote(quote_at(now))
        disconnected.mark_disconnect("synthetic-massive")
        disconnected_decision = evidence_decision(disconnected, now)
        self.assertFalse(disconnected_decision.eligible)
        self.assertIn(
            "MARKET_DATA_DISCONNECTED:synthetic-massive",
            disconnected_decision.failures,
        )

        sequence_gap = MarketDataCache()
        prior = completed_bar(now - timedelta(minutes=1), sequence=1)
        latest = completed_bar(now, sequence=3)
        sequence_gap.record_completed_bar(prior, received_at=now)
        sequence_gap.record_completed_bar(latest, received_at=now)
        sequence_gap.record_quote(quote_at(now))
        gap_decision = evidence_decision(sequence_gap, now)
        self.assertFalse(gap_decision.eligible)
        self.assertIn("MARKET_DATA_SEQUENCE_GAP", gap_decision.failures)


if __name__ == "__main__":
    unittest.main()
