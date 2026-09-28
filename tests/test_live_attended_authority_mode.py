from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from titan_brain.live.cli import (
    InstallLayout,
    _account_writer_lock,
    _background_mutations_enabled,
    _maintenance_interlock,
    _queue_runtime_control,
    _service_process_lock,
)
from titan_brain.live.policy import PolicyBundle
from titan_brain.live.writer_lock import AccountWriterLock, WriterLockBusy


ROOT = Path(__file__).resolve().parents[1]


class AttendedAuthorityModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = PolicyBundle.load(ROOT)

    def policy(self, mode: str, *, interlock: bool) -> PolicyBundle:
        config = copy.deepcopy(self.base.config)
        config["execution"].update(
            {
                "execution_authority_mode": mode,
                "local_mutation_interlock_enabled": interlock,
                "supported_unattended_mutation": mode == "unattended",
                "per_mutation_user_confirmation_required": mode == "attended_only",
            }
        )
        candidate = replace(self.base, config=config)
        candidate.validate()
        return candidate

    def test_persistent_service_cannot_mutate_in_attended_only_mode(self) -> None:
        policy = self.policy("attended_only", interlock=True)
        self.assertFalse(_background_mutations_enabled(policy))

    def test_legacy_unattended_mode_still_requires_local_interlock(self) -> None:
        self.assertFalse(
            _background_mutations_enabled(self.policy("unattended", interlock=False))
        )
        self.assertTrue(
            _background_mutations_enabled(self.policy("unattended", interlock=True))
        )

    def test_attended_read_coordinator_does_not_hold_broker_writer_lock(self) -> None:
        config = copy.deepcopy(self.base.config)
        config["execution"].update(
            {
                "execution_authority_mode": "attended_only",
                "broker_adapter": "supported_production_transport",
                "production_account_binding_fingerprint": "a" * 64,
                "production_authorization_binding_id": "b" * 64,
                "supported_unattended_mutation": False,
                "per_mutation_user_confirmation_required": True,
                "local_mutation_interlock_enabled": True,
            }
        )
        policy = replace(self.base, config=config)
        with tempfile.TemporaryDirectory() as directory:
            layout = InstallLayout(directory)
            coordinator = _service_process_lock(layout, policy)
            broker_writer = _account_writer_lock(layout, policy)
        self.assertNotEqual(coordinator.path, broker_writer.path)
        self.assertIsNone(coordinator.broker_account_binding_fingerprint)
        self.assertEqual(
            broker_writer.broker_account_binding_fingerprint, "a" * 64
        )
        self.assertFalse(coordinator.held)
        self.assertFalse(broker_writer.held)

    def test_maintenance_interlock_excludes_coordinator_and_broker_writer(self) -> None:
        policy = self.policy("attended_only", interlock=True)
        with tempfile.TemporaryDirectory() as directory:
            layout = SimpleNamespace(lock_path=Path(directory))
            with _maintenance_interlock(
                layout, policy, owner_id="maintenance-test"
            ) as maintenance_writer:
                coordinator_probe = _service_process_lock(
                    layout, policy, owner_id="coordinator-probe"
                )
                broker_probe = _account_writer_lock(
                    layout, policy, owner_id="broker-probe"
                )
                with self.assertRaises(WriterLockBusy):
                    coordinator_probe.acquire()
                with self.assertRaises(WriterLockBusy):
                    broker_probe.acquire()
                self.assertTrue(maintenance_writer.held)

            # Failure on the second lock must release the first one.
            broker_owner = _account_writer_lock(
                layout, policy, owner_id="other-broker-writer"
            )
            with broker_owner:
                with self.assertRaises(WriterLockBusy):
                    with _maintenance_interlock(
                        layout, policy, owner_id="blocked-maintenance"
                    ):
                        self.fail("maintenance acquired a busy broker lock")
                coordinator = _service_process_lock(
                    layout, policy, owner_id="post-failure-coordinator"
                )
                with coordinator:
                    self.assertTrue(coordinator.held)

    def test_safety_control_probes_attended_coordinator_not_broker_writer(self) -> None:
        policy = self.policy("attended_only", interlock=True)
        manifest = {"release_manifest_hash": "a" * 64}

        class Inbox:
            def submit(self, command, **_kwargs):
                self.command = command
                return {"request_id": "request-1"}

        inbox = Inbox()

        class Composition:
            def bind_release(self, *_args, **_kwargs):
                return None

            def control_inbox(self, *_args, **_kwargs):
                return inbox

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            layout = SimpleNamespace(
                lock_path=path / "locks",
                release_root=ROOT,
                control_path=path / "control",
                load_release=lambda: (manifest, policy),
            )
            coordinator = _service_process_lock(layout, policy)
            runtime = {
                "release_manifest_hash": manifest["release_manifest_hash"],
                "config_hash": policy.config_hash,
                "policy_hash": policy.policy_hash,
                "runtime_id": policy.runtime_id,
                "account_key": policy.account_key,
                "authority_enabled": 1,
                "activated_at": "2026-09-14T13:30:00+00:00",
            }
            lease = {"owner_id": coordinator.owner_id}

            with coordinator, mock.patch(
                "titan_brain.live.cli._read_runtime_and_lease",
                return_value=(runtime, lease),
            ):
                result = _queue_runtime_control(
                    layout,
                    command="PAUSE_NEW_ENTRIES",
                    reason="test",
                    composition=Composition(),
                )
            self.assertTrue(result["service_writer_verified"])
            self.assertEqual(inbox.command, "PAUSE_NEW_ENTRIES")

            # The safety-control liveness probe must never claim broker-write
            # authority merely to enqueue a coordinator request.
            broker = _account_writer_lock(layout, policy, owner_id="broker-check")
            with broker:
                self.assertTrue(broker.held)


if __name__ == "__main__":
    unittest.main()
