from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from titan_runtime.storage import Store


class StorageTests(unittest.TestCase):
    def test_eligible_universe_and_plan_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "test.sqlite3")
            count = store.replace_eligible_universe([
                {"ticker": "A", "name": "A Corp", "type": "CS", "active": True},
                {"ticker": "ADR", "name": "ADR Corp", "type": "ADRC", "active": True},
            ])
            self.assertEqual(count, 2)
            self.assertTrue(store.is_eligible_security("A"))
            self.assertFalse(store.is_eligible_security("SPY"))
            plan = {
                "symbol": "A", "observed_at": "2026-08-20T14:00:00+00:00",
                "status": "WATCH_ONLY", "direction": "UP", "lane": "regular_equity",
                "setup": "structure_forming", "weighted_opportunity_score": 61.2,
                "modeled_move_capacity_pct": 4.1, "trigger": None,
                "structural_stop": None, "t1": None, "t2": None, "blockers": ["base"],
            }
            self.assertIsNotNone(store.save_prepared_trade_plan(plan))
            self.assertIsNone(store.save_prepared_trade_plan(plan))
            self.assertEqual(store.latest_prepared_trade_plans(10)[0]["symbol"], "A")
            store.close()

    def test_event_deduplication_and_acknowledgement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "test.sqlite3")
            first = store.emit_event("BASE_READY", "TEST", 70, {"x": 1}, "same")
            second = store.emit_event("BASE_READY", "TEST", 70, {"x": 2}, "same")
            self.assertIsNotNone(first)
            self.assertIsNone(second)
            pending = store.pending_events()
            self.assertEqual(len(pending), 1)
            self.assertTrue(store.acknowledge_event(pending[0]["event_id"]))
            self.assertEqual(store.pending_events(), [])
            store.close()

    def test_event_decision_is_persisted_before_acknowledgement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "test.sqlite3")
            event_id = store.emit_event(
                "MOMENTUM_WATCH", "TEST", 45, {"disposition": "ENTRY_REJECTED_KEEP_WATCH"},
                "watch-test",
            )
            assert event_id is not None
            decision_id = store.record_event_decision(
                event_id, "WATCH", "Too extended for entry; retain for a fresh base.",
                {"extension_atr": 4.4},
            )
            self.assertTrue(store.acknowledge_event(event_id))
            rows = store.event_decisions(1)
            self.assertEqual(rows[0]["decision_id"], decision_id)
            self.assertEqual(rows[0]["decision"], "WATCH")
            self.assertEqual(rows[0]["details"]["extension_atr"], 4.4)
            store.close()

    def test_snapshot_leaderboard_includes_downside_movers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "test.sqlite3")
            store.upsert_snapshots([
                {
                    "ticker": "DOWN", "todaysChangePerc": -12.0,
                    "lastTrade": {"p": 20.0}, "day": {"v": 2_000_000},
                    "prevDay": {"c": 23.0, "v": 1_000_000},
                },
                {
                    "ticker": "UP", "todaysChangePerc": 6.0,
                    "lastTrade": {"p": 15.0}, "day": {"v": 2_000_000},
                    "prevDay": {"c": 14.0, "v": 1_000_000},
                },
            ])
            self.assertEqual(store.top_snapshot_symbols(2), ["DOWN", "UP"])
            store.close()

    def test_live_event_queue_coalesces_and_returns_newest_first(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "test.sqlite3")
            old = store.emit_event("BASE_READY", "TEST", 70, {"x": 1}, "base-1")
            newest = store.emit_event("BASE_READY", "TEST", 70, {"x": 2}, "base-2")
            other = store.emit_event("BASE_READY", "OTHER", 70, {"x": 3}, "base-3")
            self.assertIsNotNone(old)
            self.assertIsNotNone(newest)
            self.assertIsNotNone(other)

            rows = store.pending_events()
            self.assertEqual(len(rows), 2)
            self.assertEqual({row["event_id"] for row in rows}, {newest, other})
            old_status = store.conn.execute(
                "SELECT status FROM events WHERE event_id=?", (old,)
            ).fetchone()["status"]
            self.assertEqual(old_status, "superseded")
            store.close()

    def test_stale_pending_events_expire(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "test.sqlite3")
            event_id = store.emit_event(
                "TRIGGER_CROSS", "TEST", 95, {"x": 1}, "cross-1"
            )
            store.conn.execute(
                "UPDATE events SET created_at='2020-01-01T00:00:00+00:00' WHERE event_id=?",
                (event_id,),
            )
            self.assertEqual(store.pending_events(max_age_seconds=60), [])
            status = store.conn.execute(
                "SELECT status FROM events WHERE event_id=?", (event_id,)
            ).fetchone()["status"]
            self.assertEqual(status, "expired")
            store.close()

    def test_candidate_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "test.sqlite3")
            payload = {
                "symbol": "TEST", "observed_at": "2026-08-19T12:00:00+00:00",
                "state": "BUILDING", "lane": "regular_equity", "signal_strength": 70,
                "price": 10, "gap_pct": 8, "dollar_volume": 5_000_000,
                "volume_acceleration": 1.5, "price_acceleration": 0.02,
                "relative_volume": 0.8, "spread_pct": 0.1, "short_atr": 0.2,
                "base_high": 10.2, "support": 9.9, "invalidation": 9.9,
                "limit_ceiling": 10.21, "extension_atr": 1.0,
                "quote_fresh": True, "preliminary_liquidity_pass": True,
            }
            store.upsert_candidate(payload)
            row = store.get_candidate("TEST")
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row["base_high"], 10.2)
            self.assertEqual(store.leaderboard(1)[0]["symbol"], "TEST")
            store.close()

    def test_daily_research_is_persisted_as_input_not_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "test.sqlite3")
            lesson_id = store.ingest_lesson(
                "2026-08-20", "trader_brain", "# Top Five\nTEST",
                structured={"trade_authority": False},
            )
            latest = store.latest_lessons(1)
            self.assertEqual(latest[0]["lesson_id"], lesson_id)
            self.assertFalse(latest[0]["structured"]["trade_authority"])
            with self.assertRaisesRegex(ValueError, "daily research cannot register"):
                store.propose_strategy_change({
                    "title": "Bypass confirmation",
                    "change_class": "production_rule",
                    "category": "strategy_logic",
                    "expected_effect": "Earlier entries",
                    "production_approved": True,
                })
            store.close()

    def test_entry_plan_is_frozen_before_outcome_and_locks_afterward(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "test.sqlite3")
            plan_id = store.create_entry_plan({
                "trade_date": "2026-08-20", "symbol": "test", "setup": "premarket_high_break",
                "lane": "earliest_reasonable_entry", "earliest_time": "08:12:00-04:00",
                "earliest_price": 4.20, "conservative_time": "09:38:00-04:00",
                "conservative_price": 4.85, "selected_price": 4.85,
                "structural_stop": 4.00, "quantity": 100, "capital": 485,
                "context": {"catalyst_verified": False},
            })
            before = store.entry_comparisons(1)[0]
            self.assertFalse(before["outcome_locked"])
            self.assertEqual(before["symbol"], "TEST")
            store.attach_entry_outcome(plan_id, {
                "observation_end": "2026-08-20T16:00:00-04:00",
                "session_high": 7.10, "session_low": 3.95,
                "hesitation_cost": 65.0, "confirmation_savings": 0.0,
            })
            after = store.entry_comparisons(1)[0]
            self.assertTrue(after["outcome_locked"])
            self.assertEqual(after["hesitation_cost"], 65.0)
            with self.assertRaisesRegex(ValueError, "outcome already attached"):
                store.attach_entry_outcome(plan_id, {
                    "observation_end": "2026-08-20T16:01:00-04:00"
                })
            store.close()


if __name__ == "__main__":
    unittest.main()
