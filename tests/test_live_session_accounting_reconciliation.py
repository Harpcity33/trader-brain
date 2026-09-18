"""Synthetic cash identities, never broker calls or authentic evidence."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal, localcontext
import json
import unittest

from titan_brain.live.session_accounting_reconciliation import (
    SessionAccountingError, SessionNonTradingAdjustment, reconcile_session_accounting,
)
from tests import test_live_session_trading_calculation as fixtures


D = Decimal
NOW, FROZEN, ACCOUNT = fixtures.NOW, fixtures.FROZEN, fixtures.ACCOUNT


def cash(value, amount="10000", **changes):
    return replace(value, facts=replace(value.facts,
        cash_value=D(amount), cash_currency="USD",
        cash_received_at=value.facts.collection_completed_at - timedelta(milliseconds=50), **changes))


def baseline():
    return tuple(cash(value) for value in fixtures.baseline_pair())


def current(amount="4999", **changes):
    return cash(fixtures.observation(**changes), amount)


def adjustment(identity="known-transfer-1", **changes):
    return replace(SessionNonTradingAdjustment(
        identity, ACCOUNT, "EXTERNAL_CASH_MOVEMENT", D(1000), "USD",
        FROZEN + timedelta(minutes=1), NOW, "4" * 64,
    ), **changes)


class SessionAccountingReconciliationTests(unittest.TestCase):
    def reconcile(self, value=None, *, pair=None, adjustments=(), prior=(), now=NOW):
        return reconcile_session_accounting(
            baseline_observations=baseline() if pair is None else pair,
            observation=current() if value is None else value, now=now,
            prior_observations=prior, nontrading_adjustments=adjustments,
        )

    def blocked(self, expected, value=None, **kwargs):
        result = self.reconcile(value, **kwargs)
        self.assertIn(expected, result.material_blockers)
        self.assertFalse(result.observed_cash_identity_matched)
        self.assertIsNone(result.unexplained_cash_residual)
        return result

    def test_buy_actual_cash_and_fees_match_without_nlv_or_quote(self):
        result = self.reconcile()
        self.assertEqual(result.gross_execution_cash_flow, D(-5000))
        self.assertEqual(result.actual_signed_commissions, D(1))
        self.assertEqual(result.trading_cash_flow, D(-5001))
        self.assertEqual(result.expected_cash, D(4999))
        self.assertEqual(result.unexplained_cash_residual, D(0))
        self.assertTrue(result.observed_cash_identity_matched)
        self.assertEqual(result.material_blockers, ())
        changed = current()
        changed = replace(changed, facts=replace(changed.facts, net_liquidation=D("999999")))
        independent = self.reconcile(changed)
        self.assertEqual(independent, result)

    def test_sell_proceeds_and_actual_fees_count_once(self):
        sell = fixtures.execution("sell", side="SELL", price=D(52), source_executed_at=FROZEN + timedelta(minutes=6))
        result = self.reconcile(current("10198", executions=(fixtures.execution(), sell), positions=()))
        self.assertEqual(result.gross_execution_cash_flow, D(200))
        self.assertEqual(result.actual_signed_commissions, D(2))
        self.assertEqual(result.trading_cash_flow, D(198))
        self.assertTrue(result.observed_cash_identity_matched)

    def test_signed_commission_rebate_is_not_charged_twice(self):
        result = self.reconcile(current("5000.25", executions=(fixtures.execution(commission=D("-0.25")),)))
        self.assertEqual(result.actual_signed_commissions, D("-0.25"))
        self.assertEqual(result.trading_cash_flow, D("-4999.75"))
        self.assertTrue(result.observed_cash_identity_matched)

    def test_identical_execution_callbacks_are_deduplicated(self):
        row = fixtures.execution()
        result = self.reconcile(current(executions=(row, row, replace(row, price=D("50.000")))))
        self.assertEqual(result.execution_count, 1)
        self.assertTrue(result.observed_cash_identity_matched)

    def test_cumulative_history_receipt_replays_do_not_double_count(self):
        prior_time = NOW - timedelta(seconds=2)
        row = replace(fixtures.execution(), received_at=prior_time - timedelta(milliseconds=200), commission_received_at=prior_time - timedelta(milliseconds=100))
        prior = current(at=prior_time, collection_id="d" * 64, executions=(row,))
        result = self.reconcile(current(prior_id="d" * 64), prior=(prior,))
        self.assertEqual(result.execution_count, 1)
        self.assertTrue(result.observed_cash_identity_matched)

    def test_omitted_prior_execution_blocks_even_if_current_cash_looks_balanced(self):
        prior_time = NOW - timedelta(seconds=2)
        row = replace(fixtures.execution(), received_at=prior_time - timedelta(milliseconds=200), commission_received_at=prior_time - timedelta(milliseconds=100))
        prior = current(at=prior_time, collection_id="d" * 64, executions=(row,))
        self.blocked("EXECUTION_HISTORY_OMITTED", current("10000", prior_id="d" * 64, executions=(), positions=()), prior=(prior,))

    def test_fee_revision_and_native_execid_correction_block(self):
        row = fixtures.execution()
        self.blocked("EXECUTION_OR_FEE_REVISION_UNRESOLVED", current(executions=(row, replace(row, commission=D(2)))))
        self.blocked("EXECUTION_CORRECTION_UNRESOLVED", current(executions=(fixtures.execution("abc.def.ghi.01"), fixtures.execution("abc.def.ghi.02"))))

    def test_known_signed_nontrading_flows_are_separate_from_trading(self):
        for amount in (D(1000), D(-1000)):
            with self.subTest(amount=amount):
                result = self.reconcile(current(str(D(4999) + amount)), adjustments=(adjustment(amount=amount),))
                self.assertEqual(result.trading_cash_flow, D(-5001))
                self.assertEqual(result.known_nontrading_cash_flow, amount)
                self.assertTrue(result.observed_cash_identity_matched)

    def test_duplicate_adjustment_counted_once_and_conflict_blocks(self):
        row = adjustment()
        result = self.reconcile(current("5999"), adjustments=(row, row))
        self.assertEqual(result.adjustment_count, 1)
        self.assertTrue(result.observed_cash_identity_matched)
        self.blocked("NONTRADING_ADJUSTMENT_CONFLICT", current("5999"), adjustments=(row, replace(row, amount=D(2000))))

    def test_unexplained_positive_and_negative_cash_residual_not_assumed_flow(self):
        for amount, expected in (("5999", D(1000)), ("3999", D(-1000))):
            result = self.reconcile(current(amount))
            self.assertEqual(result.unexplained_cash_residual, expected)
            self.assertEqual(result.known_nontrading_cash_flow, D(0))
            self.assertIn("UNEXPLAINED_CASH_RESIDUAL", result.material_blockers)
            self.assertFalse(result.observed_cash_identity_matched)
            self.assertTrue(result.public_dict()["unexplained_cash_residual_present"])

    def test_missing_actual_cash_never_falls_back_to_nlv_or_zero(self):
        for field in ("cash_value", "cash_currency", "cash_received_at"):
            value = current()
            value = replace(value, facts=replace(value.facts, **{field: None}))
            result = self.blocked("ACTUAL_CASH_VALUE_MISSING", value)
            self.assertIsNone(result.expected_cash)
            pair = list(baseline())
            pair[1] = replace(pair[1], facts=replace(pair[1].facts, **{field: None}))
            self.blocked("ACTUAL_CASH_VALUE_MISSING", pair=tuple(pair))

    def test_currency_source_completion_and_scope_fail_closed(self):
        cases = (
            ({"cash_currency": "BASE"}, "ACTUAL_CASH_USD_UNPROVEN"),
            ({"account_values_source": "IBKR_ACCOUNT_SUMMARY_V1"}, "ACCOUNT_VALUES_SOURCE_OR_COMPLETION_UNACCEPTED"),
            ({"completed_reads": ()}, "ACCOUNT_VALUES_SOURCE_OR_COMPLETION_UNACCEPTED"),
            ({"generation": 2}, "OBSERVATION_SCOPE_CHANGED"),
        )
        for changes, reason in cases:
            value = current()
            self.blocked(reason, replace(value, facts=replace(value.facts, **changes)))
        self.blocked("OBSERVATION_SCOPE_CHANGED", replace(current(), account_binding_fingerprint="f" * 64))
        self.blocked("OBSERVATION_SCOPE_CHANGED", replace(current(), read_client_id=999))

    def test_stale_future_or_outside_cash_receipt_blocks(self):
        self.blocked("CURRENT_CASH_LOCAL_RECEIPT_STALE_OR_FUTURE", now=NOW + timedelta(seconds=6))
        value = current()
        self.blocked("ACTUAL_CASH_RECEIPT_OUTSIDE_COLLECTION", replace(value, facts=replace(value.facts, cash_received_at=NOW + timedelta(seconds=1))))
        self.blocked("OBSERVATION_TIME_INVALID", now=NOW - timedelta(seconds=1))

    def test_execution_source_time_fee_currency_scope_and_receipts_required(self):
        cases = (
            ({"source_executed_at": None, "source_time_basis": "ABSENT"}, "EXECUTION_TIME_PROVENANCE_UNRESOLVED"),
            ({"source_time_basis": "CONFIGURED_SESSION_ZONE_INTERPRETATION"}, "EXECUTION_TIME_PROVENANCE_UNRESOLVED"),
            ({"commission_currency": "EUR"}, "ACTUAL_COMMISSION_USD_UNPROVEN"),
            ({"commission_received_at": NOW + timedelta(seconds=1)}, "EXECUTION_OR_FEE_RECEIPT_OUTSIDE_COLLECTION"),
            ({"quantity": D("0.5")}, "EXECUTION_SCOPE_UNACCEPTED"),
            ({"security_type": "OPT"}, "EXECUTION_SCOPE_UNACCEPTED"),
            ({"source_executed_at": FROZEN - timedelta(seconds=1)}, "EXECUTION_OUTSIDE_CASH_INTERVAL"),
        )
        for changes, reason in cases:
            self.blocked(reason, current(executions=(fixtures.execution(**changes),)))

    def test_short_chronology_and_contract_identity_conflicts_block(self):
        self.blocked("EXECUTION_LONG_ONLY_CHRONOLOGY_UNRESOLVED", current("14999", executions=(fixtures.execution(side="SELL"),)))
        self.blocked("EXECUTION_CONTRACT_IDENTITY_CONFLICT", current(executions=(fixtures.execution("a"), fixtures.execution("b", symbol="OTHER"))))

    def test_baseline_must_be_flat_stable_and_correctly_linked(self):
        pair = list(baseline())
        pair[0] = replace(pair[0], facts=replace(pair[0].facts, cash_value=D(9999)))
        self.blocked("BASELINE_CASH_NOT_STABLE", pair=tuple(pair))
        pair = list(baseline())
        pair[0] = replace(pair[0], facts=replace(pair[0].facts, positions=(fixtures.position(),)))
        self.blocked("BASELINE_NOT_FLAT_PRE_ENTRY", pair=tuple(pair))
        self.blocked("OBSERVATION_LINEAGE_UNRESOLVED", replace(current(), prior_collection_id="e" * 64))

    def test_gap_and_commission_uncertainty_not_cleared_by_zero_residual(self):
        self.blocked("INPUT_OBSERVATION_REQUIRES_RECONCILIATION", replace(current(), sticky_read_gap=True))
        value = current()
        self.blocked("ACTUAL_COMMISSION_RECONCILIATION_UNRESOLVED", replace(value, facts=replace(value.facts, commission_conflict_observed=True)))

    def test_adjustment_wrong_account_outside_interval_and_future_receipt_block(self):
        self.blocked("NONTRADING_ADJUSTMENT_ACCOUNT_MISMATCH", current("5999"), adjustments=(adjustment(account_binding_sha256="f" * 64),))
        self.blocked("NONTRADING_ADJUSTMENT_OUTSIDE_CASH_INTERVAL", current("5999"), adjustments=(adjustment(effective_at=FROZEN - timedelta(seconds=1)),))
        self.blocked("NONTRADING_ADJUSTMENT_OUTSIDE_CASH_INTERVAL", current("5999"), adjustments=(adjustment(received_at=NOW + timedelta(seconds=1)),))

    def test_invalid_amounts_and_types_raise_only_static_codes(self):
        for amount in (None, True, 1.0, D("NaN"), D("Infinity"), D("1e19")):
            value = current()
            with self.subTest(amount=amount), self.assertRaisesRegex(SessionAccountingError, "^SESSION_ACCOUNTING_AMOUNT_INVALID$"):
                self.reconcile(replace(value, facts=replace(value.facts, executions=(replace(fixtures.execution(), commission=amount),))))
        with self.assertRaisesRegex(SessionAccountingError, "^SESSION_ACCOUNTING_FACTS_TYPE_INVALID$"):
            self.reconcile(replace(current(), facts=None))

    def test_precision_is_independent_of_callers_decimal_context(self):
        with localcontext() as context:
            context.prec = 3
            result = self.reconcile()
        self.assertEqual(result.expected_cash, D(4999))
        self.assertTrue(result.observed_cash_identity_matched)

    def test_low_decimal_precision_cannot_round_away_short_exposure(self):
        buy = fixtures.execution("buy", quantity=D(999), price=D(1), commission=D(0))
        sell = fixtures.execution("sell", quantity=D(1000), price=D(1), commission=D(0), side="SELL", source_executed_at=FROZEN + timedelta(minutes=6))
        with localcontext() as context:
            context.prec = 2
            result = self.reconcile(current("10001", executions=(buy, sell), positions=()))
        self.assertIn("EXECUTION_LONG_ONLY_CHRONOLOGY_UNRESOLVED", result.material_blockers)
        self.assertIsNone(result.unexplained_cash_residual)
        self.assertFalse(result.observed_cash_identity_matched)

    def test_evidence_hash_binds_scope_blockers_exposure_and_outcome(self):
        clean = current("10000", executions=(), positions=())
        expected = self.reconcile(clean)
        variants = (
            replace(clean, sticky_read_gap=True),
            replace(clean, prior_collection_id="e" * 64),
            replace(clean, read_client_id=123),
            replace(clean, facts=replace(clean.facts, generation=2)),
            replace(clean, facts=replace(clean.facts, completed_reads=())),
            replace(clean, facts=replace(clean.facts, positions=(fixtures.position(),))),
        )
        for value in variants:
            self.assertNotEqual(self.reconcile(value).evidence_sha256, expected.evidence_sha256)

    def test_zero_empty_ledger_is_only_observed_identity_not_authority(self):
        result = self.reconcile(current("10000", executions=(), positions=()))
        self.assertTrue(result.observed_cash_identity_matched)
        self.assertEqual(result.execution_count, 0)
        public = result.public_dict()
        for name in ("live_authority", "session_measurement_authority", "whole_account_coverage_verified"):
            self.assertIs(public[name], False)
        self.assertTrue(public["source_blockers"])
        encoded = json.dumps(public)
        for private in (ACCOUNT, "10000", "synthetic-fill-1", "known-transfer-1"):
            self.assertNotIn(private, encoded)
        self.assertFalse(hasattr(result, "to_measurement"))


if __name__ == "__main__":
    unittest.main()
