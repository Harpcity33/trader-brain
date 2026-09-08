from __future__ import annotations

from contextlib import redirect_stderr
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import io
import tempfile
import unittest

from titan_brain.live.broker import FakeBrokerClient, PositionSnapshot
from titan_brain.live.broker.robinhood import RobinhoodBrokerAdapter
from titan_brain.live.cli import (
    ACCOUNT_KEY,
    InstallLayout,
    _machine_readiness,
    _probe_legacy_heartbeat,
    build_parser,
)
from titan_brain.live.policy import PolicyBundle
from titan_brain.live.state import LiveStateStore
from titan_brain.live.writer_lock import AccountWriterLock
from tests.live_activation_support import record_flat_reconciliation
from tests.test_live_service import account_snapshot


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 8, 12, 30, tzinfo=timezone.utc)


class StaticMarketHealth:
    def health(self, *, now: datetime):
        return SimpleNamespace(
            blockers=(),
            producer_fresh=True,
            latest_quote_at=now,
            latest_completed_bar_at=now,
        )


class CliReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.install = Path(self.temporary.name) / "full-live"
        self.layout = InstallLayout(self.install)
        self.layout.state_path.parent.mkdir(parents=True)
        self.policy = PolicyBundle.load(ROOT)
        self.manifest = {"release_manifest_hash": "a" * 64}
        self.store = LiveStateStore(self.layout.state_path)
        self.store.initialize_runtime(
            runtime_id=self.policy.runtime_id,
            account_key=ACCOUNT_KEY,
            release_manifest_hash="a" * 64,
            config_hash=self.policy.config_hash,
            policy_hash=self.policy.policy_hash,
            initialized_at=NOW,
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_current_connector_is_actually_read_and_truthfully_blocks(self) -> None:
        lock = AccountWriterLock(
            self.layout.lock_path, ACCOUNT_KEY, owner_id="readiness-test"
        )
        with lock:
            self.store.acquire_writer_lease(
                account_key=ACCOUNT_KEY,
                owner_id=lock.owner_id,
                acquired_at=NOW,
            )
            evidence = _machine_readiness(
                layout=self.layout,
                manifest=self.manifest,
                policy=self.policy,
                store=self.store,
                writer_lock=lock,
                now=NOW,
                broker=RobinhoodBrokerAdapter(),
                market_source=StaticMarketHealth(),
                legacy_heartbeat_path=Path(self.temporary.name) / "missing.toml",
            )
            self.store.release_writer_lease(
                account_key=ACCOUNT_KEY,
                owner_id=lock.owner_id,
                released_at=NOW,
            )
        self.assertTrue(evidence.broker_read_attempted)
        self.assertFalse(evidence.broker_read_succeeded)
        self.assertEqual(evidence.broker_read_error_type, "BrokerCapabilityError")
        self.assertFalse(evidence.daemon_accessible_supported_client)
        self.assertFalse(evidence.unattended_mutation_supported)
        self.assertTrue(evidence.per_mutation_confirmation_required)
        self.assertFalse(evidence.positions_reconciled)
        self.assertFalse(evidence.notification_destination_configured)
        self.assertIn("BROKER_READ_FAILED", evidence.blockers(self.policy, now=NOW))
        self.assertIn(
            "DURABLE_RECONCILIATION_EVIDENCE_MISSING",
            evidence.blockers(self.policy, now=NOW),
        )

    def test_active_legacy_heartbeat_is_machine_detected(self) -> None:
        target = Path(self.temporary.name) / "automation.toml"
        target.write_text(
            'id = "robinhood-momentum-engine"\nkind = "heartbeat"\nstatus = "ACTIVE"\n',
            encoding="utf-8",
        )
        heartbeat_id, status, digest, disabled, error = _probe_legacy_heartbeat(target)
        self.assertEqual(heartbeat_id, "robinhood-momentum-engine")
        self.assertEqual(status, "ACTIVE")
        self.assertEqual(len(digest or ""), 64)
        self.assertFalse(disabled)
        self.assertIsNone(error)

    def test_operator_readiness_json_is_not_a_cli_input(self) -> None:
        parser = build_parser()
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "prepare-activation",
                    "--install-root",
                    str(self.install),
                    "--readiness-json",
                    str(Path(self.temporary.name) / "claims.json"),
                ]
            )

    def test_fresh_exposure_cannot_reuse_bound_durable_flat_snapshot(self) -> None:
        durable, _ = record_flat_reconciliation(
            self.store,
            account_key=ACCOUNT_KEY,
            received_at=NOW - timedelta(seconds=1),
            label="old-flat",
        )
        position = PositionSnapshot(
            symbol="XYZ",
            quantity=Decimal("2"),
            sellable_quantity=Decimal("2"),
            average_price=Decimal("10"),
        )
        exposed = replace(
            account_snapshot(),
            observed_at=NOW,
            received_at=NOW,
            risk_evidence_as_of=NOW,
            equity_positions=(position,),
        )
        broker = FakeBrokerClient(initial_snapshot=exposed, clock=lambda: NOW)
        lock = AccountWriterLock(
            self.layout.lock_path, ACCOUNT_KEY, owner_id="activation-consume-test"
        )
        with lock:
            self.store.acquire_writer_lease(
                account_key=ACCOUNT_KEY,
                owner_id=lock.owner_id,
                acquired_at=NOW,
            )
            evidence = _machine_readiness(
                layout=self.layout,
                manifest=self.manifest,
                policy=self.policy,
                store=self.store,
                writer_lock=lock,
                now=NOW,
                broker=broker,
                market_source=StaticMarketHealth(),
                legacy_heartbeat_path=Path(self.temporary.name) / "missing.toml",
                persist_fresh_broker_read=False,
            )
            self.store.release_writer_lease(
                account_key=ACCOUNT_KEY,
                owner_id=lock.owner_id,
                released_at=NOW,
            )

        self.assertEqual(evidence.durable_snapshot_id, durable.snapshot_id)
        self.assertTrue(evidence.durable_account_flat)
        self.assertFalse(evidence.positions_reconciled)
        self.assertIn(
            "broker_read:DURABLE_MATERIAL_MISMATCH", evidence.probe_errors
        )
        blockers = evidence.blockers(self.policy, now=NOW)
        self.assertIn("WHOLE_BROKER_RECONCILIATION_INCOMPLETE", blockers)
        self.assertIn("READINESS_PROBE_ERROR", blockers)


if __name__ == "__main__":
    unittest.main()
