"""Hermetic tests for the durable autonomous IBKR plan reader."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from titan_brain.live.broker.base import (
    BrokerSide,
    EquityOrderType,
    MarketHours,
    OrderRequest,
    TimeInForce,
)
from titan_brain.live.broker.ibkr_preflight import (
    AttendedPlanReader,
    IbkrAttendedOrderPlan,
    IbkrOrderPurpose,
)
from titan_brain.live.ibkr_autonomous_plans import (
    AUTONOMOUS_IBKR_PLAN_EVENT,
    AutonomousIbkrPlanBindings,
    AutonomousIbkrPlanError,
    AutonomousIbkrPlanSeal,
    StateBackedAutonomousIbkrPlanProducer,
    StateBackedAutonomousIbkrPlanReader,
    autonomous_ibkr_stop_client_ref,
    seal_autonomous_ibkr_plan,
)
from titan_brain.live.models import (
    ExpiringPlan,
    IntentKind,
    IntentState,
    OrderIntent,
    RiskReservation,
)
from titan_brain.live.state import LiveStateStore, object_hash


UTC = timezone.utc
NOW = datetime(2026, 9, 14, 14, 30, tzinfo=UTC)
RUNTIME = "titan-autonomous-plan-test"
RELEASE = "1" * 64
POLICY = "2" * 64
CONFIG = "3" * 64
EVIDENCE = "4" * 64
ACCOUNT_KEY = "ibkr-live-ending-3103"
ACCOUNT_MASKED = "****3103"
STRATEGY = "titan-full-live-test"
PLAN_ID = hashlib.sha256(b"exact-autonomous-plan").hexdigest()


def _request_tuple(account_key: str, request: OrderRequest) -> dict[str, object]:
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
            format(request.limit_price, "f")
            if request.limit_price is not None
            else None
        ),
        "stop_price": (
            format(request.stop_price, "f")
            if request.stop_price is not None
            else None
        ),
        "client_ref_id": request.client_ref_id,
    }


class AutonomousIbkrPlanReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state = LiveStateStore(
            Path(self.temporary.name) / "full-live.sqlite3"
        )
        self.addCleanup(self.state.close)
        self.state.initialize_runtime(
            runtime_id=RUNTIME,
            account_key=ACCOUNT_KEY,
            release_manifest_hash=RELEASE,
            config_hash=CONFIG,
            policy_hash=POLICY,
            initialized_at=NOW - timedelta(minutes=1),
        )
        self.bindings = AutonomousIbkrPlanBindings(
            runtime_id=RUNTIME,
            release_manifest_hash=RELEASE,
            account_key=ACCOUNT_KEY,
            account_masked=ACCOUNT_MASKED,
            strategy_id=STRATEGY,
            policy_hash=POLICY,
            config_hash=CONFIG,
        )
        self.clock = lambda: NOW
        self.reader = StateBackedAutonomousIbkrPlanReader(
            state=self.state,
            bindings=self.bindings,
            clock=self.clock,
        )

    def _producer(self) -> StateBackedAutonomousIbkrPlanProducer:
        return StateBackedAutonomousIbkrPlanProducer(
            state=self.state,
            bindings=self.bindings,
            clock=self.clock,
            seal_ttl_seconds=5,
        )

    def _prepare_entry(
        self,
        *,
        plan_id: str = PLAN_ID,
        stress_risk: Decimal = Decimal("3.10"),
    ) -> tuple[OrderRequest, OrderRequest, IbkrAttendedOrderPlan, OrderIntent]:
        request = OrderRequest(
            account_masked=ACCOUNT_MASKED,
            symbol="TEST",
            side=BrokerSide.BUY,
            order_type=EquityOrderType.LIMIT,
            quantity=5,
            market_hours=MarketHours.REGULAR,
            time_in_force=TimeInForce.GFD,
            client_ref_id=str(uuid4()),
            limit_price=Decimal("10.00"),
        )
        stop = OrderRequest(
            account_masked=ACCOUNT_MASKED,
            symbol="TEST",
            side=BrokerSide.SELL,
            order_type=EquityOrderType.STOP_MARKET,
            quantity=5,
            market_hours=MarketHours.REGULAR,
            time_in_force=TimeInForce.GTC,
            client_ref_id=str(uuid4()),
            stop_price=Decimal("9.50"),
        )
        durable_plan = ExpiringPlan(
            plan_id=plan_id,
            account_key=ACCOUNT_KEY,
            strategy_id=STRATEGY,
            symbol="TEST",
            setup_id="FIRST_PULLBACK",
            quantity=5,
            limit_price=Decimal("10.00"),
            structural_stop=Decimal("9.50"),
            market_hours="regular_hours",
            time_in_force="gfd",
            evidence_cutoff_at=NOW - timedelta(seconds=13),
            created_at=NOW - timedelta(seconds=12),
            expires_at=NOW + timedelta(seconds=20),
            policy_hash=POLICY,
            config_hash=CONFIG,
            evidence_hash=EVIDENCE,
            targets=(Decimal("10.50"), Decimal("11.00")),
        )
        reservation = RiskReservation(
            reservation_id=str(uuid4()),
            plan_id=plan_id,
            account_key=ACCOUNT_KEY,
            planned_risk=Decimal("2.50"),
            stress_risk=stress_risk,
            execution_reserve=Decimal("0.50"),
            notional=Decimal("50.00"),
            created_at=NOW - timedelta(seconds=11),
        )
        order_tuple = _request_tuple(ACCOUNT_KEY, request)
        intent = OrderIntent(
            intent_id=str(uuid4()),
            plan_id=plan_id,
            reservation_id=reservation.reservation_id,
            account_key=ACCOUNT_KEY,
            kind=IntentKind.ENTRY,
            client_ref=request.client_ref_id,
            order_tuple=order_tuple,
            tuple_hash=object_hash(order_tuple),
            created_at=NOW - timedelta(seconds=10),
            acknowledgement_deadline_at=NOW + timedelta(seconds=10),
        )
        self.state.prepare_submission(
            plan=durable_plan,
            reservation=reservation,
            intent=intent,
        )
        plan = IbkrAttendedOrderPlan(
            plan_id=plan_id,
            purpose=IbkrOrderPurpose.ENTRY,
            request=request,
            structural_stop=Decimal("9.50"),
            targets=(Decimal("10.50"), Decimal("11.00")),
            execution_reserve=Decimal("0.50"),
            fee_reserve=Decimal("0.10"),
            required_stop_request=stop,
        )
        return request, stop, plan, intent

    def _seal(
        self,
        plan: IbkrAttendedOrderPlan,
        intent: OrderIntent,
        *,
        created_at: datetime = NOW - timedelta(seconds=5),
        expires_at: datetime = NOW + timedelta(seconds=5),
    ) -> AutonomousIbkrPlanSeal:
        seal = AutonomousIbkrPlanSeal.build(
            intent_id=intent.intent_id,
            runtime_id=RUNTIME,
            release_manifest_hash=RELEASE,
            account_key=ACCOUNT_KEY,
            strategy_id=STRATEGY,
            policy_hash=POLICY,
            config_hash=CONFIG,
            created_at=created_at,
            expires_at=expires_at,
            plan=plan,
        )
        self.assertTrue(
            seal_autonomous_ibkr_plan(
                state=self.state,
                bindings=self.bindings,
                seal=seal,
                clock=self.clock,
            )
        )
        return seal

    def test_exact_sealed_entry_is_read_in_prepared_and_submitting_states(self):
        request, _stop, plan, intent = self._prepare_entry()
        self._seal(plan, intent)
        self.assertIsInstance(self.reader, AttendedPlanReader)
        self.assertEqual(self.reader(request), plan)
        self.state.transition_intent(
            intent.intent_id,
            IntentState.SUBMITTING,
            occurred_at=NOW - timedelta(seconds=1),
        )
        self.assertEqual(self.reader(request), plan)

    def test_unknown_intent_cannot_reuse_plan_or_retry(self):
        request, _stop, plan, intent = self._prepare_entry()
        self._seal(plan, intent)
        self.state.transition_intent(
            intent.intent_id,
            IntentState.SUBMITTING,
            occurred_at=NOW - timedelta(seconds=2),
        )
        self.state.transition_intent(
            intent.intent_id,
            IntentState.UNKNOWN,
            occurred_at=NOW - timedelta(seconds=1),
        )
        with self.assertRaisesRegex(
            AutonomousIbkrPlanError, "INTENT_NOT_CURRENT"
        ):
            self.reader(request)

    def test_different_exact_request_or_reference_is_denied(self):
        request, _stop, plan, intent = self._prepare_entry()
        self._seal(plan, intent)
        changed = OrderRequest(
            **{
                **request.__dict__,
                "client_ref_id": str(uuid4()),
            }
        )
        with self.assertRaisesRegex(
            AutonomousIbkrPlanError, "EXACT_INTENT_UNAVAILABLE"
        ):
            self.reader(changed)

    def test_unsealed_state_is_not_promoted_to_a_plan(self):
        request, _stop, _plan, _intent = self._prepare_entry()
        with self.assertRaisesRegex(
            AutonomousIbkrPlanError, "EXACT_SEAL_UNAVAILABLE"
        ):
            self.reader(request)

    def test_producer_builds_exact_entry_and_deterministic_stop_template(self):
        request, _unused_stop, _unused_plan, intent = self._prepare_entry()
        producer = self._producer()

        seal = producer.seal(
            intent_id=intent.intent_id,
            plan_id=PLAN_ID,
            kind=IntentKind.ENTRY,
            request=request,
        )

        plan = self.reader(request)
        self.assertEqual(plan, seal.plan)
        self.assertEqual(plan.purpose, IbkrOrderPurpose.ENTRY)
        self.assertEqual(plan.execution_reserve, Decimal("0.50"))
        self.assertEqual(plan.fee_reserve, Decimal("0.10"))
        self.assertEqual(
            plan.targets,
            (Decimal("10.50"), Decimal("11.00")),
        )
        stop = plan.required_stop_request
        self.assertIsNotNone(stop)
        assert stop is not None
        self.assertEqual(stop.symbol, request.symbol)
        self.assertEqual(stop.quantity, request.quantity)
        self.assertEqual(stop.side, BrokerSide.SELL)
        self.assertEqual(stop.order_type, EquityOrderType.STOP_MARKET)
        self.assertEqual(stop.market_hours, MarketHours.REGULAR)
        self.assertEqual(stop.time_in_force, TimeInForce.GTC)
        self.assertEqual(stop.stop_price, Decimal("9.50"))
        self.assertEqual(
            stop.client_ref_id,
            autonomous_ibkr_stop_client_ref(
                plan_id=PLAN_ID,
                entry_client_ref_id=request.client_ref_id,
            ),
        )

    def test_producer_seal_is_crash_replay_idempotent(self):
        request, _stop, _plan, intent = self._prepare_entry()
        producer = self._producer()
        first = producer.seal(
            intent_id=intent.intent_id,
            plan_id=PLAN_ID,
            kind=IntentKind.ENTRY,
            request=request,
        )
        # Model a process death after the durable append but before the caller
        # receives success: a new producer instance must reuse, not append.
        restarted = self._producer()
        second = restarted.seal(
            intent_id=intent.intent_id,
            plan_id=PLAN_ID,
            kind=IntentKind.ENTRY,
            request=request,
        )
        self.assertEqual(second, first)
        self.assertEqual(
            len(
                self.state.rows(
                    "SELECT * FROM audit_events WHERE event_type=?",
                    (AUTONOMOUS_IBKR_PLAN_EVENT,),
                )
            ),
            1,
        )

    def test_producer_rejects_nonpositive_or_missing_fee_residual(self):
        request, _stop, _plan, intent = self._prepare_entry(
            stress_risk=Decimal("3.00")
        )
        with self.assertRaisesRegex(
            AutonomousIbkrPlanError,
            "POSITIVE_FEE_RESIDUAL_REQUIRED",
        ):
            self._producer().seal(
                intent_id=intent.intent_id,
                plan_id=PLAN_ID,
                kind=IntentKind.ENTRY,
                request=request,
            )
        self.assertEqual(
            self.state.rows(
                "SELECT * FROM audit_events WHERE event_type=?",
                (AUTONOMOUS_IBKR_PLAN_EVENT,),
            ),
            [],
        )

    def test_aggregate_without_positive_durable_fee_residual_cannot_be_sealed(self):
        _request, _stop, plan, intent = self._prepare_entry(
            stress_risk=Decimal("3.00")
        )
        seal = AutonomousIbkrPlanSeal.build(
            intent_id=intent.intent_id,
            runtime_id=RUNTIME,
            release_manifest_hash=RELEASE,
            account_key=ACCOUNT_KEY,
            strategy_id=STRATEGY,
            policy_hash=POLICY,
            config_hash=CONFIG,
            created_at=NOW - timedelta(seconds=5),
            expires_at=NOW + timedelta(seconds=5),
            plan=plan,
        )
        with self.assertRaisesRegex(
            AutonomousIbkrPlanError, "RISK_RESERVATION_MISMATCH"
        ):
            seal_autonomous_ibkr_plan(
                state=self.state,
                bindings=self.bindings,
                seal=seal,
                clock=self.clock,
            )

    def test_seal_is_idempotent_but_changed_envelope_conflicts(self):
        _request, _stop, plan, intent = self._prepare_entry()
        seal = self._seal(plan, intent)
        self.assertFalse(
            seal_autonomous_ibkr_plan(
                state=self.state,
                bindings=self.bindings,
                seal=seal,
                clock=self.clock,
            )
        )
        changed = AutonomousIbkrPlanSeal.build(
            intent_id=intent.intent_id,
            runtime_id=RUNTIME,
            release_manifest_hash=RELEASE,
            account_key=ACCOUNT_KEY,
            strategy_id=STRATEGY,
            policy_hash=POLICY,
            config_hash=CONFIG,
            created_at=NOW - timedelta(seconds=4),
            expires_at=NOW + timedelta(seconds=5),
            plan=plan,
        )
        with self.assertRaisesRegex(AutonomousIbkrPlanError, "SEAL_CONFLICT"):
            seal_autonomous_ibkr_plan(
                state=self.state,
                bindings=self.bindings,
                seal=changed,
                clock=self.clock,
            )

    def test_expired_seal_is_denied(self):
        request, _stop, plan, intent = self._prepare_entry()
        seal = AutonomousIbkrPlanSeal.build(
            intent_id=intent.intent_id,
            runtime_id=RUNTIME,
            release_manifest_hash=RELEASE,
            account_key=ACCOUNT_KEY,
            strategy_id=STRATEGY,
            policy_hash=POLICY,
            config_hash=CONFIG,
            created_at=NOW - timedelta(seconds=6),
            expires_at=NOW - timedelta(seconds=1),
            plan=plan,
        )
        # Seal at a clock inside the seal, then advance the reader clock.
        early = lambda: NOW - timedelta(seconds=2)
        self.assertTrue(
            seal_autonomous_ibkr_plan(
                state=self.state,
                bindings=self.bindings,
                seal=seal,
                clock=early,
            )
        )
        with self.assertRaisesRegex(
            AutonomousIbkrPlanError, "INTENT_OR_SEAL_STALE"
        ):
            self.reader(request)

    def test_release_or_policy_binding_change_is_denied(self):
        request, _stop, plan, intent = self._prepare_entry()
        self._seal(plan, intent)
        wrong = StateBackedAutonomousIbkrPlanReader(
            state=self.state,
            bindings=AutonomousIbkrPlanBindings(
                runtime_id=RUNTIME,
                release_manifest_hash="9" * 64,
                account_key=ACCOUNT_KEY,
                account_masked=ACCOUNT_MASKED,
                strategy_id=STRATEGY,
                policy_hash=POLICY,
                config_hash=CONFIG,
            ),
            clock=self.clock,
        )
        with self.assertRaisesRegex(
            AutonomousIbkrPlanError, "RUNTIME_BINDING_MISMATCH"
        ):
            wrong(request)

    def test_hash_chain_tamper_is_denied(self):
        request, _stop, plan, intent = self._prepare_entry()
        self._seal(plan, intent)
        with self.state.transaction() as connection:
            connection.execute("DROP TRIGGER audit_events_no_update")
            connection.execute(
                "UPDATE audit_events SET event_hash=? WHERE sequence=1",
                ("f" * 64,),
            )
        with self.assertRaisesRegex(
            AutonomousIbkrPlanError, "AUDIT_CHAIN_INVALID"
        ):
            self.reader(request)

    def test_concurrent_valid_append_changes_expected_chain_and_is_denied(self):
        request, _stop, plan, intent = self._prepare_entry()
        self._seal(plan, intent)
        original_validate = self.reader._validate

        def validate_then_append(**kwargs):
            original_validate(**kwargs)
            self.state.append_event(
                stream=ACCOUNT_KEY,
                event_type="CONCURRENT_VALID_APPEND",
                entity_type="test_probe",
                entity_id=str(uuid4()),
                occurred_at=NOW,
                payload={"purpose": "prove-reader-chain-snapshot-changed"},
            )

        with patch.object(
            self.reader, "_validate", side_effect=validate_then_append
        ):
            with self.assertRaisesRegex(
                AutonomousIbkrPlanError, "AUDIT_CHAIN_CHANGED"
            ):
                self.reader(request)
        self.assertTrue(self.state.verify_event_chain()[0])

    def test_exact_safety_exit_plan_uses_same_durable_risk_geometry(self):
        _entry, _stop, _plan, _entry_intent = self._prepare_entry()
        request = OrderRequest(
            account_masked=ACCOUNT_MASKED,
            symbol="TEST",
            side=BrokerSide.SELL,
            order_type=EquityOrderType.LIMIT,
            quantity=3,
            market_hours=MarketHours.REGULAR,
            time_in_force=TimeInForce.GFD,
            client_ref_id=str(uuid4()),
            limit_price=Decimal("10.40"),
        )
        order_tuple = {
            **_request_tuple(ACCOUNT_KEY, request),
            "operation": "place_equity_order",
            "plan_id": PLAN_ID,
            "kind": IntentKind.EXIT.value,
            "operation_key": "closeout:TEST:3",
        }
        intent = OrderIntent(
            intent_id=str(uuid4()),
            plan_id=PLAN_ID,
            reservation_id=None,
            account_key=ACCOUNT_KEY,
            kind=IntentKind.EXIT,
            client_ref=request.client_ref_id,
            order_tuple=order_tuple,
            tuple_hash=object_hash(order_tuple),
            created_at=NOW - timedelta(seconds=4),
            acknowledgement_deadline_at=NOW + timedelta(seconds=10),
        )
        self.state.prepare_safety_intent(intent)
        plan = IbkrAttendedOrderPlan(
            plan_id=PLAN_ID,
            purpose=IbkrOrderPurpose.EXIT,
            request=request,
            structural_stop=Decimal("9.50"),
            targets=(),
            execution_reserve=Decimal("0.50"),
            fee_reserve=Decimal("0.10"),
            required_stop_request=None,
        )
        self._seal(
            plan,
            intent,
            created_at=NOW - timedelta(seconds=3),
        )
        self.assertEqual(self.reader(request), plan)

    def test_producer_builds_exit_from_durable_original_geometry(self):
        self._prepare_entry()
        request = OrderRequest(
            account_masked=ACCOUNT_MASKED,
            symbol="TEST",
            side=BrokerSide.SELL,
            order_type=EquityOrderType.LIMIT,
            quantity=3,
            market_hours=MarketHours.REGULAR,
            time_in_force=TimeInForce.GFD,
            client_ref_id=str(uuid4()),
            limit_price=Decimal("10.40"),
        )
        order_tuple = {
            **_request_tuple(ACCOUNT_KEY, request),
            "operation": "place_equity_order",
            "plan_id": PLAN_ID,
            "kind": IntentKind.EXIT.value,
            "operation_key": "closeout:TEST:producer:3",
        }
        intent = OrderIntent(
            intent_id=str(uuid4()),
            plan_id=PLAN_ID,
            reservation_id=None,
            account_key=ACCOUNT_KEY,
            kind=IntentKind.EXIT,
            client_ref=request.client_ref_id,
            order_tuple=order_tuple,
            tuple_hash=object_hash(order_tuple),
            created_at=NOW - timedelta(seconds=4),
            acknowledgement_deadline_at=NOW + timedelta(seconds=10),
        )
        self.state.prepare_safety_intent(intent)

        self._producer()(
            intent_id=intent.intent_id,
            plan_id=PLAN_ID,
            kind=IntentKind.EXIT,
            request=request,
        )

        plan = self.reader(request)
        self.assertEqual(plan.purpose, IbkrOrderPurpose.EXIT)
        self.assertEqual(plan.structural_stop, Decimal("9.50"))
        self.assertEqual(
            plan.targets,
            (Decimal("10.50"), Decimal("11.00")),
        )
        self.assertEqual(plan.execution_reserve, Decimal("0.50"))
        self.assertEqual(plan.fee_reserve, Decimal("0.10"))
        self.assertIsNone(plan.required_stop_request)

    def test_protection_must_match_original_structural_stop(self):
        _entry, _stop, _plan, _entry_intent = self._prepare_entry()
        widened = OrderRequest(
            account_masked=ACCOUNT_MASKED,
            symbol="TEST",
            side=BrokerSide.SELL,
            order_type=EquityOrderType.STOP_MARKET,
            quantity=5,
            market_hours=MarketHours.REGULAR,
            time_in_force=TimeInForce.GTC,
            client_ref_id=str(uuid4()),
            stop_price=Decimal("9.40"),
        )
        order_tuple = {
            **_request_tuple(ACCOUNT_KEY, widened),
            "operation": "place_equity_order",
            "plan_id": PLAN_ID,
            "kind": IntentKind.PROTECTION.value,
            "operation_key": "protect:TEST:5",
        }
        intent = OrderIntent(
            intent_id=str(uuid4()),
            plan_id=PLAN_ID,
            reservation_id=None,
            account_key=ACCOUNT_KEY,
            kind=IntentKind.PROTECTION,
            client_ref=widened.client_ref_id,
            order_tuple=order_tuple,
            tuple_hash=object_hash(order_tuple),
            created_at=NOW - timedelta(seconds=4),
            acknowledgement_deadline_at=NOW + timedelta(seconds=10),
        )
        self.state.prepare_safety_intent(intent)
        plan = IbkrAttendedOrderPlan(
            plan_id=PLAN_ID,
            purpose=IbkrOrderPurpose.PROTECTION,
            request=widened,
            structural_stop=Decimal("9.40"),
            targets=(),
            execution_reserve=Decimal("0.50"),
            fee_reserve=Decimal("0.10"),
            required_stop_request=None,
        )
        seal = AutonomousIbkrPlanSeal.build(
            intent_id=intent.intent_id,
            runtime_id=RUNTIME,
            release_manifest_hash=RELEASE,
            account_key=ACCOUNT_KEY,
            strategy_id=STRATEGY,
            policy_hash=POLICY,
            config_hash=CONFIG,
            created_at=NOW - timedelta(seconds=3),
            expires_at=NOW + timedelta(seconds=5),
            plan=plan,
        )
        with self.assertRaisesRegex(
            AutonomousIbkrPlanError, "PROTECTION_GEOMETRY_MISMATCH"
        ):
            seal_autonomous_ibkr_plan(
                state=self.state,
                bindings=self.bindings,
                seal=seal,
                clock=self.clock,
            )

    def test_exact_protection_plan_is_read_without_widening_or_defaulting(self):
        _entry, _stop, _plan, _entry_intent = self._prepare_entry()
        request = OrderRequest(
            account_masked=ACCOUNT_MASKED,
            symbol="TEST",
            side=BrokerSide.SELL,
            order_type=EquityOrderType.STOP_MARKET,
            quantity=2,
            market_hours=MarketHours.REGULAR,
            time_in_force=TimeInForce.GTC,
            client_ref_id=str(uuid4()),
            stop_price=Decimal("9.50"),
        )
        order_tuple = {
            **_request_tuple(ACCOUNT_KEY, request),
            "operation": "place_equity_order",
            "plan_id": PLAN_ID,
            "kind": IntentKind.PROTECTION.value,
            "operation_key": "protect:TEST:fill-delta-2",
        }
        intent = OrderIntent(
            intent_id=str(uuid4()),
            plan_id=PLAN_ID,
            reservation_id=None,
            account_key=ACCOUNT_KEY,
            kind=IntentKind.PROTECTION,
            client_ref=request.client_ref_id,
            order_tuple=order_tuple,
            tuple_hash=object_hash(order_tuple),
            created_at=NOW - timedelta(seconds=4),
            acknowledgement_deadline_at=NOW + timedelta(seconds=10),
        )
        self.state.prepare_safety_intent(intent)
        plan = IbkrAttendedOrderPlan(
            plan_id=PLAN_ID,
            purpose=IbkrOrderPurpose.PROTECTION,
            request=request,
            structural_stop=Decimal("9.50"),
            targets=(),
            execution_reserve=Decimal("0.50"),
            fee_reserve=Decimal("0.10"),
            required_stop_request=None,
        )
        self._seal(
            plan,
            intent,
            created_at=NOW - timedelta(seconds=3),
        )
        self.assertEqual(self.reader(request), plan)

    def test_producer_builds_protection_from_original_stop_and_risk(self):
        self._prepare_entry()
        request = OrderRequest(
            account_masked=ACCOUNT_MASKED,
            symbol="TEST",
            side=BrokerSide.SELL,
            order_type=EquityOrderType.STOP_MARKET,
            quantity=2,
            market_hours=MarketHours.REGULAR,
            time_in_force=TimeInForce.GTC,
            client_ref_id=str(uuid4()),
            stop_price=Decimal("9.50"),
        )
        order_tuple = {
            **_request_tuple(ACCOUNT_KEY, request),
            "operation": "place_equity_order",
            "plan_id": PLAN_ID,
            "kind": IntentKind.PROTECTION.value,
            "operation_key": "protect:TEST:producer:2",
        }
        intent = OrderIntent(
            intent_id=str(uuid4()),
            plan_id=PLAN_ID,
            reservation_id=None,
            account_key=ACCOUNT_KEY,
            kind=IntentKind.PROTECTION,
            client_ref=request.client_ref_id,
            order_tuple=order_tuple,
            tuple_hash=object_hash(order_tuple),
            created_at=NOW - timedelta(seconds=4),
            acknowledgement_deadline_at=NOW + timedelta(seconds=10),
        )
        self.state.prepare_safety_intent(intent)

        self._producer()(
            intent_id=intent.intent_id,
            plan_id=PLAN_ID,
            kind=IntentKind.PROTECTION,
            request=request,
        )

        plan = self.reader(request)
        self.assertEqual(plan.purpose, IbkrOrderPurpose.PROTECTION)
        self.assertEqual(plan.structural_stop, Decimal("9.50"))
        self.assertEqual(plan.targets, ())
        self.assertEqual(plan.execution_reserve, Decimal("0.50"))
        self.assertEqual(plan.fee_reserve, Decimal("0.10"))
        self.assertIsNone(plan.required_stop_request)


if __name__ == "__main__":
    unittest.main()
