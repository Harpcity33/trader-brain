"""Hermetic tests for the autonomous wrapper over attended IBKR preflight."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import hmac
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from titan_brain.live.broker.base import (
    AttendedLocalReview,
    BrokerMutationBlocked,
    BrokerSide,
    EquityOrderType,
    LocalCancelDecision,
    LocalPreflightDecision,
    MarketHours,
    OrderCheck,
    OrderRequest,
    TimeInForce,
)
from titan_brain.live.broker.ibkr_orders import (
    IbkrContractIdentity,
    attended_confirmation_phrase,
    attended_order_preview,
)
from titan_brain.live.broker.ibkr_preflight import (
    IbkrAttendedPreflightBridge,
    IbkrAutonomousPolicyPreflightBridge,
)
from titan_brain.live.ibkr_autonomous_authority import (
    IBKR_AUTONOMOUS_AUTHORITY_SCHEMA,
    IbkrAutonomousAuthorityBindings,
    load_verified_ibkr_autonomous_authority,
)
from titan_brain.live.policy import canonical_json


NOW = datetime(2026, 9, 14, 15, 0, tzinfo=timezone.utc)
MASK = "****3103"
CLIENT_ID = 19736
SECRET = b"autonomous-preflight-test-secret-32-bytes-minimum"
ENTRY_REF = "00000000-0000-4000-8000-000000000091"
STOP_REF = "00000000-0000-4000-8000-000000000092"
CANCEL_REF = "00000000-0000-4000-8000-000000000093"


def authority_bindings(**changes: object) -> IbkrAutonomousAuthorityBindings:
    values: dict[str, object] = {
        "release_manifest_hash": "1" * 64,
        "config_hash": "2" * 64,
        "policy_binding_id": "3" * 64,
        "account_key": "ibkr-live-ending-3103",
        "account_masked": MASK,
        "account_binding_fingerprint": "4" * 64,
        "authorization_binding_id": "5" * 64,
        "provider_contract_id": "6" * 64,
        "transport_id": "ibkr-tws-api-10.50.2-v1",
        "api_name": "official_tws_python_api",
        "api_version": "10.50.2",
        "environment": "live",
        "client_id": CLIENT_ID,
    }
    values.update(changes)
    return IbkrAutonomousAuthorityBindings(**values)


def authority_body(
    expected: IbkrAutonomousAuthorityBindings,
    *,
    expires_at: datetime,
) -> dict[str, object]:
    return {
        "schema_version": IBKR_AUTONOMOUS_AUTHORITY_SCHEMA,
        "issued_at": (NOW - timedelta(minutes=1)).isoformat(),
        "expires_at": expires_at.isoformat(),
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
            "reference": "IBKR-SUPPORT-CASE-AUTONOMOUS-PREFLIGHT",
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


def order_request(
    *,
    client_ref_id: str = ENTRY_REF,
    side: BrokerSide = BrokerSide.BUY,
    order_type: EquityOrderType = EquityOrderType.LIMIT,
    time_in_force: TimeInForce = TimeInForce.GFD,
    limit_price: Decimal | None = Decimal("10.00"),
    stop_price: Decimal | None = None,
) -> OrderRequest:
    return OrderRequest(
        account_masked=MASK,
        symbol="TEST",
        side=side,
        order_type=order_type,
        quantity=3,
        market_hours=MarketHours.REGULAR,
        time_in_force=time_in_force,
        client_ref_id=client_ref_id,
        limit_price=limit_price,
        stop_price=stop_price,
    )


class FakeAttendedPreflight(IbkrAttendedPreflightBridge):
    """Narrow test delegate that still satisfies the concrete type boundary."""

    def __init__(self, clock: list[datetime]) -> None:
        self._account_masked = MASK
        self._command_client_id = CLIENT_ID
        self.policy_binding_id = "3" * 64
        self.provider_contract_id = "6" * 64
        self.clock = clock
        self.review_calls = 0
        self.protection_calls = 0
        self.contract_calls = 0
        self.revalidations: list[tuple[OrderRequest, AttendedLocalReview]] = []
        self.cancel_calls: list[tuple[str, int]] = []
        self.receipts: dict[str, AttendedLocalReview] = {}
        self.contract = IbkrContractIdentity(
            con_id=12345,
            symbol="TEST",
            primary_exchange="NASDAQ",
        )

    def current_time(self) -> datetime:
        return self.clock[0]

    def _receipt(self, request: OrderRequest) -> AttendedLocalReview:
        current = self.current_time()
        receipt = AttendedLocalReview(
            request=request,
            reviewed_at=current,
            received_at=current,
            expires_at=current + timedelta(seconds=20),
            disclosure="Attended local test receipt.",
            order_checks=(OrderCheck("TEST", "INFO", "Exact facts checked."),),
            required_confirmation_phrase=attended_confirmation_phrase(request),
            broker_review_id=None,
            broker_bound=False,
            preview=dict(attended_order_preview(request)),
            decision_id=str(uuid4()),
            policy_binding_id=self.policy_binding_id,
            evidence_collection_id="7" * 64,
            provider_contract_id=self.provider_contract_id,
        )
        self.receipts[request.client_ref_id] = receipt
        return receipt

    def review(self, request: OrderRequest) -> AttendedLocalReview:
        self.review_calls += 1
        return self._receipt(request)

    def review_protection(
        self,
        source_request: OrderRequest,
        source_plan_id: str,
        stop_template: OrderRequest,
        source_claimed_at: datetime,
    ) -> AttendedLocalReview:
        del source_request, source_plan_id, source_claimed_at
        self.protection_calls += 1
        return self._receipt(stop_template)

    def contract_for(self, request: OrderRequest) -> IbkrContractIdentity:
        self.contract_calls += 1
        if request.client_ref_id not in self.receipts:
            raise BrokerMutationBlocked("TEST_RECEIPT_MISSING")
        return self.contract

    def revalidate(
        self,
        request: OrderRequest,
        review: AttendedLocalReview,
    ) -> None:
        if self.receipts.get(request.client_ref_id) is not review:
            raise BrokerMutationBlocked("TEST_ORIGINAL_ATTENDED_RECEIPT_REQUIRED")
        self.revalidations.append((request, review))
        return None

    def cancel_evidence(self, client_ref_id: str, order_id: int) -> str:
        self.cancel_calls.append((client_ref_id, order_id))
        return hashlib.sha256(
            f"{client_ref_id}:{order_id}:{len(self.cancel_calls)}".encode("ascii")
        ).hexdigest()


class IbkrAutonomousPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.path = self.root / "authority.json"
        self.clock = [NOW]
        self.bindings = authority_bindings()
        body = authority_body(
            self.bindings,
            expires_at=NOW + timedelta(seconds=60),
        )
        payload = dict(body)
        payload["hmac_sha256"] = hmac.new(
            SECRET,
            canonical_json(body).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        self.path.write_text(canonical_json(payload) + "\n", encoding="utf-8")
        self.path.chmod(0o600)
        self.authority = load_verified_ibkr_autonomous_authority(
            self.path,
            secret=SECRET,
            expected=self.bindings,
            now=NOW,
        )
        self.attended = FakeAttendedPreflight(self.clock)

    def bridge(
        self,
        *,
        bindings: IbkrAutonomousAuthorityBindings | None = None,
    ) -> IbkrAutonomousPolicyPreflightBridge:
        return IbkrAutonomousPolicyPreflightBridge(
            attended=self.attended,
            authority=self.authority,
            expected_bindings=bindings or self.bindings,
            cancel_ttl_seconds=5,
        )

    def test_converts_attended_receipt_and_revalidates_original_delegate_receipt(self) -> None:
        bridge = self.bridge()
        request = order_request()
        decision = bridge.review(request)

        self.assertIsInstance(decision, LocalPreflightDecision)
        self.assertIsNone(decision.required_confirmation_phrase)
        self.assertEqual(decision.request.exact_tuple, request.exact_tuple)
        self.assertEqual(decision.policy_binding_id, self.bindings.policy_binding_id)
        self.assertEqual(
            decision.provider_contract_id,
            self.bindings.provider_contract_id,
        )
        self.assertEqual(decision.evidence_collection_id, "7" * 64)
        self.assertEqual(
            decision.preview["execution_authority"]["authority_contract_id"],
            self.authority.contract_id,
        )
        self.assertNotIn(attended_confirmation_phrase(request), repr(decision))

        self.assertEqual(bridge.contract_for(request), self.attended.contract)
        self.assertIsNone(bridge.revalidate(request, decision))
        self.assertIs(
            self.attended.revalidations[-1][1],
            self.attended.receipts[request.client_ref_id],
        )

    def test_mapping_is_exact_one_outstanding_and_cannot_be_forged(self) -> None:
        bridge = self.bridge()
        request = order_request()
        decision = bridge.review(request)
        with self.assertRaisesRegex(BrokerMutationBlocked, "ALREADY_OUTSTANDING"):
            bridge.review(request)
        self.assertEqual(self.attended.review_calls, 1)

        forged = replace(decision, decision_id=str(uuid4()))
        with self.assertRaisesRegex(BrokerMutationBlocked, "EXACT_UNEXPIRED"):
            bridge.revalidate(request, forged)
        changed = replace(request, quantity=4)
        with self.assertRaisesRegex(BrokerMutationBlocked, "EXACT_UNEXPIRED"):
            bridge.contract_for(changed)

    def test_authority_is_rechecked_and_bounds_decision_expiry(self) -> None:
        bridge = self.bridge()
        decision = bridge.review(order_request())
        self.assertEqual(decision.expires_at, NOW + timedelta(seconds=20))

        self.clock[0] = NOW + timedelta(seconds=61)
        calls = self.attended.review_calls
        with self.assertRaisesRegex(BrokerMutationBlocked, "AUTHORITY_NOT_CURRENT"):
            bridge.review(order_request(client_ref_id=str(uuid4())))
        self.assertEqual(self.attended.review_calls, calls)

        wrong = authority_bindings(release_manifest_hash="a" * 64)
        self.clock[0] = NOW
        with self.assertRaisesRegex(BrokerMutationBlocked, "AUTHORITY_NOT_CURRENT"):
            self.bridge(bindings=wrong)

    def test_protection_uses_same_conversion_without_confirmation_text(self) -> None:
        bridge = self.bridge()
        source = order_request()
        stop = order_request(
            client_ref_id=STOP_REF,
            side=BrokerSide.SELL,
            order_type=EquityOrderType.STOP_MARKET,
            time_in_force=TimeInForce.GTC,
            limit_price=None,
            stop_price=Decimal("9.00"),
        )
        decision = bridge.review_protection(
            source,
            "8" * 64,
            stop,
            NOW,
        )
        self.assertIsInstance(decision, LocalPreflightDecision)
        self.assertIsNone(decision.required_confirmation_phrase)
        self.assertEqual(decision.request.exact_tuple, stop.exact_tuple)
        self.assertEqual(self.attended.protection_calls, 1)
        self.assertIsNone(bridge.revalidate(stop, decision))

    def test_cancel_decision_requires_exact_target_and_fresh_broker_proof(self) -> None:
        bridge = self.bridge()
        broker_order_id = f"ibkr:{CLIENT_ID}:41"
        decision = bridge.review_cancel(CANCEL_REF, 41, broker_order_id)

        self.assertIsInstance(decision, LocalCancelDecision)
        self.assertEqual(decision.account_masked, MASK)
        self.assertEqual(decision.broker_order_id, broker_order_id)
        self.assertEqual(decision.client_ref_id, CANCEL_REF)
        self.assertRegex(decision.evidence_collection_id, r"^[0-9a-f]{64}$")
        self.assertEqual(decision.provider_contract_id, "6" * 64)
        self.assertEqual(len(self.attended.cancel_calls), 1)

        self.assertIsNone(bridge.authorize_cancel(CANCEL_REF, 41))
        self.assertEqual(len(self.attended.cancel_calls), 2)
        with self.assertRaisesRegex(BrokerMutationBlocked, "ALREADY_OUTSTANDING"):
            bridge.review_cancel(CANCEL_REF, 41, broker_order_id)
        with self.assertRaisesRegex(BrokerMutationBlocked, "BROKER_ORDER_ID_INVALID"):
            bridge.review_cancel(CANCEL_REF, 41, f"ibkr:{CLIENT_ID}:42")
        with self.assertRaisesRegex(BrokerMutationBlocked, "EXACT_UNEXPIRED"):
            bridge.authorize_cancel(str(uuid4()), 41)

    def test_release_inventory_has_one_attended_delegate_and_no_second_clock(self) -> None:
        inventory = self.bridge().release_components()
        self.assertEqual(len(inventory), 1)
        role, component, members = inventory[0]
        self.assertEqual(role, "ibkr_attended_preflight_delegate")
        self.assertIs(component, self.attended)
        self.assertIn("current_time", members)
        self.assertNotIn("ibkr_autonomous_preflight_clock", {item[0] for item in inventory})


if __name__ == "__main__":
    unittest.main()
