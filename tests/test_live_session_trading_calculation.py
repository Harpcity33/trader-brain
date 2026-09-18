"""Synthetic finite-observation arithmetic; never live account evidence."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
import unittest

from titan_brain.live.broker.ibkr_session_inputs import (
    IbkrFiniteSessionFacts, SessionExecutionFact, SessionInputObservation,
    SessionOrderFact, SessionPositionFact,
)
from titan_brain.live.session_trading_calculation import (
    SessionBidMark, SessionTradingCalculationError, calculate_session_trading_pnl,
    session_baseline_evidence_sha256, validate_session_baseline_observations,
)
from titan_brain.live.session_trading_policy import (
    OWNER_AMENDMENT_PATH, OWNER_AMENDMENT_SHA256,
    SessionTradingBaseline, SessionTradingPolicy,
)


D = Decimal
ACCOUNT = "1" * 64
POLICY = SessionTradingPolicy("2" * 64, OWNER_AMENDMENT_PATH, OWNER_AMENDMENT_SHA256)
FROZEN = datetime(2026, 9, 18, 13, 35, tzinfo=timezone.utc)
NOW = FROZEN + timedelta(minutes=10)
FIRST, SECOND, CURRENT = "a" * 64, "b" * 64, "c" * 64
SOURCE_BLOCKERS = (
    "ALL_CLIENT_EXECUTION_COVERAGE_UNPROVEN",
    "CONTINUOUS_EVENT_COVERAGE_UNPROVEN", "EXTERNAL_ADJUSTMENT_RECONCILIATION_UNAVAILABLE",
)


def execution(exec_id="synthetic-fill-1", **changes):
    return replace(SessionExecutionFact(
        exec_id=exec_id, contract_id=123, symbol="TEST", security_type="STK", currency="USD",
        side="BUY", quantity=D(100), price=D(50),
        source_executed_at=FROZEN + timedelta(minutes=5), source_time_basis="PROVIDER_EXPLICIT_ZONE",
        received_at=NOW - timedelta(milliseconds=200), commission=D(1), commission_currency="USD",
        commission_received_at=NOW - timedelta(milliseconds=100),
    ), **changes)


def position(**changes):
    return replace(SessionPositionFact(123, "TEST", "STK", "USD", D(100), NOW - timedelta(milliseconds=200)), **changes)


def observation(*, at=NOW, collection_id=CURRENT, prior_id=SECOND, executions=None, positions=None, **changes):
    executions = (execution(),) if executions is None else executions
    positions = (position(),) if positions is None else positions
    facts = IbkrFiniteSessionFacts(
        generation=1, collection_id=collection_id,
        collection_started_at=at - timedelta(seconds=1), collection_completed_at=at,
        net_liquidation=D(10000), net_liquidation_currency="USD", net_liquidation_received_at=at - timedelta(milliseconds=500),
        positions=positions, executions=executions, orders=(),
        completed_reads=("account_updates_multi", "completed_orders", "executions", "open_orders", "positions"),
        commission_conflict_observed=False, orphan_commission_report_count=0,
        account_values_source="IBKR_ACCOUNT_UPDATES_MULTI_V1",
    )
    value = SessionInputObservation(ACCOUNT, facts, 19735, prior_id, prior_id is not None, False, SOURCE_BLOCKERS)
    return replace(value, **changes)


def baseline_pair():
    return (
        observation(at=FROZEN - timedelta(seconds=2), collection_id=FIRST, prior_id=None, executions=(), positions=()),
        observation(at=FROZEN, collection_id=SECOND, prior_id=FIRST, executions=(), positions=()),
    )


def baseline(pair=None, **changes):
    first, second = baseline_pair() if pair is None else pair
    value = SessionTradingBaseline(
        policy_sha256=POLICY.policy_sha256, account_binding_sha256=ACCOUNT,
        evidence_sha256=session_baseline_evidence_sha256(first, second), frozen_at=second.facts.collection_completed_at,
        starting_nlv=second.facts.net_liquidation, flat_start=True, pre_entry=True, initial_exposure_reconciled=True,
    )
    return replace(value, **changes)


def mark(**changes):
    return replace(SessionBidMark(ACCOUNT, 123, "USD", D(51), 100, NOW - timedelta(seconds=1), NOW, "3" * 64), **changes)


class SessionTradingCalculationTests(unittest.TestCase):
    def calculate(self, value=None, *, pair=None, start=None, marks=None, now=NOW, prior=()):
        pair = baseline_pair() if pair is None else pair
        return calculate_session_trading_pnl(
            POLICY, baseline(pair) if start is None else start,
            baseline_observations=pair, observation=observation() if value is None else value,
            bid_marks=(mark(),) if marks is None else marks, now=now, prior_observations=prior,
        )

    def assert_blocked(self, reason, value=None, **kwargs):
        result = self.calculate(value, **kwargs)
        self.assertIn(reason, result.material_blockers)
        self.assertIsNone(result.calculated_pnl)
        self.assertIsNone(result.performance_fraction)
        self.assertFalse(result.to_incomplete_measurement().complete)
        return result

    def test_open_long_formula_uses_signed_actual_fees_once(self):
        result = self.calculate()
        self.assertEqual(result.calculated_pnl, D(99))
        self.assertEqual(result.performance_fraction, D("0.0099"))
        self.assertEqual(result.material_blockers, ())
        self.assertEqual(result.source_blockers, SOURCE_BLOCKERS)
        rebate = self.calculate(observation(executions=(execution(commission=D("-0.25")),)))
        self.assertEqual(rebate.calculated_pnl, D("100.25"))

    def test_pre_entry_receipts_do_not_require_unprovided_economic_timestamp(self):
        first, second = baseline_pair()
        validate_session_baseline_observations(first, second, now=FROZEN)
        result = self.calculate()
        self.assertNotIn("ACCOUNT_VALUES_ECONOMIC_TIME_UNAVAILABLE", result.blockers)
        self.assertIn("ALL_CLIENT_EXECUTION_COVERAGE_UNPROVEN", result.blockers)
        self.assertIn("CONTINUOUS_EVENT_COVERAGE_UNPROVEN", result.blockers)
        self.assertFalse(result.to_incomplete_measurement().complete)
        with self.assertRaises(SessionTradingCalculationError):
            validate_session_baseline_observations(first, second, now=FROZEN + timedelta(seconds=6))
        foreign_source = replace(second, facts=replace(second.facts, account_values_source="IBKR_ACCOUNT_SUMMARY_V1"))
        with self.assertRaises(SessionTradingCalculationError):
            validate_session_baseline_observations(first, foreign_source, now=FROZEN)

    def test_partial_fills_partial_exit_and_identical_replay_dedup(self):
        a = execution("a", quantity=D(60))
        b = execution("b", quantity=D(40), price=D(52), source_executed_at=FROZEN + timedelta(minutes=6), commission=D("0.5"))
        c = execution("c", quantity=D(30), side="SELL", price=D(55), source_executed_at=FROZEN + timedelta(minutes=7), commission=D("0.5"))
        value = observation(executions=(a, b, c, replace(a, price=D("50.000"), commission=D("1.00"))), positions=(position(quantity=D(70)),))
        result = self.calculate(value, marks=(mark(bid=D(54)),))
        self.assertEqual(result.calculated_pnl, D(348))

    def test_closed_round_trip_needs_no_quote(self):
        sell = execution("sell", side="SELL", price=D(52), source_executed_at=FROZEN + timedelta(minutes=6))
        value = observation(executions=(execution(), sell), positions=())
        self.assertEqual(self.calculate(value, marks=()).calculated_pnl, D(198))

    def test_empty_observed_ledger_zero_is_never_complete_session_zero(self):
        result = self.calculate(observation(executions=(), positions=()), marks=())
        self.assertEqual(result.calculated_pnl, D(0))
        self.assertIsNone(result.to_incomplete_measurement().session_pnl)
        self.assertFalse(result.to_incomplete_measurement().complete)
        self.assertFalse(result.public_dict()["full_session_pnl_established"])
        self.assertEqual(result.public_dict()["measurement_scope"], "OBSERVED_FINITE_RECORD_ARITHMETIC_ONLY")

    def test_removing_caller_blockers_cannot_grant_authoritative_coverage(self):
        pair = tuple(replace(item, blockers=()) for item in baseline_pair())
        result = self.calculate(observation(blockers=()), pair=pair)
        self.assertEqual(result.calculated_pnl, D(99))
        self.assertEqual(result.source_blockers, SOURCE_BLOCKERS)
        self.assertFalse(result.live_authority)
        self.assertTrue(result.diagnostic_only)
        self.assertFalse(result.to_incomplete_measurement().complete)
        self.assertIsNone(result.to_incomplete_measurement().session_pnl)

    def test_result_contains_no_account_id_or_promoted_shadow_authority(self):
        result = self.calculate()
        public = result.public_dict()
        self.assertNotIn(ACCOUNT, str(public))
        self.assertFalse(public["loss_latch_persisted"])
        self.assertFalse(public["unobserved_breach_exclusion_established"])
        self.assertEqual(result.as_of, NOW)
        self.assertEqual(public["timing_basis"], "LOCAL_CALCULATION_TIME_NOT_ATOMIC_PROVIDER_SNAPSHOT")

    def test_missing_fee_is_invalid_not_zero(self):
        for missing in (None, D("NaN"), D("Infinity")):
            with self.subTest(missing=missing), self.assertRaises(SessionTradingCalculationError):
                self.calculate(observation(executions=(execution(commission=missing),)))
        self.assertEqual(self.calculate(observation(executions=(execution(commission=D(0)),))).calculated_pnl, D(100))

    def test_actual_fee_currency_and_provider_timestamp_basis_required(self):
        for currency in ("", "BASE", "CAD"):
            self.assert_blocked("ACTUAL_COMMISSION_CURRENCY_UNPROVEN", observation(executions=(execution(commission_currency=currency),)))
        for basis, time_value in (("ABSENT", None), ("CONFIGURED_SESSION_ZONE_INTERPRETATION", FROZEN + timedelta(minutes=5))):
            self.assert_blocked("EXECUTION_TIME_PROVENANCE_UNRESOLVED", observation(executions=(execution(source_time_basis=basis, source_executed_at=time_value),)))

    def test_prior_session_prebaseline_and_future_fills_rejected(self):
        for stamp in (FROZEN - timedelta(days=1), FROZEN - timedelta(seconds=1), NOW + timedelta(seconds=1)):
            self.assert_blocked("EXECUTION_OUTSIDE_BASELINE_SESSION", observation(executions=(execution(source_executed_at=stamp),)))

    def test_execution_cannot_follow_its_receipts(self):
        self.assert_blocked("EXECUTION_PROVIDER_TIME_FOLLOWS_RECEIPT", observation(executions=(execution(source_executed_at=NOW),)))

    def test_positions_must_match_fills_and_contract_facts(self):
        self.assert_blocked("POSITIONS_DO_NOT_MATCH_SESSION_EXECUTIONS", observation(positions=()))
        self.assert_blocked("POSITIONS_DO_NOT_MATCH_SESSION_EXECUTIONS", observation(positions=(position(contract_id=456),)))
        self.assert_blocked("CONTRACT_FACTS_CONFLICT", observation(positions=(position(symbol="OTHER"),)))
        self.assert_blocked("POSITION_IDENTITY_DUPLICATE", observation(positions=(position(), position())))

    def test_non_usd_fractional_short_and_nonstock_scope_rejected(self):
        for row in (execution(currency="CAD"), execution(quantity=D("1.5")), execution(security_type="OPT")):
            self.assert_blocked("EXECUTION_SCOPE_INVALID", observation(executions=(row,)))
        for row in (position(currency="CAD"), position(quantity=D("1.5")), position(quantity=D(-1)), position(security_type="OPT")):
            self.assert_blocked("POSITION_SCOPE_INVALID", observation(positions=(row,)))
        self.assert_blocked("SHORT_OR_EXECUTION_CHRONOLOGY_UNRESOLVED", observation(executions=(execution(side="SELL"),)))

    def test_same_contract_fresh_usd_bid_required_for_remaining_long(self):
        self.assert_blocked("RESIDUAL_POSITION_BID_MISSING", marks=())
        self.assert_blocked("RESIDUAL_POSITION_BID_MISSING", marks=(mark(contract_id=999),))
        self.assert_blocked("BID_MARK_SCOPE_MISMATCH", marks=(mark(currency="CAD"),))
        self.assert_blocked("BID_MARK_SCOPE_MISMATCH", marks=(mark(account_binding_sha256="9" * 64),))
        self.assert_blocked("RESIDUAL_POSITION_BID_SIZE_INSUFFICIENT", marks=(mark(bid_size=99),))

    def test_stale_future_and_conflicting_bid_marks_block(self):
        self.assert_blocked("BID_MARK_STALE_OR_FUTURE", marks=(mark(quoted_at=NOW - timedelta(seconds=6)),))
        self.assert_blocked("BID_MARK_STALE_OR_FUTURE", marks=(mark(received_at=NOW + timedelta(seconds=1)),))
        self.assert_blocked("BID_MARK_STALE_OR_FUTURE", marks=(mark(quoted_at=NOW + timedelta(seconds=1)),))
        self.assert_blocked("BID_MARK_CONFLICT", marks=(mark(), mark(bid=D(52))))

    def test_malformed_bid_inputs_raise_static_errors(self):
        for change in (dict(bid=D("NaN")), dict(bid=D(0)), dict(bid_size=True), dict(contract_id=0), dict(quoted_at=NOW.replace(tzinfo=None))):
            with self.subTest(change=change), self.assertRaises(SessionTradingCalculationError):
                mark(**change)

    def test_conflicting_duplicates_or_native_corrections_never_double_count(self):
        self.assert_blocked("EXECUTION_REPLAY_CONFLICT", observation(executions=(execution(), execution(price=D(49)))))
        self.assert_blocked("EXECUTION_CORRECTION_UNRESOLVED", observation(executions=(execution("abc.def.ghi.01"), execution("abc.def.ghi.02")), positions=(position(quantity=D(200)),)), marks=(mark(bid_size=200),))

    def test_cumulative_history_omission_and_fee_changes_are_detected(self):
        prior_at = NOW - timedelta(seconds=2)
        earlier = execution(received_at=prior_at - timedelta(milliseconds=200), commission_received_at=prior_at - timedelta(milliseconds=100))
        prior = observation(at=prior_at, collection_id="d" * 64, executions=(earlier,), positions=(position(received_at=prior_at - timedelta(milliseconds=200)),))
        current = observation(prior_id=prior.facts.collection_id)
        self.assertEqual(self.calculate(current, prior=(prior,)).calculated_pnl, D(99))
        omitted = replace(current, facts=replace(current.facts, executions=(), positions=()))
        self.assert_blocked("EXECUTION_HISTORY_OMITTED", omitted, prior=(prior,), marks=())
        changed = replace(current, facts=replace(current.facts, executions=(execution(commission=D(2)),)))
        self.assert_blocked("EXECUTION_HISTORY_CONFLICT", changed, prior=(prior,))

    def test_account_generation_client_and_lineage_cannot_change(self):
        self.assert_blocked("ACCOUNT_BINDING_MISMATCH", observation(account_binding_fingerprint="9" * 64))
        value = observation()
        self.assert_blocked("READ_GENERATION_CHANGED", replace(value, facts=replace(value.facts, generation=2)))
        self.assert_blocked("READ_CLIENT_CHANGED", replace(value, read_client_id=0))
        self.assert_blocked("OBSERVATION_LINEAGE_MISMATCH", replace(value, prior_collection_id=None))
        self.assert_blocked("COLLECTION_REPLAY_COLLISION", replace(value, facts=replace(value.facts, collection_id=SECOND)))

    def test_current_collection_staleness_or_cross_day_blocks(self):
        self.assert_blocked("CURRENT_OBSERVATION_STALE_OR_FUTURE", now=NOW + timedelta(seconds=6))
        self.assert_blocked("CURRENT_OBSERVATION_STALE_OR_FUTURE", now=NOW - timedelta(seconds=1))
        self.assert_blocked("SESSION_DAY_MISMATCH", now=NOW + timedelta(days=1))

    def test_sticky_gap_unknown_order_or_extra_adapter_blockers_block_arithmetic(self):
        self.assert_blocked("READ_GAP_REQUIRES_RECONCILIATION", observation(sticky_read_gap=True))
        self.assert_blocked("INPUT_ADAPTER_REPORTED_BLOCKERS", observation(blockers=(*SOURCE_BLOCKERS, "UNEXPLAINED_MOVEMENT")))
        value = observation()
        unknown = SessionOrderFact("ibkr:7:91", 123, "standard_equity", False, "UNKNOWN", False)
        self.assert_blocked("ORDER_STATE_UNKNOWN", replace(value, facts=replace(value.facts, orders=(unknown,))))

    def test_missing_finite_end_or_commission_conflict_blocks(self):
        value = observation()
        self.assert_blocked("FINITE_CALLBACK_SET_INCOMPLETE", replace(value, facts=replace(value.facts, completed_reads=("positions",))))
        self.assert_blocked("COMMISSION_RECONCILIATION_UNRESOLVED", replace(value, facts=replace(value.facts, commission_conflict_observed=True)))

    def test_baseline_boolean_claims_alone_are_not_accepted(self):
        pair = baseline_pair()
        wrong = replace(pair[1], facts=replace(pair[1].facts, positions=(position(received_at=FROZEN),)))
        pair = (pair[0], wrong)
        self.assert_blocked("BASELINE_FLAT_PRE_ENTRY_NOT_OBSERVED", pair=pair, start=baseline(pair))

    def test_baseline_policy_evidence_amount_and_exact_freeze_time_are_bound(self):
        for reason, change in (
            ("BASELINE_POLICY_BINDING_MISMATCH", dict(policy_sha256="9" * 64)),
            ("BASELINE_EVIDENCE_BINDING_MISMATCH", dict(evidence_sha256="9" * 64)),
            ("BASELINE_AMOUNT_OR_FREEZE_TIME_MISMATCH", dict(starting_nlv=D(10001))),
            ("BASELINE_AMOUNT_OR_FREEZE_TIME_MISMATCH", dict(frozen_at=FROZEN + timedelta(seconds=1))),
            ("BASELINE_PRECONDITIONS_UNPROVEN", dict(flat_start=False)),
        ):
            self.assert_blocked(reason, start=baseline(**change))

    def test_deposit_or_nlv_increase_does_not_change_denominator_or_metric(self):
        value = observation()
        result = self.calculate(replace(value, facts=replace(value.facts, net_liquidation=D(1000000))))
        self.assertEqual(result.starting_nlv, D(10000))
        self.assertEqual(result.calculated_pnl, D(99))
        self.assertEqual(result.performance_fraction, D("0.0099"))
        self.assertIn("EXTERNAL_ADJUSTMENT_RECONCILIATION_UNAVAILABLE", result.blockers)

    def test_decimal_context_does_not_round_cash_arithmetic_or_change_evidence_hash(self):
        usual = self.calculate()
        with localcontext() as context:
            context.prec = 2
            result = self.calculate()
        self.assertEqual(result.calculated_pnl, D(99))
        self.assertEqual(result.evidence_sha256, usual.evidence_sha256)

    def test_observed_loss_boundary_is_exact_not_a_persisted_latch(self):
        self.assertTrue(self.calculate(marks=(mark(bid=D("40.01")),)).loss_boundary_observed)
        self.assertFalse(self.calculate(marks=(mark(bid=D("40.02")),)).loss_boundary_observed)
        self.assertFalse(self.calculate().loss_boundary_observed)

    def test_prewrite_baseline_validator_accepts_source_limits_but_not_material_gaps(self):
        pair = baseline_pair()
        validate_session_baseline_observations(*pair, now=FROZEN)
        for second in (
            replace(pair[1], sticky_read_gap=True),
            replace(pair[1], prior_collection_id=None),
            replace(pair[1], facts=replace(pair[1].facts, net_liquidation_currency="BASE")),
            replace(pair[1], facts=replace(pair[1].facts, net_liquidation=D(9999))),
            replace(pair[1], facts=replace(pair[1].facts, executions=(execution(),))),
            replace(pair[1], facts=replace(pair[1].facts, positions=(position(),))),
        ):
            with self.subTest(second=second), self.assertRaises(SessionTradingCalculationError):
                validate_session_baseline_observations(pair[0], second, now=FROZEN)
        with self.assertRaises(SessionTradingCalculationError):
            validate_session_baseline_observations(*pair, now=FROZEN + timedelta(seconds=6))

    def test_observation_type_and_nonfinite_financial_values_fail_closed(self):
        with self.assertRaises(SessionTradingCalculationError):
            self.calculate(value=object())
        value = observation()
        with self.assertRaises(SessionTradingCalculationError):
            self.calculate(replace(value, facts=replace(value.facts, net_liquidation=D("NaN"))))
        with self.assertRaises(SessionTradingCalculationError):
            self.calculate(observation(executions=(execution(price=D("Infinity")),)))

    def test_real_adapter_output_shapes_work_offline_without_pnl_or_orders(self):
        from tests.test_live_ibkr_session_inputs import IbkrSessionInputsTests

        fixture = IbkrSessionInputsTests()
        fixture.setUp()
        try:
            fixture.connect()
            first, second = fixture.adapter.capture(), fixture.adapter.capture()
            validate_session_baseline_observations(first, second, now=fixture.now)
            start = SessionTradingBaseline(
                POLICY.policy_sha256, fixture.runtime.account_binding_fingerprint,
                session_baseline_evidence_sha256(first, second), second.facts.collection_completed_at,
                second.facts.net_liquidation, True, True, True,
            )
            current = fixture.adapter.capture()
            result = calculate_session_trading_pnl(POLICY, start, baseline_observations=(first, second), observation=current, now=fixture.now)
            self.assertEqual(result.calculated_pnl, D(0))
            self.assertFalse(result.to_incomplete_measurement().complete)
            self.assertFalse(fixture.runtime.status().command_connected)
            self.assertNotIn("reqPnL", [call[0] for call in fixture.client.calls])
        finally:
            fixture.doCleanups()


if __name__ == "__main__":
    unittest.main()
