"""Offline starting-point contract for the selected IBKR/Massive split.

These tests exercise existing Titan boundaries with synthetic quotes. They
are not IBKR acceptance tests and do not verify feed entitlements, real-time
connectivity, protection, or an executable IBKR transport. Numeric liquidity
parameters below are test fixtures, not proposed owner-policy changes.
"""

from datetime import datetime, timedelta, timezone
import unittest

from titan_brain.live.broker.base import (
    BrokerSide, EquityOrderType, MarketHours, OrderRequest, TimeInForce,
)
from titan_brain.live.broker.factory import BrokerFactoryError, build_broker_client
from titan_brain.live.market_data import CompletedBar, MarketDataCache, Quote
from titan_brain.live.massive_adapter import _massive_quote_sizes


NOW = datetime(2026, 9, 14, 8, 10, tzinfo=timezone.utc)


def massive_quote(**overrides):
    values = dict(
        symbol="TEST", bid="10.00", ask="10.02", bid_size=500, ask_size=500,
        venue_bid_at=NOW-timedelta(seconds=1),
        venue_ask_at=NOW-timedelta(seconds=1), observed_at=NOW,
        source="massive", tradable=True,
        size_source_version="massive_stock_quotes_shares_effective_2025-11-03",
    )
    values.update(overrides)
    return Quote.build(**values)


class IbkrMassiveStartingContractTests(unittest.TestCase):
    def setUp(self):
        self.cache = MarketDataCache()
        self.causal = CompletedBar.build(
            symbol="TEST", start_at=NOW-timedelta(minutes=2),
            end_at=NOW-timedelta(minutes=1), open="9.99", high="10.03",
            low="9.98", close="10.01", volume=800_000, sequence=1,
            revision=0, source_event_id="synthetic-massive-bar",
        )
        self.cache.record_completed_bar(self.causal, received_at=NOW)

    def decision(self):
        return self.cache.validate_entry_evidence(
            symbol="TEST", now=NOW,
            plan_created_at=NOW-timedelta(seconds=10),
            plan_expires_at=NOW+timedelta(seconds=10),
            causal_bar_end=self.causal.end_at,
            quote_max_age_seconds=5, completed_bar_max_age_seconds=120,
            minimum_session_volume=750_000, max_spread_bps="25",
            minimum_depth_multiple="5", quantity=10,
        )

    def test_massive_quote_can_satisfy_data_gate_without_broker_quote(self):
        self.cache.record_quote(massive_quote())
        result = self.decision()
        self.assertTrue(result.eligible, result.failures)
        self.assertEqual(result.quote.source, "massive")
        # Data eligibility alone is not broker/order authorization.

    def test_missing_quote_does_not_fall_back_to_bar_close(self):
        self.assertIn("QUOTE_MISSING", self.decision().failures)

    def test_delayed_quote_is_not_freshened_by_current_receipt(self):
        stale = NOW-timedelta(minutes=15)
        self.cache.record_quote(massive_quote(venue_bid_at=stale, venue_ask_at=stale))
        self.assertIn("QUOTE_STALE", self.decision().failures)

    def test_either_stale_side_blocks_the_quote(self):
        for side in ("venue_bid_at", "venue_ask_at"):
            with self.subTest(side=side):
                self.cache = MarketDataCache()
                self.cache.record_completed_bar(self.causal, received_at=NOW)
                self.cache.record_quote(massive_quote(**{side: NOW-timedelta(seconds=6)}))
                self.assertIn("QUOTE_STALE", self.decision().failures)

    def test_halted_market_remains_blocked_with_good_prices(self):
        self.cache.record_quote(massive_quote(halted=True))
        self.assertIn("MARKET_HALTED", self.decision().failures)

    def test_feed_quotes_do_not_establish_broker_tradability(self):
        self.cache.record_quote(massive_quote(tradable=False))
        self.assertFalse(self.decision().eligible)
        # Current failure name is Robinhood-specific; adapter migration must
        # replace its evidence provider, not mark every Massive symbol tradable.
        self.assertIn("ROBINHOOD_NOT_TRADABLE", self.decision().failures)

    def test_disconnect_blocks_entries_without_quote_fallback(self):
        self.cache.record_quote(massive_quote())
        self.cache.mark_disconnect("massive")
        self.assertFalse(self.decision().eligible)
        self.assertTrue(any("DISCONNECTED" in item for item in self.decision().failures))

    def test_quote_sizes_are_shares_and_not_full_order_book_depth(self):
        bid, ask, version = _massive_quote_sizes(7, 8, venue_at=NOW)
        self.assertEqual((bid, ask), (7, 8))
        self.cache.record_quote(massive_quote(
            bid_size=bid, ask_size=ask, size_source_version=version,
        ))
        self.assertIn("DISPLAYED_DEPTH_INSUFFICIENT", self.decision().failures)
        with self.assertRaisesRegex(ValueError, "top_of_book"):
            massive_quote(depth_scope="full_order_book")

    def test_broker_selection_cannot_construct_an_unconfigured_transport(self):
        with self.assertRaisesRegex(BrokerFactoryError, "without an injected"):
            build_broker_client(
                {"broker_adapter": "supported_production_transport"},
                account_masked="****1234",
            )

    def test_shared_model_still_requires_migration_for_premarket_stop_limit(self):
        # This asserts an identified limitation, not desired final IBKR support.
        # Do not bypass this boundary by labelling an extended stop as RTH.
        with self.assertRaisesRegex(ValueError, "must be limit orders"):
            OrderRequest(
                account_masked="****1234", symbol="TEST", side=BrokerSide.SELL,
                order_type=EquityOrderType.STOP_LIMIT, quantity=10,
                market_hours=MarketHours.EXTENDED, time_in_force=TimeInForce.GFD,
                client_ref_id="3e173798-751a-492b-9a9a-a19a2ff1bed0",
                stop_price="9.90", limit_price="9.85",
            )


if __name__ == "__main__":
    unittest.main()
