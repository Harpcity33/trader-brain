from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from titan_brain.live.eod_live import build_eod_evidence, write_eod_evidence
from titan_brain.live.models import BrokerSnapshot
from titan_brain.live.state import LiveStateStore


class LiveEODTests(unittest.TestCase):
    def test_flatness_requires_complete_whole_broker_evidence_and_packet_is_create_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = datetime(2026, 9, 8, 20, 1, tzinfo=timezone.utc)
            with LiveStateStore(root / "state.sqlite3") as store:
                store.initialize_runtime(
                    runtime_id="runtime",
                    account_key="ending-7153",
                    release_manifest_hash="a" * 64,
                    config_hash="b" * 64,
                    policy_hash="c" * 64,
                    initialized_at=now,
                )
                store.record_broker_snapshot(
                    BrokerSnapshot(
                        snapshot_id="snapshot-1",
                        account_key="ending-7153",
                        evidence_revision="revision-1",
                        observed_at=now,
                        received_at=now,
                        account_state="active",
                        equity=Decimal("912.80"),
                        cash=Decimal("912.80"),
                        unleveraged_buying_power=Decimal("912.80"),
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
                        advanced_orders_reconciled=True,
                        realized_pnl_reconciled=True,
                        positions_digest="d" * 64,
                        orders_digest="e" * 64,
                    )
                )
                evidence = build_eod_evidence(
                    store,
                    account_key="ending-7153",
                    trading_date=date(2026, 9, 8),
                    generated_at=now,
                )
                self.assertTrue(evidence["flat_proven"])
                self.assertTrue(evidence["audit_chain"]["valid"])
                target = root / "eod.json"
                digest = write_eod_evidence(target, evidence)
                self.assertEqual(len(digest), 64)
                with self.assertRaises(FileExistsError):
                    write_eod_evidence(target, evidence)


if __name__ == "__main__":
    unittest.main()
