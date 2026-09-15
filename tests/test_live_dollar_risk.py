from __future__ import annotations

import copy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from titan_brain.live.pipeline import FullLiveEntryPipeline
from titan_brain.live.policy import PolicyBundle
from titan_brain.live.risk_runtime import (
    RiskExposure, SessionLatch, entry_lifecycle_fee_reserve, evaluate_entry, update_session_latch,
)
from tests import test_live_pipeline as pipeline_fixtures
from tests import test_live_risk_runtime as risk_fixtures
from tests.live_dollar_policy_support import copy_dollar_policy_inputs


ROOT = Path(__file__).resolve().parents[1]
NOW = risk_fixtures.NOW


class DollarHeadroomRiskTests(unittest.TestCase):
    def setUp(self):
        self.policy = PolicyBundle.load(ROOT, config_relative="config/full_live_ibkr.json")
        self.fixture = risk_fixtures.RiskRuntimeTests()
        self.fixture.policy = self.policy

    def plan(self, **values):
        return self.fixture.plan(account_last4="3103", **values)

    def snapshot(self, **values):
        return self.fixture.snapshot(account_last4="3103", **values)

    def decision(self, *, plan=None, latch=None, **snapshot_values):
        return evaluate_entry(
            policy=self.policy, snapshot=self.snapshot(**snapshot_values),
            plan=plan or self.plan(), latch=latch or SessionLatch(NOW.date()), now=NOW,
        )

    def exposure(self, **overrides):
        values = dict(
            reference="existing", category="pending", planned_risk="10",
            execution_reserve="2", fee_reserve="3", stress_risk="15",
            notional="50", protected=True,
        )
        values.update(overrides)
        return RiskExposure.build(**values)

    def test_only_minus_100_latches_independently_of_equity_and_cannot_recover(self):
        for equity in ("100", "1000", "10000"):
            for pnl, locked in (("-60", False), ("-80", False), ("-99.99", False), ("-100", True)):
                with self.subTest(equity=equity, pnl=pnl):
                    latch = update_session_latch(
                        self.policy, SessionLatch(NOW.date()), realized_pnl=pnl,
                        usable_equity=equity, observed_at=NOW,
                    )
                    self.assertIs(latch.loss_lock, locked)
                    self.assertFalse(latch.hard_kill)
                    recovered = update_session_latch(
                        self.policy, latch, realized_pnl="200", usable_equity=equity,
                        observed_at=NOW + timedelta(seconds=1),
                    )
                    self.assertIs(recovered.loss_lock, locked)
        prior = SessionLatch(NOW.date(), hard_kill=True)
        self.assertTrue(update_session_latch(
            self.policy, prior, realized_pnl="0", usable_equity="1000", observed_at=NOW,
        ).hard_kill)

    def test_positive_realized_profit_increases_headroom_before_goal(self):
        plan = self.plan(quantity=75, entry_limit="6", structural_stop="5", targets=["7"])
        accepted = self.decision(plan=plan, daily_realized_pnl="100")
        self.assertTrue(accepted.allowed, accepted.failures)
        self.assertEqual(accepted.proposal_stress_risk, Decimal("155.75"))
        self.assertEqual(accepted.remaining_daily_headroom, Decimal("44.25"))
        self.assertFalse(self.decision(plan=plan, daily_realized_pnl="0").allowed)
        self.assertTrue(self.decision(plan=plan, daily_realized_pnl="55.75").allowed)
        rejected = self.decision(plan=plan, daily_realized_pnl="55.74")
        self.assertIn("INSUFFICIENT_DAILY_RISK_HEADROOM", rejected.failures)

    def test_existing_reserves_fees_and_new_fees_are_counted_exactly_once(self):
        decision = self.decision(daily_realized_pnl="-79.90", exposures=(self.exposure(),))
        self.assertTrue(decision.allowed, decision.failures)
        # Existing $10 downside + $2 reserve + $3 fee; new $1 + $0.10 + $4.
        for value in (decision.remaining_daily_headroom, decision.remaining_portfolio_headroom, decision.remaining_stress_headroom):
            self.assertEqual(value, Decimal("0"))
        self.assertEqual(decision.remaining_buying_power, Decimal("919"))
        self.assertFalse(self.decision(daily_realized_pnl="-79.91", exposures=(self.exposure(),)).allowed)
        self.assertFalse(self.decision(daily_realized_pnl="-99.99").allowed)

    def test_post_goal_floor_is_latched_and_covers_every_reserved_cost(self):
        latch = update_session_latch(
            self.policy, SessionLatch(NOW.date()), realized_pnl="150",
            usable_equity="1000", observed_at=NOW,
        )
        latch = update_session_latch(
            self.policy, latch, realized_pnl="145.10", usable_equity="1000",
            observed_at=NOW + timedelta(seconds=1),
        )
        self.assertTrue(latch.profit_goal_crossed)
        accepted = self.decision(latch=latch, daily_realized_pnl="145.10", exposures=(self.exposure(),))
        self.assertTrue(accepted.allowed, accepted.failures)
        self.assertEqual(accepted.remaining_portfolio_headroom, Decimal("0"))
        rejected = self.decision(latch=latch, daily_realized_pnl="145.09", exposures=(self.exposure(),))
        self.assertIn("POST_GOAL_125_FLOOR_NOT_PRESERVED", rejected.failures)

    def test_first_current_goal_crossing_limits_risk_before_latch_persistence(self):
        plan = self.plan(quantity=20)
        self.assertTrue(self.decision(plan=plan, daily_realized_pnl="149.99").allowed)
        decision = self.decision(plan=plan, daily_realized_pnl="150")
        self.assertEqual(decision.proposal_stress_risk, Decimal("33"))
        self.assertEqual(decision.remaining_portfolio_headroom, Decimal("-8"))
        self.assertIn("POST_GOAL_125_FLOOR_NOT_PRESERVED", decision.failures)

    def test_unknown_uncovered_funds_and_evidence_controls_remain(self):
        for overrides, failure in (
            ({"exposures": (self.exposure(category="unknown", protected=False),)}, "UNKNOWN_POSSIBLE_EXPOSURE"),
            ({"exposures": (self.exposure(category="open", protected=False),)}, "UNPROTECTED_OPEN_EXPOSURE"),
            ({"advanced_orders_reconciled": False}, "WHOLE_BROKER_RECONCILIATION_INCOMPLETE"),
            ({"cash": "27.99"}, "INSUFFICIENT_UNLEVERAGED_FUNDS"),
            ({"observed_at": NOW - timedelta(seconds=6)}, "BROKER_RISK_SNAPSHOT_STALE"),
        ):
            with self.subTest(failure=failure):
                self.assertIn(failure, self.decision(**overrides).failures)
        for field in ("daily_realized_pnl", "weekly_realized_pnl", "peak_equity"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.snapshot(**{field: None})

    def test_dollar_sizing_uses_realized_headroom_fees_and_whole_share_capacity(self):
        pipeline = object.__new__(FullLiveEntryPipeline)
        pipeline.policy = self.policy
        structure = pipeline_fixtures.structure()
        validation = pipeline_fixtures.validation_for(structure)
        instrument = pipeline_fixtures.StaticInstrumentProvider().get_instrument_evidence("XYZ", now=NOW)
        quote = pipeline_fixtures.cache_for("XYZ").quotes["XYZ"]
        for pnl, crossed, expected in (("100", False, 90), ("0", False, 61), ("-50", False, 30), ("150", True, 14), ("150", False, 14)):
            with self.subTest(pnl=pnl, crossed=crossed):
                plan, _snapshot, failures = pipeline._size_plan(
                    structure=structure, validation=validation, instrument=instrument,
                    quality_tier="normal", quote=quote, created_at=NOW,
                    expires_at=NOW + timedelta(seconds=20), source_event_ids=("test",),
                    snapshot=self.snapshot(daily_realized_pnl=pnl),
                    latch=SessionLatch(NOW.date(), profit_goal_crossed=crossed), now=NOW,
                )
                self.assertEqual(failures, ())
                self.assertIsNotNone(plan)
                self.assertEqual(plan.quantity, expected)

    def test_legacy_percentage_latches_and_caps_are_unchanged(self):
        legacy = PolicyBundle.load(ROOT)
        latch = update_session_latch(
            legacy, SessionLatch(NOW.date()), realized_pnl="-60",
            usable_equity="1000", observed_at=NOW,
        )
        self.assertTrue(latch.loss_lock)
        self.assertFalse(latch.hard_kill)
        self.assertTrue(update_session_latch(
            legacy, latch, realized_pnl="-80", usable_equity="1000", observed_at=NOW,
        ).hard_kill)
        self.assertEqual(legacy.risk_limits.normal_planned_risk_pct, .03)

    def test_dollar_fee_reserves_cannot_be_missing_or_below_approved_floors(self):
        for field, value in (
            ("minimum_commission_reserve_per_order_dollars", None),
            ("minimum_commission_reserve_per_order_dollars", ".99"),
            ("minimum_entry_lifecycle_fee_reserve_dollars", None),
            ("minimum_entry_lifecycle_fee_reserve_dollars", "1.99"),
        ):
            config = copy.deepcopy(self.policy.config)
            config["execution"][field] = value
            candidate = replace(self.policy, config=config)
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    candidate.validate()
                with self.assertRaises(ValueError):
                    entry_lifecycle_fee_reserve(candidate, quantity=1)
        config = copy.deepcopy(self.policy.config)
        config["execution"]["minimum_entry_lifecycle_fee_reserve_dollars"] = "5.00"
        self.assertEqual(
            entry_lifecycle_fee_reserve(replace(self.policy, config=config), quantity=1),
            Decimal("5.00"),
        )

    def test_approval_bytes_and_exact_contract_are_required_even_with_boolean_true(self):
        self.assertTrue(self.policy.risk_provenance_verified)
        self.assertFalse(self.policy.config["risk"]["limits_live_provenance_verified"])
        self.assertIn("LIVE_DRAWDOWN_REVIEW_POLICY_UNVERIFIED", self.policy.activation_blockers)
        for extra in ({"live_drawdown_review_pct": .20}, {"normal_planned_risk_pct": .03}, {"daily_realized_loss_lock_dollars": "101"}):
            raw = {**self.policy.risk_raw, **extra}
            config = copy.deepcopy(self.policy.config)
            config["risk"]["limits_live_provenance_verified"] = True
            candidate = replace(self.policy, risk_raw=raw, config=config)
            self.assertFalse(candidate.risk_provenance_verified)
            with self.assertRaisesRegex(ValueError, "exact approved contract"):
                candidate.validate()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = replace(self.policy, root=root)
            self.assertFalse(candidate.risk_provenance_verified)
            copy_dollar_policy_inputs(ROOT, root)
            self.assertTrue(candidate.risk_provenance_verified)
            approval = root / self.policy.config["owner_policy_approval"]["approval_record_path"]
            approval.write_text("corrupt approval", encoding="utf-8")
            self.assertFalse(candidate.risk_provenance_verified)
            with self.assertRaisesRegex(ValueError, "exact approved contract"):
                candidate.validate()


if __name__ == "__main__":
    unittest.main()
