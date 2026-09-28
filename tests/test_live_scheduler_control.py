from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from titan_brain.live.cli import _probe_legacy_scheduler_runtime
from titan_brain.live.policy import canonical_json
from titan_brain.live.provider_clients import KeychainItem
from titan_brain.live.scheduler_control import (
    CODEX_SCHEDULER_CONTROL_PLANE_SOURCE,
    CODEX_SCHEDULER_EVIDENCE_SCHEMA,
    REQUIRED_CODEX_AUTOMATION_IDS,
    SchedulerEvidenceBindings,
    SchedulerEvidenceError,
    SignedFileSchedulerControlPlane,
    load_verified_scheduler_evidence,
)


NOW = datetime(2026, 9, 14, 15, 0, tzinfo=timezone.utc)
SECRET = b"scheduler-control-plane-test-key!"


class StaticKeychain:
    def __init__(self, secret: bytes = SECRET) -> None:
        self.secret = secret
        self.calls: list[KeychainItem] = []

    def read(self, item: KeychainItem) -> bytes:
        self.calls.append(item)
        return self.secret


class SchedulerControlEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        # macOS exposes the temporary root through a /var -> /private/var
        # symlink.  Exercise the production verifier with the canonical path it
        # requires instead of weakening the no-symlink boundary for tests.
        self.path = Path(self.temporary.name).resolve() / "scheduler-evidence.json"
        self.bindings = SchedulerEvidenceBindings(
            release_manifest_hash="1" * 64,
            config_hash="2" * 64,
            policy_hash="3" * 64,
            runtime_id="titan_full_live_ibkr_2026-09-14_v1",
            account_key="ibkr-live-ending-3103",
        )

    def body(
        self,
        *,
        statuses: tuple[str, str] = ("PAUSED", "DISABLED"),
        active_counts: tuple[int, int] = (0, 0),
        automation_ids: tuple[str, ...] = REQUIRED_CODEX_AUTOMATION_IDS,
        observed_at: datetime = NOW,
    ) -> dict[str, object]:
        return {
            "schema_version": CODEX_SCHEDULER_EVIDENCE_SCHEMA,
            "issued_at": NOW.isoformat(),
            "expires_at": (NOW + timedelta(seconds=10)).isoformat(),
            "bindings": self.bindings.to_payload(),
            "control_plane_source": CODEX_SCHEDULER_CONTROL_PLANE_SOURCE,
            "automations": [
                {
                    "automation_id": automation_id,
                    "scheduler_runtime_id": f"codex-runtime-{index + 1}",
                    "status": statuses[index],
                    "config_hash": str(index + 4) * 64,
                    "active_execution_count": active_counts[index],
                    "observed_at": observed_at.isoformat(),
                    "query_receipt_hash": str(index + 6) * 64,
                }
                for index, automation_id in enumerate(automation_ids)
            ],
        }

    def write(self, body: dict[str, object], *, secret: bytes = SECRET) -> None:
        raw = dict(body)
        raw["hmac_sha256"] = hmac.new(
            secret,
            canonical_json(body).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        self.path.write_text(canonical_json(raw) + "\n", encoding="utf-8")
        self.path.chmod(0o600)

    def test_signed_adapter_verifies_exact_release_and_both_automations(self) -> None:
        self.write(self.body())
        keychain = StaticKeychain()
        adapter = SignedFileSchedulerControlPlane(
            self.path,
            keychain=keychain,
            key_item=KeychainItem(
                service="titan-full-live-codex-scheduler-control-plane",
                account=self.bindings.account_key,
            ),
        )

        evidence, error = _probe_legacy_scheduler_runtime(
            now=NOW,
            expected=self.bindings,
            control_plane=adapter,
        )

        self.assertIsNone(error)
        self.assertIsNotNone(evidence)
        self.assertEqual(
            tuple(item.automation_id for item in evidence.automations),
            REQUIRED_CODEX_AUTOMATION_IDS,
        )
        self.assertTrue(evidence.all_retired)
        self.assertEqual(evidence.active_execution_count, 0)
        self.assertEqual(len(evidence.signed_evidence_hash), 64)
        self.assertEqual(len(keychain.calls), 1)

    def test_missing_second_automation_and_wrong_release_fail_closed(self) -> None:
        self.write(
            self.body(automation_ids=(REQUIRED_CODEX_AUTOMATION_IDS[0],))
        )
        with self.assertRaisesRegex(SchedulerEvidenceError, "AUTOMATION_SET_INVALID"):
            load_verified_scheduler_evidence(
                self.path,
                secret=SECRET,
                expected=self.bindings,
                now=NOW,
            )

        self.write(self.body())
        wrong = SchedulerEvidenceBindings(
            release_manifest_hash="f" * 64,
            config_hash=self.bindings.config_hash,
            policy_hash=self.bindings.policy_hash,
            runtime_id=self.bindings.runtime_id,
            account_key=self.bindings.account_key,
        )
        with self.assertRaisesRegex(SchedulerEvidenceError, "BINDING_MISMATCH"):
            load_verified_scheduler_evidence(
                self.path,
                secret=SECRET,
                expected=wrong,
                now=NOW,
            )

    def test_active_status_or_running_execution_is_a_precise_blocker(self) -> None:
        self.write(self.body(statuses=("PAUSED", "ACTIVE")))
        active = load_verified_scheduler_evidence(
            self.path,
            secret=SECRET,
            expected=self.bindings,
            now=NOW,
        )
        _, active_error = _probe_legacy_scheduler_runtime(
            now=NOW,
            expected=self.bindings,
            observed=active,
        )
        self.assertEqual(
            active_error,
            "legacy_retirement:SCHEDULER_NOT_DISABLED:"
            "robinhood-titan-premarket-deep-dive",
        )

        self.write(self.body(active_counts=(0, 1)))
        running = load_verified_scheduler_evidence(
            self.path,
            secret=SECRET,
            expected=self.bindings,
            now=NOW,
        )
        _, running_error = _probe_legacy_scheduler_runtime(
            now=NOW,
            expected=self.bindings,
            observed=running,
        )
        self.assertEqual(
            running_error,
            "legacy_retirement:SCHEDULER_ACTIVE_EXECUTIONS:"
            "robinhood-titan-premarket-deep-dive",
        )

    def test_tamper_noncanonical_permissions_and_expiry_are_rejected(self) -> None:
        body = self.body()
        self.write(body)
        raw = self.path.read_text(encoding="utf-8").replace(
            '"active_execution_count":0',
            '"active_execution_count":1',
            1,
        )
        self.path.write_text(raw, encoding="utf-8")
        with self.assertRaisesRegex(SchedulerEvidenceError, "HMAC_INVALID"):
            load_verified_scheduler_evidence(
                self.path,
                secret=SECRET,
                expected=self.bindings,
                now=NOW,
            )

        self.write(body)
        self.path.chmod(0o644)
        with self.assertRaisesRegex(SchedulerEvidenceError, "FILE_UNSAFE"):
            load_verified_scheduler_evidence(
                self.path,
                secret=SECRET,
                expected=self.bindings,
                now=NOW,
            )

        self.path.chmod(0o600)
        with self.assertRaisesRegex(SchedulerEvidenceError, "EXPIRED"):
            load_verified_scheduler_evidence(
                self.path,
                secret=SECRET,
                expected=self.bindings,
                now=NOW + timedelta(seconds=10),
            )

        self.write(self.body(observed_at=NOW - timedelta(seconds=2)))
        with self.assertRaisesRegex(
            SchedulerEvidenceError, "AUTOMATION_OBSERVATION_STALE"
        ):
            load_verified_scheduler_evidence(
                self.path,
                secret=SECRET,
                expected=self.bindings,
                now=NOW + timedelta(seconds=9),
            )


if __name__ == "__main__":
    unittest.main()
