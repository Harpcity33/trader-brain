"""A known unused opportunity is retryable; actual/possible exposure is not."""

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import unittest
from unittest.mock import patch

from titan_brain.live.broker import (
    BrokerSide,
    EquityOrderType,
    FakeBrokerClient,
    FakeFault,
    FillSnapshot,
    MarketHours,
    OrderSnapshot,
    PositionSnapshot,
    TimeInForce,
)
from titan_brain.live.execution import ExecutionStatus
from titan_brain.live.models import BrokerOrderState
from titan_brain.live.pipeline import PipelineStatus
from titan_brain.live.risk_runtime import SessionLatch
from tests import test_live_pipeline as fixtures

NOW = fixtures.NOW
structure = fixtures.structure


class RejectedOpportunityRetryTests(unittest.TestCase):
    setUp = fixtures.PipelineTests.setUp
    tearDown = fixtures.PipelineTests.tearDown
    pipeline = fixtures.PipelineTests.pipeline

    def run_opportunity(self, source_id, *, snapshot=None):
        item = structure(source_plan_id=source_id)
        return self.pipeline([item]).run_once(
            structures=[item],
            broker_snapshot=snapshot or self.snapshot,
            latch=SessionLatch(NOW.date()),
        )

    def reject(self, source_id="first", *, review=False):
        self.broker.inject_fault(
            FakeBrokerClient.REVIEW if review else FakeBrokerClient.PLACE,
            FakeFault.REVIEW_REJECTED if review else FakeFault.PLACE_REJECTED,
        )
        result = self.run_opportunity(source_id)
        self.assertEqual(result.status, PipelineStatus.ATTEMPT_FAILED)
        self.assertEqual(
            result.selected.execution.status,
            ExecutionStatus.FAILED if review else ExecutionStatus.REJECTED,
        )
        return result

    def assert_retry_blocked(self, *, snapshot=None):
        snapshot = snapshot or self.snapshot
        pipeline = self.pipeline([structure()])
        self.assertTrue(pipeline._prior_symbol_plan_today(
            "XYZ", NOW, broker_snapshot=snapshot
        ))
        calls_before = len(self.broker.calls)
        result = self.run_opportunity("new-opportunity", snapshot=snapshot)
        self.assertEqual(result.status, PipelineStatus.BLOCKED)
        self.assertEqual(len(self.broker.calls), calls_before)

    def order_for(self, result, *, state, filled=0, linked=True):
        fills = () if not filled else (
            FillSnapshot(
                fill_id="observed-fill", quantity=Decimal(filled),
                price=Decimal("10.02"), executed_at=NOW,
            ),
        )
        return OrderSnapshot(
            broker_order_id="observed-order",
            account_masked=self.snapshot.account_masked,
            symbol="XYZ", side=BrokerSide.BUY, order_type=EquityOrderType.LIMIT,
            state=state, requested_quantity=Decimal(result.selected.quantity),
            cumulative_filled_quantity=Decimal(filled),
            market_hours=MarketHours.REGULAR, time_in_force=TimeInForce.GFD,
            broker_updated_at=NOW, received_at=NOW, limit_price=Decimal("10.05"),
            client_ref_id=result.selected.execution.client_ref_id if linked else None,
            fills=fills,
        )

    def assert_unused_rejection_allows_new_plan(self, *, review):
        first = self.reject(review=review)
        calls_before = len(self.broker.calls)
        second = self.run_opportunity("new-opportunity")
        self.assertEqual(second.status, PipelineStatus.ACKNOWLEDGED)
        self.assertNotEqual(first.attempted_plan_id, second.attempted_plan_id)
        self.assertNotEqual(
            first.selected.execution.client_ref_id, second.selected.execution.client_ref_id
        )
        self.assertEqual(len(self.broker.calls), calls_before + 2)
        self.assertEqual(
            [row["state"] for row in self.store.rows(
                "SELECT state FROM risk_reservations ORDER BY rowid"
            )], ["RELEASED", "RESERVED"],
        )
        self.assertFalse(second.selected.plan.allow_reentry)
        self.assertFalse(second.selected.plan.allow_add)

    def test_known_place_rejection_allows_fresh_same_day_opportunity(self):
        self.assert_unused_rejection_allows_new_plan(review=False)

    def test_review_rejection_before_submission_allows_fresh_opportunity(self):
        self.assert_unused_rejection_allows_new_plan(review=True)

    def test_exact_rejected_plan_replay_never_resubmits(self):
        first = self.reject()
        calls_before = len(self.broker.calls)
        replay = self.run_opportunity("first")
        self.assertEqual(first.attempted_plan_id, replay.attempted_plan_id)
        self.assertTrue(replay.selected.execution.replay)
        self.assertEqual(len(self.broker.calls), calls_before)

    def test_multiple_conclusive_unused_rejections_allow_one_new_attempt(self):
        self.reject("first", review=True)
        self.reject("second")
        third = self.run_opportunity("third")
        self.assertEqual(third.status, PipelineStatus.ACKNOWLEDGED)
        self.assertEqual(len(self.store.rows("SELECT * FROM plans")), 3)
        self.assert_retry_blocked()

    def test_oldest_same_day_attempt_must_also_be_conclusively_unused(self):
        first = self.reject("first")
        self.reject("second")
        # Deliberately contradictory persisted evidence: never trust only the
        # most recent rejection when an earlier attempt still reserves risk.
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE risk_reservations SET state='RESERVED' WHERE reservation_id=?",
                (first.selected.execution.reservation_id,),
            )
        self.assert_retry_blocked()

    def test_unknown_before_acceptance_is_not_a_rejected_unused_attempt(self):
        self.broker.inject_fault(
            FakeBrokerClient.PLACE, FakeFault.PLACE_UNKNOWN_BEFORE_ACCEPT
        )
        self.assertEqual(
            self.run_opportunity("first").selected.execution.status, ExecutionStatus.UNKNOWN
        )
        self.assert_retry_blocked()

    def test_lost_acknowledgement_is_not_a_rejected_unused_attempt(self):
        self.broker.inject_fault(
            FakeBrokerClient.PLACE, FakeFault.PLACE_UNKNOWN_AFTER_ACCEPT
        )
        self.assertEqual(
            self.run_opportunity("first").selected.execution.status, ExecutionStatus.UNKNOWN
        )
        self.assert_retry_blocked()

    def test_released_label_without_immediate_zero_fill_proof_is_insufficient(self):
        with patch.object(self.store, "release_reservation", return_value=False):
            self.reject()
        with self.store.transaction() as connection:
            connection.execute("UPDATE risk_reservations SET state='RELEASED'")
        self.assert_retry_blocked()

    def test_prior_fill_remains_reentry_even_when_flat_and_reservation_released(self):
        first = self.run_opportunity("first")
        order = self.broker.fill_order(
            first.selected.execution.broker_order_id, first.selected.quantity, "10.02"
        )
        self.pipeline([structure()]).execution.persist_order_evidence(
            intent_id=first.selected.execution.intent_id, order=order
        )
        with self.store.transaction() as connection:
            connection.execute("UPDATE risk_reservations SET state='RELEASED'")
        self.assertEqual(len(self.store.rows("SELECT * FROM fills")), 1)
        self.assert_retry_blocked()

    def test_fractional_durable_fill_cannot_be_truncated_to_zero(self):
        first = self.reject()
        # SQLite INTEGER affinity is not a strict integer constraint. Even
        # malformed evidence bypassing the whole-share writer must block a buy.
        with self.store.transaction() as connection:
            connection.execute(
                """INSERT INTO broker_orders(
                       broker_order_id,intent_id,account_key,state,quantity,
                       cumulative_filled_quantity,revision,broker_updated_at,
                       received_at,raw_hash) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    "fractional-evidence", first.selected.execution.intent_id,
                    self.policy.config["account"]["masked_identifier"],
                    "REJECTED", first.selected.quantity, 0.5, 0,
                    NOW.isoformat(), NOW.isoformat(), "a" * 64,
                ),
            )
        self.assert_retry_blocked()

    def test_partial_fill_is_never_an_unused_attempt(self):
        first = self.run_opportunity("first")
        order = self.broker.fill_order(first.selected.execution.broker_order_id, 1, "10.02")
        self.pipeline([structure()]).execution.persist_order_evidence(
            intent_id=first.selected.execution.intent_id, order=order
        )
        self.assert_retry_blocked()

    def test_current_position_blocks_retry_of_rejected_plan(self):
        self.reject()
        snapshot = replace(self.snapshot, equity_positions=(PositionSnapshot(
            symbol="XYZ", quantity=Decimal("1"), sellable_quantity=Decimal("1"),
            average_price=Decimal("10.02"),
        ),))
        self.assert_retry_blocked(snapshot=snapshot)

    def test_unlinked_working_order_blocks_retry_of_rejected_plan(self):
        first = self.reject()
        snapshot = replace(self.snapshot, equity_orders=(self.order_for(
            first, state=BrokerOrderState.CONFIRMED, linked=False
        ),))
        self.assert_retry_blocked(snapshot=snapshot)

    def test_broker_terminal_fill_overrides_earlier_local_rejection(self):
        first = self.reject()
        snapshot = replace(self.snapshot, equity_orders=(self.order_for(
            first, state=BrokerOrderState.FILLED, filled=first.selected.quantity
        ),))
        self.assert_retry_blocked(snapshot=snapshot)

    def test_broker_cancelled_order_is_not_a_conclusive_rejection(self):
        first = self.reject()
        snapshot = replace(self.snapshot, equity_orders=(self.order_for(
            first, state=BrokerOrderState.CANCELLED
        ),))
        self.assert_retry_blocked(snapshot=snapshot)

    def test_matching_broker_zero_fill_rejection_is_compatible(self):
        first = self.reject()
        snapshot = replace(self.snapshot, equity_orders=(self.order_for(
            first, state=BrokerOrderState.REJECTED
        ),))
        second = self.run_opportunity("new-opportunity", snapshot=snapshot)
        self.assertEqual(second.status, PipelineStatus.ACKNOWLEDGED)

    def test_trading_day_uses_policy_timezone_not_utc_date(self):
        self.run_opportunity("first")
        pipeline = self.pipeline([structure()])
        # 00:01 UTC tomorrow is still the same New York trading date.
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE plans SET created_at=?",
                (datetime(2026, 9, 9, 0, 1, tzinfo=timezone.utc).isoformat(),),
            )
        self.assertTrue(pipeline._prior_symbol_plan_today(
            "XYZ", NOW, broker_snapshot=self.snapshot
        ))
        # 03:59 UTC today belongs to the previous New York date.
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE plans SET created_at=?",
                (datetime(2026, 9, 8, 3, 59, tzinfo=timezone.utc).isoformat(),),
            )
        self.assertFalse(pipeline._prior_symbol_plan_today(
            "XYZ", NOW, broker_snapshot=self.snapshot
        ))


if __name__ == "__main__":
    unittest.main()
