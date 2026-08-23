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
        self.assertIsNone(plan["preliminary_quantity_cap"])
        self.assertEqual(plan["blockers"], [])
        self.assertIn("catalyst_quality", plan["context_missing"])
        self.assertIsNone(plan["preliminary_allocation_cap"])
        self.assertIsNone(plan["preliminary_risk_cap"])
        self.assertEqual(plan["quantity_status"], "BROKER_CONFIRMED_LIMITS_REQUIRED")
        self.assertEqual(
            plan["position_notional_policy"]["max_unleveraged_buying_power_fraction"],
            1.0,
        )
        self.assertTrue(
            plan["position_notional_policy"]["single_setup_concentration_allowed"]
        )
        self.assertTrue(
            plan["position_notional_policy"][
                "full_buying_power_requires_materially_best_available_setup"
            ]
        )
        self.assertFalse(plan["position_notional_policy"]["leverage_allowed"])
        self.assertIsNone(
            plan["position_notional_policy"]["fixed_notional_cap_dollars"]
        )
        self.assertEqual(
            plan["loss_at_stop_policy"]["account_day_loss_limit_dollars"],
            100.0,
        )
        self.assertIsNone(
            plan["loss_at_stop_policy"]["fixed_per_trade_loss_cap_dollars"]
        )
        self.assertAlmostEqual(plan["reference_entry_price"], 10.12)
        self.assertAlmostEqual(plan["risk_per_share"], 0.22)
        self.assertAlmostEqual(plan["t1"], 10.34)
        self.assertAlmostEqual(plan["t2"], 10.56)
        self.assertAlmostEqual(plan["t3"], 10.78)
        self.assertLess(plan["modeled_move_capacity_pct"], 1.0)
        self.assertIsNone(plan["risk_campaign"]["fixed_initial_risk_dollars"])
        self.assertTrue(
            plan["risk_campaign"][
                "single_setup_may_consume_dynamic_new_stressed_risk_capacity"
            ]
        )
        self.assertTrue(
            plan["loss_at_stop_policy"][
                "requires_unleveraged_buying_power_and_gross_exposure_check"
            ]
        )
        self.assertTrue(
            plan["loss_at_stop_policy"][
                "requires_max_of_stop_or_stress_tail_loss"
            ]
        )
        self.assertTrue(
            plan["loss_at_stop_policy"]["requires_existing_open_risk_reservation"]
        )
        self.assertTrue(
            plan["loss_at_stop_policy"]["requires_loss_lock_and_profit_floor_checks"]
        )
        self.assertEqual(
            plan["initial_entry_allocation_policy"]["initial_allocation_pct_range"],
            [0, 100],
        )
        self.assertTrue(
            plan["initial_entry_allocation_policy"]["full_initial_allocation_allowed"]
        )
        self.assertFalse(
            plan["initial_entry_allocation_policy"][
                "initial_entry_requires_profit_funding"
            ]
        )
        self.assertFalse(
            plan["initial_entry_allocation_policy"]["initial_entry_requires_staging"]
        )
        self.assertTrue(plan["initial_entry_allocation_policy"]["adds_optional"])
        self.assertEqual(plan["core_runner_policy"]["runner_pct"], [20, 30])
        self.assertTrue(plan["add_policy"]["adds_optional"])
        self.assertEqual(plan["add_policy"]["risk_constraint_logic"], "OR")
        self.assertEqual(len(plan["add_policy"]["allowed_when_any"]), 2)

    def test_default_policy_resolution_uses_capital_flexible_version(self) -> None:
        signal = {
            "symbol": "TEST",
            "observed_at": "2026-08-20T14:00:00+00:00",
            "lane": "regular_equity",
            "direction": "UP",
            "base_high": 10.10,
            "invalidation": 9.90,
            "limit_ceiling": 10.12,
            "quote_fresh": True,
            "preliminary_liquidity_pass": True,
            "entry_rejection_reasons": [],
            "exhaustion_lock": False,
        }
        plan = build_preliminary_trade_plan(signal)
        self.assertEqual(
            plan["policy_version"],
            "capital_flexible_live_preparation_2026-08-23_v2",
        )
        self.assertEqual(
            plan["sizing_policy_version"],
            "capital_flexible_sizing_2026-08-23_v2",
        )

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
        self.assertIsNone(plan["preliminary_allocation_cap"])
        self.assertEqual(plan["sizing_tier"], "under5_broker_resolved_capital")
        self.assertIsNone(plan["preliminary_risk_cap"])
        self.assertEqual(
            plan["position_notional_policy"]["max_unleveraged_buying_power_fraction"],
            1.0,
        )
        self.assertEqual(
            plan["risk_campaign"]["account_day_loss_limit_dollars"],
            100.0,
        )
        self.assertTrue(any("not an unconditional" in note for note in plan["context_notes"]))
        self.assertTrue(any("adverse filing" in item for item in plan["skip_conditions"]))


if __name__ == "__main__":
    unittest.main()
