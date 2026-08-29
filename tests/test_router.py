from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from titan_brain.models import (  # noqa: E402
    HardGateEvidence,
    Instrument,
    RouteInput,
)
from titan_brain.router import select_instrument  # noqa: E402


def passing_gates(**overrides: bool) -> HardGateEvidence:
    values = {
        "fresh_data": True,
        "tradable": True,
        "sufficient_liquidity": True,
        "acceptable_spread": True,
        "broker_state_known": True,
        "account_eligible": True,
        "session_order_valid": True,
        "risk_within_limits": True,
    }
    values.update(overrides)
    return HardGateEvidence(**values)


class RouterTests(unittest.TestCase):
    def test_selects_strongest_conservative_expectancy_per_stress_risk(self) -> None:
        stock = RouteInput(
            instrument=Instrument.STOCK,
            feasible=True,
            setup_score=85,
            execution_score=85,
            target_probability=0.55,
            loss_probability=0.45,
            target_profit_dollars=100,
            loss_if_wrong_dollars=50,
            stress_loss_dollars=50,
            spread_cost_dollars=4,
            slippage_cost_dollars=6,
        )
        call = RouteInput(
            instrument=Instrument.LONG_CALL,
            feasible=True,
            setup_score=85,
            execution_score=75,
            target_probability=0.55,
            loss_probability=0.45,
            target_profit_dollars=200,
            loss_if_wrong_dollars=100,
            stress_loss_dollars=150,
            premium_paid_dollars=150,
            spread_cost_dollars=10,
            slippage_cost_dollars=5,
            theta_cost_dollars=5,
            iv_change_cost_dollars=5,
            uncertainty_reserve_dollars=5,
        )
        decision = select_instrument(
            [stock, call],
            hard_gates=passing_gates(),
            minimum_setup_score=70,
            minimum_execution_score=65,
        )
        self.assertEqual(decision.selected_instrument, Instrument.STOCK)
        self.assertEqual(len(decision.evaluations), 2)
        self.assertGreater(
            decision.evaluations[0].net_expectancy_r,
            decision.evaluations[1].net_expectancy_r,
        )

    def test_missing_full_premium_evidence_rejects_long_option(self) -> None:
        call = RouteInput(
            instrument=Instrument.LONG_CALL,
            feasible=True,
            setup_score=90,
            execution_score=90,
            target_probability=0.7,
            loss_probability=0.3,
            target_profit_dollars=250,
            loss_if_wrong_dollars=80,
            stress_loss_dollars=100,
        )
        decision = select_instrument(
            [call],
            hard_gates=passing_gates(),
            minimum_setup_score=70,
            minimum_execution_score=65,
        )
        self.assertEqual(decision.selected_instrument, Instrument.NO_TRADE)
        self.assertIn(
            "FULL_PREMIUM_STRESS_EVIDENCE_MISSING",
            decision.evaluations[0].rejection_reasons,
        )

    def test_hard_gate_failure_forces_no_trade_despite_expectancy(self) -> None:
        stock = RouteInput(
            instrument=Instrument.STOCK,
            feasible=True,
            setup_score=99,
            execution_score=99,
            target_probability=0.8,
            loss_probability=0.2,
            target_profit_dollars=200,
            loss_if_wrong_dollars=20,
            stress_loss_dollars=25,
        )
        decision = select_instrument(
            [stock],
            hard_gates=passing_gates(broker_state_known=False),
            minimum_setup_score=70,
            minimum_execution_score=65,
        )
        self.assertEqual(decision.selected_instrument, Instrument.NO_TRADE)
        self.assertIn(
            "UNKNOWN_BROKER_STATE", decision.evaluations[0].rejection_reasons
        )

    def test_all_routes_retained_for_shadow_comparison(self) -> None:
        infeasible_put = RouteInput(
            instrument=Instrument.LONG_PUT,
            feasible=False,
            setup_score=80,
            execution_score=80,
            target_probability=0.5,
            loss_probability=0.5,
            target_profit_dollars=100,
            loss_if_wrong_dollars=50,
            stress_loss_dollars=100,
            premium_paid_dollars=100,
        )
        decision = select_instrument(
            [infeasible_put],
            hard_gates=passing_gates(),
            minimum_setup_score=70,
            minimum_execution_score=65,
        )
        self.assertEqual(len(decision.evaluations), 1)
        self.assertIn("ROUTE_INFEASIBLE", decision.evaluations[0].rejection_reasons)


if __name__ == "__main__":
    unittest.main()
