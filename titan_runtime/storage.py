from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
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
    symbol TEXT NOT NULL,
    pilot_id TEXT NOT NULL DEFAULT 'legacy_unattributed',
    book_mode TEXT NOT NULL DEFAULT 'SHADOW' CHECK(book_mode IN ('LIVE','PAPER','SHADOW')),
    decision_contract_version TEXT NOT NULL DEFAULT 'legacy_unattributed',
    decision_contract_hash TEXT NOT NULL DEFAULT '0000000000000000000000000000000000000000000000000000000000000000',
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
    payload_json TEXT NOT NULL,
    PRIMARY KEY(pilot_id,book_mode,symbol)
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
    pilot_id TEXT NOT NULL DEFAULT 'legacy_unattributed',
    book_mode TEXT NOT NULL DEFAULT 'SHADOW' CHECK(book_mode IN ('LIVE','PAPER','SHADOW')),
    decision_contract_version TEXT NOT NULL DEFAULT 'legacy_unattributed',
    decision_contract_hash TEXT NOT NULL DEFAULT '0000000000000000000000000000000000000000000000000000000000000000',
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
    pilot_id TEXT NOT NULL DEFAULT 'legacy_unattributed',
    book_mode TEXT NOT NULL DEFAULT 'SHADOW' CHECK(book_mode IN ('LIVE','PAPER','SHADOW')),
    decision_contract_version TEXT NOT NULL DEFAULT 'legacy_unattributed',
    decision_contract_hash TEXT NOT NULL DEFAULT '0000000000000000000000000000000000000000000000000000000000000000',
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
    pilot_id TEXT NOT NULL DEFAULT 'legacy_unattributed',
    book_mode TEXT NOT NULL DEFAULT 'SHADOW' CHECK(book_mode IN ('LIVE','PAPER','SHADOW')),
    decision_contract_version TEXT NOT NULL DEFAULT 'legacy_unattributed',
    decision_contract_hash TEXT NOT NULL DEFAULT '0000000000000000000000000000000000000000000000000000000000000000',
    context_json TEXT NOT NULL,
    outcome_locked INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_entry_plans_date
    ON research_entry_plans(trade_date DESC, symbol);

CREATE TABLE IF NOT EXISTS research_entry_outcomes (
    plan_id TEXT PRIMARY KEY REFERENCES research_entry_plans(plan_id),
    pilot_id TEXT NOT NULL DEFAULT 'legacy_unattributed',
    book_mode TEXT NOT NULL DEFAULT 'SHADOW' CHECK(book_mode IN ('LIVE','PAPER','SHADOW')),
    decision_contract_version TEXT NOT NULL DEFAULT 'legacy_unattributed',
    decision_contract_hash TEXT NOT NULL DEFAULT '0000000000000000000000000000000000000000000000000000000000000000',
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
    pilot_id TEXT NOT NULL DEFAULT 'legacy_unattributed',
    book_mode TEXT NOT NULL DEFAULT 'LIVE' CHECK(book_mode IN ('LIVE','PAPER','SHADOW')),
    decision_contract_version TEXT NOT NULL DEFAULT 'legacy_unattributed',
    decision_contract_hash TEXT NOT NULL DEFAULT '0000000000000000000000000000000000000000000000000000000000000000',
    entry_submission_intent_id TEXT,
    entry_order_resolution_key TEXT,
    entry_order_acknowledged_at TEXT,
    entry_order_ack_deadline_at TEXT,
    entry_order_ack_state TEXT CHECK(entry_order_ack_state IS NULL OR entry_order_ack_state IN (
        'ON_TIME','LATE_CONFIRMED','UNKNOWN_RESOLVED'
    )),
    add_submission_intent_id TEXT,
    add_order_resolution_key TEXT,
    add_order_acknowledged_at TEXT,
    add_order_ack_deadline_at TEXT,
    add_order_ack_state TEXT CHECK(add_order_ack_state IS NULL OR add_order_ack_state IN (
        'ON_TIME','LATE_CONFIRMED','UNKNOWN_RESOLVED'
    )),
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
    pilot_id TEXT NOT NULL DEFAULT 'legacy_unattributed',
    book_mode TEXT NOT NULL DEFAULT 'LIVE' CHECK(book_mode IN ('LIVE','PAPER','SHADOW')),
    decision_contract_version TEXT NOT NULL DEFAULT 'legacy_unattributed',
    decision_contract_hash TEXT NOT NULL DEFAULT '0000000000000000000000000000000000000000000000000000000000000000',
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

CREATE TABLE IF NOT EXISTS risk_session_snapshots (
    account_key TEXT NOT NULL,
    session_date TEXT NOT NULL,
    broker_confirmed_at TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(account_key, session_date, broker_confirmed_at)
);
CREATE INDEX IF NOT EXISTS idx_risk_session_snapshots_date
    ON risk_session_snapshots(session_date DESC, broker_confirmed_at DESC);

CREATE TABLE IF NOT EXISTS risk_authorizations (
    authorization_id TEXT PRIMARY KEY,
    account_key TEXT NOT NULL,
    session_date TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    pilot_id TEXT NOT NULL DEFAULT 'legacy_unattributed',
    book_mode TEXT NOT NULL DEFAULT 'LIVE' CHECK(book_mode IN ('LIVE','PAPER','SHADOW')),
    decision_contract_version TEXT NOT NULL DEFAULT 'legacy_unattributed',
    decision_contract_hash TEXT NOT NULL DEFAULT '0000000000000000000000000000000000000000000000000000000000000000',
    instrument_key TEXT NOT NULL,
    thesis_key TEXT NOT NULL,
    risk_action TEXT NOT NULL CHECK(risk_action IN ('ENTRY','ADD')),
    status TEXT NOT NULL CHECK(status IN (
        'ACTIVE','SUBMISSION_UNKNOWN','CONSUMED','RECONCILED',
        'RECONCILED_NO_ORDER','RELEASED','EXPIRED'
    )),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    bound_at TEXT,
    reconciled_at TEXT,
    broker_order_id TEXT,
    campaign_id TEXT,
    release_reason TEXT,
    evidence_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_risk_authorizations_session
    ON risk_authorizations(account_key, session_date, status, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_risk_authorizations_one_active
    ON risk_authorizations(account_key, session_date)
    WHERE status='ACTIVE';
CREATE UNIQUE INDEX IF NOT EXISTS idx_risk_authorizations_one_pending
    ON risk_authorizations(account_key, session_date)
    WHERE status IN ('ACTIVE','SUBMISSION_UNKNOWN','CONSUMED');

CREATE TABLE IF NOT EXISTS risk_submission_intents (
    intent_id TEXT PRIMARY KEY,
    authorization_id TEXT NOT NULL UNIQUE,
    attempted_at TEXT NOT NULL,
    broker_ack_deadline_at TEXT NOT NULL,
    intent_hash TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS risk_submission_intents_no_update
BEFORE UPDATE ON risk_submission_intents
BEGIN
    SELECT RAISE(ABORT, 'risk submission intents are append-only');
END;

CREATE TRIGGER IF NOT EXISTS risk_submission_intents_no_delete
BEFORE DELETE ON risk_submission_intents
BEGIN
    SELECT RAISE(ABORT, 'risk submission intents are append-only');
END;

CREATE TABLE IF NOT EXISTS risk_unknown_resolutions (
    resolution_key TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL REFERENCES risk_submission_intents(intent_id),
    authorization_id TEXT NOT NULL UNIQUE,
    resolution_state TEXT NOT NULL CHECK(resolution_state IN (
        'ORDER_FOUND','NO_ORDER_CONFIRMED'
    )),
    broker_confirmed_at TEXT NOT NULL,
    broker_order_id TEXT,
    evidence_json TEXT NOT NULL,
    resolution_hash TEXT NOT NULL UNIQUE,
    recorded_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS risk_unknown_resolutions_no_update
BEFORE UPDATE ON risk_unknown_resolutions
BEGIN
    SELECT RAISE(ABORT, 'risk unknown resolutions are append-only');
END;

CREATE TRIGGER IF NOT EXISTS risk_unknown_resolutions_no_delete
BEFORE DELETE ON risk_unknown_resolutions
BEGIN
    SELECT RAISE(ABORT, 'risk unknown resolutions are append-only');
END;

CREATE TABLE IF NOT EXISTS daily_performance_grades (
    grade_id TEXT PRIMARY KEY,
    account_key TEXT NOT NULL,
    session_date TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    pilot_id TEXT NOT NULL DEFAULT 'legacy_unattributed',
    book_mode TEXT NOT NULL DEFAULT 'LIVE' CHECK(book_mode IN ('LIVE','PAPER','SHADOW')),
    decision_contract_version TEXT NOT NULL DEFAULT 'legacy_unattributed',
    decision_contract_hash TEXT NOT NULL DEFAULT '0000000000000000000000000000000000000000000000000000000000000000',
    revision INTEGER NOT NULL CHECK(revision >= 1),
    corrects_grade_id TEXT REFERENCES daily_performance_grades(grade_id),
    rubric_version TEXT NOT NULL,
    graded_at TEXT NOT NULL,
    broker_confirmed_at TEXT,
    recorded_at TEXT NOT NULL,
    payload_hash TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    evidence_coverage_pct REAL NOT NULL CHECK(
        evidence_coverage_pct >= 0 AND evidence_coverage_pct <= 100
    ),
    process_score REAL NOT NULL CHECK(process_score >= 0 AND process_score <= 100),
    outcome_score REAL NOT NULL CHECK(outcome_score >= 0 AND outcome_score <= 100),
    raw_overall_score REAL NOT NULL CHECK(
        raw_overall_score >= 0 AND raw_overall_score <= 100
    ),
    overall_score REAL CHECK(
        overall_score IS NULL OR (overall_score >= 0 AND overall_score <= 100)
    ),
    letter_grade TEXT NOT NULL CHECK(letter_grade IN (
        'A','A-','B+','B','B-','C+','C','C-','D','F','INCOMPLETE'
    )),
    grade_status TEXT NOT NULL CHECK(grade_status IN (
        'PENDING_RECONCILIATION','FINAL','INCOMPLETE'
    )),
    evidence_ceiling REAL,
    incomplete_reasons_json TEXT NOT NULL,
    hard_fail INTEGER NOT NULL CHECK(hard_fail IN (0,1)),
    hard_ceiling REAL,
    UNIQUE(account_key, session_date, strategy_version, revision)
);
CREATE INDEX IF NOT EXISTS idx_daily_performance_grades_session
    ON daily_performance_grades(session_date DESC, account_key, strategy_version, revision DESC);

CREATE TRIGGER IF NOT EXISTS daily_performance_grades_no_update
BEFORE UPDATE ON daily_performance_grades
BEGIN
    SELECT RAISE(ABORT, 'daily performance grades are append-only');
END;

CREATE TRIGGER IF NOT EXISTS daily_performance_grades_no_delete
BEFORE DELETE ON daily_performance_grades
BEGIN
    SELECT RAISE(ABORT, 'daily performance grades are append-only');
END;

CREATE TABLE IF NOT EXISTS pilot_fact_sheets (
    fact_sheet_id TEXT PRIMARY KEY,
    pilot_id TEXT NOT NULL,
    pilot_name TEXT NOT NULL,
    book_mode TEXT NOT NULL CHECK(book_mode IN ('LIVE','PAPER','SHADOW')),
    fact_sheet_version TEXT NOT NULL,
    decision_contract_version TEXT NOT NULL,
    decision_contract_hash TEXT NOT NULL,
    measured_through TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    payload_hash TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    UNIQUE(pilot_id, book_mode, fact_sheet_version)
);
CREATE INDEX IF NOT EXISTS idx_pilot_fact_sheets_latest
    ON pilot_fact_sheets(book_mode, pilot_id, measured_through DESC, recorded_at DESC);

CREATE TRIGGER IF NOT EXISTS pilot_fact_sheets_no_update
BEFORE UPDATE ON pilot_fact_sheets
BEGIN
    SELECT RAISE(ABORT, 'pilot fact sheets are append-only');
END;

CREATE TRIGGER IF NOT EXISTS pilot_fact_sheets_no_delete
BEFORE DELETE ON pilot_fact_sheets
BEGIN
    SELECT RAISE(ABORT, 'pilot fact sheets are append-only');
END;

CREATE TABLE IF NOT EXISTS operator_entry_stop (
    latch_key TEXT PRIMARY KEY CHECK(latch_key='GLOBAL'),
    engaged INTEGER NOT NULL CHECK(engaged IN (0,1)),
    generation INTEGER NOT NULL CHECK(generation >= 1),
    reason TEXT NOT NULL,
    changed_by TEXT NOT NULL,
    changed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS operator_entry_stop_events (
    event_id TEXT PRIMARY KEY,
    generation INTEGER NOT NULL UNIQUE,
    action TEXT NOT NULL CHECK(action IN ('INITIALIZED','ENGAGED','RELEASED')),
    reason TEXT NOT NULL,
    changed_by TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    previous_event_hash TEXT NOT NULL DEFAULT '0000000000000000000000000000000000000000000000000000000000000000',
    event_hash TEXT NOT NULL UNIQUE
);

CREATE TRIGGER IF NOT EXISTS operator_entry_stop_events_no_update
BEFORE UPDATE ON operator_entry_stop_events
BEGIN
    SELECT RAISE(ABORT, 'operator entry-stop events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS operator_entry_stop_events_no_delete
BEFORE DELETE ON operator_entry_stop_events
BEGIN
    SELECT RAISE(ABORT, 'operator entry-stop events are append-only');
END;
"""


PERFORMANCE_PROCESS_WEIGHT = Decimal("0.80")
PERFORMANCE_OUTCOME_WEIGHT = Decimal("0.20")
PERFORMANCE_SCORE_QUANTUM = Decimal("0.0001")
PERFORMANCE_RUBRIC_VERSION = "titan_daily_performance_2026-08-23_v1"
PERFORMANCE_RUBRIC = {
    "account_and_risk_integrity": ("process", Decimal("25")),
    "execution_and_protection": ("process", Decimal("15")),
    "causal_data_and_evidence": ("process", Decimal("15")),
    "opportunity_coverage_and_offense": ("process", Decimal("15")),
    "entry_quality_and_selectivity": ("process", Decimal("10")),
    "position_management_and_profit_capture": ("process", Decimal("12")),
    "audit_and_learning_quality": ("process", Decimal("8")),
    "broker_net_pnl_vs_objective_and_boundary": ("outcome", Decimal("40")),
    "net_r_after_execution_costs": ("outcome", Decimal("30")),
    "risk_weighted_after_cost_opportunity_capture": (
        "outcome", Decimal("30")
    ),
}
PERFORMANCE_MINIMUM_APPLICABLE_PROCESS_WEIGHT = Decimal("55")
PERFORMANCE_REQUIRED_FAILURE_MARKERS = {
    "unreconciled_broker_state",
    "order_lifecycle_or_quantity_defect",
    "unprotected_or_overlapping_exit_or_overnight",
    "prohibited_or_unauthorized_action",
    "loss_lock_violation",
    "fabricated_or_future_data",
}
PERFORMANCE_HARD_FAILURE_CEILINGS = {
    "order_lifecycle_or_quantity_defect": Decimal("59"),
    "unprotected_or_overlapping_exit_or_overnight": Decimal("39"),
    "prohibited_or_unauthorized_action": Decimal("0"),
    "loss_lock_violation": Decimal("0"),
    "fabricated_or_future_data": Decimal("0"),
}

BOOK_MODES = frozenset({"LIVE", "PAPER", "SHADOW"})
TITAN_LIVE_PILOT_ID = "titan_momentum_equity"
TITAN_LIVE_DECISION_CONTRACT_VERSION = "titan_momentum_equity_2026-08-23_v1"
TITAN_LIVE_DECISION_CONTRACT_HASH = (
    "dba59bd7fb006e5c0ec8fd31fb467076638ba6de9e30442fdc5411449c4d7c21"
)
PRETRADE_RISK_FACTS_SCHEMA_VERSION = (
    "titan_live_pretrade_risk_facts_2026-08-23_v1"
)
LEGACY_PILOT_ID = "legacy_unattributed"
LEGACY_DECISION_CONTRACT_VERSION = "legacy_unattributed"
ZERO_SHA256 = "0" * 64
PILOT_CONTRACT_VERSIONS = {
    "titan_momentum_equity": "titan_momentum_equity_2026-08-23_v1",
    "titan_catalyst_swing": "titan_catalyst_swing_2026-08-23_v1",
    "titan_defined_risk_options": "titan_defined_risk_options_2026-08-23_v1",
    "titan_crypto_momentum": "titan_crypto_momentum_2026-08-23_v1",
    "titan_equity_setup_challengers": (
        "titan_equity_setup_challengers_2026-08-23_v1"
    ),
}
PILOT_CONTRACT_HASHES = {
    "titan_momentum_equity": (
        "dba59bd7fb006e5c0ec8fd31fb467076638ba6de9e30442fdc5411449c4d7c21"
    ),
    "titan_catalyst_swing": (
        "1b1c1f1500d215639430251810a78cdcce60ecea72cf07848af5162d1e540d56"
    ),
    "titan_defined_risk_options": (
        "7734f8775f3c513c17120834f6954b5292c59b87b2fdb7a4a3298c2f40a49bbb"
    ),
    "titan_crypto_momentum": (
        "c359dd1d39f7a59e2046ee63d238c451d138335bee809ac1801ed56dca4fce0c"
    ),
    "titan_equity_setup_challengers": (
        "b099823f49b3b13d55e8063c98662b01c6ce4293313687aae5bde683dd060f88"
    ),
}
PILOT_ALLOWED_BOOK_MODES = {
    "titan_momentum_equity": frozenset({"LIVE", "PAPER", "SHADOW"}),
    "titan_catalyst_swing": frozenset({"SHADOW"}),
    "titan_defined_risk_options": frozenset({"SHADOW"}),
    "titan_crypto_momentum": frozenset({"SHADOW"}),
    "titan_equity_setup_challengers": frozenset({"SHADOW"}),
}


def _risk_authorization_hash_evidence(
    authorization: dict[str, Any],
) -> dict[str, Any]:
    """Return the immutable reserve-time facts covered by authorization_id.

    Submission timing is written later to the append-only submission-intent
    ledger.  Excluding those two enrichment fields keeps the reserve identity
    stable while the intent independently commits their exact values.
    """
    return {
        key: value
        for key, value in authorization.items()
        if key not in {
            "authorization_id",
            "submission_intent_at",
            "broker_ack_deadline_at",
        }
    }


def _canonical_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


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


def _finite_decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{field} must be a finite number") from error
    if not result.is_finite():
        raise ValueError(f"{field} must be a finite number")
    return result


def _bounded_score(value: Any, field: str) -> Decimal:
    result = _finite_decimal(value, field)
    if result < 0 or result > 100:
        raise ValueError(f"{field} must be within 0..100")
    return result


def _quantized_score(value: Decimal) -> float:
    return float(value.quantize(PERFORMANCE_SCORE_QUANTUM, rounding=ROUND_HALF_UP))


def _sha256_hex(value: Any, field: str, *, allow_zero: bool = False) -> str:
    digest = str(value or "").strip()
    if (
        len(digest) != 64
        or digest != digest.lower()
        or any(character not in "0123456789abcdef" for character in digest)
        or (not allow_zero and digest == ZERO_SHA256)
    ):
        qualifier = "nonzero " if not allow_zero else ""
        raise ValueError(f"{field} must be a {qualifier}64-lowercase-hex SHA-256")
    return digest


def _normalize_pilot_attribution(
    payload: dict[str, Any],
    *,
    allow_legacy: bool = False,
) -> dict[str, str]:
    """Validate immutable strategy identity without manufacturing a live hash.

    Legacy defaults are reserved for rows that predate this schema.  Callers
    creating current decision, risk, campaign, grade, or fact-sheet evidence
    must carry the exact hash of the version-controlled decision contract.
    """
    fields = (
        "pilot_id", "book_mode", "decision_contract_version",
        "decision_contract_hash",
    )
    if allow_legacy and all(payload.get(field) is None for field in fields):
        return {
            "pilot_id": LEGACY_PILOT_ID,
            "book_mode": "SHADOW",
            "decision_contract_version": LEGACY_DECISION_CONTRACT_VERSION,
            "decision_contract_hash": ZERO_SHA256,
        }
    missing = [field for field in fields if payload.get(field) in (None, "")]
    if missing:
        raise ValueError("missing pilot attribution fields: " + ", ".join(missing))
    pilot_id = str(payload["pilot_id"]).strip().lower()
    book_mode = str(payload["book_mode"]).strip().upper()
    contract_version = str(payload["decision_contract_version"]).strip()
    if not pilot_id or pilot_id == LEGACY_PILOT_ID:
        raise ValueError("new records require a non-legacy pilot_id")
    if pilot_id not in PILOT_CONTRACT_VERSIONS:
        raise ValueError("pilot_id is not registered")
    if book_mode not in BOOK_MODES:
        raise ValueError("book_mode must be LIVE, PAPER, or SHADOW")
    if book_mode == "LIVE" and pilot_id != TITAN_LIVE_PILOT_ID:
        raise ValueError("Titan Momentum Equity is the sole LIVE pilot")
    if book_mode not in PILOT_ALLOWED_BOOK_MODES[pilot_id]:
        raise ValueError(f"pilot {pilot_id} is not registered for {book_mode} mode")
    expected_contract_version = PILOT_CONTRACT_VERSIONS[pilot_id]
    if contract_version != expected_contract_version:
        raise ValueError(
            f"pilot {pilot_id} requires decision_contract_version "
            f"{expected_contract_version}"
        )
    contract_hash = _sha256_hex(
        payload["decision_contract_hash"], "decision_contract_hash"
    )
    if contract_hash != PILOT_CONTRACT_HASHES[pilot_id]:
        raise ValueError(
            f"pilot {pilot_id} requires the exact canonical decision_contract_hash"
        )
    return {
        "pilot_id": pilot_id,
        "book_mode": book_mode,
        "decision_contract_version": contract_version,
        "decision_contract_hash": contract_hash,
    }


def _row_pilot_attribution(row: sqlite3.Row | dict[str, Any]) -> dict[str, str]:
    keys = set(row.keys())
    return {
        "pilot_id": str(row["pilot_id"]) if "pilot_id" in keys else LEGACY_PILOT_ID,
        "book_mode": str(row["book_mode"]) if "book_mode" in keys else "SHADOW",
        "decision_contract_version": (
            str(row["decision_contract_version"])
            if "decision_contract_version" in keys
            else LEGACY_DECISION_CONTRACT_VERSION
        ),
        "decision_contract_hash": (
            str(row["decision_contract_hash"])
            if "decision_contract_hash" in keys
            else ZERO_SHA256
        ),
    }


def _validate_risk_gate_authorization(
    authorization: Any,
    *,
    account_key: str,
    instrument_key: str,
    symbol: str,
    direction: str,
    asset_class: str,
    thesis_key: str,
    strategy_version: str,
    pilot_id: str,
    book_mode: str,
    decision_contract_version: str,
    decision_contract_hash: str,
    expected_action: str,
    order_submitted_at: str,
) -> None:
    """Validate the exact pre-order risk-gate evidence carried into a campaign.

    The watcher still has no broker or order authority.  This check makes it
    impossible for an entry/add transition to silently omit the notional,
    downside, reserve, or fresh risk-gate decision that authorized submission.
    """
    if not isinstance(authorization, dict):
        raise ValueError(
            "broker_state.risk_gate_authorization must be an object"
        )
    required = (
        "authorization_id", "schema_version", "account_key", "session_date",
        "strategy_version",
        "pilot_id", "book_mode", "decision_contract_version",
        "decision_contract_hash",
        "broker_confirmed_at", "broker_snapshot_hash",
        "broker_snapshot_valid_until", "checked_at",
        "reservation_expires_at",
        "reservation_scope", "current_equity_dollars",
        "instrument_key", "symbol", "direction", "asset_class",
        "thesis_key", "risk_action",
        "preview_id", "preview_confirmed_at", "preview_account_key",
        "preview_instrument_key", "preview_side", "preview_order_quantity",
        "preview_limit_price", "preview_equity_dollars",
        "preview_current_gross_exposure_dollars",
        "preview_working_entry_notional_dollars",
        "preview_projected_cost_dollars",
        "reviewed_entry_price", "structural_stop_price", "quantity",
        "contract_multiplier", "modeled_execution_loss_dollars",
        "stress_tail_loss_dollars", "reviewed_notional_dollars",
        "estimated_slippage_dollars",
        "maximum_acceptable_slippage_dollars",
        "maximum_contractual_loss_dollars",
        "notional_pct_of_current_equity",
        "calculated_stop_defined_loss_dollars", "proposed_new_risk_dollars",
        "existing_open_downside_dollars", "existing_pending_risk_dollars",
        "execution_reserve_dollars",
        "unleveraged_buying_power_dollars",
        "expected_unleveraged_buying_power_dollars",
        "buying_power_mismatch_dollars",
        "buying_power_mismatch_tolerance_dollars",
        "buying_power_mismatch_detected",
        "emergency_entry_stop_generation",
        "emergency_entry_stop_state_hash",
        "broker_ack_timeout_seconds",
        "current_gross_exposure_dollars",
        "working_entry_notional_dollars",
        "broker_new_notional_capacity_dollars",
        "post_order_gross_exposure_dollars",
        "projected_remaining_buying_power_dollars",
        "account_day_loss_headroom_dollars",
        "uncredited_open_profit_dollars",
        "open_loss_gauge_degradation_dollars",
        "loss_lock_new_risk_capacity_dollars",
        "profit_floor_new_risk_capacity_dollars",
        "dynamic_new_risk_capacity_dollars",
        "submission_intent_at", "broker_ack_deadline_at",
    )
    missing = [field for field in required if field not in authorization]
    if missing:
        raise ValueError(
            "risk-gate authorization is missing: " + ", ".join(missing)
        )
    expected_id = _canonical_hash(_risk_authorization_hash_evidence(authorization))
    if str(authorization["authorization_id"]) != expected_id:
        raise ValueError("risk-gate authorization hash does not match its evidence")
    attribution = _normalize_pilot_attribution(authorization)
    if attribution["book_mode"] != "LIVE":
        raise ValueError("PAPER/SHADOW pilots cannot receive broker risk authorization")
    if authorization["schema_version"] != PRETRADE_RISK_FACTS_SCHEMA_VERSION:
        raise ValueError("risk-gate authorization schema_version is invalid")
    _sha256_hex(
        authorization["broker_snapshot_hash"],
        "risk authorization broker_snapshot_hash",
    )
    symbol = str(authorization["symbol"]).strip().upper()
    direction = str(authorization["direction"]).strip().upper()
    asset_class = str(authorization["asset_class"]).strip().upper()
    if not symbol:
        raise ValueError("risk-gate authorization symbol cannot be empty")
    if direction not in {"UP", "DOWN"}:
        raise ValueError("risk-gate authorization direction must be UP or DOWN")
    if asset_class not in {"EQUITY", "OPTION"}:
        raise ValueError("risk-gate authorization asset_class must be EQUITY or OPTION")
    exact_identity = {
        "account_key": account_key,
        "instrument_key": instrument_key,
        "symbol": symbol,
        "direction": direction,
        "asset_class": asset_class,
        "thesis_key": thesis_key,
        "strategy_version": strategy_version,
        "pilot_id": pilot_id,
        "book_mode": book_mode,
        "decision_contract_version": decision_contract_version,
        "decision_contract_hash": decision_contract_hash,
        "risk_action": expected_action,
    }
    for field, expected in exact_identity.items():
        actual = str(authorization[field])
        if field in {
            "symbol", "direction", "asset_class", "thesis_key",
            "risk_action", "book_mode",
        }:
            actual = actual.upper()
        if actual != expected:
            raise ValueError(
                f"risk-gate authorization {field} does not match campaign"
            )
    checked_at = datetime.fromisoformat(
        _aware_timestamp(authorization["checked_at"], "risk authorization checked_at")
    )
    expires_at = datetime.fromisoformat(
        _aware_timestamp(
            authorization["reservation_expires_at"],
            "risk authorization reservation_expires_at",
        )
    )
    snapshot_valid_until = datetime.fromisoformat(
        _aware_timestamp(
            authorization["broker_snapshot_valid_until"],
            "risk authorization broker_snapshot_valid_until",
        )
    )
    broker_confirmed_at = datetime.fromisoformat(
        _aware_timestamp(
            authorization["broker_confirmed_at"],
            "risk authorization broker_confirmed_at",
        )
    )
    preview_confirmed_at = datetime.fromisoformat(
        _aware_timestamp(
            authorization["preview_confirmed_at"],
            "risk authorization preview_confirmed_at",
        )
    )
    if not str(authorization["preview_id"]).strip():
        raise ValueError("risk-gate authorization preview_id cannot be empty")
    if str(authorization["preview_account_key"]) != account_key:
        raise ValueError("risk-gate preview account does not match authorization")
    if str(authorization["preview_instrument_key"]) != instrument_key:
        raise ValueError("risk-gate preview instrument does not match authorization")
    if str(authorization["preview_side"]).strip().upper() != "BUY":
        raise ValueError("risk-gate preview side must be BUY")
    if preview_confirmed_at < broker_confirmed_at:
        raise ValueError("risk-gate preview predates the broker risk snapshot")
    if preview_confirmed_at > checked_at + timedelta(seconds=15):
        raise ValueError("risk-gate preview timestamp is in the future")
    if str(authorization["reservation_scope"]) != "one_active_per_account_session":
        raise ValueError("risk-gate authorization has an invalid reservation scope")
    if broker_confirmed_at > checked_at:
        raise ValueError("risk-gate authorization predates its broker snapshot")
    if checked_at >= snapshot_valid_until:
        raise ValueError("risk-gate authorization uses an expired broker snapshot")
    if expires_at <= checked_at or expires_at - checked_at > timedelta(seconds=180):
        raise ValueError("risk-gate authorization has an invalid reservation lease")
    if expires_at > snapshot_valid_until + timedelta(milliseconds=1):
        raise ValueError("risk authorization outlives its broker snapshot")
    submitted_at = datetime.fromisoformat(
        _aware_timestamp(order_submitted_at, "broker order_submitted_at")
    )
    submission_intent_at = authorization["submission_intent_at"]
    broker_ack_deadline_at = authorization["broker_ack_deadline_at"]
    if (submission_intent_at is None) != (broker_ack_deadline_at is None):
        raise ValueError(
            "submission_intent_at and broker_ack_deadline_at must both be null or set"
        )
    if submission_intent_at is not None:
        intent_time = datetime.fromisoformat(
            _aware_timestamp(submission_intent_at, "submission_intent_at")
        )
        deadline_time = datetime.fromisoformat(
            _aware_timestamp(broker_ack_deadline_at, "broker_ack_deadline_at")
        )
        if intent_time != submitted_at:
            raise ValueError("submission intent timestamp must equal broker submission time")
        if deadline_time != intent_time + timedelta(
            seconds=int(authorization["broker_ack_timeout_seconds"])
        ):
            raise ValueError("broker ack deadline does not match immutable intent")
    if checked_at > submitted_at + timedelta(seconds=15):
        raise ValueError("risk-gate authorization cannot postdate broker submission")
    if submitted_at - checked_at > timedelta(seconds=180):
        raise ValueError("risk-gate authorization is too old for broker submission")
    if submitted_at > expires_at:
        raise ValueError("broker submission occurred after risk authorization expired")
    current_equity = _finite_float(
        authorization["current_equity_dollars"],
        "risk authorization current_equity_dollars",
    )
    entry_price = _finite_float(
        authorization["reviewed_entry_price"],
        "risk authorization reviewed_entry_price",
    )
    stop_price = _finite_float(
        authorization["structural_stop_price"],
        "risk authorization structural_stop_price",
    )
    quantity = _finite_float(
        authorization["quantity"], "risk authorization quantity"
    )
    multiplier = _finite_float(
        authorization["contract_multiplier"],
        "risk authorization contract_multiplier",
    )
    modeled_execution_loss = _finite_float(
        authorization["modeled_execution_loss_dollars"],
        "risk authorization modeled_execution_loss_dollars",
    )
    stress_tail_loss = _finite_float(
        authorization["stress_tail_loss_dollars"],
        "risk authorization stress_tail_loss_dollars",
    )
    reviewed_notional = _finite_float(
        authorization["reviewed_notional_dollars"],
        "risk authorization reviewed_notional_dollars",
    )
    estimated_slippage = _finite_float(
        authorization["estimated_slippage_dollars"],
        "risk authorization estimated_slippage_dollars",
    )
    maximum_acceptable_slippage = _finite_float(
        authorization["maximum_acceptable_slippage_dollars"],
        "risk authorization maximum_acceptable_slippage_dollars",
    )
    raw_maximum_contractual_loss = authorization[
        "maximum_contractual_loss_dollars"
    ]
    if asset_class == "EQUITY":
        if raw_maximum_contractual_loss is not None:
            raise ValueError(
                "EQUITY maximum_contractual_loss_dollars must be null"
            )
        maximum_contractual_loss: float | None = None
    else:
        maximum_contractual_loss = _finite_float(
            raw_maximum_contractual_loss,
            "risk authorization maximum_contractual_loss_dollars",
        )
    notional_pct_equity = _finite_float(
        authorization["notional_pct_of_current_equity"],
        "risk authorization notional_pct_of_current_equity",
    )
    stop_defined_loss = _finite_float(
        authorization["calculated_stop_defined_loss_dollars"],
        "risk authorization calculated_stop_defined_loss_dollars",
    )
    proposed_risk = _finite_float(
        authorization["proposed_new_risk_dollars"],
        "risk authorization proposed_new_risk_dollars",
    )
    reserve = _finite_float(
        authorization["execution_reserve_dollars"],
        "risk authorization execution_reserve_dollars",
    )
    capacity = _finite_float(
        authorization["dynamic_new_risk_capacity_dollars"],
        "risk authorization dynamic_new_risk_capacity_dollars",
    )
    expected_buying_power = _finite_float(
        authorization["expected_unleveraged_buying_power_dollars"],
        "risk authorization expected_unleveraged_buying_power_dollars",
    )
    projected_remaining_buying_power = _finite_float(
        authorization["projected_remaining_buying_power_dollars"],
        "risk authorization projected_remaining_buying_power_dollars",
    )
    account_day_loss_headroom = _finite_float(
        authorization["account_day_loss_headroom_dollars"],
        "risk authorization account_day_loss_headroom_dollars",
    )
    buying_power_mismatch = _finite_float(
        authorization["buying_power_mismatch_dollars"],
        "risk authorization buying_power_mismatch_dollars",
    )
    buying_power_tolerance = _finite_float(
        authorization["buying_power_mismatch_tolerance_dollars"],
        "risk authorization buying_power_mismatch_tolerance_dollars",
    )
    ack_timeout = _finite_decimal(
        authorization["broker_ack_timeout_seconds"],
        "risk authorization broker_ack_timeout_seconds",
    )
    if ack_timeout != ack_timeout.to_integral_value() or not 1 <= ack_timeout <= 30:
        raise ValueError("risk authorization broker_ack_timeout_seconds must be 1..30")
    stop_generation = _finite_decimal(
        authorization["emergency_entry_stop_generation"],
        "risk authorization emergency_entry_stop_generation",
    )
    if stop_generation < 1 or stop_generation != stop_generation.to_integral_value():
        raise ValueError("risk authorization emergency entry-stop generation is invalid")
    _sha256_hex(
        authorization["emergency_entry_stop_state_hash"],
        "risk authorization emergency_entry_stop_state_hash",
    )
    for field in (
        "existing_open_downside_dollars", "existing_pending_risk_dollars",
        "uncredited_open_profit_dollars",
        "open_loss_gauge_degradation_dollars",
        "loss_lock_new_risk_capacity_dollars",
        "unleveraged_buying_power_dollars",
        "current_gross_exposure_dollars",
        "working_entry_notional_dollars",
        "broker_new_notional_capacity_dollars",
        "post_order_gross_exposure_dollars",
    ):
        if _finite_float(authorization[field], f"risk authorization {field}") < 0:
            raise ValueError(f"risk authorization {field} cannot be negative")
    floor_capacity = authorization["profit_floor_new_risk_capacity_dollars"]
    if floor_capacity is not None and _finite_float(
        floor_capacity, "risk authorization profit_floor_new_risk_capacity_dollars"
    ) < 0:
        raise ValueError(
            "risk authorization profit_floor_new_risk_capacity_dollars cannot be negative"
        )
    if (
        current_equity <= 0 or entry_price <= 0 or stop_price <= 0
        or quantity <= 0 or stop_price >= entry_price
    ):
        raise ValueError("risk-gate authorization has invalid entry geometry")
    if multiplier not in {1.0, 100.0}:
        raise ValueError("risk-gate authorization multiplier must be 1 or 100")
    if modeled_execution_loss < 0 or stress_tail_loss < 0:
        raise ValueError("risk-gate execution and stress losses cannot be negative")
    if maximum_acceptable_slippage < modeled_execution_loss:
        raise ValueError(
            "modeled execution loss exceeds maximum acceptable slippage"
        )
    expected_notional = entry_price * quantity * multiplier
    expected_stop_loss = (
        (entry_price - stop_price) * quantity * multiplier
        + modeled_execution_loss
    )
    expected_proposed_risk = max(expected_stop_loss, stress_tail_loss)
    expected_contractual_loss = expected_notional + modeled_execution_loss
    expected_notional_pct = expected_notional / current_equity * 100
    buying_power = float(authorization["unleveraged_buying_power_dollars"])
    expected_mismatch = abs(buying_power - expected_buying_power)
    preview_numeric_pairs = (
        (
            _finite_float(
                authorization["preview_order_quantity"],
                "risk authorization preview_order_quantity",
            ),
            quantity,
            "preview quantity",
        ),
        (
            _finite_float(
                authorization["preview_limit_price"],
                "risk authorization preview_limit_price",
            ),
            entry_price,
            "preview limit price",
        ),
        (
            _finite_float(
                authorization["preview_equity_dollars"],
                "risk authorization preview_equity_dollars",
            ),
            current_equity,
            "preview equity",
        ),
        (
            _finite_float(
                authorization["preview_current_gross_exposure_dollars"],
                "risk authorization preview_current_gross_exposure_dollars",
            ),
            float(authorization["current_gross_exposure_dollars"]),
            "preview gross exposure",
        ),
        (
            _finite_float(
                authorization["preview_working_entry_notional_dollars"],
                "risk authorization preview_working_entry_notional_dollars",
            ),
            float(authorization["working_entry_notional_dollars"]),
            "preview working notional",
        ),
        (
            _finite_float(
                authorization["preview_projected_cost_dollars"],
                "risk authorization preview_projected_cost_dollars",
            ),
            reviewed_notional,
            "preview projected cost",
        ),
    )
    for actual_preview, expected_preview, label in preview_numeric_pairs:
        if abs(actual_preview - expected_preview) > 0.005:
            raise ValueError(f"risk-gate authorization {label} is inconsistent")
    mismatch_detected = authorization["buying_power_mismatch_detected"]
    if not isinstance(mismatch_detected, bool):
        raise ValueError("risk authorization buying_power_mismatch_detected must be boolean")
    if buying_power_tolerance < 0:
        raise ValueError("risk authorization buying-power tolerance cannot be negative")
    expected_buying_power_tolerance = max(5.0, 0.01 * current_equity)
    if abs(buying_power_tolerance - expected_buying_power_tolerance) > 0.005:
        raise ValueError(
            "risk authorization buying-power tolerance must equal max($5, 1% equity)"
        )
    if abs(buying_power_mismatch - expected_mismatch) > 0.005:
        raise ValueError("risk authorization buying-power mismatch is inconsistent")
    if mismatch_detected != (expected_mismatch > buying_power_tolerance + 0.005):
        raise ValueError("risk authorization buying-power mismatch flag is inconsistent")
    if mismatch_detected:
        raise ValueError("risk authorization cannot proceed with buying-power mismatch")
    gross_exposure = float(authorization["current_gross_exposure_dollars"])
    working_notional = float(authorization["working_entry_notional_dollars"])
    expected_notional_capacity = min(
        buying_power,
        max(0.0, current_equity - gross_exposure - working_notional),
    )
    expected_post_order_gross = gross_exposure + working_notional + expected_notional
    expected_remaining_buying_power = expected_buying_power - expected_notional
    calculated_pairs = (
        (reviewed_notional, expected_notional, "reviewed notional"),
        (estimated_slippage, modeled_execution_loss, "estimated slippage"),
        (notional_pct_equity, expected_notional_pct, "notional percent of equity"),
        (stop_defined_loss, expected_stop_loss, "stop-defined loss"),
        (proposed_risk, expected_proposed_risk, "proposed risk"),
        (
            float(authorization["broker_new_notional_capacity_dollars"]),
            expected_notional_capacity,
            "broker notional capacity",
        ),
        (
            float(authorization["post_order_gross_exposure_dollars"]),
            expected_post_order_gross,
            "post-order gross exposure",
        ),
        (
            projected_remaining_buying_power,
            expected_remaining_buying_power,
            "projected remaining buying power",
        ),
    )
    if asset_class == "OPTION":
        assert maximum_contractual_loss is not None
        if abs(maximum_contractual_loss - expected_contractual_loss) > 0.005:
            raise ValueError(
                "risk-gate authorization maximum contractual loss is inconsistent"
            )
    for actual, expected, label in calculated_pairs:
        if abs(actual - expected) > 0.005:
            raise ValueError(f"risk-gate authorization {label} is inconsistent")
    if reviewed_notional <= 0 or proposed_risk <= 0:
        raise ValueError("risk-gate authorization requires positive notional and risk")
    if reviewed_notional > expected_notional_capacity + 0.005:
        raise ValueError("risk-gate authorization exceeds broker notional capacity")
    if reviewed_notional > expected_buying_power + 0.005:
        raise ValueError("risk-gate authorization exceeds preview buying power")
    if projected_remaining_buying_power < -0.005:
        raise ValueError("risk-gate authorization projects negative buying power")
    if account_day_loss_headroom < 0:
        raise ValueError("risk-gate account-day loss headroom cannot be negative")
    if reserve < 5:
        raise ValueError("risk-gate authorization requires at least a $5 reserve")
    if capacity < 0 or proposed_risk > capacity + 0.005:
        raise ValueError("risk-gate authorization exceeds dynamic risk capacity")


def _validate_risk_authorization_against_session(
    connection: sqlite3.Connection,
    authorization: dict[str, Any],
    *,
    account_key: str,
    strategy_version: str,
) -> None:
    """Bind a new order authorization to the exact durable broker snapshot."""
    snapshot_row = connection.execute(
        """SELECT snapshot_hash,snapshot_json FROM risk_session_snapshots
           WHERE account_key=? AND session_date=? AND broker_confirmed_at=?""",
        (
            account_key,
            str(authorization["session_date"]),
            _aware_timestamp(
                authorization["broker_confirmed_at"],
                "risk authorization broker_confirmed_at",
            ),
        ),
    ).fetchone()
    if not snapshot_row:
        raise ValueError(
            "risk-gate authorization has no matching immutable risk snapshot"
        )
    risk_row = json.loads(str(snapshot_row["snapshot_json"]))
    if str(authorization["broker_snapshot_hash"]) != str(
        snapshot_row["snapshot_hash"]
    ):
        raise ValueError(
            "risk-gate authorization broker_snapshot_hash does not match risk session"
        )
    if str(risk_row["strategy_version"]) != strategy_version:
        raise ValueError(
            "risk-gate authorization strategy does not match risk session"
        )
    attribution = _normalize_pilot_attribution(authorization)
    for field, expected in attribution.items():
        if str(risk_row.get(field)) != expected:
            raise ValueError(
                f"risk-gate authorization {field} does not match risk session"
            )
    latest_row = connection.execute(
        """SELECT loss_lock FROM risk_sessions
           WHERE account_key=? AND session_date=?""",
        (account_key, str(authorization["session_date"])),
    ).fetchone()
    if not latest_row or bool(latest_row["loss_lock"]):
        raise ValueError(
            "risk-gate authorization cannot bind without a current unlocked session"
        )
    if _aware_timestamp(
        authorization["broker_confirmed_at"],
        "risk authorization broker_confirmed_at",
    ) != str(risk_row["broker_confirmed_at"]):
        raise ValueError(
            "risk-gate authorization broker timestamp does not match risk session"
        )
    if abs(
        float(authorization["current_equity_dollars"])
        - float(risk_row["current_equity"])
    ) > 0.005:
        raise ValueError("risk-gate authorization equity does not match risk session")
    risk_broker_state = risk_row["broker_state"]
    for field in (
        "unleveraged_buying_power_dollars",
        "current_gross_exposure_dollars",
        "working_entry_notional_dollars",
    ):
        if abs(
            float(authorization[field]) - float(risk_broker_state[field])
        ) > 0.005:
            raise ValueError(
                f"risk-gate authorization {field} does not match risk session"
            )
    uncredited_open_profit = max(
        0.0,
        float(risk_row["account_day_pnl"]) - float(risk_row["loss_gauge"]),
    )
    open_degradation = max(
        0.0,
        float(authorization["existing_open_downside_dollars"])
        - uncredited_open_profit,
    )
    loss_headroom = max(
        0.0,
        float(risk_row["loss_gauge"])
        - float(risk_row["loss_limit_dollars"]),
    )
    if abs(
        float(authorization["account_day_loss_headroom_dollars"])
        - loss_headroom
    ) > 0.005:
        raise ValueError(
            "risk-gate authorization account-day loss headroom does not match session"
        )
    loss_capacity = max(
        0.0,
        loss_headroom
        - open_degradation
        - float(authorization["existing_pending_risk_dollars"])
        - float(authorization["execution_reserve_dollars"]),
    )
    floor_capacity = None
    if bool(risk_row["profit_objective_reached"]):
        floor_capacity = max(
            0.0,
            float(risk_row["account_day_pnl"]) - 125.0
            - float(authorization["existing_open_downside_dollars"])
            - float(authorization["existing_pending_risk_dollars"])
            - float(authorization["execution_reserve_dollars"]),
        )
    dynamic_capacity = (
        min(loss_capacity, floor_capacity)
        if floor_capacity is not None
        else loss_capacity
    )
    session_pairs = (
        (
            float(authorization["uncredited_open_profit_dollars"]),
            uncredited_open_profit,
            "uncredited open profit",
        ),
        (
            float(authorization["open_loss_gauge_degradation_dollars"]),
            open_degradation,
            "open loss-gauge degradation",
        ),
        (
            float(authorization["loss_lock_new_risk_capacity_dollars"]),
            loss_capacity,
            "loss-lock capacity",
        ),
        (
            authorization["profit_floor_new_risk_capacity_dollars"],
            floor_capacity,
            "profit-floor capacity",
        ),
        (
            float(authorization["dynamic_new_risk_capacity_dollars"]),
            dynamic_capacity,
            "dynamic capacity",
        ),
    )
    for actual, expected, label in session_pairs:
        if actual is None or expected is None:
            if actual is not expected:
                raise ValueError(
                    f"risk-gate authorization {label} does not match risk session"
                )
        elif abs(float(actual) - float(expected)) > 0.005:
            raise ValueError(
                f"risk-gate authorization {label} does not match risk session"
            )


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate_phase_one_schema()

    def _migrate_phase_one_schema(self) -> None:
        """Add pilot attribution to existing databases without rewriting history."""
        legacy_live_columns = {
            "pilot_id": "TEXT NOT NULL DEFAULT 'legacy_unattributed'",
            "book_mode": "TEXT NOT NULL DEFAULT 'LIVE'",
            "decision_contract_version": (
                "TEXT NOT NULL DEFAULT 'legacy_unattributed'"
            ),
            "decision_contract_hash": (
                "TEXT NOT NULL DEFAULT "
                "'0000000000000000000000000000000000000000000000000000000000000000'"
            ),
        }
        legacy_shadow_columns = {
            **legacy_live_columns,
            "book_mode": "TEXT NOT NULL DEFAULT 'SHADOW'",
        }
        migrations = {
            "candidates": legacy_shadow_columns,
            "prepared_trade_plans": legacy_shadow_columns,
            "event_decisions": legacy_shadow_columns,
            "research_entry_plans": legacy_shadow_columns,
            "research_entry_outcomes": legacy_shadow_columns,
            "position_campaigns": legacy_live_columns,
            "risk_sessions": legacy_live_columns,
            "risk_authorizations": legacy_live_columns,
            "daily_performance_grades": legacy_live_columns,
        }
        with self.transaction():
            for table, definitions in migrations.items():
                existing = {
                    str(row["name"])
                    for row in self.conn.execute(f"PRAGMA table_info({table})")
                }
                for column, definition in definitions.items():
                    if column not in existing:
                        self.conn.execute(
                            f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                        )
            campaign_columns = {
                "entry_submission_intent_id": "TEXT",
                "entry_order_resolution_key": "TEXT",
                "entry_order_acknowledged_at": "TEXT",
                "entry_order_ack_deadline_at": "TEXT",
                "entry_order_ack_state": "TEXT",
                "add_submission_intent_id": "TEXT",
                "add_order_resolution_key": "TEXT",
                "add_order_acknowledged_at": "TEXT",
                "add_order_ack_deadline_at": "TEXT",
                "add_order_ack_state": "TEXT",
            }
            existing_campaign_columns = {
                str(row["name"])
                for row in self.conn.execute(
                    "PRAGMA table_info(position_campaigns)"
                )
            }
            for column, definition in campaign_columns.items():
                if column not in existing_campaign_columns:
                    self.conn.execute(
                        f"ALTER TABLE position_campaigns ADD COLUMN {column} {definition}"
                    )
            stop_event_columns = {
                str(row["name"])
                for row in self.conn.execute(
                    "PRAGMA table_info(operator_entry_stop_events)"
                )
            }
            if "previous_event_hash" not in stop_event_columns:
                self.conn.execute(
                    """ALTER TABLE operator_entry_stop_events
                       ADD COLUMN previous_event_hash TEXT NOT NULL DEFAULT
                       '0000000000000000000000000000000000000000000000000000000000000000'"""
                )
            candidate_sql_row = self.conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='candidates'"
            ).fetchone()
            candidate_sql = str(candidate_sql_row["sql"] or "").replace(" ", "")
            if "PRIMARYKEY(pilot_id,book_mode,symbol)" not in candidate_sql:
                self.conn.execute("ALTER TABLE candidates RENAME TO candidates_legacy")
                self.conn.execute(
                    """CREATE TABLE candidates (
                           symbol TEXT NOT NULL,pilot_id TEXT NOT NULL,
                           book_mode TEXT NOT NULL CHECK(book_mode IN ('LIVE','PAPER','SHADOW')),
                           decision_contract_version TEXT NOT NULL,
                           decision_contract_hash TEXT NOT NULL,observed_at TEXT NOT NULL,
                           state TEXT NOT NULL,lane TEXT NOT NULL,
                           signal_strength REAL NOT NULL,price REAL NOT NULL,gap_pct REAL,
                           dollar_volume REAL,volume_acceleration REAL,price_acceleration REAL,
                           relative_volume REAL,spread_pct REAL,short_atr REAL,base_high REAL,
                           support REAL,invalidation REAL,limit_ceiling REAL,extension_atr REAL,
                           quote_fresh INTEGER NOT NULL DEFAULT 0,
                           preliminary_liquidity_pass INTEGER NOT NULL DEFAULT 0,
                           catalyst_required INTEGER NOT NULL DEFAULT 1,
                           payload_json TEXT NOT NULL,
                           PRIMARY KEY(pilot_id,book_mode,symbol)
                       )"""
                )
                self.conn.execute(
                    """INSERT INTO candidates SELECT
                           symbol,pilot_id,book_mode,decision_contract_version,
                           decision_contract_hash,observed_at,state,lane,signal_strength,
                           price,gap_pct,dollar_volume,volume_acceleration,price_acceleration,
                           relative_volume,spread_pct,short_atr,base_high,support,invalidation,
                           limit_ceiling,extension_atr,quote_fresh,
                           preliminary_liquidity_pass,catalyst_required,payload_json
                       FROM candidates_legacy"""
                )
                self.conn.execute("DROP TABLE candidates_legacy")
            self.conn.execute("DROP INDEX IF EXISTS idx_candidates_rank")
            self.conn.execute(
                """CREATE INDEX idx_candidates_rank
                   ON candidates(book_mode,pilot_id,signal_strength DESC,dollar_volume DESC)"""
            )
            risk_table_sql_row = self.conn.execute(
                """SELECT sql FROM sqlite_master
                   WHERE type='table' AND name='risk_authorizations'"""
            ).fetchone()
            risk_table_sql = str(risk_table_sql_row["sql"] or "")
            if "SUBMISSION_UNKNOWN" not in risk_table_sql:
                self.conn.execute(
                    "ALTER TABLE risk_authorizations RENAME TO risk_authorizations_legacy"
                )
                self.conn.execute(
                    """CREATE TABLE risk_authorizations (
                           authorization_id TEXT PRIMARY KEY,
                           account_key TEXT NOT NULL,
                           session_date TEXT NOT NULL,
                           strategy_version TEXT NOT NULL,
                           pilot_id TEXT NOT NULL,
                           book_mode TEXT NOT NULL CHECK(book_mode IN ('LIVE','PAPER','SHADOW')),
                           decision_contract_version TEXT NOT NULL,
                           decision_contract_hash TEXT NOT NULL,
                           instrument_key TEXT NOT NULL,
                           thesis_key TEXT NOT NULL,
                           risk_action TEXT NOT NULL CHECK(risk_action IN ('ENTRY','ADD')),
                           status TEXT NOT NULL CHECK(status IN (
                               'ACTIVE','SUBMISSION_UNKNOWN','CONSUMED','RECONCILED',
                               'RECONCILED_NO_ORDER','RELEASED','EXPIRED'
                           )),
                           created_at TEXT NOT NULL,
                           expires_at TEXT NOT NULL,
                           bound_at TEXT,
                           reconciled_at TEXT,
                           broker_order_id TEXT,
                           campaign_id TEXT,
                           release_reason TEXT,
                           evidence_json TEXT NOT NULL
                       )"""
                )
                self.conn.execute(
                    """INSERT INTO risk_authorizations(
                           authorization_id,account_key,session_date,strategy_version,
                           pilot_id,book_mode,decision_contract_version,
                           decision_contract_hash,instrument_key,thesis_key,risk_action,
                           status,created_at,expires_at,bound_at,reconciled_at,
                           broker_order_id,campaign_id,release_reason,evidence_json
                       ) SELECT authorization_id,account_key,session_date,strategy_version,
                                pilot_id,book_mode,decision_contract_version,
                                decision_contract_hash,instrument_key,thesis_key,risk_action,
                                status,created_at,expires_at,bound_at,reconciled_at,
                                broker_order_id,campaign_id,release_reason,evidence_json
                         FROM risk_authorizations_legacy"""
                )
                self.conn.execute("DROP TABLE risk_authorizations_legacy")
            self.conn.execute("DROP INDEX IF EXISTS idx_risk_authorizations_session")
            self.conn.execute("DROP INDEX IF EXISTS idx_risk_authorizations_one_active")
            self.conn.execute("DROP INDEX IF EXISTS idx_risk_authorizations_one_pending")
            self.conn.execute(
                """CREATE INDEX idx_risk_authorizations_session
                   ON risk_authorizations(account_key,session_date,status,created_at DESC)"""
            )
            self.conn.execute(
                """CREATE UNIQUE INDEX idx_risk_authorizations_one_active
                   ON risk_authorizations(account_key,session_date)
                   WHERE status='ACTIVE'"""
            )
            self.conn.execute(
                """CREATE UNIQUE INDEX idx_risk_authorizations_one_pending
                   ON risk_authorizations(account_key,session_date)
                   WHERE status IN ('ACTIVE','SUBMISSION_UNKNOWN','CONSUMED')"""
            )
            latch = self.conn.execute(
                "SELECT generation FROM operator_entry_stop WHERE latch_key='GLOBAL'"
            ).fetchone()
            if latch is None:
                changed_at = utc_now()
                event = {
                    "generation": 1,
                    "action": "INITIALIZED",
                    "reason": "entry stop initialized released; no broker action taken",
                    "changed_by": "titan_runtime.storage",
                    "changed_at": changed_at,
                    "previous_event_hash": ZERO_SHA256,
                }
                event_hash = hashlib.sha256(
                    json.dumps(
                        event, sort_keys=True, separators=(",", ":")
                    ).encode("utf-8")
                ).hexdigest()
                self.conn.execute(
                    """INSERT INTO operator_entry_stop(
                           latch_key,engaged,generation,reason,changed_by,changed_at
                       ) VALUES('GLOBAL',0,1,?,?,?)""",
                    (event["reason"], event["changed_by"], changed_at),
                )
                self.conn.execute(
                    """INSERT INTO operator_entry_stop_events(
                           event_id,generation,action,reason,changed_by,changed_at,
                           previous_event_hash,event_hash
                       ) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        str(uuid.uuid4()), 1, event["action"], event["reason"],
                        event["changed_by"], changed_at, ZERO_SHA256, event_hash,
                    ),
                )

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

    def clear_candidates(
        self,
        *,
        pilot_id: str = TITAN_LIVE_PILOT_ID,
        book_mode: str = "SHADOW",
    ) -> None:
        """Clear one reporting book without disturbing another Pilot's board."""
        self.conn.execute(
            "DELETE FROM candidates WHERE pilot_id=? AND book_mode=?",
            (str(pilot_id).strip().lower(), str(book_mode).strip().upper()),
        )

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
        attribution = _normalize_pilot_attribution(payload)
        if attribution["book_mode"] == "LIVE":
            raise ValueError("candidate boards are PAPER/SHADOW evidence, not LIVE orders")
        self.conn.execute(
            """INSERT INTO candidates(
                   symbol,pilot_id,book_mode,decision_contract_version,
                   decision_contract_hash,observed_at,state,lane,signal_strength,price,gap_pct,dollar_volume,
                   volume_acceleration,price_acceleration,relative_volume,spread_pct,short_atr,
                   base_high,support,invalidation,limit_ceiling,extension_atr,quote_fresh,
                   preliminary_liquidity_pass,catalyst_required,payload_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(pilot_id,book_mode,symbol) DO UPDATE SET
                   decision_contract_version=excluded.decision_contract_version,
                   decision_contract_hash=excluded.decision_contract_hash,
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
                payload["symbol"], attribution["pilot_id"], attribution["book_mode"],
                attribution["decision_contract_version"],
                attribution["decision_contract_hash"], payload["observed_at"],
                payload["state"], payload["lane"],
                payload["signal_strength"], payload["price"], payload.get("gap_pct"),
                payload.get("dollar_volume"), payload.get("volume_acceleration"),
                payload.get("price_acceleration"), payload.get("relative_volume"),
                payload.get("spread_pct"), payload.get("short_atr"), payload.get("base_high"),
                payload.get("support"), payload.get("invalidation"), payload.get("limit_ceiling"),
                payload.get("extension_atr"), int(payload.get("quote_fresh", False)),
                int(payload.get("preliminary_liquidity_pass", False)), 1,
                json.dumps({**payload, **attribution}, separators=(",", ":")),
            ),
        )

    def get_candidate(
        self,
        symbol: str,
        *,
        pilot_id: str = TITAN_LIVE_PILOT_ID,
        book_mode: str = "SHADOW",
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            """SELECT * FROM candidates
               WHERE pilot_id=? AND book_mode=? AND symbol=?""",
            (str(pilot_id).lower(), str(book_mode).upper(), symbol),
        ).fetchone()
        return dict(row) if row else None

    def delete_candidate(
        self,
        symbol: str,
        *,
        pilot_id: str = TITAN_LIVE_PILOT_ID,
        book_mode: str = "SHADOW",
    ) -> None:
        self.conn.execute(
            "DELETE FROM candidates WHERE pilot_id=? AND book_mode=? AND symbol=?",
            (str(pilot_id).lower(), str(book_mode).upper(), symbol),
        )

    def leaderboard(
        self,
        limit: int = 20,
        *,
        pilot_id: str = TITAN_LIVE_PILOT_ID,
        book_mode: str = "SHADOW",
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT * FROM candidates
               WHERE pilot_id=? AND book_mode=?
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
            (str(pilot_id).lower(), str(book_mode).upper(), limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def all_candidate_symbols(
        self,
        *,
        pilot_id: str = TITAN_LIVE_PILOT_ID,
        book_mode: str = "SHADOW",
    ) -> list[str]:
        rows = self.conn.execute(
            """SELECT symbol FROM candidates
               WHERE pilot_id=? AND book_mode=?
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
               dollar_volume DESC""",
            (str(pilot_id).lower(), str(book_mode).upper()),
        ).fetchall()
        return [str(row["symbol"]) for row in rows]

    def save_prepared_trade_plan(self, payload: dict[str, Any]) -> str | None:
        attribution = _normalize_pilot_attribution(payload)
        if attribution["book_mode"] == "LIVE":
            raise ValueError(
                "prepared trade plans are non-authoritative PAPER/SHADOW evidence"
            )
        fingerprint = {
            key: payload.get(key)
            for key in (
                "schema_version", "policy_version", "sizing_policy_version",
                "weighted_scale_version", "symbol", "status", "direction", "lane",
                "setup", "trigger", "review_limit_ceiling", "reference_entry_price",
                "structural_stop", "risk_per_share", "t1", "t2", "t3",
                "weighted_opportunity_score", "modeled_move_capacity_pct",
                "preliminary_quantity_cap", "preliminary_risk_cap", "risk_campaign",
                "initial_entry_allocation_policy", "core_runner_policy", "add_policy",
                "blockers",
            )
        }
        fingerprint.update(attribution)
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
                       structural_stop,t1,t2,pilot_id,book_mode,
                       decision_contract_version,decision_contract_hash,
                       payload_json,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    plan_id, plan_key, payload["symbol"], payload["observed_at"],
                    payload["status"], payload["direction"], payload["lane"],
                    payload.get("weighted_opportunity_score"),
                    payload.get("modeled_move_capacity_pct"), payload.get("trigger"),
                    payload.get("structural_stop"), payload.get("t1"), payload.get("t2"),
                    attribution["pilot_id"], attribution["book_mode"],
                    attribution["decision_contract_version"],
                    attribution["decision_contract_hash"],
                    json.dumps({**payload, **attribution}, separators=(",", ":")),
                    utc_now(),
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

    def latest_prepared_trade_plans(
        self,
        limit: int = 100,
        *,
        pilot_id: str = TITAN_LIVE_PILOT_ID,
        book_mode: str = "SHADOW",
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT p.* FROM prepared_trade_plans p
               JOIN (
                   SELECT pilot_id,book_mode,symbol,MAX(created_at) AS latest
                   FROM prepared_trade_plans
                   WHERE pilot_id=? AND book_mode=?
                   GROUP BY pilot_id,book_mode,symbol
               ) newest ON newest.pilot_id=p.pilot_id
                        AND newest.book_mode=p.book_mode
                        AND newest.symbol=p.symbol
                        AND newest.latest=p.created_at
               ORDER BY p.weighted_opportunity_score DESC, p.created_at DESC LIMIT ?""",
            (
                str(pilot_id).strip().lower(),
                str(book_mode).strip().upper(),
                limit,
            ),
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
        normalized_details = details or {}
        if not isinstance(normalized_details, dict):
            raise ValueError("decision details must be an object")
        attribution = _normalize_pilot_attribution(normalized_details)
        event = self.conn.execute(
            "SELECT event_id FROM events WHERE event_id=?", (event_id,)
        ).fetchone()
        if not event:
            raise ValueError(f"unknown event_id: {event_id}")
        decision_id = str(uuid.uuid4())
        self.conn.execute(
            """INSERT INTO event_decisions(
                   decision_id,event_id,decided_at,decision,reason,pilot_id,book_mode,
                   decision_contract_version,decision_contract_hash,details_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                decision_id, event_id, utc_now(), decision.strip().upper(), reason.strip(),
                attribution["pilot_id"], attribution["book_mode"],
                attribution["decision_contract_version"],
                attribution["decision_contract_hash"],
                json.dumps({**normalized_details, **attribution}, separators=(",", ":")),
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
        if payload.get("status", "proposed") != "proposed":
            raise ValueError("new strategy change proposals must start as proposed")
        if bool(payload.get("production_approved", False)):
            raise ValueError("proposal input cannot self-assert production approval")
        change_id = str(uuid.uuid4())
        self.conn.execute(
            """INSERT INTO strategy_changes(
                   change_id,proposed_at,title,change_class,category,evidence_json,
                   expected_effect,status,production_approved,prior_version,proposed_version
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                change_id, utc_now(), payload["title"], change_class, category,
                json.dumps(payload.get("evidence") or {}, separators=(",", ":")),
                payload["expected_effect"], "proposed", 0,
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

    def _performance_grade_item(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        payload = json.loads(item.pop("payload_json"))
        item["incomplete_reasons"] = json.loads(
            item.pop("incomplete_reasons_json")
        )
        item.update(payload)
        item["hard_fail"] = bool(item["hard_fail"])
        newer = self.conn.execute(
            """SELECT 1 FROM daily_performance_grades
               WHERE account_key=? AND session_date=? AND strategy_version=?
                 AND revision>? LIMIT 1""",
            (
                item["account_key"], item["session_date"],
                item["strategy_version"], item["revision"],
            ),
        ).fetchone()
        item["is_canonical"] = newer is None
        item["score_calculation"] = {
            "process_weight_pct": 80.0,
            "outcome_weight_pct": 20.0,
            "process_score": item["process_score"],
            "outcome_score": item["outcome_score"],
            "raw_overall_score": item["raw_overall_score"],
            "evidence_ceiling": item["evidence_ceiling"],
            "hard_ceiling": item["hard_ceiling"],
            "overall_score": item["overall_score"],
            "letter_grade": item["letter_grade"],
        }
        # The current recorder validates shape, formulas, the immutable broker
        # snapshot, and terminal account state.  It does not yet derive every
        # checklist item or source digest from an independent evidence sealer.
        # Therefore even a high-quality FINAL grade is reporting/shadow
        # evidence only and can never be used as a production-promotion token.
        item["authoritative_evidence_verified"] = False
        item["evidence_sufficient_for_change_evaluation"] = False
        item["promotion_blocked_reason"] = (
            "independent evidence sealer and hash-bound promotion gate are not installed"
        )
        # Recording a grade never authorizes or applies a production edit.
        item["change_authority"] = False
        return item

    def record_performance_grade(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Append one evidence-bound daily grade or an explicit correction.

        The first record for an account/session/strategy is revision 1.  An
        exact replay is idempotent.  Any different payload must explicitly
        name the current canonical grade in ``corrects_grade_id`` and becomes
        the next immutable revision.  This method only records the completed
        grade; it never applies a strategy or automation change.
        """
        if not isinstance(payload, dict):
            raise ValueError("daily performance grade must be a JSON object")
        allowed_fields = {
            "account_key", "session_date", "strategy_version", "rubric_version",
            "pilot_id", "book_mode", "decision_contract_version",
            "decision_contract_hash",
            "graded_at", "broker_confirmed_pnl", "execution_metrics",
            "category_scores", "evidence_coverage_pct", "strengths", "mistakes",
            "improvement_proposals", "hard_failures", "corrects_grade_id",
            "correction_reason", "no_change_reason", "evidence_manifest", "notes",
        }
        unknown = sorted(set(payload) - allowed_fields)
        if unknown:
            raise ValueError(
                "unknown daily performance grade fields: " + ", ".join(unknown)
            )
        required = (
            "account_key", "session_date", "strategy_version", "rubric_version",
            "pilot_id", "book_mode", "decision_contract_version",
            "decision_contract_hash",
            "graded_at", "broker_confirmed_pnl", "execution_metrics",
            "category_scores", "evidence_coverage_pct", "strengths", "mistakes",
            "improvement_proposals", "hard_failures",
        )
        missing = [field for field in required if field not in payload]
        if missing:
            raise ValueError(
                "missing daily performance grade fields: " + ", ".join(missing)
            )

        account_key = str(payload["account_key"]).strip()
        strategy_version = str(payload["strategy_version"]).strip()
        attribution = _normalize_pilot_attribution(payload)
        if attribution["book_mode"] != "LIVE":
            raise ValueError("broker-bound daily performance grades are LIVE-only")
        rubric_version = str(payload["rubric_version"]).strip()
        if not account_key or not strategy_version or not rubric_version:
            raise ValueError(
                "account_key, strategy_version, and rubric_version cannot be empty"
            )
        if rubric_version != PERFORMANCE_RUBRIC_VERSION:
            raise ValueError(
                f"unsupported rubric_version: {rubric_version}; "
                f"expected {PERFORMANCE_RUBRIC_VERSION}"
            )
        session_date = str(payload["session_date"])
        try:
            parsed_date = datetime.strptime(session_date, "%Y-%m-%d").date()
        except ValueError as error:
            raise ValueError("session_date must use YYYY-MM-DD") from error
        if parsed_date.isoformat() != session_date:
            raise ValueError("session_date must use YYYY-MM-DD")
        graded_at = _aware_timestamp(payload["graded_at"], "graded_at")
        graded_time = datetime.fromisoformat(graded_at)
        if graded_time > datetime.now(timezone.utc) + timedelta(seconds=15):
            raise ValueError("graded_at may not be in the future")
        graded_et = graded_time.astimezone(ZoneInfo("America/New_York"))
        if graded_et.date() != parsed_date:
            raise ValueError("graded_at must fall on session_date in America/New_York")
        if (graded_et.hour, graded_et.minute) < (16, 10):
            raise ValueError("daily grading may not begin before 16:10 America/New_York")

        pnl = payload["broker_confirmed_pnl"]
        required_pnl = (
            "broker_confirmed_at", "start_of_day_equity", "current_equity",
            "realized_net_pnl", "confirmed_cash_flow_adjustment", "account_day_pnl",
        )
        broker_confirmed_at: str | None = None
        normalized_pnl: dict[str, object] | None = None
        if pnl is not None:
            if not isinstance(pnl, dict):
                raise ValueError("broker_confirmed_pnl must be an object or null")
            missing_pnl = [field for field in required_pnl if field not in pnl]
            if missing_pnl:
                raise ValueError(
                    "missing broker-confirmed P&L fields: " + ", ".join(missing_pnl)
                )
            broker_confirmed_at = _aware_timestamp(
                pnl["broker_confirmed_at"],
                "broker_confirmed_pnl.broker_confirmed_at",
            )
            broker_time = datetime.fromisoformat(broker_confirmed_at)
            if broker_time > graded_time:
                raise ValueError("graded_at must not precede broker-confirmed P&L")
            broker_et = broker_time.astimezone(ZoneInfo("America/New_York"))
            if broker_et.date() != parsed_date or (broker_et.hour, broker_et.minute) < (16, 5):
                raise ValueError(
                    "final broker snapshot must be from session_date at or after 16:05 America/New_York"
                )
            normalized_pnl = {"broker_confirmed_at": broker_confirmed_at}
            for field in required_pnl[1:]:
                normalized_pnl[field] = float(
                    _finite_decimal(pnl[field], f"broker_confirmed_pnl.{field}")
                )
            if (
                float(normalized_pnl["start_of_day_equity"]) <= 0
                or float(normalized_pnl["current_equity"]) <= 0
            ):
                raise ValueError("broker-confirmed account equity must be positive")
            calculated_account_day_pnl = (
                float(normalized_pnl["current_equity"])
                - float(normalized_pnl["start_of_day_equity"])
                - float(normalized_pnl["confirmed_cash_flow_adjustment"])
            )
            if abs(
                float(normalized_pnl["account_day_pnl"])
                - calculated_account_day_pnl
            ) > 0.005:
                raise ValueError(
                    "broker_confirmed_pnl.account_day_pnl is inconsistent with equity and cash flow"
                )

        execution = payload["execution_metrics"]
        if not isinstance(execution, dict):
            raise ValueError("execution_metrics must be an object")
        count_fields = (
            "campaigns_reviewed", "campaigns_entered", "campaigns_closed",
            "winning_campaigns", "losing_campaigns", "orders_submitted",
            "orders_filled", "missed_qualified_setups", "false_positive_entries",
            "qualified_setups",
        )
        continuous_fields = (
            "mfe_dollars", "mae_dollars", "capture_ratio_pct",
            "average_entry_slippage_bps", "average_exit_slippage_bps",
            "max_protection_latency_seconds", "authorized_filled_risk_dollars",
            "realized_after_cost_profit_dollars",
            "executed_after_cost_favorable_opportunity_dollars",
            "missed_after_cost_favorable_opportunity_dollars",
        )
        missing_execution = [
            field for field in (*count_fields, *continuous_fields)
            if field not in execution
        ]
        if missing_execution:
            raise ValueError(
                "missing execution metrics: " + ", ".join(missing_execution)
            )
        normalized_execution = dict(execution)
        for field in count_fields:
            value = _finite_decimal(execution[field], f"execution_metrics.{field}")
            if value < 0 or value != value.to_integral_value():
                raise ValueError(f"execution_metrics.{field} must be a nonnegative integer")
            normalized_execution[field] = int(value)
        for field in continuous_fields:
            value = _finite_decimal(execution[field], f"execution_metrics.{field}")
            normalized_execution[field] = float(value)
        for field in (
            "mfe_dollars", "mae_dollars", "max_protection_latency_seconds",
            "authorized_filled_risk_dollars",
            "realized_after_cost_profit_dollars",
            "executed_after_cost_favorable_opportunity_dollars",
            "missed_after_cost_favorable_opportunity_dollars",
        ):
            if float(normalized_execution[field]) < 0:
                raise ValueError(f"execution_metrics.{field} cannot be negative")
        _bounded_score(
            normalized_execution["capture_ratio_pct"],
            "execution_metrics.capture_ratio_pct",
        )
        if normalized_execution["campaigns_entered"] > normalized_execution["campaigns_reviewed"]:
            raise ValueError("campaigns_entered cannot exceed campaigns_reviewed")
        if normalized_execution["campaigns_closed"] > normalized_execution["campaigns_entered"]:
            raise ValueError("campaigns_closed cannot exceed campaigns_entered")
        if (
            normalized_execution["winning_campaigns"]
            + normalized_execution["losing_campaigns"]
            > normalized_execution["campaigns_closed"]
        ):
            raise ValueError("winning plus losing campaigns cannot exceed campaigns_closed")
        if normalized_execution["orders_filled"] > normalized_execution["orders_submitted"]:
            raise ValueError("orders_filled cannot exceed orders_submitted")
        if normalized_execution["campaigns_entered"] > normalized_execution["qualified_setups"]:
            raise ValueError("campaigns_entered cannot exceed qualified_setups")
        if normalized_execution["missed_qualified_setups"] > normalized_execution["qualified_setups"]:
            raise ValueError("missed_qualified_setups cannot exceed qualified_setups")
        if (
            normalized_execution["campaigns_entered"]
            + normalized_execution["missed_qualified_setups"]
            > normalized_execution["qualified_setups"]
        ):
            raise ValueError(
                "entered plus missed qualified setups cannot exceed qualified_setups"
            )
        if (
            normalized_execution["campaigns_entered"] > 0
            and normalized_execution["authorized_filled_risk_dollars"] <= 0
        ):
            raise ValueError(
                "entered campaigns require positive authorized_filled_risk_dollars"
            )
        try:
            json.dumps(normalized_execution, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError) as error:
            raise ValueError("execution_metrics must contain JSON values") from error

        categories = payload["category_scores"]
        if not isinstance(categories, dict) or set(categories) != set(PERFORMANCE_RUBRIC):
            raise ValueError(
                "category_scores must contain exactly the fixed rubric categories: "
                + ", ".join(sorted(PERFORMANCE_RUBRIC))
            )
        normalized_categories: dict[str, dict[str, object]] = {}
        weighted_sums = {"process": Decimal("0"), "outcome": Decimal("0")}
        weight_sums = {"process": Decimal("0"), "outcome": Decimal("0")}
        for raw_name in sorted(categories):
            name = str(raw_name).strip()
            value = categories[raw_name]
            if not name or not isinstance(value, dict):
                raise ValueError("each category score must be a named object")
            extra_category_fields = set(value) - {
                "group", "score", "weight", "evidence", "applicable"
            }
            if extra_category_fields:
                raise ValueError(
                    f"unknown fields for category {name}: "
                    + ", ".join(sorted(extra_category_fields))
                )
            missing_category_fields = {
                "group", "score", "weight", "evidence", "applicable"
            } - set(value)
            if missing_category_fields:
                raise ValueError(
                    f"missing fields for category {name}: "
                    + ", ".join(sorted(missing_category_fields))
                )
            expected_group, expected_weight = PERFORMANCE_RUBRIC[name]
            group = str(value["group"]).strip().lower()
            if group != expected_group:
                raise ValueError(
                    f"category {name} group must be {expected_group}"
                )
            weight = _finite_decimal(value["weight"], f"category_scores.{name}.weight")
            if weight != expected_weight:
                raise ValueError(
                    f"category {name} weight must be {expected_weight}"
                )
            applicable = value["applicable"]
            if type(applicable) is not bool:
                raise ValueError(f"category {name} applicable must be boolean")
            if group == "outcome" and not applicable:
                raise ValueError(f"outcome category {name} cannot be N/A")
            evidence = value["evidence"]
            if evidence in (None, "", [], {}):
                raise ValueError(f"category {name} requires nonempty evidence")
            if applicable:
                score = _bounded_score(
                    value["score"], f"category_scores.{name}.score"
                )
            else:
                if value["score"] is not None:
                    raise ValueError(
                        f"inapplicable category {name} score must be null"
                    )
                score = None
            if group == "process":
                required_evidence_fields = {
                    "eligible_items", "passed_items", "source_ids"
                }
                if (
                    not isinstance(evidence, dict)
                    or set(evidence) != required_evidence_fields
                ):
                    raise ValueError(
                        f"process category {name} evidence must contain exactly "
                        "eligible_items, passed_items, and source_ids"
                    )
                eligible_item_count = _finite_decimal(
                    evidence["eligible_items"],
                    f"category_scores.{name}.evidence.eligible_items",
                )
                passed_item_count = _finite_decimal(
                    evidence["passed_items"],
                    f"category_scores.{name}.evidence.passed_items",
                )
                source_ids = evidence["source_ids"]
                if (
                    eligible_item_count < 0
                    or eligible_item_count != eligible_item_count.to_integral_value()
                    or passed_item_count < 0
                    or passed_item_count != passed_item_count.to_integral_value()
                    or passed_item_count > eligible_item_count
                    or not isinstance(source_ids, list)
                    or not source_ids
                    or any(
                        not isinstance(source_id, str) or not source_id.strip()
                        for source_id in source_ids
                    )
                ):
                    raise ValueError(
                        f"process category {name} has invalid checklist evidence"
                    )
                if applicable:
                    if eligible_item_count <= 0:
                        raise ValueError(
                            f"applicable process category {name} needs eligible items"
                        )
                    expected_process_score = (
                        passed_item_count * Decimal("100") / eligible_item_count
                    )
                    assert score is not None
                    if abs(score - expected_process_score) > Decimal("0.01"):
                        raise ValueError(
                            f"process category {name} score does not match checklist; "
                            f"expected {_quantized_score(expected_process_score)}"
                        )
                elif eligible_item_count != 0 or passed_item_count != 0:
                    raise ValueError(
                        f"inapplicable process category {name} checklist counts must be zero"
                    )
            normalized_category: dict[str, object] = {
                "group": group,
                "score": float(score) if score is not None else None,
                "weight": float(weight),
                "applicable": applicable,
                "evidence": evidence,
            }
            try:
                json.dumps(normalized_category, sort_keys=True, separators=(",", ":"))
            except (TypeError, ValueError) as error:
                raise ValueError(f"category {name} evidence must be JSON") from error
            normalized_categories[name] = normalized_category
            if applicable:
                assert score is not None
                weighted_sums[group] += score * weight
                weight_sums[group] += weight
        if any(weight_sums[group] <= 0 for group in weight_sums):
            raise ValueError("category_scores must include process and outcome categories")
        if weight_sums["process"] < PERFORMANCE_MINIMUM_APPLICABLE_PROCESS_WEIGHT:
            raise ValueError(
                "applicable process category weight must total at least 55"
            )

        no_trade_no_setup = (
            normalized_execution["campaigns_entered"] == 0
            and normalized_execution["qualified_setups"] == 0
            and normalized_execution["orders_submitted"] == 0
            and normalized_execution["orders_filled"] == 0
            and normalized_execution["authorized_filled_risk_dollars"] == 0
            and (
                normalized_pnl is None
                or abs(float(normalized_pnl["account_day_pnl"])) <= 0.005
            )
        )
        if normalized_pnl is None:
            expected_pnl_score = Decimal("50")
        else:
            account_day_pnl = Decimal(str(normalized_pnl["account_day_pnl"]))
            if account_day_pnl <= 0:
                expected_pnl_score = Decimal("50") * (
                    Decimal("1") + account_day_pnl / Decimal("100")
                )
            else:
                expected_pnl_score = Decimal("50") + Decimal("50") * (
                    account_day_pnl / Decimal("150")
                )
            expected_pnl_score = min(
                Decimal("100"), max(Decimal("0"), expected_pnl_score)
            )

        if no_trade_no_setup:
            expected_net_r_score = expected_capture_score = Decimal("50")
        else:
            authorized_risk = Decimal(
                str(normalized_execution["authorized_filled_risk_dollars"])
            )
            if authorized_risk > 0 and normalized_pnl is not None:
                day_r = Decimal(str(normalized_pnl["account_day_pnl"])) / authorized_risk
                if day_r <= 0:
                    expected_net_r_score = Decimal("50") * (Decimal("1") + day_r)
                else:
                    expected_net_r_score = Decimal("50") + (
                        Decimal("50") * day_r / Decimal("1.5")
                    )
                expected_net_r_score = min(
                    Decimal("100"), max(Decimal("0"), expected_net_r_score)
                )
            else:
                expected_net_r_score = (
                    Decimal("0")
                    if normalized_execution["qualified_setups"] > 0
                    else Decimal("50")
                )
            capture_numerator = Decimal(
                str(normalized_execution["realized_after_cost_profit_dollars"])
            )
            capture_denominator = (
                Decimal(str(normalized_execution[
                    "executed_after_cost_favorable_opportunity_dollars"
                ]))
                + Decimal(str(normalized_execution[
                    "missed_after_cost_favorable_opportunity_dollars"
                ]))
            )
            if capture_numerator - capture_denominator > Decimal("0.01"):
                raise ValueError(
                    "realized profit cannot exceed executed plus missed after-cost favorable opportunity"
                )
            if normalized_pnl is not None and abs(
                capture_numerator
                - max(Decimal("0"), Decimal(str(normalized_pnl["account_day_pnl"])))
            ) > Decimal("0.01"):
                raise ValueError(
                    "realized_after_cost_profit_dollars must equal positive broker account-day P&L"
                )
            if capture_denominator > 0:
                expected_capture_score = min(
                    Decimal("100"), max(
                        Decimal("0"),
                        Decimal("100") * capture_numerator / capture_denominator,
                    )
                )
            else:
                expected_capture_score = Decimal("0")
            supplied_capture_ratio = Decimal(
                str(normalized_execution["capture_ratio_pct"])
            )
            if abs(supplied_capture_ratio - expected_capture_score) > Decimal("0.01"):
                raise ValueError(
                    "execution_metrics.capture_ratio_pct does not match executed plus missed opportunity"
                )

        deterministic_outcomes = {
            "broker_net_pnl_vs_objective_and_boundary": expected_pnl_score,
            "net_r_after_execution_costs": expected_net_r_score,
            "risk_weighted_after_cost_opportunity_capture": expected_capture_score,
        }
        if normalized_pnl is not None:
            for name, expected_score in deterministic_outcomes.items():
                supplied_score = Decimal(str(normalized_categories[name]["score"]))
                if abs(supplied_score - expected_score) > Decimal("0.01"):
                    raise ValueError(
                        f"category {name} score does not match deterministic rubric formula; "
                        f"expected {_quantized_score(expected_score)}"
                    )
        process_decimal = weighted_sums["process"] / weight_sums["process"]
        outcome_decimal = weighted_sums["outcome"] / weight_sums["outcome"]
        raw_overall_decimal = (
            process_decimal * PERFORMANCE_PROCESS_WEIGHT
            + outcome_decimal * PERFORMANCE_OUTCOME_WEIGHT
        )

        evidence_coverage = _bounded_score(
            payload["evidence_coverage_pct"], "evidence_coverage_pct"
        )

        def normalized_text_list(field: str, *, allow_empty: bool = False) -> list[str]:
            raw = payload[field]
            if not isinstance(raw, list) or (not raw and not allow_empty):
                requirement = "a list" if allow_empty else "a nonempty list"
                raise ValueError(f"{field} must be {requirement}")
            values = []
            for value in raw:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"{field} entries must be nonempty strings")
                values.append(value.strip())
            return values

        strengths = normalized_text_list("strengths")
        mistakes = normalized_text_list("mistakes", allow_empty=True)
        proposals = payload["improvement_proposals"]
        if not isinstance(proposals, list) or len(proposals) > 3:
            raise ValueError("improvement_proposals must be a list of at most three items")
        proposal_fields = {
            "title", "causal_problem", "proposed_change", "evidence",
            "independent_sample_count", "independent_session_count",
            "expected_primary_metric", "possible_adverse_effect", "test_horizon",
            "success_threshold", "rollback_trigger", "classification",
        }
        normalized_proposals = []
        for proposal in proposals:
            if not isinstance(proposal, dict) or set(proposal) != proposal_fields:
                raise ValueError(
                    "each improvement proposal must contain exactly: "
                    + ", ".join(sorted(proposal_fields))
                )
            normalized_proposal = dict(proposal)
            for field in proposal_fields - {
                "evidence", "independent_sample_count", "independent_session_count"
            }:
                if not isinstance(proposal[field], str) or not proposal[field].strip():
                    raise ValueError(f"improvement proposal {field} must be nonempty text")
                normalized_proposal[field] = proposal[field].strip()
            if proposal["classification"] not in {
                "IMMEDIATE_SAFE", "SHADOW_FIRST", "PROTECTED_USER_ONLY"
            }:
                raise ValueError("invalid improvement proposal classification")
            for field in ("independent_sample_count", "independent_session_count"):
                count = _finite_decimal(proposal[field], f"improvement_proposals.{field}")
                if count < 0 or count != count.to_integral_value():
                    raise ValueError(f"improvement proposal {field} must be a nonnegative integer")
                normalized_proposal[field] = int(count)
            if proposal["evidence"] in (None, "", [], {}):
                raise ValueError("improvement proposal evidence must be nonempty")
            normalized_proposals.append(normalized_proposal)
        no_change_reason = payload.get("no_change_reason")
        if not proposals:
            if not isinstance(no_change_reason, str) or not no_change_reason.strip():
                raise ValueError("no_change_reason is required when no proposal is warranted")
            no_change_reason = no_change_reason.strip()
        elif no_change_reason is not None:
            raise ValueError("no_change_reason is allowed only when proposals are empty")
        try:
            json.dumps(normalized_proposals, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError) as error:
            raise ValueError("improvement_proposals must contain JSON values") from error

        hard_failures = payload["hard_failures"]
        required_failures = PERFORMANCE_REQUIRED_FAILURE_MARKERS
        if not isinstance(hard_failures, dict) or set(hard_failures) != required_failures:
            raise ValueError(
                "hard_failures must contain exactly: "
                + ", ".join(sorted(required_failures))
            )
        if any(type(hard_failures[field]) is not bool for field in required_failures):
            raise ValueError("hard failure markers must be booleans")
        active_ceilings = [
            ceiling for field, ceiling in PERFORMANCE_HARD_FAILURE_CEILINGS.items()
            if hard_failures[field]
        ]
        hard_ceiling_decimal = min(active_ceilings) if active_ceilings else None
        evidence_ceiling_decimal = (
            Decimal("69") if Decimal("80") <= evidence_coverage < Decimal("95")
            else None
        )
        incomplete_reasons = []
        if evidence_coverage < 80:
            incomplete_reasons.append("evidence_coverage_below_80_pct")
        if normalized_pnl is None:
            incomplete_reasons.append("final_broker_confirmed_pnl_missing")
        if hard_failures["unreconciled_broker_state"]:
            incomplete_reasons.append("broker_state_unreconciled")
        grade_status = "INCOMPLETE" if incomplete_reasons else "FINAL"
        if (
            grade_status == "INCOMPLETE"
            and (graded_et.hour, graded_et.minute) < (17, 0)
        ):
            raise ValueError(
                "terminal INCOMPLETE grade may not be sealed before 17:00 America/New_York"
            )
        if grade_status == "INCOMPLETE":
            overall_score: float | None = None
            letter_grade = "INCOMPLETE"
        else:
            score_ceilings = [
                ceiling for ceiling in (
                    evidence_ceiling_decimal, hard_ceiling_decimal
                ) if ceiling is not None
            ]
            overall_decimal = (
                min(raw_overall_decimal, *score_ceilings)
                if score_ceilings else raw_overall_decimal
            )
            overall_score = _quantized_score(overall_decimal)
            letter_grade = (
                "A" if overall_score >= 93 else
                "A-" if overall_score >= 90 else
                "B+" if overall_score >= 87 else
                "B" if overall_score >= 83 else
                "B-" if overall_score >= 80 else
                "C+" if overall_score >= 77 else
                "C" if overall_score >= 73 else
                "C-" if overall_score >= 70 else
                "D" if overall_score >= 60 else "F"
            )

        corrects_grade_id_raw = payload.get("corrects_grade_id")
        corrects_grade_id = (
            str(corrects_grade_id_raw).strip() if corrects_grade_id_raw is not None else None
        )
        if corrects_grade_id_raw is not None and not corrects_grade_id:
            raise ValueError("corrects_grade_id cannot be empty")
        if corrects_grade_id is not None:
            raise ValueError(
                "grade corrections are disabled until an independent evidence sealer is installed"
            )
        correction_reason_raw = payload.get("correction_reason")
        correction_reason = (
            str(correction_reason_raw).strip()
            if correction_reason_raw is not None else None
        )
        if corrects_grade_id is not None and not correction_reason:
            raise ValueError("correction_reason is required for a grade correction")
        if corrects_grade_id is None and correction_reason_raw is not None:
            raise ValueError("revision 1 cannot include correction_reason")
        normalized_payload: dict[str, object] = {
            "account_key": account_key,
            "session_date": session_date,
            "strategy_version": strategy_version,
            **attribution,
            "rubric_version": rubric_version,
            "graded_at": graded_at,
            "broker_confirmed_pnl": normalized_pnl,
            "execution_metrics": normalized_execution,
            "category_scores": normalized_categories,
            "evidence_coverage_pct": float(evidence_coverage),
            "strengths": strengths,
            "mistakes": mistakes,
            "improvement_proposals": normalized_proposals,
            "hard_failures": {
                field: hard_failures[field] for field in sorted(required_failures)
            },
            "corrects_grade_id": corrects_grade_id,
        }
        if correction_reason is not None:
            normalized_payload["correction_reason"] = correction_reason
        if no_change_reason is not None:
            normalized_payload["no_change_reason"] = no_change_reason
        evidence_manifest = payload.get("evidence_manifest")
        required_manifest_fields = {
            "sealed_at", "eligible_items", "verified_items", "source_hashes",
            "source_record_counts", "massive_data_watermark", "decision_watermark",
        }
        if (
            not isinstance(evidence_manifest, dict)
            or set(evidence_manifest) != required_manifest_fields
        ):
            raise ValueError(
                "evidence_manifest must contain exactly: "
                + ", ".join(sorted(required_manifest_fields))
            )
        sealed_at = _aware_timestamp(evidence_manifest["sealed_at"], "evidence_manifest.sealed_at")
        if datetime.fromisoformat(sealed_at) > graded_time:
            raise ValueError("evidence manifest must be sealed no later than graded_at")
        if (
            broker_confirmed_at is not None
            and datetime.fromisoformat(sealed_at) < datetime.fromisoformat(broker_confirmed_at)
        ):
            raise ValueError("evidence manifest cannot be sealed before the final broker snapshot")
        eligible_items = _finite_decimal(
            evidence_manifest["eligible_items"], "evidence_manifest.eligible_items"
        )
        verified_items = _finite_decimal(
            evidence_manifest["verified_items"], "evidence_manifest.verified_items"
        )
        if (
            eligible_items <= 0
            or eligible_items != eligible_items.to_integral_value()
            or verified_items < 0
            or verified_items != verified_items.to_integral_value()
            or verified_items > eligible_items
        ):
            raise ValueError("evidence manifest item counts are invalid")
        calculated_coverage = verified_items * Decimal("100") / eligible_items
        if abs(calculated_coverage - evidence_coverage) > Decimal("0.01"):
            raise ValueError(
                "evidence_coverage_pct does not match verified/eligible evidence items"
            )
        source_hashes = evidence_manifest["source_hashes"]
        if not isinstance(source_hashes, dict) or not source_hashes:
            raise ValueError("evidence_manifest.source_hashes must be nonempty")
        for source, digest in source_hashes.items():
            if (
                not isinstance(source, str) or not source.strip()
                or not isinstance(digest, str) or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest.lower())
            ):
                raise ValueError("evidence source hashes must be named SHA-256 values")
        source_counts = evidence_manifest["source_record_counts"]
        if not isinstance(source_counts, dict) or not source_counts:
            raise ValueError("evidence_manifest.source_record_counts must be nonempty")
        for source, count in source_counts.items():
            count_value = _finite_decimal(count, f"evidence_manifest.source_record_counts.{source}")
            if count_value < 0 or count_value != count_value.to_integral_value():
                raise ValueError("evidence source record counts must be nonnegative integers")
        normalized_manifest = {
            "sealed_at": sealed_at,
            "eligible_items": int(eligible_items),
            "verified_items": int(verified_items),
            "source_hashes": source_hashes,
            "source_record_counts": {
                source: int(count) for source, count in source_counts.items()
            },
            "massive_data_watermark": _aware_timestamp(
                evidence_manifest["massive_data_watermark"],
                "evidence_manifest.massive_data_watermark",
            ),
            "decision_watermark": _aware_timestamp(
                evidence_manifest["decision_watermark"],
                "evidence_manifest.decision_watermark",
            ),
        }
        sealed_time = datetime.fromisoformat(sealed_at)
        for watermark_field in ("massive_data_watermark", "decision_watermark"):
            if datetime.fromisoformat(str(normalized_manifest[watermark_field])) > sealed_time:
                raise ValueError(
                    f"evidence_manifest.{watermark_field} cannot follow sealed_at"
                )
        normalized_payload["evidence_manifest"] = normalized_manifest
        if "notes" in payload:
            if not isinstance(payload["notes"], str):
                raise ValueError("notes must be a string")
            normalized_payload["notes"] = payload["notes"]
        canonical_json = json.dumps(
            normalized_payload, sort_keys=True, separators=(",", ":")
        )
        payload_hash = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
        grade_id = payload_hash

        with self.transaction():
            # Exact replays are immutable reads.  Resolve them before checking
            # whether the broker has since produced a newer snapshot; later
            # account state must not invalidate a previously sealed grade.
            exact_replay = self.conn.execute(
                "SELECT * FROM daily_performance_grades WHERE payload_hash=?",
                (payload_hash,),
            ).fetchone()
            if exact_replay:
                result = self._performance_grade_item(exact_replay)
                result["idempotent_replay"] = True
                return result
            if (
                normalized_pnl is not None
                and not hard_failures["unreconciled_broker_state"]
            ):
                assert broker_confirmed_at is not None
                source = self.conn.execute(
                    """SELECT snapshot_json FROM risk_session_snapshots
                       WHERE account_key=? AND session_date=? AND broker_confirmed_at=?""",
                    (account_key, session_date, broker_confirmed_at),
                ).fetchone()
                if not source:
                    raise ValueError(
                        "daily grade has no matching immutable broker-confirmed risk snapshot"
                    )
                latest_source = self.conn.execute(
                    """SELECT broker_confirmed_at FROM risk_session_snapshots
                       WHERE account_key=? AND session_date=?
                       ORDER BY broker_confirmed_at DESC LIMIT 1""",
                    (account_key, session_date),
                ).fetchone()
                if (
                    not latest_source
                    or str(latest_source["broker_confirmed_at"]) != broker_confirmed_at
                ):
                    raise ValueError(
                        "daily grade must use the latest immutable broker snapshot"
                    )
                source_snapshot = json.loads(str(source["snapshot_json"]))
                if str(source_snapshot.get("strategy_version")) != strategy_version:
                    raise ValueError(
                        "daily grade strategy does not match the broker risk session"
                    )
                for field, expected_value in attribution.items():
                    if str(source_snapshot.get(field)) != expected_value:
                        raise ValueError(
                            f"daily grade {field} does not match the broker risk session"
                        )
                source_pairs = (
                    ("start_of_day_equity", "start_of_day_equity"),
                    ("current_equity", "current_equity"),
                    ("realized_net_pnl", "realized_net_pnl"),
                    ("confirmed_cash_flow_adjustment", "confirmed_cash_flow_adjustment"),
                    ("account_day_pnl", "account_day_pnl"),
                )
                for grade_field, source_field in source_pairs:
                    if abs(
                        float(normalized_pnl[grade_field])
                        - float(source_snapshot[source_field])
                    ) > 0.005:
                        raise ValueError(
                            f"daily grade {grade_field} does not match its immutable broker snapshot"
                        )
                source_broker_state = source_snapshot.get("broker_state")
                if not isinstance(source_broker_state, dict):
                    raise ValueError("daily grade source snapshot has no broker_state")
                for field in (
                    "account_state_readable", "orders_reconciled", "positions_reconciled"
                ):
                    if source_broker_state.get(field) is not True:
                        raise ValueError(
                            f"daily grade source snapshot is not final: {field}"
                        )
                for field in (
                    "current_gross_exposure_dollars", "working_entry_notional_dollars"
                ):
                    try:
                        flat_value = float(source_broker_state[field])
                    except (KeyError, TypeError, ValueError) as error:
                        raise ValueError(
                            f"daily grade source snapshot is missing {field}"
                        ) from error
                    if not isfinite(flat_value) or abs(flat_value) > 0.005:
                        raise ValueError(
                            f"daily grade source snapshot is not flat: {field}"
                        )
                for field in (
                    "position_count", "working_order_count",
                    "working_entry_order_count", "working_exit_order_count",
                ):
                    try:
                        count_value = _finite_decimal(source_broker_state[field], field)
                    except (KeyError, TypeError, ValueError) as error:
                        raise ValueError(
                            f"daily grade source snapshot is missing {field}"
                        ) from error
                    if count_value != 0:
                        raise ValueError(
                            f"daily grade source snapshot is not terminal: {field}"
                        )
                active_campaign_count = int(self.conn.execute(
                    """SELECT COUNT(*) FROM position_campaigns
                       WHERE account_key=?
                         AND status NOT IN ('CLOSED','CANCELED','REJECTED','FAILED')""",
                    (account_key,),
                ).fetchone()[0])
                if active_campaign_count:
                    raise ValueError(
                        "daily grade cannot be FINAL with an active campaign"
                    )
                pending_authorization_count = int(self.conn.execute(
                    """SELECT COUNT(*) FROM risk_authorizations
                       WHERE account_key=? AND session_date=?
                         AND status IN ('ACTIVE','SUBMISSION_UNKNOWN','CONSUMED')""",
                    (account_key, session_date),
                ).fetchone()[0])
                if pending_authorization_count:
                    raise ValueError(
                        "daily grade cannot be FINAL with a pending risk authorization"
                    )

            exact = self.conn.execute(
                "SELECT * FROM daily_performance_grades WHERE payload_hash=?",
                (payload_hash,),
            ).fetchone()
            if exact:
                exact_id = str(exact["grade_id"])
                replay = True
            else:
                latest = self.conn.execute(
                    """SELECT * FROM daily_performance_grades
                       WHERE account_key=? AND session_date=? AND strategy_version=?
                       ORDER BY revision DESC LIMIT 1""",
                    (account_key, session_date, strategy_version),
                ).fetchone()
                if latest is None:
                    revision = 1
                else:
                    raise ValueError(
                        "a different grade already exists and corrections are disabled"
                    )
                self.conn.execute(
                    """INSERT INTO daily_performance_grades(
                           grade_id,account_key,session_date,strategy_version,pilot_id,
                           book_mode,decision_contract_version,decision_contract_hash,revision,
                           corrects_grade_id,rubric_version,graded_at,broker_confirmed_at,
                           recorded_at,payload_hash,payload_json,evidence_coverage_pct,
                           process_score,outcome_score,raw_overall_score,overall_score,
                           letter_grade,grade_status,evidence_ceiling,
                           incomplete_reasons_json,hard_fail,hard_ceiling
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        grade_id, account_key, session_date, strategy_version,
                        attribution["pilot_id"], attribution["book_mode"],
                        attribution["decision_contract_version"],
                        attribution["decision_contract_hash"], revision,
                        corrects_grade_id, rubric_version, graded_at, broker_confirmed_at,
                        utc_now(), payload_hash, canonical_json, float(evidence_coverage),
                        _quantized_score(process_decimal),
                        _quantized_score(outcome_decimal),
                        _quantized_score(raw_overall_decimal), overall_score,
                        letter_grade, grade_status,
                        (
                            float(evidence_ceiling_decimal)
                            if evidence_ceiling_decimal is not None else None
                        ),
                        json.dumps(incomplete_reasons, separators=(",", ":")),
                        int(any(ceiling == 0 for ceiling in active_ceilings)),
                        float(hard_ceiling_decimal) if hard_ceiling_decimal is not None else None,
                    ),
                )
                exact_id = grade_id
                replay = False
        row = self.conn.execute(
            "SELECT * FROM daily_performance_grades WHERE grade_id=?", (exact_id,)
        ).fetchone()
        assert row is not None
        result = self._performance_grade_item(row)
        result["idempotent_replay"] = replay
        return result

    def performance_grade(
        self,
        *,
        grade_id: str | None = None,
        account_key: str | None = None,
        session_date: str | None = None,
        strategy_version: str | None = None,
    ) -> dict[str, Any] | None:
        if grade_id:
            row = self.conn.execute(
                "SELECT * FROM daily_performance_grades WHERE grade_id=?", (grade_id,)
            ).fetchone()
        else:
            if not account_key or not session_date or not strategy_version:
                raise ValueError(
                    "performance grade lookup requires grade_id or account/session/strategy"
                )
            row = self.conn.execute(
                """SELECT * FROM daily_performance_grades
                   WHERE account_key=? AND session_date=? AND strategy_version=?
                   ORDER BY revision DESC LIMIT 1""",
                (account_key, session_date, strategy_version),
            ).fetchone()
        return self._performance_grade_item(row) if row else None

    def performance_grades(
        self,
        limit: int = 20,
        *,
        account_key: str | None = None,
        session_date: str | None = None,
        strategy_version: str | None = None,
        include_revisions: bool = False,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            raise ValueError("performance grade limit must be positive")
        clauses = []
        values: list[object] = []
        for field, value in (
            ("account_key", account_key),
            ("session_date", session_date),
            ("strategy_version", strategy_version),
        ):
            if value is not None:
                clauses.append(f"g.{field}=?")
                values.append(value)
        if not include_revisions:
            clauses.append(
                """NOT EXISTS (
                    SELECT 1 FROM daily_performance_grades newer
                    WHERE newer.account_key=g.account_key
                      AND newer.session_date=g.session_date
                      AND newer.strategy_version=g.strategy_version
                      AND newer.revision>g.revision
                )"""
            )
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.conn.execute(
            f"""SELECT g.* FROM daily_performance_grades g{where}
                ORDER BY g.session_date DESC,g.recorded_at DESC,g.revision DESC LIMIT ?""",
            (*values, limit),
        ).fetchall()
        return [self._performance_grade_item(row) for row in rows]

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
        attribution = _normalize_pilot_attribution(payload)
        if attribution["book_mode"] == "LIVE":
            raise ValueError("research entry plans must use PAPER or SHADOW book_mode")
        plan_id = str(uuid.uuid4())
        context = payload.get("context") or {}
        context["capture_rule"] = "Entry alternatives recorded before outcome attachment."
        context.update(attribution)
        self.conn.execute(
            """INSERT INTO research_entry_plans(
                   plan_id,trade_date,symbol,setup,lane,captured_at,earliest_time,earliest_price,
                   conservative_time,conservative_price,selected_time,selected_price,
                   structural_stop,quantity,capital,pilot_id,book_mode,
                   decision_contract_version,decision_contract_hash,context_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                plan_id, payload["trade_date"], str(payload["symbol"]).upper(), payload["setup"],
                payload["lane"], utc_now(), payload.get("earliest_time"), payload.get("earliest_price"),
                payload.get("conservative_time"), payload.get("conservative_price"),
                payload.get("selected_time"), payload.get("selected_price"),
                payload["structural_stop"], payload["quantity"], payload["capital"],
                attribution["pilot_id"], attribution["book_mode"],
                attribution["decision_contract_version"],
                attribution["decision_contract_hash"],
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
                       plan_id,pilot_id,book_mode,decision_contract_version,
                       decision_contract_hash,completed_at,observation_end,session_high,session_low,
                       actual_exit_price,actual_pnl,actual_pnl_r,earliest_pnl,earliest_pnl_r,
                       conservative_pnl,conservative_pnl_r,hesitation_cost,confirmation_savings,
                       mfe,mae,outcome_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    plan_id, plan["pilot_id"], plan["book_mode"],
                    plan["decision_contract_version"],
                    plan["decision_contract_hash"], utc_now(), payload["observation_end"], payload.get("session_high"),
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
            "asset_class", "status", "strategy_version", "pilot_id", "book_mode",
            "decision_contract_version", "decision_contract_hash",
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
        attribution = _normalize_pilot_attribution(payload)
        if attribution["book_mode"] != "LIVE":
            raise ValueError("broker position campaigns are LIVE-only")
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
                           initial_quantity,current_quantity,broker_state_json,
                           broker_confirmed_at, symbol, thesis_key, direction,
                           asset_class, strategy_version,pilot_id,book_mode,
                           decision_contract_version,decision_contract_hash
                    FROM position_campaigns
                    WHERE account_key=? AND instrument_key=?
                      AND status NOT IN ({placeholders})
                    ORDER BY updated_at DESC LIMIT 1""",
                (payload["account_key"], payload["instrument_key"], *terminal_statuses),
            ).fetchone()
            prior_status = str(existing["status"]) if existing else None
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
            last_action = str(payload.get("last_action") or "").strip().upper()
            risk_lease_to_bind = None
            risk_lease_order_id = None
            risk_lease_submitted_at = None
            risk_lease_intent_id = None
            risk_lease_resolution_key = None
            prior_broker_state = (
                json.loads(str(existing["broker_state_json"])) if existing else {}
            )
            entry_order_fields = (
                "entry_order_id",
                "entry_order_submitted_at",
                "entry_submission_intent_id",
                "entry_order_resolution_key",
                "entry_order_acknowledged_at",
                "entry_order_ack_deadline_at",
                "entry_order_ack_state",
                "entry_order_quantity",
                "entry_cumulative_filled_quantity",
                "entry_risk_gate_authorization",
            )

            def entry_order_state(
                state: dict[str, Any], *, required: bool, label: str
            ) -> dict[str, Any] | None:
                present = any(field in state for field in entry_order_fields)
                if not required and not present:
                    return None
                missing_entry_fields = [
                    field for field in entry_order_fields if state.get(field) is None
                ]
                if missing_entry_fields:
                    raise ValueError(
                        f"{label} is missing dedicated entry-order evidence: "
                        + ", ".join(missing_entry_fields)
                    )
                order_id = str(state["entry_order_id"]).strip()
                if not order_id:
                    raise ValueError(f"{label}.entry_order_id cannot be empty")
                submitted_at = _aware_timestamp(
                    state["entry_order_submitted_at"],
                    f"{label}.entry_order_submitted_at",
                )
                acknowledged_at = _aware_timestamp(
                    state["entry_order_acknowledged_at"],
                    f"{label}.entry_order_acknowledged_at",
                )
                ack_deadline_at = _aware_timestamp(
                    state["entry_order_ack_deadline_at"],
                    f"{label}.entry_order_ack_deadline_at",
                )
                ack_state = str(state["entry_order_ack_state"]).strip().upper()
                valid_ack_states = {
                    "ON_TIME", "LATE_CONFIRMED", "UNKNOWN_RESOLVED",
                }
                if ack_state not in valid_ack_states:
                    raise ValueError("entry_order_ack_state is invalid")
                submission_intent_id = str(
                    state["entry_submission_intent_id"]
                ).strip()
                order_resolution_key = str(
                    state["entry_order_resolution_key"]
                ).strip()
                if not submission_intent_id or not order_resolution_key:
                    raise ValueError(
                        "entry submission intent and resolution key cannot be empty"
                    )
                order_quantity = _finite_float(
                    state["entry_order_quantity"],
                    f"{label}.entry_order_quantity",
                )
                cumulative_filled = _finite_float(
                    state["entry_cumulative_filled_quantity"],
                    f"{label}.entry_cumulative_filled_quantity",
                )
                authorization = state["entry_risk_gate_authorization"]
                if not isinstance(authorization, dict):
                    raise ValueError(
                        f"{label}.entry_risk_gate_authorization must be an object"
                    )
                if order_quantity <= 0:
                    raise ValueError(f"{label}.entry_order_quantity must be positive")
                if not 0 <= cumulative_filled <= order_quantity:
                    raise ValueError(
                        "entry cumulative fill must be within entry_order_quantity"
                    )
                submitted_time = datetime.fromisoformat(submitted_at)
                acknowledged_time = datetime.fromisoformat(acknowledged_at)
                deadline_time = datetime.fromisoformat(ack_deadline_at)
                timeout_seconds = _finite_decimal(
                    authorization.get("broker_ack_timeout_seconds"),
                    f"{label}.broker_ack_timeout_seconds",
                )
                if timeout_seconds != timeout_seconds.to_integral_value():
                    raise ValueError("broker ack timeout must be an integer")
                if acknowledged_time < submitted_time:
                    raise ValueError("entry broker ack cannot precede submission")
                if acknowledged_time <= deadline_time and ack_state != "ON_TIME":
                    raise ValueError("on-time entry ack requires ON_TIME")
                if acknowledged_time > deadline_time and ack_state not in {
                    "LATE_CONFIRMED", "UNKNOWN_RESOLVED"
                }:
                    raise ValueError(
                        "late entry ack requires an explicit late/unknown-resolved state"
                    )
                authorization_id = str(
                    authorization.get("authorization_id") or ""
                )
                intent_row = self.conn.execute(
                    """SELECT * FROM risk_submission_intents
                       WHERE intent_id=? AND authorization_id=?""",
                    (submission_intent_id, authorization_id),
                ).fetchone()
                if intent_row is None:
                    raise ValueError(
                        "entry evidence has no matching immutable submission intent"
                    )
                if (
                    str(intent_row["attempted_at"]) != submitted_at
                    or str(intent_row["broker_ack_deadline_at"])
                    != ack_deadline_at
                ):
                    raise ValueError(
                        "entry submission/deadline does not match immutable intent"
                    )
                resolution_row = self.conn.execute(
                    """SELECT * FROM risk_unknown_resolutions
                       WHERE resolution_key=? AND intent_id=?
                         AND authorization_id=?""",
                    (
                        order_resolution_key, submission_intent_id,
                        authorization_id,
                    ),
                ).fetchone()
                if (
                    resolution_row is None
                    or str(resolution_row["resolution_state"]) != "ORDER_FOUND"
                    or str(resolution_row["broker_order_id"]) != order_id
                ):
                    raise ValueError(
                        "entry evidence requires exact ORDER_FOUND broker resolution"
                    )
                return {
                    "order_id": order_id,
                    "submission_intent_id": submission_intent_id,
                    "order_resolution_key": order_resolution_key,
                    "submitted_at": submitted_at,
                    "acknowledged_at": acknowledged_at,
                    "ack_deadline_at": ack_deadline_at,
                    "ack_state": ack_state,
                    "order_quantity": order_quantity,
                    "cumulative_filled": cumulative_filled,
                    "authorization": authorization,
                }

            entry_required_statuses = {
                "SUBMITTED", "PARTIAL", "FILLED", "PROTECTED", "CLOSING", "CLOSED",
            }
            prior_entry = entry_order_state(
                prior_broker_state, required=False, label="prior broker_state"
            )
            incoming_entry = entry_order_state(
                broker_state,
                required=status in entry_required_statuses or prior_entry is not None,
                label="broker_state",
            )
            if status in {"PLANNED", "REVIEWED"} and incoming_entry is not None:
                raise ValueError(
                    "entry-order evidence may begin only with a durable SUBMITTED transition"
                )
            if status in {"PARTIAL", "FILLED", "PROTECTED"} and prior_entry is None:
                raise ValueError(
                    f"{status} requires a prior durable SUBMITTED entry-order transition"
                )

            if incoming_entry is not None:
                incoming_authorization = incoming_entry["authorization"]
                if abs(
                    float(incoming_authorization.get("quantity") or 0)
                    - incoming_entry["order_quantity"]
                ) > 1e-9:
                    raise ValueError(
                        "entry risk-gate authorization quantity must equal entry_order_quantity"
                    )
                if abs(
                    quantities["initial_quantity"] - incoming_entry["order_quantity"]
                ) > 1e-9:
                    raise ValueError(
                        "initial_quantity must equal the immutable entry_order_quantity"
                    )
                _validate_risk_gate_authorization(
                    incoming_authorization,
                    account_key=str(payload["account_key"]),
                    instrument_key=str(payload["instrument_key"]),
                    symbol=str(payload["symbol"]).upper(),
                    direction=direction,
                    asset_class=str(payload["asset_class"]).upper(),
                    thesis_key=thesis_key,
                    strategy_version=str(payload["strategy_version"]),
                    pilot_id=attribution["pilot_id"],
                    book_mode=attribution["book_mode"],
                    decision_contract_version=attribution[
                        "decision_contract_version"
                    ],
                    decision_contract_hash=attribution["decision_contract_hash"],
                    expected_action="ENTRY",
                    order_submitted_at=incoming_entry["submitted_at"],
                )
                if original_stop_input is not None and abs(
                    float(incoming_authorization["structural_stop_price"])
                    - original_stop_input
                ) > 1e-9:
                    raise ValueError(
                        "entry risk-gate stop does not match original_stop"
                    )

                if prior_entry is None:
                    if status != "SUBMITTED":
                        raise ValueError(
                            f"{status} requires a prior durable SUBMITTED entry-order transition"
                        )
                    if incoming_entry["cumulative_filled"] != 0:
                        raise ValueError(
                            "SUBMITTED must precede entry fills and requires cumulative fill 0"
                        )
                    if existing and float(existing["initial_quantity"]) not in {
                        0.0, incoming_entry["order_quantity"]
                    }:
                        raise ValueError(
                            "planned initial_quantity must match entry_order_quantity"
                        )
                    _validate_risk_authorization_against_session(
                        self.conn,
                        incoming_authorization,
                        account_key=str(payload["account_key"]),
                        strategy_version=str(payload["strategy_version"]),
                    )
                    risk_lease_to_bind = incoming_authorization
                    risk_lease_order_id = incoming_entry["order_id"]
                    risk_lease_submitted_at = incoming_entry["submitted_at"]
                    risk_lease_intent_id = incoming_entry[
                        "submission_intent_id"
                    ]
                    risk_lease_resolution_key = incoming_entry[
                        "order_resolution_key"
                    ]
                else:
                    if incoming_entry["order_id"] != prior_entry["order_id"]:
                        raise ValueError("entry_order_id is immutable")
                    if incoming_entry["submitted_at"] != prior_entry["submitted_at"]:
                        raise ValueError("entry_order_submitted_at is immutable")
                    if incoming_entry["acknowledged_at"] != prior_entry["acknowledged_at"]:
                        raise ValueError("entry_order_acknowledged_at is immutable")
                    if incoming_entry["ack_deadline_at"] != prior_entry["ack_deadline_at"]:
                        raise ValueError("entry_order_ack_deadline_at is immutable")
                    if incoming_entry["ack_state"] != prior_entry["ack_state"]:
                        raise ValueError("entry_order_ack_state is immutable")
                    if (
                        incoming_entry["submission_intent_id"]
                        != prior_entry["submission_intent_id"]
                    ):
                        raise ValueError("entry_submission_intent_id is immutable")
                    if (
                        incoming_entry["order_resolution_key"]
                        != prior_entry["order_resolution_key"]
                    ):
                        raise ValueError("entry_order_resolution_key is immutable")
                    if abs(
                        incoming_entry["order_quantity"]
                        - prior_entry["order_quantity"]
                    ) > 1e-9:
                        raise ValueError("entry_order_quantity is immutable")
                    if incoming_authorization != prior_entry["authorization"]:
                        raise ValueError(
                            "entry order must preserve its original risk authorization"
                        )
                    if existing and abs(
                        quantities["initial_quantity"]
                        - float(existing["initial_quantity"])
                    ) > 1e-9:
                        raise ValueError(
                            "initial_quantity is immutable after entry submission"
                        )
                    if (
                        incoming_entry["cumulative_filled"] + 1e-9
                        < prior_entry["cumulative_filled"]
                    ):
                        raise ValueError(
                            "entry cumulative fill may not move backward"
                        )
                    fill_increment = (
                        incoming_entry["cumulative_filled"]
                        - prior_entry["cumulative_filled"]
                    )
                    if (
                        prior_status in {"SUBMITTED", "PARTIAL"}
                        and status in {"PARTIAL", "FILLED", "PROTECTED"}
                    ):
                        current_increment = (
                            quantities["current_quantity"]
                            - float(existing["current_quantity"])
                        )
                        if abs(fill_increment - current_increment) > 1e-9:
                            raise ValueError(
                                "entry cumulative fill increment does not match "
                                "position quantity increment"
                            )
                    durable_entry_lease = self.conn.execute(
                        """SELECT status,campaign_id,broker_order_id,evidence_json
                           FROM risk_authorizations WHERE authorization_id=?""",
                        (str(incoming_authorization.get("authorization_id") or ""),),
                    ).fetchone()
                    if (
                        not durable_entry_lease
                        or str(durable_entry_lease["status"])
                        not in {"CONSUMED", "RECONCILED"}
                        or str(durable_entry_lease["campaign_id"])
                        != str(existing["campaign_id"])
                        or str(durable_entry_lease["broker_order_id"])
                        != incoming_entry["order_id"]
                        or _risk_authorization_hash_evidence(
                            json.loads(str(durable_entry_lease["evidence_json"]))
                        ) != _risk_authorization_hash_evidence(
                            incoming_authorization
                        )
                    ):
                        raise ValueError(
                            "entry order is not bound to its original durable risk lease"
                        )

                if status == "SUBMITTED" and incoming_entry["cumulative_filled"] != 0:
                    raise ValueError("SUBMITTED requires entry cumulative fill 0")
                if status == "PARTIAL" and not (
                    0
                    < incoming_entry["cumulative_filled"]
                    < incoming_entry["order_quantity"]
                ):
                    raise ValueError(
                        "PARTIAL requires a positive incomplete entry cumulative fill"
                    )
                if status in {"FILLED", "PROTECTED"} and abs(
                    incoming_entry["cumulative_filled"]
                    - incoming_entry["order_quantity"]
                ) > 1e-9:
                    raise ValueError(
                        f"{status} requires entry cumulative fill equal to order quantity"
                    )

            filled_quantity_increase = bool(
                existing
                and prior_status in {"FILLED", "PROTECTED"}
                and quantities["current_quantity"]
                > float(existing["current_quantity"]) + 1e-9
            )
            add_actions = {
                "ADD_SUBMITTED", "ADD_PARTIAL", "ADD_FILLED",
                "ADD_CANCELED", "ADD_REJECTED",
            }
            if filled_quantity_increase and last_action not in add_actions:
                raise ValueError(
                    "a filled quantity increase requires an explicit ADD transition"
                )
            if last_action.startswith("ADD") and last_action not in add_actions:
                raise ValueError(
                    "last_action must use an explicit ADD_SUBMITTED, ADD_PARTIAL, "
                    "ADD_FILLED, ADD_CANCELED, or ADD_REJECTED transition"
                )
            if last_action in add_actions:
                if broker_confirmed_at is None:
                    raise ValueError("ADD requires broker_confirmed_at")
                risk_authorization = broker_state.get("risk_gate_authorization")
                prior_add_state = None
                add_order_id = str(broker_state.get("add_order_id") or "").strip()
                order_submitted_at = broker_state.get("add_order_submitted_at")
                add_submission_intent_id = str(
                    broker_state.get("add_submission_intent_id") or ""
                ).strip()
                add_order_resolution_key = str(
                    broker_state.get("add_order_resolution_key") or ""
                ).strip()
                add_acknowledged_at = broker_state.get(
                    "add_order_acknowledged_at"
                )
                add_ack_deadline_at = broker_state.get(
                    "add_order_ack_deadline_at"
                )
                add_ack_state = str(
                    broker_state.get("add_order_ack_state") or ""
                ).strip().upper()
                add_order_quantity = _finite_float(
                    broker_state.get("add_order_quantity"),
                    "broker_state.add_order_quantity",
                )
                add_cumulative_filled = _finite_float(
                    broker_state.get("add_cumulative_filled_quantity"),
                    "broker_state.add_cumulative_filled_quantity",
                )
                if (
                    not add_order_id or not order_submitted_at
                    or not add_submission_intent_id
                    or not add_order_resolution_key
                    or not add_acknowledged_at or not add_ack_deadline_at
                    or not add_ack_state
                ):
                    raise ValueError(
                        "ADD broker evidence requires order ID, submission, "
                        "acknowledgement, and ack deadline"
                    )
                submitted_time = datetime.fromisoformat(_aware_timestamp(
                    order_submitted_at, "broker_state.add_order_submitted_at"
                ))
                acknowledged_time = datetime.fromisoformat(_aware_timestamp(
                    add_acknowledged_at,
                    "broker_state.add_order_acknowledged_at",
                ))
                deadline_time = datetime.fromisoformat(_aware_timestamp(
                    add_ack_deadline_at, "broker_state.add_order_ack_deadline_at"
                ))
                if not isinstance(risk_authorization, dict):
                    raise ValueError("ADD risk_gate_authorization must be an object")
                timeout_seconds = _finite_decimal(
                    risk_authorization.get("broker_ack_timeout_seconds"),
                    "ADD broker_ack_timeout_seconds",
                )
                if acknowledged_time < submitted_time:
                    raise ValueError("ADD broker ack cannot precede submission")
                if acknowledged_time <= deadline_time and add_ack_state != "ON_TIME":
                    raise ValueError("on-time ADD ack requires ON_TIME")
                if acknowledged_time > deadline_time and add_ack_state not in {
                    "LATE_CONFIRMED", "UNKNOWN_RESOLVED"
                }:
                    raise ValueError(
                        "late ADD ack requires an explicit late/unknown-resolved state"
                    )
                if add_order_quantity <= 0 or not 0 <= add_cumulative_filled <= add_order_quantity:
                    raise ValueError(
                        "ADD cumulative fill must be within the authorized order quantity"
                    )
                add_authorization_id = str(
                    risk_authorization.get("authorization_id") or ""
                )
                add_intent_row = self.conn.execute(
                    """SELECT * FROM risk_submission_intents
                       WHERE intent_id=? AND authorization_id=?""",
                    (add_submission_intent_id, add_authorization_id),
                ).fetchone()
                if (
                    add_intent_row is None
                    or str(add_intent_row["attempted_at"])
                    != submitted_time.isoformat()
                    or str(add_intent_row["broker_ack_deadline_at"])
                    != deadline_time.isoformat()
                ):
                    raise ValueError(
                        "ADD submission/deadline does not match immutable intent"
                    )
                add_resolution_row = self.conn.execute(
                    """SELECT * FROM risk_unknown_resolutions
                       WHERE resolution_key=? AND intent_id=?
                         AND authorization_id=?""",
                    (
                        add_order_resolution_key, add_submission_intent_id,
                        add_authorization_id,
                    ),
                ).fetchone()
                if (
                    add_resolution_row is None
                    or str(add_resolution_row["resolution_state"]) != "ORDER_FOUND"
                    or str(add_resolution_row["broker_order_id"])
                    != add_order_id
                ):
                    raise ValueError(
                        "ADD evidence requires exact ORDER_FOUND broker resolution"
                    )
                if existing:
                    prior_rows = self.conn.execute(
                        """SELECT payload_json FROM position_campaign_events
                           WHERE campaign_id=? ORDER BY observed_at DESC""",
                        (str(existing["campaign_id"]),),
                    ).fetchall()
                    for prior_row in prior_rows:
                        prior_payload = json.loads(str(prior_row["payload_json"]))
                        prior_broker = prior_payload.get("broker_state") or {}
                        if str(prior_broker.get("add_order_id") or "") == add_order_id:
                            prior_add_state = prior_broker
                            break
                new_order_authorization = prior_add_state is None
                if prior_add_state is None:
                    if last_action != "ADD_SUBMITTED":
                        raise ValueError(
                            "ADD fills require a prior ADD_SUBMITTED transition"
                        )
                    if add_cumulative_filled != 0 or filled_quantity_increase:
                        raise ValueError(
                            "ADD_SUBMITTED must precede fills and cannot increase quantity"
                        )
                else:
                    prior_authorization = prior_add_state.get(
                        "risk_gate_authorization"
                    ) or {}
                    if (
                        prior_authorization.get("authorization_id")
                        != (risk_authorization or {}).get("authorization_id")
                    ):
                        raise ValueError(
                            "ADD order must preserve its original risk authorization"
                        )
                    if abs(
                        float(prior_add_state.get("add_order_quantity") or 0)
                        - add_order_quantity
                    ) > 1e-9:
                        raise ValueError("ADD order quantity is immutable")
                    if _aware_timestamp(
                        prior_add_state.get("add_order_submitted_at"),
                        "prior add_order_submitted_at",
                    ) != _aware_timestamp(
                        order_submitted_at,
                        "broker_state.add_order_submitted_at",
                    ):
                        raise ValueError(
                            "ADD order submission timestamp is immutable"
                        )
                    for field in (
                        "add_submission_intent_id", "add_order_resolution_key",
                    ):
                        if str(prior_add_state.get(field) or "") != str(
                            broker_state.get(field) or ""
                        ):
                            raise ValueError(f"{field} is immutable")
                    for field in (
                        "add_order_acknowledged_at", "add_order_ack_deadline_at",
                    ):
                        if _aware_timestamp(
                            prior_add_state.get(field), f"prior {field}"
                        ) != _aware_timestamp(broker_state.get(field), field):
                            raise ValueError(f"{field} is immutable")
                    if str(prior_add_state.get("add_order_ack_state") or "").upper() != (
                        add_ack_state
                    ):
                        raise ValueError("add_order_ack_state is immutable")
                    prior_cumulative = _finite_float(
                        prior_add_state.get("add_cumulative_filled_quantity"),
                        "prior add_cumulative_filled_quantity",
                    )
                    if add_cumulative_filled + 1e-9 < prior_cumulative:
                        raise ValueError("ADD cumulative fill may not move backward")
                    fill_increment = add_cumulative_filled - prior_cumulative
                    current_increment = (
                        quantities["current_quantity"]
                        - float(existing["current_quantity"])
                    )
                    if abs(fill_increment - current_increment) > 1e-9:
                        raise ValueError(
                            "ADD cumulative fill increment does not match position quantity"
                        )
                    if last_action == "ADD_PARTIAL" and not (
                        0 < add_cumulative_filled < add_order_quantity
                    ):
                        raise ValueError(
                            "ADD_PARTIAL requires a positive incomplete cumulative fill"
                        )
                    if last_action == "ADD_FILLED" and abs(
                        add_cumulative_filled - add_order_quantity
                    ) > 1e-9:
                        raise ValueError(
                            "ADD_FILLED requires cumulative fill equal to order quantity"
                        )
                _validate_risk_gate_authorization(
                    risk_authorization,
                    account_key=str(payload["account_key"]),
                    instrument_key=str(payload["instrument_key"]),
                    symbol=str(payload["symbol"]).upper(),
                    direction=direction,
                    asset_class=str(payload["asset_class"]).upper(),
                    thesis_key=thesis_key,
                    strategy_version=str(payload["strategy_version"]),
                    pilot_id=attribution["pilot_id"],
                    book_mode=attribution["book_mode"],
                    decision_contract_version=attribution[
                        "decision_contract_version"
                    ],
                    decision_contract_hash=attribution["decision_contract_hash"],
                    expected_action="ADD",
                    order_submitted_at=str(order_submitted_at),
                )
                assert isinstance(risk_authorization, dict)
                if new_order_authorization:
                    _validate_risk_authorization_against_session(
                        self.conn,
                        risk_authorization,
                        account_key=str(payload["account_key"]),
                        strategy_version=str(payload["strategy_version"]),
                    )
                    risk_lease_to_bind = risk_authorization
                    risk_lease_order_id = add_order_id
                    risk_lease_submitted_at = str(order_submitted_at)
                    risk_lease_intent_id = add_submission_intent_id
                    risk_lease_resolution_key = add_order_resolution_key
                if abs(
                    float(risk_authorization["quantity"])
                    - float(add_order_quantity)
                ) > 1e-9:
                    raise ValueError(
                        "ADD risk-gate quantity does not match add_order_quantity"
                    )
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
                    **attribution,
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
            if risk_lease_to_bind is not None:
                self.bind_risk_authorization(
                    risk_lease_to_bind,
                    campaign_id=campaign_id,
                    broker_order_id=str(risk_lease_order_id),
                    order_submitted_at=str(risk_lease_submitted_at),
                    submission_intent_id=str(risk_lease_intent_id),
                    order_resolution_key=str(risk_lease_resolution_key),
                )
            now = utc_now()
            self.conn.execute(
                """INSERT INTO position_campaigns(
                       campaign_id,account_key,instrument_key,symbol,thesis_key,direction,
                       asset_class,status,
                       strategy_version,pilot_id,book_mode,decision_contract_version,
                       decision_contract_hash,opened_at,updated_at,broker_confirmed_at,
                       entry_price,original_stop,current_stop,initial_quantity,current_quantity,
                       core_quantity,runner_quantity,reference_risk_dollars,high_water_price,
                       mfe_r,mae_r,continuation_health,remaining_opportunity,last_action,
                       next_actions_json,broker_state_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                    payload["strategy_version"], attribution["pilot_id"],
                    attribution["book_mode"],
                    attribution["decision_contract_version"],
                    attribution["decision_contract_hash"], opened_at, now,
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
            self.conn.execute(
                """UPDATE position_campaigns SET
                       entry_submission_intent_id=COALESCE(
                           entry_submission_intent_id,?
                       ),
                       entry_order_resolution_key=COALESCE(
                           entry_order_resolution_key,?
                       ),
                       entry_order_acknowledged_at=COALESCE(
                           entry_order_acknowledged_at,?
                       ),
                       entry_order_ack_deadline_at=COALESCE(
                           entry_order_ack_deadline_at,?
                       ),
                       entry_order_ack_state=COALESCE(entry_order_ack_state,?),
                       add_submission_intent_id=COALESCE(
                           add_submission_intent_id,?
                       ),
                       add_order_resolution_key=COALESCE(
                           add_order_resolution_key,?
                       ),
                       add_order_acknowledged_at=COALESCE(
                           add_order_acknowledged_at,?
                       ),
                       add_order_ack_deadline_at=COALESCE(
                           add_order_ack_deadline_at,?
                       ),
                       add_order_ack_state=COALESCE(add_order_ack_state,?)
                   WHERE campaign_id=?""",
                (
                    (
                        incoming_entry["submission_intent_id"]
                        if incoming_entry is not None else None
                    ),
                    (
                        incoming_entry["order_resolution_key"]
                        if incoming_entry is not None else None
                    ),
                    (
                        incoming_entry["acknowledged_at"]
                        if incoming_entry is not None else None
                    ),
                    (
                        incoming_entry["ack_deadline_at"]
                        if incoming_entry is not None else None
                    ),
                    (
                        incoming_entry["ack_state"]
                        if incoming_entry is not None else None
                    ),
                    broker_state.get("add_submission_intent_id"),
                    broker_state.get("add_order_resolution_key"),
                    broker_state.get("add_order_acknowledged_at"),
                    broker_state.get("add_order_ack_deadline_at"),
                    broker_state.get("add_order_ack_state"),
                    campaign_id,
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
                **attribution,
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

    def reserve_risk_authorization(
        self,
        evidence: dict[str, Any],
        lease_seconds: int = 180,
    ) -> dict[str, Any]:
        """Atomically reserve the account's next pending order authorization.

        One short lease per account/session prevents overlapping automation
        wakes from spending the same notional or loss headroom before the first
        broker order becomes visible in a reconciled snapshot.
        """
        if lease_seconds <= 0 or lease_seconds > 180:
            raise ValueError("risk-authorization lease must be in 1..180 seconds")
        attribution = _normalize_pilot_attribution(evidence)
        if attribution["book_mode"] != "LIVE":
            raise ValueError("PAPER/SHADOW pilots cannot reserve broker risk")
        checked_at = datetime.fromisoformat(
            _aware_timestamp(evidence.get("checked_at"), "authorization checked_at")
        )
        snapshot_valid_until = datetime.fromisoformat(
            _aware_timestamp(
                evidence.get("broker_snapshot_valid_until"),
                "broker_snapshot_valid_until",
            )
        )
        expires = min(
            checked_at + timedelta(seconds=lease_seconds),
            snapshot_valid_until,
        )
        now_dt = datetime.now(timezone.utc)
        if expires <= now_dt + timedelta(seconds=5):
            raise ValueError(
                "broker snapshot expires before the minimum submission buffer"
            )
        expires_at = expires.isoformat()
        complete_evidence = {
            **evidence,
            "reservation_expires_at": expires_at,
            "reservation_scope": "one_active_per_account_session",
        }
        authorization_id = _canonical_hash(
            _risk_authorization_hash_evidence(complete_evidence)
        )
        authorization = {
            "authorization_id": authorization_id,
            **complete_evidence,
        }
        with self.transaction():
            transaction_now = datetime.now(timezone.utc)
            if expires <= transaction_now + timedelta(seconds=5):
                raise ValueError(
                    "broker snapshot expired while authorization was being reserved"
                )
            now = transaction_now.isoformat()
            stop_row = self.conn.execute(
                "SELECT * FROM operator_entry_stop WHERE latch_key='GLOBAL'"
            ).fetchone()
            if stop_row is None:
                raise ValueError("durable operator entry-stop state is missing")
            stop_chain_valid, stop_chain_error, _ = self._validate_entry_stop_chain(
                stop_row
            )
            if not stop_chain_valid:
                raise ValueError(
                    "operator emergency entry-stop chain is invalid: "
                    + str(stop_chain_error or "unknown chain error")
                )
            if bool(stop_row["engaged"]):
                raise ValueError("operator emergency entry stop is engaged")
            if int(authorization["emergency_entry_stop_generation"]) != int(
                stop_row["generation"]
            ):
                raise ValueError(
                    "operator emergency entry-stop generation changed before reservation"
                )
            if str(authorization["emergency_entry_stop_state_hash"]) != (
                self._entry_stop_state_hash(stop_row)
            ):
                raise ValueError(
                    "operator emergency entry-stop state changed before reservation"
                )
            latest = self.conn.execute(
                """SELECT strategy_version,pilot_id,book_mode,
                          decision_contract_version,decision_contract_hash,
                          broker_confirmed_at,loss_lock
                   FROM risk_sessions
                   WHERE account_key=? AND session_date=?""",
                (
                    str(authorization["account_key"]),
                    str(authorization["session_date"]),
                ),
            ).fetchone()
            if not latest:
                raise ValueError(
                    "risk authorization requires a current durable risk session"
                )
            if bool(latest["loss_lock"]):
                raise ValueError(
                    "risk authorization cannot reserve after the durable loss lock"
                )
            if str(latest["strategy_version"]) != str(
                authorization["strategy_version"]
            ):
                raise ValueError(
                    "risk authorization strategy does not match the current session"
                )
            for field, expected_value in attribution.items():
                if str(latest[field]) != expected_value:
                    raise ValueError(
                        f"risk authorization {field} does not match the current session"
                    )
            if _aware_timestamp(
                latest["broker_confirmed_at"],
                "current risk-session broker_confirmed_at",
            ) != _aware_timestamp(
                authorization["broker_confirmed_at"],
                "risk authorization broker_confirmed_at",
            ):
                raise ValueError(
                    "risk session changed before authorization could be reserved"
                )
            risk_action = str(authorization["risk_action"]).upper()
            if risk_action not in {"ENTRY", "ADD"}:
                raise ValueError("risk authorization action must be ENTRY or ADD")
            _validate_risk_gate_authorization(
                authorization,
                account_key=str(authorization["account_key"]),
                instrument_key=str(authorization["instrument_key"]),
                symbol=str(authorization["symbol"]).upper(),
                direction=str(authorization["direction"]).upper(),
                asset_class=str(authorization["asset_class"]).upper(),
                thesis_key=str(authorization["thesis_key"]).upper(),
                strategy_version=str(authorization["strategy_version"]),
                pilot_id=attribution["pilot_id"],
                book_mode=attribution["book_mode"],
                decision_contract_version=attribution[
                    "decision_contract_version"
                ],
                decision_contract_hash=attribution["decision_contract_hash"],
                expected_action=risk_action,
                order_submitted_at=str(authorization["checked_at"]),
            )
            _validate_risk_authorization_against_session(
                self.conn,
                authorization,
                account_key=str(authorization["account_key"]),
                strategy_version=str(authorization["strategy_version"]),
            )
            self.conn.execute(
                """UPDATE risk_authorizations
                   SET status='EXPIRED', release_reason='lease_expired'
                   WHERE status='ACTIVE' AND expires_at<=?""",
                (now,),
            )
            pending = self.conn.execute(
                """SELECT authorization_id,instrument_key,thesis_key,risk_action,
                          status,created_at,expires_at,broker_order_id,campaign_id
                   FROM risk_authorizations
                   WHERE account_key=? AND session_date=?
                     AND status IN ('ACTIVE','SUBMISSION_UNKNOWN','CONSUMED')
                   LIMIT 1""",
                (
                    str(authorization["account_key"]),
                    str(authorization["session_date"]),
                ),
            ).fetchone()
            if pending:
                return {
                    "reserved": False,
                    "reason": (
                        "another pending order authorization is active or awaiting "
                        "a newer broker reconciliation"
                    ),
                    "active_authorization": dict(pending),
                }
            self.conn.execute(
                """INSERT INTO risk_authorizations(
                       authorization_id,account_key,session_date,strategy_version,
                       pilot_id,book_mode,decision_contract_version,
                       decision_contract_hash,
                       instrument_key,thesis_key,risk_action,status,created_at,
                       expires_at,evidence_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    authorization_id,
                    str(authorization["account_key"]),
                    str(authorization["session_date"]),
                    str(authorization["strategy_version"]),
                    attribution["pilot_id"], attribution["book_mode"],
                    attribution["decision_contract_version"],
                    attribution["decision_contract_hash"],
                    str(authorization["instrument_key"]),
                    str(authorization["thesis_key"]),
                    str(authorization["risk_action"]),
                    "ACTIVE",
                    str(authorization["checked_at"]),
                    expires_at,
                    json.dumps(authorization, sort_keys=True, separators=(",", ":")),
                ),
            )
        return {"reserved": True, "authorization": authorization}

    def mark_risk_submission_unknown(
        self,
        authorization_id: str,
        *,
        attempted_at: str,
        reason: str,
    ) -> dict[str, Any]:
        """Freeze the one permitted broker submission intent before the call.

        The executor invokes this immediately before leaving the process for
        the broker.  The resulting append-only intent fixes both the attempt
        time and broker-ack deadline; timeout/unknown responses cannot expire
        back into reusable risk capacity.
        """
        authorization_id = str(authorization_id).strip()
        reason = str(reason).strip()
        if not authorization_id or not reason:
            raise ValueError("authorization_id and unknown-submission reason are required")
        attempted = datetime.fromisoformat(
            _aware_timestamp(attempted_at, "submission attempted_at")
        )
        result_row: sqlite3.Row | None = None
        intent_row: sqlite3.Row | None = None
        with self.transaction():
            row = self.conn.execute(
                "SELECT * FROM risk_authorizations WHERE authorization_id=?",
                (authorization_id,),
            ).fetchone()
            if row is None:
                raise ValueError("unknown risk authorization")
            base_authorization = json.loads(str(row["evidence_json"]))
            prior_intent = self.conn.execute(
                "SELECT * FROM risk_submission_intents WHERE authorization_id=?",
                (authorization_id,),
            ).fetchone()
            if str(row["status"]) == "SUBMISSION_UNKNOWN":
                if prior_intent is None:
                    raise ValueError(
                        "submission-unknown authorization is missing its immutable intent"
                    )
                if str(prior_intent["attempted_at"]) != attempted.isoformat():
                    raise ValueError("submission intent timestamp is immutable")
                prior_intent_payload = json.loads(
                    str(prior_intent["payload_json"])
                )
                if str(prior_intent_payload.get("reason") or "") != reason:
                    raise ValueError("submission intent reason is immutable")
            elif str(row["status"]) != "ACTIVE":
                raise ValueError(
                    f"cannot mark submission unknown from status {row['status']}"
                )
            else:
                created = datetime.fromisoformat(str(row["created_at"]))
                expires = datetime.fromisoformat(str(row["expires_at"]))
                if attempted < created - timedelta(seconds=1) or attempted > expires:
                    raise ValueError(
                        "unknown submission attempt must occur within the authorization lease"
                    )
                timeout = _finite_decimal(
                    base_authorization.get("broker_ack_timeout_seconds"),
                    "broker_ack_timeout_seconds",
                )
                if timeout != 10:
                    raise ValueError("broker_ack_timeout_seconds must be exactly 10")
                deadline = attempted + timedelta(seconds=10)
                effective_authorization = {
                    **base_authorization,
                    "submission_intent_at": attempted.isoformat(),
                    "broker_ack_deadline_at": deadline.isoformat(),
                }
                if _canonical_hash(
                    _risk_authorization_hash_evidence(effective_authorization)
                ) != authorization_id:
                    raise ValueError(
                        "submission intent does not match durable authorization identity"
                    )
                intent_payload = {
                    "schema_version": "titan_risk_submission_intent_2026-08-23_v1",
                    "authorization_id": authorization_id,
                    "authorization": effective_authorization,
                    "attempted_at": attempted.isoformat(),
                    "broker_ack_deadline_at": deadline.isoformat(),
                    "broker_ack_timeout_seconds": 10,
                    "reason": reason,
                    "duplicate_submission_allowed": False,
                }
                intent_hash = _canonical_hash(intent_payload)
                self.conn.execute(
                    """INSERT INTO risk_submission_intents(
                           intent_id,authorization_id,attempted_at,
                           broker_ack_deadline_at,intent_hash,payload_json,recorded_at
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        intent_hash, authorization_id, attempted.isoformat(),
                        deadline.isoformat(), intent_hash,
                        json.dumps(
                            intent_payload, sort_keys=True, separators=(",", ":")
                        ),
                        utc_now(),
                    ),
                )
                self.conn.execute(
                    """UPDATE risk_authorizations
                       SET status='SUBMISSION_UNKNOWN',bound_at=?,release_reason=?
                       WHERE authorization_id=? AND status='ACTIVE'""",
                    (attempted.isoformat(), reason, authorization_id),
                )
            result_row = self.conn.execute(
                "SELECT * FROM risk_authorizations WHERE authorization_id=?",
                (authorization_id,),
            ).fetchone()
            intent_row = self.conn.execute(
                "SELECT * FROM risk_submission_intents WHERE authorization_id=?",
                (authorization_id,),
            ).fetchone()
        assert result_row is not None and intent_row is not None
        result = dict(result_row)
        intent = dict(intent_row)
        intent["payload"] = json.loads(intent.pop("payload_json"))
        result["evidence"] = intent["payload"]["authorization"]
        result.pop("evidence_json")
        result["submission_intent"] = intent
        result["trade_authority"] = False
        return result

    def bind_risk_authorization(
        self,
        authorization: dict[str, Any],
        *,
        campaign_id: str,
        broker_order_id: str,
        order_submitted_at: str,
        submission_intent_id: str,
        order_resolution_key: str,
    ) -> None:
        """Bind a serialized attempt only after broker-snapshot ORDER_FOUND."""
        authorization_id = str(authorization.get("authorization_id") or "")
        if not authorization_id or not broker_order_id:
            raise ValueError("risk authorization and broker order ID are required")
        row = self.conn.execute(
            "SELECT * FROM risk_authorizations WHERE authorization_id=?",
            (authorization_id,),
        ).fetchone()
        if not row:
            raise ValueError("risk authorization lease is not durable")
        stored = json.loads(str(row["evidence_json"]))
        if _risk_authorization_hash_evidence(stored) != (
            _risk_authorization_hash_evidence(authorization)
        ):
            raise ValueError("risk authorization does not match durable lease evidence")
        intent = self.conn.execute(
            """SELECT * FROM risk_submission_intents
               WHERE authorization_id=? AND intent_id=?""",
            (authorization_id, str(submission_intent_id)),
        ).fetchone()
        if intent is None:
            raise ValueError("risk authorization has no matching immutable submission intent")
        intent_payload = json.loads(str(intent["payload_json"]))
        if intent_payload.get("authorization") != authorization:
            raise ValueError("campaign authorization does not match immutable intent facts")
        if str(row["status"]) == "CONSUMED":
            if (
                str(row["campaign_id"]) != campaign_id
                or str(row["broker_order_id"]) != broker_order_id
            ):
                raise ValueError("risk authorization is already bound to another order")
            return
        resolution = self.conn.execute(
            """SELECT * FROM risk_unknown_resolutions
               WHERE resolution_key=? AND intent_id=? AND authorization_id=?""",
            (
                str(order_resolution_key), str(submission_intent_id),
                authorization_id,
            ),
        ).fetchone()
        if resolution is None or str(resolution["resolution_state"]) != "ORDER_FOUND":
            raise ValueError(
                "campaign bind requires broker-snapshot-derived ORDER_FOUND resolution"
            )
        if str(resolution["broker_order_id"]) != broker_order_id:
            raise ValueError("ORDER_FOUND broker order does not match campaign")
        submitted = datetime.fromisoformat(
            _aware_timestamp(order_submitted_at, "broker order_submitted_at")
        )
        if submitted.isoformat() != str(intent["attempted_at"]):
            raise ValueError("broker submission time does not match immutable intent")
        expires = datetime.fromisoformat(str(row["expires_at"]))
        if submitted > expires:
            raise ValueError("broker order was submitted after authorization lease expired")
        if str(row["status"]) != "SUBMISSION_UNKNOWN":
            raise ValueError(
                f"risk authorization is not awaiting exact campaign bind: {row['status']}"
            )
        self.conn.execute(
            """UPDATE risk_authorizations
               SET status='CONSUMED',bound_at=?,broker_order_id=?,campaign_id=?
               WHERE authorization_id=? AND status='SUBMISSION_UNKNOWN'""",
            (
                _aware_timestamp(order_submitted_at, "broker order_submitted_at"),
                broker_order_id,
                campaign_id,
                authorization_id,
            ),
        )

    def release_risk_authorization(
        self, authorization_id: str, reason: str
    ) -> dict[str, Any]:
        authorization_id = str(authorization_id).strip()
        reason = str(reason).strip()
        if not authorization_id or not reason:
            raise ValueError("authorization_id and release reason are required")
        now = utc_now()
        with self.transaction():
            self.conn.execute(
                """UPDATE risk_authorizations
                   SET status='EXPIRED',release_reason='lease_expired'
                   WHERE status='ACTIVE' AND expires_at<=?""",
                (now,),
            )
            row = self.conn.execute(
                "SELECT * FROM risk_authorizations WHERE authorization_id=?",
                (authorization_id,),
            ).fetchone()
            if not row:
                raise ValueError("unknown risk authorization")
            if str(row["status"]) == "ACTIVE":
                self.conn.execute(
                    """UPDATE risk_authorizations
                       SET status='RELEASED',release_reason=?
                       WHERE authorization_id=? AND status='ACTIVE'""",
                    (reason, authorization_id),
                )
            elif str(row["status"]) != "RELEASED":
                raise ValueError(
                    f"cannot release risk authorization in status {row['status']}"
                )
            final = self.conn.execute(
                "SELECT * FROM risk_authorizations WHERE authorization_id=?",
                (authorization_id,),
            ).fetchone()
        assert final is not None
        result = dict(final)
        result["evidence"] = json.loads(result.pop("evidence_json"))
        result["trade_authority"] = False
        return result

    def risk_authorizations(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT * FROM risk_authorizations
               ORDER BY created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["evidence"] = json.loads(item.pop("evidence_json"))
            intent = self.conn.execute(
                "SELECT * FROM risk_submission_intents WHERE authorization_id=?",
                (item["authorization_id"],),
            ).fetchone()
            if intent is not None:
                intent_item = dict(intent)
                intent_item["payload"] = json.loads(intent_item.pop("payload_json"))
                item["submission_intent"] = intent_item
                item["evidence"] = intent_item["payload"]["authorization"]
            resolution = self.conn.execute(
                """SELECT * FROM risk_unknown_resolutions
                   WHERE authorization_id=?""",
                (item["authorization_id"],),
            ).fetchone()
            if resolution is not None:
                resolution_item = dict(resolution)
                resolution_item["evidence"] = json.loads(
                    resolution_item.pop("evidence_json")
                )
                item["unknown_resolution"] = resolution_item
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
            "pilot_id",
            "book_mode",
            "decision_contract_version",
            "decision_contract_hash",
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
        attribution = _normalize_pilot_attribution(payload)
        if attribution["book_mode"] != "LIVE":
            raise ValueError("broker risk sessions are LIVE-only")
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
        unknown_resolutions = broker_state.get("submission_unknown_resolutions", [])
        if not isinstance(unknown_resolutions, list):
            raise ValueError(
                "broker_state.submission_unknown_resolutions must be a list"
            )
        for resolution in unknown_resolutions:
            if not isinstance(resolution, dict):
                raise ValueError("each submission unknown resolution must be an object")
        required_broker_balances = (
            "unleveraged_buying_power_dollars",
            "current_gross_exposure_dollars",
            "working_entry_notional_dollars",
        )
        missing_balances = [
            field for field in required_broker_balances
            if broker_state.get(field) is None
        ]
        if missing_balances:
            raise ValueError(
                "risk-session broker balances are incomplete: "
                + ", ".join(missing_balances)
            )
        for field in required_broker_balances:
            if _finite_float(broker_state[field], field) < 0:
                raise ValueError(f"{field} cannot be negative")
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
                for field, expected_value in attribution.items():
                    if str(existing[field]) != expected_value:
                        raise ValueError(f"{field} is immutable for the session")
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
                       account_key,session_date,strategy_version,pilot_id,book_mode,
                       decision_contract_version,decision_contract_hash,start_of_day_equity,
                       baseline_confirmed_at,current_equity,realized_net_pnl,
                       confirmed_cash_flow_adjustment,account_day_pnl,loss_gauge,
                       loss_limit_dollars,loss_lock,loss_lock_triggered_at,
                       profit_objective_dollars,profit_objective_reached,
                       profit_objective_reached_at,active_profit_floor_dollars,
                       updated_at,broker_confirmed_at,broker_state_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                    attribution["pilot_id"],
                    attribution["book_mode"],
                    attribution["decision_contract_version"],
                    attribution["decision_contract_hash"],
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
            snapshot = {
                "account_key": account_key,
                "session_date": session_date,
                "strategy_version": strategy_version,
                **attribution,
                "start_of_day_equity": start_equity,
                "baseline_confirmed_at": baseline_confirmed_at,
                "current_equity": current_equity,
                "realized_net_pnl": realized_net_pnl,
                "confirmed_cash_flow_adjustment": cash_flow_adjustment,
                "account_day_pnl": account_day_pnl,
                "loss_gauge": loss_gauge,
                "loss_limit_dollars": -100.0,
                "loss_lock": loss_lock,
                "profit_objective_reached": objective_reached,
                "active_profit_floor_dollars": profit_floor,
                "broker_confirmed_at": broker_confirmed_at,
                "broker_state": broker_state,
            }
            snapshot_json = json.dumps(
                snapshot, sort_keys=True, separators=(",", ":")
            )
            snapshot_hash = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
            self.conn.execute(
                """INSERT OR IGNORE INTO risk_session_snapshots(
                       account_key,session_date,broker_confirmed_at,snapshot_hash,
                       snapshot_json,created_at
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    account_key, session_date, broker_confirmed_at,
                    snapshot_hash, snapshot_json, now,
                ),
            )
            stored_snapshot = self.conn.execute(
                """SELECT snapshot_hash FROM risk_session_snapshots
                   WHERE account_key=? AND session_date=? AND broker_confirmed_at=?""",
                (account_key, session_date, broker_confirmed_at),
            ).fetchone()
            if (
                not stored_snapshot
                or str(stored_snapshot["snapshot_hash"]) != snapshot_hash
            ):
                raise ValueError(
                    "conflicting immutable risk snapshots share broker_confirmed_at"
                )
            for raw_resolution in unknown_resolutions:
                required_resolution_fields = (
                    "resolution_key", "authorization_id", "intent_id",
                    "resolution_state", "broker_confirmed_at", "evidence",
                )
                missing_resolution = [
                    field for field in required_resolution_fields
                    if raw_resolution.get(field) is None
                ]
                if missing_resolution:
                    raise ValueError(
                        "submission unknown resolution is missing: "
                        + ", ".join(missing_resolution)
                    )
                resolution_key = str(raw_resolution["resolution_key"]).strip()
                authorization_id = str(
                    raw_resolution["authorization_id"]
                ).strip()
                intent_id = str(raw_resolution["intent_id"]).strip()
                resolution_state = str(
                    raw_resolution["resolution_state"]
                ).strip().upper()
                resolution_confirmed_at = _aware_timestamp(
                    raw_resolution["broker_confirmed_at"],
                    "submission unknown resolution broker_confirmed_at",
                )
                if not resolution_key or not authorization_id or not intent_id:
                    raise ValueError(
                        "submission unknown resolution keys cannot be empty"
                    )
                if resolution_state not in {
                    "ORDER_FOUND", "NO_ORDER_CONFIRMED"
                }:
                    raise ValueError(
                        "resolution_state must be ORDER_FOUND or NO_ORDER_CONFIRMED"
                    )
                if resolution_confirmed_at != broker_confirmed_at:
                    raise ValueError(
                        "unknown resolution must use the exact current broker snapshot"
                    )
                resolution_evidence = raw_resolution["evidence"]
                if not isinstance(resolution_evidence, dict):
                    raise ValueError("unknown resolution evidence must be an object")
                for field in ("matching_order_count", "matching_position_count"):
                    if field not in resolution_evidence:
                        raise ValueError(
                            f"unknown resolution evidence requires {field}"
                        )
                    value = _finite_decimal(
                        resolution_evidence[field],
                        f"unknown resolution evidence {field}",
                    )
                    if value < 0 or value != value.to_integral_value():
                        raise ValueError(
                            f"unknown resolution evidence {field} must be a nonnegative integer"
                        )
                matching_orders = int(resolution_evidence["matching_order_count"])
                matching_positions = int(
                    resolution_evidence["matching_position_count"]
                )
                broker_order_id = str(
                    raw_resolution.get("broker_order_id") or ""
                ).strip()
                authorization_row = self.conn.execute(
                    """SELECT * FROM risk_authorizations
                       WHERE authorization_id=?""",
                    (authorization_id,),
                ).fetchone()
                intent_row = self.conn.execute(
                    """SELECT * FROM risk_submission_intents
                       WHERE intent_id=? AND authorization_id=?""",
                    (intent_id, authorization_id),
                ).fetchone()
                if authorization_row is None or intent_row is None:
                    raise ValueError(
                        "unknown resolution does not match a durable authorization intent"
                    )
                if (
                    str(authorization_row["account_key"]) != account_key
                    or str(authorization_row["session_date"]) != session_date
                ):
                    raise ValueError(
                        "unknown resolution authorization is outside this account session"
                    )
                if str(authorization_row["status"]) not in {
                    "SUBMISSION_UNKNOWN", "RECONCILED_NO_ORDER", "CONSUMED",
                    "RECONCILED",
                }:
                    raise ValueError(
                        "unknown resolution authorization is not a submitted attempt"
                    )
                if datetime.fromisoformat(broker_confirmed_at) <= datetime.fromisoformat(
                    str(intent_row["attempted_at"])
                ):
                    raise ValueError(
                        "unknown resolution requires a strictly newer broker snapshot"
                    )
                if resolution_state == "NO_ORDER_CONFIRMED":
                    if broker_order_id or matching_orders or matching_positions:
                        raise ValueError(
                            "NO_ORDER_CONFIRMED requires zero matches and no broker order ID"
                        )
                elif not broker_order_id or matching_orders + matching_positions <= 0:
                    raise ValueError(
                        "ORDER_FOUND requires a broker order ID and a positive exact match"
                    )
                normalized_resolution = {
                    "resolution_key": resolution_key,
                    "intent_id": intent_id,
                    "authorization_id": authorization_id,
                    "resolution_state": resolution_state,
                    "broker_confirmed_at": broker_confirmed_at,
                    "broker_order_id": broker_order_id or None,
                    "evidence": resolution_evidence,
                    "risk_snapshot_hash": snapshot_hash,
                    "orders_reconciled": True,
                    "positions_reconciled": True,
                }
                resolution_hash = _canonical_hash(normalized_resolution)
                prior_resolution = self.conn.execute(
                    """SELECT * FROM risk_unknown_resolutions
                       WHERE authorization_id=? OR resolution_key=?""",
                    (authorization_id, resolution_key),
                ).fetchone()
                if prior_resolution is not None:
                    if (
                        str(prior_resolution["resolution_hash"])
                        != resolution_hash
                    ):
                        raise ValueError(
                            "conflicting immutable unknown-submission resolution"
                        )
                else:
                    self.conn.execute(
                        """INSERT INTO risk_unknown_resolutions(
                               resolution_key,intent_id,authorization_id,
                               resolution_state,broker_confirmed_at,broker_order_id,
                               evidence_json,resolution_hash,recorded_at
                           ) VALUES(?,?,?,?,?,?,?,?,?)""",
                        (
                            resolution_key, intent_id, authorization_id,
                            resolution_state, broker_confirmed_at,
                            broker_order_id or None,
                            json.dumps(
                                normalized_resolution,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                            resolution_hash, now,
                        ),
                    )
                if resolution_state == "NO_ORDER_CONFIRMED":
                    self.conn.execute(
                        """UPDATE risk_authorizations
                           SET status='RECONCILED_NO_ORDER',reconciled_at=?,
                               release_reason='exact_newer_snapshot_confirmed_no_order'
                           WHERE authorization_id=?
                             AND status='SUBMISSION_UNKNOWN'""",
                        (now, authorization_id),
                    )
                else:
                    # ORDER_FOUND remains serialized until the exact campaign
                    # bind consumes the same authorization, intent, and key.
                    self.conn.execute(
                        """UPDATE risk_authorizations
                           SET broker_order_id=?,reconciled_at=?,
                               release_reason='exact_newer_snapshot_found_order_pending_bind'
                           WHERE authorization_id=?
                             AND status='SUBMISSION_UNKNOWN'""",
                        (broker_order_id, now, authorization_id),
                    )
            # A consumed reservation remains pending until a strictly newer,
            # fully reconciled broker snapshot can see the submitted order or
            # resulting position.  This prevents a second wake from reusing the
            # same buying power/loss headroom in the submit-to-reconcile gap.
            self.conn.execute(
                """UPDATE risk_authorizations
                   SET status='RECONCILED',reconciled_at=?,
                       release_reason='newer_broker_snapshot_reconciled'
                   WHERE account_key=? AND session_date=? AND status='CONSUMED'
                     AND bound_at IS NOT NULL AND bound_at<?""",
                (now, account_key, session_date, broker_confirmed_at),
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
        snapshot = self.conn.execute(
            """SELECT snapshot_hash FROM risk_session_snapshots
               WHERE account_key=? AND session_date=? AND broker_confirmed_at=?""",
            (account_key, session_date, item["broker_confirmed_at"]),
        ).fetchone()
        item["broker_snapshot_hash"] = (
            str(snapshot["snapshot_hash"]) if snapshot is not None else None
        )
        item["loss_lock"] = bool(item["loss_lock"])
        item["profit_objective_reached"] = bool(item["profit_objective_reached"])
        item["loss_headroom_to_lock"] = max(
            0.0,
            float(item["loss_gauge"]) - float(item["loss_limit_dollars"]),
        )
        item["post_objective_new_risk_buffer"] = (
            max(0.0, float(item["account_day_pnl"]) - 125.0)
            if item["profit_objective_reached"]
            else None
        )
        item["new_entries_allowed"] = bool(
            not item["loss_lock"]
            and float(item["loss_headroom_to_lock"]) > 0
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

    @staticmethod
    def _entry_stop_state_hash(row: sqlite3.Row | dict[str, Any]) -> str:
        state = {
            key: row[key]
            for key in (
                "latch_key", "engaged", "generation", "reason", "changed_by",
                "changed_at",
            )
        }
        state["engaged"] = bool(state["engaged"])
        return hashlib.sha256(
            json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def _validate_entry_stop_chain(
        self, latch: sqlite3.Row | dict[str, Any]
    ) -> tuple[bool, str | None, str | None]:
        events = self.conn.execute(
            """SELECT * FROM operator_entry_stop_events
               ORDER BY generation ASC"""
        ).fetchall()
        if not events:
            return False, "operator entry-stop event chain is missing", None
        prior_hash = ZERO_SHA256
        expected_generation = 1
        latest: sqlite3.Row | None = None
        for event in events:
            generation = int(event["generation"])
            if generation != expected_generation:
                return False, "operator entry-stop generations are not contiguous", None
            base = {
                "generation": generation,
                "action": str(event["action"]),
                "reason": str(event["reason"]),
                "changed_by": str(event["changed_by"]),
                "changed_at": str(event["changed_at"]),
            }
            recorded_previous = str(event["previous_event_hash"])
            chained = {**base, "previous_event_hash": prior_hash}
            chained_hash = _canonical_hash(chained)
            legacy_hash = _canonical_hash(base)
            actual_hash = str(event["event_hash"])
            if recorded_previous == prior_hash and actual_hash == chained_hash:
                pass
            elif recorded_previous == ZERO_SHA256 and actual_hash == legacy_hash:
                # Additive migration compatibility for events written before
                # the chain column existed.  Every legacy event is still
                # individually authenticated and generations stay contiguous.
                pass
            else:
                return False, "operator entry-stop event chain hash mismatch", None
            prior_hash = actual_hash
            expected_generation += 1
            latest = event
        assert latest is not None
        if int(latch["generation"]) != int(latest["generation"]):
            return False, "operator entry-stop latch generation mismatches chain", None
        expected_engaged = str(latest["action"]) == "ENGAGED"
        if str(latest["action"]) == "INITIALIZED":
            expected_engaged = False
        if bool(latch["engaged"]) != expected_engaged:
            return False, "operator entry-stop latch state mismatches chain", None
        for field in ("reason", "changed_by", "changed_at"):
            if str(latch[field]) != str(latest[field]):
                return False, f"operator entry-stop latch {field} mismatches chain", None
        return True, None, prior_hash

    def entry_stop_status(self) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM operator_entry_stop WHERE latch_key='GLOBAL'"
        ).fetchone()
        if row is None:
            # A missing durable latch is a corrupt/mid-migration state.  Never
            # interpret it as permission to submit a new order.
            return {
                "latch_key": "GLOBAL",
                "engaged": True,
                "effective_entry_stop": True,
                "reason": "durable operator entry-stop state is missing",
                "state_valid": False,
                "new_entries_allowed": False,
                "broker_state_mutated": False,
                "trade_authority": False,
            }
        item = dict(row)
        item["engaged"] = bool(item["engaged"])
        item["state_hash"] = self._entry_stop_state_hash(row)
        chain_valid, chain_error, latest_event_hash = (
            self._validate_entry_stop_chain(row)
        )
        item["chain_valid"] = chain_valid
        item["chain_error"] = chain_error
        item["latest_event_hash"] = latest_event_hash
        item["effective_entry_stop"] = bool(item["engaged"] or not chain_valid)
        item["state_valid"] = chain_valid
        item["new_entries_allowed"] = not item["effective_entry_stop"]
        item["broker_state_mutated"] = False
        item["trade_authority"] = False
        return item

    def _entry_stop_engaged(self, connection: sqlite3.Connection) -> bool:
        row = connection.execute(
            "SELECT engaged FROM operator_entry_stop WHERE latch_key='GLOBAL'"
        ).fetchone()
        if row is None:
            raise ValueError("durable operator entry-stop state is missing")
        return bool(row["engaged"])

    def set_entry_stop(
        self,
        *,
        engaged: bool,
        reason: str,
        changed_by: str,
    ) -> dict[str, Any]:
        """Durably engage the entry latch without touching the broker.

        Engaging also releases any still-unsubmitted ACTIVE local reservation.
        CONSUMED authorizations remain available for reconciliation because
        their broker submissions already occurred before the latch changed.
        """
        reason = str(reason).strip()
        changed_by = str(changed_by).strip()
        if not reason or not changed_by:
            raise ValueError("entry-stop reason and changed_by are required")
        if not engaged:
            raise ValueError(
                "PROTECTED_USER_ONLY release is unavailable through titan runtime"
            )
        action = "ENGAGED"
        with self.transaction():
            prior = self.conn.execute(
                "SELECT * FROM operator_entry_stop WHERE latch_key='GLOBAL'"
            ).fetchone()
            if prior is None:
                raise ValueError("durable operator entry-stop state is missing")
            chain_valid, chain_error, previous_event_hash = (
                self._validate_entry_stop_chain(prior)
            )
            if not chain_valid or previous_event_hash is None:
                raise ValueError(
                    "operator entry-stop chain is invalid: "
                    + str(chain_error or "unknown chain error")
                )
            if bool(prior["engaged"]) == bool(engaged):
                result = self.entry_stop_status()
                result["idempotent_replay"] = True
                return result
            generation = int(prior["generation"]) + 1
            changed_at = utc_now()
            event = {
                "generation": generation,
                "action": action,
                "reason": reason,
                "changed_by": changed_by,
                "changed_at": changed_at,
                "previous_event_hash": previous_event_hash,
            }
            event_hash = hashlib.sha256(
                json.dumps(
                    event, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest()
            self.conn.execute(
                """UPDATE operator_entry_stop
                   SET engaged=?,generation=?,reason=?,changed_by=?,changed_at=?
                   WHERE latch_key='GLOBAL'""",
                (int(engaged), generation, reason, changed_by, changed_at),
            )
            self.conn.execute(
                """INSERT INTO operator_entry_stop_events(
                       event_id,generation,action,reason,changed_by,changed_at,
                       previous_event_hash,event_hash
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    str(uuid.uuid4()), generation, action, reason, changed_by,
                    changed_at, previous_event_hash, event_hash,
                ),
            )
            released_authorizations = 0
            if engaged:
                cursor = self.conn.execute(
                    """UPDATE risk_authorizations
                       SET status='RELEASED',release_reason='operator_entry_stop_engaged'
                       WHERE status='ACTIVE'"""
                )
                released_authorizations = cursor.rowcount
        result = self.entry_stop_status()
        result["idempotent_replay"] = False
        result["released_unsubmitted_authorization_count"] = released_authorizations
        return result

    def entry_stop_events(self, limit: int = 20) -> list[dict[str, Any]]:
        if limit <= 0:
            raise ValueError("entry-stop event limit must be positive")
        rows = self.conn.execute(
            """SELECT * FROM operator_entry_stop_events
               ORDER BY generation DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _pilot_fact_sheet_item(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        item["reporting_only"] = True
        item["trade_authority"] = False
        item["capital_reallocation_authority"] = False
        return item

    def record_pilot_fact_sheet(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Append one machine-readable, mode-isolated Pilot fact sheet."""
        if not isinstance(payload, dict):
            raise ValueError("pilot fact sheet must be a JSON object")
        required = {
            "pilot_id", "pilot_name", "book_mode", "fact_sheet_version",
            "decision_contract_version", "decision_contract_hash", "policy_hash",
            "measured_through", "evidence_status", "metrics",
            "known_failure_modes", "evidence",
        }
        missing = sorted(field for field in required if field not in payload)
        if missing:
            raise ValueError("missing pilot fact-sheet fields: " + ", ".join(missing))
        attribution = _normalize_pilot_attribution(payload)
        pilot_name = str(payload["pilot_name"]).strip()
        fact_sheet_version = str(payload["fact_sheet_version"]).strip()
        if not pilot_name or not fact_sheet_version:
            raise ValueError("pilot_name and fact_sheet_version cannot be empty")
        policy_hash = _sha256_hex(payload["policy_hash"], "policy_hash")
        measured_through = _aware_timestamp(
            payload["measured_through"], "measured_through"
        )
        evidence_status = str(payload["evidence_status"]).strip().upper()
        if evidence_status not in {"INSUFFICIENT", "ESTIMABLE"}:
            raise ValueError("evidence_status must be INSUFFICIENT or ESTIMABLE")
        metrics = payload["metrics"]
        if not isinstance(metrics, dict):
            raise ValueError("pilot fact-sheet metrics must be an object")
        analytical_metrics = {
            "net_expectancy_r_after_costs",
            "clustered_95pct_lower_bound_expectancy_r",
            "clustered_95pct_upper_bound_expectancy_r",
            "win_rate_pct",
            "expected_shortfall_95_r",
            "expected_shortfall_99_r",
            "max_drawdown_r",
            "profit_factor",
            "execution_shortfall_bps",
            "entry_slippage_bps",
            "exit_slippage_bps",
            "top_day_profit_concentration_pct",
            "largest_winner_profit_concentration_pct",
        }
        evidence_metrics = {
            "effective_independent_sample_size",
            "evidence_coverage_pct",
            "quote_coverage_pct",
            "fill_rate_pct",
            "no_fill_rate_pct",
            "stale_data_rate_pct",
            "order_reject_rate_pct",
            "position_episode_count",
            "session_count",
            "underlying_count",
            "distinct_ticker_session_count",
            "control_breach_count",
        }
        required_metrics = analytical_metrics | evidence_metrics
        missing_metrics = sorted(required_metrics - set(metrics))
        if missing_metrics:
            raise ValueError(
                "missing pilot fact-sheet metrics: " + ", ".join(missing_metrics)
            )
        unknown_metrics = sorted(set(metrics) - required_metrics)
        if unknown_metrics:
            raise ValueError(
                "unknown pilot fact-sheet metrics: " + ", ".join(unknown_metrics)
            )
        normalized_metrics: dict[str, float | int | None] = {}
        integer_metrics = {
            "position_episode_count", "session_count", "underlying_count",
            "distinct_ticker_session_count", "control_breach_count",
        }
        nonnegative_metrics = {
            "expected_shortfall_95_r", "expected_shortfall_99_r",
            "max_drawdown_r", "profit_factor", "execution_shortfall_bps",
            "effective_independent_sample_size", "evidence_coverage_pct",
            "quote_coverage_pct", "fill_rate_pct", "no_fill_rate_pct",
            "stale_data_rate_pct", "order_reject_rate_pct",
            "top_day_profit_concentration_pct",
            "largest_winner_profit_concentration_pct",
            *integer_metrics,
        }
        for field in required_metrics:
            if field in analytical_metrics and metrics[field] is None:
                normalized_metrics[field] = None
                continue
            value = _finite_decimal(metrics[field], f"metrics.{field}")
            if field in nonnegative_metrics and value < 0:
                raise ValueError(f"metrics.{field} cannot be negative")
            if field.endswith("_pct") and value > 100:
                raise ValueError(f"metrics.{field} cannot exceed 100")
            if field in integer_metrics:
                if value != value.to_integral_value():
                    raise ValueError(f"metrics.{field} must be an integer")
                normalized_metrics[field] = int(value)
            else:
                normalized_metrics[field] = float(value)
        if (
            int(normalized_metrics["position_episode_count"] or 0) > 0
            and abs(
                float(normalized_metrics["fill_rate_pct"] or 0)
                + float(normalized_metrics["no_fill_rate_pct"] or 0)
                - 100.0
            ) > 0.01
        ):
            raise ValueError("fill_rate_pct plus no_fill_rate_pct must equal 100")
        if int(normalized_metrics["distinct_ticker_session_count"] or 0) > int(
            normalized_metrics["position_episode_count"] or 0
        ):
            raise ValueError(
                "distinct_ticker_session_count cannot exceed position_episode_count"
            )
        analytical_values = [normalized_metrics[field] for field in analytical_metrics]
        if evidence_status == "INSUFFICIENT":
            if any(value is not None for value in analytical_values):
                raise ValueError(
                    "INSUFFICIENT fact sheets must leave analytical metrics null"
                )
        else:
            if any(value is None for value in analytical_values):
                raise ValueError(
                    "ESTIMABLE fact sheets require all analytical metrics"
                )
            if float(
                normalized_metrics["clustered_95pct_lower_bound_expectancy_r"]
            ) > float(
                normalized_metrics["clustered_95pct_upper_bound_expectancy_r"]
            ):
                raise ValueError(
                    "clustered 95pct expectancy lower bound cannot exceed upper bound"
                )
            if float(normalized_metrics["effective_independent_sample_size"] or 0) <= 0:
                raise ValueError("ESTIMABLE fact sheets require positive effective sample size")
            if (
                int(normalized_metrics["position_episode_count"] or 0) < 100
                or int(normalized_metrics["session_count"] or 0) < 40
                or int(normalized_metrics["underlying_count"] or 0) < 30
            ):
                raise ValueError(
                    "ESTIMABLE fact sheets require at least 100 episodes, "
                    "40 sessions, and 30 independent underlyings"
                )
        failure_modes = payload["known_failure_modes"]
        if not isinstance(failure_modes, list) or any(
            not isinstance(value, str) or not value.strip() for value in failure_modes
        ):
            raise ValueError("known_failure_modes must be a list of nonempty strings")
        evidence = payload["evidence"]
        if not isinstance(evidence, dict) or not evidence:
            raise ValueError("pilot fact-sheet evidence must be a nonempty object")
        normalized_payload = {
            **attribution,
            "pilot_name": pilot_name,
            "fact_sheet_version": fact_sheet_version,
            "policy_hash": policy_hash,
            "measured_through": measured_through,
            "evidence_status": evidence_status,
            "metrics": normalized_metrics,
            "known_failure_modes": [value.strip() for value in failure_modes],
            "evidence": evidence,
        }
        canonical_json = json.dumps(
            normalized_payload, sort_keys=True, separators=(",", ":")
        )
        payload_hash = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
        with self.transaction():
            replay = self.conn.execute(
                "SELECT * FROM pilot_fact_sheets WHERE payload_hash=?",
                (payload_hash,),
            ).fetchone()
            if replay is not None:
                result = self._pilot_fact_sheet_item(replay)
                result["idempotent_replay"] = True
                return result
            conflicting = self.conn.execute(
                """SELECT fact_sheet_id FROM pilot_fact_sheets
                   WHERE pilot_id=? AND book_mode=? AND fact_sheet_version=?""",
                (
                    attribution["pilot_id"], attribution["book_mode"],
                    fact_sheet_version,
                ),
            ).fetchone()
            if conflicting is not None:
                raise ValueError(
                    "a different immutable fact sheet already uses this pilot/mode/version"
                )
            fact_sheet_id = payload_hash
            self.conn.execute(
                """INSERT INTO pilot_fact_sheets(
                       fact_sheet_id,pilot_id,pilot_name,book_mode,fact_sheet_version,
                       decision_contract_version,decision_contract_hash,measured_through,
                       recorded_at,payload_hash,payload_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    fact_sheet_id, attribution["pilot_id"], pilot_name,
                    attribution["book_mode"], fact_sheet_version,
                    attribution["decision_contract_version"],
                    attribution["decision_contract_hash"], measured_through,
                    utc_now(), payload_hash, canonical_json,
                ),
            )
        row = self.conn.execute(
            "SELECT * FROM pilot_fact_sheets WHERE fact_sheet_id=?",
            (payload_hash,),
        ).fetchone()
        assert row is not None
        result = self._pilot_fact_sheet_item(row)
        result["idempotent_replay"] = False
        return result

    def pilot_fact_sheet(self, fact_sheet_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM pilot_fact_sheets WHERE fact_sheet_id=?",
            (str(fact_sheet_id),),
        ).fetchone()
        return self._pilot_fact_sheet_item(row) if row else None

    def pilot_fact_sheets(
        self,
        limit: int = 20,
        *,
        pilot_id: str | None = None,
        book_mode: str | None = None,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            raise ValueError("pilot fact-sheet limit must be positive")
        clauses: list[str] = []
        values: list[object] = []
        if pilot_id is not None:
            clauses.append("pilot_id=?")
            values.append(str(pilot_id).strip().lower())
        if book_mode is not None:
            normalized_mode = str(book_mode).strip().upper()
            if normalized_mode not in BOOK_MODES:
                raise ValueError("book_mode must be LIVE, PAPER, or SHADOW")
            clauses.append("book_mode=?")
            values.append(normalized_mode)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.conn.execute(
            f"""SELECT * FROM pilot_fact_sheets{where}
                ORDER BY measured_through DESC,recorded_at DESC LIMIT ?""",
            (*values, limit),
        ).fetchall()
        return [self._pilot_fact_sheet_item(row) for row in rows]

    def pilot_leaderboard(
        self, *, book_mode: str, limit: int = 20
    ) -> dict[str, Any]:
        """Rank only like-for-like books; never create capital authority."""
        normalized_mode = str(book_mode).strip().upper()
        if normalized_mode not in BOOK_MODES:
            raise ValueError("book_mode must be LIVE, PAPER, or SHADOW")
        if limit <= 0:
            raise ValueError("pilot leaderboard limit must be positive")
        rows = self.conn.execute(
            """SELECT f.* FROM pilot_fact_sheets f
               WHERE f.book_mode=? AND NOT EXISTS (
                   SELECT 1 FROM pilot_fact_sheets newer
                   WHERE newer.pilot_id=f.pilot_id
                     AND newer.book_mode=f.book_mode
                     AND (
                         newer.measured_through>f.measured_through OR
                         (newer.measured_through=f.measured_through
                          AND newer.recorded_at>f.recorded_at)
                     )
               )""",
            (normalized_mode,),
        ).fetchall()
        items = [self._pilot_fact_sheet_item(row) for row in rows]
        estimable = [
            item for item in items
            if item["payload"]["evidence_status"] == "ESTIMABLE"
        ]
        insufficient = [
            item for item in items
            if item["payload"]["evidence_status"] == "INSUFFICIENT"
        ]
        estimable.sort(
            key=lambda item: (
                float(item["payload"]["metrics"][
                    "clustered_95pct_lower_bound_expectancy_r"
                ]),
                float(item["payload"]["metrics"]["net_expectancy_r_after_costs"]),
                -float(item["payload"]["metrics"]["expected_shortfall_95_r"]),
                -float(item["payload"]["metrics"]["max_drawdown_r"]),
                -float(item["payload"]["metrics"]["execution_shortfall_bps"]),
                -float(item["payload"]["metrics"]["control_breach_count"]),
                float(item["payload"]["metrics"]["evidence_coverage_pct"]),
                float(item["payload"]["metrics"][
                    "effective_independent_sample_size"
                ]),
            ),
            reverse=True,
        )
        ranked = []
        for rank, item in enumerate(estimable[:limit], start=1):
            ranked.append({
                "rank": rank,
                "pilot_id": item["pilot_id"],
                "pilot_name": item["pilot_name"],
                "book_mode": item["book_mode"],
                "fact_sheet_id": item["fact_sheet_id"],
                "fact_sheet_version": item["fact_sheet_version"],
                "measured_through": item["measured_through"],
                "metrics": item["payload"]["metrics"],
                "ranking_status": "RANKED",
                "reporting_only": True,
                "trade_authority": False,
                "capital_reallocation_authority": False,
            })
        unranked = [
            {
                "rank": None,
                "pilot_id": item["pilot_id"],
                "pilot_name": item["pilot_name"],
                "book_mode": item["book_mode"],
                "fact_sheet_id": item["fact_sheet_id"],
                "fact_sheet_version": item["fact_sheet_version"],
                "measured_through": item["measured_through"],
                "metrics": item["payload"]["metrics"],
                "ranking_status": "UNRANKED_INSUFFICIENT_EVIDENCE",
                "reporting_only": True,
                "trade_authority": False,
                "capital_reallocation_authority": False,
            }
            for item in insufficient[:limit]
        ]
        return {
            "book_mode": normalized_mode,
            "ranked_pilots": ranked,
            "unranked_pilots": unranked,
            "ranking_basis": (
                "ESTIMABLE books only: clustered 95% after-cost expectancy lower "
                "bound and expectancy, then expected shortfall, drawdown, execution "
                "shortfall, control breaches, evidence coverage, and effective sample size"
            ),
            "raw_pnl_or_win_rate_used": False,
            "books_combined": False,
            "reporting_only": True,
            "trade_authority": False,
            "capital_reallocation_authority": False,
        }

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
