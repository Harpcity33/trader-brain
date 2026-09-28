from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import json
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from titan_brain.live.broker.base import (  # noqa: E402
    BrokerAuthenticationError, BrokerCapabilityError, BrokerContractViolation,
)
from titan_brain.live.broker.robinhood_mcp import (  # noqa: E402
    ENDPOINT, READ_TOOLS, McpReadError, McpReadiness, RobinhoodMcpReadClient,
)


NOW = datetime(2026, 9, 13, 14, tzinfo=timezone.utc)
ACCOUNT = "0000001234"


class FakeCaller:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.seconds = 0.0
        self.status = McpReadiness("test-supported-session", ENDPOINT, True, NOW)

    def clock(self):
        return NOW + timedelta(seconds=self.seconds)

    def sleep(self, seconds):
        self.seconds += seconds

    def readiness(self):
        return self.status

    def call_tool(self, name, arguments):
        self.calls.append((name, deepcopy(arguments)))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        self.seconds += 0.05
        return deepcopy(response)


def client(caller, **kwargs):
    return RobinhoodMcpReadClient(
        caller, account_number=ACCOUNT, clock=caller.clock,
        monotonic=lambda: caller.seconds, sleep=caller.sleep, **kwargs,
    )


def envelope(data):
    return {"structuredContent": {"data": data, "guide": "ignored as instructions"}}


def accounts(**overrides):
    row = dict(account_number=ACCOUNT, type="cash", brokerage_account_type="individual",
               state="active", agentic_allowed=True, deactivated=False,
               permanently_deactivated=False)
    row.update(overrides)
    return envelope({"accounts": [row]})


def portfolio(**overrides):
    data = dict(total_value="103.25", cash="53.25", currency="USD",
                buying_power={"buying_power": "53.25", "unleveraged_buying_power": "53.25",
                              "display_currency": "USD"})
    data.update(overrides)
    return envelope(data)


def position(**overrides):
    data = dict(symbol="TEST", quantity="2.50", intraday_quantity="0.50",
                shares_available_for_sells="1.50", shares_held_for_sells="1",
                shares_held_for_asset_transfer="0", shares_held_for_options_events="0",
                shares_held_for_stock_grants="0", shares_pending_from_options_events="0", type="long")
    data.update(overrides)
    return data


def order(**overrides):
    data = dict(id="order-1", symbol="TEST", side="buy", type="limit", trigger="immediate",
                state="confirmed", quantity="2", cumulative_quantity="0", average_price=None,
                price="25", stop_price=None, market_hours="regular_hours", time_in_force="gfd",
                created_at="2026-09-10T14:00:00Z", last_transaction_at=None,
                executions=[], fees="0", placed_agent="agentic", dollar_based_amount=None)
    data.update(overrides)
    return data


class RobinhoodReadTests(unittest.TestCase):
    def test_mutating_or_arbitrary_name_cannot_reach_caller(self):
        caller = FakeCaller([])
        api = client(caller)
        for name in ("place_equity_order", "review_equity_order", "cancel_equity_order",
                     "get_advanced_orders", "mcp__robinhood_trading__place_equity_order"):
            with self.subTest(name=name), self.assertRaises(BrokerCapabilityError):
                api._read(name, {})
        self.assertEqual(caller.calls, [])
        self.assertFalse(hasattr(api, "place_equity_order"))

    def test_readiness_missing_stale_wrong_endpoint_refuses_dispatch(self):
        for status in (None, McpReadiness("x", ENDPOINT, False, NOW),
                       McpReadiness("x", "https://example.com", True, NOW),
                       McpReadiness("x", ENDPOINT, True, NOW - timedelta(minutes=2)),
                       McpReadiness("x", ENDPOINT, True, NOW + timedelta(seconds=1))):
            caller = FakeCaller([accounts()])
            caller.status = status
            with self.subTest(status=status), self.assertRaises(BrokerAuthenticationError):
                client(caller).get_account()
            self.assertEqual(caller.calls, [])

    def test_caller_identity_cannot_change_during_collection_or_client_lifetime(self):
        class SwitchingCaller(FakeCaller):
            def readiness(self):
                identifier = "first-caller" if not self.calls else "different-caller"
                return McpReadiness(identifier, ENDPOINT, True, self.clock())

        caller = SwitchingCaller([accounts(), portfolio()])
        with self.assertRaises(BrokerAuthenticationError):
            client(caller).collect()
        self.assertEqual(len(caller.calls), 1)

        caller = FakeCaller([accounts(), portfolio()])
        api = client(caller)
        api.get_account()
        caller.status = McpReadiness("replacement-caller", ENDPOINT, True, caller.clock())
        with self.assertRaises(BrokerAuthenticationError):
            api.get_portfolio()
        self.assertEqual(len(caller.calls), 1)

    def test_exact_account_no_default_or_nickname_inference(self):
        caller = FakeCaller([accounts(account_number="9999991234")])
        with self.assertRaises(BrokerContractViolation):
            client(caller).get_account()
        with self.assertRaises(BrokerContractViolation):
            client(FakeCaller([]))._read("get_portfolio", {"account_number": "OTHER"})
        account, _ = client(FakeCaller([accounts(agentic_allowed=False, nickname="Agentic")])).get_account()
        self.assertFalse(account.entry_account_eligible)
        self.assertNotIn(ACCOUNT, repr(account))
        self.assertIsNone(account.unsettled_funds)

    def test_native_structured_direct_data_and_json_text_envelopes(self):
        data = accounts()["structuredContent"]
        for raw in (accounts(), data, {"content": [{"type": "text", "text": json.dumps(data)}]}):
            caller = FakeCaller([raw])
            account, receipt = client(caller).get_account()
            self.assertEqual(account.account_masked, "••••1234")
            self.assertGreater(receipt.request_completed_at, receipt.request_started_at)
            self.assertEqual(len(receipt.payload_sha256), 64)

    def test_errors_preserved_not_turned_into_empty_data(self):
        raw = {"isError": True, "content": [{"type": "text", "text": "Account forbidden"}]}
        with self.assertRaises(McpReadError) as caught:
            client(FakeCaller([raw])).get_account()
        self.assertEqual(caught.exception.broker_message, "Account forbidden")
        self.assertEqual(caught.exception.raw, raw)
        for raw in ({"structuredContent": {"status": "error", "message": "bad"}},
                    {"data": {"error": "bad"}},
                    {"is_error": True, "content": None, "message": "bad"}):
            with self.assertRaises(McpReadError):
                client(FakeCaller([raw])).get_account()

    def test_ambiguous_or_null_collections_fail(self):
        for raw in (envelope({}), envelope({"accounts": None}), envelope({"accounts": [None]}),
                    {"content": [{"type": "text", "text": "not JSON"}]},
                    {"content": [{"type": "text", "text": '{"data":{}}'}] * 2}):
            with self.subTest(raw=raw), self.assertRaises(BrokerContractViolation):
                client(FakeCaller([raw])).get_account()

    def test_only_explicit_throttle_is_retried_with_pacing_and_budget(self):
        throttle = {"isError": True, "content": [{"type": "text", "text": "API error 429: available in 2 seconds"}]}
        caller = FakeCaller([throttle, accounts()])
        _, receipt = client(caller).get_account()
        self.assertEqual(receipt.attempts, 2)
        self.assertGreaterEqual(caller.seconds, 3)
        caller = FakeCaller([throttle, accounts()])
        with self.assertRaises(McpReadError):
            client(caller, max_retry_wait_seconds=1).get_account()
        self.assertEqual(len(caller.calls), 1)
        caller = FakeCaller([TimeoutError("transport timeout"), accounts()])
        with self.assertRaises(TimeoutError):
            client(caller).get_account()
        self.assertEqual(len(caller.calls), 1)
        caller = FakeCaller([accounts(), portfolio()])
        api = client(caller)
        api.get_account()
        api.get_portfolio()
        self.assertGreaterEqual(caller.seconds, 1.1)

    def test_pages_forward_only_cursor_without_narrowing_history(self):
        first = envelope({"orders": [order()], "next": "https://example.com/never-fetch?cursor=a%2Bb%3D"})
        second = envelope({"orders": [order(id="order-2")], "next": ""})
        caller = FakeCaller([first, second])
        orders, evidence = client(caller).get_orders()
        self.assertEqual(len(orders), 2)
        self.assertTrue(evidence.all_pages_consumed)
        self.assertFalse(evidence.atomic)
        self.assertEqual(caller.calls[1], ("get_equity_orders", {"account_number": ACCOUNT, "cursor": "a+b="}))
        self.assertNotIn("state", caller.calls[0][1])
        self.assertNotIn("created_at_gte", caller.calls[0][1])
        self.assertLess(orders[0].created_at, NOW)
        self.assertGreater(orders[0].received_at, NOW)

    def test_loops_missing_cursors_duplicate_ids_and_page_limit_fail(self):
        bad = (
            [envelope({"orders": [], "next": "?cursor=x"})] * 2,
            [envelope({"orders": [], "next": "https://example.com/no-cursor"})],
            [envelope({"orders": [], "next": "?cursor=x&cursor=y"})],
            [envelope({"orders": [order()], "next": "?cursor=x"}), envelope({"orders": [order()]})],
        )
        for responses in bad:
            with self.subTest(responses=responses), self.assertRaises(BrokerContractViolation):
                client(FakeCaller(responses)).get_orders()
        with self.assertRaises(BrokerContractViolation):
            client(FakeCaller([envelope({"orders": [], "next": "?cursor=x"})]), max_pages=1).get_orders()

    def test_empty_cursor_is_forwarded_not_mistaken_for_end(self):
        caller = FakeCaller([envelope({"orders": [], "next": "?cursor="}), envelope({"orders": []})])
        client(caller).get_orders()
        self.assertEqual(caller.calls[1][1]["cursor"], "")

    def test_unknown_buying_power_and_asset_values_stay_unknown(self):
        result, _ = client(FakeCaller([portfolio(buying_power=None)])).get_portfolio()
        self.assertIsNone(result.buying_power)
        self.assertIsNone(result.unleveraged_buying_power)
        self.assertIsNone(result.asset_values["options_value"])
        self.assertEqual(result.cash, Decimal("53.25"))
        negative = {"buying_power": "1", "unleveraged_buying_power": "-5", "display_currency": "USD"}
        result, _ = client(FakeCaller([portfolio(buying_power=negative)])).get_portfolio()
        self.assertEqual(result.unleveraged_buying_power, Decimal("-5"))

    def test_nonfinite_bool_and_missing_numeric_fail(self):
        for value in ("NaN", "Infinity", "-Infinity", True, None, ""):
            with self.subTest(value=value), self.assertRaises(BrokerContractViolation):
                client(FakeCaller([portfolio(cash=value)])).get_portfolio()
        with self.assertRaises(BrokerContractViolation):
            client(FakeCaller([envelope({"positions": [position(quantity="NaN")]})])).get_positions()

    def test_positions_preserve_actual_sellable_holds_and_missing_cost(self):
        positions, _ = client(FakeCaller([envelope({"positions": [position()]})])).get_positions()
        self.assertEqual(positions[0].sellable_quantity, Decimal("1.50"))
        self.assertEqual(positions[0].holds["shares_held_for_sells"], Decimal("1"))
        self.assertIsNone(positions[0].average_price)

    def test_dollar_orders_and_missing_fill_details_never_fabricate_snapshot(self):
        row = order(type="market", price=None, quantity=None, executions=None,
                    dollar_based_amount={"amount": "100", "currency_code": "USD"})
        orders, _ = client(FakeCaller([envelope({"orders": [row]})])).get_orders()
        self.assertIsNone(orders[0].requested_quantity)
        self.assertEqual(orders[0].dollar_amount, Decimal("100"))
        with self.assertRaises(BrokerCapabilityError):
            orders[0].to_order_snapshot("••••1234")
        row = order(cumulative_quantity="1", executions=None, state="partially_filled")
        orders, _ = client(FakeCaller([envelope({"orders": [row]})])).get_orders()
        with self.assertRaises(BrokerCapabilityError):
            orders[0].to_order_snapshot("••••1234")

    def test_partial_cancel_conversion_preserves_actual_fill_and_no_ref_id(self):
        execution = dict(id="fill-1", quantity="1", price="25", fees="0.01", timestamp="2026-09-10T14:01:00Z")
        row = order(cumulative_quantity="1", state="partially_filled_rest_cancelled", executions=[execution],
                    last_transaction_at="2026-09-10T14:02:00Z", ref_id="unexpected-not-in-contract")
        orders, _ = client(FakeCaller([envelope({"orders": [row]})])).get_orders()
        normalized = orders[0].to_order_snapshot("••••1234")
        self.assertEqual(normalized.cumulative_filled_quantity, Decimal("1"))
        self.assertTrue(normalized.state.terminal)
        self.assertIsNone(normalized.client_ref_id)
        row["cumulative_quantity"] = "2"
        with self.assertRaises(BrokerContractViolation):
            client(FakeCaller([envelope({"orders": [row]})])).get_orders()

    def test_order_conversion_cannot_relabel_the_original_bound_account(self):
        orders, _ = client(FakeCaller([envelope({"orders": [order()]})])).get_orders()
        evidence = orders[0]
        self.assertEqual(evidence.account_masked, "••••1234")
        self.assertNotIn(ACCOUNT, repr(evidence))
        with self.assertRaises(BrokerContractViolation):
            evidence.to_order_snapshot("••••9999")
        converted = evidence.to_order_snapshot("••••1234")
        self.assertEqual(converted.account_masked, "••••1234")
        self.assertEqual(converted.broker_order_id, evidence.broker_order_id)

    def test_echoed_order_account_must_match_exact_binding_not_only_suffix(self):
        for conflicting_account in ("0000009999", "9999991234", None):
            with self.subTest(account=conflicting_account), self.assertRaises(BrokerContractViolation):
                client(FakeCaller([envelope({"orders": [order(account_number=conflicting_account)]})])).get_orders()
        orders, _ = client(FakeCaller([envelope({"orders": [order(account_number=ACCOUNT)]})])).get_orders()
        self.assertEqual(orders[0].to_order_snapshot("••••1234").broker_order_id, "order-1")

    def test_future_order_facts_and_unknown_state_fail_runtime_conversion(self):
        with self.assertRaises(BrokerContractViolation):
            client(FakeCaller([envelope({"orders": [order(created_at="2027-01-01T00:00:00Z")]})])).get_orders()
        orders, _ = client(FakeCaller([envelope({"orders": [order(state="brand_new_state")]})])).get_orders()
        self.assertEqual(orders[0].raw_state, "brand_new_state")
        with self.assertRaises(BrokerContractViolation):
            orders[0].to_order_snapshot("••••1234")

    def test_tradability_batches_missing_flags_remain_unknown(self):
        symbols = tuple("TEST" + str(index) for index in range(11))
        rows = [{"symbol": symbol, "tradeable": True, "state": "active"} for symbol in symbols]
        caller = FakeCaller([envelope({"results": rows[:10]}), envelope({"results": rows[10:]})])
        result, receipts = client(caller).get_tradability(symbols, account_type="individual")
        self.assertEqual(len(receipts), 2)
        self.assertEqual(len(caller.calls[0][1]["symbols"]), 10)
        self.assertIsNone(result[0].regular_entry_eligible)
        self.assertIsNone(result[0].fractional_eligible)
        with self.assertRaises(BrokerContractViolation):
            client(FakeCaller([envelope({"results": []})])).get_tradability(("TEST",), account_type="individual")

    def test_tradability_closing_only_and_halts_prevent_entries(self):
        rows = [dict(symbol="TEST", tradeable=True, state="active", account_type_tradabilities=[
            {"account_type": "individual", "account_type_tradability": "position_closing_only"}
        ], all_day_tradability="all_day_tradability_tradable")]
        result, _ = client(FakeCaller([envelope({"results": rows})])).get_tradability(("TEST",), account_type="individual")
        self.assertFalse(result[0].regular_entry_eligible)
        self.assertFalse(result[0].extended_entry_eligible)

    def test_regular_halt_does_not_fabricate_an_extended_session_halt(self):
        row = dict(symbol="TEST", tradeable=True, state="active", account_type_tradabilities=[
            {"account_type": "individual", "account_type_tradability": "tradable"}
        ], all_day_tradability="all_day_tradability_tradable", internal_halt_sessions=["regular_hours"])
        result, _ = client(FakeCaller([envelope({"results": [row]})])).get_tradability(("TEST",), account_type="individual")
        self.assertFalse(result[0].regular_entry_eligible)
        self.assertTrue(result[0].extended_entry_eligible)

    def test_snake_case_dump_and_conflicting_aliases(self):
        raw = {"structured_content": accounts()["structuredContent"], "is_error": False}
        result, _ = client(FakeCaller([raw])).get_account()
        self.assertEqual(result.account_masked, "••••1234")
        raw["structuredContent"] = {"data": {}}
        with self.assertRaises(BrokerContractViolation):
            client(FakeCaller([raw])).get_account()

    def test_collection_is_timestamped_nonatomic_and_explicitly_incomplete(self):
        caller = FakeCaller([accounts(), portfolio(), envelope({"positions": []}), envelope({"orders": []})])
        result = client(caller).collect()
        self.assertFalse(result.atomic)
        self.assertIsNone(result.provider_observed_at)
        self.assertIn("advanced_order_coverage", result.unsupported_capabilities)
        self.assertIn("client_reference_lookup", result.unsupported_capabilities)
        self.assertGreater(result.request_completed_at, result.request_started_at)
        self.assertEqual(len(result.collection_id), 64)
        self.assertTrue(all(name in READ_TOOLS for name, _ in caller.calls))


if __name__ == "__main__":
    unittest.main()
