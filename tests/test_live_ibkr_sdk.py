"""Offline contract tests: injected SDK only; socket creation is forbidden."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from titan_brain.live.broker.base import BrokerMutationBlocked, BrokerUnknownSubmission
from titan_brain.live.broker.ibkr_sdk import (
    IBKR_SDK_SEMANTIC_MEMBERS, IbkrSdkSession, IbkrWriteEvidence,
    assert_sdk_order_matches_plan, sdk_order_fingerprint,
)


NOW = datetime(2026, 9, 14, 2, tzinfo=timezone.utc)
ACCOUNT = "U1234567"  # Synthetic fixture, not an actual account.
FINGERPRINT = "a" * 64


class FakeConnection:
    def __init__(self):
        self.messages = []
        self.failure = None
        self.short = False

    def sendMsg(self, payload):
        self.messages.append(payload)
        if self.failure:
            raise self.failure
        return len(payload) - 1 if self.short else len(payload)


class FakeSdk:
    def __init__(self, *, protobuf=False):
        self.connected = False
        self.conn = None
        self.connects = []
        self.orders = []
        self.cancels = []
        self.protobuf = protobuf
        self.behavior = None
        self.connectOptions = None

    def isConnected(self):
        return self.connected

    def connect(self, host, port, clientId):
        self.connects.append((host, port, clientId))
        self.host, self.port, self.clientId = host, port, clientId
        self.conn = FakeConnection()
        self.connected = True
        if self.protobuf:
            self.sendMsgProtoBuf(271, b"start")
        else:
            self.sendMsg(71, "start")

    def disconnect(self):
        self.connected = False

    def sendMsg(self, msgId, msg):
        self.conn.sendMsg(str(msgId).encode() + b":" + msg.encode())

    def sendMsgProtoBuf(self, msgId, msg):
        self.conn.sendMsg(str(msgId).encode() + b":" + msg)

    def placeOrder(self, orderId, contract, order):
        self.orders.append((orderId, contract, order))
        if self.behavior:
            return self.behavior(self, orderId, contract, order)
        if self.protobuf:
            self.sendMsgProtoBuf(203, b"order")
        else:
            self.sendMsg(3, "order")

    def cancelOrder(self, orderId, orderCancel):
        self.cancels.append((orderId, orderCancel))
        if self.protobuf:
            self.sendMsgProtoBuf(204, b"cancel")
        else:
            self.sendMsg(4, "cancel")


class IbkrSdkTests(unittest.TestCase):
    def setUp(self):
        self.network = patch("socket.socket", side_effect=AssertionError("network forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.calls = []
        self.interlocks = 0
        self.now = NOW

    def interlock(self):
        self.interlocks += 1

    def authorizer(self, request, evidence):
        self.calls.append((request, evidence))

    def make(self, *, ready=True, armed=True, **overrides):
        sdk = overrides.pop("client", FakeSdk())
        args = dict(client=sdk, sdk_version="10.50.2", expected_account=ACCOUNT,
                    account_binding_fingerprint=FINGERPRINT, environment="live",
                    client_id=17, order_cancel_factory=lambda: SimpleNamespace(),
                    mutation_interlock=self.interlock, authorize_dispatch=self.authorizer,
                    clock=lambda: self.now)
        args.update(overrides)
        session = IbkrSdkSession(**args)
        if ready:
            generation = session.connect()
            session.observe_next_valid_id(101, generation=generation)
            session.observe_managed_accounts(args["expected_account"], generation=generation)
        if armed:
            session.authorize_writes(self.evidence(environment=args["environment"], client_id=args["client_id"]))
        return session, sdk

    def evidence(self, **overrides):
        args = dict(authorization_binding_id="b" * 64,
                    account_binding_fingerprint=FINGERPRINT, environment="live",
                    client_id=17, reviewed_contract_id="c" * 64,
                    issued_at=NOW - timedelta(minutes=1), expires_at=NOW + timedelta(minutes=1))
        args.update(overrides)
        return IbkrWriteEvidence(**args)

    def order(self, **overrides):
        return SimpleNamespace(**dict(dict(
            account=ACCOUNT, whatIf=False, orderId=101, clientId=17,
            action="BUY", orderType="LMT", totalQuantity=Decimal("2"),
            tif="DAY", outsideRth=False, lmtPrice=10.0,
            auxPrice=sys.float_info.max, orderRef="fixture-order-ref",
            transmit=True, includeOvernight=False, parentId=0, ocaGroup="",
            ocaType=0, conditions=[], conditionsCancelOrder=False,
            conditionsIgnoreRth=False, triggerMethod=0,
            overridePercentageConstraints=False, advancedErrorOverride="",
        ), **overrides))

    def contract(self, **overrides):
        return SimpleNamespace(**dict(dict(
            conId=265598, symbol="AAPL", secType="STK", currency="USD",
            exchange="SMART", primaryExchange="NASDAQ",
        ), **overrides))

    def plan(self):
        from uuid import UUID
        from titan_brain.live.broker.base import (
            BrokerSide, EquityOrderType, MarketHours, OrderRequest, TimeInForce,
        )
        from titan_brain.live.broker.ibkr_orders import IbkrContractIdentity, build_ibkr_order_plan
        request = OrderRequest(
            account_masked="****4567", symbol="AAPL", side=BrokerSide.BUY,
            order_type=EquityOrderType.LIMIT, quantity=2, market_hours=MarketHours.REGULAR,
            time_in_force=TimeInForce.GFD, client_ref_id=str(UUID(int=17)),
            limit_price=Decimal("10.00"),
        )
        return build_ibkr_order_plan(
            request, contract=IbkrContractIdentity(265598, "AAPL", "NASDAQ"),
            account_id=ACCOUNT, client_id=17, order_id=101, transmit=True,
        )

    def test_import_and_construction_do_not_import_sdk_or_connect(self):
        before = set(sys.modules)
        importlib.import_module("titan_brain.live.broker.ibkr_sdk")
        session, sdk = self.make(ready=False, armed=False)
        self.assertEqual(sdk.connects, [])
        self.assertFalse(any(name == "ibapi" or name.startswith("ibapi.") for name in set(sys.modules) - before))
        self.assertNotIn(ACCOUNT, repr(session))

    def test_pin_supported_version_environment_and_client(self):
        for overrides in ({"sdk_version": "10.30"}, {"environment": "other"},
                          {"client_id": 0}, {"client_id": True}, {"expected_account": "****4567"},
                          {"environment": "paper"}, {"account_binding_fingerprint": "bad"}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.make(ready=False, armed=False, **overrides)

    def test_exact_endpoint_and_metadata(self):
        session, sdk = self.make()
        self.assertEqual(sdk.connects, [("127.0.0.1", 4001, 17)])
        self.assertEqual((session.client_id, session.environment, session.next_valid_id), (17, "live", 101))
        self.assertEqual((session.api_host, session.api_port), ("127.0.0.1", 4001))
        self.assertEqual(session.account_binding_fingerprint, FINGERPRINT)
        session.assert_account(ACCOUNT)
        with self.assertRaises(BrokerMutationBlocked):
            session.assert_account("U9999999")

    def test_paper_port_and_account_are_independent(self):
        session, sdk = self.make(environment="paper", expected_account="DU1234567")
        self.assertEqual(sdk.connects, [("127.0.0.1", 4002, 17)])
        session.submit(101, self.contract(), self.order(account="DU1234567"))

    def test_reviewed_custom_live_loopback_port_is_bound(self):
        session, sdk = self.make(api_port=4000)
        self.assertEqual(sdk.connects, [("127.0.0.1", 4000, 17)])
        self.assertEqual((session.api_host, session.api_port), ("127.0.0.1", 4000))
        session.submit(101, self.contract(), self.order())

    def test_non_loopback_and_invalid_ports_are_rejected_before_connect(self):
        for overrides in (
            {"api_host": "localhost"},
            {"api_host": "::1"},
            {"api_host": "0.0.0.0"},
            {"api_host": "192.0.2.1"},
            {"api_port": True},
            {"api_port": 0},
            {"api_port": -1},
            {"api_port": 65_536},
            {"api_port": "4000"},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.make(ready=False, armed=False, **overrides)

    def test_endpoint_mutation_after_connect_revokes_write_readiness(self):
        session, sdk = self.make(api_port=4000)
        sdk.port = 4001
        with self.assertRaises(BrokerMutationBlocked):
            session.submit(101, self.contract(), self.order())
        self.assertEqual(sdk.orders, [])

    def test_default_deny_and_no_boolean_authority(self):
        session, sdk = self.make(armed=False, mutation_interlock=None, authorize_dispatch=None)
        with self.assertRaises(BrokerMutationBlocked):
            session.authorize_writes(self.evidence())
        with self.assertRaises(BrokerMutationBlocked):
            session.submit(101, self.contract(), self.order())
        with self.assertRaises(BrokerMutationBlocked):
            session.authorize_writes(True)
        self.assertEqual(sdk.orders, [])

    def test_requires_both_handshake_callbacks_and_single_exact_account(self):
        session, sdk = self.make(ready=False, armed=False)
        generation = session.connect()
        with self.assertRaises(BrokerMutationBlocked):
            session.authorize_writes(self.evidence())
        session.observe_next_valid_id(101, generation=generation)
        with self.assertRaises(BrokerMutationBlocked):
            session.authorize_writes(self.evidence())
        for accounts in ("", "U9999999", ACCOUNT + ",U9999999", ACCOUNT + ",", (ACCOUNT, ACCOUNT)):
            with self.subTest(accounts=accounts), self.assertRaises(BrokerMutationBlocked):
                session.observe_managed_accounts(accounts, generation=generation)
        self.assertIsNone(session.next_valid_id)
        self.assertEqual(sdk.orders, [])

    def test_stale_generation_cannot_establish_identity(self):
        session, sdk = self.make()
        first = session.generation
        session.disconnect()
        second = session.connect()
        session.observe_next_valid_id(101, generation=first)
        session.observe_managed_accounts(ACCOUNT, generation=first)
        self.assertIsNone(session.next_valid_id)
        with self.assertRaises(BrokerMutationBlocked):
            session.authorize_writes(self.evidence())
        session.observe_next_valid_id(101, generation=second)
        session.observe_managed_accounts(ACCOUNT, generation=second)
        with self.assertRaises(BrokerMutationBlocked):
            session.submit(101, self.contract(), self.order())

    def test_next_id_only_moves_up_and_invalid_id_revokes(self):
        session, _ = self.make()
        session.observe_next_valid_id(100, generation=session.generation)
        self.assertEqual(session.next_valid_id, 101)
        with self.assertRaises(BrokerMutationBlocked):
            session.observe_next_valid_id(True, generation=session.generation)
        self.assertIsNone(session.next_valid_id)

    def test_legacy_and_protobuf_submit_cancel_are_handoff_only(self):
        for protobuf in (False, True):
            with self.subTest(protobuf=protobuf):
                session, sdk = self.make(client=FakeSdk(protobuf=protobuf))
                self.calls.clear()
                self.interlocks = 0
                receipt = session.submit(101, self.contract(), self.order())
                self.assertTrue(receipt.message_sent)
                self.assertEqual(receipt.operation, "submit")
                self.assertFalse(hasattr(receipt, "accepted"))
                cancel = session.cancel(101)
                self.assertTrue(cancel.message_sent)
                self.assertEqual([call[0].operation for call in self.calls], ["submit", "submit", "cancel", "cancel"])
                self.assertEqual(self.interlocks, 4)
                self.assertEqual(len(sdk.conn.messages), 3)
                self.assertEqual(session.next_valid_id, 102)

    def test_cancel_ownership_is_checked_not_inferred(self):
        def own_only(request, evidence):
            if request.operation == "cancel" and request.order_id != 101:
                raise ValueError("private account detail")
        session, sdk = self.make(authorize_dispatch=own_only)
        session.observe_next_valid_id(999, generation=session.generation)
        with self.assertRaises(BrokerMutationBlocked):
            session.cancel(999)
        self.assertEqual(sdk.cancels, [])
        session.cancel(101)

    def test_used_id_is_never_a_modify(self):
        session, sdk = self.make()
        session.submit(101, self.contract(), self.order())
        with self.assertRaises(BrokerMutationBlocked):
            session.submit(101, self.contract(), self.order())
        self.assertEqual(len(sdk.orders), 1)

    def test_evidence_binding_and_dates_fail_closed(self):
        for changes in ({"account_binding_fingerprint": "d" * 64}, {"environment": "paper"},
                        {"client_id": 99}, {"expires_at": NOW}, {"issued_at": NOW + timedelta(seconds=1)}):
            with self.subTest(changes=changes):
                session, sdk = self.make(armed=False)
                with self.assertRaises(BrokerMutationBlocked):
                    session.authorize_writes(self.evidence(**changes))
                self.assertEqual(sdk.orders, [])

    def test_wrong_account_and_whatif_never_reach_sdk(self):
        for order in (self.order(account="U9999999"), self.order(whatIf=True), SimpleNamespace(account=ACCOUNT)):
            with self.subTest(order=order):
                session, sdk = self.make()
                with self.assertRaises(BrokerMutationBlocked):
                    session.submit(101, self.contract(), order)
                self.assertEqual(sdk.orders, [])

    def test_write_receipt_bindings_require_active_exact_evidence(self):
        session, sdk = self.make()
        session.assert_write_binding("b" * 64, "c" * 64)
        for authorization, contract in (("d" * 64, "c" * 64), ("b" * 64, "d" * 64)):
            with self.assertRaises(BrokerMutationBlocked):
                session.assert_write_binding(authorization, contract)
        self.now = NOW + timedelta(minutes=2)
        with self.assertRaises(BrokerMutationBlocked):
            session.assert_write_binding("b" * 64, "c" * 64)
        self.assertEqual(sdk.orders, [])

    def test_dispatch_authorizer_requires_actual_callable_identity(self):
        authorizer = self.authorizer
        session, sdk = self.make(authorize_dispatch=authorizer)
        session.assert_dispatch_authorizer(authorizer)
        for different in (None, True, lambda *args: None, self.authorizer):
            with self.assertRaises(BrokerMutationBlocked):
                session.assert_dispatch_authorizer(different)
        self.assertEqual(sdk.orders, [])

    def test_fingerprint_is_stable_normalized_and_uses_opaque_account(self):
        contract, order = self.contract(), self.order()
        fingerprint = sdk_order_fingerprint(contract, order, FINGERPRINT)
        self.assertRegex(fingerprint, r"^[0-9a-f]{64}$")
        self.assertEqual(fingerprint, sdk_order_fingerprint(self.contract(), self.order(totalQuantity=2, lmtPrice=10), FINGERPRINT))
        self.assertEqual(fingerprint, sdk_order_fingerprint(contract, self.order(account="U7654321"), FINGERPRINT))
        self.assertNotEqual(fingerprint, sdk_order_fingerprint(contract, order, "d" * 64))
        self.assertNotIn(ACCOUNT, fingerprint)

    def test_authorizer_sees_exact_submit_payload_but_cancel_has_none(self):
        session, sdk = self.make()
        contract, order = self.contract(), self.order()
        fingerprint = sdk_order_fingerprint(contract, order, FINGERPRINT)
        session.submit(101, contract, order)
        self.assertEqual([call[0].payload_fingerprint for call in self.calls], [fingerprint, fingerprint])
        session.cancel(101)
        self.assertIsNone(self.calls[-1][0].payload_fingerprint)

    def test_order_payload_mutation_before_wire_denied(self):
        mutations = {
            "account": "U7654321", "lmtPrice": 11.0, "orderRef": "changed",
            "action": "SELL", "totalQuantity": Decimal(3), "tif": "GTC",
            "outsideRth": True, "clientId": 18, "orderId": 102,
            "transmit": False, "overridePercentageConstraints": True,
            "advancedErrorOverride": "8229", "includeOvernight": True,
            "triggerMethod": 4, "parentId": 77, "ocaGroup": "foreign",
            "conditions": [SimpleNamespace()], "whatIf": True,
            "conditionsIncludeOvernight": True, "cashQty": 200.0,
            "algoStrategy": "Adaptive", "customerAccount": "U7654321",
            "extraFutureSetting": True,
        }
        for field, value in mutations.items():
            with self.subTest(field=field):
                session, sdk = self.make()
                def mutate(s, i, c, o):
                    setattr(o, field, value)
                    s.sendMsg(3, "order")
                sdk.behavior = mutate
                with self.assertRaises(BrokerMutationBlocked):
                    session.submit(101, self.contract(), self.order())
                self.assertEqual(len(sdk.conn.messages), 1)

    def test_contract_payload_mutation_before_wire_denied(self):
        mutations = {
            "conId": 265599, "symbol": "MSFT", "primaryExchange": "NYSE",
            "currency": "CAD", "exchange": "OVERNIGHT", "secType": "OPT",
            "localSymbol": "other", "includeExpired": True,
        }
        for field, value in mutations.items():
            with self.subTest(field=field):
                session, sdk = self.make()
                def mutate(s, i, c, o):
                    setattr(c, field, value)
                    s.sendMsg(3, "order")
                sdk.behavior = mutate
                with self.assertRaises(BrokerMutationBlocked):
                    session.submit(101, self.contract(), self.order())
                self.assertEqual(len(sdk.conn.messages), 1)

    def test_callback_cannot_mutate_price_after_payload_check(self):
        session, sdk = self.make()
        contract, order = self.contract(), self.order()
        count = 0
        def mutate(request, evidence):
            nonlocal count
            count += 1
            if count == 2:
                order.lmtPrice = 12.0
        session._authorizer = mutate
        with self.assertRaises(BrokerMutationBlocked):
            session.submit(101, contract, order)
        self.assertEqual(len(sdk.conn.messages), 1)

    def test_payload_requires_strict_types_and_all_core_fields(self):
        for change in ({"totalQuantity": True}, {"totalQuantity": Decimal("1.5")},
                       {"lmtPrice": float("nan")}, {"lmtPrice": float("inf")},
                       {"tif": "IOC"}, {"orderRef": "bad\x00ref"}, {"transmit": 1}):
            with self.subTest(change=change), self.assertRaises(BrokerMutationBlocked):
                sdk_order_fingerprint(self.contract(), self.order(**change), FINGERPRINT)
        order = self.order()
        del order.orderRef
        with self.assertRaises(BrokerMutationBlocked):
            sdk_order_fingerprint(self.contract(), order, FINGERPRINT)

    def test_optional_default_fields_are_bound_even_when_not_special_cased(self):
        contract, order = self.contract(), self.order()
        order.hidden = False
        before = sdk_order_fingerprint(contract, order, FINGERPRINT)
        order.hidden = True
        self.assertNotEqual(before, sdk_order_fingerprint(contract, order, FINGERPRINT))

    def test_official_sdk_data_objects_only_when_sdk_is_available(self):
        # Run explicitly with the official isolated SDK venv. Import only the
        # data classes, never EClient/EWrapper or networking; sockets remain
        # patched by setUp throughout import and object construction.
        try:
            from ibapi import __version__
            from ibapi.contract import Contract
            from ibapi.order import Order
        except ImportError:
            self.skipTest("official SDK data classes are not installed in this interpreter")
        self.assertEqual(__version__, "10.50.2")
        contract, order = Contract(), Order()
        for name, value in vars(self.contract()).items():
            setattr(contract, name, value)
        for name, value in vars(self.order()).items():
            setattr(order, name, value)
        fingerprint = sdk_order_fingerprint(contract, order, FINGERPRINT)
        self.assertRegex(fingerprint, r"^[0-9a-f]{64}$")
        self.assertNotIn("ibapi.client", sys.modules)
        session, sdk = self.make()
        session.submit(101, contract, order)
        self.assertEqual(self.calls[-1][0].payload_fingerprint, fingerprint)

    def test_release_inventory_includes_actual_executable_leaves(self):
        session, sdk = self.make()
        leaves = {role: (component, members) for role, component, members in session.release_components()}
        expected = {
            "ibkr_sdk_client": sdk, "ibkr_sdk_cancel_factory": session._cancel_factory,
            "ibkr_sdk_mutation_interlock": session._interlock,
            "ibkr_sdk_dispatch_authorizer": session._authorizer,
            "ibkr_sdk_clock": session._clock,
        }
        self.assertEqual(set(leaves), set(expected))
        for role, component in expected.items():
            self.assertIs(leaves[role][0], component)
            self.assertTrue(leaves[role][1])
        self.assertTrue({"submit", "cancel", "_wire_send", "_check_write", "_assert_payload",
                         "authorize_writes", "assert_dispatch_authorizer",
                         "command_lane_status", "release_components"}
                        <= set(IBKR_SDK_SEMANTIC_MEMBERS))

    def test_command_lane_status_proves_handshake_without_authorizing_writes(self):
        session, sdk = self.make(armed=False)

        status = session.command_lane_status()

        self.assertTrue(status.command_connected)
        self.assertTrue(status.next_valid_id_received)
        self.assertTrue(status.account_authenticated)
        self.assertFalse(status.write_authority_granted)
        self.assertEqual(sdk.orders, [])
        self.assertEqual(sdk.cancels, [])
        with self.assertRaisesRegex(
            BrokerMutationBlocked, "IBKR_REVIEWED_WRITE_EVIDENCE_REQUIRED"
        ):
            session.validate_mutation_interlock()

    def test_plan_match_derives_digest_without_trusting_caller_hash(self):
        plan = self.plan()
        contract, order = plan.to_sdk(contract_factory=SimpleNamespace, order_factory=SimpleNamespace)
        self.assertEqual(assert_sdk_order_matches_plan(plan, contract, order, FINGERPRINT),
                         sdk_order_fingerprint(contract, order, FINGERPRINT))
        order.totalQuantity = Decimal(200)
        # A self-consistent digest for 200 shares is valid shape, but it is not
        # authority to transmit a different quantity than the two-share plan.
        self.assertRegex(sdk_order_fingerprint(contract, order, FINGERPRINT), r"^[0-9a-f]{64}$")
        with self.assertRaises(BrokerMutationBlocked):
            assert_sdk_order_matches_plan(plan, contract, order, FINGERPRINT)

    def test_plan_match_refuses_wrong_core_or_unreviewed_controls(self):
        order_changes = {
            "totalQuantity": Decimal(200), "action": "SELL", "lmtPrice": 20.0,
            "account": "U7654321", "orderRef": "changed", "orderId": 102,
            "clientId": 18, "outsideRth": True, "transmit": False,
            "overridePercentageConstraints": True, "includeOvernight": True,
            "hidden": True, "allOrNone": True, "minQty": 1, "cashQty": 200.0,
            "futureUnknownField": False, "algoStrategy": "Adaptive",
            "parentId": 50, "conditions": [], "auxPrice": 1.0,
        }
        # Empty conditions are the reviewed default; an active condition isn't.
        order_changes["conditions"] = [SimpleNamespace()]
        for field, value in order_changes.items():
            with self.subTest(field=field):
                plan = self.plan()
                contract, order = plan.to_sdk(contract_factory=SimpleNamespace, order_factory=SimpleNamespace)
                setattr(order, field, value)
                with self.assertRaises(BrokerMutationBlocked):
                    assert_sdk_order_matches_plan(plan, contract, order, FINGERPRINT)
        for field, value in {"symbol": "MSFT", "conId": 123, "primaryExchange": "NYSE",
                             "exchange": "OVERNIGHT", "strike": 5.0, "includeExpired": True}.items():
            with self.subTest(field=field):
                plan = self.plan()
                contract, order = plan.to_sdk(contract_factory=SimpleNamespace, order_factory=SimpleNamespace)
                setattr(contract, field, value)
                with self.assertRaises(BrokerMutationBlocked):
                    assert_sdk_order_matches_plan(plan, contract, order, FINGERPRINT)

    def test_plan_match_tolerates_only_missing_optional_inert_defaults(self):
        plan = self.plan()
        contract, order = plan.to_sdk(contract_factory=SimpleNamespace, order_factory=SimpleNamespace)
        self.assertFalse(hasattr(order, "auxPrice"))
        assert_sdk_order_matches_plan(plan, contract, order, FINGERPRINT)
        order.auxPrice = sys.float_info.max
        order.hidden = False
        order.minQty = 2_147_483_647
        assert_sdk_order_matches_plan(plan, contract, order, FINGERPRINT)
        del order.transmit
        with self.assertRaises(BrokerMutationBlocked):
            assert_sdk_order_matches_plan(plan, contract, order, FINGERPRINT)

    def test_pinned_defaults_exactly_match_official_sdk_when_available(self):
        try:
            from ibapi import __version__
            from ibapi.contract import Contract
            from ibapi.order import Order
        except ImportError:
            self.skipTest("official SDK data classes are not installed in this interpreter")
        from titan_brain.live.broker.ibkr_sdk import _canonical_value, _pinned_sdk_defaults
        self.assertEqual(__version__, "10.50.2")
        expected_contract, expected_order = _pinned_sdk_defaults()
        self.assertEqual(expected_contract, _canonical_value(vars(Contract())))
        self.assertEqual(expected_order, _canonical_value(vars(Order())))
        plan = self.plan()
        contract, order = plan.to_sdk(contract_factory=Contract, order_factory=Order)
        self.assertEqual(assert_sdk_order_matches_plan(plan, contract, order, FINGERPRINT),
                         sdk_order_fingerprint(contract, order, FINGERPRINT))
        order.hidden = True
        with self.assertRaises(BrokerMutationBlocked):
            assert_sdk_order_matches_plan(plan, contract, order, FINGERPRINT)

    def test_authorizer_cannot_mutate_account_at_wire(self):
        session, sdk = self.make()
        order = self.order()
        count = 0
        def mutate(request, evidence):
            nonlocal count
            count += 1
            if count == 2:
                order.account = "U9999999"
        session._authorizer = mutate
        with self.assertRaises(BrokerMutationBlocked):
            session.submit(101, self.contract(), order)
        self.assertEqual(len(sdk.conn.messages), 1)

    def test_mutation_between_sdk_entry_and_wire_is_denied(self):
        for behavior in (lambda s, i, c, o: setattr(o, "whatIf", True),
                         lambda s, i, c, o: setattr(o, "account", "U9999999"),
                         lambda s, i, c, o: setattr(s, "host", "remote.invalid"),
                         lambda s, i, c, o: setattr(s, "connected", False)):
            with self.subTest(behavior=behavior):
                session, sdk = self.make()
                def mutate_then_send(s, i, c, o):
                    behavior(s, i, c, o)
                    s.sendMsg(3, "order")
                sdk.behavior = mutate_then_send
                with self.assertRaises(BrokerMutationBlocked):
                    session.submit(101, self.contract(), self.order())
                self.assertEqual(len(sdk.conn.messages), 1)

    def test_expiry_at_wire_and_interlock_at_wire_are_rechecked(self):
        session, sdk = self.make()
        def expire(s, i, c, o):
            self.now = NOW + timedelta(minutes=2)
            s.sendMsg(3, "order")
        sdk.behavior = expire
        with self.assertRaises(BrokerMutationBlocked):
            session.submit(101, self.contract(), self.order())
        self.assertEqual(len(sdk.conn.messages), 1)
        self.now = NOW
        def deny_second():
            self.interlocks += 1
            if self.interlocks == 2:
                raise RuntimeError(ACCOUNT)
        self.interlocks = 0
        session, sdk = self.make(mutation_interlock=deny_second)
        with self.assertRaises(BrokerMutationBlocked):
            session.submit(101, self.contract(), self.order())
        self.assertEqual(len(sdk.conn.messages), 1)

    def test_authorizer_revocation_and_boolean_return_denied(self):
        for factory in (lambda session: lambda r, e: session.revoke_writes(),
                        lambda session: lambda r, e: True):
            session, sdk = self.make()
            session._authorizer = factory(session)
            with self.assertRaises(BrokerMutationBlocked):
                session.submit(101, self.contract(), self.order())
            self.assertEqual(sdk.orders, [])

    def test_unexpected_messages_and_raw_writes_are_blocked(self):
        session, sdk = self.make()
        for message_id in (1, 2, 7, 8, 49, 58, 61, 62, 63, 71, 76, 77, 203, 204, 999):
            with self.subTest(message_id=message_id), self.assertRaises(BrokerMutationBlocked):
                sdk.sendMsg(message_id, "forbidden")
        with self.assertRaises(BrokerMutationBlocked):
            sdk.conn.sendMsg(b"forbidden")
        self.assertEqual(len(sdk.conn.messages), 1)

    def test_surprise_message_during_submit_never_leaks(self):
        for protobuf in (False, True):
            session, sdk = self.make(client=FakeSdk(protobuf=protobuf))
            sdk.behavior = lambda s, i, c, o: s.sendMsgProtoBuf(258, b"global cancel") if protobuf else s.sendMsg(58, "global cancel")
            with self.assertRaises(BrokerMutationBlocked):
                session.submit(101, self.contract(), self.order())
            self.assertEqual(len(sdk.conn.messages), 1)

    def test_direct_raw_send_during_submit_is_blocked(self):
        session, sdk = self.make()
        sdk.behavior = lambda s, i, c, o: s.conn.sendMsg(b"surprise")
        with self.assertRaises(BrokerMutationBlocked):
            session.submit(101, self.contract(), self.order())
        self.assertEqual(len(sdk.conn.messages), 1)

    def test_sdk_swallowed_guard_failure_still_denied(self):
        session, sdk = self.make()
        def swallow(s, i, c, o):
            try:
                s.sendMsg(1, "paid quote")
            except Exception:
                pass
        sdk.behavior = swallow
        with self.assertRaises(BrokerMutationBlocked):
            session.submit(101, self.contract(), self.order())
        self.assertEqual(len(sdk.conn.messages), 1)

    def test_forbidden_attempt_cannot_fall_through_to_valid_send(self):
        session, sdk = self.make()
        def swallow_then_send(s, i, c, o):
            try:
                s.sendMsg(1, "paid quote")
            except Exception:
                pass
            s.sendMsg(3, "order")
        sdk.behavior = swallow_then_send
        with self.assertRaises(BrokerMutationBlocked):
            session.submit(101, self.contract(), self.order())
        self.assertEqual(len(sdk.conn.messages), 1)

    def test_silent_sdk_validation_failure_is_not_acceptance(self):
        session, sdk = self.make()
        sdk.behavior = lambda *args: None
        with self.assertRaises(BrokerMutationBlocked):
            session.submit(101, self.contract(), self.order())
        self.assertEqual(session.next_valid_id, 102)

    def test_duplicate_wire_send_is_unknown_without_second_send(self):
        session, sdk = self.make()
        def twice(s, i, c, o):
            s.sendMsg(3, "first")
            s.sendMsg(3, "second")
        sdk.behavior = twice
        with self.assertRaises(BrokerUnknownSubmission):
            session.submit(101, self.contract(), self.order())
        self.assertEqual(len(sdk.conn.messages), 2)

    def test_socket_failure_and_short_send_remain_unknown(self):
        for short in (False, True):
            session, sdk = self.make()
            sdk.conn.short = short
            if not short:
                sdk.conn.failure = RuntimeError(ACCOUNT)
            with self.assertRaises(BrokerUnknownSubmission) as caught:
                session.submit(101, self.contract(), self.order())
            self.assertNotIn(ACCOUNT, str(caught.exception))
            self.assertTrue(caught.exception.__suppress_context__)

    def test_order_exception_before_socket_is_sanitized(self):
        session, sdk = self.make()
        def fail(*args):
            raise RuntimeError(ACCOUNT)
        sdk.behavior = fail
        with self.assertRaises(BrokerMutationBlocked) as caught:
            session.submit(101, self.contract(), self.order())
        self.assertNotIn(ACCOUNT, str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)

    def test_connection_loss_revokes_authority(self):
        session, sdk = self.make()
        session.observe_connection_closed(generation=session.generation)
        with self.assertRaises(BrokerMutationBlocked):
            session.cancel(101)
        self.assertEqual(sdk.cancels, [])

    def test_connection_check_exception_is_sanitized(self):
        session, sdk = self.make()
        def broken():
            raise RuntimeError(ACCOUNT)
        sdk.isConnected = broken
        with self.assertRaises(BrokerMutationBlocked) as caught:
            session.assert_write_binding("b" * 64, "c" * 64)
        self.assertNotIn(ACCOUNT, str(caught.exception))
        self.assertEqual(sdk.orders, [])

    def test_guard_replacement_is_not_trusted(self):
        for target in ("legacy", "raw"):
            session, sdk = self.make()
            if target == "legacy":
                sdk.sendMsg = lambda *args: None
            else:
                sdk.conn.sendMsg = lambda *args: None
            with self.assertRaises(BrokerMutationBlocked):
                session.submit(101, self.contract(), self.order())
            self.assertEqual(sdk.orders, [])

    def test_reentrant_callback_cannot_submit(self):
        session, sdk = self.make()
        session._authorizer = lambda r, e: session.submit(102, self.contract(), self.order())
        with self.assertRaises(BrokerMutationBlocked):
            session.submit(101, self.contract(), self.order())
        self.assertEqual(sdk.orders, [])

    def test_unreviewed_connection_options_refused(self):
        sdk = FakeSdk()
        sdk.connectOptions = "+PACEAPI"
        session, _ = self.make(client=sdk, ready=False, armed=False)
        with self.assertRaises(BrokerMutationBlocked):
            session.connect()
        self.assertEqual(sdk.connects, [])


if __name__ == "__main__":
    unittest.main()
