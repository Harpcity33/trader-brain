from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from titan_runtime.daily_ingest import ingest_daily_report
from titan_runtime.storage import Store


class DailyIngestTests(unittest.TestCase):
    def test_daily_report_is_archived_and_ingested_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_directory = root / "source"
            source_directory.mkdir()
            report = source_directory / "trader_brain_codex_2026-08-20.md"
            report.write_text("# Trader Brain\nFirst revision.\n", encoding="utf-8")
            (root / "config").mkdir()
            production_db = root / "production/runtime.sqlite3"
            config = {
                "mode": "shadow",
                "websocket_url": "wss://socket.massive.com/stocks",
                "rest_base_url": "https://api.massive.com",
                "keychain_service": "test",
                "database_path": str(production_db),
                "event_export_path": "runtime/events.jsonl",
                "log_path": "runtime/test.log",
                "timezone": "America/New_York",
                "start_time_et": "03:55",
                "stop_time_et": "16:05",
                "snapshot_refresh_seconds": 300,
                "stale_data_seconds": 90,
                "quote_watch_count": 30,
                "candidate_min_signal_strength": 62,
                "candidate_min_price": 1,
                "candidate_max_price": 1000,
                "candidate_min_gap_pct": 4,
                "watch_min_gap_pct": 3,
                "candidate_min_dollar_volume": 2_000_000,
                "watch_min_signal_strength": 50,
                "under5_min_dollar_volume": 10_000_000,
                "max_quote_spread_pct": 0.75,
                "under5_max_quote_spread_pct": 0.75,
                "retention_days": 15,
            }
            config_path = root / "config/titan-massive.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")

            first = ingest_daily_report(config_path, source_directory, "2026-08-20")
            second = ingest_daily_report(config_path, source_directory, "2026-08-20")
            self.assertEqual(first["archive"]["status"], "archived")
            self.assertEqual(second["archive"]["status"], "already_archived")
            self.assertTrue(all(row["status"] == "already_ingested" for row in second["ingestion"]))

            index = json.loads((root / "knowledge/index.json").read_text())
            self.assertEqual(len(index["reports"]), 1)
            for database in (production_db, root / "runtime/titan-intelligence.sqlite3"):
                store = Store(database)
                count = store.conn.execute("SELECT COUNT(*) FROM trader_brain_lessons").fetchone()[0]
                store.close()
                self.assertEqual(count, 1)

            report.write_text("# Trader Brain\nSecond revision.\n", encoding="utf-8")
            third = ingest_daily_report(config_path, source_directory, "2026-08-20")
            self.assertEqual(third["archive"]["revision"], 2)
            index = json.loads((root / "knowledge/index.json").read_text())
            self.assertEqual(len(index["reports"]), 2)
            self.assertTrue(index["reports"][1]["path"].endswith("-r2.md"))


if __name__ == "__main__":
    unittest.main()
