"""No sockets, credentials, orders, or invented valuation authority."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from titan_brain.live.broker.base import AccountSnapshot, FundsSnapshot, PositionSnapshot
from titan_brain.live.broker.ibkr_position_valuation import (
    IbkrPositionValuationCollector, PositionValuationContract, PositionValuationError,
)
from titan_brain.live.risk_runtime import remaining_position_stop_downside
from tests import test_live_ibkr_runtime as runtime_fixtures


NOW = datetime(2026, 9, 14, 18, tzinfo=timezone.utc)
ACCOUNT = "U9993103"
COLLECTION = "a" * 64


def contract(**changes):
    values = dict(conId=1234, symbol="XYZ", secType="STK", currency="USD")
    values.update(changes)
    return SimpleNamespace(**values)


def snapshot(**changes):
    values = dict(account_masked="****3103", observed_at=NOW, received_at=NOW,
        account_state="active", account_type="no_borrow_margin",
        funds=FundsSnapshot(total_value=Decimal("1200"), cash=Decimal("1000"),
            buying_power=Decimal("1000"), unleveraged_buying_power=Decimal("1000")),
        equity_positions=(PositionSnapshot("XYZ", Decimal("10"), Decimal("10")),),
        equity_orders=(), option_position_count=0, option_order_count=0, advanced_order_count=0,
        standard_equity_positions_complete=True, standard_equity_orders_complete=True,
        option_positions_complete=True, option_orders_complete=True, advanced_orders_complete=True,
        auth_point_in_time=True)
    values.update(changes)
    return AccountSnapshot(**values)


class Requester:
    def __init__(self):
        self.calls, self.value, self.quantity = [], 200.0, Decimal("10")
        self.emit, self.fail_cancel, self.hook = True, False, None

    def reqPnLSingle(self, request_id, account, model, con_id):
        self.calls.append(("reqPnLSingle", request_id, account, model, con_id))
        if self.hook:
            self.hook(request_id)
        if self.emit:
            self.callbacks.pnlSingle(request_id, self.quantity, 0, 0, 0, self.value)

    def cancelPnLSingle(self, request_id):
        self.calls.append(("cancelPnLSingle", request_id))
        if self.fail_cancel:
            raise RuntimeError("secret provider text")


class PositionValuationTests(unittest.TestCase):
    def setUp(self):
        self.clock = NOW
        self.requester = Requester()
        self.collector = IbkrPositionValuationCollector(requester=self.requester,
            exact_account_id=ACCOUNT, account_masked="****3103", clock=lambda: self.clock,
            timeout_seconds=.01)
        self.callbacks = self.collector.open_generation(1)
        self.requester.callbacks = self.callbacks
        self.callbacks.managedAccounts(ACCOUNT)
        self.contract = PositionValuationContract.from_callback(contract(), Decimal("10"), NOW)

    def collect(self, **changes):
        values = dict(contracts=(self.contract,), snapshot=snapshot(), collection_id=COLLECTION)
        values.update(changes)
        return self.collector.collect(**values)

    def test_actual_subscription_receipt_is_not_source_time_or_order_authority(self):
        self.assertEqual(self.requester.calls, [])
        report = self.collect()
        self.assertTrue(report.cash_plus_positions_matches_nlv)
        self.assertEqual(report.samples[0].market_value, Decimal("200"))
        self.assertIsNone(report.samples[0].provider_observed_at)
        self.assertEqual(report.samples[0].received_at, NOW)
        self.assertFalse(report.remaining_risk_authorized)
        self.assertIn("IBKR_POSITION_VALUATION_NLV_EPOCH_UNPROVEN", report.failures)
        self.assertIn("IBKR_POSITION_VALUATION_REMAINING_FEES_UNPROVEN", report.failures)
        self.assertEqual([row[0] for row in self.requester.calls], ["reqPnLSingle", "cancelPnLSingle"])
        self.assertNotIn(ACCOUNT, json.dumps(report.public_dict()))

    def test_wrong_account_or_missing_inventory_never_dispatches(self):
        for changes in ({"snapshot": snapshot(account_masked="****7153")}, {"contracts": ()},
                        {"contracts": (self.contract, self.contract)}, {"collection_id": ACCOUNT}):
            with self.subTest(changes=changes), self.assertRaises(PositionValuationError):
                self.collect(**changes)
        self.assertEqual(self.requester.calls, [])

    def test_foreign_currency_missing_conid_or_fractional_contract_is_rejected(self):
        for raw in (contract(currency="EUR"), contract(conId=0), contract(conId=True), contract(secType="OPT")):
            with self.assertRaises(PositionValuationError):
                PositionValuationContract.from_callback(raw, Decimal("10"), NOW)
        with self.assertRaises(PositionValuationError):
            PositionValuationContract.from_callback(contract(), Decimal(".5"), NOW)
        with self.assertRaises(PositionValuationError):
            replace(self.contract, currency="EUR")

    def test_stale_or_future_inputs_are_not_refreshed_by_callback_receipt(self):
        for stamp in (NOW - timedelta(seconds=6), NOW + timedelta(seconds=1)):
            with self.assertRaisesRegex(PositionValuationError, "INPUT_RECEIPT_STALE"):
                self.collect(contracts=(replace(self.contract, positions_received_at=stamp),))
        self.assertEqual(self.requester.calls, [])

    def test_nonfinite_unset_negative_and_mismatched_quantity_fail_and_cancel(self):
        for value in (float("nan"), float("inf"), 1.7976931348623157e308, -1, True):
            self.requester.value = value
            with self.subTest(value=value), self.assertRaisesRegex(PositionValuationError, "NUMBER_INVALID"):
                self.collect()
            self.assertEqual(self.requester.calls[-1][0], "cancelPnLSingle")
        self.requester.value, self.requester.quantity = 200, Decimal("9")
        with self.assertRaisesRegex(PositionValuationError, "POSITION_QUANTITY_CHANGED"):
            self.collect()

    def test_missing_wrong_request_and_stale_generation_callbacks_timeout(self):
        self.requester.emit = False
        self.requester.hook = lambda request: self.callbacks.pnlSingle(request + 1, 10, 0, 0, 0, 200)
        with self.assertRaisesRegex(PositionValuationError, "CALLBACK_TIMEOUT"):
            self.collect()
        old = self.callbacks
        self.callbacks = self.collector.open_generation(2)
        self.callbacks.managedAccounts(ACCOUNT)
        self.requester.hook = lambda request: old.pnlSingle(request, 10, 0, 0, 0, 200)
        with self.assertRaisesRegex(PositionValuationError, "CALLBACK_TIMEOUT"):
            self.collect()

    def test_generation_loss_error_text_and_failed_cleanup_are_redacted(self):
        self.requester.hook = lambda request: self.callbacks.error(request, 500, ACCOUNT, "secret")
        with self.assertRaisesRegex(PositionValuationError, "CALLBACK_FAILED") as error:
            self.collect()
        self.assertNotIn(ACCOUNT, str(error.exception))
        self.requester.hook = lambda request: self.collector.open_generation(2)
        with self.assertRaisesRegex(PositionValuationError, "GENERATION_LOST"):
            self.collect()
        self.callbacks = self.collector.open_generation(3)
        self.callbacks.managedAccounts(ACCOUNT)
        self.requester.callbacks, self.requester.hook, self.requester.fail_cancel = self.callbacks, None, True
        with self.assertRaisesRegex(PositionValuationError, "SUBSCRIPTION_CLEANUP_FAILED"):
            self.collect()

    def test_accounting_mismatch_incomplete_coverage_and_delayed_receipt_never_authorize(self):
        self.requester.value = 190
        self.requester.hook = lambda request: setattr(self, "clock", NOW + timedelta(seconds=6))
        report = self.collect(snapshot=snapshot(standard_equity_orders_complete=False))
        self.assertFalse(report.cash_plus_positions_matches_nlv)
        self.assertFalse(report.remaining_risk_authorized)
        for code in ("RECEIPT_STALE", "WHOLE_ACCOUNT_COVERAGE_UNPROVEN", "CASH_PLUS_POSITIONS_NLV_MISMATCH"):
            self.assertIn("IBKR_POSITION_VALUATION_" + code, report.failures)

    def test_position_receipt_must_remain_fresh_at_collection_completion(self):
        # Inventory is admissible at start but ages out before the value reply.
        # Current sample receipt must not refresh the earlier position fact.
        original = replace(self.contract, positions_received_at=NOW - timedelta(seconds=4))
        self.requester.hook = lambda request: setattr(self, "clock", NOW + timedelta(seconds=2))
        report = self.collect(contracts=(original,))
        self.assertTrue(report.cash_plus_positions_matches_nlv)
        self.assertIn("IBKR_POSITION_VALUATION_RECEIPT_STALE", report.failures)
        self.assertFalse(report.remaining_risk_authorized)
        self.assertIsNone(report.samples[0].provider_observed_at)
        self.assertEqual(report.samples[0].contract.positions_received_at, NOW - timedelta(seconds=4))

    def test_duplicate_broker_positions_or_unauthenticated_account_fail_before_request(self):
        position = snapshot().equity_positions[0]
        with self.assertRaisesRegex(PositionValuationError, "ACCOUNT_POSITION_SCOPE_UNPROVEN"):
            self.collect(snapshot=snapshot(equity_positions=(position, position)))
        self.callbacks.managedAccounts("U8887153")
        with self.assertRaisesRegex(PositionValuationError, "SESSION_UNAVAILABLE"):
            self.collect()
        self.assertEqual(self.requester.calls, [])

    def test_reentrant_collection_cannot_replace_active_request_or_skip_cleanup(self):
        nested = []
        def reenter(request):
            try:
                self.collect()
            except PositionValuationError as exc:
                nested.append(str(exc))
        self.requester.hook = reenter
        report = self.collect()
        self.assertEqual(nested, ["IBKR_POSITION_VALUATION_SESSION_UNAVAILABLE"])
        self.assertEqual(len(report.samples), 1)
        self.assertEqual([row[0] for row in self.requester.calls], ["reqPnLSingle", "cancelPnLSingle"])
        self.assertIsNone(self.collector._active)

    def test_dispatch_error_cancels_uncertain_subscription_and_future_collect_can_recover(self):
        def fail(request):
            raise RuntimeError(ACCOUNT)
        self.requester.hook = fail
        with self.assertRaisesRegex(PositionValuationError, "REQUEST_FAILED") as error:
            self.collect()
        self.assertNotIn(ACCOUNT, str(error.exception))
        self.assertEqual(self.requester.calls[-1][0], "cancelPnLSingle")
        self.assertIsNone(self.collector._active)
        self.requester.hook = None
        self.assertFalse(self.collect().remaining_risk_authorized)

    def test_malformed_callback_ids_are_ignored_and_late_conflicting_callback_fails(self):
        def malformed(request):
            self.callbacks.pnlSingle([], 10, 0, 0, 0, 200)
            self.callbacks.error({}, 500, "secret", "")
        self.requester.hook = malformed
        self.assertFalse(self.collect().remaining_risk_authorized)
        def conflict(request):
            self.callbacks.pnlSingle(request, 10, 0, 0, 0, 200)
            self.callbacks.pnlSingle(request, 9, 0, 0, 0, 180)
        self.requester.hook = conflict
        with self.assertRaisesRegex(PositionValuationError, "POSITION_QUANTITY_CHANGED"):
            self.collect()


class RuntimePositionValuationTests(unittest.TestCase):
    setUp = runtime_fixtures.IbkrOfficialRuntimeTests.setUp
    tearDown = runtime_fixtures.IbkrOfficialRuntimeTests.tearDown
    make = runtime_fixtures.IbkrOfficialRuntimeTests.make

    def test_real_runtime_routes_pnl_single_on_read_client_without_contract_or_order_requests(self):
        def positions(client):
            client.calls.append(("reqPositions",))
            client.wrapper.position(ACCOUNT, contract(), Decimal("10"), 19.0)
            client.wrapper.positionEnd()
        def pnl(client, request_id, account, model, con_id):
            client.calls.append(("reqPnLSingle", request_id, account, model, con_id))
            client.wrapper.pnlSingle(request_id, Decimal("10"), 0, 0, 0, 200.0)
        def cancel(client, request_id):
            client.calls.append(("cancelPnLSingle", request_id))
        with patch.object(runtime_fixtures.FakeEClient, "reqPositions", positions), \
             patch.object(runtime_fixtures.FakeEClient, "reqPnLSingle", pnl, create=True), \
             patch.object(runtime_fixtures.FakeEClient, "cancelPnLSingle", cancel, create=True):
            runtime = self.make()
            runtime.connect_reads()
            report = runtime.probe_position_valuations()
        self.assertFalse(report.remaining_risk_authorized)
        self.assertEqual(len(report.samples), 1)
        self.assertEqual(len(runtime_fixtures.FakeEClient.instances), 1)
        client = runtime_fixtures.FakeEClient.instances[0]
        self.assertEqual(client.mutations, [])
        self.assertFalse(any(call[0] == "reqContractDetails" for call in client.calls))
        self.assertEqual([call[0] for call in client.calls if "PnLSingle" in call[0]], ["reqPnLSingle", "cancelPnLSingle"])


class RemainingRiskArithmeticTests(unittest.TestCase):
    def calculate(self, **changes):
        values = dict(quantity=10, current_market_value="200", stop_allocations=(("stop-a", 4, "18"), ("stop-b", 6, "19")),
                      execution_reserve="2", remaining_fee_reserve="3")
        values.update(changes)
        return remaining_position_stop_downside(**values)

    def test_exact_current_mark_to_stops_plus_only_remaining_fees_and_reserves(self):
        self.assertEqual(self.calculate(), Decimal("19"))  # 200 - (72 + 114) + 2 + 3
        self.assertEqual(self.calculate(current_market_value="300"), Decimal("119"))

    def test_undercoverage_overlap_nonfinite_and_nonpositive_reserves_rejected(self):
        for changes in ({"stop_allocations": (("stop-a", 9, "18"),)},
                        {"stop_allocations": (("stop-a", 10, "18"), ("stop-b", 1, "18"))},
                        {"stop_allocations": (("stop-a", 4, "18"), ("stop-a", 6, "19"))},
                        {"stop_allocations": (("stop-a", 10, "20"),)},
                        {"execution_reserve": "0"}, {"remaining_fee_reserve": "0"},
                        {"current_market_value": "NaN"}, {"quantity": True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.calculate(**changes)


if __name__ == "__main__":
    unittest.main()
