"""Offline order codec contract; no network, accounts, SDK sessions, or orders."""

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from types import SimpleNamespace
import socket
import sys
import unittest
from unittest.mock import patch

from titan_brain.live.broker.base import (
    BrokerSide, EquityOrderType, MarketHours, OrderRequest, TimeInForce,
)
from titan_brain.live.broker.ibkr_orders import (
    IbkrContractIdentity, IbkrOrderPlan, build_ibkr_order_plan,
)


# These are synthetic identifiers, never copied from a real account/lookup.
ACCOUNT = "U9991234"
REF = "21672fe8-9b5a-4eb6-b9bb-28da3de59229"
UNSET = object()


def request(**overrides):
    values = dict(
        account_masked="****1234", symbol="TEST", side=BrokerSide.BUY,
        order_type=EquityOrderType.LIMIT, quantity=10,
        market_hours=MarketHours.REGULAR, time_in_force=TimeInForce.GFD,
        client_ref_id=REF, limit_price=Decimal("10.01"),
    )
    values.update(overrides)
    return OrderRequest(**values)


def identity(**overrides):
    values = dict(con_id=999001, symbol="TEST", primary_exchange="NASDAQ")
    values.update(overrides)
    return IbkrContractIdentity(**values)


def plan(order_request=None, **overrides):
    values = dict(
        contract=identity(), account_id=ACCOUNT, client_id=19735,
        order_id=101, transmit=False,
    )
    values.update(overrides)
    return build_ibkr_order_plan(order_request or request(), **values)


def sdk_order():
    return SimpleNamespace(lmtPrice=UNSET, auxPrice=UNSET)


def to_sdk(order_plan):
    return order_plan.to_sdk(contract_factory=SimpleNamespace, order_factory=sdk_order)


class IbkrOrderCodecTests(unittest.TestCase):
    def setUp(self):
        # Any accidental connection in construction/conversion fails the suite.
        self.addCleanup(patch.stopall)
        patch.object(socket, "socket", side_effect=AssertionError("network forbidden")).start()
        patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")).start()

    def test_all_regular_order_side_type_and_tif_mappings(self):
        tuples = (
            (EquityOrderType.MARKET, "MKT", None, None),
            (EquityOrderType.LIMIT, "LMT", Decimal("10.01"), None),
            (EquityOrderType.STOP_MARKET, "STP", None, Decimal("9.90")),
            (EquityOrderType.STOP_LIMIT, "STP LMT", Decimal("9.85"), Decimal("9.90")),
        )
        for side in BrokerSide:
            for tif in TimeInForce:
                for typ, wire, limit, stop in tuples:
                    with self.subTest(side=side, tif=tif, typ=typ):
                        p = plan(request(side=side, time_in_force=tif, order_type=typ,
                                         limit_price=limit, stop_price=stop))
                        c, o = to_sdk(p)
                        self.assertEqual(o.action, side.value.upper())
                        self.assertEqual(o.orderType, wire)
                        self.assertEqual(o.tif, "DAY" if tif is TimeInForce.GFD else "GTC")
                        self.assertFalse(o.outsideRth)
                        self.assertEqual(o.totalQuantity, Decimal(10))
                        self.assertIsInstance(o.totalQuantity, Decimal)
                        self.assertEqual(o.lmtPrice, UNSET if limit is None else float(limit))
                        self.assertEqual(o.auxPrice, UNSET if stop is None else float(stop))
                        self.assertEqual((c.conId, c.symbol, c.secType, c.currency, c.exchange,
                                          c.primaryExchange),
                                         (999001, "TEST", "STK", "USD", "SMART", "NASDAQ"))

    def test_extended_limit_is_rejected_at_codec_boundary(self):
        for side in BrokerSide:
            for tif in TimeInForce:
                with self.subTest(side=side, tif=tif):
                    with self.assertRaisesRegex(ValueError, "regular hours"):
                        plan(request(side=side, time_in_force=tif,
                                     market_hours=MarketHours.EXTENDED))

    def test_all_day_is_not_silently_mapped_to_extended(self):
        with self.assertRaisesRegex(ValueError, "regular hours"):
            plan(request(market_hours=MarketHours.ALL_DAY))

    def test_shared_boundary_still_rejects_extended_stops(self):
        for typ, limit in ((EquityOrderType.STOP_MARKET, None),
                           (EquityOrderType.STOP_LIMIT, "9.85")):
            with self.subTest(typ=typ), self.assertRaisesRegex(ValueError, "must be limit"):
                request(order_type=typ, market_hours=MarketHours.EXTENDED,
                        limit_price=limit, stop_price="9.90")

    def test_explicit_transmit_is_required_and_not_a_what_if(self):
        for transmit in (True, False):
            p = plan(transmit=transmit)
            _, o = to_sdk(p)
            self.assertIs(p.transmit, transmit)
            self.assertIs(o.transmit, transmit)
            self.assertIs(o.whatIf, False)
            self.assertIs(p.what_if, False)
        with self.assertRaises(TypeError):
            build_ibkr_order_plan(request(), contract=identity(), account_id=ACCOUNT,
                                  client_id=19735, order_id=101)
        for value in (None, 0, 1, "False", "True"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "boolean"):
                plan(transmit=value)

    def test_id_and_uuid_fields_match_the_exact_request(self):
        p = plan(request(client_ref_id=REF.upper()), order_id=9876, client_id=345)
        _, o = to_sdk(p)
        self.assertEqual((o.orderId, o.clientId, o.orderRef), (9876, 345, REF))
        self.assertEqual(o.account, ACCOUNT)

    def test_no_brackets_conditions_or_precaution_bypass(self):
        _, o = to_sdk(plan())
        self.assertEqual((o.parentId, o.ocaGroup, o.ocaType), (0, "", 0))
        self.assertEqual(o.conditions, [])
        self.assertFalse(o.conditionsCancelOrder)
        self.assertFalse(o.conditionsIgnoreRth)
        self.assertEqual(o.triggerMethod, 0)
        self.assertFalse(o.overridePercentageConstraints)
        self.assertEqual(o.advancedErrorOverride, "")

    def test_frozen_plan_keeps_private_copies_and_no_sdk_mutation_leaks_back(self):
        r, c = request(), identity()
        p = plan(r, contract=c)
        self.assertIsNot(p.request, r)
        self.assertIsNot(p.contract, c)
        with self.assertRaises(FrozenInstanceError):
            p.order_id = 500
        with self.assertRaises(FrozenInstanceError):
            p.contract.con_id = 500
        sdk_c, sdk_o = to_sdk(p)
        sdk_o.totalQuantity = 1000
        sdk_o.conditions.append("changed")
        sdk_c.symbol = "DIFFERENT"
        self.assertEqual((p.quantity, p.contract.symbol), (10, "TEST"))
        _, second = to_sdk(p)
        self.assertEqual(second.conditions, [])

    def test_repr_and_str_redact_full_account(self):
        p = plan()
        for rendering in (repr(p), str(p)):
            self.assertNotIn(ACCOUNT, rendering)
            self.assertIn("****1234", rendering)
        self.assertEqual(p.account_id, ACCOUNT)

    def test_account_binding_never_normalizes_or_changes_account(self):
        for value in (None, "", "U1234", "U9991235", " U9991234", "U9991234\n",
                      "u9991234", "****1234", "F9991234", "U9991234;BUY"):
            with self.subTest(value=value), self.assertRaises(ValueError) as error:
                plan(account_id=value)
            if isinstance(value, str) and value:
                self.assertNotIn(value, str(error.exception))
        # A paper-format account is not mistaken for an environment proof.
        self.assertEqual(plan(account_id="DU9991234").account_id, "DU9991234")
        self.assertEqual(plan(request(account_masked="••••1234")).request.account_masked,
                         "••••1234")

    def test_client_order_and_contract_ids_are_nonzero_int32(self):
        for name in ("client_id", "order_id"):
            for bad in (0, -1, True, False, "1", 1.0, Decimal(1), 2**31):
                with self.subTest(name=name, bad=bad), self.assertRaises(ValueError):
                    plan(**{name: bad})
        for bad in (0, -1, True, "1", 1.0, 2**31):
            with self.subTest(con_id=bad), self.assertRaises(ValueError):
                identity(con_id=bad)
        self.assertEqual(plan(order_id=2**31-1).order_id, 2**31-1)

    def test_contract_never_accepts_symbol_currency_security_or_route_mismatch(self):
        with self.assertRaisesRegex(ValueError, "symbol does not match"):
            plan(contract=identity(symbol="OTHER"))
        for override in ({"symbol": "test"}, {"symbol": "TEST "}, {"symbol": "TEST\n"},
                         {"currency": "CAD"}, {"currency": "usd"}, {"sec_type": "OPT"},
                         {"exchange": "NASDAQ"}, {"exchange": "OVERNIGHT"}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                identity(**override)
        for exchange in ("", "SMART", "BEST", "OVERNIGHT", "IBKRATS", "nasdaq", "NASDAQ ", None):
            with self.subTest(exchange=exchange), self.assertRaises(ValueError):
                identity(primary_exchange=exchange)

    def test_extra_order_or_contract_fields_are_not_silently_accepted(self):
        with self.assertRaises(TypeError):
            identity(multiplier="100")
        with self.assertRaises(TypeError):
            plan(parent_id=88)
        with self.assertRaises(TypeError):
            plan(what_if=True)

    def test_direct_plan_construction_applies_all_validation(self):
        p = plan()
        with self.assertRaisesRegex(ValueError, "symbol does not match"):
            replace(p, contract=identity(symbol="OTHER"))
        with self.assertRaises(ValueError):
            replace(p, client_id=0)
        with self.assertRaises(ValueError):
            replace(p, request=SimpleNamespace())
        with self.assertRaises(ValueError):
            replace(p, contract=SimpleNamespace())
        self.assertIsInstance(p, IbkrOrderPlan)

    def test_whole_shares_preserved_without_float_quantity(self):
        p = plan(request(quantity=Decimal("13")))
        self.assertEqual(p.quantity, 13)
        for bad in (True, 0, -1, "0.1", "NaN", "Infinity"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                request(quantity=bad)

    def test_prices_preserve_decimal_roundtrip_without_silent_rounding(self):
        for good in ("0.0001", "10.01", "123.4567", "10.0100"):
            with self.subTest(good=good):
                p = plan(request(limit_price=good))
                _, o = to_sdk(p)
                self.assertEqual(p.limit_price, Decimal(good))
                self.assertEqual(Decimal(str(o.lmtPrice)), Decimal(good))
        for bad in ("10.0000000000000001", "9007199254740993", "1e-400", "1e400",
                    str(sys.float_info.max)):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "round-trip"):
                plan(request(limit_price=bad))

    def test_stop_price_has_the_same_roundtrip_guard(self):
        with self.assertRaisesRegex(ValueError, "stop_price.*round-trip"):
            plan(request(order_type=EquityOrderType.STOP_MARKET, limit_price=None,
                         stop_price="9.0000000000000001"))

    def test_nonfinite_and_nonpositive_prices_remain_shared_boundary_errors(self):
        for bad in ("NaN", "Infinity", "-Infinity", "0", "-0.01", True):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                request(limit_price=bad)

    def test_constructor_failures_cannot_relay_private_exception_text(self):
        def failing_factory():
            raise RuntimeError("private " + ACCOUNT)
        with self.assertRaisesRegex(ValueError, "SDK object construction failed") as error:
            plan().to_sdk(contract_factory=failing_factory, order_factory=sdk_order)
        self.assertNotIn(ACCOUNT, str(error.exception))
        self.assertTrue(error.exception.__suppress_context__)

    def test_contract_and_order_cannot_be_the_same_object(self):
        shared = SimpleNamespace()
        with self.assertRaisesRegex(ValueError, "construction failed"):
            plan().to_sdk(contract_factory=lambda: shared, order_factory=lambda: shared)


if __name__ == "__main__":
    unittest.main()
