from __future__ import annotations

from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import runpy
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from titan_brain.live.cli import (
    CommandBlocked,
    InstallLayout,
    _notification_test_notification,
    _notification_test_payload,
    _verified_notification_receipt,
    build_parser,
    command_notification_confirm_receipt,
    command_notification_test,
)
from titan_brain.live.composition import RuntimeComposition
from titan_brain.live.notifications import (
    DeliveryAssurance,
    DeliveryReceipt,
    InjectedProviderNotificationSink,
    JsonlNotificationSink,
    LiveStateOutboxAdapter,
    NotificationRoute,
    OutboxDispatcher,
    destination_fingerprint,
    notification_payload_hash,
)
from titan_brain.live.state import (
    LiveStateStore,
    NOTIFICATION_TEST_SUBJECT,
    StateConflict,
    notification_owner_confirmation_phrase,
)


NOW = datetime(2026, 9, 14, 14, 0, tzinfo=timezone.utc)
ACCOUNT = "ibkr-live-ending-3103"
RUNTIME_ID = "titan-runtime-owner-confirmation-test"
RELEASE_HASH = "a" * 64
CONFIG_HASH = "b" * 64
POLICY_HASH = "c" * 64
ROOT = Path(__file__).resolve().parents[1]


def provider_route() -> NotificationRoute:
    return NotificationRoute(
        provider="gmail",
        destination_fingerprint=destination_fingerprint(
            "gmail", "owner@example.invalid"
        ),
        route_version="gmail-owner-confirmation-v1",
        required_assurance=DeliveryAssurance.OWNER_CONFIRMED,
    )


class _ProviderSender:
    def __init__(self, route: NotificationRoute) -> None:
        self.route = route
        self.calls = 0

    def __call__(self, notification, *, idempotency_key, timeout_seconds):
        self.calls += 1
        return {
            "provider": self.route.provider,
            "destination_fingerprint": self.route.destination_fingerprint,
            "provider_receipt_id": "gmail-provider-test-message-1",
            "accepted_at": NOW + timedelta(seconds=1),
        }


class NotificationOwnerConfirmationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.install = Path(self.temporary.name) / "full-live"
        self.layout = InstallLayout(self.install)
        self.layout.state_path.parent.mkdir(parents=True)
        self.route = provider_route()
        self.policy = SimpleNamespace(
            runtime_id=RUNTIME_ID,
            account_key=ACCOUNT,
            config_hash=CONFIG_HASH,
            policy_hash=POLICY_HASH,
            execution_authority_mode="unattended",
            config={
                "execution": {"broker_adapter": "disabled"},
                "notifications": {
                    "delivery_sink": "gmail_api",
                    "destination_bridge_configured": True,
                    "provider": self.route.provider,
                    "destination_fingerprint": (
                        self.route.destination_fingerprint
                    ),
                    "route_version": self.route.route_version,
                    "required_assurance": (
                        self.route.required_assurance.value
                    ),
                },
            },
        )
        self.manifest = {"release_manifest_hash": RELEASE_HASH}
        self.store = LiveStateStore(self.layout.state_path)
        self.store.initialize_runtime(
            runtime_id=RUNTIME_ID,
            account_key=ACCOUNT,
            release_manifest_hash=RELEASE_HASH,
            config_hash=CONFIG_HASH,
            policy_hash=POLICY_HASH,
            initialized_at=NOW,
        )
        self.lock_patch = mock.patch(
            "titan_brain.live.cli.user_account_writer_lock_directory",
            return_value=Path(self.temporary.name) / "account-locks",
        )
        self.lock_patch.start()

    def tearDown(self) -> None:
        self.lock_patch.stop()
        self.store.close()
        self.temporary.cleanup()

    def _queue_test(self, event_id: str = "owner-visible-test-1") -> str:
        composition = RuntimeComposition()
        sink = SimpleNamespace(route=self.route)
        args = SimpleNamespace(
            install_root=str(self.install),
            event_id=event_id,
            runtime_composition=composition,
        )
        output = io.StringIO()
        with (
            mock.patch.object(
                InstallLayout,
                "load_release",
                return_value=(self.manifest, self.policy),
            ),
            mock.patch.object(composition, "bind_release", return_value=()),
            mock.patch.object(
                composition, "notification_sink", return_value=sink
            ),
            mock.patch("titan_brain.live.cli._now", return_value=NOW),
            redirect_stdout(output),
        ):
            self.assertEqual(command_notification_test(args), 0)
        report = json.loads(output.getvalue())
        self.assertTrue(report["test_only"])
        self.assertTrue(report["no_trading_action"])
        self.assertTrue(report["subject"].startswith(NOTIFICATION_TEST_SUBJECT))
        self.assertIn(report["visible_test_token"], report["subject"])
        row = self.store.row(
            "notification_outbox", "message_id", report["message_id"]
        )
        wrapper = json.loads(str(row["payload_json"]))
        self.assertIn(event_id, wrapper["body"])
        self.assertIn(report["visible_test_token"], wrapper["body"])
        return str(report["message_id"])

    def _deliver(self, message_id: str) -> DeliveryReceipt:
        sender = _ProviderSender(self.route)
        dispatcher = OutboxDispatcher(
            LiveStateOutboxAdapter(self.store, ACCOUNT),
            InjectedProviderNotificationSink(route=self.route, sender=sender),
            completion_clock=lambda: NOW + timedelta(seconds=1),
            worker_id="owner-confirmation-test-worker",
        )
        self.assertEqual(dispatcher.drain(NOW + timedelta(seconds=1)), (1, 0))
        self.assertEqual(sender.calls, 1)
        row = self.store.row("notification_outbox", "message_id", message_id)
        self.assertIsNotNone(row)
        receipt = DeliveryReceipt.from_json(str(row["delivery_receipt"]))
        self.assertEqual(receipt.assurance, DeliveryAssurance.PROVIDER_ACCEPTED)
        self.assertIsNone(receipt.owner_confirmed_at)
        return receipt

    def _confirmation_command(
        self, message_id: str, *, confirmation: str | None
    ) -> tuple[int, dict[str, object]]:
        output = io.StringIO()
        args = SimpleNamespace(
            install_root=str(self.install),
            message_id=message_id,
            confirm=confirmation,
        )
        with (
            mock.patch.object(
                InstallLayout,
                "load_release",
                return_value=(self.manifest, self.policy),
            ),
            mock.patch(
                "titan_brain.live.cli._now",
                return_value=NOW + timedelta(seconds=2),
            ),
            redirect_stdout(output),
        ):
            result = command_notification_confirm_receipt(args)
        return result, json.loads(output.getvalue())

    def test_exact_human_challenge_is_required_and_audited(self) -> None:
        message_id = self._queue_test()
        provider_receipt = self._deliver(message_id)
        before = self.store.row("notification_outbox", "message_id", message_id)
        self.assertEqual(before["delivery_assurance"], "PROVIDER_ACCEPTED")
        self.assertEqual(before["delivery_receipt_hash"], provider_receipt.receipt_hash)
        self.assertEqual(
            self.store.rows(
                "SELECT * FROM audit_events WHERE entity_id=? "
                "AND event_type='NOTIFICATION_OWNER_CONFIRMED'",
                (message_id,),
            ),
            [],
        )

        status_code, challenge = self._confirmation_command(
            message_id, confirmation=None
        )
        self.assertEqual(status_code, 2)
        self.assertFalse(challenge["owner_confirmed"])
        self.assertFalse(challenge["provider_acceptance_is_owner_confirmation"])
        self.assertIn("actually receiving", challenge["confirmation_required"])
        phrase = str(challenge["confirmation_phrase"])
        self.assertEqual(
            phrase,
            notification_owner_confirmation_phrase(
                visible_test_token=str(challenge["visible_test_token"]),
                account_key=ACCOUNT,
                release_manifest_hash=RELEASE_HASH,
                route_id=self.route.route_id,
                message_id=message_id,
                provider_receipt_hash=provider_receipt.receipt_hash,
            ),
        )

        with self.assertRaisesRegex(CommandBlocked, "exact notification"):
            self._confirmation_command(
                message_id,
                confirmation="YES I AUTHORIZE GMAIL SETUP AND A TEST",
            )
        unchanged = self.store.row("notification_outbox", "message_id", message_id)
        self.assertEqual(unchanged["delivery_assurance"], "PROVIDER_ACCEPTED")

        result, confirmed = self._confirmation_command(
            message_id, confirmation=phrase
        )
        self.assertEqual(result, 0)
        self.assertTrue(confirmed["owner_confirmed"])
        self.assertEqual(confirmed["assurance"], "OWNER_CONFIRMED")
        self.assertEqual(
            confirmed["immutable_audit_event"],
            "NOTIFICATION_OWNER_CONFIRMED",
        )
        row = self.store.row("notification_outbox", "message_id", message_id)
        owner_receipt = DeliveryReceipt.from_json(str(row["delivery_receipt"]))
        self.assertEqual(owner_receipt.assurance, DeliveryAssurance.OWNER_CONFIRMED)
        self.assertEqual(
            owner_receipt.owner_confirmed_at, NOW + timedelta(seconds=2)
        )
        events = self.store.rows(
            "SELECT * FROM audit_events WHERE entity_id=? "
            "AND event_type='NOTIFICATION_OWNER_CONFIRMED'",
            (message_id,),
        )
        self.assertEqual(len(events), 1)
        self.assertTrue(self.store.verify_event_chain()[0])
        self.assertEqual(
            _verified_notification_receipt(
                self.store,
                account_key=ACCOUNT,
                route=self.route,
                manifest=self.manifest,
                policy=self.policy,
                now=NOW + timedelta(seconds=2),
            ),
            (owner_receipt.receipt_hash, provider_receipt.accepted_at),
        )

        replay_result, replay = self._confirmation_command(
            message_id, confirmation=phrase
        )
        self.assertEqual(replay_result, 0)
        self.assertTrue(replay["already_confirmed"])
        self.assertEqual(
            len(
                self.store.rows(
                    "SELECT * FROM audit_events WHERE entity_id=? "
                    "AND event_type='NOTIFICATION_OWNER_CONFIRMED'",
                    (message_id,),
                )
            ),
            1,
        )

    def test_persistence_boundary_rejects_broad_approval_and_stale_receipt(self) -> None:
        message_id = self._queue_test("state-boundary-test")
        receipt = self._deliver(message_id)
        arguments = {
            "account_key": ACCOUNT,
            "runtime_id": RUNTIME_ID,
            "release_manifest_hash": RELEASE_HASH,
            "config_hash": CONFIG_HASH,
            "policy_hash": POLICY_HASH,
            "route_id": self.route.route_id,
            "provider_receipt_hash": receipt.receipt_hash,
        }
        with self.assertRaisesRegex(StateConflict, "exact notification"):
            self.store.confirm_notification_owner_receipt(
                message_id,
                **arguments,
                confirmed_at=NOW + timedelta(seconds=2),
                confirmation_phrase="OWNER APPROVED NOTIFICATION SETUP",
            )
        phrase = notification_owner_confirmation_phrase(
            visible_test_token=_notification_test_payload(
                event_id="state-boundary-test",
                account_key=ACCOUNT,
                runtime_id=RUNTIME_ID,
                release_manifest_hash=RELEASE_HASH,
                config_hash=CONFIG_HASH,
                policy_hash=POLICY_HASH,
                route_id=self.route.route_id,
            )["visible_test_token"],
            account_key=ACCOUNT,
            release_manifest_hash=RELEASE_HASH,
            route_id=self.route.route_id,
            message_id=message_id,
            provider_receipt_hash=receipt.receipt_hash,
        )
        with self.assertRaisesRegex(StateConflict, "fresh exact delivery"):
            self.store.confirm_notification_owner_receipt(
                message_id,
                **arguments,
                confirmed_at=NOW + timedelta(minutes=6),
                confirmation_phrase=phrase,
            )
        row = self.store.row("notification_outbox", "message_id", message_id)
        self.assertEqual(row["delivery_assurance"], "PROVIDER_ACCEPTED")

    def test_initial_delivery_cannot_claim_owner_confirmation(self) -> None:
        message_id = self._queue_test("worker-cannot-confirm-owner")
        adapter = LiveStateOutboxAdapter(self.store, ACCOUNT)
        claimed = adapter.claim_notifications(
            NOW,
            "provider-worker",
            NOW + timedelta(seconds=30),
            1,
        )
        self.assertEqual([item["outbox_id"] for item in claimed], [message_id])
        payload = _notification_test_payload(
            event_id="worker-cannot-confirm-owner",
            account_key=ACCOUNT,
            runtime_id=RUNTIME_ID,
            release_manifest_hash=RELEASE_HASH,
            config_hash=CONFIG_HASH,
            policy_hash=POLICY_HASH,
            route_id=self.route.route_id,
        )
        notification = _notification_test_notification(payload)
        invented = DeliveryReceipt(
            route_id=self.route.route_id,
            provider=self.route.provider,
            destination_fingerprint=self.route.destination_fingerprint,
            route_version=self.route.route_version,
            event_key=notification.dedupe_key,
            payload_hash=notification_payload_hash(notification),
            provider_receipt_id="provider-must-not-confirm-owner",
            accepted_at=NOW + timedelta(seconds=1),
            assurance=DeliveryAssurance.OWNER_CONFIRMED,
            owner_confirmed_at=NOW + timedelta(seconds=2),
        )
        with self.assertRaisesRegex(ValueError, "initial delivery cannot"):
            adapter.notification_sent(
                message_id,
                invented,
                NOW + timedelta(seconds=2),
                "provider-worker",
            )
        row = self.store.row("notification_outbox", "message_id", message_id)
        self.assertIsNone(row["delivery_assurance"])
        self.assertEqual(
            self.store.rows(
                "SELECT * FROM audit_events WHERE entity_id=? "
                "AND event_type='NOTIFICATION_OWNER_CONFIRMED'",
                (message_id,),
            ),
            [],
        )

    def test_forged_owner_receipt_without_confirmation_event_fails_readiness(self) -> None:
        message_id = self._queue_test("forged-owner-without-audit")
        provider_receipt = self._deliver(message_id)
        forged = provider_receipt.owner_confirmed(
            confirmed_at=NOW + timedelta(seconds=2)
        )
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE notification_outbox SET delivery_receipt=?, "
                "delivery_assurance=?,delivery_receipt_hash=? WHERE message_id=?",
                (
                    forged.to_json(),
                    forged.assurance.value,
                    forged.receipt_hash,
                    message_id,
                ),
            )
        self.assertTrue(self.store.verify_event_chain()[0])
        self.assertIsNone(
            _verified_notification_receipt(
                self.store,
                account_key=ACCOUNT,
                route=self.route,
                manifest=self.manifest,
                policy=self.policy,
                now=NOW + timedelta(seconds=2),
            )
        )

    def test_release_unbound_owner_receipt_fails_readiness_even_with_audit(self) -> None:
        payload = _notification_test_payload(
            event_id="forged-wrong-release",
            account_key=ACCOUNT,
            runtime_id=RUNTIME_ID,
            release_manifest_hash="f" * 64,
            config_hash=CONFIG_HASH,
            policy_hash=POLICY_HASH,
            route_id=self.route.route_id,
        )
        notification = _notification_test_notification(payload)
        message_id = LiveStateOutboxAdapter(
            self.store, ACCOUNT
        ).enqueue_notification(notification, NOW)
        provider_receipt = self._deliver(message_id)
        forged = provider_receipt.owner_confirmed(
            confirmed_at=NOW + timedelta(seconds=2)
        )
        phrase = notification_owner_confirmation_phrase(
            visible_test_token=payload["visible_test_token"],
            account_key=ACCOUNT,
            release_manifest_hash="f" * 64,
            route_id=self.route.route_id,
            message_id=message_id,
            provider_receipt_hash=provider_receipt.receipt_hash,
        )
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE notification_outbox SET delivery_receipt=?, "
                "delivery_assurance=?,delivery_receipt_hash=? WHERE message_id=?",
                (
                    forged.to_json(),
                    forged.assurance.value,
                    forged.receipt_hash,
                    message_id,
                ),
            )
        self.store.append_event(
            stream=ACCOUNT,
            event_type="NOTIFICATION_OWNER_CONFIRMED",
            entity_type="notification",
            entity_id=message_id,
            occurred_at=NOW + timedelta(seconds=2),
            payload={
                "schema_version": (
                    "titan_notification_owner_confirmation_2026-09-14_v1"
                ),
                "runtime_id": RUNTIME_ID,
                "release_manifest_hash": "f" * 64,
                "config_hash": CONFIG_HASH,
                "policy_hash": POLICY_HASH,
                "event_key": notification.dedupe_key,
                "route_id": self.route.route_id,
                "payload_hash": provider_receipt.payload_hash,
                "provider_receipt_hash": provider_receipt.receipt_hash,
                "owner_receipt_hash": forged.receipt_hash,
                "owner_confirmation_sha256": hashlib.sha256(
                    phrase.encode("utf-8")
                ).hexdigest(),
            },
        )
        self.assertTrue(self.store.verify_event_chain()[0])
        self.assertIsNone(
            _verified_notification_receipt(
                self.store,
                account_key=ACCOUNT,
                route=self.route,
                manifest=self.manifest,
                policy=self.policy,
                now=NOW + timedelta(seconds=2),
            )
        )

    def test_visible_tokens_distinguish_tests_and_tampering_is_rejected(self) -> None:
        first = self._queue_test("visible-test-a")
        self._deliver(first)
        second = self._queue_test("visible-test-b")
        first_wrapper = json.loads(
            str(
                self.store.row(
                    "notification_outbox", "message_id", first
                )["payload_json"]
            )
        )
        second_wrapper = json.loads(
            str(
                self.store.row(
                    "notification_outbox", "message_id", second
                )["payload_json"]
            )
        )
        first_token = first_wrapper["payload"]["visible_test_token"]
        second_token = second_wrapper["payload"]["visible_test_token"]
        self.assertNotEqual(first_token, second_token)
        self.assertIn("visible-test-a", first_wrapper["body"])
        self.assertIn(first_token, first_wrapper["subject"])
        self.assertIn(first_token, first_wrapper["body"])
        self.assertIn("visible-test-b", second_wrapper["body"])
        self.assertIn(second_token, second_wrapper["subject"])
        self.assertIn(second_token, second_wrapper["body"])

        with self.store.transaction() as connection:
            changed = dict(first_wrapper)
            changed["body"] = str(first_wrapper["body"]).replace(
                str(first_token), str(second_token)
            )
            connection.execute(
                "UPDATE notification_outbox SET payload_json=? WHERE message_id=?",
                (json.dumps(changed, sort_keys=True, separators=(",", ":")), first),
            )
        with self.assertRaisesRegex(CommandBlocked, "signed route and payload"):
            self._confirmation_command(first, confirmation=None)

    def test_unbound_or_local_test_cannot_be_owner_confirmed(self) -> None:
        generic = LiveStateOutboxAdapter(self.store, ACCOUNT).enqueue_notification(
            _notification_test_notification(
                {
                    **_notification_test_payload(
                        event_id="wrong-release",
                        account_key=ACCOUNT,
                        runtime_id=RUNTIME_ID,
                        release_manifest_hash="f" * 64,
                        config_hash=CONFIG_HASH,
                        policy_hash=POLICY_HASH,
                        route_id=self.route.route_id,
                    )
                }
            ),
            NOW,
        )
        self._deliver(generic)
        with self.assertRaisesRegex(CommandBlocked, "signed route and payload"):
            self._confirmation_command(generic, confirmation=None)

        local_sink = JsonlNotificationSink(
            Path(self.temporary.name) / "notifications.jsonl",
            clock=lambda: NOW + timedelta(seconds=1),
        )
        payload = _notification_test_payload(
            event_id="local-only",
            account_key=ACCOUNT,
            runtime_id=RUNTIME_ID,
            release_manifest_hash=RELEASE_HASH,
            config_hash=CONFIG_HASH,
            policy_hash=POLICY_HASH,
            route_id=local_sink.route.route_id,
        )
        local_message = LiveStateOutboxAdapter(
            self.store, ACCOUNT
        ).enqueue_notification(_notification_test_notification(payload), NOW)
        local_dispatcher = OutboxDispatcher(
            LiveStateOutboxAdapter(self.store, ACCOUNT),
            local_sink,
            completion_clock=lambda: NOW + timedelta(seconds=1),
            worker_id="local-owner-confirmation-test-worker",
        )
        self.assertEqual(
            local_dispatcher.drain(NOW + timedelta(seconds=1)), (1, 0)
        )
        local_row = self.store.row(
            "notification_outbox", "message_id", local_message
        )
        local_receipt = DeliveryReceipt.from_json(local_row["delivery_receipt"])
        phrase = notification_owner_confirmation_phrase(
            visible_test_token=payload["visible_test_token"],
            account_key=ACCOUNT,
            release_manifest_hash=RELEASE_HASH,
            route_id=local_sink.route.route_id,
            message_id=local_message,
            provider_receipt_hash=local_receipt.receipt_hash,
        )
        with self.assertRaisesRegex(StateConflict, "provider-accepted test"):
            self.store.confirm_notification_owner_receipt(
                local_message,
                account_key=ACCOUNT,
                runtime_id=RUNTIME_ID,
                release_manifest_hash=RELEASE_HASH,
                config_hash=CONFIG_HASH,
                policy_hash=POLICY_HASH,
                route_id=local_sink.route.route_id,
                provider_receipt_hash=local_receipt.receipt_hash,
                confirmed_at=NOW + timedelta(seconds=2),
                confirmation_phrase=phrase,
            )

    def test_parser_and_launcher_keep_confirmation_off_provider_graphs(self) -> None:
        parsed = build_parser().parse_args(
            [
                "notification-confirm-receipt",
                "--install-root",
                str(self.install),
                "--message-id",
                "message-1",
            ]
        )
        self.assertIs(parsed.handler, command_notification_confirm_receipt)
        self.assertIsNone(parsed.confirm)

        namespace = runpy.run_path(str(ROOT / "scripts/titan-full-live"))
        self.assertIn(
            "notification-confirm-receipt",
            namespace["_NOTIFICATION_ONLY_COMMANDS"],
        )
        notification_factory = object()
        assembly = SimpleNamespace(
            notification_runtime_composition=notification_factory,
            control_runtime_composition=object(),
            coordinator_runtime_composition=object(),
            runtime_composition=object(),
        )
        self.assertIs(
            namespace["_command_composition_factory"](
                assembly, ["notification-confirm-receipt"]
            ),
            notification_factory,
        )


if __name__ == "__main__":
    unittest.main()
