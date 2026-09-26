"""Offline probe/launcher tests. All runtime and subprocess entry points mocked."""

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
import logging
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from titan_brain.live import session_input_probe as probe


ROOT = Path(__file__).resolve().parents[1]
SECRET = "synthetic-private-sdk-account-and-token-payload"
SCHEMA = "titan_session_input_probe_2026-09-18_v1"


def public_report(**changes):
    result = {
        "schema_version": SCHEMA, "diagnostic_only": True,
        "checked_at": "2026-09-18T14:00:00+00:00", "model": probe.MODEL,
        "mode": "pre_entry_candidate", "policy_binding_verified": True,
        "measurement_policy_sha256": "a" * 64, "owner_amendment_sha256": "b" * 64,
        "installed_release_manifest_sha256": "c" * 64,
        "live_authority": False, "baseline_frozen": False,
        "production_policy_changed": False, "collection_completed": True,
        "cleanup_completed": True,
        "command_connected_before_cleanup": False,
        "read_connected_after_cleanup": False, "command_connected_after_cleanup": False,
        "inputs": {
            "first_collection_id": "d" * 64, "second_collection_id": "e" * 64,
            "observed_at": "2026-09-18T14:00:00+00:00",
            "starting_balance_candidate_present": False,
            "observation_preconditions_met": False,
            "blockers": ["ALL_CLIENT_EXECUTION_COVERAGE_UNPROVEN"],
            "baseline_authority": False, "baseline_frozen": False,
            "diagnostic_only": True, "live_authority": False,
        },
    }
    result.update(changes)
    return result


class SessionInputProbeTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.install = Path(directory).resolve()
        (self.install / "current").mkdir()
        self.source = self.install / "source"
        self.source.mkdir()
        self.runtime = Mock(spec_set=("connect_reads", "status", "stop"))
        self.flags = {"read_connected": False, "command_connected": False, "runtime_error_code": None}
        self.runtime.status.side_effect = lambda: SimpleNamespace(**self.flags)
        self.runtime.connect_reads.side_effect = lambda: self.flags.update(read_connected=True)
        self.runtime.stop.side_effect = lambda: self.flags.update(read_connected=False, command_connected=False)
        self.observation = Mock(spec_set=("public_dict",))
        self.observation.public_dict.return_value = {
            "diagnostic_only": True, "live_authority": False,
            "baseline_authority": False, "baseline_frozen": False,
            "observation_preconditions_met": False,
            "starting_balance_candidate_present": False,
            "blockers": ("ALL_CLIENT_EXECUTION_COVERAGE_UNPROVEN",),
        }
        self.adapter = Mock(spec_set=("capture", "capture_pre_entry_candidate"))
        self.adapter.capture.return_value = self.observation
        self.adapter.capture_pre_entry_candidate.return_value = self.observation
        self.policy_loader = self.stack.enter_context(patch.object(
            probe, "load_session_trading_policy_from_root",
            return_value=SimpleNamespace(policy_sha256="a" * 64, amendment_sha256="b" * 64)))
        self.manifest_loader = self.stack.enter_context(patch(
            "titan_brain.live.release.load_release_manifest", return_value={
                "config_path": "config/full_live_ibkr.json", "release_manifest_hash": "c" * 64}))
        self.installed_policy = SimpleNamespace(config={"synthetic": "installed-config"})
        self.bundle_loader = self.stack.enter_context(patch(
            "titan_brain.live.policy.PolicyBundle.load", return_value=self.installed_policy))
        self.profile = object()
        self.profile_factory = self.stack.enter_context(patch(
            "titan_brain.live.provider_profile.IbkrLocalProviderProfile.from_config", return_value=self.profile))
        self.runtime_factory = self.stack.enter_context(patch(
            "titan_brain.live.broker.ibkr_runtime.build_ibkr_official_runtime", return_value=self.runtime))
        self.adapter_factory = self.stack.enter_context(patch(
            "titan_brain.live.broker.ibkr_session_inputs.IbkrSessionInputAdapter", return_value=self.adapter))
        self.stack.enter_context(patch("socket.socket.connect", side_effect=AssertionError("network forbidden")))

    def run_probe(self, **changes):
        return probe.run_probe(source_root=self.source, install_root=self.install, **changes)

    def assert_non_authorizing(self, report):
        self.assertTrue(report["diagnostic_only"])
        for field in ("live_authority", "baseline_frozen", "production_policy_changed"):
            self.assertIs(report[field], False)
        self.assertNotIn(SECRET, repr(report))

    def test_default_double_read_candidate_is_not_readiness_or_baseline(self):
        report = self.run_probe()
        self.assert_non_authorizing(report)
        self.assertTrue(report["policy_binding_verified"])
        self.assertTrue(report["collection_completed"])
        self.assertTrue(report["cleanup_completed"])
        self.assertEqual(report["mode"], "pre_entry_candidate")
        self.assertFalse(report["inputs"]["observation_preconditions_met"])
        self.assertFalse(report["inputs"]["baseline_authority"])
        self.assertTrue(report["inputs"]["blockers"])
        self.adapter.capture_pre_entry_candidate.assert_called_once_with()
        self.adapter.capture.assert_not_called()
        self.runtime.connect_reads.assert_called_once_with()
        self.runtime.stop.assert_called_once_with()
        self.runtime_factory.assert_called_once_with(profile=self.profile, install_root=self.install)
        self.manifest_loader.assert_called_once_with(
            self.install / "release-manifest.json", verify_files_root=self.install / "current")
        self.bundle_loader.assert_called_once_with(
            self.install / "current", config_relative="config/full_live_ibkr.json")

    def test_single_read_uses_only_capture(self):
        report = self.run_probe(single_read=True)
        self.assert_non_authorizing(report)
        self.assertEqual(report["mode"], "single_read")
        self.adapter.capture.assert_called_once_with()
        self.adapter.capture_pre_entry_candidate.assert_not_called()

    def test_sdk_stdout_stderr_and_logging_are_suppressed_and_logging_restored(self):
        prior = logging.root.manager.disable
        self.addCleanup(logging.disable, prior)
        logging.disable(logging.WARNING)
        observed_disable = []

        def noisy_capture():
            observed_disable.append(logging.root.manager.disable)
            print(SECRET)
            print(SECRET, file=sys.stderr)
            logging.getLogger("synthetic-sdk").critical(SECRET)
            return self.observation

        self.adapter.capture_pre_entry_candidate.side_effect = noisy_capture
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            report = self.run_probe()
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(observed_disable, [sys.maxsize])
        self.assertEqual(logging.root.manager.disable, logging.WARNING)
        self.assert_non_authorizing(report)

    def test_policy_failure_never_loads_installed_profile_or_connects(self):
        self.policy_loader.side_effect = RuntimeError(SECRET)
        report = self.run_probe()
        self.assert_non_authorizing(report)
        self.assertFalse(report["policy_binding_verified"])
        self.assertFalse(report["collection_completed"])
        self.assertEqual(report["failure_phase"], "policy_binding")
        self.assertEqual(report["error_code"], "SESSION_INPUT_PROBE_FAILED")
        self.manifest_loader.assert_not_called()
        self.runtime_factory.assert_not_called()
        self.runtime.stop.assert_not_called()

    def test_invalid_installed_release_or_missing_profile_never_connects(self):
        for target in ("manifest", "profile"):
            with self.subTest(target=target):
                self.manifest_loader.side_effect = RuntimeError(SECRET) if target == "manifest" else None
                self.profile_factory.return_value = None if target == "profile" else self.profile
                report = self.run_probe()
                self.assert_non_authorizing(report)
                self.assertEqual(report["failure_phase"], "installed_profile")
                self.runtime_factory.assert_not_called()

    def test_connect_failure_still_attempts_cleanup_without_private_error(self):
        self.runtime.connect_reads.side_effect = RuntimeError(SECRET)
        report = self.run_probe()
        self.assert_non_authorizing(report)
        self.assertEqual(report["failure_phase"], "read_connection")
        self.assertFalse(report["collection_completed"])
        self.assertTrue(report["cleanup_completed"])
        self.runtime.stop.assert_called_once_with()
        self.adapter_factory.assert_not_called()

    def test_diagnostic_store_rejects_existing_relative_and_production_paths_before_connect(self):
        existing = self.install / "already-exists.sqlite3"
        existing.touch()
        for path in (existing, Path("relative.sqlite3"), self.install / "state" / "new.sqlite3"):
            with self.subTest(path=path):
                report = self.run_probe(diagnostic_session_store=path)
                self.assertFalse(report["collection_completed"])
                self.runtime_factory.assert_not_called()

    def test_persisted_failed_read_is_not_reported_as_completed_collection(self):
        target = self.install.parent / (self.install.name + "-new-diagnostic.sqlite3")
        result = SimpleNamespace(public_dict=lambda: {
            "diagnostic_only": True, "live_authority": False, "baseline_authority": False,
            "baseline_frozen": False, "full_session_pnl_established": False,
            "observation_failed": True, "diagnostic_risk_state_recorded": True,
        })
        with patch("titan_brain.live.session_trading_rehearsal.record_fresh_session_rehearsal", return_value=result) as rehearsal:
            report = self.run_probe(diagnostic_session_store=target)
        self.assertFalse(report["collection_completed"])
        self.assertTrue(report["cleanup_completed"])
        self.assertEqual(report["error_code"], "SESSION_INPUT_PROBE_FAILED")
        self.assertTrue(report["inputs"]["diagnostic_risk_state_recorded"])
        self.assertEqual(report["mode"], "persisted_session_rehearsal")
        rehearsal.assert_called_once()
        self.adapter.capture.assert_not_called()
        self.adapter.capture_pre_entry_candidate.assert_not_called()

    def test_successful_diagnostic_rehearsal_keeps_production_baseline_false(self):
        target = self.install.parent / (self.install.name + "-new-diagnostic.sqlite3")
        result = SimpleNamespace(public_dict=lambda: {
            "diagnostic_only": True, "live_authority": False, "baseline_authority": False,
            "baseline_frozen": False, "full_session_pnl_established": False,
            "observation_failed": False, "diagnostic_risk_state_recorded": True,
        })
        with patch("titan_brain.live.session_trading_rehearsal.record_fresh_session_rehearsal", return_value=result):
            report = self.run_probe(diagnostic_session_store=target)
        self.assert_non_authorizing(report)
        self.assertTrue(report["collection_completed"])
        self.assertTrue(report["cleanup_completed"])

    def test_unexpected_command_connection_blocks_capture_and_is_cleaned_up(self):
        self.runtime.connect_reads.side_effect = lambda: self.flags.update(read_connected=True, command_connected=True)
        report = self.run_probe()
        self.assert_non_authorizing(report)
        self.assertFalse(report["collection_completed"])
        self.assertTrue(report["command_connected_before_cleanup"])
        self.assertFalse(report["command_connected_after_cleanup"])
        self.runtime.stop.assert_called_once_with()
        self.adapter_factory.assert_not_called()

    def test_capture_and_public_serialization_failures_always_stop(self):
        for target in ("capture", "serialize"):
            with self.subTest(target=target):
                self.runtime.stop.reset_mock()
                self.adapter.capture_pre_entry_candidate.side_effect = RuntimeError(SECRET) if target == "capture" else None
                self.observation.public_dict.side_effect = RuntimeError(SECRET) if target == "serialize" else None
                report = self.run_probe()
                self.assert_non_authorizing(report)
                self.assertEqual(report["failure_phase"], "finite_input_collection")
                self.assertFalse(report["collection_completed"])
                self.runtime.stop.assert_called_once_with()

    def test_status_failure_does_not_prevent_stop_or_restore_private_logs(self):
        prior = logging.root.manager.disable
        self.runtime.status.side_effect = RuntimeError(SECRET)
        report = self.run_probe()
        self.assert_non_authorizing(report)
        self.assertEqual(report["runtime_error_code"], "SESSION_INPUT_STATUS_UNAVAILABLE")
        self.assertEqual(report["error_code"], "SESSION_INPUT_PROBE_CLEANUP_FAILED")
        self.assertFalse(report["cleanup_completed"])
        self.runtime.stop.assert_called_once_with()
        self.assertEqual(logging.root.manager.disable, prior)

    def test_pre_cleanup_status_failure_still_stops_and_verifies_cleanup(self):
        calls = []

        def status():
            calls.append(None)
            if len(calls) == 2:
                raise RuntimeError(SECRET)
            return SimpleNamespace(**self.flags)

        self.runtime.status.side_effect = status
        report = self.run_probe()
        self.assert_non_authorizing(report)
        self.assertIsNone(report["command_connected_before_cleanup"])
        self.assertTrue(report["cleanup_completed"])
        self.runtime.stop.assert_called_once_with()

    def test_stop_failure_is_fixed_code_not_claimed_cleanup(self):
        self.runtime.stop.side_effect = RuntimeError(SECRET)
        report = self.run_probe()
        self.assert_non_authorizing(report)
        self.assertTrue(report["collection_completed"])
        self.assertFalse(report["cleanup_completed"])
        self.assertEqual(report["error_code"], "SESSION_INPUT_PROBE_CLEANUP_FAILED")

    def test_connected_after_stop_is_not_successful_cleanup(self):
        self.runtime.stop.side_effect = lambda: None
        report = self.run_probe()
        self.assert_non_authorizing(report)
        self.assertTrue(report["read_connected_after_cleanup"])
        self.assertFalse(report["cleanup_completed"])

    def test_main_exit_code_reflects_collection_and_cleanup_only(self):
        for collection, cleanup, expected in ((True, True, 0), (True, False, 2), (False, True, 2)):
            with self.subTest(collection=collection, cleanup=cleanup):
                report = public_report(collection_completed=collection, cleanup_completed=cleanup)
                output = io.StringIO()
                with patch.object(probe, "run_probe", return_value=report) as call, redirect_stdout(output):
                    code = probe.main(source_root=self.source, argv=["--install-root", str(self.install), "--single-read"])
                self.assertEqual(code, expected)
                self.assertEqual(json.loads(output.getvalue()), report)
                call.assert_called_once_with(source_root=self.source, install_root=self.install, single_read=True)


class SessionProbeWatchdogTests(unittest.TestCase):
    def run_child(self, *, report=None, stdout=None, returncode=0, error=None):
        completed = SimpleNamespace(
            stdout=json.dumps(public_report() if report is None else report) if stdout is None else stdout,
            stderr=SECRET, returncode=returncode)
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(probe.subprocess, "run", return_value=completed, side_effect=error) as child, redirect_stdout(output), redirect_stderr(errors):
            code = probe.run_isolated_probe(source_root=ROOT, argv=["--install-root", "/synthetic-install"], timeout_seconds=45)
        self.assertNotIn(SECRET, output.getvalue())
        self.assertEqual(errors.getvalue(), "")
        return code, json.loads(output.getvalue()), child

    def test_child_isolated_flags_capture_and_hard_timeout_are_required(self):
        code, report, child = self.run_child()
        self.assertEqual(code, 0)
        self.assertEqual(report, public_report())
        args, kwargs = child.call_args
        self.assertEqual(args[0][:5], [sys.executable, "-I", "-S", "-B", "-c"])
        self.assertEqual(args[0][-3:], [str(ROOT), "--install-root", "/synthetic-install"])
        self.assertEqual(kwargs, {"capture_output": True, "text": True, "input": "", "timeout": 45, "check": False})

    def test_timeout_discards_partial_output_and_does_not_claim_graceful_cleanup(self):
        error = subprocess.TimeoutExpired(["synthetic"], 45, output=SECRET, stderr=SECRET)
        code, report, _ = self.run_child(error=error)
        self.assertEqual(code, 2)
        self.assertEqual(report["error_code"], "SESSION_INPUT_PROBE_WORKER_TIMEOUT")
        self.assertFalse(report["cleanup_completed"])
        self.assertFalse(report["collection_completed"])
        self.assertFalse(report["live_authority"])

    def test_malformed_nonzero_and_spawn_errors_never_forward_private_payload(self):
        for changes in ({"stdout": SECRET}, {"stdout": "[]"}, {"returncode": 1},
                        {"returncode": -9}, {"error": OSError(SECRET)}):
            with self.subTest(changes=changes):
                code, report, _ = self.run_child(**changes)
                self.assertEqual(code, 2)
                self.assertEqual(report["error_code"], "SESSION_INPUT_PROBE_WORKER_FAILED")

    def test_false_authority_markers_cannot_be_omitted_changed_or_coerced(self):
        for field in ("live_authority", "baseline_frozen", "production_policy_changed"):
            for value in (True, 0, None, "false"):
                with self.subTest(field=field, value=value):
                    code, report, _ = self.run_child(report=public_report(**{field: value}))
                    self.assertEqual(code, 2)
                    self.assertEqual(report["error_code"], "SESSION_INPUT_PROBE_WORKER_FAILED")
        report = public_report()
        del report["live_authority"]
        self.assertEqual(self.run_child(report=report)[0], 2)

    def test_legitimate_blocked_worker_report_is_retained_without_stderr(self):
        report = public_report(collection_completed=False, error_code="SESSION_INPUT_PROBE_FAILED")
        code, retained, _ = self.run_child(report=report, returncode=2)
        self.assertEqual(code, 2)
        self.assertEqual(retained, report)

    def test_failed_rehearsal_read_cannot_claim_success_through_parent(self):
        report = public_report(mode="persisted_session_rehearsal")
        report["inputs"]["observation_failed"] = True
        code, result, _ = self.run_child(report=report)
        self.assertEqual(code, 2)
        self.assertEqual(result["error_code"], "SESSION_INPUT_PROBE_WORKER_FAILED")
        report["collection_completed"] = False
        report["error_code"] = "SESSION_INPUT_PROBE_FAILED"
        code, result, _ = self.run_child(report=report, returncode=2)
        self.assertEqual(code, 2)
        self.assertEqual(result, report)

    def test_unknown_top_level_and_nested_payloads_are_rejected_not_forwarded(self):
        top = public_report(private_payload=SECRET)
        nested = public_report()
        nested["inputs"]["private_payload"] = SECRET
        for report in (top, nested):
            with self.subTest(nested="private_payload" not in report):
                code, result, _ = self.run_child(report=report)
                self.assertEqual(code, 2)
                self.assertEqual(result["error_code"], "SESSION_INPUT_PROBE_WORKER_FAILED")

    def test_capture_failure_code_is_strictly_filtered_and_nullable(self):
        for failure_code in (None, "IBKR_SESSION_INPUT_READ_CALLBACK_REQUEST_322", "IBKR_SESSION_INPUT_READ_TIMEOUT_COMPLETED_ORDERS"):
            report = public_report(collection_completed=False, error_code="SESSION_INPUT_PROBE_FAILED")
            report["inputs"]["observation_failed"] = True
            report["inputs"]["capture_failure_code"] = failure_code
            with self.subTest(failure_code=failure_code):
                code, retained, _ = self.run_child(report=report, returncode=2)
                self.assertEqual(code, 2)
                self.assertEqual(retained, report)
        for private in (SECRET, "IBKR_SESSION_INPUT_PRIVATE_ACCOUNT_U1234567", "IBKR_SESSION_INPUT_READ_CALLBACK_PRIVATE_322", 322):
            report = public_report(collection_completed=False)
            report["inputs"]["capture_failure_code"] = private
            with self.subTest(private=private):
                code, retained, _ = self.run_child(report=report, returncode=2)
                self.assertEqual(code, 2)
                self.assertEqual(retained["error_code"], "SESSION_INPUT_PROBE_WORKER_FAILED")

    def test_nested_authority_or_boolean_coercion_is_rejected(self):
        for key in ("live_authority", "baseline_authority", "baseline_frozen"):
            for value in (True, 0, "false"):
                with self.subTest(key=key, value=value):
                    report = public_report()
                    report["inputs"][key] = value
                    code, result, _ = self.run_child(report=report)
                    self.assertEqual(code, 2)
                    self.assertEqual(result["error_code"], "SESSION_INPUT_PROBE_WORKER_FAILED")

    def test_private_payloads_under_known_fields_and_invalid_types_are_rejected(self):
        cases = (
            (False, "checked_at", SECRET),
            (False, "checked_at", "2026-09-18T14:00:00"),
            (True, "position_count", {"private": SECRET}),
            (True, "position_count", SECRET),
            (True, "position_count", True),
            (True, "blockers", [SECRET]),
            (True, "blockers", ["UNRECOGNIZED_PROVIDER_MESSAGE"]),
            (True, "observation_preconditions_met", SECRET),
            (True, "observation_preconditions_met", 1),
            (True, "execution_time_bases", [SECRET]),
        )
        for nested, key, value in cases:
            with self.subTest(nested=nested, key=key, value=value):
                report = public_report()
                (report["inputs"] if nested else report)[key] = value
                code, result, _ = self.run_child(report=report)
                self.assertEqual(code, 2)
                self.assertEqual(result["error_code"], "SESSION_INPUT_PROBE_WORKER_FAILED")

    def test_valid_single_read_with_execution_time_bases_is_preserved(self):
        report = public_report(mode="single_read", inputs={
            "collection_id": "d" * 64, "read_generation": 1,
            "collection_started_at": "2026-09-18T14:00:00+00:00",
            "collection_completed_at": "2026-09-18T14:00:01+00:00",
            "timing_basis": "local_receipt_not_atomic_broker_valuation",
            "net_liquidation_currency": "USD", "position_count": 1,
            "execution_count": 3, "order_count": 1, "active_order_count": 1,
            "completed_reads": ["account_updates_multi", "completed_orders", "executions", "open_orders", "positions"],
            "account_values_source": "IBKR_ACCOUNT_UPDATES_MULTI_V1",
            "commission_conflict_observed": False, "orphan_commission_report_count": 0,
            "daily_pnl_status": "not_requested", "diagnostic_only": True,
            "live_authority": False, "read_client_id": 42,
            "prior_collection_id": None, "unobserved_interval_since_prior_collection": False,
            "sticky_read_gap": False,
            "blockers": ["ALL_CLIENT_EXECUTION_COVERAGE_UNPROVEN", "CONTINUOUS_EVENT_COVERAGE_UNPROVEN"],
            "baseline_authority": False, "session_measurement_authority": False,
            "whole_account_coverage_verified": False,
            "execution_time_bases": ["ABSENT", "CONFIGURED_SESSION_ZONE_INTERPRETATION", "PROVIDER_EXPLICIT_ZONE"],
        })
        code, retained, _ = self.run_child(report=report)
        self.assertEqual(code, 0)
        self.assertEqual(retained, report)
        self.assertTrue(retained["inputs"]["blockers"])
        self.assertFalse(retained["inputs"]["session_measurement_authority"])

    def test_success_requires_collection_and_cleanup_not_only_zero_returncode(self):
        for changes in ({"collection_completed": False}, {"cleanup_completed": False}):
            with self.subTest(changes=changes):
                self.assertEqual(self.run_child(report=public_report(**changes))[0], 2)

    def test_launcher_rejects_unisolated_interpreter_without_starting_worker(self):
        namespace = runpy.run_path(str(ROOT / "scripts/titan-session-inputs-probe"), run_name="test_launcher")
        output = io.StringIO()
        with patch.object(sys, "flags", SimpleNamespace(isolated=0, no_site=0)), patch.object(probe, "run_isolated_probe") as child, redirect_stdout(output):
            self.assertEqual(namespace["main"](), 2)
        child.assert_not_called()
        report = json.loads(output.getvalue())
        self.assertEqual(report["error_code"], "SESSION_PROBE_REQUIRES_ISOLATED_NO_SITE_NO_BYTECODE")
        self.assertFalse(report["live_authority"])

    def test_launcher_delegates_to_watchdog_not_in_process_probe(self):
        namespace = runpy.run_path(str(ROOT / "scripts/titan-session-inputs-probe"), run_name="test_launcher")
        argv = ["synthetic-launcher", "--install-root", "/synthetic-install", "--single-read"]
        with patch.object(sys, "flags", SimpleNamespace(isolated=1, no_site=1)), patch.object(sys, "dont_write_bytecode", True), patch.object(sys, "argv", argv), patch.object(sys, "path", list(sys.path)), patch.object(probe, "run_isolated_probe", return_value=2) as child, patch.object(probe, "run_probe") as direct:
            self.assertEqual(namespace["main"](), 2)
        child.assert_called_once_with(source_root=ROOT, argv=argv[1:])
        direct.assert_not_called()


if __name__ == "__main__":
    unittest.main()
