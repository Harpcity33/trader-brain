from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
import unittest
from zoneinfo import ZoneInfo

from titan_brain.live.activation import (
    ACTIVATION_SCHEMA,
    MACHINE_EVIDENCE_SOURCE,
    READINESS_SCHEMA,
    ActivationRecord,
    ReadinessEvidence,
)
from titan_brain.live.policy import PolicyBundle


ROOT = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 8, 8, 30, tzinfo=ET)


def readiness(policy: PolicyBundle, **overrides) -> ReadinessEvidence:
    raw = dict(
        collected_at=NOW - timedelta(seconds=1),
        release_manifest_hash="a" * 64,
        config_hash=policy.config_hash,
        policy_hash=policy.policy_hash,
        runtime_id=policy.runtime_id,
        database_schema_version=1,
        account_key="ending-7153",
        account_last4="7153",
        runtime_identity_valid=True,
        evidence_source=MACHINE_EVIDENCE_SOURCE,
        broker_connector="machine-test-broker",
        broker_read_attempted=True,
        broker_read_succeeded=True,
        broker_read_error_type=None,
        broker_account_last4="7153",
        broker_snapshot_received_at=NOW - timedelta(seconds=1),
        account_active=True,
        broker_authenticated=True,
        daemon_accessible_supported_client=True,
        unattended_mutation_supported=True,
        per_mutation_confirmation_required=False,
        durable_snapshot_id="broker-" + "1" * 64,
        durable_snapshot_received_at=NOW - timedelta(seconds=1),
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
        legacy_heartbeat_id="robinhood-momentum-engine",
        legacy_heartbeat_status="PAUSED",
        legacy_heartbeat_config_hash="2" * 64,
        old_writer_disabled=True,
        new_writer_lock_held=True,
        writer_lock_owner_id="activation-test",
        writer_lock_process_id=123,
        local_state_writable=True,
        audit_chain_valid=True,
        audit_chain_length=3,
        audit_chain_head="3" * 64,
        market_data_connected=True,
        market_data_resynced=True,
        market_data_blockers=(),
        tradability_provider_ready=True,
        notification_sink="verified-test-destination",
        notification_destination_configured=True,
        notification_delivery_receipt_hash="4" * 64,
        notification_delivered_at=NOW - timedelta(minutes=1),
        notification_tested=True,
        broker_snapshot_age_seconds=1,
        durable_snapshot_age_seconds=1,
        quote_age_seconds=1,
        completed_bar_age_seconds=30,
        probe_errors=(),
        schema_version=READINESS_SCHEMA,
    )
    raw.update(overrides)
    return ReadinessEvidence(**raw)


class ActivationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = PolicyBundle.load(ROOT)
        self.readiness = readiness(self.policy)
        self.record = ActivationRecord.build(
            release_manifest_hash="a" * 64,
            policy=self.policy,
            database_schema_version=1,
            created_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
            readiness=self.readiness,
        )

    def test_current_checked_in_policy_refuses_live_activation(self) -> None:
        with self.assertRaisesRegex(ValueError, "LIVE_ENTRIES_DISABLED"):
            self.record.validate(
                policy=self.policy,
                release_manifest_hash="a" * 64,
                database_schema_version=1,
                now=NOW,
                already_consumed=False,
                current_readiness=self.readiness,
            )

    def test_readiness_exposes_broker_writer_reconciliation_and_destination_failures(self) -> None:
        failures = readiness(
            self.policy,
            broker_read_succeeded=False,
            broker_read_error_type="BrokerCapabilityError",
            daemon_accessible_supported_client=False,
            per_mutation_confirmation_required=True,
            advanced_orders_reconciled=False,
            durable_account_flat=False,
            old_writer_disabled=False,
            notification_destination_configured=False,
            notification_tested=False,
            probe_errors=("broker_read:BrokerCapabilityError",),
        ).blockers(self.policy, now=NOW)
        for expected in (
            "BROKER_READ_FAILED",
            "DAEMON_BROKER_CLIENT_UNAVAILABLE",
            "PER_MUTATION_CONFIRMATION_REQUIRED",
            "WHOLE_BROKER_RECONCILIATION_INCOMPLETE",
            "ACCOUNT_NOT_FLAT_FOR_ACTIVATION",
            "OLD_ACCOUNT_WRITER_STILL_ENABLED",
            "NOTIFICATION_DESTINATION_UNVERIFIED",
            "READINESS_PROBE_ERROR",
        ):
            self.assertIn(expected, failures)

    def test_unknown_unprotected_or_stale_durable_state_blocks(self) -> None:
        failures = readiness(
            self.policy,
            unknown_submissions=1,
            uncovered_quantity=2,
            durable_snapshot_id=None,
            reconciliation_audit_event_id=None,
            broker_snapshot_age_seconds=6,
            durable_snapshot_age_seconds=None,
        ).blockers(self.policy, now=NOW)
        self.assertIn("UNKNOWN_SUBMISSION_PRESENT", failures)
        self.assertIn("UNPROTECTED_EXPOSURE_PRESENT", failures)
        self.assertIn("DURABLE_RECONCILIATION_EVIDENCE_MISSING", failures)
        self.assertIn("BROKER_SNAPSHOT_STALE", failures)
        self.assertIn("DURABLE_BROKER_SNAPSHOT_STALE", failures)

    def test_evidence_round_trip_is_strict_canonical_and_hash_stable(self) -> None:
        payload = self.readiness.to_payload()
        parsed = ReadinessEvidence.from_payload(payload)
        self.assertEqual(parsed, self.readiness)
        self.assertEqual(parsed.evidence_hash, self.readiness.evidence_hash)
        payload["operator_says_ready"] = True
        with self.assertRaisesRegex(ValueError, "fields differ"):
            ReadinessEvidence.from_payload(payload)

    def test_record_round_trip_recomputes_both_hashes(self) -> None:
        payload = self.record.to_payload()
        parsed = ActivationRecord.from_payload(payload)
        self.assertEqual(parsed.to_payload(), payload)
        self.assertEqual(parsed.activation_id, parsed.recomputed_activation_id())
        self.assertEqual(parsed.readiness_hash, parsed.readiness_evidence.evidence_hash)
        self.assertEqual(parsed.schema_version, ACTIVATION_SCHEMA)

    def test_record_tamper_and_one_use_fail_before_policy(self) -> None:
        with self.assertRaisesRegex(ValueError, "canonical record"):
            replace(self.record, activation_id="f" * 64).validate(
                policy=self.policy,
                release_manifest_hash="a" * 64,
                database_schema_version=1,
                now=NOW,
                already_consumed=False,
            )
        tampered_readiness = replace(self.readiness, notification_tested=False)
        with self.assertRaisesRegex(ValueError, "canonical record"):
            replace(self.record, readiness_evidence=tampered_readiness).validate(
                policy=self.policy,
                release_manifest_hash="a" * 64,
                database_schema_version=1,
                now=NOW,
                already_consumed=False,
            )
        with self.assertRaisesRegex(ValueError, "already consumed"):
            self.record.validate(
                policy=self.policy,
                release_manifest_hash="a" * 64,
                database_schema_version=1,
                now=NOW,
                already_consumed=True,
            )

    def test_noncanonical_account_and_non_machine_source_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "account_key"):
            readiness(self.policy, account_key="7153")
        with self.assertRaisesRegex(ValueError, "machine probe"):
            readiness(self.policy, evidence_source="operator_json")
        with self.assertRaisesRegex(ValueError, "crosses broker accounts"):
            ActivationRecord.build(
                release_manifest_hash="a" * 64,
                policy=self.policy,
                database_schema_version=1,
                created_at=NOW,
                expires_at=NOW + timedelta(minutes=5),
                readiness=readiness(self.policy, broker_account_last4="9999"),
            )


if __name__ == "__main__":
    unittest.main()
