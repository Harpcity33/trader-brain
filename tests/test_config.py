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
        self.assertEqual(config.policy_version, "profit_seeking_live_preparation_2026-08-22_v1")
        self.assertEqual(config.supersedes_policy_version, "sol_ultra_2026-08-21_v1")
        self.assertEqual(config.sizing_policy.version, "profit_seeking_sizing_2026-08-22_v1")
        self.assertEqual(config.sizing_policy.probe_allocation_cap, 437.5)
        self.assertEqual(config.sizing_policy.probe_risk_cap, 15.0)
        self.assertEqual(config.sizing_policy.regular_allocation_ceiling, 1062.5)
        self.assertEqual(config.sizing_policy.regular_risk_ceiling, 37.5)
        self.assertEqual(config.sizing_policy.under5_allocation_ceiling, 625.0)
        self.assertEqual(config.sizing_policy.under5_risk_ceiling, 22.5)
        self.assertEqual(config.sizing_policy.reference_risk_unit, 20.0)
        self.assertEqual(config.sizing_policy.initial_risk, 15.0)
        self.assertEqual(config.sizing_policy.strengthened_winner_risk_cap, 30.0)
        self.assertEqual(config.sizing_policy.build_tranches_pct, (40, 35, 25))

    def test_sizing_policy_rejects_invalid_tranche_percentages(self) -> None:
        with self.assertRaisesRegex(ValueError, "totaling 100"):
            SizingPolicy.from_mapping({"build_tranches_pct": [50, 50, 1]})

    def test_research_config_cannot_mutate_production(self) -> None:
        path = Path(__file__).resolve().parent.parent / "config" / "titan-research.json"
        config = json.loads(path.read_text())
        self.assertEqual(config["mode"], "research_and_paper_only")
        self.assertFalse(config["production_mutation_allowed"])
        self.assertEqual(sum(config["experimental_score_weights"].values()), 100)


if __name__ == "__main__":
    unittest.main()
