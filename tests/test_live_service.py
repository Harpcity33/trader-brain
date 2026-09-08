from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from titan_brain.live.broker import (
    AccountSnapshot,
    FakeBrokerClient,
    FundsSnapshot,
    PositionSnapshot,
)
from titan_brain.live.broker.robinhood import RobinhoodBrokerAdapter
from titan_brain.live.control import ControlInbox
from titan_brain.live.notifications import JsonlNotificationSink
from titan_brain.live.policy import PolicyBundle
from titan_brain.live.service import FullLiveService, ServiceRunner, build_local_outbox
from titan_brain.live.state import LiveStateStore
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
            total_value=Decimal("912.80"),
            cash=Decimal("912.80"),
            buying_power=Decimal("912.80"),
            unleveraged_buying_power=Decimal("912.80"),
            unsettled_funds=Decimal("602.93"),
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
        peak_equity=Decimal("912.80"),
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
