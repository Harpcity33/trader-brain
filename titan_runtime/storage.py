from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Iterator
import uuid


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
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
                   CAST(json_extract(payload_json,'$.weighted_opportunity_score') AS REAL),
                   signal_strength
               ) DESC, dollar_volume DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def all_candidate_symbols(self) -> list[str]:
        rows = self.conn.execute(
            """SELECT symbol FROM candidates
               ORDER BY COALESCE(
                   CAST(json_extract(payload_json,'$.weighted_opportunity_score') AS REAL),
                   signal_strength
               ) DESC, dollar_volume DESC"""
        ).fetchall()
        return [str(row["symbol"]) for row in rows]

    def save_prepared_trade_plan(self, payload: dict[str, Any]) -> str | None:
        fingerprint = {
            key: payload.get(key)
            for key in (
                "symbol", "status", "direction", "lane", "setup", "trigger",
                "structural_stop", "t1", "t2", "weighted_opportunity_score",
                "modeled_move_capacity_pct", "blockers",
            )
        }
        import hashlib
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
        if event_type in {"LEADER_CANDIDATE", "MOMENTUM_WATCH", "BASE_READY", "TRIGGER_CROSS"}:
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
