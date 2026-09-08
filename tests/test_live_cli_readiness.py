from __future__ import annotations

from contextlib import redirect_stderr
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import io
import copy
import hashlib
import os
import tempfile
import unittest
from unittest import mock

from titan_brain.live.broker import (
    BrokerSide,
    EquityOrderType,
    FakeBrokerClient,
    FillSnapshot,
    MarketHours,
    OrderSnapshot,
    PositionSnapshot,
    TimeInForce,
)
from titan_brain.live.broker.robinhood import RobinhoodBrokerAdapter
from titan_brain.live.cli import (
    ACCOUNT_KEY,
    InstallLayout,
    LegacySchedulerRuntimeEvidence,
    _legacy_retirement_payload,
    _machine_readiness,
    _probe_legacy_heartbeat,
    _probe_legacy_writer_processes,
    _verified_notification_receipt,
    build_parser,
)
from titan_brain.live.composition import RuntimeComposition
from titan_brain.live.notification_worker import notification_worker_health
from titan_brain.live.models import BrokerOrderState
from titan_brain.live.notifications import (
    DeliveryAssurance,
    GmailAuthorizationEvidence,
    GmailProviderBinding,
    InjectedProviderNotificationSink,
    JsonlNotificationSink,
    LiveStateOutboxAdapter,
    NotificationRoute,
    OutboxDispatcher,
    destination_fingerprint,
)
from titan_brain.live.policy import PolicyBundle, sha256_json
from titan_brain.live.state import LiveStateStore, object_hash
from titan_brain.live.writer_lock import AccountWriterLock
from tests.live_activation_support import record_flat_reconciliation
from tests.test_live_service import account_snapshot


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 8, 12, 30, tzinfo=timezone.utc)


def readiness_snapshot():
    return replace(
        account_snapshot(),
        observed_at=NOW,
        received_at=NOW,
        risk_evidence_as_of=NOW,
    )


class StaticMarketHealth:
    def health(self, *, now: datetime):
        return SimpleNamespace(
            blockers=(),
            producer_fresh=True,
            latest_quote_at=now,
            latest_completed_bar_at=now,
        )


class MutableProbeClock:
    def __init__(self) -> None:
        self.current = NOW
        self.ticks = 100.0

    def __call__(self) -> datetime:
        return self.current

    def monotonic(self) -> float:
        return self.ticks

    def advance(self, *, wall_seconds: float, monotonic_seconds: float | None = None) -> None:
        self.current += timedelta(seconds=wall_seconds)
        self.ticks += wall_seconds if monotonic_seconds is None else monotonic_seconds


class DelayedReceiptBroker(FakeBrokerClient):
    def __init__(self, probe_clock: MutableProbeClock, *, delay_seconds: float) -> None:
        super().__init__(initial_snapshot=readiness_snapshot(), clock=probe_clock)
        self.probe_clock = probe_clock
        self.delay_seconds = delay_seconds

    def get_account_snapshot(self, account_masked: str):
        self.probe_clock.advance(wall_seconds=self.delay_seconds)
        return super().get_account_snapshot(account_masked)


class EarliestPageTimestampBroker(FakeBrokerClient):
    def __init__(self, probe_clock: MutableProbeClock, *, delay_seconds: float) -> None:
        super().__init__(initial_snapshot=readiness_snapshot(), clock=probe_clock)
        self.probe_clock = probe_clock
        self.delay_seconds = delay_seconds

    def get_account_snapshot(self, account_masked: str):
        first_page = super().get_account_snapshot(account_masked)
        self.probe_clock.advance(wall_seconds=self.delay_seconds)
        return first_page


class RollbackBroker(FakeBrokerClient):
    def __init__(self, probe_clock: MutableProbeClock) -> None:
        super().__init__(initial_snapshot=readiness_snapshot(), clock=probe_clock)
        self.probe_clock = probe_clock

    def get_account_snapshot(self, account_masked: str):
        self.probe_clock.advance(wall_seconds=-1, monotonic_seconds=1)
        return super().get_account_snapshot(account_masked)


class ReadinessGmailAuthorizer:
    evidence = GmailAuthorizationEvidence(
        binding_id="d" * 64,
        credential_source="owner-injected-existing-oauth-client",
        scopes=("https://www.googleapis.com/auth/gmail.send",),
        authenticated=True,
    )

    def authorize(self, headers):
        raise AssertionError("readiness must not send a notification")


class CliReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.install = Path(self.temporary.name) / "full-live"
        self.fixed_lock_directory = Path(self.temporary.name) / "fixed-user-locks"
        self.lock_directory_patch = mock.patch(
            "titan_brain.live.cli.user_account_writer_lock_directory",
            return_value=self.fixed_lock_directory,
        )
        self.lock_directory_patch.start()
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
        self.lock_directory_patch.stop()
        self.temporary.cleanup()

    def test_install_layout_uses_one_user_lock_root_across_install_roots(self) -> None:
        other = InstallLayout(Path(self.temporary.name) / "other/full-live")
        self.assertEqual(self.layout.lock_path, self.fixed_lock_directory)
        self.assertEqual(other.lock_path, self.fixed_lock_directory)
        self.assertNotEqual(self.layout.lock_path, self.layout.root.parent)
        self.assertNotEqual(other.lock_path, other.root.parent)

    def paused_legacy_path(self) -> Path:
        target = Path(self.temporary.name) / "automation.toml"
        target.write_text(
            'id = "robinhood-momentum-engine"\n'
            'kind = "heartbeat"\n'
            'status = "PAUSED"\n',
            encoding="utf-8",
        )
        return target

    def scheduler_runtime(
        self,
        legacy_path: Path,
        *,
        runtime_id: str = "synthetic-codex-scheduler-runtime-1",
        query_receipt_hash: str = "9" * 64,
    ) -> LegacySchedulerRuntimeEvidence:
        heartbeat_id, status, config_hash, disabled, error = (
            _probe_legacy_heartbeat(legacy_path)
        )
        self.assertTrue(disabled)
        self.assertIsNone(error)
        self.assertIsNotNone(config_hash)
        return LegacySchedulerRuntimeEvidence(
            automation_id=heartbeat_id,
            scheduler_runtime_id=runtime_id,
            status=status,
            config_hash=str(config_hash),
            active_execution_count=0,
            observed_at=NOW,
            query_receipt_hash=query_receipt_hash,
        )

    def collect(
        self,
        broker,
        *,
        clock: MutableProbeClock | None = None,
        legacy_path: Path | None = None,
        legacy_process_listing: str = "",
        legacy_scheduler_runtime_evidence: LegacySchedulerRuntimeEvidence | None = None,
        persist_fresh_broker_read: bool = True,
        policy: PolicyBundle | None = None,
        manifest: dict | None = None,
        runtime_composition: RuntimeComposition | None = None,
    ):
        selected_policy = policy or self.policy
        lock = AccountWriterLock(
            self.layout.lock_path, ACCOUNT_KEY, owner_id="readiness-helper"
        )
        with lock:
            self.store.acquire_writer_lease(
                account_key=ACCOUNT_KEY,
                owner_id=lock.owner_id,
                acquired_at=(clock() if clock is not None else NOW),
                recover_stale=True,
            )
            try:
                evidence = _machine_readiness(
                    layout=self.layout,
                    manifest=manifest or self.manifest,
                    policy=selected_policy,
                    store=self.store,
                    writer_lock=lock,
                    now=(clock() if clock is not None else NOW),
                    broker=broker,
                    market_source=StaticMarketHealth(),
                    legacy_heartbeat_path=legacy_path
                    or Path(self.temporary.name) / "missing.toml",
                    persist_fresh_broker_read=persist_fresh_broker_read,
                    clock=clock,
                    monotonic_clock=(clock.monotonic if clock is not None else None),
                    legacy_process_listing=legacy_process_listing,
                    legacy_scheduler_runtime_evidence=(
                        legacy_scheduler_runtime_evidence
                    ),
                    runtime_composition=runtime_composition,
                )
            finally:
                self.store.release_writer_lease(
                    account_key=ACCOUNT_KEY,
                    owner_id=lock.owner_id,
                    released_at=(clock() if clock is not None else NOW),
                )
        return evidence

    def record_retirement_receipt(
        self,
        evidence,
        legacy_path: Path,
        scheduler_runtime: LegacySchedulerRuntimeEvidence,
    ) -> str:
        lock = AccountWriterLock(
            self.layout.lock_path, ACCOUNT_KEY, owner_id="retirement-receipt-test"
        )
        with lock:
            self.store.acquire_writer_lease(
                account_key=ACCOUNT_KEY,
                owner_id=lock.owner_id,
                acquired_at=NOW,
                recover_stale=True,
            )
            try:
                _, status, config_hash, scheduler_disabled, error = (
                    _probe_legacy_heartbeat(legacy_path)
                )
                self.assertTrue(scheduler_disabled)
                self.assertIsNone(error)
                self.assertIsNotNone(config_hash)
                payload = _legacy_retirement_payload(
                    manifest=self.manifest,
                    policy=self.policy,
                    legacy_heartbeat_id="robinhood-momentum-engine",
                    legacy_heartbeat_status=status,
                    legacy_heartbeat_config_hash=str(config_hash),
                    scheduler_runtime=scheduler_runtime,
                    durable_snapshot_id=str(evidence.durable_snapshot_id),
                    reconciliation_audit_event_id=str(
                        evidence.reconciliation_audit_event_id
                    ),
                    writer_lock=lock,
                    writer_lock_owner_id=lock.owner_id,
                    writer_lock_process_id=int(lock.holder_metadata()["pid"]),
                    recorded_at=NOW,
                )
                receipt_id = object_hash(payload)
                self.store.append_event(
                    stream=ACCOUNT_KEY,
                    event_type="LEGACY_ACCOUNT_WRITER_RETIRED",
                    entity_type="legacy_writer_retirement",
                    entity_id=receipt_id,
                    occurred_at=NOW,
                    payload=payload,
                )
            finally:
                self.store.release_writer_lease(
                    account_key=ACCOUNT_KEY,
                    owner_id=lock.owner_id,
                    released_at=NOW,
                )
        return receipt_id

    @staticmethod
    def notification_route(
        *,
        destination: str = "owner@example.invalid",
        version: str = "readiness-v1",
        assurance: DeliveryAssurance = DeliveryAssurance.PROVIDER_ACCEPTED,
    ) -> NotificationRoute:
        return NotificationRoute(
            provider="gmail",
            destination_fingerprint=destination_fingerprint(
                "gmail", destination
            ),
            route_version=version,
            required_assurance=assurance,
        )

    def deliver_provider_readiness(
        self, route: NotificationRoute, *, event_id: str
    ) -> str:
        class Sender:
            def __call__(self, notification, *, idempotency_key, timeout_seconds):
                return {
                    "provider": route.provider,
                    "destination_fingerprint": route.destination_fingerprint,
                    "provider_receipt_id": f"gmail-{event_id}",
                    "accepted_at": NOW,
                }

        dispatcher = OutboxDispatcher(
            LiveStateOutboxAdapter(self.store, ACCOUNT_KEY),
            InjectedProviderNotificationSink(route=route, sender=Sender()),
            worker_id=f"readiness-{event_id}",
        )
        message_id = dispatcher.enqueue(
            "READINESS",
            {"event_id": event_id, "state": "notification_test"},
            NOW,
        )
        self.assertEqual(dispatcher.drain(NOW), (1, 0))
        return message_id

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

    def test_missing_legacy_toml_is_not_retirement_evidence(self) -> None:
        missing = Path(self.temporary.name) / "absent.toml"
        _, status, digest, disabled, error = _probe_legacy_heartbeat(missing)
        self.assertEqual(status, "ABSENT")
        self.assertIsNone(digest)
        self.assertFalse(disabled)
        self.assertEqual(error, "legacy_heartbeat:ABSENT")

    def test_normal_later_receipt_uses_probe_end_and_never_has_negative_age(self) -> None:
        clock = MutableProbeClock()
        evidence = self.collect(
            DelayedReceiptBroker(clock, delay_seconds=0.4), clock=clock
        )
        self.assertEqual(evidence.probe_started_at, NOW)
        self.assertEqual(evidence.probe_completed_at, NOW + timedelta(seconds=0.4))
        self.assertAlmostEqual(evidence.probe_elapsed_monotonic_seconds or -1, 0.4)
        self.assertTrue(evidence.probe_clock_stable)
        self.assertEqual(evidence.broker_snapshot_age_seconds, 0.0)
        self.assertIsNotNone(evidence.durable_snapshot_age_seconds)
        self.assertGreaterEqual(float(evidence.durable_snapshot_age_seconds), 0.0)
        self.assertNotIn(
            "BROKER_SNAPSHOT_STALE",
            evidence.blockers(self.policy, now=evidence.collected_at),
        )

    def test_slow_paginated_read_is_assessed_at_probe_end(self) -> None:
        clock = MutableProbeClock()
        evidence = self.collect(
            EarliestPageTimestampBroker(clock, delay_seconds=6), clock=clock
        )
        self.assertEqual(evidence.broker_snapshot_age_seconds, 6.0)
        self.assertIn(
            "BROKER_SNAPSHOT_STALE",
            evidence.blockers(self.policy, now=evidence.collected_at),
        )

    def test_genuine_future_timestamp_is_not_clamped_to_zero(self) -> None:
        clock = MutableProbeClock()

        class FutureTimestampBroker(FakeBrokerClient):
            def get_account_snapshot(self, account_masked: str):
                snapshot = super().get_account_snapshot(account_masked)
                future = clock() + timedelta(seconds=2)
                return replace(
                    snapshot,
                    observed_at=future,
                    received_at=future,
                    risk_evidence_as_of=future,
                )

        evidence = self.collect(
            FutureTimestampBroker(initial_snapshot=readiness_snapshot(), clock=clock),
            clock=clock,
        )
        self.assertEqual(evidence.broker_snapshot_age_seconds, -2.0)
        self.assertIn(
            "BROKER_SNAPSHOT_STALE",
            evidence.blockers(self.policy, now=evidence.collected_at),
        )

    def test_wall_clock_rollback_fails_closed_even_when_receipt_age_is_zero(self) -> None:
        clock = MutableProbeClock()
        evidence = self.collect(RollbackBroker(clock), clock=clock)
        self.assertFalse(evidence.probe_clock_stable)
        self.assertTrue(
            any("WALL_CLOCK_ROLLBACK" in item for item in evidence.probe_errors)
        )
        blockers = evidence.blockers(self.policy, now=evidence.collected_at)
        self.assertIn("READINESS_CLOCK_UNSTABLE", blockers)
        self.assertIn("READINESS_PROBE_ERROR", blockers)

    def test_stale_reconnect_data_remains_stale(self) -> None:
        clock = MutableProbeClock()

        class ReconnectedStaleBroker(FakeBrokerClient):
            def get_account_snapshot(self, account_masked: str):
                snapshot = super().get_account_snapshot(account_masked)
                stale = clock() - timedelta(seconds=10)
                return replace(
                    snapshot,
                    observed_at=stale,
                    received_at=stale,
                    risk_evidence_as_of=stale,
                )

        evidence = self.collect(
            ReconnectedStaleBroker(initial_snapshot=readiness_snapshot(), clock=clock),
            clock=clock,
        )
        self.assertEqual(evidence.broker_snapshot_age_seconds, 10.0)
        self.assertIn(
            "BROKER_SNAPSHOT_STALE",
            evidence.blockers(self.policy, now=evidence.collected_at),
        )

    def test_paused_toml_alone_cannot_retire_legacy_writer(self) -> None:
        legacy = self.paused_legacy_path()
        scheduler_runtime = self.scheduler_runtime(legacy)
        evidence = self.collect(
            FakeBrokerClient(initial_snapshot=readiness_snapshot(), clock=lambda: NOW),
            legacy_path=legacy,
            legacy_scheduler_runtime_evidence=scheduler_runtime,
        )
        self.assertFalse(evidence.old_writer_disabled)
        self.assertIn("legacy_retirement:RECEIPT_MISSING", evidence.probe_errors)

    def test_paused_file_without_scheduler_runtime_identity_fails_closed(self) -> None:
        legacy = self.paused_legacy_path()
        evidence = self.collect(
            FakeBrokerClient(initial_snapshot=readiness_snapshot(), clock=lambda: NOW),
            legacy_path=legacy,
        )
        self.assertFalse(evidence.old_writer_disabled)
        self.assertIn(
            "legacy_retirement:SCHEDULER_RUNTIME_IDENTITY_UNAVAILABLE",
            evidence.probe_errors,
        )

    def test_hash_bound_retirement_receipt_is_reprobed_before_acceptance(self) -> None:
        legacy = self.paused_legacy_path()
        scheduler_runtime = self.scheduler_runtime(legacy)
        broker = FakeBrokerClient(initial_snapshot=readiness_snapshot(), clock=lambda: NOW)
        initial = self.collect(
            broker,
            legacy_path=legacy,
            legacy_scheduler_runtime_evidence=scheduler_runtime,
        )
        self.record_retirement_receipt(initial, legacy, scheduler_runtime)

        retired = self.collect(
            broker,
            legacy_path=legacy,
            persist_fresh_broker_read=False,
            legacy_scheduler_runtime_evidence=scheduler_runtime,
        )
        self.assertTrue(retired.old_writer_disabled)
        self.assertFalse(
            any(item.startswith("legacy_retirement:") for item in retired.probe_errors)
        )

        running = self.collect(
            broker,
            legacy_path=legacy,
            legacy_process_listing="4321 titan_runtime.mcp_server --account 7153\n",
            persist_fresh_broker_read=False,
            legacy_scheduler_runtime_evidence=scheduler_runtime,
        )
        self.assertFalse(running.old_writer_disabled)
        self.assertIn(
            "legacy_retirement:LEGACY_PROCESS_STILL_RUNNING", running.probe_errors
        )

        changed_runtime = self.collect(
            broker,
            legacy_path=legacy,
            persist_fresh_broker_read=False,
            legacy_scheduler_runtime_evidence=self.scheduler_runtime(
                legacy,
                runtime_id="synthetic-codex-scheduler-runtime-2",
                query_receipt_hash="8" * 64,
            ),
        )
        self.assertFalse(changed_runtime.old_writer_disabled)
        self.assertIn(
            "legacy_retirement:SCHEDULER_RUNTIME_BINDING_MISMATCH",
            changed_runtime.probe_errors,
        )

    def test_restart_after_partial_fill_invalidates_retirement_quiescence(self) -> None:
        legacy = self.paused_legacy_path()
        scheduler_runtime = self.scheduler_runtime(legacy)
        flat_broker = FakeBrokerClient(
            initial_snapshot=readiness_snapshot(), clock=lambda: NOW
        )
        initial = self.collect(
            flat_broker,
            legacy_path=legacy,
            legacy_scheduler_runtime_evidence=scheduler_runtime,
        )
        self.record_retirement_receipt(initial, legacy, scheduler_runtime)
        partial = OrderSnapshot(
            broker_order_id="legacy-partial-1",
            account_masked="••••7153",
            symbol="XYZ",
            side=BrokerSide.BUY,
            order_type=EquityOrderType.LIMIT,
            state=BrokerOrderState.PARTIALLY_FILLED,
            requested_quantity=Decimal("2"),
            cumulative_filled_quantity=Decimal("1"),
            market_hours=MarketHours.REGULAR,
            time_in_force=TimeInForce.GFD,
            broker_updated_at=NOW,
            received_at=NOW,
            limit_price=Decimal("10.00"),
            client_ref_id=None,
            fills=(
                FillSnapshot(
                    fill_id="legacy-fill-1",
                    quantity=Decimal("1"),
                    price=Decimal("10.00"),
                    executed_at=NOW,
                ),
            ),
        )
        exposed_snapshot = replace(
            readiness_snapshot(),
            equity_positions=(
                PositionSnapshot(
                    symbol="XYZ",
                    quantity=Decimal("1"),
                    sellable_quantity=Decimal("1"),
                    average_price=Decimal("10.00"),
                ),
            ),
            equity_orders=(partial,),
        )
        evidence = self.collect(
            FakeBrokerClient(initial_snapshot=exposed_snapshot, clock=lambda: NOW),
            legacy_path=legacy,
            legacy_scheduler_runtime_evidence=scheduler_runtime,
        )
        self.assertFalse(evidence.old_writer_disabled)
        self.assertIn("legacy_retirement:INFLIGHT_DRAIN_UNPROVEN", evidence.probe_errors)

    def test_process_probe_redacts_command_and_detects_known_identity(self) -> None:
        observations, error = _probe_legacy_writer_processes(
            "17 titan_runtime.mcp_server --token super-secret\n"
        )
        self.assertIsNone(error)
        self.assertEqual(observations[0]["pid"], 17)
        self.assertEqual(observations[0]["marker"], "titan_runtime.mcp_server")
        self.assertEqual(len(observations[0]["command_sha256"]), 64)
        self.assertNotIn("super-secret", str(observations))

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

    def test_notification_receipt_binds_exact_route_event_payload_and_hashes(self) -> None:
        route = self.notification_route()
        message_id = self.deliver_provider_readiness(
            route, event_id="exact-route-1"
        )
        row = self.store.row("notification_outbox", "message_id", message_id)
        self.assertEqual(
            _verified_notification_receipt(
                self.store, account_key=ACCOUNT_KEY, route=route
            ),
            (row["delivery_receipt_hash"], NOW),
        )
        self.assertIsNone(
            _verified_notification_receipt(
                self.store,
                account_key=ACCOUNT_KEY,
                route=self.notification_route(version="readiness-v2"),
            )
        )
        self.assertIsNone(
            _verified_notification_receipt(
                self.store,
                account_key=ACCOUNT_KEY,
                route=self.notification_route(
                    destination="different@example.invalid"
                ),
            )
        )
        self.assertIsNone(
            _verified_notification_receipt(
                self.store,
                account_key=ACCOUNT_KEY,
                route=self.notification_route(
                    assurance=DeliveryAssurance.OWNER_CONFIRMED
                ),
            )
        )

        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE notification_outbox SET delivery_receipt_hash=? "
                "WHERE message_id=?",
                ("f" * 64, message_id),
            )
        self.assertIsNone(
            _verified_notification_receipt(
                self.store, account_key=ACCOUNT_KEY, route=route
            )
        )

    def test_local_or_legacy_receipt_never_satisfies_provider_readiness(self) -> None:
        local_dispatcher = OutboxDispatcher(
            LiveStateOutboxAdapter(self.store, ACCOUNT_KEY),
            JsonlNotificationSink(
                Path(self.temporary.name) / "notifications.jsonl",
                clock=lambda: NOW,
            ),
            worker_id="readiness-local",
        )
        local_dispatcher.enqueue(
            "READINESS",
            {"event_id": "local-route-1", "state": "notification_test"},
            NOW,
        )
        self.assertEqual(local_dispatcher.drain(NOW), (1, 0))
        self.assertIsNone(
            _verified_notification_receipt(
                self.store,
                account_key=ACCOUNT_KEY,
                route=self.notification_route(),
            )
        )

    def test_machine_readiness_accepts_only_current_injected_provider_route(self) -> None:
        route = self.notification_route()
        self.deliver_provider_readiness(route, event_id="machine-route-1")
        self.store.acquire_notification_worker_lease(
            account_key=ACCOUNT_KEY,
            worker_id="readiness-worker",
            process_id=os.getpid(),
            route_id=route.route_id,
            provider=route.provider,
            destination_fingerprint=route.destination_fingerprint,
            route_version=route.route_version,
            acquired_at=NOW,
        )
        config = copy.deepcopy(self.policy.config)
        config["notifications"].update(
            {
                "delivery_sink": "gmail_api",
                "destination_bridge_configured": True,
                "provider": "gmail",
                "destination_fingerprint": route.destination_fingerprint,
                "route_version": route.route_version,
                "required_assurance": "PROVIDER_ACCEPTED",
                "provider_composition_id": "titan.gmail_api.rfc2822.oauth_injected.v1",
                "authorization_binding_id": "d" * 64,
                "timeout_seconds": 5,
            }
        )
        policy = replace(
            self.policy,
            config=config,
            config_hash=sha256_json(config),
        )
        policy.validate()
        notification_source = ROOT / "src/titan_brain/live/notifications.py"
        source_bytes = notification_source.read_bytes()
        authorizer_source = Path(__file__).resolve()
        authorizer_bytes = authorizer_source.read_bytes()
        manifest = {
            "release_manifest_hash": "a" * 64,
            "files": [
                {
                    "path": "src/titan_brain/live/notifications.py",
                    "sha256": hashlib.sha256(source_bytes).hexdigest(),
                    "size": len(source_bytes),
                },
                {
                    "path": authorizer_source.relative_to(ROOT).as_posix(),
                    "sha256": hashlib.sha256(authorizer_bytes).hexdigest(),
                    "size": len(authorizer_bytes),
                },
            ],
        }
        self.layout.release_root.symlink_to(ROOT, target_is_directory=True)
        composition = RuntimeComposition(
            notification_provider=GmailProviderBinding(
                authorizer=ReadinessGmailAuthorizer(),
                destination="owner@example.invalid",
                sender_address="titan@example.invalid",
            )
        )
        evidence = self.collect(
            FakeBrokerClient(
                initial_snapshot=readiness_snapshot(), clock=lambda: NOW
            ),
            policy=policy,
            manifest=manifest,
            runtime_composition=composition,
        )
        self.assertTrue(evidence.notification_destination_configured)
        self.assertTrue(evidence.notification_tested)
        self.assertIsNotNone(evidence.notification_delivery_receipt_hash)
        self.assertFalse(
            any(
                item.startswith(("notification_route:", "notification_worker:"))
                for item in evidence.probe_errors
            )
        )

    def test_notification_worker_health_requires_live_exact_route_and_empty_outbox(self) -> None:
        route = self.notification_route()
        healthy, errors = notification_worker_health(
            self.store, account_key=ACCOUNT_KEY, route=route, now=NOW
        )
        self.assertFalse(healthy)
        self.assertIn("notification_worker:LEASE_MISSING", errors)

        self.store.acquire_notification_worker_lease(
            account_key=ACCOUNT_KEY,
            worker_id="health-worker",
            process_id=os.getpid(),
            route_id=route.route_id,
            provider=route.provider,
            destination_fingerprint=route.destination_fingerprint,
            route_version=route.route_version,
            acquired_at=NOW,
        )
        healthy, errors = notification_worker_health(
            self.store, account_key=ACCOUNT_KEY, route=route, now=NOW
        )
        self.assertTrue(healthy)
        self.assertEqual(errors, ())

        OutboxDispatcher(
            LiveStateOutboxAdapter(self.store, ACCOUNT_KEY),
            JsonlNotificationSink(
                Path(self.temporary.name) / "worker-health.jsonl",
                clock=lambda: NOW,
            ),
            worker_id="must-not-deliver",
        ).enqueue(
            "RUNTIME_INCIDENT",
            {"event_id": "pending-health-1", "state": "blocked"},
            NOW,
        )
        healthy, errors = notification_worker_health(
            self.store, account_key=ACCOUNT_KEY, route=route, now=NOW
        )
        self.assertFalse(healthy)
        self.assertIn("notification_worker:OUTBOX_BACKLOG_PENDING", errors)

    def test_notification_worker_health_rejects_stale_wrong_and_released_lease(self) -> None:
        route = self.notification_route()
        wrong_route = self.notification_route(version="wrong-route")
        wrong_generation = self.store.acquire_notification_worker_lease(
            account_key=ACCOUNT_KEY,
            worker_id="wrong-worker",
            process_id=os.getpid(),
            route_id=wrong_route.route_id,
            provider=wrong_route.provider,
            destination_fingerprint=wrong_route.destination_fingerprint,
            route_version=wrong_route.route_version,
            acquired_at=NOW - timedelta(seconds=16),
        )
        healthy, errors = notification_worker_health(
            self.store, account_key=ACCOUNT_KEY, route=route, now=NOW
        )
        self.assertFalse(healthy)
        self.assertIn("notification_worker:ROUTE_MISMATCH", errors)
        self.assertIn("notification_worker:HEARTBEAT_STALE_OR_FUTURE", errors)

        self.store.release_notification_worker_lease(
            account_key=ACCOUNT_KEY,
            worker_id="wrong-worker",
            generation=wrong_generation,
            process_id=os.getpid(),
            released_at=NOW,
        )
        _, errors = notification_worker_health(
            self.store, account_key=ACCOUNT_KEY, route=route, now=NOW
        )
        self.assertIn("notification_worker:LEASE_RELEASED", errors)

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
