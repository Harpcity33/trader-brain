from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from titan_brain.live.control import ControlInbox
from titan_brain.live.models import BrokerSnapshot
from titan_brain.live.state import LiveStateStore
from tests.live_activation_support import activate_canonical_runtime


UTC = timezone.utc
NOW = datetime(2026, 9, 8, 13, 0, tzinfo=UTC)
RELEASE = "a" * 64


class ControlInboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = LiveStateStore(self.root / "state.sqlite3")
        self.store.initialize_runtime(
            runtime_id="full-live-control-test",
            account_key="ending-7153",
            release_manifest_hash=RELEASE,
            config_hash="b" * 64,
            policy_hash="c" * 64,
            initialized_at=NOW,
        )
        activate_canonical_runtime(
            self.store,
            created_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
            activated_at=NOW + timedelta(seconds=1),
        )
        self.inbox = ControlInbox(
            self.root / "control",
            account_key="ending-7153",
            runtime_id="full-live-control-test",
            release_manifest_hash=RELEASE,
            max_snapshot_age=timedelta(seconds=5),
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    @property
    def activated_at(self) -> str:
        return str(self.store.runtime_status()["activated_at"])

    def flat_snapshot(self, second: int) -> BrokerSnapshot:
        observed = NOW + timedelta(seconds=second)
        return BrokerSnapshot(
            snapshot_id=f"flat-{second}",
            account_key="ending-7153",
            evidence_revision=f"revision-{second}",
            observed_at=observed,
            received_at=observed + timedelta(milliseconds=10),
            account_state="active",
            equity=Decimal("912.80"),
            cash=Decimal("912.80"),
            unleveraged_buying_power=Decimal("912.80"),
            realized_pnl=Decimal("0"),
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

    def test_pause_closeout_and_flat_deactivation_are_applied_by_one_consumer(self) -> None:
        pause = self.inbox.submit(
            "PAUSE_NEW_ENTRIES",
            reason="owner pause",
            requested_at=NOW + timedelta(seconds=2),
            activated_at=self.activated_at,
        )
        first = self.inbox.drain(self.store, now=NOW + timedelta(seconds=2))
        self.assertEqual(first[0].request_id, pause["request_id"])
        self.assertEqual(first[0].status, "APPLIED")
        self.assertEqual(self.store.runtime_status()["mode"], "PAUSE_NEW_ENTRIES")

        self.inbox.submit(
            "MANAGED_CLOSEOUT",
            reason="owner closeout",
            requested_at=NOW + timedelta(seconds=3),
            activated_at=self.activated_at,
        )
        self.inbox.drain(self.store, now=NOW + timedelta(seconds=3))
        self.assertEqual(self.store.runtime_status()["mode"], "MANAGED_CLOSEOUT")

        snapshot = self.flat_snapshot(4)
        self.store.record_broker_snapshot(snapshot)
        self.store.reconcile_positions(
            snapshot_id=snapshot.snapshot_id,
            account_key="ending-7153",
            positions=(),
            reconciled_at=snapshot.received_at,
        )
        self.inbox.submit(
            "DEACTIVATE_FLAT",
            reason="owner flat rollback",
            requested_at=NOW + timedelta(seconds=5),
            activated_at=self.activated_at,
            arguments={"flatness_snapshot_id": snapshot.snapshot_id},
        )
        final = self.inbox.drain(self.store, now=NOW + timedelta(seconds=5))
        self.assertEqual(final[0].status, "APPLIED")
        self.assertEqual(self.store.runtime_status()["mode"], "PAUSED")
        self.assertEqual(self.store.runtime_status()["authority_enabled"], 0)
        self.assertEqual(len(tuple(self.inbox.processed.glob("*.json"))), 3)
        self.assertEqual(len(tuple(self.inbox.inbox.glob("*.json"))), 0)

    def test_tampered_request_is_quarantined_without_runtime_transition(self) -> None:
        self.inbox.submit(
            "PAUSE_NEW_ENTRIES",
            reason="owner pause",
            requested_at=NOW + timedelta(seconds=2),
            activated_at=self.activated_at,
        )
        path = next(self.inbox.inbox.glob("*.json"))
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["reason"] = "tampered"
        path.write_text(json.dumps(payload), encoding="utf-8")
        result = self.inbox.drain(self.store, now=NOW + timedelta(seconds=2))
        self.assertEqual(result[0].status, "REJECTED")
        self.assertIn("hash differs", result[0].detail)
        self.assertEqual(self.store.runtime_status()["mode"], "RECONCILING")
        self.assertEqual(len(tuple(self.inbox.rejected.glob("*.json"))), 1)

    def test_expired_or_prior_activation_request_never_applies(self) -> None:
        self.inbox.submit(
            "PAUSE_NEW_ENTRIES",
            reason="stale request",
            requested_at=NOW + timedelta(seconds=2),
            activated_at=self.activated_at,
        )
        result = self.inbox.drain(self.store, now=NOW + timedelta(minutes=6))
        self.assertEqual(result[0].status, "REJECTED")
        self.assertIn("expired", result[0].detail)
        self.assertEqual(self.store.runtime_status()["mode"], "RECONCILING")


if __name__ == "__main__":
    unittest.main()
