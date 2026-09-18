"""Focused tests for the private owner-policy and pricing receipt."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import hmac
import json
from pathlib import Path
import tempfile
import unittest

from titan_brain.live.ibkr_autonomous_policy_receipt import (
    IBKR_AUTONOMOUS_MAX_PRICING_VERIFICATION_AGE,
    IBKR_AUTONOMOUS_POLICY_RECEIPT_SCHEMA,
    IbkrAutonomousPolicyReceiptBindings,
    IbkrAutonomousPolicyReceiptError,
    VerifiedIbkrAutonomousPolicyReceipt,
    load_verified_ibkr_autonomous_policy_receipt,
)
from titan_brain.live.policy import canonical_json


NOW = datetime(2026, 9, 14, 15, 0, tzinfo=timezone.utc)
SECRET = b"owner-policy-pricing-receipt-test-secret-material-v1"


def bindings() -> IbkrAutonomousPolicyReceiptBindings:
    return IbkrAutonomousPolicyReceiptBindings(
        release_manifest_hash="1" * 64,
        config_hash="2" * 64,
        policy_hash="3" * 64,
        risk_hash="4" * 64,
        account_key="ibkr-live-ending-3103",
        account_masked="****3103",
        account_binding_fingerprint="5" * 64,
        authorization_binding_id="6" * 64,
        provider_contract_id="7" * 64,
        transport_id="ibkr-tws-api-10.50.2-v1",
        max_spread_bps=Decimal("25.0"),
        spread_denominator="executable_nbbo_midpoint",
        minimum_depth_multiple=Decimal("5.0"),
        depth_source="fresh_executable_side_top_of_book",
        quote_size_unit="shares",
        target_exit_mode="first_target_completed_minute_full_exit",
        target_index=0,
        target_trigger=(
            "fresh_aligned_completed_one_minute_close_at_or_above_target"
        ),
        target_quantity="full_broker_confirmed_sellable_position",
        cancel_working_sells_before_exit=True,
        require_strictly_newer_cancel_evidence=True,
        deadline_feasibility_gate=True,
        minimum_commission_reserve_per_order_dollars=Decimal("1.00"),
    )


def receipt_body() -> dict[str, object]:
    expected = bindings()
    return {
        "schema_version": IBKR_AUTONOMOUS_POLICY_RECEIPT_SCHEMA,
        "issued_at": (NOW - timedelta(minutes=1)).isoformat(),
        "expires_at": (NOW + timedelta(hours=8)).isoformat(),
        "bindings": {
            "release_manifest_hash": expected.release_manifest_hash,
            "config_hash": expected.config_hash,
            "policy_hash": expected.policy_hash,
            "risk_hash": expected.risk_hash,
            "account_key": expected.account_key,
            "account_masked": expected.account_masked,
            "account_binding_fingerprint": (
                expected.account_binding_fingerprint
            ),
            "authorization_binding_id": expected.authorization_binding_id,
            "provider_contract_id": expected.provider_contract_id,
            "transport_id": expected.transport_id,
        },
        "owner_approval": {
            "status": "owner_approved",
            "approved_at": (NOW - timedelta(minutes=3)).isoformat(),
            "reference": "OWNER-POLICY-APPROVAL-TEST-001",
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
        "target_exit_policy": {
            "target_exit_mode": "first_target_completed_minute_full_exit",
            "target_index": 0,
            "target_trigger": (
                "fresh_aligned_completed_one_minute_close_at_or_above_target"
            ),
            "quantity": "full_broker_confirmed_sellable_position",
            "cancel_working_sells_before_exit": True,
            "require_strictly_newer_cancel_evidence": True,
            "deadline_feasibility_gate": True,
        },
        "effective_pricing": {
            "status": "provider_account_verified",
            "verified_at": (NOW - timedelta(minutes=2)).isoformat(),
            "provider": "interactive_brokers",
            "source_kind": (
                "authenticated_ibkr_account_effective_pricing_receipt"
            ),
            "source_reference": "IBKR-ACCOUNT-PRICING-TEST-001",
            "source_receipt_sha256": "8" * 64,
            "currency": "USD",
            "routing_scope": "smart_routed_us_stock_orders",
            "fee_scope": "all_in_commission_and_regulatory_fees_per_order",
            "all_in_commission_floor_dollars": "1.00",
            "floor_conservative_for_allowed_order_scope": True,
        },
    }


class PolicyReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name).resolve() / "policy-pricing.json"

    def write(
        self,
        body: dict[str, object],
        *,
        secret: bytes = SECRET,
        canonical: bool = True,
    ) -> None:
        payload = deepcopy(body)
        payload["hmac_sha256"] = hmac.new(
            secret,
            canonical_json(body).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        encoded = (
            canonical_json(payload) + "\n"
            if canonical
            else json.dumps(payload, indent=2) + "\n"
        )
        self.path.write_text(encoded, encoding="utf-8")
        self.path.chmod(0o600)

    def assert_code(self, code: str, callback) -> None:
        with self.assertRaises(IbkrAutonomousPolicyReceiptError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)

    def load(self, *, now: datetime = NOW):
        return load_verified_ibkr_autonomous_policy_receipt(
            self.path,
            secret=SECRET,
            expected=bindings(),
            now=now,
        )

    def test_authenticates_exact_release_policy_risk_quality_target_and_pricing(self) -> None:
        self.write(receipt_body())
        verified = self.load()
        self.assertEqual(verified.bindings, bindings())
        self.assertEqual(
            verified.bindings.minimum_commission_reserve_per_order_dollars,
            Decimal("1.00"),
        )
        self.assertEqual(verified.pricing_source_receipt_sha256, "8" * 64)
        self.assertEqual(len(verified.receipt_hash), 64)
        self.assertIsNone(verified.assert_current(NOW, bindings()))

    def test_config_floor_or_exact_policy_change_cannot_reuse_receipt(self) -> None:
        self.write(receipt_body())
        expected = bindings()
        changed_floor = IbkrAutonomousPolicyReceiptBindings(
            **{
                **expected.__dict__,
                "minimum_commission_reserve_per_order_dollars": Decimal("1.01"),
            }
        )
        self.assert_code(
            "IBKR_AUTONOMOUS_POLICY_RECEIPT_BINDING_MISMATCH",
            lambda: load_verified_ibkr_autonomous_policy_receipt(
                self.path,
                secret=SECRET,
                expected=changed_floor,
                now=NOW,
            ),
        )
        changed = receipt_body()
        changed["quality_policy"]["minimum_depth_multiple"] = "4.0"
        self.write(changed)
        self.assert_code(
            "IBKR_AUTONOMOUS_POLICY_RECEIPT_BINDING_MISMATCH",
            self.load,
        )

    def test_public_price_page_or_base_commission_is_not_effective_pricing(self) -> None:
        for field, value in (
            ("source_kind", "public_pricing_page"),
            ("fee_scope", "base_commission_only"),
            ("status", "owner_assumed"),
            ("floor_conservative_for_allowed_order_scope", False),
        ):
            with self.subTest(field=field):
                body = receipt_body()
                body["effective_pricing"][field] = value
                self.write(body)
                self.assert_code(
                    "IBKR_AUTONOMOUS_POLICY_RECEIPT_EFFECTIVE_PRICING_UNACCEPTED",
                    self.load,
                )

    def test_pricing_freshness_and_receipt_expiry_are_independent(self) -> None:
        body = receipt_body()
        stale_at = NOW - IBKR_AUTONOMOUS_MAX_PRICING_VERIFICATION_AGE
        body["effective_pricing"]["verified_at"] = (
            stale_at - timedelta(seconds=1)
        ).isoformat()
        body["issued_at"] = (NOW - timedelta(minutes=1)).isoformat()
        self.write(body)
        self.assert_code(
            "IBKR_AUTONOMOUS_POLICY_RECEIPT_PRICING_VERIFICATION_STALE",
            self.load,
        )

        body = receipt_body()
        body["expires_at"] = NOW.isoformat()
        self.write(body)
        self.assert_code(
            "IBKR_AUTONOMOUS_POLICY_RECEIPT_RECEIPT_EXPIRED",
            self.load,
        )

    def test_hmac_canonicality_and_private_file_mode_are_mandatory(self) -> None:
        self.write(receipt_body(), secret=b"x" * 40)
        self.assert_code(
            "IBKR_AUTONOMOUS_POLICY_RECEIPT_HMAC_INVALID",
            self.load,
        )
        self.write(receipt_body(), canonical=False)
        self.assert_code(
            "IBKR_AUTONOMOUS_POLICY_RECEIPT_FILE_NOT_CANONICAL",
            self.load,
        )
        self.write(receipt_body())
        self.path.chmod(0o644)
        self.assert_code(
            "IBKR_AUTONOMOUS_POLICY_RECEIPT_FILE_UNSAFE",
            self.load,
        )

    def test_verified_object_cannot_be_constructed_from_config_values(self) -> None:
        with self.assertRaises(IbkrAutonomousPolicyReceiptError):
            VerifiedIbkrAutonomousPolicyReceipt(
                bindings=bindings(),
                issued_at=NOW,
                expires_at=NOW + timedelta(hours=1),
                owner_approved_at=NOW,
                owner_approval_reference="OWNER-POLICY-APPROVAL-TEST-001",
                pricing_verified_at=NOW,
                pricing_source_reference="IBKR-ACCOUNT-PRICING-TEST-001",
                pricing_source_receipt_sha256="8" * 64,
                receipt_hash="9" * 64,
            )


if __name__ == "__main__":
    unittest.main()
