from __future__ import annotations

from datetime import datetime, timedelta
import math
from pathlib import Path
import unittest
from zoneinfo import ZoneInfo

from titan_brain.live.plans import ExpiringPlan
from titan_brain.live.policy import PolicyBundle
from titan_brain.live.risk_runtime import (
    AccountRiskSnapshot,
    RiskExposure,
    SessionLatch,
    evaluate_entry,
    update_session_latch,
)


ROOT = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 8, 10, 0, tzinfo=ET)


class RiskRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = PolicyBundle.load(ROOT)

    def plan(self, **overrides) -> ExpiringPlan:
        raw = dict(
            strategy_id=self.policy.strategy_id,
            policy_hash=self.policy.policy_hash,
            config_hash=self.policy.config_hash,
            account_last4="7153",
            symbol="XYZ",
            instrument_id="instrument-xyz",
            setup_id="ORB_BREAKOUT",
            quantity=2,
            entry_limit="12",
            structural_stop="11.50",
            targets=["13"],
            execution_reserve_per_share="0.05",
            market_hours="regular_hours",
            time_in_force="gfd",
            quality_tier="normal",
            completed_bar_end=NOW - timedelta(minutes=1),
            quote_observed_at=NOW - timedelta(seconds=1),
            created_at=NOW - timedelta(seconds=1),
            expires_at=NOW + timedelta(seconds=20),
            source_event_ids=["bar", "quote"],
        )
        raw.update(overrides)
        return ExpiringPlan.build(**raw)

    def snapshot(self, **overrides) -> AccountRiskSnapshot:
        raw = dict(
            account_last4="7153",
            observed_at=NOW - timedelta(seconds=1),
            usable_equity="1000",
            unleveraged_buying_power="1000",
            cash="1000",
            daily_realized_pnl="0",
            weekly_realized_pnl="0",
            peak_equity="1000",
            exposures=(),
            account_active=True,
            restricted=False,
            standard_orders_reconciled=True,
            option_orders_reconciled=True,
            advanced_orders_reconciled=True,
            positions_reconciled=True,
        )
        raw.update(overrides)
        return AccountRiskSnapshot.build(**raw)

    def test_clean_entry_uses_exact_account_wide_headroom(self) -> None:
        decision = evaluate_entry(
            policy=self.policy,
            snapshot=self.snapshot(),
            plan=self.plan(),
            latch=SessionLatch(NOW.date()),
            now=NOW,
        )
        self.assertTrue(decision.allowed, decision.failures)
        self.assertEqual(str(decision.proposal_planned_risk), "1.00")
        self.assertEqual(str(decision.remaining_buying_power), "976")

    def test_unknown_unprotected_and_incomplete_reconciliation_block(self) -> None:
        unknown = RiskExposure.build(
            reference="intent-unknown",
            category="unknown",
            planned_risk="3",
            stress_risk="4",
            execution_reserve="1",
            notional="50",
            protected=False,
        )
        decision = evaluate_entry(
            policy=self.policy,
            snapshot=self.snapshot(exposures=(unknown,), advanced_orders_reconciled=False),
            plan=self.plan(),
            latch=SessionLatch(NOW.date()),
            now=NOW,
        )
        self.assertIn("UNKNOWN_POSSIBLE_EXPOSURE", decision.failures)
        self.assertIn("WHOLE_BROKER_RECONCILIATION_INCOMPLETE", decision.failures)

    def test_daily_lock_is_irreversible_and_profit_floor_is_preserved(self) -> None:
        latch = update_session_latch(
            self.policy,
            SessionLatch(NOW.date()),
            realized_pnl="-100",
            usable_equity="10000",
            observed_at=NOW,
        )
        self.assertTrue(latch.loss_lock)
        recovered = update_session_latch(
            self.policy,
            latch,
            realized_pnl="10",
            usable_equity="10000",
            observed_at=NOW + timedelta(minutes=1),
        )
        self.assertTrue(recovered.loss_lock)
        goal = update_session_latch(
            self.policy,
            SessionLatch(NOW.date()),
            realized_pnl="150",
            usable_equity="1000",
            observed_at=NOW,
        )
        decision = evaluate_entry(
            policy=self.policy,
            snapshot=self.snapshot(daily_realized_pnl="125.50"),
            plan=self.plan(),
            latch=goal,
            now=NOW,
        )
        self.assertIn("POST_GOAL_125_FLOOR_NOT_PRESERVED", decision.failures)

    def test_funds_and_nonfinite_values_fail_closed(self) -> None:
        decision = evaluate_entry(
            policy=self.policy,
            snapshot=self.snapshot(unleveraged_buying_power="10", cash="10"),
            plan=self.plan(),
            latch=SessionLatch(NOW.date()),
            now=NOW,
        )
        self.assertIn("INSUFFICIENT_UNLEVERAGED_FUNDS", decision.failures)
        with self.assertRaisesRegex(ValueError, "finite"):
            self.snapshot(usable_equity=math.nan)


if __name__ == "__main__":
    unittest.main()
