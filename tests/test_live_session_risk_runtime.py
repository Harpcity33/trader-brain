"""Offline session-risk consumer: synthetic facts never grant live authority."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal, localcontext
import unittest

from titan_brain.live.plans import ExpiringPlan
from titan_brain.live.policy import PolicyBundle
from titan_brain.live.risk_runtime import (
    RiskExposure, SessionAccountRiskSnapshot, SessionLatch, entry_lifecycle_fee_reserve,
    evaluate_entry, minimum_remaining_position_fee_reserve, update_session_latch,
)
from titan_brain.live.session_trading_policy import begin_observation, fail_observation
from tests import test_live_risk_runtime as legacy_fixtures
from tests import test_live_session_policy_bundle as bundle_fixtures
from tests import test_live_session_trading_policy as session_fixtures


D = Decimal
NOW = session_fixtures.START + timedelta(minutes=30)


class SessionRiskRuntimeTests(unittest.TestCase):
    def setUp(self):
        fixture = bundle_fixtures.SessionPolicyBundleTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.config["execution"]["production_account_binding_fingerprint"] = "a" * 64
        self.policy = fixture.load()
        self.initial = session_fixtures.initial(starting_nlv=D("2000"))
        self.assertEqual(self.initial.baseline.policy_sha256, self.policy.risk_hash)

    def state(self, pnl="0", *, state=None, at=NOW, token="risk-read"):
        return session_fixtures.observe(self.initial if state is None else state, pnl, at=at, token=token)

    def snapshot(self, **changes):
        raw = dict(
            account_last4=self.policy.account_last4, account_binding_sha256="a" * 64,
            observed_at=NOW, usable_equity="2000", unleveraged_buying_power="2000", cash="2000",
            exposures=(), account_active=True, restricted=False, standard_orders_reconciled=True,
            option_orders_reconciled=True, advanced_orders_reconciled=True, positions_reconciled=True,
            session_state=self.state(),
        )
        raw.update(changes)
        return SessionAccountRiskSnapshot.build(**raw)

    def plan(self, *, policy=None, **changes):
        policy = policy or self.policy
        raw = dict(
            strategy_id=policy.strategy_id, policy_hash=policy.policy_hash, config_hash=policy.config_hash,
            account_last4=policy.account_last4, symbol="XYZ", instrument_id="synthetic-xyz",
            setup_id="ORB_BREAKOUT", quantity=2, entry_limit="12", structural_stop="11.50",
            targets=["13"], execution_reserve_per_share="0.05", market_hours="regular_hours",
            time_in_force="gfd", quality_tier="normal", completed_bar_end=NOW-timedelta(minutes=1),
            quote_observed_at=NOW-timedelta(seconds=1), created_at=NOW-timedelta(seconds=1),
            expires_at=NOW+timedelta(seconds=20), source_event_ids=["synthetic-bar", "synthetic-quote"],
        )
        raw.update(changes)
        return ExpiringPlan.build(**raw)

    def evaluate(self, snapshot=None, **changes):
        raw = dict(policy=self.policy, snapshot=snapshot or self.snapshot(), plan=self.plan(),
                   latch=SessionLatch(NOW.date()), now=NOW)
        raw.update(changes)
        return evaluate_entry(**raw)

    def exposure(self, **changes):
        raw = dict(reference="synthetic-pending", category="pending", planned_risk="10", stress_risk="15",
                   execution_reserve="2", fee_reserve="3", notional="100", protected=True)
        raw.update(changes)
        return RiskExposure.build(**raw)

    def test_distinct_exact_budget_with_no_legacy_fields_or_live_authority(self):
        snapshot = self.snapshot()
        result = self.evaluate(snapshot)
        self.assertTrue(result.allowed, result.failures)
        self.assertEqual(result.proposal_planned_risk, D("1"))
        self.assertEqual(result.proposal_reserve, D("0.10"))
        self.assertEqual(result.proposal_stress_risk, D("5.10"))
        self.assertEqual(result.remaining_daily_headroom, D("194.90"))
        self.assertEqual(result.remaining_portfolio_headroom, D("194.90"))
        self.assertEqual(result.remaining_stress_headroom, D("194.90"))
        self.assertEqual(result.remaining_buying_power, D("1972"))
        self.assertEqual(result.remaining_cash_headroom, D("1972"))
        self.assertFalse(snapshot.live_authority)
        self.assertFalse(result.live_authority)
        self.assertFalse(self.policy.account_day_headroom_risk)
        self.assertIn("SESSION_TRADING_RUNTIME_INTEGRATION_UNAVAILABLE", self.policy.activation_blockers)
        for field in ("daily_realized_pnl", "weekly_realized_pnl", "peak_equity",
                      "daily_starting_equity", "daily_external_cash_flow"):
            self.assertFalse(hasattr(snapshot, field))

    def test_all_existing_pending_risk_and_fees_are_charged_at_exact_boundary(self):
        for pnl, allowed, remaining in (("-179.90", True, "0"), ("-179.91", False, "-0.01")):
            with self.subTest(pnl=pnl):
                snapshot = self.snapshot(session_state=self.state(pnl), exposures=(self.exposure(),))
                result = self.evaluate(snapshot)
                self.assertEqual(result.allowed, allowed, result.failures)
                self.assertEqual(result.remaining_daily_headroom, D(remaining))
                self.assertEqual(result.remaining_cash_headroom, D("1869"))
                if not allowed:
                    self.assertIn("INSUFFICIENT_PORTFOLIO_STRESS_HEADROOM", result.failures)

    def test_incurred_fees_already_in_net_measurement_are_not_subtracted_again(self):
        result = self.evaluate(self.snapshot(session_state=self.state("-2")))
        self.assertTrue(result.allowed, result.failures)
        self.assertEqual(result.remaining_daily_headroom, D("192.90"))

    def test_profit_and_deposit_cannot_raise_pre_entry_denominator(self):
        for pnl, funds in (("1000", "3000"), ("0", "900000")):
            with self.subTest(pnl=pnl):
                snapshot = self.snapshot(session_state=self.state(pnl), usable_equity=funds, cash=funds,
                                         unleveraged_buying_power=funds)
                result = self.evaluate(snapshot)
                self.assertTrue(result.allowed, result.failures)
                self.assertEqual(result.remaining_daily_headroom, D("194.90"))
                self.assertEqual(snapshot.session_state.baseline.starting_nlv, D("2000"))

    def test_profit_aspiration_has_no_post_goal_floor_or_legacy_profit_overlay(self):
        at = NOW+timedelta(seconds=1)
        state = self.state("400")
        state = self.state("0", state=state, at=at, token="after-aspiration")
        latch = SessionLatch(NOW.date(), profit_goal_crossed=True, highest_realized_pnl=D("1000000"))
        result = self.evaluate(self.snapshot(session_state=state, observed_at=at), now=at, latch=latch)
        self.assertTrue(state.profit_aspiration_observed)
        self.assertTrue(result.allowed, result.failures)
        self.assertEqual(result.remaining_daily_headroom, D("194.90"))

    def test_observed_loss_and_old_safety_latches_do_not_clear_on_recovery(self):
        at = NOW+timedelta(seconds=1)
        state = self.state("-200")
        state = self.state("1000", state=state, at=at, token="after-loss")
        result = self.evaluate(self.snapshot(session_state=state, observed_at=at), now=at)
        self.assertFalse(result.allowed)
        self.assertIn("SESSION_LOSS_LATCHED", result.failures)
        for flag, code in (("loss_lock", "IRREVERSIBLE_DAILY_NEW_ENTRY_LOCK"),
                           ("hard_kill", "HARD_DAILY_LOSS_KILL")):
            result = self.evaluate(latch=replace(SessionLatch(NOW.date()), **{flag: True}))
            self.assertFalse(result.allowed)
            self.assertIn(code, result.failures)

    def test_missing_pending_failed_and_gap_never_turn_into_zero_pnl(self):
        pending = begin_observation(self.state(), token="interrupted", now=NOW)
        candidates = (self.initial, pending, fail_observation(pending, token="interrupted"),
                      fail_observation(pending, token="interrupted", gap=True))
        for state in candidates:
            with self.subTest(state=state.last_observation_token):
                result = self.evaluate(self.snapshot(session_state=state))
                self.assertFalse(result.allowed)
                self.assertTrue(set(result.failures) & {"SESSION_MEASUREMENT_MISSING", "UNRESOLVED_OBSERVATION_INCIDENT"})
        recovered = self.state(state=candidates[-1], token="later-health")
        self.assertIn("UNRESOLVED_OBSERVATION_INCIDENT", self.evaluate(self.snapshot(session_state=recovered)).failures)

    def test_measurement_staleness_and_clock_rollback_reject(self):
        for at in (NOW-timedelta(seconds=6), NOW+timedelta(seconds=1)):
            result = self.evaluate(self.snapshot(session_state=self.state(at=at)))
            self.assertFalse(result.allowed)
            self.assertIn("SESSION_MEASUREMENT_STALE", result.failures)

    def test_wrong_or_missing_state_and_legacy_snapshot_cannot_fall_through(self):
        for state in (None, object(), {"session_pnl": "0"}):
            with self.subTest(state_type=type(state).__name__):
                with self.assertRaisesRegex(ValueError, "SESSION_RISK_STATE_REQUIRED"):
                    self.snapshot(session_state=state)
                snapshot = self.snapshot()
                object.__setattr__(snapshot, "session_state", state)
                self.assertEqual(self.evaluate(snapshot).failures, ("SESSION_RISK_STATE_REQUIRED",))
        fixture = legacy_fixtures.RiskRuntimeTests()
        fixture.setUp()
        result = self.evaluate(fixture.snapshot())
        self.assertEqual(result.failures, ("SESSION_RISK_SNAPSHOT_REQUIRED",))

    def test_legacy_policy_does_not_reinterpret_new_snapshot_or_latch_column(self):
        legacy = PolicyBundle.load(bundle_fixtures.ROOT)
        with self.assertRaisesRegex(ValueError, "SESSION_RISK_POLICY_MODEL_MISMATCH"):
            self.evaluate(policy=legacy)
        with self.assertRaisesRegex(ValueError, "SESSION_RISK_REQUIRES_SEPARATE_DURABLE_STATE"):
            update_session_latch(self.policy, SessionLatch(NOW.date()), realized_pnl="0",
                                 usable_equity="2000", observed_at=NOW)

    def test_account_policy_and_plan_bindings_remain_required(self):
        for change, code in (({"account_last4": "1234"}, "ACCOUNT_POLICY_MISMATCH"),
                             ({"observed_at": NOW-timedelta(seconds=6)}, "BROKER_RISK_SNAPSHOT_STALE"),
                             ({"observed_at": NOW+timedelta(seconds=1)}, "BROKER_RISK_SNAPSHOT_STALE")):
            result = self.evaluate(self.snapshot(**change))
            self.assertFalse(result.allowed)
            self.assertIn(code, result.failures)
        wrong_initial = replace(self.initial, baseline=replace(self.initial.baseline, policy_sha256="f" * 64))
        result = self.evaluate(self.snapshot(session_state=self.state(state=wrong_initial)))
        self.assertIn("SESSION_RISK_POLICY_BINDING_MISMATCH", result.failures)
        result = self.evaluate(plan=self.plan(policy_hash="f" * 64))
        self.assertIn("PLAN_POLICY_BINDING_MISMATCH", result.failures)
        config = deepcopy(self.policy.config)
        config["execution"].pop("production_account_binding_fingerprint")
        self.assertIn("ACCOUNT_POLICY_MISMATCH", self.evaluate(policy=replace(self.policy, config=config)).failures)
        with self.assertRaisesRegex(ValueError, "SESSION_RISK_ACCOUNT_BINDING_MISMATCH"):
            self.snapshot(account_binding_sha256="f" * 64)

    def test_common_account_and_reconciliation_gates_remain(self):
        cases = [("account_active", False, "ACCOUNT_INACTIVE_OR_RESTRICTED"),
                 ("restricted", True, "ACCOUNT_INACTIVE_OR_RESTRICTED"),
                 ("usable_equity", "0", "USABLE_EQUITY_NOT_POSITIVE")]
        cases += [(field, False, "WHOLE_BROKER_RECONCILIATION_INCOMPLETE") for field in (
            "standard_orders_reconciled", "option_orders_reconciled", "advanced_orders_reconciled", "positions_reconciled")]
        for field, value, code in cases:
            with self.subTest(field=field):
                result = self.evaluate(self.snapshot(**{field: value}))
                self.assertFalse(result.allowed)
                self.assertIn(code, result.failures)
        result = self.evaluate(latch=SessionLatch(NOW.date()-timedelta(days=1)))
        self.assertIn("SESSION_LATCH_DATE_MISMATCH", result.failures)

    def test_funds_charge_notional_and_future_fees_separately(self):
        for field in ("cash", "unleveraged_buying_power"):
            for value, allowed in (("28", True), ("27.99", False)):
                with self.subTest(field=field, value=value):
                    result = self.evaluate(self.snapshot(**{field: value}))
                    self.assertEqual(result.allowed, allowed, result.failures)
                    if not allowed:
                        self.assertIn("INSUFFICIENT_UNLEVERAGED_FUNDS", result.failures)

    def test_unknown_and_generic_open_exposure_do_not_claim_remaining_risk_proof(self):
        for category in ("unknown", "open", "manual"):
            for protected in (False, True):
                result = self.evaluate(self.snapshot(exposures=(self.exposure(category=category, protected=protected),)))
                self.assertFalse(result.allowed)
                self.assertIn("UNKNOWN_POSSIBLE_EXPOSURE" if category == "unknown" else
                              "SESSION_OPEN_RISK_REVALUATION_REQUIRED", result.failures)

    def test_existing_exposure_reserves_cannot_default_to_zero(self):
        for change in ({"execution_reserve": "0", "stress_risk": "13"},
                       {"fee_reserve": "0", "stress_risk": "12"}):
            result = self.evaluate(self.snapshot(exposures=(self.exposure(**change),)))
            self.assertFalse(result.allowed)
            self.assertIn("EXISTING_EXPOSURE_POSITIVE_RESERVES_REQUIRED", result.failures)

    def test_snapshot_strict_revalidation_rejects_malformed_reconstructed_inputs(self):
        for field, value in (("cash", D("NaN")), ("cash", 2.0), ("cash", D("-1")),
                             ("cash", D("1E999")), ("cash", D("1E-13")),
                             ("account_active", 1), ("observed_at", NOW.replace(tzinfo=None))):
            snapshot = self.snapshot()
            object.__setattr__(snapshot, field, value)
            self.assertEqual(self.evaluate(snapshot).failures, ("SESSION_RISK_SNAPSHOT_INVALID",))
        exposure = self.exposure()
        for exposures in ((exposure, exposure), (replace(exposure, stress_risk=D("1")),),
                          (replace(exposure, protected=1),), (replace(exposure, fee_reserve=1.0),)):
            with self.subTest(exposures=exposures), self.assertRaises(ValueError):
                self.snapshot(exposures=exposures)

    def test_missing_invalid_or_below_approved_fee_policy_never_defaults_zero(self):
        for field, value in (("minimum_commission_reserve_per_order_dollars", None),
                             ("minimum_commission_reserve_per_order_dollars", "0"),
                             ("minimum_commission_reserve_per_order_dollars", "-1"),
                             ("minimum_commission_reserve_per_order_dollars", "0.99"),
                             ("minimum_entry_lifecycle_fee_reserve_dollars", None),
                             ("minimum_entry_lifecycle_fee_reserve_dollars", "1.99")):
            with self.subTest(field=field, value=value):
                config = deepcopy(self.policy.config)
                config["execution"][field] = value
                policy = replace(self.policy, config=config)
                result = self.evaluate(policy=policy)
                self.assertFalse(result.allowed)
                self.assertIn("SESSION_RISK_FEE_RESERVE_UNAVAILABLE", result.failures)
                with self.assertRaises(ValueError):
                    entry_lifecycle_fee_reserve(policy, quantity=2)
                if field == "minimum_commission_reserve_per_order_dollars" and value in (None, "0", "-1"):
                    with self.assertRaises(ValueError):
                        minimum_remaining_position_fee_reserve(policy, working_stop_order_count=1)

    def test_session_exact_arithmetic_ignores_ambient_low_decimal_precision(self):
        snapshot = self.snapshot(session_state=self.state("-179.90"), exposures=(self.exposure(),))
        plan = self.plan()
        expected = self.evaluate(snapshot, plan=plan)
        with localcontext() as context:
            context.prec = 2
            actual = self.evaluate(snapshot, plan=plan)
            self.assertEqual(entry_lifecycle_fee_reserve(self.policy, quantity=111), D("113"))
            self.assertEqual(minimum_remaining_position_fee_reserve(self.policy, working_stop_order_count=111), D("112"))
        self.assertEqual(actual, expected)
        self.assertTrue(actual.allowed)

    def test_plan_shape_expiry_positive_reserve_and_session_lane_are_not_relaxed(self):
        for change in ({"execution_reserve_per_share": "0"}, {"entry_limit": "5"},
                       {"expires_at": NOW-timedelta(microseconds=1)}, {"market_hours": "extended_hours"}):
            result = self.evaluate(plan=self.plan(**change))
            self.assertFalse(result.allowed)
            self.assertIn("SESSION_RISK_PLAN_INVALID", result.failures)
        malformed = replace(self.plan(), quantity=True)
        self.assertIn("SESSION_RISK_PLAN_INVALID", self.evaluate(plan=malformed).failures)


if __name__ == "__main__":
    unittest.main()
