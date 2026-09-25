"""Hermetic positive-evidence recovery tests for the IBKR intent journal."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from titan_brain.live.broker.base import (
    BrokerContractViolation,
    BrokerSide,
    EquityOrderType,
    FillSnapshot,
    MarketHours,
    OrderSnapshot,
    TimeInForce,
)
from titan_brain.live.broker.ibkr_ledger import IbkrExecutionLedger
from titan_brain.live.broker.ibkr_ledger_reconcile import IbkrLedgerReconciler
from titan_brain.live.models import BrokerOrderState


NOW = datetime(2026, 9, 14, 15, 0, tzinfo=timezone.utc)
MASK = "****3103"
CLIENT = 19736


class IbkrLedgerReconcilerTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.ledger = IbkrExecutionLedger(
            Path(temporary.name).resolve() / "ibkr.sqlite3",
            account_fingerprint="a" * 64,
            environment="live",
            client_id=CLIENT,
        )
        self.addCleanup(self.ledger.close)
        self.ref = str(uuid4())
        self.intent = self.ledger.allocate_intent(
            self.ref, "b" * 64, str(uuid4()), 40
        )
        self.ledger.mark_sending(self.ref)
        self.reconciler = IbkrLedgerReconciler(
            self.ledger, account_masked=MASK
        )

    def order(self, **changes) -> OrderSnapshot:
        values = dict(
            broker_order_id=f"ibkr:{CLIENT}:40",
            account_masked=MASK,
            symbol="TEST",
            side=BrokerSide.BUY,
            order_type=EquityOrderType.LIMIT,
            state=BrokerOrderState.CONFIRMED,
            requested_quantity=Decimal("2"),
            cumulative_filled_quantity=Decimal("0"),
            market_hours=MarketHours.REGULAR,
            time_in_force=TimeInForce.GFD,
            broker_updated_at=NOW,
            received_at=NOW,
            limit_price=Decimal("10"),
            client_ref_id=self.ref,
            fills=(),
            broker_perm_id=900,
        )
        values.update(changes)
        return OrderSnapshot(**values)

    def test_positive_order_and_fill_recover_unknown_idempotently(self) -> None:
        self.ledger.record_event(self.ref, "wire-unknown", "UNKNOWN")
        fill = FillSnapshot(
            fill_id="0001.abc.01.01",
            quantity=Decimal("1"),
            price=Decimal("10.01"),
            executed_at=NOW,
            fee=Decimal("0.35"),
            broker_perm_id=900,
            provider_commission=Decimal("0.35"),
            provider_commission_currency="USD",
        )
        order = self.order(
            state=BrokerOrderState.PARTIALLY_FILLED,
            cumulative_filled_quantity=Decimal("1"),
            fills=(fill,),
        )
        report = self.reconciler.reconcile_orders((order,))
        self.assertEqual(report.owned_orders, 1)
        self.assertEqual(report.recorded_fills, 1)
        self.assertEqual(report.reconciled_commissions, 1)
        recovered = self.ledger.lookup(self.ref)
        self.assertEqual(recovered.status, "ACK")
        self.assertEqual(recovered.perm_id, 900)
        self.assertEqual(recovered.fill_count, 1)
        self.reconciler.reconcile_orders((order,))
        self.assertEqual(self.ledger.lookup(self.ref).fill_count, 1)
        self.assertEqual(len(self.ledger.commissions(fill.fill_id)), 1)

        corrected = replace(
            fill,
            fee=Decimal("0.42"),
            provider_commission=Decimal("0.42"),
        )
        self.reconciler.reconcile_orders((replace(order, fills=(corrected,)),))
        self.assertEqual(
            tuple(item.commission for item in self.ledger.commissions(fill.fill_id)),
            (Decimal("0.35"), Decimal("0.42")),
        )

    def test_pending_and_terminal_cancel_recover_monotonically(self) -> None:
        pending = self.order(state=BrokerOrderState.PENDING_CANCELLED)
        self.reconciler.reconcile_orders((pending,))
        self.assertEqual(self.ledger.lookup(self.ref).status, "PENDING_CANCEL")
        cancelled = replace(
            pending,
            state=BrokerOrderState.CANCELLED,
            broker_updated_at=NOW,
        )
        self.reconciler.reconcile_orders((cancelled,))
        self.assertEqual(self.ledger.lookup(self.ref).status, "CANCELLED")

    def test_terminal_rejection_is_recorded_without_inventing_acceptance(self) -> None:
        self.ledger.record_event(self.ref, "wire-unknown", "UNKNOWN")
        self.reconciler.reconcile_orders((
            self.order(state=BrokerOrderState.REJECTED, broker_perm_id=None),
        ))
        result = self.ledger.lookup(self.ref)
        self.assertTrue(result.rejection_seen)
        self.assertFalse(result.acknowledgement_seen)
        self.assertFalse(result.can_transmit)

    def test_absence_never_changes_or_retries_unknown_intent(self) -> None:
        self.ledger.record_event(self.ref, "wire-unknown", "UNKNOWN")
        report = self.reconciler.reconcile_orders(())
        self.assertEqual(report.owned_orders, 0)
        result = self.ledger.lookup(self.ref)
        self.assertEqual(result.status, "UNKNOWN")
        self.assertFalse(result.can_transmit)

    def test_exact_ref_order_and_client_identity_are_all_required(self) -> None:
        wrong_ref = str(uuid4())
        cases = (
            self.order(client_ref_id=wrong_ref),
            self.order(broker_order_id=f"ibkr:{CLIENT}:41"),
            self.order(broker_order_id="ibkr:8:40"),
            self.order(broker_order_id="ibkr:perm:900"),
        )
        for order in cases:
            with self.subTest(identity=order.broker_order_id):
                with self.assertRaises(BrokerContractViolation):
                    self.reconciler.reconcile_orders((order,))

    def test_unowned_external_order_is_ignored(self) -> None:
        external = self.order(
            broker_order_id="ibkr:8:77",
            client_ref_id=str(uuid4()),
            broker_perm_id=901,
        )
        report = self.reconciler.reconcile_orders((external,))
        self.assertEqual(report.inspected_orders, 1)
        self.assertEqual(report.owned_orders, 0)

    def test_fill_requires_one_positive_consistent_permanent_identity(self) -> None:
        fill = FillSnapshot(
            fill_id="exec-1",
            quantity=Decimal("1"),
            price=Decimal("10"),
            executed_at=NOW,
        )
        with self.assertRaisesRegex(
            BrokerContractViolation, "FILL_PERMANENT_ID_MISSING"
        ):
            self.reconciler.reconcile_orders((
                self.order(
                    state=BrokerOrderState.PARTIALLY_FILLED,
                    cumulative_filled_quantity=Decimal("1"),
                    fills=(fill,),
                    broker_perm_id=None,
                ),
            ))

    def test_fill_without_provider_commission_rolls_back_ack_and_execution(self) -> None:
        fill = FillSnapshot(
            fill_id="exec-without-commission",
            quantity=Decimal("1"),
            price=Decimal("10"),
            executed_at=NOW,
            broker_perm_id=900,
        )
        with self.assertRaisesRegex(
            BrokerContractViolation, "FILL_COMMISSION_EVIDENCE_MISSING"
        ):
            self.reconciler.reconcile_orders((
                self.order(
                    state=BrokerOrderState.PARTIALLY_FILLED,
                    cumulative_filled_quantity=Decimal("1"),
                    fills=(fill,),
                ),
            ))

        after = self.ledger.lookup(self.ref)
        self.assertIsNotNone(after)
        assert after is not None
        self.assertFalse(after.acknowledgement_seen)
        self.assertIsNone(after.perm_id)
        self.assertEqual(after.fill_count, 0)
        self.assertEqual(self.ledger.fills(self.ref), ())
        self.assertEqual(self.ledger.commissions(fill.fill_id), ())

    def test_later_fill_conflict_rolls_back_the_entire_snapshot_batch(self) -> None:
        second_ref = str(uuid4())
        second = self.ledger.allocate_intent(
            second_ref, "c" * 64, str(uuid4()), 41
        )
        self.assertEqual(second.order_id, 41)
        self.ledger.mark_sending(second_ref)
        # Durable evidence from an earlier broker snapshot.  The next snapshot
        # contradicts its immutable execution facts only on the second order.
        self.ledger.record_fill(
            second_ref,
            "existing-execution",
            Decimal("1"),
            Decimal("20"),
            perm_id=901,
            executed_at=NOW,
        )

        first_fill = FillSnapshot(
            fill_id="first-new-execution",
            quantity=Decimal("1"),
            price=Decimal("10.01"),
            executed_at=NOW,
            broker_perm_id=900,
            provider_commission=Decimal("0.35"),
            provider_commission_currency="USD",
        )
        first_order = self.order(
            state=BrokerOrderState.PARTIALLY_FILLED,
            cumulative_filled_quantity=Decimal("1"),
            fills=(first_fill,),
        )
        conflicting_fill = FillSnapshot(
            fill_id="existing-execution",
            quantity=Decimal("1"),
            price=Decimal("20.01"),
            executed_at=NOW,
            broker_perm_id=901,
            provider_commission=Decimal("0.35"),
            provider_commission_currency="USD",
        )
        second_order = self.order(
            broker_order_id=f"ibkr:{CLIENT}:41",
            client_ref_id=second_ref,
            broker_perm_id=901,
            state=BrokerOrderState.PARTIALLY_FILLED,
            requested_quantity=Decimal("2"),
            cumulative_filled_quantity=Decimal("1"),
            fills=(conflicting_fill,),
        )

        with self.assertRaisesRegex(
            BrokerContractViolation, "RECONCILIATION_CONFLICT"
        ):
            self.reconciler.reconcile_orders((first_order, second_order))

        first_after = self.ledger.lookup(self.ref)
        self.assertIsNotNone(first_after)
        assert first_after is not None
        self.assertFalse(first_after.acknowledgement_seen)
        self.assertIsNone(first_after.perm_id)
        self.assertEqual(first_after.fill_count, 0)
        self.assertEqual(self.ledger.fills(self.ref), ())


if __name__ == "__main__":
    unittest.main()
