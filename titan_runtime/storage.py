from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from math import isfinite
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Iterator
import uuid
from zoneinfo import ZoneInfo


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    symbol TEXT PRIMARY KEY,
    updated_ms INTEGER,
    price REAL,
    prev_close REAL,
    day_open REAL,
    day_high REAL,
    day_low REAL,
    day_volume REAL,
    prev_day_volume REAL,
    change_pct REAL,
    bid REAL,
    ask REAL,
    bid_size REAL,
    ask_size REAL,
    payload_json TEXT NOT NULL,
    received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS eligible_universe (
    symbol TEXT PRIMARY KEY,
    name TEXT,
    ticker_type TEXT NOT NULL,
    primary_exchange TEXT,
    cik TEXT,
    active INTEGER NOT NULL,
    refreshed_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eligible_universe_type
    ON eligible_universe(active, ticker_type, symbol);

CREATE TABLE IF NOT EXISTS bars_1m (
    symbol TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    window_vwap REAL,
    session_vwap REAL,
    accumulated_volume REAL,
    official_open REAL,
    otc INTEGER NOT NULL DEFAULT 0,
    received_at TEXT NOT NULL,
    PRIMARY KEY(symbol, start_ms)
);
CREATE INDEX IF NOT EXISTS idx_bars_1m_time ON bars_1m(start_ms);

CREATE TABLE IF NOT EXISTS bars_1s (
    symbol TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    window_vwap REAL,
    received_at TEXT NOT NULL,
    PRIMARY KEY(symbol, start_ms)
);
CREATE INDEX IF NOT EXISTS idx_bars_1s_time ON bars_1s(start_ms);

CREATE TABLE IF NOT EXISTS quotes (
    symbol TEXT PRIMARY KEY,
    timestamp_ms INTEGER NOT NULL,
    bid REAL,
    ask REAL,
    bid_size REAL,
    ask_size REAL,
    spread_pct REAL,
    received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidates (
    symbol TEXT PRIMARY KEY,
    observed_at TEXT NOT NULL,
    state TEXT NOT NULL,
    lane TEXT NOT NULL,
    signal_strength REAL NOT NULL,
    price REAL NOT NULL,
    gap_pct REAL,
    dollar_volume REAL,
    volume_acceleration REAL,
    price_acceleration REAL,
    relative_volume REAL,
    spread_pct REAL,
    short_atr REAL,
    base_high REAL,
    support REAL,
    invalidation REAL,
    limit_ceiling REAL,
    extension_atr REAL,
    quote_fresh INTEGER NOT NULL DEFAULT 0,
    preliminary_liquidity_pass INTEGER NOT NULL DEFAULT 0,
    catalyst_required INTEGER NOT NULL DEFAULT 1,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_candidates_rank
    ON candidates(signal_strength DESC, dollar_volume DESC);

CREATE TABLE IF NOT EXISTS prepared_trade_plans (
    plan_id TEXT PRIMARY KEY,
    plan_key TEXT UNIQUE NOT NULL,
    symbol TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    status TEXT NOT NULL,
    direction TEXT NOT NULL,
    lane TEXT NOT NULL,
    weighted_opportunity_score REAL,
    modeled_move_capacity_pct REAL,
    trigger REAL,
    structural_stop REAL,
    t1 REAL,
    t2 REAL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prepared_plans_rank
    ON prepared_trade_plans(observed_at DESC, weighted_opportunity_score DESC);

CREATE TABLE IF NOT EXISTS candidate_enrichment (
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    as_of_date TEXT NOT NULL,
    historical_follow_through REAL,
    gap_fade_risk REAL,
    market_cap REAL,
    weighted_shares_outstanding REAL,
    sic_code TEXT,
    sic_description TEXT,
    analog_count INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL,
    refreshed_at TEXT NOT NULL,
    PRIMARY KEY(symbol, direction)
);

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    symbol TEXT,
    priority INTEGER NOT NULL,
    dedupe_key TEXT UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending',
    payload_json TEXT NOT NULL,
    acknowledged_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_pending
    ON events(status, priority DESC, created_at ASC);

CREATE TABLE IF NOT EXISTS event_decisions (
    decision_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(event_id),
    decided_at TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL,
    details_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_decisions_time
    ON event_decisions(decided_at DESC, event_id);

CREATE TABLE IF NOT EXISTS health (
    component TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    details_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS halt_events (
    symbol TEXT NOT NULL,
    timestamp_ms INTEGER NOT NULL,
    indicator INTEGER NOT NULL,
    upper_band REAL,
    lower_band REAL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY(symbol, timestamp_ms, indicator)
);

CREATE TABLE IF NOT EXISTS trader_brain_lessons (
    lesson_id TEXT PRIMARY KEY,
    lesson_date TEXT NOT NULL,
    source TEXT NOT NULL,
    content_format TEXT NOT NULL,
    content_text TEXT NOT NULL,
    structured_json TEXT,
    ingested_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lessons_date
    ON trader_brain_lessons(lesson_date DESC, ingested_at DESC);

CREATE TABLE IF NOT EXISTS strategy_changes (
    change_id TEXT PRIMARY KEY,
    proposed_at TEXT NOT NULL,
    title TEXT NOT NULL,
    change_class TEXT NOT NULL CHECK(change_class IN (
        'observation','hypothesis','experimental_rule','validated_rule','production_rule'
    )),
    category TEXT NOT NULL CHECK(category IN (
        'data_collection','analytics','scanner_logic','strategy_logic','risk_logic','execution_logic'
    )),
    evidence_json TEXT NOT NULL,
    expected_effect TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed' CHECK(status IN (
        'proposed','testing','validated','rejected','approved','reverted'
    )),
    production_approved INTEGER NOT NULL DEFAULT 0,
    prior_version TEXT,
    proposed_version TEXT
);
CREATE INDEX IF NOT EXISTS idx_changes_status
    ON strategy_changes(status, proposed_at DESC);

CREATE TABLE IF NOT EXISTS research_entry_plans (
    plan_id TEXT PRIMARY KEY,
    trade_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    setup TEXT NOT NULL,
    lane TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    earliest_time TEXT,
    earliest_price REAL,
    conservative_time TEXT,
    conservative_price REAL,
    selected_time TEXT,
    selected_price REAL,
    structural_stop REAL NOT NULL,
    quantity REAL NOT NULL,
    capital REAL NOT NULL,
    context_json TEXT NOT NULL,
    outcome_locked INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_entry_plans_date
    ON research_entry_plans(trade_date DESC, symbol);

CREATE TABLE IF NOT EXISTS research_entry_outcomes (
    plan_id TEXT PRIMARY KEY REFERENCES research_entry_plans(plan_id),
    completed_at TEXT NOT NULL,
    observation_end TEXT NOT NULL,
    session_high REAL,
    session_low REAL,
    actual_exit_price REAL,
    actual_pnl REAL,
    actual_pnl_r REAL,
    earliest_pnl REAL,
    earliest_pnl_r REAL,
    conservative_pnl REAL,
    conservative_pnl_r REAL,
    hesitation_cost REAL,
    confirmation_savings REAL,
    mfe REAL,
    mae REAL,
    outcome_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS position_campaigns (
    campaign_id TEXT PRIMARY KEY,
    account_key TEXT NOT NULL,
    instrument_key TEXT NOT NULL,
    symbol TEXT NOT NULL,
    thesis_key TEXT NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('UP','DOWN')),
    asset_class TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'PLANNED','REVIEWED','SUBMITTED','PARTIAL','FILLED','PROTECTED',
        'CLOSING','CLOSED','CANCELED','REJECTED','FAILED'
    )),
    strategy_version TEXT NOT NULL,
    opened_at TEXT,
    updated_at TEXT NOT NULL,
    broker_confirmed_at TEXT,
    entry_price REAL,
    original_stop REAL,
    current_stop REAL,
    initial_quantity REAL NOT NULL DEFAULT 0,
    current_quantity REAL NOT NULL DEFAULT 0,
    core_quantity REAL NOT NULL DEFAULT 0,
    runner_quantity REAL NOT NULL DEFAULT 0,
    reference_risk_dollars REAL,
    high_water_price REAL,
    mfe_r REAL,
    mae_r REAL,
    continuation_health TEXT CHECK(continuation_health IN (
        'DOMINANT','HEALTHY','VULNERABLE','BROKEN','UNKNOWN'
    )),
    remaining_opportunity TEXT CHECK(remaining_opportunity IN (
        'EXPANDING','AVAILABLE','DEPLETED','UNKNOWN'
    )),
    last_action TEXT,
    next_actions_json TEXT NOT NULL,
    broker_state_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_position_campaigns_status
    ON position_campaigns(status, updated_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_position_campaigns_one_active
    ON position_campaigns(account_key, instrument_key)
    WHERE status NOT IN ('CLOSED','CANCELED','REJECTED','FAILED');
CREATE UNIQUE INDEX IF NOT EXISTS idx_position_campaigns_one_active_thesis
    ON position_campaigns(account_key, thesis_key)
    WHERE status NOT IN ('CLOSED','CANCELED','REJECTED','FAILED');

CREATE TABLE IF NOT EXISTS position_campaign_events (
    event_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES position_campaigns(campaign_id),
    status TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    broker_confirmed_at TEXT,
    event_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    UNIQUE(campaign_id, event_hash)
);
CREATE INDEX IF NOT EXISTS idx_position_campaign_events_campaign
    ON position_campaign_events(campaign_id, observed_at ASC);

CREATE TABLE IF NOT EXISTS risk_sessions (
    account_key TEXT NOT NULL,
    session_date TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    start_of_day_equity REAL NOT NULL,
    baseline_confirmed_at TEXT NOT NULL,
    current_equity REAL NOT NULL,
    realized_net_pnl REAL NOT NULL,
    confirmed_cash_flow_adjustment REAL NOT NULL DEFAULT 0,
    account_day_pnl REAL NOT NULL,
    loss_gauge REAL NOT NULL,
    loss_limit_dollars REAL NOT NULL DEFAULT -100,
    loss_lock INTEGER NOT NULL DEFAULT 0 CHECK(loss_lock IN (0,1)),
    loss_lock_triggered_at TEXT,
    profit_objective_dollars REAL NOT NULL DEFAULT 150,
    profit_objective_reached INTEGER NOT NULL DEFAULT 0
        CHECK(profit_objective_reached IN (0,1)),
    profit_objective_reached_at TEXT,
    active_profit_floor_dollars REAL,
    updated_at TEXT NOT NULL,
    broker_confirmed_at TEXT NOT NULL,
    broker_state_json TEXT NOT NULL,
    PRIMARY KEY(account_key, session_date)
);
CREATE INDEX IF NOT EXISTS idx_risk_sessions_date
    ON risk_sessions(session_date DESC, account_key);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _aware_timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty ISO-8601 timestamp")
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include an explicit UTC offset")
    return parsed.astimezone(timezone.utc).isoformat()


def _finite_float(value: Any, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be a finite number") from error
    if not isfinite(result):
        raise ValueError(f"{field} must be a finite number")
    return result


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def set_metadata(self, key: str, value: str) -> None:
        self.conn.execute(
            """INSERT INTO metadata(key, value, updated_at) VALUES(?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
            (key, value, utc_now()),
        )

    def upsert_snapshots(self, rows: Iterable[dict[str, Any]]) -> int:
        now = utc_now()
        values = []
        for item in rows:
            symbol = item.get("ticker")
            if not symbol:
                continue
            day = item.get("day") or {}
            prev = item.get("prevDay") or {}
            quote = item.get("lastQuote") or {}
            trade = item.get("lastTrade") or {}
            minute = item.get("min") or {}
            price = trade.get("p") or minute.get("c") or day.get("c")
            values.append(
                (
                    symbol,
                    item.get("updated"),
                    price,
                    prev.get("c"),
                    day.get("o"),
                    day.get("h"),
                    day.get("l"),
                    day.get("v") or day.get("dv"),
                    prev.get("v") or prev.get("dv"),
                    item.get("todaysChangePerc"),
                    quote.get("p"),
                    quote.get("P"),
                    quote.get("s"),
                    quote.get("S"),
                    json.dumps(item, separators=(",", ":")),
                    now,
                )
            )
        if not values:
            return 0
        with self.transaction():
            self.conn.executemany(
                """INSERT INTO snapshots(
                       symbol, updated_ms, price, prev_close, day_open, day_high, day_low,
                       day_volume, prev_day_volume, change_pct, bid, ask, bid_size, ask_size,
                       payload_json, received_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(symbol) DO UPDATE SET
                       updated_ms=excluded.updated_ms, price=excluded.price,
                       prev_close=excluded.prev_close, day_open=excluded.day_open,
                       day_high=excluded.day_high, day_low=excluded.day_low,
                       day_volume=excluded.day_volume, prev_day_volume=excluded.prev_day_volume,
                       change_pct=excluded.change_pct, bid=excluded.bid, ask=excluded.ask,
                       bid_size=excluded.bid_size, ask_size=excluded.ask_size,
                       payload_json=excluded.payload_json, received_at=excluded.received_at""",
                values,
            )
        return len(values)

    def get_snapshot(self, symbol: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM snapshots WHERE symbol=?", (symbol,)).fetchone()
        return dict(row) if row else None

    def replace_eligible_universe(self, rows: Iterable[dict[str, Any]]) -> int:
        now = utc_now()
        values = []
        for item in rows:
            symbol = item.get("ticker")
            ticker_type = item.get("type")
            if not symbol or not ticker_type:
                continue
            values.append(
                (
                    symbol, item.get("name"), ticker_type, item.get("primary_exchange"),
                    item.get("cik"), int(bool(item.get("active", True))), now,
                    json.dumps(item, separators=(",", ":")),
                )
            )
        with self.transaction():
            self.conn.execute("DELETE FROM eligible_universe")
            self.conn.executemany(
                """INSERT INTO eligible_universe(
                       symbol,name,ticker_type,primary_exchange,cik,active,refreshed_at,payload_json
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                values,
            )
        return len(values)

    def is_eligible_security(self, symbol: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM eligible_universe WHERE symbol=? AND active=1", (symbol,)
        ).fetchone()
        return bool(row)

    def get_eligible_security(self, symbol: str) -> dict[str, Any] | None:
        """Return provider reference metadata without implying broker tradability."""
        row = self.conn.execute(
            "SELECT * FROM eligible_universe WHERE symbol=? AND active=1", (symbol,)
        ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        item["broker_tradability_verified"] = False
        return item

    def eligible_universe_count(self) -> int:
        return int(
            self.conn.execute(
                "SELECT COUNT(*) FROM eligible_universe WHERE active=1"
            ).fetchone()[0]
        )

    def snapshot_coverage(self) -> dict[str, int]:
        row = self.conn.execute(
            """SELECT
                   COUNT(*) AS universe_count,
                   SUM(CASE WHEN s.symbol IS NOT NULL THEN 1 ELSE 0 END) AS snapshot_count,
                   SUM(CASE WHEN s.price BETWEEN 1 AND 1000
                                 AND ABS(s.change_pct) >= 4
                                 AND COALESCE(s.price * s.day_volume, 0) >= 2000000
                            THEN 1 ELSE 0 END) AS broad_qualifier_count
               FROM eligible_universe u
               LEFT JOIN snapshots s ON s.symbol=u.symbol
               WHERE u.active=1"""
        ).fetchone()
        return {key: int(row[key] or 0) for key in row.keys()}

    def top_snapshot_symbols(self, limit: int) -> list[str]:
        rows = self.conn.execute(
            """SELECT symbol FROM snapshots
               WHERE price >= 1 AND ABS(change_pct) >= 4 AND day_volume > 0
               ORDER BY ABS(change_pct) DESC, day_volume * price DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [row["symbol"] for row in rows]

    def insert_minute_bar(self, event: dict[str, Any]) -> None:
        self.conn.execute(
            """INSERT INTO bars_1m(
                   symbol,start_ms,end_ms,open,high,low,close,volume,window_vwap,
                   session_vwap,accumulated_volume,official_open,otc,received_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(symbol,start_ms) DO UPDATE SET
                   end_ms=excluded.end_ms, open=excluded.open, high=excluded.high,
                   low=excluded.low, close=excluded.close, volume=excluded.volume,
                   window_vwap=excluded.window_vwap, session_vwap=excluded.session_vwap,
                   accumulated_volume=excluded.accumulated_volume,
                   official_open=excluded.official_open, otc=excluded.otc,
                   received_at=excluded.received_at""",
            (
                event["sym"], event["s"], event["e"], event["o"], event["h"],
                event["l"], event["c"], float(event.get("dv") or event.get("v") or 0),
                event.get("vw"), event.get("a"),
                float(event.get("dav") or event.get("av") or 0), event.get("op"),
                1 if event.get("otc") else 0, utc_now(),
            ),
        )

    def insert_second_bar(self, event: dict[str, Any]) -> None:
        self.conn.execute(
            """INSERT INTO bars_1s(
                   symbol,start_ms,end_ms,open,high,low,close,volume,window_vwap,received_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(symbol,start_ms) DO UPDATE SET
                   end_ms=excluded.end_ms, open=excluded.open, high=excluded.high,
                   low=excluded.low, close=excluded.close, volume=excluded.volume,
                   window_vwap=excluded.window_vwap, received_at=excluded.received_at""",
            (
                event["sym"], event["s"], event["e"], event["o"], event["h"],
                event["l"], event["c"], float(event.get("dv") or event.get("v") or 0),
                event.get("vw"), utc_now(),
            ),
        )

    def recent_bars(self, symbol: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM bars_1m WHERE symbol=? ORDER BY start_ms DESC LIMIT ?",
            (symbol, limit),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def recent_bars_since(self, symbol: str, start_ms: int, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT * FROM bars_1m WHERE symbol=? AND start_ms>=?
               ORDER BY start_ms DESC LIMIT ?""",
            (symbol, start_ms, limit),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def same_minute_cumulative_history(
        self,
        symbol: str,
        reference_ms: int,
        timezone_name: str,
        max_sessions: int = 20,
    ) -> list[float]:
        """Return causal same-clock-minute cumulative volume from prior sessions.

        Matching happens in the configured market timezone so daylight-saving
        changes cannot shift the comparison minute.  The current local session
        is excluded even when an earlier bar happens to share the same UTC
        clock value.  Missing bars remain missing and are never synthesized.
        """
        if max_sessions < 1:
            raise ValueError("max_sessions must be positive")
        market_tz = ZoneInfo(timezone_name)
        reference = datetime.fromtimestamp(reference_ms / 1000, timezone.utc).astimezone(market_tz)
        target_clock = (reference.hour, reference.minute)
        target_date = reference.date()
        # Pull a bounded calendar window large enough to find prior completed
        # sessions around weekends and exchange holidays.
        cutoff = reference - timedelta(days=max(45, max_sessions * 3))
        rows = self.conn.execute(
            """SELECT start_ms, accumulated_volume
               FROM bars_1m
               WHERE symbol=? AND start_ms<? AND start_ms>=?
                     AND accumulated_volume IS NOT NULL AND accumulated_volume>0
               ORDER BY start_ms DESC""",
            (symbol, reference_ms, int(cutoff.timestamp() * 1000)),
        ).fetchall()
        by_session: dict[str, float] = {}
        for row in rows:
            observed = datetime.fromtimestamp(
                int(row["start_ms"]) / 1000, timezone.utc
            ).astimezone(market_tz)
            if observed.date() >= target_date or (observed.hour, observed.minute) != target_clock:
                continue
            session_key = observed.date().isoformat()
            if session_key not in by_session:
                by_session[session_key] = float(row["accumulated_volume"])
            if len(by_session) >= max_sessions:
                break
        return list(by_session.values())

    def clear_candidates(self) -> None:
        self.conn.execute("DELETE FROM candidates")
        self.conn.execute("DELETE FROM quotes")

    def upsert_quote(self, event: dict[str, Any]) -> None:
        bid = event.get("bp")
        ask = event.get("ap")
        midpoint = ((bid or 0) + (ask or 0)) / 2
        spread_pct = ((ask - bid) / midpoint * 100) if bid and ask and midpoint > 0 else None
        self.conn.execute(
            """INSERT INTO quotes(symbol,timestamp_ms,bid,ask,bid_size,ask_size,spread_pct,received_at)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(symbol) DO UPDATE SET
                   timestamp_ms=excluded.timestamp_ms,bid=excluded.bid,ask=excluded.ask,
                   bid_size=excluded.bid_size,ask_size=excluded.ask_size,
                   spread_pct=excluded.spread_pct,received_at=excluded.received_at""",
            (
                event["sym"], event.get("t", 0), bid, ask, event.get("bs"),
                event.get("as"), spread_pct, utc_now(),
            ),
        )

    def get_quote(self, symbol: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM quotes WHERE symbol=?", (symbol,)).fetchone()
        return dict(row) if row else None

    def upsert_candidate(self, payload: dict[str, Any]) -> None:
        self.conn.execute(
            """INSERT INTO candidates(
                   symbol,observed_at,state,lane,signal_strength,price,gap_pct,dollar_volume,
                   volume_acceleration,price_acceleration,relative_volume,spread_pct,short_atr,
                   base_high,support,invalidation,limit_ceiling,extension_atr,quote_fresh,
                   preliminary_liquidity_pass,catalyst_required,payload_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(symbol) DO UPDATE SET
                   observed_at=excluded.observed_at,state=excluded.state,lane=excluded.lane,
                   signal_strength=excluded.signal_strength,price=excluded.price,
                   gap_pct=excluded.gap_pct,dollar_volume=excluded.dollar_volume,
                   volume_acceleration=excluded.volume_acceleration,
                   price_acceleration=excluded.price_acceleration,
                   relative_volume=excluded.relative_volume,spread_pct=excluded.spread_pct,
                   short_atr=excluded.short_atr,base_high=excluded.base_high,
                   support=excluded.support,invalidation=excluded.invalidation,
                   limit_ceiling=excluded.limit_ceiling,extension_atr=excluded.extension_atr,
                   quote_fresh=excluded.quote_fresh,
                   preliminary_liquidity_pass=excluded.preliminary_liquidity_pass,
                   catalyst_required=excluded.catalyst_required,payload_json=excluded.payload_json""",
            (
                payload["symbol"], payload["observed_at"], payload["state"], payload["lane"],
                payload["signal_strength"], payload["price"], payload.get("gap_pct"),
                payload.get("dollar_volume"), payload.get("volume_acceleration"),
                payload.get("price_acceleration"), payload.get("relative_volume"),
                payload.get("spread_pct"), payload.get("short_atr"), payload.get("base_high"),
                payload.get("support"), payload.get("invalidation"), payload.get("limit_ceiling"),
                payload.get("extension_atr"), int(payload.get("quote_fresh", False)),
                int(payload.get("preliminary_liquidity_pass", False)), 1,
                json.dumps(payload, separators=(",", ":")),
            ),
        )

    def get_candidate(self, symbol: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM candidates WHERE symbol=?", (symbol,)).fetchone()
        return dict(row) if row else None

    def delete_candidate(self, symbol: str) -> None:
        self.conn.execute("DELETE FROM candidates WHERE symbol=?", (symbol,))

    def leaderboard(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT * FROM candidates
               ORDER BY COALESCE(
                   CASE
                       WHEN json_extract(payload_json,'$.weighted_scale_version') =
                            'trader_brain_2026-08-22_v2'
                       THEN CAST(json_extract(payload_json,'$.weighted_opportunity_score') AS REAL)
                   END,
                   CAST(json_extract(payload_json,'$.available_evidence_score') AS REAL),
                   CAST(json_extract(payload_json,'$.weighted_opportunity_score') AS REAL),
                   signal_strength
               ) DESC,
               COALESCE(
                   CAST(json_extract(payload_json,'$.weighted_evidence_coverage_pct') AS REAL),
                   0
               ) DESC,
               dollar_volume DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def all_candidate_symbols(self) -> list[str]:
        rows = self.conn.execute(
            """SELECT symbol FROM candidates
               ORDER BY COALESCE(
                   CASE
                       WHEN json_extract(payload_json,'$.weighted_scale_version') =
                            'trader_brain_2026-08-22_v2'
                       THEN CAST(json_extract(payload_json,'$.weighted_opportunity_score') AS REAL)
                   END,
                   CAST(json_extract(payload_json,'$.available_evidence_score') AS REAL),
                   CAST(json_extract(payload_json,'$.weighted_opportunity_score') AS REAL),
                   signal_strength
               ) DESC,
               COALESCE(
                   CAST(json_extract(payload_json,'$.weighted_evidence_coverage_pct') AS REAL),
                   0
               ) DESC,
               dollar_volume DESC"""
        ).fetchall()
        return [str(row["symbol"]) for row in rows]

    def save_prepared_trade_plan(self, payload: dict[str, Any]) -> str | None:
        fingerprint = {
            key: payload.get(key)
            for key in (
                "schema_version", "policy_version", "sizing_policy_version",
                "weighted_scale_version", "symbol", "status", "direction", "lane",
                "setup", "trigger", "review_limit_ceiling", "reference_entry_price",
                "structural_stop", "risk_per_share", "t1", "t2", "t3",
                "weighted_opportunity_score", "modeled_move_capacity_pct",
                "preliminary_quantity_cap", "preliminary_risk_cap", "risk_campaign",
                "build_tranches_pct", "core_runner_policy", "add_policy", "blockers",
            )
        }
        digest = hashlib.sha256(
            json.dumps(fingerprint, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        plan_key = f"{payload['symbol']}:{digest}"
        plan_id = str(uuid.uuid4())
        try:
            self.conn.execute(
                """INSERT INTO prepared_trade_plans(
                       plan_id,plan_key,symbol,observed_at,status,direction,lane,
                       weighted_opportunity_score,modeled_move_capacity_pct,trigger,
                       structural_stop,t1,t2,payload_json,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    plan_id, plan_key, payload["symbol"], payload["observed_at"],
                    payload["status"], payload["direction"], payload["lane"],
                    payload.get("weighted_opportunity_score"),
                    payload.get("modeled_move_capacity_pct"), payload.get("trigger"),
                    payload.get("structural_stop"), payload.get("t1"), payload.get("t2"),
                    json.dumps(payload, separators=(",", ":")), utc_now(),
                ),
            )
        except sqlite3.IntegrityError:
            return None
        return plan_id

    def upsert_candidate_enrichment(self, payload: dict[str, Any]) -> None:
        self.conn.execute(
            """INSERT INTO candidate_enrichment(
                   symbol,direction,as_of_date,historical_follow_through,gap_fade_risk,
                   market_cap,weighted_shares_outstanding,sic_code,sic_description,
                   analog_count,payload_json,refreshed_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(symbol,direction) DO UPDATE SET
                   as_of_date=excluded.as_of_date,
                   historical_follow_through=excluded.historical_follow_through,
                   gap_fade_risk=excluded.gap_fade_risk,
                   market_cap=excluded.market_cap,
                   weighted_shares_outstanding=excluded.weighted_shares_outstanding,
                   sic_code=excluded.sic_code,sic_description=excluded.sic_description,
                   analog_count=excluded.analog_count,payload_json=excluded.payload_json,
                   refreshed_at=excluded.refreshed_at""",
            (
                payload["symbol"], payload["direction"], payload["as_of_date"],
                payload.get("historical_follow_through"), payload.get("gap_fade_risk"),
                payload.get("market_cap"), payload.get("weighted_shares_outstanding"),
                payload.get("sic_code"), payload.get("sic_description"),
                int(payload.get("analog_count") or 0),
                json.dumps(payload, separators=(",", ":")), utc_now(),
            ),
        )

    def get_candidate_enrichment(self, symbol: str, direction: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM candidate_enrichment WHERE symbol=? AND direction=?",
            (symbol, direction),
        ).fetchone()
        if not row:
            return None
        item = dict(row)
        item.update(json.loads(item.pop("payload_json")))
        return item

    def latest_prepared_trade_plans(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT p.* FROM prepared_trade_plans p
               JOIN (
                   SELECT symbol, MAX(created_at) AS latest
                   FROM prepared_trade_plans GROUP BY symbol
               ) newest ON newest.symbol=p.symbol AND newest.latest=p.created_at
               ORDER BY p.weighted_opportunity_score DESC, p.created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def emit_event(
        self,
        event_type: str,
        symbol: str | None,
        priority: int,
        payload: dict[str, Any],
        dedupe_key: str,
    ) -> str | None:
        event_id = str(uuid.uuid4())
        now = utc_now()
        try:
            self.conn.execute(
                """INSERT INTO events(event_id,created_at,event_type,symbol,priority,dedupe_key,payload_json)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    event_id, now, event_type, symbol, priority, dedupe_key,
                    json.dumps(payload, separators=(",", ":")),
                ),
            )
        except sqlite3.IntegrityError:
            return None
        # The queue is for live decisions, while the table also serves as
        # durable decision-time evidence.  Keep the newest actionable
        # observation per symbol/type pending and retain older rows as
        # superseded history.  Excluding the new row also preserves exact-key
        # deduplication when an insert is rejected above.
        if event_type in {
            "LEADER_CANDIDATE",
            "MOMENTUM_WATCH",
            "EMERGING_INTRADAY_LEADER_WATCH",
            "BASE_READY",
            "TRIGGER_CROSS",
        }:
            self.conn.execute(
                """UPDATE events
                   SET status='superseded', acknowledged_at=?
                   WHERE status='pending' AND event_type=? AND symbol=?
                         AND event_id != ?""",
                (now, event_type, symbol, event_id),
            )
        return event_id

    def pending_events(
        self, limit: int = 50, max_age_seconds: int = 600
    ) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=max_age_seconds)
        self.conn.execute(
            """UPDATE events
               SET status='expired', acknowledged_at=?
               WHERE status='pending' AND created_at < ?""",
            (now.isoformat(), cutoff.isoformat()),
        )
        rows = self.conn.execute(
            """SELECT * FROM events WHERE status='pending'
               ORDER BY priority DESC, created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def acknowledge_event(self, event_id: str) -> bool:
        cursor = self.conn.execute(
            "UPDATE events SET status='acknowledged', acknowledged_at=? WHERE event_id=?",
            (utc_now(), event_id),
        )
        return cursor.rowcount == 1

    def record_event_decision(
        self,
        event_id: str,
        decision: str,
        reason: str,
        details: dict[str, Any] | None = None,
    ) -> str:
        if not decision.strip() or not reason.strip():
            raise ValueError("decision and reason are required")
        event = self.conn.execute(
            "SELECT event_id FROM events WHERE event_id=?", (event_id,)
        ).fetchone()
        if not event:
            raise ValueError(f"unknown event_id: {event_id}")
        decision_id = str(uuid.uuid4())
        self.conn.execute(
            """INSERT INTO event_decisions(
                   decision_id,event_id,decided_at,decision,reason,details_json
               ) VALUES(?,?,?,?,?,?)""",
            (
                decision_id, event_id, utc_now(), decision.strip().upper(), reason.strip(),
                json.dumps(details or {}, separators=(",", ":")),
            ),
        )
        return decision_id

    def event_decisions(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT d.*, e.event_type, e.symbol, e.created_at AS event_created_at
               FROM event_decisions d
               JOIN events e ON e.event_id=d.event_id
               ORDER BY d.decided_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            result.append(item)
        return result

    def set_health(self, component: str, status: str, details: dict[str, Any]) -> None:
        self.conn.execute(
            """INSERT INTO health(component,status,checked_at,details_json) VALUES(?,?,?,?)
               ON CONFLICT(component) DO UPDATE SET status=excluded.status,
               checked_at=excluded.checked_at,details_json=excluded.details_json""",
            (component, status, utc_now(), json.dumps(details, separators=(",", ":"))),
        )

    def health(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM health ORDER BY component").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            result.append(item)
        return result

    def insert_halt(self, event: dict[str, Any], indicator: int) -> None:
        self.conn.execute(
            """INSERT OR IGNORE INTO halt_events(
                   symbol,timestamp_ms,indicator,upper_band,lower_band,payload_json
               ) VALUES(?,?,?,?,?,?)""",
            (
                event.get("T"), event.get("t", 0), indicator, event.get("h"), event.get("l"),
                json.dumps(event, separators=(",", ":")),
            ),
        )

    def post_halt_context(
        self,
        symbol: str,
        reference_ms: int,
        timezone_name: str,
    ) -> dict[str, Any] | None:
        """Return causal same-session LULD state for an entry rearm check.

        The latest halt indicator is authoritative.  A halt remains entry-blocking
        until a resumption arrives, and an under-$5 resumption remains blocking
        until two completed one-minute bars have been persisted.  Callers still
        require a newly recomputed structure before emitting another entry event.
        """
        row = self.conn.execute(
            """SELECT timestamp_ms, indicator FROM halt_events
               WHERE symbol=? AND indicator IN (17,18) AND timestamp_ms<=?
               ORDER BY timestamp_ms DESC LIMIT 1""",
            (symbol, reference_ms),
        ).fetchone()
        if not row:
            return None
        latest_event_ms = int(row["timestamp_ms"])
        latest_indicator = int(row["indicator"])
        market_tz = ZoneInfo(timezone_name)
        reference_date = datetime.fromtimestamp(
            reference_ms / 1000, timezone.utc
        ).astimezone(market_tz).date()
        latest_event_date = datetime.fromtimestamp(
            latest_event_ms / 1000, timezone.utc
        ).astimezone(market_tz).date()
        if latest_event_date != reference_date:
            return None
        if latest_indicator == 17:
            return {
                "halt_timestamp_ms": latest_event_ms,
                "resumption_timestamp_ms": None,
                "active_halt": True,
                "completed_post_resumption_bars": 0,
                "minimum_completed_bars": 2,
                "entry_rearmed": False,
                "trade_authority": False,
            }

        resumption_ms = latest_event_ms
        completed_bars = int(
            self.conn.execute(
                """SELECT COUNT(*) FROM bars_1m
                   WHERE symbol=? AND start_ms>? AND start_ms<=?""",
                (symbol, resumption_ms, reference_ms),
            ).fetchone()[0]
        )
        return {
            "halt_timestamp_ms": None,
            "resumption_timestamp_ms": resumption_ms,
            "active_halt": False,
            "completed_post_resumption_bars": completed_bars,
            "minimum_completed_bars": 2,
            "entry_rearmed": completed_bars >= 2,
            "trade_authority": False,
        }

    def ingest_lesson(
        self,
        lesson_date: str,
        source: str,
        content_text: str,
        content_format: str = "markdown",
        structured: dict[str, Any] | None = None,
    ) -> str:
        if not lesson_date or not source or not content_text.strip():
            raise ValueError("lesson_date, source, and non-empty content are required")
        lesson_id = str(uuid.uuid4())
        self.conn.execute(
            """INSERT INTO trader_brain_lessons(
                   lesson_id,lesson_date,source,content_format,content_text,structured_json,ingested_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                lesson_id, lesson_date, source, content_format, content_text,
                json.dumps(structured, separators=(",", ":")) if structured is not None else None,
                utc_now(),
            ),
        )
        return lesson_id

    def latest_lessons(self, limit: int = 5) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT * FROM trader_brain_lessons
               ORDER BY lesson_date DESC, ingested_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            raw = item.pop("structured_json")
            item["structured"] = json.loads(raw) if raw else None
            result.append(item)
        return result

    def propose_strategy_change(self, payload: dict[str, Any]) -> str:
        change_class = str(payload.get("change_class") or "").lower()
        category = str(payload.get("category") or "").lower()
        allowed_classes = {
            "observation", "hypothesis", "experimental_rule", "validated_rule", "production_rule"
        }
        allowed_categories = {
            "data_collection", "analytics", "scanner_logic", "strategy_logic", "risk_logic", "execution_logic"
        }
        if change_class not in allowed_classes:
            raise ValueError(f"invalid change_class: {change_class}")
        if category not in allowed_categories:
            raise ValueError(f"invalid category: {category}")
        if change_class == "production_rule":
            raise ValueError(
                "daily research cannot register a production_rule; live changes require a separate user-authorized workflow"
            )
        change_id = str(uuid.uuid4())
        self.conn.execute(
            """INSERT INTO strategy_changes(
                   change_id,proposed_at,title,change_class,category,evidence_json,
                   expected_effect,status,production_approved,prior_version,proposed_version
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                change_id, utc_now(), payload["title"], change_class, category,
                json.dumps(payload.get("evidence") or {}, separators=(",", ":")),
                payload["expected_effect"], payload.get("status", "proposed"),
                int(bool(payload.get("production_approved", False))),
                payload.get("prior_version"), payload.get("proposed_version"),
            ),
        )
        return change_id

    def strategy_changes(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM strategy_changes ORDER BY proposed_at DESC LIMIT ?", (limit,)
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["evidence"] = json.loads(item.pop("evidence_json"))
            item["production_approved"] = bool(item["production_approved"])
            result.append(item)
        return result

    def create_entry_plan(self, payload: dict[str, Any]) -> str:
        required = ("trade_date", "symbol", "setup", "lane", "structural_stop", "quantity", "capital")
        missing = [field for field in required if payload.get(field) is None]
        if missing:
            raise ValueError(f"missing entry-plan fields: {', '.join(missing)}")
        prices = [
            payload.get("earliest_price"), payload.get("conservative_price"), payload.get("selected_price")
        ]
        if not any(price is not None for price in prices):
            raise ValueError("at least one entry alternative price is required")
        if float(payload["quantity"]) <= 0 or float(payload["capital"]) <= 0:
            raise ValueError("quantity and capital must be positive")
        plan_id = str(uuid.uuid4())
        context = payload.get("context") or {}
        context["capture_rule"] = "Entry alternatives recorded before outcome attachment."
        self.conn.execute(
            """INSERT INTO research_entry_plans(
                   plan_id,trade_date,symbol,setup,lane,captured_at,earliest_time,earliest_price,
                   conservative_time,conservative_price,selected_time,selected_price,
                   structural_stop,quantity,capital,context_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                plan_id, payload["trade_date"], str(payload["symbol"]).upper(), payload["setup"],
                payload["lane"], utc_now(), payload.get("earliest_time"), payload.get("earliest_price"),
                payload.get("conservative_time"), payload.get("conservative_price"),
                payload.get("selected_time"), payload.get("selected_price"),
                payload["structural_stop"], payload["quantity"], payload["capital"],
                json.dumps(context, separators=(",", ":")),
            ),
        )
        return plan_id

    def attach_entry_outcome(self, plan_id: str, payload: dict[str, Any]) -> None:
        plan = self.conn.execute(
            "SELECT * FROM research_entry_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if not plan:
            raise ValueError(f"unknown plan_id: {plan_id}")
        if plan["outcome_locked"]:
            raise ValueError("outcome already attached; append a correction through change control")
        if not payload.get("observation_end"):
            raise ValueError("observation_end is required")
        with self.transaction():
            self.conn.execute(
                """INSERT INTO research_entry_outcomes(
                       plan_id,completed_at,observation_end,session_high,session_low,
                       actual_exit_price,actual_pnl,actual_pnl_r,earliest_pnl,earliest_pnl_r,
                       conservative_pnl,conservative_pnl_r,hesitation_cost,confirmation_savings,
                       mfe,mae,outcome_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    plan_id, utc_now(), payload["observation_end"], payload.get("session_high"),
                    payload.get("session_low"), payload.get("actual_exit_price"),
                    payload.get("actual_pnl"), payload.get("actual_pnl_r"),
                    payload.get("earliest_pnl"), payload.get("earliest_pnl_r"),
                    payload.get("conservative_pnl"), payload.get("conservative_pnl_r"),
                    payload.get("hesitation_cost"), payload.get("confirmation_savings"),
                    payload.get("mfe"), payload.get("mae"),
                    json.dumps(payload, separators=(",", ":")),
                ),
            )
            self.conn.execute(
                "UPDATE research_entry_plans SET outcome_locked=1 WHERE plan_id=?", (plan_id,)
            )

    def entry_comparisons(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT p.*, o.completed_at, o.observation_end, o.session_high, o.session_low,
                      o.actual_exit_price, o.actual_pnl, o.actual_pnl_r,
                      o.earliest_pnl, o.earliest_pnl_r,
                      o.conservative_pnl, o.conservative_pnl_r,
                      o.hesitation_cost, o.confirmation_savings, o.mfe, o.mae
               FROM research_entry_plans p
               LEFT JOIN research_entry_outcomes o ON o.plan_id=p.plan_id
               ORDER BY p.trade_date DESC, p.captured_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["context"] = json.loads(item.pop("context_json"))
            item["outcome_locked"] = bool(item["outcome_locked"])
            result.append(item)
        return result

    def upsert_position_campaign(self, payload: dict[str, Any]) -> str:
        """Persist management state; this never creates broker authority.

        Filled/protected/closing/closed states require a broker confirmation
        timestamp so local inference cannot silently become the source of truth.
        The original thesis stop is immutable after the campaign is created.
        """
        required = (
            "account_key", "instrument_key", "symbol", "thesis_key", "direction",
            "asset_class", "status", "strategy_version",
        )
        missing = [field for field in required if not payload.get(field)]
        if missing:
            raise ValueError(f"missing position-campaign fields: {', '.join(missing)}")
        status = str(payload["status"]).upper()
        valid_statuses = {
            "PLANNED", "REVIEWED", "SUBMITTED", "PARTIAL", "FILLED", "PROTECTED",
            "CLOSING", "CLOSED", "CANCELED", "REJECTED", "FAILED",
        }
        if status not in valid_statuses:
            raise ValueError(f"invalid position-campaign status: {status}")
        thesis_key = str(payload["thesis_key"]).strip().upper()
        direction = str(payload["direction"]).strip().upper()
        if not thesis_key:
            raise ValueError("thesis_key cannot be empty")
        if direction not in {"UP", "DOWN"}:
            raise ValueError("position-campaign direction must be UP or DOWN")
        broker_confirmed_statuses = {
            "SUBMITTED", "PARTIAL", "FILLED", "PROTECTED", "CLOSING", "CLOSED",
            "CANCELED", "REJECTED", "FAILED",
        }
        broker_confirmed_at = None
        broker_state = payload.get("broker_state") or {}
        if not isinstance(broker_state, dict):
            raise ValueError("broker_state must be an object")
        if status in broker_confirmed_statuses:
            if not payload.get("broker_confirmed_at"):
                raise ValueError(f"{status} requires broker_confirmed_at")
            broker_confirmed_at = _aware_timestamp(
                payload.get("broker_confirmed_at"), "broker_confirmed_at"
            )
            if not broker_state:
                raise ValueError(f"{status} requires nonempty broker_state evidence")
        opened_at = (
            _aware_timestamp(payload.get("opened_at"), "opened_at")
            if payload.get("opened_at")
            else None
        )
        quantities = {
            key: _finite_float(payload.get(key) or 0, key)
            for key in ("initial_quantity", "current_quantity", "core_quantity", "runner_quantity")
        }
        if any(value < 0 for value in quantities.values()):
            raise ValueError("position-campaign quantities cannot be negative")
        if quantities["core_quantity"] + quantities["runner_quantity"] > quantities["current_quantity"] + 1e-9:
            raise ValueError("core plus runner quantity cannot exceed current quantity")
        zero_position_statuses = {"PLANNED", "REVIEWED", "SUBMITTED", "CLOSED", "CANCELED", "REJECTED", "FAILED"}
        if status in zero_position_statuses and quantities["current_quantity"] != 0:
            raise ValueError(f"{status} requires current_quantity=0")
        position_statuses = {"PARTIAL", "FILLED", "PROTECTED"}
        if status in position_statuses and quantities["current_quantity"] <= 0:
            raise ValueError(f"{status} requires a positive current_quantity")
        if quantities["current_quantity"] > 0 and abs(
            quantities["core_quantity"]
            + quantities["runner_quantity"]
            - quantities["current_quantity"]
        ) > 1e-9:
            raise ValueError("core plus runner quantity must equal current quantity")
        entry_price = (
            _finite_float(payload.get("entry_price"), "entry_price")
            if payload.get("entry_price") is not None
            else None
        )
        if status in position_statuses and (entry_price is None or entry_price <= 0):
            raise ValueError(f"{status} requires a positive entry_price")
        original_stop_input = (
            _finite_float(payload.get("original_stop"), "original_stop")
            if payload.get("original_stop") is not None
            else None
        )
        current_stop = (
            _finite_float(payload.get("current_stop"), "current_stop")
            if payload.get("current_stop") is not None
            else None
        )
        if original_stop_input is not None and original_stop_input <= 0:
            raise ValueError("original_stop must be positive")
        if current_stop is not None and current_stop <= 0:
            raise ValueError("current_stop must be positive")
        if (
            original_stop_input is not None
            and current_stop is not None
            and current_stop + 1e-9 < original_stop_input
        ):
            raise ValueError("current_stop may not widen below original_stop")
        if status == "PROTECTED":
            if broker_state.get("protection_confirmed") is not True:
                raise ValueError("PROTECTED requires broker-confirmed protection evidence")
            if current_stop is None:
                raise ValueError("PROTECTED requires current_stop")

        terminal_statuses = ("CLOSED", "CANCELED", "REJECTED", "FAILED")
        placeholders = ",".join("?" for _ in terminal_statuses)
        with self.transaction():
            existing = self.conn.execute(
                f"""SELECT campaign_id, original_stop, current_stop, status,
                           broker_confirmed_at, symbol, thesis_key, direction,
                           asset_class, strategy_version
                    FROM position_campaigns
                    WHERE account_key=? AND instrument_key=?
                      AND status NOT IN ({placeholders})
                    ORDER BY updated_at DESC LIMIT 1""",
                (payload["account_key"], payload["instrument_key"], *terminal_statuses),
            ).fetchone()
            if not existing:
                thesis_conflict = self.conn.execute(
                    f"""SELECT campaign_id, instrument_key FROM position_campaigns
                        WHERE account_key=? AND thesis_key=?
                          AND status NOT IN ({placeholders})
                        ORDER BY updated_at DESC LIMIT 1""",
                    (payload["account_key"], thesis_key, *terminal_statuses),
                ).fetchone()
                if thesis_conflict:
                    raise ValueError(
                        "an active campaign already exists for account and thesis_key; "
                        "reconcile or close it before opening another expression"
                    )
            if not existing and status in {"CLOSED", "CANCELED", "REJECTED", "FAILED"}:
                raise ValueError(f"{status} requires an existing active campaign")
            if existing:
                ordered_statuses = {
                    "PLANNED": 0,
                    "REVIEWED": 1,
                    "SUBMITTED": 2,
                    "PARTIAL": 3,
                    "FILLED": 4,
                    "PROTECTED": 5,
                    "CLOSING": 6,
                    "CLOSED": 7,
                }
                old_status = str(existing["status"])
                immutable_identity = {
                    "symbol": str(payload["symbol"]).upper(),
                    "thesis_key": thesis_key,
                    "direction": direction,
                    "asset_class": str(payload["asset_class"]).lower(),
                    "strategy_version": str(payload["strategy_version"]),
                }
                for field, value in immutable_identity.items():
                    if str(existing[field]) != value:
                        raise ValueError(f"{field} is immutable for an active campaign")
                if status in {"CANCELED", "REJECTED", "FAILED"} and old_status not in {
                    "PLANNED", "REVIEWED", "SUBMITTED"
                }:
                    raise ValueError(
                        f"{old_status} may not transition to terminal status {status}; "
                        "broker exposure must remain active, closing, or closed"
                    )
                if status == "CLOSED" and old_status not in {
                    "PARTIAL", "FILLED", "PROTECTED", "CLOSING"
                }:
                    raise ValueError(f"{old_status} may not transition directly to CLOSED")
                if (
                    status in ordered_statuses
                    and old_status in ordered_statuses
                    and ordered_statuses[status] < ordered_statuses[old_status]
                ):
                    raise ValueError(f"invalid backward campaign transition: {old_status} -> {status}")
                if (
                    broker_confirmed_at
                    and existing["broker_confirmed_at"]
                    and datetime.fromisoformat(broker_confirmed_at)
                    < datetime.fromisoformat(str(existing["broker_confirmed_at"]))
                ):
                    raise ValueError("broker_confirmed_at may not move backward")
            original_stop = original_stop_input
            if existing and existing["original_stop"] is not None:
                if (
                    original_stop is not None
                    and abs(float(original_stop) - float(existing["original_stop"])) > 1e-9
                ):
                    raise ValueError("original_stop is immutable for an existing campaign")
                original_stop = existing["original_stop"]
            if status == "PROTECTED" and original_stop is None:
                raise ValueError("PROTECTED requires an immutable original_stop")
            if (
                original_stop is not None
                and current_stop is not None
                and current_stop + 1e-9 < float(original_stop)
            ):
                raise ValueError("current_stop may not widen below original_stop")
            if (
                existing
                and existing["current_stop"] is not None
                and current_stop is not None
                and current_stop + 1e-9 < float(existing["current_stop"])
            ):
                raise ValueError("current_stop may not be widened for an active campaign")
            campaign_id = str(existing["campaign_id"]) if existing else str(uuid.uuid4())
            now = utc_now()
            self.conn.execute(
                """INSERT INTO position_campaigns(
                       campaign_id,account_key,instrument_key,symbol,thesis_key,direction,
                       asset_class,status,
                       strategy_version,opened_at,updated_at,broker_confirmed_at,
                       entry_price,original_stop,current_stop,initial_quantity,current_quantity,
                       core_quantity,runner_quantity,reference_risk_dollars,high_water_price,
                       mfe_r,mae_r,continuation_health,remaining_opportunity,last_action,
                       next_actions_json,broker_state_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(campaign_id) DO UPDATE SET
                       symbol=excluded.symbol,asset_class=excluded.asset_class,status=excluded.status,
                       strategy_version=excluded.strategy_version,
                       opened_at=COALESCE(position_campaigns.opened_at,excluded.opened_at),
                       updated_at=excluded.updated_at,
                       broker_confirmed_at=COALESCE(excluded.broker_confirmed_at,position_campaigns.broker_confirmed_at),
                       entry_price=COALESCE(excluded.entry_price,position_campaigns.entry_price),
                       current_stop=COALESCE(excluded.current_stop,position_campaigns.current_stop),
                       initial_quantity=MAX(position_campaigns.initial_quantity,excluded.initial_quantity),
                       current_quantity=excluded.current_quantity,core_quantity=excluded.core_quantity,
                       runner_quantity=excluded.runner_quantity,
                       reference_risk_dollars=COALESCE(excluded.reference_risk_dollars,position_campaigns.reference_risk_dollars),
                       high_water_price=MAX(COALESCE(position_campaigns.high_water_price,0),COALESCE(excluded.high_water_price,0)),
                       mfe_r=MAX(COALESCE(position_campaigns.mfe_r,0),COALESCE(excluded.mfe_r,0)),
                       mae_r=MIN(COALESCE(position_campaigns.mae_r,0),COALESCE(excluded.mae_r,0)),
                       continuation_health=excluded.continuation_health,
                       remaining_opportunity=excluded.remaining_opportunity,
                       last_action=excluded.last_action,next_actions_json=excluded.next_actions_json,
                       broker_state_json=excluded.broker_state_json""",
                (
                    campaign_id, payload["account_key"], payload["instrument_key"],
                    str(payload["symbol"]).upper(), thesis_key, direction,
                    str(payload["asset_class"]).lower(), status,
                    payload["strategy_version"], opened_at, now,
                    broker_confirmed_at, entry_price, original_stop,
                    current_stop, quantities["initial_quantity"],
                    quantities["current_quantity"], quantities["core_quantity"],
                    quantities["runner_quantity"], payload.get("reference_risk_dollars"),
                    payload.get("high_water_price"), payload.get("mfe_r"), payload.get("mae_r"),
                    str(payload.get("continuation_health") or "UNKNOWN").upper(),
                    str(payload.get("remaining_opportunity") or "UNKNOWN").upper(),
                    payload.get("last_action"),
                    json.dumps(payload.get("next_actions") or {}, separators=(",", ":")),
                    json.dumps(broker_state, separators=(",", ":")),
                ),
            )
            event_payload = {
                "campaign_id": campaign_id,
                "account_key": str(payload["account_key"]),
                "instrument_key": str(payload["instrument_key"]),
                "symbol": str(payload["symbol"]).upper(),
                "thesis_key": thesis_key,
                "direction": direction,
                "asset_class": str(payload["asset_class"]).lower(),
                "strategy_version": str(payload["strategy_version"]),
                "status": status,
                "observed_at": now,
                "broker_confirmed_at": broker_confirmed_at,
                "entry_price": entry_price,
                "original_stop": original_stop,
                "current_stop": current_stop,
                **quantities,
                "reference_risk_dollars": payload.get("reference_risk_dollars"),
                "high_water_price": payload.get("high_water_price"),
                "mfe_r": payload.get("mfe_r"),
                "mae_r": payload.get("mae_r"),
                "continuation_health": str(
                    payload.get("continuation_health") or "UNKNOWN"
                ).upper(),
                "remaining_opportunity": str(
                    payload.get("remaining_opportunity") or "UNKNOWN"
                ).upper(),
                "last_action": payload.get("last_action"),
                "next_actions": payload.get("next_actions") or {},
                "broker_state": broker_state,
            }
            event_hash = hashlib.sha256(
                json.dumps(
                    {key: value for key, value in event_payload.items() if key != "observed_at"},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            self.conn.execute(
                """INSERT OR IGNORE INTO position_campaign_events(
                       event_id,campaign_id,status,observed_at,broker_confirmed_at,
                       event_hash,payload_json
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    str(uuid.uuid4()), campaign_id, status, now, broker_confirmed_at,
                    event_hash, json.dumps(event_payload, separators=(",", ":")),
                ),
            )
        return campaign_id

    def position_campaigns(self, include_terminal: bool = False) -> list[dict[str, Any]]:
        terminal = ("CLOSED", "CANCELED", "REJECTED", "FAILED")
        if include_terminal:
            rows = self.conn.execute(
                "SELECT * FROM position_campaigns ORDER BY updated_at DESC"
            ).fetchall()
        else:
            placeholders = ",".join("?" for _ in terminal)
            rows = self.conn.execute(
                f"SELECT * FROM position_campaigns WHERE status NOT IN ({placeholders}) ORDER BY updated_at DESC",
                terminal,
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["next_actions"] = json.loads(item.pop("next_actions_json"))
            item["broker_state"] = json.loads(item.pop("broker_state_json"))
            item["trade_authority"] = False
            result.append(item)
        return result

    def position_campaign_events(self, campaign_id: str | None = None) -> list[dict[str, Any]]:
        if campaign_id:
            rows = self.conn.execute(
                """SELECT * FROM position_campaign_events
                   WHERE campaign_id=? ORDER BY observed_at ASC""",
                (campaign_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM position_campaign_events ORDER BY observed_at DESC"
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            item["trade_authority"] = False
            result.append(item)
        return result

    def upsert_risk_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Persist broker-confirmed account-day risk state with irreversible latches.

        This ledger does not grant order authority.  It makes the fixed -$100
        loss lock and the first +$150 objective crossing durable across prompt
        compaction, heartbeat overlap, and process restarts.
        """
        required = (
            "account_key",
            "session_date",
            "strategy_version",
            "start_of_day_equity",
            "baseline_confirmed_at",
            "current_equity",
            "realized_net_pnl",
            "confirmed_cash_flow_adjustment",
            "broker_confirmed_at",
        )
        missing = [field for field in required if payload.get(field) is None]
        if missing:
            raise ValueError(f"missing risk-session fields: {', '.join(missing)}")
        account_key = str(payload["account_key"]).strip()
        strategy_version = str(payload["strategy_version"]).strip()
        if not account_key or not strategy_version:
            raise ValueError("account_key and strategy_version cannot be empty")
        session_date = str(payload["session_date"])
        try:
            parsed_date = datetime.strptime(session_date, "%Y-%m-%d").date()
        except ValueError as error:
            raise ValueError("session_date must use YYYY-MM-DD") from error
        if parsed_date.isoformat() != session_date:
            raise ValueError("session_date must use YYYY-MM-DD")
        baseline_confirmed_at = _aware_timestamp(
            payload["baseline_confirmed_at"], "baseline_confirmed_at"
        )
        broker_confirmed_at = _aware_timestamp(
            payload["broker_confirmed_at"], "broker_confirmed_at"
        )
        start_equity = _finite_float(
            payload["start_of_day_equity"], "start_of_day_equity"
        )
        current_equity = _finite_float(payload["current_equity"], "current_equity")
        realized_net_pnl = _finite_float(
            payload["realized_net_pnl"], "realized_net_pnl"
        )
        cash_flow_adjustment = _finite_float(
            payload["confirmed_cash_flow_adjustment"],
            "confirmed_cash_flow_adjustment",
        )
        if start_equity <= 0 or current_equity <= 0:
            raise ValueError("broker-confirmed account equity must be positive")
        if payload.get("loss_limit_dollars", -100) != -100:
            raise ValueError("the account-day loss limit is fixed at -100 dollars")
        if payload.get("profit_objective_dollars", 150) != 150:
            raise ValueError("the primary daily profit objective is fixed at 150 dollars")
        broker_state = payload.get("broker_state") or {}
        if not isinstance(broker_state, dict) or not broker_state:
            raise ValueError("risk sessions require nonempty broker_state evidence")
        required_broker_checks = (
            "account_state_readable",
            "orders_reconciled",
            "positions_reconciled",
        )
        missing_checks = [
            field for field in required_broker_checks if broker_state.get(field) is not True
        ]
        if missing_checks:
            raise ValueError(
                "risk-session broker evidence is incomplete: " + ", ".join(missing_checks)
            )
        baseline_time = datetime.fromisoformat(baseline_confirmed_at)
        broker_time = datetime.fromisoformat(broker_confirmed_at)
        now_utc = datetime.now(timezone.utc)
        if baseline_time > broker_time:
            raise ValueError("baseline_confirmed_at may not follow broker_confirmed_at")
        if broker_time > now_utc + timedelta(seconds=15):
            raise ValueError("broker_confirmed_at may not be in the future")

        account_day_pnl = current_equity - start_equity - cash_flow_adjustment
        loss_gauge = min(account_day_pnl, realized_net_pnl)
        with self.transaction():
            existing = self.conn.execute(
                """SELECT * FROM risk_sessions
                   WHERE account_key=? AND session_date=?""",
                (account_key, session_date),
            ).fetchone()
            if existing:
                if abs(float(existing["start_of_day_equity"]) - start_equity) > 0.005:
                    raise ValueError("start_of_day_equity is immutable for the session")
                if str(existing["baseline_confirmed_at"]) != baseline_confirmed_at:
                    raise ValueError("baseline_confirmed_at is immutable for the session")
                if str(existing["strategy_version"]) != strategy_version:
                    raise ValueError("strategy_version is immutable for the session")
                if (
                    abs(
                        float(existing["confirmed_cash_flow_adjustment"])
                        - cash_flow_adjustment
                    )
                    > 0.005
                    and broker_state.get("cash_flow_confirmed") is not True
                ):
                    raise ValueError(
                        "cash-flow adjustment changes require broker confirmation evidence"
                    )
                if datetime.fromisoformat(broker_confirmed_at) < datetime.fromisoformat(
                    str(existing["broker_confirmed_at"])
                ):
                    raise ValueError("broker_confirmed_at may not move backward")
                if broker_confirmed_at == str(existing["broker_confirmed_at"]):
                    conflicting_same_timestamp = bool(
                        abs(float(existing["current_equity"]) - current_equity) > 0.005
                        or abs(float(existing["realized_net_pnl"]) - realized_net_pnl) > 0.005
                        or abs(
                            float(existing["confirmed_cash_flow_adjustment"])
                            - cash_flow_adjustment
                        ) > 0.005
                        or json.loads(str(existing["broker_state_json"])) != broker_state
                    )
                    if conflicting_same_timestamp:
                        raise ValueError(
                            "conflicting risk snapshots share broker_confirmed_at"
                        )
            existing_lock = bool(existing["loss_lock"]) if existing else False
            loss_lock = bool(
                existing_lock
                or loss_gauge <= -100
                or payload.get("loss_lock") is True
            )
            loss_lock_triggered_at = (
                str(existing["loss_lock_triggered_at"])
                if existing and existing["loss_lock_triggered_at"]
                else (broker_confirmed_at if loss_lock else None)
            )
            existing_objective = (
                bool(existing["profit_objective_reached"]) if existing else False
            )
            objective_reached = bool(
                existing_objective
                or account_day_pnl >= 150
                or payload.get("profit_objective_reached") is True
            )
            objective_reached_at = (
                str(existing["profit_objective_reached_at"])
                if existing and existing["profit_objective_reached_at"]
                else (broker_confirmed_at if objective_reached else None)
            )
            profit_floor = 125.0 if objective_reached else None
            now = utc_now()
            self.conn.execute(
                """INSERT INTO risk_sessions(
                       account_key,session_date,strategy_version,start_of_day_equity,
                       baseline_confirmed_at,current_equity,realized_net_pnl,
                       confirmed_cash_flow_adjustment,account_day_pnl,loss_gauge,
                       loss_limit_dollars,loss_lock,loss_lock_triggered_at,
                       profit_objective_dollars,profit_objective_reached,
                       profit_objective_reached_at,active_profit_floor_dollars,
                       updated_at,broker_confirmed_at,broker_state_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(account_key,session_date) DO UPDATE SET
                       current_equity=excluded.current_equity,
                       realized_net_pnl=excluded.realized_net_pnl,
                       confirmed_cash_flow_adjustment=excluded.confirmed_cash_flow_adjustment,
                       account_day_pnl=excluded.account_day_pnl,
                       loss_gauge=excluded.loss_gauge,
                       loss_lock=MAX(risk_sessions.loss_lock,excluded.loss_lock),
                       loss_lock_triggered_at=COALESCE(
                           risk_sessions.loss_lock_triggered_at,
                           excluded.loss_lock_triggered_at
                       ),
                       profit_objective_reached=MAX(
                           risk_sessions.profit_objective_reached,
                           excluded.profit_objective_reached
                       ),
                       profit_objective_reached_at=COALESCE(
                           risk_sessions.profit_objective_reached_at,
                           excluded.profit_objective_reached_at
                       ),
                       active_profit_floor_dollars=COALESCE(
                           risk_sessions.active_profit_floor_dollars,
                           excluded.active_profit_floor_dollars
                       ),
                       updated_at=excluded.updated_at,
                       broker_confirmed_at=excluded.broker_confirmed_at,
                       broker_state_json=excluded.broker_state_json""",
                (
                    account_key,
                    session_date,
                    strategy_version,
                    start_equity,
                    baseline_confirmed_at,
                    current_equity,
                    realized_net_pnl,
                    cash_flow_adjustment,
                    account_day_pnl,
                    loss_gauge,
                    -100.0,
                    int(loss_lock),
                    loss_lock_triggered_at,
                    150.0,
                    int(objective_reached),
                    objective_reached_at,
                    profit_floor,
                    now,
                    broker_confirmed_at,
                    json.dumps(broker_state, separators=(",", ":")),
                ),
            )
        row = self.risk_session(account_key, session_date)
        assert row is not None
        return row

    def risk_session(self, account_key: str, session_date: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """SELECT * FROM risk_sessions
               WHERE account_key=? AND session_date=?""",
            (account_key, session_date),
        ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["broker_state"] = json.loads(item.pop("broker_state_json"))
        item["loss_lock"] = bool(item["loss_lock"])
        item["profit_objective_reached"] = bool(item["profit_objective_reached"])
        item["post_objective_new_risk_buffer"] = (
            max(0.0, float(item["account_day_pnl"]) - 125.0)
            if item["profit_objective_reached"]
            else None
        )
        item["new_entries_allowed"] = bool(
            not item["loss_lock"]
            and (
                not item["profit_objective_reached"]
                or float(item["post_objective_new_risk_buffer"] or 0) > 0
            )
        )
        item["trade_authority"] = False
        return item

    def risk_sessions(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT account_key,session_date FROM risk_sessions
               ORDER BY session_date DESC,updated_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [
            item
            for row in rows
            if (
                item := self.risk_session(
                    str(row["account_key"]), str(row["session_date"])
                )
            )
            is not None
        ]

    def prune(self, retention_days: int) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        cutoff_ms = int(cutoff.timestamp() * 1000)
        with self.transaction():
            self.conn.execute("DELETE FROM bars_1m WHERE start_ms < ?", (cutoff_ms,))
            self.conn.execute("DELETE FROM bars_1s WHERE start_ms < ?", (cutoff_ms,))
            self.conn.execute(
                """DELETE FROM events
                   WHERE status != 'pending' AND acknowledged_at < ?""",
                (cutoff.isoformat(),),
            )
