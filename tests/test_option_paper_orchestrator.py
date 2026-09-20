"""Tests for the end-to-end paper orchestrator (milestone 9a)."""

from __future__ import annotations

from datetime import datetime, timezone
import unittest

from titan_brain.option_paper_orchestrator import (
    PaperOrchestratorError,
    analyzer_contract_to_identity,
    run_paper_trade,
    simulate_paper_fill,
)
from titan_brain.option_risk_policy import OptionsRiskPolicy, POLICY_VERSION
from titan_brain.option_attended_ticket import AttendedOptionTicket
from titan_brain.option_reconciliation_gate import CaptureHealth, ReconciliationInput
from titan_brain.option_paper_orchestrator import PaperFillResult
from titan_brain.live.broker.ibkr_option_orders import IbkrOptionContractIdentity


NOW = datetime(2026, 10, 5, 16, 0, 5, tzinfo=timezone.utc)


def _analyzer_payload(**contract_overrides):
    contract = {"con_id": 1, "symbol": "TEST", "right": "CALL", "strike": "100",
                "expiry": "2026-10-14", "multiplier": 100, "currency": "USD"}
    contract.update(contract_overrides)
    return {
        "schema_version": "attended_option_analysis_input_v1",
        "account": {"currency": "USD", "observed_at": "2026-10-05T16:00:00+00:00",
                    "equity": "100000", "cash": "100000", "settled_cash": "100000",
                    "available_funds": "100000", "pending_debits_not_in_balances": "0"},
        "contract": contract,
        "quote": {"observed_at": "2026-10-05T16:00:00+00:00", "bid": "1.55", "ask": "1.65"},
        "trade": {"quantity": 2, "entry_limit": "1.65", "latest_exit_at": "2026-10-13T16:00:00+00:00",
                  "thesis": "hold above 95", "invalidation": "below 95"},
        "fees": {"entry_per_order": "1", "entry_per_contract": "0.65",
                 "exit_per_order": "1", "exit_per_contract": "0.65"},
        "budgets": {"planned_loss": "5000", "stress_loss": "5000", "premium_exposure": "50000"},
        "scenarios": [
            {"name": "planned", "kind": "planned", "exit_bid": "1.55", "assumptions": "a"},
            {"name": "stress", "kind": "stress", "exit_bid": "0.15", "assumptions": "b"},
        ],
    }


def _policy(**o):
    f = dict(version=POLICY_VERSION, max_planned_loss="5000", max_stress_loss="5000",
             max_premium_exposure="50000", account_equity_ceiling_fraction="0.9")
    f.update(o)
    return OptionsRiskPolicy(**f)


def _ticket():
    return AttendedOptionTicket(
        contract_wire_fingerprint="a" * 64, account_exposure_fingerprint="b" * 64,
        material_facts={"symbol": "TEST", "quantity": 2, "limit_price": "1.65"},
        issued_at=NOW, validity_seconds=60,
    )


def _recon(**o):
    f = dict(expected_positions={}, observed_positions={}, expected_orders={}, observed_orders={},
             capture=CaptureHealth(reporting_scope_account_wide=True, capture_continuous=True,
                                   subscription_active=True, history_complete=True,
                                   duplicate_or_late_events_detected=False))
    f.update(o)
    return ReconciliationInput(**f)


def _run(**o):
    f = dict(analyzer_payload=_analyzer_payload(), now=NOW, policy=_policy(),
             account_equity="100000", ticket=_ticket(), reconciliation=_recon(),
             paper_fill=PaperFillResult(filled_quantity=2, fill_price="1.65", status="FILLED"),
             pretrade_baseline_cash="100000")
    f.update(o)
    return run_paper_trade(**f)


class PaperOrchestratorTests(unittest.TestCase):
    def test_happy_path_completes_paper_run(self) -> None:
        r = _run()
        self.assertTrue(r.ok)
        self.assertEqual(r.stopped_at, "complete")
        self.assertFalse(r.live_authority)
        self.assertTrue(r.paper_only)
        self.assertEqual(r.lifecycle["open_contracts"], 2)

    def test_reconciliation_blocker_stops_first(self) -> None:
        r = _run(reconciliation=_recon(observed_positions={"other": 5}))
        self.assertFalse(r.ok)
        self.assertEqual(r.stopped_at, "reconciliation")
        self.assertIn("MANUAL_POSITION_CHANGE_UNRECONCILED", r.blockers)

    def test_analysis_blocker_stops(self) -> None:
        # Expiry outside 1-10 day window -> analysis fails closed.
        r = _run(analyzer_payload=_analyzer_payload(expiry="2026-11-30"))
        self.assertFalse(r.ok)
        self.assertEqual(r.stopped_at, "analysis")

    def test_policy_budget_blocker_stops(self) -> None:
        r = _run(policy=_policy(max_premium_exposure="10"))  # premium >> 10
        self.assertFalse(r.ok)
        self.assertEqual(r.stopped_at, "policy")

    def test_rejected_paper_fill_opens_no_position(self) -> None:
        r = _run(paper_fill=PaperFillResult(filled_quantity=0, fill_price="0", status="REJECTED"))
        self.assertFalse(r.ok)
        self.assertEqual(r.stopped_at, "paper_fill")
        self.assertIn("REJECTED", r.blockers)
        self.assertEqual(r.lifecycle, {})

    def test_unknown_paper_fill_opens_no_position(self) -> None:
        r = _run(paper_fill=PaperFillResult(filled_quantity=0, fill_price="0", status="UNKNOWN"))
        self.assertFalse(r.ok)
        self.assertEqual(r.stopped_at, "paper_fill")
        self.assertIn("UNKNOWN", r.blockers)


class PaperFillSimulatorTests(unittest.TestCase):
    def test_marketable_fills(self) -> None:
        r = simulate_paper_fill(requested_quantity=2, limit_price="1.65",
                                available_liquidity=5, marketable=True)
        self.assertEqual(r.status, "FILLED")
        self.assertEqual(r.filled_quantity, 2)

    def test_not_marketable_rejects(self) -> None:
        r = simulate_paper_fill(requested_quantity=2, limit_price="1.65",
                                available_liquidity=5, marketable=False)
        self.assertEqual(r.status, "REJECTED")

    def test_partial_when_liquidity_short(self) -> None:
        r = simulate_paper_fill(requested_quantity=5, limit_price="1.65",
                                available_liquidity=2, marketable=True)
        self.assertEqual(r.status, "PARTIAL")
        self.assertEqual(r.filled_quantity, 2)

    def test_forced_unknown(self) -> None:
        r = simulate_paper_fill(requested_quantity=2, limit_price="1.65",
                                available_liquidity=5, marketable=True, outcome="unknown")
        self.assertEqual(r.status, "UNKNOWN")


class SchemaAdapterTests(unittest.TestCase):
    def test_adapter_bridges_analyzer_contract_to_identity(self) -> None:
        contract = {"con_id": 1, "symbol": "TEST", "right": "CALL", "strike": "100",
                    "expiry": "2026-10-14", "multiplier": 100, "currency": "USD"}
        ident = analyzer_contract_to_identity(
            contract, trading_class="TEST", deliverable="100_SHARES",
            exercise_style="AMERICAN", settlement_type="PHYSICAL")
        self.assertIsInstance(ident, IbkrOptionContractIdentity)
        self.assertEqual(ident.right, "C")
        self.assertEqual(ident.expiry, "20261014")

    def test_adapter_put(self) -> None:
        contract = {"con_id": 1, "symbol": "TEST", "right": "PUT", "strike": "100",
                    "expiry": "2026-10-14", "multiplier": 100}
        ident = analyzer_contract_to_identity(
            contract, trading_class="TEST", deliverable="100_SHARES",
            exercise_style="AMERICAN", settlement_type="PHYSICAL")
        self.assertEqual(ident.right, "P")

    def test_adapter_bad_right_rejected(self) -> None:
        with self.assertRaises(PaperOrchestratorError):
            analyzer_contract_to_identity(
                {"con_id": 1, "symbol": "TEST", "right": "X", "strike": "100",
                 "expiry": "2026-10-14", "multiplier": 100},
                trading_class="TEST", deliverable="100_SHARES",
                exercise_style="AMERICAN", settlement_type="PHYSICAL")


if __name__ == "__main__":
    unittest.main()
