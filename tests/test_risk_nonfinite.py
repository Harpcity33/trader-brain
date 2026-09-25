from __future__ import annotations

import math
import unittest

from titan_brain.models import Instrument, Session
from titan_brain.risk import (
    RiskAmounts,
    RiskContext,
    RiskLimits,
    assess_new_trade,
    calculate_equity_risk,
)


def limits() -> RiskLimits:
    return RiskLimits.from_mapping(
        {
            "normal_planned_risk_pct": 0.03,
            "a_plus_planned_risk_pct": 0.04,
            "premarket_planned_risk_pct": 0.02,
            "premarket_stress_risk_pct": 0.04,
            "max_single_trade_stress_risk_pct": 0.04,
            "max_total_open_planned_risk_pct": 0.05,
            "max_total_open_stress_risk_pct": 0.08,
            "daily_new_entry_lock_pct": 0.06,
            "hard_daily_loss_kill_pct": 0.08,
            "weekly_loss_lock_pct": 0.12,
            "live_drawdown_review_pct": 0.20,
            "absolute_dollar_ceilings": {},
        }
    )


class NonFiniteRiskTests(unittest.TestCase):
    def test_context_rejects_nonfinite_account_values(self) -> None:
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "finite"):
                RiskContext(current_usable_equity=value)

    def test_context_rejects_nonfinite_pnl_and_reservations(self) -> None:
        with self.assertRaisesRegex(ValueError, "finite"):
            RiskContext(current_usable_equity=1000, daily_realized_pnl=math.nan)
        with self.assertRaisesRegex(ValueError, "finite"):
            RiskContext(current_usable_equity=1000, pending_planned_risk_dollars=math.inf)

    def test_calculation_rejects_nonfinite_market_inputs(self) -> None:
        with self.assertRaisesRegex(ValueError, "finite"):
            calculate_equity_risk(
                shares=1,
                entry_price=math.nan,
                structural_stop=9,
                liquidity_slippage_reserve_per_share=0.10,
            )

    def test_assessment_rejects_nonfinite_proposal(self) -> None:
        context = RiskContext(
            current_usable_equity=1000,
            daily_realized_pnl=0,
            weekly_realized_pnl=0,
            peak_equity=1000,
        )
        with self.assertRaisesRegex(ValueError, "finite"):
            assess_new_trade(
                limits=limits(),
                context=context,
                amounts=RiskAmounts(math.nan, math.nan, math.nan),
                session=Session.REGULAR,
                instrument=Instrument.STOCK,
            )


if __name__ == "__main__":
    unittest.main()
