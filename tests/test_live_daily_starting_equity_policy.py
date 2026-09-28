"""Exact owner-amendment binding without activation or provider assertions."""

import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from titan_brain.live.policy import PolicyBundle
from tests.live_dollar_policy_support import copy_dollar_policy_inputs, legacy_dollar_policy
from tests import test_live_policy_calendar as policy_fixtures


ROOT = Path(__file__).resolve().parents[1]


class DailyStartingEquityPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = PolicyBundle.load(ROOT, config_relative="config/full_live_ibkr.json")

    def test_exact_new_policy_replaces_only_daily_risk_and_is_not_authority(self):
        risk = self.policy.config["risk"]
        self.assertTrue(self.policy.daily_starting_equity_risk)
        self.assertTrue(self.policy.account_day_headroom_risk)
        self.assertFalse(self.policy.dollar_headroom_risk)
        self.assertTrue(self.policy.risk_provenance_verified)
        self.assertFalse(risk["limits_live_provenance_verified"])
        self.assertEqual(risk["daily_loss_fraction"], "0.10")
        self.assertEqual(risk["daily_profit_aspiration_fraction"], "0.15")
        self.assertIsNone(risk["post_goal_floor"])
        for removed in ("daily_realized_loss_lock_dollars", "profit_goal_dollars", "post_goal_floor_dollars"):
            self.assertNotIn(removed, risk)
        self.assertEqual(self.policy.risk_raw["daily_start_time"], "00:00")
        self.assertEqual(self.policy.risk_raw["daily_start_timezone"], "America/New_York")
        self.assertFalse(self.policy.risk_raw["intraday_baseline_reset_allowed"])
        self.assertNotIn("LIVE_DRAWDOWN_REVIEW_POLICY_UNVERIFIED", self.policy.activation_blockers)
        self.assertIn("AUTHENTICATED_DAILY_STARTING_EQUITY_AND_CASH_FLOW_EVIDENCE_REQUIRED", self.policy.activation_blockers)
        execution = self.policy.config["execution"]
        self.assertEqual(self.policy.execution_authority_mode, "attended_only")
        self.assertFalse(execution["supported_unattended_mutation"])
        self.assertTrue(execution["per_mutation_user_confirmation_required"])
        self.assertFalse(execution["local_mutation_interlock_enabled"])
        self.assertFalse(execution["order_precaution_bypass_allowed"])
        self.assertFalse(self.policy.live_entries_configured)
        with self.assertRaises(ValueError):
            self.policy.require_activation_ready()
        with self.assertRaisesRegex(ValueError, "legacy percentage"):
            self.policy.risk_limits

    def test_immutable_amendment_and_original_approvals_are_rehashed(self):
        amendment = self.policy.config["owner_risk_policy_amendment"]
        self.assertEqual(
            hashlib.sha256((ROOT / amendment["amendment_path"]).read_bytes()).hexdigest(),
            amendment["amendment_sha256"],
        )
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            candidate = replace(self.policy, root=target)
            self.assertFalse(candidate.risk_provenance_verified)
            copy_dollar_policy_inputs(ROOT, target)
            self.assertTrue(candidate.risk_provenance_verified)
            for path in (
                amendment["amendment_path"],
                self.policy.config["owner_policy_approval"]["approval_record_path"],
                self.policy.config["owner_policy_approval"]["proposal_path"],
            ):
                with self.subTest(path=path):
                    artifact = target / path
                    original = artifact.read_bytes()
                    artifact.write_bytes(original + b"altered\n")
                    self.assertFalse(candidate.risk_provenance_verified)
                    with self.assertRaisesRegex(ValueError, "risk provenance"):
                        candidate.validate()
                    artifact.write_bytes(original)
            self.assertTrue(candidate.risk_provenance_verified)

    def test_numeric_basis_overlay_or_floor_changes_cannot_be_approved_by_boolean(self):
        changes = (
            {"daily_loss_fraction": "0.11"},
            {"daily_loss_fraction": .10},
            {"daily_profit_aspiration_fraction": "0.20"},
            {"post_goal_floor": "125.00"},
            {"daily_realized_loss_lock_dollars": "100.00"},
            {"live_drawdown_review_pct": .20},
            {"authenticated_daily_starting_equity_required": False},
            {"authenticated_daily_external_cash_flow_required": False},
            {"daily_loss_requires_guarded_closeout": False},
        )
        for change in changes:
            with self.subTest(change=change):
                config = copy.deepcopy(self.policy.config)
                config["risk"].update(change)
                config["risk"]["limits_live_provenance_verified"] = True
                candidate = replace(self.policy, config=config)
                self.assertFalse(candidate.risk_provenance_verified)
                with self.assertRaisesRegex(ValueError, "approved amendment"):
                    candidate.validate()
        for change in (
            {"capacity_basis": "cash"}, {"daily_start_time": "09:30"},
            {"includes_open_pnl": False}, {"includes_incurred_fees": False},
            {"intraday_baseline_reset_allowed": True}, {"normal_planned_risk_pct": .03},
        ):
            with self.subTest(contract=change):
                raw = copy.deepcopy(self.policy.risk_raw)
                raw.update(change)
                candidate = replace(self.policy, risk_raw=raw)
                self.assertFalse(candidate.risk_provenance_verified)
                with self.assertRaisesRegex(ValueError, "risk provenance"):
                    candidate.validate()

    def test_corrupt_or_missing_amendment_binding_denies_source_load(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            copy_dollar_policy_inputs(ROOT, target)
            shutil.copy2(ROOT / "config/nyse_calendar_2026.json", target / "config/nyse_calendar_2026.json")
            config = copy.deepcopy(self.policy.config)
            config["owner_risk_policy_amendment"]["amendment_sha256"] = "f" * 64
            (target / "config/full_live_ibkr.json").write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "risk provenance"):
                PolicyBundle.load(target, config_relative="config/full_live_ibkr.json")
            config.pop("owner_risk_policy_amendment")
            (target / "config/full_live_ibkr.json").write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "risk provenance"):
                PolicyBundle.load(target, config_relative="config/full_live_ibkr.json")

    def test_new_mode_requires_new_authenticated_baseline_schema(self):
        fixture = policy_fixtures.PolicyCalendarTests()
        candidate = fixture.ibkr_supported_policy("unattended")
        candidate.validate()
        self.assertEqual(candidate.daily_risk_baseline_schema,
                         "titan_ibkr_daily_starting_equity_risk_baseline_2026-09-14_v1")
        config = copy.deepcopy(candidate.config)
        config["execution"]["ibkr_daily_risk_baseline_schema"] = "titan_ibkr_daily_risk_baseline_2026-09-14_v1"
        with self.assertRaisesRegex(ValueError, "daily risk baseline schema"):
            replace(candidate, config=config).validate()

    def test_legacy_dollar_and_percentage_models_remain_separate(self):
        dollar = legacy_dollar_policy(ROOT)
        self.assertTrue(dollar.dollar_headroom_risk)
        self.assertTrue(dollar.account_day_headroom_risk)
        self.assertFalse(dollar.daily_starting_equity_risk)
        self.assertTrue(dollar.risk_provenance_verified)
        self.assertEqual(dollar.config["risk"]["daily_realized_loss_lock_dollars"], "100.00")
        self.assertIn("LIVE_DRAWDOWN_REVIEW_POLICY_UNVERIFIED", dollar.activation_blockers)
        self.assertEqual(dollar.daily_risk_baseline_schema, "titan_ibkr_daily_risk_baseline_2026-09-14_v1")
        legacy = PolicyBundle.load(ROOT)
        self.assertFalse(legacy.account_day_headroom_risk)
        self.assertEqual(legacy.risk_limits.normal_planned_risk_pct, .03)


if __name__ == "__main__":
    unittest.main()
