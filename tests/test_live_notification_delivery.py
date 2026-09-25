from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
import json
import hashlib
import sqlite3
from pathlib import Path
import tempfile
import threading
import unittest

from titan_brain.live.composition import RuntimeComposition, RuntimeCompositionError
from titan_brain.live.notification_worker import (
    NotificationWorkerSettings,
    run_notification_worker,
)
from titan_brain.live.notifications import (
    DeliveryAssurance,
    DeliveryReceipt,
    EnqueueOnlyOutbox,
    GmailApiSender,
    GmailAuthorizationEvidence,
    GmailProviderBinding,
    InjectedProviderNotificationSink,
    JsonlNotificationSink,
    LiveStateOutboxAdapter,
    NotificationRoute,
    NotificationWorker,
    OutboxDispatcher,
    build_notification_sink,
    build_notification,
    destination_fingerprint,
    notification_payload_hash,
    receipt_satisfies_route,
)
from titan_brain.live.state import LiveStateStore, StateConflict, UnsupportedSchema


NOW = datetime(2026, 9, 8, 14, 0, tzinfo=timezone.utc)
ACCOUNT = "ending-7153"


def provider_route(
    *, destination: str = "owner@example.invalid", version: str = "gmail-v1"
) -> NotificationRoute:
    return NotificationRoute(
        provider="gmail",
        destination_fingerprint=destination_fingerprint("gmail", destination),
        route_version=version,
        required_assurance=DeliveryAssurance.PROVIDER_ACCEPTED,
    )


def gmail_config(route: NotificationRoute) -> dict[str, object]:
    return {
        "delivery_sink": "gmail_api",
        "destination_bridge_configured": True,
        "provider": route.provider,
        "destination_fingerprint": route.destination_fingerprint,
        "route_version": route.route_version,
        "required_assurance": route.required_assurance.value,
        "provider_composition_id": "titan.gmail_api.rfc2822.oauth_injected.v1",
        "authorization_binding_id": "d" * 64,
        "timeout_seconds": 4,
    }


def release_manifest_for(*paths: Path) -> dict[str, object]:
    files = []
    root = Path(__file__).resolve().parents[1]
    for path in paths:
        data = path.read_bytes()
        files.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
            }
        )
    return {"release_manifest_hash": "a" * 64, "files": files}


class CapturingProviderSender:
    def __init__(self, route: NotificationRoute) -> None:
        self.route = route
        self.calls = []

    def __call__(self, notification, *, idempotency_key, timeout_seconds):
        self.calls.append((notification, idempotency_key, timeout_seconds))
        return {
            "provider": self.route.provider,
            "destination_fingerprint": self.route.destination_fingerprint,
            "provider_receipt_id": "gmail-provider-message-0001",
            "accepted_at": NOW,
        }


class GmailAuthorizer:
    evidence = GmailAuthorizationEvidence(
        binding_id="d" * 64,
        credential_source="owner-injected-existing-oauth-client",
        scopes=("https://www.googleapis.com/auth/gmail.send",),
        authenticated=True,
    )

    def authorize(self, headers):
        return {**headers, "Authorization": "Bearer never-log-this-token"}


class GmailResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read(self, _limit):
        return self.payload


class MismatchedReceiptSink:
    def __init__(self) -> None:
        self.route = provider_route()
        self.other = provider_route(destination="other@example.invalid")

    def send(self, notification):
        return DeliveryReceipt(
            route_id=self.other.route_id,
            provider=self.other.provider,
            destination_fingerprint=self.other.destination_fingerprint,
            route_version=self.other.route_version,
            event_key=notification.dedupe_key,
            payload_hash=notification_payload_hash(notification),
            provider_receipt_id="wrong-destination-receipt",
            accepted_at=NOW,
            assurance=DeliveryAssurance.PROVIDER_ACCEPTED,
        )


class NotificationDeliveryTests(unittest.TestCase):
    def test_concrete_gmail_sender_builds_bounded_rfc2822_api_request(self) -> None:
        route = provider_route()
        captured = {}

        def opener(request, *, timeout):
            captured["url"] = request.full_url
            captured["method"] = request.method
            captured["timeout"] = timeout
            captured["headers"] = dict(request.header_items())
            captured["body"] = request.data
            return GmailResponse(json.dumps({"id": "gmail-msg-42"}).encode())

        sender = GmailApiSender(
            route=route,
            authorizer=GmailAuthorizer(),
            destination="owner@example.invalid",
            sender_address="titan@example.invalid",
            opener=opener,
            clock=lambda: NOW,
        )
        notification = build_notification(
            "READINESS", {"event_id": "gmail-channel-test", "state": "paused"}
        )
        result = sender(
            notification,
            idempotency_key=notification.dedupe_key,
            timeout_seconds=4,
        )
        self.assertEqual(
            captured["url"],
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        )
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["timeout"], 4.0)
        self.assertIn("Authorization", captured["headers"])
        envelope = json.loads(captured["body"])
        message = BytesParser(policy=policy.default).parsebytes(
            base64.urlsafe_b64decode(envelope["raw"])
        )
        self.assertEqual(message["To"], "owner@example.invalid")
        self.assertEqual(message["From"], "titan@example.invalid")
        self.assertEqual(message["Subject"], notification.subject)
        self.assertEqual(message["X-Titan-Event-Key"], notification.dedupe_key)
        self.assertEqual(result["provider_receipt_id"], "gmail-msg-42")
        self.assertNotIn("never-log-this-token", json.dumps(result, default=str))

    def test_signed_sink_composition_requires_matching_runtime_injection(self) -> None:
        route = provider_route()
        config = gmail_config(route)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "was not injected"):
                build_notification_sink(
                    config,
                    local_jsonl_path=Path(directory) / "local.jsonl",
                )
            sink = build_notification_sink(
                config,
                local_jsonl_path=Path(directory) / "local.jsonl",
                gmail=GmailProviderBinding(
                    authorizer=GmailAuthorizer(),
                    destination="owner@example.invalid",
                    sender_address="titan@example.invalid",
                ),
                opener=lambda request, timeout: GmailResponse(
                    json.dumps({"id": "gmail-msg-composed"}).encode()
                ),
                clock=lambda: NOW,
            )
            self.assertEqual(sink.route.route_id, route.route_id)

    def test_runtime_composition_binds_implementation_authorization_and_route(self) -> None:
        route = provider_route()
        binding = GmailProviderBinding(
            authorizer=GmailAuthorizer(),
            destination="owner@example.invalid",
            sender_address="titan@example.invalid",
        )
        composition = RuntimeComposition(notification_provider=binding)
        root = Path(__file__).resolve().parents[1]
        composition.bind_release(
            release_manifest_for(
                root / "src/titan_brain/live/notifications.py",
                Path(__file__).resolve(),
            ),
            release_root=root,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "local.jsonl"
            sink = composition.notification_sink(
                gmail_config(route), local_jsonl_path=path
            )
            self.assertEqual(sink.route, route)

            changed = gmail_config(provider_route(version="gmail-v2"))
            with self.assertRaisesRegex(
                RuntimeCompositionError, "different signed identity"
            ):
                composition.notification_sink(changed, local_jsonl_path=path)

            wrong_authorization = dict(gmail_config(route))
            wrong_authorization["authorization_binding_id"] = "e" * 64
            wrong_composition = RuntimeComposition(notification_provider=binding)
            wrong_composition.bind_release(
                release_manifest_for(
                    root / "src/titan_brain/live/notifications.py",
                    Path(__file__).resolve(),
                ),
                release_root=root,
            )
            with self.assertRaisesRegex(
                RuntimeCompositionError, "authorization differs"
            ):
                wrong_composition.notification_sink(
                    wrong_authorization, local_jsonl_path=path
                )

    def test_runtime_profile_is_canonical_and_detects_authorizer_mutation(self) -> None:
        root = Path(__file__).resolve().parents[1]
        manifest = release_manifest_for(
            root / "src/titan_brain/live/notifications.py",
            Path(__file__).resolve(),
        )

        def composition(sender: str = "titan@example.invalid"):
            authorizer = GmailAuthorizer()
            value = RuntimeComposition(
                notification_provider=GmailProviderBinding(
                    authorizer=authorizer,
                    destination="owner@example.invalid",
                    sender_address=sender,
                )
            )
            value.bind_release(manifest, release_root=root)
            return value, authorizer

        first, first_authorizer = composition()
        second, _ = composition()
        different_sender, _ = composition("alerts@example.invalid")
        self.assertEqual(first.runtime_profile_hash, second.runtime_profile_hash)
        self.assertNotEqual(
            first.runtime_profile_hash, different_sender.runtime_profile_hash
        )

        first_authorizer.evidence = GmailAuthorizationEvidence(
            binding_id="e" * 64,
            credential_source="owner-injected-existing-oauth-client",
            scopes=("https://www.googleapis.com/auth/gmail.send",),
            authenticated=True,
        )
        with self.assertRaisesRegex(
            RuntimeCompositionError, "bound instance profile changed"
        ):
            _ = first.runtime_profile_hash

    def test_runtime_profile_rejects_uninventoried_authorizer_code(self) -> None:
        root = Path(__file__).resolve().parents[1]
        composition = RuntimeComposition(
            notification_provider=GmailProviderBinding(
                authorizer=GmailAuthorizer(),
                destination="owner@example.invalid",
                sender_address="titan@example.invalid",
            )
        )
        with self.assertRaisesRegex(
            RuntimeCompositionError, "gmail_authorizer"
        ):
            composition.bind_release(
                release_manifest_for(
                    root / "src/titan_brain/live/notifications.py"
                ),
                release_root=root,
            )

    def test_gmail_transport_error_never_echoes_token_or_provider_detail(self) -> None:
        route = provider_route()

        def failing(_request, *, timeout):
            raise OSError("never-log-this-token")

        sender = GmailApiSender(
            route=route,
            authorizer=GmailAuthorizer(),
            destination="owner@example.invalid",
            sender_address="titan@example.invalid",
            opener=failing,
        )
        notification = build_notification(
            "READINESS", {"event_id": "gmail-failure", "state": "paused"}
        )
        with self.assertRaises(RuntimeError) as raised:
            sender(
                notification,
                idempotency_key=notification.dedupe_key,
                timeout_seconds=2,
            )
        self.assertNotIn("never-log-this-token", str(raised.exception))

    def test_provider_sink_returns_route_event_and_payload_bound_receipt(self) -> None:
        route = provider_route()
        sender = CapturingProviderSender(route)
        sink = InjectedProviderNotificationSink(
            route=route, sender=sender, timeout_seconds=2.5
        )
        notification = build_notification(
            "READINESS", {"event_id": "channel-test-1", "state": "paused"}
        )
        receipt = sink.send(notification)
        self.assertTrue(
            receipt_satisfies_route(
                receipt,
                route,
                event_key=notification.dedupe_key,
                payload_hash=notification_payload_hash(notification),
            )
        )
        self.assertEqual(receipt.assurance, DeliveryAssurance.PROVIDER_ACCEPTED)
        self.assertEqual(sender.calls[0][1], notification.dedupe_key)
        self.assertEqual(sender.calls[0][2], 2.5)
        self.assertNotIn("owner@example.invalid", receipt.to_json())

    def test_route_or_destination_change_invalidates_old_receipt(self) -> None:
        old = provider_route()
        notification = build_notification(
            "READINESS", {"event_id": "channel-test-2", "state": "paused"}
        )
        receipt = InjectedProviderNotificationSink(
            route=old, sender=CapturingProviderSender(old)
        ).send(notification)
        self.assertFalse(receipt_satisfies_route(receipt, provider_route(version="gmail-v2")))
        self.assertFalse(
            receipt_satisfies_route(
                receipt, provider_route(destination="new@example.invalid")
            )
        )
        self.assertFalse(receipt_satisfies_route("legacy-local-hash", old))

    def test_owner_confirmation_is_explicit_and_local_staging_cannot_claim_it(self) -> None:
        route = provider_route()
        notification = build_notification(
            "READINESS", {"event_id": "channel-test-3", "state": "paused"}
        )
        receipt = InjectedProviderNotificationSink(
            route=route, sender=CapturingProviderSender(route)
        ).send(notification)
        confirmed_route = NotificationRoute(
            provider=route.provider,
            destination_fingerprint=route.destination_fingerprint,
            route_version=route.route_version,
            required_assurance=DeliveryAssurance.OWNER_CONFIRMED,
        )
        self.assertFalse(receipt_satisfies_route(receipt, confirmed_route))
        confirmed = receipt.owner_confirmed(confirmed_at=NOW + timedelta(seconds=5))
        self.assertTrue(receipt_satisfies_route(confirmed, confirmed_route))

        with tempfile.TemporaryDirectory() as directory:
            local = JsonlNotificationSink(Path(directory) / "notifications.jsonl")
            staged = local.send(notification)
            with self.assertRaisesRegex(ValueError, "local staging"):
                staged.owner_confirmed(confirmed_at=NOW + timedelta(seconds=5))

    def test_dispatcher_persists_structured_provider_receipt_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with LiveStateStore(Path(directory) / "state.sqlite3") as state:
                route = provider_route()
                dispatcher = OutboxDispatcher(
                    LiveStateOutboxAdapter(state, ACCOUNT),
                    InjectedProviderNotificationSink(
                        route=route, sender=CapturingProviderSender(route)
                    ),
                    worker_id="notification-worker-1",
                )
                message_id = dispatcher.enqueue(
                    "READINESS",
                    {"event_id": "channel-test-4", "state": "paused"},
                    NOW,
                )
                self.assertEqual(dispatcher.drain(NOW), (1, 0))
                row = state.row("notification_outbox", "message_id", message_id)
                receipt = DeliveryReceipt.from_json(row["delivery_receipt"])
                self.assertEqual(row["delivery_route_id"], route.route_id)
                self.assertEqual(row["delivery_assurance"], "PROVIDER_ACCEPTED")
                self.assertEqual(row["delivery_receipt_hash"], receipt.receipt_hash)
                self.assertEqual(row["delivery_payload_hash"], receipt.payload_hash)
                self.assertIsNone(row["claim_owner"])

    def test_receipt_route_mismatch_retries_instead_of_claiming_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with LiveStateStore(Path(directory) / "state.sqlite3") as state:
                dispatcher = OutboxDispatcher(
                    LiveStateOutboxAdapter(state, ACCOUNT),
                    MismatchedReceiptSink(),
                    worker_id="notification-worker-2",
                )
                message_id = dispatcher.enqueue(
                    "READINESS",
                    {"event_id": "channel-test-5", "state": "paused"},
                    NOW,
                )
                self.assertEqual(dispatcher.drain(NOW), (0, 1))
                row = state.row("notification_outbox", "message_id", message_id)
                self.assertEqual(row["state"], "PENDING")
                self.assertEqual(
                    row["last_error"],
                    "NOTIFICATION_RECEIPT_PROVENANCE_MISMATCH",
                )
                self.assertIsNone(row["delivery_route_id"])
                self.assertIsNone(row["claim_owner"])

    def test_transactional_claims_prevent_overlap_and_allow_expiry_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with LiveStateStore(Path(directory) / "state.sqlite3") as state:
                first = LiveStateOutboxAdapter(state, ACCOUNT)
                second = LiveStateOutboxAdapter(state, ACCOUNT)
                notification = build_notification(
                    "RUNTIME_INCIDENT", {"event_id": "claim-1", "state": "blocked"}
                )
                message_id = first.enqueue_notification(notification, NOW)
                claimed = first.claim_notifications(
                    NOW, "worker-a", NOW + timedelta(seconds=30), 20
                )
                self.assertEqual([row["outbox_id"] for row in claimed], [message_id])
                self.assertEqual(
                    second.claim_notifications(
                        NOW + timedelta(seconds=1),
                        "worker-b",
                        NOW + timedelta(seconds=31),
                        20,
                    ),
                    [],
                )
                reclaimed = second.claim_notifications(
                    NOW + timedelta(seconds=31),
                    "worker-b",
                    NOW + timedelta(seconds=61),
                    20,
                )
                self.assertEqual(len(reclaimed), 1)
                with self.assertRaisesRegex(StateConflict, "does not own"):
                    first.notification_sent(message_id, "legacy-receipt", NOW, "worker-a")
                second.notification_sent(
                    message_id,
                    "legacy-receipt",
                    NOW + timedelta(seconds=31),
                    "worker-b",
                )
                self.assertEqual(
                    state.row("notification_outbox", "message_id", message_id)["state"],
                    "DELIVERED",
                )

    def test_independent_worker_drains_without_service_or_broker_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with LiveStateStore(root / "state.sqlite3") as state:
                dispatcher = OutboxDispatcher(
                    LiveStateOutboxAdapter(state, ACCOUNT),
                    JsonlNotificationSink(
                        root / "notifications.jsonl", clock=lambda: NOW
                    ),
                    worker_id="standalone-worker",
                )
                dispatcher.enqueue(
                    "END_OF_DAY", {"event_id": "eod-1", "state": "flat"}, NOW
                )
                worker = NotificationWorker(dispatcher, clock=lambda: NOW)
                self.assertEqual(worker.run_once(), (1, 0))
                self.assertTrue((root / "notifications.jsonl").is_file())

    def test_process_worker_entrypoint_drains_enqueue_only_service_outbox(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / "state.sqlite3"
            with LiveStateStore(state_path) as state:
                publisher = EnqueueOnlyOutbox(
                    LiveStateOutboxAdapter(state, ACCOUNT)
                )
                message_id = publisher.enqueue(
                    "RUNTIME_INCIDENT",
                    {"event_id": "process-worker-1", "state": "blocked"},
                    NOW,
                )
                self.assertEqual(
                    state.row("notification_outbox", "message_id", message_id)[
                        "state"
                    ],
                    "PENDING",
                )
            result = run_notification_worker(
                state_path=state_path,
                account_key=ACCOUNT,
                sink=JsonlNotificationSink(
                    root / "notifications.jsonl", clock=lambda: NOW
                ),
                stop_event=threading.Event(),
                settings=NotificationWorkerSettings(),
                worker_id="independent-process-test",
                once=True,
                clock=lambda: NOW,
            )
            self.assertEqual(result, (1, 0))
            with LiveStateStore(state_path) as state:
                self.assertEqual(
                    state.row("notification_outbox", "message_id", message_id)[
                        "state"
                    ],
                    "DELIVERED",
                )
                worker = state.row(
                    "notification_worker_lease", "account_key", ACCOUNT
                )
                self.assertEqual(worker["worker_id"], "independent-process-test")
                self.assertEqual(worker["provider"], "local_jsonl")
                self.assertIsNotNone(worker["released_at"])

    def test_notification_worker_lease_is_single_owner_and_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with LiveStateStore(Path(directory) / "state.sqlite3") as state:
                route = provider_route()
                first_generation = state.acquire_notification_worker_lease(
                    account_key=ACCOUNT,
                    worker_id="worker-a",
                    process_id=111,
                    route_id=route.route_id,
                    provider=route.provider,
                    destination_fingerprint=route.destination_fingerprint,
                    route_version=route.route_version,
                    acquired_at=NOW,
                )
                self.assertEqual(first_generation, 1)
                with self.assertRaisesRegex(StateConflict, "already held"):
                    state.acquire_notification_worker_lease(
                        account_key=ACCOUNT,
                        worker_id="worker-b",
                        process_id=222,
                        route_id=route.route_id,
                        provider=route.provider,
                        destination_fingerprint=route.destination_fingerprint,
                        route_version=route.route_version,
                        acquired_at=NOW + timedelta(seconds=1),
                    )
                state.heartbeat_notification_worker(
                    account_key=ACCOUNT,
                    worker_id="worker-a",
                    generation=first_generation,
                    process_id=111,
                    observed_at=NOW + timedelta(seconds=2),
                    sent_count=2,
                    failed_count=0,
                )
                lease = state.row(
                    "notification_worker_lease", "account_key", ACCOUNT
                )
                self.assertEqual(lease["last_sent_count"], 2)
                self.assertEqual(lease["last_failed_count"], 0)
                state.release_notification_worker_lease(
                    account_key=ACCOUNT,
                    worker_id="worker-a",
                    generation=first_generation,
                    process_id=111,
                    released_at=NOW + timedelta(seconds=3),
                )
                second_generation = state.acquire_notification_worker_lease(
                    account_key=ACCOUNT,
                    worker_id="worker-b",
                    process_id=222,
                    route_id=route.route_id,
                    provider=route.provider,
                    destination_fingerprint=route.destination_fingerprint,
                    route_version=route.route_version,
                    acquired_at=NOW + timedelta(seconds=4),
                )
                self.assertEqual(second_generation, 2)
                with self.assertRaisesRegex(StateConflict, "does not own"):
                    state.heartbeat_notification_worker(
                        account_key=ACCOUNT,
                        worker_id="worker-a",
                        generation=first_generation,
                        process_id=111,
                        observed_at=NOW + timedelta(seconds=5),
                        sent_count=0,
                        failed_count=0,
                    )

    def test_runtime_refuses_schema_one_and_requires_locked_installer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE schema_meta(
                  singleton INTEGER PRIMARY KEY, version INTEGER NOT NULL,
                  applied_at TEXT NOT NULL
                );
                INSERT INTO schema_meta VALUES(1,1,'2026-09-07T00:00:00+00:00');
                CREATE TABLE notification_outbox(
                  message_id TEXT PRIMARY KEY,event_key TEXT UNIQUE,account_key TEXT,
                  template TEXT,payload_json TEXT,created_at TEXT,state TEXT,
                  attempt_count INTEGER,last_attempt_at TEXT,next_attempt_at TEXT,
                  delivered_at TEXT,last_error TEXT,delivery_receipt TEXT
                );
                PRAGMA user_version=1;
                """
            )
            connection.close()
            with self.assertRaisesRegex(
                UnsupportedSchema, "fixed-lock paused release installer"
            ):
                LiveStateStore(path)
            connection = sqlite3.connect(path)
            try:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
                columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info('notification_outbox')"
                    ).fetchall()
                }
                self.assertNotIn("claim_owner", columns)
                self.assertIsNone(
                    connection.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type='table' AND name='notification_worker_lease'"
                    ).fetchone()
                )
            finally:
                connection.close()

    def test_runtime_refuses_schema_two_and_requires_locked_installer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE schema_meta(
                  singleton INTEGER PRIMARY KEY, version INTEGER NOT NULL,
                  applied_at TEXT NOT NULL
                );
                INSERT INTO schema_meta VALUES(1,2,'2026-09-07T00:00:00+00:00');
                PRAGMA user_version=2;
                """
            )
            connection.close()
            with self.assertRaisesRegex(
                UnsupportedSchema, "fixed-lock paused release installer"
            ):
                LiveStateStore(path)
            connection = sqlite3.connect(path)
            try:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
                self.assertIsNone(
                    connection.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type='table' AND name='notification_worker_lease'"
                    ).fetchone()
                )
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
