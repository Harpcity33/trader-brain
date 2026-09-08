"""Massive market-data adapters with injected authorization and provenance.

The local SQLite bridge remains a compatibility source.  The production
REST/stream composition below consumes only an already-authorized transport;
it never discovers, reads, copies, or logs credentials.  Shadow plans are
discovery hints, never execution authority, and Robinhood instrument
eligibility always comes from an independent injected provider.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import sqlite3
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

    def drain(
        self, *, limit: int, timeout_seconds: float
    ) -> Sequence[Mapping[str, Any]]: ...


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


class MassiveRestStreamSource:
    """Production market-evidence source over injected Massive transports.

    Candidate geometry remains a separately injected, non-authoritative input.
    Quotes are NBBO/top-of-book only; their sizes are never represented as
    full Level 2 depth.  Robinhood tradability is obtained independently via
    the ``tradability`` argument to :meth:`hydrate_cache`.
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
        self.candidates = candidates
        self.rest = rest
        self.stream = stream
        self.session_state = session_state
        self.health_max_age_seconds = int(health_max_age_seconds)
        self.candidate_max_age_seconds = int(candidate_max_age_seconds)
        self.request_timeout_seconds = float(request_timeout_seconds)
        self.stream_drain_timeout_seconds = float(stream_drain_timeout_seconds)
        self.stream_batch_limit = int(stream_batch_limit)

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
                ("status", "drain"),
            ),
            (
                "prepared_candidate_source",
                self.candidates,
                ("prepared_structures",),
            ),
            ("market_session_state", self.session_state, ("__call__",)),
        )

    def health(self, *, now: datetime) -> MassiveFeedHealth:
        current = _aware(now, "now")
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
        try:
            response = self.rest.get_json(
                "/v1/marketstatus/now",
                parameters={},
                timeout_seconds=self.request_timeout_seconds,
            )
            states["massive_rest"] = str(response.get("status", "reachable"))
        except Exception as exc:
            health_blockers.append(f"MASSIVE_REST_UNAVAILABLE:{type(exc).__name__}")
            states["massive_rest"] = "unavailable"
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
        symbols = tuple(dict.fromkeys(item.symbol.strip().upper() for item in structures))
        failures: list[str] = []
        for symbol in symbols:
            eligible = False
            try:
                eligible = tradability.is_tradable(symbol, as_of=current)
                quote_payload = self.rest.get_json(
                    f"/v3/quotes/{url_quote(symbol, safe='')}",
                    parameters={"limit": "1", "order": "desc", "sort": "timestamp"},
                    timeout_seconds=self.request_timeout_seconds,
                )
                quote_results = quote_payload.get("results")
                if not isinstance(quote_results, list) or not quote_results:
                    raise MassiveStoreError("Massive quote result is missing")
                self._record_quote(
                    cache,
                    symbol=symbol,
                    raw=quote_results[0],
                    received_at=current,
                    tradable=eligible,
                    source="massive_rest_nbbo_top_of_book+robinhood_instrument",
                )
            except Exception as exc:
                failures.append(f"QUOTE_INVALID:{symbol}:{type(exc).__name__}")
            try:
                bars_payload = self.rest.get_json(
                    f"/v2/aggs/ticker/{url_quote(symbol, safe='')}/range/1/minute/"
                    f"{start.date().isoformat()}/{current.date().isoformat()}",
                    parameters={"adjusted": "true", "limit": "50000", "sort": "asc"},
                    timeout_seconds=self.request_timeout_seconds,
                )
                results = bars_payload.get("results")
                if not isinstance(results, list) or not results:
                    raise MassiveStoreError("Massive aggregate result is missing")
                last_sequence = 0
                for raw in results:
                    if not isinstance(raw, Mapping):
                        raise MassiveStoreError("Massive aggregate row is invalid")
                    bar = self._bar(symbol, raw)
                    if bar.start_at < start or bar.end_at > current:
                        continue
                    cache.record_completed_bar(bar, received_at=current)
                    last_sequence = max(last_sequence, bar.sequence)
                if not last_sequence:
                    raise MassiveStoreError("no completed in-session aggregate is available")
                cache.record_snapshot_resync(symbol, sequence=last_sequence)
            except Exception as exc:
                failures.append(f"COMPLETED_BAR_INVALID:{symbol}:{type(exc).__name__}")
        try:
            for event in self.stream.drain(
                limit=self.stream_batch_limit,
                timeout_seconds=self.stream_drain_timeout_seconds,
            ):
                if not isinstance(event, Mapping):
                    raise MassiveStoreError("Massive stream event is invalid")
                symbol = str(event.get("sym", "")).strip().upper()
                if symbol not in symbols:
                    continue
                eligible = tradability.is_tradable(symbol, as_of=current)
                kind = str(event.get("ev", "")).upper()
                if kind == "Q":
                    self._record_quote(
                        cache,
                        symbol=symbol,
                        raw=event,
                        received_at=current,
                        tradable=eligible,
                        source="massive_stream_nbbo_top_of_book+robinhood_instrument",
                    )
                elif kind in {"A", "AM"}:
                    bar = self._bar(symbol, event)
                    if bar.end_at <= current:
                        cache.record_completed_bar(bar, received_at=current)
        except Exception as exc:
            failures.append(f"MASSIVE_STREAM_DRAIN_FAILED:{type(exc).__name__}")
        cache.set_active_scores((item.symbol, item.ranking_score) for item in structures)
        return tuple(dict.fromkeys(failures))

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
        cache.record_quote(
            Quote.build(
                symbol=symbol,
                bid=raw.get("bid_price", raw.get("bp")),
                ask=raw.get("ask_price", raw.get("ap")),
                bid_size=int(raw.get("bid_size", raw.get("bs"))),
                ask_size=int(raw.get("ask_size", raw.get("as"))),
                venue_bid_at=venue,
                venue_ask_at=venue,
                observed_at=received_at,
                source=source,
                tradable=tradable,
            )
        )

    @staticmethod
    def _bar(symbol: str, raw: Mapping[str, Any]) -> CompletedBar:
        start_at = _provider_time(raw.get("t", raw.get("s")), "aggregate start")
        end_raw = raw.get("e")
        end_at = (
            _provider_time(end_raw, "aggregate end")
            if end_raw is not None
            else start_at + timedelta(minutes=1)
        )
        sequence = int(start_at.timestamp()) // 60
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
                f"massive:{str(raw.get('ev', 'A')).upper()}:{symbol}:"
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
                            cache.record_quote(
                                Quote.build(
                                    symbol=symbol,
                                    bid=quote_row["bid"],
                                    ask=quote_row["ask"],
                                    bid_size=int(quote_row["bid_size"]),
                                    ask_size=int(quote_row["ask_size"]),
                                    venue_bid_at=event_at,
                                    venue_ask_at=event_at,
                                    observed_at=received_at,
                                    source="local_titan_massive_sqlite+robinhood_tradability",
                                    tradable=tradability.is_tradable(symbol, as_of=current),
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
                                        f"massive:A:{symbol}:{int(row['start_ms'])}:{revision}"
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
    "MassiveRequestAuthorizer",
    "MassiveRestStreamSource",
    "MassiveRestTransport",
    "MassiveStoreError",
    "MassiveStreamStatus",
    "MassiveStreamTransport",
    "PreparedCandidateSource",
    "PreparedStructure",
    "TradabilityProvider",
    "UnavailableTradabilityProvider",
    "UrllibMassiveRestTransport",
]
