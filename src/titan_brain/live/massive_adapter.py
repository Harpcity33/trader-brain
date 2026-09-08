"""Massive market-data adapters with injected authorization and provenance.

The local SQLite bridge remains a compatibility source.  The production
REST/stream composition below consumes only an already-authorized transport;
it never discovers, reads, copies, or logs credentials.  Shadow plans are
discovery hints, never execution authority, and Robinhood instrument
eligibility always comes from an independent injected provider.
"""

from __future__ import annotations

from contextlib import closing
from concurrent.futures import Future, ThreadPoolExecutor
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from threading import Event, RLock, Thread
import time
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.parse import quote as url_quote, urlencode
from urllib.request import Request, urlopen

from .market_data import (
    CompletedBar,
    MarketDataCache,
    MarketSessionState,
    Quote,
)
from .money import decimal_value


class MassiveStoreError(RuntimeError):
    """The local provider store cannot prove current, compatible evidence."""


def _store_failure_code(prefix: str, error: BaseException) -> str:
    """Describe a provider/storage failure without retaining its raw message."""

    error_type = re.sub(
        r"[^A-Za-z0-9_]+", "_", type(error).__name__
    ).strip("_")
    return f"{prefix}:{error_type or 'Error'}"[:160]


class TradabilityProvider(Protocol):
    def is_tradable(self, symbol: str, *, as_of: datetime) -> bool: ...


class UnavailableTradabilityProvider:
    """Fail-closed default: Massive data cannot prove Robinhood tradability."""

    def is_tradable(self, symbol: str, *, as_of: datetime) -> bool:
        return False


@dataclass(frozen=True)
class MassiveFeedHealth:
    checked_at: datetime
    database_path: str
    producer_fresh: bool
    latest_quote_at: datetime | None
    latest_completed_bar_at: datetime | None
    component_states: Mapping[str, str]
    blockers: tuple[str, ...]
    service_healthy: bool = True
    session_state: MarketSessionState = MarketSessionState.ENTRY_ELIGIBLE
    entry_evidence_ready: bool = True
    entry_blockers: tuple[str, ...] = ()


@dataclass(frozen=True)
class PreparedStructure:
    source_plan_id: str
    symbol: str
    observed_at: datetime
    setup_id: str
    ranking_score: Decimal
    entry_limit: Decimal
    structural_stop: Decimal
    targets: tuple[Decimal, ...]
    payload_hash: str
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class MassiveAuthorizationEvidence:
    """Redacted identity of an authorization owned by an injected transport."""

    binding_id: str
    credential_source: str
    scopes: tuple[str, ...]
    authenticated: bool
    provider: str = "massive"

    def __post_init__(self) -> None:
        if self.provider != "massive":
            raise ValueError("authorization provider must be massive")
        if len(self.binding_id) != 64 or any(
            item not in "0123456789abcdef" for item in self.binding_id
        ):
            raise ValueError("authorization binding_id must be lowercase SHA-256")
        if not self.credential_source.strip():
            raise ValueError("redacted credential source identifier is required")
        if not self.scopes or any(not str(item).strip() for item in self.scopes):
            raise ValueError("authorization scopes must be explicit")
        if self.authenticated is not True:
            raise ValueError("Massive transport is not authenticated")


class MassiveRequestAuthorizer(Protocol):
    """Owner-supplied authorization; implementations retain secret custody."""

    @property
    def evidence(self) -> MassiveAuthorizationEvidence: ...

    def authorize(self, headers: Mapping[str, str]) -> Mapping[str, str]: ...


class MassiveRestTransport(Protocol):
    @property
    def authorization(self) -> MassiveAuthorizationEvidence: ...

    def get_json(
        self,
        path: str,
        *,
        parameters: Mapping[str, str],
        timeout_seconds: float,
    ) -> Mapping[str, Any]: ...


class UrllibMassiveRestTransport:
    """Bounded standard-library REST transport for an injected authorizer.

    This class intentionally has no environment, keychain, config-file, or
    OAuth discovery code.  The owning process must inject an authorizer backed
    by an existing provider-supported credential.  Headers are never returned
    in errors or persisted by this adapter.
    """

    ALLOWED_HOSTS = frozenset({"api.massive.com", "api.polygon.io"})

    def __init__(
        self,
        authorizer: MassiveRequestAuthorizer,
        *,
        base_url: str = "https://api.massive.com",
        opener: Callable[..., Any] = urlopen,
        maximum_response_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        from urllib.parse import urlparse

        parsed = urlparse(base_url)
        if parsed.scheme != "https" or parsed.hostname not in self.ALLOWED_HOSTS:
            raise ValueError("Massive REST base URL must be an approved HTTPS host")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("Massive REST base URL must not contain a path or query")
        if maximum_response_bytes <= 0:
            raise ValueError("maximum response size must be positive")
        # Accessing evidence validates the injected binding without asking for
        # or copying its secret material.
        self._authorization = authorizer.evidence
        self._authorizer = authorizer
        self._base_url = base_url.rstrip("/")
        self._opener = opener
        self._maximum_response_bytes = int(maximum_response_bytes)

    def release_components(self) -> tuple[tuple[str, object, tuple[str, ...]], ...]:
        """Declare every injected executable dependency.

        The production composition accepts only the module-captured standard
        library opener. Tests may still inject an opener when exercising this
        transport directly, but such an instance cannot be attested for live
        readiness.
        """

        if self._opener is not urlopen:
            raise ValueError("custom Massive REST opener is not production-attestable")
        return (("massive_rest_authorizer", self._authorizer, ("authorize",)),)

    @property
    def authorization(self) -> MassiveAuthorizationEvidence:
        return self._authorization

    def get_json(
        self,
        path: str,
        *,
        parameters: Mapping[str, str],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        if not path.startswith("/") or path.startswith("//") or "://" in path:
            raise ValueError("Massive REST path must be origin-relative")
        if not 0 < float(timeout_seconds) <= 30:
            raise ValueError("Massive REST timeout must be in (0, 30]")
        headers = dict(
            self._authorizer.authorize(
                {"Accept": "application/json", "User-Agent": "titan-full-live/2"}
            )
        )
        if not headers or any(not str(key).strip() for key in headers):
            raise MassiveStoreError("injected Massive authorization returned no headers")
        query = urlencode(sorted((str(key), str(value)) for key, value in parameters.items()))
        url = f"{self._base_url}{path}" + (f"?{query}" if query else "")
        request = Request(url, method="GET", headers=headers)
        try:
            with self._opener(request, timeout=float(timeout_seconds)) as response:
                payload = response.read(self._maximum_response_bytes + 1)
        except Exception as exc:
            raise MassiveStoreError(
                f"Massive REST request failed: {type(exc).__name__}"
            ) from exc
        if len(payload) > self._maximum_response_bytes:
            raise MassiveStoreError("Massive REST response exceeds size limit")
        try:
            decoded = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MassiveStoreError("Massive REST response is not valid JSON") from exc
        if not isinstance(decoded, Mapping):
            raise MassiveStoreError("Massive REST response must be an object")
        return dict(decoded)


@dataclass(frozen=True)
class MassiveStreamStatus:
    checked_at: datetime
    authorization_binding_id: str
    connected: bool
    authenticated: bool
    snapshot_resynced: bool
    latest_quote_at: datetime | None
    latest_completed_bar_at: datetime | None
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "checked_at", _aware(self.checked_at, "checked_at"))
        for name in ("latest_quote_at", "latest_completed_bar_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _aware(value, name))
        if len(self.authorization_binding_id) != 64:
            raise ValueError("stream authorization binding is invalid")


class MassiveStreamTransport(Protocol):
    """Existing authorized WebSocket client injected into the live adapter."""

    @property
    def authorization(self) -> MassiveAuthorizationEvidence: ...

    def status(self, *, now: datetime) -> MassiveStreamStatus: ...

    def set_symbols(self, symbols: Sequence[str]) -> None: ...

    def close(self) -> None: ...

    def drain(
        self, *, limit: int, timeout_seconds: float
    ) -> Sequence[Mapping[str, Any]]: ...


@dataclass(frozen=True)
class MassiveSymbolReadiness:
    symbol: str
    phase: str
    ready: bool
    quote_received_at: datetime | None
    completed_bar_end: datetime | None
    blocker: str | None


@dataclass(frozen=True)
class MassiveSymbolEvidenceSnapshot:
    sampled_at: datetime
    symbol: str
    quote: Quote | None
    latest_completed_bar: CompletedBar | None
    readiness: MassiveSymbolReadiness


@dataclass(frozen=True)
class MassiveHotPathMetrics:
    observed_at: datetime
    watched_symbols: int
    ready_symbols: int
    pending_backfills: int
    cold_start_rest_calls: int
    gap_rest_calls: int
    steady_state_rest_calls: int
    health_rest_calls: int
    stream_batches: int
    stream_events: int
    stream_backlog_batches: int
    ignored_second_aggregates: int
    stream_failures: int
    queue_age_p50_ms: float | None
    queue_age_p95_ms: float | None
    drain_wait_p50_ms: float | None
    drain_wait_p95_ms: float | None
    processing_p50_ms: float | None
    processing_p95_ms: float | None
    latest_stream_receipt_at: datetime | None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * percentile)))
    return round(ordered[index], 3)


class PreparedCandidateSource(Protocol):
    def prepared_structures(
        self, *, now: datetime, limit: int
    ) -> tuple[PreparedStructure, ...]: ...


def _provider_time(value: object, field: str) -> datetime:
    if isinstance(value, bool):
        raise MassiveStoreError(f"{field} is invalid")
    try:
        raw = int(value)
    except (TypeError, ValueError) as exc:
        raise MassiveStoreError(f"{field} is invalid") from exc
    if raw <= 0:
        raise MassiveStoreError(f"{field} is invalid")
    # Massive quote timestamps are nanoseconds while aggregates use
    # milliseconds.  Preserve that explicit unit distinction by magnitude.
    divisor = 1_000_000_000 if raw >= 10**15 else 1_000
    return datetime.fromtimestamp(raw / divisor, timezone.utc)


_MASSIVE_QUOTE_SIZE_CUTOVER = datetime(2025, 11, 3, tzinfo=timezone.utc)


def _massive_quote_sizes(
    bid_size: object, ask_size: object, *, venue_at: datetime
) -> tuple[int, int, str]:
    """Normalize provider-versioned stock quote sizes to executable shares."""

    values: list[int] = []
    for name, raw in (("bid_size", bid_size), ("ask_size", ask_size)):
        if isinstance(raw, bool):
            raise MassiveStoreError(f"{name} is invalid")
        try:
            value = Decimal(str(raw))
        except Exception as exc:
            raise MassiveStoreError(f"{name} is invalid") from exc
        if value < 0 or value != value.to_integral_value():
            raise MassiveStoreError(f"{name} is invalid")
        values.append(int(value))
    if venue_at >= _MASSIVE_QUOTE_SIZE_CUTOVER:
        return (
            values[0],
            values[1],
            "massive_stock_quotes_shares_effective_2025-11-03",
        )
    return (
        values[0] * 100,
        values[1] * 100,
        "massive_stock_quotes_legacy_round_lots_converted_to_shares",
    )


class MassiveRestStreamSource:
    """Continuously ingested Massive evidence with isolated REST backfills.

    One daemon consumer is the sole owner of ``stream.drain``.  Candidate
    discovery only updates its bounded subscription and schedules cold/gap
    work; it never drains the stream or waits for historical REST.  Slow
    history therefore makes only the affected symbol unready and cannot stop
    quote/minute-event ingestion for the remaining book.

    Massive stock quote sizes are stored exactly as *shares*, per the current
    provider contract effective 2025-11-03.  They are NBBO/top-of-book sizes,
    not round lots and not full order-book depth.
    """

    def __init__(
        self,
        *,
        candidates: PreparedCandidateSource,
        rest: MassiveRestTransport,
        stream: MassiveStreamTransport,
        session_state: Callable[[datetime], MarketSessionState],
        health_max_age_seconds: int,
        candidate_max_age_seconds: int,
        request_timeout_seconds: float = 3.0,
        stream_drain_timeout_seconds: float = 0.05,
        stream_batch_limit: int = 1000,
        backfill_concurrency: int = 4,
    ) -> None:
        if rest.authorization.binding_id != stream.authorization.binding_id:
            raise ValueError("Massive REST and stream authorization bindings differ")
        if health_max_age_seconds <= 0 or candidate_max_age_seconds <= 0:
            raise ValueError("Massive freshness limits must be positive")
        if not 0 < request_timeout_seconds <= 30:
            raise ValueError("Massive request timeout must be in (0, 30]")
        if not 0 <= stream_drain_timeout_seconds <= 5:
            raise ValueError("Massive stream drain timeout must be in [0, 5]")
        if not 1 <= stream_batch_limit <= 10_000:
            raise ValueError("Massive stream batch limit must be in [1, 10000]")
        if not 1 <= backfill_concurrency <= 16:
            raise ValueError("Massive backfill concurrency must be in [1, 16]")
        self.candidates = candidates
        self.rest = rest
        self.stream = stream
        self.session_state = session_state
        self.health_max_age_seconds = int(health_max_age_seconds)
        self.candidate_max_age_seconds = int(candidate_max_age_seconds)
        self.request_timeout_seconds = float(request_timeout_seconds)
        self.stream_drain_timeout_seconds = float(stream_drain_timeout_seconds)
        self.stream_batch_limit = int(stream_batch_limit)
        self.backfill_concurrency = int(backfill_concurrency)

        self._lock = RLock()
        self._stop = Event()
        self._stream_thread: Thread | None = None
        self._executor = ThreadPoolExecutor(
            max_workers=self.backfill_concurrency,
            thread_name_prefix="titan-massive-rest",
        )
        self._cache: MarketDataCache | None = None
        self._tradability: TradabilityProvider | None = None
        self._watched: tuple[str, ...] = ()
        self._session_start: datetime | None = None
        self._session_generation = 0
        self._symbol_phase: dict[str, str] = {}
        self._symbol_blocker: dict[str, str] = {}
        self._symbol_tradable: dict[str, bool] = {}
        self._backfills: dict[str, Future[tuple[str, str | None]]] = {}
        self._pending_minutes: dict[tuple[str, datetime], Mapping[str, Any]] = {}
        self._rest_health_future: Future[None] | None = None
        self._rest_health_at: datetime | None = None
        self._rest_health_state = "pending"
        self._rest_health_failure: str | None = None
        self._metrics: dict[str, int] = {
            "cold_start_rest_calls": 0,
            "gap_rest_calls": 0,
            "steady_state_rest_calls": 0,
            "health_rest_calls": 0,
            "stream_batches": 0,
            "stream_events": 0,
            "stream_backlog_batches": 0,
            "ignored_second_aggregates": 0,
            "stream_failures": 0,
        }
        self._queue_ages_ms: deque[float] = deque(maxlen=4096)
        self._drain_wait_ms: deque[float] = deque(maxlen=4096)
        self._processing_ms: deque[float] = deque(maxlen=4096)
        self._latest_stream_receipt_at: datetime | None = None

    def release_components(self) -> tuple[tuple[str, object, tuple[str, ...]], ...]:
        """Declare transports, candidates, and session logic for attestation."""

        return (
            (
                "massive_rest_transport",
                self.rest,
                ("get_json",),
            ),
            (
                "massive_stream_transport",
                self.stream,
                ("status", "set_symbols", "drain", "close"),
            ),
            (
                "prepared_candidate_source",
                self.candidates,
                ("prepared_structures",),
            ),
            ("market_session_state", self.session_state, ("__call__",)),
        )

    def close(self, *, wait: bool = True) -> None:
        """Stop background ingestion without mutating any provider state."""

        self._stop.set()
        # Closing the read-only socket first unblocks a transport drain that
        # may otherwise wait for its configured timeout.
        self.stream.close()
        thread = self._stream_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0 if wait else 0.0)
        self._executor.shutdown(wait=wait, cancel_futures=True)

    def _schedule_rest_health(self, current: datetime) -> None:
        with self._lock:
            prior = self._rest_health_future
            fresh = (
                self._rest_health_at is not None
                and (current - self._rest_health_at).total_seconds()
                <= self.health_max_age_seconds
            )
            if fresh or (prior is not None and not prior.done()) or self._stop.is_set():
                return
            self._rest_health_future = self._executor.submit(self._probe_rest_health)

    def _probe_rest_health(self) -> None:
        try:
            response = self.rest.get_json(
                "/v1/marketstatus/now",
                parameters={},
                timeout_seconds=self.request_timeout_seconds,
            )
            received_at = _utc_now()
            state = str(response.get("status", "reachable"))
            failure = None
        except Exception as exc:
            received_at = _utc_now()
            state = "unavailable"
            failure = f"MASSIVE_REST_UNAVAILABLE:{type(exc).__name__}"
        with self._lock:
            self._metrics["health_rest_calls"] += 1
            self._rest_health_at = received_at
            self._rest_health_state = state
            self._rest_health_failure = failure

    def health(self, *, now: datetime) -> MassiveFeedHealth:
        current = _aware(now, "now")
        # Snapshot the last completed probe before launching its refresh.  A
        # probe that races to completion during this call cannot retroactively
        # make evidence that was pending or expired at the call boundary look
        # ready.
        with self._lock:
            rest_state = self._rest_health_state
            rest_failure = self._rest_health_failure
            rest_health_at = self._rest_health_at
        self._schedule_rest_health(current)
        health_blockers: list[str] = []
        entry_blockers: list[str] = []
        states: dict[str, str] = {}
        try:
            session = self.session_state(current)
            if not isinstance(session, MarketSessionState):
                session = MarketSessionState(str(session))
        except Exception as exc:
            session = MarketSessionState.WAITING_FOR_SESSION
            health_blockers.append(f"SESSION_STATE_UNAVAILABLE:{type(exc).__name__}")
        with self._lock:
            rest_probe = self._rest_health_future
        states["massive_rest"] = rest_state
        if rest_failure is not None:
            health_blockers.append(rest_failure)
        if rest_health_at is None:
            entry_blockers.append("MASSIVE_REST_HEALTH_PENDING")
        else:
            rest_age = (current - rest_health_at).total_seconds()
            if rest_age < -1:
                entry_blockers.append("MASSIVE_REST_HEALTH_FUTURE_DATED")
            elif rest_age > self.health_max_age_seconds:
                entry_blockers.append("MASSIVE_REST_HEALTH_EXPIRED")
        if rest_probe is not None and not rest_probe.done():
            entry_blockers.append("MASSIVE_REST_HEALTH_IN_FLIGHT")
        latest_quote: datetime | None = None
        latest_bar: datetime | None = None
        try:
            stream = self.stream.status(now=current)
            latest_quote = stream.latest_quote_at
            latest_bar = stream.latest_completed_bar_at
            states["massive_stream"] = (
                "connected" if stream.connected else "disconnected"
            )
            if stream.authorization_binding_id != self.rest.authorization.binding_id:
                health_blockers.append("MASSIVE_STREAM_AUTHORIZATION_BINDING_MISMATCH")
            if session is MarketSessionState.ENTRY_ELIGIBLE:
                if not stream.connected:
                    entry_blockers.append("MASSIVE_STREAM_DISCONNECTED")
                if not stream.authenticated:
                    entry_blockers.append("MASSIVE_STREAM_UNAUTHENTICATED")
                if not stream.snapshot_resynced:
                    entry_blockers.append("MASSIVE_STREAM_RESYNC_REQUIRED")
                for name, value, maximum in (
                    ("QUOTE", latest_quote, self.health_max_age_seconds),
                    ("COMPLETED_BAR", latest_bar, self.candidate_max_age_seconds),
                ):
                    if value is None:
                        entry_blockers.append(f"MASSIVE_{name}_MISSING")
                    else:
                        age = (current - value).total_seconds()
                        if age < -1:
                            entry_blockers.append(f"MASSIVE_{name}_FUTURE_DATED")
                        elif age > maximum:
                            entry_blockers.append(f"MASSIVE_{name}_STALE")
        except Exception as exc:
            states["massive_stream"] = "unavailable"
            if session is MarketSessionState.ENTRY_ELIGIBLE:
                entry_blockers.append(f"MASSIVE_STREAM_STATUS_FAILED:{type(exc).__name__}")
        if session is MarketSessionState.WAITING_FOR_SESSION:
            entry_blockers = ["WAITING_FOR_SESSION"]
        service_healthy = not health_blockers
        entry_ready = (
            service_healthy
            and session is MarketSessionState.ENTRY_ELIGIBLE
            and not entry_blockers
        )
        return MassiveFeedHealth(
            checked_at=current,
            database_path="massive://rest+stream",
            producer_fresh=entry_ready,
            latest_quote_at=latest_quote,
            latest_completed_bar_at=latest_bar,
            component_states=states,
            blockers=tuple(dict.fromkeys(health_blockers)),
            service_healthy=service_healthy,
            session_state=session,
            entry_evidence_ready=entry_ready,
            entry_blockers=tuple(dict.fromkeys(entry_blockers)),
        )

    def prepared_structures(
        self, *, now: datetime, limit: int
    ) -> tuple[PreparedStructure, ...]:
        return self.candidates.prepared_structures(now=now, limit=limit)

    def hydrate_cache(
        self,
        cache: MarketDataCache,
        *,
        structures: Sequence[PreparedStructure],
        session_start: datetime,
        now: datetime,
        tradability: TradabilityProvider,
    ) -> tuple[str, ...]:
        current = _aware(now, "now")
        start = _aware(session_start, "session_start")
        if start >= current:
            raise ValueError("session_start must precede now")
        symbols = tuple(
            dict.fromkeys(item.symbol.strip().upper() for item in structures)
        )
        cache.begin_session(start)
        cache.set_active_scores((item.symbol, item.ranking_score) for item in structures)
        with self._lock:
            if self._cache is not None and self._cache is not cache:
                raise ValueError("Massive source cannot be rebound to another cache")
            if self._tradability is not None and self._tradability is not tradability:
                raise ValueError("Massive source cannot be rebound to another tradability provider")
            session_changed = (
                self._session_start is not None
                and self._session_start != start
            )
            self._cache = cache
            self._tradability = tradability
            self._session_start = start
            self._watched = symbols
            if session_changed:
                self._session_generation += 1
                self._symbol_phase.clear()
                self._symbol_blocker.clear()
                self._symbol_tradable.clear()
                self._pending_minutes.clear()

        # The transport owns actual subscribe/unsubscribe and reconnect
        # resubscription mechanics.  This call is idempotent and contains no
        # broker or financial mutation.
        self.stream.set_symbols(symbols)
        self._ensure_stream_consumer()

        for symbol in symbols:
            _, _, _, degradation = cache.symbol_state(symbol)
            with self._lock:
                phase = self._symbol_phase.get(symbol)
                active = self._backfills.get(symbol)
            if phase is None:
                self._schedule_backfill(symbol, mode="cold", start=start, end=current)
            else:
                gap = cache.missing_sequence_range(symbol)
                resync_required = gap is not None or degradation in {
                    "MARKET_DATA_DISCONNECTED:massive",
                }
                if not resync_required or (active is not None and not active.done()):
                    continue
                gap_start = (
                    datetime.fromtimestamp(gap[0] * 60, timezone.utc)
                    if gap is not None
                    else start
                )
                gap_end = (
                    datetime.fromtimestamp(gap[1] * 60, timezone.utc)
                    if gap is not None
                    else current
                )
                self._schedule_backfill(
                    symbol, mode="gap", start=gap_start, end=gap_end
                )

        # Per-symbol initialization/failure is represented in the cache and
        # readiness API.  It must not convert one cold candidate into a global
        # discovery or protection failure.
        return ()

    def _ensure_stream_consumer(self) -> None:
        with self._lock:
            if self._stream_thread is not None and self._stream_thread.is_alive():
                return
            if self._stop.is_set():
                raise MassiveStoreError("Massive source is closed")
            self._stream_thread = Thread(
                target=self._stream_loop,
                name="titan-massive-stream",
                daemon=True,
            )
            self._stream_thread.start()

    def _stream_loop(self) -> None:
        while not self._stop.is_set():
            drain_started = time.monotonic()
            try:
                events = self.stream.drain(
                    limit=self.stream_batch_limit,
                    timeout_seconds=self.stream_drain_timeout_seconds,
                )
                # Receipt is sampled only after the transport returned the
                # batch. Venue timestamps remain untouched inside each event.
                received_at = _utc_now()
                processing_started = time.monotonic()
                self._process_stream_batch(tuple(events), received_at=received_at)
            except Exception:
                received_at = _utc_now()
                processing_started = time.monotonic()
                with self._lock:
                    self._metrics["stream_failures"] += 1
                    cache = self._cache
                if cache is not None:
                    cache.mark_disconnect("massive")
            completed = time.monotonic()
            with self._lock:
                self._drain_wait_ms.append(
                    max(0.0, (processing_started - drain_started) * 1000)
                )
                self._processing_ms.append(
                    max(0.0, (completed - processing_started) * 1000)
                )
            # Test transports can return immediately; avoid a hot spin while
            # retaining sub-millisecond handoff with a real blocking socket.
            self._stop.wait(0.001)

    def _process_stream_batch(
        self, events: Sequence[Mapping[str, Any]], *, received_at: datetime
    ) -> None:
        receipt = _aware(received_at, "stream receipt")
        with self._lock:
            watched = set(self._watched)
            cache = self._cache
            eligible = dict(self._symbol_tradable)
            if events:
                self._metrics["stream_batches"] += 1
                self._metrics["stream_events"] += len(events)
                if len(events) >= self.stream_batch_limit:
                    self._metrics["stream_backlog_batches"] += 1
                self._latest_stream_receipt_at = receipt
        if cache is None:
            return
        for event in events:
            if not isinstance(event, Mapping):
                with self._lock:
                    self._metrics["stream_failures"] += 1
                continue
            symbol = str(event.get("sym", "")).strip().upper()
            if symbol not in watched:
                continue
            kind = str(event.get("ev", "")).upper()
            if kind == "Q":
                try:
                    self._record_quote(
                        cache,
                        symbol=symbol,
                        raw=event,
                        received_at=receipt,
                        tradable=eligible.get(symbol, False),
                        source="massive_stream_nbbo_top_of_book+robinhood_instrument",
                    )
                    venue = _provider_time(
                        event.get("sip_timestamp", event.get("t")),
                        f"quote timestamp {symbol}",
                    )
                    with self._lock:
                        self._queue_ages_ms.append(
                            max(0.0, (receipt - venue).total_seconds() * 1000)
                        )
                except Exception:
                    cache.mark_symbol_degraded(symbol, "MASSIVE_STREAM_QUOTE_INVALID")
            elif kind == "A":
                # Per-second aggregates are deliberately not minute bars.
                with self._lock:
                    self._metrics["ignored_second_aggregates"] += 1
            elif kind == "AM":
                try:
                    start_at = self._minute_start(event, stream=True)
                    with self._lock:
                        self._pending_minutes[(symbol, start_at)] = dict(event)
                except Exception:
                    cache.mark_symbol_degraded(symbol, "MASSIVE_STREAM_MINUTE_INVALID")
        self._flush_completed_minutes(receipt)

    def _flush_completed_minutes(self, received_at: datetime) -> None:
        with self._lock:
            ready = [
                (key, raw)
                for key, raw in self._pending_minutes.items()
                if key[1] + timedelta(minutes=1) <= received_at
            ]
            for key, _raw in ready:
                self._pending_minutes.pop(key, None)
            cache = self._cache
        if cache is None:
            return
        for (symbol, _start), raw in ready:
            try:
                bar = self._minute_bar(symbol, raw, stream=True)
                cache.record_completed_bar(bar, received_at=received_at)
            except Exception:
                cache.mark_symbol_degraded(symbol, "MASSIVE_STREAM_MINUTE_INVALID")

    def _schedule_backfill(
        self, symbol: str, *, mode: str, start: datetime, end: datetime
    ) -> None:
        with self._lock:
            prior = self._backfills.get(symbol)
            if prior is not None and not prior.done():
                return
            self._symbol_phase[symbol] = "COLD_START" if mode == "cold" else "RESYNC"
            self._symbol_blocker[symbol] = (
                "MASSIVE_INITIAL_HISTORY_PENDING"
                if mode == "cold"
                else "MASSIVE_GAP_RESYNC_PENDING"
            )
            cache = self._cache
            if cache is not None:
                cache.mark_symbol_degraded(symbol, self._symbol_blocker[symbol])
            future = self._executor.submit(
                self._backfill_symbol,
                symbol,
                mode,
                _aware(start, "backfill start"),
                _aware(end, "backfill end"),
                self._session_generation,
            )
            self._backfills[symbol] = future
            future.add_done_callback(
                lambda completed, requested=symbol, generation=self._session_generation: self._finish_backfill(
                    requested, generation, completed
                )
            )

    def _backfill_symbol(
        self,
        symbol: str,
        mode: str,
        start: datetime,
        end: datetime,
        generation: int,
    ) -> tuple[str, str | None]:
        with self._lock:
            cache = self._cache
            tradability = self._tradability
            current_generation = self._session_generation
        if generation != current_generation:
            return "OBSOLETE", None
        if cache is None or tradability is None:
            return "FAILED", "MASSIVE_SOURCE_NOT_BOUND"
        try:
            eligible = tradability.is_tradable(symbol, as_of=_utc_now())
            with self._lock:
                self._symbol_tradable[symbol] = eligible is True
            existing_quote = cache.quote_for(symbol)
            if mode == "cold" and existing_quote is None:
                with self._lock:
                    self._metrics["cold_start_rest_calls"] += 1
                quote_payload = self.rest.get_json(
                    f"/v3/quotes/{url_quote(symbol, safe='')}",
                    parameters={"limit": "1", "order": "desc", "sort": "timestamp"},
                    timeout_seconds=self.request_timeout_seconds,
                )
                quote_received_at = _utc_now()
                with self._lock:
                    if generation != self._session_generation:
                        return "OBSOLETE", None
                quote_results = quote_payload.get("results")
                if not isinstance(quote_results, list) or not quote_results:
                    raise MassiveStoreError("Massive quote result is missing")
                self._record_quote(
                    cache,
                    symbol=symbol,
                    raw=quote_results[0],
                    received_at=quote_received_at,
                    tradable=eligible,
                    source="massive_rest_nbbo_top_of_book+robinhood_instrument",
                )

            first_ms = int(start.timestamp() * 1000)
            # Only completed minute starts are requested.  The response receipt
            # below is still the final authority on whether each returned bar
            # was actually complete when observed.
            last_complete = _utc_now().replace(second=0, microsecond=0) - timedelta(
                minutes=1
            )
            requested_end = min(end, last_complete)
            if requested_end < start:
                raise MassiveStoreError("no completed minute is available")
            last_ms = int(requested_end.timestamp() * 1000)
            with self._lock:
                self._metrics[
                    "cold_start_rest_calls" if mode == "cold" else "gap_rest_calls"
                ] += 1
            bars_payload = self.rest.get_json(
                f"/v2/aggs/ticker/{url_quote(symbol, safe='')}/range/1/minute/"
                f"{first_ms}/{last_ms}",
                parameters={"adjusted": "true", "limit": "50000", "sort": "asc"},
                timeout_seconds=self.request_timeout_seconds,
            )
            bars_received_at = _utc_now()
            with self._lock:
                if generation != self._session_generation:
                    return "OBSOLETE", None
            if bars_payload.get("next_url"):
                raise MassiveStoreError("Massive aggregate backfill is incomplete")
            results = bars_payload.get("results")
            if not isinstance(results, list) or not results:
                raise MassiveStoreError("Massive aggregate result is missing")
            last_sequence = 0
            for raw in results:
                if not isinstance(raw, Mapping):
                    raise MassiveStoreError("Massive aggregate row is invalid")
                bar = self._minute_bar(symbol, raw, stream=False)
                if (
                    bar.start_at < start
                    or bar.start_at > requested_end
                    or bar.end_at > bars_received_at
                ):
                    continue
                existing = cache.bar_for(symbol, bar.end_at)
                if existing is not None:
                    factual_fields = (
                        "start_at",
                        "end_at",
                        "open",
                        "high",
                        "low",
                        "close",
                        "volume",
                        "sequence",
                    )
                    if any(
                        getattr(existing, field) != getattr(bar, field)
                        for field in factual_fields
                    ):
                        raise MassiveStoreError(
                            "REST backfill conflicts with an ingested stream bar"
                        )
                else:
                    cache.record_completed_bar(bar, received_at=bars_received_at)
                last_sequence = max(last_sequence, bar.sequence)
            if not last_sequence:
                raise MassiveStoreError("no completed in-range aggregate is available")
            remaining_gap = cache.missing_sequence_range(symbol)
            if remaining_gap is not None:
                requested_first = int(start.timestamp()) // 60
                requested_last = int(requested_end.timestamp()) // 60
                if mode != "gap" or not (
                    remaining_gap[1] < requested_first
                    or remaining_gap[0] > requested_last
                ):
                    raise MassiveStoreError(
                        "Massive gap backfill did not cover every missing minute"
                    )
                # A newer, disjoint gap appeared while this request was in
                # flight.  Preserve it; an older response cannot prove the
                # symbol contiguous through the newer watermark.
                raise MassiveStoreError(
                    "Massive symbol developed a newer gap during backfill"
                )
            cache.clear_symbol_degradation(
                symbol,
                expected_reason=(
                    "MASSIVE_INITIAL_HISTORY_PENDING"
                    if mode == "cold"
                    else "MASSIVE_GAP_RESYNC_PENDING"
                ),
            )
            return "READY", None
        except Exception as exc:
            return "FAILED", f"MASSIVE_BACKFILL_FAILED:{type(exc).__name__}"

    def _finish_backfill(
        self,
        symbol: str,
        generation: int,
        future: Future[tuple[str, str | None]],
    ) -> None:
        try:
            phase, blocker = future.result()
        except Exception as exc:
            phase, blocker = "FAILED", f"MASSIVE_BACKFILL_FAILED:{type(exc).__name__}"
        with self._lock:
            if generation != self._session_generation:
                return
            self._symbol_phase[symbol] = phase
            if blocker is None:
                self._symbol_blocker.pop(symbol, None)
            else:
                self._symbol_blocker[symbol] = blocker
            cache = self._cache
        if cache is not None and blocker is not None:
            cache.mark_symbol_degraded(symbol, blocker)

    def wait_for_backfills(self, *, timeout_seconds: float = 5.0) -> bool:
        """Bounded test/operations barrier; never used in the trading hot path."""

        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        while time.monotonic() <= deadline:
            with self._lock:
                pending = [future for future in self._backfills.values() if not future.done()]
                transitional = any(
                    phase in {"COLD_START", "RESYNC"}
                    for phase in self._symbol_phase.values()
                )
            if not pending and not transitional:
                return True
            self._stop.wait(0.005)
        return False

    def symbol_readiness(
        self, symbol: str, *, now: datetime
    ) -> MassiveSymbolReadiness:
        normalized = str(symbol).strip().upper()
        current = _aware(now, "readiness now")
        with self._lock:
            cache = self._cache
            phase = self._symbol_phase.get(normalized, "UNBOUND")
            blocker = self._symbol_blocker.get(normalized)
        if cache is None:
            return MassiveSymbolReadiness(
                normalized, phase, False, None, None, blocker or "MASSIVE_CACHE_UNBOUND"
            )
        quote, bars, _watermark, degradation = cache.symbol_state(normalized)
        latest_bar = max((bar.end_at for bar in bars), default=None)
        if quote is None:
            blocker = blocker or "QUOTE_MISSING"
        else:
            quote_age = (current - quote.newest_venue_at).total_seconds()
            if quote_age < -1:
                blocker = blocker or "QUOTE_FUTURE_DATED"
            elif quote_age > self.health_max_age_seconds:
                blocker = blocker or "QUOTE_STALE"
        if latest_bar is None:
            blocker = blocker or "COMPLETED_BAR_MISSING"
        else:
            bar_age = (current - latest_bar).total_seconds()
            if bar_age < 0:
                blocker = blocker or "COMPLETED_BAR_FUTURE_DATED"
            elif bar_age > self.candidate_max_age_seconds:
                blocker = blocker or "COMPLETED_BAR_STALE"
        blocker = blocker or degradation
        return MassiveSymbolReadiness(
            symbol=normalized,
            phase=phase,
            ready=phase == "READY" and blocker is None,
            quote_received_at=quote.observed_at if quote is not None else None,
            completed_bar_end=latest_bar,
            blocker=blocker,
        )

    def evidence_snapshot(
        self, symbol: str, *, now: datetime
    ) -> MassiveSymbolEvidenceSnapshot:
        current = _aware(now, "evidence snapshot now")
        readiness = self.symbol_readiness(symbol, now=current)
        with self._lock:
            cache = self._cache
        if cache is None:
            quote = None
            latest = None
        else:
            quote, bars, _watermark, _degraded = cache.symbol_state(symbol)
            latest = max(bars, key=lambda item: item.end_at, default=None)
        return MassiveSymbolEvidenceSnapshot(
            sampled_at=current,
            symbol=str(symbol).strip().upper(),
            quote=quote,
            latest_completed_bar=latest,
            readiness=readiness,
        )

    def hot_path_metrics(self, *, now: datetime) -> MassiveHotPathMetrics:
        current = _aware(now, "metrics now")
        with self._lock:
            values = dict(self._metrics)
            queue_ages = tuple(self._queue_ages_ms)
            drain_wait = tuple(self._drain_wait_ms)
            processing = tuple(self._processing_ms)
            pending = sum(not future.done() for future in self._backfills.values())
            latest = self._latest_stream_receipt_at
            watched_symbols = set(self._watched)
        ready_symbols = sum(
            self.symbol_readiness(symbol, now=current).ready
            for symbol in watched_symbols
        )
        return MassiveHotPathMetrics(
            observed_at=current,
            watched_symbols=len(watched_symbols),
            ready_symbols=ready_symbols,
            pending_backfills=pending,
            queue_age_p50_ms=_percentile(queue_ages, 0.50),
            queue_age_p95_ms=_percentile(queue_ages, 0.95),
            drain_wait_p50_ms=_percentile(drain_wait, 0.50),
            drain_wait_p95_ms=_percentile(drain_wait, 0.95),
            processing_p50_ms=_percentile(processing, 0.50),
            processing_p95_ms=_percentile(processing, 0.95),
            latest_stream_receipt_at=latest,
            **values,
        )

    @staticmethod
    def _record_quote(
        cache: MarketDataCache,
        *,
        symbol: str,
        raw: Mapping[str, Any],
        received_at: datetime,
        tradable: bool,
        source: str,
    ) -> None:
        timestamp = raw.get("sip_timestamp", raw.get("t"))
        venue = _provider_time(timestamp, f"quote timestamp {symbol}")
        bid_size, ask_size, size_version = _massive_quote_sizes(
            raw.get("bid_size", raw.get("bs")),
            raw.get("ask_size", raw.get("as")),
            venue_at=venue,
        )
        cache.record_quote(
            Quote.build(
                symbol=symbol,
                bid=raw.get("bid_price", raw.get("bp")),
                ask=raw.get("ask_price", raw.get("ap")),
                bid_size=bid_size,
                ask_size=ask_size,
                venue_bid_at=venue,
                venue_ask_at=venue,
                observed_at=received_at,
                source=source,
                tradable=tradable,
                size_unit="shares",
                size_source_version=size_version,
                depth_scope="top_of_book",
            )
        )

    @staticmethod
    def _minute_start(raw: Mapping[str, Any], *, stream: bool) -> datetime:
        if stream and str(raw.get("ev", "")).upper() != "AM":
            raise MassiveStoreError("only AM events are minute aggregates")
        start_at = _provider_time(
            raw.get("s") if stream else raw.get("t"), "aggregate start"
        )
        if start_at.second != 0 or start_at.microsecond != 0:
            raise MassiveStoreError("minute aggregate start is not minute-aligned")
        return start_at

    @classmethod
    def _minute_bar(
        cls, symbol: str, raw: Mapping[str, Any], *, stream: bool
    ) -> CompletedBar:
        start_at = cls._minute_start(raw, stream=stream)
        end_at = start_at + timedelta(minutes=1)
        end_raw = raw.get("e")
        if stream and end_raw is None:
            raise MassiveStoreError("stream minute aggregate end is missing")
        if end_raw is not None:
            provider_end = _provider_time(end_raw, "aggregate end")
            if provider_end < start_at or provider_end > end_at + timedelta(seconds=1):
                raise MassiveStoreError("minute aggregate end is outside its minute")
            if stream and provider_end < end_at - timedelta(seconds=1):
                raise MassiveStoreError("stream minute aggregate is only partial")
        sequence = int(start_at.timestamp()) // 60
        kind = "AM" if stream else "REST_AM"
        return CompletedBar.build(
            symbol=symbol,
            start_at=start_at,
            end_at=end_at,
            open=raw.get("o"),
            high=raw.get("h"),
            low=raw.get("l"),
            close=raw.get("c"),
            volume=max(0, int(raw.get("v"))),
            sequence=sequence,
            revision=max(0, int(raw.get("revision", 0))),
            source_event_id=(
                f"massive:{kind}:{symbol}:"
                f"{int(start_at.timestamp() * 1000)}:{int(raw.get('revision', 0))}"
            ),
        )


def _aware(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _parse_time(value: object, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise MassiveStoreError(f"{field} is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise MassiveStoreError(f"{field} is not timezone-aware")
    return parsed.astimezone(timezone.utc)


def _event_time(milliseconds: object, field: str) -> datetime:
    if isinstance(milliseconds, bool):
        raise MassiveStoreError(f"{field} is invalid")
    try:
        value = int(milliseconds)
    except (TypeError, ValueError) as exc:
        raise MassiveStoreError(f"{field} is invalid") from exc
    if value <= 0:
        raise MassiveStoreError(f"{field} is invalid")
    return datetime.fromtimestamp(value / 1000, timezone.utc)


def _payload_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class LocalMassiveReadOnlySource:
    """Compatibility reader for the existing local Titan Massive database."""

    REQUIRED_COLUMNS: Mapping[str, frozenset[str]] = {
        "health": frozenset({"component", "status", "checked_at", "details_json"}),
        "quotes": frozenset(
            {
                "symbol",
                "timestamp_ms",
                "bid",
                "ask",
                "bid_size",
                "ask_size",
                "received_at",
            }
        ),
        "bars_1m": frozenset(
            {
                "symbol",
                "start_ms",
                "end_ms",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "accumulated_volume",
                "received_at",
            }
        ),
        "prepared_trade_plans": frozenset(
            {
                "plan_id",
                "symbol",
                "observed_at",
                "status",
                "direction",
                "lane",
                "weighted_opportunity_score",
                "trigger",
                "structural_stop",
                "t1",
                "t2",
                "payload_json",
                "pilot_id",
                "book_mode",
                "decision_contract_hash",
            }
        ),
    }

    def __init__(
        self,
        database_path: str | Path,
        *,
        pilot_id: str,
        book_mode: str,
        decision_contract_hash: str,
        health_max_age_seconds: int,
        candidate_max_age_seconds: int,
        session_state: Callable[[datetime], MarketSessionState] | None = None,
    ) -> None:
        path = Path(database_path).expanduser()
        if not path.is_absolute():
            raise ValueError("Massive database path must be absolute")
        if not pilot_id or book_mode != "SHADOW":
            raise ValueError("Massive source identity must be an explicit SHADOW producer")
        if len(decision_contract_hash) != 64 or any(
            item not in "0123456789abcdef" for item in decision_contract_hash
        ):
            raise ValueError("Massive decision contract hash must be lowercase SHA-256")
        if health_max_age_seconds <= 0 or candidate_max_age_seconds <= 0:
            raise ValueError("Massive freshness limits must be positive")
        self.database_path = path
        self.pilot_id = pilot_id
        self.book_mode = book_mode
        self.decision_contract_hash = decision_contract_hash
        self.health_max_age_seconds = health_max_age_seconds
        self.candidate_max_age_seconds = candidate_max_age_seconds
        self.session_state = session_state or (
            lambda _now: MarketSessionState.ENTRY_ELIGIBLE
        )

    def _connect(self) -> sqlite3.Connection:
        if not self.database_path.is_file():
            raise MassiveStoreError("local Massive database is missing")
        uri = f"file:{url_quote(str(self.database_path), safe='/')}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=1.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=1000")
        try:
            self._validate_schema(connection)
        except BaseException:
            connection.close()
            raise
        return connection

    def _validate_schema(self, connection: sqlite3.Connection) -> None:
        for table, required in self.REQUIRED_COLUMNS.items():
            rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
            columns = {str(row[1]) for row in rows}
            missing = sorted(required - columns)
            if missing:
                raise MassiveStoreError(
                    f"local Massive schema is incompatible: {table} missing {missing}"
                )

    def health(self, *, now: datetime) -> MassiveFeedHealth:
        current = _aware(now, "now")
        blockers: list[str] = []
        entry_blockers: list[str] = []
        states: dict[str, str] = {}
        latest_quote: datetime | None = None
        latest_bar: datetime | None = None
        try:
            session = self.session_state(current)
            if not isinstance(session, MarketSessionState):
                session = MarketSessionState(str(session))
        except Exception as exc:
            session = MarketSessionState.WAITING_FOR_SESSION
            blockers.append(f"SESSION_STATE_UNAVAILABLE:{type(exc).__name__}")
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    "SELECT component,status,checked_at FROM health "
                    "WHERE component IN ('massive_websocket','market_data_freshness')"
                ).fetchall()
                for row in rows:
                    component = str(row["component"])
                    status = str(row["status"])
                    states[component] = status
                    checked = _parse_time(row["checked_at"], f"health.{component}")
                    age = (current - checked).total_seconds()
                    if (
                        session is MarketSessionState.ENTRY_ELIGIBLE
                        and (age < -1 or age > self.health_max_age_seconds)
                    ):
                        entry_blockers.append(f"MASSIVE_HEALTH_STALE:{component}")
                    if status not in {
                        "healthy",
                        "idle",
                        "closed",
                        "waiting_for_session",
                    }:
                        blockers.append(f"MASSIVE_COMPONENT_UNHEALTHY:{component}:{status}")
                for required in ("massive_websocket", "market_data_freshness"):
                    if required not in states:
                        blockers.append(f"MASSIVE_HEALTH_MISSING:{required}")
                quote_row = connection.execute(
                    "SELECT MAX(timestamp_ms) AS latest FROM quotes"
                ).fetchone()
                bar_row = connection.execute(
                    "SELECT end_ms AS latest FROM bars_1m ORDER BY start_ms DESC LIMIT 1"
                ).fetchone()
                if quote_row is not None and quote_row["latest"] is not None:
                    latest_quote = _event_time(quote_row["latest"], "latest quote")
                if bar_row is not None and bar_row["latest"] is not None:
                    latest_bar = _event_time(bar_row["latest"], "latest completed bar")
        except (MassiveStoreError, sqlite3.Error, OSError) as exc:
            blockers.append(_store_failure_code("MASSIVE_STORE_UNAVAILABLE", exc))
        for name, value, max_age in (
            ("QUOTE", latest_quote, self.health_max_age_seconds),
            ("COMPLETED_BAR", latest_bar, self.candidate_max_age_seconds),
        ):
            if session is MarketSessionState.ENTRY_ELIGIBLE:
                if value is None:
                    entry_blockers.append(f"MASSIVE_{name}_MISSING")
                else:
                    age = (current - value).total_seconds()
                    if age < -1:
                        entry_blockers.append(f"MASSIVE_{name}_FUTURE_DATED")
                    elif age > max_age:
                        entry_blockers.append(f"MASSIVE_{name}_STALE")
        if session is MarketSessionState.WAITING_FOR_SESSION:
            entry_blockers = ["WAITING_FOR_SESSION"]
        service_healthy = not blockers
        entry_ready = (
            service_healthy
            and session is MarketSessionState.ENTRY_ELIGIBLE
            and not entry_blockers
        )
        return MassiveFeedHealth(
            checked_at=current,
            database_path=str(self.database_path),
            producer_fresh=entry_ready,
            latest_quote_at=latest_quote,
            latest_completed_bar_at=latest_bar,
            component_states=states,
            blockers=tuple(
                dict.fromkeys(
                    blockers
                    + (
                        entry_blockers
                        if session is MarketSessionState.ENTRY_ELIGIBLE
                        else []
                    )
                )
            ),
            service_healthy=service_healthy,
            session_state=session,
            entry_evidence_ready=entry_ready,
            entry_blockers=tuple(dict.fromkeys(entry_blockers)),
        )

    def prepared_structures(
        self, *, now: datetime, limit: int
    ) -> tuple[PreparedStructure, ...]:
        current = _aware(now, "now")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("candidate limit must be positive")
        cutoff = current - timedelta(seconds=self.candidate_max_age_seconds)
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    """SELECT plan_id,symbol,observed_at,status,direction,lane,
                              weighted_opportunity_score,trigger,structural_stop,t1,t2,
                              payload_json,pilot_id,book_mode,decision_contract_hash
                       FROM prepared_trade_plans
                       WHERE observed_at>=? AND observed_at<=?
                         AND status='PRELIMINARY' AND direction='UP'
                         AND lane='regular_equity' AND pilot_id=? AND book_mode=?
                         AND decision_contract_hash=?
                       ORDER BY observed_at DESC,weighted_opportunity_score DESC,symbol ASC
                       LIMIT ?""",
                    (
                        cutoff.isoformat(),
                        current.isoformat(),
                        self.pilot_id,
                        self.book_mode,
                        self.decision_contract_hash,
                        limit,
                    ),
                ).fetchall()
        except sqlite3.Error as exc:
            raise MassiveStoreError(
                _store_failure_code("MASSIVE_PREPARED_STRUCTURES_READ_FAILED", exc)
            ) from exc
        structures: list[PreparedStructure] = []
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"]))
            except json.JSONDecodeError as exc:
                raise MassiveStoreError("prepared structure payload is invalid JSON") from exc
            if not isinstance(payload, dict):
                raise MassiveStoreError("prepared structure payload is not an object")
            observed = _parse_time(row["observed_at"], "prepared observed_at")
            if (
                payload.get("trade_authority") is not False
                or payload.get("broker_authority") is not False
                or payload.get("pilot_id") != self.pilot_id
                or payload.get("book_mode") != self.book_mode
                or payload.get("decision_contract_hash") != self.decision_contract_hash
                or payload.get("symbol") != row["symbol"]
                or payload.get("observed_at") != row["observed_at"]
            ):
                raise MassiveStoreError("prepared structure identity/provenance mismatch")
            entry = decimal_value(payload.get("review_limit_ceiling"), "review_limit_ceiling")
            stop = decimal_value(row["structural_stop"], "structural_stop")
            targets = tuple(
                decimal_value(row[name], name)
                for name in ("t1", "t2")
                if row[name] is not None
            )
            if entry <= Decimal("5") or stop <= 0 or stop >= entry:
                continue
            if not targets or any(target <= entry for target in targets):
                continue
            setup = str(payload.get("setup", "")).strip()
            if not setup:
                continue
            structures.append(
                PreparedStructure(
                    source_plan_id=str(row["plan_id"]),
                    symbol=str(row["symbol"]),
                    observed_at=observed,
                    setup_id=setup,
                    ranking_score=decimal_value(
                        row["weighted_opportunity_score"], "ranking_score"
                    ),
                    entry_limit=entry,
                    structural_stop=stop,
                    targets=targets,
                    payload_hash=_payload_hash(payload),
                    payload=payload,
                )
            )
        return tuple(structures)

    def hydrate_cache(
        self,
        cache: MarketDataCache,
        *,
        structures: Sequence[PreparedStructure],
        session_start: datetime,
        now: datetime,
        tradability: TradabilityProvider,
    ) -> tuple[str, ...]:
        current = _aware(now, "now")
        start = _aware(session_start, "session_start")
        if start >= current:
            raise ValueError("session_start must precede now")
        symbols = tuple(dict.fromkeys(item.symbol for item in structures))
        failures: list[str] = []
        if not symbols:
            return ()
        try:
            with closing(self._connect()) as connection:
                for symbol in symbols:
                    quote_row = connection.execute(
                        "SELECT * FROM quotes WHERE symbol=?", (symbol,)
                    ).fetchone()
                    if quote_row is None:
                        failures.append(f"QUOTE_MISSING:{symbol}")
                    else:
                        try:
                            event_at = _event_time(
                                quote_row["timestamp_ms"], f"quote timestamp {symbol}"
                            )
                            received_at = _parse_time(
                                quote_row["received_at"], f"quote received_at {symbol}"
                            )
                            bid_size, ask_size, size_version = _massive_quote_sizes(
                                quote_row["bid_size"],
                                quote_row["ask_size"],
                                venue_at=event_at,
                            )
                            cache.record_quote(
                                Quote.build(
                                    symbol=symbol,
                                    bid=quote_row["bid"],
                                    ask=quote_row["ask"],
                                    bid_size=bid_size,
                                    ask_size=ask_size,
                                    venue_bid_at=event_at,
                                    venue_ask_at=event_at,
                                    observed_at=received_at,
                                    source="local_titan_massive_sqlite+robinhood_tradability",
                                    tradable=tradability.is_tradable(symbol, as_of=current),
                                    size_unit="shares",
                                    size_source_version=size_version,
                                    depth_scope="top_of_book",
                                )
                            )
                        except (TypeError, ValueError, MassiveStoreError) as exc:
                            failures.append(f"QUOTE_INVALID:{symbol}:{type(exc).__name__}")
                    rows = connection.execute(
                        """SELECT * FROM bars_1m
                           WHERE symbol=? AND end_ms>=? AND end_ms<=?
                           ORDER BY end_ms ASC""",
                        (
                            symbol,
                            int(start.timestamp() * 1000),
                            int(current.timestamp() * 1000),
                        ),
                    ).fetchall()
                    if not rows:
                        failures.append(f"COMPLETED_BAR_MISSING:{symbol}")
                        continue
                    complete = True
                    last_sequence = 0
                    for row in rows:
                        try:
                            received_at = _parse_time(
                                row["received_at"], f"bar received_at {symbol}"
                            )
                            start_at = _event_time(row["start_ms"], "bar start")
                            end_at = _event_time(row["end_ms"], "bar end")
                            if (
                                start_at.second != 0
                                or start_at.microsecond != 0
                                or end_at - start_at != timedelta(minutes=1)
                            ):
                                raise MassiveStoreError(
                                    "local completed bar is not one aligned minute"
                                )
                            sequence = int(row["end_ms"]) // 60_000
                            revision = int(received_at.timestamp() * 1_000_000)
                            result = cache.record_completed_bar(
                                CompletedBar.build(
                                    symbol=symbol,
                                    start_at=start_at,
                                    end_at=end_at,
                                    open=row["open"],
                                    high=row["high"],
                                    low=row["low"],
                                    close=row["close"],
                                    volume=max(0, int(row["volume"])),
                                    sequence=sequence,
                                    revision=revision,
                                    source_event_id=(
                                        f"massive:SQLITE_1M:{symbol}:"
                                        f"{int(row['start_ms'])}:{revision}"
                                    ),
                                ),
                                received_at=received_at,
                            )
                            if result == "stale_revision":
                                complete = False
                            last_sequence = sequence
                        except (TypeError, ValueError, MassiveStoreError):
                            complete = False
                            failures.append(f"COMPLETED_BAR_INVALID:{symbol}")
                            break
                    if complete and last_sequence:
                        cache.record_snapshot_resync(symbol, sequence=last_sequence)
        except sqlite3.Error as exc:
            raise MassiveStoreError(
                _store_failure_code("MASSIVE_MARKET_CACHE_HYDRATION_FAILED", exc)
            ) from exc
        cache.set_active_scores((item.symbol, item.ranking_score) for item in structures)
        return tuple(dict.fromkeys(failures))


__all__ = [
    "LocalMassiveReadOnlySource",
    "MassiveAuthorizationEvidence",
    "MassiveFeedHealth",
    "MassiveHotPathMetrics",
    "MassiveRequestAuthorizer",
    "MassiveRestStreamSource",
    "MassiveRestTransport",
    "MassiveStoreError",
    "MassiveStreamStatus",
    "MassiveStreamTransport",
    "MassiveSymbolEvidenceSnapshot",
    "MassiveSymbolReadiness",
    "PreparedCandidateSource",
    "PreparedStructure",
    "TradabilityProvider",
    "UnavailableTradabilityProvider",
    "UrllibMassiveRestTransport",
]
