from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from titan_brain.live import cli
from titan_brain.live.cli import InstallLayout
from titan_brain.live.control import ControlError, ControlInbox, HmacControlAuthenticator
from titan_brain.live.composition import RuntimeComposition, RuntimeCompositionError
from titan_brain.live.models import BrokerSnapshot
from titan_brain.live.state import LiveStateStore, canonical_json
from titan_brain.live.writer_lock import AccountWriterLock
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
            authenticator=HmacControlAuthenticator(
                b"test-control-key-material-is-32-bytes-minimum",
                authorization_binding_id="f" * 64,
            ),
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
            equity=Decimal("1000.00"),
            cash=Decimal("1000.00"),
            unleveraged_buying_power=Decimal("1000.00"),
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
        self.assertEqual(result[0].detail, "ControlError:CONTROL_REQUEST_REJECTED")
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
        self.assertEqual(result[0].detail, "ControlError:CONTROL_REQUEST_REJECTED")
        self.assertEqual(self.store.runtime_status()["mode"], "RECONCILING")

    def test_managed_closeout_requires_secret_authenticated_request(self) -> None:
        unsigned = ControlInbox(
            self.root / "unsigned-control",
            account_key="ending-7153",
            runtime_id="full-live-control-test",
            release_manifest_hash=RELEASE,
            max_snapshot_age=timedelta(seconds=5),
        )
        with self.assertRaisesRegex(ControlError, "authenticated control authority"):
            unsigned.submit(
                "MANAGED_CLOSEOUT",
                reason="same uid forged closeout",
                requested_at=NOW + timedelta(seconds=2),
                activated_at=self.activated_at,
            )

    def test_rehashed_managed_closeout_tamper_fails_hmac(self) -> None:
        self.inbox.submit(
            "MANAGED_CLOSEOUT",
            reason="owner closeout",
            requested_at=NOW + timedelta(seconds=2),
            activated_at=self.activated_at,
        )
        path = next(self.inbox.inbox.glob("*.json"))
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["reason"] = "same uid rewrote and rehashed the request"
        body = dict(payload)
        body.pop("request_id")
        payload["request_id"] = hashlib.sha256(
            canonical_json(body).encode("utf-8")
        ).hexdigest()
        path.write_text(canonical_json(payload) + "\n", encoding="utf-8")

        result = self.inbox.drain(self.store, now=NOW + timedelta(seconds=2))

        self.assertEqual(result[0].status, "REJECTED")
        self.assertEqual(self.store.runtime_status()["mode"], "RECONCILING")

    def test_runtime_composition_binds_control_secret_to_signed_authorization(self) -> None:
        source_root = Path(__file__).resolve().parents[1]
        source = source_root / "src/titan_brain/live/control.py"
        encoded = source.read_bytes()
        manifest = {
            "release_manifest_hash": RELEASE,
            "files": [
                {
                    "path": "src/titan_brain/live/control.py",
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                    "size": len(encoded),
                }
            ],
        }
        composition = RuntimeComposition(
            control_authentication_key=b"runtime-only-control-secret-material-32-bytes",
            control_authorization_binding_id="f" * 64,
        )
        composition.bind_release(manifest, release_root=source_root)
        production = {
            "broker_adapter": "supported_production_transport",
            "production_authorization_binding_id": "f" * 64,
        }

        inbox = composition.control_inbox(
            self.root / "composed-control",
            account_key="ending-7153",
            runtime_id="full-live-control-test",
            release_manifest_hash=RELEASE,
            max_snapshot_age=timedelta(seconds=5),
            execution_config=production,
        )
        self.assertIsNotNone(inbox.authenticator)

        missing = RuntimeComposition()
        missing.bind_release(manifest, release_root=source_root)
        with self.assertRaisesRegex(RuntimeCompositionError, "no authenticated"):
            missing.control_inbox(
                self.root / "missing-control",
                account_key="ending-7153",
                runtime_id="full-live-control-test",
                release_manifest_hash=RELEASE,
                max_snapshot_age=timedelta(seconds=5),
                execution_config=production,
            )

        with self.assertRaisesRegex(RuntimeCompositionError, "differs from signed"):
            composition.control_inbox(
                self.root / "wrong-control",
                account_key="ending-7153",
                runtime_id="full-live-control-test",
                release_manifest_hash=RELEASE,
                max_snapshot_age=timedelta(seconds=5),
                execution_config={
                    **production,
                    "production_authorization_binding_id": "e" * 64,
                },
            )


class EmergencyControlCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source_root = Path(__file__).resolve().parents[1]
        self.layout = InstallLayout(self.root)
        self.layout.release_root = self.source_root
        self.layout.lock_path = self.root / "locks"
        self.account_key = "ibkr-live-ending-3103"
        self.binding = "f" * 64
        self.policy = SimpleNamespace(
            account_key=self.account_key,
            runtime_id="full-live-emergency-control-test",
            config_hash="b" * 64,
            policy_hash="c" * 64,
            execution_authority_mode="unattended",
            config={
                "execution": {
                    "execution_authority_mode": "unattended",
                    "broker_adapter": "supported_production_transport",
                    "production_account_binding_fingerprint": "d" * 64,
                    "production_authorization_binding_id": self.binding,
                },
                "evidence": {"broker_snapshot_max_age_seconds": 5},
            },
        )
        control_source = self.source_root / "src/titan_brain/live/control.py"
        encoded = control_source.read_bytes()
        self.manifest = {
            "release_manifest_hash": RELEASE,
            "source_commit": "emergency-control-test",
            "files": [
                {
                    "path": "src/titan_brain/live/control.py",
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                    "size": len(encoded),
                }
            ],
        }
        with LiveStateStore(self.layout.state_path) as store:
            store.initialize_runtime(
                runtime_id=self.policy.runtime_id,
                account_key=self.account_key,
                release_manifest_hash=RELEASE,
                config_hash=self.policy.config_hash,
                policy_hash=self.policy.policy_hash,
                initialized_at=NOW,
            )
            activate_canonical_runtime(
                store,
                created_at=NOW + timedelta(seconds=1),
                activated_at=NOW + timedelta(seconds=2),
                expires_at=NOW + timedelta(minutes=5),
                writer_owner_id="emergency-control-service",
            )
        self.service_lock = AccountWriterLock(
            self.layout.lock_path,
            self.account_key,
            owner_id="emergency-control-service",
            broker_account_binding_fingerprint=(
                self.policy.config["execution"][
                    "production_account_binding_fingerprint"
                ]
            ),
            authorization_binding_id=self.binding,
        )
        self.service_lock.acquire()
        self.addCleanup(self.service_lock.release)
        self.composition = RuntimeComposition(
            control_authentication_key=(
                b"runtime-only-emergency-control-secret-material"
            ),
            control_authorization_binding_id=self.binding,
        )

    def test_emergency_commands_enqueue_without_a_provider_assembly(self) -> None:
        commands = (
            (
                cli.command_pause_new_entries,
                SimpleNamespace(
                    install_root=str(self.root),
                    reason="operator pause",
                    runtime_composition=self.composition,
                ),
                "PAUSE_NEW_ENTRIES",
            ),
            (
                cli.command_managed_closeout,
                SimpleNamespace(
                    install_root=str(self.root),
                    reason="operator closeout",
                    runtime_composition=self.composition,
                ),
                "MANAGED_CLOSEOUT",
            ),
            (
                cli.command_deactivate,
                SimpleNamespace(
                    install_root=str(self.root),
                    reason="operator flat deactivation",
                    flatness_snapshot_id="flat-snapshot-test",
                    confirm=(
                        "DEACTIVATE FULL LIVE ibkr-live-ending-3103 "
                        "FLAT flat-snapshot-test"
                    ),
                    runtime_composition=self.composition,
                ),
                "DEACTIVATE_FLAT",
            ),
        )
        with mock.patch.object(
            cli, "InstallLayout", return_value=self.layout
        ), mock.patch.object(
            self.layout,
            "load_release",
            return_value=(self.manifest, self.policy),
        ), mock.patch.object(cli, "_print"):
            for handler, arguments, expected in commands:
                with self.subTest(command=expected):
                    self.assertEqual(handler(arguments), 0)

        requests = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((self.layout.control_path / "inbox").glob("*.json"))
        ]
        self.assertEqual(
            {item["command"] for item in requests},
            {
                "PAUSE_NEW_ENTRIES",
                "MANAGED_CLOSEOUT",
                "DEACTIVATE_FLAT",
            },
        )
        for request in requests:
            self.assertEqual(request["account_key"], self.account_key)
            self.assertEqual(request["runtime_id"], self.policy.runtime_id)
            self.assertEqual(request["release_manifest_hash"], RELEASE)
        closeout = next(
            item for item in requests if item["command"] == "MANAGED_CLOSEOUT"
        )
        self.assertEqual(
            closeout["control_authorization_binding_id"], self.binding
        )
        self.assertEqual(len(closeout["control_authorization_tag"]), 64)
        self.assertNotIn(
            "runtime-only-emergency-control-secret-material",
            json.dumps(requests, sort_keys=True),
        )

    def test_status_reads_only_local_state_and_never_resolves_composition(self) -> None:
        provider_factory = mock.Mock(
            side_effect=AssertionError("status resolved provider composition")
        )
        arguments = SimpleNamespace(
            install_root=str(self.root),
            runtime_composition=provider_factory,
        )
        with mock.patch.object(
            cli, "InstallLayout", return_value=self.layout
        ), mock.patch.object(
            self.layout,
            "load_release",
            return_value=(self.manifest, self.policy),
        ), mock.patch.object(
            cli,
            "LiveStateStore",
            side_effect=AssertionError("status opened a writable state store"),
        ), mock.patch.object(cli, "_print") as output:
            self.assertEqual(cli.command_status(arguments), 0)
        provider_factory.assert_not_called()
        report = output.call_args.args[0]
        self.assertTrue(report["runtime_bindings_valid"])
        self.assertTrue(report["service_writer_lease_present"])
        self.assertFalse(report["provider_checks_performed"])
        self.assertEqual(report["runtime"]["account_key"], self.account_key)
        self.assertEqual(report["control_requests"]["inbox"], 0)


if __name__ == "__main__":
    unittest.main()
