from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from titan_brain.models import Instrument, Session  # noqa: E402
from titan_brain.risk import (  # noqa: E402
    RiskAmounts,
    RiskContext,
    RiskLimits,
    assess_new_trade,
    calculate_debit_spread_risk,
    calculate_equity_risk,
    calculate_long_option_risk,
)


CONFIG = Path(__file__).resolve().parents[1] / "config" / "risk_limits.json"


class RiskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.limits = RiskLimits.from_json(CONFIG)

    def test_smaller_percentage_or_absolute_ceiling_controls(self) -> None:
        self.assertEqual(
            self.limits.cap_dollars(
                1_000.0,
                "daily_new_entry_lock_pct",
                "daily_new_entry_lock_dollars",
            ),
            60.0,
        )
        self.assertEqual(
            self.limits.cap_dollars(
                10_000.0,
                "daily_new_entry_lock_pct",
                "daily_new_entry_lock_dollars",
            ),
            100.0,
        )

    def test_equity_risk_and_remaining_headroom(self) -> None:
        amounts = calculate_equity_risk(
            shares=10,
            entry_price=10.0,
            structural_stop=9.0,
            liquidity_slippage_reserve_per_share=0.10,
        )
        decision = assess_new_trade(
            limits=self.limits,
            context=RiskContext(
                current_usable_equity=1_000.0,
                daily_realized_pnl=0.0,
                weekly_realized_pnl=0.0,
                peak_equity=1_000.0,
                open_planned_risk_dollars=5.0,
                pending_planned_risk_dollars=2.0,
                existing_execution_reserve_dollars=1.0,
            ),
            amounts=amounts,
            session=Session.REGULAR,
            instrument=Instrument.STOCK,
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.planned_risk_dollars, 10.0)
        self.assertEqual(decision.stress_risk_dollars, 11.0)
        self.assertAlmostEqual(decision.planned_risk_pct, 0.01)
        self.assertEqual(decision.remaining_daily_risk, 41.0)
        self.assertEqual(decision.remaining_portfolio_risk, 31.0)
        self.assertEqual(decision.remaining_portfolio_stress_risk, 39.0)

    def test_total_open_stress_capacity_fails_closed(self) -> None:
        decision = assess_new_trade(
            limits=self.limits,
            context=RiskContext(
                current_usable_equity=1_000.0,
                daily_realized_pnl=0.0,
                weekly_realized_pnl=0.0,
                peak_equity=1_000.0,
                open_stress_risk_dollars=45.0,
            ),
            amounts=RiskAmounts(3.0, 6.0, 1.0),
            session=Session.REGULAR,
            instrument=Instrument.STOCK,
        )
        self.assertIn("INSUFFICIENT_PORTFOLIO_STRESS_HEADROOM", decision.failures)

    def test_premarket_planned_and_stress_caps_are_deterministic(self) -> None:
        amounts = RiskAmounts(
            planned_risk_dollars=21.0,
            stress_risk_dollars=41.0,
            execution_reserve_dollars=20.0,
        )
        decision = assess_new_trade(
            limits=self.limits,
            context=RiskContext(
                current_usable_equity=1_000.0,
                daily_realized_pnl=0.0,
                weekly_realized_pnl=0.0,
                peak_equity=1_000.0,
            ),
            amounts=amounts,
            session=Session.PREMARKET,
            instrument=Instrument.STOCK,
        )
        self.assertFalse(decision.allowed)
        self.assertIn("PLANNED_RISK_CAP_EXCEEDED", decision.failures)
        self.assertIn("STRESS_RISK_CAP_EXCEEDED", decision.failures)

    def test_premarket_requires_positive_reserve_inside_stress(self) -> None:
        amounts = RiskAmounts(
            planned_risk_dollars=10.0,
            stress_risk_dollars=10.0,
            execution_reserve_dollars=1.0,
        )
        decision = assess_new_trade(
            limits=self.limits,
            context=RiskContext(
                current_usable_equity=1_000.0,
                daily_realized_pnl=0.0,
                weekly_realized_pnl=0.0,
                peak_equity=1_000.0,
            ),
            amounts=amounts,
            session=Session.PREMARKET,
            instrument=Instrument.STOCK,
        )
        self.assertIn("PREMARKET_STRESS_RESERVE_MISSING", decision.failures)

    def test_long_option_stress_is_full_premium(self) -> None:
        amounts = calculate_long_option_risk(
            contracts=1,
            premium_per_share=2.0,
            tactical_loss_dollars=80.0,
            execution_reserve_dollars=5.0,
        )
        self.assertEqual(amounts.stress_risk_dollars, 200.0)
        decision = assess_new_trade(
            limits=self.limits,
            context=RiskContext(
                current_usable_equity=10_000.0,
                daily_realized_pnl=0.0,
                weekly_realized_pnl=0.0,
                peak_equity=10_000.0,
            ),
            amounts=amounts,
            session=Session.REGULAR,
            instrument=Instrument.LONG_CALL,
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.stress_risk_pct, 0.02)

    def test_understated_long_option_stress_fails_closed(self) -> None:
        amounts = RiskAmounts(
            planned_risk_dollars=80.0,
            stress_risk_dollars=100.0,
            execution_reserve_dollars=5.0,
            premium_paid_dollars=200.0,
        )
        decision = assess_new_trade(
            limits=self.limits,
            context=RiskContext(
                current_usable_equity=10_000.0,
                daily_realized_pnl=0.0,
                weekly_realized_pnl=0.0,
                peak_equity=10_000.0,
            ),
            amounts=amounts,
            session=Session.REGULAR,
            instrument=Instrument.LONG_PUT,
        )
        self.assertIn("FULL_PREMIUM_STRESS_NOT_COVERED", decision.failures)

    def test_debit_spread_requires_positive_multileg_eligibility(self) -> None:
        amounts = calculate_debit_spread_risk(
            contracts=1,
            net_debit_per_share=0.75,
            tactical_loss_dollars=40.0,
            execution_reserve_dollars=2.0,
        )
        decision = assess_new_trade(
            limits=self.limits,
            context=RiskContext(
                current_usable_equity=5_000.0,
                daily_realized_pnl=0.0,
                weekly_realized_pnl=0.0,
                peak_equity=5_000.0,
            ),
            amounts=amounts,
            session=Session.REGULAR,
            instrument=Instrument.DEBIT_SPREAD,
            multileg_eligible=False,
        )
        self.assertIn("MULTILEG_ELIGIBILITY_UNVERIFIED", decision.failures)

    def test_daily_lock_and_drawdown_review_block_new_entry(self) -> None:
        decision = assess_new_trade(
            limits=self.limits,
            context=RiskContext(
                current_usable_equity=1_000.0,
                daily_realized_pnl=-60.0,
                weekly_realized_pnl=0.0,
                peak_equity=1_300.0,
            ),
            amounts=RiskAmounts(10.0, 11.0, 1.0),
            session=Session.REGULAR,
            instrument=Instrument.STOCK,
        )
        self.assertIn("DAILY_NEW_ENTRY_LOCK", decision.failures)
        self.assertIn("LIVE_DRAWDOWN_REVIEW_REQUIRED", decision.failures)

    def test_missing_broker_or_ledger_loss_evidence_fails_closed(self) -> None:
        decision = assess_new_trade(
            limits=self.limits,
            context=RiskContext(current_usable_equity=1_000.0),
            amounts=RiskAmounts(3.0, 4.0, 1.0),
            session=Session.REGULAR,
            instrument=Instrument.STOCK,
        )
        self.assertFalse(decision.allowed)
        self.assertIn("DAILY_REALIZED_PNL_UNAVAILABLE", decision.failures)
        self.assertIn("WEEKLY_REALIZED_PNL_UNAVAILABLE", decision.failures)
        self.assertIn("PEAK_EQUITY_OR_DRAWDOWN_UNAVAILABLE", decision.failures)


if __name__ == "__main__":
    unittest.main()
