from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from titan_brain.live.broker import (
    AccountSnapshot,
    BrokerOrderState,
    BrokerSide,
    EquityOrderType,
    FakeBrokerClient,
    FillSnapshot,
    FundsSnapshot,
    MarketHours,
    OrderSnapshot,
    PositionSnapshot,
    TimeInForce,
)
from titan_brain.live.calendar import NEW_YORK
from titan_brain.live.lifecycle_actions import (
    LifecycleReconcileResult,
    TargetExitAssessment,
)
from titan_brain.live.models import (
    ExpiringPlan,
    IntentKind,
    IntentState,
    OrderIntent,
    RiskReservation,
)
from titan_brain.live.notifications import JsonlNotificationSink
from titan_brain.live.policy import PolicyBundle
from titan_brain.live.service import FullLiveService, build_local_outbox
from titan_brain.live.state import LiveStateStore, object_hash
from tests.live_activation_support import activate_canonical_runtime


class _RecordingActions:
    def __init__(self) -> None:
        self.protected: list[str] = []
        self.target_assessments = 0
        self.closed: list[tuple[str, str]] = []
        self.discoveries = 0

    def clear(self) -> None:
        self.protected.clear()
        self.target_assessments = 0
        self.closed.clear()
        self.discoveries = 0

    def reconcile(self, **_kwargs) -> LifecycleReconcileResult:
        return LifecycleReconcileResult()

    def protect(self, *, decision, **_kwargs) -> str:
        self.protected.append(decision.symbol)
        return "NO_MUTATION:TEST_PROTECTION"

    def target_exits(self, **_kwargs) -> TargetExitAssessment:
        self.target_assessments += 1
        return TargetExitAssessment()

    def closeout(self, *, decision, **_kwargs) -> str:
        self.closed.append((decision.symbol, decision.action.value))
        return "NO_MUTATION:TEST_CLOSEOUT"

    def discover_and_execute(self, **_kwargs) -> tuple[str, ...]:
        self.discoveries += 1
        return ("ENTRY:TEST_SHOULD_NOT_RUN",)


class MissedSessionCloseoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.temp = Path(self.temporary.name)
        self.root = Path(__file__).resolve().parents[1]
        self.policy = PolicyBundle.load(self.root)
        self.store = LiveStateStore(self.temp / "state.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    @staticmethod
    def _snapshot(
        when: datetime, *, order_only: bool = False
    ) -> AccountSnapshot:
        position = PositionSnapshot(
            symbol="CARRY",
            quantity=Decimal("2"),
            sellable_quantity=Decimal("2"),
            average_price=Decimal("10"),
        )
        order = OrderSnapshot(
            broker_order_id="carry-order",
            account_masked="••••7153",
            symbol="CARRY",
            side=BrokerSide.BUY,
            order_type=EquityOrderType.LIMIT,
            state=BrokerOrderState.CONFIRMED,
            requested_quantity=Decimal("2"),
            cumulative_filled_quantity=Decimal("0"),
            market_hours=MarketHours.REGULAR,
            time_in_force=TimeInForce.GFD,
            broker_updated_at=when,
            received_at=when,
            limit_price=Decimal("10"),
        )
        return AccountSnapshot(
            account_masked="••••7153",
            observed_at=when,
            received_at=when,
            account_state="active",
            account_type="limited_margin",
            funds=FundsSnapshot(
                total_value=Decimal("1000"),
                cash=Decimal("1000"),
                buying_power=Decimal("1000"),
                unleveraged_buying_power=Decimal("1000"),
                unsettled_funds=Decimal("0"),
            ),
            equity_positions=() if order_only else (position,),
            equity_orders=(order,) if order_only else (),
            option_position_count=0,
            option_order_count=0,
            advanced_order_count=0,
            standard_equity_positions_complete=True,
            standard_equity_orders_complete=True,
            option_positions_complete=True,
            option_orders_complete=True,
            advanced_orders_complete=True,
            auth_point_in_time=True,
            daily_realized_pnl=Decimal("0"),
            weekly_realized_pnl=Decimal("0"),
            peak_equity=Decimal("1000"),
            daily_realized_pnl_complete=True,
            weekly_realized_pnl_complete=True,
            peak_equity_complete=True,
            risk_evidence_authoritative=True,
            risk_evidence_source="deterministic-test-ledger",
            risk_evidence_as_of=when,
        )

    def _service(
        self, *, when: datetime, order_only: bool, actions: _RecordingActions
    ) -> FullLiveService:
        snapshot = self._snapshot(when, order_only=order_only)
        return FullLiveService(
            policy=self.policy,
            state=self.store,
            broker=FakeBrokerClient(
                initial_snapshot=snapshot,
                clock=lambda: when,
            ),
            notifications=build_local_outbox(
                self.store,
                "ending-7153",
                JsonlNotificationSink(self.temp / "notifications.jsonl"),
            ),
            actions=actions,
            clock=lambda: when,
        )

    def _activate(self, *, prior: datetime) -> str:
        self.store.initialize_runtime(
            runtime_id=self.policy.runtime_id,
            account_key="ending-7153",
            release_manifest_hash="a" * 64,
            config_hash=self.policy.config_hash,
            policy_hash=self.policy.policy_hash,
            initialized_at=prior - timedelta(seconds=20),
        )
        _activation, writer_owner = activate_canonical_runtime(
            self.store,
            created_at=prior - timedelta(seconds=3),
            activated_at=prior - timedelta(seconds=1),
            expires_at=prior + timedelta(minutes=5),
        )
        return writer_owner

    def _seed_current_entry_intent(self, *, when: datetime) -> str:
        client_ref = "00000000-0000-4000-8000-000000000777"
        order_tuple = {
            "account_key": "ending-7153",
            "account_masked": "••••7153",
            "symbol": "CARRY",
            "side": "buy",
            "order_type": "limit",
            "quantity": 2,
            "market_hours": "regular_hours",
            "time_in_force": "gfd",
            "limit_price": "10",
            "stop_price": None,
            "client_ref_id": client_ref,
        }
        plan = ExpiringPlan(
            plan_id="current-session-entry-plan",
            account_key="ending-7153",
            strategy_id="titan-full-live-test",
            symbol="CARRY",
            setup_id="FIRST_PULLBACK",
            quantity=2,
            limit_price=Decimal("10"),
            structural_stop=Decimal("9.50"),
            market_hours="regular_hours",
            time_in_force="gfd",
            evidence_cutoff_at=when - timedelta(seconds=4),
            created_at=when - timedelta(seconds=3),
            expires_at=when + timedelta(minutes=1),
            policy_hash=self.policy.policy_hash,
            config_hash=self.policy.config_hash,
            evidence_hash="c" * 64,
        )
        reservation = RiskReservation(
            reservation_id="current-session-entry-reservation",
            plan_id=plan.plan_id,
            account_key=plan.account_key,
            planned_risk=Decimal("1"),
            stress_risk=Decimal("2"),
            execution_reserve=Decimal("1"),
            notional=Decimal("20"),
            created_at=when - timedelta(seconds=2),
        )
        intent = OrderIntent(
            intent_id="current-session-entry-intent",
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
            plan=plan,
            reservation=reservation,
            intent=intent,
        )
        self.store.transition_intent(
            intent.intent_id,
            IntentState.SUBMITTING,
            occurred_at=when - timedelta(milliseconds=500),
        )
        return client_ref

    def _exercise_gap(
        self,
        *,
        prior: datetime,
        restarted: datetime,
        order_only: bool = False,
    ):
        writer_owner = self._activate(prior=prior)
        actions = _RecordingActions()

        # Durable in-session exposure exists, but the process never observes
        # that session's closeout/flat lanes.
        self._service(
            when=prior, order_only=order_only, actions=actions
        ).run_once()
        actions.clear()

        # The real runner renews this lease before each service iteration.
        self.store.heartbeat_writer_lease(
            account_key="ending-7153",
            owner_id=writer_owner,
            observed_at=restarted,
        )
        result = self._service(
            when=restarted, order_only=order_only, actions=actions
        ).run_once()

        self.assertIn(
            "MISSED_SESSION_CLOSEOUT_EXPOSURE",
            result.reconciliation_blockers,
        )
        self.assertEqual(result.mode_after, "MANAGED_CLOSEOUT")
        self.assertEqual(actions.protected, [])
        self.assertEqual(actions.target_assessments, 0)
        self.assertEqual(actions.discoveries, 0)
        self.assertEqual(len(actions.closed), 1)
        self.assertEqual(actions.closed[0][0], "CARRY")
        self.assertIn(
            actions.closed[0][1],
            {"CANCEL_ENTRY_ORDERS", "CANCEL_WORKING_ORDERS", "SUBMIT_SAFE_CLOSE"},
        )
        missed_action = next(
            item
            for item in result.actions
            if item.startswith("MISSED_SESSION_CLOSEOUT_EXPOSURE:")
        )
        self.assertLess(
            result.actions.index(missed_action),
            result.actions.index("VERIFY_OR_ESTABLISH_PROTECTION"),
        )
        incidents = self.store.rows(
            "SELECT * FROM incidents WHERE account_key=? AND category=?",
            ("ending-7153", "MISSED_SESSION_CLOSEOUT_EXPOSURE"),
        )
        self.assertEqual(len(incidents), 1)
        return result, json.loads(str(incidents[0]["detail_json"]))

    def test_next_day_regular_hours_is_reduce_only_closeout(self) -> None:
        prior = datetime(2026, 9, 8, 10, 0, tzinfo=NEW_YORK)
        restarted = datetime(2026, 9, 9, 10, 0, tzinfo=NEW_YORK)

        _result, detail = self._exercise_gap(
            prior=prior,
            restarted=restarted,
        )

        self.assertEqual(detail["prior_trading_date"], "2026-09-08")

    def test_working_order_surviving_prior_session_is_closeout_owned(self) -> None:
        prior = datetime(2026, 9, 8, 10, 0, tzinfo=NEW_YORK)
        restarted = datetime(2026, 9, 9, 10, 0, tzinfo=NEW_YORK)

        self._exercise_gap(
            prior=prior,
            restarted=restarted,
            order_only=True,
        )

    def test_weekend_and_holiday_gap_uses_last_trading_session(self) -> None:
        # Labor Day Monday is closed; Friday exposure is still mandatory
        # closeout ownership when the service returns Tuesday.
        prior = datetime(2026, 9, 4, 10, 0, tzinfo=NEW_YORK)
        restarted = datetime(2026, 9, 8, 10, 0, tzinfo=NEW_YORK)

        _result, detail = self._exercise_gap(
            prior=prior,
            restarted=restarted,
        )

        self.assertEqual(detail["prior_trading_date"], "2026-09-04")

    def test_early_close_gap_uses_early_flat_deadline(self) -> None:
        prior = datetime(2026, 11, 27, 12, 0, tzinfo=NEW_YORK)
        restarted = datetime(2026, 11, 30, 10, 0, tzinfo=NEW_YORK)

        _result, detail = self._exercise_gap(
            prior=prior,
            restarted=restarted,
        )

        required_flat = datetime.fromisoformat(detail["required_flat_at"])
        self.assertEqual(
            required_flat.astimezone(NEW_YORK),
            datetime(2026, 11, 27, 12, 55, tzinfo=NEW_YORK),
        )

    def test_first_seen_next_day_regular_exposure_is_reduce_only(self) -> None:
        prior = datetime(2026, 9, 8, 10, 0, tzinfo=NEW_YORK)
        restarted = datetime(2026, 9, 9, 10, 0, tzinfo=NEW_YORK)
        owner = self._activate(prior=prior)
        self.store.heartbeat_writer_lease(
            account_key="ending-7153",
            owner_id=owner,
            observed_at=restarted,
        )
        actions = _RecordingActions()

        result = self._service(
            when=restarted,
            order_only=False,
            actions=actions,
        ).run_once()

        self.assertIn(
            "MISSED_SESSION_CLOSEOUT_EXPOSURE",
            result.reconciliation_blockers,
        )
        self.assertEqual(result.mode_after, "MANAGED_CLOSEOUT")
        self.assertEqual(actions.protected, [])
        self.assertEqual(actions.target_assessments, 0)
        self.assertEqual(actions.discoveries, 0)
        self.assertEqual(actions.closed, [("CARRY", "SUBMIT_SAFE_CLOSE")])

    def test_incomplete_first_seen_exposure_cannot_claim_current_origin(self) -> None:
        prior = datetime(2026, 9, 8, 10, 0, tzinfo=NEW_YORK)
        restarted = datetime(2026, 9, 9, 10, 0, tzinfo=NEW_YORK)
        owner = self._activate(prior=prior)
        self.store.heartbeat_writer_lease(
            account_key="ending-7153",
            owner_id=owner,
            observed_at=restarted,
        )
        snapshot = replace(
            self._snapshot(restarted),
            standard_equity_orders_complete=False,
        )
        actions = _RecordingActions()
        service = FullLiveService(
            policy=self.policy,
            state=self.store,
            broker=FakeBrokerClient(
                initial_snapshot=snapshot,
                clock=lambda: restarted,
            ),
            notifications=build_local_outbox(
                self.store,
                "ending-7153",
                JsonlNotificationSink(self.temp / "notifications.jsonl"),
            ),
            actions=actions,
            clock=lambda: restarted,
        )

        result = service.run_once()

        self.assertIn("STANDARD_ORDERS_INCOMPLETE", result.reconciliation_blockers)
        self.assertIn(
            "MISSED_SESSION_CLOSEOUT_EXPOSURE",
            result.reconciliation_blockers,
        )
        self.assertEqual(result.mode_after, "MANAGED_CLOSEOUT")
        self.assertEqual(actions.protected, [])
        self.assertEqual(actions.discoveries, 0)

    def test_first_seen_premarket_exposure_remains_owned_at_open(self) -> None:
        prior = datetime(2026, 9, 8, 10, 0, tzinfo=NEW_YORK)
        premarket = datetime(2026, 9, 9, 8, 0, tzinfo=NEW_YORK)
        opened = datetime(2026, 9, 9, 10, 0, tzinfo=NEW_YORK)
        owner = self._activate(prior=prior)
        actions = _RecordingActions()
        self.store.heartbeat_writer_lease(
            account_key="ending-7153",
            owner_id=owner,
            observed_at=premarket,
        )

        blocked = self._service(
            when=premarket,
            order_only=False,
            actions=actions,
        ).run_once()

        self.assertIn(
            "MISSED_SESSION_CLOSEOUT_EXPOSURE",
            blocked.reconciliation_blockers,
        )
        self.assertEqual(blocked.mode_after, "INCIDENT")
        self.assertEqual(actions.protected, [])
        self.assertEqual(actions.target_assessments, 0)
        self.assertEqual(actions.discoveries, 0)

        actions.clear()
        self.store.heartbeat_writer_lease(
            account_key="ending-7153",
            owner_id=owner,
            observed_at=opened,
        )
        closeout = self._service(
            when=opened,
            order_only=False,
            actions=actions,
        ).run_once()

        self.assertIn(
            "MISSED_SESSION_CLOSEOUT_EXPOSURE",
            closeout.reconciliation_blockers,
        )
        self.assertEqual(closeout.mode_after, "MANAGED_CLOSEOUT")
        self.assertEqual(actions.protected, [])
        self.assertEqual(actions.target_assessments, 0)
        self.assertEqual(actions.discoveries, 0)
        self.assertEqual(actions.closed, [("CARRY", "SUBMIT_SAFE_CLOSE")])

    def test_first_seen_holiday_exposure_latches_until_next_open(self) -> None:
        prior = datetime(2026, 9, 4, 10, 0, tzinfo=NEW_YORK)
        holiday = datetime(2026, 9, 7, 10, 0, tzinfo=NEW_YORK)
        opened = datetime(2026, 9, 8, 10, 0, tzinfo=NEW_YORK)
        owner = self._activate(prior=prior)
        actions = _RecordingActions()
        self.store.heartbeat_writer_lease(
            account_key="ending-7153",
            owner_id=owner,
            observed_at=holiday,
        )

        blocked = self._service(
            when=holiday,
            order_only=False,
            actions=actions,
        ).run_once()

        self.assertIn(
            "MISSED_SESSION_CLOSEOUT_EXPOSURE",
            blocked.reconciliation_blockers,
        )
        self.assertEqual(blocked.mode_after, "INCIDENT")

        actions.clear()
        self.store.heartbeat_writer_lease(
            account_key="ending-7153",
            owner_id=owner,
            observed_at=opened,
        )
        closeout = self._service(
            when=opened,
            order_only=False,
            actions=actions,
        ).run_once()

        self.assertEqual(closeout.mode_after, "MANAGED_CLOSEOUT")
        self.assertEqual(actions.protected, [])
        self.assertEqual(actions.target_assessments, 0)
        self.assertEqual(actions.discoveries, 0)
        self.assertEqual(actions.closed, [("CARRY", "SUBMIT_SAFE_CLOSE")])

    def test_fill_first_observed_in_current_envelope_proves_same_session_origin(
        self,
    ) -> None:
        prior = datetime(2026, 9, 8, 10, 0, tzinfo=NEW_YORK)
        current = datetime(2026, 9, 9, 10, 0, tzinfo=NEW_YORK)
        owner = self._activate(prior=prior)
        client_ref = self._seed_current_entry_intent(when=current)
        self.store.heartbeat_writer_lease(
            account_key="ending-7153",
            owner_id=owner,
            observed_at=current,
        )
        fill = FillSnapshot(
            fill_id="current-session-fill",
            quantity=Decimal("2"),
            price=Decimal("10"),
            executed_at=current,
        )
        filled_order = OrderSnapshot(
            broker_order_id="current-session-order",
            account_masked="••••7153",
            symbol="CARRY",
            side=BrokerSide.BUY,
            order_type=EquityOrderType.LIMIT,
            state=BrokerOrderState.FILLED,
            requested_quantity=Decimal("2"),
            cumulative_filled_quantity=Decimal("2"),
            market_hours=MarketHours.REGULAR,
            time_in_force=TimeInForce.GFD,
            broker_updated_at=current,
            received_at=current,
            limit_price=Decimal("10"),
            client_ref_id=client_ref,
            fills=(fill,),
        )
        snapshot = replace(
            self._snapshot(current),
            equity_orders=(filled_order,),
        )
        actions = _RecordingActions()
        service = FullLiveService(
            policy=self.policy,
            state=self.store,
            broker=FakeBrokerClient(
                initial_snapshot=snapshot,
                clock=lambda: current,
            ),
            notifications=build_local_outbox(
                self.store,
                "ending-7153",
                JsonlNotificationSink(self.temp / "notifications.jsonl"),
            ),
            actions=actions,
            clock=lambda: current,
        )

        result = service.run_once()

        self.assertNotIn(
            "MISSED_SESSION_CLOSEOUT_EXPOSURE",
            result.reconciliation_blockers,
        )
        self.assertEqual(actions.closed, [])
        self.assertEqual(actions.protected, ["CARRY"])

    def test_proven_same_session_fill_after_flat_deadline_still_closes(self) -> None:
        prior = datetime(2026, 9, 8, 10, 0, tzinfo=NEW_YORK)
        fill_time = datetime(2026, 9, 9, 15, 54, tzinfo=NEW_YORK)
        current = datetime(2026, 9, 9, 15, 56, tzinfo=NEW_YORK)
        owner = self._activate(prior=prior)
        client_ref = self._seed_current_entry_intent(when=fill_time)
        self.store.heartbeat_writer_lease(
            account_key="ending-7153",
            owner_id=owner,
            observed_at=current,
        )
        fill = FillSnapshot(
            fill_id="post-deadline-current-session-fill",
            quantity=Decimal("2"),
            price=Decimal("10"),
            executed_at=fill_time,
        )
        filled_order = OrderSnapshot(
            broker_order_id="post-deadline-current-session-order",
            account_masked="••••7153",
            symbol="CARRY",
            side=BrokerSide.BUY,
            order_type=EquityOrderType.LIMIT,
            state=BrokerOrderState.FILLED,
            requested_quantity=Decimal("2"),
            cumulative_filled_quantity=Decimal("2"),
            market_hours=MarketHours.REGULAR,
            time_in_force=TimeInForce.GFD,
            broker_updated_at=current,
            received_at=current,
            limit_price=Decimal("10"),
            client_ref_id=client_ref,
            fills=(fill,),
        )
        snapshot = replace(
            self._snapshot(current),
            equity_orders=(filled_order,),
        )
        actions = _RecordingActions()
        service = FullLiveService(
            policy=self.policy,
            state=self.store,
            broker=FakeBrokerClient(
                initial_snapshot=snapshot,
                clock=lambda: current,
            ),
            notifications=build_local_outbox(
                self.store,
                "ending-7153",
                JsonlNotificationSink(self.temp / "notifications.jsonl"),
            ),
            actions=actions,
            clock=lambda: current,
        )

        result = service.run_once()

        self.assertNotIn(
            "MISSED_SESSION_CLOSEOUT_EXPOSURE",
            result.reconciliation_blockers,
        )
        self.assertEqual(result.mode_after, "MANAGED_CLOSEOUT")
        self.assertEqual(actions.protected, [])
        self.assertEqual(actions.target_assessments, 0)
        self.assertEqual(actions.discoveries, 0)
        self.assertEqual(actions.closed, [("CARRY", "SUBMIT_SAFE_CLOSE")])

    def test_proven_same_session_working_order_is_not_missed_closeout(self) -> None:
        prior = datetime(2026, 9, 8, 10, 0, tzinfo=NEW_YORK)
        current = datetime(2026, 9, 9, 10, 0, tzinfo=NEW_YORK)
        owner = self._activate(prior=prior)
        client_ref = self._seed_current_entry_intent(when=current)
        self.store.heartbeat_writer_lease(
            account_key="ending-7153",
            owner_id=owner,
            observed_at=current,
        )
        working_order = OrderSnapshot(
            broker_order_id="current-session-order",
            account_masked="••••7153",
            symbol="CARRY",
            side=BrokerSide.BUY,
            order_type=EquityOrderType.LIMIT,
            state=BrokerOrderState.CONFIRMED,
            requested_quantity=Decimal("2"),
            cumulative_filled_quantity=Decimal("0"),
            market_hours=MarketHours.REGULAR,
            time_in_force=TimeInForce.GFD,
            broker_updated_at=current,
            received_at=current,
            limit_price=Decimal("10"),
            client_ref_id=client_ref,
        )
        snapshot = replace(
            self._snapshot(current, order_only=True),
            equity_orders=(working_order,),
        )
        actions = _RecordingActions()
        service = FullLiveService(
            policy=self.policy,
            state=self.store,
            broker=FakeBrokerClient(
                initial_snapshot=snapshot,
                clock=lambda: current,
            ),
            notifications=build_local_outbox(
                self.store,
                "ending-7153",
                JsonlNotificationSink(self.temp / "notifications.jsonl"),
            ),
            actions=actions,
            clock=lambda: current,
        )

        result = service.run_once()

        self.assertNotIn(
            "MISSED_SESSION_CLOSEOUT_EXPOSURE",
            result.reconciliation_blockers,
        )
        self.assertEqual(actions.closed, [])


if __name__ == "__main__":
    unittest.main()
