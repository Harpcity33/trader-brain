from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from queue import Empty, Queue
from threading import Event, Lock
import time
import unittest

from titan_brain.live.market_data import CompletedBar, MarketDataCache, MarketSessionState
from titan_brain.live.massive_adapter import (
    MassiveAuthorizationEvidence,
    MassiveRestStreamSource,
    MassiveStreamStatus,
    PreparedStructure,
)


BINDING = "d" * 64


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def authorization() -> MassiveAuthorizationEvidence:
    return MassiveAuthorizationEvidence(
        binding_id=BINDING,
        credential_source="test-owned-fixture",
        scopes=("stocks:read",),
        authenticated=True,
    )


def structure(symbol: str = "XYZ") -> PreparedStructure:
    current = now_utc()
    return PreparedStructure(
        source_plan_id=f"candidate-{symbol}",
        symbol=symbol,
        observed_at=current - timedelta(seconds=1),
        setup_id="controlled_base_breakout",
        ranking_score=Decimal("80"),
        entry_limit=Decimal("10.02"),
        structural_stop=Decimal("9.80"),
        targets=(Decimal("10.50"),),
        payload_hash="e" * 64,
        payload={"trade_authority": False, "broker_authority": False},
    )


def quote_event(symbol: str = "XYZ", *, at: datetime | None = None) -> dict[str, object]:
    venue = at or (now_utc() - timedelta(milliseconds=5))
    return {
        "ev": "Q",
        "sym": symbol,
        "t": int(venue.timestamp() * 1_000_000_000),
        "bp": 10.00,
        "ap": 10.02,
        "bs": 7,
        "as": 9,
    }


def aggregate_event(
    kind: str,
    *,
    symbol: str = "XYZ",
    start: datetime,
    end_offset: timedelta = timedelta(seconds=59, milliseconds=999),
) -> dict[str, object]:
    aligned = start.astimezone(timezone.utc).replace(second=0, microsecond=0)
    return {
        "ev": kind,
        "sym": symbol,
        "s": int(aligned.timestamp() * 1000),
        "e": int((aligned + end_offset).timestamp() * 1000),
        "o": 9.90,
        "h": 10.05,
        "l": 9.88,
        "c": 10.01,
        "v": 800_000,
    }


class CandidateSource:
    def prepared_structures(self, *, now, limit):
        return ()


class Tradable:
    def is_tradable(self, symbol, *, as_of):
        return symbol != "BAD"


class MutableTradable:
    def __init__(self, value: bool) -> None:
        self.value = value

    def is_tradable(self, symbol, *, as_of):
        return self.value


class QueueStream:
    def __init__(self) -> None:
        self.authorization = authorization()
        self.events: Queue[dict[str, object]] = Queue()
        self.symbol_sets: list[tuple[str, ...]] = []
        self.drain_count = 0
        self._lock = Lock()

    def set_symbols(self, symbols):
        with self._lock:
            self.symbol_sets.append(tuple(symbols))

    def push(self, event: dict[str, object]) -> None:
        self.events.put(event)

    def drain(self, *, limit, timeout_seconds):
        with self._lock:
            self.drain_count += 1
        batch = []
        try:
            batch.append(self.events.get(timeout=timeout_seconds))
        except Empty:
            return ()
        while len(batch) < limit:
            try:
                batch.append(self.events.get_nowait())
            except Empty:
                break
        return tuple(batch)

    def status(self, *, now):
        return MassiveStreamStatus(
            checked_at=now,
            authorization_binding_id=BINDING,
            connected=True,
            authenticated=True,
            snapshot_resynced=True,
            latest_quote_at=now - timedelta(milliseconds=5),
            latest_completed_bar_at=now - timedelta(seconds=30),
        )

    def close(self):
        return None


class SlowRest:
    def __init__(self, *, failing_symbols=()) -> None:
        self.authorization = authorization()
        self.release = Event()
        self.entered = Event()
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.returned_at: dict[str, datetime] = {}
        self.failing_symbols = set(failing_symbols)

    def get_json(self, path, *, parameters, timeout_seconds):
        self.calls.append((path, dict(parameters)))
        if path == "/v1/marketstatus/now":
            return {"status": "open"}
        self.entered.set()
        self.release.wait(timeout=2)
        symbol = path.split("/")[3] if path.startswith("/v3/quotes/") else path.split("/")[4]
        if symbol in self.failing_symbols:
            raise TimeoutError("redacted")
        current = now_utc()
        if path.startswith("/v3/quotes/"):
            self.returned_at[path] = current
            return {
                "results": [
                    {
                        "sip_timestamp": int((current - timedelta(seconds=1)).timestamp() * 1_000_000_000),
                        "bid_price": 10.00,
                        "ask_price": 10.02,
                        "bid_size": 7,
                        "ask_size": 9,
                    }
                ]
            }
        if "/range/1/minute/" in path:
            first_ms, last_ms = (
                int(item)
                for item in path.split("/range/1/minute/", 1)[1].split("/")[:2]
            )
            selected_ms = first_ms if first_ms == last_ms else last_ms
            start = datetime.fromtimestamp(selected_ms / 1000, timezone.utc)
            self.returned_at[path] = current
            return {
                "results": [
                    {
                        "t": int(start.timestamp() * 1000),
                        "o": 9.90,
                        "h": 10.05,
                        "l": 9.88,
                        "c": 10.01,
                        "v": 800_000,
                    }
                ]
            }
        raise AssertionError(path)


class ImmediateRest(SlowRest):
    def __init__(self, *, failing_symbols=()) -> None:
        super().__init__(failing_symbols=failing_symbols)
        self.release.set()


class GapRest(ImmediateRest):
    """Leave two completed minutes after the cold baseline for a real gap."""

    def __init__(self) -> None:
        super().__init__()
        self.aggregate_calls = 0

    def get_json(self, path, *, parameters, timeout_seconds):
        payload = super().get_json(
            path, parameters=parameters, timeout_seconds=timeout_seconds
        )
        if "/range/1/minute/" in path:
            self.aggregate_calls += 1
            if self.aggregate_calls == 1:
                payload = {"results": [dict(payload["results"][0])]}
                payload["results"][0]["t"] -= 120_000
        return payload


class BlockingHealthRest(ImmediateRest):
    def __init__(self) -> None:
        super().__init__()
        self.health_entered = Event()
        self.health_release = Event()

    def get_json(self, path, *, parameters, timeout_seconds):
        if path == "/v1/marketstatus/now":
            self.health_entered.set()
            self.health_release.wait(timeout=2)
            return {"status": "open"}
        return super().get_json(
            path, parameters=parameters, timeout_seconds=timeout_seconds
        )


class IncompleteGapRest(GapRest):
    def get_json(self, path, *, parameters, timeout_seconds):
        payload = super().get_json(
            path, parameters=parameters, timeout_seconds=timeout_seconds
        )
        if "/range/1/minute/" in path and self.aggregate_calls > 1:
            payload = {"results": [dict(payload["results"][0])]}
            payload["results"][0]["t"] += 60_000
        return payload


class MassiveHotPathTests(unittest.TestCase):
    def source(self, rest, stream, *, concurrency=2):
        source = MassiveRestStreamSource(
            candidates=CandidateSource(),
            rest=rest,
            stream=stream,
            session_state=lambda _now: MarketSessionState.ENTRY_ELIGIBLE,
            health_max_age_seconds=15,
            candidate_max_age_seconds=120,
            stream_drain_timeout_seconds=0.01,
            backfill_concurrency=concurrency,
        )
        self.addCleanup(source.close)
        return source

    @staticmethod
    def wait_until(predicate, *, timeout=1.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.005)
        return bool(predicate())

    def test_slow_rest_never_blocks_continuous_stream_ingestion(self) -> None:
        rest = SlowRest()
        stream = QueueStream()
        source = self.source(rest, stream, concurrency=1)
        cache = MarketDataCache()
        current = now_utc()
        started = time.monotonic()
        failures = source.hydrate_cache(
            cache,
            structures=(structure(),),
            session_start=current - timedelta(minutes=30),
            now=current,
            tradability=Tradable(),
        )
        elapsed = time.monotonic() - started
        self.assertEqual(failures, ())
        # A generous scheduler allowance still proves the two-second REST
        # stall was not on the caller/stream hot path.
        self.assertLess(elapsed, 0.25)
        self.assertTrue(rest.entered.wait(timeout=0.5))

        pushed_at = now_utc()
        stream.push(quote_event(at=pushed_at - timedelta(milliseconds=1)))
        self.assertTrue(
            self.wait_until(lambda: cache.quote_for("XYZ") is not None),
            "stream quote was held behind REST",
        )
        received = cache.quote_for("XYZ")
        assert received is not None
        self.assertGreaterEqual(received.observed_at, pushed_at)
        self.assertGreater(stream.drain_count, 0)
        metrics = source.hot_path_metrics(now=now_utc())
        self.assertEqual(metrics.stream_events, 1)
        self.assertEqual(metrics.stream_batches, 1)
        self.assertIsNotNone(metrics.latest_stream_receipt_at)
        self.assertIsNotNone(metrics.queue_age_p50_ms)
        self.assertIsNotNone(metrics.queue_age_p95_ms)
        self.assertIsNotNone(metrics.drain_wait_p50_ms)
        self.assertIsNotNone(metrics.drain_wait_p95_ms)
        self.assertIsNotNone(metrics.processing_p50_ms)
        self.assertIsNotNone(metrics.processing_p95_ms)
        self.assertFalse(source.wait_for_backfills(timeout_seconds=0.02))
        rest.release.set()
        self.assertTrue(source.wait_for_backfills(timeout_seconds=2))

    def test_pending_and_expired_rest_health_block_entries_not_stream(self) -> None:
        rest = BlockingHealthRest()
        stream = QueueStream()
        source = self.source(rest, stream, concurrency=2)
        current = now_utc()
        pending = source.health(now=current)
        self.assertTrue(rest.health_entered.wait(timeout=0.5))
        self.assertIn("MASSIVE_REST_HEALTH_PENDING", pending.entry_blockers)
        self.assertIn("MASSIVE_REST_HEALTH_IN_FLIGHT", pending.entry_blockers)
        self.assertFalse(pending.entry_evidence_ready)

        cache = MarketDataCache()
        source.hydrate_cache(
            cache,
            structures=(structure(),),
            session_start=current - timedelta(minutes=30),
            now=current,
            tradability=Tradable(),
        )
        stream.push(quote_event())
        self.assertTrue(self.wait_until(lambda: cache.quote_for("XYZ") is not None))

        rest.health_release.set()
        self.assertTrue(
            self.wait_until(
                lambda: source.hot_path_metrics(now=now_utc()).health_rest_calls == 1
            )
        )
        with source._lock:
            source._rest_health_at = now_utc() - timedelta(seconds=30)
            source._rest_health_future = None
        expired = source.health(now=now_utc())
        self.assertIn("MASSIVE_REST_HEALTH_EXPIRED", expired.entry_blockers)
        self.assertFalse(expired.entry_evidence_ready)

    def test_receipts_are_post_response_and_sizes_are_unscaled_shares(self) -> None:
        rest = ImmediateRest()
        source = self.source(rest, QueueStream())
        cache = MarketDataCache()
        tradability = Tradable()
        current = now_utc()
        source.hydrate_cache(
            cache,
            structures=(structure(),),
            session_start=current - timedelta(minutes=30),
            now=current,
            tradability=tradability,
        )
        self.assertTrue(source.wait_for_backfills(timeout_seconds=2))
        cached = cache.quote_for("XYZ")
        assert cached is not None
        quote_path = "/v3/quotes/XYZ"
        self.assertGreaterEqual(cached.observed_at, rest.returned_at[quote_path])
        self.assertEqual((cached.bid_size, cached.ask_size), (7, 9))
        self.assertEqual(cached.size_unit, "shares")
        self.assertEqual(
            cached.size_source_version,
            "massive_stock_quotes_shares_effective_2025-11-03",
        )
        self.assertEqual(cached.depth_scope, "top_of_book")

    def test_same_cache_refreshes_broker_eligibility_across_regular_open(self) -> None:
        rest = ImmediateRest()
        source = self.source(rest, QueueStream())
        cache = MarketDataCache()
        eligibility = MutableTradable(False)
        current = now_utc().replace(second=0, microsecond=0)
        # Equality is required for the exact 07:00 analysis slot: there is no
        # completed minute yet, but binding the continuous source is valid.
        source.hydrate_cache(
            cache,
            structures=(structure(),),
            session_start=current,
            now=current,
            tradability=eligibility,
        )
        self.assertTrue(source.wait_for_backfills(timeout_seconds=2))
        quote = cache.quote_for("XYZ")
        self.assertIsNotNone(quote)
        assert quote is not None
        self.assertFalse(quote.tradable)

        eligibility.value = True
        source.hydrate_cache(
            cache,
            structures=(structure(),),
            session_start=current,
            now=now_utc(),
            tradability=eligibility,
        )
        refreshed = cache.quote_for("XYZ")
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertTrue(refreshed.tradable)

    def test_historical_round_lot_replay_is_explicitly_converted(self) -> None:
        cache = MarketDataCache()
        venue = datetime(2025, 11, 2, 15, 0, tzinfo=timezone.utc)
        MassiveRestStreamSource._record_quote(
            cache,
            symbol="XYZ",
            raw={
                "t": int(venue.timestamp() * 1_000_000_000),
                "bp": 10.00,
                "ap": 10.02,
                "bs": 7,
                "as": 9,
            },
            received_at=venue + timedelta(milliseconds=1),
            tradable=True,
            source="historical_massive_replay",
        )
        cached = cache.quote_for("XYZ")
        assert cached is not None
        self.assertEqual((cached.bid_size, cached.ask_size), (700, 900))
        self.assertEqual(cached.size_unit, "shares")
        self.assertEqual(
            cached.size_source_version,
            "massive_stock_quotes_legacy_round_lots_converted_to_shares",
        )

    def test_second_aggregate_cannot_masquerade_as_completed_minute(self) -> None:
        rest = SlowRest()
        stream = QueueStream()
        source = self.source(rest, stream)
        cache = MarketDataCache()
        tradability = Tradable()
        current = now_utc()
        source.hydrate_cache(
            cache,
            structures=(structure(),),
            session_start=current - timedelta(minutes=30),
            now=current,
            tradability=tradability,
        )
        self.assertTrue(rest.entered.wait(timeout=0.5))
        completed_start = current.replace(second=0, microsecond=0) - timedelta(minutes=1)
        stream.push(aggregate_event("A", start=completed_start))
        self.assertTrue(
            self.wait_until(
                lambda: source.hot_path_metrics(now=now_utc()).ignored_second_aggregates
                == 1
            )
        )
        self.assertEqual(cache.symbol_state("XYZ")[1], ())

        incomplete_start = current.replace(second=0, microsecond=0)
        stream.push(aggregate_event("AM", start=incomplete_start))
        time.sleep(0.03)
        self.assertEqual(cache.symbol_state("XYZ")[1], ())

        stream.push(aggregate_event("AM", start=completed_start))
        self.assertTrue(
            self.wait_until(lambda: len(cache.symbol_state("XYZ")[1]) == 1)
        )
        bar = cache.symbol_state("XYZ")[1][0]
        self.assertEqual(bar.start_at, completed_start)
        self.assertEqual(bar.end_at, completed_start + timedelta(minutes=1))
        self.assertIn(":AM:", bar.source_event_id)
        rest.release.set()

    def test_partial_am_update_cannot_become_a_completed_minute(self) -> None:
        rest = SlowRest()
        stream = QueueStream()
        source = self.source(rest, stream)
        cache = MarketDataCache()
        current = now_utc()
        source.hydrate_cache(
            cache,
            structures=(structure(),),
            session_start=current - timedelta(minutes=30),
            now=current,
            tradability=Tradable(),
        )
        self.assertTrue(rest.entered.wait(timeout=0.5))
        completed_start = current.replace(second=0, microsecond=0) - timedelta(
            minutes=1
        )
        stream.push(
            aggregate_event(
                "AM", start=completed_start, end_offset=timedelta(seconds=12)
            )
        )
        self.assertTrue(
            self.wait_until(
                lambda: cache.degradation_for("XYZ")
                == "MASSIVE_STREAM_MINUTE_INVALID"
            )
        )
        self.assertEqual(cache.symbol_state("XYZ")[1], ())
        rest.release.set()

    def test_cold_hydration_is_once_and_one_symbol_failure_isolated(self) -> None:
        rest = ImmediateRest(failing_symbols={"BAD"})
        stream = QueueStream()
        source = self.source(rest, stream)
        cache = MarketDataCache()
        tradability = Tradable()
        current = now_utc()
        structures = (structure(), replace(structure(), symbol="BAD", source_plan_id="bad"))
        self.assertEqual(
            source.hydrate_cache(
                cache,
                structures=structures,
                session_start=current - timedelta(minutes=30),
                now=current,
                tradability=tradability,
            ),
            (),
        )
        self.assertTrue(source.wait_for_backfills(timeout_seconds=2))
        good = source.symbol_readiness("XYZ", now=now_utc())
        bad = source.symbol_readiness("BAD", now=now_utc())
        self.assertTrue(good.ready, good)
        self.assertFalse(bad.ready)
        self.assertEqual(bad.phase, "FAILED")
        calls_after_cold = len(
            [path for path, _parameters in rest.calls if path != "/v1/marketstatus/now"]
        )

        source.hydrate_cache(
            cache,
            structures=structures,
            session_start=current - timedelta(minutes=30),
            now=now_utc(),
            tradability=tradability,
        )
        self.assertTrue(source.wait_for_backfills(timeout_seconds=0.2))
        calls_after_steady = len(
            [path for path, _parameters in rest.calls if path != "/v1/marketstatus/now"]
        )
        self.assertEqual(calls_after_steady, calls_after_cold)
        metrics = source.hot_path_metrics(now=now_utc())
        self.assertEqual(metrics.cold_start_rest_calls, 3)
        self.assertEqual(metrics.steady_state_rest_calls, 0)
        self.assertEqual(metrics.ready_symbols, 1)
        self.assertEqual(stream.symbol_sets[-1], ("XYZ", "BAD"))

    def test_gap_backfill_requests_only_missing_minute_range(self) -> None:
        rest = GapRest()
        source = self.source(rest, QueueStream())
        cache = MarketDataCache()
        tradability = Tradable()
        current = now_utc()
        source.hydrate_cache(
            cache,
            structures=(structure(),),
            session_start=current - timedelta(minutes=30),
            now=current,
            tradability=tradability,
        )
        self.assertTrue(source.wait_for_backfills(timeout_seconds=2))
        _quote, bars, watermark, _degradation = cache.symbol_state("XYZ")
        self.assertIsNotNone(watermark)
        existing = max(bars, key=lambda item: item.end_at)
        gap_bar = CompletedBar.build(
            symbol="XYZ",
            start_at=existing.start_at + timedelta(minutes=2),
            end_at=existing.end_at + timedelta(minutes=2),
            open="9.90",
            high="10.05",
            low="9.88",
            close="10.01",
            volume=1,
            sequence=int(watermark) + 2,
            source_event_id="synthetic-gap",
        )
        # Use a receipt after the bar; the facts remain historical.
        cache.record_completed_bar(gap_bar, received_at=gap_bar.end_at)
        expected = cache.missing_sequence_range("XYZ")
        self.assertIsNotNone(expected)
        aggregate_calls = [path for path, _ in rest.calls if "/range/1/minute/" in path]

        source.hydrate_cache(
            cache,
            structures=(structure(),),
            session_start=current - timedelta(minutes=30),
            now=max(now_utc(), gap_bar.end_at),
            tradability=tradability,
        )
        self.assertTrue(source.wait_for_backfills(timeout_seconds=2))
        new_calls = [path for path, _ in rest.calls if "/range/1/minute/" in path]
        self.assertEqual(len(new_calls), len(aggregate_calls) + 1)
        assert expected is not None
        self.assertIn(f"/{expected[0] * 60_000}/", new_calls[-1])
        self.assertEqual(source.hot_path_metrics(now=now_utc()).gap_rest_calls, 1)

    def test_incomplete_gap_response_never_clears_symbol_degradation(self) -> None:
        rest = IncompleteGapRest()
        source = self.source(rest, QueueStream())
        cache = MarketDataCache()
        tradability = Tradable()
        current = now_utc()
        source.hydrate_cache(
            cache,
            structures=(structure(),),
            session_start=current - timedelta(minutes=30),
            now=current,
            tradability=tradability,
        )
        self.assertTrue(source.wait_for_backfills(timeout_seconds=2))
        _quote, bars, watermark, _degradation = cache.symbol_state("XYZ")
        existing = max(bars, key=lambda item: item.end_at)
        assert watermark is not None
        gap_bar = CompletedBar.build(
            symbol="XYZ",
            start_at=existing.start_at + timedelta(minutes=2),
            end_at=existing.end_at + timedelta(minutes=2),
            open="9.90",
            high="10.05",
            low="9.88",
            close="10.01",
            volume=1,
            sequence=watermark + 2,
            source_event_id="unresolved-gap",
        )
        cache.record_completed_bar(gap_bar, received_at=gap_bar.end_at)
        self.assertIsNotNone(cache.missing_sequence_range("XYZ"))
        source.hydrate_cache(
            cache,
            structures=(structure(),),
            session_start=current - timedelta(minutes=30),
            now=max(now_utc(), gap_bar.end_at),
            tradability=tradability,
        )
        self.assertTrue(source.wait_for_backfills(timeout_seconds=2))
        readiness = source.symbol_readiness("XYZ", now=max(now_utc(), gap_bar.end_at))
        self.assertFalse(readiness.ready)
        self.assertEqual(readiness.phase, "FAILED")
        self.assertIsNotNone(cache.missing_sequence_range("XYZ"))
        self.assertIn("BACKFILL_FAILED", cache.degradation_for("XYZ"))


if __name__ == "__main__":
    unittest.main()
