from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sqlite3
import tempfile
import unittest
from uuid import uuid4

from titan_brain.live.money import NumericPolicyError, finite_decimal, money, whole_shares
from titan_brain.live.models import (
    BrokerOrder,
    BrokerOrderState,
    BrokerSnapshot,
    ExpiringPlan,
    Fill,
    Incident,
    IncidentSeverity,
    IntentKind,
    IntentState,
    LatencySample,
    OrderIntent,
    OutboxMessage,
    ProtectionObligation,
    ProtectionState,
    RiskReservation,
    SessionLatch,
)
from titan_brain.live.state import (
    LiveStateStore,
    OutOfOrderEvent,
    StateConflict,
    UnsupportedSchema,
    object_hash,
)


UTC = timezone.utc
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


def lifecycle(now: datetime | None = None, *, suffix: str = "1"):
    now = now or datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
    order_tuple = {
        "account_key": "ending-7153",
        "symbol": "TEST",
        "side": "buy",
        "type": "limit",
        "quantity": 5,
        "limit_price": "10.00",
        "time_in_force": "gfd",
        "market_hours": "regular_hours",
    }
    plan = ExpiringPlan(
        plan_id=f"plan-{suffix}",
        account_key="ending-7153",
        strategy_id="titan-full-live-test",
        symbol="test",
        setup_id="FIRST_PULLBACK",
        quantity=5,
        limit_price=Decimal("10.00"),
        structural_stop=Decimal("9.50"),
        market_hours="regular_hours",
        time_in_force="gfd",
        evidence_cutoff_at=now,
        created_at=now + timedelta(seconds=1),
        expires_at=now + timedelta(seconds=31),
        policy_hash=HASH_A,
        config_hash=HASH_B,
        evidence_hash=HASH_C,
        targets=(Decimal("10.50"), Decimal("11.00")),
    )
    reservation = RiskReservation(
        reservation_id=f"reservation-{suffix}",
        plan_id=plan.plan_id,
        account_key=plan.account_key,
        planned_risk=Decimal("2.50"),
        stress_risk=Decimal("3.00"),
        execution_reserve=Decimal("0.50"),
        notional=Decimal("50.00"),
        created_at=now + timedelta(seconds=2),
    )
    intent = OrderIntent(
        intent_id=f"intent-{suffix}",
        plan_id=plan.plan_id,
        reservation_id=reservation.reservation_id,
        account_key=plan.account_key,
        kind=IntentKind.ENTRY,
        client_ref=str(uuid4()),
        order_tuple=order_tuple,
        tuple_hash=object_hash(order_tuple),
        created_at=now + timedelta(seconds=3),
        acknowledgement_deadline_at=now + timedelta(seconds=13),
    )
    return plan, reservation, intent


class ExactNumericTests(unittest.TestCase):
    def test_nonfinite_and_boolean_values_fail_closed(self):
        for value in (float("nan"), float("inf"), float("-inf"), "NaN", "Infinity", True, None):
            with self.subTest(value=value):
                with self.assertRaises(NumericPolicyError):
                    finite_decimal(value)

    def test_money_is_exact_to_cents_and_whole_shares_are_strict(self):
        self.assertEqual(money("12.30"), Decimal("12.30"))
        with self.assertRaises(NumericPolicyError):
            money("12.301")
        self.assertEqual(whole_shares("5.0"), 5)
        self.assertEqual(whole_shares(0, allow_zero=True), 0)
        for value in (0, -1, 1.5, Decimal("2.01"), False):
            with self.subTest(value=value):
                with self.assertRaises(NumericPolicyError):
                    whole_shares(value)

    def test_plan_rejects_fractional_quantity_and_nonfinite_price(self):
        plan, _, _ = lifecycle()
        values = dict(plan.__dict__)
        values["quantity"] = Decimal("1.5")
        with self.assertRaises(NumericPolicyError):
            ExpiringPlan(**values)
        values = dict(plan.__dict__)
        values["limit_price"] = Decimal("NaN")
        with self.assertRaises(NumericPolicyError):
            ExpiringPlan(**values)


class SchemaStartupTests(unittest.TestCase):
    def test_unversioned_partial_database_fails_closed_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "partial.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE interrupted_install(value TEXT)")
            connection.commit()
            connection.close()

            with self.assertRaisesRegex(UnsupportedSchema, "partial schema"):
                LiveStateStore(path)

            connection = sqlite3.connect(path)
            try:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 0)
                self.assertEqual(
                    connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall(),
                    [("interrupted_install",)],
                )
            finally:
                connection.close()


class LiveStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "live.sqlite3"
        self.store = LiveStateStore(self.path)
        self.now = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def prepare(self, suffix: str = "1"):
        values = lifecycle(self.now, suffix=suffix)
        self.assertTrue(
            self.store.prepare_submission(
                plan=values[0], reservation=values[1], intent=values[2]
            )
        )
        return values

    def record_flat_snapshot(
        self,
        *,
        suffix: str,
        received_at: datetime,
        equity_order_count: int = 0,
        complete: bool = True,
        blocker_count: int = 0,
    ) -> BrokerSnapshot:
        snapshot = BrokerSnapshot(
            snapshot_id=f"flat-{suffix}",
            account_key="ending-7153",
            evidence_revision=f"flat-revision-{suffix}",
            observed_at=received_at,
            received_at=received_at,
            account_state="active",
            equity=Decimal("1000.00"),
            cash=Decimal("1000.00"),
            unleveraged_buying_power=Decimal("1000.00"),
            realized_pnl=Decimal("0.00"),
            equity_position_count=0,
            equity_order_count=equity_order_count,
            equity_nonterminal_order_count=0,
            external_material_order_count=0,
            option_position_count=0,
            option_order_count=0,
            advanced_order_count=0,
            reconciliation_blocker_count=blocker_count,
            positions_reconciled=complete,
            equity_orders_reconciled=complete,
            option_positions_reconciled=complete,
            option_orders_reconciled=complete,
            advanced_orders_reconciled=complete,
            realized_pnl_reconciled=complete,
            positions_digest=HASH_A,
            orders_digest=HASH_B,
        )
        self.assertTrue(self.store.record_broker_snapshot(snapshot))
        if complete:
            self.store.reconcile_positions(
                snapshot_id=snapshot.snapshot_id,
                account_key=snapshot.account_key,
                positions=(),
                reconciled_at=received_at,
            )
        return snapshot

    def test_database_uses_required_durability_pragmas_and_schema(self):
        self.assertEqual(self.store.schema_version, 3)
        self.assertEqual(str(self.store.pragma("journal_mode")).lower(), "wal")
        self.assertEqual(self.store.pragma("synchronous"), 2)
        self.assertEqual(self.store.pragma("foreign_keys"), 1)
        tables = {
            row["name"]
            for row in self.store.rows(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertTrue(
            {
                "broker_snapshots",
                "plans",
                "risk_reservations",
                "order_intents",
                "broker_orders",
                "fills",
                "protection_obligations",
                "session_latches",
                "incidents",
                "notification_outbox",
                "notification_worker_lease",
                "latency_samples",
                "audit_events",
            }.issubset(tables)
        )

    def test_plan_reservation_and_intent_are_atomic_durable_and_idempotent(self):
        plan, reservation, intent = self.prepare()
        self.assertFalse(
            self.store.prepare_submission(
                plan=plan, reservation=reservation, intent=intent
            )
        )
        self.assertEqual(len(self.store.rows("SELECT * FROM plans")), 1)
        self.assertEqual(len(self.store.rows("SELECT * FROM risk_reservations")), 1)
        self.assertEqual(len(self.store.rows("SELECT * FROM order_intents")), 1)
        self.assertEqual(len(self.store.rows("SELECT * FROM audit_events")), 1)
        self.store.close()
        self.store = LiveStateStore(self.path)
        self.assertEqual(
            self.store.row("order_intents", "intent_id", intent.intent_id)["state"],
            IntentState.PREPARED.value,
        )

    def test_conflicting_replay_rolls_back_entire_prepare(self):
        _, existing_reservation, _ = self.prepare("1")
        plan, _, intent = lifecycle(self.now + timedelta(minutes=1), suffix="2")
        collision = RiskReservation(
            **{
                **existing_reservation.__dict__,
                "plan_id": plan.plan_id,
                "created_at": plan.created_at + timedelta(seconds=1),
            }
        )
        conflicting_intent = OrderIntent(
            **{
                **intent.__dict__,
                "reservation_id": collision.reservation_id,
                "created_at": collision.created_at + timedelta(seconds=1),
                "acknowledgement_deadline_at": collision.created_at
                + timedelta(seconds=11),
            }
        )
        with self.assertRaises(StateConflict):
            self.store.prepare_submission(
                plan=plan, reservation=collision, intent=conflicting_intent
            )
        self.assertIsNone(self.store.row("plans", "plan_id", plan.plan_id))

    def test_intent_unknown_is_durable_and_cannot_return_to_submitting(self):
        _, _, intent = self.prepare()
        self.assertTrue(
            self.store.transition_intent(
                intent.intent_id,
                IntentState.SUBMITTING,
                occurred_at=self.now + timedelta(seconds=4),
            )
        )
        self.assertTrue(
            self.store.transition_intent(
                intent.intent_id,
                IntentState.UNKNOWN,
                occurred_at=self.now + timedelta(seconds=14),
                detail={"reason": "connector timeout"},
            )
        )
        with self.assertRaises(StateConflict):
            self.store.transition_intent(
                intent.intent_id,
                IntentState.SUBMITTING,
                occurred_at=self.now + timedelta(seconds=15),
            )
        row = self.store.row("order_intents", "intent_id", intent.intent_id)
        self.assertEqual(row["state"], IntentState.UNKNOWN.value)

    def test_immediate_risk_release_requires_durable_known_zero_exposure_proof(self):
        _, reservation, intent = self.prepare()
        failed_at = self.now + timedelta(seconds=4)
        self.store.transition_intent(
            intent.intent_id,
            IntentState.FAILED,
            occurred_at=failed_at,
            detail={"phase": "review", "code": "KNOWN_REVIEW_FAILURE"},
        )
        self.assertTrue(
            self.store.release_reservation(
                reservation.reservation_id,
                occurred_at=failed_at,
                reason="KNOWN_REVIEW_FAILURE",
            )
        )
        self.assertFalse(
            self.store.release_reservation(
                reservation.reservation_id,
                occurred_at=failed_at,
                reason="IDEMPOTENT_REPLAY",
            )
        )
        self.assertEqual(
            self.store.row(
                "risk_reservations", "reservation_id", reservation.reservation_id
            )["state"],
            "RELEASED",
        )

        _, unsafe_reservation, unsafe_intent = self.prepare("unsafe")
        self.store.transition_intent(
            unsafe_intent.intent_id,
            IntentState.FAILED,
            occurred_at=failed_at,
            detail={"phase": "place", "known_no_accept": False},
        )
        with self.assertRaises(StateConflict):
            self.store.release_reservation(
                unsafe_reservation.reservation_id,
                occurred_at=failed_at,
                reason="UNPROVEN_FAILURE",
            )

    def test_immediate_risk_release_refuses_any_durable_fill(self):
        plan, reservation, intent = self.prepare("fill-race")
        self.store.transition_intent(
            intent.intent_id,
            IntentState.SUBMITTING,
            occurred_at=self.now + timedelta(seconds=4),
        )
        order = BrokerOrder(
            broker_order_id="broker-fill-race",
            intent_id=intent.intent_id,
            account_key=plan.account_key,
            state=BrokerOrderState.FILLED,
            quantity=5,
            cumulative_filled_quantity=5,
            revision=1,
            broker_updated_at=self.now + timedelta(seconds=5),
            received_at=self.now + timedelta(seconds=6),
            raw_hash=HASH_A,
        )
        self.store.record_broker_order(order)
        self.store.record_fill(
            Fill(
                fill_id="fill-race",
                broker_order_id=order.broker_order_id,
                account_key=plan.account_key,
                quantity=5,
                price=Decimal("10.00"),
                executed_at=self.now + timedelta(seconds=5),
                received_at=self.now + timedelta(seconds=6),
            )
        )
        self.store.transition_intent(
            intent.intent_id,
            IntentState.FAILED,
            occurred_at=self.now + timedelta(seconds=7),
            detail={"phase": "place", "known_no_accept": True},
        )
        with self.assertRaises(StateConflict):
            self.store.release_reservation(
                reservation.reservation_id,
                occurred_at=self.now + timedelta(seconds=7),
                reason="CONTRADICTED_BY_FILL",
            )
        self.assertEqual(
            self.store.row(
                "risk_reservations", "reservation_id", reservation.reservation_id
            )["state"],
            "RESERVED",
        )

    def test_reconciled_unknown_releases_only_from_newer_complete_durable_flat_snapshot(self):
        _, reservation, intent = self.prepare("unknown-flat")
        self.store.transition_intent(
            intent.intent_id,
            IntentState.SUBMITTING,
            occurred_at=self.now + timedelta(seconds=4),
        )
        self.store.transition_intent(
            intent.intent_id,
            IntentState.UNKNOWN,
            occurred_at=self.now + timedelta(seconds=5),
        )
        old = self.record_flat_snapshot(
            suffix="before-resolution",
            received_at=self.now + timedelta(seconds=5, milliseconds=500),
        )
        self.store.transition_intent(
            intent.intent_id,
            IntentState.RECONCILED,
            occurred_at=self.now + timedelta(seconds=6),
            detail={"resolution": "BROKER_CONFIRMED_CLIENT_REF_ABSENT"},
        )
        self.assertEqual(
            self.store.release_reservations_after_flat_snapshot(
                account_key="ending-7153",
                snapshot_id=old.snapshot_id,
                occurred_at=self.now + timedelta(seconds=7),
            ),
            (),
        )
        current = self.record_flat_snapshot(
            suffix="after-resolution",
            received_at=self.now + timedelta(seconds=8),
        )
        self.assertEqual(
            self.store.release_reservations_after_flat_snapshot(
                account_key="ending-7153",
                snapshot_id=current.snapshot_id,
                occurred_at=self.now + timedelta(seconds=9),
            ),
            (reservation.reservation_id,),
        )

    def test_flat_snapshot_release_requires_closed_durable_fill_inventory(self):
        plan, reservation, intent = self.prepare("closed-fill")
        self.store.transition_intent(
            intent.intent_id,
            IntentState.SUBMITTING,
            occurred_at=self.now + timedelta(seconds=4),
        )
        entry_order = BrokerOrder(
            broker_order_id="broker-entry-closed-fill",
            intent_id=intent.intent_id,
            account_key=plan.account_key,
            state=BrokerOrderState.FILLED,
            quantity=5,
            cumulative_filled_quantity=5,
            revision=1,
            broker_updated_at=self.now + timedelta(seconds=5),
            received_at=self.now + timedelta(seconds=6),
            raw_hash=HASH_A,
        )
        self.store.record_broker_order(entry_order)
        self.store.record_fill(
            Fill(
                fill_id="entry-closed-fill",
                broker_order_id=entry_order.broker_order_id,
                account_key=plan.account_key,
                quantity=5,
                price=Decimal("10.00"),
                executed_at=self.now + timedelta(seconds=5),
                received_at=self.now + timedelta(seconds=6),
            )
        )
        inconsistent = self.record_flat_snapshot(
            suffix="missing-exit-fill",
            received_at=self.now + timedelta(seconds=7),
            equity_order_count=1,
        )
        self.assertEqual(
            self.store.release_reservations_after_flat_snapshot(
                account_key=plan.account_key,
                snapshot_id=inconsistent.snapshot_id,
                occurred_at=self.now + timedelta(seconds=8),
            ),
            (),
        )

        exit_tuple = {
            **intent.order_tuple,
            "side": "sell",
        }
        exit_intent = OrderIntent(
            intent_id="exit-intent-closed-fill",
            plan_id=plan.plan_id,
            reservation_id=None,
            account_key=plan.account_key,
            kind=IntentKind.EXIT,
            client_ref=str(uuid4()),
            order_tuple=exit_tuple,
            tuple_hash=object_hash(exit_tuple),
            created_at=self.now + timedelta(seconds=8),
            acknowledgement_deadline_at=self.now + timedelta(seconds=18),
        )
        self.store.prepare_safety_intent(exit_intent)
        self.store.transition_intent(
            exit_intent.intent_id,
            IntentState.SUBMITTING,
            occurred_at=self.now + timedelta(seconds=9),
        )
        exit_order = BrokerOrder(
            broker_order_id="broker-exit-closed-fill",
            intent_id=exit_intent.intent_id,
            account_key=plan.account_key,
            state=BrokerOrderState.FILLED,
            quantity=5,
            cumulative_filled_quantity=5,
            revision=1,
            broker_updated_at=self.now + timedelta(seconds=10),
            received_at=self.now + timedelta(seconds=11),
            raw_hash=HASH_B,
        )
        self.store.record_broker_order(exit_order)
        self.store.record_fill(
            Fill(
                fill_id="exit-closed-fill",
                broker_order_id=exit_order.broker_order_id,
                account_key=plan.account_key,
                quantity=5,
                price=Decimal("10.25"),
                executed_at=self.now + timedelta(seconds=10),
                received_at=self.now + timedelta(seconds=11),
            )
        )
        self.store.transition_intent(
            exit_intent.intent_id,
            IntentState.RECONCILED,
            occurred_at=self.now + timedelta(seconds=11),
        )
        consistent = self.record_flat_snapshot(
            suffix="after-exit-fill",
            received_at=self.now + timedelta(seconds=12),
            equity_order_count=2,
        )
        self.assertEqual(
            self.store.release_reservations_after_flat_snapshot(
                account_key=plan.account_key,
                snapshot_id=consistent.snapshot_id,
                occurred_at=self.now + timedelta(seconds=13),
            ),
            (reservation.reservation_id,),
        )

    def test_broker_revisions_and_fills_are_monotonic_and_idempotent(self):
        plan, _, intent = self.prepare()
        self.store.transition_intent(
            intent.intent_id,
            IntentState.SUBMITTING,
            occurred_at=self.now + timedelta(seconds=4),
        )
        first = BrokerOrder(
            broker_order_id="broker-1",
            intent_id=intent.intent_id,
            account_key=plan.account_key,
            state=BrokerOrderState.PARTIALLY_FILLED,
            quantity=5,
            cumulative_filled_quantity=2,
            revision=2,
            broker_updated_at=self.now + timedelta(seconds=5),
            received_at=self.now + timedelta(seconds=6),
            raw_hash=HASH_A,
        )
        self.assertTrue(self.store.record_broker_order(first))
        self.assertFalse(self.store.record_broker_order(first))
        stale = BrokerOrder(
            **{
                **first.__dict__,
                "state": BrokerOrderState.CONFIRMED,
                "cumulative_filled_quantity": 0,
                "revision": 1,
            }
        )
        self.assertFalse(self.store.record_broker_order(stale))
        rollback = BrokerOrder(
            **{
                **first.__dict__,
                "cumulative_filled_quantity": 1,
                "revision": 3,
                "broker_updated_at": self.now + timedelta(seconds=7),
                "received_at": self.now + timedelta(seconds=8),
            }
        )
        with self.assertRaises(OutOfOrderEvent):
            self.store.record_broker_order(rollback)

        fill = Fill(
            fill_id="fill-1",
            broker_order_id=first.broker_order_id,
            account_key=plan.account_key,
            quantity=2,
            price=Decimal("9.99"),
            executed_at=self.now + timedelta(seconds=5),
            received_at=self.now + timedelta(seconds=7),
        )
        self.assertTrue(self.store.record_fill(fill))
        self.assertFalse(self.store.record_fill(fill))
        conflict = Fill(**{**fill.__dict__, "price": Decimal("9.98")})
        with self.assertRaises(StateConflict):
            self.store.record_fill(conflict)

        obligation = ProtectionObligation(
            obligation_id="protect-fill-1",
            source_fill_id=fill.fill_id,
            account_key=plan.account_key,
            symbol=plan.symbol,
            required_quantity=2,
            working_quantity=0,
            stop_price=plan.structural_stop,
            state=ProtectionState.REQUIRED,
            revision=0,
            updated_at=self.now + timedelta(seconds=8),
        )
        self.assertTrue(self.store.record_protection_obligation(obligation))
        self.assertFalse(self.store.record_protection_obligation(obligation))
        row = self.store.row(
            "protection_obligations", "obligation_id", obligation.obligation_id
        )
        self.assertEqual(row["required_quantity"] - row["working_quantity"], 2)

    def test_session_latches_are_one_way_and_out_of_order_safe(self):
        first = SessionLatch(
            account_key="ending-7153",
            trading_date=date(2026, 9, 8),
            loss_locked=False,
            objective_crossed=False,
            pause_new_entries=False,
            closeout_started=False,
            revision=1,
            updated_at=self.now,
        )
        locked = SessionLatch(
            **{
                **first.__dict__,
                "loss_locked": True,
                "pause_new_entries": True,
                "revision": 2,
                "updated_at": self.now + timedelta(seconds=1),
            }
        )
        self.assertTrue(self.store.apply_session_latch(first))
        self.assertTrue(self.store.apply_session_latch(locked))
        self.assertFalse(self.store.apply_session_latch(first))
        attempted_clear = SessionLatch(
            **{
                **locked.__dict__,
                "loss_locked": False,
                "revision": 3,
                "updated_at": self.now + timedelta(seconds=2),
            }
        )
        with self.assertRaises(StateConflict):
            self.store.apply_session_latch(attempted_clear)

    def test_snapshot_incident_outbox_latency_and_chain_are_durable(self):
        snapshot = BrokerSnapshot(
            snapshot_id="snapshot-1",
            account_key="ending-7153",
            evidence_revision="broker-rev-1",
            observed_at=self.now,
            received_at=self.now + timedelta(milliseconds=20),
            account_state="active",
            equity=Decimal("1000.00"),
            cash=Decimal("900.00"),
            unleveraged_buying_power=Decimal("900.00"),
            realized_pnl=Decimal("0.00"),
            equity_position_count=0,
            equity_order_count=0,
            equity_nonterminal_order_count=0,
            external_material_order_count=0,
            option_position_count=0,
            option_order_count=0,
            advanced_order_count=0,
            reconciliation_blocker_count=0,
            positions_reconciled=True,
            equity_orders_reconciled=True,
            option_positions_reconciled=True,
            option_orders_reconciled=True,
            positions_digest=HASH_A,
            orders_digest=HASH_B,
        )
        incident = Incident(
            incident_id="incident-1",
            account_key="ending-7153",
            category="AUTHENTICATION_REVOKED",
            severity=IncidentSeverity.CRITICAL,
            opened_at=self.now + timedelta(seconds=1),
            detail={"new_entries": "blocked"},
        )
        message = OutboxMessage(
            message_id="message-1",
            event_key="incident-1:opened",
            account_key="ending-7153",
            template="critical_incident",
            payload={"category": incident.category},
            created_at=self.now + timedelta(seconds=2),
        )
        sample = LatencySample(
            sample_id="latency-1",
            account_key="ending-7153",
            stage="fill_to_working_protection",
            duration_microseconds=12_345,
            observed_at=self.now + timedelta(seconds=3),
            correlation_id="fill-1",
        )
        self.assertTrue(self.store.record_broker_snapshot(snapshot))
        self.assertTrue(self.store.record_incident(incident))
        self.assertTrue(self.store.enqueue_notification(message))
        self.assertTrue(self.store.record_latency(sample))
        self.store.mark_notification_attempt(
            message.message_id,
            attempted_at=self.now + timedelta(seconds=4),
            delivered=False,
            error="destination unavailable",
        )
        row = self.store.row("notification_outbox", "message_id", message.message_id)
        self.assertEqual(row["state"], "PENDING")
        self.assertEqual(row["attempt_count"], 1)
        valid, count, head = self.store.verify_event_chain()
        self.assertTrue(valid)
        self.assertGreaterEqual(count, 5)
        self.assertEqual(len(head), 64)

        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.transaction() as connection:
                connection.execute(
                    "UPDATE audit_events SET event_type = 'TAMPERED' WHERE sequence = 1"
                )


if __name__ == "__main__":
    unittest.main()
