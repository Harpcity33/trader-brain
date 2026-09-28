from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from titan_brain.live.models import (
    BrokerSnapshot,
    IntentKind,
    OrderIntent,
    PositionRecord,
    SessionLatch,
)
from titan_brain.live.state import (
    LiveStateStore,
    OutOfOrderEvent,
    StateConflict,
    object_hash,
)
from tests.test_live_state import lifecycle
from tests.live_activation_support import (
    activate_canonical_runtime,
    record_flat_reconciliation,
    stage_canonical_activation,
)


UTC = timezone.utc
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


class RuntimeStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "live.sqlite3"
        self.store = LiveStateStore(self.path)
        self.now = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def initialize(self) -> None:
        self.assertTrue(
            self.store.initialize_runtime(
                runtime_id="titan-full-live-test",
                account_key="ending-7153",
                release_manifest_hash=HASH_A,
                config_hash=HASH_B,
                policy_hash=HASH_C,
                initialized_at=self.now,
            )
        )

    def snapshot(self, suffix: str, *, advanced: bool = True) -> BrokerSnapshot:
        observed = self.now + timedelta(seconds=int(suffix))
        return BrokerSnapshot(
            snapshot_id=f"snapshot-{suffix}",
            account_key="ending-7153",
            evidence_revision=f"revision-{suffix}",
            observed_at=observed,
            received_at=observed + timedelta(milliseconds=10),
            account_state="active",
            equity=Decimal("1000.00"),
            cash=Decimal("1000.00"),
            unleveraged_buying_power=Decimal("1000.00"),
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
            advanced_orders_reconciled=advanced,
            realized_pnl_reconciled=True,
            positions_digest=HASH_A,
            orders_digest=HASH_B,
        )

    def position(
        self,
        revision: int,
        *,
        received_at: datetime,
        quantity: str = "4",
        raw_hash: str = HASH_C,
    ) -> PositionRecord:
        return PositionRecord(
            account_key="ending-7153",
            symbol="test",
            quantity=Decimal(quantity),
            sellable_quantity=Decimal(quantity),
            held_for_sells=Decimal("0"),
            average_price=Decimal("10.00"),
            source="broker",
            broker_updated_at=self.now,
            received_at=received_at,
            revision=revision,
            raw_hash=raw_hash,
        )

    def test_runtime_identity_is_hash_bound_and_always_initializes_paused(self) -> None:
        self.initialize()
        status = self.store.runtime_status()
        self.assertEqual(status["mode"], "PAUSED")
        self.assertEqual(status["authority_enabled"], 0)
        with self.assertRaises(StateConflict):
            self.store.set_runtime_mode(
                "RECONCILING",
                occurred_at=self.now + timedelta(milliseconds=1),
                reason="bypass attempt",
            )
        self.assertFalse(
            self.store.initialize_runtime(
                runtime_id="titan-full-live-test",
                account_key="ending-7153",
                release_manifest_hash=HASH_A,
                config_hash=HASH_B,
                policy_hash=HASH_C,
                initialized_at=self.now + timedelta(seconds=1),
            )
        )
        with self.assertRaises(ValueError):
            self.store.initialize_runtime(
                runtime_id="unsafe",
                account_key="ending-7153",
                release_manifest_hash=HASH_A,
                config_hash=HASH_B,
                policy_hash=HASH_C,
                initialized_at=self.now,
                mode="ACTIVE",
            )
        with self.assertRaises(StateConflict):
            self.store.initialize_runtime(
                runtime_id="different-runtime",
                account_key="ending-7153",
                release_manifest_hash=HASH_A,
                config_hash=HASH_B,
                policy_hash=HASH_C,
                initialized_at=self.now,
            )

    def test_writer_lease_requires_explicit_kernel_lock_recovery(self) -> None:
        generation = self.store.acquire_writer_lease(
            account_key="ending-7153",
            owner_id="owner-one",
            process_id=1001,
            acquired_at=self.now,
        )
        self.assertEqual(generation, 1)
        with self.assertRaises(StateConflict):
            self.store.acquire_writer_lease(
                account_key="ending-7153",
                owner_id="owner-two",
                process_id=1002,
                acquired_at=self.now + timedelta(seconds=1),
            )
        recovered = self.store.acquire_writer_lease(
            account_key="ending-7153",
            owner_id="owner-two",
            process_id=1002,
            acquired_at=self.now + timedelta(seconds=2),
            recover_stale=True,
        )
        self.assertEqual(recovered, 2)
        self.assertTrue(
            self.store.release_writer_lease(
                account_key="ending-7153",
                owner_id="owner-two",
                released_at=self.now + timedelta(seconds=3),
            )
        )

    def test_position_reconciliation_accepts_identical_facts_in_new_envelope(self) -> None:
        first = self.snapshot("1")
        second = self.snapshot("2")
        self.store.record_broker_snapshot(first)
        self.store.record_broker_snapshot(second)
        initial = self.position(7, received_at=first.received_at)
        self.assertEqual(
            self.store.reconcile_positions(
                snapshot_id=first.snapshot_id,
                account_key="ending-7153",
                positions=(initial,),
                reconciled_at=first.received_at,
            ),
            ("TEST",),
        )
        replay = self.position(7, received_at=second.received_at)
        self.assertEqual(
            self.store.reconcile_positions(
                snapshot_id=second.snapshot_id,
                account_key="ending-7153",
                positions=(replay,),
                reconciled_at=second.received_at,
            ),
            (),
        )
        row = self.store.rows(
            "SELECT * FROM positions WHERE account_key=? AND symbol=?",
            ("ending-7153", "TEST"),
        )[0]
        self.assertEqual(row["snapshot_id"], second.snapshot_id)

    def test_position_same_revision_conflict_stale_receipt_and_flat_transition(self) -> None:
        first = self.snapshot("1")
        second = self.snapshot("2")
        third = self.snapshot("3")
        for snapshot in (first, second, third):
            self.store.record_broker_snapshot(snapshot)
        initial = self.position(7, received_at=first.received_at)
        self.store.reconcile_positions(
            snapshot_id=first.snapshot_id,
            account_key="ending-7153",
            positions=(initial,),
            reconciled_at=first.received_at,
        )
        with self.assertRaises(StateConflict):
            self.store.reconcile_positions(
                snapshot_id=second.snapshot_id,
                account_key="ending-7153",
                positions=(
                    self.position(
                        7,
                        received_at=second.received_at,
                        quantity="5",
                        raw_hash=HASH_A,
                    ),
                ),
                reconciled_at=second.received_at,
            )
        with self.assertRaises(OutOfOrderEvent):
            self.store.reconcile_positions(
                snapshot_id=second.snapshot_id,
                account_key="ending-7153",
                positions=(self.position(7, received_at=self.now),),
                reconciled_at=second.received_at,
            )
        self.assertEqual(
            self.store.reconcile_positions(
                snapshot_id=third.snapshot_id,
                account_key="ending-7153",
                positions=(),
                reconciled_at=third.received_at,
            ),
            ("TEST",),
        )
        quantity = self.store.rows(
            "SELECT quantity FROM positions WHERE account_key=? AND symbol=?",
            ("ending-7153", "TEST"),
        )[0]["quantity"]
        self.assertEqual(quantity, "0")

    def test_advanced_order_evidence_is_required_for_full_reconciliation(self) -> None:
        self.assertFalse(self.snapshot("1", advanced=False).fully_reconciled)
        self.assertTrue(self.snapshot("2", advanced=True).fully_reconciled)

    def test_activation_record_is_current_and_one_use(self) -> None:
        self.initialize()
        with self.assertRaisesRegex(StateConflict, "canonical v2"):
            self.store.record_activation(
                activation_id="activation-1",
                account_key="ending-7153",
                record={"release": HASH_A, "account": "ending-7153"},
                created_at=self.now,
                expires_at=self.now + timedelta(minutes=5),
            )
        record, _ = stage_canonical_activation(
            self.store,
            created_at=self.now,
            expires_at=self.now + timedelta(minutes=5),
        )
        self.assertTrue(
            self.store.consume_activation(
                record.activation_id, consumed_at=self.now + timedelta(minutes=1)
            )
        )
        self.assertFalse(
            self.store.consume_activation(
                record.activation_id, consumed_at=self.now + timedelta(minutes=2)
            )
        )

    def test_activation_arms_atomically_and_deactivation_requires_flat_snapshot(self) -> None:
        self.initialize()
        activate_canonical_runtime(
            self.store,
            created_at=self.now,
            expires_at=self.now + timedelta(minutes=5),
            activated_at=self.now + timedelta(seconds=1),
        )
        status = self.store.runtime_status()
        self.assertEqual(status["mode"], "RECONCILING")
        self.assertEqual(status["authority_enabled"], 1)
        snapshot = self.snapshot("2")
        self.store.record_broker_snapshot(snapshot)
        self.store.reconcile_positions(
            snapshot_id=snapshot.snapshot_id,
            account_key="ending-7153",
            positions=(),
            reconciled_at=snapshot.received_at,
        )
        self.store.deactivate_runtime_authority(
            deactivated_at=snapshot.received_at + timedelta(seconds=1),
            reason="owner rollback after flatness proof",
            flatness_snapshot_id=snapshot.snapshot_id,
        )
        status = self.store.runtime_status()
        self.assertEqual(status["mode"], "PAUSED")
        self.assertEqual(status["authority_enabled"], 0)

    def test_activation_rejects_readiness_without_explicit_authority_mode(self) -> None:
        self.initialize()
        with self.assertRaisesRegex(
            StateConflict, "execution authority mode is missing"
        ):
            stage_canonical_activation(
                self.store,
                created_at=self.now,
                expires_at=self.now + timedelta(minutes=5),
                readiness_overrides={
                    "execution_authority_mode": None,
                    "attended_mutation_supported": None,
                },
            )

    def test_state_boundary_rejects_unproven_command_lane(self) -> None:
        self.initialize()
        with self.assertRaisesRegex(StateConflict, "failed hard gate"):
            stage_canonical_activation(
                self.store,
                created_at=self.now,
                expires_at=self.now + timedelta(minutes=5),
                readiness_overrides={
                    "broker_command_next_valid_id_received": False,
                },
            )

        status = self.store.runtime_status()
        self.assertEqual(status["mode"], "PAUSED")
        self.assertEqual(status["authority_enabled"], 0)

    def test_state_boundary_rejects_missing_current_high_water_receipt(self) -> None:
        self.initialize()
        with self.assertRaisesRegex(StateConflict, "incomplete or blocked"):
            stage_canonical_activation(
                self.store,
                created_at=self.now,
                expires_at=self.now + timedelta(minutes=5),
                readiness_overrides={"risk_high_water_receipt_hash": None},
            )

        status = self.store.runtime_status()
        self.assertEqual(status["mode"], "PAUSED")
        self.assertEqual(status["authority_enabled"], 0)

    def test_activation_requires_exact_phrase_and_active_transition_requires_new_reconciliation(self) -> None:
        self.initialize()
        record, owner = stage_canonical_activation(
            self.store,
            created_at=self.now,
            expires_at=self.now + timedelta(minutes=5),
        )
        activated_at = self.now + timedelta(seconds=1)
        self.store.heartbeat_writer_lease(
            account_key="ending-7153",
            owner_id=owner,
            observed_at=activated_at,
        )
        with self.assertRaisesRegex(StateConflict, "confirmation phrase"):
            self.store.activate_runtime(
                record.activation_id,
                activated_at=activated_at,
                confirmation_phrase="ACTIVATE SOMETHING ELSE",
                writer_owner_id=owner,
            )
        self.store.activate_runtime(
            record.activation_id,
            activated_at=activated_at,
            confirmation_phrase=(
                f"ACTIVATE FULL LIVE ending-7153 {record.activation_id}"
            ),
            writer_owner_id=owner,
        )
        with self.assertRaisesRegex(StateConflict, "strictly newer"):
            self.store.set_runtime_mode(
                "ACTIVE",
                occurred_at=self.now + timedelta(seconds=2),
                reason="attempted direct bypass",
            )
        record_flat_reconciliation(
            self.store,
            account_key="ending-7153",
            received_at=self.now + timedelta(milliseconds=1500),
            label="post-activation",
        )
        self.store.heartbeat_writer_lease(
            account_key="ending-7153",
            owner_id=owner,
            observed_at=self.now + timedelta(seconds=2),
        )
        self.assertTrue(
            self.store.set_runtime_mode(
                "ACTIVE",
                occurred_at=self.now + timedelta(seconds=2),
                reason="fresh service-side reconciliation complete",
            )
        )

    def test_deactivation_rejects_pre_activation_stale_and_superseded_evidence(self) -> None:
        self.initialize()
        activation, _ = activate_canonical_runtime(
            self.store,
            created_at=self.now,
            expires_at=self.now + timedelta(minutes=5),
            activated_at=self.now + timedelta(seconds=2),
        )
        with self.assertRaisesRegex(StateConflict, "strictly newer"):
            self.store.deactivate_runtime_authority(
                deactivated_at=self.now + timedelta(seconds=3),
                reason="bad old proof",
                flatness_snapshot_id=activation.readiness_evidence.durable_snapshot_id,
            )

        fresh = self.snapshot("3")
        newer = self.snapshot("4")
        for snapshot in (fresh, newer):
            self.store.record_broker_snapshot(snapshot)
            self.store.reconcile_positions(
                snapshot_id=snapshot.snapshot_id,
                account_key="ending-7153",
                positions=(),
                reconciled_at=snapshot.received_at,
            )
        with self.assertRaisesRegex(StateConflict, "superseded"):
            self.store.deactivate_runtime_authority(
                deactivated_at=self.now + timedelta(seconds=5),
                reason="bad superseded proof",
                flatness_snapshot_id=fresh.snapshot_id,
            )
        with self.assertRaisesRegex(StateConflict, "too old"):
            self.store.deactivate_runtime_authority(
                deactivated_at=newer.received_at + timedelta(seconds=16),
                reason="bad stale proof",
                flatness_snapshot_id=newer.snapshot_id,
            )

    def test_safety_intents_are_durable_without_fabricated_risk(self) -> None:
        plan, reservation, entry = lifecycle(self.now)
        self.store.prepare_submission(plan=plan, reservation=reservation, intent=entry)
        safety_tuple = {
            "account_key": "ending-7153",
            "symbol": "TEST",
            "side": "sell",
            "type": "stop_market",
            "quantity": 5,
            "stop_price": "9.50",
            "time_in_force": "gtc",
            "market_hours": "regular_hours",
        }
        protection = OrderIntent(
            intent_id="protect-intent-1",
            plan_id=plan.plan_id,
            reservation_id=None,
            account_key=plan.account_key,
            kind=IntentKind.PROTECTION,
            client_ref=str(uuid4()),
            order_tuple=safety_tuple,
            tuple_hash=object_hash(safety_tuple),
            created_at=plan.expires_at + timedelta(minutes=1),
            acknowledgement_deadline_at=plan.expires_at + timedelta(minutes=1, seconds=10),
        )
        self.assertTrue(self.store.prepare_safety_intent(protection))
        self.assertFalse(self.store.prepare_safety_intent(protection))
        row = self.store.row("order_intents", "intent_id", protection.intent_id)
        self.assertIsNone(row["reservation_id"])
        self.assertEqual(row["kind"], IntentKind.PROTECTION.value)
        invalid = OrderIntent(
            **{
                **protection.__dict__,
                "intent_id": "protect-intent-invalid",
                "client_ref": str(uuid4()),
                "kind": IntentKind.EXIT,
            }
        )
        object.__setattr__(invalid, "reservation_id", reservation.reservation_id)
        with self.assertRaises(StateConflict):
            self.store.prepare_safety_intent(invalid)

    def test_session_high_water_and_first_crossing_are_irreversible(self) -> None:
        first_cross = self.now + timedelta(seconds=1)
        initial = SessionLatch(
            account_key="ending-7153",
            trading_date=date(2026, 9, 8),
            loss_locked=False,
            objective_crossed=True,
            pause_new_entries=False,
            closeout_started=False,
            revision=1,
            updated_at=first_cross,
            highest_realized_pnl=Decimal("150.00"),
            first_objective_crossed_at=first_cross,
        )
        self.assertTrue(self.store.apply_session_latch(initial))
        with self.assertRaises(StateConflict):
            self.store.apply_session_latch(
                SessionLatch(
                    **{
                        **initial.__dict__,
                        "revision": 2,
                        "updated_at": first_cross + timedelta(seconds=1),
                        "highest_realized_pnl": Decimal("149.00"),
                    }
                )
            )
        with self.assertRaises(StateConflict):
            self.store.apply_session_latch(
                SessionLatch(
                    **{
                        **initial.__dict__,
                        "revision": 2,
                        "updated_at": first_cross + timedelta(seconds=2),
                        "highest_realized_pnl": Decimal("151.00"),
                        "first_objective_crossed_at": first_cross + timedelta(seconds=1),
                    }
                )
            )


if __name__ == "__main__":
    unittest.main()
