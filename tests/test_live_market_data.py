from __future__ import annotations

from datetime import datetime, timedelta
import math
import unittest
from zoneinfo import ZoneInfo

from titan_brain.live.market_data import CompletedBar, MarketDataCache, Quote


ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 8, 10, 1, tzinfo=ET)


def quote(**overrides):
    raw = {
        "symbol": "XYZ",
        "bid": "10.00",
        "ask": "10.02",
        "bid_size": 500,
        "ask_size": 500,
        "venue_bid_at": NOW - timedelta(seconds=1),
        "venue_ask_at": NOW - timedelta(seconds=1),
        "observed_at": NOW,
        "source": "robinhood",
        "tradable": True,
    }
    raw.update(overrides)
    return Quote.build(**raw)


def bar(sequence=1, revision=0, volume=800_000, end_at=None, **overrides):
    end = end_at or NOW - timedelta(minutes=1)
    raw = {
        "symbol": "XYZ",
        "start_at": end - timedelta(minutes=1),
        "end_at": end,
        "open": "9.90",
        "high": "10.05",
        "low": "9.88",
        "close": "10.01",
        "volume": volume,
        "sequence": sequence,
        "revision": revision,
        "source_event_id": f"event-{sequence}-{revision}",
    }
    raw.update(overrides)
    return CompletedBar.build(**raw)


class MarketDataTests(unittest.TestCase):
    def test_invalid_crossed_future_and_nonfinite_quotes_fail(self) -> None:
        with self.assertRaisesRegex(ValueError, "crossed"):
            quote(bid="10.03", ask="10.02")
        with self.assertRaisesRegex(ValueError, "future"):
            quote(venue_ask_at=NOW + timedelta(seconds=2))
        with self.assertRaisesRegex(ValueError, "finite"):
            quote(bid=math.nan)

    def test_duplicates_corrections_and_sequence_gap(self) -> None:
        cache = MarketDataCache()
        original = bar()
        self.assertEqual(cache.record_completed_bar(original, received_at=NOW), "inserted")
        self.assertEqual(cache.record_completed_bar(original, received_at=NOW), "duplicate")
        corrected = bar(revision=1, close="10.00")
        self.assertEqual(cache.record_completed_bar(corrected, received_at=NOW), "corrected")
        self.assertIn("CORRECTED", cache.degraded["XYZ"])
        next_end = NOW
        cache.record_snapshot_resync("XYZ", sequence=1)
        cache.record_completed_bar(bar(sequence=3, end_at=next_end), received_at=NOW)
        self.assertEqual(cache.degraded["XYZ"], "MARKET_DATA_SEQUENCE_GAP")

    def test_entry_evidence_requires_all_numeric_and_causal_gates(self) -> None:
        cache = MarketDataCache()
        causal = bar()
        cache.record_completed_bar(causal, received_at=NOW)
        cache.record_quote(quote())
        decision = cache.validate_entry_evidence(
            symbol="XYZ",
            now=NOW,
            plan_created_at=NOW - timedelta(seconds=10),
            plan_expires_at=NOW + timedelta(seconds=10),
            causal_bar_end=causal.end_at,
            quote_max_age_seconds=5,
            completed_bar_max_age_seconds=120,
            minimum_session_volume=750_000,
            max_spread_bps="25",
            minimum_depth_multiple="5",
            quantity=10,
        )
        self.assertTrue(decision.eligible, decision.failures)
        unresolved = cache.validate_entry_evidence(
            symbol="XYZ",
            now=NOW,
            plan_created_at=NOW - timedelta(seconds=10),
            plan_expires_at=NOW + timedelta(seconds=10),
            causal_bar_end=causal.end_at,
            quote_max_age_seconds=5,
            completed_bar_max_age_seconds=120,
            minimum_session_volume=750_000,
            max_spread_bps=None,
            minimum_depth_multiple=None,
            quantity=10,
        )
        self.assertIn("NUMERIC_SPREAD_GATE_UNRESOLVED", unresolved.failures)
        self.assertIn("NUMERIC_DEPTH_GATE_UNRESOLVED", unresolved.failures)

    def test_data_loss_blocks_entries_but_can_be_explicitly_resynced(self) -> None:
        cache = MarketDataCache()
        cache.record_quote(quote())
        cache.record_completed_bar(bar(), received_at=NOW)
        cache.mark_disconnect("massive")
        self.assertIn("DISCONNECTED", cache.degraded["XYZ"])
        cache.record_snapshot_resync("XYZ", sequence=10)
        self.assertNotIn("XYZ", cache.degraded)

    def test_active_set_is_ranked_and_bounded(self) -> None:
        cache = MarketDataCache(max_active=2)
        self.assertEqual(
            cache.set_active_scores((("bbb", 80), ("AAA", 90), ("CCC", 85))),
            ("AAA", "CCC"),
        )


if __name__ == "__main__":
    unittest.main()
