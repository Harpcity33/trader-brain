from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from titan_brain.live.market_data import MarketDataCache
from titan_brain.live.massive_adapter import (
    LocalMassiveReadOnlySource,
    MassiveStoreError,
)


NOW = datetime(2026, 9, 8, 14, 1, 1, tzinfo=timezone.utc)
CONTRACT = "0b1cfbd1fb9524f7dbda47a046422258c23978e396d0f9d4b150ede888128350"


class Tradable:
    def is_tradable(self, symbol: str, *, as_of: datetime) -> bool:
        return symbol == "GOOD" and as_of == NOW


class LocalMassiveSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "massive.sqlite3"
        self._create_database()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def source(self, **overrides):
        values = {
            "database_path": self.path,
            "pilot_id": "titan_momentum_equity",
            "book_mode": "SHADOW",
            "decision_contract_hash": CONTRACT,
            "health_max_age_seconds": 15,
            "candidate_max_age_seconds": 120,
        }
        values.update(overrides)
        return LocalMassiveReadOnlySource(**values)

    def _create_database(self) -> None:
        connection = sqlite3.connect(self.path)
        connection.executescript(
            """
            CREATE TABLE health(component TEXT PRIMARY KEY,status TEXT,checked_at TEXT,details_json TEXT);
            CREATE TABLE quotes(symbol TEXT PRIMARY KEY,timestamp_ms INTEGER,bid REAL,ask REAL,
              bid_size REAL,ask_size REAL,spread_pct REAL,received_at TEXT);
            CREATE TABLE bars_1m(symbol TEXT,start_ms INTEGER,end_ms INTEGER,open REAL,high REAL,
              low REAL,close REAL,volume REAL,window_vwap REAL,session_vwap REAL,
              accumulated_volume REAL,official_open REAL,otc INTEGER,received_at TEXT,
              PRIMARY KEY(symbol,start_ms));
            CREATE TABLE prepared_trade_plans(plan_id TEXT PRIMARY KEY,plan_key TEXT UNIQUE,
              symbol TEXT,observed_at TEXT,status TEXT,direction TEXT,lane TEXT,
              weighted_opportunity_score REAL,modeled_move_capacity_pct REAL,trigger REAL,
              structural_stop REAL,t1 REAL,t2 REAL,payload_json TEXT,created_at TEXT,
              pilot_id TEXT,book_mode TEXT,decision_contract_version TEXT,
              decision_contract_hash TEXT);
            CREATE INDEX idx_prepared_plans_rank ON prepared_trade_plans(
              observed_at DESC,weighted_opportunity_score DESC);
            """
        )
        for component in ("massive_websocket", "market_data_freshness"):
            connection.execute(
                "INSERT INTO health VALUES(?,?,?,?)",
                (component, "healthy", (NOW - timedelta(seconds=1)).isoformat(), "{}"),
            )
        quote_at = NOW - timedelta(seconds=1)
        connection.execute(
            "INSERT INTO quotes VALUES(?,?,?,?,?,?,?,?)",
            (
                "GOOD",
                int(quote_at.timestamp() * 1000),
                10.00,
                10.02,
                500,
                600,
                0.2,
                quote_at.isoformat(),
            ),
        )
        start = NOW - timedelta(seconds=61)
        end = NOW - timedelta(seconds=1)
        connection.execute(
            "INSERT INTO bars_1m VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "GOOD",
                int(start.timestamp() * 1000),
                int(end.timestamp() * 1000),
                9.90,
                10.10,
                9.85,
                10.01,
                800_000,
                10.0,
                9.95,
                800_000,
                9.90,
                0,
                NOW.isoformat(),
            ),
        )
        observed = NOW - timedelta(seconds=2)
        payload = {
            "schema_version": 2,
            "pilot_id": "titan_momentum_equity",
            "book_mode": "SHADOW",
            "decision_contract_hash": CONTRACT,
            "symbol": "GOOD",
            "observed_at": observed.isoformat(),
            "setup": "controlled_base_breakout",
            "review_limit_ceiling": 10.02,
            "trade_authority": False,
            "broker_authority": False,
        }
        connection.execute(
            "INSERT INTO prepared_trade_plans VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "source-plan-1",
                "source-key-1",
                "GOOD",
                observed.isoformat(),
                "PRELIMINARY",
                "UP",
                "regular_equity",
                81.5,
                2.0,
                10.01,
                9.80,
                10.25,
                10.50,
                json.dumps(payload, sort_keys=True),
                observed.isoformat(),
                "titan_momentum_equity",
                "SHADOW",
                "titan_momentum_equity_2026-08-24_v3",
                CONTRACT,
            ),
        )
        connection.commit()
        connection.close()

    def test_reads_fresh_existing_feed_and_hydrates_completed_evidence(self) -> None:
        source = self.source()
        health = source.health(now=NOW)
        self.assertTrue(health.producer_fresh)
        self.assertEqual(health.blockers, ())
        structures = source.prepared_structures(now=NOW, limit=5)
        self.assertEqual(len(structures), 1)
        self.assertEqual(str(structures[0].entry_limit), "10.02")
        self.assertEqual(structures[0].setup_id, "controlled_base_breakout")

        cache = MarketDataCache(max_active=5)
        failures = source.hydrate_cache(
            cache,
            structures=structures,
            session_start=NOW - timedelta(hours=2),
            now=NOW,
            tradability=Tradable(),
        )
        self.assertEqual(failures, ())
        bar = next(iter(cache.bars["GOOD"].values()))
        evidence = cache.validate_entry_evidence(
            symbol="GOOD",
            now=NOW,
            plan_created_at=NOW,
            plan_expires_at=NOW + timedelta(seconds=30),
            causal_bar_end=bar.end_at,
            quote_max_age_seconds=5,
            completed_bar_max_age_seconds=120,
            minimum_session_volume=750_000,
            max_spread_bps=25,
            minimum_depth_multiple=1,
            quantity=5,
        )
        self.assertTrue(evidence.eligible, evidence.failures)
        self.assertEqual(tuple(cache.active_scores), ("GOOD",))

    def test_stale_feed_and_candidates_fail_closed(self) -> None:
        late = NOW + timedelta(minutes=3)
        health = self.source().health(now=late)
        self.assertFalse(health.producer_fresh)
        self.assertIn("MASSIVE_QUOTE_STALE", health.blockers)
        self.assertIn("MASSIVE_COMPLETED_BAR_STALE", health.blockers)
        self.assertEqual(self.source().prepared_structures(now=late, limit=5), ())

    def test_wrong_shadow_identity_is_rejected_not_reinterpreted(self) -> None:
        connection = sqlite3.connect(self.path)
        payload = json.loads(
            connection.execute("SELECT payload_json FROM prepared_trade_plans").fetchone()[0]
        )
        payload["trade_authority"] = True
        connection.execute(
            "UPDATE prepared_trade_plans SET payload_json=?", (json.dumps(payload),)
        )
        connection.commit()
        connection.close()
        with self.assertRaisesRegex(MassiveStoreError, "identity/provenance"):
            self.source().prepared_structures(now=NOW, limit=5)

    def test_missing_or_incompatible_store_reports_exact_blocker(self) -> None:
        missing = self.source(database_path=self.path.parent / "missing.sqlite3")
        report = missing.health(now=NOW)
        self.assertFalse(report.producer_fresh)
        self.assertTrue(any(item.startswith("MASSIVE_STORE_UNAVAILABLE") for item in report.blockers))

        bad_path = self.path.parent / "bad.sqlite3"
        sqlite3.connect(bad_path).close()
        with self.assertRaisesRegex(MassiveStoreError, "schema is incompatible"):
            with self.source(database_path=bad_path)._connect():
                pass


if __name__ == "__main__":
    unittest.main()
