"""Hermetic official-callback tests; no IBKR socket or order mutation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sys
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


def state(status):
    return SimpleNamespace(status=status)


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
        self.callbacks.openOrder(12, contract(), stop, state("Submitted"))
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
        self.callbacks.completedOrder(contract(), filled, state("Filled"))
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
        with self.assertRaisesRegex(BrokerCapabilityError, "daily_realized_pnl"):
            self.collect()
        request = next(call for call in self.requester.calls if call[0] == "reqPnL")
        self.assertEqual(request[2:], (ACCOUNT, ""))
        self.assertIn("cancelPnL", [call[0] for call in self.requester.calls])

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
        self.requester.daily_pnl = Decimal("1.7976931348623157e308")
        with self.assertRaisesRegex(BrokerCapabilityError, "daily_pnl_shape"):
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
        evidence = self.collect()
        self.assertEqual(evidence.snapshot.account_state, "active")
        self.assertTrue(evidence.snapshot.auth_point_in_time)

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
