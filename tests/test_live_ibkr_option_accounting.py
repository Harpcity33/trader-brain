"""Tests for multiplier-aware option accounting (milestone 2, unit 2b)."""

from __future__ import annotations

from decimal import Decimal
import unittest

from titan_brain.live.broker.ibkr_option_orders import IbkrOptionContractIdentity
from titan_brain.live.broker.ibkr_option_accounting import (
    OptionAccountingError,
    OptionFill,
    account_option_fills,
)


def _identity(**overrides):
    fields = dict(
        con_id=765432109,
        symbol="TEST",
        right="C",
        strike="12.50",
        expiry="20261016",
        trading_class="TEST",
        multiplier=100,
        deliverable="100_SHARES",
        exercise_style="AMERICAN",
        settlement_type="PHYSICAL",
        currency="USD",
        exchange="SMART",
    )
    fields.update(overrides)
    return IbkrOptionContractIdentity(**fields)


class OptionAccountingTests(unittest.TestCase):
    def test_buy_applies_multiplier_not_one_share(self) -> None:
        # 2 contracts x 100 multiplier x 1.25 premium = 250 premium, + 1.30 fee.
        ident = _identity()
        fill = OptionFill(identity=ident, side="BUY", quantity=2, price="1.25", commission="1.30")
        result = account_option_fills([fill])
        self.assertEqual(result.cash_delta, Decimal("-251.30"))
        self.assertEqual(result.total_commission, Decimal("1.30"))
        self.assertEqual(result.net_contracts[ident.wire_fingerprint()], 2)

    def test_multiplier_actually_scales(self) -> None:
        # Same fill at multiplier 100 vs a (hypothetical) 10 must differ 10x on
        # premium -- proves the multiplier is applied, not ignored.
        ident100 = _identity(multiplier=100, deliverable="100_SHARES")
        # A non-standard deliverable at multiplier 10 is rejected by identity,
        # so compare 1-contract premium magnitude via cash math directly.
        buy = OptionFill(identity=ident100, side="BUY", quantity=1, price="2.00", commission="0")
        result = account_option_fills([buy])
        # 1 x 100 x 2.00 = 200, not 2.00 (which the stock path would produce).
        self.assertEqual(result.cash_delta, Decimal("-200"))

    def test_sell_increases_cash(self) -> None:
        ident = _identity()
        buy = OptionFill(identity=ident, side="BUY", quantity=1, price="3.00", commission="1.00")
        sell = OptionFill(identity=ident, side="SELL", quantity=1, price="4.00", commission="1.00")
        result = account_option_fills([buy, sell])
        # -300 -1 +400 -1 = +98 ; net contracts 0 (dropped).
        self.assertEqual(result.cash_delta, Decimal("98"))
        self.assertEqual(result.total_commission, Decimal("2.00"))
        self.assertEqual(dict(result.net_contracts), {})

    def test_distinct_contracts_do_not_cross_net(self) -> None:
        call = _identity(right="C")
        put = _identity(right="P")
        result = account_option_fills([
            OptionFill(identity=call, side="BUY", quantity=2, price="1.00", commission="0"),
            OptionFill(identity=put, side="BUY", quantity=3, price="1.00", commission="0"),
        ])
        self.assertEqual(result.net_contracts[call.wire_fingerprint()], 2)
        self.assertEqual(result.net_contracts[put.wire_fingerprint()], 3)
        self.assertNotEqual(call.wire_fingerprint(), put.wire_fingerprint())

    def test_float_price_rejected(self) -> None:
        with self.assertRaises(OptionAccountingError):
            OptionFill(identity=_identity(), side="BUY", quantity=1, price=1.25, commission="0")

    def test_bad_side_and_quantity_rejected(self) -> None:
        with self.assertRaisesRegex(OptionAccountingError, "side"):
            OptionFill(identity=_identity(), side="HOLD", quantity=1, price="1", commission="0")
        with self.assertRaisesRegex(OptionAccountingError, "quantity"):
            OptionFill(identity=_identity(), side="BUY", quantity=0, price="1", commission="0")
        with self.assertRaisesRegex(OptionAccountingError, "quantity"):
            OptionFill(identity=_identity(), side="BUY", quantity=-1, price="1", commission="0")

    def test_negative_commission_rejected(self) -> None:
        with self.assertRaisesRegex(OptionAccountingError, "commission"):
            OptionFill(identity=_identity(), side="BUY", quantity=1, price="1", commission="-1")

    def test_empty_fills_rejected(self) -> None:
        with self.assertRaises(OptionAccountingError):
            account_option_fills([])

    def test_non_optionfill_rejected(self) -> None:
        with self.assertRaises(OptionAccountingError):
            account_option_fills(["not a fill"])  # type: ignore[list-item]


if __name__ == "__main__":
    unittest.main()
