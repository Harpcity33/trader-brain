from __future__ import annotations

import unittest
from pathlib import Path

from titan_runtime.config import RuntimeConfig
from titan_runtime.massive import historical_analog_features
from titan_runtime.ranking import apply_weighted_opportunity_scale, build_preliminary_trade_plan


class RankingTests(unittest.TestCase):
    def test_historical_analogs_use_only_matching_gap_direction(self) -> None:
        rows = [
            {"o": 10.0, "h": 10.5, "l": 9.8, "c": 10.0},
            {"o": 10.6, "h": 11.66, "l": 10.4, "c": 11.0},
            {"o": 10.0, "h": 10.1, "l": 9.0, "c": 9.2},
        ]
        result = historical_analog_features(rows, "UP")
        self.assertEqual(result["analog_count"], 1)
        self.assertAlmostEqual(result["historical_median_follow_through_pct"], 10.0)
        self.assertEqual(result["gap_fade_risk"], 0.0)

    def test_weighted_scale_does_not_invent_missing_inputs(self) -> None:
        signal = {
            "symbol": "TEST", "observed_at": "2026-08-20T14:00:00+00:00",
            "state": "ACCELERATING", "lane": "regular_equity", "direction": "UP",
            "price": 10.0, "dollar_volume": 30_000_000, "relative_volume": 4.0,
            "spread_pct": 0.10, "extension_atr": 1.0, "short_atr": 0.25,
            "base_high": 10.10, "invalidation": 9.90, "limit_ceiling": 10.12,
            "quote_fresh": True, "preliminary_liquidity_pass": True,
            "entry_rejection_reasons": [], "exhaustion_lock": False,
        }
        result = apply_weighted_opportunity_scale(signal)
        self.assertEqual(result["weighted_scale_version"], "trader_brain_2026-08-22_v2")
        self.assertIn("catalyst_quality", result["missing_weighted_inputs"])
        self.assertIn("historical_follow_through", result["missing_weighted_inputs"])
        self.assertLess(result["weighted_evidence_coverage_pct"], 100)
        self.assertGreater(
            result["weighted_opportunity_score"],
            result["raw_covered_contribution_score"],
        )
        self.assertGreater(result["modeled_move_capacity_pct"], 0)

    def test_missing_context_does_not_block_executable_preliminary_plan(self) -> None:
        signal = {
            "symbol": "TEST", "observed_at": "2026-08-20T14:00:00+00:00",
            "state": "ACCELERATING", "lane": "regular_equity", "direction": "UP",
            "price": 10.0, "dollar_volume": 30_000_000, "relative_volume": 4.0,
            "spread_pct": 0.10, "extension_atr": 1.0, "short_atr": 0.25,
            "base_high": 10.10, "invalidation": 9.90, "limit_ceiling": 10.12,
            "quote_fresh": True, "preliminary_liquidity_pass": True,
            "entry_rejection_reasons": [], "exhaustion_lock": False,
        }
        apply_weighted_opportunity_scale(signal)
        signal["modeled_move_capacity_pct"] = 0.01
        config_path = Path(__file__).resolve().parent.parent / "config" / "titan-massive.json"
        plan = build_preliminary_trade_plan(signal, RuntimeConfig.load(config_path))
        self.assertEqual(plan["status"], "PRELIMINARY")
        self.assertFalse(plan["trade_authority"])
        self.assertGreater(plan["preliminary_quantity_cap"], 0)
        self.assertEqual(plan["blockers"], [])
        self.assertIn("catalyst_quality", plan["context_missing"])
        self.assertEqual(plan["preliminary_allocation_cap"], 437.5)
        self.assertEqual(plan["preliminary_risk_cap"], 15.0)
        self.assertEqual(plan["normal_regular_allocation_ceiling"], 1062.5)
        self.assertEqual(plan["configured_regular_hard_risk_ceiling"], 37.5)
        self.assertAlmostEqual(plan["reference_entry_price"], 10.12)
        self.assertAlmostEqual(plan["risk_per_share"], 0.22)
        self.assertAlmostEqual(plan["t1"], 10.34)
        self.assertAlmostEqual(plan["t2"], 10.56)
        self.assertAlmostEqual(plan["t3"], 10.78)
        self.assertLessEqual(
            plan["preliminary_quantity_cap"] * plan["risk_per_share"],
            plan["preliminary_risk_cap"],
        )
        self.assertLess(plan["modeled_move_capacity_pct"], 1.0)
        self.assertEqual(plan["build_tranches_pct"], [40, 35, 25])
        self.assertEqual(plan["risk_campaign"]["reference_risk_unit"], 20.0)
        self.assertEqual(plan["risk_campaign"]["strengthened_winner_risk_cap"], 30.0)
        self.assertEqual(plan["core_runner_policy"]["runner_pct"], [20, 30])
        self.assertTrue(plan["add_policy"]["open_risk_neutral"])

    def test_under5_uses_lane_sizing_without_fresh_catalyst_blocker(self) -> None:
        signal = {
            "symbol": "TEST", "observed_at": "2026-08-20T14:00:00+00:00",
            "lane": "under5", "direction": "UP", "base_high": 4.20,
            "invalidation": 4.10, "limit_ceiling": 4.21, "quote_fresh": True,
            "preliminary_liquidity_pass": True, "entry_rejection_reasons": [],
            "exhaustion_lock": False, "missing_weighted_inputs": ["catalyst_quality"],
        }
        config_path = Path(__file__).resolve().parent.parent / "config" / "titan-massive.json"
        plan = build_preliminary_trade_plan(signal, RuntimeConfig.load(config_path))
        self.assertEqual(plan["status"], "PRELIMINARY")
        self.assertEqual(plan["blockers"], [])
        self.assertIn("catalyst_quality", plan["context_missing"])
        self.assertEqual(plan["preliminary_allocation_cap"], 625.0)
        self.assertEqual(plan["sizing_tier"], "under5_initial_probe")
        self.assertEqual(plan["preliminary_risk_cap"], 15.0)
        self.assertEqual(plan["risk_campaign"]["normal_campaign_risk"], 20.0)
        self.assertEqual(plan["risk_campaign"]["strengthened_winner_risk_cap"], 22.5)
        self.assertLessEqual(
            plan["preliminary_quantity_cap"] * plan["risk_per_share"],
            plan["preliminary_risk_cap"],
        )
        self.assertTrue(any("not an unconditional" in note for note in plan["context_notes"]))
        self.assertTrue(any("adverse filing" in item for item in plan["skip_conditions"]))


if __name__ == "__main__":
    unittest.main()
