from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import os
import tempfile
import threading
from types import SimpleNamespace
import unittest

from titan_brain.live.broker import (
    AccountSnapshot,
    BrokerOrderState,
    BrokerSide,
    ClientRefLookupResult,
    EquityOrderType,
    FakeBrokerClient,
    FundsSnapshot,
    MarketHours,
    OrderSnapshot,
    PositionSnapshot,
    TimeInForce,
)
from titan_brain.live.broker.robinhood import RobinhoodBrokerAdapter
from titan_brain.live.control import ControlInbox, HmacControlAuthenticator
from titan_brain.live.calendar import NEW_YORK
from titan_brain.live.lifecycle_actions import (
    LifecycleReconcileResult,
    TargetExitAssessment,
)
from titan_brain.live.notifications import (
    DeliveryAssurance,
    JsonlNotificationSink,
    NotificationRoute,
    destination_fingerprint,
)
from titan_brain.live.models import (
    BrokerOrder,
    ExpiringPlan,
    Fill,
    IntentKind,
    IntentState,
    OrderIntent,
    RiskReservation,
)
from titan_brain.live.policy import PolicyBundle
from titan_brain.live.protection import ensure_durable_entry_fill_obligations
from titan_brain.live.service import (
    AcquiredServiceWriterAuthority,
    FullLiveService,
    ServiceRunner,
    TickResult,
    build_enqueue_only_outbox,
    build_local_outbox,
)
from titan_brain.live.state import LiveStateStore, StateConflict, object_hash
from titan_brain.live.writer_lock import AccountWriterLock
from tests.live_activation_support import activate_canonical_runtime


UTC = timezone.utc
NOW = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)


def account_snapshot() -> AccountSnapshot:
    return AccountSnapshot(
        account_masked="••••7153",
        observed_at=NOW,
        received_at=NOW,
        account_state="active",
        account_type="limited_margin",
        funds=FundsSnapshot(
            total_value=Decimal("1000.00"),
            cash=Decimal("1000.00"),
            buying_power=Decimal("1000.00"),
            unleveraged_buying_power=Decimal("1000.00"),
            unsettled_funds=Decimal("250.00"),
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
        risk_evidence_source="deterministic-test-ledger",
        risk_evidence_as_of=NOW,
    )


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.temp = Path(self.temporary.name)
        self.root = Path(__file__).resolve().parents[1]
        self.policy = PolicyBundle.load(self.root)
        self.store = LiveStateStore(self.temp / "state.sqlite3")
        self.store.initialize_runtime(
            runtime_id=self.policy.runtime_id,
            account_key="ending-7153",
            release_manifest_hash="a" * 64,
            config_hash=self.policy.config_hash,
            policy_hash=self.policy.policy_hash,
            initialized_at=NOW,
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def service(self, broker):
        return FullLiveService(
            policy=self.policy,
            state=self.store,
            broker=broker,
            notifications=build_local_outbox(
                self.store,
                "ending-7153",
                JsonlNotificationSink(self.temp / "notifications.jsonl"),
            ),
            clock=lambda: NOW,
        )

    @staticmethod
    def closeout_book(when: datetime, count: int = 3) -> AccountSnapshot:
        positions = tuple(
            PositionSnapshot(
                symbol=f"X{index}",
                quantity=Decimal("1"),
                sellable_quantity=Decimal("0"),
                held_for_sells=Decimal("1"),
                average_price=Decimal("10"),
            )
            for index in range(count)
        )
        orders = tuple(
            OrderSnapshot(
                broker_order_id=f"stop-{index}",
                account_masked="••••7153",
                symbol=f"X{index}",
                side=BrokerSide.SELL,
                order_type=EquityOrderType.STOP_MARKET,
                state=BrokerOrderState.CONFIRMED,
                requested_quantity=Decimal("1"),
                cumulative_filled_quantity=Decimal("0"),
                market_hours=MarketHours.REGULAR,
                time_in_force=TimeInForce.GTC,
                broker_updated_at=when,
                received_at=when,
                stop_price=Decimal("9.50"),
                client_ref_id=f"00000000-0000-4000-8000-{index:012d}",
            )
            for index in range(count)
        )
        return replace(
            account_snapshot(),
            observed_at=when,
            received_at=when,
            equity_positions=positions,
            equity_orders=orders,
            risk_evidence_as_of=when,
        )

    def seed_completed_entry(
        self, *, when: datetime, fill_count: int
    ) -> None:
        client_ref = "00000000-0000-4000-8000-000000000555"
        order_tuple = {
            "account_key": "ending-7153",
            "account_masked": "••••7153",
            "symbol": "TEST",
            "side": "buy",
            "order_type": "limit",
            "quantity": fill_count,
            "market_hours": "regular_hours",
            "time_in_force": "gfd",
            "limit_price": "10.00",
            "stop_price": None,
            "client_ref_id": client_ref,
        }
        plan = ExpiringPlan(
            plan_id="completed-entry-plan",
            account_key="ending-7153",
            strategy_id="titan-full-live-test",
            symbol="TEST",
            setup_id="FIRST_PULLBACK",
            quantity=fill_count,
            limit_price=Decimal("10.00"),
            structural_stop=Decimal("9.50"),
            market_hours="regular_hours",
            time_in_force="gfd",
            evidence_cutoff_at=when - timedelta(seconds=4),
            created_at=when - timedelta(seconds=3),
            expires_at=when + timedelta(minutes=1),
            policy_hash="a" * 64,
            config_hash="b" * 64,
            evidence_hash="c" * 64,
        )
        reservation = RiskReservation(
            reservation_id="completed-entry-reservation",
            plan_id=plan.plan_id,
            account_key=plan.account_key,
            planned_risk=Decimal("2.50"),
            stress_risk=Decimal("3.00"),
            execution_reserve=Decimal("0.50"),
            notional=Decimal("50.00"),
            created_at=when - timedelta(seconds=2),
        )
        intent = OrderIntent(
            intent_id="completed-entry-intent",
            plan_id=plan.plan_id,
            reservation_id=reservation.reservation_id,
            account_key=plan.account_key,
            kind=IntentKind.ENTRY,
            client_ref=client_ref,
            order_tuple=order_tuple,
            tuple_hash=object_hash(order_tuple),
            created_at=when - timedelta(seconds=1),
            acknowledgement_deadline_at=when + timedelta(seconds=10),
        )
        self.store.prepare_submission(
            plan=plan, reservation=reservation, intent=intent
        )
        self.store.transition_intent(
            intent.intent_id,
            IntentState.SUBMITTING,
            occurred_at=when - timedelta(milliseconds=500),
        )
        self.store.record_broker_order(
            BrokerOrder(
                broker_order_id="completed-entry-order",
                intent_id=intent.intent_id,
                account_key=intent.account_key,
                state=BrokerOrderState.FILLED,
                quantity=fill_count,
                cumulative_filled_quantity=fill_count,
                revision=1,
                broker_updated_at=when,
                received_at=when,
                raw_hash="d" * 64,
            )
        )
        for index in range(fill_count):
            self.store.record_fill(
                Fill(
                    fill_id=f"completed-entry-fill-{index}",
                    broker_order_id="completed-entry-order",
                    account_key="ending-7153",
                    quantity=1,
                    price=Decimal("10.00"),
                    executed_at=when,
                    received_at=when,
                )
            )
        ensure_durable_entry_fill_obligations(
            self.store,
            intent_id=intent.intent_id,
            account_key="ending-7153",
        )

    def test_closeout_feasibility_starts_early_before_margin_is_exhausted(self) -> None:
        now = datetime(2026, 9, 8, 15, 48, tzinfo=NEW_YORK)
        book = self.closeout_book(now)
        service = self.service(
            FakeBrokerClient(initial_snapshot=book, clock=lambda: now)
        )

        result = service._closeout_feasibility(snapshot=book, now=now)

        self.assertEqual(result.obligation_count, 6)
        self.assertEqual(result.required_cycles, 13)
        self.assertEqual(result.cycle_budget_seconds, 30.0)
        self.assertEqual(result.estimated_seconds, 390.0)
        self.assertEqual(result.seconds_to_flat_deadline, 420.0)
        self.assertTrue(result.start_early)
        self.assertFalse(result.margin_exhausted)

    def test_closeout_feasibility_ignores_terminal_entry_ack_when_flat_next_session(self) -> None:
        completed_at = datetime(2026, 9, 8, 15, 0, tzinfo=NEW_YORK)
        self.seed_completed_entry(when=completed_at, fill_count=2)
        next_session = datetime(2026, 9, 9, 10, 0, tzinfo=NEW_YORK)
        flat = replace(
            account_snapshot(),
            observed_at=next_session,
            received_at=next_session,
            risk_evidence_as_of=next_session,
        )
        service = self.service(
            FakeBrokerClient(initial_snapshot=flat, clock=lambda: next_session)
        )

        result = service._closeout_feasibility(
            snapshot=flat, now=next_session
        )

        self.assertEqual(result.obligation_count, 0)
        self.assertEqual(result.obligation_facts, ())
        self.assertFalse(result.start_early)

    def test_many_partial_fills_count_uncreated_stops_until_closeout_owns_lane(self) -> None:
        now = datetime(2026, 9, 8, 15, 48, tzinfo=NEW_YORK)
        self.seed_completed_entry(when=now - timedelta(minutes=1), fill_count=5)
        exposed = replace(
            account_snapshot(),
            observed_at=now,
            received_at=now,
            equity_positions=(
                PositionSnapshot(
                    symbol="TEST",
                    quantity=Decimal("5"),
                    sellable_quantity=Decimal("5"),
                    average_price=Decimal("10"),
                ),
            ),
            risk_evidence_as_of=now,
        )
        service = self.service(
            FakeBrokerClient(initial_snapshot=exposed, clock=lambda: now)
        )

        before_closeout = service._closeout_feasibility(
            snapshot=exposed, now=now
        )
        owned_by_closeout = service._closeout_feasibility(
            snapshot=exposed,
            now=now,
            closeout_owns_exposure=True,
        )

        self.assertEqual(before_closeout.obligation_count, 6)
        self.assertEqual(
            sum(
                fact.startswith("uncreated_protection:")
                for fact in before_closeout.obligation_facts
            ),
            5,
        )
        self.assertTrue(before_closeout.start_early)
        self.assertEqual(owned_by_closeout.obligation_count, 1)
        self.assertFalse(
            any(
                fact.startswith("uncreated_protection:")
                for fact in owned_by_closeout.obligation_facts
            )
        )

    def test_closeout_feasibility_margin_blocker_is_durable_and_resolves_flat(self) -> None:
        now = datetime(2026, 9, 8, 15, 49, tzinfo=NEW_YORK)
        book = self.closeout_book(now)
        service = self.service(
            FakeBrokerClient(initial_snapshot=book, clock=lambda: now)
        )
        feasibility = service._closeout_feasibility(snapshot=book, now=now)
        self.assertTrue(feasibility.margin_exhausted)

        service._record_closeout_feasibility_incident(feasibility, now)

        rows = self.store.rows(
            "SELECT * FROM incidents WHERE category=?",
            ("CLOSEOUT_DEADLINE_FEASIBILITY_MARGIN_EXHAUSTED",),
        )
        self.assertEqual(len(rows), 1)
        self.assertIn("not_fill_guarantee", str(rows[0]["detail_json"]))
        self.assertTrue(service._resolve_closeout_feasibility_incident(now))
        self.assertIsNotNone(
            self.store.rows(
                "SELECT resolved_at FROM incidents WHERE incident_id=?",
                (rows[0]["incident_id"],),
            )[0]["resolved_at"]
        )

    def test_service_escalates_to_managed_closeout_on_early_feasibility_threshold(self) -> None:
        now = datetime(2026, 9, 8, 15, 48, tzinfo=NEW_YORK)
        book = self.closeout_book(now)
        _, _owner = activate_canonical_runtime(
            self.store,
            created_at=now - timedelta(seconds=2),
            activated_at=now - timedelta(seconds=1),
            expires_at=now + timedelta(minutes=5),
        )

        class Actions:
            def __init__(inner_self):
                inner_self.protect_symbols = []
                inner_self.closeout_symbols = []

            def reconcile(self, **_kwargs):
                return LifecycleReconcileResult()

            def protect(inner_self, *, decision, **_kwargs):
                inner_self.protect_symbols.append(decision.symbol)
                return "NO_MUTATION:PROTECTION_RECONCILIATION_REQUIRED"

            def target_exits(self, **_kwargs):
                return TargetExitAssessment()

            def closeout(inner_self, *, decision, **_kwargs):
                inner_self.closeout_symbols.append(decision.symbol)
                return "NO_MUTATION:WAIT_CANCEL_CONFIRMATION"

            def discover_and_execute(self, **_kwargs):
                raise AssertionError("early closeout must suppress discovery")

        lifecycle_actions = Actions()
        service = FullLiveService(
            policy=self.policy,
            state=self.store,
            broker=FakeBrokerClient(initial_snapshot=book, clock=lambda: now),
            notifications=build_local_outbox(
                self.store,
                "ending-7153",
                JsonlNotificationSink(self.temp / "notifications.jsonl"),
            ),
            actions=lifecycle_actions,
            clock=lambda: now,
        )

        result = service.run_once()

        self.assertIn("CLOSEOUT_FEASIBILITY_EARLY_START", result.actions)
        self.assertIn("MANAGED_CLOSEOUT", result.actions)
        self.assertFalse(result.entries_considered)
        self.assertEqual(self.store.runtime_status()["mode"], "MANAGED_CLOSEOUT")
        self.assertEqual(lifecycle_actions.protect_symbols, [])
        self.assertEqual(lifecycle_actions.closeout_symbols, ["X0"])
        self.assertIn("CLOSEOUT_DEFER_ADDITIONAL_SYMBOLS:2", result.actions)

    def test_target_exit_executes_only_one_symbol_per_broker_snapshot(self) -> None:
        now = NOW
        book = self.closeout_book(now, count=2)
        activate_canonical_runtime(
            self.store,
            created_at=now - timedelta(seconds=2),
            activated_at=now - timedelta(seconds=1),
            expires_at=now + timedelta(minutes=5),
        )
        decisions: tuple = ()

        class Actions:
            def __init__(self):
                self.closeout_symbols = []

            def reconcile(self, **_kwargs):
                return LifecycleReconcileResult()

            def protect(self, **_kwargs):
                return "NO_MUTATION:PROTECTION_RECONCILIATION_REQUIRED"

            def target_exits(inner_self, **_kwargs):
                return TargetExitAssessment(
                    decisions=decisions,
                    actions=("TARGET_EXIT_LATCH_ACTIVE:X0", "TARGET_EXIT_LATCH_ACTIVE:X1"),
                )

            def closeout(self, *, decision, **_kwargs):
                self.closeout_symbols.append(decision.symbol)
                return "NO_MUTATION:WAIT_CANCEL_CONFIRMATION"

            def discover_and_execute(self, **_kwargs):
                raise AssertionError("target exit must suppress discovery")

        actions = Actions()
        service = FullLiveService(
            policy=self.policy,
            state=self.store,
            broker=FakeBrokerClient(initial_snapshot=book, clock=lambda: now),
            notifications=build_local_outbox(
                self.store,
                "ending-7153",
                JsonlNotificationSink(self.temp / "notifications.jsonl"),
            ),
            actions=actions,
            clock=lambda: now,
        )
        decisions = service._plan_closeout(book)

        result = service.run_once()

        self.assertEqual(actions.closeout_symbols, ["X0"])
        self.assertIn("TARGET_EXIT_DEFER_ADDITIONAL_SYMBOLS:1", result.actions)
        self.assertFalse(result.entries_considered)

    def test_hard_kill_owns_closeout_before_protection_during_regular_lane(self) -> None:
        activate_canonical_runtime(
            self.store,
            created_at=NOW - timedelta(seconds=2),
            activated_at=NOW - timedelta(seconds=1),
            expires_at=NOW + timedelta(minutes=5),
        )
        exposed = replace(
            account_snapshot(),
            daily_realized_pnl=Decimal("-90.00"),
            equity_positions=tuple(
                PositionSnapshot(
                    symbol=f"K{index}",
                    quantity=Decimal("1"),
                    sellable_quantity=Decimal("1"),
                    average_price=Decimal("10"),
                )
                for index in range(2)
            ),
        )

        class Actions:
            def __init__(inner_self):
                inner_self.protect_symbols = []
                inner_self.closeout_symbols = []

            def reconcile(inner_self, **_kwargs):
                return LifecycleReconcileResult()

            def protect(inner_self, *, decision, **_kwargs):
                inner_self.protect_symbols.append(decision.symbol)
                return "PROTECTION:ACKNOWLEDGED"

            def closeout(inner_self, *, decision, **_kwargs):
                inner_self.closeout_symbols.append(decision.symbol)
                return "EXIT:ACKNOWLEDGED"

            def target_exits(inner_self, **_kwargs):
                raise AssertionError("hard-kill owns the closeout lane")

            def discover_and_execute(inner_self, **_kwargs):
                raise AssertionError("hard-kill suppresses discovery")

        lifecycle_actions = Actions()
        service = FullLiveService(
            policy=self.policy,
            state=self.store,
            broker=FakeBrokerClient(initial_snapshot=exposed, clock=lambda: NOW),
            notifications=build_local_outbox(
                self.store,
                "ending-7153",
                JsonlNotificationSink(self.temp / "notifications.jsonl"),
            ),
            actions=lifecycle_actions,
            clock=lambda: NOW,
        )

        result = service.run_once()

        self.assertIn("HARD_DAILY_LOSS_KILL", result.reconciliation_blockers)
        self.assertEqual(lifecycle_actions.protect_symbols, [])
        self.assertEqual(lifecycle_actions.closeout_symbols, ["K0"])
        self.assertIn("CLOSEOUT_DEFER_ADDITIONAL_SYMBOLS:1", result.actions)
        self.assertIn(
            "DEFER_NEW_PROTECTION_OBLIGATIONS_TO_CLOSEOUT", result.actions
        )

    def test_protection_executes_only_one_symbol_per_broker_snapshot(self) -> None:
        activate_canonical_runtime(
            self.store,
            created_at=NOW - timedelta(seconds=2),
            activated_at=NOW - timedelta(seconds=1),
            expires_at=NOW + timedelta(minutes=5),
        )
        exposed = replace(
            account_snapshot(),
            equity_positions=tuple(
                PositionSnapshot(
                    symbol=f"P{index}",
                    quantity=Decimal("1"),
                    sellable_quantity=Decimal("1"),
                    average_price=Decimal("10"),
                )
                for index in range(2)
            ),
        )

        class Actions:
            def __init__(inner_self):
                inner_self.protect_symbols = []

            def reconcile(inner_self, **_kwargs):
                return LifecycleReconcileResult()

            def protect(inner_self, *, decision, **_kwargs):
                inner_self.protect_symbols.append(decision.symbol)
                return "PROTECTION:ACKNOWLEDGED"

            def target_exits(inner_self, **_kwargs):
                return TargetExitAssessment()

            def closeout(inner_self, **_kwargs):
                raise AssertionError("ordinary protection is not closeout")

            def discover_and_execute(inner_self, **_kwargs):
                return ()

        lifecycle_actions = Actions()
        service = FullLiveService(
            policy=self.policy,
            state=self.store,
            broker=FakeBrokerClient(initial_snapshot=exposed, clock=lambda: NOW),
            notifications=build_local_outbox(
                self.store,
                "ending-7153",
                JsonlNotificationSink(self.temp / "notifications.jsonl"),
            ),
            actions=lifecycle_actions,
            clock=lambda: NOW,
        )

        result = service.run_once()

        self.assertEqual(lifecycle_actions.protect_symbols, ["P0"])
        self.assertIn("PROTECTION_DEFER_ADDITIONAL_SYMBOLS:1", result.actions)
        self.assertEqual(result.next_poll_delay_seconds, 0.0)

    def test_entry_risk_evidence_failure_blocks_entry_but_preserves_protection(self) -> None:
        activate_canonical_runtime(
            self.store,
            created_at=NOW - timedelta(seconds=2),
            activated_at=NOW - timedelta(seconds=1),
            expires_at=NOW + timedelta(minutes=5),
        )
        exposed = replace(
            account_snapshot(),
            equity_positions=(
                PositionSnapshot(
                    symbol="RISK",
                    quantity=Decimal("1"),
                    sellable_quantity=Decimal("1"),
                    average_price=Decimal("10"),
                ),
            ),
            weekly_realized_pnl=None,
            peak_equity=None,
            weekly_realized_pnl_complete=False,
            peak_equity_complete=False,
            risk_evidence_source="ibkr:reqPnL.realizedPnL:current-day",
        )

        class Actions:
            def __init__(inner_self):
                inner_self.protected = []

            def reconcile(inner_self, **_kwargs):
                return LifecycleReconcileResult()

            def protect(inner_self, *, decision, **_kwargs):
                inner_self.protected.append(decision.symbol)
                return "PROTECTION:ACKNOWLEDGED"

            def target_exits(inner_self, **_kwargs):
                return TargetExitAssessment()

            def closeout(inner_self, **_kwargs):
                raise AssertionError("regular-session protection is not closeout")

            def discover_and_execute(inner_self, **_kwargs):
                raise AssertionError("incomplete entry risk evidence must block discovery")

        actions = Actions()
        service = FullLiveService(
            policy=self.policy,
            state=self.store,
            broker=FakeBrokerClient(initial_snapshot=exposed, clock=lambda: NOW),
            notifications=build_local_outbox(
                self.store,
                "ending-7153",
                JsonlNotificationSink(self.temp / "notifications.jsonl"),
            ),
            actions=actions,
            clock=lambda: NOW,
        )

        result = service.run_once()

        self.assertIsNotNone(result.snapshot_id)
        self.assertEqual(actions.protected, ["RISK"])
        self.assertIn(
            "WEEKLY_REALIZED_PNL_INCOMPLETE", result.reconciliation_blockers
        )
        self.assertIn("PEAK_EQUITY_INCOMPLETE", result.reconciliation_blockers)
        self.assertFalse(result.entries_considered)

    def test_entry_risk_evidence_failure_preserves_mandatory_closeout(self) -> None:
        closeout_now = datetime(2026, 9, 8, 15, 50, tzinfo=NEW_YORK)
        activate_canonical_runtime(
            self.store,
            created_at=closeout_now - timedelta(seconds=2),
            activated_at=closeout_now - timedelta(seconds=1),
            expires_at=closeout_now + timedelta(minutes=5),
        )
        exposed = replace(
            account_snapshot(),
            observed_at=closeout_now,
            received_at=closeout_now,
            equity_positions=(
                PositionSnapshot(
                    symbol="RISK",
                    quantity=Decimal("1"),
                    sellable_quantity=Decimal("1"),
                    average_price=Decimal("10"),
                ),
            ),
            weekly_realized_pnl=None,
            peak_equity=None,
            weekly_realized_pnl_complete=False,
            peak_equity_complete=False,
            risk_evidence_source="ibkr:reqPnL.realizedPnL:current-day",
            risk_evidence_as_of=closeout_now,
        )

        class Actions:
            def __init__(inner_self):
                inner_self.closed = []

            def reconcile(inner_self, **_kwargs):
                return LifecycleReconcileResult()

            def protect(inner_self, **_kwargs):
                raise AssertionError("closeout owns exposed shares")

            def target_exits(inner_self, **_kwargs):
                raise AssertionError("mandatory closeout owns target exits")

            def closeout(inner_self, *, decision, **_kwargs):
                inner_self.closed.append(decision.symbol)
                return "EXIT:ACKNOWLEDGED"

            def discover_and_execute(inner_self, **_kwargs):
                raise AssertionError("closeout must suppress discovery")

        actions = Actions()
        service = FullLiveService(
            policy=self.policy,
            state=self.store,
            broker=FakeBrokerClient(
                initial_snapshot=exposed, clock=lambda: closeout_now
            ),
            notifications=build_local_outbox(
                self.store,
                "ending-7153",
                JsonlNotificationSink(self.temp / "notifications.jsonl"),
            ),
            actions=actions,
            clock=lambda: closeout_now,
        )

        result = service.run_once()

        self.assertIsNotNone(result.snapshot_id)
        self.assertEqual(actions.closed, ["RISK"])
        self.assertIn("MANAGED_CLOSEOUT", result.actions)
        self.assertIn(
            "WEEKLY_REALIZED_PNL_INCOMPLETE", result.reconciliation_blockers
        )
        self.assertIn("PEAK_EQUITY_INCOMPLETE", result.reconciliation_blockers)
        self.assertFalse(result.entries_considered)

    def test_exact_client_ref_positive_is_carried_into_account_reconciliation(self) -> None:
        client_ref = "00000000-0000-4000-8000-000000000001"
        recovered = OrderSnapshot(
            broker_order_id="recovered-terminal-order",
            account_masked="••••7153",
            symbol="XYZ",
            side=BrokerSide.BUY,
            order_type=EquityOrderType.LIMIT,
            state=BrokerOrderState.CANCELLED,
            requested_quantity=Decimal("1"),
            cumulative_filled_quantity=Decimal("0"),
            market_hours=MarketHours.REGULAR,
            time_in_force=TimeInForce.GFD,
            broker_updated_at=NOW,
            received_at=NOW,
            limit_price=Decimal("10.00"),
            client_ref_id=client_ref,
        )
        lookup = ClientRefLookupResult(
            account_masked="••••7153",
            requested_client_refs=(client_ref,),
            found_orders=(recovered,),
            confirmed_absent_client_refs=(),
            observed_at=NOW,
            received_at=NOW,
            complete=True,
        )

        merged = FullLiveService._merge_client_ref_lookup(
            account_snapshot(), lookup
        )

        self.assertEqual(merged.equity_orders, (recovered,))

    def test_exact_ref_recovery_includes_crash_left_submitting_intents(self) -> None:
        client_ref = "00000000-0000-4000-8000-000000000002"

        class SubmittingIntentState:
            def __init__(self) -> None:
                self.query = ""

            def rows(self, query, parameters):
                self.query = query
                self.asserted_parameters = parameters
                if "'SUBMITTING'" not in query:
                    return ()
                return ({"client_ref": client_ref},)

        broker = FakeBrokerClient(
            initial_snapshot=account_snapshot(), clock=lambda: NOW
        )
        service = self.service(broker)
        state = SubmittingIntentState()
        service.state = state  # type: ignore[assignment]

        result = service._lookup_unknown_client_refs(snapshot=account_snapshot())

        self.assertIsNotNone(result)
        self.assertEqual(result.requested_client_refs, (client_ref,))
        self.assertIn("state IN ('SUBMITTING','UNKNOWN')", state.query)
        self.assertEqual(broker.calls[-1], (broker.LOOKUP, (client_ref,)))

    def test_service_rejects_false_negative_from_eventually_consistent_lookup(self) -> None:
        client_ref = "00000000-0000-4000-8000-000000000003"
        fake = FakeBrokerClient(
            initial_snapshot=account_snapshot(), clock=lambda: NOW
        )
        coverage = replace(
            fake.capabilities.order_coverage,
            negative_client_ref_results_authoritative=False,
        )

        class LyingNegativeBroker:
            capabilities = replace(fake.capabilities, order_coverage=coverage)

            def lookup_equity_orders_by_client_ref(self, _account, requested):
                return ClientRefLookupResult(
                    account_masked="••••7153",
                    requested_client_refs=requested,
                    found_orders=(),
                    confirmed_absent_client_refs=requested,
                    observed_at=NOW,
                    received_at=NOW,
                    complete=True,
                )

        class UnknownState:
            def rows(self, _query, _parameters):
                return ({"client_ref": client_ref},)

        service = self.service(LyingNegativeBroker())
        service.state = UnknownState()  # type: ignore[assignment]
        with self.assertRaisesRegex(ValueError, "authoritative negative"):
            service._lookup_unknown_client_refs(snapshot=account_snapshot())

    def test_account_read_completion_time_drives_tick_freshness_and_lane(self) -> None:
        times = iter((NOW, NOW + timedelta(seconds=3), NOW + timedelta(seconds=3)))

        service = FullLiveService(
            policy=self.policy,
            state=self.store,
            broker=FakeBrokerClient(
                initial_snapshot=account_snapshot(), clock=lambda: NOW
            ),
            notifications=build_local_outbox(
                self.store,
                "ending-7153",
                JsonlNotificationSink(self.temp / "notifications.jsonl"),
            ),
            clock=lambda: next(times),
        )

        result = service.run_once()

        self.assertEqual(result.observed_at, NOW + timedelta(seconds=3))

    def test_paused_tick_reconciles_before_protection_outbox_and_discovery_gate(self) -> None:
        broker = FakeBrokerClient(initial_snapshot=account_snapshot(), clock=lambda: NOW)
        result = self.service(broker).run_once()
        self.assertIsNotNone(result.snapshot_id)
        self.assertEqual(result.mode_after, "PAUSED")
        self.assertFalse(result.entries_considered)
        self.assertIn("LIVE_ENTRIES_DISABLED_IN_SIGNED_CONFIG", result.reconciliation_blockers)
        self.assertLess(result.actions.index("RECONCILE_ACCOUNT"), result.actions.index("INGEST_CONFIRMED_FILLS"))
        self.assertLess(
            result.actions.index("VERIFY_OR_ESTABLISH_PROTECTION"),
            result.actions.index("FLUSH_OUTBOX"),
        )
        self.assertLess(result.actions.index("BLOCK_DISCOVERY"), result.actions.index("FLUSH_OUTBOX"))
        broker_rows = self.store.rows("SELECT * FROM broker_snapshots")
        self.assertEqual(len(broker_rows), 1)
        self.assertEqual(broker_rows[0]["realized_pnl_reconciled"], 1)

    def test_premarket_analysis_runs_after_broker_reconciliation_and_deduplicates_slot(self) -> None:
        now = datetime(2026, 9, 8, 8, 0, tzinfo=NEW_YORK)
        book = replace(
            account_snapshot(),
            observed_at=now,
            received_at=now,
            risk_evidence_as_of=now,
        )

        class AnalysisActions:
            def __init__(inner_self) -> None:
                inner_self.calls = []

            def analyze_premarket(
                inner_self, *, now, last_completed_slot
            ):
                # This proves the service performed and persisted the
                # account-first reconciliation before invoking analysis.
                self.assertEqual(
                    len(self.store.rows("SELECT * FROM broker_snapshots")), 1
                )
                inner_self.calls.append(last_completed_slot)
                scheduled = datetime(2026, 9, 8, 8, 0, tzinfo=NEW_YORK)
                schedule = SimpleNamespace(
                    due=last_completed_slot is None,
                    scheduled_for=scheduled,
                    execution_authority=False,
                    approved_to_buy=False,
                )
                if last_completed_slot is not None:
                    return SimpleNamespace(
                        status="NOT_DUE",
                        schedule=schedule,
                        analysis_id=None,
                        observed_at=now,
                        candidates=(),
                        blockers=(),
                        execution_authority=False,
                        approved_to_buy=False,
                    )
                candidate = SimpleNamespace(
                    rank=1,
                    symbol="XYZ",
                    source_plan_id="prepared-xyz",
                    instrument_evidence_id="instrument-xyz",
                    quote_observed_at=now,
                    latest_completed_bar_end=now - timedelta(minutes=1),
                    hard_gate_failures=(),
                    deferred_execution_gates=("remaining_capacity",),
                    execution_authority=False,
                    approved_to_buy=False,
                )
                return SimpleNamespace(
                    status="COMPLETED",
                    schedule=schedule,
                    analysis_id="analysis-exact-slot",
                    observed_at=now,
                    candidates=(candidate,),
                    blockers=(),
                    execution_authority=False,
                    approved_to_buy=False,
                )

            def reconcile(inner_self, **_kwargs):
                return LifecycleReconcileResult()

            def protect(inner_self, **_kwargs):
                return "NO_MUTATION"

            def target_exits(inner_self, **_kwargs):
                return TargetExitAssessment()

            def closeout(inner_self, **_kwargs):
                return "NO_MUTATION"

            def discover_and_execute(inner_self, **_kwargs):
                raise AssertionError("premarket analysis cannot enter")

        analysis_actions = AnalysisActions()
        service = FullLiveService(
            policy=self.policy,
            state=self.store,
            broker=FakeBrokerClient(initial_snapshot=book, clock=lambda: now),
            notifications=build_local_outbox(
                self.store,
                "ending-7153",
                JsonlNotificationSink(self.temp / "notifications.jsonl"),
            ),
            actions=analysis_actions,
            clock=lambda: now,
        )

        result = service.run_once()
        replay = service._premarket_analysis_actions(now=now)

        self.assertTrue(
            any(item.startswith("PREMARKET_ANALYSIS:COMPLETED:") for item in result.actions)
        )
        self.assertEqual(
            replay, ("PREMARKET_ANALYSIS:CURRENT_SLOT_ALREADY_COMPLETE",)
        )
        self.assertEqual(analysis_actions.calls, [None])
        rows = self.store.rows(
            "SELECT * FROM audit_events WHERE event_type=?",
            ("PREMARKET_ANALYSIS_COMPLETED",),
        )
        self.assertEqual(len(rows), 1)
        self.assertIn('"execution_authority":false', rows[0]["payload_json"])
        attempts = self.store.rows(
            "SELECT * FROM audit_events WHERE event_type=?",
            ("PREMARKET_ANALYSIS_ATTEMPT",),
        )
        self.assertEqual(len(attempts), 1)
        self.assertIn('"status":"COMPLETED"', attempts[0]["payload_json"])
        self.assertFalse(result.entries_considered)

    def test_premarket_analysis_authority_claim_fails_without_durable_slot_or_entry_blocker(self) -> None:
        premarket = datetime(2026, 9, 8, 8, 0, tzinfo=NEW_YORK)

        class UnsafeAnalysis:
            def analyze_premarket(self, **_kwargs):
                return SimpleNamespace(
                    status="COMPLETED",
                    execution_authority=True,
                    approved_to_buy=True,
                )

        service = self.service(
            FakeBrokerClient(initial_snapshot=account_snapshot(), clock=lambda: NOW)
        )
        service.actions = UnsafeAnalysis()  # type: ignore[assignment]

        failed = service._premarket_analysis_actions(now=premarket)
        regular = service._premarket_analysis_actions(now=NOW)

        self.assertEqual(failed, ("PREMARKET_ANALYSIS:FAILED:ValueError",))
        self.assertEqual(regular, ())
        self.assertEqual(
            self.store.rows(
                "SELECT * FROM audit_events WHERE event_type=?",
                ("PREMARKET_ANALYSIS_COMPLETED",),
            ),
            [],
        )
        attempts = self.store.rows(
            "SELECT * FROM audit_events WHERE event_type=?",
            ("PREMARKET_ANALYSIS_ATTEMPT",),
        )
        self.assertEqual(len(attempts), 1)
        self.assertIn('"status":"FAILED"', attempts[0]["payload_json"])
        self.assertEqual(
            self.store.rows(
                "SELECT * FROM incidents WHERE category LIKE 'PREMARKET_ANALYSIS%'"
            ),
            [],
        )

    def test_premarket_blocked_slot_uses_durable_bounded_backoff_across_restart(self) -> None:
        slot = datetime(2026, 9, 8, 8, 0, tzinfo=NEW_YORK)

        class BlockedAnalysis:
            def __init__(inner_self) -> None:
                inner_self.calls = []

            def analyze_premarket(
                inner_self, *, now, last_completed_slot
            ):
                inner_self.calls.append(now)
                return SimpleNamespace(
                    status="BLOCKED",
                    schedule=SimpleNamespace(
                        due=True,
                        scheduled_for=slot,
                        execution_authority=False,
                        approved_to_buy=False,
                    ),
                    analysis_id=None,
                    observed_at=now,
                    candidates=(),
                    blockers=("MARKET_PROVIDER_UNAVAILABLE",),
                    execution_authority=False,
                    approved_to_buy=False,
                )

        analysis = BlockedAnalysis()
        service = self.service(
            FakeBrokerClient(initial_snapshot=account_snapshot(), clock=lambda: NOW)
        )
        service.actions = analysis  # type: ignore[assignment]

        first = service._premarket_analysis_actions(now=slot)
        quick_retry = service._premarket_analysis_actions(
            now=slot + timedelta(seconds=2)
        )

        restarted = self.service(
            FakeBrokerClient(initial_snapshot=account_snapshot(), clock=lambda: NOW)
        )
        restarted.actions = analysis  # type: ignore[assignment]
        restart_retry = restarted._premarket_analysis_actions(
            now=slot + timedelta(seconds=2)
        )
        second = restarted._premarket_analysis_actions(
            now=slot + timedelta(minutes=5)
        )
        third = restarted._premarket_analysis_actions(
            now=slot + timedelta(minutes=15)
        )
        exhausted = restarted._premarket_analysis_actions(
            now=slot + timedelta(minutes=20)
        )

        self.assertEqual(
            first,
            ("PREMARKET_ANALYSIS:BLOCKED:MARKET_PROVIDER_UNAVAILABLE",),
        )
        self.assertTrue(quick_retry[0].startswith("PREMARKET_ANALYSIS:RETRY_BACKOFF:"))
        self.assertEqual(restart_retry, quick_retry)
        self.assertEqual(
            second,
            ("PREMARKET_ANALYSIS:BLOCKED:MARKET_PROVIDER_UNAVAILABLE",),
        )
        self.assertEqual(
            third,
            ("PREMARKET_ANALYSIS:BLOCKED:MARKET_PROVIDER_UNAVAILABLE",),
        )
        self.assertTrue(exhausted[0].startswith("PREMARKET_ANALYSIS:RETRY_EXHAUSTED:"))
        self.assertEqual(
            analysis.calls,
            [
                slot,
                slot + timedelta(minutes=5),
                slot + timedelta(minutes=15),
            ],
        )
        attempts = self.store.rows(
            "SELECT * FROM audit_events WHERE event_type=? ORDER BY sequence",
            ("PREMARKET_ANALYSIS_ATTEMPT",),
        )
        self.assertEqual(len(attempts), 3)
        self.assertTrue(
            all('"status":"BLOCKED"' in row["payload_json"] for row in attempts)
        )

    def test_production_coordinator_enqueues_but_never_delivers_provider_outbox(self) -> None:
        class FailedBroker:
            capabilities = FakeBrokerClient(
                initial_snapshot=account_snapshot(), clock=lambda: NOW
            ).capabilities

            def get_account_snapshot(self, _account_masked):
                raise RuntimeError("synthetic broker outage")

        service = FullLiveService(
            policy=self.policy,
            state=self.store,
            broker=FailedBroker(),
            notifications=build_enqueue_only_outbox(
                self.store, "ending-7153"
            ),
            clock=lambda: NOW,
        )
        result = service.run_once()
        self.assertIn(
            "DEFER_OUTBOX_TO_INDEPENDENT_NOTIFICATION_WORKER", result.actions
        )
        self.assertEqual(result.notification_sent, 0)
        row = self.store.rows(
            "SELECT state,claim_owner,delivery_receipt FROM notification_outbox"
        )[0]
        self.assertEqual(row["state"], "PENDING")
        self.assertIsNone(row["claim_owner"])
        self.assertIsNone(row["delivery_receipt"])
        self.assertFalse((self.temp / "notifications.jsonl").exists())

    def test_independent_notification_worker_health_is_a_continuous_entry_gate(self) -> None:
        route = NotificationRoute(
            provider="gmail",
            destination_fingerprint=destination_fingerprint(
                "gmail", "owner@example.invalid"
            ),
            route_version="service-health-v1",
            required_assurance=DeliveryAssurance.PROVIDER_ACCEPTED,
        )
        self.store.acquire_notification_worker_lease(
            account_key="ending-7153",
            worker_id="service-health-worker",
            process_id=os.getpid(),
            route_id=route.route_id,
            provider=route.provider,
            destination_fingerprint=route.destination_fingerprint,
            route_version=route.route_version,
            acquired_at=NOW,
        )
        service = FullLiveService(
            policy=self.policy,
            state=self.store,
            broker=FakeBrokerClient(
                initial_snapshot=account_snapshot(), clock=lambda: NOW
            ),
            notifications=build_enqueue_only_outbox(
                self.store, "ending-7153"
            ),
            notification_route=route,
            clock=lambda: NOW,
        )
        self.assertEqual(service._notification_entry_blockers(NOW), ())
        self.assertIn(
            "NOTIFICATION_WORKER_HEARTBEAT_STALE_OR_FUTURE",
            service._notification_entry_blockers(NOW + timedelta(seconds=16)),
        )

    def test_notification_and_entry_provider_failures_never_suppress_closeout(self) -> None:
        activate_canonical_runtime(
            self.store,
            created_at=NOW - timedelta(seconds=4),
            expires_at=NOW + timedelta(minutes=5),
            activated_at=NOW - timedelta(seconds=2),
        )
        self.store.set_runtime_mode(
            "MANAGED_CLOSEOUT", occurred_at=NOW - timedelta(seconds=1), reason="test"
        )
        exposed = replace(
            account_snapshot(),
            equity_positions=(
                PositionSnapshot(
                    symbol="XYZ",
                    quantity=Decimal("2"),
                    sellable_quantity=Decimal("2"),
                    average_price=Decimal("10"),
                ),
            ),
        )

        class RaisingOutbox:
            def enqueue(self, *_args, **_kwargs):
                raise OSError("synthetic outbox failure")

            def drain(self, _now):
                return 0, 0

        class ActionSpy:
            def __init__(self):
                self.calls = []

            def reconcile(self, **_kwargs):
                self.calls.append("reconcile")
                return LifecycleReconcileResult()

            def protect(self, **_kwargs):
                self.calls.append("protect")
                return "PROTECTION:ACKNOWLEDGED"

            def closeout(self, **_kwargs):
                self.calls.append("closeout")
                return "NO_MUTATION:WAIT_EXISTING_EXIT"

            def discover_and_execute(self, **_kwargs):
                self.calls.append("discover")
                return ()

        actions = ActionSpy()
        service = FullLiveService(
            policy=self.policy,
            state=self.store,
            broker=FakeBrokerClient(initial_snapshot=exposed, clock=lambda: NOW),
            notifications=RaisingOutbox(),  # type: ignore[arg-type]
            actions=actions,
            entry_path_blockers=(
                "DISCOVERY_COMPOSITION_UNAVAILABLE:CredentialUnavailable",
            ),
            clock=lambda: NOW,
        )

        result = service.run_once()

        self.assertEqual(actions.calls, ["reconcile", "closeout"])
        self.assertIn(
            "DEFER_NEW_PROTECTION_OBLIGATIONS_TO_CLOSEOUT", result.actions
        )
        self.assertIn(
            "PROTECTION_DEFERRED_TO_CLOSEOUT:symbols=1", result.actions
        )
        self.assertNotIn("discover", actions.calls)
        self.assertIn(
            "DISCOVERY_COMPOSITION_UNAVAILABLE:CredentialUnavailable",
            result.reconciliation_blockers,
        )
        self.assertEqual(result.next_poll_delay_seconds, 1.0)
        self.assertEqual(
            self.store.runtime_status()["mode"], "MANAGED_CLOSEOUT"
        )
        incidents = self.store.rows(
            "SELECT category FROM incidents WHERE account_key=?",
            ("ending-7153",),
        )
        self.assertIn(
            "NOTIFICATION_ENQUEUE_FAILED",
            {str(row["category"]) for row in incidents},
        )

    def test_broker_consequence_requests_fresh_snapshot_before_normal_sleep(self) -> None:
        self.assertEqual(
            FullLiveService._priority_poll_delay(
                actions=("ENTRY:UNKNOWN:plan-1",),
                blockers=(),
                protection=(),
                closeout=(),
            ),
            0.0,
        )
        self.assertEqual(
            FullLiveService._priority_poll_delay(
                actions=(),
                blockers=("UNRESOLVED_ENTRY_INTENT",),
                protection=(),
                closeout=(),
            ),
            1.0,
        )

    def test_runner_reconciles_priority_tick_before_ordinary_wait(self) -> None:
        class RecordingEvent:
            def __init__(self):
                self.stopped = False
                self.waits = []

            def is_set(self):
                return self.stopped

            def set(self):
                self.stopped = True

            def wait(self, delay):
                self.waits.append(delay)
                self.stopped = True

        class SequenceService:
            def __init__(self, state):
                self.state = state
                self.account_key = "ending-7153"
                self.calls = 0
                self.runner = None

            def _now(self):
                return NOW

            def run_once(self):
                self.calls += 1
                if self.calls == 2:
                    self.runner.stop_event.set()
                return TickResult(
                    observed_at=NOW,
                    mode_before="PAUSED",
                    mode_after="PAUSED",
                    lane="regular_entry",
                    snapshot_id=None,
                    reconciliation_blockers=(),
                    protection=(),
                    closeout=(),
                    actions=(),
                    entries_considered=False,
                    notification_sent=0,
                    notification_failed=0,
                    next_poll_delay_seconds=(0.0 if self.calls == 1 else None),
                )

        service = SequenceService(self.store)
        runner = ServiceRunner(
            service=service,  # type: ignore[arg-type]
            lock=AccountWriterLock(
                self.temp / "priority-locks",
                "ending-7153",
                owner_id="priority-runner",
            ),
            interval_seconds=60,
        )
        event = RecordingEvent()
        runner.stop_event = event  # type: ignore[assignment]
        service.runner = runner

        runner.run()

        self.assertEqual(service.calls, 2)
        # The only ordinary wait is reached after the second tick has already
        # stopped the loop; there was no wait between the consequence and the
        # fresh reconciliation tick.
        self.assertEqual(event.waits, [60])

    def test_flat_risk_release_hook_runs_only_for_ingestible_snapshots(self) -> None:
        valid = account_snapshot()
        invalid = replace(
            account_snapshot(),
            observed_at=NOW + timedelta(minutes=1),
            received_at=NOW + timedelta(minutes=1),
            auth_point_in_time=False,
        )
        capabilities = FakeBrokerClient(
            initial_snapshot=valid, clock=lambda: NOW
        ).capabilities

        class SequenceBroker:
            def __init__(self):
                self.snapshots = [valid, invalid]
                self.capabilities = capabilities

            def get_account_snapshot(self, _account):
                return self.snapshots.pop(0)

        calls: list[dict[str, object]] = []

        def track_release(**kwargs):
            calls.append(dict(kwargs))
            return ()

        self.store.release_reservations_after_flat_snapshot = track_release
        service = self.service(SequenceBroker())
        first = service.run_once()
        second = service.run_once()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["account_key"], "ending-7153")
        self.assertEqual(calls[0]["snapshot_id"], first.snapshot_id)
        self.assertIn("SNAPSHOT_FROM_FUTURE", second.reconciliation_blockers)
        self.assertNotIn(
            "RISK_RESERVATION_RELEASE_GUARD_FAILED",
            first.reconciliation_blockers,
        )

    def test_missing_daemon_broker_transport_fails_closed_and_notifies(self) -> None:
        result = self.service(RobinhoodBrokerAdapter()).run_once()
        self.assertFalse(result.healthy)
        self.assertFalse(result.entries_considered)
        self.assertEqual(result.mode_after, "PAUSED")
        self.assertIn("RECONCILIATION_INCIDENT", result.reconciliation_blockers)
        rows = self.store.rows("SELECT * FROM notification_outbox")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "DELIVERED")
        self.assertTrue((self.temp / "notifications.jsonl").exists())

    def test_runner_owns_and_releases_both_kernel_and_database_writer_locks(self) -> None:
        service = self.service(FakeBrokerClient(initial_snapshot=account_snapshot(), clock=lambda: NOW))
        lock = AccountWriterLock(self.temp / "locks", "ending-7153", owner_id="runner-test")
        result = ServiceRunner(service=service, lock=lock, interval_seconds=0.01).run(once=True)
        self.assertIsNotNone(result)
        self.assertFalse(lock.held)
        lease = self.store.rows(
            "SELECT * FROM account_writer_lease WHERE account_key=?", ("ending-7153",)
        )[0]
        self.assertIsNotNone(lease["released_at"])

    def test_runner_consumes_preacquired_writer_authority_without_reacquiring(self) -> None:
        service = self.service(
            FakeBrokerClient(initial_snapshot=account_snapshot(), clock=lambda: NOW)
        )
        lock = AccountWriterLock(
            self.temp / "preacquired-locks",
            "ending-7153",
            owner_id="preacquired-runner-test",
        )
        authority = AcquiredServiceWriterAuthority.acquire(
            state=self.store,
            account_key="ending-7153",
            lock=lock,
            acquired_at=NOW,
        )
        generation = authority.generation
        self.assertTrue(lock.held)
        self.assertEqual(lock.writer_lease_generation, generation)

        result = ServiceRunner(
            service=service,
            lock=lock,
            interval_seconds=0.01,
            writer_authority=authority,
        ).run(once=True)

        self.assertIsNotNone(result)
        self.assertTrue(authority.released)
        self.assertFalse(lock.held)
        lease = self.store.rows(
            "SELECT * FROM account_writer_lease WHERE account_key=?",
            ("ending-7153",),
        )[0]
        self.assertEqual(lease["generation"], generation)
        self.assertIsNotNone(lease["released_at"])

    def test_runner_renews_writer_lease_while_service_iteration_is_blocked(self) -> None:
        lock = AccountWriterLock(
            self.temp / "heartbeat-locks",
            "ending-7153",
            owner_id="heartbeat-runner-test",
        )
        authority = AcquiredServiceWriterAuthority.acquire(
            state=self.store,
            account_key="ending-7153",
            lock=lock,
            acquired_at=NOW,
        )
        original_heartbeat = authority.heartbeat
        renewed_during_iteration = threading.Event()
        heartbeat_calls = 0

        def tracked_heartbeat(observed_at):
            nonlocal heartbeat_calls
            heartbeat_calls += 1
            original_heartbeat(observed_at)
            if heartbeat_calls >= 2:
                renewed_during_iteration.set()

        authority.heartbeat = tracked_heartbeat

        class SlowService:
            state = self.store
            account_key = "ending-7153"

            @staticmethod
            def _now():
                return NOW

            @staticmethod
            def run_once():
                if not renewed_during_iteration.wait(2):
                    raise AssertionError("writer lease was not renewed in flight")
                return SimpleNamespace(next_poll_delay_seconds=None)

        result = ServiceRunner(
            service=SlowService(),
            lock=lock,
            interval_seconds=0.01,
            writer_authority=authority,
        ).run(once=True)

        self.assertIsNotNone(result)
        self.assertGreaterEqual(heartbeat_calls, 2)
        self.assertTrue(authority.released)
        self.assertFalse(lock.held)

    def test_runner_serializes_lease_timestamp_capture_with_heartbeat(self) -> None:
        lock = AccountWriterLock(
            self.temp / "ordered-heartbeat-locks",
            "ending-7153",
            owner_id="ordered-heartbeat-runner-test",
        )
        authority = AcquiredServiceWriterAuthority.acquire(
            state=self.store,
            account_key="ending-7153",
            lock=lock,
            acquired_at=NOW,
        )
        original_heartbeat = authority.heartbeat
        background_committed = threading.Event()
        main_renewal_delayed = threading.Event()

        def force_commit_inversion(observed_at):
            if (
                threading.current_thread() is threading.main_thread()
                and observed_at == NOW + timedelta(seconds=1)
            ):
                main_renewal_delayed.set()
                background_committed.wait(1)
            original_heartbeat(observed_at)
            if observed_at == NOW + timedelta(seconds=2):
                background_committed.set()

        authority.heartbeat = force_commit_inversion

        class InversionService:
            state = self.store
            account_key = "ending-7153"

            def __init__(self):
                self.main_clock_calls = 0

            def _now(self):
                if threading.current_thread().name == (
                    "titan-account-writer-lease-heartbeat"
                ):
                    return NOW + timedelta(seconds=2)
                self.main_clock_calls += 1
                if self.main_clock_calls == 1:
                    return NOW
                if self.main_clock_calls == 2:
                    return NOW + timedelta(seconds=1)
                return NOW + timedelta(seconds=3)

            @staticmethod
            def run_once():
                return SimpleNamespace(next_poll_delay_seconds=None)

        result = ServiceRunner(
            service=InversionService(),  # type: ignore[arg-type]
            lock=lock,
            interval_seconds=0.2,
            writer_authority=authority,
        ).run(once=True)

        self.assertIsNotNone(result)
        self.assertTrue(main_renewal_delayed.is_set())
        self.assertTrue(background_committed.is_set())
        self.assertTrue(authority.released)
        self.assertFalse(lock.held)

    def test_runner_raises_when_background_heartbeat_fails_during_poll_sleep(self) -> None:
        lock = AccountWriterLock(
            self.temp / "failed-heartbeat-locks",
            "ending-7153",
            owner_id="failed-heartbeat-runner-test",
        )
        authority = AcquiredServiceWriterAuthority.acquire(
            state=self.store,
            account_key="ending-7153",
            lock=lock,
            acquired_at=NOW,
        )
        original_heartbeat = authority.heartbeat
        background_failed = threading.Event()

        def fail_background_heartbeat(observed_at):
            if threading.current_thread().name == (
                "titan-account-writer-lease-heartbeat"
            ):
                background_failed.set()
                raise RuntimeError("forced background heartbeat failure")
            original_heartbeat(observed_at)

        authority.heartbeat = fail_background_heartbeat

        class SleepingService:
            state = self.store
            account_key = "ending-7153"

            @staticmethod
            def _now():
                return NOW

            @staticmethod
            def run_once():
                return SimpleNamespace(next_poll_delay_seconds=None)

        runner = ServiceRunner(
            service=SleepingService(),  # type: ignore[arg-type]
            lock=lock,
            interval_seconds=0.2,
            writer_authority=authority,
        )
        with self.assertRaises(StateConflict) as raised:
            runner.run()

        self.assertEqual(
            str(raised.exception), "service writer lease heartbeat failed"
        )
        self.assertIsInstance(raised.exception.__cause__, RuntimeError)
        self.assertTrue(background_failed.is_set())
        self.assertTrue(authority.released)
        self.assertFalse(lock.held)

    def test_runner_consumes_managed_closeout_control_as_sole_writer(self) -> None:
        _, activation_owner = activate_canonical_runtime(
            self.store,
            created_at=NOW - timedelta(seconds=2),
            expires_at=NOW + timedelta(minutes=5),
            activated_at=NOW - timedelta(seconds=1),
        )
        self.store.release_writer_lease(
            account_key="ending-7153",
            owner_id=activation_owner,
            released_at=NOW - timedelta(milliseconds=500),
        )
        inbox = ControlInbox(
            self.temp / "control",
            account_key="ending-7153",
            runtime_id=self.policy.runtime_id,
            release_manifest_hash="a" * 64,
            max_snapshot_age=timedelta(seconds=5),
            authenticator=HmacControlAuthenticator(
                b"test-control-key-material-is-32-bytes-minimum",
                authorization_binding_id="f" * 64,
            ),
        )
        inbox.submit(
            "MANAGED_CLOSEOUT",
            reason="owner requested closeout",
            requested_at=NOW,
            activated_at=str(self.store.runtime_status()["activated_at"]),
        )
        service = self.service(
            FakeBrokerClient(initial_snapshot=account_snapshot(), clock=lambda: NOW)
        )
        lock = AccountWriterLock(
            self.temp / "locks", "ending-7153", owner_id="control-runner"
        )
        result = ServiceRunner(
            service=service,
            lock=lock,
            interval_seconds=0.01,
            control_inbox=inbox,
        ).run(once=True)
        self.assertEqual(result.mode_before, "MANAGED_CLOSEOUT")
        self.assertIn("MANAGED_CLOSEOUT", result.actions)
        self.assertEqual(len(tuple(inbox.processed.glob("*.json"))), 1)

    def test_stale_complete_empty_snapshot_cannot_erase_durable_exposure(self) -> None:
        position = PositionSnapshot(
            symbol="XYZ",
            quantity=Decimal("2"),
            sellable_quantity=Decimal("2"),
            average_price=Decimal("10"),
        )
        first = replace(account_snapshot(), equity_positions=(position,))
        stale = replace(
            account_snapshot(),
            observed_at=NOW - timedelta(minutes=1),
            received_at=NOW - timedelta(minutes=1),
            risk_evidence_as_of=NOW - timedelta(minutes=1),
            equity_positions=(),
        )
        capabilities = FakeBrokerClient(
            initial_snapshot=account_snapshot(), clock=lambda: NOW
        ).capabilities

        class SequenceBroker:
            def __init__(self):
                self.snapshots = [first, stale]
                self.capabilities = capabilities

            def get_account_snapshot(self, _account):
                return self.snapshots.pop(0)

        service = self.service(SequenceBroker())
        service.run_once()
        second = service.run_once()
        self.assertIn("STALE_SNAPSHOT", second.reconciliation_blockers)
        row = self.store.rows(
            "SELECT quantity,snapshot_id FROM positions WHERE account_key=? AND symbol=?",
            ("ending-7153", "XYZ"),
        )[0]
        self.assertEqual(row["quantity"], "2")
        stale_row = self.store.row("broker_snapshots", "snapshot_id", second.snapshot_id)
        self.assertEqual(stale_row["positions_reconciled"], 0)

    def test_future_unauthenticated_empty_snapshot_is_quarantined(self) -> None:
        position = PositionSnapshot(
            symbol="XYZ",
            quantity=Decimal("2"),
            sellable_quantity=Decimal("2"),
            average_price=Decimal("10"),
        )
        first = replace(account_snapshot(), equity_positions=(position,))
        invalid = replace(
            account_snapshot(),
            observed_at=NOW + timedelta(seconds=60),
            received_at=NOW + timedelta(seconds=60),
            auth_point_in_time=False,
            risk_evidence_as_of=NOW,
            equity_positions=(),
        )
        capabilities = FakeBrokerClient(
            initial_snapshot=account_snapshot(), clock=lambda: NOW
        ).capabilities

        class SequenceBroker:
            def __init__(self):
                self.snapshots = [first, invalid]
                self.capabilities = capabilities

            def get_account_snapshot(self, _account):
                return self.snapshots.pop(0)

        service = self.service(SequenceBroker())
        service.run_once()
        second = service.run_once()
        self.assertIn("SNAPSHOT_FROM_FUTURE", second.reconciliation_blockers)
        self.assertIn("AUTH_NOT_CURRENT", second.reconciliation_blockers)
        position_row = self.store.rows(
            "SELECT quantity FROM positions WHERE account_key=? AND symbol=?",
            ("ending-7153", "XYZ"),
        )[0]
        self.assertEqual(position_row["quantity"], "2")
        invalid_row = self.store.row(
            "broker_snapshots", "snapshot_id", second.snapshot_id
        )
        self.assertEqual(invalid_row["positions_reconciled"], 0)
        self.assertEqual(invalid_row["equity_orders_reconciled"], 0)


if __name__ == "__main__":
    unittest.main()
