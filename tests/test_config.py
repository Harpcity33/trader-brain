from __future__ import annotations

from datetime import time
import json
from pathlib import Path
import unittest

from titan_runtime.config import RuntimeConfig


class ConfigTests(unittest.TestCase):
    def test_production_config_is_shadow_only(self) -> None:
        path = Path(__file__).resolve().parent.parent / "config" / "titan-massive.json"
        config = RuntimeConfig.load(path)
        self.assertEqual(config.mode, "shadow")
        self.assertEqual(config.websocket_url, "wss://socket.massive.com/stocks")
        self.assertTrue(str(config.database_path).endswith("runtime/titan-intelligence.sqlite3"))
        self.assertEqual(config.start_time, time(3, 55))
        self.assertLess(config.watch_min_signal_strength, config.candidate_min_signal_strength)
        self.assertLess(config.watch_min_gap_pct, config.candidate_min_gap_pct)
        self.assertEqual(config.eligible_ticker_types, ("CS", "ADRC"))
        self.assertGreaterEqual(config.quote_watch_count, 1000)

    def test_research_config_cannot_mutate_production(self) -> None:
        path = Path(__file__).resolve().parent.parent / "config" / "titan-research.json"
        config = json.loads(path.read_text())
        self.assertEqual(config["mode"], "research_and_paper_only")
        self.assertFalse(config["production_mutation_allowed"])
        self.assertEqual(sum(config["experimental_score_weights"].values()), 100)


if __name__ == "__main__":
    unittest.main()
