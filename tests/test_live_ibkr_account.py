"""Hermetic stable-account assembly tests; no broker or socket access."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from titan_brain.live.broker.base import (
    AccountSnapshot,
    ClientRefRecoverySource,
    FundsSnapshot,
    OrderFamily,
)
from titan_brain.live.broker.ibkr_account import IbkrStableAccountSnapshotReader
from titan_brain.live.broker.ibkr_read import IbkrWholeAccountReadBridge
from titan_brain.live.broker.production import CollectedObservation, OrderFamilyPage


NOW = datetime(2026, 9, 14, 14, 0, tzinfo=timezone.utc)
ACCOUNT = "U1234567"
MASK = "****4567"


def base_snapshot(*, cash: str = "1000") -> AccountSnapshot:
    return AccountSnapshot(
        account_masked=MASK,
        observed_at=NOW,
        received_at=NOW,
        account_state="active",
        account_type="INDIVIDUAL",
        funds=FundsSnapshot(
            total_value=Decimal("1000"),
            cash=Decimal(cash),
            buying_power=Decimal(cash),
            unleveraged_buying_power=Decimal(cash),
        ),
        equity_positions=(),
        equity_orders=(),
        option_position_count=0,
        option_order_count=0,
        advanced_order_count=0,
        standard_equity_positions_complete=True,
        standard_equity_orders_complete=False,
        option_positions_complete=True,
        option_orders_complete=False,
        advanced_orders_complete=False,
        auth_point_in_time=True,
        daily_realized_pnl=Decimal("0"),
        daily_realized_pnl_complete=True,
        risk_evidence_authoritative=True,
        risk_evidence_source="ibkr:reqPnL.realizedPnL:current-day",
        risk_evidence_as_of=NOW,
    )


class FakeWholeAccountReads(IbkrWholeAccountReadBridge):
    def __init__(self, snapshots):
        self.snapshots = list(snapshots)
        self.index = -1
        self.collection_id = ""

    def get_account_base(self, exact_account_id):
        if exact_account_id != ACCOUNT:
            raise AssertionError("wrong private account")
        self.index += 1
        snapshot = self.snapshots[self.index]
        self.collection_id = f"{self.index + 1:064x}"
        return CollectedObservation(
            snapshot=snapshot,
            collection_id=self.collection_id,
            request_started_at=NOW + timedelta(milliseconds=self.index * 10),
            request_completed_at=NOW + timedelta(milliseconds=self.index * 10 + 1),
            order_event_watermark="stable-watermark",
        )

    def list_order_family_page(self, exact_account_id, family, cursor):
        if exact_account_id != ACCOUNT or cursor is not None:
            raise AssertionError("invalid family request")
        return OrderFamilyPage(
            account_masked=MASK,
            family=family,
            snapshot_token=None,
            collection_id=self.collection_id,
            page_id=f"page-{self.index}-{family.value}",
            orders=(),
            active_order_count=0,
            observed_at=NOW,
            received_at=NOW,
            next_cursor=None,
            page_index=0,
            page_complete=True,
            provider_watermark="stable-watermark",
        )


class IbkrStableAccountSnapshotReaderTests(unittest.TestCase):
    def test_double_collection_returns_stable_bounded_snapshot_without_overclaiming(self):
        reads = FakeWholeAccountReads((base_snapshot(), base_snapshot()))
        reader = IbkrStableAccountSnapshotReader(
            reads=reads,
            exact_account_id=ACCOUNT,
            account_masked=MASK,
            clock=lambda: NOW,
        )
        snapshot = reader()
        self.assertFalse(snapshot.whole_broker_reconciled)
        self.assertEqual(reads.index, 1)
        self.assertFalse(reader.coverage.proves_whole_account_order_coverage)
        self.assertEqual(
            reader.coverage.client_ref_recovery_source,
            ClientRefRecoverySource.CURRENT_DAY_ORDER_ROSTER,
        )
        self.assertTrue(reader.coverage.supports_exact_client_ref_recovery)
        self.assertFalse(reader.coverage.negative_client_ref_results_authoritative)
        self.assertEqual(
            {item.family for item in reader.coverage.families}, set(OrderFamily)
        )
        self.assertTrue(
            all(item.includes_working_orders_across_dates for item in reader.coverage.families)
        )

    def test_material_motion_fails_closed(self):
        reader = IbkrStableAccountSnapshotReader(
            reads=FakeWholeAccountReads(
                (base_snapshot(cash="1000"), base_snapshot(cash="999"))
            ),
            exact_account_id=ACCOUNT,
            account_masked=MASK,
            clock=lambda: NOW,
        )
        with self.assertRaisesRegex(RuntimeError, "STATE_MOVED"):
            reader()
        with self.assertRaisesRegex(RuntimeError, "COVERAGE_UNAVAILABLE"):
            _ = reader.coverage


if __name__ == "__main__":
    unittest.main()
