"""Unselected session-policy fixtures only; no broker or installed config."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from titan_brain.live.policy import PolicyBundle, SESSION_TRADING_MODEL
from titan_brain.live import session_trading_policy as session_policy
from tests import test_live_policy_calendar as policy_fixtures


ROOT = Path(__file__).resolve().parents[1]
CONFIG = "config/full_live_session_candidate.json"
RISK = {
    "model": SESSION_TRADING_MODEL,
    "limits_path": session_policy.POLICY_RELATIVE_PATH,
    "limits_live_provenance_verified": False,
    "limits_provenance_state": "owner_approved_session_trading_amendment_bound_to_2026-09-18_artifact",
    "daily_loss_fraction": "0.10",
    "daily_profit_aspiration_fraction": "0.15",
    "post_goal_floor": None,
    "positive_execution_reserve_required": True,
    "loss_lock_is_irreversible_for_session": True,
    "daily_loss_requires_guarded_closeout": True,
    "authenticated_session_baseline_required": True,
    "authenticated_session_measurement_required": True,
    "missing_data_incident_policy": "pending_before_read_failed_or_gap_sticky",
}


class SessionPolicyBundleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        for relative in (*session_policy._APPROVAL_FILES,
                         session_policy.POLICY_RELATIVE_PATH, "config/nyse_calendar_2026.json"):
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, target)
        self.config = deepcopy(policy_fixtures.PolicyCalendarTests().ibkr_supported_policy("unattended").config)
        self.config["risk"] = deepcopy(RISK)
        self.config["owner_risk_policy_amendment"] = {
            "amendment_path": session_policy.OWNER_AMENDMENT_PATH,
            "amendment_sha256": session_policy.OWNER_AMENDMENT_SHA256,
        }
        # The new measurement does not acquire legacy NAV/flow/weekly/high-water
        # receipt requirements simply to make the full-live policy parse.
        self.config["execution"] = {
            key: value for key, value in self.config["execution"].items()
            if not key.startswith("ibkr_daily_risk_baseline_")
            and key != "ibkr_risk_high_water_ledger_relative_path"
        }
        forbidden = patch("socket.socket.connect", side_effect=AssertionError("network forbidden"))
        forbidden.start()
        self.addCleanup(forbidden.stop)

    def load(self):
        (self.root / CONFIG).write_text(json.dumps(self.config))
        return PolicyBundle.load(self.root, config_relative=CONFIG)

    def risk_file(self, change):
        target = self.root / session_policy.POLICY_RELATIVE_PATH
        value = json.loads(target.read_text())
        change(value)
        target.write_text(json.dumps(value))

    def test_exact_session_policy_is_recognized_without_legacy_measurement_flags(self):
        bundle = self.load()
        self.assertTrue(bundle.session_trading_risk)
        self.assertTrue(bundle.risk_provenance_verified)
        self.assertFalse(bundle.account_day_headroom_risk)
        self.assertFalse(bundle.daily_starting_equity_risk)
        self.assertFalse(bundle.dollar_headroom_risk)
        verified = session_policy.load_session_trading_policy_from_root(self.root)
        self.assertEqual(bundle.risk_hash, verified.policy_sha256)
        self.assertNotIn("daily_realized_loss_lock_dollars", bundle.config["risk"])
        self.assertNotIn("ibkr_daily_risk_baseline_schema", bundle.config["execution"])
        with self.assertRaises(ValueError):
            bundle.risk_limits
        with self.assertRaises(ValueError):
            bundle.daily_risk_baseline_schema

    def test_valid_policy_and_boolean_flags_do_not_claim_runtime_integration(self):
        for claimed in (False, True):
            with self.subTest(claimed=claimed):
                self.config["risk"]["limits_live_provenance_verified"] = claimed
                self.config["authority"]["live_entries_enabled"] = True
                self.config["discovery"].update(
                    provider_composition_id="titan.massive_rest_stream.ibkr_contract.quality.v1",
                    provider_binding_id="d" * 64,
                )
                bundle = self.load()
                self.assertTrue(bundle.risk_provenance_verified)
                self.assertIn("SESSION_TRADING_RUNTIME_INTEGRATION_UNAVAILABLE", bundle.activation_blockers)
                with self.assertRaisesRegex(ValueError, "SESSION_TRADING_RUNTIME_INTEGRATION_UNAVAILABLE"):
                    bundle.require_activation_ready()

    def test_config_rejects_altered_limits_legacy_fields_and_missing_data_relaxation(self):
        cases = (
            ("daily_loss_fraction", "0.20"), ("daily_profit_aspiration_fraction", "0.25"),
            ("post_goal_floor", "0.05"), ("positive_execution_reserve_required", 1),
            ("authenticated_session_baseline_required", False),
            ("authenticated_session_measurement_required", False),
            ("missing_data_incident_policy", "clear_on_success"),
            ("daily_realized_loss_lock_dollars", "100.00"),
            ("authenticated_daily_external_cash_flow_required", True),
            ("limits_path", "config/other-risk.json"),
        )
        original = deepcopy(self.config["risk"])
        for key, value in cases:
            with self.subTest(field=key):
                self.config["risk"] = dict(original, **{key: value})
                if key == "limits_path":
                    shutil.copy2(self.root / session_policy.POLICY_RELATIVE_PATH, self.root / value)
                with self.assertRaises(ValueError):
                    self.load()

    def test_risk_json_changes_cannot_be_approved_with_config_boolean(self):
        self.config["risk"]["limits_live_provenance_verified"] = True
        self.risk_file(lambda value: value.update(intraday_baseline_reset_allowed=True))
        with self.assertRaises(ValueError):
            self.load()

    def test_every_pinned_approval_file_is_required_unchanged(self):
        for relative in session_policy._APPROVAL_FILES:
            with self.subTest(file=relative):
                target = self.root / relative
                original = target.read_bytes()
                target.write_bytes(original + b"\nsynthetic modification")
                with self.assertRaises(ValueError):
                    self.load()
                target.write_bytes(original)
        self.assertTrue(self.load().risk_provenance_verified)

    def test_amendment_binding_must_be_exact_without_extra_authority_fields(self):
        for change in ({"amendment_sha256": "a" * 64}, {"approved": True},
                       {"amendment_path": "validation/other.md"}):
            original = dict(self.config["owner_risk_policy_amendment"])
            self.config["owner_risk_policy_amendment"].update(change)
            with self.assertRaises(ValueError):
                self.load()
            self.config["owner_risk_policy_amendment"] = original

    def test_original_approval_binding_and_new_york_day_cannot_be_rekeyed(self):
        self.config["owner_policy_approval"]["approval_record_sha256"] = "e" * 64
        with self.assertRaises(ValueError):
            self.load()
        self.config["owner_policy_approval"] = json.loads((ROOT / "config/full_live_ibkr.json").read_text())["owner_policy_approval"]
        self.config["sessions"]["timezone"] = "UTC"
        with self.assertRaises(ValueError):
            self.load()

    def test_changed_account_binding_is_not_a_new_approved_policy(self):
        self.config["account"]["account_key"] = "ibkr-live-ending-other"
        with self.assertRaises(ValueError):
            self.load()

    def test_independent_authority_pricing_and_exposure_reserve_gates_remain(self):
        for field in ("ibkr_autonomous_authority_schema", "ibkr_autonomous_policy_receipt_schema",
                      "local_mutation_interlock_enabled", "durable_intent_before_submit",
                      "minimum_commission_reserve_per_order_dollars", "minimum_entry_lifecycle_fee_reserve_dollars"):
            with self.subTest(field=field):
                original = self.config["execution"][field]
                self.config["execution"][field] = False if type(original) is bool else "0"
                with self.assertRaises(ValueError):
                    self.load()
                self.config["execution"][field] = original

    def test_duplicate_risk_fields_are_rejected_by_pinned_root_loader(self):
        target = self.root / session_policy.POLICY_RELATIVE_PATH
        text = target.read_text()
        target.write_text(text.replace("{", '{"daily_loss_fraction":"0.10",', 1))
        with self.assertRaises(ValueError):
            self.load()

    def test_loaded_policy_rechecks_original_files_instead_of_caching_approval_boolean(self):
        bundle = self.load()
        target = self.root / session_policy.OWNER_AMENDMENT_PATH
        target.unlink()
        self.assertFalse(bundle.risk_provenance_verified)

    def test_checked_in_production_selection_and_legacy_properties_are_unchanged(self):
        path = ROOT / "config/full_live_ibkr.json"
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        old = PolicyBundle.load(ROOT, config_relative="config/full_live_ibkr.json")
        self.assertFalse(old.session_trading_risk)
        self.assertTrue(old.daily_starting_equity_risk)
        self.assertTrue(old.account_day_headroom_risk)
        self.assertTrue(old.risk_provenance_verified)
        self.assertNotIn("SESSION_TRADING_RUNTIME_INTEGRATION_UNAVAILABLE", old.activation_blockers)
        self.load()
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)


if __name__ == "__main__":
    unittest.main()
