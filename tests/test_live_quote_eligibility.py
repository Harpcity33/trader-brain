"""Regression coverage for independent eligibility arriving after a quote."""
from dataclasses import replace
from datetime import timedelta
from threading import Event
import unittest

from tests.test_live_provider_integrations import CandidateSource, FakeRest, FakeStream, NOW, structure
from titan_brain.live.massive_adapter import MassiveRestStreamSource
from titan_brain.live.market_data import MarketDataCache, MarketSessionState


class OneShotStream(FakeStream):
    def drain(self, *, limit, timeout_seconds):
        events, self.events = self.events[:limit], self.events[limit:]
        return events


class StreamFirstSource(MassiveRestStreamSource):
    def __init__(self, **kwargs):
        self.first_batch = Event()
        self.initial_quote = None
        super().__init__(**kwargs)

    def _process_stream_batch(self, events, *, received_at):
        super()._process_stream_batch(events, received_at=received_at)
        if events and not self.first_batch.is_set():
            self.initial_quote = self._cache.quote_for("XYZ")
            self.first_batch.set()

    def _ensure_stream_consumer(self):
        super()._ensure_stream_consumer()
        if not self.first_batch.wait(2):
            raise AssertionError("One-shot stream did not publish its first quote")


class MutableEligibility:
    allowed = True

    def is_tradable(self, symbol, *, as_of):
        return self.allowed


class QuoteEligibilityRegressionTests(unittest.TestCase):
    def setUp(self):
        quote = {
            "ev": "Q", "sym": "XYZ",
            "t": int((NOW-timedelta(seconds=1)).timestamp()*1_000_000_000),
            "bp": 10.01, "ap": 10.03, "bs": 700, "as": 800,
        }
        self.source = StreamFirstSource(
            candidates=CandidateSource(), rest=FakeRest(),
            stream=OneShotStream(events=(quote,)),
            session_state=lambda _: MarketSessionState.ENTRY_ELIGIBLE,
            health_max_age_seconds=15, candidate_max_age_seconds=120,
        )
        self.addCleanup(self.source.close)
        self.cache = MarketDataCache()
        self.eligibility = MutableEligibility()
        self.source.hydrate_cache(
            self.cache, structures=(structure(),),
            session_start=NOW-timedelta(minutes=31), now=NOW,
            tradability=self.eligibility,
        )
        self.assertTrue(self.source.wait_for_backfills(timeout_seconds=2))

    def test_backfill_joins_eligibility_to_one_shot_quote_without_freshening_it(self):
        initial = self.source.initial_quote
        self.assertIsNotNone(initial)
        self.assertFalse(initial.tradable)
        self.assertEqual(self.cache.quote_for("XYZ"), replace(initial, tradable=True))
        self.assertEqual(self.source._symbol_phase["XYZ"], "READY")

    def test_later_ineligibility_updates_existing_quote_without_waiting_for_a_tick(self):
        initial = self.cache.quote_for("XYZ")
        self.assertTrue(initial.tradable)
        self.eligibility.allowed = False
        result = self.source._backfill_symbol(
            "XYZ", "gap", NOW-timedelta(minutes=31), NOW,
            self.source._session_generation,
        )
        self.assertEqual(result, ("READY", None))
        self.assertEqual(self.cache.quote_for("XYZ"), replace(initial, tradable=False))
