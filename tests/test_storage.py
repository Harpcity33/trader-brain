from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from titan_runtime.storage import (
    PRETRADE_RISK_FACTS_SCHEMA_VERSION,
    Store,
    TITAN_LIVE_DECISION_CONTRACT_HASH,
    TITAN_LIVE_DECISION_CONTRACT_VERSION,
    TITAN_LIVE_PILOT_ID,
)


LIVE_ATTRIBUTION = {
    "pilot_id": TITAN_LIVE_PILOT_ID,
    "book_mode": "LIVE",
    "decision_contract_version": TITAN_LIVE_DECISION_CONTRACT_VERSION,
    "decision_contract_hash": TITAN_LIVE_DECISION_CONTRACT_HASH,
}
SHADOW_ATTRIBUTION = {**LIVE_ATTRIBUTION, "book_mode": "SHADOW"}
CATALYST_SWING_DECISION_CONTRACT_HASH = (
    "1b1c1f1500d215639430251810a78cdcce60ecea72cf07848af5162d1e540d56"
)


def pilot_fact_sheet_payload(
    *,
    pilot_id: str = "titan_catalyst_swing",
    contract_version: str = "titan_catalyst_swing_2026-08-23_v1",
    contract_hash: str = CATALYST_SWING_DECISION_CONTRACT_HASH,
    evidence_status: str = "INSUFFICIENT",
    fact_sheet_version: str = "2026-08-23-v1",
) -> dict:
    analytical = {
        "net_expectancy_r_after_costs": None,
        "clustered_95pct_lower_bound_expectancy_r": None,
        "clustered_95pct_upper_bound_expectancy_r": None,
        "win_rate_pct": None,
        "expected_shortfall_95_r": None,
        "expected_shortfall_99_r": None,
        "max_drawdown_r": None,
        "profit_factor": None,
        "execution_shortfall_bps": None,
        "entry_slippage_bps": None,
        "exit_slippage_bps": None,
        "top_day_profit_concentration_pct": None,
        "largest_winner_profit_concentration_pct": None,
    }
    counts = {
        "effective_independent_sample_size": 0,
        "evidence_coverage_pct": 0,
        "quote_coverage_pct": 0,
        "fill_rate_pct": 0,
        "no_fill_rate_pct": 0,
        "stale_data_rate_pct": 0,
        "order_reject_rate_pct": 0,
        "position_episode_count": 0,
        "session_count": 0,
        "underlying_count": 0,
        "distinct_ticker_session_count": 0,
        "control_breach_count": 0,
    }
    if evidence_status == "ESTIMABLE":
        analytical = {
            "net_expectancy_r_after_costs": 0.12,
            "clustered_95pct_lower_bound_expectancy_r": 0.02,
            "clustered_95pct_upper_bound_expectancy_r": 0.22,
            "win_rate_pct": 54,
            "expected_shortfall_95_r": 0.8,
            "expected_shortfall_99_r": 1.1,
            "max_drawdown_r": 2.4,
            "profit_factor": 1.35,
            "execution_shortfall_bps": 3.2,
            "entry_slippage_bps": 2.1,
            "exit_slippage_bps": 1.1,
            "top_day_profit_concentration_pct": 18,
            "largest_winner_profit_concentration_pct": 12,
        }
        counts = {
            **counts,
            "effective_independent_sample_size": 81.5,
            "evidence_coverage_pct": 98,
            "quote_coverage_pct": 99,
            "fill_rate_pct": 75,
            "no_fill_rate_pct": 25,
            "stale_data_rate_pct": 0.5,
            "order_reject_rate_pct": 0.2,
            "position_episode_count": 120,
            "session_count": 45,
            "underlying_count": 35,
            "distinct_ticker_session_count": 110,
        }
    return {
        "pilot_id": pilot_id,
        "pilot_name": pilot_id.replace("_", " ").title(),
        "book_mode": "SHADOW",
        "fact_sheet_version": fact_sheet_version,
        "decision_contract_version": contract_version,
        "decision_contract_hash": contract_hash,
        "policy_hash": "e" * 64,
        "measured_through": "2026-08-23T20:00:00+00:00",
        "evidence_status": evidence_status,
        "metrics": {**analytical, **counts},
        "known_failure_modes": [],
        "evidence": {"source_record_counts": {"position_episodes": counts["position_episode_count"]}},
    }


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
    entry_stop = store.entry_stop_status()
    risk_session = store.risk_session("ending-7153", "2026-08-22")
    if risk_session is None:
        raise ValueError("risk test helper requires a durable risk session")
    reviewed_notional = 10.0 * quantity
    symbol = instrument_key.split(":")[-1].upper()
    evidence = {
        **LIVE_ATTRIBUTION,
        "schema_version": PRETRADE_RISK_FACTS_SCHEMA_VERSION,
        "account_key": "ending-7153",
        "session_date": "2026-08-22",
        "strategy_version": "titan_profitability_live_2026-08-22_v2",
        "broker_confirmed_at": broker_confirmed_at,
        "broker_snapshot_hash": risk_session["broker_snapshot_hash"],
        "broker_snapshot_valid_until": (
            datetime.fromisoformat(checked_at) + timedelta(seconds=80)
        ).isoformat(),
        "checked_at": checked_at,
        "current_equity_dollars": 5_000.0,
        "instrument_key": instrument_key,
        "symbol": symbol,
        "direction": "UP",
        "asset_class": "EQUITY",
        "thesis_key": thesis_key,
        "risk_action": risk_action,
        "preview_id": f"preview-{symbol.lower()}",
        "preview_confirmed_at": checked_at,
        "preview_account_key": "ending-7153",
        "preview_instrument_key": instrument_key,
        "preview_side": "BUY",
        "preview_order_quantity": quantity,
        "preview_limit_price": 10.0,
        "preview_equity_dollars": 5_000.0,
        "preview_current_gross_exposure_dollars": 0.0,
        "preview_working_entry_notional_dollars": 0.0,
        "preview_projected_cost_dollars": reviewed_notional,
        "reviewed_entry_price": 10.0,
        "structural_stop_price": 9.75,
        "quantity": quantity,
        "contract_multiplier": 1.0,
        "modeled_execution_loss_dollars": 0.0,
        "stress_tail_loss_dollars": stress_loss,
        "reviewed_notional_dollars": reviewed_notional,
        "estimated_slippage_dollars": 0.0,
        "maximum_acceptable_slippage_dollars": 1.0,
        "maximum_contractual_loss_dollars": None,
        "notional_pct_of_current_equity": reviewed_notional / 5_000 * 100,
        "calculated_stop_defined_loss_dollars": stop_defined_loss,
        "proposed_new_risk_dollars": proposed_risk,
        "existing_open_downside_dollars": 0.0,
        "existing_pending_risk_dollars": 0.0,
        "execution_reserve_dollars": 5.0,
        "unleveraged_buying_power_dollars": 5_000.0,
        "expected_unleveraged_buying_power_dollars": 5_000.0,
        "buying_power_mismatch_dollars": 0.0,
        "buying_power_mismatch_tolerance_dollars": 50.0,
        "buying_power_mismatch_detected": False,
        "emergency_entry_stop_generation": entry_stop["generation"],
        "emergency_entry_stop_state_hash": entry_stop["state_hash"],
        "broker_ack_timeout_seconds": 10,
        "current_gross_exposure_dollars": 0.0,
        "working_entry_notional_dollars": 0.0,
        "broker_new_notional_capacity_dollars": 5_000.0,
        "post_order_gross_exposure_dollars": reviewed_notional,
        "projected_remaining_buying_power_dollars": 5_000.0 - reviewed_notional,
        "account_day_loss_headroom_dollars": 100.0,
        "uncredited_open_profit_dollars": 0.0,
        "open_loss_gauge_degradation_dollars": 0.0,
        "loss_lock_new_risk_capacity_dollars": 95.0,
        "profit_floor_new_risk_capacity_dollars": None,
        "dynamic_new_risk_capacity_dollars": 95.0,
        "submission_intent_at": None,
        "broker_ack_deadline_at": None,
    }
    reservation_clock = datetime.fromisoformat(checked_at).astimezone(timezone.utc)
    with patch("titan_runtime.storage.datetime", wraps=datetime) as clock:
        clock.now.return_value = reservation_clock
        reservation = store.reserve_risk_authorization(evidence)
    if not reservation["reserved"]:
        raise ValueError(str(reservation["reason"]))
    return reservation["authorization"]


def resolve_order_found(
    store: Store,
    authorization: dict,
    *,
    broker_order_id: str,
    attempted_at: str,
    broker_confirmed_at: str,
) -> tuple[dict, str, str]:
    unknown = store.mark_risk_submission_unknown(
        authorization["authorization_id"],
        attempted_at=attempted_at,
        reason="broker request is leaving the executor",
    )
    intent_id = unknown["submission_intent"]["intent_id"]
    resolution_key = (
        f"resolution:{authorization['authorization_id']}:{broker_order_id}"
    )
    current = store.risk_session(
        authorization["account_key"], authorization["session_date"]
    )
    if current is None:
        raise ValueError("missing risk session")
    store.upsert_risk_session({
        **LIVE_ATTRIBUTION,
        "account_key": current["account_key"],
        "session_date": current["session_date"],
        "strategy_version": current["strategy_version"],
        "start_of_day_equity": current["start_of_day_equity"],
        "baseline_confirmed_at": current["baseline_confirmed_at"],
        "current_equity": current["current_equity"],
        "realized_net_pnl": current["realized_net_pnl"],
        "confirmed_cash_flow_adjustment": current[
            "confirmed_cash_flow_adjustment"
        ],
        "broker_confirmed_at": broker_confirmed_at,
        "broker_state": {
            **current["broker_state"],
            "submission_unknown_resolutions": [{
                "resolution_key": resolution_key,
                "authorization_id": authorization["authorization_id"],
                "intent_id": intent_id,
                "resolution_state": "ORDER_FOUND",
                "broker_confirmed_at": broker_confirmed_at,
                "broker_order_id": broker_order_id,
                "evidence": {
                    "matching_order_count": 1,
                    "matching_position_count": 0,
                },
            }],
        },
    })
    durable = next(
        row for row in store.risk_authorizations(1000)
        if row["authorization_id"] == authorization["authorization_id"]
    )
    return durable["evidence"], intent_id, resolution_key


def grade_risk_snapshot(store: Store) -> dict:
    payload = {
        **LIVE_ATTRIBUTION,
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
        **LIVE_ATTRIBUTION,
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
                **SHADOW_ATTRIBUTION,
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
                {**SHADOW_ATTRIBUTION, "extension_atr": 4.4},
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
                **SHADOW_ATTRIBUTION,
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
            catalyst_payload = {
                **payload,
                "pilot_id": "titan_catalyst_swing",
                "book_mode": "SHADOW",
                "decision_contract_version": (
                    "titan_catalyst_swing_2026-08-23_v1"
                ),
                "decision_contract_hash": CATALYST_SWING_DECISION_CONTRACT_HASH,
                "price": 20,
                "signal_strength": 99,
            }
            store.upsert_candidate(catalyst_payload)
            self.assertEqual(store.get_candidate("TEST")["price"], 10)
            catalyst_row = store.get_candidate(
                "TEST", pilot_id="titan_catalyst_swing", book_mode="SHADOW"
            )
            assert catalyst_row is not None
            self.assertEqual(catalyst_row["price"], 20)
            self.assertEqual(store.leaderboard(1)[0]["symbol"], "AVAILABLE")
            self.assertEqual(
                store.leaderboard(
                    1, pilot_id="titan_catalyst_swing", book_mode="SHADOW"
                )[0]["symbol"],
                "TEST",
            )
            store.clear_candidates()
            self.assertIsNone(store.get_candidate("TEST"))
            self.assertIsNotNone(store.get_candidate(
                "TEST", pilot_id="titan_catalyst_swing", book_mode="SHADOW"
            ))
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
                **SHADOW_ATTRIBUTION,
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
                **LIVE_ATTRIBUTION,
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
                **LIVE_ATTRIBUTION,
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
            (
                entry_authorization,
                entry_intent_id,
                entry_resolution_key,
            ) = resolve_order_found(
                store,
                entry_authorization,
                broker_order_id="entry-order-1",
                attempted_at="2026-08-22T14:00:01+00:00",
                broker_confirmed_at="2026-08-22T14:00:01.500000+00:00",
            )
            entry_order_state = {
                "entry_order_id": "entry-order-1",
                "entry_order_submitted_at": "2026-08-22T14:00:01+00:00",
                "entry_submission_intent_id": entry_intent_id,
                "entry_order_resolution_key": entry_resolution_key,
                "entry_order_acknowledged_at": "2026-08-22T14:00:01+00:00",
                "entry_order_ack_deadline_at": "2026-08-22T14:00:11+00:00",
                "entry_order_ack_state": "ON_TIME",
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
            self.assertEqual(
                store.risk_authorizations()[0]["status"], "SUBMISSION_UNKNOWN"
            )

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
            self.assertEqual(
                store.risk_authorizations()[0]["status"], "SUBMISSION_UNKNOWN"
            )
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
            with self.assertRaisesRegex(
                ValueError, "entry_order_id is immutable|exact ORDER_FOUND"
            ):
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
            self.assertEqual(row["entry_submission_intent_id"], entry_intent_id)
            self.assertEqual(
                row["entry_order_resolution_key"], entry_resolution_key
            )
            self.assertEqual(row["entry_order_ack_state"], "ON_TIME")
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
                    broker_confirmed_at="2026-08-22T14:00:01.500000+00:00",
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
            (
                add_authorization,
                add_intent_id,
                add_resolution_key,
            ) = resolve_order_found(
                store,
                add_authorization,
                broker_order_id="add-order-1",
                attempted_at="2026-08-22T14:00:06+00:00",
                broker_confirmed_at="2026-08-22T14:00:06.500000+00:00",
            )
            add_submitted = {
                **filled,
                "broker_confirmed_at": "2026-08-22T14:00:06+00:00",
                "last_action": "ADD_SUBMITTED",
                "broker_state": {
                    **filled["broker_state"],
                    "add_order_id": "add-order-1",
                    "add_order_submitted_at": "2026-08-22T14:00:06+00:00",
                    "add_submission_intent_id": add_intent_id,
                    "add_order_resolution_key": add_resolution_key,
                    "add_order_acknowledged_at": "2026-08-22T14:00:06+00:00",
                    "add_order_ack_deadline_at": "2026-08-22T14:00:16+00:00",
                    "add_order_ack_state": "ON_TIME",
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
            with self.assertRaisesRegex(
                ValueError,
                "submission timestamp is immutable|ack deadline is inconsistent|"
                "ack cannot precede submission",
            ):
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
            (
                unprotected_authorization,
                unprotected_intent_id,
                unprotected_resolution_key,
            ) = resolve_order_found(
                store,
                unprotected_authorization,
                broker_order_id="entry-order-2",
                attempted_at="2026-08-22T14:00:11+00:00",
                broker_confirmed_at="2026-08-22T14:00:11.500000+00:00",
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
                            "entry_submission_intent_id": unprotected_intent_id,
                            "entry_order_resolution_key": unprotected_resolution_key,
                            "entry_order_acknowledged_at": "2026-08-22T14:00:11+00:00",
                            "entry_order_ack_deadline_at": "2026-08-22T14:00:21+00:00",
                            "entry_order_ack_state": "ON_TIME",
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
                    **LIVE_ATTRIBUTION,
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
                    submission_intent_id="intent-fake",
                    order_resolution_key="resolution-fake",
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
            with self.assertRaisesRegex(ValueError, "matching immutable submission intent"):
                store.bind_risk_authorization(
                    released,
                    campaign_id="campaign-released",
                    broker_order_id="order-released",
                    order_submitted_at="2026-08-22T14:00:01+00:00",
                    submission_intent_id="intent-released",
                    order_resolution_key="resolution-released",
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
            with self.assertRaisesRegex(ValueError, "matching immutable submission intent"):
                store.bind_risk_authorization(
                    expired,
                    campaign_id="campaign-expired",
                    broker_order_id="order-expired",
                    order_submitted_at="2026-08-22T14:00:02+00:00",
                    submission_intent_id="intent-expired",
                    order_resolution_key="resolution-expired",
                )

            active = risk_authorization(
                store,
                instrument_key="equity:BOUND",
                thesis_key="BOUND",
                checked_at="2026-08-22T14:00:02+00:00",
            )
            active, active_intent_id, active_resolution_key = resolve_order_found(
                store,
                active,
                broker_order_id="order-bound",
                attempted_at="2026-08-22T14:00:03+00:00",
                broker_confirmed_at="2026-08-22T14:00:04+00:00",
            )
            store.bind_risk_authorization(
                active,
                campaign_id="campaign-bound",
                broker_order_id="order-bound",
                order_submitted_at="2026-08-22T14:00:03+00:00",
                submission_intent_id=active_intent_id,
                order_resolution_key=active_resolution_key,
            )
            store.bind_risk_authorization(
                active,
                campaign_id="campaign-bound",
                broker_order_id="order-bound",
                order_submitted_at="2026-08-22T14:00:03+00:00",
                submission_intent_id=active_intent_id,
                order_resolution_key=active_resolution_key,
            )
            with self.assertRaisesRegex(ValueError, "bound to another order"):
                store.bind_risk_authorization(
                    active,
                    campaign_id="campaign-bound",
                    broker_order_id="order-other",
                    order_submitted_at="2026-08-22T14:00:03+00:00",
                    submission_intent_id=active_intent_id,
                    order_resolution_key=active_resolution_key,
                )
            with self.assertRaisesRegex(ValueError, "bound to another order"):
                store.bind_risk_authorization(
                    active,
                    campaign_id="campaign-other",
                    broker_order_id="order-bound",
                    order_submitted_at="2026-08-22T14:00:03+00:00",
                    submission_intent_id=active_intent_id,
                    order_resolution_key=active_resolution_key,
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
                **LIVE_ATTRIBUTION,
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
                **LIVE_ATTRIBUTION,
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
                **LIVE_ATTRIBUTION,
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

    def test_phase_one_migration_labels_existing_live_rows_without_rewriting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "old.sqlite3"
            connection = sqlite3.connect(database)
            connection.executescript(
                """CREATE TABLE candidates (
                       symbol TEXT PRIMARY KEY,observed_at TEXT NOT NULL,
                       state TEXT NOT NULL,lane TEXT NOT NULL,
                       signal_strength REAL NOT NULL,price REAL NOT NULL,
                       gap_pct REAL,dollar_volume REAL,volume_acceleration REAL,
                       price_acceleration REAL,relative_volume REAL,spread_pct REAL,
                       short_atr REAL,base_high REAL,support REAL,invalidation REAL,
                       limit_ceiling REAL,extension_atr REAL,
                       quote_fresh INTEGER NOT NULL DEFAULT 0,
                       preliminary_liquidity_pass INTEGER NOT NULL DEFAULT 0,
                       catalyst_required INTEGER NOT NULL DEFAULT 1,
                       payload_json TEXT NOT NULL
                   );
                   INSERT INTO candidates VALUES(
                       'OLD','2026-08-22T14:00:00+00:00','BUILDING','regular',
                       50,10,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,
                       NULL,NULL,0,0,1,'{}'
                   );
                   CREATE TABLE position_campaigns (
                       campaign_id TEXT PRIMARY KEY,account_key TEXT NOT NULL,
                       instrument_key TEXT NOT NULL,thesis_key TEXT NOT NULL,
                       status TEXT NOT NULL,updated_at TEXT NOT NULL
                   );
                   CREATE TABLE risk_sessions (
                       account_key TEXT NOT NULL, session_date TEXT NOT NULL,
                       strategy_version TEXT NOT NULL,
                       start_of_day_equity REAL NOT NULL,
                       baseline_confirmed_at TEXT NOT NULL,
                       current_equity REAL NOT NULL, realized_net_pnl REAL NOT NULL,
                       confirmed_cash_flow_adjustment REAL NOT NULL DEFAULT 0,
                       account_day_pnl REAL NOT NULL, loss_gauge REAL NOT NULL,
                       loss_limit_dollars REAL NOT NULL DEFAULT -100,
                       loss_lock INTEGER NOT NULL DEFAULT 0,
                       loss_lock_triggered_at TEXT,
                       profit_objective_dollars REAL NOT NULL DEFAULT 150,
                       profit_objective_reached INTEGER NOT NULL DEFAULT 0,
                       profit_objective_reached_at TEXT,
                       active_profit_floor_dollars REAL, updated_at TEXT NOT NULL,
                       broker_confirmed_at TEXT NOT NULL,
                       broker_state_json TEXT NOT NULL,
                       PRIMARY KEY(account_key,session_date)
                   );
                   INSERT INTO risk_sessions VALUES(
                       'legacy-account','2026-08-22','legacy-strategy',5000,
                       '2026-08-22T13:30:00+00:00',5000,0,0,0,0,-100,0,NULL,
                       150,0,NULL,NULL,'2026-08-22T14:00:00+00:00',
                       '2026-08-22T14:00:00+00:00',
                       '{"account_state_readable":true}'
                   );
                   CREATE TABLE risk_authorizations (
                       authorization_id TEXT PRIMARY KEY,
                       account_key TEXT NOT NULL, session_date TEXT NOT NULL,
                       strategy_version TEXT NOT NULL, instrument_key TEXT NOT NULL,
                       thesis_key TEXT NOT NULL,
                       risk_action TEXT NOT NULL CHECK(risk_action IN ('ENTRY','ADD')),
                       status TEXT NOT NULL CHECK(status IN (
                           'ACTIVE','CONSUMED','RECONCILED','RELEASED','EXPIRED'
                       )),
                       created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                       bound_at TEXT, reconciled_at TEXT, broker_order_id TEXT,
                       campaign_id TEXT, release_reason TEXT,
                       evidence_json TEXT NOT NULL
                   );
                   INSERT INTO risk_authorizations VALUES(
                       'legacy-auth','legacy-account','2026-08-22','legacy-strategy',
                       'equity:OLD','OLD','ENTRY','RELEASED',
                       '2026-08-22T14:00:00+00:00','2026-08-22T14:01:00+00:00',
                       NULL,NULL,NULL,NULL,'legacy cleanup','{}'
                   );"""
            )
            connection.close()
            store = Store(database)
            columns = {
                row["name"] for row in store.conn.execute(
                    "PRAGMA table_info(risk_sessions)"
                )
            }
            self.assertTrue({
                "pilot_id", "book_mode", "decision_contract_version",
                "decision_contract_hash",
            }.issubset(columns))
            migrated = store.conn.execute(
                "SELECT * FROM risk_sessions WHERE account_key='legacy-account'"
            ).fetchone()
            assert migrated is not None
            self.assertEqual(migrated["pilot_id"], "legacy_unattributed")
            self.assertEqual(migrated["decision_contract_hash"], "0" * 64)
            migrated_auth = store.conn.execute(
                "SELECT * FROM risk_authorizations WHERE authorization_id='legacy-auth'"
            ).fetchone()
            assert migrated_auth is not None
            self.assertEqual(migrated_auth["pilot_id"], "legacy_unattributed")
            rebuilt_sql = store.conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='risk_authorizations'"
            ).fetchone()["sql"]
            self.assertIn("SUBMISSION_UNKNOWN", rebuilt_sql)
            campaign_columns = {
                row["name"] for row in store.conn.execute(
                    "PRAGMA table_info(position_campaigns)"
                )
            }
            self.assertTrue({
                "entry_submission_intent_id", "entry_order_resolution_key",
                "entry_order_acknowledged_at", "entry_order_ack_deadline_at",
                "entry_order_ack_state", "add_submission_intent_id",
                "add_order_resolution_key", "add_order_acknowledged_at",
                "add_order_ack_deadline_at", "add_order_ack_state",
            }.issubset(campaign_columns))
            candidate_pk = {
                row["name"]: row["pk"] for row in store.conn.execute(
                    "PRAGMA table_info(candidates)"
                )
            }
            self.assertEqual(
                [candidate_pk["pilot_id"], candidate_pk["book_mode"],
                 candidate_pk["symbol"]],
                [1, 2, 3],
            )
            migrated_candidate = store.conn.execute(
                "SELECT * FROM candidates WHERE symbol='OLD'"
            ).fetchone()
            assert migrated_candidate is not None
            self.assertEqual(migrated_candidate["pilot_id"], "legacy_unattributed")
            self.assertTrue({
                "risk_submission_intents", "risk_unknown_resolutions",
            }.issubset({
                row["name"] for row in store.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }))
            self.assertFalse(store.entry_stop_status()["engaged"])
            store.close()

    def test_pilot_fact_sheets_are_immutable_and_insufficient_books_unranked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "test.sqlite3")
            insufficient_payload = pilot_fact_sheet_payload()
            insufficient = store.record_pilot_fact_sheet(insufficient_payload)
            replay = store.record_pilot_fact_sheet(insufficient_payload)
            self.assertTrue(replay["idempotent_replay"])
            self.assertEqual(replay["fact_sheet_id"], insufficient["fact_sheet_id"])
            with self.assertRaisesRegex(Exception, "append-only"):
                store.conn.execute(
                    "UPDATE pilot_fact_sheets SET pilot_name='x' WHERE fact_sheet_id=?",
                    (insufficient["fact_sheet_id"],),
                )
            estimable_payload = pilot_fact_sheet_payload(
                pilot_id=TITAN_LIVE_PILOT_ID,
                contract_version=TITAN_LIVE_DECISION_CONTRACT_VERSION,
                contract_hash=TITAN_LIVE_DECISION_CONTRACT_HASH,
                evidence_status="ESTIMABLE",
            )
            estimable = store.record_pilot_fact_sheet(estimable_payload)
            for index, (metric, below_floor) in enumerate((
                ("position_episode_count", 99),
                ("session_count", 39),
                ("underlying_count", 29),
            ), start=1):
                with self.assertRaisesRegex(
                    ValueError, "at least 100 episodes, 40 sessions, and 30"
                ):
                    store.record_pilot_fact_sheet({
                        **estimable_payload,
                        "fact_sheet_version": f"below-floor-{index}",
                        "metrics": {
                            **estimable_payload["metrics"],
                            metric: below_floor,
                            **(
                                {"distinct_ticker_session_count": below_floor}
                                if metric == "position_episode_count" else {}
                            ),
                        },
                    })
            board = store.pilot_leaderboard(book_mode="SHADOW")
            self.assertEqual(
                board["ranked_pilots"][0]["fact_sheet_id"],
                estimable["fact_sheet_id"],
            )
            self.assertEqual(
                board["unranked_pilots"][0]["ranking_status"],
                "UNRANKED_INSUFFICIENT_EVIDENCE",
            )
            self.assertFalse(board["books_combined"])
            self.assertFalse(board["raw_pnl_or_win_rate_used"])
            self.assertFalse(board["capital_reallocation_authority"])
            with self.assertRaisesRegex(ValueError, "not registered"):
                store.record_pilot_fact_sheet({
                    **insufficient_payload,
                    "pilot_id": "rogue_pilot",
                    "fact_sheet_version": "rogue-v1",
                })
            with self.assertRaisesRegex(ValueError, "sole LIVE|not registered for LIVE"):
                store.record_pilot_fact_sheet({
                    **insufficient_payload,
                    "book_mode": "LIVE",
                    "fact_sheet_version": "wrong-mode-v1",
                })
            with self.assertRaisesRegex(ValueError, "must leave analytical metrics null"):
                store.record_pilot_fact_sheet({
                    **insufficient_payload,
                    "fact_sheet_version": "fabricated-v1",
                    "metrics": {
                        **insufficient_payload["metrics"],
                        "net_expectancy_r_after_costs": 0,
                    },
                })
            store.close()

    def test_entry_stop_is_chained_and_runtime_cannot_release_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "test.sqlite3")
            risk = {
                **LIVE_ATTRIBUTION,
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
            store.upsert_risk_session(risk)
            authorization = risk_authorization(
                store, instrument_key="equity:TEST", thesis_key="TEST"
            )
            old_evidence = {
                key: value for key, value in authorization.items()
                if key not in {
                    "authorization_id", "reservation_expires_at", "reservation_scope"
                }
            }
            engaged = store.set_entry_stop(
                engaged=True,
                reason="operator observed unsafe broker behavior",
                changed_by="operator:test",
            )
            self.assertTrue(engaged["engaged"])
            self.assertEqual(engaged["released_unsubmitted_authorization_count"], 1)
            with self.assertRaisesRegex(ValueError, "operator emergency entry stop"):
                with patch("titan_runtime.storage.datetime", wraps=datetime) as clock:
                    clock.now.return_value = datetime.fromisoformat(
                        old_evidence["checked_at"]
                    )
                    store.reserve_risk_authorization(old_evidence)
            with self.assertRaisesRegex(ValueError, "PROTECTED_USER_ONLY"):
                store.set_entry_stop(
                    engaged=False, reason="automation wants to resume", changed_by="automation"
                )
            with self.assertRaisesRegex(ValueError, "PROTECTED_USER_ONLY"):
                store.set_entry_stop(
                    engaged=False,
                    reason="forged operator identity cannot release",
                    changed_by="operator:test",
                )
            self.assertEqual(
                [event["action"] for event in store.entry_stop_events(3)],
                ["ENGAGED", "INITIALIZED"],
            )
            self.assertTrue(store.entry_stop_status()["chain_valid"])
            store.conn.execute("DROP TRIGGER operator_entry_stop_events_no_update")
            store.conn.execute(
                """UPDATE operator_entry_stop_events SET reason='tampered'
                   WHERE generation=2"""
            )
            tampered = store.entry_stop_status()
            self.assertFalse(tampered["chain_valid"])
            self.assertTrue(tampered["effective_entry_stop"])
            self.assertFalse(tampered["new_entries_allowed"])
            store.close()

    def test_submission_unknown_cannot_expire_into_a_duplicate_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "test.sqlite3")
            risk = {
                **LIVE_ATTRIBUTION,
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
            store.upsert_risk_session(risk)
            authorization = risk_authorization(
                store, instrument_key="equity:TEST", thesis_key="TEST"
            )
            unknown = store.mark_risk_submission_unknown(
                authorization["authorization_id"],
                attempted_at="2026-08-22T14:00:01+00:00",
                reason="connector timed out after request left the process",
            )
            self.assertEqual(unknown["status"], "SUBMISSION_UNKNOWN")
            self.assertEqual(
                unknown["submission_intent"]["broker_ack_deadline_at"],
                "2026-08-22T14:00:11+00:00",
            )
            self.assertEqual(
                unknown["evidence"]["submission_intent_at"],
                "2026-08-22T14:00:01+00:00",
            )
            with self.assertRaisesRegex(Exception, "append-only"):
                store.conn.execute(
                    """UPDATE risk_submission_intents
                       SET broker_ack_deadline_at='2026-08-22T14:00:12+00:00'
                       WHERE authorization_id=?""",
                    (authorization["authorization_id"],),
                )
            with self.assertRaisesRegex(ValueError, "reason is immutable"):
                store.mark_risk_submission_unknown(
                    authorization["authorization_id"],
                    attempted_at="2026-08-22T14:00:01+00:00",
                    reason="a different caller explanation",
                )
            with self.assertRaisesRegex(ValueError, "awaiting a newer broker reconciliation"):
                risk_authorization(
                    store,
                    instrument_key="equity:OTHER",
                    thesis_key="OTHER",
                    checked_at="2026-08-22T14:05:00+00:00",
                )
            reconciled = {
                **risk,
                "broker_confirmed_at": "2026-08-22T14:05:01+00:00",
            }
            store.upsert_risk_session(reconciled)
            self.assertEqual(
                store.risk_authorizations()[0]["status"], "SUBMISSION_UNKNOWN"
            )
            no_order_confirmed_at = "2026-08-22T14:05:02+00:00"
            store.upsert_risk_session({
                **risk,
                "broker_confirmed_at": no_order_confirmed_at,
                "broker_state": {
                    **risk["broker_state"],
                    "submission_unknown_resolutions": [{
                        "resolution_key": "no-order:test-order-attempt",
                        "authorization_id": authorization["authorization_id"],
                        "intent_id": unknown["submission_intent"]["intent_id"],
                        "resolution_state": "NO_ORDER_CONFIRMED",
                        "broker_confirmed_at": no_order_confirmed_at,
                        "evidence": {
                            "matching_order_count": 0,
                            "matching_position_count": 0,
                        },
                    }],
                },
            })
            self.assertEqual(
                store.risk_authorizations()[0]["status"], "RECONCILED_NO_ORDER"
            )
            next_authorization = risk_authorization(
                store,
                instrument_key="equity:OTHER",
                thesis_key="OTHER",
                checked_at="2026-08-22T14:05:03+00:00",
                broker_confirmed_at=no_order_confirmed_at,
            )
            self.assertTrue(next_authorization["authorization_id"])
            store.close()

    def test_wrong_live_contract_hash_is_rejected_on_authoritative_paths(self) -> None:
        wrong = {**LIVE_ATTRIBUTION, "decision_contract_hash": "f" * 64}
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "test.sqlite3")
            risk = grade_risk_snapshot(store)
            with self.assertRaisesRegex(ValueError, "exact canonical"):
                store.upsert_risk_session({**risk, **wrong, "session_date": "2026-08-23"})
            with self.assertRaisesRegex(ValueError, "exact canonical"):
                store.upsert_position_campaign({
                    **wrong,
                    "account_key": "ending-7153",
                    "instrument_key": "equity:WRONG",
                    "symbol": "WRONG",
                    "thesis_key": "WRONG",
                    "direction": "UP",
                    "asset_class": "equity",
                    "status": "PLANNED",
                    "strategy_version": "grade-test-v1",
                })
            with self.assertRaisesRegex(ValueError, "exact canonical"):
                store.record_performance_grade({**performance_grade_payload(), **wrong})
            store.close()
            auth_store = Store(Path(directory) / "auth.sqlite3")
            auth_store.upsert_risk_session({
                **risk,
                "strategy_version": "titan_profitability_live_2026-08-22_v2",
                "current_equity": 5000,
                "realized_net_pnl": 0,
                "broker_state": {
                    **risk["broker_state"],
                    "unleveraged_buying_power_dollars": 5000,
                },
            })
            authorization = risk_authorization(
                auth_store,
                instrument_key="equity:AUTH",
                thesis_key="AUTH",
                broker_confirmed_at="2026-08-22T20:05:00+00:00",
                checked_at="2026-08-22T20:05:01+00:00",
            )
            raw = {
                key: value for key, value in authorization.items()
                if key not in {
                    "authorization_id", "reservation_expires_at", "reservation_scope"
                }
            }
            with self.assertRaisesRegex(ValueError, "exact canonical"):
                auth_store.reserve_risk_authorization({**raw, **wrong})
            auth_store.close()

    def test_late_broker_ack_records_truth_and_keeps_protection_path_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "test.sqlite3")
            risk = {
                **LIVE_ATTRIBUTION,
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
            store.upsert_risk_session(risk)
            authorization = risk_authorization(
                store, instrument_key="equity:LATE", thesis_key="LATE"
            )
            (
                authorization,
                late_intent_id,
                late_resolution_key,
            ) = resolve_order_found(
                store,
                authorization,
                broker_order_id="late-order-1",
                attempted_at="2026-08-22T14:00:01+00:00",
                broker_confirmed_at="2026-08-22T14:00:12+00:00",
            )
            planned = {
                **LIVE_ATTRIBUTION,
                "account_key": "ending-7153",
                "instrument_key": "equity:LATE",
                "symbol": "LATE",
                "thesis_key": "LATE",
                "direction": "UP",
                "asset_class": "equity",
                "status": "PLANNED",
                "strategy_version": "titan_profitability_live_2026-08-22_v2",
                "original_stop": 9.75,
                "current_stop": 9.75,
                "initial_quantity": 40,
                "current_quantity": 0,
                "core_quantity": 0,
                "runner_quantity": 0,
            }
            campaign_id = store.upsert_position_campaign(planned)
            late_order = {
                "entry_order_id": "late-order-1",
                "entry_order_submitted_at": "2026-08-22T14:00:01+00:00",
                "entry_submission_intent_id": late_intent_id,
                "entry_order_resolution_key": late_resolution_key,
                "entry_order_acknowledged_at": "2026-08-22T14:00:12+00:00",
                "entry_order_ack_deadline_at": "2026-08-22T14:00:11+00:00",
                "entry_order_ack_state": "LATE_CONFIRMED",
                "entry_order_quantity": 40,
                "entry_cumulative_filled_quantity": 0,
                "entry_risk_gate_authorization": authorization,
            }
            submitted = {
                **planned,
                "status": "SUBMITTED",
                "broker_confirmed_at": "2026-08-22T14:00:12+00:00",
                "broker_state": late_order,
            }
            self.assertEqual(store.upsert_position_campaign(submitted), campaign_id)
            self.assertEqual(store.risk_authorizations()[0]["status"], "CONSUMED")
            filled = {
                **submitted,
                "status": "FILLED",
                "entry_price": 10,
                "broker_confirmed_at": "2026-08-22T14:00:13+00:00",
                "current_quantity": 40,
                "core_quantity": 30,
                "runner_quantity": 10,
                "broker_state": {
                    **late_order,
                    "entry_cumulative_filled_quantity": 40,
                },
            }
            store.upsert_position_campaign(filled)
            protected = {
                **filled,
                "status": "PROTECTED",
                "broker_confirmed_at": "2026-08-22T14:00:14+00:00",
                "broker_state": {**filled["broker_state"], "protection_confirmed": True},
            }
            self.assertEqual(store.upsert_position_campaign(protected), campaign_id)
            self.assertEqual(store.position_campaigns()[0]["status"], "PROTECTED")
            store.close()


if __name__ == "__main__":
    unittest.main()
