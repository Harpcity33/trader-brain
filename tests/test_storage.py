from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from titan_runtime.storage import Store


def risk_authorization(
    store: Store,
    *,
    instrument_key: str,
    thesis_key: str,
    checked_at: str = "2026-08-22T14:00:00+00:00",
    broker_confirmed_at: str = "2026-08-22T13:59:50+00:00",
    risk_action: str = "ENTRY",
    quantity: float = 40.0,
    stress_tail_loss: float | None = None,
) -> dict:
    stop_defined_loss = 0.25 * quantity
    stress_loss = quantity if stress_tail_loss is None else stress_tail_loss
    proposed_risk = max(stop_defined_loss, stress_loss)
    evidence = {
        "account_key": "ending-7153",
        "session_date": "2026-08-22",
        "strategy_version": "titan_profitability_live_2026-08-22_v2",
        "broker_confirmed_at": broker_confirmed_at,
        "broker_snapshot_valid_until": (
            datetime.fromisoformat(checked_at) + timedelta(seconds=80)
        ).isoformat(),
        "checked_at": checked_at,
        "current_equity_dollars": 5_000.0,
        "instrument_key": instrument_key,
        "thesis_key": thesis_key,
        "risk_action": risk_action,
        "reviewed_entry_price": 10.0,
        "structural_stop_price": 9.75,
        "quantity": quantity,
        "contract_multiplier": 1.0,
        "modeled_execution_loss_dollars": 0.0,
        "stress_tail_loss_dollars": stress_loss,
        "reviewed_notional_dollars": 10.0 * quantity,
        "calculated_stop_defined_loss_dollars": stop_defined_loss,
        "proposed_new_risk_dollars": proposed_risk,
        "existing_open_downside_dollars": 0.0,
        "existing_pending_risk_dollars": 0.0,
        "execution_reserve_dollars": 5.0,
        "unleveraged_buying_power_dollars": 5_000.0,
        "current_gross_exposure_dollars": 0.0,
        "working_entry_notional_dollars": 0.0,
        "broker_new_notional_capacity_dollars": 5_000.0,
        "post_order_gross_exposure_dollars": 10.0 * quantity,
        "uncredited_open_profit_dollars": 0.0,
        "open_loss_gauge_degradation_dollars": 0.0,
        "loss_lock_new_risk_capacity_dollars": 95.0,
        "profit_floor_new_risk_capacity_dollars": None,
        "dynamic_new_risk_capacity_dollars": 95.0,
    }
    reservation_clock = datetime.fromisoformat(checked_at).astimezone(timezone.utc)
    with patch("titan_runtime.storage.datetime", wraps=datetime) as clock:
        clock.now.return_value = reservation_clock
        reservation = store.reserve_risk_authorization(evidence)
    if not reservation["reserved"]:
        raise ValueError(str(reservation["reason"]))
    return reservation["authorization"]


def grade_risk_snapshot(store: Store) -> dict:
    payload = {
        "account_key": "ending-7153",
        "session_date": "2026-08-22",
        "strategy_version": "grade-test-v1",
        "start_of_day_equity": 5000,
        "baseline_confirmed_at": "2026-08-22T13:30:00+00:00",
        "current_equity": 5020,
        "realized_net_pnl": 20,
        "confirmed_cash_flow_adjustment": 0,
        "broker_confirmed_at": "2026-08-22T20:05:00+00:00",
        "broker_state": {
            "account_state_readable": True,
            "orders_reconciled": True,
            "positions_reconciled": True,
            "unleveraged_buying_power_dollars": 5020,
            "current_gross_exposure_dollars": 0,
            "working_entry_notional_dollars": 0,
            "position_count": 0,
            "working_order_count": 0,
            "working_entry_order_count": 0,
            "working_exit_order_count": 0,
        },
    }
    store.upsert_risk_session(payload)
    return payload


def performance_proposal() -> dict:
    return {
        "title": "Measure trigger-to-submit latency",
        "causal_problem": "Latency may increase avoidable entry slippage.",
        "proposed_change": "Add append-only trigger and submission timestamps.",
        "evidence": {"source_ids": ["decision-ledger", "order-ledger"]},
        "independent_sample_count": 2,
        "independent_session_count": 1,
        "expected_primary_metric": "trigger_to_submit_latency_ms",
        "possible_adverse_effect": "Additional telemetry could increase log volume.",
        "test_horizon": "Five shadow sessions.",
        "success_threshold": "At least 95% timestamp coverage.",
        "rollback_trigger": "Any live decision-path behavior changes.",
        "classification": "IMMEDIATE_SAFE",
    }


def performance_correction(
    base: dict,
    corrects_grade_id: str,
    sequence: int,
    **overrides: object,
) -> dict:
    result = {**base, **overrides}
    result["corrects_grade_id"] = corrects_grade_id
    result["correction_reason"] = f"Correction {sequence} uses newly sealed evidence."
    result["graded_at"] = (
        datetime.fromisoformat(base["graded_at"]) + timedelta(minutes=sequence)
    ).isoformat()
    supplied_manifest = result.get("evidence_manifest", base["evidence_manifest"])
    source_hashes = dict(supplied_manifest["source_hashes"])
    source_hashes["correction"] = f"{sequence:064x}"
    result["evidence_manifest"] = {
        **supplied_manifest,
        "sealed_at": (
            datetime.fromisoformat(base["evidence_manifest"]["sealed_at"])
            + timedelta(minutes=sequence)
        ).isoformat(),
        "source_hashes": source_hashes,
    }
    return result


def performance_grade_payload() -> dict:
    return {
        "account_key": "ending-7153",
        "session_date": "2026-08-22",
        "strategy_version": "grade-test-v1",
        "rubric_version": "titan_daily_performance_2026-08-23_v1",
        "graded_at": "2026-08-22T20:10:00+00:00",
        "broker_confirmed_pnl": {
            "broker_confirmed_at": "2026-08-22T20:05:00+00:00",
            "start_of_day_equity": 5000,
            "current_equity": 5020,
            "realized_net_pnl": 20,
            "confirmed_cash_flow_adjustment": 0,
            "account_day_pnl": 20,
        },
        "execution_metrics": {
            "campaigns_reviewed": 4,
            "campaigns_entered": 2,
            "campaigns_closed": 2,
            "winning_campaigns": 1,
            "losing_campaigns": 1,
            "orders_submitted": 6,
            "orders_filled": 6,
            "missed_qualified_setups": 1,
            "false_positive_entries": 1,
            "qualified_setups": 3,
            "mfe_dollars": 50,
            "mae_dollars": 25,
            "capture_ratio_pct": 40,
            "average_entry_slippage_bps": 2.5,
            "average_exit_slippage_bps": 3.0,
            "max_protection_latency_seconds": 1.2,
            "authorized_filled_risk_dollars": 40,
            "realized_after_cost_profit_dollars": 20,
            "executed_after_cost_favorable_opportunity_dollars": 40,
            "missed_after_cost_favorable_opportunity_dollars": 10,
        },
        "category_scores": {
            "account_and_risk_integrity": {
                "group": "process", "score": 90, "weight": 25,
                "applicable": True, "evidence": {
                    "eligible_items": 10, "passed_items": 9,
                    "source_ids": ["risk-snapshot"],
                },
            },
            "execution_and_protection": {
                "group": "process", "score": 90, "weight": 15,
                "applicable": True, "evidence": {
                    "eligible_items": 10, "passed_items": 9,
                    "source_ids": ["order-ledger"],
                },
            },
            "causal_data_and_evidence": {
                "group": "process", "score": 90, "weight": 15,
                "applicable": True, "evidence": {
                    "eligible_items": 10, "passed_items": 9,
                    "source_ids": ["decision-ledger"],
                },
            },
            "opportunity_coverage_and_offense": {
                "group": "process", "score": 90, "weight": 15,
                "applicable": True, "evidence": {
                    "eligible_items": 10, "passed_items": 9,
                    "source_ids": ["board-audit"],
                },
            },
            "entry_quality_and_selectivity": {
                "group": "process", "score": 90, "weight": 10,
                "applicable": True, "evidence": {
                    "eligible_items": 10, "passed_items": 9,
                    "source_ids": ["entry-reviews"],
                },
            },
            "position_management_and_profit_capture": {
                "group": "process", "score": 90, "weight": 12,
                "applicable": True, "evidence": {
                    "eligible_items": 10, "passed_items": 9,
                    "source_ids": ["campaign-events"],
                },
            },
            "audit_and_learning_quality": {
                "group": "process", "score": 90, "weight": 8,
                "applicable": True, "evidence": {
                    "eligible_items": 10, "passed_items": 9,
                    "source_ids": ["sealed-manifest"],
                },
            },
            "broker_net_pnl_vs_objective_and_boundary": {
                "group": "outcome", "score": 56.6667, "weight": 40,
                "applicable": True, "evidence": ["broker P&L"],
            },
            "net_r_after_execution_costs": {
                "group": "outcome", "score": 66.6667, "weight": 30,
                "applicable": True, "evidence": ["authorized risk"],
            },
            "risk_weighted_after_cost_opportunity_capture": {
                "group": "outcome", "score": 40, "weight": 30,
                "applicable": True, "evidence": ["MFE and fills"],
            },
        },
        "evidence_coverage_pct": 100,
        "strengths": ["Loss lock and broker reconciliation were respected."],
        "mistakes": ["One qualified setup was missed."],
        "improvement_proposals": [performance_proposal()],
        "hard_failures": {
            "unreconciled_broker_state": False,
            "order_lifecycle_or_quantity_defect": False,
            "unprotected_or_overlapping_exit_or_overnight": False,
            "prohibited_or_unauthorized_action": False,
            "loss_lock_violation": False,
            "fabricated_or_future_data": False,
        },
        "evidence_manifest": {
            "sealed_at": "2026-08-22T20:06:00+00:00",
            "eligible_items": 100,
            "verified_items": 100,
            "source_hashes": {
                "risk_snapshot": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "decision_ledger": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
            },
            "source_record_counts": {"risk_snapshot": 1, "decisions": 12},
            "massive_data_watermark": "2026-08-22T20:00:00+00:00",
            "decision_watermark": "2026-08-22T19:55:00+00:00"
        },
    }


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
            risk_snapshot = {
                "account_key": "ending-7153",
                "session_date": "2026-08-22",
                "strategy_version": "titan_profitability_live_2026-08-22_v2",
                "start_of_day_equity": 5000,
                "baseline_confirmed_at": "2026-08-22T13:30:00+00:00",
                "current_equity": 5000,
                "realized_net_pnl": 0,
                "confirmed_cash_flow_adjustment": 0,
                "broker_confirmed_at": "2026-08-22T13:59:50+00:00",
                "broker_state": {
                    "account_state_readable": True,
                    "orders_reconciled": True,
                    "positions_reconciled": True,
                    "unleveraged_buying_power_dollars": 5000,
                    "current_gross_exposure_dollars": 0,
                    "working_entry_notional_dollars": 0,
                },
            }
            store.upsert_risk_session(risk_snapshot)

            def refresh_risk_snapshot(timestamp: str) -> None:
                store.upsert_risk_session(
                    {**risk_snapshot, "broker_confirmed_at": timestamp}
                )
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
            with self.assertRaisesRegex(ValueError, "entry_risk_gate_authorization"):
                store.upsert_position_campaign(
                    {
                        **planned,
                        "status": "SUBMITTED",
                        "broker_confirmed_at": "2026-08-22T14:00:01+00:00",
                        "broker_state": {
                            "entry_order_id": "entry-order-1",
                            "entry_order_submitted_at": "2026-08-22T14:00:01+00:00",
                            "entry_order_quantity": 40,
                            "entry_cumulative_filled_quantity": 0,
                        },
                    }
                )
            entry_authorization = risk_authorization(
                store,
                instrument_key="equity:TEST",
                thesis_key="TEST",
            )
            entry_order_state = {
                "entry_order_id": "entry-order-1",
                "entry_order_submitted_at": "2026-08-22T14:00:01+00:00",
                "entry_order_quantity": 40,
                "entry_cumulative_filled_quantity": 40,
                "entry_risk_gate_authorization": entry_authorization,
            }
            direct_exposure = {
                **planned,
                "entry_price": 10.0,
                "broker_confirmed_at": "2026-08-22T14:00:01+00:00",
                "current_quantity": 40, "core_quantity": 30, "runner_quantity": 10,
                "high_water_price": 10.3, "mfe_r": 0.6, "mae_r": -0.2,
                "continuation_health": "HEALTHY",
                "broker_state": entry_order_state,
            }
            for direct_status in ("FILLED", "PROTECTED"):
                direct_broker_state = dict(entry_order_state)
                if direct_status == "PROTECTED":
                    direct_broker_state["protection_confirmed"] = True
                with self.assertRaisesRegex(ValueError, "prior durable SUBMITTED"):
                    store.upsert_position_campaign(
                        {
                            **direct_exposure,
                            "status": direct_status,
                            "broker_state": direct_broker_state,
                        }
                    )
            self.assertEqual(store.risk_authorizations()[0]["status"], "ACTIVE")

            with self.assertRaisesRegex(ValueError, "must equal entry_order_quantity"):
                store.upsert_position_campaign(
                    {
                        **planned,
                        "status": "SUBMITTED",
                        "broker_confirmed_at": "2026-08-22T14:00:01+00:00",
                        "broker_state": {
                            **entry_order_state,
                            "entry_order_quantity": 41,
                            "entry_cumulative_filled_quantity": 0,
                        },
                    }
                )
            self.assertEqual(store.risk_authorizations()[0]["status"], "ACTIVE")
            submitted = {
                **planned,
                "status": "SUBMITTED",
                "broker_confirmed_at": "2026-08-22T14:00:01+00:00",
                "broker_state": {
                    **entry_order_state,
                    "entry_cumulative_filled_quantity": 0,
                },
            }
            self.assertEqual(store.upsert_position_campaign(submitted), campaign_id)
            entry_lease = store.risk_authorizations()[0]
            self.assertEqual(entry_lease["status"], "CONSUMED")
            self.assertEqual(entry_lease["campaign_id"], campaign_id)
            self.assertEqual(entry_lease["broker_order_id"], "entry-order-1")

            partial = {
                **submitted,
                "status": "PARTIAL",
                "entry_price": 10.0,
                "broker_confirmed_at": "2026-08-22T14:00:02+00:00",
                "current_quantity": 5,
                "core_quantity": 5,
                "runner_quantity": 0,
                "broker_state": {
                    **submitted["broker_state"],
                    "entry_cumulative_filled_quantity": 5,
                },
            }
            with self.assertRaisesRegex(
                ValueError,
                "PROTECTED requires entry cumulative fill equal to order quantity",
            ):
                store.upsert_position_campaign(
                    {
                        **partial,
                        "status": "PROTECTED",
                        "broker_state": {
                            **partial["broker_state"],
                            "protection_confirmed": True,
                        },
                    }
                )
            missing_entry_auth = dict(partial["broker_state"])
            missing_entry_auth.pop("entry_risk_gate_authorization")
            with self.assertRaisesRegex(ValueError, "entry_risk_gate_authorization"):
                store.upsert_position_campaign(
                    {**partial, "broker_state": missing_entry_auth}
                )
            missing_entry_order = dict(partial["broker_state"])
            missing_entry_order.pop("entry_order_id")
            with self.assertRaisesRegex(ValueError, "entry_order_id"):
                store.upsert_position_campaign(
                    {**partial, "broker_state": missing_entry_order}
                )
            with self.assertRaisesRegex(ValueError, "entry_order_id is immutable"):
                store.upsert_position_campaign(
                    {
                        **partial,
                        "broker_state": {
                            **partial["broker_state"],
                            "entry_order_id": "changed-entry-order",
                        },
                    }
                )
            with self.assertRaisesRegex(ValueError, "authorization"):
                store.upsert_position_campaign(
                    {
                        **partial,
                        "broker_state": {
                            **partial["broker_state"],
                            "entry_risk_gate_authorization": {
                                **entry_authorization,
                                "quantity": 39,
                            },
                        },
                    }
                )
            with self.assertRaisesRegex(ValueError, "initial_quantity"):
                store.upsert_position_campaign(
                    {**partial, "initial_quantity": 41}
                )
            self.assertEqual(store.upsert_position_campaign(partial), campaign_id)
            with self.assertRaisesRegex(ValueError, "may not move backward"):
                store.upsert_position_campaign(
                    {
                        **partial,
                        "broker_confirmed_at": "2026-08-22T14:00:03+00:00",
                        "current_quantity": 4,
                        "core_quantity": 4,
                        "broker_state": {
                            **partial["broker_state"],
                            "entry_cumulative_filled_quantity": 4,
                        },
                    }
                )
            with self.assertRaisesRegex(ValueError, "within entry_order_quantity"):
                store.upsert_position_campaign(
                    {
                        **partial,
                        "status": "FILLED",
                        "broker_confirmed_at": "2026-08-22T14:00:03+00:00",
                        "current_quantity": 50,
                        "core_quantity": 40,
                        "runner_quantity": 10,
                        "broker_state": {
                            **partial["broker_state"],
                            "entry_cumulative_filled_quantity": 50,
                        },
                    }
                )
            after_rejected_overfill = store.position_campaigns()[0]
            self.assertEqual(after_rejected_overfill["status"], "PARTIAL")
            self.assertEqual(after_rejected_overfill["current_quantity"], 5)
            self.assertEqual(
                after_rejected_overfill["broker_state"][
                    "entry_cumulative_filled_quantity"
                ],
                5,
            )
            entry_filled = {
                **partial,
                "status": "FILLED",
                "broker_confirmed_at": "2026-08-22T14:00:03+00:00",
                "current_quantity": 40,
                "core_quantity": 30,
                "runner_quantity": 10,
                "broker_state": {
                    **partial["broker_state"],
                    "entry_cumulative_filled_quantity": 40,
                },
            }
            self.assertEqual(store.upsert_position_campaign(entry_filled), campaign_id)
            filled = {
                **entry_filled,
                "status": "PROTECTED",
                "broker_confirmed_at": "2026-08-22T14:00:04+00:00",
                "broker_state": {
                    **entry_filled["broker_state"],
                    "protection_confirmed": True,
                },
            }
            self.assertEqual(store.upsert_position_campaign(filled), campaign_id)
            row = store.position_campaigns()[0]
            self.assertEqual(row["original_stop"], 9.75)
            self.assertFalse(row["trade_authority"])
            self.assertTrue(row["broker_state"]["protection_confirmed"])
            with self.assertRaisesRegex(ValueError, "immutable|does not match original_stop"):
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
            with self.assertRaisesRegex(ValueError, "explicit ADD transition"):
                store.upsert_position_campaign(
                    {
                        **filled,
                        "broker_confirmed_at": "2026-08-22T14:00:06+00:00",
                        "current_quantity": 45,
                        "core_quantity": 35,
                        "runner_quantity": 10,
                        "broker_state": filled["broker_state"],
                    }
                )
            with self.assertRaisesRegex(ValueError, "awaiting a newer broker reconciliation"):
                risk_authorization(
                    store,
                    instrument_key="equity:TEST",
                    thesis_key="TEST",
                    checked_at="2026-08-22T14:00:05+00:00",
                    risk_action="ADD",
                    quantity=5,
                    stress_tail_loss=5,
                )
            refresh_risk_snapshot("2026-08-22T14:00:05+00:00")
            self.assertEqual(store.risk_authorizations()[0]["status"], "RECONCILED")
            add_authorization = risk_authorization(
                store,
                instrument_key="equity:TEST",
                thesis_key="TEST",
                checked_at="2026-08-22T14:00:05+00:00",
                broker_confirmed_at="2026-08-22T14:00:05+00:00",
                risk_action="ADD",
                quantity=5,
                stress_tail_loss=5,
            )
            add_submitted = {
                **filled,
                "broker_confirmed_at": "2026-08-22T14:00:06+00:00",
                "last_action": "ADD_SUBMITTED",
                "broker_state": {
                    **filled["broker_state"],
                    "add_order_id": "add-order-1",
                    "add_order_submitted_at": "2026-08-22T14:00:06+00:00",
                    "add_order_quantity": 5,
                    "add_cumulative_filled_quantity": 0,
                    "risk_gate_authorization": add_authorization,
                },
            }
            self.assertEqual(store.upsert_position_campaign(add_submitted), campaign_id)
            add_partial = {
                **add_submitted,
                "broker_confirmed_at": "2026-08-22T14:00:07+00:00",
                "last_action": "ADD_PARTIAL",
                "current_quantity": 42,
                "core_quantity": 32,
                "runner_quantity": 10,
                "broker_state": {
                    **add_submitted["broker_state"],
                    "add_cumulative_filled_quantity": 2,
                },
            }
            with self.assertRaisesRegex(ValueError, "submission timestamp is immutable"):
                store.upsert_position_campaign(
                    {
                        **add_partial,
                        "broker_state": {
                            **add_partial["broker_state"],
                            "add_order_submitted_at": "2026-08-22T14:00:07+00:00",
                        },
                    }
                )
            self.assertEqual(store.upsert_position_campaign(add_partial), campaign_id)
            add_filled = {
                **add_partial,
                "broker_confirmed_at": "2026-08-22T14:00:08+00:00",
                "last_action": "ADD_FILLED",
                "current_quantity": 45,
                "core_quantity": 35,
                "runner_quantity": 10,
                "broker_state": {
                    **add_submitted["broker_state"],
                    "add_cumulative_filled_quantity": 5,
                },
            }
            self.assertEqual(store.upsert_position_campaign(add_filled), campaign_id)

            closed_id = store.upsert_position_campaign(
                {
                    **filled,
                    "status": "CLOSED",
                    "broker_confirmed_at": "2026-08-22T14:00:09+00:00",
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
            self.assertEqual(len(store.position_campaign_events()), 10)
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
            refresh_risk_snapshot("2026-08-22T14:00:10+00:00")
            self.assertEqual(store.risk_authorizations()[0]["status"], "RECONCILED")
            unprotected_authorization = risk_authorization(
                store,
                instrument_key="equity:NOSTOP",
                thesis_key="NOSTOP",
                checked_at="2026-08-22T14:00:10+00:00",
                broker_confirmed_at="2026-08-22T14:00:10+00:00",
            )
            with self.assertRaisesRegex(ValueError, "prior durable SUBMITTED"):
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
                        "broker_confirmed_at": "2026-08-22T14:00:11+00:00",
                        "broker_state": {
                            "protection_confirmed": True,
                            "entry_order_id": "entry-order-2",
                            "entry_order_submitted_at": "2026-08-22T14:00:11+00:00",
                            "entry_order_quantity": 40,
                            "entry_cumulative_filled_quantity": 40,
                            "entry_risk_gate_authorization": unprotected_authorization,
                        },
                    }
                )
            store.close()

    def test_risk_authorization_lease_binding_is_durable_and_single_use(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "test.sqlite3")
            store.upsert_risk_session(
                {
                    "account_key": "ending-7153",
                    "session_date": "2026-08-22",
                    "strategy_version": "titan_profitability_live_2026-08-22_v2",
                    "start_of_day_equity": 5000,
                    "baseline_confirmed_at": "2026-08-22T13:30:00+00:00",
                    "current_equity": 5000,
                    "realized_net_pnl": 0,
                    "confirmed_cash_flow_adjustment": 0,
                    "broker_confirmed_at": "2026-08-22T13:59:50+00:00",
                    "broker_state": {
                        "account_state_readable": True,
                        "orders_reconciled": True,
                        "positions_reconciled": True,
                        "unleveraged_buying_power_dollars": 5000,
                        "current_gross_exposure_dollars": 0,
                        "working_entry_notional_dollars": 0,
                    },
                }
            )
            with self.assertRaisesRegex(ValueError, "not durable"):
                store.bind_risk_authorization(
                    {"authorization_id": "fabricated"},
                    campaign_id="campaign-fake",
                    broker_order_id="order-fake",
                    order_submitted_at="2026-08-22T14:00:01+00:00",
                )

            released = risk_authorization(
                store,
                instrument_key="equity:RELEASED",
                thesis_key="RELEASED",
            )
            with patch("titan_runtime.storage.datetime", wraps=datetime) as clock:
                clock.now.return_value = datetime.fromisoformat(
                    "2026-08-22T14:00:00+00:00"
                )
                store.release_risk_authorization(
                    released["authorization_id"], "review_abandoned"
                )
            with self.assertRaisesRegex(ValueError, "not active: RELEASED"):
                store.bind_risk_authorization(
                    released,
                    campaign_id="campaign-released",
                    broker_order_id="order-released",
                    order_submitted_at="2026-08-22T14:00:01+00:00",
                )

            expired = risk_authorization(
                store,
                instrument_key="equity:EXPIRED",
                thesis_key="EXPIRED",
                checked_at="2026-08-22T14:00:01+00:00",
            )
            store.conn.execute(
                "UPDATE risk_authorizations SET status='EXPIRED' WHERE authorization_id=?",
                (expired["authorization_id"],),
            )
            with self.assertRaisesRegex(ValueError, "not active: EXPIRED"):
                store.bind_risk_authorization(
                    expired,
                    campaign_id="campaign-expired",
                    broker_order_id="order-expired",
                    order_submitted_at="2026-08-22T14:00:02+00:00",
                )

            active = risk_authorization(
                store,
                instrument_key="equity:BOUND",
                thesis_key="BOUND",
                checked_at="2026-08-22T14:00:02+00:00",
            )
            store.bind_risk_authorization(
                active,
                campaign_id="campaign-bound",
                broker_order_id="order-bound",
                order_submitted_at="2026-08-22T14:00:03+00:00",
            )
            store.bind_risk_authorization(
                active,
                campaign_id="campaign-bound",
                broker_order_id="order-bound",
                order_submitted_at="2026-08-22T14:00:03+00:00",
            )
            with self.assertRaisesRegex(ValueError, "bound to another order"):
                store.bind_risk_authorization(
                    active,
                    campaign_id="campaign-bound",
                    broker_order_id="order-other",
                    order_submitted_at="2026-08-22T14:00:03+00:00",
                )
            with self.assertRaisesRegex(ValueError, "bound to another order"):
                store.bind_risk_authorization(
                    active,
                    campaign_id="campaign-other",
                    broker_order_id="order-bound",
                    order_submitted_at="2026-08-22T14:00:03+00:00",
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
                    "unleveraged_buying_power_dollars": 5000,
                    "current_gross_exposure_dollars": 0,
                    "working_entry_notional_dollars": 0,
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
            self.assertEqual(opening["loss_headroom_to_lock"], 100)

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
            self.assertEqual(objective["loss_headroom_to_lock"], 260)
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
                    "unleveraged_buying_power_dollars": 5000,
                    "current_gross_exposure_dollars": 0,
                    "working_entry_notional_dollars": 0,
                },
            }
            with self.assertRaisesRegex(
                ValueError, "confirmed_cash_flow_adjustment"
            ):
                store.upsert_risk_session(payload)
            store.close()

    def test_daily_performance_grade_is_idempotent_append_only_and_revisioned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "test.sqlite3")
            risk_snapshot = grade_risk_snapshot(store)
            payload = performance_grade_payload()

            first = store.record_performance_grade(payload)
            self.assertFalse(first["idempotent_replay"])
            self.assertEqual(first["revision"], 1)
            self.assertEqual(first["process_score"], 90)
            self.assertEqual(first["outcome_score"], 54.6667)
            self.assertEqual(first["raw_overall_score"], 82.9333)
            self.assertEqual(first["overall_score"], 82.9333)
            self.assertEqual(first["letter_grade"], "B-")
            self.assertEqual(first["grade_status"], "FINAL")
            self.assertTrue(first["is_canonical"])
            self.assertFalse(first["authoritative_evidence_verified"])
            self.assertFalse(first["evidence_sufficient_for_change_evaluation"])
            self.assertFalse(first["change_authority"])

            replay = store.record_performance_grade(payload)
            self.assertTrue(replay["idempotent_replay"])
            self.assertEqual(replay["grade_id"], first["grade_id"])
            self.assertEqual(replay["revision"], 1)

            with self.assertRaisesRegex(ValueError, "corrections are disabled"):
                store.record_performance_grade({**payload, "notes": "different"})

            with self.assertRaisesRegex(ValueError, "corrections are disabled"):
                store.record_performance_grade(performance_correction(
                    payload,
                    first["grade_id"],
                    1,
                    notes="New evidence arrived after sealing.",
                ))
            self.assertEqual(len(store.performance_grades()), 1)
            self.assertEqual(len(store.performance_grades(include_revisions=True)), 1)
            self.assertEqual(
                store.performance_grade(
                    account_key="ending-7153",
                    session_date="2026-08-22",
                    strategy_version="grade-test-v1",
                )["grade_id"],
                first["grade_id"],
            )
            with self.assertRaisesRegex(Exception, "append-only"):
                store.conn.execute(
                    "UPDATE daily_performance_grades SET rubric_version='x' WHERE grade_id=?",
                    (first["grade_id"],),
                )
            with self.assertRaisesRegex(Exception, "append-only"):
                store.conn.execute(
                    "DELETE FROM daily_performance_grades WHERE grade_id=?",
                    (first["grade_id"],),
                )
            store.upsert_risk_session({
                **risk_snapshot,
                "broker_confirmed_at": "2026-08-22T20:07:00+00:00",
            })
            advanced_state_replay = store.record_performance_grade(payload)
            self.assertTrue(advanced_state_replay["idempotent_replay"])
            self.assertEqual(advanced_state_replay["grade_id"], first["grade_id"])
            self.assertEqual(len(store.performance_grades(include_revisions=True)), 1)
            self.assertFalse(advanced_state_replay["change_authority"])
            store.close()

    def test_daily_grade_evidence_and_safety_ceilings_fail_closed(self) -> None:
        payload = performance_grade_payload()

        def record_variant(candidate: dict) -> dict:
            with tempfile.TemporaryDirectory() as directory:
                store = Store(Path(directory) / "test.sqlite3")
                grade_risk_snapshot(store)
                grade = store.record_performance_grade(candidate)
                store.close()
                return grade

        low_confidence = record_variant({
            **payload,
            "evidence_coverage_pct": 90,
            "evidence_manifest": {
                **payload["evidence_manifest"], "verified_items": 90,
            },
        })
        self.assertEqual(low_confidence["grade_status"], "FINAL")
        self.assertEqual(low_confidence["evidence_ceiling"], 69)
        self.assertEqual(low_confidence["overall_score"], 69)
        self.assertEqual(low_confidence["letter_grade"], "D")
        self.assertFalse(low_confidence["evidence_sufficient_for_change_evaluation"])

        early_incomplete = {
            **payload,
            "broker_confirmed_pnl": None,
            "hard_failures": {
                **payload["hard_failures"],
                "unreconciled_broker_state": True,
            },
        }
        with self.assertRaisesRegex(ValueError, "may not be sealed before 17:00"):
            record_variant(early_incomplete)
        incomplete = record_variant({
            **early_incomplete,
            "graded_at": "2026-08-22T21:00:00+00:00",
        })
        self.assertEqual(incomplete["grade_status"], "INCOMPLETE")
        self.assertIsNone(incomplete["overall_score"])
        self.assertIn("final_broker_confirmed_pnl_missing", incomplete["incomplete_reasons"])
        self.assertIn("broker_state_unreconciled", incomplete["incomplete_reasons"])

        lifecycle_failure = record_variant({
            **payload,
            "hard_failures": {
                **payload["hard_failures"],
                "order_lifecycle_or_quantity_defect": True,
            },
        })
        self.assertEqual(lifecycle_failure["hard_ceiling"], 59)
        self.assertEqual(lifecycle_failure["overall_score"], 59)

        protection_failure = record_variant({
            **payload,
            "hard_failures": {
                **payload["hard_failures"],
                "unprotected_or_overlapping_exit_or_overnight": True,
            },
        })
        self.assertEqual(protection_failure["hard_ceiling"], 39)
        self.assertEqual(protection_failure["overall_score"], 39)

        safety_failure = record_variant({
            **payload,
            "hard_failures": {
                **payload["hard_failures"],
                "loss_lock_violation": True,
            },
        })
        self.assertEqual(safety_failure["overall_score"], 0)
        self.assertEqual(safety_failure["letter_grade"], "F")
        self.assertTrue(safety_failure["hard_fail"])

        invalid_score = {
            **payload,
            "category_scores": {
                **payload["category_scores"],
                "execution_and_protection": {
                    **payload["category_scores"]["execution_and_protection"],
                    "score": 100.01,
                },
            },
        }
        with self.assertRaisesRegex(ValueError, "within 0..100"):
            record_variant(invalid_score)

        with self.assertRaisesRegex(ValueError, "must contain exactly"):
            record_variant({
                **payload,
                "hard_failures": {
                    "unreconciled_broker_state": False,
                    "loss_lock_violation": False,
                },
            })

        with self.assertRaisesRegex(ValueError, "positive broker account-day P&L"):
            record_variant({
                **payload,
                "broker_confirmed_pnl": {
                    **payload["broker_confirmed_pnl"],
                    "current_equity": 5021,
                    "account_day_pnl": 21,
                },
            })

    def test_compliant_gap_loss_does_not_infer_a_hard_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "test.sqlite3")
            store.upsert_risk_session({
                "account_key": "ending-7153",
                "session_date": "2026-08-22",
                "strategy_version": "gap-loss-test-v1",
                "start_of_day_equity": 5000,
                "baseline_confirmed_at": "2026-08-22T13:30:00+00:00",
                "current_equity": 4880,
                "realized_net_pnl": -120,
                "confirmed_cash_flow_adjustment": 0,
                "broker_confirmed_at": "2026-08-22T20:05:00+00:00",
                "broker_state": {
                    "account_state_readable": True,
                    "orders_reconciled": True,
                    "positions_reconciled": True,
                    "unleveraged_buying_power_dollars": 4880,
                    "current_gross_exposure_dollars": 0,
                    "working_entry_notional_dollars": 0,
                    "position_count": 0,
                    "working_order_count": 0,
                    "working_entry_order_count": 0,
                    "working_exit_order_count": 0,
                },
            })
            base = performance_grade_payload()
            payload = {
                **base,
                "strategy_version": "gap-loss-test-v1",
                "broker_confirmed_pnl": {
                    **base["broker_confirmed_pnl"],
                    "current_equity": 4880,
                    "realized_net_pnl": -120,
                    "account_day_pnl": -120,
                },
                "category_scores": {
                    **base["category_scores"],
                    "broker_net_pnl_vs_objective_and_boundary": {
                        **base["category_scores"]["broker_net_pnl_vs_objective_and_boundary"],
                        "score": 0,
                    },
                    "net_r_after_execution_costs": {
                        **base["category_scores"]["net_r_after_execution_costs"],
                        "score": 0,
                    },
                    "risk_weighted_after_cost_opportunity_capture": {
                        **base["category_scores"]["risk_weighted_after_cost_opportunity_capture"],
                        "score": 0,
                    },
                },
                "execution_metrics": {
                    **base["execution_metrics"],
                    "capture_ratio_pct": 0,
                    "realized_after_cost_profit_dollars": 0,
                },
                "mistakes": [
                    "A compliant stop experienced gap-through loss; no post-lock order occurred."
                ],
            }
            grade = store.record_performance_grade(payload)
            self.assertEqual(grade["broker_confirmed_pnl"]["account_day_pnl"], -120)
            self.assertIsNone(grade["hard_ceiling"])
            self.assertFalse(grade["hard_fail"])
            self.assertGreater(grade["overall_score"], 0)
            store.close()

    def test_change_evaluation_requires_process_score_of_at_least_85(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "test.sqlite3")
            grade_risk_snapshot(store)
            base = performance_grade_payload()
            low_process_categories = {}
            for name, category in base["category_scores"].items():
                if category["group"] == "process":
                    low_process_categories[name] = {
                        **category,
                        "score": 84,
                        "evidence": {
                            "eligible_items": 100, "passed_items": 84,
                            "source_ids": [f"{name}-audit"],
                        },
                    }
                else:
                    low_process_categories[name] = category
            grade = store.record_performance_grade({
                **base,
                "category_scores": low_process_categories,
            })
            self.assertEqual(grade["grade_status"], "FINAL")
            self.assertEqual(grade["evidence_coverage_pct"], 100)
            self.assertIsNone(grade["hard_ceiling"])
            self.assertFalse(grade["evidence_sufficient_for_change_evaluation"])
            store.close()

    def test_daily_grade_requires_terminal_broker_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "test.sqlite3")
            risk = grade_risk_snapshot(store)
            store.close()

        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "test.sqlite3")
            store.upsert_risk_session({
                **risk,
                "broker_state": {
                    **risk["broker_state"],
                    "working_exit_order_count": 1,
                },
            })
            with self.assertRaisesRegex(ValueError, "not terminal: working_exit_order_count"):
                store.record_performance_grade(performance_grade_payload())
            store.close()

    def test_daily_grade_allows_an_evidence_backed_no_change_day(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "test.sqlite3")
            grade_risk_snapshot(store)
            base = performance_grade_payload()
            grade = store.record_performance_grade({
                **base,
                "improvement_proposals": [],
                "no_change_reason": (
                    "No causal defect or sufficiently supported improvement hypothesis "
                    "was found in the sealed evidence."
                ),
            })
            self.assertEqual(grade["improvement_proposals"], [])
            self.assertIn("No causal defect", grade["no_change_reason"])
            self.assertFalse(grade["change_authority"])
            store.close()

    def test_strategy_change_proposal_cannot_self_approve(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "test.sqlite3")
            proposal = {
                "title": "Shadow telemetry experiment",
                "change_class": "experimental_rule",
                "category": "analytics",
                "evidence": {"grade_id": "grade-1"},
                "expected_effect": "Improve measurement coverage.",
            }
            with self.assertRaisesRegex(ValueError, "must start as proposed"):
                store.propose_strategy_change({**proposal, "status": "approved"})
            with self.assertRaisesRegex(ValueError, "cannot self-assert"):
                store.propose_strategy_change({**proposal, "production_approved": True})
            change_id = store.propose_strategy_change(proposal)
            stored = store.strategy_changes()[0]
            self.assertEqual(stored["change_id"], change_id)
            self.assertEqual(stored["status"], "proposed")
            self.assertFalse(stored["production_approved"])
            store.close()


if __name__ == "__main__":
    unittest.main()
