from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from titan_brain.research import (  # noqa: E402
    ALREADY_COMPLETED,
    ArtifactExistsError,
    BAD_PROCESS_BAD_OUTCOME,
    BAD_PROCESS_GOOD_OUTCOME,
    CALENDAR_UNAVAILABLE,
    COMPLETED,
    DEADLINE_MISSED,
    DailyResearchEngine,
    GOOD_PROCESS_BAD_OUTCOME,
    GOOD_PROCESS_GOOD_OUTCOME,
    MARKET_CLOSED,
    UNAVAILABLE,
    classify_process_outcome,
    gate_research_run,
    normalize_evidence_windows,
    normalize_tactical_adjustments,
    read_prior_day_artifacts,
    select_top_candidates,
    write_immutable_artifact,
)


TRADING_DATE = date(2026, 8, 31)


class ScheduleGateTests(unittest.TestCase):
    def test_valid_trading_date_runs_at_0400_new_york(self) -> None:
        gate = gate_research_run(
            datetime(2026, 8, 31, 8, 0, tzinfo=timezone.utc),
            {TRADING_DATE},
        )
        self.assertTrue(gate.should_run)
        self.assertEqual(gate.local_time.hour, 4)

    def test_calendar_is_required_and_weekday_is_not_assumed(self) -> None:
        gate = gate_research_run(
            datetime(2026, 8, 31, 8, 0, tzinfo=timezone.utc),
            None,
        )
        self.assertEqual(gate.decision, CALENDAR_UNAVAILABLE)

    def test_holiday_unexpected_closure_and_deadline_fail_closed(self) -> None:
        holiday = gate_research_run(
            datetime(2026, 8, 31, 8, 0, tzinfo=timezone.utc),
            set(),
        )
        closure = gate_research_run(
            datetime(2026, 8, 31, 8, 0, tzinfo=timezone.utc),
            {TRADING_DATE},
            unexpected_closures={TRADING_DATE},
        )
        late = gate_research_run(
            datetime(2026, 8, 31, 10, 31, tzinfo=timezone.utc),
            {TRADING_DATE},
        )
        self.assertEqual(holiday.decision, MARKET_CLOSED)
        self.assertEqual(closure.decision, MARKET_CLOSED)
        self.assertEqual(late.decision, DEADLINE_MISSED)


class EvidenceAndLearningTests(unittest.TestCase):
    def test_all_six_horizons_are_explicit_and_missing_is_unavailable(self) -> None:
        evidence = normalize_evidence_windows(
            {
                "prior_day": {
                    "available": True,
                    "metrics": {"net_expectancy_r": 0.25},
                    "provenance": "immutable prior-day ledger",
                    "data_quality": "complete",
                },
                "six_months": {
                    "available": False,
                    "unavailable_reason": "Massive history gap",
                },
            }
        )
        self.assertEqual(len(evidence), 6)
        by_key = {item["key"]: item for item in evidence}
        self.assertEqual(by_key["prior_day"]["status"], "AVAILABLE")
        self.assertEqual(by_key["last_7_days"]["status"], UNAVAILABLE)
        self.assertEqual(by_key["six_months"]["metrics"], UNAVAILABLE)
        self.assertIn("Massive history gap", by_key["six_months"]["unavailable_reason"])
        self.assertIn("twelve_months", by_key)

    def test_four_process_outcome_quadrants_and_violation_guard(self) -> None:
        self.assertEqual(
            classify_process_outcome(True, outcome_good=True)["quadrant"],
            GOOD_PROCESS_GOOD_OUTCOME,
        )
        self.assertEqual(
            classify_process_outcome(True, outcome_good=False)["quadrant"],
            GOOD_PROCESS_BAD_OUTCOME,
        )
        bad_but_profitable = classify_process_outcome(False, realized_r=1.2)
        self.assertEqual(bad_but_profitable["quadrant"], BAD_PROCESS_GOOD_OUTCOME)
        self.assertFalse(bad_but_profitable["learning_eligible"])
        self.assertTrue(bad_but_profitable["profitable_rule_violation"])
        self.assertIn("never reinforce", bad_but_profitable["note"])
        self.assertEqual(
            classify_process_outcome(False, realized_r=-0.5)["quadrant"],
            BAD_PROCESS_BAD_OUTCOME,
        )

    def test_daily_adjustments_require_evidence_and_expire_eod(self) -> None:
        accepted, rejected = normalize_tactical_adjustments(
            [
                {
                    "parameter": "minimum_setup_score",
                    "value": 82,
                    "reason": "unstable breadth",
                    "evidence": ["last_7_days", "last_30_days"],
                },
                {"parameter": "premarket_risk", "value": 0.01},
            ],
            TRADING_DATE,
        )
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["scope"], "DAILY_TACTICAL_ADJUSTMENT")
        self.assertEqual(accepted[0]["promotion_effect"], "NONE")
        self.assertTrue(accepted[0]["expiration"].startswith("2026-08-31T23:59:59"))
        self.assertEqual(len(rejected), 1)
        self.assertIn("reason", rejected[0]["reason"])


class CandidateAndArtifactTests(unittest.TestCase):
    def test_top_candidates_are_capped_and_never_padded(self) -> None:
        candidates = [
            {
                "ticker": f"T{i}",
                "standards_met": True,
                "estimated_attractiveness": i,
            }
            for i in range(7)
        ]
        candidates.append(
            {
                "ticker": "FAIL",
                "standards_met": False,
                "estimated_attractiveness": 100,
                "rejection_reason": "spread too wide",
            }
        )
        selected, rejected = select_top_candidates(candidates)
        self.assertEqual([item["ticker"] for item in selected], ["T6", "T5", "T4", "T3", "T2"])
        self.assertEqual(len(selected), 5)
        self.assertTrue(any(item["candidate"] == "FAIL" for item in rejected))

        selected, rejected = select_top_candidates(
            [{"ticker": "ONLY", "standards_met": True, "estimated_attractiveness": 90}]
        )
        self.assertEqual(len(selected), 1)
        self.assertEqual(rejected, [])

    def test_immutable_writer_never_overwrites(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "2026-08-31.md"
            write_immutable_artifact(path, "first")
            with self.assertRaises(ArtifactExistsError):
                write_immutable_artifact(path, "second")
            self.assertEqual(path.read_text(encoding="utf-8"), "first")

    def test_missing_prior_artifact_remains_traceable_and_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.json"
            records = read_prior_day_artifacts({"live_equity": missing})
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["record_type"], "live_equity")
            self.assertIn("missing", records[0]["unavailable_reason"])


class DailyResearchEngineTests(unittest.TestCase):
    def test_prior_day_is_resolved_before_market_and_failures_are_isolated(self) -> None:
        calls: list[str] = []

        def prior() -> list[dict[str, object]]:
            calls.append("prior")
            return [
                {
                    "record_id": "trade-1",
                    "record_type": "live_equity_trade",
                    "setup_id": "FIRST_PULLBACK",
                    "process_adherent": False,
                    "realized_r": 1.0,
                }
            ]

        def broken_evidence() -> object:
            calls.append("evidence")
            raise RuntimeError("Massive timeout")

        with tempfile.TemporaryDirectory() as directory:
            engine = DailyResearchEngine(directory)
            result = engine.run(
                now=datetime(2026, 8, 31, 8, 5, tzinfo=timezone.utc),
                valid_trading_dates={TRADING_DATE},
                inputs={
                    "prior_day_records": prior,
                    "evidence_windows": broken_evidence,
                    "market_regime": {"market_bias": "neutral"},
                    "tactical_adjustments": [
                        {
                            "parameter": "minimum_execution_score",
                            "value": 85,
                            "reason": "wide spreads",
                            "evidence": "last_7_days",
                        }
                    ],
                    "promoted_edges": [{"edge_id": "edge-1", "state": "PROMOTED"}],
                    "candidates": [
                        {
                            "ticker": "ABC",
                            "setup_id": "VWAP_RECLAIM",
                            "standards_met": True,
                            "estimated_attractiveness": 88,
                            "live_qualified": False,
                            "live_rejection_reason": "quote stale",
                        }
                    ],
                },
            )
            self.assertEqual(result.status, COMPLETED)
            self.assertEqual(calls[:2], ["prior", "evidence"])
            self.assertTrue(any("market_evidence" in error for error in result.stage_errors))
            self.assertIsNotNone(result.artifact_path)
            content = result.artifact_path.read_text(encoding="utf-8")
            self.assertIn("BAD_PROCESS_GOOD_OUTCOME", content)
            self.assertIn("never reinforce", content)
            self.assertIn("market_evidence: UNAVAILABLE", content)
            self.assertIn("ABC", content)
            self.assertIn("quote stale", content)
            self.assertIn("Promotion effect", content)

            second = engine.run(
                now=datetime(2026, 8, 31, 8, 10, tzinfo=timezone.utc),
                valid_trading_dates={TRADING_DATE},
                inputs={},
            )
            self.assertEqual(second.status, ALREADY_COMPLETED)
            self.assertEqual(result.artifact_path.read_text(encoding="utf-8"), content)

    def test_repository_config_is_valid_json_and_matches_schedule_contract(self) -> None:
        config = json.loads((PROJECT_ROOT / "config" / "research_engine.json").read_text(encoding="utf-8"))
        self.assertEqual(config["schedule"]["start_local"], "04:00:00")
        self.assertEqual(config["schedule"]["analysis_deadline_local"], "06:30:00")
        self.assertEqual(config["history"]["minimum_months"], 6)
        self.assertEqual(config["history"]["preferred_months"], 12)
        self.assertFalse(config["failure_isolation"]["may_pause_or_edit_live_equity_automation"])


if __name__ == "__main__":
    unittest.main()
