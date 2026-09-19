"""Offline end-to-end rehearsal with concrete runtime and synthetic callbacks."""

from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from titan_brain.live.session_trading_policy import (
    ObservationStatus, load_session_trading_policy_from_root,
)
from titan_brain.live.session_trading_rehearsal import record_fresh_session_rehearsal
from titan_brain.live.session_trading_store import SessionTradingStore
from titan_brain.live.session_observation_ledger import SessionObservationLedger
from tests import test_live_ibkr_session_inputs as fixtures


ROOT = Path(__file__).resolve().parents[1]


class SessionTradingRehearsalTests(unittest.TestCase):
    def setUp(self):
        fixtures.IbkrSessionInputsTests.setUp(self)
        fixtures.IbkrSessionInputsTests.connect(self)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name).resolve() / "diagnostic.sqlite3"
        self.policy = load_session_trading_policy_from_root(ROOT)
        self.identity = {
            "account_binding_sha256": self.runtime.account_binding_fingerprint,
            "session_date": self.now.astimezone(ZoneInfo("America/New_York")).date(),
        }

    def run_rehearsal(self):
        return record_fresh_session_rehearsal(
            policy=self.policy, adapter=self.adapter, store_path=self.path, now=lambda: self.now,
        )

    def stored(self):
        with SessionTradingStore(self.path) as store:
            return store.load(**self.identity)

    def evidence(self):
        with SessionObservationLedger(self.path.with_name(self.path.name + ".observations"), **self.identity) as ledger:
            return ledger.history(), ledger.head_receipt

    def test_real_adapter_to_calculation_to_disk_reopen_never_grants_authority(self):
        result = self.run_rehearsal()
        public = result.public_dict()
        self.assertTrue(public["arithmetic_available"])
        self.assertTrue(public["diagnostic_baseline_recorded"])
        self.assertTrue(public["state_reopened_and_matched"])
        self.assertTrue(public["evidence_reopened_and_matched"])
        self.assertEqual(public["cumulative_observation_count"], 3)
        history, head = self.evidence()
        self.assertEqual(len(history), 3)
        self.assertEqual(head, public["evidence_ledger_head_sha256"])
        self.assertEqual(history[-1].facts.account_values_source, "IBKR_ACCOUNT_UPDATES_MULTI_V1")
        self.assertTrue(public["accounting_check_completed"])
        self.assertTrue(public["observed_cash_identity_matched"])
        self.assertEqual(public["accounting_material_blocker_count"], 0)
        self.assertRegex(public["accounting_evidence_sha256"], r"^[a-f0-9]{64}$")
        self.assertEqual(public["unresolved_observation_count"], 1)
        self.assertEqual(public["material_blocker_count"], 0)
        self.assertIn("ALL_CLIENT_EXECUTION_COVERAGE_UNPROVEN", public["source_blockers"])
        self.assertIn("UNRESOLVED_OBSERVATION_INCIDENT", public["risk_blockers"])
        for key in ("live_authority", "baseline_authority", "baseline_frozen", "session_measurement_authority", "full_session_pnl_established"):
            self.assertIs(public[key], False)
        row = self.stored()
        self.assertIsNone(row.state.last_measurement)
        self.assertEqual(row.state.incidents[0].status, ObservationStatus.FAILED)
        self.assertEqual(row.state.incidents[0].reason, "INCOMPLETE")
        self.assertEqual(row.audit_head_sha256, result.audit_head_sha256)
        self.assertEqual(result.observation_failure_phase, "none")
        self.assertIsNone(public["capture_failure_code"])
        self.assertTrue(public["read_diagnostic_available"])
        self.assertEqual(public["read_missing_channels"], ())
        self.assertTrue(public["read_normalization_completed"])
        self.assertNotIn("reqPnL", [call[0] for call in self.client.calls])
        self.assertFalse(self.runtime.status().command_connected)
        self.assertEqual(self.client.mutations, [])

    def test_pending_is_committed_and_visible_before_third_broker_read(self):
        capture = self.adapter.capture
        count = 0

        def inspected_capture():
            nonlocal count
            count += 1
            if count == 3:
                stored = self.stored()
                self.assertEqual(len(stored.state.incidents), 1)
                self.assertEqual(stored.state.incidents[0].status, ObservationStatus.PENDING)
            return capture()

        with patch.object(self.adapter, "capture", side_effect=inspected_capture):
            self.run_rehearsal()
        self.assertEqual(count, 3)

    def test_crash_after_pending_commit_leaves_recoverable_unresolved_marker(self):
        capture = self.adapter.capture
        count = 0

        def interrupted_capture():
            nonlocal count
            count += 1
            if count == 3:
                raise KeyboardInterrupt("synthetic crash")
            return capture()

        with patch.object(self.adapter, "capture", side_effect=interrupted_capture):
            with self.assertRaises(KeyboardInterrupt):
                self.run_rehearsal()
        state = self.stored().state
        self.assertEqual(state.incidents[0].status, ObservationStatus.PENDING)
        self.assertIsNone(state.last_measurement)
        with patch.object(self.adapter, "capture") as read:
            with self.assertRaises(Exception):
                self.run_rehearsal()
            read.assert_not_called()
        self.assertEqual(self.stored().state, state)
        self.assertEqual(len(self.evidence()[0]), 2)

    def test_crash_after_evidence_commit_preserves_raw_history_and_pending_risk(self):
        with patch("titan_brain.live.session_trading_rehearsal.calculate_session_trading_pnl", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_rehearsal()
        self.assertEqual(len(self.evidence()[0]), 3)
        self.assertEqual(self.stored().state.incidents[0].status, ObservationStatus.PENDING)

    def test_evidence_failure_leaves_gap_and_never_calculates(self):
        append = SessionObservationLedger.append
        count = 0

        def failed_append(ledger, observation, **kwargs):
            nonlocal count
            count += 1
            if count == 3:
                raise RuntimeError("private-storage-failure")
            return append(ledger, observation, **kwargs)

        with patch.object(SessionObservationLedger, "append", new=failed_append), patch("titan_brain.live.session_trading_rehearsal.calculate_session_trading_pnl") as calculate:
            result = self.run_rehearsal()
        calculate.assert_not_called()
        self.assertEqual(result.observation_failure_phase, "evidence_persistence")
        self.assertEqual(result.source_blockers, ("EVIDENCE_PERSISTENCE_FAILED",))
        self.assertEqual(result.cumulative_observation_count, 2)
        self.assertEqual(self.stored().state.incidents[0].reason, "GAP")
        self.assertNotIn("private-storage-failure", json.dumps(result.public_dict()))

    def test_existing_evidence_file_rejected_before_broker_read(self):
        self.path.with_name(self.path.name + ".observations").touch()
        with patch.object(self.adapter, "capture") as capture:
            with self.assertRaisesRegex(ValueError, "EVIDENCE_ALREADY_EXISTS"):
                self.run_rehearsal()
        capture.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_failed_third_read_persists_gap_without_private_exception(self):
        capture = self.adapter.capture
        count = 0

        def failed_capture():
            nonlocal count
            count += 1
            if count == 3:
                raise RuntimeError("synthetic-private-payload")
            return capture()

        with patch.object(self.adapter, "capture", side_effect=failed_capture):
            result = self.run_rehearsal()
        self.assertTrue(result.observation_failed)
        self.assertFalse(result.arithmetic_available)
        self.assertEqual(result.observation_failure_phase, "capture")
        self.assertIsNone(result.read_diagnostic)  # mocked pre-dispatch failure cannot borrow baseline diagnostic
        self.assertIsNone(result.capture_failure_code)
        self.assertEqual(self.stored().state.incidents[0].reason, "GAP")
        self.assertNotIn("synthetic-private-payload", json.dumps(result.public_dict()))

    def test_calculation_failure_persists_gap_not_successful_measurement(self):
        with patch("titan_brain.live.session_trading_rehearsal.calculate_session_trading_pnl", side_effect=RuntimeError("private")):
            result = self.run_rehearsal()
        self.assertTrue(result.observation_failed)
        self.assertEqual(result.observation_failure_phase, "calculation")
        self.assertIsNone(result.capture_failure_code)
        self.assertTrue(result.read_diagnostic.normalization_completed)
        self.assertEqual(result.source_blockers, ("CALCULATION_REJECTED",))
        self.assertEqual(self.stored().state.incidents[0].reason, "GAP")

    def test_accounting_failure_retains_raw_evidence_and_gap_without_claiming_match(self):
        with patch("titan_brain.live.session_trading_rehearsal.reconcile_session_accounting", side_effect=ValueError("private-accounting")):
            result = self.run_rehearsal()
        self.assertEqual(result.observation_failure_phase, "accounting")
        self.assertEqual(result.source_blockers, ("ACCOUNTING_CHECK_FAILED",))
        self.assertFalse(result.accounting_check_completed)
        self.assertFalse(result.observed_cash_identity_matched)
        self.assertIsNone(result.accounting_evidence_sha256)
        self.assertEqual(len(self.evidence()[0]), 3)
        self.assertEqual(self.stored().state.incidents[0].reason, "GAP")

    def test_failed_finite_channel_is_attributed_to_third_read_not_baseline(self):
        capture = self.adapter.capture
        count = 0

        def timeout_capture():
            nonlocal count
            count += 1
            if count == 3:
                self.client.omit_end = "completed_orders"
            return capture()

        with patch.object(self.adapter, "capture", side_effect=timeout_capture):
            result = self.run_rehearsal()
        self.assertTrue(result.observation_failed)
        self.assertEqual(result.observation_failure_phase, "capture")
        public = result.public_dict()
        self.assertTrue(public["read_diagnostic_available"])
        self.assertEqual(public["read_missing_channels"], ("completed_orders",))
        self.assertFalse(public["read_normalization_completed"])
        self.assertEqual(public["capture_failure_code"], "IBKR_SESSION_INPUT_READ_TIMEOUT_COMPLETED_ORDERS")

    def test_baseline_currency_failure_does_not_commit_baseline(self):
        self.client.nlv_currency = "BASE"
        with self.assertRaises(ValueError):
            self.run_rehearsal()
        self.assertIsNone(self.stored())

    def test_baseline_exposure_failure_does_not_commit_baseline(self):
        self.client.emit_active_order = True
        with self.assertRaises(ValueError):
            self.run_rehearsal()
        self.assertIsNone(self.stored())

    def test_existing_file_cannot_be_reused_to_reset_diagnostic_baseline(self):
        result = self.run_rehearsal()
        with patch.object(self.adapter, "capture") as read:
            with self.assertRaises(Exception):
                self.run_rehearsal()
            read.assert_not_called()
        self.assertEqual(self.stored().audit_head_sha256, result.audit_head_sha256)

    def test_public_result_contains_no_account_balance_pnl_or_database_path(self):
        public = self.run_rehearsal().public_dict()
        encoded = json.dumps(public)
        for private in (fixtures.SYNTHETIC_ACCOUNT, self.runtime.account_binding_fingerprint, str(self.path)):
            self.assertNotIn(private, encoded)
        self.assertNotIn("calculated_pnl", public)
        self.assertNotIn("starting_nlv", public)


if __name__ == "__main__":
    unittest.main()
