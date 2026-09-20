"""Tests for the manual-intervention reconciliation gate (milestone 8)."""

from __future__ import annotations

import unittest

from titan_brain.option_reconciliation_gate import (
    CaptureHealth,
    ReconciliationError,
    ReconciliationInput,
    reconcile_account_activity,
)


def _capture(**overrides):
    fields = dict(
        reporting_scope_account_wide=True,
        capture_continuous=True,
        subscription_active=True,
        history_complete=True,
        duplicate_or_late_events_detected=False,
    )
    fields.update(overrides)
    return CaptureHealth(**fields)


def _input(**overrides):
    fields = dict(
        expected_positions={"optA": 2},
        observed_positions={"optA": 2},
        expected_orders={"optA": 1},
        observed_orders={"optA": 1},
        capture=_capture(),
    )
    fields.update(overrides)
    return ReconciliationInput(**fields)


class ReconciliationGateTests(unittest.TestCase):
    def test_clean_reconciliation(self) -> None:
        r = reconcile_account_activity(_input())
        self.assertTrue(r.reconciled)
        self.assertEqual(r.blockers, ())

    def test_manual_position_change_blocks(self) -> None:
        # Owner closed 1 contract manually: observed 1 vs expected 2.
        r = reconcile_account_activity(_input(observed_positions={"optA": 1}))
        self.assertFalse(r.reconciled)
        self.assertIn("MANUAL_POSITION_CHANGE_UNRECONCILED", r.blockers)
        self.assertIn("optA", r.manual_position_symbols)

    def test_new_manual_position_blocks(self) -> None:
        # A position Titan didn't open appears (another client).
        r = reconcile_account_activity(_input(observed_positions={"optA": 2, "optB": 5}))
        self.assertIn("MANUAL_POSITION_CHANGE_UNRECONCILED", r.blockers)
        self.assertIn("optB", r.manual_position_symbols)

    def test_unknown_working_order_blocks(self) -> None:
        r = reconcile_account_activity(_input(observed_orders={"optA": 1, "optC": 1}))
        self.assertIn("UNKNOWN_OR_MISSING_WORKING_ORDER", r.blockers)
        self.assertIn("optC", r.unknown_order_symbols)

    def test_missing_expected_order_blocks(self) -> None:
        # Titan expected an order that is no longer observed (cancelled elsewhere).
        r = reconcile_account_activity(_input(observed_orders={}))
        self.assertIn("UNKNOWN_OR_MISSING_WORKING_ORDER", r.blockers)

    def test_reporting_scope_not_account_wide_blocks(self) -> None:
        r = reconcile_account_activity(_input(capture=_capture(reporting_scope_account_wide=False)))
        self.assertIn("REPORTING_SCOPE_NOT_ACCOUNT_WIDE", r.blockers)

    def test_capture_discontinuity_blocks(self) -> None:
        r = reconcile_account_activity(_input(capture=_capture(capture_continuous=False)))
        self.assertIn("CAPTURE_DISCONTINUITY", r.blockers)

    def test_subscription_inactive_blocks(self) -> None:
        r = reconcile_account_activity(_input(capture=_capture(subscription_active=False)))
        self.assertIn("SUBSCRIPTION_INACTIVE_OR_REJECTED", r.blockers)

    def test_incomplete_history_blocks(self) -> None:
        r = reconcile_account_activity(_input(capture=_capture(history_complete=False)))
        self.assertIn("HISTORY_INCOMPLETE", r.blockers)

    def test_duplicate_or_late_events_block(self) -> None:
        r = reconcile_account_activity(_input(capture=_capture(duplicate_or_late_events_detected=True)))
        self.assertIn("DUPLICATE_OR_LATE_EVENTS", r.blockers)

    def test_capture_flags_must_be_strict_true(self) -> None:
        # A truthy non-True value must not pass the scope gate.
        r = reconcile_account_activity(_input(capture=_capture(reporting_scope_account_wide=1)))
        self.assertIn("REPORTING_SCOPE_NOT_ACCOUNT_WIDE", r.blockers)

    def test_non_int_map_rejected(self) -> None:
        with self.assertRaises(ReconciliationError):
            reconcile_account_activity(_input(expected_positions={"optA": "2"}))

    def test_bool_map_value_rejected(self) -> None:
        with self.assertRaises(ReconciliationError):
            reconcile_account_activity(_input(observed_positions={"optA": True}))

    def test_multiple_blockers_accumulate(self) -> None:
        r = reconcile_account_activity(_input(
            observed_positions={"optA": 1},
            observed_orders={"optZ": 1},
            capture=_capture(history_complete=False),
        ))
        self.assertIn("MANUAL_POSITION_CHANGE_UNRECONCILED", r.blockers)
        self.assertIn("UNKNOWN_OR_MISSING_WORKING_ORDER", r.blockers)
        self.assertIn("HISTORY_INCOMPLETE", r.blockers)
        self.assertEqual(len(r.blockers), len(set(r.blockers)))


if __name__ == "__main__":
    unittest.main()
