from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from titan_brain.live.broker.ibkr_preflight import IbkrOrderPurpose
from titan_brain.live.ibkr_autonomous_authority import (
    IbkrAutonomousAuthorityBindings,
)
from titan_brain.live.ibkr_command_inputs import (
    DurableIbkrRiskPolicyCheck,
    IbkrCommandInputError,
)
from titan_brain.live.ibkr_autonomous_interlock import (
    AutonomousIbkrWriterInterlock,
    IbkrAutonomousInterlockError,
)
from titan_brain.live.policy import PolicyBundle, canonical_json, sha256_json
from titan_brain.live.state import LiveStateStore
from titan_brain.live.writer_lock import AccountWriterLock
from tests.live_activation_support import activate_canonical_runtime


ROOT = Path(__file__).resolve().parents[1]
RELEASE = "8" * 64
ACCOUNT_BINDING = "a" * 64
AUTHORIZATION_BINDING = "b" * 64
PROVIDER_CONTRACT = "d" * 64
TRANSPORT = "ibkr-tws-api-10.50.2-v1"
CLIENT_ID = 903


def autonomous_policy() -> PolicyBundle:
    base = PolicyBundle.load(
        ROOT, config_relative="config/full_live_ibkr.json"
    )
    config = dict(base.config)
    execution = dict(config["execution"])
    execution.update(
        {
            "execution_authority_mode": "unattended",
            "broker_adapter": "supported_production_transport",
            "production_transport_id": TRANSPORT,
            "production_account_binding_fingerprint": ACCOUNT_BINDING,
            "production_authorization_binding_id": AUTHORIZATION_BINDING,
            "ibkr_provider_contract_id": PROVIDER_CONTRACT,
            "supported_unattended_mutation": True,
            "per_mutation_user_confirmation_required": False,
            "local_mutation_interlock_enabled": True,
            "durable_intent_before_submit": True,
            "automatic_retry_unknown_submission": False,
            "one_account_writer_required": True,
        }
    )
    config["execution"] = execution
    return replace(base, config=config, config_hash=sha256_json(config))


class AutonomousIbkrWriterInterlockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.policy = autonomous_policy()
        self.bindings = IbkrAutonomousAuthorityBindings(
            release_manifest_hash=RELEASE,
            config_hash=self.policy.config_hash,
            policy_binding_id=self.policy.policy_hash,
            account_key=self.policy.account_key,
            account_masked="****3103",
            account_binding_fingerprint=ACCOUNT_BINDING,
            authorization_binding_id=AUTHORIZATION_BINDING,
            provider_contract_id=PROVIDER_CONTRACT,
            transport_id=TRANSPORT,
            api_name="ibapi",
            api_version="10.50.2",
            environment="live",
            client_id=CLIENT_ID,
        )
        self.now = datetime.now(timezone.utc) + timedelta(seconds=1)
        self.clock_value = self.now
        self.lock = AccountWriterLock(
            self.root / "locks",
            self.policy.account_key,
            owner_id="autonomous-service-owner",
            broker_account_binding_fingerprint=ACCOUNT_BINDING,
            authorization_binding_id=AUTHORIZATION_BINDING,
        )
        self.lock.acquire(acquired_at=self.now - timedelta(seconds=15))
        self.addCleanup(self.lock.release)
        self.database = self.root / "full-live.sqlite3"
        self.store = LiveStateStore(self.database)
        self.addCleanup(self.store.close)
        self.store.initialize_runtime(
            runtime_id=self.policy.runtime_id,
            account_key=self.policy.account_key,
            release_manifest_hash=RELEASE,
            config_hash=self.policy.config_hash,
            policy_hash=self.policy.policy_hash,
            initialized_at=self.now - timedelta(seconds=10),
        )
        generation = self.store.acquire_writer_lease(
            account_key=self.policy.account_key,
            owner_id=self.lock.owner_id,
            acquired_at=self.now - timedelta(seconds=15),
        )
        self.lock.bind_writer_lease(generation)
        self.activation, _ = activate_canonical_runtime(
            self.store,
            created_at=self.now - timedelta(seconds=3),
            activated_at=self.now - timedelta(seconds=2),
            expires_at=self.now + timedelta(minutes=3),
            writer_owner_id=self.lock.owner_id,
            readiness_overrides={
                "execution_authority_mode": "unattended",
                "attended_mutation_supported": False,
                "unattended_mutation_supported": True,
                "per_mutation_confirmation_required": False,
                "broker_account_binding_fingerprint": ACCOUNT_BINDING,
                "broker_authorization_binding_id": AUTHORIZATION_BINDING,
            },
        )
        self.interlock = AutonomousIbkrWriterInterlock(
            lock=self.lock,
            state=self.store,
            state_path=self.database,
            state_identity=(
                self.database.stat().st_dev,
                self.database.stat().st_ino,
            ),
            policy=self.policy,
            release_manifest_hash=RELEASE,
            authority_bindings=self.bindings,
            clock=lambda: self.clock_value,
            maximum_lease_age=timedelta(seconds=10),
        )

    def assert_code(self, expected: str, callback) -> None:
        with self.assertRaises(IbkrAutonomousInterlockError) as caught:
            callback()
        self.assertEqual(str(caught.exception), expected)
        self.assertEqual(caught.exception.code, expected)

    def sql(self, statement: str, parameters=()) -> None:
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(statement, parameters)
            connection.commit()
        finally:
            connection.close()

    def replace_activation_readiness(self, readiness) -> None:
        provisional = replace(
            self.activation,
            activation_id="0" * 64,
            readiness_hash=readiness.evidence_hash,
            readiness_evidence=readiness,
        )
        changed = replace(
            provisional,
            activation_id=provisional.recomputed_activation_id(),
        )
        payload = changed.to_payload()
        self.sql(
            """UPDATE activation_records
                  SET activation_id=?,record_hash=?,record_json=?
                WHERE activation_id=?""",
            (
                changed.activation_id,
                sha256_json(payload),
                canonical_json(payload),
                self.activation.activation_id,
            ),
        )

    def test_valid_join_refreshes_only_the_already_held_kernel_lock(self) -> None:
        before = dict(
            self.store.rows(
                "SELECT * FROM account_writer_lease WHERE account_key=?",
                (self.policy.account_key,),
            )[0]
        )
        with patch.object(self.lock, "refresh", wraps=self.lock.refresh) as refresh:
            self.assertIsNone(self.interlock())
        refresh.assert_called_once_with()
        after = dict(
            self.store.rows(
                "SELECT * FROM account_writer_lease WHERE account_key=?",
                (self.policy.account_key,),
            )[0]
        )
        self.assertEqual(after, before)
        self.assertTrue(self.lock.held)
        self.assertEqual(
            self.interlock.release_components(),
            ((
                "ibkr_account_writer_lock",
                self.lock,
                (
                    "held",
                    "holder_metadata",
                    "refresh",
                    "acquisition_id",
                    "acquired_at",
                    "writer_lease_generation",
                ),
            ),),
        )

    def test_replaced_lock_path_cannot_impersonate_held_descriptor(self) -> None:
        metadata = self.lock.path.read_bytes()
        replacement = self.root / "replacement.writer.lock"
        replacement.write_bytes(metadata)
        replacement.chmod(0o600)
        held_identity = os.fstat(self.lock._descriptor)
        os.replace(replacement, self.lock.path)
        path_identity = self.lock.path.stat()
        self.assertNotEqual(
            (held_identity.st_dev, held_identity.st_ino),
            (path_identity.st_dev, path_identity.st_ino),
        )
        self.assertFalse(self.lock.held)
        self.assertEqual(self.lock.holder_metadata(), {})
        self.assert_code(
            "IBKR_AUTONOMOUS_INTERLOCK_LOCK_NOT_HELD",
            self.interlock,
        )

    def test_replaced_state_path_cannot_supply_mutation_authority(self) -> None:
        replacement = self.root / "replacement.sqlite3"
        source = sqlite3.connect(self.database)
        target = sqlite3.connect(replacement)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        original = self.database.stat()
        os.replace(replacement, self.database)
        changed = self.database.stat()
        self.assertNotEqual(
            (original.st_dev, original.st_ino),
            (changed.st_dev, changed.st_ino),
        )
        self.assert_code(
            "IBKR_AUTONOMOUS_INTERLOCK_STATE_FILE_CHANGED",
            self.interlock,
        )

    def test_exact_lock_acquisition_and_lease_generation_are_joined(self) -> None:
        original = dict(
            self.store.rows(
                "SELECT * FROM account_writer_lease WHERE account_key=?",
                (self.policy.account_key,),
            )[0]
        )
        self.sql(
            "UPDATE account_writer_lease SET generation=? WHERE account_key=?",
            (int(original["generation"]) + 1, self.policy.account_key),
        )
        self.assert_code(
            "IBKR_AUTONOMOUS_INTERLOCK_WRITER_LEASE_BINDING_INVALID",
            self.interlock,
        )
        self.sql(
            "UPDATE account_writer_lease SET generation=?,acquired_at=? WHERE account_key=?",
            (
                original["generation"],
                (self.now - timedelta(seconds=4)).isoformat(),
                self.policy.account_key,
            ),
        )
        self.assert_code(
            "IBKR_AUTONOMOUS_INTERLOCK_WRITER_LEASE_BINDING_INVALID",
            self.interlock,
        )

    def test_unheld_lock_is_denied_and_never_acquired(self) -> None:
        self.lock.release()
        with patch.object(
            self.lock, "acquire", side_effect=AssertionError("must not acquire")
        ) as acquire:
            self.assert_code(
                "IBKR_AUTONOMOUS_INTERLOCK_LOCK_NOT_HELD",
                self.interlock,
            )
        acquire.assert_not_called()

    def test_holder_owner_pid_and_bindings_are_exact(self) -> None:
        metadata = self.lock.holder_metadata()
        cases = {
            "owner_id": "different-owner",
            "pid": os.getpid() + 1,
            "authorization_binding_id": "c" * 64,
            "broker_account_binding_fingerprint": "e" * 64,
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                changed = dict(metadata)
                changed[field] = value
                self.lock.path.write_text(
                    json.dumps(changed, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="utf-8",
                )
                self.assert_code(
                    "IBKR_AUTONOMOUS_INTERLOCK_LOCK_METADATA_INVALID",
                    self.interlock,
                )
                self.lock.path.write_text(
                    json.dumps(metadata, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="utf-8",
                )

    def test_current_unreleased_same_owner_and_process_lease_is_required(self) -> None:
        cases = (
            (
                "owner_id",
                "some-other-owner",
                "IBKR_AUTONOMOUS_INTERLOCK_WRITER_LEASE_BINDING_INVALID",
            ),
            (
                "process_id",
                os.getpid() + 1,
                "IBKR_AUTONOMOUS_INTERLOCK_WRITER_LEASE_BINDING_INVALID",
            ),
            (
                "released_at",
                self.now.isoformat(),
                "IBKR_AUTONOMOUS_INTERLOCK_WRITER_LEASE_BINDING_INVALID",
            ),
        )
        original = dict(
            self.store.rows(
                "SELECT * FROM account_writer_lease WHERE account_key=?",
                (self.policy.account_key,),
            )[0]
        )
        for field, value, code in cases:
            with self.subTest(field=field):
                self.sql(
                    f"UPDATE account_writer_lease SET {field}=? WHERE account_key=?",
                    (value, self.policy.account_key),
                )
                self.assert_code(code, self.interlock)
                self.sql(
                    f"UPDATE account_writer_lease SET {field}=? WHERE account_key=?",
                    (original[field], self.policy.account_key),
                )
        self.sql(
            "DELETE FROM account_writer_lease WHERE account_key=?",
            (self.policy.account_key,),
        )
        self.assert_code(
            "IBKR_AUTONOMOUS_INTERLOCK_WRITER_LEASE_NOT_UNIQUE",
            self.interlock,
        )

    def test_stale_and_future_lease_heartbeats_are_distinct_denials(self) -> None:
        self.sql(
            "UPDATE account_writer_lease SET heartbeat_at=? WHERE account_key=?",
            (
                (self.now - timedelta(seconds=11)).isoformat(),
                self.policy.account_key,
            ),
        )
        self.assert_code(
            "IBKR_AUTONOMOUS_INTERLOCK_WRITER_LEASE_HEARTBEAT_STALE",
            self.interlock,
        )
        self.sql(
            "UPDATE account_writer_lease SET heartbeat_at=? WHERE account_key=?",
            (
                (self.now + timedelta(microseconds=1)).isoformat(),
                self.policy.account_key,
            ),
        )
        self.assert_code(
            "IBKR_AUTONOMOUS_INTERLOCK_WRITER_LEASE_HEARTBEAT_FUTURE",
            self.interlock,
        )

    def test_runtime_requires_exact_bindings_authority_and_safe_mode(self) -> None:
        cases = (
            (
                "release_manifest_hash",
                "f" * 64,
                "IBKR_AUTONOMOUS_INTERLOCK_RUNTIME_BINDING_INVALID",
            ),
            (
                "authority_enabled",
                0,
                "IBKR_AUTONOMOUS_INTERLOCK_RUNTIME_AUTHORITY_DISABLED",
            ),
            (
                "mode",
                "PAUSED",
                "IBKR_AUTONOMOUS_INTERLOCK_RUNTIME_MODE_BLOCKED",
            ),
        )
        original = dict(self.store.runtime_status() or {})
        for field, value, code in cases:
            with self.subTest(field=field):
                self.sql(
                    f"UPDATE runtime_identity SET {field}=? WHERE singleton=1",
                    (value,),
                )
                self.assert_code(code, self.interlock)
                self.sql(
                    f"UPDATE runtime_identity SET {field}=? WHERE singleton=1",
                    (original[field],),
                )

    def test_all_entry_and_safety_mutation_modes_remain_available(self) -> None:
        for mode in (
            "RECONCILING",
            "ACTIVE",
            "PAUSE_NEW_ENTRIES",
            "MANAGED_CLOSEOUT",
            "INCIDENT",
        ):
            with self.subTest(mode=mode):
                self.sql(
                    "UPDATE runtime_identity SET mode=? WHERE singleton=1",
                    (mode,),
                )
                self.assertIsNone(self.interlock())

    def test_consumed_activation_must_be_unique_and_canonical(self) -> None:
        row = dict(
            self.store.rows(
                "SELECT * FROM activation_records WHERE activation_id=?",
                (self.activation.activation_id,),
            )[0]
        )
        self.sql(
            "UPDATE activation_records SET consumed_at=NULL WHERE activation_id=?",
            (self.activation.activation_id,),
        )
        self.assert_code(
            "IBKR_AUTONOMOUS_INTERLOCK_ACTIVATION_NOT_UNIQUE",
            self.interlock,
        )
        self.sql(
            "UPDATE activation_records SET consumed_at=? WHERE activation_id=?",
            (row["consumed_at"], self.activation.activation_id),
        )
        self.sql(
            """INSERT INTO activation_records(
                   activation_id,account_key,record_hash,created_at,expires_at,
                   consumed_at,record_json
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                "f" * 64,
                row["account_key"],
                row["record_hash"],
                row["created_at"],
                row["expires_at"],
                row["consumed_at"],
                row["record_json"],
            ),
        )
        self.assert_code(
            "IBKR_AUTONOMOUS_INTERLOCK_ACTIVATION_NOT_UNIQUE",
            self.interlock,
        )
        self.sql("DELETE FROM activation_records WHERE activation_id=?", ("f" * 64,))
        self.sql(
            "UPDATE activation_records SET record_hash=? WHERE activation_id=?",
            ("0" * 64, self.activation.activation_id),
        )
        self.assert_code(
            "IBKR_AUTONOMOUS_INTERLOCK_ACTIVATION_HASH_OR_CANONICAL_MISMATCH",
            self.interlock,
        )

    def test_owner_activation_survives_rollover_for_safety_mutations(self) -> None:
        self.clock_value = self.now + timedelta(days=1)
        self.sql(
            "UPDATE account_writer_lease SET heartbeat_at=? WHERE account_key=?",
            (self.clock_value.isoformat(), self.policy.account_key),
        )
        for mode in ("PAUSE_NEW_ENTRIES", "MANAGED_CLOSEOUT", "INCIDENT"):
            with self.subTest(mode=mode):
                self.sql(
                    "UPDATE runtime_identity SET mode=? WHERE singleton=1",
                    (mode,),
                )
                self.assertIsNone(self.interlock())

    def test_rollover_does_not_carry_prior_day_entry_latch(self) -> None:
        session_timezone = ZoneInfo(
            str(self.policy.config["sessions"]["timezone"])
        )
        prior_date = self.now.astimezone(session_timezone).date().isoformat()
        self.sql(
            """INSERT INTO session_latches(
                   account_key,trading_date,loss_locked,objective_crossed,
                   pause_new_entries,closeout_started,hard_kill,
                   highest_realized_pnl_cents,first_objective_crossed_at,
                   revision,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                self.policy.account_key,
                prior_date,
                0,
                0,
                0,
                0,
                0,
                0,
                None,
                0,
                self.now.isoformat(),
            ),
        )
        self.sql(
            "UPDATE runtime_identity SET mode='ACTIVE' WHERE singleton=1"
        )
        next_session = self.now + timedelta(days=1)
        checker = DurableIbkrRiskPolicyCheck(
            state_path=self.database,
            policy=self.policy,
        )
        entry_plan = SimpleNamespace(
            purpose=IbkrOrderPurpose.ENTRY,
            plan_id="f" * 64,
            request=SimpleNamespace(
                client_ref_id="00000000-0000-4000-8000-000000000001"
            ),
        )
        snapshot = SimpleNamespace(
            daily_realized_pnl_ready=True,
            daily_realized_pnl=Decimal("0"),
        )
        with self.assertRaises(IbkrCommandInputError) as caught:
            checker(snapshot, entry_plan, next_session)
        self.assertEqual(
            str(caught.exception),
            "IBKR_COMMAND_SESSION_RISK_LATCH_BLOCKED",
        )

        # Purpose-aware safety mutations do not consume the daily entry gate.
        checker(
            object(),
            SimpleNamespace(purpose=IbkrOrderPurpose.PROTECTION),
            next_session,
        )

    def test_activation_requires_explicit_unattended_support_without_confirmation(self) -> None:
        readiness = replace(
            self.activation.readiness_evidence,
            execution_authority_mode="attended_only",
            attended_mutation_supported=True,
            unattended_mutation_supported=False,
            per_mutation_confirmation_required=True,
        )
        self.replace_activation_readiness(readiness)
        self.assert_code(
            "IBKR_AUTONOMOUS_INTERLOCK_ACTIVATION_AUTHORITY_UNSUPPORTED",
            self.interlock,
        )

    def test_activation_broker_and_authorization_receipts_are_exact(self) -> None:
        readiness = replace(
            self.activation.readiness_evidence,
            broker_authorization_binding_id="c" * 64,
        )
        self.replace_activation_readiness(readiness)
        self.assert_code(
            "IBKR_AUTONOMOUS_INTERLOCK_ACTIVATION_BINDING_INVALID",
            self.interlock,
        )

    def test_database_schema_mismatch_is_not_treated_as_authority(self) -> None:
        self.sql("PRAGMA user_version=2")
        self.assert_code(
            "IBKR_AUTONOMOUS_INTERLOCK_STATE_SCHEMA_MISMATCH",
            self.interlock,
        )

    def test_constructor_rejects_attended_flags_and_mismatched_lock_binding(self) -> None:
        attended = PolicyBundle.load(
            ROOT, config_relative="config/full_live_ibkr.json"
        )
        self.assert_code(
            "IBKR_AUTONOMOUS_INTERLOCK_POLICY_BINDING_INVALID",
            lambda: AutonomousIbkrWriterInterlock(
                lock=self.lock,
                state=self.store,
                state_path=self.database,
                state_identity=(
                    self.database.stat().st_dev,
                    self.database.stat().st_ino,
                ),
                policy=attended,
                release_manifest_hash=RELEASE,
                authority_bindings=self.bindings,
                clock=lambda: self.clock_value,
            ),
        )
        wrong = AccountWriterLock(
            self.root / "other-locks",
            self.policy.account_key,
            broker_account_binding_fingerprint=ACCOUNT_BINDING,
            authorization_binding_id="c" * 64,
        )
        self.assert_code(
            "IBKR_AUTONOMOUS_INTERLOCK_LOCK_BINDING_INVALID",
            lambda: AutonomousIbkrWriterInterlock(
                lock=wrong,
                state=self.store,
                state_path=self.database,
                state_identity=(
                    self.database.stat().st_dev,
                    self.database.stat().st_ino,
                ),
                policy=self.policy,
                release_manifest_hash=RELEASE,
                authority_bindings=self.bindings,
                clock=lambda: self.clock_value,
            ),
        )


if __name__ == "__main__":
    unittest.main()
