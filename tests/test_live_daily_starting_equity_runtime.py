from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from titan_brain.live.pipeline import FullLiveEntryPipeline
from titan_brain.live.policy import PolicyBundle
from titan_brain.live.risk_runtime import (
    SessionLatch, RiskExposure, daily_starting_equity_capacity,
    daily_starting_equity_performance, evaluate_entry, update_session_latch,
)
from titan_brain.live.service import FullLiveService
from titan_brain.live.state import LiveStateStore
from tests import test_live_risk_runtime as fixtures
from tests import test_live_pipeline as pipeline_fixtures

ROOT = Path(__file__).resolve().parents[1]
NOW = fixtures.NOW


class DailyStartingEquityRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.policy = PolicyBundle.load(ROOT, config_relative="config/full_live_ibkr.json")
        self.fixture = fixtures.RiskRuntimeTests()
        self.fixture.policy = self.policy

    def plan(self, **kwargs):
        return self.fixture.plan(account_last4="3103", **kwargs)

    def snapshot(self, **kwargs):
        values = dict(account_last4="3103", usable_equity="2000", total_equity="2000",
                      peak_equity="3000", daily_starting_equity="2000",
                      daily_external_cash_flow="0")
        values.update(kwargs)
        return self.fixture.snapshot(**values)

    def latch(self, total="2000", *, start="2000", flow="0", prior=None, realized="0"):
        return update_session_latch(
            self.policy, prior or SessionLatch(NOW.date()), realized_pnl=realized,
            usable_equity=total, total_equity=total, daily_starting_equity=start,
            daily_external_cash_flow=flow, observed_at=NOW,
        )

    def decision(self, *, plan=None, latch=None, **kwargs):
        return evaluate_entry(policy=self.policy, snapshot=self.snapshot(**kwargs),
                              plan=plan or self.plan(), latch=latch or SessionLatch(NOW.date()),
                              now=NOW)

    def test_loss_boundary_includes_open_loss_even_with_profitable_realized_pnl(self):
        for total, locked in (("1800.01", False), ("1800", True), ("1799.99", True), ("0", True)):
            with self.subTest(total=total):
                actual = self.latch(total, realized="500")
                self.assertEqual(actual.loss_lock, locked)
                self.assertEqual(actual.hard_kill, locked)

    def test_loss_is_irreversible_on_price_recovery_or_deposit(self):
        breached = self.latch("1800")
        recovered = self.latch("2600", prior=breached)
        self.assertTrue(recovered.loss_lock)
        self.assertTrue(recovered.hard_kill)
        self.assertTrue(self.latch("4000", flow="2000", prior=breached).hard_kill)

    def test_fifteen_percent_uses_fixed_start_not_current_balance(self):
        for total, crossed in (("2299.99", False), ("2300", True), ("2500", True)):
            with self.subTest(total=total):
                self.assertEqual(self.latch(total).profit_goal_crossed, crossed)
        self.assertFalse(self.latch("2150", realized="150").profit_goal_crossed)
        self.assertFalse(self.latch("1900", realized="-100").loss_lock)

    def test_external_cash_movements_are_not_daily_profit_or_loss(self):
        for total, flow in (("3000", "1000"), ("1000", "-1000")):
            with self.subTest(total=total):
                actual = self.latch(total, flow=flow)
                self.assertFalse(actual.loss_lock)
                self.assertFalse(actual.profit_goal_crossed)
                self.assertEqual(daily_starting_equity_performance(
                    starting_equity="2000", total_equity=total, external_cash_flow=flow
                ), Decimal("0"))

    def test_cash_movement_cannot_hide_loss(self):
        self.assertTrue(self.latch("2800", flow="1000").hard_kill)
        self.assertTrue(self.latch("800", flow="-1000").hard_kill)

    def test_profits_do_not_expand_original_daily_budget(self):
        for current, expected in (("1900", "100"), ("2000", "200"), ("2400", "200")):
            self.assertEqual(daily_starting_equity_capacity(
                self.policy, starting_equity="2000", total_equity=current, external_cash_flow="0"
            ), Decimal(expected))

    def test_old_hundred_dollar_cap_is_not_retained(self):
        plan = self.plan(quantity=75, entry_limit="6", structural_stop="5", targets=["7"])
        result = self.decision(plan=plan)
        self.assertTrue(result.allowed, result.failures)
        self.assertEqual(result.proposal_stress_risk, Decimal("155.75"))
        self.assertEqual(result.remaining_daily_headroom, Decimal("44.25"))

    def test_goal_does_not_impose_old_post_goal_floor(self):
        crossed = self.latch("2300")
        result = self.decision(latch=crossed, total_equity="2100", usable_equity="2100",
                               daily_realized_pnl="0")
        self.assertTrue(result.allowed, result.failures)
        self.assertNotIn("POST_GOAL_125_FLOOR_NOT_PRESERVED", result.failures)

    def test_target_does_not_override_loss_lock(self):
        prior = self.latch("2300")
        breached = self.latch("1800", prior=prior)
        self.assertTrue(breached.profit_goal_crossed)
        result = self.decision(latch=breached, total_equity="1800")
        self.assertIn("HARD_DAILY_LOSS_KILL", result.failures)

    def test_missing_baseline_flow_or_total_cannot_be_zero_defaulted(self):
        for field in ("daily_starting_equity", "daily_external_cash_flow", "total_equity"):
            result = self.decision(**{field: None})
            self.assertFalse(result.allowed)
            self.assertIn("DAILY_STARTING_EQUITY_EVIDENCE_INCOMPLETE", result.failures)

    def test_nonfinite_zero_and_negative_baseline_rejected(self):
        for start in (None, "0", "-1", "NaN", "Infinity", "-Infinity"):
            with self.subTest(start=start), self.assertRaises((TypeError, ValueError)):
                self.latch(start=start)
        for flow in (None, "NaN", "Infinity"):
            with self.subTest(flow=flow), self.assertRaises((TypeError, ValueError)):
                self.latch(flow=flow)

    def test_equality_breaches_without_rounding_up_allowance(self):
        self.assertFalse(self.latch("9.0046", start="10.005").hard_kill)
        self.assertTrue(self.latch("9.0045", start="10.005").hard_kill)

    def test_every_pending_reserve_and_candidate_fee_counts_once(self):
        exposure = RiskExposure.build(reference="existing", category="pending",
            planned_risk="10", execution_reserve="2", fee_reserve="3", stress_risk="15",
            notional="50", protected=True)
        result = self.decision(total_equity="1820.10", exposures=(exposure,))
        self.assertTrue(result.allowed, result.failures)
        self.assertEqual(result.remaining_daily_headroom, Decimal("0"))
        self.assertFalse(self.decision(total_equity="1820.09", exposures=(exposure,)).allowed)

    def test_unknown_unprotected_and_unleveraged_cash_controls_retained(self):
        unknown = RiskExposure.build(reference="unknown", category="unknown",
            planned_risk="0", execution_reserve="0", fee_reserve="0", stress_risk="0",
            notional="0", protected=False)
        self.assertIn("UNKNOWN_POSSIBLE_EXPOSURE", self.decision(exposures=(unknown,)).failures)
        self.assertIn("UNPROTECTED_OPEN_EXPOSURE",
                      self.decision(exposures=(replace(unknown, category="open"),)).failures)
        self.assertIn("INSUFFICIENT_UNLEVERAGED_FUNDS", self.decision(cash="27.99").failures)

    def test_unpersisted_current_loss_already_blocks_entry(self):
        result = self.decision(total_equity="1800")
        self.assertIn("DAILY_STARTING_EQUITY_LOSS_LIMIT_REACHED", result.failures)

    def test_open_profit_cannot_fund_new_risk_using_original_entry_to_stop(self):
        open_position = RiskExposure.build(reference="open", category="open",
            planned_risk="50", execution_reserve="0", fee_reserve="0", stress_risk="50",
            notional="500", protected=True)
        # The original $50 stop risk omits $100 of open profit giveback.
        # Allowing another trade from current NLV would understate the floor risk.
        result = self.decision(daily_starting_equity="1000", total_equity="1020",
                               daily_realized_pnl="-80", exposures=(open_position,))
        self.assertFalse(result.allowed)
        self.assertIn("DAILY_EQUITY_OPEN_RISK_REVALUATION_REQUIRED", result.failures)

    def test_stale_account_and_wrong_date_still_block(self):
        self.assertIn("BROKER_RISK_SNAPSHOT_STALE",
                      self.decision(observed_at=NOW-timedelta(seconds=6)).failures)
        self.assertIn("SESSION_LATCH_DATE_MISMATCH",
                      self.decision(latch=SessionLatch((NOW-timedelta(days=1)).date())).failures)

    def test_percentage_sizer_uses_fixed_baseline_not_old_dollar_floor(self):
        pipeline = object.__new__(FullLiveEntryPipeline)
        pipeline.policy = self.policy
        structure = pipeline_fixtures.structure()
        validation = pipeline_fixtures.validation_for(structure)
        instrument = pipeline_fixtures.StaticInstrumentProvider().get_instrument_evidence("XYZ", now=NOW)
        quote = pipeline_fixtures.cache_for("XYZ").quotes["XYZ"]
        quantities = []
        for total in ("2000", "2100", "1900"):
            plan, _, failures = pipeline._size_plan(
                structure=structure, validation=validation, instrument=instrument,
                quality_tier="normal", quote=quote, created_at=NOW,
                expires_at=NOW+timedelta(seconds=20), source_event_ids=("test",),
                snapshot=self.snapshot(total_equity=total),
                latch=SessionLatch(NOW.date(), profit_goal_crossed=True), now=NOW)
            self.assertEqual(failures, ())
            self.assertIsNotNone(plan)
            quantities.append(plan.quantity)
        self.assertEqual(quantities[0], quantities[1])
        self.assertGreater(quantities[0], quantities[2])

    def test_service_persists_hard_closeout_latch_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            state = LiveStateStore(path)
            state.initialize_runtime(runtime_id=self.policy.runtime_id,
                account_key=self.policy.account_key, release_manifest_hash="a"*64,
                config_hash=self.policy.config_hash, policy_hash=self.policy.policy_hash,
                initialized_at=NOW)
            service = object.__new__(FullLiveService)
            service.policy = self.policy
            service.state = state
            service.account_key = self.policy.account_key
            # This unit tests service routing after authenticated broker admission.
            snapshot = SimpleNamespace(daily_realized_pnl_ready=True,
                daily_realized_pnl=Decimal("0"), daily_starting_equity_ready=True,
                entry_risk_evidence_ready=True, funds=SimpleNamespace(total_value=Decimal("1800")),
                observed_at=NOW,
                daily_starting_equity_as_of=datetime.combine(NOW.date(), datetime.min.time(), NOW.tzinfo),
                daily_starting_equity=Decimal("2000"), daily_external_cash_flow=Decimal("0"))
            valid_start = snapshot.daily_starting_equity_as_of
            for field, value in (
                ("observed_at", NOW - timedelta(seconds=6)),
                ("observed_at", NOW + timedelta(seconds=1)),
                ("daily_starting_equity_as_of", valid_start - timedelta(days=1)),
                ("daily_starting_equity_as_of", valid_start + timedelta(minutes=1)),
            ):
                original = getattr(snapshot, field)
                setattr(snapshot, field, value)
                with self.subTest(field=field, value=value):
                    self.assertIsNone(service._update_risk_latch(snapshot, NOW))
                    self.assertEqual(state.rows("SELECT * FROM session_latches"), [])
                setattr(snapshot, field, original)
            with patch.object(state, "apply_session_latch", return_value=False):
                with self.assertRaisesRegex(RuntimeError, "RISK_LATCH_PERSISTENCE_NOT_CONFIRMED"):
                    service._update_risk_latch(snapshot, NOW)
            self.assertTrue(service._update_risk_latch(snapshot, NOW).hard_kill)
            row = state.rows("SELECT * FROM session_latches")[0]
            self.assertEqual(row["pause_new_entries"], 1)
            self.assertEqual(row["closeout_started"], 1)
            state.close()
            service.state = LiveStateStore(path)
            try:
                snapshot.funds.total_value = Decimal("2500")
                self.assertTrue(service._update_risk_latch(snapshot, NOW+timedelta(seconds=1)).hard_kill)
                snapshot.daily_starting_equity_ready = False
                self.assertTrue(service._update_risk_latch(snapshot, NOW+timedelta(seconds=2)).hard_kill)
            finally:
                service.state.close()


if __name__ == "__main__":
    unittest.main()
