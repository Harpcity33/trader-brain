"""Read-only bridge to the already-installed Titan Massive market-data store.

The full-live runtime does not duplicate or alter the existing Massive watcher.
It consumes only completed, provenance-bearing records through a query-only
SQLite connection.  Shadow plans are discovery inputs, never execution
authority; the live pipeline must independently revalidate every field.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import quote as url_quote

from .market_data import CompletedBar, MarketDataCache, Quote
from .money import decimal_value


class MassiveStoreError(RuntimeError):
    """The local provider store cannot prove current, compatible evidence."""


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
        states: dict[str, str] = {}
        latest_quote: datetime | None = None
        latest_bar: datetime | None = None
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
                    if age < -1 or age > self.health_max_age_seconds:
                        blockers.append(f"MASSIVE_HEALTH_STALE:{component}")
                    if status != "healthy":
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
            blockers.append(f"MASSIVE_STORE_UNAVAILABLE:{type(exc).__name__}:{exc}")
        for name, value, max_age in (
            ("QUOTE", latest_quote, self.health_max_age_seconds),
            ("COMPLETED_BAR", latest_bar, self.candidate_max_age_seconds),
        ):
            if value is None:
                blockers.append(f"MASSIVE_{name}_MISSING")
            else:
                age = (current - value).total_seconds()
                if age < -1 or age > max_age:
                    blockers.append(f"MASSIVE_{name}_STALE")
        return MassiveFeedHealth(
            checked_at=current,
            database_path=str(self.database_path),
            producer_fresh=not blockers,
            latest_quote_at=latest_quote,
            latest_completed_bar_at=latest_bar,
            component_states=states,
            blockers=tuple(dict.fromkeys(blockers)),
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
            raise MassiveStoreError(f"cannot read prepared structures: {exc}") from exc
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
            raise MassiveStoreError(f"cannot hydrate market cache: {exc}") from exc
        cache.set_active_scores((item.symbol, item.ranking_score) for item in structures)
        return tuple(dict.fromkeys(failures))


__all__ = [
    "LocalMassiveReadOnlySource",
    "MassiveFeedHealth",
    "MassiveStoreError",
    "PreparedStructure",
    "TradabilityProvider",
    "UnavailableTradabilityProvider",
]
