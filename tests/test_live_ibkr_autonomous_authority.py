"""Hermetic tests for the autonomous IBKR provider-authority boundary."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from titan_brain.live.ibkr_autonomous_authority import (
    IBKR_AUTONOMOUS_AUTHORITY_SCHEMA,
    IBKR_AUTONOMOUS_MAX_AUTHORITY_TTL,
    IBKR_AUTONOMOUS_MAX_PROVIDER_CONFIRMATION_AGE,
    IBKR_AUTONOMOUS_MAX_READ_ONLY_VERIFICATION_AGE,
    IbkrAutonomousAuthorityBindings,
    IbkrAutonomousAuthorityError,
    load_verified_ibkr_autonomous_authority,
)
from titan_brain.live.broker.base import (
    ClientRefRecoverySource,
    MarketHours,
    OrderCoverageContract,
    OrderFamily,
    OrderFamilyCoverage,
    OrderFamilyCoverageStatus,
)
from titan_brain.live.broker.ibkr_transport import autonomous_ibkr_descriptor
from titan_brain.live.policy import canonical_json


NOW = datetime(2026, 9, 14, 15, 0, tzinfo=timezone.utc)
SECRET = b"autonomous-authority-test-secret-32-bytes-minimum"


def bindings() -> IbkrAutonomousAuthorityBindings:
    return IbkrAutonomousAuthorityBindings(
        release_manifest_hash="1" * 64,
        config_hash="2" * 64,
        policy_binding_id="3" * 64,
        account_key="ibkr-live-ending-3103",
        account_masked="****3103",
        account_binding_fingerprint="4" * 64,
        authorization_binding_id="5" * 64,
        provider_contract_id="6" * 64,
        transport_id="ibkr-tws-api-10.50.2-v1",
        api_name="official_tws_python_api",
        api_version="10.50.2",
        environment="live",
        client_id=19736,
    )


def authority_body() -> dict[str, object]:
    expected = bindings()
    return {
        "schema_version": IBKR_AUTONOMOUS_AUTHORITY_SCHEMA,
        "issued_at": (NOW - timedelta(minutes=1)).isoformat(),
        "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
        "bindings": {
            "release_manifest_hash": expected.release_manifest_hash,
            "config_hash": expected.config_hash,
            "policy_binding_id": expected.policy_binding_id,
            "account_key": expected.account_key,
            "account_masked": expected.account_masked,
            "account_binding_fingerprint": expected.account_binding_fingerprint,
            "authorization_binding_id": expected.authorization_binding_id,
            "provider_contract_id": expected.provider_contract_id,
            "transport_id": expected.transport_id,
            "api_name": expected.api_name,
            "api_version": expected.api_version,
            "environment": expected.environment,
            "client_id": expected.client_id,
        },
        "support": {
            "reference": "IBKR-SUPPORT-CASE-12345",
            "status": "provider_confirmed_supported",
            "confirmed_at": (NOW - timedelta(minutes=2)).isoformat(),
            "scope": "unattended_regular_hours_api_orders_with_external_market_data",
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


class IbkrAutonomousAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.path = self.root / "ibkr-autonomous-authority.json"

    def write(
        self,
        body: dict[str, object] | None = None,
        *,
        secret: bytes = SECRET,
        mode: int = 0o600,
        canonical: bool = True,
    ) -> dict[str, object]:
        payload = deepcopy(body if body is not None else authority_body())
        payload["hmac_sha256"] = hmac.new(
            secret,
            canonical_json(payload).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if canonical:
            encoded = canonical_json(payload) + "\n"
        else:
            encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        self.path.write_text(encoded, encoding="utf-8")
        self.path.chmod(mode)
        return payload

    def load(self, *, now: datetime = NOW, expected=None):
        return load_verified_ibkr_autonomous_authority(
            self.path,
            secret=SECRET,
            expected=expected or bindings(),
            now=now,
        )

    @staticmethod
    def assert_current(verified, now: datetime) -> None:
        expected = bindings()
        verified.assert_current(
            now,
            expected.release_manifest_hash,
            expected.config_hash,
            expected.policy_binding_id,
            expected.account_masked,
            expected.account_binding_fingerprint,
            expected.authorization_binding_id,
            expected.provider_contract_id,
            expected.transport_id,
            expected.environment,
            expected.client_id,
            expected_account_key=expected.account_key,
            expected_api_name=expected.api_name,
            expected_api_version=expected.api_version,
        )

    def test_verified_object_is_immutable_exact_and_current(self) -> None:
        self.write()
        verified = self.load()
        self.assertEqual(verified.transport_id, "ibkr-tws-api-10.50.2-v1")
        self.assertEqual(verified.account_masked, "****3103")
        self.assertEqual(verified.account_binding_fingerprint, "4" * 64)
        self.assertEqual(verified.authorization_binding_id, "5" * 64)
        self.assertEqual(verified.provider_contract_id, "6" * 64)
        self.assertEqual(verified.policy_binding_id, "3" * 64)
        self.assertEqual(verified.environment, "live")
        self.assertEqual(verified.client_id, 19736)
        self.assertEqual(verified.release_manifest_hash, "1" * 64)
        self.assertEqual(verified.config_hash, "2" * 64)
        with self.assertRaises(FrozenInstanceError):
            verified.client_id = 0
        expected = bindings()
        verified.assert_current(
            NOW,
            expected.release_manifest_hash,
            expected.config_hash,
            expected.policy_binding_id,
            expected.account_masked,
            expected.account_binding_fingerprint,
            expected.authorization_binding_id,
            expected.provider_contract_id,
            expected.transport_id,
            expected.environment,
            expected.client_id,
            expected_account_key=expected.account_key,
            expected_api_name=expected.api_name,
            expected_api_version=expected.api_version,
        )
        with self.assertRaisesRegex(
            IbkrAutonomousAuthorityError, "UNVERIFIED_OBJECT_CONSTRUCTION"
        ):
            type(verified)(
                **{
                    name: getattr(verified, name)
                    for name in verified.__dataclass_fields__
                    if name != "_verification_seal"
                }
            )

    def test_verified_contract_is_required_to_describe_autonomous_writes(self) -> None:
        self.write()
        verified = self.load()
        expected = bindings()
        coverage = OrderCoverageContract(
            contract_version="ibkr-authority-test-v1",
            evidence_observed_at=NOW,
            families=tuple(
                OrderFamilyCoverage(
                    family=family,
                    status=OrderFamilyCoverageStatus.COMPLETE_GENERAL,
                    evidence_id=f"authority-test-{family.value}",
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
        descriptor = autonomous_ibkr_descriptor(
            exact_account_id="U1233103",
            account_masked=expected.account_masked,
            account_binding_fingerprint=expected.account_binding_fingerprint,
            authorization_binding_id=expected.authorization_binding_id,
            coverage=coverage,
            authority=verified,
            authority_bindings=expected,
            now=NOW,
        )
        capabilities = descriptor.capabilities
        self.assertTrue(capabilities.supports_unattended_writes)
        self.assertFalse(capabilities.review_requires_explicit_confirmation)
        self.assertFalse(capabilities.cancel_requires_explicit_confirmation)
        self.assertEqual(capabilities.supported_market_hours, (MarketHours.REGULAR,))
        with self.assertRaisesRegex(Exception, "authority"):
            autonomous_ibkr_descriptor(
                exact_account_id="U1233103",
                account_masked=expected.account_masked,
                account_binding_fingerprint=expected.account_binding_fingerprint,
                authorization_binding_id=expected.authorization_binding_id,
                coverage=coverage,
                authority=object(),
                authority_bindings=expected,
                now=NOW,
            )

    def test_tamper_and_wrong_hmac_key_fail(self) -> None:
        payload = self.write()
        payload["execution"]["unattended_cancel_supported"] = False
        self.path.write_text(canonical_json(payload) + "\n", encoding="utf-8")
        self.path.chmod(0o600)
        with self.assertRaisesRegex(IbkrAutonomousAuthorityError, "HMAC_INVALID"):
            self.load()
        self.write()
        with self.assertRaisesRegex(IbkrAutonomousAuthorityError, "HMAC_INVALID"):
            load_verified_ibkr_autonomous_authority(
                self.path,
                secret=b"another-valid-secret-that-is-at-least-32-bytes",
                expected=bindings(),
                now=NOW,
            )

    def test_expiry_and_future_issue_fail_on_load_and_assert(self) -> None:
        self.write()
        verified = self.load()
        with self.assertRaisesRegex(IbkrAutonomousAuthorityError, "AUTHORITY_EXPIRED"):
            verified.assert_current(
                NOW + timedelta(minutes=6),
                "1" * 64,
                "2" * 64,
                "3" * 64,
                "****3103",
                "4" * 64,
                "5" * 64,
                "6" * 64,
                "ibkr-tws-api-10.50.2-v1",
                "live",
                19736,
            )
        with self.assertRaisesRegex(IbkrAutonomousAuthorityError, "AUTHORITY_EXPIRED"):
            self.load(now=NOW + timedelta(minutes=6))
        body = authority_body()
        body["issued_at"] = (NOW + timedelta(minutes=1)).isoformat()
        body["expires_at"] = (NOW + timedelta(minutes=2)).isoformat()
        self.write(body)
        with self.assertRaisesRegex(IbkrAutonomousAuthorityError, "NOT_YET_CURRENT"):
            self.load()

    def test_authority_ttl_hard_maximum_accepts_boundary_and_rejects_over(self) -> None:
        body = authority_body()
        issued = NOW - timedelta(minutes=1)
        body["issued_at"] = issued.isoformat()
        body["expires_at"] = (
            issued + IBKR_AUTONOMOUS_MAX_AUTHORITY_TTL
        ).isoformat()
        self.write(body)
        verified = self.load()
        self.assert_current(verified, NOW)

        body["expires_at"] = (
            issued + IBKR_AUTONOMOUS_MAX_AUTHORITY_TTL + timedelta(microseconds=1)
        ).isoformat()
        self.write(body)
        with self.assertRaisesRegex(IbkrAutonomousAuthorityError, "TTL_EXCEEDED"):
            self.load()

    def test_provider_confirmation_freshness_is_rechecked_at_every_use(self) -> None:
        body = authority_body()
        issued = NOW - timedelta(minutes=1)
        body["issued_at"] = issued.isoformat()
        body["expires_at"] = (
            issued + IBKR_AUTONOMOUS_MAX_AUTHORITY_TTL
        ).isoformat()
        body["support"]["confirmed_at"] = (
            NOW - IBKR_AUTONOMOUS_MAX_PROVIDER_CONFIRMATION_AGE
        ).isoformat()
        self.write(body)
        verified = self.load()
        self.assert_current(verified, NOW)
        with self.assertRaisesRegex(
            IbkrAutonomousAuthorityError, "PROVIDER_CONFIRMATION_STALE"
        ):
            self.assert_current(verified, NOW + timedelta(microseconds=1))

        body["support"]["confirmed_at"] = (
            NOW
            - IBKR_AUTONOMOUS_MAX_PROVIDER_CONFIRMATION_AGE
            - timedelta(microseconds=1)
        ).isoformat()
        self.write(body)
        with self.assertRaisesRegex(
            IbkrAutonomousAuthorityError, "PROVIDER_CONFIRMATION_STALE"
        ):
            self.load()

    def test_read_only_verification_freshness_is_rechecked_at_every_use(self) -> None:
        body = authority_body()
        issued = NOW - timedelta(minutes=1)
        body["issued_at"] = issued.isoformat()
        body["expires_at"] = (
            issued + IBKR_AUTONOMOUS_MAX_AUTHORITY_TTL
        ).isoformat()
        body["account_controls"]["read_only_api_verified_at"] = (
            NOW - IBKR_AUTONOMOUS_MAX_READ_ONLY_VERIFICATION_AGE
        ).isoformat()
        self.write(body)
        verified = self.load()
        self.assert_current(verified, NOW)
        with self.assertRaisesRegex(
            IbkrAutonomousAuthorityError, "READ_ONLY_VERIFICATION_STALE"
        ):
            self.assert_current(verified, NOW + timedelta(microseconds=1))

        body["account_controls"]["read_only_api_verified_at"] = (
            NOW
            - IBKR_AUTONOMOUS_MAX_READ_ONLY_VERIFICATION_AGE
            - timedelta(microseconds=1)
        ).isoformat()
        self.write(body)
        with self.assertRaisesRegex(
            IbkrAutonomousAuthorityError, "READ_ONLY_VERIFICATION_STALE"
        ):
            self.load()

    def test_clock_skew_boundary_is_exact_and_future_overage_is_rejected(self) -> None:
        body = authority_body()
        body["issued_at"] = (NOW + timedelta(seconds=2)).isoformat()
        body["expires_at"] = (NOW + timedelta(minutes=5)).isoformat()
        body["support"]["confirmed_at"] = (NOW + timedelta(seconds=2)).isoformat()
        body["account_controls"]["read_only_api_verified_at"] = (
            NOW + timedelta(seconds=2)
        ).isoformat()
        self.write(body)
        self.assert_current(self.load(), NOW)

        body["issued_at"] = (
            NOW + timedelta(seconds=2, microseconds=1)
        ).isoformat()
        body["support"]["confirmed_at"] = body["issued_at"]
        body["account_controls"]["read_only_api_verified_at"] = body["issued_at"]
        self.write(body)
        with self.assertRaisesRegex(IbkrAutonomousAuthorityError, "NOT_YET_CURRENT"):
            self.load()

    def test_wrong_release_account_transport_api_and_client_bindings_fail(self) -> None:
        self.write()
        base = bindings()
        changes = {
            "release_manifest_hash": "a" * 64,
            "config_hash": "b" * 64,
            "policy_binding_id": "c" * 64,
            "account_masked": "****9999",
            "account_binding_fingerprint": "d" * 64,
            "authorization_binding_id": "e" * 64,
            "provider_contract_id": "f" * 64,
            "transport_id": "ibkr-other-transport-v1",
            "api_version": "10.50.1",
            "environment": "paper",
            "client_id": 19737,
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                values = dict(base.__dict__)
                values[field] = value
                if field in {"account_masked", "environment"}:
                    # Invalid expected bindings are themselves rejected; they
                    # cannot be used to weaken the authenticated comparison.
                    with self.assertRaises(IbkrAutonomousAuthorityError):
                        IbkrAutonomousAuthorityBindings(**values)
                else:
                    with self.assertRaisesRegex(
                        IbkrAutonomousAuthorityError, "BINDING_MISMATCH"
                    ):
                        self.load(expected=IbkrAutonomousAuthorityBindings(**values))

    def test_resigned_raw_boolean_values_never_count_as_capabilities(self) -> None:
        cases = (
            ("account_controls", "read_only_api_enabled", 0),
            ("execution", "unattended_place_supported", 1),
            ("execution", "unattended_cancel_supported", "true"),
            ("execution", "per_order_confirmation_required", 0),
            ("scope", "margin_debit_allowed", 0),
            ("scope", "extended_hours_orders_allowed", "false"),
            ("protection", "broker_working_evidence_required", 1),
            ("precautions", "order_constraint_override_allowed", 0),
        )
        for group, field, value in cases:
            with self.subTest(group=group, field=field, value=value):
                body = authority_body()
                body[group][field] = value
                self.write(body)
                with self.assertRaisesRegex(
                    IbkrAutonomousAuthorityError, "UNACCEPTED"
                ):
                    self.load()

    def test_missing_or_unconfirmed_support_reference_fails_even_when_resigned(self) -> None:
        body = authority_body()
        body["support"].pop("reference")
        self.write(body)
        with self.assertRaisesRegex(IbkrAutonomousAuthorityError, "SUPPORT_REFERENCE_REQUIRED"):
            self.load()
        body = authority_body()
        body["support"]["status"] = "inquiry_sent"
        self.write(body)
        with self.assertRaisesRegex(IbkrAutonomousAuthorityError, "SUPPORT_UNACCEPTED"):
            self.load()

    def test_scope_unknown_retry_protection_and_precautions_are_exact(self) -> None:
        cases = (
            ("scope", "options_allowed", True),
            ("scope", "shorting_allowed", True),
            ("scope", "fractional_allowed", True),
            ("scope", "overnight_allowed", True),
            ("scope", "allowed_market_hours", ["regular_hours", "extended_hours"]),
            ("execution", "automatic_unknown_retry_allowed", True),
            ("execution", "per_order_confirmation_required", True),
            ("protection", "mode", "atomic_bracket"),
            ("protection", "atomic_protection_claimed", True),
            ("precautions", "bypassed_precautions", ["price_percentage_constraint"]),
            ("precautions", "broker_order_precautions", "disabled"),
        )
        for group, field, value in cases:
            with self.subTest(group=group, field=field):
                body = authority_body()
                body[group][field] = value
                self.write(body)
                with self.assertRaisesRegex(
                    IbkrAutonomousAuthorityError, "UNACCEPTED"
                ):
                    self.load()

    def test_unsafe_permissions_symlink_noncanonical_and_short_secret_fail(self) -> None:
        self.write(mode=0o644)
        with self.assertRaisesRegex(IbkrAutonomousAuthorityError, "FILE_UNSAFE"):
            self.load()
        self.write(canonical=False)
        with self.assertRaisesRegex(IbkrAutonomousAuthorityError, "NOT_CANONICAL"):
            self.load()
        target = self.root / "real-authority.json"
        os.replace(self.path, target)
        self.path.symlink_to(target)
        with self.assertRaisesRegex(IbkrAutonomousAuthorityError, "PATH_UNSAFE"):
            self.load()
        self.path.unlink()
        os.replace(target, self.path)
        with self.assertRaisesRegex(IbkrAutonomousAuthorityError, "HMAC_KEY_INVALID"):
            load_verified_ibkr_autonomous_authority(
                self.path,
                secret=b"short",
                expected=bindings(),
                now=NOW,
            )


if __name__ == "__main__":
    unittest.main()
