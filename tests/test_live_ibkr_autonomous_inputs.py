"""Focused tests for inert release-bound autonomous IBKR input loading."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import hmac
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from titan_brain.live.ibkr_autonomous_authority import (
    IBKR_AUTONOMOUS_AUTHORITY_SCHEMA,
)
from titan_brain.live.ibkr_autonomous_inputs import (
    DurableIbkrAutonomousAcceptanceVerifier,
    DurableIbkrAutonomousRiskPolicyCheck,
    IbkrAutonomousInputError,
    build_release_bound_ibkr_autonomous_inputs,
)
from titan_brain.live.ibkr_autonomous_interlock import (
    AutonomousIbkrWriterInterlock,
)
from titan_brain.live.broker.base import AccountSnapshot, FundsSnapshot
from titan_brain.live.broker.ibkr_preflight import IbkrOrderPurpose
from titan_brain.live.ibkr_autonomous_plans import (
    StateBackedAutonomousIbkrPlanProducer,
)
from titan_brain.live.ibkr_autonomous_policy_receipt import (
    IBKR_AUTONOMOUS_POLICY_RECEIPT_SCHEMA,
)
from titan_brain.live.policy import PolicyBundle, canonical_json
from titan_brain.live.provider_clients import KeychainItem
from titan_brain.live.risk_evidence_binding import risk_high_water_receipt_hash
from titan_brain.live.state import LiveStateStore
from tests.live_dollar_policy_support import copy_dollar_policy_inputs


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 14, 15, 0, tzinfo=timezone.utc)
RELEASE = "8" * 64
ACCOUNT_BINDING = "a" * 64
AUTHORIZATION_BINDING = "b" * 64
PROVIDER_CONTRACT = "c" * 64
SECRET = b"loader-test-autonomous-authority-secret-material-v1"


class RecordingKeychain:
    def __init__(self, result: bytes = SECRET, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[KeychainItem] = []

    def read(self, item: KeychainItem) -> bytes:
        self.calls.append(item)
        if self.error is not None:
            raise self.error
        return self.result


class AutonomousIbkrInputTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.release = self.root / "release"
        config_root = self.release / "config"
        config_root.mkdir(parents=True)
        copy_dollar_policy_inputs(ROOT, self.release)
        shutil.copy2(
            ROOT / "config/risk_limits.json", config_root / "risk_limits.json"
        )
        shutil.copy2(
            ROOT / "config/nyse_calendar_2026.json",
            config_root / "nyse_calendar_2026.json",
        )
        self.config_path = config_root / "full_live_ibkr_autonomous.json"
        config = json.loads(
            (ROOT / "config/full_live_ibkr.json").read_text(encoding="utf-8")
        )
        config["risk"]["limits_live_provenance_verified"] = True
        config["evidence"].update(
            {
                "max_spread_bps": 25.0,
                "spread_denominator": "executable_nbbo_midpoint",
                "minimum_depth_multiple": 5.0,
                "depth_source": "fresh_executable_side_top_of_book",
                "quote_size_unit": "shares",
            }
        )
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
                "execution_authority_mode": "unattended",
                "broker_adapter": "supported_production_transport",
                "production_transport_id": "ibkr-tws-api-10.50.2-v1",
                "production_account_binding_fingerprint": ACCOUNT_BINDING,
                "production_authorization_binding_id": AUTHORIZATION_BINDING,
                "ibkr_provider_contract_id": PROVIDER_CONTRACT,
                "ibkr_ledger_relative_path": "state/ibkr-execution.sqlite3",
                "ibkr_existing_order_reserve_dollars": "0.25",
                "minimum_commission_reserve_per_order_dollars": "1.00",
                "supported_unattended_mutation": True,
                "per_mutation_user_confirmation_required": False,
                "local_mutation_interlock_enabled": True,
                "one_account_writer_required": True,
                "durable_intent_before_submit": True,
                "automatic_retry_unknown_submission": False,
                "ibkr_autonomous_authority_schema": (
                    IBKR_AUTONOMOUS_AUTHORITY_SCHEMA
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
                    IBKR_AUTONOMOUS_POLICY_RECEIPT_SCHEMA
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
                    "titan_ibkr_daily_risk_baseline_2026-09-14_v1"
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
            }
        )
        self._write_config(config)
        self.policy = PolicyBundle.load(
            self.release,
            config_relative="config/full_live_ibkr_autonomous.json",
        )

        self.install = self.root / "install"
        self.authority_path = (
            self.install / "control/ibkr/autonomous-provider-authority.json"
        )
        self.authority_path.parent.mkdir(parents=True)
        self.policy_receipt_path = (
            self.install / "control/ibkr/autonomous-owner-policy-pricing.json"
        )
        state_root = self.install / "state"
        state_root.mkdir(parents=True)
        self.state_path = state_root / "full-live.sqlite3"
        state = LiveStateStore(self.state_path)
        state.initialize_runtime(
            runtime_id=self.policy.runtime_id,
            account_key=self.policy.account_key,
            release_manifest_hash=RELEASE,
            config_hash=self.policy.config_hash,
            policy_hash=self.policy.policy_hash,
            initialized_at=NOW - timedelta(minutes=5),
        )
        state.close()
        self._write_authority()
        self._write_policy_receipt()
        self.locks = self.root / "account-locks"
        self.clock_value = NOW

    def _write_config(self, config: dict[str, object]) -> None:
        self.config_path.write_text(
            json.dumps(config, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _authority_body(self) -> dict[str, object]:
        return {
            "schema_version": IBKR_AUTONOMOUS_AUTHORITY_SCHEMA,
            "issued_at": (NOW - timedelta(minutes=1)).isoformat(),
            "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
            "bindings": {
                "release_manifest_hash": RELEASE,
                "config_hash": self.policy.config_hash,
                "policy_binding_id": self.policy.policy_hash,
                "account_key": self.policy.account_key,
                "account_masked": "****3103",
                "account_binding_fingerprint": ACCOUNT_BINDING,
                "authorization_binding_id": AUTHORIZATION_BINDING,
                "provider_contract_id": PROVIDER_CONTRACT,
                "transport_id": "ibkr-tws-api-10.50.2-v1",
                "api_name": "official_tws_python_api",
                "api_version": "10.50.2",
                "environment": "live",
                "client_id": 19736,
            },
            "support": {
                "reference": "IBKR-SUPPORT-CASE-INPUT-TEST",
                "status": "provider_confirmed_supported",
                "confirmed_at": (NOW - timedelta(minutes=2)).isoformat(),
                "scope": (
                    "unattended_regular_hours_api_orders_with_external_market_data"
                ),
            },
            "account_controls": {
                "read_only_api_enabled": False,
                "read_only_api_verified_at": (
                    NOW - timedelta(minutes=2)
                ).isoformat(),
                "no_borrow_margin_account": True,
            },
            "order_visibility": {
                "scope": "exact_account_all_clients",
                "standard_equity_orders": "exhaustive",
                "advanced_equity_orders": "exhaustive",
                "option_orders": "exhaustive",
                "working_orders_across_dates": True,
                "parent_child_conditional_orders": True,
                "completed_orders": True,
                "executions": True,
                "all_pages_consumed": True,
                "client_ref_recovery_source": "exhaustive_order_history",
                "broker_preserves_client_ref": True,
                "negative_client_ref_results_authoritative": False,
            },
            "execution": {
                "daemon_writes_supported": True,
                "unattended_place_supported": True,
                "unattended_cancel_supported": True,
                "per_order_confirmation_required": False,
                "durable_intent_before_submit": True,
                "automatic_unknown_retry_allowed": False,
                "unknown_submission_behavior": "reconcile_without_retry",
            },
            "scope": {
                "allowed_security_types": ["stock"],
                "allowed_direction": "long",
                "whole_shares_only": True,
                "allowed_market_hours": ["regular_hours"],
                "margin_debit_allowed": False,
                "shorting_allowed": False,
                "options_allowed": False,
                "fractional_allowed": False,
                "extended_hours_orders_allowed": False,
                "overnight_allowed": False,
            },
            "protection": {
                "mode": "sequential_verified",
                "atomic_protection_claimed": False,
                "broker_working_evidence_required": True,
                "block_new_entries_while_unprotected_or_unresolved": True,
                "closeout_requires_broker_confirmed_flatness": True,
            },
            "precautions": {
                "external_market_data_transmission": (
                    "provider_confirmed_without_manual_transmit_or_precaution_bypass"
                ),
                "broker_order_precautions": "enforced",
                "order_constraint_override_allowed": False,
                "advanced_error_override_allowed": False,
                "bypassed_precautions": [],
            },
        }

    def _write_authority(
        self,
        *,
        secret: bytes = SECRET,
        body: dict[str, object] | None = None,
    ) -> None:
        authority = self._authority_body() if body is None else body
        payload = deepcopy(authority)
        payload["hmac_sha256"] = hmac.new(
            secret,
            canonical_json(authority).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        self.authority_path.write_text(
            canonical_json(payload) + "\n", encoding="utf-8"
        )
        self.authority_path.chmod(0o600)

    def _policy_receipt_body(self) -> dict[str, object]:
        execution = self.policy.config["execution"]
        return {
            "schema_version": IBKR_AUTONOMOUS_POLICY_RECEIPT_SCHEMA,
            "issued_at": (NOW - timedelta(minutes=1)).isoformat(),
            "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
            "bindings": {
                "release_manifest_hash": RELEASE,
                "config_hash": self.policy.config_hash,
                "policy_hash": self.policy.policy_hash,
                "risk_hash": self.policy.risk_hash,
                "account_key": self.policy.account_key,
                "account_masked": "****3103",
                "account_binding_fingerprint": ACCOUNT_BINDING,
                "authorization_binding_id": AUTHORIZATION_BINDING,
                "provider_contract_id": PROVIDER_CONTRACT,
                "transport_id": execution["production_transport_id"],
            },
            "owner_approval": {
                "status": "owner_approved",
                "approved_at": (NOW - timedelta(minutes=3)).isoformat(),
                "reference": "OWNER-POLICY-INPUT-TEST-001",
                "scope": (
                    "exact_release_config_policy_risk_quality_target_and_pricing"
                ),
            },
            "quality_policy": {
                "max_spread_bps": "25.0",
                "spread_denominator": "executable_nbbo_midpoint",
                "minimum_depth_multiple": "5.0",
                "depth_source": "fresh_executable_side_top_of_book",
                "quote_size_unit": "shares",
            },
            "target_exit_policy": deepcopy(self.policy.config["exits"]),
            "effective_pricing": {
                "status": "provider_account_verified",
                "verified_at": (NOW - timedelta(minutes=2)).isoformat(),
                "provider": "interactive_brokers",
                "source_kind": (
                    "authenticated_ibkr_account_effective_pricing_receipt"
                ),
                "source_reference": "IBKR-ACCOUNT-PRICING-INPUT-TEST-001",
                "source_receipt_sha256": "e" * 64,
                "currency": "USD",
                "routing_scope": "smart_routed_us_stock_orders",
                "fee_scope": (
                    "all_in_commission_and_regulatory_fees_per_order"
                ),
                "all_in_commission_floor_dollars": "1.00",
                "floor_conservative_for_allowed_order_scope": True,
            },
        }

    def _write_policy_receipt(
        self,
        *,
        secret: bytes = SECRET,
        body: dict[str, object] | None = None,
    ) -> None:
        receipt = self._policy_receipt_body() if body is None else body
        payload = deepcopy(receipt)
        payload["hmac_sha256"] = hmac.new(
            secret,
            canonical_json(receipt).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        self.policy_receipt_path.write_text(
            canonical_json(payload) + "\n", encoding="utf-8"
        )
        self.policy_receipt_path.chmod(0o600)

    def _rows(self, table: str) -> list[tuple[object, ...]]:
        if table not in {"activation_records", "account_writer_lease"}:
            raise ValueError("unsafe test table")
        connection = sqlite3.connect(self.state_path)
        try:
            return list(connection.execute(f"SELECT * FROM {table}").fetchall())
        finally:
            connection.close()

    def _build(self, keychain: RecordingKeychain | None = None):
        reader = keychain if keychain is not None else RecordingKeychain()
        with patch(
            "titan_brain.live.ibkr_autonomous_inputs."
            "user_account_writer_lock_directory",
            return_value=self.locks,
        ):
            result = build_release_bound_ibkr_autonomous_inputs(
                release_root=self.release,
                install_root=self.install,
                full_live_config_name="full_live_ibkr_autonomous.json",
                release_manifest_hash=RELEASE,
                clock=lambda: self.clock_value,
                keychain=reader,
            )
        return result, reader

    def assert_code(self, code: str, callback) -> None:
        with self.assertRaises(IbkrAutonomousInputError) as caught:
            callback()
        self.assertEqual(str(caught.exception), code)
        self.assertEqual(caught.exception.code, code)
        self.assertIsNone(caught.exception.__cause__)

    def test_builds_exact_shared_inert_dependencies_without_granting_authority(self) -> None:
        authority_bytes = self.authority_path.read_bytes()
        policy_receipt_bytes = self.policy_receipt_path.read_bytes()
        before_activations = self._rows("activation_records")
        before_leases = self._rows("account_writer_lease")
        inputs, keychain = self._build()
        self.addCleanup(inputs.owned_resource.close)

        self.assertEqual(inputs.authority_mode, "unattended")
        self.assertIsInstance(
            inputs.acceptance_verifier,
            DurableIbkrAutonomousAcceptanceVerifier,
        )
        self.assertIsInstance(
            inputs.mutation_interlock, AutonomousIbkrWriterInterlock
        )
        self.assertIsInstance(
            inputs.risk_policy_check,
            DurableIbkrAutonomousRiskPolicyCheck,
        )
        self.assertIsInstance(
            inputs.plan_sealer, StateBackedAutonomousIbkrPlanProducer
        )
        self.assertIs(inputs.plan_reader, inputs.plan_sealer.reader)
        self.assertIs(
            inputs.service_writer_lock, inputs.mutation_interlock.lock
        )
        self.assertIs(
            inputs.autonomous_authority_bindings,
            inputs.mutation_interlock.authority_bindings,
        )
        self.assertIs(
            inputs.plan_reader.bindings, inputs.plan_sealer.bindings
        )
        self.assertIs(inputs.owned_resource, inputs.plan_reader.state)
        self.assertIs(inputs.owned_resource, inputs.mutation_interlock.state)
        self.assertEqual(inputs.mutation_interlock.state_path, self.state_path)
        self.assertEqual(
            inputs.mutation_interlock.state_identity,
            (self.state_path.stat().st_dev, self.state_path.stat().st_ino),
        )
        self.assertTrue(
            inputs.service_writer_lock.owner_id.startswith(
                "autonomous-full-live-service-"
            )
        )
        self.assertEqual(
            inputs.service_writer_lock.broker_account_binding_fingerprint,
            ACCOUNT_BINDING,
        )
        self.assertEqual(
            inputs.service_writer_lock.authorization_binding_id,
            AUTHORIZATION_BINDING,
        )
        self.assertEqual(
            inputs.autonomous_authority.release_manifest_hash, RELEASE
        )
        self.assertEqual(
            inputs.autonomous_authority.config_hash, self.policy.config_hash
        )
        self.assertEqual(
            inputs.autonomous_authority.policy_binding_id,
            self.policy.policy_hash,
        )
        self.assertEqual(inputs.acceptance_verifier.path, self.authority_path)
        self.assertEqual(
            inputs.acceptance_verifier.policy_receipt_path,
            self.policy_receipt_path,
        )
        self.assertIs(
            inputs.acceptance_verifier.bindings,
            inputs.autonomous_authority_bindings,
        )
        self.assertFalse(inputs.service_writer_lock.held)
        self.assertFalse(self.locks.exists())
        self.assertEqual(self.authority_path.read_bytes(), authority_bytes)
        self.assertEqual(
            self.policy_receipt_path.read_bytes(), policy_receipt_bytes
        )
        self.assertEqual(self._rows("activation_records"), before_activations)
        self.assertEqual(self._rows("account_writer_lease"), before_leases)
        self.assertEqual(len(keychain.calls), 2)
        self.assertEqual(
            keychain.calls[0].source_label,
            "macos-keychain:titan-full-live-ibkr-autonomous-authority:"
            "ibkr-live-ending-3103",
        )
        self.assertEqual(
            keychain.calls[1].source_label,
            "macos-keychain:titan-full-live-ibkr-owner-policy-pricing:"
            "ibkr-live-ending-3103",
        )
        self.assertEqual(
            inputs.write_evidence.reviewed_contract_id, PROVIDER_CONTRACT
        )
        self.assertIsNone(
            inputs.acceptance_verifier(inputs.write_evidence)
        )
        self.assertEqual(len(keychain.calls), 4)

        inventories = (
            *inputs.acceptance_verifier.release_components(),
            *inputs.plan_sealer.release_components(),
            *inputs.plan_reader.release_components(),
            *inputs.mutation_interlock.release_components(),
        )
        roles = [item[0] for item in inventories]
        components = [item[1] for item in inventories]
        self.assertEqual(len(roles), len(set(roles)))
        self.assertEqual(len(components), len({id(item) for item in components}))
        self.assertEqual(
            inputs.acceptance_verifier.release_components(),
            (("ibkr_autonomous_receipt_key_loader", keychain, ("read",)),),
        )

    def test_acceptance_reauthenticates_and_rejects_change_or_expiry(self) -> None:
        inputs, _keychain = self._build()
        self.addCleanup(inputs.owned_resource.close)
        changed = replace(
            inputs.write_evidence,
            reviewed_contract_id="d" * 64,
        )
        self.assert_code(
            "IBKR_AUTONOMOUS_INPUT_WRITE_EVIDENCE_MISMATCH",
            lambda: inputs.acceptance_verifier(changed),
        )
        self.clock_value = NOW + timedelta(minutes=6)
        self.assert_code(
            "IBKR_AUTONOMOUS_INPUT_AUTHORITY_NOT_CURRENT",
            lambda: inputs.acceptance_verifier(inputs.write_evidence),
        )

    def test_newer_same_binding_authority_rotates_across_trading_days(self) -> None:
        policy_receipt = self._policy_receipt_body()
        policy_receipt["expires_at"] = (NOW + timedelta(days=2)).isoformat()
        self._write_policy_receipt(body=policy_receipt)
        inputs, _keychain = self._build()
        self.addCleanup(inputs.owned_resource.close)
        original_hash = inputs.acceptance_verifier.authority_hash
        self.assertGreater(inputs.write_evidence.expires_at, NOW + timedelta(days=1))

        self.clock_value = NOW + timedelta(days=1)
        rotated = self._authority_body()
        rotated["issued_at"] = (
            self.clock_value - timedelta(minutes=1)
        ).isoformat()
        rotated["expires_at"] = (
            self.clock_value + timedelta(minutes=5)
        ).isoformat()
        rotated["account_controls"]["read_only_api_verified_at"] = (
            self.clock_value - timedelta(minutes=2)
        ).isoformat()
        self._write_authority(body=rotated)
        self.assertIsNone(inputs.acceptance_verifier(inputs.write_evidence))
        self.assertNotEqual(
            inputs.acceptance_verifier.authority_hash,
            original_hash,
        )

        rollback = deepcopy(rotated)
        rollback["issued_at"] = (
            self.clock_value - timedelta(minutes=2)
        ).isoformat()
        rollback["account_controls"]["read_only_api_verified_at"] = (
            self.clock_value - timedelta(minutes=3)
        ).isoformat()
        self._write_authority(body=rollback)
        self.assert_code(
            "IBKR_AUTONOMOUS_INPUT_AUTHORITY_ROLLBACK",
            lambda: inputs.acceptance_verifier(inputs.write_evidence),
        )

    def test_config_provenance_flags_cannot_replace_owner_pricing_receipt(self) -> None:
        self.policy_receipt_path.unlink()
        keychain = RecordingKeychain()
        with patch(
            "titan_brain.live.ibkr_autonomous_inputs.LiveStateStore"
        ) as state_store, patch(
            "titan_brain.live.ibkr_autonomous_inputs.AccountWriterLock"
        ) as writer_lock:
            self.assert_code(
                "IBKR_AUTONOMOUS_INPUT_POLICY_RECEIPT_UNAVAILABLE",
                lambda: self._build(keychain),
            )
        state_store.assert_not_called()
        writer_lock.assert_not_called()
        self.assertEqual(keychain.calls, [])
        self.assertFalse(self.locks.exists())

    def test_policy_receipt_is_reauthenticated_around_risk_and_write_edges(self) -> None:
        inputs, keychain = self._build()
        self.addCleanup(inputs.owned_resource.close)
        risk_calls: list[tuple[object, object, object]] = []
        inputs.risk_policy_check.delegate = (
            lambda snapshot, plan, now: risk_calls.append((snapshot, plan, now))
        )
        entry_plan = SimpleNamespace(purpose=IbkrOrderPurpose.ENTRY)
        exit_plan = SimpleNamespace(purpose=IbkrOrderPurpose.EXIT)
        protection_plan = SimpleNamespace(purpose=IbkrOrderPurpose.PROTECTION)
        lineage = "7" * 64
        snapshot = AccountSnapshot(
            account_masked=f"****{self.policy.account_last4}",
            observed_at=NOW,
            received_at=NOW,
            account_state="active",
            account_type="no_borrow_margin",
            funds=FundsSnapshot(
                total_value=Decimal("1000"),
                cash=Decimal("1000"),
                buying_power=Decimal("1000"),
                unleveraged_buying_power=Decimal("1000"),
            ),
            equity_positions=(),
            equity_orders=(),
            option_position_count=0,
            option_order_count=0,
            advanced_order_count=0,
            standard_equity_positions_complete=True,
            standard_equity_orders_complete=True,
            option_positions_complete=True,
            option_orders_complete=True,
            advanced_orders_complete=True,
            auth_point_in_time=True,
            daily_realized_pnl=Decimal("0"),
            weekly_realized_pnl=Decimal("0"),
            peak_equity=Decimal("1000"),
            daily_realized_pnl_complete=True,
            weekly_realized_pnl_complete=True,
            peak_equity_complete=True,
            risk_evidence_authoritative=True,
            risk_evidence_source="authenticated-test-risk-evidence",
            risk_evidence_as_of=NOW,
            risk_baseline_identity_hash="4" * 64,
            risk_baseline_receipt_hash="5" * 64,
            risk_high_water_identity_hash="6" * 64,
            risk_high_water_lineage_hash=lineage,
            risk_high_water_receipt_hash=risk_high_water_receipt_hash(
                identity_hash="6" * 64,
                baseline_receipt_hash="5" * 64,
                lineage_hash=lineage,
                peak_equity=1000,
            ),
        )
        self.assertIsNone(
            inputs.risk_policy_check.bind_entry_risk_activation(
                lineage_hash=lineage,
                minimum_peak=Decimal("1000"),
            )
        )
        self.assertIsNone(inputs.risk_policy_check(snapshot, entry_plan, NOW))
        self.assertEqual(risk_calls, [(snapshot, entry_plan, NOW)])
        # Build read two keys; the risk edge reads the policy key before and
        # after the underlying durable check.
        self.assertEqual(len(keychain.calls), 4)

        replacement_snapshot = replace(
            snapshot,
            peak_equity=Decimal("1000"),
            risk_high_water_identity_hash="8" * 64,
            risk_high_water_lineage_hash="9" * 64,
            risk_high_water_receipt_hash=risk_high_water_receipt_hash(
                identity_hash="8" * 64,
                baseline_receipt_hash="5" * 64,
                lineage_hash="9" * 64,
                peak_equity=1000,
            ),
        )
        self.assert_code(
            "IBKR_AUTONOMOUS_INPUT_ACTIVATION_RISK_EVIDENCE_CHANGED",
            lambda: inputs.risk_policy_check(
                replacement_snapshot,
                entry_plan,
                NOW,
            ),
        )
        self.assertEqual(len(keychain.calls), 4)

        changed = self._policy_receipt_body()
        changed["owner_approval"]["reference"] = "OWNER-POLICY-INPUT-TEST-002"
        self._write_policy_receipt(body=changed)
        self.assert_code(
            "IBKR_AUTONOMOUS_INPUT_POLICY_RECEIPT_CHANGED",
            lambda: inputs.risk_policy_check(snapshot, entry_plan, NOW),
        )
        # An owner-pricing receipt controls new risk only.  Its failure cannot
        # disable reduce-only protection/exit checks.
        calls_before_reduce_only = len(keychain.calls)
        self.assertIsNone(
            inputs.risk_policy_check(snapshot, protection_plan, NOW)
        )
        self.assertIsNone(inputs.risk_policy_check(snapshot, exit_plan, NOW))
        self.assertEqual(len(keychain.calls), calls_before_reduce_only)
        self.assertEqual(
            risk_calls[-2:],
            [
                (snapshot, protection_plan, NOW),
                (snapshot, exit_plan, NOW),
            ],
        )
        self.assert_code(
            "IBKR_AUTONOMOUS_INPUT_POLICY_RECEIPT_CHANGED",
            lambda: inputs.acceptance_verifier(inputs.write_evidence),
        )

    def test_tampered_or_unverified_pricing_receipt_is_inert(self) -> None:
        body = self._policy_receipt_body()
        body["effective_pricing"]["fee_scope"] = "base_commission_only"
        self._write_policy_receipt(body=body)
        with patch(
            "titan_brain.live.ibkr_autonomous_inputs.LiveStateStore"
        ) as state_store, patch(
            "titan_brain.live.ibkr_autonomous_inputs.AccountWriterLock"
        ) as writer_lock:
            self.assert_code(
                "IBKR_AUTONOMOUS_INPUT_POLICY_RECEIPT_REJECTED",
                self._build,
            )
        state_store.assert_not_called()
        writer_lock.assert_not_called()
        self.assertFalse(self.locks.exists())

    def test_bad_key_and_injected_keychain_error_are_redacted_and_inert(self) -> None:
        marker = "raw-secret-must-not-escape"
        bad = RecordingKeychain(b"x" * 40)
        self.assert_code(
            "IBKR_AUTONOMOUS_INPUT_AUTHORITY_REJECTED",
            lambda: self._build(bad),
        )
        self.assertFalse(self.locks.exists())
        self.assertEqual(self._rows("activation_records"), [])
        self.assertEqual(self._rows("account_writer_lease"), [])
        failing = RecordingKeychain(error=RuntimeError(marker))
        with self.assertRaises(IbkrAutonomousInputError) as caught:
            self._build(failing)
        self.assertEqual(
            str(caught.exception), "IBKR_AUTONOMOUS_INPUT_KEYCHAIN_UNAVAILABLE"
        )
        self.assertNotIn(marker, str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_missing_or_empty_state_is_not_created_or_migrated(self) -> None:
        self.state_path.unlink()
        with patch(
            "titan_brain.live.ibkr_autonomous_inputs.LiveStateStore"
        ) as semantic_store, patch(
            "titan_brain.live.ibkr_autonomous_inputs.AccountWriterLock"
        ) as writer_lock:
            self.assert_code(
                "IBKR_AUTONOMOUS_INPUT_STATE_UNAVAILABLE",
                self._build,
            )
        semantic_store.assert_not_called()
        writer_lock.assert_not_called()
        self.assertFalse(self.state_path.exists())
        self.state_path.touch()
        with patch(
            "titan_brain.live.ibkr_autonomous_inputs.LiveStateStore"
        ) as semantic_store, patch(
            "titan_brain.live.ibkr_autonomous_inputs.AccountWriterLock"
        ) as writer_lock:
            self.assert_code(
                "IBKR_AUTONOMOUS_INPUT_STATE_SCHEMA_OR_OPEN_INVALID",
                self._build,
            )
        semantic_store.assert_not_called()
        writer_lock.assert_not_called()
        connection = sqlite3.connect(self.state_path)
        try:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
                ).fetchone()[0],
                0,
            )
        finally:
            connection.close()
        self.assertFalse(self.locks.exists())

    def test_runtime_release_config_policy_and_schema_are_exact(self) -> None:
        connection = sqlite3.connect(self.state_path)
        try:
            connection.execute(
                "UPDATE runtime_identity SET release_manifest_hash=? WHERE singleton=1",
                ("f" * 64,),
            )
            connection.commit()
        finally:
            connection.close()
        self.assert_code(
            "IBKR_AUTONOMOUS_INPUT_STATE_RUNTIME_BINDING_INVALID",
            self._build,
        )
        self.assertFalse(self.locks.exists())

    def test_state_schema_version_mismatch_is_rejected_without_migration(self) -> None:
        connection = sqlite3.connect(self.state_path)
        try:
            connection.execute("PRAGMA user_version=4")
            connection.commit()
        finally:
            connection.close()
        self.assert_code(
            "IBKR_AUTONOMOUS_INPUT_STATE_SCHEMA_OR_OPEN_INVALID",
            self._build,
        )
        connection = sqlite3.connect(self.state_path)
        try:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 4)
        finally:
            connection.close()
        self.assertFalse(self.locks.exists())

    def test_unsafe_authority_path_is_rejected_before_keychain_use(self) -> None:
        target = self.root / "authority-target.json"
        self.authority_path.replace(target)
        self.authority_path.symlink_to(target)
        keychain = RecordingKeychain()
        self.assert_code(
            "IBKR_AUTONOMOUS_INPUT_AUTHORITY_PATH_UNSAFE",
            lambda: self._build(keychain),
        )
        self.assertEqual(keychain.calls, [])
        self.assertFalse(self.locks.exists())

    def test_policy_cannot_select_another_key_source_or_authority_path(self) -> None:
        original = json.loads(self.config_path.read_text(encoding="utf-8"))
        cases = (
            ("ibkr_autonomous_authority_key_source", "environment"),
            ("ibkr_autonomous_authority_relative_path", "../authority.json"),
            ("ibkr_autonomous_api_version", "10.49.0"),
            ("ibkr_autonomous_policy_receipt_hmac_key", "forbidden-secret"),
        )
        for field, value in cases:
            with self.subTest(field=field):
                changed = deepcopy(original)
                changed["execution"][field] = value
                self._write_config(changed)
                self.assert_code(
                    "IBKR_AUTONOMOUS_INPUT_POLICY_INVALID",
                    self._build,
                )
                self.assertFalse(self.locks.exists())
        self._write_config(original)

    def test_policy_receipt_source_and_path_are_fail_closed(self) -> None:
        original = json.loads(self.config_path.read_text(encoding="utf-8"))
        cases = (
            (
                "ibkr_autonomous_policy_receipt_key_source",
                "environment",
                "IBKR_AUTONOMOUS_INPUT_POLICY_INVALID",
            ),
            (
                "ibkr_autonomous_policy_receipt_relative_path",
                "../policy.json",
                "IBKR_AUTONOMOUS_INPUT_POLICY_INVALID",
            ),
        )
        for field, value, code in cases:
            with self.subTest(field=field):
                changed = deepcopy(original)
                changed["execution"][field] = value
                self._write_config(changed)
                self.assert_code(code, self._build)
                self.assertFalse(self.locks.exists())
        self._write_config(original)

    def test_resource_is_closed_when_plan_resource_construction_fails(self) -> None:
        closed: list[LiveStateStore] = []
        original_close = LiveStateStore.close

        def tracked_close(state: LiveStateStore) -> None:
            closed.append(state)
            original_close(state)

        with patch(
            "titan_brain.live.ibkr_autonomous_inputs."
            "StateBackedAutonomousIbkrPlanProducer",
            side_effect=RuntimeError("private-construction-detail"),
        ), patch.object(LiveStateStore, "close", new=tracked_close):
            self.assert_code(
                "IBKR_AUTONOMOUS_INPUT_RESOURCE_ASSEMBLY_FAILED",
                self._build,
            )
        self.assertEqual(len(closed), 1)
        self.assertFalse(self.locks.exists())


if __name__ == "__main__":
    unittest.main()
