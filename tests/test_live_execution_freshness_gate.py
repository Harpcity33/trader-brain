"""Area 5: pre-submit external-change / expiry gate in EntryExecutionCoordinator.

Tests _plan_freshness_failures directly (the gate's pure decision) against real
broker AccountSnapshot fixtures: a matching fingerprint passes, an out-of-band
exposure change or an expiry fails closed, a missing snapshot fails closed. The
gate is skipped entirely when no plan_bound_fingerprint is supplied (all current
callers), so existing submit_entry behaviour is unchanged. No broker/network.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from titan_brain.live.broker.base import (
    AccountSnapshot, BrokerOrderState, BrokerSide, EquityOrderType, FundsSnapshot,
    MarketHours, OrderSnapshot, PositionSnapshot, TimeInForce,
)
from titan_brain.live.execution import EntryExecutionCoordinator
from titan_brain.live.plan_freshness import (
    SymbolOpenOrder, SymbolPosition, account_exposure_fingerprint,
)


D = Decimal
CREATED = datetime(2026, 9, 18, 14, 0, tzinfo=timezone.utc)
EXPIRES = CREATED + timedelta(seconds=30)
NOW = CREATED + timedelta(seconds=5)
MASK = "••••7153"


def _snapshot(*, positions=(), orders=()):
    return AccountSnapshot(
        account_masked=MASK, observed_at=NOW, received_at=NOW,
        account_state="active", account_type="no_borrow_margin",
        funds=FundsSnapshot(total_value=D(10000), cash=D(9000), buying_power=D(9000),
                            unleveraged_buying_power=D(9000)),
        equity_positions=positions, equity_orders=orders,
        option_position_count=0, option_order_count=0, advanced_order_count=0,
        standard_equity_positions_complete=True, standard_equity_orders_complete=True,
        option_positions_complete=True, option_orders_complete=True,
        advanced_orders_complete=True, auth_point_in_time=True,
    )


def _position(symbol="ABC", quantity="100"):
    return PositionSnapshot(symbol=symbol, quantity=D(quantity), sellable_quantity=D(quantity))


def _order(symbol="ABC", quantity="100"):
    return OrderSnapshot(
        broker_order_id="stop-1", account_masked=MASK, symbol=symbol, side=BrokerSide.SELL,
        order_type=EquityOrderType.STOP_MARKET, state=BrokerOrderState.CONFIRMED,
        requested_quantity=D(quantity), cumulative_filled_quantity=D(0),
        market_hours=MarketHours.REGULAR, time_in_force=TimeInForce.GTC,
        broker_updated_at=NOW, received_at=NOW, stop_price=D("48.00"),
    )


def _coordinator():
    # The helper is pure over its arguments; build a bare instance.
    return object.__new__(EntryExecutionCoordinator)


def _plan_bound_fp(positions, orders):
    return account_exposure_fingerprint(positions, orders)


class ExecutionFreshnessGateTests(unittest.TestCase):
    def setUp(self):
        self.coord = _coordinator()
        self.snapshot = _snapshot(positions=(_position(),), orders=(_order(),))
        # The fingerprint the plan was bound to (matches the snapshot above).
        # The STOP_MARKET order has limit_price=None (only stop_price set), and
        # the mapper fingerprints limit_price — so the bound order carries None.
        self.matching_fp = _plan_bound_fp(
            (SymbolPosition("ABC", 100),),
            (SymbolOpenOrder("stop-1", "ABC", "SELL", 100, None),),
        )

    def _failures(self, *, fingerprint, snapshot, now=NOW):
        return self.coord._plan_freshness_failures(
            plan_bound_fingerprint=fingerprint, broker_snapshot=snapshot,
            created_at=CREATED, expires_at=EXPIRES, now=now,
        )

    def test_matching_exposure_within_window_passes(self):
        self.assertEqual(self._failures(fingerprint=self.matching_fp, snapshot=self.snapshot), ())

    def test_out_of_band_position_change_fails_closed(self):
        changed = _snapshot(positions=(_position(quantity="90"),), orders=(_order(),))
        self.assertIn("EXTERNAL_EXPOSURE_CHANGED",
                      self._failures(fingerprint=self.matching_fp, snapshot=changed))

    def test_cancelled_stop_order_fails_closed(self):
        changed = _snapshot(positions=(_position(),), orders=())
        self.assertIn("EXTERNAL_EXPOSURE_CHANGED",
                      self._failures(fingerprint=self.matching_fp, snapshot=changed))

    def test_expired_plan_fails_closed(self):
        self.assertIn("PLAN_EXPIRED",
                      self._failures(fingerprint=self.matching_fp, snapshot=self.snapshot,
                                     now=EXPIRES + timedelta(seconds=1)))

    def test_missing_snapshot_fails_closed(self):
        self.assertEqual(self._failures(fingerprint=self.matching_fp, snapshot=None),
                         ("PLAN_FRESHNESS_SNAPSHOT_UNAVAILABLE",))

    def test_fractional_position_is_unmappable_and_fails_closed(self):
        frac = _snapshot(positions=(_position(quantity="100.5"),), orders=())
        self.assertEqual(self._failures(fingerprint=self.matching_fp, snapshot=frac),
                         ("PLAN_FRESHNESS_UNMAPPABLE_POSITION",))

    def test_bad_fingerprint_fails_closed_not_raises(self):
        # A malformed plan-bound fingerprint becomes a blocker, never an escape.
        result = self._failures(fingerprint="not-a-hash", snapshot=self.snapshot)
        self.assertTrue(result)
        self.assertTrue(all(isinstance(code, str) for code in result))


if __name__ == "__main__":
    unittest.main()
