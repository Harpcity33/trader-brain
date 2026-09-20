"""Tests for thesis + joint scenario generation (milestone 4)."""

from __future__ import annotations

from datetime import datetime, timezone
import unittest

from titan_brain.option_scenario_analysis import (
    JointScenarioInput,
    OptionThesis,
    ScenarioError,
    generate_scenarios,
)
from titan_brain.option_trade_analysis import analyze_option_trade


LATEST_EXIT = datetime(2026, 10, 14, 16, 0, tzinfo=timezone.utc)


def _thesis(**overrides):
    fields = dict(
        underlying_symbol="TEST",
        direction="long_call_thesis",
        invalidation_underlying="95",
        latest_exit=LATEST_EXIT,
        thesis_note="hold above 95; exit before expiry",
    )
    fields.update(overrides)
    return OptionThesis(**fields)


def _inputs():
    return [
        JointScenarioInput(name="planned_target", kind="planned", scenario_underlying="105",
                           days_remaining=3, implied_vol_percent="40",
                           estimated_mid="1.60", liquidation_spread="0.10", note="thesis works"),
        JointScenarioInput(name="stress_gap_down", kind="stress", scenario_underlying="92",
                           days_remaining=3, implied_vol_percent="30",
                           estimated_mid="0.20", liquidation_spread="0.10", note="overnight gap + IV crush"),
        JointScenarioInput(name="no_move_decay", kind="upside", scenario_underlying="100",
                           days_remaining=2, implied_vol_percent="35",
                           executable_bid="0.90", note="time decay only"),
    ]


class ScenarioGenerationTests(unittest.TestCase):
    def test_generates_analyzer_ready_scenarios(self) -> None:
        result = generate_scenarios(_thesis(), _inputs())
        self.assertEqual(len(result.scenarios), 3)
        kinds = {s["kind"] for s in result.scenarios}
        self.assertTrue({"planned", "stress"}.issubset(kinds))
        for s in result.scenarios:
            self.assertEqual(set(s), {"name", "kind", "exit_bid", "assumptions"})
            self.assertIn("ASSUMPTION_NOT_PROBABILITY", s["assumptions"])

    def test_conservative_exit_bid_is_mid_minus_half_spread(self) -> None:
        result = generate_scenarios(_thesis(), _inputs())
        planned = next(s for s in result.scenarios if s["name"] == "planned_target")
        # mid 1.60 - spread 0.10/2 = 1.55
        self.assertEqual(planned["exit_bid"], "1.55")

    def test_supplied_executable_bid_used_directly(self) -> None:
        result = generate_scenarios(_thesis(), _inputs())
        decay = next(s for s in result.scenarios if s["name"] == "no_move_decay")
        self.assertEqual(decay["exit_bid"], "0.9")

    def test_exit_bid_floored_at_zero(self) -> None:
        result = generate_scenarios(_thesis(), [
            JointScenarioInput(name="p", kind="planned", scenario_underlying="105", days_remaining=3,
                               implied_vol_percent="40", estimated_mid="1.00", liquidation_spread="0.10"),
            JointScenarioInput(name="wipeout", kind="stress", scenario_underlying="80", days_remaining=3,
                               implied_vol_percent="20", estimated_mid="0.02", liquidation_spread="0.50"),
        ])
        wipe = next(s for s in result.scenarios if s["name"] == "wipeout")
        self.assertEqual(wipe["exit_bid"], "0")  # 0.02 - 0.25 -> floored

    def test_output_feeds_m1_analyzer(self) -> None:
        # The generated scenarios must be consumable by analyze_option_trade.
        # Payload matches examples/attended_option_analysis.json schema exactly.
        gen = generate_scenarios(_thesis(), _inputs())
        payload = {
            "schema_version": "attended_option_analysis_input_v1",
            "account": {"currency": "USD", "observed_at": "2026-10-05T16:00:00+00:00",
                        "equity": "100000", "cash": "100000", "settled_cash": "100000",
                        "available_funds": "100000", "pending_debits_not_in_balances": "0"},
            "contract": {"con_id": 1, "symbol": "TEST", "right": "CALL", "strike": "100",
                         "expiry": "2026-10-14", "multiplier": 100, "currency": "USD"},
            "quote": {"observed_at": "2026-10-05T16:00:00+00:00", "bid": "1.55", "ask": "1.65"},
            "trade": {"quantity": 2, "entry_limit": "1.65",
                      "latest_exit_at": "2026-10-13T16:00:00+00:00",
                      "thesis": "hold above 95", "invalidation": "below 95"},
            "fees": {"entry_per_order": "1", "entry_per_contract": "0.65",
                     "exit_per_order": "1", "exit_per_contract": "0.65"},
            "budgets": {"planned_loss": "5000", "stress_loss": "5000",
                        "premium_exposure": "50000"},
            "scenarios": list(gen.scenarios),
        }
        report = analyze_option_trade(payload, now=datetime(2026, 10, 5, 16, 0, 5, tzinfo=timezone.utc))
        # The generated scenarios must not be the reason for any rejection.
        self.assertNotIn("SCENARIOS_MISSING_OR_INVALID", report.get("reasons", []))
        self.assertNotIn("PLANNED_AND_STRESS_SCENARIOS_REQUIRED", report.get("reasons", []))
        self.assertNotIn("INPUT_SCHEMA_INVALID", report.get("reasons", []))
        self.assertTrue(report.get("scenario_results"))

    def test_requires_planned_and_stress(self) -> None:
        with self.assertRaisesRegex(ScenarioError, "planned and one stress"):
            generate_scenarios(_thesis(), [
                JointScenarioInput(name="only_planned", kind="planned", scenario_underlying="105",
                                   days_remaining=3, implied_vol_percent="40",
                                   estimated_mid="1.60", liquidation_spread="0.10"),
            ])

    def test_duplicate_names_rejected(self) -> None:
        dup = [
            JointScenarioInput(name="x", kind="planned", scenario_underlying="105", days_remaining=3,
                               implied_vol_percent="40", estimated_mid="1.60", liquidation_spread="0.10"),
            JointScenarioInput(name="x", kind="stress", scenario_underlying="92", days_remaining=3,
                               implied_vol_percent="30", estimated_mid="0.20", liquidation_spread="0.10"),
        ]
        with self.assertRaisesRegex(ScenarioError, "unique"):
            generate_scenarios(_thesis(), dup)

    def test_bad_kind_rejected(self) -> None:
        with self.assertRaisesRegex(ScenarioError, "kind"):
            generate_scenarios(_thesis(), [
                JointScenarioInput(name="a", kind="planned", scenario_underlying="105", days_remaining=3,
                                   implied_vol_percent="40", estimated_mid="1.60", liquidation_spread="0.10"),
                JointScenarioInput(name="b", kind="maybe", scenario_underlying="92", days_remaining=3,
                                   implied_vol_percent="30", estimated_mid="0.20", liquidation_spread="0.10"),
            ])

    def test_missing_bid_inputs_rejected(self) -> None:
        with self.assertRaisesRegex(ScenarioError, "executable_bid"):
            generate_scenarios(_thesis(), [
                JointScenarioInput(name="a", kind="planned", scenario_underlying="105", days_remaining=3,
                                   implied_vol_percent="40", estimated_mid=None, liquidation_spread=None),
                JointScenarioInput(name="b", kind="stress", scenario_underlying="92", days_remaining=3,
                                   implied_vol_percent="30", estimated_mid="0.20", liquidation_spread="0.10"),
            ])

    def test_float_and_naive_rejected(self) -> None:
        with self.assertRaises(ScenarioError):
            _thesis(invalidation_underlying=95.0)
        with self.assertRaises(ScenarioError):
            _thesis(latest_exit=datetime(2026, 10, 14, 16, 0))

    def test_thesis_view_carries_disclaimer(self) -> None:
        result = generate_scenarios(_thesis(), _inputs())
        self.assertIn("disclaimer", result.thesis)
        self.assertIn("no probability", result.thesis["disclaimer"])


if __name__ == "__main__":
    unittest.main()
