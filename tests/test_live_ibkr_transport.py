"""Synthetic integration only. Never connect to IBKR or a real socket."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import unittest
from uuid import uuid4

from titan_brain.live.broker.base import (
    AccountSnapshot, AttendedLocalReview, BrokerCapabilityError, BrokerContractViolation,
    BrokerMutationBlocked,
    BrokerOrderState, BrokerSide, BrokerUnknownSubmission, ClientRefLookupResult,
    ClientRefRecoverySource, EquityOrderType, FundsSnapshot, LocalPreflightDecision, MarketHours,
    OperationStatus, OrderCoverageContract, OrderFamily, OrderFamilyCoverage,
    OrderFamilyCoverageStatus, OrderRequest, OrderSnapshot, TimeInForce,
)
from titan_brain.live.broker.factory import build_broker_client
from titan_brain.live.broker.ibkr_ledger import IbkrExecutionLedger
from titan_brain.live.broker.ibkr_instrument import IbkrInstrumentProvider
from titan_brain.live.broker.ibkr_authority import IbkrDispatchAuthority
from titan_brain.live.broker.ibkr_orders import IbkrContractIdentity, IbkrOrderPlan
from titan_brain.live.broker.ibkr_preflight import (
    IbkrAttendedOrderPlan,
    IbkrAttendedPreflightBridge,
    IbkrOrderPurpose,
)
from titan_brain.live.broker.ibkr_sdk import IbkrSdkSession, IbkrWriteEvidence
from titan_brain.live.broker.ibkr_transport import (
    IBKR_TRANSPORT_ID, IbkrProductionTransport, attended_confirmation_phrase,
    attended_ibkr_descriptor, attended_order_preview, staged_ibkr_descriptor,
)
from titan_brain.live.broker.production import ProductionTransport
from titan_brain.live.component_provenance import UnsignedCompositionError
from titan_brain.live.composition import _unsigned_composition_failure


NOW = datetime(2026, 9, 14, 14, 0, tzinfo=timezone.utc)
ACCOUNT = "DU1234567"  # Synthetic fixture, not a connected account.
MASK = "****4567"
BINDING = "a" * 64
AUTH = "b" * 64
POLICY = "c" * 64
CONTRACT = "d" * 64
CLIENT = 19735


def complete_coverage():
    return OrderCoverageContract(
        contract_version="ibkr-synthetic-complete-v1",
        evidence_observed_at=NOW,
        families=tuple(
            OrderFamilyCoverage(
                family=family,
                status=OrderFamilyCoverageStatus.COMPLETE_GENERAL,
                evidence_id=f"synthetic-{family.value}",
                broker_authoritative=True,
                all_pages_consumed=True,
                includes_working_orders_across_dates=True,
                includes_parent_child_conditional=(family is OrderFamily.ADVANCED_EQUITY),
            )
            for family in OrderFamily
        ),
        client_ref_recovery_source=ClientRefRecoverySource.EXHAUSTIVE_ORDER_HISTORY,
        broker_preserves_client_ref=True,
        negative_client_ref_results_authoritative=False,
    )


class FakeConnection:
    def __init__(self):
        self.sent = []
        self.fail = False

    def sendMsg(self, payload):
        self.sent.append(payload)
        if self.fail:
            raise RuntimeError("private synthetic SDK error should not escape")
        return len(payload)


class FakeSdkClient:
    def __init__(self):
        self.connected = False
        self.conn = FakeConnection()
        self.calls = []
        self.on_place = None
        self.on_cancel = None

    def connect(self, host, port, clientId):
        self.host, self.port, self.clientId = host, port, clientId
        self.connected = True
        self.sendMsg(71, "start")

    def disconnect(self):
        self.connected = False

    def isConnected(self):
        return self.connected

    def sendMsg(self, msg_id, msg):
        return self.conn.sendMsg(f"{msg_id}:{msg}".encode())

    def sendMsgProtoBuf(self, msg_id, msg):
        return self.conn.sendMsg(str(msg_id).encode() + b":" + msg)

    def placeOrder(self, order_id, contract, order):
        self.calls.append(("submit", order_id, contract, order))
        if self.on_place:
            self.on_place()
        self.sendMsg(3, "synthetic-order")

    def cancelOrder(self, order_id, order_cancel):
        self.calls.append(("cancel", order_id))
        if self.on_cancel:
            self.on_cancel()
        self.sendMsg(4, "synthetic-cancel")


class ImmediateContractRequester:
    """Offline contract-details fixture; never opens a socket."""

    def __init__(self):
        self.callbacks = None

    def reqContractDetails(self, reqId, query):
        returned = SimpleNamespace(
            conId=12345,
            symbol=query.symbol,
            secType="STK",
            currency="USD",
            exchange="SMART",
            primaryExchange="NASDAQ",
        )
        self.callbacks.contractDetails(
            reqId,
            SimpleNamespace(
                contract=returned,
                validExchanges="SMART,NASDAQ",
                liquidHours="20260914:0930-20260914:1600",
                timeZoneId="US/Eastern",
            ),
        )
        self.callbacks.contractDetailsEnd(reqId)

    def cancelContractDetails(self, reqId):
        del reqId


class FixturePreflight:
    def __init__(self):
        self.revalidations = 0
        self.allowed = True
        self.cancel_allowed = True
        self.contract = IbkrContractIdentity(12345, "TEST", "NASDAQ")
        self.review_change = lambda review: review
        self.reviews_seen = []

    def release_components(self):
        return ()

    def review(self, request):
        return self.review_change(AttendedLocalReview(
            request=request, reviewed_at=NOW, received_at=NOW,
            expires_at=NOW + timedelta(seconds=5), disclosure="Synthetic offline test only",
            order_checks=(), required_confirmation_phrase=attended_confirmation_phrase(request),
            broker_review_id=None, broker_bound=False, decision_id=str(uuid4()),
            policy_binding_id=POLICY, evidence_collection_id="e" * 64,
            provider_contract_id=CONTRACT, preview=attended_order_preview(request),
        ))

    def contract_for(self, request):
        return self.contract

    def revalidate(self, request, review):
        self.reviews_seen.append(review)
        self.revalidations += 1
        if not self.allowed:
            return False
        return None

    def authorize_cancel(self, client_ref_id, order_id):
        return None if self.cancel_allowed else False

    def review_protection(
        self, source_request, source_plan_id, stop_template, source_claimed_at
    ):
        raise AssertionError("not used by the transport fixture")


class FixtureReads:
    def release_components(self):
        return ()

    def get_account_base(self, exact_account_id):
        raise AssertionError("not used in dependency inventory test")

    def list_order_family_page(self, exact_account_id, family, cursor):
        raise AssertionError("not used in dependency inventory test")

    def lookup_equity_orders_by_client_ref(self, exact_account_id, client_refs):
        raise AssertionError("not used in dependency inventory test")


class IbkrTransportTests(unittest.TestCase):
    def setUp(self):
        self.no_socket = patch("socket.socket.connect", side_effect=AssertionError("network forbidden"))
        self.no_socket.start()
        self.addCleanup(self.no_socket.stop)
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = IbkrExecutionLedger(
            Path(self.tmp.name) / "ibkr.sqlite3", account_fingerprint=BINDING,
            environment="paper", client_id=CLIENT,
        )
        self.addCleanup(self.ledger.close)
        self.sdk = FakeSdkClient()
        self.preflight = FixturePreflight()
        self.authorizer_calls = []
        self.wire_allowed = True
        self.clock = lambda: NOW
        self.authority = IbkrDispatchAuthority(
            ledger=self.ledger, authorization_binding_id=AUTH,
            provider_contract_id=CONTRACT, policy_binding_id=POLICY,
            verify_acceptance=self.verify_acceptance,
            revalidate=self.preflight.revalidate,
            authorize_cancel=self.preflight.authorize_cancel,
            clock=self.clock,
        )
        self.session = IbkrSdkSession(
            client=self.sdk, sdk_version="10.50.2", expected_account=ACCOUNT,
            account_binding_fingerprint=BINDING, environment="paper", client_id=CLIENT,
            order_cancel_factory=SimpleNamespace, mutation_interlock=lambda: None,
            authorize_dispatch=self.authority, clock=self.clock,
        )
        generation = self.session.connect()
        self.session.observe_next_valid_id(100, generation=generation)
        self.session.observe_managed_accounts((ACCOUNT,), generation=generation)
        self.session.authorize_writes(IbkrWriteEvidence(
            authorization_binding_id=AUTH, account_binding_fingerprint=BINDING,
            environment="paper", client_id=CLIENT, reviewed_contract_id=CONTRACT,
            issued_at=NOW, expires_at=NOW + timedelta(minutes=1),
        ))
        self.staged = staged_ibkr_descriptor(
            exact_account_id=ACCOUNT, account_masked=MASK,
            account_binding_fingerprint=BINDING, authorization_binding_id=AUTH,
            observed_at=NOW,
        )
        self.descriptor = attended_ibkr_descriptor(
            exact_account_id=ACCOUNT,
            account_masked=MASK,
            account_binding_fingerprint=BINDING,
            authorization_binding_id=AUTH,
            coverage=complete_coverage(),
        )
        self.transport = self.build()
        self.request = OrderRequest(
            account_masked=MASK, symbol="TEST", side=BrokerSide.BUY,
            order_type=EquityOrderType.LIMIT, quantity=2,
            market_hours=MarketHours.REGULAR, time_in_force=TimeInForce.GFD,
            client_ref_id=str(uuid4()), limit_price=Decimal("10.00"),
        )

    def verify_acceptance(self, evidence):
        self.authorizer_calls.append("verify")
        if not self.wire_allowed:
            raise BrokerMutationBlocked("fixture denies")
        if evidence.authorization_binding_id != AUTH or evidence.reviewed_contract_id != CONTRACT:
            raise BrokerMutationBlocked("wrong acceptance")
        return None

    def build(self, **overrides):
        args = dict(
            descriptor=self.descriptor, session=self.session, ledger=self.ledger,
            preflight=self.preflight, contract_factory=SimpleNamespace,
            order_factory=SimpleNamespace, policy_binding_id=POLICY,
            provider_contract_id=CONTRACT, clock=self.clock,
            authority=self.authority,
        )
        args.update(overrides)
        return IbkrProductionTransport(**args)

    def submit(self):
        review = self.transport.review_equity_order(ACCOUNT, self.request)
        return self.transport.place_equity_order(
            ACCOUNT,
            self.request,
            review=review,
            explicit_confirmation=review.required_confirmation_phrase,
        )

    def cancel(self, *, transport=None, target=None):
        transport = transport or self.transport
        target = target or transport.broker_order_id(100)
        intent = self.ledger.lookup(self.request.client_ref_id)
        if intent is not None and intent.status == "UNKNOWN":
            self.ledger.record_event(
                self.request.client_ref_id, str(uuid4()), "ACK", perm_id=1000
            )
        review = transport.review_cancel_equity_order(ACCOUNT, target)
        return transport.cancel_equity_order(
            ACCOUNT,
            target,
            explicit_confirmation=review.required_confirmation_phrase,
        )

    def test_staged_descriptor_does_not_enable_runtime(self):
        staged = self.build(descriptor=self.staged)
        self.assertIsInstance(staged, ProductionTransport)
        self.assertFalse(staged.descriptor.capabilities.supports_daemon_writes)
        self.assertFalse(staged.descriptor.capabilities.can_prove_whole_broker_reconciliation)
        with self.assertRaises(BrokerCapabilityError):
            build_broker_client({
                "broker_adapter": "supported_production_transport",
                "production_transport_id": IBKR_TRANSPORT_ID,
                "production_account_binding_fingerprint": BINDING,
                "production_authorization_binding_id": AUTH,
            }, account_masked=MASK, production_transport=staged)

    def test_place_through_existing_factory_is_unknown_not_accepted(self):
        adapter = build_broker_client({
            "broker_adapter": "supported_production_transport",
            "production_transport_id": IBKR_TRANSPORT_ID,
            "production_account_binding_fingerprint": BINDING,
            "production_authorization_binding_id": AUTH,
        }, account_masked=MASK, production_transport=self.transport)
        # Existing adapter's real-time clock is overridden only for this fixture.
        adapter._clock = lambda: NOW
        review = adapter.review_equity_order(self.request)
        result = adapter.place_equity_order(
            self.request,
            review=review,
            explicit_confirmation=review.required_confirmation_phrase,
        )
        self.assertEqual(result.status, OperationStatus.UNKNOWN)
        self.assertIsNone(result.accepted)
        self.assertEqual(self.authorizer_calls, ["verify"] * 4)
        self.assertEqual(self.preflight.revalidations, 5)
        intent = self.ledger.lookup(self.request.client_ref_id)
        self.assertFalse(intent.can_transmit)
        self.assertEqual(self.sdk.calls[0][1], intent.order_id)
        self.assertEqual(self.sdk.calls[0][3].orderRef, self.request.client_ref_id)
        with self.assertRaises(BrokerContractViolation):
            adapter.place_equity_order(
                self.request,
                review=review,
                explicit_confirmation=review.required_confirmation_phrase,
            )
        self.assertEqual(len(self.sdk.calls), 1)

    def test_attended_place_requires_exact_phrase_and_burns_failed_attempt(self):
        review = self.transport.review_equity_order(ACCOUNT, self.request)
        self.assertEqual(
            review.required_confirmation_phrase,
            "CONFIRM BUY 2 TEST LIMIT 10.00 GFD REGULAR_HOURS",
        )
        self.assertEqual(dict(review.preview), dict(attended_order_preview(self.request)))
        with self.assertRaisesRegex(BrokerMutationBlocked, "exact attended confirmation"):
            self.transport.place_equity_order(
                ACCOUNT, self.request, review=review,
                explicit_confirmation=review.required_confirmation_phrase.lower(),
            )
        intent = self.ledger.lookup(self.request.client_ref_id)
        self.assertEqual(intent.status, "ABORTED")
        self.assertFalse(intent.can_transmit)
        with self.assertRaises(BrokerMutationBlocked):
            self.transport.place_equity_order(
                ACCOUNT, self.request, review=review,
                explicit_confirmation=review.required_confirmation_phrase,
            )

    def test_local_or_incomplete_preview_cannot_replace_attended_review(self):
        attended = self.preflight.review(self.request)
        local = LocalPreflightDecision(
            request=attended.request,
            reviewed_at=attended.reviewed_at,
            received_at=attended.received_at,
            expires_at=attended.expires_at,
            disclosure=attended.disclosure,
            order_checks=attended.order_checks,
            required_confirmation_phrase=None,
            broker_review_id=None,
            broker_bound=False,
            preview=attended.preview,
            decision_id=attended.decision_id,
            policy_binding_id=attended.policy_binding_id,
            evidence_collection_id=attended.evidence_collection_id,
            provider_contract_id=attended.provider_contract_id,
        )
        for change in (
            lambda _review: local,
            lambda review: replace(review, preview={"symbol": "TEST"}),
            lambda review: replace(review, required_confirmation_phrase="CONFIRM SOMETHING ELSE"),
        ):
            with self.subTest(change=change):
                self.preflight.review_change = change
                with self.assertRaises(BrokerMutationBlocked):
                    self.transport.review_equity_order(ACCOUNT, self.request)
        self.assertEqual(self.sdk.calls, [])

    def test_attended_review_object_reaches_socket_authority_without_downgrade(self):
        review = self.transport.review_equity_order(ACCOUNT, self.request)
        self.transport.place_equity_order(
            ACCOUNT,
            self.request,
            review=review,
            explicit_confirmation=review.required_confirmation_phrase,
        )
        self.assertGreaterEqual(len(self.preflight.reviews_seen), 4)
        self.assertTrue(all(item is review for item in self.preflight.reviews_seen))
        self.assertTrue(all(isinstance(item, AttendedLocalReview)
                            for item in self.preflight.reviews_seen))

    def test_extended_hours_is_rejected_at_transport_boundary(self):
        extended = replace(self.request, market_hours=MarketHours.EXTENDED)
        with self.assertRaisesRegex(BrokerCapabilityError, "regular hours"):
            self.transport.review_equity_order(ACCOUNT, extended)
        self.assertEqual(self.sdk.calls, [])

    def test_duplicate_intent_cannot_send_after_restart_of_coordinator(self):
        self.submit()
        restarted = self.build()
        review = restarted.review_equity_order(ACCOUNT, self.request)
        with self.assertRaises(BrokerMutationBlocked):
            restarted.place_equity_order(
                ACCOUNT,
                self.request,
                review=review,
                explicit_confirmation=review.required_confirmation_phrase,
            )
        self.assertEqual(len(self.sdk.calls), 1)

    def test_socket_failure_keeps_unknown_and_redacts_sdk_error(self):
        self.sdk.conn.fail = True
        with self.assertRaises(BrokerUnknownSubmission) as caught:
            self.submit()
        self.assertNotIn("private synthetic", str(caught.exception))
        self.assertFalse(self.ledger.lookup(self.request.client_ref_id).can_transmit)

    def test_post_send_ledger_failure_stays_unknown(self):
        with patch.object(self.ledger, "record_event", side_effect=RuntimeError("db detail")):
            with self.assertRaises(BrokerUnknownSubmission) as caught:
                self.submit()
        self.assertNotIn("db detail", str(caught.exception))
        self.assertEqual(self.ledger.lookup(self.request.client_ref_id).status, "SENDING")
        self.assertEqual(len(self.sdk.calls), 1)

    def test_socket_and_ledger_failure_stays_unknown(self):
        self.sdk.conn.fail = True
        with patch.object(self.ledger, "record_event", side_effect=RuntimeError("db detail")):
            with self.assertRaises(BrokerUnknownSubmission):
                self.submit()

    def test_missing_and_false_preflight_cannot_send(self):
        missing = self.build(preflight=None)
        with self.assertRaises(BrokerMutationBlocked):
            missing.review_equity_order(ACCOUNT, self.request)
        self.preflight.allowed = False
        with self.assertRaises(BrokerMutationBlocked):
            self.submit()
        self.assertEqual(self.sdk.calls, [])
        self.assertIsNone(self.ledger.lookup(self.request.client_ref_id))

    def test_revoked_at_wire_does_not_transmit(self):
        self.sdk.on_place = lambda: setattr(self, "wire_allowed", False)
        before = len(self.sdk.conn.sent)
        with self.assertRaises(BrokerMutationBlocked):
            self.submit()
        self.assertEqual(len(self.sdk.conn.sent), before)
        intent = self.ledger.lookup(self.request.client_ref_id)
        self.assertEqual(intent.status, "ABORTED")
        self.assertFalse(intent.submission_unknown_seen)

    def test_missing_read_bridge_never_returns_empty_ready_account(self):
        with self.assertRaises(BrokerCapabilityError):
            self.transport.get_account_base(ACCOUNT)

    def test_attended_transport_rejects_autonomous_risk_enricher(self):
        with self.assertRaisesRegex(
            BrokerCapabilityError,
            "attended transport cannot retain autonomous risk enrichment",
        ):
            self.build(account_snapshot_enricher=lambda snapshot: snapshot)

    def test_missing_concrete_authority_cannot_send(self):
        missing = self.build(authority=None)
        review = missing.review_equity_order(ACCOUNT, self.request)
        with self.assertRaises(BrokerMutationBlocked):
            missing.place_equity_order(
                ACCOUNT,
                self.request,
                review=review,
                explicit_confirmation=review.required_confirmation_phrase,
            )
        self.assertEqual(self.sdk.calls, [])
        intent = self.ledger.lookup(self.request.client_ref_id)
        self.assertEqual(intent.status, "ABORTED")
        self.assertFalse(intent.can_transmit)

    def test_place_revalidation_failure_aborts_reserved_intent(self):
        review = self.transport.review_equity_order(ACCOUNT, self.request)
        self.assertTrue(self.ledger.lookup(self.request.client_ref_id).can_transmit)
        self.preflight.allowed = False
        with self.assertRaises(BrokerMutationBlocked):
            self.transport.place_equity_order(
                ACCOUNT,
                self.request,
                review=review,
                explicit_confirmation=review.required_confirmation_phrase,
            )
        intent = self.ledger.lookup(self.request.client_ref_id)
        self.assertEqual(intent.status, "ABORTED")
        self.assertFalse(intent.can_transmit)
        self.assertEqual(self.sdk.calls, [])

    def test_pre_wire_abort_persistence_failure_becomes_unknown(self):
        review = self.transport.review_equity_order(ACCOUNT, self.request)
        self.preflight.allowed = False
        with patch.object(
            self.ledger,
            "abort_reserved_intent",
            side_effect=RuntimeError("synthetic persistence failure"),
        ):
            with self.assertRaises(BrokerUnknownSubmission):
                self.transport.place_equity_order(
                    ACCOUNT,
                    self.request,
                    review=review,
                    explicit_confirmation=review.required_confirmation_phrase,
                )
        intent = self.ledger.lookup(self.request.client_ref_id)
        self.assertEqual(intent.status, "UNKNOWN")
        self.assertFalse(intent.can_transmit)
        self.assertEqual(self.sdk.calls, [])

    def test_review_capacity_failure_aborts_allocated_intent(self):
        seed = self.preflight.review(self.request)
        reviews = tuple(
            replace(seed, decision_id=str(uuid4())) for _ in range(256)
        )
        self.transport._reviews = {item.decision_id: item for item in reviews}
        with self.assertRaisesRegex(BrokerMutationBlocked, "capacity"):
            self.transport.review_equity_order(ACCOUNT, self.request)
        intent = self.ledger.lookup(self.request.client_ref_id)
        self.assertEqual(intent.status, "ABORTED")
        self.assertFalse(intent.can_transmit)
        self.assertEqual(self.sdk.calls, [])

    def test_missing_acceptance_verifier_cannot_send(self):
        self.authority._verify = None
        before = len(self.sdk.conn.sent)
        with self.assertRaises(BrokerMutationBlocked):
            self.submit()
        self.assertEqual(len(self.sdk.conn.sent), before)
        self.assertEqual(self.ledger.lookup(self.request.client_ref_id).status, "ABORTED")

    def test_boolean_acceptance_is_not_approval(self):
        self.authority._verify = lambda evidence: True
        before = len(self.sdk.conn.sent)
        with self.assertRaises(BrokerMutationBlocked):
            self.submit()
        self.assertEqual(len(self.sdk.conn.sent), before)
        self.assertEqual(self.ledger.lookup(self.request.client_ref_id).status, "ABORTED")

    def test_sdk_payload_mutation_cannot_escape_review(self):
        self.sdk.on_place = lambda: setattr(self.sdk.calls[-1][3], "totalQuantity", Decimal("200"))
        before = len(self.sdk.conn.sent)
        with self.assertRaises(BrokerMutationBlocked):
            self.submit()
        self.assertEqual(len(self.sdk.conn.sent), before)
        self.assertEqual(self.authority._contexts, {})
        self.assertEqual(self.ledger.lookup(self.request.client_ref_id).status, "ABORTED")

    def test_changed_payload_before_registration_cannot_reuse_review(self):
        original = IbkrOrderPlan.to_sdk
        def corrupt(plan, **factories):
            contract, order = original(plan, **factories)
            order.totalQuantity = Decimal("200")
            return contract, order
        with patch.object(IbkrOrderPlan, "to_sdk", corrupt):
            with self.assertRaises(BrokerMutationBlocked):
                self.submit()
        self.assertEqual(self.sdk.calls, [])
        intent = self.ledger.lookup(self.request.client_ref_id)
        self.assertIsNotNone(intent)
        self.assertEqual(intent.status, "ABORTED")
        self.assertFalse(intent.can_transmit)
        # Repeated coordinator calls still cannot reuse the reserved intent.
        with self.assertRaises(BrokerMutationBlocked):
            self.submit()

    def test_review_persists_exact_reference_before_any_dispatch(self):
        review = self.transport.review_equity_order(ACCOUNT, self.request)
        intent = self.ledger.lookup(self.request.client_ref_id)
        self.assertIsNotNone(intent)
        self.assertEqual(intent.status, "RESERVED")
        self.assertTrue(intent.can_transmit)
        self.assertEqual(intent.order_id, 100)
        self.assertEqual(self.sdk.calls, [])

        # A recreated transport can recover the exact same identity without
        # allocating a second order ID; it still needs a fresh exact review.
        restarted = self.build()
        fresh = restarted.review_equity_order(ACCOUNT, self.request)
        recovered = self.ledger.lookup(self.request.client_ref_id)
        self.assertEqual(recovered.order_id, intent.order_id)
        self.assertNotEqual(fresh.decision_id, review.decision_id)
        self.assertEqual(self.sdk.calls, [])

    def test_rejection_during_no_wire_cleanup_surfaces_unknown(self):
        calls = []
        def revalidate(request, review):
            calls.append(1)
            if len(calls) == 2:
                self.ledger.record_event(request.client_ref_id, str(uuid4()), "REJECT")
        self.authority._revalidate = revalidate
        before = len(self.sdk.conn.sent)
        with self.assertRaisesRegex(BrokerUnknownSubmission, "possible exposure"):
            self.submit()
        self.assertEqual(len(self.sdk.conn.sent), before)
        intent = self.ledger.lookup(self.request.client_ref_id)
        self.assertEqual(intent.status, "UNKNOWN")
        self.assertTrue(intent.rejection_seen)
        self.assertTrue(intent.submission_unknown_seen)
        self.assertFalse(intent.can_transmit)

    def test_ack_and_fill_races_during_no_wire_cleanup_surface_unknown(self):
        for evidence in ("ACK", "FILL"):
            with self.subTest(evidence=evidence):
                calls = []

                def revalidate(request, review):
                    calls.append(1)
                    if len(calls) != 2:
                        return
                    if evidence == "ACK":
                        self.ledger.record_event(
                            request.client_ref_id,
                            str(uuid4()),
                            "ACK",
                            perm_id=1000,
                        )
                    else:
                        self.ledger.record_fill(
                            request.client_ref_id,
                            f"fill-{evidence.lower()}",
                            1,
                            "10.00",
                            1001,
                            NOW,
                        )

                self.authority._revalidate = revalidate
                before = len(self.sdk.conn.sent)
                with self.assertRaisesRegex(
                    BrokerUnknownSubmission,
                    "possible exposure",
                ):
                    self.submit()
                self.assertEqual(len(self.sdk.conn.sent), before)
                intent = self.ledger.lookup(self.request.client_ref_id)
                self.assertEqual(intent.status, "ACK")
                self.assertTrue(intent.acknowledgement_seen)
                self.assertTrue(intent.submission_unknown_seen)
                self.assertFalse(intent.can_transmit)

                if evidence == "ACK":
                    self.request = replace(
                        self.request,
                        client_ref_id=str(uuid4()),
                    )

    def test_context_removed_even_when_send_fails(self):
        self.sdk.conn.fail = True
        with self.assertRaises(BrokerUnknownSubmission):
            self.submit()
        self.assertEqual(self.authority._contexts, {})

    def test_review_expiry_during_wire_callback_prevents_send(self):
        calls = []
        def revalidate(request, review):
            calls.append(1)
            if len(calls) == 2:
                self.authority._clock = lambda: NOW + timedelta(seconds=6)
        self.authority._revalidate = revalidate
        before = len(self.sdk.conn.sent)
        with self.assertRaises(BrokerMutationBlocked):
            self.submit()
        self.assertEqual(len(self.sdk.conn.sent), before)
        self.assertEqual(self.ledger.lookup(self.request.client_ref_id).status, "ABORTED")

    def test_context_revocation_during_wire_callback_prevents_send(self):
        calls = []
        def revalidate(request, review):
            calls.append(1)
            if len(calls) == 2:
                self.authority.finish(100)
        self.authority._revalidate = revalidate
        before = len(self.sdk.conn.sent)
        with self.assertRaises(BrokerMutationBlocked):
            self.submit()
        self.assertEqual(len(self.sdk.conn.sent), before)
        self.assertEqual(self.ledger.lookup(self.request.client_ref_id).status, "ABORTED")

    def test_cancel_context_revocation_during_callback_prevents_send(self):
        self.submit()
        self.ledger.record_event(
            self.request.client_ref_id, str(uuid4()), "ACK", perm_id=1000
        )
        review = self.transport.review_cancel_equity_order(
            ACCOUNT, self.transport.broker_order_id(100)
        )
        self.authority._authorize_cancel = lambda ref, order_id: self.authority.finish(order_id)
        before = len(self.sdk.conn.sent)
        with self.assertRaises(BrokerMutationBlocked):
            self.transport.cancel_equity_order(
                ACCOUNT,
                self.transport.broker_order_id(100),
                explicit_confirmation=review.required_confirmation_phrase,
            )
        self.assertEqual(len(self.sdk.conn.sent), before)
        intent = self.ledger.lookup(self.request.client_ref_id)
        self.assertTrue(intent.can_cancel)
        self.assertFalse(intent.cancellation_unknown_seen)

    def test_cancel_review_expiry_at_socket_boundary_prevents_send(self):
        self.submit()
        self.ledger.record_event(
            self.request.client_ref_id, str(uuid4()), "ACK", perm_id=1000
        )
        target = self.transport.broker_order_id(100)
        review = self.transport.review_cancel_equity_order(ACCOUNT, target)
        self.sdk.on_cancel = lambda: setattr(
            self.authority, "_clock", lambda: NOW + timedelta(seconds=16)
        )
        before = len(self.sdk.conn.sent)
        with self.assertRaises(BrokerMutationBlocked):
            self.transport.cancel_equity_order(
                ACCOUNT,
                target,
                explicit_confirmation=review.required_confirmation_phrase,
            )
        self.assertEqual(len(self.sdk.conn.sent), before)
        intent = self.ledger.lookup(self.request.client_ref_id)
        self.assertTrue(intent.can_cancel)
        self.assertFalse(intent.cancellation_unknown_seen)

    def test_binding_mismatch_between_authority_and_descriptor_refused(self):
        with self.assertRaises(BrokerContractViolation):
            self.build(provider_contract_id="f"*64)

    def test_wrong_account_refused_before_any_write(self):
        with self.assertRaises(BrokerContractViolation):
            self.transport.review_equity_order("DU9994567", self.request)
        self.assertEqual(self.sdk.calls, [])

    def test_expired_or_wrong_provider_preflight_refused(self):
        for change in (
            lambda r: replace(r, reviewed_at=NOW-timedelta(seconds=6), expires_at=NOW),
            lambda r: replace(r, provider_contract_id="f"*64),
            lambda r: replace(r, evidence_collection_id="not-a-collection"),
            lambda r: replace(r, policy_binding_id="f"*64),
        ):
            with self.subTest(change=change):
                self.preflight.review_change = change
                with self.assertRaises(BrokerMutationBlocked):
                    self.submit()
        self.assertEqual(self.sdk.calls, [])

    def test_cancel_requires_owned_sent_order_and_is_not_acknowledged(self):
        with self.assertRaises(BrokerMutationBlocked):
            self.transport.review_cancel_equity_order(
                ACCOUNT, self.transport.broker_order_id(100)
            )
        self.submit()
        result = self.cancel()
        self.assertEqual(result.status, OperationStatus.UNKNOWN)
        self.assertIsNone(result.accepted)
        self.assertEqual(self.sdk.calls[-1], ("cancel", 100))

    def test_cancel_requires_exact_fresh_one_shot_preview(self):
        self.submit()
        self.ledger.record_event(
            self.request.client_ref_id, str(uuid4()), "ACK", perm_id=1000
        )
        target = self.transport.broker_order_id(100)
        with self.assertRaisesRegex(BrokerMutationBlocked, "exact unexpired"):
            self.transport.cancel_equity_order(
                ACCOUNT, target,
                explicit_confirmation=self.transport.cancel_confirmation_phrase(target),
            )
        review = self.transport.review_cancel_equity_order(ACCOUNT, target)
        self.assertEqual(review.required_confirmation_phrase, f"CONFIRM CANCEL {target}")
        self.assertEqual(review.preview["broker_order_id"], target)
        with self.assertRaisesRegex(BrokerMutationBlocked, "exact unexpired"):
            self.transport.cancel_equity_order(
                ACCOUNT, target,
                explicit_confirmation=review.required_confirmation_phrase + " ",
            )
        result = self.transport.cancel_equity_order(
            ACCOUNT, target,
            explicit_confirmation=review.required_confirmation_phrase,
        )
        self.assertEqual(result.status, OperationStatus.UNKNOWN)
        with self.assertRaises(BrokerMutationBlocked):
            self.transport.cancel_equity_order(
                ACCOUNT, target,
                explicit_confirmation=review.required_confirmation_phrase,
            )

    def test_unknown_cancel_is_never_blindly_repeated(self):
        self.submit()
        target = self.transport.broker_order_id(100)
        self.cancel(target=target)
        with self.assertRaises(Exception):
            self.build().review_cancel_equity_order(ACCOUNT, target)
        self.assertEqual([c[0] for c in self.sdk.calls], ["submit", "cancel"])

    def test_other_client_or_non_namespaced_cancel_refused(self):
        for target in ("100", "ibkr:0:100", "ibkr:19736:100", "ibkr:19735:-1"):
            with self.subTest(target=target):
                with self.assertRaises((BrokerContractViolation, BrokerMutationBlocked)):
                    self.transport.cancel_equity_order(ACCOUNT, target)
        self.assertEqual(self.sdk.calls, [])

    def test_false_cancel_preflight_refuses_dispatch(self):
        self.submit()
        self.ledger.record_event(
            self.request.client_ref_id, str(uuid4()), "ACK", perm_id=1000
        )
        self.preflight.cancel_allowed = False
        with self.assertRaises(BrokerMutationBlocked):
            self.transport.review_cancel_equity_order(
                ACCOUNT, self.transport.broker_order_id(100)
            )
        self.assertEqual(len(self.sdk.calls), 1)

    def test_post_cancel_persistence_failure_remains_unknown(self):
        self.submit()
        self.ledger.record_event(
            self.request.client_ref_id, str(uuid4()), "ACK", perm_id=1000
        )
        review = self.transport.review_cancel_equity_order(
            ACCOUNT, self.transport.broker_order_id(100)
        )
        with patch.object(self.ledger, "record_event", side_effect=RuntimeError("db detail")):
            with self.assertRaises(BrokerUnknownSubmission):
                self.transport.cancel_equity_order(
                    ACCOUNT,
                    self.transport.broker_order_id(100),
                    explicit_confirmation=review.required_confirmation_phrase,
                )
        self.assertEqual(self.sdk.calls[-1], ("cancel", 100))

    def test_unsafe_descriptor_claims_refused(self):
        for flag in (
            "supports_atomic_protection",
            "supports_equity_replace",
            "supports_unattended_writes",
        ):
            with self.subTest(flag=flag):
                descriptor = replace(self.descriptor, capabilities=replace(
                    self.descriptor.capabilities, **{flag: True},
                ))
                with self.assertRaises(BrokerCapabilityError):
                    self.build(descriptor=descriptor)

    def test_unaccepted_dependency_graph_blocks_release(self):
        with self.assertRaisesRegex(BrokerCapabilityError, "inventory is incomplete"):
            self.transport.release_components()

    def test_complete_dependency_graph_is_exhaustively_inventoried(self):
        transport = self.build(reads=FixtureReads())
        leaves = {
            role: (component, members)
            for role, component, members in transport.release_components()
        }
        self.assertEqual(set(leaves), {
            "ibkr_sdk_session",
            "ibkr_read_bridge",
            "ibkr_preflight_bridge",
            "ibkr_contract_factory",
            "ibkr_order_factory",
            "ibkr_ledger_reconciler",
        })
        self.assertIs(leaves["ibkr_sdk_session"][0], self.session)
        self.assertIs(leaves["ibkr_preflight_bridge"][0], self.preflight)
        self.assertTrue(all(members for _component, members in leaves.values()))
        authority_leaves = {
            role: (component, members)
            for role, component, members in self.authority.release_components()
        }
        self.assertEqual(set(authority_leaves), {
            "ibkr_execution_ledger",
            "ibkr_acceptance_verifier",
            "ibkr_revalidation_callback",
            "ibkr_cancel_authorizer",
        })
        self.assertIs(authority_leaves["ibkr_execution_ledger"][0], self.ledger)

    def test_release_inventory_refuses_missing_authority_callback(self):
        transport = self.build(reads=FixtureReads())
        self.authority._verify = None
        with self.assertRaisesRegex(BrokerCapabilityError, "nested dependency"):
            transport.release_components()

    def test_ibkr_attestation_roles_are_explicitly_allowlisted(self):
        roles = {
            "ibkr_sdk_session",
            "ibkr_read_bridge",
            "ibkr_read_callback_router",
            "ibkr_command_callback_router",
            "ibkr_execution_filter_factory",
            "ibkr_read_clock",
            "ibkr_account_snapshot_reader",
            "ibkr_instrument_provider",
            "ibkr_attended_plan_reader",
            "ibkr_attended_preflight_delegate",
            "ibkr_risk_policy_check",
            "ibkr_session_eligibility_check",
            "ibkr_preflight_clock",
            "ibkr_preflight_bridge",
            "ibkr_contract_factory",
            "ibkr_order_factory",
            "ibkr_read_requester",
            "ibkr_contract_details_requester",
            "ibkr_instrument_contract_factory",
            "ibkr_instrument_clock",
            "ibkr_execution_ledger",
            "ibkr_ledger_reconciler",
            "ibkr_autonomous_authority_contract",
            "ibkr_autonomous_authority_bindings",
            "ibkr_autonomous_authority_clock",
            "ibkr_autonomous_authority_key_loader",
            "ibkr_autonomous_plan_clock",
            "ibkr_autonomous_plan_reader",
            "ibkr_autonomous_plan_sealer",
            "ibkr_autonomous_plan_state",
            "ibkr_acceptance_verifier",
            "ibkr_revalidation_callback",
            "ibkr_cancel_authorizer",
            "ibkr_sdk_client",
            "ibkr_sdk_cancel_factory",
            "ibkr_sdk_mutation_interlock",
            "ibkr_sdk_dispatch_authorizer",
            "ibkr_sdk_clock",
        }
        for role in roles:
            with self.subTest(role=role):
                self.assertEqual(
                    _unsigned_composition_failure(
                        UnsignedCompositionError(
                            role, "IMPLEMENTATION_NOT_IN_MANIFEST"
                        )
                    ),
                    f"UNSIGNED_COMPOSITION:{role}:IMPLEMENTATION_NOT_IN_MANIFEST",
                )
        self.assertEqual(
            _unsigned_composition_failure(
                UnsignedCompositionError(
                    "ibkr_unapproved_extension", "IMPLEMENTATION_NOT_IN_MANIFEST"
                )
            ),
            "UNSIGNED_COMPOSITION:runtime:IMPLEMENTATION_NOT_IN_MANIFEST",
        )


class ConcreteIbkrAttendedCancelTests(unittest.TestCase):
    """Concrete preflight -> transport -> guarded SDK integration, offline."""

    def setUp(self):
        self.no_socket = patch(
            "socket.socket.connect", side_effect=AssertionError("network forbidden")
        )
        self.no_socket.start()
        self.addCleanup(self.no_socket.stop)
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.current = NOW
        self.clock = lambda: self.current
        self.snapshot_reads = 0
        self.visible_orders = ()
        self.request = OrderRequest(
            account_masked=MASK,
            symbol="TEST",
            side=BrokerSide.BUY,
            order_type=EquityOrderType.LIMIT,
            quantity=2,
            market_hours=MarketHours.REGULAR,
            time_in_force=TimeInForce.GFD,
            client_ref_id=str(uuid4()),
            limit_price=Decimal("10.00"),
        )
        self.stop = OrderRequest(
            account_masked=MASK,
            symbol="TEST",
            side=BrokerSide.SELL,
            order_type=EquityOrderType.STOP_MARKET,
            quantity=2,
            market_hours=MarketHours.REGULAR,
            time_in_force=TimeInForce.GTC,
            client_ref_id=str(uuid4()),
            stop_price=Decimal("9.00"),
        )
        self.plan = IbkrAttendedOrderPlan(
            plan_id="concrete-entry-plan",
            purpose=IbkrOrderPurpose.ENTRY,
            request=self.request,
            structural_stop=Decimal("9.00"),
            targets=(Decimal("12.00"),),
            execution_reserve=Decimal("1.00"),
            fee_reserve=Decimal("0.50"),
            required_stop_request=self.stop,
        )
        requester = ImmediateContractRequester()
        self.instrument = IbkrInstrumentProvider(
            requester=requester,
            contract_factory=SimpleNamespace,
            exact_account_id=ACCOUNT,
            account_masked=MASK,
            timeout_seconds=0.02,
            clock=self.clock,
        )
        callbacks = self.instrument.open_generation(1)
        requester.callbacks = callbacks
        callbacks.managedAccounts(ACCOUNT)
        self.preflight = IbkrAttendedPreflightBridge(
            account_snapshot=self.account_snapshot,
            instruments=self.instrument,
            plan_reader=lambda request: self.plan,
            risk_policy_check=lambda snapshot, plan, now: None,
            session_is_entry_eligible=lambda now, purpose: True,
            account_masked=MASK,
            command_client_id=CLIENT,
            policy_binding_id=POLICY,
            provider_contract_id=CONTRACT,
            account_max_age_seconds=2,
            instrument_max_age_seconds=2,
            review_ttl_seconds=5,
            existing_order_reserve=Decimal("0.25"),
            clock=self.clock,
        )
        self.ledger = IbkrExecutionLedger(
            Path(self.tmp.name) / "concrete.sqlite3",
            account_fingerprint=BINDING,
            environment="paper",
            client_id=CLIENT,
        )
        self.addCleanup(self.ledger.close)
        self.sdk = FakeSdkClient()
        self.authority = IbkrDispatchAuthority(
            ledger=self.ledger,
            authorization_binding_id=AUTH,
            provider_contract_id=CONTRACT,
            policy_binding_id=POLICY,
            verify_acceptance=lambda evidence: None,
            revalidate=self.preflight.revalidate,
            authorize_cancel=self.preflight.authorize_cancel,
            clock=self.clock,
        )
        self.session = IbkrSdkSession(
            client=self.sdk,
            sdk_version="10.50.2",
            expected_account=ACCOUNT,
            account_binding_fingerprint=BINDING,
            environment="paper",
            client_id=CLIENT,
            order_cancel_factory=SimpleNamespace,
            mutation_interlock=lambda: None,
            authorize_dispatch=self.authority,
            clock=self.clock,
        )
        generation = self.session.connect()
        self.session.observe_next_valid_id(100, generation=generation)
        self.session.observe_managed_accounts((ACCOUNT,), generation=generation)
        self.session.authorize_writes(
            IbkrWriteEvidence(
                authorization_binding_id=AUTH,
                account_binding_fingerprint=BINDING,
                environment="paper",
                client_id=CLIENT,
                reviewed_contract_id=CONTRACT,
                issued_at=NOW,
                expires_at=NOW + timedelta(minutes=1),
            )
        )
        descriptor = attended_ibkr_descriptor(
            exact_account_id=ACCOUNT,
            account_masked=MASK,
            account_binding_fingerprint=BINDING,
            authorization_binding_id=AUTH,
            coverage=complete_coverage(),
        )
        self.transport = IbkrProductionTransport(
            descriptor=descriptor,
            session=self.session,
            ledger=self.ledger,
            preflight=self.preflight,
            contract_factory=SimpleNamespace,
            order_factory=SimpleNamespace,
            policy_binding_id=POLICY,
            provider_contract_id=CONTRACT,
            authority=self.authority,
            clock=self.clock,
        )

    def account_snapshot(self):
        self.snapshot_reads += 1
        return AccountSnapshot(
            account_masked=MASK,
            observed_at=self.current,
            received_at=self.current,
            account_state="connected",
            account_type="CASH",
            funds=FundsSnapshot(
                total_value=Decimal("1000"),
                cash=Decimal("500"),
                buying_power=Decimal("500"),
                unleveraged_buying_power=Decimal("500"),
            ),
            equity_positions=(),
            equity_orders=tuple(self.visible_orders),
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
            daily_realized_pnl_complete=True,
            risk_evidence_authoritative=True,
            risk_evidence_source="ibkr:reqPnL.realizedPnL:current-day",
            risk_evidence_as_of=self.current,
        )

    def broker_order(self, state=BrokerOrderState.CONFIRMED):
        return OrderSnapshot(
            broker_order_id=f"ibkr:{CLIENT}:100",
            account_masked=MASK,
            symbol="TEST",
            side=BrokerSide.BUY,
            order_type=EquityOrderType.LIMIT,
            state=state,
            requested_quantity=Decimal("2"),
            cumulative_filled_quantity=Decimal("0"),
            market_hours=MarketHours.REGULAR,
            time_in_force=TimeInForce.GFD,
            broker_updated_at=self.current,
            received_at=self.current,
            limit_price=Decimal("10.00"),
            client_ref_id=self.request.client_ref_id,
        )

    def place(self):
        review = self.transport.review_equity_order(ACCOUNT, self.request)
        return review, self.transport.place_equity_order(
            ACCOUNT,
            self.request,
            review=review,
            explicit_confirmation=review.required_confirmation_phrase,
        )

    def reconcile_ack(self):
        self.visible_orders = (self.broker_order(),)
        self.ledger.record_event(
            self.request.client_ref_id,
            str(uuid4()),
            "ACK",
            perm_id=1000,
        )

    def test_concrete_reviewed_place_and_cancel_each_dispatch_exactly_once(self):
        place_review = self.transport.review_equity_order(ACCOUNT, self.request)
        placed = self.transport.place_equity_order(
            ACCOUNT,
            self.request,
            review=place_review,
            explicit_confirmation=place_review.required_confirmation_phrase,
        )
        self.assertEqual(placed.status, OperationStatus.UNKNOWN)
        self.assertEqual([call[0] for call in self.sdk.calls], ["submit"])
        with self.assertRaises(BrokerMutationBlocked):
            self.transport.place_equity_order(
                ACCOUNT,
                self.request,
                review=place_review,
                explicit_confirmation=place_review.required_confirmation_phrase,
            )
        self.assertEqual([call[0] for call in self.sdk.calls], ["submit"])

        self.reconcile_ack()
        target = self.transport.broker_order_id(100)
        cancel_review = self.transport.review_cancel_equity_order(ACCOUNT, target)
        with self.assertRaises(BrokerMutationBlocked):
            self.transport.cancel_equity_order(
                ACCOUNT,
                target,
                explicit_confirmation=cancel_review.required_confirmation_phrase + " ",
            )
        self.assertEqual([call[0] for call in self.sdk.calls], ["submit"])
        cancelled = self.transport.cancel_equity_order(
            ACCOUNT,
            target,
            explicit_confirmation=cancel_review.required_confirmation_phrase,
        )
        self.assertEqual(cancelled.status, OperationStatus.UNKNOWN)
        self.assertEqual([call[0] for call in self.sdk.calls], ["submit", "cancel"])
        with self.assertRaises(BrokerMutationBlocked):
            self.transport.cancel_equity_order(
                ACCOUNT,
                target,
                explicit_confirmation=cancel_review.required_confirmation_phrase,
            )
        self.assertEqual([call[0] for call in self.sdk.calls], ["submit", "cancel"])

    def test_expired_place_review_dispatches_nothing(self):
        review = self.transport.review_equity_order(ACCOUNT, self.request)
        self.current += timedelta(seconds=6)
        with self.assertRaises(BrokerMutationBlocked):
            self.transport.place_equity_order(
                ACCOUNT,
                self.request,
                review=review,
                explicit_confirmation=review.required_confirmation_phrase,
            )
        self.assertEqual(self.sdk.calls, [])

    def test_expired_cancel_review_dispatches_nothing(self):
        _review, _result = self.place()
        self.reconcile_ack()
        target = self.transport.broker_order_id(100)
        cancel_review = self.transport.review_cancel_equity_order(ACCOUNT, target)
        self.current += timedelta(seconds=16)
        with self.assertRaises(BrokerMutationBlocked):
            self.transport.cancel_equity_order(
                ACCOUNT,
                target,
                explicit_confirmation=cancel_review.required_confirmation_phrase,
            )
        self.assertEqual([call[0] for call in self.sdk.calls], ["submit"])

    def test_cancel_rejects_unknown_ledger_and_changed_broker_state(self):
        _review, _result = self.place()
        self.visible_orders = (self.broker_order(),)
        target = self.transport.broker_order_id(100)
        reads_before = self.snapshot_reads
        with self.assertRaisesRegex(BrokerMutationBlocked, "reconciled"):
            self.transport.review_cancel_equity_order(ACCOUNT, target)
        self.assertEqual(self.snapshot_reads, reads_before)
        self.assertEqual([call[0] for call in self.sdk.calls], ["submit"])

        self.ledger.record_event(
            self.request.client_ref_id, str(uuid4()), "ACK", perm_id=1000
        )
        cancel_review = self.transport.review_cancel_equity_order(ACCOUNT, target)
        self.visible_orders = (self.broker_order(BrokerOrderState.UNKNOWN),)
        with self.assertRaisesRegex(BrokerMutationBlocked, "TERMINAL_AMBIGUOUS"):
            self.transport.cancel_equity_order(
                ACCOUNT,
                target,
                explicit_confirmation=cancel_review.required_confirmation_phrase,
            )
        self.assertEqual([call[0] for call in self.sdk.calls], ["submit"])


if __name__ == "__main__":
    unittest.main()
