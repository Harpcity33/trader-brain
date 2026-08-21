from __future__ import annotations

import unittest

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
        self.assertIn("catalyst_quality", result["missing_weighted_inputs"])
        self.assertIn("historical_follow_through", result["missing_weighted_inputs"])
        self.assertLess(result["weighted_evidence_coverage_pct"], 100)
        self.assertGreater(result["modeled_move_capacity_pct"], 0)

    def test_preliminary_plan_is_blocked_until_full_evidence_exists(self) -> None:
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
        plan = build_preliminary_trade_plan(signal)
        self.assertEqual(plan["status"], "WATCH_ONLY")
        self.assertFalse(plan["trade_authority"])
        self.assertGreater(plan["preliminary_quantity_cap"], 0)
        self.assertGreater(plan["t1"], plan["trigger"])


if __name__ == "__main__":
    unittest.main()
