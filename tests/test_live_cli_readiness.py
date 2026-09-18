from __future__ import annotations

from contextlib import redirect_stderr
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import ModuleType, SimpleNamespace
import io
import copy
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

from titan_brain.live.broker import (
    BrokerMutationBlocked,
    BrokerSide,
    EquityOrderType,
    FakeBrokerClient,
    FillSnapshot,
    MarketHours,
    OrderRequest,
    OrderSnapshot,
    PositionSnapshot,
    TimeInForce,
)
from titan_brain.live.broker.robinhood import RobinhoodBrokerAdapter
from titan_brain.live.cli import (
    ACCOUNT_KEY,
    CommandBlocked,
    InstallLayout,
    _broker_command_lane_blockers,
    _broker_command_lane_report,
    _doctor,
    _ActivationRiskBoundBroker,
    _legacy_retirement_payload,
    _machine_readiness,
    _risk_observation_entry_blockers,
    _probe_legacy_heartbeat,
    _probe_legacy_writer_processes,
    _verified_notification_receipt,
    build_parser,
    command_activate,
    command_doctor,
    command_provider_status,
    command_serve,
)
from titan_brain.live.composition import RuntimeComposition
from titan_brain.live.notification_worker import notification_worker_health
from titan_brain.live.models import BrokerOrderState, Incident, IncidentSeverity
from titan_brain.live.market_data import MarketSessionState
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
from titan_brain.live.risk_evidence_binding import risk_high_water_receipt_hash
from titan_brain.live.scheduler_control import (
    CODEX_SCHEDULER_CONTROL_PLANE_SOURCE,
    CODEX_SCHEDULER_EVIDENCE_SCHEMA,
    REQUIRED_CODEX_AUTOMATION_IDS,
    SchedulerAutomationEvidence,
    SchedulerEvidenceBindings,
    SchedulerRetirementEvidence,
)
from titan_brain.live.state import LiveStateStore, StateConflict, object_hash
from titan_brain.live.writer_lock import (
    AccountWriterLock,
    WriterLockBusy,
    attended_coordinator_lock_key,
)
from tests.live_activation_support import (
    record_flat_reconciliation,
    stage_canonical_activation,
)
from tests.test_live_service import account_snapshot


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 8, 12, 30, tzinfo=timezone.utc)


def readiness_snapshot():
    return replace(
        account_snapshot(),
        observed_at=NOW,
        received_at=NOW,
        risk_evidence_as_of=NOW,
        risk_baseline_identity_hash="4" * 64,
        risk_baseline_receipt_hash="5" * 64,
        risk_high_water_identity_hash="6" * 64,
        risk_high_water_lineage_hash="7" * 64,
        risk_high_water_receipt_hash=risk_high_water_receipt_hash(
            identity_hash="6" * 64,
            baseline_receipt_hash="5" * 64,
            lineage_hash="7" * 64,
            peak_equity=1000,
        ),
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

    def risk_observation_incident(
        self, incident_id: str, *, account_key: str = ACCOUNT_KEY,
        category: str = "IBKR_FINAL_RISK_OBSERVATION_UNRESOLVED",
    ) -> None:
        self.store.record_incident(
            Incident(
                incident_id=incident_id,
                account_key=account_key,
                category=category,
                severity=IncidentSeverity.CRITICAL,
                opened_at=NOW - timedelta(days=1),
                detail={
                    "release_manifest_hash": "f" * 64,
                    "config_hash": "e" * 64,
                    "policy_hash": "d" * 64,
                    "trading_date": "2026-09-04",
                },
            )
        )

    def test_unresolved_risk_observation_is_account_global_across_restart(self) -> None:
        self.risk_observation_incident("other-account", account_key="other-account")
        self.risk_observation_incident("other-category", category="OTHER")
        self.risk_observation_incident("resolved")
        self.store.resolve_incident("resolved", resolved_at=NOW)
        self.assertEqual(
            _risk_observation_entry_blockers(self.store, account_key=ACCOUNT_KEY), ()
        )

        self.risk_observation_incident("older-release-and-trading-day")
        expected = ("IBKR_FINAL_RISK_OBSERVATION_UNRESOLVED",)
        self.assertEqual(
            _risk_observation_entry_blockers(self.store, account_key=ACCOUNT_KEY),
            expected,
        )
        self.store.close()
        self.store = LiveStateStore(self.layout.state_path)
        before = tuple(dict(row) for row in self.store.rows("SELECT * FROM incidents"))
        self.assertEqual(
            _risk_observation_entry_blockers(self.store, account_key=ACCOUNT_KEY),
            expected,
        )
        self.assertEqual(
            tuple(dict(row) for row in self.store.rows("SELECT * FROM incidents")), before
        )

    def test_recovered_readiness_snapshot_never_clears_pending_risk_observation(self) -> None:
        self.risk_observation_incident("missing-final-read")
        evidence = self.collect(
            FakeBrokerClient(initial_snapshot=readiness_snapshot(), clock=lambda: NOW)
        )
        self.assertIn("IBKR_FINAL_RISK_OBSERVATION_UNRESOLVED", evidence.probe_errors)
        self.assertIn("READINESS_PROBE_ERROR", evidence.blockers(self.policy, now=NOW))
        self.assertIsNone(
            self.store.rows(
                "SELECT resolved_at FROM incidents WHERE incident_id=?",
                ("missing-final-read",),
            )[0]["resolved_at"]
        )

    def test_unreadable_risk_observation_state_fails_closed_without_error_details(self) -> None:
        expected = ("IBKR_FINAL_RISK_OBSERVATION_STATE_UNAVAILABLE",)
        with mock.patch.object(
            self.store, "rows", side_effect=sqlite3.DatabaseError("private storage detail")
        ):
            self.assertEqual(
                _risk_observation_entry_blockers(self.store, account_key=ACCOUNT_KEY),
                expected,
            )
        real_rows = self.store.rows

        def unavailable_incidents(statement, parameters=()):
            if "IBKR_FINAL_RISK_OBSERVATION_UNRESOLVED" in statement:
                raise sqlite3.DatabaseError("private storage detail")
            return real_rows(statement, parameters)

        with mock.patch.object(self.store, "rows", side_effect=unavailable_incidents):
            evidence = self.collect(
                FakeBrokerClient(initial_snapshot=readiness_snapshot(), clock=lambda: NOW)
            )
        self.assertIn(expected[0], evidence.probe_errors)
        self.assertNotIn("private storage detail", repr(evidence.to_payload()))
        self.assertIn("READINESS_PROBE_ERROR", evidence.blockers(self.policy, now=NOW))

    def test_serve_keeps_safety_runner_available_with_pending_risk_entry_blocker(self) -> None:
        self.risk_observation_incident("interrupted-before-restart")
        serve_store = LiveStateStore(self.layout.state_path)
        before = dict(self.store.runtime_status())
        composition = RuntimeComposition()
        broker = FakeBrokerClient(initial_snapshot=readiness_snapshot(), clock=lambda: NOW)
        args = SimpleNamespace(
            install_root=str(self.install), once=True, provider_assembly=None,
            runtime_composition=composition,
        )
        with (
            mock.patch.object(InstallLayout, "load_release", return_value=(self.manifest, self.policy)),
            mock.patch("titan_brain.live.cli._open_state", return_value=serve_store),
            mock.patch.object(composition, "bind_release", return_value=()),
            mock.patch.object(composition, "broker_client", return_value=broker),
            mock.patch.object(composition, "build_discovery_executor", return_value=object()),
            mock.patch.object(composition, "control_inbox", return_value=object()),
            mock.patch("titan_brain.live.cli.ProductionLifecycleActions") as lifecycle,
            mock.patch("titan_brain.live.cli.FullLiveService") as service,
            mock.patch("titan_brain.live.cli.ServiceRunner") as runner,
        ):
            runner.return_value.run.return_value = None
            self.assertEqual(command_serve(args), 0)
        self.assertEqual(
            service.call_args.kwargs["entry_path_blockers"],
            ["IBKR_FINAL_RISK_OBSERVATION_UNRESOLVED"],
        )
        self.assertIs(service.call_args.kwargs["actions"], lifecycle.return_value)
        runner.return_value.run.assert_called_once_with(once=True)
        self.assertEqual(dict(self.store.runtime_status()), before)
        self.assertEqual(
            _risk_observation_entry_blockers(self.store, account_key=ACCOUNT_KEY),
            ("IBKR_FINAL_RISK_OBSERVATION_UNRESOLVED",),
        )

    def test_install_layout_uses_one_user_lock_root_across_install_roots(self) -> None:
        other = InstallLayout(Path(self.temporary.name) / "other/full-live")
        self.assertEqual(self.layout.lock_path, self.fixed_lock_directory)
        self.assertEqual(other.lock_path, self.fixed_lock_directory)
        self.assertNotEqual(self.layout.lock_path, self.layout.root.parent)
        self.assertNotEqual(other.lock_path, other.root.parent)

    def test_capabilities_cannot_substitute_for_command_handshake_proof(self) -> None:
        execution = {"broker_adapter": "supported_production_transport"}
        capability_only = RuntimeComposition(
            production_transport=SimpleNamespace(
                session=SimpleNamespace(),
                descriptor=SimpleNamespace(),
            )
        )

        report, error = _broker_command_lane_report(
            capability_only,
            execution,
        )

        self.assertEqual(
            error,
            "broker_command:COMMAND_LANE_PROOF_UNAVAILABLE",
        )
        self.assertEqual(
            _broker_command_lane_blockers(report, required=True),
            (
                "BROKER_COMMAND_LANE_DISCONNECTED",
                "BROKER_COMMAND_NEXT_VALID_ID_MISSING",
                "BROKER_COMMAND_ACCOUNT_UNAUTHENTICATED",
            ),
        )

    def test_authenticated_unarmed_command_handshake_is_ready(self) -> None:
        status = SimpleNamespace(
            command_connected=True,
            next_valid_id_received=True,
            account_authenticated=True,
            write_authority_granted=False,
        )
        composition = RuntimeComposition(
            production_transport=SimpleNamespace(
                session=SimpleNamespace(command_lane_status=lambda: status),
                descriptor=SimpleNamespace(),
            )
        )

        report, error = _broker_command_lane_report(
            composition,
            {"broker_adapter": "supported_production_transport"},
        )

        self.assertIsNone(error)
        self.assertEqual(
            _broker_command_lane_blockers(report, required=True),
            (),
        )

    def test_serve_lock_contention_stops_before_lazy_provider_composition(self) -> None:
        serve_store = LiveStateStore(self.layout.state_path)
        lock_key = attended_coordinator_lock_key(ACCOUNT_KEY)
        holder = AccountWriterLock(
            self.fixed_lock_directory,
            lock_key,
            owner_id="existing-service",
        )
        contender = AccountWriterLock(
            self.fixed_lock_directory,
            lock_key,
            owner_id="contending-service",
        )
        composition_calls: list[None] = []

        def compose():
            composition_calls.append(None)
            return RuntimeComposition()

        args = SimpleNamespace(
            install_root=str(self.install),
            once=True,
            provider_assembly=None,
            runtime_composition=compose,
        )
        holder.acquire(acquired_at=NOW)
        try:
            with (
                mock.patch.object(
                    InstallLayout,
                    "load_release",
                    return_value=(self.manifest, self.policy),
                ),
                mock.patch(
                    "titan_brain.live.cli._open_state",
                    return_value=serve_store,
                ),
                mock.patch(
                    "titan_brain.live.cli._service_process_lock",
                    return_value=contender,
                ),
            ):
                with self.assertRaises(WriterLockBusy):
                    command_serve(args)
        finally:
            holder.release()

        self.assertEqual(composition_calls, [])
        self.assertFalse(contender.held)

    def test_doctor_lock_contention_stops_before_command_graph_composition(self) -> None:
        holder = AccountWriterLock(
            self.fixed_lock_directory,
            ACCOUNT_KEY,
            owner_id="running-coordinator",
        )
        composition_calls: list[None] = []

        def compose():
            composition_calls.append(None)
            return RuntimeComposition()

        args = SimpleNamespace(
            install_root=str(self.install),
            runtime_composition=compose,
        )
        holder.acquire(acquired_at=NOW)
        try:
            with mock.patch.object(
                InstallLayout,
                "load_release",
                return_value=(self.manifest, self.policy),
            ):
                with self.assertRaises(WriterLockBusy):
                    command_doctor(args)
        finally:
            holder.release()
        self.assertEqual(composition_calls, [])

    def test_doctor_fallback_uses_manifest_calendar_for_session_diagnostics(self) -> None:
        class CalendarAwareFallback:
            def __init__(
                inner,
                database_path,
                *,
                session_state,
                **_kwargs,
            ) -> None:
                inner.database_path = str(database_path)
                inner.session_state = session_state

            def health(inner, *, now):
                session = inner.session_state(now)
                entry = session is MarketSessionState.ENTRY_ELIGIBLE
                stale = ("MASSIVE_QUOTE_STALE",) if entry else ()
                return SimpleNamespace(
                    blockers=stale,
                    producer_fresh=False,
                    database_path=inner.database_path,
                    latest_quote_at=None,
                    latest_completed_bar_at=None,
                    component_states={
                        "massive_websocket": "healthy",
                        "market_data_freshness": "healthy",
                    },
                    session_state=session,
                    entry_evidence_ready=False,
                    entry_blockers=(
                        stale
                        if entry
                        else ("WAITING_FOR_SESSION",)
                    ),
                )

        cases = (
            (datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc), "premarket_attended", False),
            (datetime(2026, 9, 8, 13, 30, tzinfo=timezone.utc), "transition", False),
            (datetime(2026, 9, 8, 13, 35, tzinfo=timezone.utc), "regular_entry", True),
            (datetime(2026, 9, 8, 20, 0, tzinfo=timezone.utc), "closed", False),
            (datetime(2026, 9, 7, 14, 0, tzinfo=timezone.utc), "closed", False),
        )
        for observed_at, expected_lane, entry_eligible in cases:
            with self.subTest(lane=expected_lane, observed_at=observed_at):
                with (
                    mock.patch.object(
                        InstallLayout,
                        "load_release",
                        return_value=(self.manifest, self.policy),
                    ),
                    mock.patch(
                        "titan_brain.live.cli.LocalMassiveReadOnlySource",
                        CalendarAwareFallback,
                    ),
                    mock.patch("titan_brain.live.cli._now", return_value=observed_at),
                ):
                    report = _doctor(
                        self.layout,
                        runtime_composition=RuntimeComposition(),
                    )
                market = report["market_data"]
                self.assertEqual(market["calendar_lane"], expected_lane)
                self.assertEqual(
                    market["session_state"],
                    (
                        MarketSessionState.ENTRY_ELIGIBLE.value
                        if entry_eligible
                        else MarketSessionState.WAITING_FOR_SESSION.value
                    ),
                )
                self.assertFalse(market["entry_evidence_ready"])
                if entry_eligible:
                    self.assertIn("MASSIVE_QUOTE_STALE", market["entry_blockers"])
                    self.assertIn("MASSIVE_QUOTE_STALE", report["blockers"])
                else:
                    self.assertEqual(
                        market["entry_blockers"], ["WAITING_FOR_SESSION"]
                    )
                    self.assertNotIn("MASSIVE_QUOTE_STALE", report["blockers"])

    def test_flex_setup_cli_is_lazy_and_accepts_only_reporting_results(self) -> None:
        calls = []
        module = ModuleType("titan_brain.live.ibkr_flex_setup")

        def setup_status(policy):
            calls.append(("status", policy))
            return {"ok": True, "reporting_only": True, "state": "NOT_ENROLLED"}

        def enroll(policy):
            calls.append(("enroll", policy))
            return {"ok": True, "reporting_only": True, "state": "ENROLLED"}

        def probe(policy, *, report_date, install_root):
            calls.append(("probe", policy, report_date, install_root))
            return {"ok": False, "reporting_only": True, "state": "NOT_READY"}

        module.setup_status = setup_status
        module.enroll = enroll
        module.probe = probe
        parser = build_parser()
        for forbidden in ("--token", "--account-id", "--query-id"):
            with (
                self.subTest(forbidden_argument=forbidden),
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                parser.parse_args(
                    [
                        "flex-enroll",
                        "--install-root",
                        str(self.install),
                        forbidden,
                        "private-value",
                    ]
                )
        cases = (
            ("flex-setup-status", (), 0, "status"),
            ("flex-enroll", (), 0, "enroll"),
            ("flex-probe", ("--date", "2026-09-14"), 2, "probe"),
        )
        with (
            mock.patch.dict(
                sys.modules,
                {"titan_brain.live.ibkr_flex_setup": module},
            ),
            mock.patch.object(
                InstallLayout,
                "load_release",
                return_value=(self.manifest, self.policy),
            ),
        ):
            for command_name, extra, expected_code, expected_call in cases:
                with self.subTest(command=command_name):
                    args = parser.parse_args(
                        [command_name, "--install-root", str(self.install), *extra]
                    )
                    output = io.StringIO()
                    with mock.patch("sys.stdout", output):
                        self.assertEqual(args.handler(args), expected_code)
                    payload = json.loads(output.getvalue())
                    self.assertIs(payload["reporting_only"], True)
                    self.assertEqual(calls[-1][0], expected_call)
        self.assertIs(calls[0][1], self.policy)
        self.assertIs(calls[1][1], self.policy)
        self.assertEqual(calls[2][2].isoformat(), "2026-09-14")
        self.assertEqual(calls[2][3], self.layout.root)

        invalid = parser.parse_args(
            ["flex-probe", "--install-root", str(self.install), "--date", "bad"]
        )
        with mock.patch.object(
            InstallLayout,
            "load_release",
            side_effect=AssertionError("invalid date must stop before release access"),
        ):
            with self.assertRaisesRegex(CommandBlocked, "REPORT_DATE_INVALID"):
                invalid.handler(invalid)

        invalid_report = parser.parse_args(
            ["flex-setup-status", "--install-root", str(self.install)]
        )
        module.setup_status = lambda _policy: {
            "ok": True,
            "reporting_only": False,
        }
        with (
            mock.patch.dict(
                sys.modules,
                {"titan_brain.live.ibkr_flex_setup": module},
            ),
            mock.patch.object(
                InstallLayout,
                "load_release",
                return_value=(self.manifest, self.policy),
            ),
        ):
            with self.assertRaisesRegex(CommandBlocked, "SETUP_REPORT_INVALID"):
                invalid_report.handler(invalid_report)

    def test_network_provider_probe_cannot_collide_with_coordinator(self) -> None:
        holder = AccountWriterLock(
            self.fixed_lock_directory,
            ACCOUNT_KEY,
            owner_id="running-coordinator",
        )
        reports: list[bool] = []
        args = SimpleNamespace(
            install_root=str(self.install),
            probe_network=True,
            provider_assembly=SimpleNamespace(
                connection_report=lambda *, probe_network: reports.append(
                    probe_network
                )
            ),
        )
        holder.acquire(acquired_at=NOW)
        try:
            with mock.patch.object(
                InstallLayout,
                "load_release",
                return_value=(self.manifest, self.policy),
            ):
                with self.assertRaises(WriterLockBusy):
                    command_provider_status(args)
        finally:
            holder.release()
        self.assertEqual(reports, [])

    def test_activated_risk_binding_strips_only_entry_authority(self) -> None:
        record, _owner = stage_canonical_activation(
            self.store,
            created_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )
        prepared = replace(
            record.readiness_evidence,
            risk_high_water_peak_equity="1200",
            risk_high_water_receipt_hash=risk_high_water_receipt_hash(
                identity_hash=str(
                    record.readiness_evidence.risk_high_water_identity_hash
                ),
                baseline_receipt_hash=str(
                    record.readiness_evidence.risk_baseline_receipt_hash
                ),
                lineage_hash=str(
                    record.readiness_evidence.risk_high_water_lineage_hash
                ),
                peak_equity=1200,
            ),
        )
        replacement = readiness_snapshot()
        cancel_receipt = object()
        inner = SimpleNamespace(
            capabilities=object(),
            bind_entry_risk_activation=lambda **_kwargs: None,
            get_account_snapshot=lambda _masked: replacement,
            cancel_equity_order=lambda *_args, **_kwargs: cancel_receipt,
        )
        guarded = _ActivationRiskBoundBroker(inner, prepared)

        degraded = guarded.get_account_snapshot("••••7153")

        self.assertTrue(degraded.whole_broker_reconciled)
        self.assertEqual(degraded.funds.total_value, Decimal("1000.00"))
        self.assertFalse(degraded.entry_risk_evidence_ready)
        self.assertIsNone(degraded.peak_equity)
        self.assertIs(
            guarded.cancel_equity_order("••••7153", "order-1"),
            cancel_receipt,
        )

    def test_activated_risk_binding_guards_entry_review_and_place_only(self) -> None:
        record, _owner = stage_canonical_activation(
            self.store,
            created_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )
        prepared = replace(
            record.readiness_evidence,
            risk_high_water_peak_equity="1200",
            risk_high_water_receipt_hash=risk_high_water_receipt_hash(
                identity_hash=str(
                    record.readiness_evidence.risk_high_water_identity_hash
                ),
                baseline_receipt_hash=str(
                    record.readiness_evidence.risk_baseline_receipt_hash
                ),
                lineage_hash=str(
                    record.readiness_evidence.risk_high_water_lineage_hash
                ),
                peak_equity=1200,
            ),
        )
        valid = replace(
            readiness_snapshot(),
            peak_equity=Decimal("1200"),
            risk_high_water_receipt_hash=risk_high_water_receipt_hash(
                identity_hash="6" * 64,
                baseline_receipt_hash="5" * 64,
                lineage_hash="7" * 64,
                peak_equity=1200,
            ),
        )
        # A recreated ledger can be internally self-consistent while still
        # being unrelated to the lineage consumed at activation.
        replacement = replace(
            readiness_snapshot(),
            risk_high_water_identity_hash="8" * 64,
            risk_high_water_lineage_hash="9" * 64,
            risk_high_water_receipt_hash=risk_high_water_receipt_hash(
                identity_hash="8" * 64,
                baseline_receipt_hash="5" * 64,
                lineage_hash="9" * 64,
                peak_equity=1000,
            ),
        )
        advanced = replace(
            valid,
            peak_equity=Decimal("1300"),
            risk_high_water_receipt_hash=risk_high_water_receipt_hash(
                identity_hash="6" * 64,
                baseline_receipt_hash="5" * 64,
                lineage_hash="7" * 64,
                peak_equity=1300,
            ),
        )
        rolled_back = replace(
            valid,
            peak_equity=Decimal("1250"),
            risk_high_water_receipt_hash=risk_high_water_receipt_hash(
                identity_hash="6" * 64,
                baseline_receipt_hash="5" * 64,
                lineage_hash="7" * 64,
                peak_equity=1250,
            ),
        )
        current = [valid]
        calls: list[tuple[str, object]] = []
        activation_bindings: list[dict[str, object]] = []
        entry_review = object()
        exit_review = object()
        exit_result = object()
        cancel_result = object()

        def review(request):
            calls.append(("review", request.side))
            return entry_review if request.side is BrokerSide.BUY else exit_review

        def place(request, *, review, explicit_confirmation=None):
            calls.append(("place", request.side))
            return exit_result

        inner = SimpleNamespace(
            capabilities=object(),
            bind_entry_risk_activation=lambda **kwargs: activation_bindings.append(
                kwargs
            ),
            get_account_snapshot=lambda _masked: current[0],
            lookup_equity_orders_by_client_ref=lambda *_args: (),
            review_equity_order=review,
            place_equity_order=place,
            cancel_equity_order=lambda *_args, **_kwargs: cancel_result,
        )
        guarded = _ActivationRiskBoundBroker(inner, prepared)
        self.assertEqual(
            activation_bindings,
            [
                {
                    "lineage_hash": "7" * 64,
                    "minimum_peak": Decimal("1200"),
                }
            ],
        )
        entry = OrderRequest(
            account_masked="••••7153",
            symbol="XYZ",
            side=BrokerSide.BUY,
            order_type=EquityOrderType.LIMIT,
            quantity=1,
            market_hours=MarketHours.REGULAR,
            time_in_force=TimeInForce.GFD,
            client_ref_id="00000000-0000-4000-8000-000000000001",
            limit_price=Decimal("10"),
        )
        exit_request = replace(
            entry,
            side=BrokerSide.SELL,
            client_ref_id="00000000-0000-4000-8000-000000000002",
        )

        self.assertIs(guarded.review_equity_order(entry), entry_review)
        current[0] = replacement
        with self.assertRaisesRegex(
            BrokerMutationBlocked,
            "ACTIVATED_IBKR_ENTRY_RISK_EVIDENCE_CHANGED",
        ):
            guarded.place_equity_order(entry, review=entry_review)
        with self.assertRaisesRegex(
            BrokerMutationBlocked,
            "ACTIVATED_IBKR_ENTRY_RISK_EVIDENCE_CHANGED",
        ):
            guarded.review_equity_order(entry)

        current[0] = advanced
        self.assertEqual(
            guarded.get_account_snapshot("••••7153").peak_equity,
            Decimal("1300"),
        )
        current[0] = rolled_back
        with self.assertRaisesRegex(
            BrokerMutationBlocked,
            "ACTIVATED_IBKR_ENTRY_RISK_EVIDENCE_CHANGED",
        ):
            guarded.review_equity_order(entry)

        # Sell-side protection/exit and cancellation stay available despite
        # the entry-only ledger fault.
        current[0] = replacement
        self.assertIs(guarded.review_equity_order(exit_request), exit_review)
        self.assertIs(
            guarded.place_equity_order(exit_request, review=exit_review),
            exit_result,
        )
        self.assertIs(
            guarded.cancel_equity_order("••••7153", "order-1"),
            cancel_result,
        )
        self.assertEqual(
            calls,
            [
                ("review", BrokerSide.BUY),
                ("review", BrokerSide.SELL),
                ("place", BrokerSide.SELL),
            ],
        )

    def test_serve_lease_failure_releases_kernel_before_provider_composition(self) -> None:
        serve_store = LiveStateStore(self.layout.state_path)
        lock = AccountWriterLock(
            self.fixed_lock_directory,
            attended_coordinator_lock_key(ACCOUNT_KEY),
            owner_id="lease-failure-service",
        )
        composition_calls: list[None] = []

        def compose():
            composition_calls.append(None)
            return RuntimeComposition()

        args = SimpleNamespace(
            install_root=str(self.install),
            once=True,
            provider_assembly=None,
            runtime_composition=compose,
        )
        with (
            mock.patch.object(
                InstallLayout,
                "load_release",
                return_value=(self.manifest, self.policy),
            ),
            mock.patch(
                "titan_brain.live.cli._open_state",
                return_value=serve_store,
            ),
            mock.patch(
                "titan_brain.live.cli._service_process_lock",
                return_value=lock,
            ),
            mock.patch.object(
                serve_store,
                "acquire_writer_lease",
                side_effect=StateConflict("synthetic active writer lease"),
            ),
        ):
            with self.assertRaises(StateConflict):
                command_serve(args)

        self.assertEqual(composition_calls, [])
        self.assertFalse(lock.held)

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
        statuses: tuple[str, str] = ("PAUSED", "DISABLED"),
        active_counts: tuple[int, int] = (0, 0),
    ) -> SchedulerRetirementEvidence:
        _, _, config_hash, disabled, error = (
            _probe_legacy_heartbeat(legacy_path)
        )
        self.assertTrue(disabled)
        self.assertIsNone(error)
        self.assertIsNotNone(config_hash)
        return SchedulerRetirementEvidence(
            schema_version=CODEX_SCHEDULER_EVIDENCE_SCHEMA,
            bindings=SchedulerEvidenceBindings(
                release_manifest_hash=str(self.manifest["release_manifest_hash"]),
                config_hash=self.policy.config_hash,
                policy_hash=self.policy.policy_hash,
                runtime_id=self.policy.runtime_id,
                account_key=ACCOUNT_KEY,
            ),
            issued_at=NOW,
            expires_at=NOW + timedelta(seconds=10),
            automations=tuple(
                SchedulerAutomationEvidence(
                    automation_id=automation_id,
                    scheduler_runtime_id=f"{runtime_id}-{index + 1}",
                    status=statuses[index],
                    config_hash=(
                        str(config_hash) if index == 0 else "8" * 64
                    ),
                    active_execution_count=active_counts[index],
                    observed_at=NOW,
                    query_receipt_hash=(
                        query_receipt_hash if index == 0 else "7" * 64
                    ),
                    source=CODEX_SCHEDULER_CONTROL_PLANE_SOURCE,
                )
                for index, automation_id in enumerate(
                    REQUIRED_CODEX_AUTOMATION_IDS
                )
            ),
            signed_evidence_hash="6" * 64,
        )

    def collect(
        self,
        broker,
        *,
        clock: MutableProbeClock | None = None,
        legacy_path: Path | None = None,
        legacy_process_listing: str = "",
        legacy_scheduler_runtime_evidence: SchedulerRetirementEvidence | None = None,
        persist_fresh_broker_read: bool = True,
        policy: PolicyBundle | None = None,
        manifest: dict | None = None,
        runtime_composition: RuntimeComposition | None = None,
        coordinator_runtime_composition: RuntimeComposition | None = None,
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
                    coordinator_runtime_composition=(
                        coordinator_runtime_composition
                    ),
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
        scheduler_runtime: SchedulerRetirementEvidence,
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
                payload = _legacy_retirement_payload(
                    manifest=self.manifest,
                    policy=self.policy,
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

    def test_current_risk_failure_cannot_reuse_prepared_durable_reconciliation(self) -> None:
        prepared_snapshot = readiness_snapshot()
        self.collect(
            FakeBrokerClient(initial_snapshot=prepared_snapshot, clock=lambda: NOW)
        )
        raw_after_high_water_failure = replace(
            prepared_snapshot,
            weekly_realized_pnl=None,
            peak_equity=None,
            weekly_realized_pnl_complete=False,
            peak_equity_complete=False,
            risk_baseline_identity_hash=None,
            risk_baseline_receipt_hash=None,
            risk_high_water_identity_hash=None,
            risk_high_water_lineage_hash=None,
            risk_high_water_receipt_hash=None,
        )

        current = self.collect(
            FakeBrokerClient(
                initial_snapshot=raw_after_high_water_failure,
                clock=lambda: NOW,
            ),
            persist_fresh_broker_read=False,
        )

        self.assertTrue(current.realized_pnl_reconciled)
        self.assertEqual(current.reconciliation_blocker_count, 0)
        self.assertFalse(current.entry_risk_evidence_ready)
        self.assertFalse(current.weekly_realized_pnl_complete)
        self.assertFalse(current.peak_equity_complete)
        self.assertIsNone(current.risk_high_water_receipt_hash)
        blockers = current.blockers(self.policy, now=NOW)
        self.assertIn("ENTRY_RISK_EVIDENCE_UNAVAILABLE", blockers)
        self.assertIn("RISK_HIGH_WATER_RECEIPT_MISSING", blockers)

    def test_activate_high_water_failure_keeps_authority_disabled(self) -> None:
        record, owner = stage_canonical_activation(
            self.store,
            created_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )
        self.store.release_writer_lease(
            account_key=ACCOUNT_KEY,
            owner_id=owner,
            released_at=NOW + timedelta(milliseconds=100),
        )
        activated_at = NOW + timedelta(seconds=1)
        current_after_high_water_failure = replace(
            record.readiness_evidence,
            collected_at=activated_at,
            broker_snapshot_received_at=activated_at,
            probe_started_at=activated_at - timedelta(milliseconds=100),
            probe_completed_at=activated_at,
            probe_elapsed_monotonic_seconds=0.1,
            risk_evidence_as_of=activated_at,
            risk_evidence_age_seconds=0,
            entry_risk_evidence_ready=False,
            weekly_realized_pnl_complete=False,
            peak_equity_complete=False,
            risk_baseline_identity_hash=None,
            risk_baseline_receipt_hash=None,
            risk_high_water_identity_hash=None,
            risk_high_water_lineage_hash=None,
            risk_high_water_peak_equity=None,
            risk_high_water_receipt_hash=None,
        )
        config = copy.deepcopy(self.policy.config)
        config["execution"].update(
            {
                "broker_adapter": "supported_production_transport",
                "execution_authority_mode": "unattended",
                "production_account_binding_fingerprint": "a" * 64,
                "production_authorization_binding_id": "b" * 64,
            }
        )
        activation_ready_policy = SimpleNamespace(
            config=config,
            config_hash=self.policy.config_hash,
            policy_hash=self.policy.policy_hash,
            runtime_id=self.policy.runtime_id,
            account_key=ACCOUNT_KEY,
            account_last4=self.policy.account_last4,
            execution_authority_mode="unattended",
            activation_blockers=(),
            require_activation_ready=lambda: None,
        )
        args = SimpleNamespace(
            install_root=str(self.install),
            activation_id=record.activation_id,
            confirm=f"ACTIVATE FULL LIVE {ACCOUNT_KEY} {record.activation_id}",
            runtime_composition=RuntimeComposition(),
            scheduler_control_plane=object(),
        )

        with (
            mock.patch.object(
                InstallLayout,
                "load_release",
                return_value=(self.manifest, activation_ready_policy),
            ),
            mock.patch(
                "titan_brain.live.cli._machine_readiness",
                return_value=current_after_high_water_failure,
            ) as probe,
            mock.patch(
                "titan_brain.live.cli._now", return_value=activated_at
            ),
            self.assertRaisesRegex(
                ValueError,
                "ACTIVATION_CURRENT_READINESS_BLOCKED:.*RISK_HIGH_WATER_RECEIPT_MISSING",
            ),
        ):
            command_activate(args)

        self.assertFalse(probe.call_args.kwargs["persist_fresh_broker_read"])
        status = self.store.runtime_status()
        self.assertEqual(status["mode"], "PAUSED")
        self.assertEqual(status["authority_enabled"], 0)
        activation = self.store.rows(
            "SELECT consumed_at FROM activation_records WHERE activation_id=?",
            (record.activation_id,),
        )[0]
        self.assertIsNone(activation["consumed_at"])

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

    def test_readiness_requires_both_automations_retired_and_zero_executions(self) -> None:
        legacy = self.paused_legacy_path()
        broker = FakeBrokerClient(
            initial_snapshot=readiness_snapshot(), clock=lambda: NOW
        )
        active = self.collect(
            broker,
            legacy_scheduler_runtime_evidence=self.scheduler_runtime(
                legacy,
                statuses=("PAUSED", "ACTIVE"),
            ),
        )
        self.assertFalse(active.old_writer_disabled)
        self.assertIn(
            "legacy_retirement:SCHEDULER_NOT_DISABLED:"
            "robinhood-titan-premarket-deep-dive",
            active.probe_errors,
        )
        self.assertIn(
            "OLD_ACCOUNT_WRITER_STILL_ENABLED",
            active.blockers(self.policy, now=NOW),
        )

        running = self.collect(
            broker,
            legacy_scheduler_runtime_evidence=self.scheduler_runtime(
                legacy,
                active_counts=(0, 1),
            ),
        )
        self.assertFalse(running.old_writer_disabled)
        self.assertIn(
            "legacy_retirement:SCHEDULER_ACTIVE_EXECUTIONS:"
            "robinhood-titan-premarket-deep-dive",
            running.probe_errors,
        )
        self.assertIn(
            "OLD_ACCOUNT_WRITER_STILL_ENABLED",
            running.blockers(self.policy, now=NOW),
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

    def test_machine_readiness_persists_full_and_coordinator_profiles_separately(self) -> None:
        transport = object()

        class FixedProfileComposition(RuntimeComposition):
            def __init__(self, profile_hash, *, providers):
                super().__init__(
                    production_transport=transport,
                    discovery_provider=(object() if providers else None),
                    notification_provider=(object() if providers else None),
                )
                self.profile_hash = profile_hash

            def bind_release(self, manifest, *, release_root):
                return ()

            @property
            def component_provenance_hash(self):
                return self.profile_hash

            def tradability_ready(self, discovery_config, *, now):
                return True

        full_hash = "d" * 64
        coordinator_hash = "e" * 64
        full = FixedProfileComposition(full_hash, providers=True)
        coordinator = FixedProfileComposition(coordinator_hash, providers=False)
        broker = FakeBrokerClient(
            initial_snapshot=readiness_snapshot(), clock=lambda: NOW
        )
        broker.descriptor = SimpleNamespace(
            account_binding_fingerprint="a" * 64,
            authorization_binding_id="b" * 64,
        )

        evidence = self.collect(
            broker,
            runtime_composition=full,
            coordinator_runtime_composition=coordinator,
        )

        self.assertEqual(evidence.component_provenance_hash, full_hash)
        self.assertEqual(
            evidence.coordinator_component_provenance_hash, coordinator_hash
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
