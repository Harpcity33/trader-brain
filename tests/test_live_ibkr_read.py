"""Hermetic official-callback tests; no IBKR socket or order mutation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from titan_brain.live.broker.base import (  # noqa: E402
    BrokerCapabilityError,
    BrokerContractViolation,
    BrokerSide,
    EquityOrderType,
    MarketHours,
    OrderFamily,
    TimeInForce,
)
from titan_brain.live.broker.ibkr_read import (  # noqa: E402
    IbkrWholeAccountReadBridge,
    classify_ibkr_error_callback,
)
from titan_brain.live.models import BrokerOrderState  # noqa: E402
from titan_brain.live.protection import is_verified_working_protection  # noqa: E402


NOW = datetime(2026, 9, 14, 14, 0, tzinfo=timezone.utc)
ACCOUNT = "DU1234567"
MASK = "****4567"
ENTRY_REF = "00000000-0000-4000-8000-000000000001"
FILLED_REF = "00000000-0000-4000-8000-000000000002"
STOP_REF = "00000000-0000-4000-8000-000000000003"
MISSING_REF = "00000000-0000-4000-8000-000000000004"


def contract(symbol="TEST", sec_type="STK"):
    return SimpleNamespace(symbol=symbol, secType=sec_type)


def order(
    order_id,
    *,
    client_id=7,
    action="BUY",
    order_type="LMT",
    quantity="10",
    tif="DAY",
    outside_rth=False,
    order_ref=ENTRY_REF,
    parent_id=0,
    account=ACCOUNT,
    perm_id=100,
    lmt_price=Decimal("10.00"),
    aux_price=Decimal("0"),
):
    return SimpleNamespace(
        orderId=order_id,
        clientId=client_id,
        account=account,
        action=action,
        orderType=order_type,
        totalQuantity=Decimal(quantity),
        tif=tif,
        outsideRth=outside_rth,
        includeOvernight=False,
        orderRef=order_ref,
        parentId=parent_id,
        ocaGroup="",
        conditions=[],
        algoStrategy="",
        hedgeType="",
        permId=perm_id,
        lmtPrice=lmt_price,
        auxPrice=aux_price,
    )


def state(status, warning_text="", completed_time=""):
    return SimpleNamespace(
        status=status,
        warningText=warning_text,
        completedTime=completed_time,
    )


class FakeReadRequester:
    def __init__(self):
        self.callbacks = None
        self.calls = []
        self.omit_end = None
        self.omit_commission = False
        self.omit_daily_pnl = False
        self.daily_pnl = Decimal("12.34")
        self.inject_error = None
        self.duplicate_ref = False
        self.short_position = False
        self.exotic_order = False
        self.new_commission_callback = False
        self.stop_order_state = None
        self.filled_order_state = None
        self.stop_duplicate_order_states = ()
        self.before_stop_duplicate = None
        self.after_stop_duplicate = None

    def reqAccountSummary(self, reqId, groupName, tags):
        self.calls.append(("reqAccountSummary", reqId, groupName, tags))
        for tag, value, currency in (
            ("AccountType", "MARGIN", ""),
            ("NetLiquidation", "1000", "USD"),
            ("TotalCashValue", "500", "USD"),
            ("AvailableFunds", "480", "USD"),
            ("BuyingPower", "1000", "USD"),
            ("SettledCash", "450", "USD"),
        ):
            self.callbacks.accountSummary(reqId, ACCOUNT, tag, value, currency)
        if self.inject_error is not None:
            code, message = self.inject_error
            self.callbacks.error(reqId, code, message, "private-json")
        if self.omit_end != "account_summary":
            self.callbacks.accountSummaryEnd(reqId)

    def cancelAccountSummary(self, reqId):
        self.calls.append(("cancelAccountSummary", reqId))

    def reqPositions(self):
        self.calls.append(("reqPositions",))
        quantity = Decimal("-1") if self.short_position else Decimal("10")
        self.callbacks.position(
            ACCOUNT, contract(), quantity, Decimal("9.50")
        )
        self.callbacks.position(
            ACCOUNT, contract("TEST", "OPT"), Decimal("1"), Decimal("50")
        )
        if self.omit_end != "positions":
            self.callbacks.positionEnd()

    def cancelPositions(self):
        self.calls.append(("cancelPositions",))

    def reqAllOpenOrders(self):
        self.calls.append(("reqAllOpenOrders",))
        entry = order(11)
        self.callbacks.openOrder(11, contract(), entry, state("Submitted"))
        stop = order(
            12,
            action="SELL",
            order_type="STP",
            quantity="2",
            tif="GTC",
            order_ref=STOP_REF,
            parent_id=11,
            perm_id=101,
            lmt_price=Decimal("0"),
            aux_price=Decimal("8.75"),
        )
        stop_state = self.stop_order_state or state("Submitted")
        self.callbacks.openOrder(12, contract(), stop, stop_state)
        for duplicate_state in self.stop_duplicate_order_states:
            if self.before_stop_duplicate is not None:
                self.before_stop_duplicate()
            self.callbacks.openOrder(12, contract(), stop, duplicate_state)
            if self.after_stop_duplicate is not None:
                self.after_stop_duplicate()
        option = order(13, order_ref="manual", perm_id=102)
        self.callbacks.openOrder(
            13, contract("TEST", "OPT"), option, state("Submitted")
        )
        if self.exotic_order:
            exotic = order(14, order_ref="external", perm_id=103)
            self.callbacks.openOrder(
                14, contract("EUR", "CASH"), exotic, state("Submitted")
            )
        if self.omit_end != "open_orders":
            self.callbacks.openOrderEnd()

    def reqCompletedOrders(self, apiOnly):
        self.calls.append(("reqCompletedOrders", apiOnly))
        filled = order(
            10,
            quantity="1",
            order_ref=ENTRY_REF if self.duplicate_ref else FILLED_REF,
            perm_id=99,
        )
        filled_state = self.filled_order_state or state("Filled")
        self.callbacks.completedOrder(contract(), filled, filled_state)
        if self.omit_end != "completed_orders":
            self.callbacks.completedOrdersEnd()

    def reqExecutions(self, reqId, execFilter):
        self.calls.append(("reqExecutions", reqId, type(execFilter).__name__))
        execution = SimpleNamespace(
            execId="exec-1",
            acctNumber=ACCOUNT,
            clientId=7,
            orderId=10,
            permId=99,
            shares=Decimal("1"),
            price=Decimal("10.02"),
            time="20260914 09:59:00 America/New_York",
        )
        self.callbacks.execDetails(reqId, contract(), execution)
        if self.omit_end != "executions":
            self.callbacks.execDetailsEnd(reqId)
        if not self.omit_commission:
            if self.new_commission_callback:
                self.callbacks.commissionAndFeesReport(
                    SimpleNamespace(
                        execId="exec-1",
                        commissionAndFees=Decimal("0.35"),
                        currency="USD",
                    )
                )
            else:
                self.callbacks.commissionReport(
                    SimpleNamespace(
                        execId="exec-1",
                        commission=Decimal("0.35"),
                        currency="USD",
                    )
                )

    def reqPnL(self, reqId, account, modelCode):
        self.calls.append(("reqPnL", reqId, account, modelCode))
        if not self.omit_daily_pnl:
            self.callbacks.pnl(reqId, self.daily_pnl, Decimal("0"), self.daily_pnl)

    def cancelPnL(self, reqId):
        self.calls.append(("cancelPnL", reqId))


class TickingClock:
    def __init__(self):
        self.value = NOW

    def __call__(self):
        current = self.value
        self.value += timedelta(milliseconds=1)
        return current


class MutableClock:
    def __init__(self):
        self.value = NOW

    def __call__(self):
        return self.value


class IbkrReadBridgeTests(unittest.TestCase):
    def setUp(self):
        self.no_socket = patch(
            "socket.socket.connect", side_effect=AssertionError("network forbidden")
        )
        self.no_socket.start()
        self.addCleanup(self.no_socket.stop)
        self.requester = FakeReadRequester()
        self.bridge = IbkrWholeAccountReadBridge(
            requester=self.requester,
            exact_account_id=ACCOUNT,
            account_masked=MASK,
            timeout_seconds=0.02,
            clock=lambda: NOW,
        )
        self.callbacks = self.bridge.open_generation(1)
        self.requester.callbacks = self.callbacks
        self.callbacks.connectAck()
        self.callbacks.managedAccounts(f"{ACCOUNT},DU7654321")

    def collect(self):
        return self.bridge.get_account_base(ACCOUNT)

    def channel_diagnostics(self):
        return {item.channel: item for item in self.bridge.last_read_diagnostic.channels}

    def test_channel_diagnostics_preserve_simultaneous_missing_completed_end_and_pnl(self):
        self.requester.omit_end = "completed_orders"
        self.requester.omit_daily_pnl = True
        with self.assertRaisesRegex(BrokerCapabilityError, "IBKR_READ_TIMEOUT:completed_orders"):
            self.collect()
        channels = self.channel_diagnostics()
        self.assertEqual(channels["completed_orders"].observation, "required_completion_not_received")
        self.assertIsNotNone(channels["completed_orders"].first_callback_ms)
        self.assertIsNone(channels["completed_orders"].end_callback_ms)
        self.assertEqual(channels["daily_realized_pnl"].observation, "no_matching_callback_received")
        self.assertEqual(channels["daily_realized_pnl"].request_attempts, 1)
        for channel in ("account_summary", "positions", "open_orders", "executions"):
            self.assertEqual(channels[channel].observation, "end_callback_received")
        public = self.bridge.last_read_diagnostic.public_dict()
        self.assertFalse(public["normalization_completed"])
        self.assertTrue(public["diagnostic_only"])
        for field in ("dispatch_return_is_broker_acknowledgement", "provider_cause_verified",
                      "write_authority_granted", "daily_starting_equity_ready"):
            self.assertFalse(public[field])
        for private in (ACCOUNT, ENTRY_REF, FILLED_REF, "exec-1", "1000"):
            self.assertNotIn(private, repr(public))
        self.assertIsNone(self.bridge._last)
        self.assertIsNone(self.bridge._active)

    def test_channel_diagnostics_distinguish_no_response_dispatch_failure_and_no_request(self):
        with patch.object(self.requester, "reqCompletedOrders"):
            with self.assertRaisesRegex(BrokerCapabilityError, "IBKR_READ_TIMEOUT:completed_orders"):
                self.bridge.diagnose_finite_reads(ACCOUNT)
        channels = self.channel_diagnostics()
        self.assertEqual(channels["completed_orders"].observation, "no_matching_callback_received")
        self.assertIsNotNone(channels["completed_orders"].last_dispatch_returned_ms)
        self.assertEqual(channels["daily_realized_pnl"].observation, "not_requested")
        with patch.object(self.requester, "reqPositions", side_effect=RuntimeError("private " + ACCOUNT)):
            with self.assertRaisesRegex(BrokerCapabilityError, "IBKR_READ_REQUEST_DISPATCH_FAILED"):
                self.collect()
        channels = self.channel_diagnostics()
        self.assertEqual(channels["positions"].observation, "dispatch_did_not_return")
        self.assertIsNone(channels["positions"].last_dispatch_returned_ms)
        self.assertEqual(channels["completed_orders"].observation, "not_requested")
        self.assertNotIn("private", repr(self.bridge.last_read_diagnostic.public_dict()))

    def test_channel_diagnostics_record_local_timing_not_broker_source_time(self):
        clock = [0.0]
        original = self.requester.reqCompletedOrders

        def completed(api_only):
            clock[0] = 0.003
            original(api_only)
            clock[0] = 0.004

        with patch("titan_brain.live.broker.ibkr_read.time.monotonic", side_effect=lambda: clock[0]), patch.object(
            self.requester, "reqCompletedOrders", side_effect=completed
        ):
            self.collect()
        row = self.channel_diagnostics()["completed_orders"]
        self.assertEqual((row.first_dispatch_started_ms, row.last_dispatch_started_ms,
                          row.first_callback_ms, row.end_callback_ms,
                          row.last_dispatch_returned_ms), (0, 0, 3, 3, 4))
        diagnostic = self.bridge.last_read_diagnostic
        self.assertEqual(diagnostic.elapsed_ms, 4)
        self.assertTrue(diagnostic.normalization_completed)
        self.assertEqual(diagnostic.public_dict()["timing_basis"], "local_monotonic_receipt_not_broker_source_time")

    def test_channel_diagnostics_freeze_before_cancel_and_clear_on_new_generation(self):
        self.requester.omit_end = "completed_orders"
        original_cancel = self.requester.cancelPositions

        def late_end():
            self.callbacks.completedOrdersEnd()
            original_cancel()

        with patch.object(self.requester, "cancelPositions", side_effect=late_end):
            with self.assertRaisesRegex(BrokerCapabilityError, "IBKR_READ_TIMEOUT:completed_orders"):
                self.collect()
        frozen = self.bridge.last_read_diagnostic
        self.assertIsNone(self.channel_diagnostics()["completed_orders"].end_callback_ms)
        self.callbacks.completedOrdersEnd()
        self.assertIs(self.bridge.last_read_diagnostic, frozen)
        self.bridge.open_generation(2)
        self.assertIsNone(self.bridge.last_read_diagnostic)
        with self.assertRaisesRegex(BrokerCapabilityError, "IBKR_READ_NOT_AUTHENTICATED"):
            self.collect()
        self.assertIsNone(self.bridge.last_read_diagnostic)

    def test_channel_diagnostics_report_unset_pnl_retry_and_commission_without_private_values(self):
        self.requester.daily_pnl = Decimal("1.7976931348623157e308")
        with self.assertRaisesRegex(BrokerCapabilityError, "daily_realized_pnl_retry_exhausted_unavailable"):
            self.collect()
        row = self.channel_diagnostics()["daily_realized_pnl"]
        self.assertEqual(row.observation, "value_unavailable")
        self.assertEqual(row.request_attempts, 2)
        self.requester.omit_commission = True
        with self.assertRaisesRegex(BrokerCapabilityError, "IBKR_READ_TIMEOUT:commission"):
            self.bridge.diagnose_finite_reads(ACCOUNT)
        self.assertTrue(self.bridge.last_read_diagnostic.commission_reports_missing)
        self.assertFalse(self.bridge.last_read_diagnostic.normalization_completed)
        self.assertEqual(self.channel_diagnostics()["daily_realized_pnl"].observation, "not_requested")

    def test_older_attempt_cannot_replace_a_newer_diagnostic(self):
        collections = []
        normalize = self.bridge._normalize

        def capture(collection):
            collections.append(collection)
            return normalize(collection)

        with patch.object(self.bridge, "_normalize", side_effect=capture):
            self.collect()
        self.bridge.diagnose_finite_reads(ACCOUNT)
        newer = self.bridge.last_read_diagnostic
        self.bridge._finish_read_diagnostic(collections[0], normalized=True)
        self.assertIs(self.bridge.last_read_diagnostic, newer)
        self.assertEqual(self.channel_diagnostics()["daily_realized_pnl"].observation, "not_requested")

    def test_channel_diagnostic_does_not_reuse_first_dispatch_return_after_retry_failure(self):
        attempts = []

        def retry_fails(request_id, account, model):
            attempts.append(request_id)
            if len(attempts) == 2:
                raise RuntimeError("private " + ACCOUNT)

        with patch.object(self.requester, "reqPnL", side_effect=retry_fails):
            with self.assertRaisesRegex(BrokerCapabilityError, "IBKR_READ_PNL_RETRY_DISPATCH_FAILED"):
                self.collect()
        row = self.channel_diagnostics()["daily_realized_pnl"]
        self.assertEqual(row.request_attempts, 2)
        self.assertIsNotNone(row.first_dispatch_started_ms)
        self.assertGreaterEqual(row.last_dispatch_started_ms, row.first_dispatch_started_ms)
        self.assertIsNone(row.last_dispatch_returned_ms)
        self.assertEqual(row.observation, "dispatch_did_not_return")
        self.assertNotIn(ACCOUNT, repr(self.bridge.last_read_diagnostic.public_dict()))

    def test_finite_diagnostic_has_no_pnl_or_production_publication(self):
        previous = self.collect()
        self.requester.calls.clear()
        self.requester.omit_daily_pnl = True
        normalized = []
        normalize = self.bridge._normalize_finite

        def inspect(collection):
            result = normalize(collection)
            normalized.append(result.observation.snapshot)
            return result

        with patch.object(self.bridge, "_normalize_finite", side_effect=inspect):
            diagnostic = self.bridge.diagnose_finite_reads(ACCOUNT)
        result = diagnostic.public_dict()
        self.assertEqual(set(result["completed_reads"]), {
            "account_summary", "positions", "open_orders", "completed_orders", "executions",
        })
        self.assertTrue(result["finite_data_normalized"])
        self.assertTrue(result["diagnostic_only"])
        self.assertEqual(result["daily_pnl_status"], "not_requested")
        for field in ("strict_account_read_complete", "daily_starting_equity_ready",
                      "whole_broker_history_verified", "write_authority_granted"):
            self.assertFalse(result[field])
        self.assertEqual(len(normalized), 1)
        snapshot = normalized[0]
        self.assertEqual(snapshot.funds.total_value, Decimal("1000"))
        self.assertIsNone(snapshot.daily_realized_pnl)
        self.assertFalse(snapshot.daily_realized_pnl_complete)
        self.assertFalse(snapshot.risk_evidence_authoritative)
        self.assertIsNone(snapshot.risk_evidence_source)
        self.assertIsNone(snapshot.risk_evidence_as_of)
        self.assertFalse(snapshot.daily_starting_equity_ready)
        self.assertIsNone(self.bridge._last)
        for read in (
            lambda: self.bridge.list_order_family_page(ACCOUNT, OrderFamily.STANDARD_EQUITY, None),
            lambda: self.bridge.lookup_equity_orders_by_client_ref(ACCOUNT, (FILLED_REF,)),
            lambda: self.bridge.position_valuation_inputs(previous.collection_id),
        ):
            with self.assertRaises((BrokerCapabilityError, BrokerContractViolation)):
                read()
        methods = [call[0] for call in self.requester.calls]
        self.assertNotIn("reqPnL", methods)
        self.assertNotIn("cancelPnL", methods)
        self.assertIn("cancelAccountSummary", methods)
        self.assertIn("cancelPositions", methods)
        for private in (ACCOUNT, ENTRY_REF, FILLED_REF, "exec-1", "1000"):
            self.assertNotIn(private, repr(result))
        with self.assertRaisesRegex(BrokerCapabilityError, "daily_realized_pnl"):
            self.collect()

    def test_finite_diagnostic_still_requires_every_end_and_commission(self):
        for missing in ("account_summary", "positions", "open_orders", "completed_orders",
                        "executions", "commission"):
            with self.subTest(missing=missing):
                self.requester.omit_end = None if missing == "commission" else missing
                self.requester.omit_commission = missing == "commission"
                with self.assertRaisesRegex(BrokerCapabilityError, "IBKR_READ_TIMEOUT:" + missing):
                    self.bridge.diagnose_finite_reads(ACCOUNT)
                self.assertIsNone(self.bridge._last)
                self.assertIsNone(self.bridge._active)
        self.assertNotIn("reqPnL", [call[0] for call in self.requester.calls])

    def test_finite_diagnostic_rejects_callback_failure_and_invalid_data(self):
        self.requester.inject_error = (2103, "private broker text " + ACCOUNT)
        with self.assertRaisesRegex(BrokerCapabilityError, "IBKR_READ_CALLBACK_ERROR:2103") as raised:
            self.bridge.diagnose_finite_reads(ACCOUNT)
        self.assertNotIn(ACCOUNT, str(raised.exception))
        self.requester.inject_error = None
        self.requester.exotic_order = True
        with self.assertRaises(BrokerContractViolation):
            self.bridge.diagnose_finite_reads(ACCOUNT)
        self.assertIsNone(self.bridge._last)
        self.assertIsNone(self.bridge._active)

    def test_finite_diagnostic_rejects_generation_change_before_freeze(self):
        original = self.requester.reqExecutions

        def change_generation(*args):
            original(*args)
            self.bridge.open_generation(2)

        with patch.object(self.requester, "reqExecutions", side_effect=change_generation):
            with self.assertRaises(BrokerCapabilityError):
                self.bridge.diagnose_finite_reads(ACCOUNT)
        self.assertIsNone(self.bridge._last)
        self.assertIsNone(self.bridge._active)

    def test_finite_diagnostic_rejects_connection_loss_and_wrong_account(self):
        with self.assertRaises(BrokerContractViolation):
            self.bridge.diagnose_finite_reads("DU9999999")
        self.assertEqual(self.requester.calls, [])
        original = self.requester.reqExecutions

        def disconnect(*args):
            original(*args)
            self.callbacks.connectionClosed()

        with patch.object(self.requester, "reqExecutions", side_effect=disconnect):
            with self.assertRaises(BrokerCapabilityError):
                self.bridge.diagnose_finite_reads(ACCOUNT)
        self.assertIsNone(self.bridge._last)
        self.assertIsNone(self.bridge._active)

    def test_finite_diagnostic_cannot_escape_dispatch_or_freeze_deadline(self):
        for phase in ("account_summary_dispatch", "collection_freeze"):
            with self.subTest(phase=phase):
                monotonic = [0.0]
                target = self.requester if phase == "account_summary_dispatch" else self.bridge
                method = "reqAccountSummary" if phase == "account_summary_dispatch" else "_issue_finite_requests"
                original = getattr(target, method)

                def delayed(*args):
                    original(*args)
                    monotonic[0] = 1.0

                with patch("titan_brain.live.broker.ibkr_read.time.monotonic", side_effect=lambda: monotonic[0]), \
                        patch.object(target, method, side_effect=delayed):
                    with self.assertRaisesRegex(BrokerCapabilityError, "IBKR_READ_TIMEOUT:" + phase):
                        self.bridge.diagnose_finite_reads(ACCOUNT)
                self.assertIsNone(self.bridge._last)
                self.assertIsNone(self.bridge._active)

    def test_release_inventory_exposes_the_read_requester_contract(self):
        inventory = self.bridge.release_components()
        by_role = {role: (component, members) for role, component, members in inventory}
        self.assertEqual(set(by_role), {
            "ibkr_read_requester",
            "ibkr_execution_filter_factory",
            "ibkr_read_clock",
        })
        component, members = by_role["ibkr_read_requester"]
        self.assertIs(component, self.requester)
        self.assertEqual(
            set(members),
            {
                "reqAccountSummary",
                "cancelAccountSummary",
                "reqPositions",
                "cancelPositions",
                "reqAllOpenOrders",
                "reqCompletedOrders",
                "reqExecutions",
                "reqPnL",
                "cancelPnL",
            },
        )

    def test_collects_all_read_families_and_normalizes_conservatively(self):
        evidence = self.collect()
        snapshot = evidence.snapshot
        self.assertEqual(snapshot.account_masked, MASK)
        self.assertEqual(snapshot.account_type, "MARGIN")
        self.assertEqual(snapshot.funds.total_value, Decimal("1000"))
        self.assertEqual(snapshot.funds.cash, Decimal("500"))
        self.assertEqual(snapshot.funds.buying_power, Decimal("1000"))
        self.assertEqual(snapshot.funds.unleveraged_buying_power, Decimal("450"))
        self.assertEqual(snapshot.funds.unsettled_funds, Decimal("50"))
        self.assertTrue(snapshot.daily_realized_pnl_complete)
        self.assertTrue(snapshot.risk_evidence_authoritative)
        self.assertEqual(snapshot.daily_realized_pnl, Decimal("12.34"))
        self.assertEqual(
            snapshot.risk_evidence_source,
            "ibkr:reqPnL.realizedPnL:current-day",
        )
        self.assertEqual(snapshot.risk_evidence_as_of, NOW)
        self.assertEqual(snapshot.option_position_count, 1)
        self.assertEqual(len(snapshot.equity_positions), 1)
        position = snapshot.equity_positions[0]
        self.assertEqual(position.quantity, Decimal("10"))
        self.assertEqual(position.held_for_sells, Decimal("2"))
        self.assertEqual(position.sellable_quantity, Decimal("8"))

        standard = self.bridge.list_order_family_page(
            ACCOUNT, OrderFamily.STANDARD_EQUITY, None
        )
        advanced = self.bridge.list_order_family_page(
            ACCOUNT, OrderFamily.ADVANCED_EQUITY, None
        )
        options = self.bridge.list_order_family_page(ACCOUNT, OrderFamily.OPTION, None)
        self.assertEqual(len(standard.orders), 2)
        self.assertEqual(standard.active_order_count, 1)
        self.assertEqual(len(advanced.orders), 1)
        self.assertEqual(advanced.active_order_count, 1)
        self.assertEqual(options.orders, ())
        self.assertEqual(options.active_order_count, 1)
        self.assertEqual(standard.collection_id, evidence.collection_id)
        self.assertEqual(standard.next_cursor, None)
        filled = next(item for item in standard.orders if item.client_ref_id == FILLED_REF)
        self.assertEqual(filled.state, BrokerOrderState.FILLED)
        self.assertEqual(filled.fills[0].fee, Decimal("0.35"))
        self.assertEqual(
            filled.fills[0].provider_commission, Decimal("0.35")
        )
        self.assertEqual(
            filled.fills[0].provider_commission_currency, "USD"
        )
        self.assertEqual(filled.fills[0].executed_at, NOW - timedelta(minutes=1))
        entry = next(item for item in standard.orders if item.client_ref_id == ENTRY_REF)
        self.assertEqual(entry.side, BrokerSide.BUY)
        self.assertEqual(entry.order_type, EquityOrderType.LIMIT)
        self.assertEqual(entry.market_hours, MarketHours.REGULAR)
        self.assertEqual(entry.time_in_force, TimeInForce.GFD)
        stop = advanced.orders[0]
        self.assertEqual(stop.order_type, EquityOrderType.STOP_MARKET)
        self.assertEqual(stop.stop_price, Decimal("8.75"))
        self.assertEqual(stop.time_in_force, TimeInForce.GTC)

        request_names = [call[0] for call in self.requester.calls]
        self.assertEqual(
            request_names[:6],
            [
                "reqAccountSummary",
                "reqPositions",
                "reqAllOpenOrders",
                "reqCompletedOrders",
                "reqExecutions",
                "reqPnL",
            ],
        )
        completed_call = next(
            call for call in self.requester.calls if call[0] == "reqCompletedOrders"
        )
        self.assertIs(completed_call[1], False)
        self.assertFalse(any("place" in name.lower() or "cancelorder" in name.lower() for name in request_names))

    def test_sdk_1050_commission_and_fees_callback_is_accepted(self):
        self.requester.new_commission_callback = True
        self.collect()
        page = self.bridge.list_order_family_page(
            ACCOUNT, OrderFamily.STANDARD_EQUITY, None
        )
        filled = next(
            item
            for item in page.orders
            if item.client_ref_id == FILLED_REF
        )
        self.assertEqual(filled.fills[0].fee, Decimal("0.35"))

    def test_sdk_1050_error_time_callback_is_accepted_and_redacted(self):
        secret = "private broker error with account U9993103"
        self.callbacks.error(91, 1789394400, 2104, secret, "private-json")
        evidence = self.collect()
        self.assertIsNotNone(evidence.collection_id)
        self.assertNotIn(secret, repr(self.bridge))

    def test_exact_reference_recovery_never_invents_authoritative_absence(self):
        self.collect()
        result = self.bridge.lookup_equity_orders_by_client_ref(
            ACCOUNT, (FILLED_REF, MISSING_REF)
        )
        self.assertEqual(tuple(item.client_ref_id for item in result.found_orders), (FILLED_REF,))
        self.assertEqual(result.confirmed_absent_client_refs, ())
        self.assertEqual(result.not_seen_yet_client_refs, (MISSING_REF,))
        self.assertTrue(result.complete)

    def test_failed_new_collection_retires_prior_pages_and_reference_results(self):
        self.collect()
        self.assertTrue(
            self.bridge.lookup_equity_orders_by_client_ref(
                ACCOUNT, (FILLED_REF,)
            ).found_orders
        )

        self.requester.omit_end = "completed_orders"
        with self.assertRaisesRegex(BrokerCapabilityError, "completed_orders"):
            self.collect()
        with self.assertRaisesRegex(
            BrokerCapabilityError, "IBKR_READ_COLLECTION_UNAVAILABLE"
        ):
            self.bridge.list_order_family_page(
                ACCOUNT, OrderFamily.STANDARD_EQUITY, None
            )
        with self.assertRaisesRegex(
            BrokerCapabilityError, "IBKR_READ_COLLECTION_UNAVAILABLE"
        ):
            self.bridge.lookup_equity_orders_by_client_ref(
                ACCOUNT, (FILLED_REF,)
            )

    def test_all_callback_end_markers_are_required(self):
        self.requester.omit_end = "completed_orders"
        with self.assertRaisesRegex(BrokerCapabilityError, "completed_orders"):
            self.collect()
        self.assertIn("cancelAccountSummary", [call[0] for call in self.requester.calls])
        self.assertIn("cancelPositions", [call[0] for call in self.requester.calls])

    def test_commission_is_required_for_every_execution(self):
        self.requester.omit_commission = True
        with self.assertRaisesRegex(BrokerCapabilityError, "commission"):
            self.collect()

    def test_dedicated_current_day_realized_pnl_is_required(self):
        self.requester.omit_daily_pnl = True
        with self.assertRaisesRegex(
            BrokerCapabilityError,
            "daily_realized_pnl_retry_exhausted_no_callback",
        ):
            self.collect()
        requests = [call for call in self.requester.calls if call[0] == "reqPnL"]
        self.assertEqual(len(requests), 2)
        self.assertNotEqual(requests[0][1], requests[1][1])
        self.assertTrue(all(request[2:] == (ACCOUNT, "") for request in requests))
        cancelled = [call[1] for call in self.requester.calls if call[0] == "cancelPnL"]
        self.assertEqual(cancelled, [requests[0][1], requests[1][1]])

    def test_daily_pnl_retry_uses_fresh_id_and_ignores_retired_callback(self):
        request_ids = []

        def delayed_then_fresh(reqId, account, modelCode):
            self.requester.calls.append(("reqPnL", reqId, account, modelCode))
            request_ids.append(reqId)
            if len(request_ids) == 2:
                self.callbacks.pnl(
                    request_ids[0], Decimal("999"), Decimal("0"), Decimal("999")
                )
                self.callbacks.pnl(
                    request_ids[1], Decimal("12.34"), Decimal("0"), Decimal("12.34")
                )

        self.requester.reqPnL = delayed_then_fresh
        evidence = self.collect()
        self.assertEqual(evidence.snapshot.daily_realized_pnl, Decimal("12.34"))
        self.assertEqual(len(request_ids), 2)
        self.assertNotEqual(request_ids[0], request_ids[1])
        cancelled = [call[1] for call in self.requester.calls if call[0] == "cancelPnL"]
        self.assertEqual(cancelled, request_ids)

    def test_callback_delivered_inside_cancel_stays_bound_to_retired_id(self):
        request_ids = []

        def retry_with_distinct_value(reqId, account, modelCode):
            self.requester.calls.append(("reqPnL", reqId, account, modelCode))
            request_ids.append(reqId)
            if len(request_ids) == 2:
                self.callbacks.pnl(
                    reqId, Decimal("4.50"), Decimal("0"), Decimal("4.50")
                )

        def cancel_with_late_old_callback(reqId):
            self.requester.calls.append(("cancelPnL", reqId))
            self.callbacks.pnl(
                reqId, Decimal("999"), Decimal("0"), Decimal("999")
            )

        self.requester.reqPnL = retry_with_distinct_value
        self.requester.cancelPnL = cancel_with_late_old_callback
        evidence = self.collect()
        self.assertEqual(evidence.snapshot.daily_realized_pnl, Decimal("4.50"))
        self.assertEqual(len(request_ids), 2)

    def test_unset_daily_pnl_is_retryable_but_never_financial_evidence(self):
        sentinel = Decimal("1.7976931348623157e308")
        attempts = []

        def unset_then_value(reqId, account, modelCode):
            self.requester.calls.append(("reqPnL", reqId, account, modelCode))
            attempts.append(reqId)
            value = sentinel if len(attempts) == 1 else Decimal("7.25")
            self.callbacks.pnl(reqId, value, Decimal("0"), value)

        self.requester.reqPnL = unset_then_value
        evidence = self.collect()
        self.assertEqual(evidence.snapshot.daily_realized_pnl, Decimal("7.25"))
        self.assertEqual(len(attempts), 2)

    def test_unset_daily_pnl_retry_exhaustion_is_distinct_from_no_callback(self):
        self.requester.daily_pnl = Decimal("1.7976931348623157e308")
        with self.assertRaisesRegex(
            BrokerCapabilityError,
            "daily_realized_pnl_retry_exhausted_unavailable",
        ):
            self.collect()

    def test_valid_daily_pnl_followed_by_unset_fails_closed(self):
        def valid_then_unset(reqId, account, modelCode):
            self.requester.calls.append(("reqPnL", reqId, account, modelCode))
            self.callbacks.pnl(
                reqId, Decimal("3.25"), Decimal("0"), Decimal("3.25")
            )
            sentinel = Decimal("1.7976931348623157e308")
            self.callbacks.pnl(reqId, sentinel, Decimal("0"), sentinel)

        self.requester.reqPnL = valid_then_unset
        with self.assertRaisesRegex(
            BrokerCapabilityError, "daily_pnl_became_unavailable"
        ):
            self.collect()

    def test_freeze_revalidates_late_pnl_and_authentication_failures(self):
        cases = (
            (
                "unavailable",
                "daily_pnl_became_unavailable",
            ),
            (
                "nonfinite",
                "daily_pnl_nonfinite_or_shape",
            ),
            (
                "moved",
                "daily_pnl_moved",
            ),
            (
                "connection_closed",
                "IBKR_READ_CALLBACK_ERROR:1100:connection_closed",
            ),
            (
                "authentication_lost",
                "IBKR_READ_AUTHENTICATION_LOST",
            ),
        )
        for mode, expected in cases:
            with self.subTest(mode=mode):
                requester = FakeReadRequester()
                bridge = IbkrWholeAccountReadBridge(
                    requester=requester,
                    exact_account_id=ACCOUNT,
                    account_masked=MASK,
                    timeout_seconds=0.05,
                    clock=lambda: NOW,
                )
                callbacks = bridge.open_generation(1)
                requester.callbacks = callbacks
                callbacks.managedAccounts(ACCOUNT)
                original_wait = bridge._wait_for_collection

                def wait_then_invalidate(collection, deadline):
                    original_wait(collection, deadline)
                    if mode == "unavailable":
                        callbacks.pnl(
                            collection.pnl_request_id,
                            Decimal("1.7976931348623157e308"),
                            Decimal("0"),
                            Decimal("1.7976931348623157e308"),
                        )
                    elif mode == "nonfinite":
                        callbacks.pnl(
                            collection.pnl_request_id,
                            Decimal("0"),
                            Decimal("0"),
                            Decimal("NaN"),
                        )
                    elif mode == "moved":
                        callbacks.pnl(
                            collection.pnl_request_id,
                            Decimal("13.00"),
                            Decimal("0"),
                            Decimal("13.00"),
                        )
                    elif mode == "connection_closed":
                        callbacks.connectionClosed()
                    else:
                        callbacks.managedAccounts("DU7654321")

                bridge._wait_for_collection = wait_then_invalidate
                with self.assertRaisesRegex(BrokerCapabilityError, expected):
                    bridge.get_account_base(ACCOUNT)
                with self.assertRaisesRegex(
                    BrokerCapabilityError, "IBKR_READ_COLLECTION_UNAVAILABLE"
                ):
                    bridge.list_order_family_page(
                        ACCOUNT, OrderFamily.STANDARD_EQUITY, None
                    )

    def test_complete_collection_cannot_escape_after_dispatch_or_freeze_deadline(self):
        for phase in ("account_summary_dispatch", "collection_freeze"):
            with self.subTest(phase=phase):
                requester = FakeReadRequester()
                bridge = IbkrWholeAccountReadBridge(
                    requester=requester,
                    exact_account_id=ACCOUNT,
                    account_masked=MASK,
                    timeout_seconds=0.02,
                    clock=lambda: NOW,
                )
                callbacks = bridge.open_generation(1)
                requester.callbacks = callbacks
                callbacks.managedAccounts(ACCOUNT)
                if phase == "account_summary_dispatch":
                    original_request = requester.reqAccountSummary

                    def delayed_request(*arguments):
                        original_request(*arguments)
                        time.sleep(0.04)

                    requester.reqAccountSummary = delayed_request
                else:
                    original_wait = bridge._wait_for_collection

                    def delayed_freeze(collection, deadline):
                        original_wait(collection, deadline)
                        time.sleep(0.04)

                    bridge._wait_for_collection = delayed_freeze
                with self.assertRaisesRegex(
                    BrokerCapabilityError, f"IBKR_READ_TIMEOUT:{phase}"
                ):
                    bridge.get_account_base(ACCOUNT)

    def test_retry_cancel_or_dispatch_cannot_extend_overall_deadline(self):
        for phase in ("cancel", "dispatch"):
            with self.subTest(phase=phase):
                requester = FakeReadRequester()
                bridge = IbkrWholeAccountReadBridge(
                    requester=requester,
                    exact_account_id=ACCOUNT,
                    account_masked=MASK,
                    timeout_seconds=0.08,
                    clock=lambda: NOW,
                )
                callbacks = bridge.open_generation(1)
                requester.callbacks = callbacks
                callbacks.managedAccounts(ACCOUNT)
                attempts = []

                def delayed_retry(req_id, account, model_code):
                    requester.calls.append(("reqPnL", req_id, account, model_code))
                    attempts.append(req_id)
                    if len(attempts) == 2:
                        callbacks.pnl(
                            req_id, Decimal("8"), Decimal("0"), Decimal("8")
                        )
                        if phase == "dispatch":
                            time.sleep(0.08)

                requester.reqPnL = delayed_retry
                if phase == "cancel":
                    def delayed_cancel(req_id):
                        requester.calls.append(("cancelPnL", req_id))
                        time.sleep(0.08)

                    requester.cancelPnL = delayed_cancel
                with self.assertRaisesRegex(
                    BrokerCapabilityError,
                    f"IBKR_READ_TIMEOUT:daily_realized_pnl_retry_{phase}",
                ):
                    bridge.get_account_base(ACCOUNT)
                self.assertEqual(
                    len(attempts),
                    1 if phase == "cancel" else 2,
                )

    def test_daily_pnl_retry_lifecycle_reports_exact_dispatch_and_cancel_failures(self):
        def initial_failure(*_args):
            raise OSError("private initial dispatch details")

        self.requester.reqPnL = initial_failure
        with patch("titan_brain.live.broker.ibkr_read.time.monotonic", return_value=0.0):
            with self.assertRaisesRegex(
                BrokerCapabilityError, "IBKR_READ_PNL_INITIAL_DISPATCH_FAILED"
            ) as caught:
                self.collect()
        self.assertNotIn("private initial", str(caught.exception))

        for phase in ("cancel", "dispatch"):
            with self.subTest(phase=phase):
                requester = FakeReadRequester()
                requester.omit_daily_pnl = True
                bridge = IbkrWholeAccountReadBridge(
                    requester=requester,
                    exact_account_id=ACCOUNT,
                    account_masked=MASK,
                    timeout_seconds=0.02,
                    clock=lambda: NOW,
                )
                callbacks = bridge.open_generation(1)
                requester.callbacks = callbacks
                callbacks.managedAccounts(ACCOUNT)
                if phase == "cancel":
                    requester.cancelPnL = lambda *_args: (_ for _ in ()).throw(
                        OSError("private cancel details")
                    )
                    expected = "IBKR_READ_PNL_RETRY_CANCEL_FAILED"
                else:
                    calls = 0

                    def fail_retry(reqId, account, modelCode):
                        nonlocal calls
                        requester.calls.append(("reqPnL", reqId, account, modelCode))
                        calls += 1
                        if calls == 2:
                            raise OSError("private retry details")

                    requester.reqPnL = fail_retry
                    expected = "IBKR_READ_PNL_RETRY_DISPATCH_FAILED"
                # Finite callbacks are synchronous; advance only the collection's
                # condition wait so scheduling cannot consume its 20 ms deadline.
                monotonic = [0.0]

                def advance_wait(timeout):
                    self.assertGreater(timeout, 0.0)
                    monotonic[0] += timeout

                with patch(
                    "titan_brain.live.broker.ibkr_read.time.monotonic",
                    side_effect=lambda: monotonic[0],
                ), patch.object(bridge._condition, "wait", side_effect=advance_wait) as wait:
                    with self.assertRaisesRegex(BrokerCapabilityError, expected) as error:
                        bridge.get_account_base(ACCOUNT)
                wait.assert_called_once()
                self.assertAlmostEqual(monotonic[0], 0.01)
                self.assertNotIn("private", str(error.exception))

    def test_account_summary_realized_pnl_is_not_accepted_as_daily_authority(self):
        original = self.requester.reqAccountSummary

        def with_ambiguous_realized(reqId, groupName, tags):
            original(reqId, groupName, tags)
            self.callbacks.accountSummary(
                reqId, ACCOUNT, "RealizedPnL", "999.99", "USD"
            )

        self.requester.reqAccountSummary = with_ambiguous_realized
        evidence = self.collect()
        self.assertEqual(evidence.snapshot.daily_realized_pnl, Decimal("12.34"))
        summary_request = next(
            call for call in self.requester.calls if call[0] == "reqAccountSummary"
        )
        self.assertNotIn("RealizedPnL", summary_request[3].split(","))

    def test_unset_double_is_not_accepted_as_daily_pnl(self):
        self.requester.daily_pnl = Decimal("NaN")
        with self.assertRaisesRegex(
            BrokerCapabilityError, "daily_pnl_nonfinite_or_shape"
        ):
            self.collect()

    def test_error_text_and_advanced_json_are_discarded(self):
        secret = "sensitive broker account text"
        self.requester.inject_error = (200, secret)
        with self.assertRaises(BrokerCapabilityError) as caught:
            self.collect()
        self.assertNotIn(secret, str(caught.exception))
        self.assertEqual(self.bridge.sanitized_errors[-1].code, 200)
        self.assertEqual(self.bridge.sanitized_errors[-1].scope, "request")
        self.assertFalse(hasattr(self.bridge.sanitized_errors[-1], "message"))

    def test_read_only_cause_requires_both_exact_provider_text_and_code(self):
        message = (
            "Error validating request:-'b_' : cause - "
            "The API interface is currently in Read-Only mode."
        )
        for arguments in (
            (321, message),
            (321, message, "private account JSON"),
            (1789394400, 321, message),
            (1789394400, 321, message, "private account JSON"),
            (1789394400, 321, message.replace("request:", "request."), "private account JSON"),
        ):
            with self.subTest(shape=len(arguments)):
                self.assertEqual(
                    classify_ibkr_error_callback(arguments), (321, "API_READ_ONLY")
                )
        for code, text in (
            (321, "different validation failure"),
            (200, message),
            (321, "not " + message),
            (321, message + " private account text"),
            (321, " " * 4097 + message),
            (321, True),
        ):
            with self.subTest(code=code, text_type=type(text)):
                self.assertEqual(
                    classify_ibkr_error_callback((code, text, "")),
                    (code, "UNSPECIFIED"),
                )

        class PrivatePayload:
            def __str__(self):
                raise AssertionError("must not stringify broker payload")

        self.assertEqual(
            classify_ibkr_error_callback((321, PrivatePayload(), "")),
            (321, "UNSPECIFIED"),
        )

    def test_recognized_read_only_error_is_still_collection_fatal(self):
        self.requester.inject_error = (
            321, "The API interface is currently in Read-Only mode."
        )
        with self.assertRaisesRegex(BrokerCapabilityError, "321:request:API_READ_ONLY"):
            self.collect()
        error = self.bridge.sanitized_errors[-1]
        self.assertEqual(error.reason, "API_READ_ONLY")
        self.assertNotIn("private-json", repr(error))
        self.assertFalse(hasattr(error, "message"))

    def test_reconnect_generation_ignores_stale_callbacks(self):
        old = self.callbacks
        new = self.bridge.open_generation(2)
        self.requester.callbacks = new
        old.managedAccounts(ACCOUNT)
        with self.assertRaisesRegex(BrokerCapabilityError, "NOT_AUTHENTICATED"):
            self.collect()
        new.managedAccounts(ACCOUNT)
        old.connectionClosed()
        warning_secret = "current-generation private warning"
        self.requester.stop_order_state = state("Submitted", warning_secret)
        current_request = self.requester.reqAllOpenOrders

        def with_stale_clean_order():
            current_request()
            old.openOrder(
                12,
                contract(),
                order(
                    12,
                    action="SELL",
                    order_type="STP",
                    quantity="2",
                    tif="GTC",
                    order_ref=STOP_REF,
                    parent_id=11,
                    perm_id=101,
                    lmt_price=Decimal("0"),
                    aux_price=Decimal("8.75"),
                ),
                state("Submitted"),
            )

        self.requester.reqAllOpenOrders = with_stale_clean_order
        evidence = self.collect()
        self.assertEqual(evidence.snapshot.account_state, "active")
        self.assertTrue(evidence.snapshot.auth_point_in_time)
        stop = self.bridge.list_order_family_page(
            ACCOUNT, OrderFamily.ADVANCED_EQUITY, None
        ).orders[0]
        self.assertEqual(stop.state, BrokerOrderState.UNKNOWN)
        self.assertFalse(is_verified_working_protection(stop))
        self.assertNotIn(warning_secret, repr(self.bridge._last))

    def test_connection_close_invalidates_authentication_and_active_collection(self):
        self.callbacks.connectionClosed()
        with self.assertRaisesRegex(BrokerCapabilityError, "NOT_AUTHENTICATED"):
            self.collect()

    def test_idle_session_error_invalidates_authentication(self):
        self.callbacks.error(-1, 1100, "private session text", "")
        with self.assertRaisesRegex(BrokerCapabilityError, "NOT_AUTHENTICATED"):
            self.collect()
        self.assertEqual(self.bridge.sanitized_errors[-1].code, 1100)
        self.assertEqual(self.bridge.sanitized_errors[-1].scope, "session")

    def test_idle_authentication_loss_retires_all_prior_collection_inputs(self):
        for loss in ("closed", "session_error", "account_mismatch"):
            with self.subTest(loss=loss):
                self.callbacks.managedAccounts(ACCOUNT)
                evidence = self.collect()
                if loss == "closed":
                    self.callbacks.connectionClosed()
                elif loss == "session_error":
                    self.callbacks.error(-1, 1100, "private session text", "")
                else:
                    self.callbacks.managedAccounts("DU7654321")
                with self.assertRaisesRegex(
                    BrokerContractViolation, "VALUATION_COLLECTION_CHANGED"
                ):
                    self.bridge.position_valuation_inputs(evidence.collection_id)
                with self.assertRaisesRegex(BrokerCapabilityError, "NOT_AUTHENTICATED"):
                    self.collect()
                # Even a later matching account callback cannot resurrect the
                # retired result. A new complete collection is required.
                self.callbacks.managedAccounts(ACCOUNT)
                with self.assertRaisesRegex(BrokerCapabilityError, "COLLECTION_UNAVAILABLE"):
                    self.bridge.list_order_family_page(
                        ACCOUNT, OrderFamily.STANDARD_EQUITY, None
                    )
                with self.assertRaisesRegex(BrokerCapabilityError, "COLLECTION_UNAVAILABLE"):
                    self.bridge.lookup_equity_orders_by_client_ref(ACCOUNT, (ENTRY_REF,))

    def test_managed_account_mismatch_never_authenticates(self):
        bridge = IbkrWholeAccountReadBridge(
            requester=self.requester,
            exact_account_id=ACCOUNT,
            account_masked=MASK,
            timeout_seconds=0.02,
            clock=lambda: NOW,
        )
        callbacks = bridge.open_generation(1)
        callbacks.managedAccounts("DU7654321")
        with self.assertRaisesRegex(BrokerCapabilityError, "NOT_AUTHENTICATED"):
            bridge.get_account_base(ACCOUNT)

    def test_duplicate_client_reference_is_unresolved_contract_violation(self):
        self.requester.duplicate_ref = True
        self.collect()
        with self.assertRaisesRegex(BrokerContractViolation, "duplicate orderRef"):
            self.bridge.lookup_equity_orders_by_client_ref(ACCOUNT, (ENTRY_REF,))

    def test_short_or_unknown_material_position_fails_closed(self):
        self.requester.short_position = True
        with self.assertRaisesRegex(BrokerContractViolation, "SHORT_POSITION"):
            self.collect()

    def test_unknown_material_order_family_fails_closed(self):
        self.requester.exotic_order = True
        with self.assertRaisesRegex(BrokerContractViolation, "ORDER_FAMILY"):
            self.collect()

    def test_wrong_exact_account_and_pagination_are_rejected(self):
        with self.assertRaisesRegex(BrokerContractViolation, "account binding"):
            self.bridge.get_account_base("DU7654321")
        self.collect()
        with self.assertRaisesRegex(BrokerContractViolation, "exactly one page"):
            self.bridge.list_order_family_page(
                ACCOUNT, OrderFamily.STANDARD_EQUITY, "unexpected"
            )

    def test_informational_farm_status_does_not_abort_collection(self):
        self.requester.inject_error = (2104, "market data farm is okay")
        evidence = self.collect()
        self.assertEqual(evidence.snapshot.funds.total_value, Decimal("1000"))
        self.assertEqual(self.bridge.sanitized_errors[-1].scope, "informational")

    def test_blocking_order_warning_is_unknown_and_never_working_protection(self):
        warning_secret = f"private stop warning for {ACCOUNT}"
        self.requester.stop_order_state = state("Submitted", warning_secret)

        self.collect()

        page = self.bridge.list_order_family_page(
            ACCOUNT, OrderFamily.ADVANCED_EQUITY, None
        )
        stop = page.orders[0]
        self.assertEqual(stop.state, BrokerOrderState.UNKNOWN)
        self.assertFalse(is_verified_working_protection(stop))
        for public_or_normalized in (
            repr(page),
            repr(self.bridge._last),
            repr(self.bridge._order_fact_times),
            repr(self.bridge.sanitized_errors),
        ):
            self.assertNotIn(warning_secret, public_or_normalized)

    def test_order_warning_preserves_cancelled_and_execution_proven_terminal_states(self):
        warning_secret = f"private terminal warning for {ACCOUNT}"
        self.requester.stop_order_state = state("Cancelled", warning_secret)
        self.requester.filled_order_state = state("Filled", warning_secret)

        self.collect()

        advanced = self.bridge.list_order_family_page(
            ACCOUNT, OrderFamily.ADVANCED_EQUITY, None
        )
        standard = self.bridge.list_order_family_page(
            ACCOUNT, OrderFamily.STANDARD_EQUITY, None
        )
        cancelled_stop = advanced.orders[0]
        filled = next(
            item for item in standard.orders if item.client_ref_id == FILLED_REF
        )
        self.assertEqual(cancelled_stop.state, BrokerOrderState.CANCELLED)
        self.assertEqual(advanced.active_order_count, 0)
        self.assertFalse(is_verified_working_protection(cancelled_stop))
        self.assertEqual(filled.state, BrokerOrderState.FILLED)
        self.assertEqual(filled.broker_updated_at, NOW - timedelta(minutes=1))
        self.assertNotIn(warning_secret, repr(self.bridge._last))

    def test_missing_malformed_or_unreadable_order_warning_fails_closed(self):
        warning_secret = f"private unreadable warning for {ACCOUNT}"

        class UnreadableWarning:
            status = "Submitted"
            completedTime = ""

            @property
            def warningText(self):
                raise RuntimeError(warning_secret)

            def __str__(self):
                raise AssertionError("untrusted order state must not be stringified")

        cases = (
            ("missing", SimpleNamespace(status="Submitted", completedTime="")),
            ("non_string", state("Submitted", True)),
            ("oversized", state("Submitted", "x" * 4097)),
            ("unreadable", UnreadableWarning()),
        )
        for name, order_state in cases:
            with self.subTest(name=name):
                self.requester.stop_order_state = order_state
                self.collect()
                stop = self.bridge.list_order_family_page(
                    ACCOUNT, OrderFamily.ADVANCED_EQUITY, None
                ).orders[0]
                self.assertEqual(stop.state, BrokerOrderState.UNKNOWN)
                self.assertFalse(is_verified_working_protection(stop))
                self.assertNotIn(warning_secret, repr(self.bridge._last))

    def test_duplicate_order_callbacks_cannot_clear_or_hide_a_warning(self):
        warning_secret = f"private duplicate warning for {ACCOUNT}"
        self.requester.stop_order_state = state("Submitted", warning_secret)
        self.requester.stop_duplicate_order_states = (state("Submitted"),)
        self.collect()
        later_clean_stop = self.bridge.list_order_family_page(
            ACCOUNT, OrderFamily.ADVANCED_EQUITY, None
        ).orders[0]
        self.assertEqual(later_clean_stop.state, BrokerOrderState.UNKNOWN)

        requester = FakeReadRequester()
        clock = MutableClock()
        bridge = IbkrWholeAccountReadBridge(
            requester=requester,
            exact_account_id=ACCOUNT,
            account_masked=MASK,
            timeout_seconds=0.02,
            clock=clock,
        )
        callbacks = bridge.open_generation(1)
        requester.callbacks = callbacks
        callbacks.managedAccounts(ACCOUNT)
        requester.stop_order_state = state("Submitted")
        requester.stop_duplicate_order_states = (
            state("Submitted", warning_secret),
        )
        requester.before_stop_duplicate = lambda: setattr(
            clock, "value", NOW - timedelta(seconds=1)
        )
        requester.after_stop_duplicate = lambda: setattr(clock, "value", NOW)

        bridge.get_account_base(ACCOUNT)
        older_warning_stop = bridge.list_order_family_page(
            ACCOUNT, OrderFamily.ADVANCED_EQUITY, None
        ).orders[0]
        self.assertEqual(older_warning_stop.state, BrokerOrderState.UNKNOWN)
        self.assertFalse(is_verified_working_protection(older_warning_stop))
        self.assertNotIn(warning_secret, repr(bridge._order_fact_times))

    def test_warning_fingerprint_tracks_presence_but_not_private_text(self):
        requester = FakeReadRequester()
        clock = TickingClock()
        bridge = IbkrWholeAccountReadBridge(
            requester=requester,
            exact_account_id=ACCOUNT,
            account_masked=MASK,
            timeout_seconds=0.02,
            clock=clock,
        )
        callbacks = bridge.open_generation(1)
        requester.callbacks = callbacks
        callbacks.managedAccounts(ACCOUNT)

        first_secret = f"first private warning for {ACCOUNT}"
        second_secret = f"second private warning for {ACCOUNT}"
        requester.stop_order_state = state("Submitted", first_secret)
        first_observation = bridge.get_account_base(ACCOUNT)
        first_stop = bridge.list_order_family_page(
            ACCOUNT, OrderFamily.ADVANCED_EQUITY, None
        ).orders[0]

        requester.stop_order_state = state("Submitted", second_secret)
        second_observation = bridge.get_account_base(ACCOUNT)
        second_stop = bridge.list_order_family_page(
            ACCOUNT, OrderFamily.ADVANCED_EQUITY, None
        ).orders[0]

        self.assertEqual(first_stop.state, BrokerOrderState.UNKNOWN)
        self.assertEqual(second_stop.state, BrokerOrderState.UNKNOWN)
        self.assertEqual(
            first_stop.broker_updated_at, second_stop.broker_updated_at
        )
        self.assertEqual(
            first_observation.order_event_watermark,
            second_observation.order_event_watermark,
        )

        requester.stop_order_state = state("Submitted")
        third_observation = bridge.get_account_base(ACCOUNT)
        third_stop = bridge.list_order_family_page(
            ACCOUNT, OrderFamily.ADVANCED_EQUITY, None
        ).orders[0]
        self.assertEqual(third_stop.state, BrokerOrderState.CONFIRMED)
        self.assertTrue(is_verified_working_protection(third_stop))
        self.assertGreater(
            third_stop.broker_updated_at, second_stop.broker_updated_at
        )
        self.assertNotEqual(
            third_observation.order_event_watermark,
            second_observation.order_event_watermark,
        )
        for warning_secret in (first_secret, second_secret):
            self.assertNotIn(warning_secret, repr(bridge._order_fact_times))

    def test_unreadable_completed_time_is_a_sanitized_collection_failure(self):
        completed_time_secret = f"private completed time for {ACCOUNT}"

        class UnreadableCompletedTime:
            status = "Submitted"
            warningText = ""

            @property
            def completedTime(self):
                raise RuntimeError(completed_time_secret)

        self.requester.stop_order_state = UnreadableCompletedTime()
        with self.assertRaisesRegex(
            BrokerCapabilityError, "IBKR_READ_CALLBACK_ERROR:0:order_shape"
        ) as caught:
            self.collect()
        self.assertNotIn(completed_time_secret, str(caught.exception))
        self.assertNotIn(completed_time_secret, repr(self.bridge.sanitized_errors))

    def test_unchanged_open_order_fact_keeps_stable_observed_timestamp(self):
        requester = FakeReadRequester()
        clock = TickingClock()
        bridge = IbkrWholeAccountReadBridge(
            requester=requester,
            exact_account_id=ACCOUNT,
            account_masked=MASK,
            timeout_seconds=0.02,
            clock=clock,
        )
        callbacks = bridge.open_generation(1)
        requester.callbacks = callbacks
        callbacks.managedAccounts(ACCOUNT)
        first = bridge.get_account_base(ACCOUNT)
        first_page = bridge.list_order_family_page(
            ACCOUNT, OrderFamily.STANDARD_EQUITY, None
        )
        second = bridge.get_account_base(ACCOUNT)
        second_page = bridge.list_order_family_page(
            ACCOUNT, OrderFamily.STANDARD_EQUITY, None
        )
        first_entry = next(item for item in first_page.orders if item.client_ref_id == ENTRY_REF)
        second_entry = next(item for item in second_page.orders if item.client_ref_id == ENTRY_REF)
        self.assertEqual(first_entry.broker_updated_at, second_entry.broker_updated_at)
        self.assertLess(first_entry.received_at, second_entry.received_at)
        self.assertLess(first.request_completed_at, second.request_started_at)


if __name__ == "__main__":
    unittest.main()
