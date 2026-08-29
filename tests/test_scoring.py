from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from titan_brain.models import HardGateEvidence, SetupID  # noqa: E402
from titan_brain.scoring import (  # noqa: E402
    BASELINE_SETUP_WEIGHTS,
    EQUITY_EXECUTION_WEIGHTS,
    qualify_candidate,
    score_execution,
    score_setup,
)


def gates(**overrides: bool) -> HardGateEvidence:
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


class ScoringTests(unittest.TestCase):
    def test_setup_score_is_weighted_zero_to_one_hundred(self) -> None:
        components = {key: 80.0 for key in BASELINE_SETUP_WEIGHTS}
        components["liquidity"] = 100.0
        result = score_setup(components)
        self.assertEqual(result.score, 83.0)

    def test_repository_baseline_weights_are_preserved(self) -> None:
        self.assertEqual(
            BASELINE_SETUP_WEIGHTS,
            {
                "liquidity": 0.15,
                "relative_volume": 0.15,
                "technical_structure_vwap": 0.20,
                "catalyst_context": 0.15,
                "sector_market_sympathy": 0.10,
                "prior_90_day_behavior": 0.15,
                "gap_behavior": 0.05,
                "other_massive_data": 0.05,
            },
        )

    def test_execution_score_is_independent(self) -> None:
        result = score_execution(
            {key: 70.0 for key in EQUITY_EXECUTION_WEIGHTS},
            instrument_kind="equity",
        )
        self.assertEqual(result.score, 70.0)

    def test_high_scores_never_override_stale_data_gate(self) -> None:
        result = qualify_candidate(
            setup_id=SetupID.FIRST_PULLBACK,
            setup_score=100.0,
            execution_score=100.0,
            hard_gates=gates(fresh_data=False),
            minimum_setup_score=70.0,
            minimum_execution_score=65.0,
        )
        self.assertFalse(result.live_qualified)
        self.assertFalse(result.hard_gates_passed)
        self.assertIn("STALE_DATA", result.rejection_reasons)

    def test_scores_below_floor_are_explicit_rejections(self) -> None:
        result = qualify_candidate(
            setup_id="VWAP_RECLAIM",
            setup_score=69.0,
            execution_score=64.0,
            hard_gates=gates(),
            minimum_setup_score=70.0,
            minimum_execution_score=65.0,
        )
        self.assertFalse(result.live_qualified)
        self.assertEqual(
            result.rejection_reasons,
            ("SETUP_SCORE_BELOW_MINIMUM", "EXECUTION_SCORE_BELOW_MINIMUM"),
        )

    def test_generic_momentum_setup_id_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            qualify_candidate(
                setup_id="MOMENTUM",
                setup_score=90,
                execution_score=90,
                hard_gates=gates(),
                minimum_setup_score=70,
                minimum_execution_score=65,
            )


if __name__ == "__main__":
    unittest.main()
