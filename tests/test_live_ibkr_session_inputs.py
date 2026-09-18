"""Synthetic callback tests only: no socket, broker, custody or live baseline."""

from dataclasses import FrozenInstanceError, replace
from datetime import timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from titan_brain.live.broker.base import AccountSnapshot, BrokerCapabilityError, OrderFamily
from titan_brain.live.broker.ibkr_read import IbkrReadCollectionDiagnostic
from titan_brain.live.broker.ibkr_runtime import IbkrOfficialRuntime
from titan_brain.live.broker.ibkr_session_inputs import (
    IbkrFiniteSessionFacts,
    IbkrSessionInputAdapter,
    IbkrSessionInputError,
    is_public_session_input_failure_code,
)
from tests.test_live_ibkr_runtime import (
    FakeEClient,
    NOW,
    SYNTHETIC_ACCOUNT,
    bundle,
    profile,
)
from tests.test_live_ibkr_read import order, state


D = Decimal


class SessionFakeClient(FakeEClient):
    def __init__(self, wrapper):
        super().__init__(wrapper)
        self.raw_contract = SimpleNamespace(conId=123, symbol="TEST", secType="STK", currency="USD")
        self.position_quantity = D(0)
        self.executions = []
        self.commissions = {}
        self.second_commissions = {}
        self.omit_end = None
        self.emit_active_order = False
        self.nlv = "10000"
        self.nlv_currency = "USD"
        self.summary_count = 0
        self.nlv_after_first = None
        self.disconnect_after_execution = False
        self.summary_account = SYNTHETIC_ACCOUNT
        self.emitted_order = None
        self.order_warning = ""

    def reqAccountUpdatesMulti(self, reqId, account, modelCode, ledgerAndNLV):
        self.calls.append(("reqAccountUpdatesMulti", reqId, account, modelCode, ledgerAndNLV))
        self.summary_count += 1
        nlv = self.nlv_after_first if self.summary_count > 1 and self.nlv_after_first is not None else self.nlv
        for tag, value, currency in (
            ("AccountType", "CASH", ""),
            ("NetLiquidation", nlv, self.nlv_currency),
            ("TotalCashValue", "10000", "USD"),
            ("AvailableFunds", "10000", "USD"),
            ("BuyingPower", "10000", "USD"),
            ("SettledCash", "10000", "USD"),
        ):
            self.wrapper.accountUpdateMulti(reqId, self.summary_account, "", tag, value, currency)
        if self.omit_end != "account_updates_multi":
            self.wrapper.accountUpdateMultiEnd(reqId)

    def cancelAccountUpdatesMulti(self, reqId):
        self.calls.append(("cancelAccountUpdatesMulti", reqId))

    def reqPositions(self):
        self.calls.append(("reqPositions",))
        if self.position_quantity:
            self.wrapper.position(SYNTHETIC_ACCOUNT, self.raw_contract, self.position_quantity, D(10))
        if self.omit_end != "positions":
            self.wrapper.positionEnd()

    def reqAllOpenOrders(self):
        self.calls.append(("reqAllOpenOrders",))
        if self.emit_active_order:
            self.wrapper.openOrder(91, self.raw_contract, self._order(), state("Submitted", self.order_warning))
        if self.omit_end != "open_orders":
            self.wrapper.openOrderEnd()

    def _order(self):
        quantity = sum((item.shares for item in self.executions), D(0)) or D(1)
        if self.emit_active_order:
            quantity += 1
        value = order(91, quantity=str(quantity), account=SYNTHETIC_ACCOUNT)
        self.emitted_order = value
        return value

    def reqCompletedOrders(self, apiOnly):
        self.calls.append(("reqCompletedOrders", apiOnly))
        if self.executions and not self.emit_active_order:
            self.wrapper.completedOrder(self.raw_contract, self._order(), state("Filled", self.order_warning))
        if self.omit_end != "completed_orders":
            self.wrapper.completedOrdersEnd()

    def reqExecutions(self, reqId, execution_filter):
        self.calls.append(("reqExecutions", reqId))
        for execution in self.executions:
            self.wrapper.execDetails(reqId, self.raw_contract, execution)
            if execution.execId in self.commissions:
                amount, currency = self.commissions[execution.execId]
                self.wrapper.commissionAndFeesReport(SimpleNamespace(
                    execId=execution.execId, commissionAndFees=amount, currency=currency,
                ))
            if execution.execId in self.second_commissions:
                amount, currency = self.second_commissions[execution.execId]
                self.wrapper.commissionAndFeesReport(SimpleNamespace(
                    execId=execution.execId, commissionAndFees=amount, currency=currency,
                ))
        if self.omit_end != "executions":
            self.wrapper.execDetailsEnd(reqId)
        if self.disconnect_after_execution:
            self.wrapper.connectionClosed()

    def reqPnL(self, *args):
        self.calls.append(("reqPnL", *args))
        # Intentionally no callback. The new finite path must still work.


class IbkrSessionInputsTests(unittest.TestCase):
    def setUp(self):
        SessionFakeClient.instances = []
        SessionFakeClient.accounts = SYNTHETIC_ACCOUNT
        for name in (
            "emit_error", "emit_new_error_signature", "emit_new_read_error_signature",
            "emit_completed_orders_error", "emit_order", "suppress_callbacks",
        ):
            setattr(SessionFakeClient, name, False)
        self.loader_patch = patch(
            "titan_brain.live.broker.ibkr_runtime._load_attested_sdk",
            return_value=replace(bundle(), client_type=SessionFakeClient),
        )
        self.loader = self.loader_patch.start()
        self.addCleanup(self.loader_patch.stop)
        self.now = NOW
        self.runtime = IbkrOfficialRuntime(
            profile=profile(), install_root=Path("/synthetic/no-real-sdk"),
            read_timeout_seconds=0.1, connect_timeout_seconds=0.2,
            shutdown_timeout_seconds=0.2, clock=lambda: self.now,
        )
        self.addCleanup(self.runtime.stop)
        self.adapter = IbkrSessionInputAdapter(self.runtime)

    def connect(self):
        self.components = self.runtime.connect_reads()
        self.client = SessionFakeClient.instances[-1]
        return self.client

    def fill(self, exec_id="synthetic-fill-1", *, commission="-0.25", currency="USD", **changes):
        values = dict(
            execId=exec_id, acctNumber=SYNTHETIC_ACCOUNT,
            clientId=7, orderId=91, permId=100,
            side="BOT", shares=D(1), price=D(10),
            time=NOW - timedelta(minutes=1),
        )
        values.update(changes)
        value = SimpleNamespace(**values)
        self.client.executions.append(value)
        if commission is not None:
            self.client.commissions[exec_id] = (D(commission), currency)
        return value

    def test_constructing_adapter_is_inert_and_rejects_generic_supplier(self):
        self.loader.assert_not_called()
        self.assertEqual(SessionFakeClient.instances, [])
        with self.assertRaises(TypeError):
            IbkrSessionInputAdapter(SimpleNamespace(complete=True))

    def test_read_capture_does_not_implicitly_connect(self):
        self.assertIsNone(self.adapter.last_capture_diagnostic)
        self.assertIsNone(self.adapter.last_capture_failure_code)
        with self.assertRaises(IbkrSessionInputError):
            self.adapter.capture()
        self.assertIsNone(self.adapter.last_capture_diagnostic)
        self.assertEqual(self.adapter.last_capture_failure_code, "IBKR_RUNTIME_READS_NOT_READY")
        self.loader.assert_not_called()
        self.assertEqual(SessionFakeClient.instances, [])

    def test_capture_retains_numeric_callback_cause_without_raw_text_or_request_id(self):
        self.connect()
        private = "synthetic private broker detail " + SYNTHETIC_ACCOUNT
        for scoped, expected in ((True, "REQUEST"), (False, "SDK_CALLBACK")):
            with self.subTest(scoped=scoped), patch.object(
                self.client, "reqAccountUpdatesMulti",
                side_effect=lambda request, *args: self.client.wrapper.error(
                    request if scoped else 7654321, 322, private,
                ),
            ):
                with self.assertRaisesRegex(IbkrSessionInputError, "^IBKR_SESSION_INPUT_CAPTURE_FAILED$"):
                    self.adapter.capture()
            code = self.adapter.last_capture_failure_code
            self.assertEqual(code, "IBKR_SESSION_INPUT_READ_CALLBACK_" + expected + "_322")
            self.assertTrue(is_public_session_input_failure_code(code))
            self.assertNotIn(private, code)
            self.assertNotIn(SYNTHETIC_ACCOUNT, code)
            self.assertNotIn("7654321", code)
            self.assertIsNotNone(self.adapter.last_capture_diagnostic)
        self.assertNotIn("reqPnL", [call[0] for call in self.client.calls])
        self.assertEqual(self.client.mutations, [])

    def test_callback_shape_and_known_read_only_reason_are_static_codes(self):
        self.connect()
        with patch.object(
            self.client, "reqAccountUpdatesMulti",
            side_effect=lambda request, *args: self.client.wrapper.accountUpdateMulti(
                request, SYNTHETIC_ACCOUNT, "", "NetLiquidation", object(), "USD",
            ),
        ):
            with self.assertRaises(IbkrSessionInputError):
                self.adapter.capture()
        self.assertEqual(
            self.adapter.last_capture_failure_code,
            "IBKR_SESSION_INPUT_READ_CALLBACK_ACCOUNT_UPDATES_SHAPE_0",
        )
        with patch.object(
            self.client, "reqAccountUpdatesMulti",
            side_effect=lambda request, *args: self.client.wrapper.error(
                request, 321, "The API interface is currently in Read-Only mode.",
            ),
        ):
            with self.assertRaises(IbkrSessionInputError):
                self.adapter.capture()
        self.assertEqual(
            self.adapter.last_capture_failure_code,
            "IBKR_SESSION_INPUT_READ_CALLBACK_REQUEST_321_API_READ_ONLY",
        )
        self.assertTrue(is_public_session_input_failure_code(self.adapter.last_capture_failure_code))

    def test_capture_failure_codes_allowlist_timeouts_dispatch_and_local_reasons(self):
        self.connect()
        cases = (
            ("IBKR_READ_TIMEOUT:positions", "IBKR_SESSION_INPUT_READ_TIMEOUT_POSITIONS"),
            ("IBKR_READ_TIMEOUT:positions,executions", "IBKR_SESSION_INPUT_READ_TIMEOUT_MULTIPLE_FINITE_CALLBACKS"),
            ("IBKR_READ_TIMEOUT:executions_dispatch", "IBKR_SESSION_INPUT_READ_TIMEOUT_EXECUTIONS_DISPATCH"),
            ("IBKR_READ_TIMEOUT:session_facts_normalization", "IBKR_SESSION_INPUT_READ_TIMEOUT_SESSION_FACTS_NORMALIZATION"),
            ("IBKR_READ_TIMEOUT:commission", "IBKR_SESSION_INPUT_READ_TIMEOUT_COMMISSION"),
            ("IBKR_READ_REQUEST_DISPATCH_FAILED", "IBKR_READ_REQUEST_DISPATCH_FAILED"),
            ("IBKR_SESSION_EXECUTION_SOURCE_TIME_INVALID", "IBKR_SESSION_EXECUTION_SOURCE_TIME_INVALID"),
            ("IBKR_READ_CALLBACK_ERROR:1100:connection_closed", "IBKR_SESSION_INPUT_READ_CALLBACK_CONNECTION_CLOSED_1100"),
        )
        for message, expected in cases:
            with self.subTest(message=message), patch.object(
                self.components.read_bridge, "collect_session_facts", side_effect=BrokerCapabilityError(message),
            ):
                with self.assertRaises(IbkrSessionInputError):
                    self.adapter.capture()
                self.assertEqual(self.adapter.last_capture_failure_code, expected)
                self.assertTrue(is_public_session_input_failure_code(expected))

    def test_unknown_private_or_pnl_failure_text_is_generic_not_echoed(self):
        self.connect()
        for message in (
            "synthetic private detail " + SYNTHETIC_ACCOUNT,
            "IBKR_PRIVATE_" + SYNTHETIC_ACCOUNT,
            "IBKR_READ_CALLBACK_ERROR:321:sdk_callback:PRIVATE_REASON",
            "IBKR_READ_CALLBACK_ERROR:322:sdk_callback:API_READ_ONLY",
            "IBKR_READ_CALLBACK_ERROR:321:position_shape:API_READ_ONLY",
            "IBKR_READ_CALLBACK_ERROR:9999999999:request",
            "IBKR_READ_CALLBACK_ERROR:322:private_scope",
            "IBKR_READ_CALLBACK_ERROR:0:daily_pnl_moved",
            "IBKR_READ_TIMEOUT:daily_realized_pnl",
            "IBKR_READ_TIMEOUT:positions,positions",
            "IBKR_READ_TIMEOUT:positions," + SYNTHETIC_ACCOUNT,
            "IBKR_READ_TIMEOUT:positions\nprivate",
            "IBKR_READ_TIMEOUT:" + "x" * 300,
        ):
            with self.subTest(message=message), patch.object(
                self.components.read_bridge, "collect_session_facts", side_effect=RuntimeError(message),
            ):
                with self.assertRaises(IbkrSessionInputError):
                    self.adapter.capture()
                self.assertEqual(self.adapter.last_capture_failure_code, "IBKR_SESSION_INPUT_CAPTURE_FAILED")

    def test_failure_classifier_does_not_stringify_arbitrary_exception_payload(self):
        self.connect()

        class PrivateFailure(Exception):
            def __str__(self):
                raise AssertionError("exception stringification must not occur")

        for failure in (PrivateFailure(object()), PrivateFailure("private", "details")):
            with patch.object(self.components.read_bridge, "collect_session_facts", side_effect=failure):
                with self.assertRaisesRegex(IbkrSessionInputError, "^IBKR_SESSION_INPUT_CAPTURE_FAILED$"):
                    self.adapter.capture()
                self.assertEqual(self.adapter.last_capture_failure_code, "IBKR_SESSION_INPUT_CAPTURE_FAILED")

    def test_failure_code_resets_before_predispatch_failure_and_after_success(self):
        self.connect()
        bridge = self.components.read_bridge
        with patch.object(bridge, "collect_session_facts", side_effect=BrokerCapabilityError("IBKR_READ_REQUEST_DISPATCH_FAILED")):
            with self.assertRaises(IbkrSessionInputError):
                self.adapter.capture()
        self.assertEqual(self.adapter.last_capture_failure_code, "IBKR_READ_REQUEST_DISPATCH_FAILED")

        def invalid_runtime():
            self.assertIsNone(self.adapter.last_capture_failure_code)
            self.assertIsNone(self.adapter.last_capture_diagnostic)
            raise IbkrSessionInputError("IBKR_SESSION_INPUT_RUNTIME_NOT_CURRENT")

        with patch.object(self.adapter, "_runtime_state", side_effect=invalid_runtime):
            with self.assertRaises(IbkrSessionInputError):
                self.adapter.capture()
        self.assertEqual(self.adapter.last_capture_failure_code, "IBKR_SESSION_INPUT_RUNTIME_NOT_CURRENT")
        result = self.adapter.capture()
        self.assertIsNone(self.adapter.last_capture_failure_code)
        self.assertTrue(result.sticky_read_gap)

    def test_public_failure_code_validator_rejects_arbitrary_suffixes_and_pnl(self):
        for value in (
            None, 322, True, "", "IBKR_PRIVATE_ACCOUNT_DATA",
            "IBKR_SESSION_INPUT_READ_TIMEOUT_PRIVATE",
            "IBKR_SESSION_INPUT_READ_TIMEOUT_DAILY_REALIZED_PNL",
            "IBKR_SESSION_INPUT_READ_CALLBACK_PRIVATE_322",
            "IBKR_SESSION_INPUT_READ_CALLBACK_REQUEST_9999999999",
            "IBKR_SESSION_INPUT_READ_CALLBACK_REQUEST_0322",
            "IBKR_SESSION_INPUT_READ_CALLBACK_REQUEST_322_API_READ_ONLY",
            "IBKR_SESSION_INPUT_READ_CALLBACK_POSITION_SHAPE_321_API_READ_ONLY",
            "IBKR_SESSION_INPUT_READ_CALLBACK_REQUEST_322_PRIVATE",
            "IBKR_SESSION_INPUT_CAPTURE_FAILED\nPRIVATE",
        ):
            with self.subTest(value=value):
                self.assertFalse(is_public_session_input_failure_code(value))

    def test_capture_diagnostic_is_immutable_sanitized_and_does_not_request_pnl(self):
        self.connect()
        self.fill()
        self.adapter.capture()
        diagnostic = self.adapter.last_capture_diagnostic
        self.assertIsInstance(diagnostic, IbkrReadCollectionDiagnostic)
        self.assertTrue(diagnostic.normalization_completed)
        channels = {item.channel: item for item in diagnostic.channels}
        self.assertEqual(channels["daily_realized_pnl"].observation, "not_requested")
        self.assertEqual(channels["daily_realized_pnl"].request_attempts, 0)
        for name in ("account_updates_multi", "positions", "open_orders", "completed_orders", "executions"):
            self.assertEqual(channels[name].observation, "end_callback_received")
        with self.assertRaises(FrozenInstanceError):
            diagnostic.normalization_completed = False
        public = diagnostic.public_dict()
        self.assertNotIn(SYNTHETIC_ACCOUNT, str(public))
        self.assertNotIn("synthetic-fill-1", str(public))
        self.assertNotIn(self.runtime.account_binding_fingerprint, str(public))
        self.assertFalse(public["write_authority_granted"])
        self.assertFalse(public["strict_account_read_complete"])
        self.assertNotIn("reqPnL", [call[0] for call in self.client.calls])
        self.assertNotIn("cancelPnL", [call[0] for call in self.client.calls])
        self.assertFalse(self.runtime.status().command_connected)
        self.assertEqual(self.client.mutations, [])

    def test_each_capture_uses_new_private_diagnostic_token(self):
        self.connect()
        bridge = self.components.read_bridge
        with (
            patch.object(bridge, "collect_session_facts", wraps=bridge.collect_session_facts) as collect,
            patch.object(bridge, "read_diagnostic_for", wraps=bridge.read_diagnostic_for) as lookup,
        ):
            self.adapter.capture()
            first = self.adapter.last_capture_diagnostic
            self.adapter.capture()
            second = self.adapter.last_capture_diagnostic
        tokens = [call.kwargs["diagnostic_token"] for call in collect.call_args_list]
        self.assertEqual(len(tokens), 2)
        self.assertIs(type(tokens[0]), object)
        self.assertIs(type(tokens[1]), object)
        self.assertIsNot(tokens[0], tokens[1])
        self.assertEqual([call.args[0] for call in lookup.call_args_list], tokens)
        self.assertIsNot(first, second)
        self.assertIsNone(bridge.read_diagnostic_for(tokens[0]))
        self.assertIs(bridge.read_diagnostic_for(tokens[1]), second)

    def test_failed_capture_retains_only_its_missing_channel_diagnostic(self):
        self.connect()
        self.adapter.capture()
        first = self.adapter.last_capture_diagnostic
        self.client.omit_end = "positions"
        with self.assertRaisesRegex(IbkrSessionInputError, "^IBKR_SESSION_INPUT_CAPTURE_FAILED$"):
            self.adapter.capture()
        diagnostic = self.adapter.last_capture_diagnostic
        self.assertIsNotNone(diagnostic)
        self.assertIsNot(diagnostic, first)
        self.assertFalse(diagnostic.normalization_completed)
        channels = {item.channel: item for item in diagnostic.channels}
        self.assertEqual(channels["positions"].observation, "no_matching_callback_received")
        self.assertEqual(channels["executions"].observation, "end_callback_received")
        self.assertEqual(channels["daily_realized_pnl"].observation, "not_requested")
        self.assertGreaterEqual(diagnostic.elapsed_ms, 0)
        self.assertNotIn("reqPnL", [call[0] for call in self.client.calls])

    def test_missing_commission_failure_retains_attributed_diagnostic(self):
        self.connect()
        self.fill(commission=None)
        with self.assertRaises(IbkrSessionInputError):
            self.adapter.capture()
        diagnostic = self.adapter.last_capture_diagnostic
        self.assertIsNotNone(diagnostic)
        self.assertFalse(diagnostic.normalization_completed)
        self.assertTrue(diagnostic.commission_reports_missing)

    def test_runtime_failure_before_dispatch_clears_previous_capture_diagnostic(self):
        self.connect()
        self.adapter.capture()
        bridge = self.components.read_bridge
        previous = bridge.last_read_diagnostic
        self.runtime._account_fingerprint = "f" * 64
        with patch.object(bridge, "collect_session_facts", wraps=bridge.collect_session_facts) as collect:
            with self.assertRaises(IbkrSessionInputError):
                self.adapter.capture()
            collect.assert_not_called()
        self.assertIsNone(self.adapter.last_capture_diagnostic)
        self.assertIs(bridge.last_read_diagnostic, previous)

    def test_dispatch_failure_cannot_borrow_prior_bridge_global_diagnostic(self):
        self.connect()
        self.adapter.capture()
        bridge = self.components.read_bridge
        previous = bridge.last_read_diagnostic
        with patch.object(bridge, "collect_session_facts", side_effect=RuntimeError("synthetic private text")):
            with self.assertRaisesRegex(IbkrSessionInputError, "^IBKR_SESSION_INPUT_CAPTURE_FAILED$"):
                self.adapter.capture()
        self.assertIsNone(self.adapter.last_capture_diagnostic)
        self.assertIs(bridge.last_read_diagnostic, previous)

    def test_intervening_other_collection_diagnostic_is_not_borrowed(self):
        self.connect()
        bridge = self.components.read_bridge
        collect = bridge.collect_session_facts

        def collect_then_other(*, diagnostic_token):
            facts = collect(diagnostic_token=diagnostic_token)
            collect(diagnostic_token=object())
            return facts

        with patch.object(bridge, "collect_session_facts", side_effect=collect_then_other):
            self.adapter.capture()
        self.assertIsNotNone(bridge.last_read_diagnostic)
        self.assertIsNone(self.adapter.last_capture_diagnostic)

    def test_generation_change_during_capture_cannot_retain_stale_diagnostic(self):
        self.connect()
        self.adapter.capture()
        bridge = self.components.read_bridge
        with patch.object(self.client, "reqExecutions", side_effect=lambda *args: bridge.open_generation(bridge.generation + 1)):
            with self.assertRaises(IbkrSessionInputError):
                self.adapter.capture()
        self.assertIsNone(self.adapter.last_capture_diagnostic)
        self.assertIsNone(bridge.last_read_diagnostic)

    def test_missing_pnl_does_not_prevent_real_typed_finite_inputs(self):
        self.connect()
        result = self.adapter.capture()
        self.assertIsInstance(result.facts, IbkrFiniteSessionFacts)
        self.assertNotIsInstance(result.facts, AccountSnapshot)
        self.assertFalse(hasattr(result.facts, "daily_realized_pnl"))
        self.assertEqual(result.facts.net_liquidation, D(10000))
        self.assertEqual(result.facts.net_liquidation_currency, "USD")
        self.assertEqual(result.account_binding_fingerprint, self.runtime.account_binding_fingerprint)
        self.assertEqual(result.facts.generation, self.runtime.status().read_generation)
        self.assertNotIn("reqPnL", [call[0] for call in self.client.calls])
        self.assertNotIn("cancelPnL", [call[0] for call in self.client.calls])
        self.assertFalse(self.runtime.status().command_connected)
        self.assertEqual(self.client.mutations, [])
        self.assertEqual(self.client.market_data, [])
        self.assertEqual(len(SessionFakeClient.instances), 1)

    def test_candidate_is_stable_flat_but_not_frozen_or_authoritative(self):
        self.connect()
        candidate = self.adapter.capture_pre_entry_candidate()
        self.assertEqual(candidate.starting_nlv, D(10000))
        self.assertTrue(candidate.observation_preconditions_met)
        self.assertNotEqual(candidate.first_collection_id, candidate.second_collection_id)
        public = candidate.public_dict()
        self.assertFalse(public["baseline_authority"])
        self.assertFalse(public["baseline_frozen"])
        self.assertFalse(public["live_authority"])
        self.assertIn("ALL_CLIENT_EXECUTION_COVERAGE_UNPROVEN", candidate.blockers)
        self.assertIn("CONTINUOUS_EVENT_COVERAGE_UNPROVEN", candidate.blockers)
        self.assertIn("EXTERNAL_ADJUSTMENT_RECONCILIATION_UNAVAILABLE", candidate.blockers)

    def test_statuses_redact_account_values_and_execution_identifiers(self):
        self.connect()
        self.fill()
        result = self.adapter.capture()
        public = result.public_dict()
        self.assertNotIn(SYNTHETIC_ACCOUNT, str(public))
        self.assertNotIn("synthetic-fill-1", str(public))
        self.assertNotIn(result.account_binding_fingerprint, str(public))
        self.assertNotIn("net_liquidation", public)
        self.assertNotIn(SYNTHETIC_ACCOUNT, repr(result))
        self.assertFalse(public["session_measurement_authority"])
        self.assertFalse(public["whole_account_coverage_verified"])

    def test_inputs_are_frozen_copies_not_mutable_sdk_payloads(self):
        self.connect()
        raw = self.fill()
        self.client.position_quantity = D(1)
        result = self.adapter.capture()
        with self.assertRaises(FrozenInstanceError):
            result.facts.net_liquidation = D(0)
        with self.assertRaises(FrozenInstanceError):
            result.facts.executions[0].price = D(0)
        raw.price = D(999)
        self.client.raw_contract.symbol = "CHANGED"
        self.assertEqual(result.facts.executions[0].price, D(10))
        self.assertEqual(result.facts.positions[0].symbol, "TEST")
        self.assertEqual(result.facts.executions[0].commission, D("-0.25"))

    def test_no_legacy_snapshot_page_reference_or_valuation_publication(self):
        self.connect()
        bridge = self.components.read_bridge
        # Seed a genuine strict collection using a fake P&L callback, then the
        # additive read must retire it, not publish the P&L-free replacement.
        with patch.object(self.client, "reqPnL", side_effect=lambda req, account, model: self.client.wrapper.pnl(req, 0.0, 0.0, 0.0)):
            bridge.get_account_base(SYNTHETIC_ACCOUNT)
        self.adapter.capture()
        with self.assertRaises(BrokerCapabilityError):
            bridge.list_order_family_page(SYNTHETIC_ACCOUNT, OrderFamily.STANDARD_EQUITY, None)
        with self.assertRaises(BrokerCapabilityError):
            bridge.lookup_equity_orders_by_client_ref(SYNTHETIC_ACCOUNT, ())
        self.assertIsNone(bridge._last)

    def test_actual_blank_commission_currency_stays_blank_and_blocks(self):
        self.connect()
        self.fill(currency="")
        result = self.adapter.capture()
        self.assertEqual(result.facts.executions[0].commission_currency, "")
        self.assertIn("ACTUAL_COMMISSION_USD_NOT_ESTABLISHED", result.blockers)

    def test_base_and_blank_nlv_currency_are_not_promoted_to_usd(self):
        self.connect()
        for currency in ("BASE", ""):
            with self.subTest(currency=currency):
                self.client.nlv_currency = currency
                candidate = self.adapter.capture_pre_entry_candidate()
                self.assertIsNone(candidate.starting_nlv)
                self.assertFalse(candidate.observation_preconditions_met)
                self.assertIn("NET_LIQUIDATION_USD_NOT_ESTABLISHED", candidate.blockers)

    def test_missing_provider_execution_time_is_none_not_local_now(self):
        self.connect()
        self.fill(time="")
        result = self.adapter.capture()
        self.assertIsNone(result.facts.executions[0].source_executed_at)
        self.assertEqual(result.facts.executions[0].source_time_basis, "ABSENT")
        self.assertIn("EXECUTION_PROVIDER_TIME_UNAVAILABLE", result.blockers)

    def test_aware_execution_datetimes_retain_explicit_zone_basis(self):
        self.connect()
        for timestamp in (NOW - timedelta(minutes=1), (NOW - timedelta(minutes=1)).astimezone(timezone(timedelta(hours=-4)))):
            with self.subTest(timestamp=timestamp):
                self.client.executions.clear()
                self.fill(time=timestamp)
                result = IbkrSessionInputAdapter(self.runtime).capture()
                fact = result.facts.executions[0]
                self.assertEqual(fact.source_time_basis, "PROVIDER_EXPLICIT_ZONE")
                self.assertEqual(fact.source_executed_at, NOW - timedelta(minutes=1))
                self.assertNotIn("EXECUTION_TIMEZONE_CONFIGURED_NOT_PROVIDER_PROVEN", result.blockers)

    def test_timezone_less_and_eastern_alias_strings_expose_configured_interpretation(self):
        self.connect()
        for suffix in ("", " US/Eastern", " America/New_York", " EST", " EDT"):
            with self.subTest(suffix=suffix):
                self.client.executions.clear()
                self.fill(time="20260914 12:00:00" + suffix)
                result = IbkrSessionInputAdapter(self.runtime).capture()
                fact = result.facts.executions[0]
                self.assertEqual(fact.source_time_basis, "CONFIGURED_SESSION_ZONE_INTERPRETATION")
                self.assertEqual(fact.source_executed_at, NOW - timedelta(hours=2))
                self.assertIn("EXECUTION_TIMEZONE_CONFIGURED_NOT_PROVIDER_PROVEN", result.blockers)
                self.assertIn("CONFIGURED_SESSION_ZONE_INTERPRETATION", result.public_dict()["execution_time_bases"])

    def test_explicit_utc_gmt_and_other_supported_zone_strings_are_distinguished(self):
        self.connect()
        for text in (
            "20260914 12:00:00 UTC", "20260914-12:00:00 GMT",
            "20260914  12:00:00 Europe/London", "20260914 12:00:00 Asia/Tokyo",
        ):
            with self.subTest(text=text):
                self.client.executions.clear()
                self.fill(time=text)
                result = IbkrSessionInputAdapter(self.runtime).capture()
                self.assertEqual(result.facts.executions[0].source_time_basis, "PROVIDER_EXPLICIT_ZONE")
                self.assertNotIn("EXECUTION_TIMEZONE_CONFIGURED_NOT_PROVIDER_PROVEN", result.blockers)

    def test_naive_datetime_and_unsupported_zone_are_rejected_not_promoted(self):
        self.connect()
        for timestamp in (NOW.replace(tzinfo=None), "20260914 12:00:00 NOT_A_REAL_TIMEZONE"):
            with self.subTest(timestamp=timestamp):
                self.client.executions.clear()
                self.fill(time=timestamp)
                with self.assertRaises(IbkrSessionInputError):
                    IbkrSessionInputAdapter(self.runtime).capture()

    def test_none_source_time_stays_explicitly_absent(self):
        self.connect()
        self.fill(time=None)
        result = self.adapter.capture()
        self.assertIsNone(result.facts.executions[0].source_executed_at)
        self.assertEqual(result.facts.executions[0].source_time_basis, "ABSENT")
        self.assertIn("EXECUTION_PROVIDER_TIME_UNAVAILABLE", result.blockers)

    def test_invalid_provider_time_type_cannot_be_relabelled_local_receipt(self):
        self.connect()
        self.fill(time=12345)
        with self.assertRaises(IbkrSessionInputError):
            self.adapter.capture()

    def test_terminal_order_warning_remains_a_reconciliation_blocker(self):
        self.connect()
        self.fill()
        self.client.order_warning = "synthetic warning text"
        result = self.adapter.capture()
        self.assertTrue(result.facts.orders[0].terminal_observed)
        self.assertIn("ORDER_WARNING_REQUIRES_RECONCILIATION", result.blockers)
        self.assertNotIn("synthetic warning text", str(result.public_dict()))

    def test_missing_commission_fails_capture_and_sticky_gap_survives_recovery(self):
        self.connect()
        self.fill(commission=None)
        with self.assertRaises(IbkrSessionInputError):
            self.adapter.capture()
        self.client.commissions["synthetic-fill-1"] = (D("1"), "USD")
        result = self.adapter.capture()
        self.assertEqual(result.facts.executions[0].commission, D(1))
        self.assertTrue(result.sticky_read_gap)
        self.assertIn("READ_GAP_REQUIRES_RECONCILIATION", result.blockers)

    def test_every_finite_end_is_required_and_failure_is_redacted(self):
        self.connect()
        for omitted in ("account_updates_multi", "positions", "open_orders", "completed_orders", "executions"):
            self.client.omit_end = omitted
            with self.subTest(omitted=omitted), self.assertRaises(IbkrSessionInputError) as failure:
                self.adapter.capture()
            self.assertEqual(str(failure.exception), "IBKR_SESSION_INPUT_CAPTURE_FAILED")
            self.assertNotIn(SYNTHETIC_ACCOUNT, str(failure.exception))
        self.client.omit_end = None
        self.assertTrue(self.adapter.capture().sticky_read_gap)

    def test_same_collection_commission_revision_remains_unknown(self):
        self.connect()
        self.fill(commission="1")
        self.client.second_commissions["synthetic-fill-1"] = (D(2), "USD")
        first = self.adapter.capture()
        self.assertTrue(first.facts.commission_conflict_observed)
        self.assertIn("EXECUTION_OR_COMMISSION_REVISION_UNRESOLVED", first.blockers)
        self.client.second_commissions.clear()
        self.assertIn("EXECUTION_OR_COMMISSION_REVISION_UNRESOLVED", self.adapter.capture().blockers)

    def test_later_fee_revision_and_history_omission_are_sticky(self):
        self.connect()
        self.fill(commission="1")
        self.adapter.capture()
        self.client.commissions["synthetic-fill-1"] = (D(2), "USD")
        self.assertIn("EXECUTION_OR_COMMISSION_REVISION_UNRESOLVED", self.adapter.capture().blockers)
        self.client.executions.clear()
        result = self.adapter.capture()
        self.assertIn("PREVIOUS_EXECUTION_NOT_IN_CURRENT_BOUNDED_READ", result.blockers)
        self.assertTrue(result.sticky_read_gap)
        self.assertTrue(self.adapter.capture().sticky_read_gap)

    def test_equal_numeric_replays_do_not_invent_revision(self):
        self.connect()
        raw = self.fill(commission="1")
        self.adapter.capture()
        self.now += timedelta(seconds=1)
        raw.price = D("10.000")
        self.client.commissions[raw.execId] = (D("1.00"), "USD")
        result = self.adapter.capture()
        self.assertNotIn("EXECUTION_OR_COMMISSION_REVISION_UNRESOLVED", result.blockers)
        self.assertTrue(result.unobserved_interval_since_prior_collection)
        self.assertIn("CONTINUOUS_EVENT_COVERAGE_UNPROVEN", result.blockers)

    def test_native_exec_id_correction_family_blocks(self):
        self.connect()
        self.fill("abc.def.ghi.01")
        self.fill("abc.def.ghi.02")
        self.assertIn("EXECUTION_OR_COMMISSION_REVISION_UNRESOLVED", self.adapter.capture().blockers)

    def test_nonflat_and_active_orders_and_current_fills_block_candidate(self):
        self.connect()
        self.client.position_quantity = D(1)
        self.client.emit_active_order = True
        self.fill()
        candidate = self.adapter.capture_pre_entry_candidate()
        self.assertIsNone(candidate.starting_nlv)
        self.assertIn("PRE_ENTRY_FLAT_POSITIONS_NOT_OBSERVED", candidate.blockers)
        self.assertIn("PRE_ENTRY_ACTIVE_OR_UNKNOWN_ORDERS_OBSERVED", candidate.blockers)
        self.assertIn("PRE_ENTRY_CURRENT_DAY_EXECUTIONS_OBSERVED", candidate.blockers)

    def test_changing_nlv_blocks_candidate(self):
        self.connect()
        self.client.nlv_after_first = "10001"
        candidate = self.adapter.capture_pre_entry_candidate()
        self.assertIsNone(candidate.starting_nlv)
        self.assertIn("PRE_ENTRY_NLV_NOT_STABLE", candidate.blockers)

    def test_unsupported_currency_and_fractional_positions_remain_blocked(self):
        self.connect()
        self.client.position_quantity = D("1.5")
        self.client.raw_contract.currency = "CAD"
        self.fill()
        result = self.adapter.capture()
        self.assertIn("POSITION_OUTSIDE_USD_WHOLE_SHARE_LONG_SCOPE", result.blockers)
        self.assertIn("EXECUTION_OUTSIDE_USD_WHOLE_SHARE_SCOPE", result.blockers)

    def test_foreign_summary_account_cannot_be_used(self):
        self.connect()
        self.client.summary_account = "U0003103"
        with self.assertRaises(IbkrSessionInputError):
            self.adapter.capture()

    def test_foreign_fill_is_not_relabelled_as_target_account(self):
        self.connect()
        self.fill(acctNumber="U0003103")
        result = self.adapter.capture()
        self.assertEqual(result.facts.executions, ())
        self.assertIn("ORPHAN_COMMISSION_SCOPE_UNKNOWN", result.blockers)

    def test_connection_loss_at_final_callback_boundary_cannot_escape(self):
        self.connect()
        self.client.disconnect_after_execution = True
        with self.assertRaises(IbkrSessionInputError):
            self.adapter.capture()
        self.assertIsNone(self.components.read_bridge._last)

    def test_runtime_binding_mismatch_is_checked_without_caller_authority_flag(self):
        self.connect()
        self.runtime._account_fingerprint = "f" * 64
        with self.assertRaises(IbkrSessionInputError):
            self.adapter.capture()

    def test_reconnect_leaves_sticky_gap(self):
        self.connect()
        self.adapter.capture()
        self.runtime.stop()
        self.connect()
        result = self.adapter.capture()
        self.assertTrue(result.sticky_read_gap)
        self.assertIn("READ_GAP_REQUIRES_RECONCILIATION", result.blockers)

    def test_direct_bridge_does_not_discover_or_authenticate_account(self):
        self.connect()
        self.components.read_bridge._authenticated_generation = None
        with self.assertRaises(BrokerCapabilityError):
            self.components.read_bridge.collect_session_facts()


if __name__ == "__main__":
    unittest.main()
