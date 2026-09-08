from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4
from zoneinfo import ZoneInfo

from titan_brain.live.broker import (
    BrokerOperationResult,
    BrokerSide,
    BrokerUnknownSubmission,
    EquityOrderType,
    FakeBrokerClient,
    FakeFault,
    MarketHours,
    OperationStatus,
    OrderCheck,
    OrderRequest,
    RobinhoodBrokerAdapter,
    TimeInForce,
)
from titan_brain.live.execution import (
    EntryExecutionCoordinator,
    ExecutionStatus,
    SafetyExecutionCoordinator,
)
from titan_brain.live.authority import MutationAuthorityDenied
from titan_brain.live.latency import LatencyRecorder
from titan_brain.live.market_data import CompletedBar, MarketDataCache, Quote
from titan_brain.live.models import BrokerOrderState, IntentKind
from titan_brain.live.plans import ExpiringPlan
from titan_brain.live.policy import PolicyBundle, sha256_json
from titan_brain.live.risk_runtime import RiskDecision
from titan_brain.live.state import LiveStateStore


ROOT = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 8, 10, 0, tzinfo=ET)


class MutableClock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class ExplicitTestMutationAuthority:
    """Deliberate test-only capability; production never defaults to this."""

    def __init__(self, *denied: str) -> None:
        self.denied = denied
        self.calls: list[tuple[object, object]] = []

    def require_mutation_authority(self, **kwargs) -> None:
        operation = kwargs["operation"]
        phase = kwargs["phase"]
        self.calls.append((operation, phase))
        if self.denied:
            raise MutationAuthorityDenied(*self.denied)


class MemoryLatencyStore:
    def __init__(self) -> None:
        self.rows: list[tuple[object, ...]] = []

    def record_latency(self, *args) -> None:
        self.rows.append(args)


def enabled_policy() -> PolicyBundle:
    base = PolicyBundle.load(ROOT)
    config = copy.deepcopy(base.config)
    config["authority"]["live_entries_enabled"] = True
    config["authority"]["blockers"] = []
    config["execution"]["supported_unattended_mutation"] = True
    config["execution"]["per_mutation_user_confirmation_required"] = False
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
    policy = replace(
        base,
        config=config,
        config_hash=config_hash,
        policy_hash=policy_hash,
    )
    policy.validate()
    policy.require_activation_ready()
    return policy


def plan_for(policy: PolicyBundle) -> ExpiringPlan:
    return ExpiringPlan.build(
        strategy_id=policy.strategy_id,
        policy_hash=policy.policy_hash,
        config_hash=policy.config_hash,
        account_last4=policy.account_last4,
        symbol="XYZ",
        instrument_id="rh-instrument-xyz",
        setup_id="ORB_BREAKOUT",
        quantity=2,
        entry_limit="10.05",
        structural_stop="9.50",
        targets=["11.00", "12.00"],
        execution_reserve_per_share="0.05",
        market_hours="regular_hours",
        time_in_force="gfd",
        quality_tier="normal",
        completed_bar_end=NOW - timedelta(minutes=1),
        quote_observed_at=NOW - timedelta(seconds=2),
        created_at=NOW - timedelta(seconds=1),
        expires_at=NOW + timedelta(seconds=20),
        source_event_ids=["massive-bar-1", "rh-quote-1"],
        direction="long",
        allow_add=False,
        allow_reentry=False,
    )


def market_cache() -> MarketDataCache:
    cache = MarketDataCache()
    end = NOW - timedelta(minutes=1)
    cache.record_completed_bar(
        CompletedBar.build(
            symbol="XYZ",
            start_at=end - timedelta(minutes=1),
            end_at=end,
            open="9.90",
            high="10.05",
            low="9.85",
            close="10.01",
            volume=800_000,
            sequence=1,
            source_event_id="massive-bar-1",
        ),
        received_at=NOW,
    )
    cache.record_quote(
        Quote.build(
            symbol="XYZ",
            bid="10.00",
            ask="10.02",
            bid_size=500,
            ask_size=500,
            venue_bid_at=NOW - timedelta(seconds=1),
            venue_ask_at=NOW - timedelta(seconds=1),
            observed_at=NOW,
            source="robinhood",
            tradable=True,
        )
    )
    return cache


def allowed_risk(plan: ExpiringPlan) -> RiskDecision:
    return RiskDecision(
        allowed=True,
        failures=(),
        proposal_planned_risk=plan.planned_risk,
        proposal_stress_risk=plan.stress_risk,
        proposal_reserve=plan.execution_reserve,
        remaining_daily_headroom=Decimal("50.00"),
        remaining_portfolio_headroom=Decimal("50.00"),
        remaining_stress_headroom=Decimal("50.00"),
        remaining_buying_power=Decimal("500.00"),
    )


def protection_request(*, quantity: int = 2, stop_price: str = "9.50") -> OrderRequest:
    return OrderRequest(
        account_masked="••••7153",
        symbol="XYZ",
        side=BrokerSide.SELL,
        order_type=EquityOrderType.STOP_MARKET,
        quantity=quantity,
        market_hours=MarketHours.REGULAR,
        time_in_force=TimeInForce.GTC,
        client_ref_id=str(uuid4()),
        stop_price=stop_price,
    )


def exit_request(*, quantity: int = 2) -> OrderRequest:
    return OrderRequest(
        account_masked="••••7153",
        symbol="XYZ",
        side=BrokerSide.SELL,
        order_type=EquityOrderType.LIMIT,
        quantity=quantity,
        market_hours=MarketHours.REGULAR,
        time_in_force=TimeInForce.GFD,
        client_ref_id=str(uuid4()),
        limit_price="10.40",
    )


class InspectingFillBroker(FakeBrokerClient):
    def __init__(self, *, store: LiveStateStore, clock: MutableClock) -> None:
        super().__init__(clock=clock)
        self.store = store
        self.review_saw_durable = False
        self.place_saw_submitting = False

    def review_equity_order(self, request):
        self.review_saw_durable = all(
            len(self.store.rows(f"SELECT * FROM {table}")) == 1
            for table in ("plans", "risk_reservations", "order_intents")
        )
        return super().review_equity_order(request)

    def place_equity_order(self, request, *, review, explicit_confirmation=None):
        row = self.store.rows("SELECT state FROM order_intents")[0]
        self.place_saw_submitting = row["state"] == "SUBMITTING"
        result = super().place_equity_order(
            request,
            review=review,
            explicit_confirmation=explicit_confirmation,
        )
        filled = self.fill_order(result.order.broker_order_id, request.quantity, "10.02")
        return replace(result, order=filled)


class CheckedReviewBroker(FakeBrokerClient):
    def review_equity_order(self, request):
        result = super().review_equity_order(request)
        return replace(
            result,
            order_checks=(
                OrderCheck(
                    code="BROKER_WARNING",
                    severity="warning",
                    message="requires manual attention",
                ),
            ),
        )


class CrashAtPlaceBroker(FakeBrokerClient):
    def place_equity_order(self, request, *, review, explicit_confirmation=None):
        self.calls.append((self.PLACE, request.exact_tuple))
        raise KeyboardInterrupt("simulated process death at mutation boundary")


class InspectingSafetyBroker(FakeBrokerClient):
    def __init__(self, *, store: LiveStateStore, clock: MutableClock) -> None:
        super().__init__(clock=clock)
        self.store = store
        self.review_saw_durable_safety_intent = False
        self.place_saw_safety_submitting = False
        self.cancel_saw_cancel_submitting = False

    def review_equity_order(self, request):
        rows = self.store.rows(
            "SELECT state, reservation_id FROM order_intents "
            "WHERE kind IN ('PROTECTION','EXIT')"
        )
        self.review_saw_durable_safety_intent = bool(rows) and all(
            row["state"] == "PREPARED" and row["reservation_id"] is None
            for row in rows
        )
        return super().review_equity_order(request)

    def place_equity_order(self, request, *, review, explicit_confirmation=None):
        rows = self.store.rows(
            "SELECT state FROM order_intents WHERE kind IN ('PROTECTION','EXIT')"
        )
        self.place_saw_safety_submitting = bool(rows) and all(
            row["state"] == "SUBMITTING" for row in rows
        )
        return super().place_equity_order(
            request,
            review=review,
            explicit_confirmation=explicit_confirmation,
        )

    def cancel_equity_order(
        self,
        account_masked,
        broker_order_id,
        *,
        explicit_confirmation=None,
    ):
        row = self.store.rows(
            "SELECT state, reservation_id FROM order_intents WHERE kind = 'CANCEL'"
        )[-1]
        self.cancel_saw_cancel_submitting = (
            row["state"] == "SUBMITTING" and row["reservation_id"] is None
        )
        return super().cancel_equity_order(
            account_masked,
            broker_order_id,
            explicit_confirmation=explicit_confirmation,
        )


class UnknownAfterCancelBroker(FakeBrokerClient):
    def cancel_equity_order(
        self,
        account_masked,
        broker_order_id,
        *,
        explicit_confirmation=None,
    ):
        super().cancel_equity_order(
            account_masked,
            broker_order_id,
            explicit_confirmation=explicit_confirmation,
        )
        raise BrokerUnknownSubmission("cancel response was lost after acceptance")


class AckWithoutOrderBroker(FakeBrokerClient):
    def place_equity_order(self, request, *, review, explicit_confirmation=None):
        self.calls.append((self.PLACE, request.exact_tuple))
        return BrokerOperationResult(
            operation="place_equity_order",
            status=OperationStatus.ACKNOWLEDGED,
            observed_at=self._now(),
            accepted=True,
            message="accepted but no normalized order evidence",
            order=None,
        )


class MismatchedReviewBroker(FakeBrokerClient):
    def review_equity_order(self, request):
        receipt = super().review_equity_order(request)
        return replace(receipt, request=replace(request, symbol="ABC"))


class EntryExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = LiveStateStore(Path(self.temporary.name) / "live.sqlite3")
        self.clock = MutableClock()
        self.policy = enabled_policy()
        self.plan = plan_for(self.policy)
        self.risk = allowed_risk(self.plan)
        self.authority = ExplicitTestMutationAuthority()

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def coordinator(self, broker) -> EntryExecutionCoordinator:
        return EntryExecutionCoordinator(
            policy=self.policy,
            market_data=market_cache(),
            state=self.store,
            broker=broker,
            authority=self.authority,
            clock=self.clock,
        )

    def test_durable_aggregate_precedes_review_and_submitting_precedes_place(self) -> None:
        broker = InspectingFillBroker(store=self.store, clock=self.clock)
        coordinator = self.coordinator(broker)
        result = coordinator.submit_entry(plan=self.plan, risk_decision=self.risk)
        self.assertEqual(result.status, ExecutionStatus.ACKNOWLEDGED)
        self.assertTrue(broker.review_saw_durable)
        self.assertTrue(broker.place_saw_submitting)
        self.assertEqual(len(self.store.rows("SELECT * FROM plans")), 1)
        self.assertEqual(len(self.store.rows("SELECT * FROM risk_reservations")), 1)
        self.assertEqual(len(self.store.rows("SELECT * FROM broker_orders")), 1)
        self.assertEqual(len(self.store.rows("SELECT * FROM fills")), 1)
        intent = self.store.row("order_intents", "intent_id", result.intent_id)
        self.assertEqual(intent["state"], "ACKNOWLEDGED")
        self.assertEqual(intent["client_ref"], result.client_ref_id)

    def test_ack_records_only_completed_durable_and_submit_stages(self) -> None:
        broker = FakeBrokerClient(clock=self.clock)
        metrics = MemoryLatencyStore()
        ticks = iter(
            (
                1_000_000_000,
                1_002_000_000,
                1_010_000_000,
                1_017_500_000,
            )
        )
        coordinator = EntryExecutionCoordinator(
            policy=self.policy,
            market_data=market_cache(),
            state=self.store,
            broker=broker,
            authority=self.authority,
            clock=self.clock,
            latency=LatencyRecorder(metrics, clock_ns=lambda: next(ticks)),
        )
        result = coordinator.submit_entry(plan=self.plan, risk_decision=self.risk)
        self.assertEqual(result.status, ExecutionStatus.ACKNOWLEDGED)
        self.assertEqual([row[0] for row in metrics.rows], [
            "durable_intent_write",
            "submit_to_ack",
        ])
        self.assertEqual([row[1] for row in metrics.rows], [2.0, 7.5])
        self.assertNotIn("ack_to_fill", {row[0] for row in metrics.rows})
        self.assertNotIn(
            "fill_to_working_protection", {row[0] for row in metrics.rows}
        )

    def test_ambiguous_submission_does_not_fabricate_ack_latency(self) -> None:
        broker = FakeBrokerClient(clock=self.clock)
        broker.inject_fault(
            FakeBrokerClient.PLACE,
            FakeFault.PLACE_UNKNOWN_AFTER_ACCEPT,
        )
        metrics = MemoryLatencyStore()
        ticks = iter((1_000_000_000, 1_001_000_000, 1_010_000_000))
        coordinator = EntryExecutionCoordinator(
            policy=self.policy,
            market_data=market_cache(),
            state=self.store,
            broker=broker,
            authority=self.authority,
            clock=self.clock,
            latency=LatencyRecorder(metrics, clock_ns=lambda: next(ticks)),
        )
        result = coordinator.submit_entry(plan=self.plan, risk_decision=self.risk)
        self.assertEqual(result.status, ExecutionStatus.UNKNOWN)
        self.assertEqual([row[0] for row in metrics.rows], ["durable_intent_write"])

    def test_acknowledged_plan_replay_is_transport_idempotent(self) -> None:
        broker = FakeBrokerClient(clock=self.clock)
        coordinator = self.coordinator(broker)
        first = coordinator.submit_entry(plan=self.plan, risk_decision=self.risk)
        call_count = len(broker.calls)
        second = coordinator.submit_entry(plan=self.plan, risk_decision=self.risk)
        self.assertEqual(first.client_ref_id, second.client_ref_id)
        self.assertEqual(first.intent_id, second.intent_id)
        self.assertEqual(second.status, ExecutionStatus.ACKNOWLEDGED)
        self.assertTrue(second.replay)
        self.assertEqual(len(broker.calls), call_count)

    def test_response_lost_after_acceptance_is_unknown_reserved_and_never_retried(self) -> None:
        broker = FakeBrokerClient(clock=self.clock)
        broker.inject_fault(FakeBrokerClient.PLACE, FakeFault.PLACE_UNKNOWN_AFTER_ACCEPT)
        coordinator = self.coordinator(broker)
        first = coordinator.submit_entry(plan=self.plan, risk_decision=self.risk)
        self.assertEqual(first.status, ExecutionStatus.UNKNOWN)
        self.assertTrue(first.risk_reserved)
        self.assertEqual(
            self.store.row("order_intents", "intent_id", first.intent_id)["state"],
            "UNKNOWN",
        )
        self.assertEqual(
            self.store.row("risk_reservations", "reservation_id", first.reservation_id)["state"],
            "RESERVED",
        )
        self.assertEqual(len(self.store.rows("SELECT * FROM incidents")), 1)
        self.assertEqual(len(self.store.rows("SELECT * FROM notification_outbox")), 1)
        place_calls = sum(call[0] == FakeBrokerClient.PLACE for call in broker.calls)

        replay = coordinator.submit_entry(plan=self.plan, risk_decision=self.risk)
        self.assertEqual(replay.status, ExecutionStatus.UNKNOWN)
        self.assertTrue(replay.replay)
        self.assertEqual(replay.client_ref_id, first.client_ref_id)
        self.assertEqual(
            sum(call[0] == FakeBrokerClient.PLACE for call in broker.calls),
            place_calls,
        )

    def test_restart_with_submitting_intent_becomes_unknown_without_retry(self) -> None:
        broker = CrashAtPlaceBroker(clock=self.clock)
        coordinator = self.coordinator(broker)
        with self.assertRaises(KeyboardInterrupt):
            coordinator.submit_entry(plan=self.plan, risk_decision=self.risk)
        intent = self.store.rows("SELECT * FROM order_intents")[0]
        self.assertEqual(intent["state"], "SUBMITTING")
        place_calls = sum(call[0] == FakeBrokerClient.PLACE for call in broker.calls)

        replay = coordinator.submit_entry(plan=self.plan, risk_decision=self.risk)
        self.assertEqual(replay.status, ExecutionStatus.UNKNOWN)
        self.assertTrue(replay.replay)
        self.assertEqual(
            sum(call[0] == FakeBrokerClient.PLACE for call in broker.calls),
            place_calls,
        )
        self.assertEqual(
            self.store.row("order_intents", "intent_id", replay.intent_id)["state"],
            "UNKNOWN",
        )
        self.assertEqual(len(self.store.rows("SELECT * FROM incidents")), 1)

    def test_unknown_can_be_reconciled_by_persisting_broker_order_and_fill(self) -> None:
        broker = FakeBrokerClient(clock=self.clock)
        broker.inject_fault(FakeBrokerClient.PLACE, FakeFault.PLACE_UNKNOWN_AFTER_ACCEPT)
        coordinator = self.coordinator(broker)
        unknown = coordinator.submit_entry(plan=self.plan, risk_decision=self.risk)
        order = broker.get_account_snapshot("••••7153").equity_orders[0]
        broker.fill_order(order.broker_order_id, self.plan.quantity, "10.02")
        filled = broker.get_account_snapshot("••••7153").equity_orders[0]
        inserted, fill_count = coordinator.persist_order_evidence(
            intent_id=unknown.intent_id,
            order=filled,
        )
        self.assertTrue(inserted)
        self.assertEqual(fill_count, 1)
        self.assertEqual(
            self.store.row("order_intents", "intent_id", unknown.intent_id)["state"],
            "ACKNOWLEDGED",
        )
        self.assertEqual(len(self.store.rows("SELECT * FROM fills")), 1)

    def test_known_rejection_is_distinct_from_unknown(self) -> None:
        broker = FakeBrokerClient(clock=self.clock)
        broker.inject_fault(FakeBrokerClient.PLACE, FakeFault.PLACE_REJECTED)
        result = self.coordinator(broker).submit_entry(
            plan=self.plan,
            risk_decision=self.risk,
        )
        self.assertEqual(result.status, ExecutionStatus.REJECTED)
        self.assertFalse(result.risk_reserved)
        self.assertIn("BROKER_KNOWN_REJECTION", result.failure_codes)
        self.assertEqual(
            self.store.row("order_intents", "intent_id", result.intent_id)["state"],
            "REJECTED",
        )
        self.assertEqual(len(self.store.rows("SELECT * FROM incidents")), 0)
        self.assertEqual(
            self.store.row(
                "risk_reservations", "reservation_id", result.reservation_id
            )["state"],
            "RELEASED",
        )
        calls = len(broker.calls)
        replay = self.coordinator(broker).submit_entry(
            plan=self.plan,
            risk_decision=self.risk,
        )
        self.assertTrue(replay.replay)
        self.assertFalse(replay.risk_reserved)
        self.assertEqual(len(broker.calls), calls)

    def test_known_review_failure_releases_zero_exposure_reservation(self) -> None:
        broker = FakeBrokerClient(clock=self.clock)
        broker.inject_fault(FakeBrokerClient.REVIEW, FakeFault.REVIEW_REJECTED)
        result = self.coordinator(broker).submit_entry(
            plan=self.plan,
            risk_decision=self.risk,
        )
        self.assertEqual(result.status, ExecutionStatus.FAILED)
        self.assertFalse(result.risk_reserved)
        self.assertFalse(any(call[0] == FakeBrokerClient.PLACE for call in broker.calls))
        self.assertEqual(
            self.store.row(
                "risk_reservations", "reservation_id", result.reservation_id
            )["state"],
            "RELEASED",
        )

    def test_any_review_check_fails_closed_before_place(self) -> None:
        broker = CheckedReviewBroker(clock=self.clock)
        result = self.coordinator(broker).submit_entry(
            plan=self.plan,
            risk_decision=self.risk,
        )
        self.assertEqual(result.status, ExecutionStatus.FAILED)
        self.assertFalse(result.risk_reserved)
        self.assertIn("BROKER_REVIEW_CHECK:BROKER_WARNING", result.failure_codes)
        self.assertFalse(any(call[0] == FakeBrokerClient.PLACE for call in broker.calls))
        self.assertEqual(
            self.store.row("order_intents", "intent_id", result.intent_id)["state"],
            "FAILED",
        )
        self.assertEqual(
            self.store.row(
                "risk_reservations", "reservation_id", result.reservation_id
            )["state"],
            "RELEASED",
        )

    def test_risk_mismatch_blocks_before_durable_state_or_broker_call(self) -> None:
        broker = FakeBrokerClient(clock=self.clock)
        mismatched = replace(
            self.risk,
            proposal_stress_risk=self.risk.proposal_stress_risk + Decimal("0.01"),
        )
        result = self.coordinator(broker).submit_entry(
            plan=self.plan,
            risk_decision=mismatched,
        )
        self.assertEqual(result.status, ExecutionStatus.BLOCKED)
        self.assertIn("RISK_STRESS_MISMATCH", result.failure_codes)
        self.assertFalse(broker.calls)
        self.assertEqual(len(self.store.rows("SELECT * FROM order_intents")), 0)

    def test_robinhood_adapter_and_checked_in_config_both_remain_blocked(self) -> None:
        adapter = RobinhoodBrokerAdapter()
        enabled_result = self.coordinator(adapter).submit_entry(
            plan=self.plan,
            risk_decision=self.risk,
        )
        self.assertEqual(enabled_result.status, ExecutionStatus.BLOCKED)
        self.assertIn("UNATTENDED_BROKER_WRITES_UNSUPPORTED", enabled_result.failure_codes)
        self.assertIn("ADVANCED_ORDER_READ_UNSUPPORTED", enabled_result.failure_codes)
        self.assertFalse(adapter.blocked_mutations)
        self.assertEqual(len(self.store.rows("SELECT * FROM order_intents")), 0)

        checked_in = PolicyBundle.load(ROOT)
        checked_plan = plan_for(checked_in)
        fake = FakeBrokerClient(clock=self.clock)
        checked_result = EntryExecutionCoordinator(
            policy=checked_in,
            market_data=market_cache(),
            state=self.store,
            broker=fake,
            authority=self.authority,
            clock=self.clock,
        ).submit_entry(plan=checked_plan, risk_decision=allowed_risk(checked_plan))
        self.assertEqual(checked_result.status, ExecutionStatus.BLOCKED)
        self.assertIn(
            "LIVE_ENTRIES_DISABLED_IN_SIGNED_CONFIG",
            checked_result.failure_codes,
        )
        self.assertFalse(fake.calls)


class SafetyExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = LiveStateStore(Path(self.temporary.name) / "live.sqlite3")
        self.clock = MutableClock()
        self.policy = enabled_policy()
        self.plan = plan_for(self.policy)
        self.risk = allowed_risk(self.plan)
        self.authority = ExplicitTestMutationAuthority()

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def entry(self, broker: FakeBrokerClient):
        return EntryExecutionCoordinator(
            policy=self.policy,
            market_data=market_cache(),
            state=self.store,
            broker=broker,
            authority=self.authority,
            clock=self.clock,
        ).submit_entry(plan=self.plan, risk_decision=self.risk)

    def safety(self, broker) -> SafetyExecutionCoordinator:
        return SafetyExecutionCoordinator(
            policy=self.policy,
            state=self.store,
            broker=broker,
            authority=self.authority,
            clock=self.clock,
        )

    def test_protection_is_durable_before_review_and_submitting_before_place(self) -> None:
        broker = InspectingSafetyBroker(store=self.store, clock=self.clock)
        entry = self.entry(broker)
        result = self.safety(broker).submit_sell(
            plan_id=self.plan.plan_id,
            kind=IntentKind.PROTECTION,
            operation_key="protect-fill-0001",
            request=protection_request(),
        )
        self.assertEqual(result.status, ExecutionStatus.ACKNOWLEDGED)
        self.assertTrue(result.requires_reconciliation)
        self.assertTrue(broker.review_saw_durable_safety_intent)
        self.assertTrue(broker.place_saw_safety_submitting)
        intent = self.store.row("order_intents", "intent_id", result.intent_id)
        self.assertEqual(intent["kind"], IntentKind.PROTECTION.value)
        self.assertIsNone(intent["reservation_id"])
        self.assertEqual(intent["state"], "ACKNOWLEDGED")
        self.assertNotEqual(result.client_ref_id, protection_request().client_ref_id)
        self.assertEqual(
            self.store.row(
                "risk_reservations", "reservation_id", entry.reservation_id
            )["state"],
            "RESERVED",
        )

    def test_protection_records_durable_write_and_exact_ack_only(self) -> None:
        broker = FakeBrokerClient(clock=self.clock)
        self.entry(broker)
        metrics = MemoryLatencyStore()
        ticks = iter((100_000_000, 103_000_000, 110_000_000, 114_000_000))
        coordinator = SafetyExecutionCoordinator(
            policy=self.policy,
            state=self.store,
            broker=broker,
            authority=self.authority,
            clock=self.clock,
            latency=LatencyRecorder(metrics, clock_ns=lambda: next(ticks)),
        )
        result = coordinator.submit_sell(
            plan_id=self.plan.plan_id,
            kind=IntentKind.PROTECTION,
            operation_key="protect-fill-latency",
            request=protection_request(),
        )
        self.assertEqual(result.status, ExecutionStatus.ACKNOWLEDGED)
        self.assertEqual(
            [(row[0], row[1]) for row in metrics.rows],
            [("durable_intent_write", 3.0), ("submit_to_ack", 4.0)],
        )

    def test_exit_replay_uses_stable_uuid_and_never_repeats_transport(self) -> None:
        broker = FakeBrokerClient(clock=self.clock)
        self.entry(broker)
        coordinator = self.safety(broker)
        first = coordinator.submit_sell(
            plan_id=self.plan.plan_id,
            kind=IntentKind.EXIT,
            operation_key="managed-close-1",
            request=exit_request(),
        )
        call_count = len(broker.calls)
        replay = coordinator.submit_sell(
            plan_id=self.plan.plan_id,
            kind=IntentKind.EXIT,
            operation_key="managed-close-1",
            request=exit_request(),
        )
        self.assertEqual(first.status, ExecutionStatus.ACKNOWLEDGED)
        self.assertEqual(replay.status, ExecutionStatus.ACKNOWLEDGED)
        self.assertTrue(replay.replay)
        self.assertEqual(replay.intent_id, first.intent_id)
        self.assertEqual(replay.client_ref_id, first.client_ref_id)
        self.assertEqual(len(broker.calls), call_count)

    def test_unknown_protection_stays_reserved_opens_incident_and_is_not_retried(self) -> None:
        broker = FakeBrokerClient(clock=self.clock)
        entry = self.entry(broker)
        broker.inject_fault(
            FakeBrokerClient.PLACE,
            FakeFault.PLACE_UNKNOWN_AFTER_ACCEPT,
        )
        coordinator = self.safety(broker)
        first = coordinator.submit_sell(
            plan_id=self.plan.plan_id,
            kind=IntentKind.PROTECTION,
            operation_key="protect-fill-0001",
            request=protection_request(),
        )
        self.assertEqual(first.status, ExecutionStatus.UNKNOWN)
        self.assertTrue(first.exposure_reserved)
        self.assertEqual(
            self.store.row("order_intents", "intent_id", first.intent_id)["state"],
            "UNKNOWN",
        )
        self.assertEqual(
            self.store.row(
                "risk_reservations", "reservation_id", entry.reservation_id
            )["state"],
            "RESERVED",
        )
        self.assertEqual(len(self.store.rows("SELECT * FROM incidents")), 1)
        self.assertEqual(
            len(self.store.rows("SELECT * FROM notification_outbox")), 1
        )
        place_calls = sum(call[0] == FakeBrokerClient.PLACE for call in broker.calls)
        replay = coordinator.submit_sell(
            plan_id=self.plan.plan_id,
            kind=IntentKind.PROTECTION,
            operation_key="protect-fill-0001",
            request=protection_request(),
        )
        self.assertEqual(replay.status, ExecutionStatus.UNKNOWN)
        self.assertTrue(replay.replay)
        self.assertEqual(
            sum(call[0] == FakeBrokerClient.PLACE for call in broker.calls),
            place_calls,
        )

    def test_protection_stop_widening_and_review_warning_fail_closed(self) -> None:
        broker = FakeBrokerClient(clock=self.clock)
        self.entry(broker)
        widened = self.safety(broker).submit_sell(
            plan_id=self.plan.plan_id,
            kind=IntentKind.PROTECTION,
            operation_key="widened-stop",
            request=protection_request(stop_price="9.40"),
        )
        self.assertEqual(widened.status, ExecutionStatus.BLOCKED)
        self.assertTrue(
            any(code.startswith("PROTECTION_TUPLE_INVALID") for code in widened.failure_codes)
        )

        checked = CheckedReviewBroker(clock=self.clock)
        warning = self.safety(checked).submit_sell(
            plan_id=self.plan.plan_id,
            kind=IntentKind.PROTECTION,
            operation_key="review-warning",
            request=protection_request(),
        )
        self.assertEqual(warning.status, ExecutionStatus.FAILED)
        self.assertIn("BROKER_REVIEW_CHECK:BROKER_WARNING", warning.failure_codes)
        self.assertFalse(
            any(call[0] == FakeBrokerClient.PLACE for call in checked.calls)
        )

        mismatch = MismatchedReviewBroker(clock=self.clock)
        mismatched_review = self.safety(mismatch).submit_sell(
            plan_id=self.plan.plan_id,
            kind=IntentKind.PROTECTION,
            operation_key="review-tuple-mismatch",
            request=protection_request(),
        )
        self.assertEqual(mismatched_review.status, ExecutionStatus.FAILED)
        self.assertIn(
            "BROKER_REVIEW_TUPLE_MISMATCH",
            mismatched_review.failure_codes,
        )
        self.assertFalse(
            any(call[0] == FakeBrokerClient.PLACE for call in mismatch.calls)
        )

    def test_acceptance_without_exact_order_evidence_is_unknown(self) -> None:
        setup_broker = FakeBrokerClient(clock=self.clock)
        entry = self.entry(setup_broker)
        broker = AckWithoutOrderBroker(clock=self.clock)
        result = self.safety(broker).submit_sell(
            plan_id=self.plan.plan_id,
            kind=IntentKind.EXIT,
            operation_key="evidence-only-ack",
            request=exit_request(),
        )
        self.assertEqual(result.status, ExecutionStatus.UNKNOWN)
        self.assertIn("AMBIGUOUS_SAFETY_PLACE_RESULT", result.failure_codes)
        self.assertEqual(
            self.store.row("order_intents", "intent_id", result.intent_id)["state"],
            "UNKNOWN",
        )
        self.assertEqual(
            self.store.row(
                "risk_reservations", "reservation_id", entry.reservation_id
            )["state"],
            "RESERVED",
        )

    def test_known_safety_rejection_is_distinct_from_unknown(self) -> None:
        broker = FakeBrokerClient(clock=self.clock)
        self.entry(broker)
        broker.inject_fault(FakeBrokerClient.PLACE, FakeFault.PLACE_REJECTED)
        result = self.safety(broker).submit_sell(
            plan_id=self.plan.plan_id,
            kind=IntentKind.PROTECTION,
            operation_key="rejected-protection",
            request=protection_request(),
        )
        self.assertEqual(result.status, ExecutionStatus.REJECTED)
        self.assertIn("BROKER_KNOWN_REJECTION", result.failure_codes)
        self.assertEqual(
            self.store.row("order_intents", "intent_id", result.intent_id)["state"],
            "REJECTED",
        )
        self.assertEqual(len(self.store.rows("SELECT * FROM incidents")), 0)

    def test_robinhood_unattended_safety_order_remains_blocked(self) -> None:
        fake = FakeBrokerClient(clock=self.clock)
        self.entry(fake)
        adapter = RobinhoodBrokerAdapter()
        result = self.safety(adapter).submit_sell(
            plan_id=self.plan.plan_id,
            kind=IntentKind.PROTECTION,
            operation_key="protect-fill-0001",
            request=protection_request(),
        )
        self.assertEqual(result.status, ExecutionStatus.BLOCKED)
        self.assertIn("UNATTENDED_BROKER_WRITES_UNSUPPORTED", result.failure_codes)
        self.assertIn("REVIEW_CONFIRMATION_REQUIRED", result.failure_codes)
        self.assertFalse(adapter.blocked_mutations)

    def test_cancel_acceptance_is_durable_nonterminal_and_replay_does_not_retry(self) -> None:
        broker = InspectingSafetyBroker(store=self.store, clock=self.clock)
        self.entry(broker)
        target = broker.get_account_snapshot("••••7153").equity_orders[0]
        self.clock.advance(1)
        coordinator = self.safety(broker)
        result = coordinator.cancel_order(plan_id=self.plan.plan_id, target=target)
        self.assertEqual(result.status, ExecutionStatus.ACKNOWLEDGED)
        self.assertTrue(result.requires_reconciliation)
        self.assertIn(
            "CANCEL_ACCEPTED_PENDING_RECONCILIATION",
            result.failure_codes,
        )
        self.assertTrue(broker.cancel_saw_cancel_submitting)
        cancel_intent = self.store.row(
            "order_intents", "intent_id", result.intent_id
        )
        self.assertEqual(cancel_intent["state"], "ACKNOWLEDGED")
        self.assertIsNone(cancel_intent["reservation_id"])
        cancel_calls = sum(call[0] == FakeBrokerClient.CANCEL for call in broker.calls)
        replay = coordinator.cancel_order(plan_id=self.plan.plan_id, target=target)
        self.assertTrue(replay.replay)
        self.assertEqual(
            sum(call[0] == FakeBrokerClient.CANCEL for call in broker.calls),
            cancel_calls,
        )

        pending = broker.get_account_snapshot("••••7153").equity_orders[0]
        self.clock.advance(1)
        terminal = replace(
            pending,
            state=BrokerOrderState.CANCELLED,
            broker_updated_at=self.clock(),
            received_at=self.clock(),
        )
        reconciled = coordinator.reconcile_cancel(
            intent_id=result.intent_id,
            order=terminal,
        )
        self.assertEqual(reconciled.status, ExecutionStatus.ACKNOWLEDGED)
        self.assertTrue(reconciled.requires_reconciliation)
        self.assertEqual(
            self.store.row("order_intents", "intent_id", result.intent_id)["state"],
            "RECONCILED",
        )

    def test_unknown_cancel_is_reserved_and_never_blindly_retried(self) -> None:
        broker = UnknownAfterCancelBroker(clock=self.clock)
        entry = self.entry(broker)
        target = broker.get_account_snapshot("••••7153").equity_orders[0]
        self.clock.advance(1)
        coordinator = self.safety(broker)
        first = coordinator.cancel_order(plan_id=self.plan.plan_id, target=target)
        self.assertEqual(first.status, ExecutionStatus.UNKNOWN)
        self.assertEqual(
            self.store.row("order_intents", "intent_id", first.intent_id)["state"],
            "UNKNOWN",
        )
        self.assertEqual(
            self.store.row(
                "risk_reservations", "reservation_id", entry.reservation_id
            )["state"],
            "RESERVED",
        )
        cancel_calls = sum(call[0] == FakeBrokerClient.CANCEL for call in broker.calls)
        replay = coordinator.cancel_order(plan_id=self.plan.plan_id, target=target)
        self.assertEqual(replay.status, ExecutionStatus.UNKNOWN)
        self.assertEqual(
            sum(call[0] == FakeBrokerClient.CANCEL for call in broker.calls),
            cancel_calls,
        )
        self.assertEqual(len(self.store.rows("SELECT * FROM incidents")), 1)

    def test_cancel_requires_local_ownership_and_unattended_capability(self) -> None:
        broker = FakeBrokerClient(clock=self.clock)
        self.entry(broker)
        target = broker.get_account_snapshot("••••7153").equity_orders[0]
        self.clock.advance(1)

        confirming = FakeBrokerClient(
            clock=self.clock,
            require_explicit_confirmation=True,
        )
        blocked = self.safety(confirming).cancel_order(
            plan_id=self.plan.plan_id,
            target=target,
        )
        self.assertEqual(blocked.status, ExecutionStatus.BLOCKED)
        self.assertIn("UNATTENDED_BROKER_WRITES_UNSUPPORTED", blocked.failure_codes)
        self.assertIn("CANCEL_CONFIRMATION_REQUIRED", blocked.failure_codes)
        self.assertFalse(confirming.calls)

        alien = replace(target, broker_order_id="not-locally-owned")
        ownership = self.safety(broker).cancel_order(
            plan_id=self.plan.plan_id,
            target=alien,
        )
        self.assertEqual(ownership.status, ExecutionStatus.BLOCKED)
        self.assertIn("CANCEL_TARGET_NOT_LOCALLY_OWNED", ownership.failure_codes)

    def test_cancel_rejection_with_fill_race_is_known_not_unknown(self) -> None:
        broker = FakeBrokerClient(clock=self.clock)
        self.entry(broker)
        target = broker.get_account_snapshot("••••7153").equity_orders[0]
        self.clock.advance(1)
        broker.inject_fault(FakeBrokerClient.CANCEL, FakeFault.CANCEL_FILL_RACE)
        result = self.safety(broker).cancel_order(
            plan_id=self.plan.plan_id,
            target=target,
        )
        self.assertEqual(result.status, ExecutionStatus.REJECTED)
        self.assertIn("BROKER_CANCEL_REJECTED", result.failure_codes)
        self.assertTrue(result.requires_reconciliation)
        self.assertEqual(
            self.store.row("broker_orders", "broker_order_id", target.broker_order_id)[
                "state"
            ],
            BrokerOrderState.FILLED.value,
        )
        self.assertEqual(len(self.store.rows("SELECT * FROM fills")), 1)
        self.assertEqual(len(self.store.rows("SELECT * FROM incidents")), 0)


if __name__ == "__main__":
    unittest.main()
