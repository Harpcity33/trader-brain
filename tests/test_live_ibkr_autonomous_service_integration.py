"""Focused fail-closed tests for autonomous launcher/service wiring."""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import hmac
import os
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from titan_brain.live import cli
from titan_brain.live.broker.base import (
    AccountSnapshot,
    BrokerContractViolation,
    BrokerMutationBlocked,
    FundsSnapshot,
)
from titan_brain.live.broker.base import (
    ClientRefRecoverySource,
    OrderCoverageContract,
    OrderFamily,
    OrderFamilyCoverage,
    OrderFamilyCoverageStatus,
)
from titan_brain.live.broker.ibkr_instrument import IbkrInstrumentProvider
from titan_brain.live.broker.ibkr_risk_evidence import (
    IBKR_DAILY_RISK_BASELINE_SCHEMA,
    IbkrRiskEvidenceAccountSnapshotReader,
    IbkrRiskHighWaterLedger,
    IbkrRiskLedgerBindings,
)
from titan_brain.live.broker.ibkr_sdk import IbkrSdkSession
from titan_brain.live.broker.ibkr_transport import (
    IbkrProductionTransport,
    autonomous_ibkr_descriptor,
)
from titan_brain.live.broker.production import (
    CollectedObservation,
    SupportedProductionBrokerAdapter,
)
from titan_brain.live.composition import RuntimeComposition, RuntimeCompositionError
from titan_brain.live.ibkr_autonomous_authority import (
    IBKR_AUTONOMOUS_AUTHORITY_SCHEMA,
)
from titan_brain.live.ibkr_autonomous_inputs import (
    IbkrAutonomousInputError,
    build_release_bound_ibkr_autonomous_inputs,
)
from titan_brain.live.ibkr_autonomous_policy_receipt import (
    IBKR_AUTONOMOUS_POLICY_RECEIPT_SCHEMA,
)
from titan_brain.live.local_assembly import LocalAssemblyError, LocalProviderAssembly
from titan_brain.live.notifications import (
    DeliveryAssurance,
    NotificationRoute,
    destination_fingerprint,
)
from titan_brain.live.policy import PolicyBundle, canonical_json
from titan_brain.live.provider_clients import CredentialUnavailable
from titan_brain.live.risk_evidence_binding import risk_high_water_receipt_hash
from titan_brain.live.state import LiveStateStore
from titan_brain.live.writer_lock import AccountWriterLock


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 14, 15, 0, tzinfo=timezone.utc)
SECRET = b"autonomous-input-test-secret-is-at-least-32-bytes"


class _InventoryPlanState:
    def rows(self, *_args, **_kwargs):
        return []

    def verify_event_chain(self):
        return True, 0, "0" * 64


def _inventory_plan_clock():
    return NOW


class _InventoryPlanReader:
    def __init__(self) -> None:
        self.state = _InventoryPlanState()

    def release_components(self):
        return (
            (
                "ibkr_autonomous_plan_state",
                self.state,
                ("rows", "verify_event_chain"),
            ),
            (
                "ibkr_autonomous_plan_clock",
                _inventory_plan_clock,
                ("__call__",),
            ),
        )

    def __call__(self, _request):
        return None


class _InventoryPlanSealer:
    def __init__(self, reader) -> None:
        self.reader = reader

    def release_components(self):
        return (
            (
                "ibkr_autonomous_plan_reader",
                self.reader,
                ("release_components", "__call__"),
            ),
        )

    def __call__(self, **_kwargs):
        return None


class _InventoryDiscoveryProvider:
    def __init__(self, reader) -> None:
        self.reader = reader
        self.provider_binding_id = "inventory-provider-binding"
        self.timeout_seconds = 1
        self._source = object()

    @property
    def identity(self):
        return "inventory-discovery-provider"

    @property
    def market_source(self):
        return self._source

    def release_components(self):
        return (
            (
                "ibkr_autonomous_plan_reader",
                self.reader,
                ("release_components", "__call__"),
            ),
        )

    def tradability_ready(self, *, now):
        return now is not None

    def build_executor(self, **_kwargs):
        return object()


def _inventory_manifest() -> dict[str, object]:
    source = Path(__file__).resolve()
    data = source.read_bytes()
    return {
        "release_manifest_hash": "7" * 64,
        "files": [
            {
                "path": source.relative_to(ROOT).as_posix(),
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
            }
        ],
    }


class StaticKeychain:
    def __init__(self, secret: bytes = SECRET) -> None:
        self.secret = secret
        self.items = []

    def read(self, item):
        self.items.append(item)
        return self.secret


def complete_order_coverage() -> OrderCoverageContract:
    return OrderCoverageContract(
        contract_version="synthetic-exhaustive-risk-wiring-v1",
        evidence_observed_at=NOW,
        families=tuple(
            OrderFamilyCoverage(
                family=family,
                status=OrderFamilyCoverageStatus.COMPLETE_GENERAL,
                evidence_id=f"synthetic-{family.value}",
                broker_authoritative=True,
                all_pages_consumed=True,
                includes_working_orders_across_dates=True,
                includes_parent_child_conditional=(
                    family is OrderFamily.ADVANCED_EQUITY
                ),
            )
            for family in OrderFamily
        ),
        client_ref_recovery_source=(
            ClientRefRecoverySource.EXHAUSTIVE_ORDER_HISTORY
        ),
        broker_preserves_client_ref=True,
        negative_client_ref_results_authoritative=False,
    )


class _RiskWiringSnapshotReader:
    coverage = complete_order_coverage()

    def release_components(self):
        return ()

    def __call__(self):
        return AccountSnapshot(
            account_masked="****3103",
            observed_at=NOW,
            received_at=NOW,
            account_state="active",
            account_type="MARGIN",
            funds=FundsSnapshot(
                total_value="1000",
                cash="500",
                buying_power="500",
                unleveraged_buying_power="500",
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
            daily_realized_pnl="2.50",
            daily_realized_pnl_complete=True,
            risk_evidence_authoritative=True,
            risk_evidence_source="ibkr:reqPnL.realizedPnL:current-day",
            risk_evidence_as_of=NOW,
        )


class _RiskWiringReadBridge:
    def __init__(self, reader) -> None:
        self.reader = reader

    def release_components(self):
        return ()

    def get_account_base(self, _exact_account_id):
        return CollectedObservation(
            snapshot=self.reader(),
            collection_id="a" * 64,
            request_started_at=NOW,
            request_completed_at=NOW,
        )

    def list_order_family_page(self, *_args, **_kwargs):
        raise AssertionError("not needed by risk wiring test")

    def lookup_equity_orders_by_client_ref(self, *_args, **_kwargs):
        raise AssertionError("not needed by risk wiring test")


class _RiskWiringRuntime:
    def __init__(self) -> None:
        self.reader = _RiskWiringSnapshotReader()
        self.instrument = object.__new__(IbkrInstrumentProvider)
        self.command_prepared = 0
        self.command_connected = 0
        self.command_session = None
        self.stopped = False
        self.read_bridge = _RiskWiringReadBridge(self.reader)
        self.components = SimpleNamespace(
            read_bridge=self.read_bridge,
            account_snapshot_reader=self.reader,
            instrument_provider=self.instrument,
            contract_factory=SimpleNamespace,
            order_factory=SimpleNamespace,
        )

    @property
    def account_binding_fingerprint(self):
        return "4" * 64

    def connect_reads(self):
        return self.components

    def status(self):
        return SimpleNamespace()

    def stop(self):
        self.stopped = True

    def autonomous_descriptor(self, **kwargs):
        return autonomous_ibkr_descriptor(
            exact_account_id="U1233103",
            account_masked="****3103",
            account_binding_fingerprint="4" * 64,
            authorization_binding_id="5" * 64,
            coverage=kwargs["coverage"],
            authority=kwargs["authority"],
            authority_bindings=kwargs["authority_bindings"],
            now=kwargs["now"],
        )

    def _command_session(self, **kwargs):
        authority = kwargs["authorize_dispatch"]
        self.command_session = IbkrSdkSession(
            client=SimpleNamespace(),
            sdk_version="10.50.2",
            expected_account="U1233103",
            account_binding_fingerprint="4" * 64,
            environment="live",
            client_id=19736,
            order_cancel_factory=SimpleNamespace,
            mutation_interlock=kwargs["mutation_interlock"],
            authorize_dispatch=authority,
            clock=authority._clock,
        )
        return self.command_session

    def prepare_disconnected_command(self, **kwargs):
        self.command_prepared += 1
        return self._command_session(**kwargs)

    def connect_command(self, **kwargs):
        self.command_connected += 1
        return self._command_session(**kwargs)


def supported_policy(mode: str = "unattended") -> PolicyBundle:
    base = PolicyBundle.load(
        ROOT, config_relative="config/full_live_ibkr.json"
    )
    config = copy.deepcopy(base.config)
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
    config["execution"].update(
        {
            "execution_authority_mode": mode,
            "broker_adapter": "supported_production_transport",
            "production_transport_id": "ibkr-tws-api-10.50.2-v1",
            "production_account_binding_fingerprint": "4" * 64,
            "production_authorization_binding_id": "5" * 64,
            "ibkr_provider_contract_id": "6" * 64,
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
                "minimum_commission_reserve_per_order_dollars": "1.00",
            }
        )
    candidate = replace(base, config=config)
    candidate.validate()
    return candidate


def authority_body(policy: PolicyBundle) -> dict[str, object]:
    execution = policy.config["execution"]
    return {
        "schema_version": IBKR_AUTONOMOUS_AUTHORITY_SCHEMA,
        "issued_at": (NOW - timedelta(minutes=1)).isoformat(),
        "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
        "bindings": {
            "release_manifest_hash": "1" * 64,
            "config_hash": policy.config_hash,
            "policy_binding_id": policy.policy_hash,
            "account_key": policy.account_key,
            "account_masked": f"****{policy.account_last4}",
            "account_binding_fingerprint": execution[
                "production_account_binding_fingerprint"
            ],
            "authorization_binding_id": execution[
                "production_authorization_binding_id"
            ],
            "provider_contract_id": execution["ibkr_provider_contract_id"],
            "transport_id": execution["production_transport_id"],
            "api_name": execution["ibkr_autonomous_api_name"],
            "api_version": execution["ibkr_autonomous_api_version"],
            "environment": execution["ibkr_autonomous_environment"],
            "client_id": execution["ibkr_autonomous_client_id"],
        },
        "support": {
            "reference": "IBKR-SUPPORT-CASE-12345",
            "status": "provider_confirmed_supported",
            "confirmed_at": (NOW - timedelta(minutes=2)).isoformat(),
            "scope": (
                "unattended_regular_hours_api_orders_with_external_market_data"
            ),
        },
        "account_controls": {
            "read_only_api_enabled": False,
            "read_only_api_verified_at": (NOW - timedelta(minutes=2)).isoformat(),
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


def policy_receipt_body(policy: PolicyBundle) -> dict[str, object]:
    execution = policy.config["execution"]
    return {
        "schema_version": IBKR_AUTONOMOUS_POLICY_RECEIPT_SCHEMA,
        "issued_at": (NOW - timedelta(minutes=1)).isoformat(),
        "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
        "bindings": {
            "release_manifest_hash": "1" * 64,
            "config_hash": policy.config_hash,
            "policy_hash": policy.policy_hash,
            "risk_hash": policy.risk_hash,
            "account_key": policy.account_key,
            "account_masked": f"****{policy.account_last4}",
            "account_binding_fingerprint": execution[
                "production_account_binding_fingerprint"
            ],
            "authorization_binding_id": execution[
                "production_authorization_binding_id"
            ],
            "provider_contract_id": execution["ibkr_provider_contract_id"],
            "transport_id": execution["production_transport_id"],
        },
        "owner_approval": {
            "status": "owner_approved",
            "approved_at": (NOW - timedelta(minutes=3)).isoformat(),
            "reference": "OWNER-POLICY-SERVICE-INTEGRATION-001",
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
        "target_exit_policy": copy.deepcopy(policy.config["exits"]),
        "effective_pricing": {
            "status": "provider_account_verified",
            "verified_at": (NOW - timedelta(minutes=2)).isoformat(),
            "provider": "interactive_brokers",
            "source_kind": (
                "authenticated_ibkr_account_effective_pricing_receipt"
            ),
            "source_reference": "IBKR-ACCOUNT-PRICING-SERVICE-TEST-001",
            "source_receipt_sha256": "8" * 64,
            "currency": "USD",
            "routing_scope": "smart_routed_us_stock_orders",
            "fee_scope": "all_in_commission_and_regulatory_fees_per_order",
            "all_in_commission_floor_dollars": "1.00",
            "floor_conservative_for_allowed_order_scope": True,
        },
    }


class AutonomousInputsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.install = Path(self.temporary.name).resolve()
        self.policy = supported_policy()

    def write_authority(self) -> Path:
        path = self.install / "control/ibkr/autonomous-provider-authority.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        body = authority_body(self.policy)
        body["hmac_sha256"] = hmac.new(
            SECRET,
            canonical_json(body).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        path.write_text(canonical_json(body) + "\n", encoding="utf-8")
        path.chmod(0o600)
        receipt_path = (
            self.install / "control/ibkr/autonomous-owner-policy-pricing.json"
        )
        receipt = policy_receipt_body(self.policy)
        receipt["hmac_sha256"] = hmac.new(
            SECRET,
            canonical_json(receipt).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        receipt_path.write_text(
            canonical_json(receipt) + "\n", encoding="utf-8"
        )
        receipt_path.chmod(0o600)
        return path

    def write_risk_baseline(self) -> Path:
        path = self.install / "control/ibkr/daily-risk-baseline.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        body = {
            "schema_version": IBKR_DAILY_RISK_BASELINE_SCHEMA,
            "issued_at": (NOW - timedelta(minutes=1)).isoformat(),
            "bindings": {
                "release_manifest_hash": "1" * 64,
                "config_hash": self.policy.config_hash,
                "policy_binding_id": self.policy.policy_hash,
                "risk_binding_id": self.policy.risk_hash,
                "account_key": self.policy.account_key,
                "account_masked": "****3103",
                "account_binding_fingerprint": "4" * 64,
                "valid_for_trading_date": "2026-09-14",
                "prior_trading_date": "2026-09-11",
            },
            "evidence": {
                "currency": "USD",
                "week_to_date_realized_pnl_through_prior_trading_day": "4.25",
                "prior_high_water_equity": "1200",
                "broker_authoritative": True,
                "account_scope": "exact_account",
                "provider_source": "ibkr-flex-query-synthetic-test",
                "provider_observed_at": (NOW - timedelta(minutes=2)).isoformat(),
                "provider_receipt_sha256": "9" * 64,
            },
        }
        body["hmac_sha256"] = hmac.new(
            SECRET,
            canonical_json(body).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        path.write_text(canonical_json(body) + "\n", encoding="utf-8")
        path.chmod(0o600)
        return path

    def build_inputs(self):
        self.write_authority()
        state_path = self.install / "state/full-live.sqlite3"
        state = LiveStateStore(state_path)
        state.initialize_runtime(
            runtime_id=self.policy.runtime_id,
            account_key=self.policy.account_key,
            release_manifest_hash="1" * 64,
            config_hash=self.policy.config_hash,
            policy_hash=self.policy.policy_hash,
            initialized_at=NOW - timedelta(minutes=5),
        )
        state.close()
        with mock.patch.object(
            PolicyBundle, "load", return_value=self.policy
        ), mock.patch(
            "titan_brain.live.ibkr_autonomous_inputs.user_account_writer_lock_directory",
            return_value=self.install / "locks",
        ):
            return build_release_bound_ibkr_autonomous_inputs(
                release_root=ROOT,
                install_root=self.install,
                full_live_config_name="full_live_ibkr.json",
                release_manifest_hash="1" * 64,
                clock=lambda: NOW,
                keychain=StaticKeychain(),
                connect_command_session=False,
            )

    def bootstrap_risk_ledger(self) -> None:
        path = self.install / "state/ibkr-risk-high-water.sqlite3"
        ledger = IbkrRiskHighWaterLedger(
            path,
            bindings=IbkrRiskLedgerBindings(
                release_manifest_hash="1" * 64,
                config_hash=self.policy.config_hash,
                policy_binding_id=self.policy.policy_hash,
                risk_binding_id=self.policy.risk_hash,
                account_key=self.policy.account_key,
                account_masked="****3103",
                account_binding_fingerprint="4" * 64,
            ),
            allow_create=True,
        )
        ledger.close()

    def test_builder_reuses_exact_lock_reader_state_and_sealer(self) -> None:
        self.write_authority()
        state_path = self.install / "state/full-live.sqlite3"
        state = LiveStateStore(state_path)
        state.initialize_runtime(
            runtime_id=self.policy.runtime_id,
            account_key=self.policy.account_key,
            release_manifest_hash="1" * 64,
            config_hash=self.policy.config_hash,
            policy_hash=self.policy.policy_hash,
            initialized_at=NOW - timedelta(minutes=5),
        )
        state.close()
        keychain = StaticKeychain()
        with mock.patch.object(
            PolicyBundle, "load", return_value=self.policy
        ), mock.patch(
            "titan_brain.live.ibkr_autonomous_inputs.user_account_writer_lock_directory",
            return_value=self.install / "locks",
        ):
            inputs = build_release_bound_ibkr_autonomous_inputs(
                release_root=ROOT,
                install_root=self.install,
                full_live_config_name="full_live_ibkr.json",
                release_manifest_hash="1" * 64,
                clock=lambda: NOW,
                keychain=keychain,
            )
        self.addCleanup(inputs.owned_resource.close)
        self.assertEqual(inputs.authority_mode, "unattended")
        self.assertIs(inputs.service_writer_lock, inputs.mutation_interlock.lock)
        self.assertIs(inputs.plan_reader, inputs.plan_sealer.reader)
        self.assertIs(inputs.owned_resource, inputs.plan_reader.state)
        self.assertFalse(inputs.service_writer_lock.held)
        self.assertIsNone(inputs.acceptance_verifier(inputs.write_evidence))
        self.assertEqual(len(keychain.items), 4)

    def test_missing_authority_fails_before_state_or_lock_construction(self) -> None:
        with mock.patch.object(
            PolicyBundle, "load", return_value=self.policy
        ), mock.patch(
            "titan_brain.live.ibkr_autonomous_inputs.load_verified_ibkr_autonomous_authority",
            side_effect=IbkrAutonomousInputError(
                "IBKR_AUTONOMOUS_INPUT_AUTHORITY_UNAVAILABLE"
            ),
        ), mock.patch(
            "titan_brain.live.ibkr_autonomous_inputs.LiveStateStore"
        ) as state_store, mock.patch(
            "titan_brain.live.ibkr_autonomous_inputs.AccountWriterLock"
        ) as writer_lock:
            with self.assertRaisesRegex(
                IbkrAutonomousInputError, "AUTHORITY_UNAVAILABLE"
            ):
                build_release_bound_ibkr_autonomous_inputs(
                    release_root=ROOT,
                    install_root=self.install,
                    full_live_config_name="full_live_ibkr.json",
                    release_manifest_hash="1" * 64,
                    clock=lambda: NOW,
                    keychain=StaticKeychain(),
                )
        state_store.assert_not_called()
        writer_lock.assert_not_called()
        self.assertFalse((self.install / "state").exists())

    def test_verified_account_control_maps_raw_ibkr_type_to_policy_semantic(self) -> None:
        self.write_authority()
        state_path = self.install / "state/full-live.sqlite3"
        state = LiveStateStore(state_path)
        state.initialize_runtime(
            runtime_id=self.policy.runtime_id,
            account_key=self.policy.account_key,
            release_manifest_hash="1" * 64,
            config_hash=self.policy.config_hash,
            policy_hash=self.policy.policy_hash,
            initialized_at=NOW - timedelta(minutes=5),
        )
        state.close()
        with mock.patch.object(
            PolicyBundle, "load", return_value=self.policy
        ), mock.patch(
            "titan_brain.live.ibkr_autonomous_inputs.user_account_writer_lock_directory",
            return_value=self.install / "locks",
        ):
            inputs = build_release_bound_ibkr_autonomous_inputs(
                release_root=ROOT,
                install_root=self.install,
                full_live_config_name="full_live_ibkr.json",
                release_manifest_hash="1" * 64,
                clock=lambda: NOW,
                keychain=StaticKeychain(),
            )
        self.addCleanup(inputs.owned_resource.close)
        raw = AccountSnapshot(
            account_masked="****3103",
            observed_at=NOW,
            received_at=NOW,
            account_state="active",
            account_type="MARGIN",
            funds=FundsSnapshot(
                total_value="1000",
                cash="500",
                buying_power="500",
                unleveraged_buying_power="500",
            ),
            equity_positions=(),
            equity_orders=(),
            option_position_count=0,
            option_order_count=0,
            advanced_order_count=0,
            standard_equity_positions_complete=True,
            standard_equity_orders_complete=False,
            option_positions_complete=True,
            option_orders_complete=False,
            advanced_orders_complete=False,
            auth_point_in_time=True,
            daily_realized_pnl="0",
            daily_realized_pnl_complete=True,
            risk_evidence_authoritative=True,
            risk_evidence_source="ibkr:reqPnL.realizedPnL:current-day",
            risk_evidence_as_of=NOW,
        )
        collection = CollectedObservation(
            snapshot=raw,
            collection_id="9" * 64,
            request_started_at=NOW,
            request_completed_at=NOW,
        )
        transport = object.__new__(IbkrProductionTransport)
        transport._descriptor = SimpleNamespace(
            exact_account_id="DU-SYNTHETIC-3103",
            account_binding_fingerprint="4" * 64,
            capabilities=SimpleNamespace(
                account_masked="****3103",
                supports_unattended_writes=True,
            ),
        )
        transport.autonomous_authority = inputs.autonomous_authority
        transport.autonomous_authority_bindings = (
            inputs.autonomous_authority_bindings
        )
        transport.reads = SimpleNamespace(
            get_account_base=lambda _account: collection
        )
        transport._account = lambda _account: None
        transport._account_snapshot_enricher = lambda value: replace(
            value,
            weekly_realized_pnl="0",
            peak_equity="1000",
            weekly_realized_pnl_complete=True,
            peak_equity_complete=True,
            risk_evidence_source=(
                "ibkr:reqPnL.realizedPnL:current-day+"
                "authenticated-daily-baseline:ibkr-test:"
                + "7" * 64
                + ":"
                + "8" * 64
            ),
            risk_baseline_identity_hash="7" * 64,
            risk_baseline_receipt_hash="8" * 64,
            risk_high_water_identity_hash="9" * 64,
            risk_high_water_lineage_hash="a" * 64,
            risk_high_water_receipt_hash=risk_high_water_receipt_hash(
                identity_hash="9" * 64,
                baseline_receipt_hash="8" * 64,
                lineage_hash="a" * 64,
                peak_equity=1000,
            ),
        )

        normalized = transport.get_account_base("DU-SYNTHETIC-3103")
        self.assertEqual(normalized.snapshot.account_type, "no_borrow_margin")
        self.assertEqual(normalized.snapshot.account_state, "active")
        self.policy.require_account(
            normalized.snapshot.account_masked,
            normalized.snapshot.account_type,
        )
        self.assertEqual(raw.account_type, "MARGIN")

    def test_local_assembly_wires_one_risk_reader_into_preflight_and_hotpath(self) -> None:
        self.write_risk_baseline()
        inputs = self.build_inputs()
        self.bootstrap_risk_ledger()
        runtime = _RiskWiringRuntime()
        keychain = StaticKeychain()

        with mock.patch(
            "titan_brain.live.local_assembly.validate_installed_sdk"
        ), mock.patch.object(
            PolicyBundle, "load", return_value=self.policy
        ):
            assembly = LocalProviderAssembly(
                release_root=ROOT,
                install_root=self.install,
                keychain=keychain,
                clock=lambda: NOW,
                full_live_config_name="full_live_ibkr.json",
                ibkr_runtime_factory=lambda **_kwargs: runtime,
                ibkr_command_inputs=inputs,
            )
            assembly.full_live = copy.deepcopy(self.policy.config)
            try:
                transport = assembly.ibkr_production_transport()
                self.assertIs(transport, assembly._ibkr_transport)
                reader = assembly._ibkr_risk_snapshot_reader
                self.assertIsInstance(
                    reader, IbkrRiskEvidenceAccountSnapshotReader
                )
                attended = transport.preflight._attended
                self.assertIs(attended._snapshot_reader, reader)
                enricher = transport._account_snapshot_enricher
                self.assertIs(enricher.__self__, reader)
                self.assertIs(enricher.__func__, type(reader).enrich)
                transport_inventory = transport.release_components()
                preflight_component = next(
                    item
                    for item in transport_inventory
                    if item[0] == "ibkr_preflight_bridge"
                )
                delegate_component = transport.preflight.release_components()[0]
                self.assertIs(delegate_component[1], attended)
                self.assertIs(
                    attended._risk_policy_check,
                    inputs.risk_policy_check,
                )
                snapshot_component = next(
                    item
                    for item in attended.release_components()
                    if item[0] == "ibkr_account_snapshot_reader"
                )
                self.assertIs(snapshot_component[1], reader)
                self.assertEqual(
                    snapshot_component[2],
                    ("release_components", "coverage", "enrich", "__call__"),
                )
                self.assertIs(preflight_component[1], transport.preflight)
                enriched = reader()
                self.assertEqual(enriched.weekly_realized_pnl, Decimal("6.75"))
                self.assertEqual(enriched.peak_equity, Decimal("1200"))
                self.assertTrue(enriched.entry_risk_evidence_ready)
                adapter = SupportedProductionBrokerAdapter(
                    transport,
                    clock=lambda: NOW,
                )
                adapter.bind_entry_risk_activation(
                    lineage_hash=str(enriched.risk_high_water_lineage_hash),
                    minimum_peak=enriched.peak_equity,
                )
                self.assertEqual(
                    inputs.risk_policy_check._activation_lineage_hash,
                    enriched.risk_high_water_lineage_hash,
                )
                self.assertEqual(
                    inputs.risk_policy_check._activation_peak_floor,
                    Decimal("1200"),
                )
                recurring = transport.get_account_base("U1233103")
                self.assertEqual(
                    recurring.snapshot.weekly_realized_pnl, Decimal("6.75")
                )
                self.assertEqual(
                    recurring.snapshot.account_type, "no_borrow_margin"
                )
                with self.assertRaisesRegex(
                    TypeError, "exact preflight-owned risk-evidence enricher"
                ):
                    IbkrProductionTransport(
                        descriptor=transport.descriptor,
                        session=transport.session,
                        ledger=transport.ledger,
                        reads=transport.reads,
                        preflight=transport.preflight,
                        contract_factory=transport.contract_factory,
                        order_factory=transport.order_factory,
                        policy_binding_id=transport.policy_binding_id,
                        provider_contract_id=transport.provider_contract_id,
                        authority=transport.authority,
                        autonomous_authority=transport.autonomous_authority,
                        autonomous_authority_bindings=(
                            transport.autonomous_authority_bindings
                        ),
                        account_snapshot_enricher=lambda value: value,
                        clock=transport._clock,
                    )
                (self.install / "control/ibkr/daily-risk-baseline.json").write_text(
                    "{}\n", encoding="utf-8"
                )
                unready = transport.get_account_base("U1233103")
                self.assertFalse(unready.snapshot.entry_risk_evidence_ready)
                self.assertIsNone(unready.snapshot.weekly_realized_pnl)
                self.assertIsNone(unready.snapshot.peak_equity)
                self.assertEqual(
                    unready.snapshot.account_type, "no_borrow_margin"
                )
                self.assertEqual(
                    unready.snapshot.risk_evidence_source,
                    "ibkr:reqPnL.realizedPnL:current-day",
                )
                self.assertEqual(runtime.command_prepared, 1)
            finally:
                assembly.close()

    def test_activation_probe_connects_command_graph_without_authorizing_writes(self) -> None:
        self.write_risk_baseline()
        inputs = replace(
            self.build_inputs(),
            connect_command_session=True,
            authorize_command_writes=False,
        )
        self.bootstrap_risk_ledger()
        runtime = _RiskWiringRuntime()
        with mock.patch(
            "titan_brain.live.local_assembly.validate_installed_sdk"
        ), mock.patch.object(
            PolicyBundle, "load", return_value=self.policy
        ):
            assembly = LocalProviderAssembly(
                release_root=ROOT,
                install_root=self.install,
                keychain=StaticKeychain(),
                clock=lambda: NOW,
                full_live_config_name="full_live_ibkr.json",
                ibkr_runtime_factory=lambda **_kwargs: runtime,
                ibkr_command_inputs=inputs,
            )
            assembly.full_live = copy.deepcopy(self.policy.config)
            try:
                transport = assembly.ibkr_production_transport()
                self.assertEqual(runtime.command_connected, 1)
                self.assertEqual(runtime.command_prepared, 0)
                self.assertIs(transport.session, runtime.command_session)
                self.assertFalse(
                    transport.session.command_lane_status().write_authority_granted
                )
            finally:
                assembly.close()

    def test_missing_baseline_or_ledger_preserves_raw_facts_but_denies_entry(self) -> None:
        for failure in ("baseline", "ledger"):
            with self.subTest(failure=failure):
                for path in (
                    self.install / "control/ibkr/daily-risk-baseline.json",
                    self.install / "state/ibkr-risk-high-water.sqlite3",
                    self.install / "state/ibkr-risk-high-water.sqlite3-wal",
                    self.install / "state/ibkr-risk-high-water.sqlite3-shm",
                ):
                    path.unlink(missing_ok=True)
                inputs = self.build_inputs()
                if failure == "ledger":
                    self.write_risk_baseline()
                else:
                    self.bootstrap_risk_ledger()
                runtime = _RiskWiringRuntime()
                with mock.patch(
                    "titan_brain.live.local_assembly.validate_installed_sdk"
                ), mock.patch.object(
                    PolicyBundle, "load", return_value=self.policy
                ):
                    assembly = LocalProviderAssembly(
                        release_root=ROOT,
                        install_root=self.install,
                        keychain=StaticKeychain(),
                        clock=lambda: NOW,
                        full_live_config_name="full_live_ibkr.json",
                        ibkr_runtime_factory=lambda **_kwargs: runtime,
                        ibkr_command_inputs=inputs,
                    )
                    assembly.full_live = copy.deepcopy(self.policy.config)
                    try:
                        transport = assembly.ibkr_production_transport()
                        snapshot = transport.get_account_base(
                            "U1233103"
                        ).snapshot
                        self.assertTrue(snapshot.whole_broker_reconciled)
                        self.assertEqual(snapshot.funds.total_value, Decimal("1000"))
                        self.assertFalse(snapshot.entry_risk_evidence_ready)
                        self.assertIsNone(snapshot.peak_equity)
                        attended = transport.preflight._attended
                        # Protection/exit snapshots require raw authoritative
                        # daily facts, not entry-only weekly/high-water proof.
                        self.assertIsNone(
                            attended._snapshot(
                                snapshot,
                                NOW,
                                expected_account_masked="****3103",
                                require_entry_risk_evidence=False,
                            )
                        )
                        with self.assertRaisesRegex(
                            BrokerMutationBlocked,
                            "IBKR_ENTRY_RISK_EVIDENCE_INCOMPLETE",
                        ):
                            attended._snapshot(
                                snapshot,
                                NOW,
                                expected_account_masked="****3103",
                                require_entry_risk_evidence=True,
                            )
                        self.assertEqual(runtime.command_prepared, 1)
                    finally:
                        assembly.close()


class AutonomousCompositionInventoryTests(unittest.TestCase):
    def test_sealer_is_static_profiled_and_shares_exact_owned_reader(self) -> None:
        reader = _InventoryPlanReader()
        sealer = _InventoryPlanSealer(reader)
        composition = RuntimeComposition(
            discovery_provider=_InventoryDiscoveryProvider(reader),
            autonomous_plan_sealer=sealer,
        )
        evidence = composition.bind_release(
            _inventory_manifest(), release_root=ROOT
        )
        roles = {item.role for item in evidence}
        self.assertTrue(
            {
                "discovery_provider",
                "ibkr_autonomous_plan_reader",
                "ibkr_autonomous_plan_state",
                "ibkr_autonomous_plan_clock",
                "ibkr_autonomous_plan_sealer",
            }.issubset(roles)
        )
        self.assertEqual(len(composition.runtime_profile_hash or ""), 64)
        with self.assertRaisesRegex(
            RuntimeCompositionError, "reused under another role"
        ):
            composition._verify_dynamic_component(
                reader,
                role="discovery_executor",
                semantic_members=("__call__",),
            )

        sealer.reader = _InventoryPlanReader()
        with self.assertRaisesRegex(
            RuntimeCompositionError, "shared reader binding changed"
        ):
            _ = composition.runtime_profile_hash

    def test_sealer_cannot_alias_a_different_reader_or_runtime_instance(self) -> None:
        reader = _InventoryPlanReader()
        wrong_sealer = _InventoryPlanSealer(_InventoryPlanReader())
        composition = RuntimeComposition(
            discovery_provider=_InventoryDiscoveryProvider(reader),
            autonomous_plan_sealer=wrong_sealer,
        )
        with self.assertRaisesRegex(
            RuntimeCompositionError, "DEPENDENCY_SHARED_READER_BINDING_MISMATCH"
        ):
            composition.bind_release(_inventory_manifest(), release_root=ROOT)

        valid_sealer = _InventoryPlanSealer(reader)
        valid = RuntimeComposition(
            discovery_provider=_InventoryDiscoveryProvider(reader),
            autonomous_plan_sealer=valid_sealer,
        )
        valid.bind_release(_inventory_manifest(), release_root=ROOT)
        with self.assertRaisesRegex(
            RuntimeCompositionError, "runtime implementation changed"
        ):
            valid.build_discovery_executor(
                policy=object(),
                state=object(),
                broker=object(),
                writer_lock=object(),
                latency=object(),
                authority=object(),
                plan_sealer=_InventoryPlanSealer(reader),
            )


class LauncherRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.namespace = runpy.run_path(
            str(ROOT / "scripts/titan-full-live"),
            run_name="titan_full_live_autonomous_launcher_test",
        )

    def select(self, command: str, policy, marker):
        selector = self.namespace["_release_bound_command_inputs"]
        with mock.patch.object(
            PolicyBundle, "load", return_value=policy
        ), mock.patch(
            "titan_brain.live.ibkr_autonomous_inputs.build_release_bound_ibkr_autonomous_inputs",
            return_value=marker,
        ) as autonomous, mock.patch(
            "titan_brain.live.ibkr_command_inputs.build_release_bound_ibkr_command_inputs",
            return_value=marker,
        ) as attended:
            result = selector(
                release_root=ROOT,
                install_root=ROOT,
                full_live_config_name="full_live_ibkr.json",
                release_manifest_hash="1" * 64,
                arguments=[command],
            )
        return result, autonomous, attended

    def run_installed_launcher(self, command: str):
        main = self.namespace["main"]
        install_root = ROOT / "mock-installed-root"
        policy = SimpleNamespace(
            config={
                "execution": {
                    "broker_adapter": "staged_stub",
                    "execution_authority_mode": "attended_only",
                }
            }
        )
        profile = object()
        normal = object()
        notification = object()
        control = object()
        coordinator = object()
        assembly = SimpleNamespace(
            runtime_composition=normal,
            notification_runtime_composition=notification,
            control_runtime_composition=control,
            coordinator_runtime_composition=coordinator,
            close=mock.Mock(),
        )
        manifest = {
            "release_manifest_hash": "1" * 64,
            "config_path": "config/full_live_ibkr.json",
        }
        with mock.patch.dict(
            main.__globals__, {"_paths": lambda: (ROOT, install_root)}
        ), mock.patch.object(
            sys, "argv", ["titan-full-live", command]
        ), mock.patch.object(
            sys, "path", list(sys.path)
        ), mock.patch.object(
            sys, "dont_write_bytecode", sys.dont_write_bytecode
        ), mock.patch.dict(
            os.environ, {}, clear=False
        ), mock.patch(
            "titan_brain.live.release.load_release_manifest",
            return_value=manifest,
        ) as load_manifest, mock.patch.object(
            PolicyBundle, "load", return_value=policy
        ) as load_policy, mock.patch(
            "titan_brain.live.provider_profile.IbkrLocalProviderProfile.from_config",
            return_value=profile,
        ) as load_profile, mock.patch(
            "titan_brain.live.provider_profile.activate_installed_sdk"
        ) as activate_sdk, mock.patch(
            "titan_brain.live.local_assembly.LocalProviderAssembly",
            return_value=assembly,
        ) as assembly_factory, mock.patch(
            "titan_brain.live.cli.main", return_value=0
        ) as cli_main:
            result = main()
        return SimpleNamespace(
            result=result,
            install_root=install_root,
            policy=policy,
            profile=profile,
            normal=normal,
            notification=notification,
            control=control,
            coordinator=coordinator,
            assembly=assembly,
            load_manifest=load_manifest,
            load_policy=load_policy,
            load_profile=load_profile,
            activate_sdk=activate_sdk,
            assembly_factory=assembly_factory,
            cli_main=cli_main,
        )

    def test_notification_commands_bypass_broker_profile_and_sdk_activation(self) -> None:
        for command in ("notification-test", "notification-worker"):
            with self.subTest(command=command):
                observed = self.run_installed_launcher(command)
                self.assertEqual(observed.result, 0)
                observed.load_manifest.assert_called_once_with(
                    observed.install_root / "release-manifest.json",
                    verify_files_root=ROOT,
                )
                observed.load_policy.assert_called_once_with(
                    ROOT,
                    config_relative="config/full_live_ibkr.json",
                )
                observed.load_profile.assert_not_called()
                observed.activate_sdk.assert_not_called()
                observed.assembly_factory.assert_called_once_with(
                    release_root=ROOT,
                    install_root=observed.install_root,
                    full_live_config_name="full_live_ibkr.json",
                    ibkr_command_inputs=None,
                )
                self.assertIs(
                    observed.cli_main.call_args.kwargs["runtime_composition"],
                    observed.notification,
                )
                observed.assembly.close.assert_called_once_with()

    def test_autonomous_commands_still_activate_exact_installed_sdk(self) -> None:
        for command in (
            "activate",
            "doctor",
            "prepare-activation",
            "readiness",
            "serve",
        ):
            with self.subTest(command=command):
                observed = self.run_installed_launcher(command)
                observed.load_manifest.assert_called_once()
                self.assertGreaterEqual(observed.load_policy.call_count, 1)
                observed.load_profile.assert_called_once_with(observed.policy.config)
                observed.activate_sdk.assert_called_once_with(
                    observed.install_root, observed.profile
                )
                self.assertIs(
                    observed.cli_main.call_args.kwargs["runtime_composition"],
                    (
                        observed.coordinator
                        if command == "serve"
                        else observed.normal
                    ),
                )

    def test_commands_select_the_smallest_provider_composition(self) -> None:
        normal = object()
        notification = object()
        control = object()
        coordinator = object()
        assembly = SimpleNamespace(
            runtime_composition=normal,
            notification_runtime_composition=notification,
            control_runtime_composition=control,
            coordinator_runtime_composition=coordinator,
        )
        selector = self.namespace["_command_composition_factory"]
        for command in ("notification-test", "notification-worker"):
            with self.subTest(command=command):
                self.assertIs(selector(assembly, [command]), notification)
        for command in (
            "status",
            "pause-new-entries",
            "managed-closeout",
            "deactivate",
        ):
            with self.subTest(command=command):
                self.assertIs(selector(assembly, [command]), control)
        self.assertIs(selector(assembly, ["serve"]), coordinator)
        for command in ("readiness", "doctor", "prepare-activation", "activate"):
            with self.subTest(command=command):
                self.assertIs(selector(assembly, [command]), normal)

    def test_autonomous_inputs_are_limited_to_composition_commands(self) -> None:
        policy = SimpleNamespace(
            config={
                "execution": {
                    "broker_adapter": "supported_production_transport",
                    "execution_authority_mode": "unattended",
                }
            }
        )
        for command in (
            "serve",
            "readiness",
            "prepare-activation",
            "activate",
            "doctor",
        ):
            marker = object()
            with self.subTest(command=command):
                result, autonomous, attended = self.select(
                    command, policy, marker
                )
                self.assertIs(result, marker)
                autonomous.assert_called_once()
                self.assertIs(
                    autonomous.call_args.kwargs["connect_command_session"],
                    True,
                )
                self.assertIs(
                    autonomous.call_args.kwargs["authorize_command_writes"],
                    command == "serve",
                )
                attended.assert_not_called()

        selector = self.namespace["_release_bound_command_inputs"]
        for command in (
            "status",
            "provider-status",
            "notification-worker",
            "local-profile-status",
            "pause-new-entries",
            "managed-closeout",
            "deactivate",
        ):
            with self.subTest(command=command), mock.patch.object(
                PolicyBundle, "load"
            ) as load:
                self.assertIsNone(
                    selector(
                        release_root=ROOT,
                        install_root=ROOT,
                        full_live_config_name="full_live_ibkr.json",
                        release_manifest_hash="1" * 64,
                        arguments=[command],
                    )
                )
                load.assert_not_called()

    def test_attended_loader_and_source_checkout_behavior_are_unchanged(self) -> None:
        policy = SimpleNamespace(
            config={
                "execution": {
                    "broker_adapter": "supported_production_transport",
                    "execution_authority_mode": "attended_only",
                }
            }
        )
        marker = object()
        result, autonomous, attended = self.select(
            "attended-review", policy, marker
        )
        self.assertIs(result, marker)
        attended.assert_called_once()
        autonomous.assert_not_called()
        selector = self.namespace["_release_bound_command_inputs"]
        self.assertIsNone(
            selector(
                release_root=ROOT,
                install_root=None,
                full_live_config_name="full_live_ibkr.json",
                release_manifest_hash=None,
                arguments=["serve"],
            )
        )


class CapturingComposition(RuntimeComposition):
    def __init__(self, *, plan_sealer=None, profile_error=None) -> None:
        super().__init__(autonomous_plan_sealer=plan_sealer)
        self.broker = object()
        self.discovery_calls = []
        self.notification_calls = []
        self.asserted_profiles = []
        self.profile_error = profile_error

    def bind_release(self, manifest, *, release_root):
        return ()

    def broker_client(self, execution_config, *, account_masked):
        return self.broker

    def notification_sink(self, notification_config, *, local_jsonl_path):
        self.notification_calls.append((notification_config, local_jsonl_path))
        return SimpleNamespace(route=object())

    def build_discovery_executor(self, **kwargs):
        self.discovery_calls.append(kwargs)
        return object()

    def control_inbox(self, *args, **kwargs):
        return None

    def assert_runtime_profile(self, expected_hash):
        self.asserted_profiles.append(expected_hash)
        if self.profile_error is not None:
            raise self.profile_error


class ServeWiringTests(unittest.TestCase):
    def exercise(
        self,
        *,
        mode: str,
        notification_config=None,
        discovery_failure=None,
        activated_profile_hash=None,
    ):
        execution = {
            "broker_adapter": "supported_production_transport",
            "execution_authority_mode": mode,
            "local_mutation_interlock_enabled": True,
            "reconcile_interval_seconds": 2,
        }
        policy = SimpleNamespace(
            config={
                "execution": execution,
                "notifications": notification_config or {},
                "evidence": {"broker_snapshot_max_age_seconds": 5},
            },
            execution_authority_mode=mode,
            account_key="ibkr-live-ending-3103",
            account_last4="3103",
            config_hash="2" * 64,
            policy_hash="3" * 64,
            runtime_id="titan-full-live-test",
        )
        manifest = {"release_manifest_hash": "1" * 64}
        runtime = {
            "release_manifest_hash": "1" * 64,
            "config_hash": "2" * 64,
            "policy_hash": "3" * 64,
            "account_key": "ibkr-live-ending-3103",
            "authority_enabled": int(activated_profile_hash is not None),
        }
        store = mock.create_autospec(LiveStateStore, instance=True)
        store.runtime_status.return_value = runtime
        store.acquire_writer_lease.return_value = 1
        # This wiring fixture represents readable state with no interrupted
        # risk observation, not an unconfigured MagicMock query result.
        store.rows.return_value = [{"n": 0}]
        layout = SimpleNamespace(
            release_root=ROOT,
            notification_path=ROOT / "notifications.jsonl",
            control_path=ROOT / "control",
            load_release=lambda: (manifest, policy),
        )
        sealer = mock.Mock(name="autonomous-plan-sealer") if mode == "unattended" else None
        if sealer is not None:
            sealer.release_components = mock.Mock(return_value=())
        composition = CapturingComposition(plan_sealer=sealer)
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        lock_root = Path(temporary.name).resolve() / "locks"
        exact_lock = AccountWriterLock(
            lock_root, "ibkr-live-ending-3103"
        )
        discovery_factory = mock.Mock(
            side_effect=discovery_failure,
            return_value=(None if discovery_failure is not None else composition),
        )
        gmail_binding = mock.Mock(
            side_effect=AssertionError("serve constructed Gmail provider credentials")
        )
        assembly = SimpleNamespace(
            service_writer_lock=lambda: exact_lock if mode == "unattended" else None,
            autonomous_plan_sealer=lambda: sealer,
            discovery_runtime_composition=discovery_factory,
            gmail_binding=gmail_binding,
        )
        args = SimpleNamespace(
            install_root=ROOT,
            once=True,
            runtime_composition=composition,
            provider_assembly=assembly,
        )
        lifecycle = SimpleNamespace(discovery=None)
        lifecycle_factory = mock.Mock(return_value=lifecycle)
        runner = mock.Mock()
        runner.run.return_value = None
        runner_factory = mock.Mock(return_value=runner)
        fallback_lock = AccountWriterLock(
            lock_root, "attended-read-coordinator::ibkr-live-ending-3103"
        )
        activated_record = (
            None
            if activated_profile_hash is None
            else SimpleNamespace(
                readiness_evidence=SimpleNamespace(
                    coordinator_component_provenance_hash=activated_profile_hash,
                    risk_high_water_lineage_hash="a" * 64,
                    risk_high_water_peak_equity="1000",
                )
            )
        )
        service_factory = mock.Mock(return_value=object())
        with mock.patch.object(cli, "InstallLayout", return_value=layout), mock.patch.object(
            cli, "_open_state", return_value=store
        ), mock.patch.object(
            cli, "ProductionLifecycleActions", lifecycle_factory
        ), mock.patch.object(
            cli, "FullLiveService", service_factory
        ), mock.patch.object(
            cli, "ServiceRunner", runner_factory
        ), mock.patch.object(
            cli, "_service_process_lock", return_value=fallback_lock
        ) as fallback, mock.patch.object(
            cli, "build_enqueue_only_outbox", return_value=object()
        ), mock.patch.object(
            cli, "_activated_runtime_record", return_value=activated_record
        ), mock.patch.object(
            cli,
            "_ActivationRiskBoundBroker",
            side_effect=lambda broker, _readiness: broker,
        ):
            self.assertEqual(cli.command_serve(args), 0)
        return {
            "lock": exact_lock if mode == "unattended" else fallback_lock,
            "sealer": sealer,
            "lifecycle_factory": lifecycle_factory,
            "runner_factory": runner_factory,
            "composition": composition,
            "discovery_factory": discovery_factory,
            "gmail_binding": gmail_binding,
            "service_factory": service_factory,
            "fallback": fallback,
            "store": store,
        }

    def test_autonomous_serve_uses_exact_assembly_lock_and_sealer_everywhere(self) -> None:
        result = self.exercise(mode="unattended")
        result["fallback"].assert_not_called()
        lifecycle_kwargs = result["lifecycle_factory"].call_args.kwargs
        discovery_kwargs = result["composition"].discovery_calls[0]
        runner_kwargs = result["runner_factory"].call_args.kwargs
        self.assertIs(lifecycle_kwargs["writer_lock"], result["lock"])
        self.assertIs(discovery_kwargs["writer_lock"], result["lock"])
        self.assertIs(runner_kwargs["lock"], result["lock"])
        self.assertIs(runner_kwargs["writer_authority"].state, result["store"])
        self.assertIs(runner_kwargs["writer_authority"].lock, result["lock"])
        self.assertEqual(runner_kwargs["writer_authority"].generation, 1)
        self.assertIs(lifecycle_kwargs["plan_sealer"], result["sealer"])
        self.assertIs(discovery_kwargs["plan_sealer"], result["sealer"])
        self.assertIs(
            result["composition"].autonomous_plan_sealer, result["sealer"]
        )
        result["store"].close.assert_called_once()

    def test_attended_serve_keeps_fallback_lock_and_no_sealer(self) -> None:
        result = self.exercise(mode="attended_only")
        result["fallback"].assert_called_once()
        lifecycle_kwargs = result["lifecycle_factory"].call_args.kwargs
        discovery_kwargs = result["composition"].discovery_calls[0]
        self.assertIs(lifecycle_kwargs["writer_lock"], result["lock"])
        self.assertIsNone(lifecycle_kwargs["plan_sealer"])
        self.assertIsNone(discovery_kwargs["plan_sealer"])
        runner_kwargs = result["runner_factory"].call_args.kwargs
        self.assertIs(runner_kwargs["writer_authority"].state, result["store"])
        self.assertIs(runner_kwargs["writer_authority"].lock, result["lock"])

    def test_gmail_keychain_failure_cannot_block_coordinator_start(self) -> None:
        route = NotificationRoute(
            provider="gmail",
            destination_fingerprint=destination_fingerprint(
                "gmail", "owner@example.invalid"
            ),
            route_version="gmail-v1",
            required_assurance=DeliveryAssurance.PROVIDER_ACCEPTED,
        )
        config = {
            "delivery_sink": "gmail_api",
            "destination_bridge_configured": True,
            "provider": route.provider,
            "destination_fingerprint": route.destination_fingerprint,
            "route_version": route.route_version,
            "required_assurance": route.required_assurance.value,
        }

        result = self.exercise(
            mode="unattended", notification_config=config
        )

        result["gmail_binding"].assert_not_called()
        self.assertEqual(result["composition"].notification_calls, [])
        self.assertEqual(
            result["service_factory"].call_args.kwargs["notification_route"],
            route,
        )
        result["runner_factory"].return_value.run.assert_called_once_with(once=True)

    def test_massive_keychain_failure_only_blocks_entry_path(self) -> None:
        failure = CredentialUnavailable("CREDENTIAL_KEYCHAIN_ITEM_UNAVAILABLE")

        result = self.exercise(
            mode="unattended", discovery_failure=failure
        )

        self.assertIsNone(result["lifecycle_factory"].return_value.discovery)
        self.assertEqual(
            result["service_factory"].call_args.kwargs["entry_path_blockers"],
            ["DISCOVERY_COMPOSITION_UNAVAILABLE:CredentialUnavailable"],
        )
        result["runner_factory"].return_value.run.assert_called_once_with(once=True)
        self.assertNotIn(
            failure.code,
            repr(result["service_factory"].call_args.kwargs),
        )

    def test_activated_serve_asserts_exact_coordinator_profile(self) -> None:
        profile_hash = "e" * 64

        result = self.exercise(
            mode="unattended", activated_profile_hash=profile_hash
        )

        self.assertEqual(result["composition"].asserted_profiles, [profile_hash])
        result["runner_factory"].return_value.run.assert_called_once_with(once=True)


if __name__ == "__main__":
    unittest.main()
