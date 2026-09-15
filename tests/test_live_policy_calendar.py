from __future__ import annotations

import copy
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
import hashlib
from pathlib import Path
import unittest
from zoneinfo import ZoneInfo

from titan_brain.live.calendar import ExchangeCalendar
from titan_brain.live.pipeline import PipelineThresholds
from titan_brain.live.policy import PolicyBundle
from titan_brain.live.risk_runtime import entry_lifecycle_fee_reserve


ROOT = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")


class PolicyCalendarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = PolicyBundle.load(ROOT)

    def ibkr_supported_policy(self, mode: str) -> PolicyBundle:
        base = PolicyBundle.load(
            ROOT, config_relative="config/full_live_ibkr.json"
        )
        config = copy.deepcopy(base.config)
        config["risk"]["limits_live_provenance_verified"] = True
        config["execution"].update(
            {
                "execution_authority_mode": mode,
                "broker_adapter": "supported_production_transport",
                "production_transport_id": "ibkr-tws-api-10.50.2-v1",
                "production_account_binding_fingerprint": "a" * 64,
                "production_authorization_binding_id": "b" * 64,
                "ibkr_provider_contract_id": "c" * 64,
                "ibkr_ledger_relative_path": "state/ibkr-execution.sqlite3",
                "ibkr_existing_order_reserve_dollars": "0.25",
                "local_mutation_interlock_enabled": True,
                "durable_intent_before_submit": True,
                "supported_unattended_mutation": mode == "unattended",
                "per_mutation_user_confirmation_required": mode == "attended_only",
            }
        )
        if mode == "unattended":
            config["exits"] = {
                "target_exit_mode": "first_target_completed_minute_full_exit",
                "target_index": 0,
                "target_trigger": (
                    "fresh_aligned_completed_one_minute_close_at_or_above_target"
                ),
                "quantity": "full_broker_confirmed_sellable_position",
                "cancel_working_sells_before_exit": True,
                "require_strictly_newer_cancel_evidence": True,
                "deadline_feasibility_gate": True,
            }
            config["execution"].update(
                {
                    "ibkr_autonomous_authority_schema": (
                        "titan_ibkr_autonomous_provider_authority_2026-09-14_v1"
                    ),
                    "ibkr_autonomous_authority_relative_path": (
                        "control/ibkr/autonomous-provider-authority.json"
                    ),
                    "ibkr_autonomous_authority_key_source": "macos_keychain",
                    "ibkr_autonomous_authority_key_service": (
                        "titan-full-live-ibkr-autonomous-authority"
                    ),
                    "ibkr_autonomous_authority_key_account": (
                        "ibkr-live-ending-3103"
                    ),
                    "ibkr_autonomous_policy_receipt_schema": (
                        "titan_ibkr_autonomous_owner_policy_pricing_receipt_2026-09-14_v1"
                    ),
                    "ibkr_autonomous_policy_receipt_relative_path": (
                        "control/ibkr/autonomous-owner-policy-pricing.json"
                    ),
                    "ibkr_autonomous_policy_receipt_key_source": "macos_keychain",
                    "ibkr_autonomous_policy_receipt_key_service": (
                        "titan-full-live-ibkr-owner-policy-pricing"
                    ),
                    "ibkr_autonomous_policy_receipt_key_account": (
                        "ibkr-live-ending-3103"
                    ),
                    "ibkr_daily_risk_baseline_schema": (
                        base.daily_risk_baseline_schema
                    ),
                    "ibkr_daily_risk_baseline_relative_path": (
                        "control/ibkr/daily-risk-baseline.json"
                    ),
                    "ibkr_daily_risk_baseline_key_source": "macos_keychain",
                    "ibkr_daily_risk_baseline_key_service": (
                        "titan-full-live-ibkr-daily-risk-baseline"
                    ),
                    "ibkr_daily_risk_baseline_key_account": (
                        "ibkr-live-ending-3103"
                    ),
                    "ibkr_risk_high_water_ledger_relative_path": (
                        "state/ibkr-risk-high-water.sqlite3"
                    ),
                    "ibkr_autonomous_api_name": "official_tws_python_api",
                    "ibkr_autonomous_api_version": "10.50.2",
                    "ibkr_autonomous_environment": "live",
                    "ibkr_autonomous_client_id": 19736,
                    "minimum_commission_reserve_per_order_dollars": "1.00",
                }
            )
        return replace(base, config=config)

    def test_policy_is_bound_and_deliberately_not_live(self) -> None:
        self.assertEqual(self.policy.account_last4, "7153")
        self.assertFalse(self.policy.live_entries_configured)
        self.assertIn("PER_MUTATION_CONFIRMATION_STILL_REQUIRED", self.policy.activation_blockers)
        with self.assertRaisesRegex(ValueError, "LIVE_ENTRIES_DISABLED"):
            self.policy.require_activation_ready()

    def test_ibkr_applied_choices_preserve_external_activation_gates(self) -> None:
        policy = PolicyBundle.load(ROOT, config_relative="config/full_live_ibkr.json")
        provenance = policy.config["owner_policy_approval"]
        for artifact in ("proposal", "approval_record"):
            actual_hash = hashlib.sha256(
                (ROOT / provenance[f"{artifact}_path"]).read_bytes()
            ).hexdigest()
            self.assertEqual(actual_hash, provenance[f"{artifact}_sha256"])

        execution = policy.config["execution"]
        self.assertEqual(policy.execution_authority_mode, "attended_only")
        self.assertEqual(execution["broker_adapter"], "ibkr_local_gateway_staged")
        self.assertFalse(execution["supported_unattended_mutation"])
        self.assertTrue(execution["per_mutation_user_confirmation_required"])
        self.assertFalse(execution["local_mutation_interlock_enabled"])
        self.assertFalse(execution["order_precaution_bypass_allowed"])
        self.assertFalse(policy.live_entries_configured)
        self.assertFalse(policy.config["risk"]["limits_live_provenance_verified"])
        self.assertTrue(
            {
                "IBKR_UNCHANGED_PRECAUTION_SETTINGS_RECHECK_REQUIRED",
                "IBKR_EXTERNAL_DATA_NO_BYPASS_TRANSMIT_CONTRACT_UNRESOLVED",
                "IBKR_TIERED_ALL_IN_FEE_BOUND_NOT_VERIFIED",
                "IBKR_LOCAL_GATEWAY_TRANSPORT_STAGED_ONLY",
                "LOCAL_MUTATION_INTERLOCK_NOT_ENABLED",
                "AUTHENTICATED_DAILY_STARTING_EQUITY_AND_CASH_FLOW_EVIDENCE_REQUIRED",
                "NOTIFICATION_DESTINATION_BRIDGE_NOT_CONFIGURED",
                "LIVE_DISCOVERY_PIPELINE_NOT_CONFIGURED",
            }.issubset(policy.activation_blockers)
        )
        for retired in (
            "APPROVED_LIVE_SCORE_THRESHOLDS_UNRESOLVED",
            "APPROVED_TARGET_EXIT_POLICY_UNRESOLVED",
            "APPROVED_NUMERIC_SPREAD_THRESHOLD_UNRESOLVED",
            "APPROVED_NUMERIC_DEPTH_THRESHOLD_UNRESOLVED",
            "NUMERIC_SPREAD_GATE_UNRESOLVED",
            "NUMERIC_DEPTH_GATE_UNRESOLVED",
            "AUTONOMOUS_TARGET_EXIT_POLICY_UNRESOLVED",
        ):
            self.assertNotIn(retired, policy.activation_blockers)
        with self.assertRaisesRegex(ValueError, "LIVE_ENTRIES_DISABLED"):
            policy.require_activation_ready()

    def test_ibkr_approved_config_drives_ranking_quality_and_fee_reserves(self) -> None:
        policy = PolicyBundle.load(ROOT, config_relative="config/full_live_ibkr.json")
        thresholds = PipelineThresholds.from_policy(policy)
        self.assertEqual(thresholds.score_policy, "ranking_only")
        self.assertFalse(thresholds.a_plus_enabled)
        self.assertIsNone(thresholds.minimum_setup_score)
        self.assertIsNone(thresholds.minimum_execution_score)
        self.assertIsNone(thresholds.a_plus_setup_score)
        self.assertIsNone(thresholds.a_plus_execution_score)
        evidence = policy.config["evidence"]
        self.assertEqual(evidence["max_spread_bps"], 25.0)
        self.assertEqual(evidence["spread_denominator"], "executable_nbbo_midpoint")
        self.assertEqual(evidence["minimum_depth_multiple"], 5.0)
        self.assertEqual(evidence["depth_source"], "fresh_executable_side_top_of_book")
        self.assertEqual(evidence["quote_size_unit"], "shares")
        self.assertEqual(
            policy.config["exits"]["target_exit_mode"],
            "first_target_completed_minute_full_exit",
        )
        # The $2 floor also reserves a contingency and every possible one-share
        # protection order, so even a one-share entry retains $3 of fee capacity.
        self.assertEqual(entry_lifecycle_fee_reserve(policy, quantity=1), Decimal("3"))
        self.assertEqual(entry_lifecycle_fee_reserve(policy, quantity=10), Decimal("12"))

    def test_ibkr_gmail_choice_does_not_invent_a_destination_or_route_receipt(self) -> None:
        policy = PolicyBundle.load(ROOT, config_relative="config/full_live_ibkr.json")
        notifications = policy.config["notifications"]
        self.assertEqual(notifications["intended_delivery_sink"], "gmail_api")
        self.assertEqual(notifications["delivery_sink"], "local_jsonl_staging")
        self.assertFalse(notifications["destination_bridge_configured"])
        self.assertTrue(notifications["owner_destination_consent_required"])
        self.assertTrue(notifications["route_bound_visible_test_required"])
        self.assertTrue(notifications["pause_new_entries_on_delivery_failure"])
        self.assertTrue(
            notifications["continue_reconciliation_protection_exits_and_closeout"]
        )
        for absent in (
            "destination", "destination_fingerprint", "authorization_binding_id",
            "route_version", "access_token", "refresh_token",
        ):
            self.assertNotIn(absent, notifications)

    def test_execution_authority_booleans_cannot_be_missing_or_null(self) -> None:
        for field in (
            "supported_unattended_mutation",
            "per_mutation_user_confirmation_required",
            "local_mutation_interlock_enabled",
        ):
            for missing in (True, False):
                with self.subTest(field=field, missing=missing):
                    config = copy.deepcopy(self.policy.config)
                    if missing:
                        config["execution"].pop(field)
                    else:
                        config["execution"][field] = None
                    candidate = replace(self.policy, config=config)
                    with self.assertRaisesRegex(ValueError, "must be boolean"):
                        candidate.validate()
                    if field == "per_mutation_user_confirmation_required":
                        self.assertIn(
                            "PER_MUTATION_CONFIRMATION_STILL_REQUIRED",
                            candidate.activation_blockers,
                        )

    def test_missing_execution_authority_mode_never_defaults_to_unattended(self) -> None:
        config = copy.deepcopy(self.policy.config)
        config["execution"].pop("execution_authority_mode")
        candidate = replace(self.policy, config=config)
        self.assertEqual(candidate.execution_authority_mode, "")
        with self.assertRaisesRegex(ValueError, "authority mode"):
            candidate.validate()

    def test_attended_only_mode_treats_confirmation_as_a_control_not_a_blocker(self) -> None:
        config = copy.deepcopy(self.policy.config)
        config["execution"].update(
            {
                "execution_authority_mode": "attended_only",
                "supported_unattended_mutation": False,
                "per_mutation_user_confirmation_required": True,
            }
        )
        candidate = replace(self.policy, config=config)
        candidate.validate()
        self.assertNotIn(
            "SUPPORTED_UNATTENDED_MUTATION_NOT_ATTESTED",
            candidate.activation_blockers,
        )
        self.assertNotIn(
            "PER_MUTATION_CONFIRMATION_STILL_REQUIRED",
            candidate.activation_blockers,
        )

    def test_attended_only_mode_rejects_unattended_or_confirmation_bypass(self) -> None:
        for field, value in (
            ("supported_unattended_mutation", True),
            ("per_mutation_user_confirmation_required", False),
        ):
            with self.subTest(field=field):
                config = copy.deepcopy(self.policy.config)
                config["execution"]["execution_authority_mode"] = "attended_only"
                config["execution"][field] = value
                candidate = replace(self.policy, config=config)
                with self.assertRaisesRegex(ValueError, "attended-only"):
                    candidate.validate()

    def test_supported_ibkr_accepts_attended_or_exact_autonomous_contract(self) -> None:
        attended = self.ibkr_supported_policy("attended_only")
        attended.validate()
        self.assertNotIn(
            "ibkr_autonomous_authority_schema", attended.config["execution"]
        )

        autonomous = self.ibkr_supported_policy("unattended")
        autonomous.validate()

    def test_supported_ibkr_unattended_contract_is_complete_and_exact(self) -> None:
        required = (
            "ibkr_autonomous_authority_schema",
            "ibkr_autonomous_authority_relative_path",
            "ibkr_autonomous_authority_key_source",
            "ibkr_autonomous_authority_key_service",
            "ibkr_autonomous_authority_key_account",
            "ibkr_autonomous_policy_receipt_schema",
            "ibkr_autonomous_policy_receipt_relative_path",
            "ibkr_autonomous_policy_receipt_key_source",
            "ibkr_autonomous_policy_receipt_key_service",
            "ibkr_autonomous_policy_receipt_key_account",
            "ibkr_daily_risk_baseline_schema",
            "ibkr_daily_risk_baseline_relative_path",
            "ibkr_daily_risk_baseline_key_source",
            "ibkr_daily_risk_baseline_key_service",
            "ibkr_daily_risk_baseline_key_account",
            "ibkr_risk_high_water_ledger_relative_path",
            "ibkr_autonomous_api_name",
            "ibkr_autonomous_api_version",
            "ibkr_autonomous_environment",
            "ibkr_autonomous_client_id",
        )
        for field in required:
            with self.subTest(missing=field):
                candidate = self.ibkr_supported_policy("unattended")
                config = copy.deepcopy(candidate.config)
                config["execution"].pop(field)
                with self.assertRaisesRegex(ValueError, "IBKR unattended"):
                    replace(candidate, config=config).validate()

        invalid_values = {
            "ibkr_autonomous_authority_relative_path": "../authority.json",
            "ibkr_autonomous_authority_key_source": "environment",
            "ibkr_autonomous_policy_receipt_schema": "unsupported-schema",
            "ibkr_autonomous_policy_receipt_relative_path": "../policy.json",
            "ibkr_autonomous_policy_receipt_key_source": "environment",
            "ibkr_daily_risk_baseline_schema": "unsupported-schema",
            "ibkr_daily_risk_baseline_relative_path": "../risk.json",
            "ibkr_daily_risk_baseline_key_source": "environment",
            "ibkr_risk_high_water_ledger_relative_path": "../risk.sqlite3",
            "ibkr_autonomous_api_name": "undocumented_api",
            "ibkr_autonomous_api_version": "10.49.0",
            "ibkr_autonomous_environment": "paper",
            "ibkr_autonomous_client_id": 19735,
        }
        for field, value in invalid_values.items():
            with self.subTest(field=field, value=value):
                candidate = self.ibkr_supported_policy("unattended")
                config = copy.deepcopy(candidate.config)
                config["execution"][field] = value
                with self.assertRaisesRegex(ValueError, "IBKR unattended"):
                    replace(candidate, config=config).validate()

    def test_supported_ibkr_unattended_high_water_path_is_immutable(self) -> None:
        candidate = self.ibkr_supported_policy("unattended")
        config = copy.deepcopy(candidate.config)
        config["execution"][
            "ibkr_risk_high_water_ledger_relative_path"
        ] = "state/renamed-risk-high-water.sqlite3"

        with self.assertRaisesRegex(
            ValueError,
            "canonical state/ibkr-risk-high-water.sqlite3 path",
        ):
            replace(candidate, config=config).validate()

    def test_supported_ibkr_unattended_retains_interlock_and_risk_provenance(self) -> None:
        for section, field, value, message in (
            (
                "execution",
                "local_mutation_interlock_enabled",
                False,
                "local mutation interlock",
            ),
            (
                "execution",
                "durable_intent_before_submit",
                False,
                "durable intent",
            ),
            (
                "owner_policy_approval",
                "approval_record_sha256",
                "f" * 64,
                "risk provenance",
            ),
        ):
            with self.subTest(field=field):
                candidate = self.ibkr_supported_policy("unattended")
                config = copy.deepcopy(candidate.config)
                config[section][field] = value
                with self.assertRaisesRegex(ValueError, message):
                    replace(candidate, config=config).validate()

    def test_supported_ibkr_unattended_requires_positive_commission_reserve(self) -> None:
        for value in (None, "", "not-a-number", "0", "-0.01", "NaN", "Infinity"):
            with self.subTest(value=value):
                candidate = self.ibkr_supported_policy("unattended")
                config = copy.deepcopy(candidate.config)
                if value is None:
                    config["execution"].pop(
                        "minimum_commission_reserve_per_order_dollars"
                    )
                else:
                    config["execution"][
                        "minimum_commission_reserve_per_order_dollars"
                    ] = value
                with self.assertRaisesRegex(ValueError, "commission reserve"):
                    replace(candidate, config=config).validate()

        attended = self.ibkr_supported_policy("attended_only")
        attended.config["execution"].pop(
            "minimum_commission_reserve_per_order_dollars", None
        )
        # Dollar-headroom fees apply to the approved policy in either authority
        # mode; legacy percentage policies retain their separate omission rule.
        with self.assertRaises(ValueError):
            attended.validate()

    def test_supported_ibkr_unattended_requires_exact_target_exit_contract(self) -> None:
        candidate = self.ibkr_supported_policy("unattended")
        for field, value in (
            ("target_exit_mode", "disabled_pending_owner_approval"),
            ("target_index", 1),
            ("cancel_working_sells_before_exit", False),
            ("deadline_feasibility_gate", False),
        ):
            with self.subTest(field=field):
                config = copy.deepcopy(candidate.config)
                config["exits"][field] = value
                with self.assertRaisesRegex(ValueError, "target exit|feasibility"):
                    replace(candidate, config=config).validate()

    def test_ranking_only_policy_requires_null_thresholds_and_disables_a_plus(self) -> None:
        config = copy.deepcopy(self.policy.config)
        config["discovery"].update(
            {
                "score_policy": "ranking_only",
                "a_plus_enabled": False,
                "minimum_setup_score": None,
                "minimum_execution_score": None,
                "a_plus_setup_score": None,
                "a_plus_execution_score": None,
            }
        )
        candidate = replace(self.policy, config=config)
        candidate.validate()
        self.assertFalse(
            any(
                blocker.startswith("LIVE_SCORE_THRESHOLD_UNRESOLVED:")
                for blocker in candidate.activation_blockers
            )
        )

        for field, value in (
            ("minimum_setup_score", 1),
            ("a_plus_enabled", True),
        ):
            with self.subTest(field=field):
                invalid = copy.deepcopy(config)
                invalid["discovery"][field] = value
                with self.assertRaisesRegex(ValueError, "ranking-only"):
                    replace(self.policy, config=invalid).validate()

    def test_threshold_gated_policy_rejects_partial_thresholds(self) -> None:
        config = copy.deepcopy(self.policy.config)
        config["discovery"].update(
            {
                "score_policy": "threshold_gated",
                "a_plus_enabled": True,
                "minimum_setup_score": 70,
            }
        )
        with self.assertRaisesRegex(ValueError, "partial thresholds"):
            replace(self.policy, config=config).validate()

    def test_supported_ibkr_policy_rejects_embedded_autonomous_key_material(self) -> None:
        for field in (
            "ibkr_autonomous_authority_hmac_key",
            "ibkr_autonomous_policy_receipt_hmac_key",
        ):
            with self.subTest(field=field):
                candidate = self.ibkr_supported_policy("unattended")
                config = copy.deepcopy(candidate.config)
                config["execution"][field] = "secret"
                with self.assertRaisesRegex(ValueError, "secrets cannot be stored"):
                    replace(candidate, config=config).validate()

    def test_account_mismatch_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "account"):
            self.policy.require_account("123456789", "limited_margin")

    def test_regular_entry_tuple_and_premarket_attended_boundary(self) -> None:
        self.policy.require_entry_tuple(
            quantity=3,
            limit_price=Decimal("12.50"),
            market_hours="regular_hours",
            order_type="limit",
            time_in_force="gfd",
            now=datetime(2026, 9, 8, 10, 0, tzinfo=ET),
        )
        with self.assertRaisesRegex(ValueError, "attended-only"):
            self.policy.require_entry_tuple(
                quantity=3,
                limit_price=Decimal("12.50"),
                market_hours="extended_hours",
                order_type="limit",
                time_in_force="gfd",
                now=datetime(2026, 9, 8, 8, 0, tzinfo=ET),
            )

    def test_protection_cannot_widen(self) -> None:
        self.policy.require_protection_tuple(
            quantity=2,
            stop_price="9.50",
            original_stop="9.50",
            entry_price="10.50",
            market_hours="regular_hours",
            order_type="stop_market",
            time_in_force="gtc",
        )
        with self.assertRaisesRegex(ValueError, "widened"):
            self.policy.require_protection_tuple(
                quantity=2,
                stop_price="9.25",
                original_stop="9.50",
                entry_price="10.50",
                market_hours="regular_hours",
                order_type="stop_market",
                time_in_force="gtc",
            )

    def test_signed_gmail_route_allows_only_non_secret_provenance(self) -> None:
        config = copy.deepcopy(self.policy.config)
        config["notifications"] = {
            "durable_outbox_required": True,
            "delivery_sink": "gmail_api",
            "intended_destination": "owner-approved-existing-gmail-route",
            "destination_bridge_configured": True,
            "suppress_scan_chatter": True,
            "redact_account_to_last4": True,
            "provider": "gmail",
            "destination_fingerprint": "f" * 64,
            "route_version": "owner-signed-v1",
            "required_assurance": "OWNER_CONFIRMED",
            "provider_composition_id": "titan.gmail_api.rfc2822.oauth_injected.v1",
            "authorization_binding_id": "d" * 64,
            "timeout_seconds": 5,
        }
        replace(self.policy, config=config).validate()
        config["notifications"]["destination"] = "must-not-live-in-policy@example.invalid"
        with self.assertRaisesRegex(ValueError, "cannot be stored"):
            replace(self.policy, config=config).validate()

    def test_holiday_early_close_and_dst_are_explicit(self) -> None:
        calendar = ExchangeCalendar.from_json(ROOT / "config/nyse_calendar_2026.json")
        self.assertFalse(calendar.is_trading_day(date(2026, 9, 7)))
        self.assertEqual(calendar.lane(datetime(2026, 9, 7, 10, tzinfo=ET)), "closed")
        early = calendar.session_times(date(2026, 11, 27))
        self.assertIsNotNone(early)
        assert early is not None
        self.assertEqual(early.close_at.hour, 13)
        self.assertEqual(early.closeout_start_at.hour, 12)
        self.assertEqual(early.closeout_start_at.minute, 50)
        winter = calendar.session_times(date(2026, 12, 1))
        summer = calendar.session_times(date(2026, 9, 8))
        assert winter is not None and summer is not None
        self.assertNotEqual(winter.open_at.utcoffset(), summer.open_at.utcoffset())


if __name__ == "__main__":
    unittest.main()
