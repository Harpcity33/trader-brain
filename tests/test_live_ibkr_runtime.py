"""Offline tests for the concrete official-SDK runtime composition."""

from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from types import SimpleNamespace
import sys
import time
import unittest
from unittest.mock import patch

from titan_brain.live.broker.ibkr_runtime import (
    IbkrOfficialRuntime,
    IbkrRuntimeError,
    _AttestedSdkBundle,
    _COMMAND_CALLBACK_MEMBERS,
    _READ_CALLBACK_MEMBERS,
)
from titan_brain.live.provider_profile import (
    IbkrLocalProviderProfile,
    InstalledSdkAttestation,
)


NOW = datetime(2026, 9, 14, 18, 0, tzinfo=timezone.utc)
SYNTHETIC_ACCOUNT = "U9993103"


class FakeConnection:
    def __init__(self):
        self.payloads = []

    def sendMsg(self, payload):
        self.payloads.append(payload)
        return len(payload)


class FakeWrapper:
    pass


class FakeContract:
    pass


class FakeOrder:
    pass


class FakeExecutionFilter:
    pass


class FakeOrderCancel:
    pass


class FakeEClient:
    instances = []
    accounts = SYNTHETIC_ACCOUNT
    emit_error = False
    emit_new_error_signature = False
    emit_new_read_error_signature = False
    emit_completed_orders_error = False
    emit_order = False
    suppress_callbacks = False

    def __init__(self, wrapper):
        self.wrapper = wrapper
        self.connected = False
        self.host = None
        self.port = None
        self.clientId = None
        self.conn = None
        self.stopped = Event()
        self.calls = []
        self.mutations = []
        self.market_data = []
        self.connectOptions = None
        self.optCapab = None
        self.extraAuth = False
        self.asynchronous = False
        type(self).instances.append(self)

    def connect(self, host, port, clientId):
        self.host, self.port, self.clientId = host, port, clientId
        self.conn = FakeConnection()
        self.connected = True
        self.startApi()

    def startApi(self):
        # Official synchronous handshake encoder call. The command session
        # permits exactly this one message before installing the raw guard.
        self.calls.append(("startApi",))
        self.sendMsg(71, "start")

    def reqOpenOrders(self):
        self.mutations.append(("bind_existing_manual_orders",))
        self.sendMsg(5, "bind")

    def reqOpenOrdersProtoBuf(self, request):
        self.mutations.append(("bind_existing_manual_orders_proto",))
        self.sendMsgProtoBuf(205, b"bind")

    def reqAutoOpenOrders(self, auto_bind):
        self.mutations.append(("bind_future_manual_orders", auto_bind))
        self.sendMsg(15, "bind")

    def reqAutoOpenOrdersProtoBuf(self, request):
        self.mutations.append(("bind_future_manual_orders_proto",))
        self.sendMsgProtoBuf(215, b"bind")

    def disconnect(self):
        self.connected = False
        self.stopped.set()

    def isConnected(self):
        return self.connected

    def sendMsg(self, msg_id, msg):
        payload = f"{msg_id}:{msg}".encode("ascii")
        return self.conn.sendMsg(payload)

    def sendMsgProtoBuf(self, msg_id, msg):
        payload = str(msg_id).encode("ascii") + b":" + bytes(msg)
        return self.conn.sendMsg(payload)

    def run(self):
        if not type(self).suppress_callbacks:
            self.wrapper.connectAck()
            self.wrapper.managedAccounts(type(self).accounts)
            if self.clientId == 19735 and type(self).emit_new_read_error_signature:
                self.wrapper.error(
                    -1,
                    1789394400,
                    2104,
                    f"private broker text {SYNTHETIC_ACCOUNT}",
                    '{"account":"USECRET3103"}',
                )
            if self.clientId == 19736:
                self.wrapper.nextValidId(700)
                if type(self).emit_error:
                    arguments = (
                        41,
                        1789394400,
                        201,
                        f"private broker text {SYNTHETIC_ACCOUNT}",
                        '{"account":"USECRET3103"}',
                    ) if type(self).emit_new_error_signature else (
                        41,
                        201,
                        f"private broker text {SYNTHETIC_ACCOUNT}",
                        '{"account":"USECRET3103"}',
                    )
                    self.wrapper.error(*arguments)
                if type(self).emit_order:
                    self.wrapper.openOrder(
                        699,
                        SimpleNamespace(symbol="SECRET"),
                        SimpleNamespace(clientId=19736, account=SYNTHETIC_ACCOUNT),
                        SimpleNamespace(status="Submitted"),
                    )
        self.stopped.wait(2)

    def reqAccountSummary(self, reqId, groupName, tags):
        self.calls.append(("reqAccountSummary", reqId, groupName, tags))
        values = {
            "AccountType": ("CASH", ""),
            "NetLiquidation": ("10000", "USD"),
            "TotalCashValue": ("10000", "USD"),
            "AvailableFunds": ("10000", "USD"),
            "BuyingPower": ("10000", "USD"),
            "SettledCash": ("10000", "USD"),
        }
        for tag, (value, currency) in values.items():
            self.wrapper.accountSummary(reqId, SYNTHETIC_ACCOUNT, tag, value, currency)
        self.wrapper.accountSummaryEnd(reqId)

    def cancelAccountSummary(self, reqId):
        self.calls.append(("cancelAccountSummary", reqId))

    def reqPositions(self):
        self.calls.append(("reqPositions",))
        self.wrapper.positionEnd()

    def cancelPositions(self):
        self.calls.append(("cancelPositions",))

    def reqAllOpenOrders(self):
        self.calls.append(("reqAllOpenOrders",))
        self.wrapper.openOrderEnd()

    def reqCompletedOrders(self, apiOnly):
        self.calls.append(("reqCompletedOrders", apiOnly))
        if type(self).emit_completed_orders_error:
            self.wrapper.error(
                -1,
                1789394400,
                321,
                f"private broker text {SYNTHETIC_ACCOUNT}",
                '{"account":"USECRET3103"}',
            )
            return
        self.wrapper.completedOrdersEnd()

    def reqExecutions(self, reqId, execution_filter):
        self.calls.append(("reqExecutions", reqId, type(execution_filter).__name__))
        self.wrapper.execDetailsEnd(reqId)

    def reqPnL(self, reqId, account, modelCode):
        self.calls.append(("reqPnL", reqId, account, modelCode))
        self.wrapper.pnl(reqId, 0.0, 0.0, 0.0)

    def cancelPnL(self, reqId):
        self.calls.append(("cancelPnL", reqId))

    def reqContractDetails(self, reqId, contract):
        self.calls.append(("reqContractDetails", reqId, contract.symbol))
        resolved = SimpleNamespace(
            conId=756733,
            symbol=contract.symbol,
            secType="STK",
            currency="USD",
            exchange="SMART",
            primaryExchange="ARCA",
        )
        details = SimpleNamespace(
            contract=resolved,
            validExchanges="SMART,ARCA",
            liquidHours="20260914:0930-20260914:1600",
            timeZoneId="America/New_York",
        )
        self.wrapper.contractDetails(reqId, details)
        self.wrapper.contractDetailsEnd(reqId)

    def cancelContractDetails(self, reqId):
        self.calls.append(("cancelContractDetails", reqId))

    def reqMktData(self, *args):
        self.market_data.append(args)

    def placeOrder(self, orderId, contract, order):
        self.mutations.append(("place", orderId, contract, order))
        self.sendMsg(3, "order")

    def cancelOrder(self, orderId, order_cancel):
        self.mutations.append(("cancel", orderId, order_cancel))
        self.sendMsg(4, "cancel")


def profile(**overrides):
    values = dict(
        profile_id="ibkr-local-live-ending-3103-v1",
        account_key="ibkr-live-ending-3103",
        account_last4="3103",
        install_subtree="Application Support/Titan Momentum/full-live-ibkr-ending-3103",
        coordinator_launchd_label="com.harpcity.trader-brain-full-live-ibkr-3103",
        notification_launchd_label="com.harpcity.trader-brain-full-live-ibkr-3103-notifications",
        host="127.0.0.1",
        port=4001,
        read_client_id=19735,
        command_client_id=19736,
        environment="live",
        sdk_version="10.50.2",
        protobuf_version="5.29.5",
        sdk_inventory_hash="a" * 64,
    )
    values.update(overrides)
    return IbkrLocalProviderProfile(**values)


def bundle():
    return _AttestedSdkBundle(
        attestation=InstalledSdkAttestation(
            profile_id="ibkr-local-live-ending-3103-v1",
            import_root=Path("/synthetic/attested-sdk"),
            inventory_hash="a" * 64,
            receipt_hash="b" * 64,
            file_count=10,
            ibapi_version="10.50.2",
            protobuf_version="5.29.5",
        ),
        client_type=FakeEClient,
        wrapper_type=FakeWrapper,
        contract_type=FakeContract,
        order_type=FakeOrder,
        execution_filter_type=FakeExecutionFilter,
        order_cancel_type=FakeOrderCancel,
    )


class IbkrOfficialRuntimeTests(unittest.TestCase):
    def setUp(self):
        FakeEClient.instances = []
        FakeEClient.accounts = SYNTHETIC_ACCOUNT
        FakeEClient.emit_error = False
        FakeEClient.emit_new_error_signature = False
        FakeEClient.emit_new_read_error_signature = False
        FakeEClient.emit_completed_orders_error = False
        FakeEClient.emit_order = False
        FakeEClient.suppress_callbacks = False
        self.load = patch(
            "titan_brain.live.broker.ibkr_runtime._load_attested_sdk",
            return_value=bundle(),
        )
        self.loader = self.load.start()
        self.addCleanup(self.load.stop)
        self.runtimes = []

    def tearDown(self):
        for runtime in self.runtimes:
            try:
                runtime.stop()
            except Exception:
                pass

    def make(self, **kwargs):
        runtime = IbkrOfficialRuntime(
            profile=kwargs.pop("profile", profile()),
            install_root=Path("/does/not/need/to/exist"),
            connect_timeout_seconds=kwargs.pop("connect_timeout_seconds", 0.25),
            shutdown_timeout_seconds=kwargs.pop("shutdown_timeout_seconds", 0.25),
            clock=kwargs.pop("clock", lambda: NOW),
            **kwargs,
        )
        self.runtimes.append(runtime)
        return runtime

    def test_import_and_construction_are_inert_and_redacted(self):
        before = {name for name in sys.modules if name == "ibapi" or name.startswith("ibapi.")}
        runtime = self.make()
        after = {name for name in sys.modules if name == "ibapi" or name.startswith("ibapi.")}
        self.assertEqual(before, after)
        self.loader.assert_not_called()
        self.assertEqual(FakeEClient.instances, [])
        self.assertNotIn(SYNTHETIC_ACCOUNT, repr(runtime))
        status = runtime.status()
        self.assertEqual(status.phase, "STAGED")
        self.assertFalse(status.connected)
        self.assertFalse(status.authenticated)
        self.assertFalse(status.write_authority_granted)

    def test_exact_reviewed_profile_is_required(self):
        for invalid in (
            profile(host="localhost"),
            profile(port=4000),
            profile(read_client_id=19736),
            profile(read_client_id=-1),
            profile(read_client_id=False),
            profile(command_client_id=0),
            profile(command_client_id=True),
            profile(command_client_id=2_147_483_647),
            profile(environment="paper"),
            profile(account_last4="9999"),
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.make(profile=invalid)
        self.loader.assert_not_called()

    def test_client_zero_support_is_inert_and_preserves_attended_reader(self):
        supported = profile(read_client_id=0)
        runtime = self.make(profile=supported)
        self.assertEqual(runtime.status().read_client_id, 0)
        self.assertFalse(runtime.status().authenticated)
        self.assertFalse(runtime.status().write_authority_granted)
        self.assertEqual(FakeEClient.instances, [])
        self.loader.assert_not_called()
        attended = supported.for_attended_command()
        self.assertEqual(attended.read_client_id, 19737)
        self.assertEqual(attended.command_client_id, 19736)
        self.make(profile=attended)

    def test_client_zero_read_connect_and_probe_never_bind_manual_orders(self):
        runtime = self.make(profile=profile(read_client_id=0))
        runtime.connect_reads()
        read = FakeEClient.instances[0]
        self.assertEqual(read.clientId, 0)
        self.assertEqual(read.calls, [("startApi",)])
        self.assertEqual(read.conn.payloads, [b"71:start"])
        requester = runtime._read_requester
        for member in (
            "reqOpenOrders", "reqOpenOrdersProtoBuf", "reqAutoOpenOrders",
            "reqAutoOpenOrdersProtoBuf", "placeOrder", "cancelOrder",
            "reqGlobalCancel", "reqMktData",
        ):
            self.assertFalse(hasattr(requester, member), member)
        result = runtime.probe_reads("SPY")
        self.assertEqual(result.phase, "CONNECTED")
        self.assertFalse(result.public_dict()["whole_broker_history_verified"])
        self.assertFalse(result.public_dict()["write_authority_granted"])
        self.assertIn("reqAllOpenOrders", {call[0] for call in read.calls})
        self.assertEqual(read.mutations, [])
        self.assertEqual(read.market_data, [])
        self.assertEqual(read.conn.payloads, [b"71:start"])

    def test_read_sdk_binding_entrypoints_fail_closed_even_during_startup(self):
        for client_id in (0, 19735):
            for member, arguments in (
                ("reqOpenOrders", ()),
                ("reqOpenOrdersProtoBuf", (SimpleNamespace(),)),
                ("reqAutoOpenOrders", (True,)),
                ("reqAutoOpenOrdersProtoBuf", (SimpleNamespace(),)),
            ):
                with self.subTest(client_id=client_id, member=member):
                    runtime = self.make(profile=profile(read_client_id=client_id))

                    def unsafe_startup(client):
                        getattr(client, member)(*arguments)

                    with patch.object(FakeEClient, "startApi", unsafe_startup):
                        with self.assertRaisesRegex(
                            IbkrRuntimeError, "READ_ORDER_BINDING_FORBIDDEN"
                        ):
                            runtime.connect_reads()
                    read = FakeEClient.instances[-1]
                    self.assertEqual(read.mutations, [])
                    self.assertEqual(read.conn.payloads, [])
                    self.assertFalse(runtime.status().authenticated)

    def test_read_bootstrap_uses_isolated_id_and_never_exposes_exact_account(self):
        runtime = self.make()
        components = runtime.connect_reads()
        self.assertEqual(len(FakeEClient.instances), 1)
        read = FakeEClient.instances[0]
        self.assertEqual((read.host, read.port, read.clientId), ("127.0.0.1", 4001, 19735))
        self.assertEqual(components.account_masked, "****3103")
        self.assertRegex(components.account_binding_fingerprint, r"^[0-9a-f]{64}$")
        self.assertNotIn(SYNTHETIC_ACCOUNT, repr(components))
        self.assertNotIn(SYNTHETIC_ACCOUNT, repr(runtime.status()))
        self.assertEqual(runtime.status().phase, "CONNECTED")
        self.assertEqual(read.mutations, [])
        self.assertEqual(read.market_data, [])

    def test_discovery_requires_one_live_account_and_exact_suffix(self):
        for accounts in (
            "",
            "DU9993103",
            "U1119999",
            SYNTHETIC_ACCOUNT + ",U1119999",
            SYNTHETIC_ACCOUNT + "," + SYNTHETIC_ACCOUNT,
        ):
            FakeEClient.instances = []
            FakeEClient.accounts = accounts
            runtime = self.make()
            with self.subTest(accounts=accounts), self.assertRaises(IbkrRuntimeError) as caught:
                runtime.connect_reads()
            self.assertIn("MANAGED_ACCOUNT", caught.exception.code)
            if accounts:
                self.assertNotIn(accounts, str(caught.exception))
            self.assertFalse(runtime.status().authenticated)

    def test_command_bootstrap_is_separate_guarded_and_does_not_authorize_write(self):
        runtime = self.make()
        runtime.connect_reads()
        calls = []

        def interlock():
            calls.append("interlock")

        def authority(request, evidence):
            calls.append((request, evidence))

        session = runtime.connect_command(
            mutation_interlock=interlock,
            authorize_dispatch=authority,
        )
        self.assertEqual(len(FakeEClient.instances), 2)
        command = FakeEClient.instances[1]
        self.assertEqual((command.host, command.port, command.clientId), ("127.0.0.1", 4001, 19736))
        self.assertEqual(session.next_valid_id, 700)
        self.assertEqual(command.mutations, [])
        self.assertEqual(calls, [])
        command_status = session.command_lane_status()
        self.assertTrue(command_status.command_connected)
        self.assertTrue(command_status.next_valid_id_received)
        self.assertTrue(command_status.account_authenticated)
        self.assertFalse(command_status.write_authority_granted)
        self.assertTrue(runtime.status().command_connected)
        self.assertFalse(runtime.status().write_authority_granted)
        with self.assertRaisesRegex(IbkrRuntimeError, "ATTENDED_TRANSPORT_NOT_READY"):
            runtime.attended_runtime()
        read_leaves = {
            role: component
            for role, component, _members in runtime.components.read_bridge.release_components()
        }
        requester = read_leaves["ibkr_read_requester"]
        self.assertEqual(
            {role for role, _component, _members in requester.release_components()},
            {"ibkr_read_callback_router"},
        )
        sdk_leaves = {
            role: component
            for role, component, _members in session.release_components()
        }
        command_client = sdk_leaves["ibkr_sdk_client"]
        self.assertEqual(
            {role for role, _component, _members in command_client.release_components()},
            {"ibkr_command_callback_router"},
        )

    def test_readiness_command_graph_is_attested_without_opening_command_socket(self):
        runtime = self.make()
        runtime.connect_reads()
        calls = []

        def interlock():
            calls.append("interlock")

        def authority(request, evidence):
            calls.append((request, evidence))

        session = runtime.prepare_disconnected_command(
            mutation_interlock=interlock,
            authorize_dispatch=authority,
        )
        self.assertEqual(len(FakeEClient.instances), 2)
        command = FakeEClient.instances[1]
        self.assertFalse(command.connected)
        self.assertIsNone(command.host)
        self.assertIsNone(command.port)
        self.assertIsNone(command.clientId)
        self.assertEqual(command.mutations, [])
        self.assertEqual(calls, [])
        self.assertFalse(runtime.status().command_connected)
        self.assertEqual(runtime.status().state, "READ_READY")
        with self.assertRaisesRegex(Exception, "IBKR_SESSION_NOT_READY"):
            session.validate_mutation_interlock()
        self.assertEqual(command.mutations, [])
        self.assertEqual(calls, [])

    def test_callbacks_are_sanitized_and_do_not_export_payload_or_text(self):
        FakeEClient.emit_error = True
        FakeEClient.emit_order = True
        runtime = self.make()
        runtime.connect_reads()
        runtime.connect_command(
            mutation_interlock=lambda: None,
            authorize_dispatch=lambda request, evidence: None,
        )
        errors = runtime.sanitized_errors
        events = runtime.sanitized_order_events
        self.assertEqual((errors[-1].request_id, errors[-1].code), (41, 201))
        self.assertEqual((events[-1].order_id, events[-1].status), (699, "Submitted"))
        for value in (repr(errors), repr(events), repr(runtime.status())):
            self.assertNotIn(SYNTHETIC_ACCOUNT, value)
            self.assertNotIn("private broker text", value)
            self.assertNotIn("SECRET", value)

    def test_sdk_1050_error_time_callback_is_sanitized_without_loop_failure(self):
        FakeEClient.emit_error = True
        FakeEClient.emit_new_error_signature = True
        runtime = self.make()
        runtime.connect_reads()
        runtime.connect_command(
            mutation_interlock=lambda: None,
            authorize_dispatch=lambda request, evidence: None,
        )
        errors = runtime.sanitized_errors
        self.assertEqual((errors[-1].request_id, errors[-1].code), (41, 201))
        self.assertTrue(runtime.status().command_connected)
        self.assertNotIn(SYNTHETIC_ACCOUNT, repr(errors))

    def test_sdk_1050_error_time_callback_is_supported_on_read_router(self):
        FakeEClient.emit_new_read_error_signature = True
        runtime = self.make()
        runtime.connect_reads()
        deadline = time.monotonic() + 0.25
        while not runtime.sanitized_errors and time.monotonic() < deadline:
            time.sleep(0.001)
        errors = runtime.sanitized_errors
        self.assertEqual((errors[-1].request_id, errors[-1].code), (-1, 2104))
        self.assertTrue(runtime.status().read_connected)
        self.assertIsNone(runtime.status().runtime_error_code)
        self.assertFalse(any(item.scope == "event_loop_failed" for item in errors))
        self.assertNotIn(SYNTHETIC_ACCOUNT, repr(errors))

    def test_protobuf_callbacks_do_not_log_or_retain_raw_payloads(self):
        runtime = self.make()
        runtime.connect_reads()
        read = FakeEClient.instances[0]
        secret = SimpleNamespace(errorMsg=f"private {SYNTHETIC_ACCOUNT}")
        expected_read_proto = {
            "managedAccountsProtoBuf",
            "accountSummaryProtoBuf",
            "accountSummaryEndProtoBuf",
            "positionProtoBuf",
            "positionEndProtoBuf",
            "openOrderProtoBuf",
            "openOrdersEndProtoBuf",
            "completedOrderProtoBuf",
            "completedOrdersEndProtoBuf",
            "orderStatusProtoBuf",
            "executionDetailsProtoBuf",
            "executionDetailsEndProtoBuf",
            "commissionAndFeesReportProtoBuf",
            "pnlProtoBuf",
            "contractDataProtoBuf",
            "contractDataEndProtoBuf",
            "errorProtoBuf",
        }
        self.assertEqual(
            {name for name in _READ_CALLBACK_MEMBERS if name.endswith("ProtoBuf")},
            expected_read_proto,
        )
        for name in expected_read_proto:
            self.assertIsNone(getattr(read.wrapper, name)(secret))
        runtime.connect_command(
            mutation_interlock=lambda: None,
            authorize_dispatch=lambda request, evidence: None,
        )
        command = FakeEClient.instances[1]
        expected_command_proto = {
            "nextValidIdProtoBuf",
            "managedAccountsProtoBuf",
            "openOrderProtoBuf",
            "openOrdersEndProtoBuf",
            "completedOrderProtoBuf",
            "completedOrdersEndProtoBuf",
            "orderStatusProtoBuf",
            "executionDetailsProtoBuf",
            "executionDetailsEndProtoBuf",
            "commissionAndFeesReportProtoBuf",
            "errorProtoBuf",
        }
        self.assertEqual(
            {
                name
                for name in _COMMAND_CALLBACK_MEMBERS
                if name.endswith("ProtoBuf")
            },
            expected_command_proto,
        )
        for name in expected_command_proto:
            self.assertIsNone(getattr(command.wrapper, name)(secret))
        self.assertIsNone(command.wrapper.execDetails(-1, secret, secret))
        self.assertIsNone(command.wrapper.execDetailsEnd(-1))
        self.assertIsNone(command.wrapper.commissionAndFeesReport(secret))
        self.assertNotIn(SYNTHETIC_ACCOUNT, repr(runtime.sanitized_errors))

    def test_complete_read_probe_uses_no_market_data_or_mutation(self):
        runtime = self.make(read_timeout_seconds=0.25, instrument_timeout_seconds=0.25)
        runtime.connect_reads()
        result = runtime.probe_reads("SPY")
        self.assertEqual(result.phase, "CONNECTED")
        self.assertTrue(result.authenticated)
        self.assertRegex(result.account_collection_id, r"^[0-9a-f]{64}$")
        self.assertRegex(result.contract_read_receipt_id, r"^[0-9a-f]{64}$")
        self.assertIsNone(result.instrument_evidence_id)
        self.assertTrue(result.contract_regular_session_open)
        self.assertFalse(result.public_dict()["whole_broker_history_verified"])
        read = FakeEClient.instances[0]
        methods = {call[0] for call in read.calls}
        self.assertTrue(
            {
                "reqAccountSummary",
                "reqPositions",
                "reqAllOpenOrders",
                "reqCompletedOrders",
                "reqExecutions",
                "reqPnL",
                "reqContractDetails",
            }
            <= methods
        )
        self.assertEqual(read.market_data, [])
        self.assertEqual(read.mutations, [])

    def test_contract_metadata_probe_connects_after_hours_without_trade_eligibility(self):
        after_hours = NOW.replace(hour=23)
        runtime = self.make(
            read_timeout_seconds=0.25,
            instrument_timeout_seconds=0.25,
            clock=lambda: after_hours,
        )
        components = runtime.connect_reads()
        result = runtime.probe_reads("SPY")
        self.assertEqual(result.phase, "CONNECTED")
        self.assertRegex(result.contract_read_receipt_id, r"^[0-9a-f]{64}$")
        self.assertFalse(result.contract_regular_session_open)
        self.assertIsNone(result.instrument_evidence_id)
        self.assertFalse(result.public_dict()["write_authority_granted"])
        with self.assertRaisesRegex(Exception, "NOT_REGULAR_HOURS_ELIGIBLE"):
            components.instrument_provider.get_instrument("SPY", now=after_hours)
        read = FakeEClient.instances[0]
        self.assertEqual(read.market_data, [])
        self.assertEqual(read.mutations, [])

    def test_missing_daily_realized_pnl_is_diagnosed_without_substituting_zero(self):
        runtime = self.make(read_timeout_seconds=0.02, instrument_timeout_seconds=0.25)
        runtime.connect_reads()
        read = FakeEClient.instances[0]
        read.reqPnL = lambda *args: None
        result = runtime.probe_reads("SPY")
        self.assertEqual(result.phase, "BLOCKED")
        self.assertEqual(result.error_code, "IBKR_RUNTIME_READ_DAILY_REALIZED_PNL_TIMEOUT")
        self.assertIsNone(result.account_collection_id)
        self.assertEqual(read.market_data, [])
        self.assertEqual(read.mutations, [])

    def test_read_probe_surfaces_only_sanitized_sdk_error_code(self):
        FakeEClient.emit_completed_orders_error = True
        runtime = self.make(read_timeout_seconds=0.25, instrument_timeout_seconds=0.25)
        runtime.connect_reads()
        result = runtime.probe_reads("SPY")
        self.assertEqual(result.phase, "BLOCKED")
        self.assertTrue(result.connected)
        self.assertFalse(result.authenticated)
        self.assertEqual(
            result.error_code, "IBKR_RUNTIME_READ_SDK_CALLBACK_321"
        )
        self.assertNotIn(SYNTHETIC_ACCOUNT, repr(result))

    def test_read_probe_identifies_read_only_cause_without_exporting_payloads(self):
        for modern in (False, True):
            with self.subTest(modern=modern):
                runtime = self.make(
                    read_timeout_seconds=0.25, instrument_timeout_seconds=0.25
                )
                runtime.connect_reads()
                read = FakeEClient.instances[-1]

                def read_only_completed_orders(apiOnly):
                    read.calls.append(("reqCompletedOrders", apiOnly))
                    message = (
                        "Error validating request:-'b_' : cause - "
                        "The API interface is currently in Read-Only mode."
                    )
                    arguments = (321, message, '{"account":"USECRET3103"}')
                    if modern:
                        message = message.replace("request:", "request.")
                        arguments = (321, message, '{"account":"USECRET3103"}')
                        # The decoder's protobuf hook must stay silent; the
                        # following normalized callback carries the diagnosis.
                        read.wrapper.errorProtoBuf(SimpleNamespace(
                            errorMsg=SYNTHETIC_ACCOUNT,
                            advancedOrderRejectJson="private protobuf payload",
                        ))
                        arguments = (1789394400, *arguments)
                    read.wrapper.error(-1, *arguments)

                read.reqCompletedOrders = read_only_completed_orders
                result = runtime.probe_reads("SPY")
                self.assertEqual(result.phase, "BLOCKED")
                self.assertEqual(
                    result.error_code,
                    "IBKR_RUNTIME_READ_SDK_CALLBACK_321_API_READ_ONLY",
                )
                self.assertEqual(runtime.sanitized_errors[-1].reason, "API_READ_ONLY")
                public = repr((result, runtime.sanitized_errors))
                for private in (
                    SYNTHETIC_ACCOUNT, "USECRET3103", "private protobuf payload",
                    "Error validating request",
                ):
                    self.assertNotIn(private, public)
                self.assertEqual(read.mutations, [])
                self.assertEqual(read.market_data, [])
                runtime.stop()

    def test_read_probe_does_not_misattribute_informational_callback(self):
        runtime = self.make(read_timeout_seconds=0.02, instrument_timeout_seconds=0.25)
        runtime.connect_reads()
        read = FakeEClient.instances[0]

        def incomplete_with_information(apiOnly):
            read.calls.append(("reqCompletedOrders", apiOnly))
            read.wrapper.error(
                -1,
                1789394400,
                2104,
                f"private broker text {SYNTHETIC_ACCOUNT}",
                '{"account":"USECRET3103"}',
            )

        read.reqCompletedOrders = incomplete_with_information
        result = runtime.probe_reads("SPY")
        self.assertEqual(result.phase, "BLOCKED")
        self.assertEqual(result.error_code, "IBKR_RUNTIME_READ_PROBE_FAILED")

    def test_read_probe_preserves_account_receipt_when_contract_probe_fails(self):
        runtime = self.make(read_timeout_seconds=0.25, instrument_timeout_seconds=0.25)
        runtime.connect_reads()
        read = FakeEClient.instances[0]

        def contract_error(reqId, contract):
            read.calls.append(("reqContractDetails", reqId, contract.symbol))
            read.wrapper.error(
                reqId,
                1789394400,
                200,
                f"private broker text {SYNTHETIC_ACCOUNT}",
                '{"account":"USECRET3103"}',
            )

        read.reqContractDetails = contract_error
        result = runtime.probe_reads("SPY")
        self.assertEqual(result.phase, "BLOCKED")
        self.assertTrue(result.connected)
        self.assertTrue(result.authenticated)
        self.assertRegex(result.account_collection_id, r"^[0-9a-f]{64}$")
        self.assertIsNone(result.instrument_evidence_id)
        self.assertIsNotNone(result.observed_at)
        self.assertEqual(
            result.error_code, "IBKR_RUNTIME_CONTRACT_SDK_CALLBACK_200"
        )
        self.assertNotIn(SYNTHETIC_ACCOUNT, repr(result))
        self.assertNotIn(SYNTHETIC_ACCOUNT, repr(result))

    def test_stale_generation_callbacks_are_ignored_and_stop_joins_threads(self):
        runtime = self.make()
        runtime.connect_reads()
        stale = runtime._read_router
        runtime.connect_command(
            mutation_interlock=lambda: None,
            authorize_dispatch=lambda request, evidence: None,
        )
        clients = tuple(FakeEClient.instances)
        runtime.stop()
        stale.managedAccounts("U0003103")
        self.assertFalse(runtime.status().authenticated)
        self.assertEqual(runtime.status().state, "STOPPED")
        self.assertTrue(all(client.stopped.is_set() for client in clients))
        self.assertTrue(all(not client.connected for client in clients))

    def test_missing_callbacks_timeout_and_cleanup_without_mutation(self):
        FakeEClient.suppress_callbacks = True
        runtime = self.make(connect_timeout_seconds=0.05)
        with self.assertRaisesRegex(IbkrRuntimeError, "ACCOUNT_DISCOVERY_TIMEOUT"):
            runtime.connect_reads()
        self.assertEqual(runtime.status().state, "FAILED")
        self.assertTrue(FakeEClient.instances[0].stopped.is_set())
        self.assertEqual(FakeEClient.instances[0].mutations, [])


if __name__ == "__main__":
    unittest.main()
