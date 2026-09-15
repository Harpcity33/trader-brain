"""Incremental market-data cache with completed-bar and continuity gates."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
import hashlib
import json
from threading import RLock
from typing import Any, Iterable

from .money import decimal_value, whole_shares


class MarketSessionState(str, Enum):
    """Entry-evidence state, deliberately separate from service health."""

    ENTRY_ELIGIBLE = "ENTRY_ELIGIBLE"
    WAITING_FOR_SESSION = "WAITING_FOR_SESSION"


def _aware(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value


def _symbol(value: str) -> str:
    result = str(value).strip().upper()
    if not result or not result.replace(".", "").replace("-", "").isalnum():
        raise ValueError("invalid symbol")
    return result


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: Decimal
    ask: Decimal
    bid_size: int
    ask_size: int
    venue_bid_at: datetime
    venue_ask_at: datetime
    observed_at: datetime
    source: str
    tradable: bool
    halted: bool = False
    size_unit: str = "shares"
    size_source_version: str = "provider_unspecified"
    depth_scope: str = "top_of_book"

    @classmethod
    def build(
        cls,
        *,
        symbol: str,
        bid: Any,
        ask: Any,
        bid_size: int,
        ask_size: int,
        venue_bid_at: datetime,
        venue_ask_at: datetime,
        observed_at: datetime,
        source: str,
        tradable: bool,
        halted: bool = False,
        size_unit: str = "shares",
        size_source_version: str = "provider_unspecified",
        depth_scope: str = "top_of_book",
    ) -> "Quote":
        bid_value = decimal_value(bid, "bid")
        ask_value = decimal_value(ask, "ask")
        if bid_value <= 0 or ask_value <= 0:
            raise ValueError("quote prices must be positive")
        if bid_value > ask_value:
            raise ValueError("crossed quote")
        for field, value in (("bid_size", bid_size), ("ask_size", ask_size)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field} must be a non-negative integer")
        observed = _aware(observed_at, "observed_at")
        venue_bid = _aware(venue_bid_at, "venue_bid_at")
        venue_ask = _aware(venue_ask_at, "venue_ask_at")
        if venue_bid > observed + timedelta(seconds=1) or venue_ask > observed + timedelta(seconds=1):
            raise ValueError("future-dated quote")
        if not str(source).strip():
            raise ValueError("quote source is required")
        if str(size_unit) != "shares":
            raise ValueError("quote sizes must be expressed in shares")
        if not str(size_source_version).strip():
            raise ValueError("quote size source/version is required")
        if str(depth_scope) != "top_of_book":
            raise ValueError("quote depth_scope must be top_of_book")
        return cls(
            symbol=_symbol(symbol),
            bid=bid_value,
            ask=ask_value,
            bid_size=bid_size,
            ask_size=ask_size,
            venue_bid_at=venue_bid,
            venue_ask_at=venue_ask,
            observed_at=observed,
            source=str(source),
            tradable=tradable is True,
            halted=halted is True,
            size_unit="shares",
            size_source_version=str(size_source_version),
            depth_scope="top_of_book",
        )

    @property
    def midpoint(self) -> Decimal:
        return (self.bid + self.ask) / Decimal("2")

    @property
    def spread_bps(self) -> Decimal:
        return ((self.ask - self.bid) / self.midpoint) * Decimal("10000")

    @property
    def newest_venue_at(self) -> datetime:
        return max(self.venue_bid_at, self.venue_ask_at)

    @property
    def oldest_venue_at(self) -> datetime:
        """Both sides must be fresh before their prices/sizes gate an entry."""
        return min(self.venue_bid_at, self.venue_ask_at)


@dataclass(frozen=True)
class CompletedBar:
    symbol: str
    start_at: datetime
    end_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    sequence: int
    revision: int = 0
    source_event_id: str = ""

    @classmethod
    def build(cls, **raw: Any) -> "CompletedBar":
        start = _aware(raw["start_at"], "start_at")
        end = _aware(raw["end_at"], "end_at")
        if end <= start:
            raise ValueError("bar end must be after start")
        values = {
            field: decimal_value(raw[field], field)
            for field in ("open", "high", "low", "close")
        }
        if any(value <= 0 for value in values.values()):
            raise ValueError("bar prices must be positive")
        if values["high"] < max(values["open"], values["close"], values["low"]):
            raise ValueError("bar high is inconsistent")
        if values["low"] > min(values["open"], values["close"], values["high"]):
            raise ValueError("bar low is inconsistent")
        volume = raw["volume"]
        sequence = raw["sequence"]
        revision = raw.get("revision", 0)
        for field, value in (("volume", volume), ("sequence", sequence), ("revision", revision)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field} must be a non-negative integer")
        return cls(
            symbol=_symbol(raw["symbol"]),
            start_at=start,
            end_at=end,
            volume=volume,
            sequence=sequence,
            revision=revision,
            source_event_id=str(raw.get("source_event_id", "")),
            **values,
        )

    @property
    def digest(self) -> str:
        return _hash(
            {
                "symbol": self.symbol,
                "start_at": self.start_at.isoformat(),
                "end_at": self.end_at.isoformat(),
                "open": str(self.open),
                "high": str(self.high),
                "low": str(self.low),
                "close": str(self.close),
                "volume": self.volume,
                "sequence": self.sequence,
                "revision": self.revision,
                "source_event_id": self.source_event_id,
            }
        )


@dataclass(frozen=True)
class EvidenceDecision:
    eligible: bool
    failures: tuple[str, ...]
    quote: Quote | None
    causal_bar: CompletedBar | None
    session_volume: int
    quote_age_seconds: float | None
    spread_bps: Decimal | None


class MarketDataCache:
    """Monotonic in-memory hot path backed by source watermarks.

    A sequence gap poisons entry freshness until a complete snapshot resync is
    explicitly recorded.  Broker reconciliation is intentionally outside this
    cache and remains available during market-data loss.
    """

    def __init__(self, *, max_active: int = 64):
        if max_active <= 0:
            raise ValueError("max_active must be positive")
        self.max_active = max_active
        self.quotes: dict[str, Quote] = {}
        self.bars: dict[str, dict[datetime, CompletedBar]] = {}
        self.watermarks: dict[str, int] = {}
        self.degraded: dict[str, str] = {}
        self.active_scores: dict[str, Decimal] = {}
        self._missing_sequences: dict[str, set[int]] = {}
        self._lock = RLock()
        self._session_start: datetime | None = None

    def begin_session(self, session_start: datetime) -> bool:
        """Atomically rotate volatile evidence at an exact session boundary."""

        start = _aware(session_start, "session_start")
        with self._lock:
            if self._session_start is None:
                self._session_start = start
                return False
            if self._session_start == start:
                return False
            self.quotes.clear()
            self.bars.clear()
            self.watermarks.clear()
            self.degraded.clear()
            self.active_scores.clear()
            self._missing_sequences.clear()
            self._session_start = start
            return True

    def record_quote(self, quote: Quote) -> bool:
        with self._lock:
            prior = self.quotes.get(quote.symbol)
            # Venue time, not local response order, decides which executable
            # quote is newer.  In particular a slow REST response must never
            # overwrite a quote already consumed from the live stream.
            if prior is not None and quote.newest_venue_at < prior.newest_venue_at:
                return False
            if prior is not None and quote.newest_venue_at == prior.newest_venue_at:
                if quote.observed_at < prior.observed_at:
                    return False
                # Two sources can be sampled in one local clock tick. Source labels
                # do not turn identical executable market facts into a conflict.
                comparable = (
                    "bid",
                    "ask",
                    "bid_size",
                    "ask_size",
                    "venue_bid_at",
                    "venue_ask_at",
                    "tradable",
                    "halted",
                    "size_unit",
                    "size_source_version",
                    "depth_scope",
                )
                if all(
                    getattr(quote, field) == getattr(prior, field)
                    for field in comparable
                ):
                    return False
                if quote.observed_at == prior.observed_at:
                    raise ValueError("conflicting quote at the same observation time")
            self.quotes[quote.symbol] = quote
            return True

    def record_completed_bar(self, bar: CompletedBar, *, received_at: datetime) -> str:
        received = _aware(received_at, "received_at")
        if bar.end_at > received:
            raise ValueError("incomplete or future bar")
        with self._lock:
            symbol_bars = self.bars.setdefault(bar.symbol, {})
            prior = symbol_bars.get(bar.end_at)
            if prior is not None:
                if bar.revision < prior.revision:
                    return "stale_revision"
                if bar.revision == prior.revision:
                    if bar.digest == prior.digest:
                        return "duplicate"
                    raise ValueError("conflicting bar without a higher revision")
                symbol_bars[bar.end_at] = bar
                self.degraded[bar.symbol] = (
                    "CORRECTED_COMPLETED_BAR_REQUIRES_PLAN_REVALIDATION"
                )
                return "corrected"
            previous_sequence = self.watermarks.get(bar.symbol)
            missing = self._missing_sequences.setdefault(bar.symbol, set())
            if previous_sequence is not None and bar.sequence > previous_sequence + 1:
                # A regular US session contains fewer than 1,000 minutes.  A
                # larger jump is corrupt input rather than a safe set to allocate.
                if bar.sequence - previous_sequence > 2_000:
                    self.degraded[bar.symbol] = "MARKET_DATA_SEQUENCE_GAP_UNBOUNDED"
                else:
                    missing.update(range(previous_sequence + 1, bar.sequence))
            missing.discard(bar.sequence)
            self.watermarks[bar.symbol] = max(
                bar.sequence, previous_sequence if previous_sequence is not None else bar.sequence
            )
            symbol_bars[bar.end_at] = bar
            if missing:
                self.degraded[bar.symbol] = "MARKET_DATA_SEQUENCE_GAP"
            elif self.degraded.get(bar.symbol) == "MARKET_DATA_SEQUENCE_GAP":
                self.degraded.pop(bar.symbol, None)
            return "inserted"

    def mark_disconnect(self, source: str) -> None:
        reason = f"MARKET_DATA_DISCONNECTED:{source}"
        with self._lock:
            for symbol in set(self.quotes) | set(self.bars):
                self.degraded[symbol] = reason

    def mark_symbol_degraded(self, symbol: str, reason: str) -> None:
        normalized = _symbol(symbol)
        if not str(reason).strip():
            raise ValueError("degradation reason is required")
        with self._lock:
            self.degraded[normalized] = str(reason)

    def clear_symbol_degradation(
        self, symbol: str, *, expected_reason: str
    ) -> bool:
        """Clear only the exact transient state a caller actually repaired.

        A concurrent disconnect, correction, or invalid stream event must not
        be erased by completion of an older REST backfill.
        """

        normalized = _symbol(symbol)
        expected = str(expected_reason)
        if not expected.strip():
            raise ValueError("expected degradation reason is required")
        with self._lock:
            if self.degraded.get(normalized) != expected:
                return False
            self.degraded.pop(normalized, None)
            return True

    def record_snapshot_resync(self, symbol: str, *, sequence: int) -> None:
        normalized = _symbol(symbol)
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise ValueError("resync sequence must be a non-negative integer")
        with self._lock:
            self.watermarks[normalized] = max(
                sequence, self.watermarks.get(normalized, sequence)
            )
            self._missing_sequences.pop(normalized, None)
            self.degraded.pop(normalized, None)

    def missing_sequence_range(self, symbol: str) -> tuple[int, int] | None:
        """Return the exact inclusive sequence gap currently blocking a symbol."""

        normalized = _symbol(symbol)
        with self._lock:
            missing = self._missing_sequences.get(normalized, set())
            if not missing:
                return None
            return min(missing), max(missing)

    def quote_for(self, symbol: str) -> Quote | None:
        with self._lock:
            return self.quotes.get(_symbol(symbol))

    def update_quote_tradability(self, symbol: str, *, tradable: bool) -> None:
        """Join independent eligibility without changing executable quote times."""

        if not isinstance(tradable, bool):
            raise ValueError("tradability must be boolean")
        normalized = _symbol(symbol)
        with self._lock:
            quote = self.quotes.get(normalized)
            if quote is not None and quote.tradable != tradable:
                self.quotes[normalized] = replace(quote, tradable=tradable)

    def bar_for(self, symbol: str, end_at: datetime) -> CompletedBar | None:
        normalized = _symbol(symbol)
        completed_at = _aware(end_at, "end_at")
        with self._lock:
            return self.bars.get(normalized, {}).get(completed_at)

    def degradation_for(self, symbol: str) -> str | None:
        with self._lock:
            return self.degraded.get(_symbol(symbol))

    def quote_prices(self) -> dict[str, Decimal]:
        with self._lock:
            return {
                symbol: quote.ask
                for symbol, quote in self.quotes.items()
                if quote.ask > 0
            }

    def symbol_state(
        self, symbol: str
    ) -> tuple[Quote | None, tuple[CompletedBar, ...], int | None, str | None]:
        """Copy one symbol's facts atomically for cross-thread inspection."""

        normalized = _symbol(symbol)
        with self._lock:
            return (
                self.quotes.get(normalized),
                tuple(self.bars.get(normalized, {}).values()),
                self.watermarks.get(normalized),
                self.degraded.get(normalized),
            )

    def set_active_scores(self, candidates: Iterable[tuple[str, Any]]) -> tuple[str, ...]:
        normalized: dict[str, Decimal] = {}
        for symbol, score in candidates:
            normalized[_symbol(symbol)] = decimal_value(score, "candidate_score")
        ranked = sorted(normalized, key=lambda key: (-normalized[key], key))[: self.max_active]
        with self._lock:
            self.active_scores = {key: normalized[key] for key in ranked}
        return tuple(ranked)

    def validate_entry_evidence(
        self,
        *,
        symbol: str,
        now: datetime,
        plan_created_at: datetime,
        plan_expires_at: datetime,
        causal_bar_end: datetime,
        quote_max_age_seconds: int,
        completed_bar_max_age_seconds: int,
        minimum_session_volume: int,
        max_spread_bps: Any | None,
        minimum_depth_multiple: Any | None,
        quantity: int,
    ) -> EvidenceDecision:
        normalized = _symbol(symbol)
        current = _aware(now, "now")
        created = _aware(plan_created_at, "plan_created_at")
        expires = _aware(plan_expires_at, "plan_expires_at")
        causal_end = _aware(causal_bar_end, "causal_bar_end")
        shares = whole_shares(quantity)
        failures: list[str] = []
        with self._lock:
            quote = self.quotes.get(normalized)
            symbol_bars = dict(self.bars.get(normalized, {}))
            degradation = self.degraded.get(normalized)
        bar = symbol_bars.get(causal_end)
        if created > current or expires <= created or current > expires:
            failures.append("PLAN_EXPIRED_OR_TIME_INVALID")
        if causal_end > created:
            failures.append("COMPLETED_BAR_CAUSALITY_FAILED")
        if bar is None:
            failures.append("CAUSAL_COMPLETED_BAR_MISSING")
        elif (current - bar.end_at).total_seconds() > completed_bar_max_age_seconds:
            failures.append("COMPLETED_BAR_STALE")
        if degradation is not None:
            failures.append(degradation)
        quote_age: float | None = None
        spread: Decimal | None = None
        if quote is None:
            failures.append("QUOTE_MISSING")
        else:
            quote_age = (current - quote.oldest_venue_at).total_seconds()
            spread = quote.spread_bps
            if (current - quote.newest_venue_at).total_seconds() < -1:
                failures.append("QUOTE_FUTURE_DATED")
            if quote_age > quote_max_age_seconds:
                failures.append("QUOTE_STALE")
            if not quote.tradable:
                failures.append("ROBINHOOD_NOT_TRADABLE")
            if quote.halted:
                failures.append("MARKET_HALTED")
            if quote.ask <= Decimal("5"):
                failures.append("PRICE_NOT_STRICTLY_ABOVE_5")
            if max_spread_bps is None:
                failures.append("NUMERIC_SPREAD_GATE_UNRESOLVED")
            elif spread > decimal_value(max_spread_bps, "max_spread_bps"):
                failures.append("SPREAD_TOO_WIDE")
            if minimum_depth_multiple is None:
                failures.append("NUMERIC_DEPTH_GATE_UNRESOLVED")
            else:
                multiplier = decimal_value(minimum_depth_multiple, "minimum_depth_multiple")
                required = Decimal(shares) * multiplier
                if Decimal(quote.ask_size) < required or Decimal(quote.bid_size) < required:
                    failures.append("DISPLAYED_DEPTH_INSUFFICIENT")
        session_volume = sum(item.volume for item in symbol_bars.values())
        if session_volume < minimum_session_volume:
            failures.append("VOLUME_BELOW_750000")
        return EvidenceDecision(
            eligible=not failures,
            failures=tuple(dict.fromkeys(failures)),
            quote=quote,
            causal_bar=bar,
            session_volume=session_volume,
            quote_age_seconds=quote_age,
            spread_bps=spread,
        )


__all__ = [
    "CompletedBar",
    "EvidenceDecision",
    "MarketDataCache",
    "MarketSessionState",
    "Quote",
]
