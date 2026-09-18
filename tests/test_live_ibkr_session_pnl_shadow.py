from __future__ import annotations

import ast
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from pathlib import Path
import sqlite3
import tempfile
import unittest

from titan_brain.live.broker.ibkr_session_pnl_shadow import (
    BidMark,
    EvidenceStatus,
    Execution,
    ExecutionCoverage,
    ExecutionFee,
    InputOrigin,
    Position,
    SessionBaseline,
    SessionObservation,
    ShadowInputError,
    ShadowSessionStore,
    evaluate_session_pnl,
)


D = Decimal
CONFIRMED = EvidenceStatus.CONFIRMED
START = datetime(2026, 9, 18, 13, 30, tzinfo=timezone.utc)
NOW = START + timedelta(minutes=10)
ACCOUNT = "synthetic-account-binding"


def baseline(**changes):
    value = SessionBaseline(
        account_binding=ACCOUNT,
        frozen_at=START,
        starting_nlv=D("10000"),
        source_evidence_id="synthetic-start",
        origin=InputOrigin.SYNTHETIC,
        flat_start=CONFIRMED,
        pre_entry=CONFIRMED,
        initial_exposure_reconciled=CONFIRMED,
    )
    return replace(value, **changes)


def execution(exec_id="fill-1", side="BUY", quantity=100, price="50", **changes):
    value = Execution(
        account_binding=ACCOUNT,
        exec_id=exec_id,
        contract_id=123,
        side=side,
        quantity=quantity,
        price=D(price),
        executed_at=START + timedelta(minutes=6),
    )
    return replace(value, **changes)


def fee(exec_id="fill-1", amount="1", **changes):
    value = ExecutionFee(ACCOUNT, exec_id, D(amount), START + timedelta(minutes=7))
    return replace(value, **changes)


def mark(price="51", *, at=NOW, **changes):
    value = BidMark(123, D(price), 100, at - timedelta(seconds=1), at, "synthetic-bid")
    return replace(value, **changes)


def observation(**changes):
    value = SessionObservation(
        account_binding=ACCOUNT,
        as_of=NOW,
        origin=InputOrigin.SYNTHETIC,
        source_evidence_id="synthetic-collection",
        executions=(execution(),),
        fees=(fee(),),
        positions=(Position(ACCOUNT, 123, 100),),
        marks=(mark(),),
        coverage=ExecutionCoverage(START, NOW, CONFIRMED, CONFIRMED, CONFIRMED, "synthetic-coverage"),
        positions_reconciled=CONFIRMED,
        orders_exposure_reconciled=CONFIRMED,
        accounting_reconciled=CONFIRMED,
        unexplained_accounting_delta=D(0),
    )
    return replace(value, **changes)


def later(value, *, seconds=1, price="51", **changes):
    at = value.as_of + timedelta(seconds=seconds)
    return replace(
        value,
        as_of=at,
        coverage=replace(value.coverage, claimed_through=at),
        marks=(mark(price, at=at),) if value.positions else (),
        **changes,
    )


class SessionPnlShadowTests(unittest.TestCase):
    def evaluate(self, value=None, start=None, **changes):
        value = observation() if value is None else value
        return evaluate_session_pnl(baseline() if start is None else start, value, now=value.as_of, **changes)

    def assert_blocked(self, reason, value=None, start=None):
        result = self.evaluate(value, start)
        self.assertIn(reason, result.blockers)
        self.assertIsNone(result.session_pnl)
        self.assertIsNone(result.performance_fraction)
        self.assertFalse(result.public_dict()["live_authority"])
        return result

    def test_open_long_pnl_uses_actual_fee_and_bid(self):
        result = self.evaluate()
        self.assertEqual(result.session_pnl, D("99"))
        self.assertEqual(result.performance_fraction, D("0.0099"))
        self.assertFalse(result.loss_latched)
        self.assertEqual(result.blockers, ())

    def test_every_output_is_shadow_unapproved_unverified(self):
        for value in (observation(), observation(fees=())):
            public = self.evaluate(value).public_dict()
            self.assertTrue(public["diagnostic_only"])
            self.assertFalse(public["live_authority"])
            self.assertFalse(public["policy_approved"])
            self.assertFalse(public["whole_account_return"])
            self.assertFalse(public["source_authentication_established"])
            self.assertEqual(public["latch_scope"], "OBSERVED_VALID_BREACHES_ONLY")
            self.assertFalse(public["unobserved_breach_exclusion_established"])
            self.assertEqual(public["external_flow_completeness"], "NOT_ESTABLISHED")
            self.assertTrue(public["synthetic_input"])
            self.assertNotIn(ACCOUNT, str(public))

    def test_non_synthetic_claim_still_not_authenticated(self):
        result = self.evaluate(
            observation(origin=InputOrigin.OBSERVED_UNVERIFIED),
            baseline(origin=InputOrigin.OBSERVED_UNVERIFIED),
        )
        self.assertFalse(result.synthetic_input)
        self.assertFalse(result.public_dict()["source_authentication_established"])
        self.assertFalse(result.public_dict()["live_authority"])

    def test_partial_fills_and_partial_exit_count_once(self):
        first = execution("fill-1", quantity=60)
        second = execution("fill-2", quantity=40, price="52", executed_at=START + timedelta(minutes=7))
        sell = execution("fill-3", side="SELL", quantity=30, price="55", executed_at=START + timedelta(minutes=8))
        value = observation(
            executions=(first, second, sell, first),
            fees=(fee(), fee("fill-2", "0.5"), fee("fill-3", "0.5", reported_at=NOW), fee()),
            positions=(Position(ACCOUNT, 123, 70),),
            marks=(mark("54"),),
        )
        self.assertEqual(self.evaluate(value).session_pnl, D("348"))

    def test_flat_closed_pnl_needs_no_quote(self):
        value = observation(
            executions=(execution(), execution("fill-2", side="SELL", price="52", executed_at=NOW)),
            fees=(fee(), fee("fill-2", reported_at=NOW)), positions=(), marks=(),
        )
        self.assertEqual(self.evaluate(value).session_pnl, D("198"))

    def test_true_empty_confirmed_session_has_derived_zero_not_missing_fallback(self):
        value = observation(executions=(), fees=(), positions=(), marks=())
        self.assertEqual(self.evaluate(value).session_pnl, D(0))
        self.assert_blocked("ALL_CLIENT_EXECUTIONS_UNPROVEN", replace(
            value, coverage=replace(value.coverage, all_clients=EvidenceStatus.UNKNOWN),
        ))

    def test_missing_fee_never_becomes_zero(self):
        self.assert_blocked("EXECUTION_FEE_MISSING", observation(fees=()))
        self.assertEqual(self.evaluate(observation(fees=(fee(amount="0"),))).session_pnl, D(100))

    def test_reported_rebate_preserves_sign(self):
        self.assertEqual(self.evaluate(observation(fees=(fee(amount="-0.25"),))).session_pnl, D("100.25"))

    def test_missing_mark_and_insufficient_size_block(self):
        self.assert_blocked("BID_MARK_MISSING", observation(marks=()))
        self.assert_blocked("BID_SIZE_INSUFFICIENT", observation(marks=(mark(size=99),)))

    def test_stale_future_and_receipt_before_quote_block(self):
        self.assert_blocked("BID_MARK_STALE", observation(marks=(mark(quoted_at=NOW - timedelta(seconds=6)),)))
        self.assert_blocked("BID_MARK_TIME_INVALID", observation(marks=(mark(quoted_at=NOW + timedelta(seconds=1)),)))
        self.assert_blocked("BID_MARK_TIME_INVALID", observation(marks=(mark(received_at=NOW + timedelta(seconds=1)),)))

    def test_stale_and_future_observation_block(self):
        result = evaluate_session_pnl(baseline(), observation(), now=NOW + timedelta(seconds=6))
        self.assertIn("OBSERVATION_STALE", result.blockers)
        result = evaluate_session_pnl(baseline(), observation(), now=NOW - timedelta(seconds=1))
        self.assertIn("OBSERVATION_TIME_INVALID", result.blockers)

    def test_all_required_evidence_statuses_fail_closed(self):
        for name in ("flat_start", "pre_entry", "initial_exposure_reconciled"):
            with self.subTest(name=name):
                self.assertTrue(self.evaluate(start=baseline(**{name: EvidenceStatus.UNKNOWN})).blockers)
        for name in ("positions_reconciled", "orders_exposure_reconciled", "accounting_reconciled"):
            with self.subTest(name=name):
                self.assertTrue(self.evaluate(observation(**{name: EvidenceStatus.UNKNOWN})).blockers)
        for name in ("all_clients", "continuous", "corrections_reconciled"):
            with self.subTest(name=name):
                value = observation()
                self.assertTrue(self.evaluate(replace(value, coverage=replace(value.coverage, **{name: EvidenceStatus.UNKNOWN}))).blockers)

    def test_flat_does_not_prove_coverage(self):
        value = observation(executions=(), fees=(), positions=(), marks=())
        value = replace(value, coverage=replace(value.coverage, continuous=EvidenceStatus.UNKNOWN))
        self.assert_blocked("EXECUTION_CONTINUITY_UNPROVEN", value)

    def test_coverage_must_cover_start_through_observation(self):
        value = observation()
        for coverage in (
            replace(value.coverage, claimed_from=START + timedelta(seconds=1)),
            replace(value.coverage, claimed_through=NOW - timedelta(seconds=1)),
            replace(value.coverage, claimed_through=NOW + timedelta(seconds=1)),
        ):
            self.assert_blocked("EXECUTION_COVERAGE_RANGE_INSUFFICIENT", replace(value, coverage=coverage))

    def test_foreign_account_fills_fees_positions_and_observation_block(self):
        self.assert_blocked("FOREIGN_ACCOUNT_EXECUTION", observation(executions=(execution(account_binding="other"),)))
        self.assert_blocked("EXECUTION_FEE_SCOPE_INVALID", observation(fees=(fee(account_binding="other"),)))
        self.assert_blocked("FOREIGN_ACCOUNT_POSITION", observation(positions=(Position("other", 123, 100),)))
        self.assert_blocked("ACCOUNT_BINDING_MISMATCH", observation(account_binding="other"))

    def test_non_usd_non_stock_and_short_inventory_block(self):
        self.assert_blocked("UNSUPPORTED_EXECUTION_INSTRUMENT", observation(executions=(execution(currency="EUR"),)))
        self.assert_blocked("UNSUPPORTED_EXECUTION_INSTRUMENT", observation(executions=(execution(security_type="OPT"),)))
        self.assert_blocked("UNSUPPORTED_POSITION", observation(positions=(Position(ACCOUNT, 123, -100),)))
        self.assert_blocked("SHORT_OR_EXECUTION_SEQUENCE_UNRESOLVED", observation(executions=(execution(side="SELL"),)))

    def test_positions_and_unexplained_account_adjustments_block(self):
        self.assert_blocked("POSITION_EXECUTION_MISMATCH", observation(positions=()))
        self.assert_blocked("UNEXPLAINED_ACCOUNTING_MOVEMENT", observation(unexplained_accounting_delta=D("1")))
        self.assert_blocked("ACCOUNTING_DELTA_UNKNOWN", observation(unexplained_accounting_delta=None))

    def test_deposit_does_not_increase_pnl_or_denominator(self):
        result = self.evaluate(observation(reported_external_cash_flow=D("1000000")))
        self.assertEqual(result.starting_nlv, D("10000"))
        self.assertEqual(result.session_pnl, D("99"))
        self.assertEqual(result.performance_fraction, D("0.0099"))
        self.assertEqual(result.public_dict()["external_flow_completeness"], "NOT_ESTABLISHED")

    def test_conflicting_duplicate_rows_block(self):
        self.assert_blocked("EXECUTION_DUPLICATE_CONFLICT", observation(executions=(execution(), execution(price="49"))))
        self.assert_blocked("FEE_DUPLICATE_CONFLICT", observation(fees=(fee(), fee(amount="2"))))
        self.assert_blocked("POSITION_DUPLICATE_CONFLICT", observation(positions=(Position(ACCOUNT, 123, 100), Position(ACCOUNT, 123, 90))))
        self.assert_blocked("MARK_DUPLICATE_CONFLICT", observation(marks=(mark(), mark("52"))))

    def test_correction_or_orphan_fee_or_wrong_time_blocks(self):
        self.assert_blocked("EXECUTION_CORRECTION_UNRESOLVED", observation(executions=(execution(correction_of="older-fill"),)))
        self.assert_blocked("ORPHAN_EXECUTION_FEE", observation(fees=(fee(), fee("unknown-fill"))))
        self.assert_blocked("EXECUTION_TIME_OUTSIDE_SESSION", observation(executions=(execution(executed_at=START - timedelta(seconds=1)),)))
        self.assert_blocked("EXECUTION_FEE_TIME_INVALID", observation(fees=(fee(reported_at=NOW + timedelta(seconds=1)),)))

    def test_native_correction_ids_cannot_be_counted_as_two_flat_round_trips(self):
        buy = execution("abc.def.ghi.01", quantity=10)
        buy_correction = replace(buy, exec_id="abc.def.ghi.02")
        sell = execution("jkl.mno.pqr.01", side="SELL", quantity=10, price="49", executed_at=NOW)
        sell_correction = replace(sell, exec_id="jkl.mno.pqr.02")
        value = observation(
            executions=(buy, buy_correction, sell, sell_correction),
            fees=tuple(fee(item.exec_id, reported_at=NOW) for item in (buy, buy_correction, sell, sell_correction)),
            positions=(), marks=(),
        )
        self.assert_blocked("EXECUTION_CORRECTION_UNRESOLVED", value)

    def test_no_other_production_module_imports_or_names_shadow_module(self):
        root = Path(__file__).resolve().parents[1] / "src" / "titan_brain"
        target = "ibkr_session_pnl_shadow"
        for path in root.rglob("*.py"):
            if path.name == target + ".py":
                continue
            with self.subTest(path=str(path.relative_to(root))):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom):
                        self.assertNotIn(target, node.module or "")
                        self.assertFalse(any(target in alias.name for alias in node.names))
                    elif isinstance(node, ast.Import):
                        self.assertFalse(any(target in alias.name for alias in node.names))
                    elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                        self.assertNotIn(target, node.value)

    def test_exact_loss_boundary_and_recovery_without_store(self):
        self.assertTrue(self.evaluate(observation(marks=(mark("40.01"),))).loss_latched)
        self.assertFalse(self.evaluate(observation(marks=(mark("40.02"),))).loss_latched)
        self.assertFalse(self.evaluate().loss_latched)  # Pure evaluation is not a durable ledger.

    def test_invalid_financial_and_structural_inputs_are_rejected(self):
        for amount in (D("NaN"), D("Infinity"), D("-Infinity"), D("1E10000"), D("1E-10000"), 1.0):
            with self.subTest(amount=amount), self.assertRaises(ShadowInputError):
                baseline(starting_nlv=amount)
        for quantity in (True, 0, D("1.5"), 1.5):
            with self.subTest(quantity=quantity), self.assertRaises(ShadowInputError):
                execution(quantity=quantity)
        with self.assertRaises(ShadowInputError):
            baseline(frozen_at=START.replace(tzinfo=None))
        with self.assertRaises(ShadowInputError):
            observation(executions=[execution()])
        with self.assertRaises(ShadowInputError):
            baseline(flat_start=True)
        with self.assertRaises(ShadowInputError):
            self.evaluate(max_quote_age=timedelta(days=1))


class DurableSessionPnlShadowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name).resolve() / "synthetic-shadow.sqlite3"
        self.store = ShadowSessionStore(self.path, create=True)
        self.addCleanup(self.close)

    def close(self):
        if self.store is not None:
            self.store.close()
            self.store = None

    def record(self, value=None, start=None, **changes):
        value = observation() if value is None else value
        return self.store.observe(baseline() if start is None else start, value, now=value.as_of, **changes)

    def reopen(self):
        self.close()
        self.store = ShadowSessionStore(self.path)

    def test_loss_latch_survives_restart_recovery_and_missing_data(self):
        loss = observation(marks=(mark("39"),))
        self.assertTrue(self.record(loss).loss_latched)
        self.reopen()
        recovery = later(loss, price="60")
        result = self.record(recovery)
        self.assertEqual(result.session_pnl, D("999"))
        self.assertTrue(result.loss_latched)
        self.assertTrue(self.record(later(recovery, fees=())).loss_latched)
        self.reopen()
        self.assertTrue(self.record(later(recovery, seconds=2)).loss_latched)

    def test_baseline_cannot_reset_after_loss_or_deposit(self):
        self.record(observation(marks=(mark("39"),)))
        for start in (
            baseline(starting_nlv=D("1000000")),
            baseline(frozen_at=START + timedelta(minutes=1)),
            baseline(source_evidence_id="replacement-start"),
        ):
            with self.assertRaises(ShadowInputError):
                self.record(later(observation()), start)
        self.reopen()
        self.assertTrue(self.record(later(observation())).loss_latched)

    def test_observation_time_cannot_regress_or_change_same_timestamp(self):
        self.record()
        self.assertEqual(self.record().session_pnl, D("99"))
        with self.assertRaises(ShadowInputError):
            self.record(later(observation(), seconds=-1))
        with self.assertRaises(ShadowInputError):
            self.record(observation(marks=(mark("52"),)))
        self.assertEqual(self.record(later(observation())).session_pnl, D("99"))

    def test_equivalent_decimal_scale_replays_after_restart_without_conflict(self):
        self.record()
        self.reopen()
        value = later(
            observation(),
            executions=(execution(price="50.000000"),),
            fees=(fee(amount="1.00"),),
            unexplained_accounting_delta=D("-0.000"),
        )
        with localcontext() as context:
            context.prec = 2
            result = self.record(value, baseline(starting_nlv=D("10000.0000")))
        self.assertEqual(result.blockers, ())
        self.assertEqual(result.session_pnl, D("99"))
        self.reopen()
        result = self.record(later(observation(), seconds=2), baseline(starting_nlv=D("1E4")))
        self.assertEqual(result.blockers, ())
        self.assertEqual(result.session_pnl, D("99"))

    def test_same_observation_rechecked_later_does_not_reuse_fresh_success(self):
        self.record()
        result = self.store.observe(baseline(), observation(), now=NOW + timedelta(seconds=6))
        self.assertIn("OBSERVATION_STALE", result.blockers)
        self.assertIsNone(result.session_pnl)

    def test_dropping_prior_round_trip_cannot_erase_loss(self):
        closed = observation(
            executions=(execution(), execution("fill-2", side="SELL", price="39", executed_at=NOW)),
            fees=(fee(), fee("fill-2", reported_at=NOW)), positions=(), marks=(),
        )
        self.assertTrue(self.record(closed).loss_latched)
        self.reopen()
        erased = later(closed, executions=(), fees=())
        result = self.record(erased)
        self.assertIn("EXECUTION_HISTORY_REGRESSED", result.blockers)
        self.assertIn("FEE_HISTORY_REGRESSED", result.blockers)
        self.assertIsNone(result.session_pnl)
        self.assertTrue(result.loss_latched)

    def test_fee_revisions_or_execution_changes_are_sticky_unresolved(self):
        for field, changed in (
            ("fees", (fee(amount="2"),)),
            ("executions", (execution(price="49"),)),
        ):
            with self.subTest(field=field):
                path = self.path.with_name(field + ".sqlite3")
                store = ShadowSessionStore(path, create=True)
                try:
                    store.observe(baseline(), observation(), now=NOW)
                    second = later(observation(), **{field: changed})
                    result = store.observe(baseline(), second, now=second.as_of)
                    self.assertIn("DURABLE_EVENT_CONFLICT_UNRESOLVED", result.blockers)
                    third = later(observation(), seconds=2)
                    result = store.observe(baseline(), third, now=third.as_of)
                    self.assertIn("DURABLE_EVENT_CONFLICT_UNRESOLVED", result.blockers)
                finally:
                    store.close()

    def test_repeated_omission_after_restart_does_not_replace_saved_history(self):
        self.record()
        empty = later(observation(), executions=(), fees=(), positions=())
        self.assertIn("EXECUTION_HISTORY_REGRESSED", self.record(empty).blockers)
        self.reopen()
        second_empty = later(empty)
        result = self.record(second_empty)
        self.assertIn("EXECUTION_HISTORY_REGRESSED", result.blockers)
        self.assertIn("FEE_HISTORY_REGRESSED", result.blockers)
        self.assertIsNone(result.session_pnl)
        # Supplying the original cumulative history restores only the genuine
        # arithmetic result; the omission never became a new empty baseline.
        restored = later(observation(), seconds=3)
        self.assertEqual(self.record(restored).session_pnl, D("99"))

    def test_blocked_missing_fee_can_recover_without_fabrication(self):
        result = self.record(observation(fees=()))
        self.assertIsNone(result.session_pnl)
        self.assertEqual(self.record(later(observation())).session_pnl, D("99"))

    def test_new_day_is_separate_not_same_day_reset(self):
        self.record(observation(marks=(mark("39"),)))
        next_start = START + timedelta(days=1)
        next_now = NOW + timedelta(days=1)
        start = baseline(frozen_at=next_start)
        value = observation(
            as_of=next_now, executions=(), fees=(), positions=(), marks=(),
            coverage=ExecutionCoverage(next_start, next_now, CONFIRMED, CONFIRMED, CONFIRMED, "synthetic-next-day"),
        )
        self.assertFalse(self.record(value, start).loss_latched)

    def test_unconfirmed_start_or_foreign_observation_never_freezes_baseline(self):
        for start, value in (
            (baseline(flat_start=EvidenceStatus.UNKNOWN), observation()),
            (baseline(), observation(account_binding="other")),
        ):
            with self.assertRaises(ShadowInputError):
                self.record(value, start)
        self.assertEqual(self.record().session_pnl, D("99"))

    def test_create_only_existing_wrong_store_and_symlink_fail_closed(self):
        with self.assertRaises(ShadowInputError):
            ShadowSessionStore(self.path, create=True)
        with self.assertRaises(ShadowInputError):
            ShadowSessionStore(self.path.with_name("missing.sqlite3"))
        bad = self.path.with_name("unrelated.sqlite3")
        connection = sqlite3.connect(bad)
        connection.execute("CREATE TABLE unrelated (value INTEGER)")
        connection.close()
        bad.chmod(0o600)
        with self.assertRaises(ShadowInputError):
            ShadowSessionStore(bad)
        link = self.path.with_name("link.sqlite3")
        link.symlink_to(self.path)
        with self.assertRaises(ShadowInputError):
            ShadowSessionStore(link)

    def test_transaction_rolls_back_rejected_baseline(self):
        self.record()
        with self.assertRaises(ShadowInputError):
            self.record(later(observation()), baseline(starting_nlv=D("20000")))
        self.assertEqual(self.record(later(observation())).session_pnl, D("99"))


if __name__ == "__main__":
    unittest.main()
