from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from titan_brain.live.activation import (
    MACHINE_EVIDENCE_SOURCE,
    ActivationRecord,
    ReadinessEvidence,
)
from titan_brain.live.broker import FakeBrokerClient
from titan_brain.live.cli import InstallLayout, _account_writer_lock, command_deactivate
from titan_brain.live.policy import PolicyBundle, sha256_json
from titan_brain.live.risk_evidence_binding import risk_high_water_receipt_hash
from titan_brain.live.service import FullLiveService, build_enqueue_only_outbox
from titan_brain.live.state import LiveStateStore


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 8, 12, 30, tzinfo=timezone.utc)
ACCOUNT_KEY = "ibkr-live-7153"


def parameterized_policy() -> PolicyBundle:
    base = PolicyBundle.load(ROOT)
    config = copy.deepcopy(base.config)
    config["account"]["account_key"] = ACCOUNT_KEY
    policy_hash = sha256_json(
        {
            "account": config["account"],
            "scope": config["scope"],
            "sessions": config["sessions"],
            "risk": config["risk"],
            "risk_hash": base.risk_hash,
            "strategy_id": config["strategy_id"],
        }
    )
    policy = replace(
        base,
        config=config,
        config_hash=sha256_json(config),
        policy_hash=policy_hash,
    )
    policy.validate()
    return policy


def readiness(policy: PolicyBundle) -> ReadinessEvidence:
    return ReadinessEvidence(
        collected_at=NOW,
        release_manifest_hash="a" * 64,
        config_hash=policy.config_hash,
        policy_hash=policy.policy_hash,
        runtime_id=policy.runtime_id,
        database_schema_version=1,
        account_key=policy.account_key,
        account_last4=policy.account_last4,
        runtime_identity_valid=True,
        evidence_source=MACHINE_EVIDENCE_SOURCE,
        broker_connector="test-broker",
        broker_read_attempted=True,
        broker_read_succeeded=True,
        broker_read_error_type=None,
        broker_account_last4=policy.account_last4,
        broker_snapshot_received_at=NOW,
        account_active=True,
        broker_authenticated=True,
        daemon_accessible_supported_client=True,
        unattended_mutation_supported=True,
        per_mutation_confirmation_required=False,
        durable_snapshot_id="broker-" + "1" * 64,
        durable_snapshot_received_at=NOW,
        reconciliation_audit_event_id="reconciled-test",
        standard_orders_reconciled=True,
        option_positions_reconciled=True,
        option_orders_reconciled=True,
        advanced_orders_reconciled=True,
        positions_reconciled=True,
        realized_pnl_reconciled=True,
        reconciliation_blocker_count=0,
        durable_account_flat=True,
        unknown_submissions=0,
        uncovered_quantity=0,
        legacy_heartbeat_id="legacy-test",
        legacy_heartbeat_status="PAUSED",
        legacy_heartbeat_config_hash="2" * 64,
        old_writer_disabled=True,
        new_writer_lock_held=True,
        writer_lock_owner_id="test-owner",
        writer_lock_process_id=1,
        local_state_writable=True,
        audit_chain_valid=True,
        audit_chain_length=0,
        audit_chain_head="0" * 64,
        market_data_connected=True,
        market_data_resynced=True,
        market_data_blockers=(),
        tradability_provider_ready=True,
        notification_sink="test-route",
        notification_destination_configured=True,
        notification_delivery_receipt_hash="3" * 64,
        notification_delivered_at=NOW,
        notification_tested=True,
        broker_snapshot_age_seconds=0,
        durable_snapshot_age_seconds=0,
        quote_age_seconds=0,
        completed_bar_age_seconds=0,
        probe_errors=(),
        execution_authority_mode=policy.execution_authority_mode,
        attended_mutation_supported=False,
        broker_command_connected=True,
        broker_command_next_valid_id_received=True,
        broker_command_account_authenticated=True,
        broker_command_write_authority_granted=False,
        entry_risk_evidence_ready=True,
        weekly_realized_pnl_complete=True,
        peak_equity_complete=True,
        risk_evidence_as_of=NOW,
        risk_evidence_age_seconds=0,
        risk_baseline_identity_hash="4" * 64,
        risk_baseline_receipt_hash="5" * 64,
        risk_high_water_identity_hash="6" * 64,
        risk_high_water_lineage_hash="7" * 64,
        risk_high_water_peak_equity="1000",
        risk_high_water_receipt_hash=risk_high_water_receipt_hash(
            identity_hash="6" * 64,
            baseline_receipt_hash="5" * 64,
            lineage_hash="7" * 64,
            peak_equity=1000,
        ),
    )


class AccountParameterizationTests(unittest.TestCase):
    def test_legacy_policy_uses_ending_7153_when_explicit_key_is_absent(self) -> None:
        base = PolicyBundle.load(ROOT)
        config = copy.deepcopy(base.config)
        config["account"].pop("account_key")
        legacy = replace(base, config=config)
        legacy.validate()
        self.assertEqual(legacy.account_key, "ending-7153")

    def test_policy_and_activation_bind_to_opaque_configured_key(self) -> None:
        policy = parameterized_policy()
        evidence = readiness(policy)
        evidence.validate_bindings(
            policy=policy,
            release_manifest_hash="a" * 64,
            database_schema_version=1,
        )
        record = ActivationRecord.build(
            release_manifest_hash="a" * 64,
            policy=policy,
            database_schema_version=1,
            created_at=NOW,
            expires_at=NOW + timedelta(minutes=1),
            readiness=evidence,
        )
        self.assertEqual(record.account_key, ACCOUNT_KEY)
        with self.assertRaisesRegex(ValueError, "LIVE_ENTRIES_DISABLED"):
            record.validate(
                policy=policy,
                release_manifest_hash="a" * 64,
                database_schema_version=1,
                now=NOW,
                already_consumed=False,
                current_readiness=evidence,
            )

    def test_runtime_lock_outbox_and_audit_use_configured_key(self) -> None:
        policy = parameterized_policy()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layout = InstallLayout(root)
            lock_root = root / "locks"
            with mock.patch(
                "titan_brain.live.cli.user_account_writer_lock_directory",
                return_value=lock_root,
            ):
                lock = _account_writer_lock(layout, policy, owner_id="parameter-test")
            expected_fingerprint = hashlib.sha256(ACCOUNT_KEY.encode("utf-8")).hexdigest()
            self.assertEqual(lock.account_fingerprint, expected_fingerprint)
            with LiveStateStore(layout.state_path) as store:
                store.initialize_runtime(
                    runtime_id=policy.runtime_id,
                    account_key=policy.account_key,
                    release_manifest_hash="a" * 64,
                    config_hash=policy.config_hash,
                    policy_hash=policy.policy_hash,
                    initialized_at=NOW,
                )
                service = FullLiveService(
                    policy=policy,
                    state=store,
                    broker=FakeBrokerClient(),
                    notifications=build_enqueue_only_outbox(store, policy.account_key),
                )
                self.assertEqual(service.account_key, ACCOUNT_KEY)
                message_id = build_enqueue_only_outbox(
                    store, service.account_key
                ).enqueue(
                    "READINESS", {"event_id": "parameterized-account"}, NOW
                )
                store.append_event(
                    stream=policy.account_key,
                    event_type="PARAMETERIZATION_TEST",
                    entity_type="test",
                    entity_id=message_id,
                    occurred_at=NOW,
                    payload={"account_key": policy.account_key},
                )
                self.assertEqual(
                    store.rows(
                        "SELECT DISTINCT account_key FROM notification_outbox"
                    )[0]["account_key"],
                    ACCOUNT_KEY,
                )
                self.assertEqual(
                    store.rows("SELECT stream FROM audit_events")[0]["stream"],
                    ACCOUNT_KEY,
                )

    def test_deactivation_phrase_uses_configured_key(self) -> None:
        policy = parameterized_policy()
        layout = InstallLayout("/tmp/titan-account-parameterization")
        request = SimpleNamespace(
            install_root=str(layout.root),
            flatness_snapshot_id="flat-test",
            confirm=f"DEACTIVATE FULL LIVE {ACCOUNT_KEY} FLAT flat-test",
            reason="test",
        )
        with (
            mock.patch.object(InstallLayout, "load_release", return_value=({}, policy)),
            mock.patch(
                "titan_brain.live.cli._queue_runtime_control",
                return_value={"queued": True},
            ) as queue,
            mock.patch("titan_brain.live.cli._print"),
        ):
            self.assertEqual(command_deactivate(request), 0)
        self.assertEqual(queue.call_args.kwargs["command"], "DEACTIVATE_FLAT")


if __name__ == "__main__":
    unittest.main()
