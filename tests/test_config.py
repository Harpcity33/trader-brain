from __future__ import annotations

from datetime import time
import json
from pathlib import Path
import unittest

from titan_runtime.config import RuntimeConfig, SizingPolicy


class ConfigTests(unittest.TestCase):
    def test_production_config_is_shadow_only(self) -> None:
        path = Path(__file__).resolve().parent.parent / "config" / "titan-massive.json"
        config = RuntimeConfig.load(path)
        self.assertEqual(config.mode, "shadow")
        self.assertEqual(config.websocket_url, "wss://socket.massive.com/stocks")
        self.assertTrue(str(config.database_path).endswith("runtime/titan-intelligence.sqlite3"))
        self.assertEqual(config.start_time, time(4, 0))
        self.assertLess(config.watch_min_signal_strength, config.candidate_min_signal_strength)
        self.assertLess(config.watch_min_gap_pct, config.candidate_min_gap_pct)
        self.assertEqual(config.eligible_ticker_types, ("CS", "ADRC", "ETF", "FUND"))
        self.assertGreaterEqual(config.quote_watch_count, 1000)
        self.assertFalse(config.score_is_entry_gate)
        self.assertFalse(config.fresh_news_required)
        self.assertFalse(config.state_is_entry_gate)
        self.assertEqual(config.max_spread_to_structural_risk, 0.15)
        self.assertEqual(config.under5_min_dollar_volume, 7_500_000)
        self.assertEqual(config.under5_max_quote_spread_pct, 0.9375)
        self.assertEqual(config.policy_version, "capital_flexible_live_preparation_2026-08-23_v2")
        self.assertEqual(
            config.supersedes_policy_version,
            "profit_seeking_live_preparation_2026-08-22_v1",
        )
        self.assertEqual(config.sizing_policy.version, "capital_flexible_sizing_2026-08-23_v2")
        self.assertEqual(config.sizing_policy.max_unleveraged_buying_power_fraction, 1.0)
        self.assertEqual(config.sizing_policy.account_day_loss_limit_dollars, 100.0)
        self.assertTrue(config.sizing_policy.single_setup_concentration_allowed)
        self.assertFalse(config.sizing_policy.leverage_allowed)
        self.assertEqual(config.sizing_policy.initial_allocation_pct_range, (0, 100))
        self.assertTrue(config.sizing_policy.full_initial_allocation_allowed)
        self.assertTrue(config.sizing_policy.adds_optional)

    def test_sizing_policy_rejects_obsolete_tranches_and_invalid_initial_range(self) -> None:
        with self.assertRaisesRegex(ValueError, "build_tranches_pct was removed"):
            SizingPolicy.from_mapping({"build_tranches_pct": [40, 35, 25]})
        with self.assertRaisesRegex(ValueError, "ordered range"):
            SizingPolicy.from_mapping({"initial_allocation_pct_range": [75, 50]})
        with self.assertRaisesRegex(ValueError, "100% upper"):
            SizingPolicy.from_mapping(
                {
                    "initial_allocation_pct_range": [0, 75],
                    "full_initial_allocation_allowed": True,
                }
            )

    def test_sizing_policy_rejects_leverage_and_more_than_full_buying_power(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot authorize leverage"):
            SizingPolicy.from_mapping({"leverage_allowed": True})
        with self.assertRaisesRegex(ValueError, "must be in"):
            SizingPolicy.from_mapping({"max_unleveraged_buying_power_fraction": 1.01})
        with self.assertRaisesRegex(ValueError, "durable loss rule"):
            SizingPolicy.from_mapping({"account_day_loss_limit_dollars": 101})

    def test_sizing_policy_rejects_removed_fixed_dollar_caps(self) -> None:
        with self.assertRaisesRegex(ValueError, "fixed dollar sizing caps were removed"):
            SizingPolicy.from_mapping({"probe_allocation_cap": 437.5})

    def test_research_config_cannot_mutate_production(self) -> None:
        path = Path(__file__).resolve().parent.parent / "config" / "titan-research.json"
        config = json.loads(path.read_text())
        self.assertEqual(config["mode"], "research_and_paper_only")
        self.assertFalse(config["production_mutation_allowed"])
        self.assertEqual(sum(config["experimental_score_weights"].values()), 100)


if __name__ == "__main__":
    unittest.main()
