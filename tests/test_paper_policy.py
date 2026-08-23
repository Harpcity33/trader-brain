from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from titan_runtime.paper_policy import (
    PaperPolicy,
    deployment_status,
    evaluate_paper_entry,
    recommended_quantity_for_target_risk,
)


ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config/paper-trading/trader-brain-paper.json"


class PaperPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = PaperPolicy.load(CONFIG)

    def _entry(self, **overrides):
        values = {
            "settled_cash_dollars": 500,
            "committed_new_entry_notional_dollars": 0,
            "same_day_sale_proceeds_dollars": 0,
            "reviewed_entry_price": 10.00,
            "structural_stop_price": 9.50,
            "quantity": 50,
            "trigger_price": 9.99,
            "short_atr": 0.50,
            "bid": 9.99,
            "ask": 10.00,
            "quote_age_seconds": 1,
            "modeled_execution_loss_dollars": 0,
            "conservative_stress_tail_loss_dollars": 25,
            "decision_timestamp": "2026-08-24T13:35:01+00:00",
            "source_timestamp": "2026-08-24T13:35:00+00:00",
            "paper_submission_recorded": True,
        }
        values.update(overrides)
        return evaluate_paper_entry(self.policy, **values)

    def test_canonical_policy_preserves_paper_account_boundaries(self) -> None:
        self.assertEqual(self.policy.version, "trader_brain_paper_2026-08-23_v1")
        self.assertEqual(self.policy.starting_settled_cash_dollars, 500)
        self.assertEqual(self.policy.minimum_daily_entry_notional_dollars, 500)
        self.assertFalse(self.policy.same_day_sale_proceeds_reusable)
        self.assertTrue(self.policy.fractional_shares_allowed)
        self.assertFalse(self.policy.leverage_allowed)
        self.assertFalse(self.policy.default_overnight_hold_allowed)
        self.assertTrue(self.policy.full_initial_allocation_allowed)
        self.assertEqual(self.policy.target_stop_defined_risk_dollars, 35)
        self.assertFalse(self.policy.target_risk_is_hard_gate)
        self.assertEqual(self.policy.max_spread_to_structural_risk, 0.15)
        self.assertEqual(self.policy.quote_max_age_seconds, 15)
        self.assertEqual(self.policy.chase_ceiling_short_atr, 0.625)
        self.assertFalse(self.policy.live_broker_authority)
        self.assertFalse(self.policy.production_change_authority)

    def test_full_account_notional_can_be_authorized_with_small_stop_risk(self) -> None:
        decision = self._entry()
        self.assertTrue(decision.authorized)
        self.assertEqual(decision.reviewed_notional_dollars, 500)
        self.assertEqual(decision.stop_defined_loss_dollars, 25)
        self.assertEqual(decision.proposed_risk_dollars, 25)
        self.assertEqual(decision.blockers, ())

    def test_same_day_sale_proceeds_never_restore_entry_capacity(self) -> None:
        decision = self._entry(
            committed_new_entry_notional_dollars=500,
            same_day_sale_proceeds_dollars=500,
            quantity=1,
        )
        self.assertFalse(decision.authorized)
        self.assertEqual(decision.available_settled_cash_dollars, 0)
        self.assertIn(
            "reviewed notional exceeds remaining settled cash",
            decision.blockers,
        )
        self.assertIn(
            "same-day sale proceeds were excluded from available settled cash",
            decision.warnings,
        )

    def test_paper_submission_and_causal_timestamp_are_required(self) -> None:
        missing_submission = self._entry(paper_submission_recorded=False)
        self.assertFalse(missing_submission.authorized)
        self.assertIn(
            "paper SUBMITTED record must exist before a simulated fill",
            missing_submission.blockers,
        )
        future_source = self._entry(
            source_timestamp="2026-08-24T13:35:02+00:00"
        )
        self.assertFalse(future_source.authorized)
        self.assertIn(
            "source evidence is timestamped after the paper decision",
            future_source.blockers,
        )

    def test_live_ask_spread_quote_age_and_chase_are_hard_entry_checks(self) -> None:
        below_trigger = self._entry(ask=9.98)
        self.assertFalse(below_trigger.authorized)
        self.assertIn(
            "fresh ask has not crossed the exact trigger",
            below_trigger.blockers,
        )
        stale = self._entry(quote_age_seconds=15.01)
        self.assertFalse(stale.authorized)
        self.assertIn("quote is stale", stale.blockers)
        wide_and_chased = self._entry(
            reviewed_entry_price=10.50,
            structural_stop_price=10.00,
            trigger_price=10.00,
            short_atr=0.50,
            bid=10.30,
            ask=10.50,
            quantity=40,
            conservative_stress_tail_loss_dollars=20,
        )
        self.assertFalse(wide_and_chased.authorized)
        self.assertIn(
            "quoted spread exceeds the structural-risk ratio limit",
            wide_and_chased.blockers,
        )
        self.assertIn(
            "entry exceeds the short-ATR chase ceiling",
            wide_and_chased.blockers,
        )

    def test_risk_above_35_is_visible_warning_not_fabricated_hard_lock(self) -> None:
        decision = self._entry(
            structural_stop_price=9.00,
            quantity=40,
            conservative_stress_tail_loss_dollars=40,
        )
        self.assertTrue(decision.authorized)
        self.assertEqual(decision.proposed_risk_dollars, 40)
        self.assertTrue(any("exceeds the $35 paper target" in item for item in decision.warnings))

    def test_reference_quantity_is_bounded_by_cash_and_target_risk(self) -> None:
        cash_limited = recommended_quantity_for_target_risk(
            self.policy,
            settled_cash_dollars=500,
            committed_new_entry_notional_dollars=0,
            reviewed_entry_price=10,
            structural_stop_price=9.50,
        )
        self.assertEqual(cash_limited, 50)
        risk_limited = recommended_quantity_for_target_risk(
            self.policy,
            settled_cash_dollars=500,
            committed_new_entry_notional_dollars=0,
            reviewed_entry_price=10,
            structural_stop_price=8,
        )
        self.assertEqual(risk_limited, 17.5)

    def test_deployment_status_never_authorizes_hindsight(self) -> None:
        failed = deployment_status(self.policy, 499.99)
        self.assertEqual(failed["status"], "DEPLOYMENT FAILURE")
        self.assertFalse(failed["hindsight_or_fabricated_fill_authorized"])
        passed = deployment_status(self.policy, 500)
        self.assertEqual(passed["status"], "PASS")

    def test_invalid_policy_cannot_grant_live_authority_or_reuse_proceeds(self) -> None:
        raw = json.loads(CONFIG.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper.json"
            raw["authority"]["live_broker_authority"] = True
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "broker or production authority"):
                PaperPolicy.load(path)
            raw["authority"]["live_broker_authority"] = False
            raw["account_model"]["same_day_sale_proceeds_reusable"] = True
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "cannot be reused"):
                PaperPolicy.load(path)


if __name__ == "__main__":
    unittest.main()
