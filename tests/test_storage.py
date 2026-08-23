from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone

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
            reference = store.get_eligible_security("A")
            assert reference is not None
            self.assertEqual(reference["ticker_type"], "CS")
            self.assertFalse(reference["broker_tradability_verified"])
            plan = {
                "symbol": "A", "observed_at": "2026-08-20T14:00:00+00:00",
                "status": "WATCH_ONLY", "direction": "UP", "lane": "regular_equity",
                "setup": "structure_forming", "weighted_opportunity_score": 61.2,
                "modeled_move_capacity_pct": 4.1, "trigger": None,
                "structural_stop": None, "t1": None, "t2": None, "blockers": ["base"],
            }
            self.assertIsNotNone(store.save_prepared_trade_plan(plan))
            self.assertIsNone(store.save_prepared_trade_plan(plan))
            versioned = {
                **plan,
                "schema_version": 2,
                "policy_version": "profit_seeking_live_preparation_2026-08-22_v1",
                "sizing_policy_version": "profit_seeking_sizing_2026-08-22_v1",
                "t3": 11.5,
            }
            self.assertIsNotNone(store.save_prepared_trade_plan(versioned))
            plan_count = store.conn.execute(
                "SELECT COUNT(*) FROM prepared_trade_plans"
            ).fetchone()[0]
            self.assertEqual(plan_count, 2)
            self.assertEqual(
                store.latest_prepared_trade_plans(10)[0]["payload"]["schema_version"],
                2,
            )
            self.assertEqual(store.latest_prepared_trade_plans(10)[0]["symbol"], "A")
            store.close()

    def test_same_minute_cumulative_history_is_causal_and_dst_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "test.sqlite3")

            def minute_event(symbol: str, iso: str, accumulated: float) -> dict:
                start_ms = int(datetime.fromisoformat(iso).astimezone(timezone.utc).timestamp() * 1000)
                return {
                    "sym": symbol, "s": start_ms, "e": start_ms + 59_999,
                    "o": 10, "h": 10.2, "l": 9.9, "c": 10.1, "v": 100,
                    "av": accumulated,
                }

            # The first session is EST and the next is EDT; both are 09:45 New York.
            store.insert_minute_bar(minute_event("TEST", "2026-03-06T09:45:00-05:00", 900))
            store.insert_minute_bar(minute_event("TEST", "2026-03-09T09:45:00-04:00", 1_100))
            store.insert_minute_bar(minute_event("TEST", "2026-03-10T09:44:00-04:00", 1_500))
            current_ms = int(
                datetime.fromisoformat("2026-03-10T09:45:00-04:00")
                .astimezone(timezone.utc).timestamp() * 1000
            )
            values = store.same_minute_cumulative_history(
                "TEST", current_ms, "America/New_York", max_sessions=20
            )
            self.assertEqual(values, [1_100.0, 900.0])
            store.close()

    def test_under5_post_halt_rearms_after_two_completed_bars(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "test.sqlite3")
            resume_ms = int(
                datetime.fromisoformat("2026-08-24T10:00:30-04:00")
                .astimezone(timezone.utc).timestamp() * 1000
            )
            store.insert_halt({"T": "TEST", "t": resume_ms}, 18)

            def event(iso: str) -> dict:
                start_ms = int(
                    datetime.fromisoformat(iso).astimezone(timezone.utc).timestamp() * 1000
                )
                return {
                    "sym": "TEST", "s": start_ms, "e": start_ms + 59_999,
                    "o": 4.5, "h": 4.6, "l": 4.4, "c": 4.55, "v": 1000,
                    "av": 10_000,
                }

            first = event("2026-08-24T10:01:00-04:00")
            store.insert_minute_bar(first)
            context = store.post_halt_context(
                "TEST", first["s"], "America/New_York"
            )
            self.assertIsNotNone(context)
            assert context is not None
            self.assertEqual(context["completed_post_resumption_bars"], 1)
            self.assertFalse(context["entry_rearmed"])

            second = event("2026-08-24T10:02:00-04:00")
            store.insert_minute_bar(second)
            context = store.post_halt_context(
                "TEST", second["s"], "America/New_York"
            )
            self.assertIsNotNone(context)
            assert context is not None
            self.assertEqual(context["completed_post_resumption_bars"], 2)
            self.assertTrue(context["entry_rearmed"])
            store.close()

    def test_active_halt_blocks_entry_before_resumption(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "test.sqlite3")
            halt_ms = int(
                datetime.fromisoformat("2026-08-24T10:00:30-04:00")
                .astimezone(timezone.utc).timestamp() * 1000
            )
            store.insert_halt({"T": "TEST", "t": halt_ms}, 17)
            context = store.post_halt_context(
                "TEST", halt_ms + 1_000, "America/New_York"
            )
            self.assertIsNotNone(context)
            assert context is not None
            self.assertTrue(context["active_halt"])
            self.assertFalse(context["entry_rearmed"])
            self.assertIsNone(context["resumption_timestamp_ms"])
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
            lower_raw_higher_available = {
                **payload,
                "symbol": "AVAILABLE",
                "weighted_opportunity_score": 20,
                "available_evidence_score": 90,
                "weighted_evidence_coverage_pct": 50,
            }
            higher_raw_lower_available = {
                **payload,
                "symbol": "RAW",
                "weighted_opportunity_score": 80,
                "available_evidence_score": 60,
                "weighted_evidence_coverage_pct": 100,
            }
            store.upsert_candidate(lower_raw_higher_available)
            store.upsert_candidate(higher_raw_lower_available)
            row = store.get_candidate("TEST")
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row["base_high"], 10.2)
            self.assertEqual(store.leaderboard(1)[0]["symbol"], "AVAILABLE")
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

    def test_position_campaign_requires_broker_confirmation_and_keeps_original_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "test.sqlite3")
            planned = {
                "account_key": "ending-7153", "instrument_key": "equity:TEST",
                "symbol": "TEST", "thesis_key": "TEST", "direction": "UP",
                "asset_class": "equity", "status": "PLANNED",
                "strategy_version": "titan_profitability_live_2026-08-22_v2",
                "original_stop": 9.75, "current_stop": 9.75,
                "initial_quantity": 40, "current_quantity": 0,
                "core_quantity": 0, "runner_quantity": 0,
                "reference_risk_dollars": 20,
                "continuation_health": "UNKNOWN", "remaining_opportunity": "AVAILABLE",
                "next_actions": {"add": "profitable retest"},
            }
            campaign_id = store.upsert_position_campaign(planned)
            with self.assertRaisesRegex(ValueError, "active campaign already exists"):
                store.upsert_position_campaign(
                    {
                        **planned,
                        "instrument_key": "option:TEST:20260918:C:10",
                        "symbol": "TEST260918C00010000",
                        "asset_class": "option",
                    }
                )
            with self.assertRaisesRegex(ValueError, "requires broker_confirmed_at"):
                store.upsert_position_campaign({**planned, "status": "FILLED"})
            filled = {
                **planned, "status": "PROTECTED", "entry_price": 10.0,
                "broker_confirmed_at": "2026-08-22T14:00:01+00:00",
                "current_quantity": 40, "core_quantity": 30, "runner_quantity": 10,
                "high_water_price": 10.3, "mfe_r": 0.6, "mae_r": -0.2,
                "continuation_health": "HEALTHY",
                "broker_state": {"protection_confirmed": True},
            }
            self.assertEqual(store.upsert_position_campaign(filled), campaign_id)
            row = store.position_campaigns()[0]
            self.assertEqual(row["original_stop"], 9.75)
            self.assertFalse(row["trade_authority"])
            self.assertTrue(row["broker_state"]["protection_confirmed"])
            with self.assertRaisesRegex(ValueError, "immutable"):
                store.upsert_position_campaign({**filled, "original_stop": 9.5})
            with self.assertRaisesRegex(ValueError, "may not widen"):
                store.upsert_position_campaign({**filled, "current_stop": 9.70})
            with self.assertRaisesRegex(ValueError, "may not transition"):
                store.upsert_position_campaign(
                    {
                        **filled,
                        "status": "REJECTED",
                        "current_quantity": 0,
                        "core_quantity": 0,
                        "runner_quantity": 0,
                    }
                )
            with self.assertRaisesRegex(ValueError, "symbol is immutable"):
                store.upsert_position_campaign({**filled, "symbol": "OTHER"})

            closed_id = store.upsert_position_campaign(
                {
                    **filled,
                    "status": "CLOSED",
                    "current_quantity": 0,
                    "core_quantity": 0,
                    "runner_quantity": 0,
                }
            )
            self.assertEqual(closed_id, campaign_id)
            reentry_id = store.upsert_position_campaign(
                {
                    **planned,
                    "original_stop": 9.5,
                    "current_stop": 9.5,
                }
            )
            self.assertNotEqual(reentry_id, campaign_id)
            all_campaigns = store.position_campaigns(include_terminal=True)
            self.assertEqual(len(all_campaigns), 2)
            self.assertEqual({row["original_stop"] for row in all_campaigns}, {9.5, 9.75})
            self.assertEqual(len(store.position_campaign_events()), 4)
            self.assertTrue(
                all(not row["trade_authority"] for row in store.position_campaign_events())
            )
            event_payload = store.position_campaign_events(campaign_id)[-1]["payload"]
            self.assertEqual(event_payload["strategy_version"], planned["strategy_version"])
            self.assertEqual(event_payload["reference_risk_dollars"], 20)

            unprotected_plan = {
                **planned,
                "instrument_key": "equity:NOSTOP",
                "symbol": "NOSTOP",
                "thesis_key": "NOSTOP",
                "current_stop": None,
            }
            store.upsert_position_campaign(unprotected_plan)
            with self.assertRaisesRegex(ValueError, "may not widen below original_stop"):
                store.upsert_position_campaign(
                    {
                        **unprotected_plan,
                        "status": "PROTECTED",
                        "entry_price": 10.0,
                        "original_stop": None,
                        "current_stop": 9.5,
                        "current_quantity": 40,
                        "core_quantity": 30,
                        "runner_quantity": 10,
                        "broker_confirmed_at": "2026-08-22T14:00:01+00:00",
                        "broker_state": {"protection_confirmed": True},
                    }
                )
            store.close()

    def test_risk_session_latches_loss_lock_and_profit_objective(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "test.sqlite3")

            def evidence(snapshot_id: str) -> dict:
                return {
                    "account_snapshot_id": snapshot_id,
                    "account_state_readable": True,
                    "orders_reconciled": True,
                    "positions_reconciled": True,
                }

            base = {
                "account_key": "ending-7153",
                "session_date": "2026-08-20",
                "strategy_version": "titan_live_canonical_2026-08-22_v1",
                "start_of_day_equity": 5000,
                "baseline_confirmed_at": "2026-08-20T11:00:00+00:00",
                "current_equity": 5000,
                "realized_net_pnl": 0,
                "confirmed_cash_flow_adjustment": 0,
                "broker_confirmed_at": "2026-08-20T11:00:01+00:00",
                "broker_state": evidence("open"),
            }
            opening = store.upsert_risk_session(base)
            self.assertFalse(opening["loss_lock"])
            self.assertTrue(opening["new_entries_allowed"])

            locked = store.upsert_risk_session(
                {
                    **base,
                    "current_equity": 4885,
                    "realized_net_pnl": -115,
                    "broker_confirmed_at": "2026-08-20T14:00:00+00:00",
                    "broker_state": evidence("loss"),
                }
            )
            self.assertTrue(locked["loss_lock"])
            self.assertFalse(locked["new_entries_allowed"])
            rebounded = store.upsert_risk_session(
                {
                    **base,
                    "current_equity": 5100,
                    "realized_net_pnl": 50,
                    "broker_confirmed_at": "2026-08-20T15:00:00+00:00",
                    "broker_state": evidence("rebound"),
                }
            )
            self.assertTrue(rebounded["loss_lock"])
            with self.assertRaisesRegex(ValueError, "immutable"):
                store.upsert_risk_session({**base, "start_of_day_equity": 5001})

            next_day = {
                **base,
                "session_date": "2026-08-21",
                "baseline_confirmed_at": "2026-08-21T11:00:00+00:00",
                "broker_confirmed_at": "2026-08-21T15:00:00+00:00",
                "current_equity": 5160,
                "realized_net_pnl": 160,
                "broker_state": evidence("objective"),
            }
            objective = store.upsert_risk_session(next_day)
            self.assertTrue(objective["profit_objective_reached"])
            self.assertEqual(objective["active_profit_floor_dollars"], 125)
            self.assertEqual(objective["post_objective_new_risk_buffer"], 35)
            after_pullback = store.upsert_risk_session(
                {
                    **next_day,
                    "current_equity": 5140,
                    "realized_net_pnl": 140,
                    "broker_confirmed_at": "2026-08-21T16:00:00+00:00",
                    "broker_state": evidence("pullback"),
                }
            )
            self.assertTrue(after_pullback["profit_objective_reached"])
            self.assertEqual(after_pullback["post_objective_new_risk_buffer"], 15)
            at_floor = store.upsert_risk_session(
                {
                    **next_day,
                    "current_equity": 5125,
                    "realized_net_pnl": 125,
                    "broker_confirmed_at": "2026-08-21T17:00:00+00:00",
                    "broker_state": evidence("floor"),
                }
            )
            self.assertFalse(at_floor["new_entries_allowed"])
            with self.assertRaisesRegex(ValueError, "conflicting risk snapshots"):
                store.upsert_risk_session(
                    {
                        **next_day,
                        "current_equity": 5126,
                        "realized_net_pnl": 126,
                        "broker_confirmed_at": "2026-08-21T17:00:00+00:00",
                        "broker_state": evidence("conflict"),
                    }
                )
            with self.assertRaisesRegex(ValueError, "broker evidence is incomplete"):
                store.upsert_risk_session(
                    {
                        **base,
                        "session_date": "2026-08-19",
                        "baseline_confirmed_at": "2026-08-19T11:00:00+00:00",
                        "broker_confirmed_at": "2026-08-19T11:00:01+00:00",
                        "broker_state": {"account_state_readable": True},
                    }
                )
            self.assertEqual(len(store.risk_sessions()), 2)
            self.assertTrue(all(not row["trade_authority"] for row in store.risk_sessions()))
            store.close()

    def test_risk_session_requires_confirmed_cash_flow_adjustment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "test.sqlite3")
            payload = {
                "account_key": "ending-7153",
                "session_date": "2026-08-24",
                "strategy_version": "test",
                "start_of_day_equity": 5_000,
                "baseline_confirmed_at": "2026-08-24T09:30:00-04:00",
                "current_equity": 5_000,
                "realized_net_pnl": 0,
                "broker_confirmed_at": "2026-08-24T09:31:00-04:00",
                "broker_state": {
                    "account_state_readable": True,
                    "orders_reconciled": True,
                    "positions_reconciled": True,
                },
            }
            with self.assertRaisesRegex(
                ValueError, "confirmed_cash_flow_adjustment"
            ):
                store.upsert_risk_session(payload)
            store.close()


if __name__ == "__main__":
    unittest.main()
