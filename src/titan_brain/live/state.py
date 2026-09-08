"""Crash-resilient SQLite state for the live execution lifecycle.

The database is the coordination boundary between evidence, risk reservation,
order intent, broker reconciliation, protection, incidents, and notification.
Every externally meaningful transition also appends a hash-chained audit event.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import json
from pathlib import Path
import os
import re
import sqlite3
import threading
from typing import Any, Iterator, Mapping, Sequence
from uuid import uuid4

from .activation import ACTIVATION_SCHEMA, ActivationRecord
from .money import to_cents
from .models import (
    BrokerOrder,
    BrokerOrderState,
    BrokerSnapshot,
    ExpiringPlan,
    Fill,
    Incident,
    IntentState,
    LatencySample,
    OrderIntent,
    OutboxMessage,
    OutboxState,
    PositionRecord,
    ProtectionObligation,
    ProtectionState,
    PlanState,
    ReservationState,
    RiskReservation,
    SessionLatch,
)


SCHEMA_VERSION = 3
ZERO_HASH = "0" * 64


class LiveStateError(RuntimeError):
    """Base error for durable lifecycle state."""


class StateConflict(LiveStateError):
    """Raised when an identifier is replayed with different facts."""


class OutOfOrderEvent(LiveStateError):
    """Raised when a newer durable fact would be overwritten by stale input."""


class UnsupportedSchema(LiveStateError):
    """Raised when the database requires a newer runtime."""


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("naive datetime cannot be serialized")
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    raise TypeError(f"unsupported canonical JSON type: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Serialize deterministically and reject NaN/Infinity."""

    return json.dumps(
        value,
        default=_json_default,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def object_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _safe_notification_error(value: object) -> str:
    """Persist only bounded machine codes, never provider exception text."""

    candidate = str(value or "").strip().upper()
    if (
        candidate.startswith("NOTIFICATION_")
        and len(candidate) <= 128
        and all(character.isalnum() or character == "_" for character in candidate)
    ):
        return candidate
    return "NOTIFICATION_DELIVERY_FAILED"


SCHEMA_V1 = """
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    version INTEGER NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS broker_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    account_key TEXT NOT NULL,
    evidence_revision TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    account_state TEXT NOT NULL,
    equity_cents INTEGER NOT NULL,
    cash_cents INTEGER NOT NULL,
    unleveraged_buying_power_cents INTEGER NOT NULL,
    realized_pnl_cents INTEGER NOT NULL,
    equity_position_count INTEGER NOT NULL CHECK (equity_position_count >= 0),
    equity_order_count INTEGER NOT NULL CHECK (equity_order_count >= 0),
    equity_nonterminal_order_count INTEGER NOT NULL CHECK (equity_nonterminal_order_count >= 0),
    external_material_order_count INTEGER NOT NULL CHECK (external_material_order_count >= 0),
    option_position_count INTEGER NOT NULL CHECK (option_position_count >= 0),
    option_order_count INTEGER NOT NULL CHECK (option_order_count >= 0),
    advanced_order_count INTEGER NOT NULL CHECK (advanced_order_count >= 0),
    reconciliation_blocker_count INTEGER NOT NULL CHECK (reconciliation_blocker_count >= 0),
    positions_reconciled INTEGER NOT NULL CHECK (positions_reconciled IN (0,1)),
    equity_orders_reconciled INTEGER NOT NULL CHECK (equity_orders_reconciled IN (0,1)),
    option_positions_reconciled INTEGER NOT NULL CHECK (option_positions_reconciled IN (0,1)),
    option_orders_reconciled INTEGER NOT NULL CHECK (option_orders_reconciled IN (0,1)),
    advanced_orders_reconciled INTEGER NOT NULL CHECK (advanced_orders_reconciled IN (0,1)),
    realized_pnl_reconciled INTEGER NOT NULL CHECK (realized_pnl_reconciled IN (0,1)),
    positions_digest TEXT NOT NULL,
    orders_digest TEXT NOT NULL,
    UNIQUE(account_key, evidence_revision)
);

CREATE TABLE IF NOT EXISTS runtime_identity (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    runtime_id TEXT NOT NULL,
    account_key TEXT NOT NULL,
    release_manifest_hash TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    policy_hash TEXT NOT NULL,
    mode TEXT NOT NULL,
    authority_enabled INTEGER NOT NULL CHECK (authority_enabled IN (0,1)),
    activated_at TEXT,
    deactivated_at TEXT,
    generation INTEGER NOT NULL CHECK (generation >= 0),
    initialized_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS account_writer_lease (
    account_key TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    process_id INTEGER NOT NULL CHECK (process_id > 0),
    generation INTEGER NOT NULL CHECK (generation > 0),
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    released_at TEXT
);

CREATE TABLE IF NOT EXISTS positions (
    account_key TEXT NOT NULL,
    symbol TEXT NOT NULL,
    quantity TEXT NOT NULL,
    sellable_quantity TEXT NOT NULL,
    held_for_sells TEXT NOT NULL,
    average_price TEXT,
    source TEXT NOT NULL,
    broker_updated_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 0),
    raw_hash TEXT NOT NULL,
    snapshot_id TEXT NOT NULL REFERENCES broker_snapshots(snapshot_id),
    PRIMARY KEY(account_key, symbol)
);

CREATE TABLE IF NOT EXISTS activation_records (
    activation_id TEXT PRIMARY KEY,
    account_key TEXT NOT NULL,
    record_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    record_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    account_key TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    setup_id TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    limit_price TEXT NOT NULL,
    structural_stop TEXT NOT NULL,
    market_hours TEXT NOT NULL,
    time_in_force TEXT NOT NULL,
    evidence_cutoff_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    policy_hash TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    evidence_hash TEXT NOT NULL,
    targets_json TEXT NOT NULL,
    state TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_reservations (
    reservation_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL UNIQUE REFERENCES plans(plan_id),
    account_key TEXT NOT NULL,
    planned_risk_cents INTEGER NOT NULL CHECK (planned_risk_cents >= 0),
    stress_risk_cents INTEGER NOT NULL CHECK (stress_risk_cents >= planned_risk_cents),
    execution_reserve_cents INTEGER NOT NULL CHECK (execution_reserve_cents >= 0),
    notional_cents INTEGER NOT NULL CHECK (notional_cents > 0),
    created_at TEXT NOT NULL,
    state TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS order_intents (
    intent_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES plans(plan_id),
    reservation_id TEXT UNIQUE REFERENCES risk_reservations(reservation_id),
    account_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    client_ref TEXT NOT NULL UNIQUE,
    order_tuple_json TEXT NOT NULL,
    tuple_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    acknowledgement_deadline_at TEXT NOT NULL,
    state TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (kind = 'ENTRY' AND reservation_id IS NOT NULL)
        OR
        (kind IN ('PROTECTION','EXIT','CANCEL') AND reservation_id IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS broker_orders (
    broker_order_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL UNIQUE REFERENCES order_intents(intent_id),
    account_key TEXT NOT NULL,
    state TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    cumulative_filled_quantity INTEGER NOT NULL
        CHECK (cumulative_filled_quantity >= 0 AND cumulative_filled_quantity <= quantity),
    revision INTEGER NOT NULL CHECK (revision >= 0),
    broker_updated_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    raw_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fills (
    fill_id TEXT PRIMARY KEY,
    broker_order_id TEXT NOT NULL REFERENCES broker_orders(broker_order_id),
    account_key TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    price TEXT NOT NULL,
    executed_at TEXT NOT NULL,
    received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS protection_obligations (
    obligation_id TEXT PRIMARY KEY,
    source_fill_id TEXT NOT NULL UNIQUE REFERENCES fills(fill_id),
    account_key TEXT NOT NULL,
    symbol TEXT NOT NULL,
    required_quantity INTEGER NOT NULL CHECK (required_quantity > 0),
    working_quantity INTEGER NOT NULL
        CHECK (working_quantity >= 0 AND working_quantity <= required_quantity),
    stop_price TEXT NOT NULL,
    state TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 0),
    updated_at TEXT NOT NULL,
    broker_order_id TEXT REFERENCES broker_orders(broker_order_id)
);

CREATE TABLE IF NOT EXISTS session_latches (
    account_key TEXT NOT NULL,
    trading_date TEXT NOT NULL,
    loss_locked INTEGER NOT NULL CHECK (loss_locked IN (0,1)),
    objective_crossed INTEGER NOT NULL CHECK (objective_crossed IN (0,1)),
    pause_new_entries INTEGER NOT NULL CHECK (pause_new_entries IN (0,1)),
    closeout_started INTEGER NOT NULL CHECK (closeout_started IN (0,1)),
    hard_kill INTEGER NOT NULL CHECK (hard_kill IN (0,1)),
    highest_realized_pnl_cents INTEGER NOT NULL,
    first_objective_crossed_at TEXT,
    revision INTEGER NOT NULL CHECK (revision >= 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(account_key, trading_date)
);

CREATE TABLE IF NOT EXISTS incidents (
    incident_id TEXT PRIMARY KEY,
    account_key TEXT NOT NULL,
    category TEXT NOT NULL,
    severity TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS notification_outbox (
    message_id TEXT PRIMARY KEY,
    event_key TEXT NOT NULL UNIQUE,
    account_key TEXT NOT NULL,
    template TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    state TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    last_attempt_at TEXT,
    next_attempt_at TEXT,
    delivered_at TEXT,
    last_error TEXT,
    delivery_receipt TEXT,
    claim_owner TEXT,
    claim_expires_at TEXT,
    delivery_route_id TEXT,
    delivery_assurance TEXT,
    delivery_receipt_hash TEXT,
    delivery_payload_hash TEXT
);

CREATE TABLE IF NOT EXISTS notification_worker_lease (
    account_key TEXT PRIMARY KEY,
    worker_id TEXT NOT NULL,
    process_id INTEGER NOT NULL CHECK (process_id > 0),
    generation INTEGER NOT NULL CHECK (generation > 0),
    route_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    destination_fingerprint TEXT NOT NULL,
    route_version TEXT NOT NULL,
    started_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    last_sent_count INTEGER NOT NULL DEFAULT 0 CHECK (last_sent_count >= 0),
    last_failed_count INTEGER NOT NULL DEFAULT 0 CHECK (last_failed_count >= 0),
    released_at TEXT
);

CREATE TABLE IF NOT EXISTS latency_samples (
    sample_id TEXT PRIMARY KEY,
    account_key TEXT NOT NULL,
    stage TEXT NOT NULL,
    duration_microseconds INTEGER NOT NULL CHECK (duration_microseconds >= 0),
    observed_at TEXT NOT NULL,
    correlation_id TEXT
);

CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    stream TEXT NOT NULL,
    event_type TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS broker_snapshots_account_time
    ON broker_snapshots(account_key, received_at);
CREATE INDEX IF NOT EXISTS order_intents_account_state
    ON order_intents(account_key, state);
CREATE INDEX IF NOT EXISTS broker_orders_account_state
    ON broker_orders(account_key, state);
CREATE INDEX IF NOT EXISTS protection_account_state
    ON protection_obligations(account_key, state);
CREATE INDEX IF NOT EXISTS incidents_account_open
    ON incidents(account_key, resolved_at);
CREATE INDEX IF NOT EXISTS outbox_pending
    ON notification_outbox(state, created_at);
CREATE INDEX IF NOT EXISTS outbox_claimable
    ON notification_outbox(state, next_attempt_at, claim_expires_at, created_at);
CREATE INDEX IF NOT EXISTS latency_stage_time
    ON latency_samples(stage, observed_at);
CREATE INDEX IF NOT EXISTS positions_account
    ON positions(account_key, symbol);

CREATE TRIGGER IF NOT EXISTS broker_snapshots_no_update
BEFORE UPDATE ON broker_snapshots BEGIN
    SELECT RAISE(ABORT, 'broker snapshots are append-only');
END;
CREATE TRIGGER IF NOT EXISTS broker_snapshots_no_delete
BEFORE DELETE ON broker_snapshots BEGIN
    SELECT RAISE(ABORT, 'broker snapshots are append-only');
END;
CREATE TRIGGER IF NOT EXISTS fills_no_update
BEFORE UPDATE ON fills BEGIN
    SELECT RAISE(ABORT, 'fills are append-only');
END;
CREATE TRIGGER IF NOT EXISTS fills_no_delete
BEFORE DELETE ON fills BEGIN
    SELECT RAISE(ABORT, 'fills are append-only');
END;
CREATE TRIGGER IF NOT EXISTS latency_samples_no_update
BEFORE UPDATE ON latency_samples BEGIN
    SELECT RAISE(ABORT, 'latency samples are append-only');
END;
CREATE TRIGGER IF NOT EXISTS latency_samples_no_delete
BEFORE DELETE ON latency_samples BEGIN
    SELECT RAISE(ABORT, 'latency samples are append-only');
END;
CREATE TRIGGER IF NOT EXISTS audit_events_no_update
BEFORE UPDATE ON audit_events BEGIN
    SELECT RAISE(ABORT, 'audit events are append-only');
END;
CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
BEFORE DELETE ON audit_events BEGIN
    SELECT RAISE(ABORT, 'audit events are append-only');
END;
"""

SCHEMA_V2_MIGRATION = """
ALTER TABLE notification_outbox ADD COLUMN claim_owner TEXT;
ALTER TABLE notification_outbox ADD COLUMN claim_expires_at TEXT;
ALTER TABLE notification_outbox ADD COLUMN delivery_route_id TEXT;
ALTER TABLE notification_outbox ADD COLUMN delivery_assurance TEXT;
ALTER TABLE notification_outbox ADD COLUMN delivery_receipt_hash TEXT;
ALTER TABLE notification_outbox ADD COLUMN delivery_payload_hash TEXT;
CREATE INDEX IF NOT EXISTS outbox_claimable
    ON notification_outbox(state, next_attempt_at, claim_expires_at, created_at);
"""

SCHEMA_V3_MIGRATION = """
CREATE TABLE IF NOT EXISTS notification_worker_lease (
    account_key TEXT PRIMARY KEY,
    worker_id TEXT NOT NULL,
    process_id INTEGER NOT NULL CHECK (process_id > 0),
    generation INTEGER NOT NULL CHECK (generation > 0),
    route_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    destination_fingerprint TEXT NOT NULL,
    route_version TEXT NOT NULL,
    started_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    last_sent_count INTEGER NOT NULL DEFAULT 0 CHECK (last_sent_count >= 0),
    last_failed_count INTEGER NOT NULL DEFAULT 0 CHECK (last_failed_count >= 0),
    released_at TEXT
);
"""


def _semantic_schema_sql(value: object) -> str:
    collapsed = " ".join(str(value or "").split())
    return re.sub(r"\s*([(),])\s*", r"\1", collapsed)


def _schema_semantic_shape(connection: sqlite3.Connection) -> Mapping[str, Any]:
    """Describe all user schema semantics without depending on DDL whitespace."""

    objects = [
        {
            "type": str(row[0]),
            "name": str(row[1]),
            "table": str(row[2]),
            "sql": _semantic_schema_sql(row[3]),
        }
        for row in connection.execute(
            """SELECT type,name,tbl_name,sql FROM sqlite_master
                 WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"""
        ).fetchall()
    ]
    tables = sorted(
        str(row[0])
        for row in connection.execute(
            """SELECT name FROM sqlite_master
                 WHERE type='table' AND name NOT LIKE 'sqlite_%'"""
        ).fetchall()
    )
    table_shapes: dict[str, Any] = {}
    for table in tables:
        if not table.replace("_", "").isalnum():
            raise UnsupportedSchema("state schema contains an unsafe table name")
        columns = [
            tuple(row)
            for row in connection.execute(
                f'PRAGMA table_xinfo("{table}")'
            ).fetchall()
        ]
        foreign_keys = sorted(
            tuple(row)
            for row in connection.execute(
                f'PRAGMA foreign_key_list("{table}")'
            ).fetchall()
        )
        indexes: list[Mapping[str, Any]] = []
        for index in connection.execute(
            f'PRAGMA index_list("{table}")'
        ).fetchall():
            index_name = str(index[1])
            if not index_name.replace("_", "").isalnum():
                raise UnsupportedSchema("state schema contains an unsafe index name")
            indexes.append(
                {
                    "name": index_name,
                    "unique": int(index[2]),
                    "origin": str(index[3]),
                    "partial": int(index[4]),
                    "columns": [
                        tuple(row)
                        for row in connection.execute(
                            f'PRAGMA index_xinfo("{index_name}")'
                        ).fetchall()
                    ],
                }
            )
        table_shapes[table] = {
            "columns": columns,
            "foreign_keys": foreign_keys,
            "indexes": sorted(indexes, key=lambda value: str(value["name"])),
        }
    return {"objects": objects, "tables": table_shapes}


def _validate_current_schema(connection: sqlite3.Connection) -> None:
    """Require exact semantic v3 shape and metadata before runtime use."""

    reference = sqlite3.connect(":memory:", isolation_level=None)
    try:
        reference.executescript(SCHEMA_V1)
        expected = _schema_semantic_shape(reference)
    finally:
        reference.close()
    if _schema_semantic_shape(connection) != expected:
        raise UnsupportedSchema("database schema shape differs from exact runtime v3")
    metadata = connection.execute(
        "SELECT singleton,version,applied_at FROM schema_meta"
    ).fetchall()
    if (
        len(metadata) != 1
        or int(metadata[0][0]) != 1
        or int(metadata[0][1]) != SCHEMA_VERSION
    ):
        raise UnsupportedSchema(
            "database schema metadata disagrees with runtime v3"
        )
    try:
        applied_at = datetime.fromisoformat(str(metadata[0][2]))
    except ValueError as exc:
        raise UnsupportedSchema("database schema metadata timestamp is invalid") from exc
    if applied_at.tzinfo is None:
        raise UnsupportedSchema(
            "database schema metadata timestamp must be timezone-aware"
        )
    if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise UnsupportedSchema("database failed SQLite quick_check")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise UnsupportedSchema("database failed foreign-key validation")


_INTENT_TRANSITIONS: Mapping[IntentState, frozenset[IntentState]] = {
    IntentState.PREPARED: frozenset(
        {IntentState.SUBMITTING, IntentState.CANCELLED, IntentState.FAILED}
    ),
    IntentState.SUBMITTING: frozenset(
        {
            IntentState.ACKNOWLEDGED,
            IntentState.UNKNOWN,
            IntentState.REJECTED,
            IntentState.FAILED,
        }
    ),
    IntentState.UNKNOWN: frozenset(
        {
            IntentState.ACKNOWLEDGED,
            IntentState.RECONCILED,
            IntentState.REJECTED,
            IntentState.FAILED,
        }
    ),
    IntentState.ACKNOWLEDGED: frozenset(
        {
            IntentState.RECONCILED,
            IntentState.REJECTED,
            IntentState.CANCELLED,
            IntentState.FAILED,
        }
    ),
}


class LiveStateStore:
    """One connection to the account lifecycle database."""

    def __init__(self, path: str | Path, *, timeout_seconds: float = 5.0):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path),
            timeout=timeout_seconds,
            isolation_level=None,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "LiveStateStore":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Serialize a write transaction and take SQLite's writer lock early."""

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    def _migrate(self) -> None:
        with self._lock:
            version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise UnsupportedSchema(
                    f"database schema {version} is newer than runtime {SCHEMA_VERSION}"
                )
            if version == 0:
                user_objects = self._conn.execute(
                    """SELECT type,name FROM sqlite_master
                         WHERE name NOT LIKE 'sqlite_%'"""
                ).fetchall()
                if user_objects:
                    raise UnsupportedSchema(
                        "unversioned database contains a partial schema; "
                        "automatic repair is forbidden"
                    )
                self._conn.executescript(SCHEMA_V1)
                now = datetime.now(timezone.utc).isoformat()
                self._conn.execute(
                    "INSERT OR REPLACE INTO schema_meta(singleton, version, applied_at) VALUES(1, ?, ?)",
                    (SCHEMA_VERSION, now),
                )
                self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                _validate_current_schema(self._conn)
            elif version in {1, 2}:
                raise UnsupportedSchema(
                    f"legacy state schema {version} requires the fixed-lock "
                    "paused release installer migration"
                )
            elif version == SCHEMA_VERSION:
                _validate_current_schema(self._conn)
            elif version != SCHEMA_VERSION:
                raise UnsupportedSchema(f"no migration path from schema {version}")

    @property
    def schema_version(self) -> int:
        return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    def pragma(self, name: str) -> Any:
        if name not in {"journal_mode", "synchronous", "foreign_keys"}:
            raise ValueError("unsupported pragma inspection")
        return self._conn.execute(f"PRAGMA {name}").fetchone()[0]

    def initialize_runtime(
        self,
        *,
        runtime_id: str,
        account_key: str,
        release_manifest_hash: str,
        config_hash: str,
        policy_hash: str,
        initialized_at: datetime,
        mode: str = "PAUSED",
    ) -> bool:
        """Bind a database permanently to one release identity and account."""

        values = (
            str(runtime_id),
            str(account_key),
            str(release_manifest_hash),
            str(config_hash),
            str(policy_hash),
            str(mode),
        )
        if any(not value.strip() for value in values):
            raise ValueError("runtime identity fields are required")
        if any(
            len(value) != 64 or any(character not in "0123456789abcdef" for character in value)
            for value in values[2:5]
        ):
            raise ValueError("runtime identity hashes must be SHA-256 values")
        if values[5] != "PAUSED":
            raise ValueError("a newly installed runtime must initialize PAUSED")
        with self.transaction() as connection:
            existing = connection.execute("SELECT * FROM runtime_identity WHERE singleton=1").fetchone()
            if existing is not None:
                immutable = tuple(
                    existing[name]
                    for name in (
                        "runtime_id",
                        "account_key",
                        "release_manifest_hash",
                        "config_hash",
                        "policy_hash",
                    )
                )
                if immutable != values[:5]:
                    raise StateConflict("runtime database identity differs from installed release")
                return False
            when = _iso(initialized_at)
            connection.execute(
                """INSERT INTO runtime_identity(
                       singleton,runtime_id,account_key,release_manifest_hash,
                       config_hash,policy_hash,mode,authority_enabled,activated_at,
                       deactivated_at,generation,initialized_at,updated_at
                   ) VALUES(1,?,?,?,?,?,?,0,NULL,NULL,0,?,?)""",
                values[:5] + (values[5], when, when),
            )
            self._append_event(
                connection,
                stream=account_key,
                event_type="RUNTIME_INITIALIZED_PAUSED",
                entity_type="runtime",
                entity_id=runtime_id,
                occurred_at=initialized_at,
                payload={
                    "release_manifest_hash": release_manifest_hash,
                    "config_hash": config_hash,
                    "policy_hash": policy_hash,
                    "mode": mode,
                },
            )
            return True

    def runtime_status(self) -> Mapping[str, Any] | None:
        row = self._conn.execute("SELECT * FROM runtime_identity WHERE singleton=1").fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def _activation_record_from_row(
        row: Mapping[str, Any], runtime: Mapping[str, Any]
    ) -> ActivationRecord:
        """Re-derive every activation binding from durable canonical bytes.

        The CLI is a convenience layer, not the security boundary.  A caller
        that reaches the state store directly still cannot stage or consume a
        hand-authored mapping, a payload with extra fields, or a record bound
        to a different release/account/schema.
        """

        try:
            raw = json.loads(str(row["record_json"]))
            if not isinstance(raw, Mapping):
                raise ValueError("activation record is not an object")
            record = ActivationRecord.from_payload(raw)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StateConflict("activation record is not canonical v2 evidence") from exc
        if canonical_json(record.to_payload()) != str(row["record_json"]):
            raise StateConflict("activation record canonical bytes differ")
        if object_hash(record.to_payload()) != str(row["record_hash"]):
            raise StateConflict("activation record hash differs from canonical bytes")
        if record.schema_version != ACTIVATION_SCHEMA:
            raise StateConflict("activation record schema is unsupported")
        if record.activation_id != str(row["activation_id"]):
            raise StateConflict("activation identifier differs from durable key")
        if record.activation_id != record.recomputed_activation_id():
            raise StateConflict("activation identifier does not match canonical body")
        if record.readiness_hash != record.readiness_evidence.evidence_hash:
            raise StateConflict("activation readiness hash differs")
        if record.account_key != str(row["account_key"]):
            raise StateConflict("activation account differs from durable key")
        expected_bindings = (
            (record.runtime_id, runtime["runtime_id"]),
            (record.account_key, runtime["account_key"]),
            (record.release_manifest_hash, runtime["release_manifest_hash"]),
            (record.config_hash, runtime["config_hash"]),
            (record.policy_hash, runtime["policy_hash"]),
            (record.database_schema_version, SCHEMA_VERSION),
        )
        if any(actual != expected for actual, expected in expected_bindings):
            raise StateConflict("activation record does not bind the durable runtime")
        readiness = record.readiness_evidence
        readiness_bindings = (
            (readiness.runtime_id, runtime["runtime_id"]),
            (readiness.account_key, runtime["account_key"]),
            (readiness.release_manifest_hash, runtime["release_manifest_hash"]),
            (readiness.config_hash, runtime["config_hash"]),
            (readiness.policy_hash, runtime["policy_hash"]),
            (readiness.database_schema_version, SCHEMA_VERSION),
        )
        if any(actual != expected for actual, expected in readiness_bindings):
            raise StateConflict("activation readiness does not bind the durable runtime")
        if record.requested_mode != "live" or record.owner_acknowledged_blockers:
            raise StateConflict("activation blockers cannot be acknowledged away")
        hard_boolean_gates = (
            readiness.runtime_identity_valid,
            readiness.broker_read_attempted,
            readiness.broker_read_succeeded,
            readiness.account_active,
            readiness.broker_authenticated,
            readiness.daemon_accessible_supported_client,
            readiness.unattended_mutation_supported,
            not readiness.per_mutation_confirmation_required,
            readiness.standard_orders_reconciled,
            readiness.option_positions_reconciled,
            readiness.option_orders_reconciled,
            readiness.advanced_orders_reconciled,
            readiness.positions_reconciled,
            readiness.realized_pnl_reconciled,
            readiness.durable_account_flat,
            readiness.old_writer_disabled,
            readiness.new_writer_lock_held,
            readiness.local_state_writable,
            readiness.audit_chain_valid,
            readiness.market_data_connected,
            readiness.market_data_resynced,
            readiness.tradability_provider_ready,
            readiness.notification_destination_configured,
            readiness.notification_tested,
        )
        if not all(hard_boolean_gates):
            raise StateConflict("activation readiness contains a failed hard gate")
        if (
            readiness.broker_account_last4 != record.account_last4
            or readiness.reconciliation_blocker_count != 0
            or readiness.unknown_submissions != 0
            or readiness.uncovered_quantity != 0
            or readiness.probe_errors
            or readiness.market_data_blockers
            or readiness.durable_snapshot_id is None
            or readiness.durable_snapshot_received_at is None
            or readiness.reconciliation_audit_event_id is None
            or readiness.writer_lock_owner_id is None
            or readiness.writer_lock_process_id is None
            or readiness.notification_delivery_receipt_hash is None
            or readiness.notification_delivered_at is None
            or readiness.probe_started_at is None
            or readiness.probe_completed_at is None
            or readiness.probe_elapsed_monotonic_seconds is None
            or readiness.probe_clock_stable is not True
            or (
                readiness.daemon_accessible_supported_client
                and (
                    readiness.broker_account_binding_fingerprint is None
                    or readiness.broker_authorization_binding_id is None
                    or readiness.component_provenance_hash is None
                )
            )
        ):
            raise StateConflict("activation readiness is incomplete or blocked")
        age_limits = (
            (readiness.broker_snapshot_age_seconds, 5.0),
            (readiness.durable_snapshot_age_seconds, 5.0),
            (readiness.quote_age_seconds, 5.0),
            (readiness.completed_bar_age_seconds, 120.0),
        )
        if any(value is None or value < 0 or value > maximum for value, maximum in age_limits):
            raise StateConflict("activation readiness contains stale evidence")
        return record

    @staticmethod
    def _require_writer_lease(
        connection: sqlite3.Connection,
        *,
        account_key: str,
        occurred_at: datetime,
        owner_id: str | None = None,
        require_current_process: bool = False,
        max_age: timedelta = timedelta(seconds=5),
    ) -> sqlite3.Row:
        lease = connection.execute(
            "SELECT * FROM account_writer_lease WHERE account_key=?", (account_key,)
        ).fetchone()
        if lease is None or lease["released_at"] is not None:
            raise StateConflict("active account-writer lease is required")
        if owner_id is not None and str(lease["owner_id"]) != str(owner_id):
            raise StateConflict("account-writer lease owner differs")
        if require_current_process and int(lease["process_id"]) != os.getpid():
            raise StateConflict("account-writer lease is held by another process")
        current = occurred_at.astimezone(timezone.utc)
        heartbeat = datetime.fromisoformat(str(lease["heartbeat_at"])).astimezone(
            timezone.utc
        )
        age = current - heartbeat
        if age < timedelta(0) or age > max_age:
            raise StateConflict("account-writer lease heartbeat is stale")
        return lease

    @staticmethod
    def _require_reconciled_runtime_state(
        connection: sqlite3.Connection,
        *,
        runtime: Mapping[str, Any],
        occurred_at: datetime,
        strictly_after: datetime | None,
        require_flat: bool,
        expected_snapshot_id: str | None = None,
        expected_audit_event_id: str | None = None,
        max_age: timedelta = timedelta(seconds=5),
    ) -> sqlite3.Row:
        """Prove current whole-broker state before authority can advance."""

        snapshot = connection.execute(
            """SELECT * FROM broker_snapshots WHERE account_key=?
                 ORDER BY observed_at DESC,received_at DESC,snapshot_id DESC LIMIT 1""",
            (runtime["account_key"],),
        ).fetchone()
        if snapshot is None:
            raise StateConflict("fresh durable broker reconciliation is required")
        if expected_snapshot_id is not None and snapshot["snapshot_id"] != expected_snapshot_id:
            raise StateConflict("activation snapshot differs from hash-bound readiness")
        complete_fields = (
            "positions_reconciled",
            "equity_orders_reconciled",
            "option_positions_reconciled",
            "option_orders_reconciled",
            "advanced_orders_reconciled",
            "realized_pnl_reconciled",
        )
        if not all(bool(snapshot[field]) for field in complete_fields):
            raise StateConflict("whole-broker reconciliation is incomplete")
        if str(snapshot["account_state"]).lower() != "active":
            raise StateConflict("broker account is not active")
        if int(snapshot["reconciliation_blocker_count"]) != 0:
            raise StateConflict("broker reconciliation has unresolved blockers")
        if any(
            int(snapshot[field]) != 0
            for field in (
                "external_material_order_count",
                "option_position_count",
                "option_order_count",
                "advanced_order_count",
            )
        ):
            raise StateConflict("unsupported or external broker exposure is present")
        current = occurred_at.astimezone(timezone.utc)
        observed = datetime.fromisoformat(str(snapshot["observed_at"])).astimezone(
            timezone.utc
        )
        age = current - observed
        if age < timedelta(0) or age > max_age:
            raise StateConflict("durable broker reconciliation is stale")
        if strictly_after is not None and observed <= strictly_after.astimezone(timezone.utc):
            raise StateConflict("broker reconciliation is not strictly newer than authority")
        audit = connection.execute(
            """SELECT event_id FROM audit_events WHERE stream=?
                 AND event_type='POSITIONS_RECONCILED'
                 AND entity_type='broker_snapshot' AND entity_id=?
                 ORDER BY sequence DESC LIMIT 1""",
            (runtime["account_key"], snapshot["snapshot_id"]),
        ).fetchone()
        if audit is None:
            raise StateConflict("broker snapshot lacks durable position reconciliation")
        if expected_audit_event_id is not None and audit["event_id"] != expected_audit_event_id:
            raise StateConflict("activation reconciliation receipt differs from readiness")
        unknown = connection.execute(
            """SELECT COUNT(*) FROM order_intents WHERE account_key=?
                 AND state IN ('SUBMITTING','UNKNOWN')""",
            (runtime["account_key"],),
        ).fetchone()[0]
        if int(unknown):
            raise StateConflict("unresolved broker submission is present")
        durable_positions = connection.execute(
            """SELECT symbol,quantity FROM positions WHERE account_key=?
                 AND CAST(quantity AS REAL)<>0""",
            (runtime["account_key"],),
        ).fetchall()
        if len(durable_positions) != int(snapshot["equity_position_count"]):
            raise StateConflict("durable positions disagree with broker position count")
        durable_live_orders = connection.execute(
            """SELECT COUNT(*) FROM broker_orders WHERE account_key=? AND state NOT IN
                 ('FILLED','CANCELLED','PARTIALLY_FILLED_REST_CANCELLED','REJECTED',
                  'FAILED','VOIDED','LOCATE_FAILED')""",
            (runtime["account_key"],),
        ).fetchone()[0]
        if int(durable_live_orders) != int(snapshot["equity_nonterminal_order_count"]):
            raise StateConflict("durable orders disagree with broker order count")
        if require_flat:
            open_obligations = connection.execute(
                """SELECT COUNT(*) FROM protection_obligations WHERE account_key=?
                     AND state NOT IN ('SATISFIED','CANCELLED')""",
                (runtime["account_key"],),
            ).fetchone()[0]
            reserved = connection.execute(
                "SELECT COUNT(*) FROM risk_reservations WHERE account_key=? AND state='RESERVED'",
                (runtime["account_key"],),
            ).fetchone()[0]
            if (
                int(snapshot["equity_position_count"])
                or int(snapshot["equity_nonterminal_order_count"])
                or int(open_obligations)
                or int(reserved)
            ):
                raise StateConflict("activation requires account-wide durable flatness")
        else:
            for position in durable_positions:
                covered = connection.execute(
                    """SELECT COALESCE(SUM(p.working_quantity),0)
                         FROM protection_obligations p
                         JOIN broker_orders o ON o.broker_order_id=p.broker_order_id
                         WHERE p.account_key=? AND p.symbol=? AND p.state='WORKING'
                           AND o.state IN ('CONFIRMED','PARTIALLY_FILLED')""",
                    (runtime["account_key"], position["symbol"]),
                ).fetchone()[0]
                if int(covered or 0) < int(Decimal(str(position["quantity"]))):
                    raise StateConflict("durable broker position is not fully protected")
            uncovered = connection.execute(
                """SELECT COUNT(*) FROM protection_obligations WHERE account_key=?
                     AND state NOT IN ('WORKING','SATISFIED','CANCELLED')""",
                (runtime["account_key"],),
            ).fetchone()[0]
            if int(uncovered):
                raise StateConflict("uncovered protection obligation is present")
        return snapshot

    def activate_runtime(
        self,
        activation_id: str,
        *,
        activated_at: datetime,
        confirmation_phrase: str,
        writer_owner_id: str,
        max_snapshot_age: timedelta = timedelta(seconds=5),
    ) -> None:
        """Consume one canonical activation under the current sole writer.

        Owner confirmation arms only ``RECONCILING``.  It never makes an
        entry-capable runtime directly; a strictly newer service-side broker
        snapshot is still required by :meth:`set_runtime_mode`.
        """

        if max_snapshot_age <= timedelta(0):
            raise ValueError("max_snapshot_age must be positive")
        if activated_at.tzinfo is None:
            raise ValueError("activated_at must be timezone-aware")

        with self.transaction() as connection:
            activation = connection.execute(
                "SELECT * FROM activation_records WHERE activation_id=?", (activation_id,)
            ).fetchone()
            runtime = connection.execute(
                "SELECT * FROM runtime_identity WHERE singleton=1"
            ).fetchone()
            if activation is None or runtime is None:
                raise LiveStateError("activation or runtime identity is missing")
            record = self._activation_record_from_row(activation, runtime)
            if activation["consumed_at"] is not None:
                raise StateConflict("activation record was already consumed")
            when = _iso(activated_at)
            if when < activation["created_at"] or when > activation["expires_at"]:
                raise StateConflict("activation record is outside its validity window")
            if runtime["mode"] != "PAUSED" or bool(runtime["authority_enabled"]):
                raise StateConflict("runtime must be unarmed and PAUSED before activation")
            expected_phrase = (
                f"ACTIVATE FULL LIVE {record.account_key} {record.activation_id}"
            )
            if confirmation_phrase != expected_phrase:
                raise StateConflict("exact activation confirmation phrase does not match")
            self._require_writer_lease(
                connection,
                account_key=str(runtime["account_key"]),
                occurred_at=activated_at,
                owner_id=writer_owner_id,
                require_current_process=True,
                max_age=max_snapshot_age,
            )
            readiness = record.readiness_evidence
            latest = self._require_reconciled_runtime_state(
                connection,
                runtime=runtime,
                occurred_at=activated_at,
                strictly_after=None,
                require_flat=True,
                expected_snapshot_id=readiness.durable_snapshot_id,
                expected_audit_event_id=readiness.reconciliation_audit_event_id,
                max_age=max_snapshot_age,
            )
            # The legacy field name is retained for payload compatibility,
            # but readiness binds the authoritative provider observation.
            if datetime.fromisoformat(str(latest["observed_at"])).astimezone(
                timezone.utc
            ) != readiness.durable_snapshot_received_at:
                raise StateConflict(
                    "activation snapshot timestamp differs from hash-bound readiness"
                )
            connection.execute(
                "UPDATE activation_records SET consumed_at=? WHERE activation_id=?",
                (when, activation_id),
            )
            connection.execute(
                """UPDATE runtime_identity SET mode='RECONCILING',authority_enabled=1,
                       activated_at=?,deactivated_at=NULL,generation=generation+1,updated_at=?
                   WHERE singleton=1""",
                (when, when),
            )
            self._append_event(
                connection,
                stream=runtime["account_key"],
                event_type="RUNTIME_AUTHORITY_ACTIVATED_RECONCILING",
                entity_type="activation",
                entity_id=activation_id,
                occurred_at=activated_at,
                payload={
                    "record_hash": activation["record_hash"],
                    "readiness_hash": record.readiness_hash,
                    "flatness_snapshot_id": latest["snapshot_id"],
                    "writer_owner_id": writer_owner_id,
                },
            )

    def deactivate_runtime_authority(
        self,
        *,
        deactivated_at: datetime,
        reason: str,
        flatness_snapshot_id: str,
        max_snapshot_age: timedelta = timedelta(seconds=15),
    ) -> None:
        """Disarm only with complete, current, broker-flat durable evidence."""

        if max_snapshot_age <= timedelta(0):
            raise ValueError("max_snapshot_age must be positive")

        with self.transaction() as connection:
            runtime = connection.execute(
                "SELECT * FROM runtime_identity WHERE singleton=1"
            ).fetchone()
            snapshot = connection.execute(
                "SELECT * FROM broker_snapshots WHERE snapshot_id=?",
                (flatness_snapshot_id,),
            ).fetchone()
            if runtime is None or snapshot is None:
                raise LiveStateError("runtime or flatness snapshot is missing")
            if not bool(runtime["authority_enabled"]):
                raise StateConflict("runtime authority is already disabled")
            if snapshot["account_key"] != runtime["account_key"]:
                raise StateConflict("flatness evidence crosses runtime accounts")
            complete_fields = (
                "positions_reconciled",
                "equity_orders_reconciled",
                "option_positions_reconciled",
                "option_orders_reconciled",
                "advanced_orders_reconciled",
                "realized_pnl_reconciled",
            )
            if not all(bool(snapshot[name]) for name in complete_fields):
                raise StateConflict("authority cannot be disabled without complete broker evidence")
            scope_counts = (
                "equity_position_count",
                "equity_nonterminal_order_count",
                "external_material_order_count",
                "option_position_count",
                "option_order_count",
                "advanced_order_count",
                "reconciliation_blocker_count",
            )
            if any(int(snapshot[name]) != 0 for name in scope_counts):
                raise StateConflict(
                    "authority cannot be disabled while account-wide broker scope is non-flat"
                )
            latest = connection.execute(
                """SELECT snapshot_id,observed_at,received_at FROM broker_snapshots
                     WHERE account_key=?
                     ORDER BY observed_at DESC,received_at DESC,snapshot_id DESC LIMIT 1""",
                (runtime["account_key"],),
            ).fetchone()
            if latest is None or latest["snapshot_id"] != flatness_snapshot_id:
                raise StateConflict(
                    "authority cannot be disabled from superseded broker evidence"
                )
            reconciled = connection.execute(
                """SELECT 1 FROM audit_events WHERE stream=?
                     AND event_type='POSITIONS_RECONCILED'
                     AND entity_type='broker_snapshot' AND entity_id=? LIMIT 1""",
                (runtime["account_key"], flatness_snapshot_id),
            ).fetchone()
            if reconciled is None:
                raise StateConflict(
                    "flatness snapshot was not applied as authoritative position evidence"
                )
            positions = connection.execute(
                "SELECT COUNT(*) FROM positions WHERE account_key=? AND CAST(quantity AS REAL)<>0",
                (runtime["account_key"],),
            ).fetchone()[0]
            orders = connection.execute(
                """SELECT COUNT(*) FROM broker_orders WHERE account_key=? AND state NOT IN
                       ('FILLED','CANCELLED','PARTIALLY_FILLED_REST_CANCELLED','REJECTED',
                        'FAILED','VOIDED','LOCATE_FAILED')""",
                (runtime["account_key"],),
            ).fetchone()[0]
            unknown = connection.execute(
                "SELECT COUNT(*) FROM order_intents WHERE account_key=? AND state IN ('SUBMITTING','UNKNOWN')",
                (runtime["account_key"],),
            ).fetchone()[0]
            obligations = connection.execute(
                """SELECT COUNT(*) FROM protection_obligations WHERE account_key=?
                     AND state NOT IN ('SATISFIED','CANCELLED')""",
                (runtime["account_key"],),
            ).fetchone()[0]
            if positions or orders or unknown or obligations:
                raise StateConflict("authority cannot be disabled until broker flatness is proven")
            if deactivated_at.tzinfo is None:
                raise ValueError("deactivated_at must be timezone-aware")
            when_value = deactivated_at.astimezone(timezone.utc)
            when = _iso(when_value)
            observed = datetime.fromisoformat(str(snapshot["observed_at"])).astimezone(
                timezone.utc
            )
            activation_floor = max(
                datetime.fromisoformat(str(value)).astimezone(timezone.utc)
                for value in (runtime["activated_at"], runtime["updated_at"])
                if value is not None
            )
            if observed <= activation_floor:
                raise StateConflict(
                    "flatness evidence must be strictly newer than activated runtime state"
                )
            if when_value <= observed:
                raise StateConflict("deactivation must follow the flatness snapshot")
            if when_value - observed > max_snapshot_age:
                raise StateConflict("flatness snapshot is too old for deactivation")
            connection.execute(
                """UPDATE runtime_identity SET mode='PAUSED',authority_enabled=0,
                       deactivated_at=?,generation=generation+1,updated_at=? WHERE singleton=1""",
                (when, when),
            )
            self._append_event(
                connection,
                stream=runtime["account_key"],
                event_type="RUNTIME_AUTHORITY_DEACTIVATED_FLAT",
                entity_type="runtime",
                entity_id=runtime["runtime_id"],
                occurred_at=deactivated_at,
                payload={"reason": str(reason), "flatness_snapshot_id": flatness_snapshot_id},
            )

    def set_runtime_mode(self, mode: str, *, occurred_at: datetime, reason: str) -> bool:
        if occurred_at.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware")
        allowed = {
            "PAUSED": {"INCIDENT", "STOPPED"},
            "RECONCILING": {"PAUSED", "ACTIVE", "PAUSE_NEW_ENTRIES", "MANAGED_CLOSEOUT", "INCIDENT"},
            "ACTIVE": {"PAUSE_NEW_ENTRIES", "MANAGED_CLOSEOUT", "INCIDENT"},
            "PAUSE_NEW_ENTRIES": {"ACTIVE", "RECONCILING", "MANAGED_CLOSEOUT", "INCIDENT", "PAUSED"},
            "MANAGED_CLOSEOUT": {"PAUSED", "INCIDENT", "RECONCILING"},
            "INCIDENT": {"PAUSED", "RECONCILING", "MANAGED_CLOSEOUT"},
            "STOPPED": {"PAUSED"},
        }
        target = str(mode)
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM runtime_identity WHERE singleton=1").fetchone()
            if row is None:
                raise LiveStateError("runtime is not initialized")
            current = str(row["mode"])
            if current == target:
                return False
            if target not in allowed.get(current, set()):
                raise StateConflict(f"invalid runtime mode transition {current} -> {target}")
            authority_modes = {
                "RECONCILING",
                "ACTIVE",
                "PAUSE_NEW_ENTRIES",
                "MANAGED_CLOSEOUT",
            }
            if target in authority_modes and not bool(row["authority_enabled"]):
                raise StateConflict("runtime authority has not been activated")
            if target == "PAUSED" and bool(row["authority_enabled"]):
                raise StateConflict("armed runtime must prove flatness and deactivate atomically")
            if target in authority_modes:
                self._require_writer_lease(
                    connection,
                    account_key=str(row["account_key"]),
                    occurred_at=occurred_at,
                )
            if target == "ACTIVE":
                if row["activated_at"] is None:
                    raise StateConflict("runtime has no activation timestamp")
                self._require_reconciled_runtime_state(
                    connection,
                    runtime=row,
                    occurred_at=occurred_at,
                    strictly_after=datetime.fromisoformat(
                        str(row["activated_at"])
                    ).astimezone(timezone.utc),
                    require_flat=False,
                )
            connection.execute(
                """UPDATE runtime_identity SET mode=?, generation=generation+1, updated_at=?
                   WHERE singleton=1""",
                (target, _iso(occurred_at)),
            )
            self._append_event(
                connection,
                stream=str(row["account_key"]),
                event_type=f"RUNTIME_MODE_{target}",
                entity_type="runtime",
                entity_id=str(row["runtime_id"]),
                occurred_at=occurred_at,
                payload={"from": current, "to": target, "reason": str(reason)},
            )
            return True

    def acquire_writer_lease(
        self,
        *,
        account_key: str,
        owner_id: str,
        acquired_at: datetime,
        process_id: int | None = None,
        recover_stale: bool = False,
    ) -> int:
        """Record the generation of the already-held kernel writer lock."""

        pid = os.getpid() if process_id is None else process_id
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise ValueError("process_id must be positive")
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM account_writer_lease WHERE account_key=?", (account_key,)
            ).fetchone()
            if row is not None and row["released_at"] is None:
                if row["owner_id"] == owner_id and int(row["process_id"]) == pid:
                    return int(row["generation"])
                if not recover_stale:
                    raise StateConflict("database account-writer lease is already held")
            generation = 1 if row is None else int(row["generation"]) + 1
            when = _iso(acquired_at)
            connection.execute(
                """INSERT INTO account_writer_lease(
                       account_key,owner_id,process_id,generation,acquired_at,heartbeat_at,released_at
                   ) VALUES(?,?,?,?,?,?,NULL)
                   ON CONFLICT(account_key) DO UPDATE SET
                       owner_id=excluded.owner_id,process_id=excluded.process_id,
                       generation=excluded.generation,acquired_at=excluded.acquired_at,
                       heartbeat_at=excluded.heartbeat_at,released_at=NULL""",
                (account_key, owner_id, pid, generation, when, when),
            )
            self._append_event(
                connection,
                stream=account_key,
                event_type=(
                    "ACCOUNT_WRITER_LEASE_RECOVERED"
                    if row is not None and row["released_at"] is None
                    else "ACCOUNT_WRITER_LEASE_ACQUIRED"
                ),
                entity_type="writer_lease",
                entity_id=f"{account_key}:{generation}",
                occurred_at=acquired_at,
                payload={"owner_id": owner_id, "process_id": pid, "generation": generation},
            )
            return generation

    def heartbeat_writer_lease(
        self, *, account_key: str, owner_id: str, observed_at: datetime
    ) -> None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM account_writer_lease WHERE account_key=?", (account_key,)
            ).fetchone()
            if row is None or row["released_at"] is not None or row["owner_id"] != owner_id:
                raise StateConflict("writer lease ownership was lost")
            if _iso(observed_at) < row["heartbeat_at"]:
                raise OutOfOrderEvent("writer heartbeat moved backwards")
            connection.execute(
                "UPDATE account_writer_lease SET heartbeat_at=? WHERE account_key=?",
                (_iso(observed_at), account_key),
            )

    def release_writer_lease(
        self, *, account_key: str, owner_id: str, released_at: datetime
    ) -> bool:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM account_writer_lease WHERE account_key=?", (account_key,)
            ).fetchone()
            if row is None or row["released_at"] is not None:
                return False
            if row["owner_id"] != owner_id:
                raise StateConflict("only the current writer may release its lease")
            connection.execute(
                "UPDATE account_writer_lease SET released_at=?, heartbeat_at=? WHERE account_key=?",
                (_iso(released_at), _iso(released_at), account_key),
            )
            self._append_event(
                connection,
                stream=account_key,
                event_type="ACCOUNT_WRITER_LEASE_RELEASED",
                entity_type="writer_lease",
                entity_id=f"{account_key}:{row['generation']}",
                occurred_at=released_at,
                payload={"owner_id": owner_id, "generation": int(row["generation"])},
            )
            return True

    @staticmethod
    def _insert_exact(
        connection: sqlite3.Connection,
        *,
        table: str,
        key_column: str,
        values: Mapping[str, Any],
    ) -> bool:
        key = values[key_column]
        existing = connection.execute(
            f"SELECT {', '.join(values)} FROM {table} WHERE {key_column} = ?",
            (key,),
        ).fetchone()
        expected = tuple(values.values())
        if existing is not None:
            if tuple(existing) != expected:
                raise StateConflict(f"{table}.{key_column} {key!r} has conflicting facts")
            return False
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        connection.execute(
            f"INSERT INTO {table}({columns}) VALUES({placeholders})",
            expected,
        )
        return True

    @staticmethod
    def _event_hash(previous_hash: str, body_json: str) -> str:
        return hashlib.sha256(
            previous_hash.encode("ascii") + b"\n" + body_json.encode("utf-8")
        ).hexdigest()

    def _append_event(
        self,
        connection: sqlite3.Connection,
        *,
        stream: str,
        event_type: str,
        entity_type: str,
        entity_id: str,
        occurred_at: datetime,
        payload: Mapping[str, Any],
        event_id: str | None = None,
    ) -> str:
        event_id = str(event_id or uuid4())
        occurred = _iso(occurred_at)
        payload_json = canonical_json(payload)
        prior = connection.execute(
            "SELECT event_hash FROM audit_events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        previous_hash = ZERO_HASH if prior is None else str(prior[0])
        body = canonical_json(
            {
                "event_id": event_id,
                "stream": stream,
                "event_type": event_type,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "occurred_at": occurred,
                "payload": json.loads(payload_json),
            }
        )
        digest = self._event_hash(previous_hash, body)
        connection.execute(
            """INSERT INTO audit_events(
                   event_id, stream, event_type, entity_type, entity_id,
                   occurred_at, payload_json, previous_hash, event_hash
               ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id,
                stream,
                event_type,
                entity_type,
                entity_id,
                occurred,
                payload_json,
                previous_hash,
                digest,
            ),
        )
        return digest

    def append_event(
        self,
        *,
        stream: str,
        event_type: str,
        entity_type: str,
        entity_id: str,
        occurred_at: datetime,
        payload: Mapping[str, Any],
        event_id: str | None = None,
    ) -> str:
        with self.transaction() as connection:
            return self._append_event(
                connection,
                stream=stream,
                event_type=event_type,
                entity_type=entity_type,
                entity_id=entity_id,
                occurred_at=occurred_at,
                payload=payload,
                event_id=event_id,
            )

    def verify_event_chain(self) -> tuple[bool, int, str]:
        rows = self._conn.execute(
            "SELECT * FROM audit_events ORDER BY sequence"
        ).fetchall()
        previous = ZERO_HASH
        for row in rows:
            if row["previous_hash"] != previous:
                return False, int(row["sequence"]), previous
            body = canonical_json(
                {
                    "event_id": row["event_id"],
                    "stream": row["stream"],
                    "event_type": row["event_type"],
                    "entity_type": row["entity_type"],
                    "entity_id": row["entity_id"],
                    "occurred_at": row["occurred_at"],
                    "payload": json.loads(row["payload_json"]),
                }
            )
            expected = self._event_hash(previous, body)
            if row["event_hash"] != expected:
                return False, int(row["sequence"]), expected
            previous = expected
        return True, len(rows), previous

    def record_broker_snapshot(self, snapshot: BrokerSnapshot) -> bool:
        values = {
            "snapshot_id": snapshot.snapshot_id,
            "account_key": snapshot.account_key,
            "evidence_revision": snapshot.evidence_revision,
            "observed_at": _iso(snapshot.observed_at),
            "received_at": _iso(snapshot.received_at),
            "account_state": snapshot.account_state,
            "equity_cents": to_cents(snapshot.equity),
            "cash_cents": to_cents(snapshot.cash),
            "unleveraged_buying_power_cents": to_cents(
                snapshot.unleveraged_buying_power
            ),
            "realized_pnl_cents": to_cents(snapshot.realized_pnl),
            "equity_position_count": snapshot.equity_position_count,
            "equity_order_count": snapshot.equity_order_count,
            "equity_nonterminal_order_count": snapshot.equity_nonterminal_order_count,
            "external_material_order_count": snapshot.external_material_order_count,
            "option_position_count": snapshot.option_position_count,
            "option_order_count": snapshot.option_order_count,
            "advanced_order_count": snapshot.advanced_order_count,
            "reconciliation_blocker_count": snapshot.reconciliation_blocker_count,
            "positions_reconciled": int(snapshot.positions_reconciled),
            "equity_orders_reconciled": int(snapshot.equity_orders_reconciled),
            "option_positions_reconciled": int(snapshot.option_positions_reconciled),
            "option_orders_reconciled": int(snapshot.option_orders_reconciled),
            "advanced_orders_reconciled": int(snapshot.advanced_orders_reconciled),
            "realized_pnl_reconciled": int(snapshot.realized_pnl_reconciled),
            "positions_digest": snapshot.positions_digest,
            "orders_digest": snapshot.orders_digest,
        }
        with self.transaction() as connection:
            inserted = self._insert_exact(
                connection,
                table="broker_snapshots",
                key_column="snapshot_id",
                values=values,
            )
            if inserted:
                self._append_event(
                    connection,
                    stream=snapshot.account_key,
                    event_type="BROKER_SNAPSHOT_RECORDED",
                    entity_type="broker_snapshot",
                    entity_id=snapshot.snapshot_id,
                    occurred_at=snapshot.received_at,
                    payload={
                        "evidence_revision": snapshot.evidence_revision,
                        "fully_reconciled": snapshot.fully_reconciled,
                        "advanced_orders_reconciled": snapshot.advanced_orders_reconciled,
                        "realized_pnl_reconciled": snapshot.realized_pnl_reconciled,
                        "positions_digest": snapshot.positions_digest,
                        "orders_digest": snapshot.orders_digest,
                        "equity_position_count": snapshot.equity_position_count,
                        "equity_order_count": snapshot.equity_order_count,
                        "equity_nonterminal_order_count": snapshot.equity_nonterminal_order_count,
                        "external_material_order_count": snapshot.external_material_order_count,
                        "option_position_count": snapshot.option_position_count,
                        "option_order_count": snapshot.option_order_count,
                        "advanced_order_count": snapshot.advanced_order_count,
                        "reconciliation_blocker_count": snapshot.reconciliation_blocker_count,
                    },
                )
            return inserted

    def reconcile_positions(
        self,
        *,
        snapshot_id: str,
        account_key: str,
        positions: Sequence[PositionRecord],
        reconciled_at: datetime,
    ) -> tuple[str, ...]:
        """Replace current position facts from one complete broker snapshot.

        Rows absent from the new complete snapshot are retained as explicit zero
        positions.  The audit chain therefore preserves the transition while
        current reads cannot mistake yesterday's row for live exposure.
        """

        if len({position.symbol for position in positions}) != len(positions):
            raise StateConflict("position snapshot contains duplicate symbols")
        if any(position.account_key != account_key for position in positions):
            raise StateConflict("position snapshot crosses accounts")
        with self.transaction() as connection:
            snapshot = connection.execute(
                "SELECT account_key,positions_reconciled,observed_at,received_at FROM broker_snapshots WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
            if snapshot is None or snapshot["account_key"] != account_key:
                raise LiveStateError("position snapshot has no matching broker snapshot")
            if not bool(snapshot["positions_reconciled"]):
                raise StateConflict("cannot reconcile positions from an incomplete snapshot")
            latest = connection.execute(
                """SELECT b.observed_at FROM audit_events e
                     JOIN broker_snapshots b ON b.snapshot_id=e.entity_id
                     WHERE e.stream=? AND e.event_type='POSITIONS_RECONCILED'
                     ORDER BY e.sequence DESC LIMIT 1""",
                (account_key,),
            ).fetchone()
            if (
                latest is not None
                and latest["observed_at"] is not None
                and snapshot["observed_at"] <= latest["observed_at"]
            ):
                raise OutOfOrderEvent(
                    "complete position replacement is not strictly newer than durable evidence"
                )
            existing = {
                row["symbol"]: row
                for row in connection.execute(
                    "SELECT * FROM positions WHERE account_key=?", (account_key,)
                ).fetchall()
            }
            changed: list[str] = []
            for position in positions:
                prior = existing.pop(position.symbol, None)
                values = (
                    format(position.quantity, "f"),
                    format(position.sellable_quantity, "f"),
                    format(position.held_for_sells, "f"),
                    format(position.average_price, "f") if position.average_price is not None else None,
                    position.source,
                    _iso(position.broker_updated_at),
                    _iso(position.received_at),
                    position.revision,
                    position.raw_hash,
                    snapshot_id,
                )
                if prior is not None:
                    if position.revision < int(prior["revision"]):
                        continue
                    if (
                        position.revision == int(prior["revision"])
                        and tuple(
                            prior[name]
                            for name in (
                                "quantity",
                                "sellable_quantity",
                                "held_for_sells",
                                "average_price",
                                "source",
                                "broker_updated_at",
                                "revision",
                                "raw_hash",
                            )
                        )
                        != (
                            values[0],
                            values[1],
                            values[2],
                            values[3],
                            values[4],
                            values[5],
                            values[7],
                            values[8],
                        )
                    ):
                        raise StateConflict("same position revision has conflicting facts")
                    if position.revision == int(prior["revision"]):
                        if _iso(position.received_at) < prior["received_at"]:
                            raise OutOfOrderEvent("position receipt timestamp decreased")
                        connection.execute(
                            """UPDATE positions SET received_at=?,snapshot_id=?
                               WHERE account_key=? AND symbol=?""",
                            (
                                _iso(position.received_at),
                                snapshot_id,
                                account_key,
                                position.symbol,
                            ),
                        )
                        continue
                    if _iso(position.broker_updated_at) < prior["broker_updated_at"]:
                        raise OutOfOrderEvent("position broker timestamp decreased")
                connection.execute(
                    """INSERT INTO positions(
                           account_key,symbol,quantity,sellable_quantity,held_for_sells,
                           average_price,source,broker_updated_at,received_at,revision,
                           raw_hash,snapshot_id
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(account_key,symbol) DO UPDATE SET
                           quantity=excluded.quantity,
                           sellable_quantity=excluded.sellable_quantity,
                           held_for_sells=excluded.held_for_sells,
                           average_price=excluded.average_price,
                           source=excluded.source,
                           broker_updated_at=excluded.broker_updated_at,
                           received_at=excluded.received_at,
                           revision=excluded.revision,
                           raw_hash=excluded.raw_hash,
                           snapshot_id=excluded.snapshot_id""",
                    (account_key, position.symbol) + values,
                )
                changed.append(position.symbol)
            for symbol, prior in existing.items():
                if Decimal(str(prior["quantity"])) == 0:
                    continue
                revision = int(prior["revision"]) + 1
                connection.execute(
                    """UPDATE positions SET quantity='0',sellable_quantity='0',held_for_sells='0',
                           source='broker_snapshot_absent',broker_updated_at=?,received_at=?,
                           revision=?,raw_hash=?,snapshot_id=?
                       WHERE account_key=? AND symbol=?""",
                    (
                        _iso(reconciled_at),
                        _iso(reconciled_at),
                        revision,
                        object_hash({"closed_by_snapshot": snapshot_id, "symbol": symbol}),
                        snapshot_id,
                        account_key,
                        symbol,
                    ),
                )
                changed.append(symbol)
            self._append_event(
                connection,
                stream=account_key,
                event_type="POSITIONS_RECONCILED",
                entity_type="broker_snapshot",
                entity_id=snapshot_id,
                occurred_at=reconciled_at,
                payload={
                    "symbols": sorted(position.symbol for position in positions),
                    "changed_symbols": sorted(changed),
                },
            )
            return tuple(sorted(changed))

    @staticmethod
    def _plan_values(plan: ExpiringPlan) -> Mapping[str, Any]:
        return {
            "plan_id": plan.plan_id,
            "account_key": plan.account_key,
            "strategy_id": plan.strategy_id,
            "symbol": plan.symbol,
            "setup_id": plan.setup_id,
            "quantity": plan.quantity,
            "limit_price": format(plan.limit_price, "f"),
            "structural_stop": format(plan.structural_stop, "f"),
            "market_hours": plan.market_hours,
            "time_in_force": plan.time_in_force,
            "evidence_cutoff_at": _iso(plan.evidence_cutoff_at),
            "created_at": _iso(plan.created_at),
            "expires_at": _iso(plan.expires_at),
            "policy_hash": plan.policy_hash,
            "config_hash": plan.config_hash,
            "evidence_hash": plan.evidence_hash,
            "targets_json": canonical_json(plan.targets),
            "state": plan.state.value,
        }

    @staticmethod
    def _reservation_values(reservation: RiskReservation) -> Mapping[str, Any]:
        return {
            "reservation_id": reservation.reservation_id,
            "plan_id": reservation.plan_id,
            "account_key": reservation.account_key,
            "planned_risk_cents": to_cents(reservation.planned_risk),
            "stress_risk_cents": to_cents(reservation.stress_risk),
            "execution_reserve_cents": to_cents(reservation.execution_reserve),
            "notional_cents": to_cents(reservation.notional),
            "created_at": _iso(reservation.created_at),
            "state": reservation.state.value,
        }

    @staticmethod
    def _intent_values(intent: OrderIntent) -> Mapping[str, Any]:
        return {
            "intent_id": intent.intent_id,
            "plan_id": intent.plan_id,
            "reservation_id": intent.reservation_id,
            "account_key": intent.account_key,
            "kind": intent.kind.value,
            "client_ref": intent.client_ref,
            "order_tuple_json": canonical_json(intent.order_tuple),
            "tuple_hash": intent.tuple_hash,
            "created_at": _iso(intent.created_at),
            "acknowledgement_deadline_at": _iso(intent.acknowledgement_deadline_at),
            "state": intent.state.value,
            "updated_at": _iso(intent.created_at),
        }

    def prepare_submission(
        self,
        *,
        plan: ExpiringPlan,
        reservation: RiskReservation,
        intent: OrderIntent,
    ) -> bool:
        """Atomically persist plan, risk reservation, and intent before submit."""

        if intent.kind.value != "ENTRY":
            raise StateConflict("entry submission aggregate requires an ENTRY intent")
        if reservation.plan_id != plan.plan_id or intent.plan_id != plan.plan_id:
            raise StateConflict("plan linkage mismatch")
        if intent.reservation_id != reservation.reservation_id:
            raise StateConflict("intent reservation linkage mismatch")
        if len({plan.account_key, reservation.account_key, intent.account_key}) != 1:
            raise StateConflict("account linkage mismatch")
        if intent.tuple_hash != object_hash(intent.order_tuple):
            raise StateConflict("intent tuple hash does not match canonical tuple")
        if plan.state is not PlanState.VALIDATED:
            raise StateConflict("only a validated plan may be prepared")
        if reservation.state is not ReservationState.RESERVED:
            raise StateConflict("risk must be freshly reserved before submission")
        if intent.state is not IntentState.PREPARED:
            raise StateConflict("new intent must begin in PREPARED state")
        if reservation.created_at < plan.created_at or intent.created_at < reservation.created_at:
            raise StateConflict("plan/reservation/intent timestamps are not causal")
        if intent.created_at >= plan.expires_at:
            raise StateConflict("intent was created after plan expiry")

        with self.transaction() as connection:
            inserted_plan = self._insert_exact(
                connection,
                table="plans",
                key_column="plan_id",
                values=self._plan_values(plan),
            )
            inserted_reservation = self._insert_exact(
                connection,
                table="risk_reservations",
                key_column="reservation_id",
                values=self._reservation_values(reservation),
            )
            inserted_intent = self._insert_exact(
                connection,
                table="order_intents",
                key_column="intent_id",
                values=self._intent_values(intent),
            )
            inserted = inserted_plan or inserted_reservation or inserted_intent
            if inserted and not (inserted_plan and inserted_reservation and inserted_intent):
                raise StateConflict("submission aggregate was only partially new")
            if inserted:
                self._append_event(
                    connection,
                    stream=plan.account_key,
                    event_type="SUBMISSION_PREPARED",
                    entity_type="order_intent",
                    entity_id=intent.intent_id,
                    occurred_at=intent.created_at,
                    payload={
                        "plan_id": plan.plan_id,
                        "reservation_id": reservation.reservation_id,
                        "client_ref": intent.client_ref,
                        "tuple_hash": intent.tuple_hash,
                        "policy_hash": plan.policy_hash,
                        "config_hash": plan.config_hash,
                        "evidence_hash": plan.evidence_hash,
                    },
                )
            return inserted

    def prepare_safety_intent(self, intent: OrderIntent) -> bool:
        """Persist a protection, exit, or cancel intent before broker mutation.

        Safety actions reduce or control existing exposure and therefore do not
        consume a second entry-risk reservation.  They remain bound to the
        originating plan and the canonical exact order/cancel tuple.
        """

        if intent.kind.value == "ENTRY" or intent.reservation_id is not None:
            raise StateConflict("safety intent must not carry an entry risk reservation")
        if intent.state is not IntentState.PREPARED:
            raise StateConflict("new safety intent must begin in PREPARED state")
        if intent.tuple_hash != object_hash(intent.order_tuple):
            raise StateConflict("safety intent tuple hash does not match canonical tuple")
        with self.transaction() as connection:
            plan = connection.execute(
                "SELECT account_key FROM plans WHERE plan_id=?", (intent.plan_id,)
            ).fetchone()
            if plan is None:
                raise LiveStateError("safety intent has no originating plan")
            if plan["account_key"] != intent.account_key:
                raise StateConflict("safety intent crosses accounts")
            inserted = self._insert_exact(
                connection,
                table="order_intents",
                key_column="intent_id",
                values=self._intent_values(intent),
            )
            if inserted:
                self._append_event(
                    connection,
                    stream=intent.account_key,
                    event_type="SAFETY_INTENT_PREPARED",
                    entity_type="order_intent",
                    entity_id=intent.intent_id,
                    occurred_at=intent.created_at,
                    payload={
                        "plan_id": intent.plan_id,
                        "kind": intent.kind.value,
                        "client_ref": intent.client_ref,
                        "tuple_hash": intent.tuple_hash,
                    },
                )
            return inserted

    def transition_intent(
        self,
        intent_id: str,
        state: IntentState,
        *,
        occurred_at: datetime,
        detail: Mapping[str, Any] | None = None,
    ) -> bool:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT account_key, state FROM order_intents WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
            if row is None:
                raise LiveStateError(f"unknown intent {intent_id!r}")
            current = IntentState(row["state"])
            if current is state:
                return False
            if state not in _INTENT_TRANSITIONS.get(current, frozenset()):
                raise StateConflict(f"invalid intent transition {current.value} -> {state.value}")
            connection.execute(
                "UPDATE order_intents SET state = ?, updated_at = ? WHERE intent_id = ?",
                (state.value, _iso(occurred_at), intent_id),
            )
            self._append_event(
                connection,
                stream=row["account_key"],
                event_type=f"INTENT_{state.value}",
                entity_type="order_intent",
                entity_id=intent_id,
                occurred_at=occurred_at,
                payload=dict(detail or {}),
            )
            return True

    def release_reservation(
        self,
        reservation_id: str,
        *,
        occurred_at: datetime,
        reason: str,
    ) -> bool:
        """Release risk after a known terminal result proves zero exposure.

        This is the narrow, immediate-release path used for review failures and
        conclusive broker rejections.  A terminal intent label is insufficient
        by itself: a cancel/failure can race a fill, so durable broker evidence
        must also prove that no shares filled and no order remains live.
        Ambiguous, acknowledged, reconciled, or filled submissions use
        :meth:`release_reservations_after_flat_snapshot` instead.
        """

        with self.transaction() as connection:
            row = connection.execute(
                """SELECT r.account_key,r.state,i.intent_id,i.kind,
                          i.state AS intent_state
                   FROM risk_reservations r JOIN order_intents i
                     ON i.reservation_id=r.reservation_id
                   WHERE r.reservation_id=?""",
                (reservation_id,),
            ).fetchone()
            if row is None:
                raise LiveStateError("unknown risk reservation")
            if row["state"] == ReservationState.RELEASED.value:
                return False
            if row["kind"] != "ENTRY":
                raise StateConflict("risk reservation is not linked to an entry intent")
            intent_state = IntentState(row["intent_state"])
            if intent_state not in {
                IntentState.REJECTED,
                IntentState.FAILED,
            }:
                raise StateConflict(
                    "immediate risk release requires a known no-exposure terminal result"
                )
            transition = connection.execute(
                """SELECT payload_json FROM audit_events
                     WHERE entity_type='order_intent' AND entity_id=?
                       AND event_type=? ORDER BY sequence DESC LIMIT 1""",
                (row["intent_id"], f"INTENT_{intent_state.value}"),
            ).fetchone()
            if transition is None:
                raise StateConflict("terminal intent has no durable transition proof")
            try:
                detail = json.loads(str(transition["payload_json"]))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise StateConflict("terminal intent proof is unreadable") from exc
            known_no_exposure = (
                intent_state is IntentState.REJECTED
                and detail.get("known_reject") is True
            ) or (
                intent_state is IntentState.FAILED
                and (
                    detail.get("phase") in {"review", "review_validation"}
                    or (
                        detail.get("phase") == "place"
                        and detail.get("known_no_accept") is True
                    )
                )
            )
            if not known_no_exposure:
                raise StateConflict(
                    "terminal intent does not prove a known no-exposure failure"
                )
            order_rows = connection.execute(
                """SELECT state,cumulative_filled_quantity
                     FROM broker_orders WHERE intent_id=?""",
                (row["intent_id"],),
            ).fetchall()
            zero_exposure_terminal_states = {
                BrokerOrderState.CANCELLED.value,
                BrokerOrderState.REJECTED.value,
                BrokerOrderState.FAILED.value,
                BrokerOrderState.VOIDED.value,
                BrokerOrderState.LOCATE_FAILED.value,
            }
            if any(
                int(order["cumulative_filled_quantity"]) != 0
                or order["state"] not in zero_exposure_terminal_states
                for order in order_rows
            ):
                raise StateConflict(
                    "immediate risk release is blocked by possible broker exposure"
                )
            fill_count = connection.execute(
                """SELECT COUNT(*) FROM fills f JOIN broker_orders o
                         ON o.broker_order_id=f.broker_order_id
                     WHERE o.intent_id=?""",
                (row["intent_id"],),
            ).fetchone()[0]
            if int(fill_count) != 0:
                raise StateConflict("immediate risk release is blocked by a durable fill")
            connection.execute(
                "UPDATE risk_reservations SET state=? WHERE reservation_id=?",
                (ReservationState.RELEASED.value, reservation_id),
            )
            self._append_event(
                connection,
                stream=row["account_key"],
                event_type="RISK_RESERVATION_RELEASED",
                entity_type="risk_reservation",
                entity_id=reservation_id,
                occurred_at=occurred_at,
                payload={
                    "reason": str(reason),
                    "intent_state": intent_state.value,
                    "proof": "KNOWN_TERMINAL_ZERO_FILL",
                },
            )
            return True

    def release_reservations_after_flat_snapshot(
        self,
        *,
        account_key: str,
        snapshot_id: str,
        occurred_at: datetime,
    ) -> tuple[str, ...]:
        """Release closed entry risk from complete, newer durable flat proof.

        The supplied snapshot is treated only as an identifier.  Every gate is
        re-read inside the same SQLite transaction: it must be the newest
        complete account-wide snapshot, all broker scope counts must be flat,
        its complete position replacement must be durably audited, and local
        positions, orders, possible submissions, protection obligations, and
        signed fill inventory must independently agree that the account is
        flat.  Reservations whose causal exposure evidence is not strictly
        older remain reserved.

        An unresolved ``UNKNOWN`` intent deliberately blocks this path.  It
        first needs the existing authoritative client-reference resolution;
        mere absence from an order-list response never frees risk.
        """

        normalized_account = str(account_key).strip()
        normalized_snapshot = str(snapshot_id).strip()
        if not normalized_account or not normalized_snapshot:
            raise ValueError("account_key and snapshot_id are required")
        released_at = datetime.fromisoformat(_iso(occurred_at)).astimezone(timezone.utc)

        with self.transaction() as connection:
            snapshot = connection.execute(
                "SELECT * FROM broker_snapshots WHERE snapshot_id=?",
                (normalized_snapshot,),
            ).fetchone()
            if snapshot is None:
                raise LiveStateError("flat-release snapshot is missing")
            if snapshot["account_key"] != normalized_account:
                raise StateConflict("flat-release evidence crosses accounts")

            complete_fields = (
                "positions_reconciled",
                "equity_orders_reconciled",
                "option_positions_reconciled",
                "option_orders_reconciled",
                "advanced_orders_reconciled",
                "realized_pnl_reconciled",
            )
            flat_scope_counts = (
                "equity_position_count",
                "equity_nonterminal_order_count",
                "external_material_order_count",
                "option_position_count",
                "option_order_count",
                "advanced_order_count",
                "reconciliation_blocker_count",
            )
            if not all(bool(snapshot[field]) for field in complete_fields):
                return ()
            if any(int(snapshot[field]) != 0 for field in flat_scope_counts):
                return ()

            latest = connection.execute(
                """SELECT snapshot_id FROM broker_snapshots
                     WHERE account_key=?
                     ORDER BY observed_at DESC,received_at DESC,snapshot_id DESC LIMIT 1""",
                (normalized_account,),
            ).fetchone()
            if latest is None or latest["snapshot_id"] != normalized_snapshot:
                return ()
            position_proof = connection.execute(
                """SELECT 1 FROM audit_events WHERE stream=?
                     AND event_type='POSITIONS_RECONCILED'
                     AND entity_type='broker_snapshot' AND entity_id=? LIMIT 1""",
                (normalized_account, normalized_snapshot),
            ).fetchone()
            if position_proof is None:
                return ()

            durable_positions = connection.execute(
                """SELECT COUNT(*) FROM positions WHERE account_key=?
                     AND (CAST(quantity AS REAL)<>0
                          OR CAST(sellable_quantity AS REAL)<>0
                          OR CAST(held_for_sells AS REAL)<>0)""",
                (normalized_account,),
            ).fetchone()[0]
            terminal_order_states = tuple(
                state.value for state in BrokerOrderState if state.terminal
            )
            placeholders = ",".join("?" for _ in terminal_order_states)
            durable_orders = connection.execute(
                f"""SELECT COUNT(*) FROM broker_orders WHERE account_key=?
                      AND state NOT IN ({placeholders})""",
                (normalized_account,) + terminal_order_states,
            ).fetchone()[0]
            unresolved_entries = connection.execute(
                """SELECT COUNT(*) FROM order_intents WHERE account_key=?
                     AND kind='ENTRY' AND state IN ('SUBMITTING','UNKNOWN')""",
                (normalized_account,),
            ).fetchone()[0]
            unresolved_safety = connection.execute(
                f"""SELECT COUNT(*) FROM order_intents i
                     LEFT JOIN broker_orders o ON o.intent_id=i.intent_id
                     WHERE i.account_key=? AND i.kind<>'ENTRY' AND (
                         i.state IN ('PREPARED','SUBMITTING','UNKNOWN')
                         OR (i.state='ACKNOWLEDGED' AND
                             (o.broker_order_id IS NULL OR o.state NOT IN ({placeholders})))
                     )""",
                (normalized_account,) + terminal_order_states,
            ).fetchone()[0]
            open_obligations = connection.execute(
                """SELECT COUNT(*) FROM protection_obligations
                     WHERE account_key=? AND state NOT IN ('SATISFIED','CANCELLED')""",
                (normalized_account,),
            ).fetchone()[0]
            if any(
                int(value) != 0
                for value in (
                    durable_positions,
                    durable_orders,
                    unresolved_entries,
                    unresolved_safety,
                    open_obligations,
                )
            ):
                return ()

            # Broker counts alone cannot prove that locally durable fills were
            # closed.  Reconstruct signed whole-share inventory from each
            # intent's immutable tuple and require an exact zero per symbol.
            order_fill_totals = connection.execute(
                """SELECT o.broker_order_id,o.cumulative_filled_quantity,
                          COALESCE(SUM(f.quantity),0) AS durable_fill_quantity
                     FROM broker_orders o LEFT JOIN fills f
                       ON f.broker_order_id=o.broker_order_id
                     WHERE o.account_key=? GROUP BY o.broker_order_id,
                          o.cumulative_filled_quantity""",
                (normalized_account,),
            ).fetchall()
            if any(
                int(order["cumulative_filled_quantity"])
                != int(order["durable_fill_quantity"])
                for order in order_fill_totals
            ):
                return ()
            signed_inventory: dict[str, int] = {}
            fill_rows = connection.execute(
                """SELECT f.quantity,i.order_tuple_json FROM fills f
                     JOIN broker_orders o ON o.broker_order_id=f.broker_order_id
                     JOIN order_intents i ON i.intent_id=o.intent_id
                     WHERE f.account_key=?""",
                (normalized_account,),
            ).fetchall()
            for fill in fill_rows:
                try:
                    order_tuple = json.loads(str(fill["order_tuple_json"]))
                    symbol = str(order_tuple["symbol"]).strip().upper()
                    side = str(order_tuple["side"]).strip().lower()
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    return ()
                if not symbol or side not in {"buy", "sell"}:
                    return ()
                quantity = int(fill["quantity"])
                signed_inventory[symbol] = signed_inventory.get(symbol, 0) + (
                    quantity if side == "buy" else -quantity
                )
            if any(quantity != 0 for quantity in signed_inventory.values()):
                return ()

            snapshot_observed_at = datetime.fromisoformat(
                str(snapshot["observed_at"])
            ).astimezone(timezone.utc)
            snapshot_received_at = datetime.fromisoformat(
                str(snapshot["received_at"])
            ).astimezone(timezone.utc)
            if released_at < snapshot_received_at:
                return ()
            candidates = connection.execute(
                """SELECT r.reservation_id,r.created_at,i.intent_id,
                          i.created_at AS intent_created_at,i.updated_at,i.state AS intent_state
                     FROM risk_reservations r JOIN order_intents i
                       ON i.reservation_id=r.reservation_id
                     WHERE r.account_key=? AND r.state<>'RELEASED' AND i.kind='ENTRY'
                       AND i.state IN ('ACKNOWLEDGED','RECONCILED','REJECTED','CANCELLED','FAILED')
                     ORDER BY r.created_at,r.reservation_id""",
                (normalized_account,),
            ).fetchall()
            released: list[str] = []
            for candidate in candidates:
                intent_state = IntentState(candidate["intent_state"])
                intent_updated_at = datetime.fromisoformat(
                    str(candidate["updated_at"])
                ).astimezone(timezone.utc)
                floor_values = [
                    datetime.fromisoformat(str(candidate["created_at"])).astimezone(
                        timezone.utc
                    ),
                    datetime.fromisoformat(
                        str(candidate["intent_created_at"])
                    ).astimezone(timezone.utc),
                ]
                if intent_state is IntentState.RECONCILED:
                    # RECONCILED is itself derived from authoritative broker
                    # evidence.  The same snapshot may carry that proof, but
                    # a snapshot predating the resolution may never release.
                    if snapshot_observed_at < intent_updated_at:
                        continue
                else:
                    floor_values.append(intent_updated_at)
                broker_times = connection.execute(
                    """SELECT o.broker_updated_at,f.executed_at
                         FROM broker_orders o LEFT JOIN fills f
                           ON f.broker_order_id=o.broker_order_id
                         WHERE o.intent_id=?""",
                    (candidate["intent_id"],),
                ).fetchall()
                if intent_state is IntentState.ACKNOWLEDGED and not broker_times:
                    # An ACKNOWLEDGED label without the corresponding durable
                    # broker order is possible exposure, never flat proof.
                    continue
                if intent_state is IntentState.RECONCILED and not broker_times:
                    resolution = connection.execute(
                        """SELECT payload_json FROM audit_events
                             WHERE entity_type='order_intent' AND entity_id=?
                               AND event_type='INTENT_RECONCILED'
                             ORDER BY sequence DESC LIMIT 1""",
                        (candidate["intent_id"],),
                    ).fetchone()
                    if resolution is None:
                        continue
                    try:
                        resolution_detail = json.loads(
                            str(resolution["payload_json"])
                        )
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                    if (
                        resolution_detail.get("resolution")
                        != "BROKER_CONFIRMED_CLIENT_REF_ABSENT"
                    ):
                        continue
                    unknown_transition = connection.execute(
                        """SELECT occurred_at FROM audit_events
                             WHERE entity_type='order_intent' AND entity_id=?
                               AND event_type='INTENT_UNKNOWN'
                             ORDER BY sequence DESC LIMIT 1""",
                        (candidate["intent_id"],),
                    ).fetchone()
                    if unknown_transition is None:
                        continue
                    floor_values.append(
                        datetime.fromisoformat(
                            str(unknown_transition["occurred_at"])
                        ).astimezone(timezone.utc)
                    )
                for evidence in broker_times:
                    for field in ("broker_updated_at", "executed_at"):
                        if evidence[field] is not None:
                            floor_values.append(
                                datetime.fromisoformat(str(evidence[field])).astimezone(
                                    timezone.utc
                                )
                            )
                evidence_floor = max(floor_values)
                if snapshot_observed_at <= evidence_floor:
                    continue
                connection.execute(
                    "UPDATE risk_reservations SET state=? WHERE reservation_id=?",
                    (ReservationState.RELEASED.value, candidate["reservation_id"]),
                )
                self._append_event(
                    connection,
                    stream=normalized_account,
                    event_type="RISK_RESERVATION_RELEASED",
                    entity_type="risk_reservation",
                    entity_id=candidate["reservation_id"],
                    occurred_at=released_at,
                    payload={
                        "reason": "ACCOUNT_WIDE_BROKER_FLAT",
                        "intent_state": intent_state.value,
                        "proof": "STRICTLY_NEWER_COMPLETE_DURABLE_FLAT_SNAPSHOT",
                        "flatness_snapshot_id": normalized_snapshot,
                        "snapshot_observed_at": _iso(snapshot_observed_at),
                        "snapshot_received_at": _iso(snapshot_received_at),
                        "evidence_floor_at": _iso(evidence_floor),
                    },
                )
                released.append(str(candidate["reservation_id"]))
            return tuple(released)

    def record_activation(
        self,
        *,
        activation_id: str,
        account_key: str,
        record: Mapping[str, Any],
        created_at: datetime,
        expires_at: datetime,
    ) -> bool:
        if expires_at <= created_at:
            raise ValueError("activation expiry must follow creation")
        with self.transaction() as connection:
            runtime = connection.execute(
                "SELECT * FROM runtime_identity WHERE singleton=1"
            ).fetchone()
            if runtime is None:
                raise LiveStateError("runtime identity is missing")
            record_json = canonical_json(record)
            values = {
                "activation_id": activation_id,
                "account_key": account_key,
                "record_hash": object_hash(record),
                "created_at": _iso(created_at),
                "expires_at": _iso(expires_at),
                "consumed_at": None,
                "record_json": record_json,
            }
            parsed = self._activation_record_from_row(values, runtime)
            if parsed.activation_id != str(activation_id):
                raise StateConflict("activation identifier argument differs from payload")
            if parsed.account_key != str(account_key):
                raise StateConflict("activation account argument differs from payload")
            if parsed.created_at != created_at.astimezone(timezone.utc):
                raise StateConflict("activation creation time differs from payload")
            if parsed.expires_at != expires_at.astimezone(timezone.utc):
                raise StateConflict("activation expiry differs from payload")
            readiness_age = parsed.created_at - parsed.readiness_evidence.collected_at
            if readiness_age < timedelta(0) or readiness_age > timedelta(seconds=5):
                raise StateConflict("activation readiness was not collected immediately before staging")
            inserted = self._insert_exact(
                connection,
                table="activation_records",
                key_column="activation_id",
                values=values,
            )
            if inserted:
                self._append_event(
                    connection,
                    stream=account_key,
                    event_type="ACTIVATION_RECORD_STAGED",
                    entity_type="activation",
                    entity_id=activation_id,
                    occurred_at=created_at,
                    payload={"record_hash": values["record_hash"], "expires_at": values["expires_at"]},
                )
            return inserted

    def consume_activation(self, activation_id: str, *, consumed_at: datetime) -> bool:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM activation_records WHERE activation_id=?", (activation_id,)
            ).fetchone()
            if row is None:
                raise LiveStateError("unknown activation record")
            if row["consumed_at"] is not None:
                return False
            when = _iso(consumed_at)
            if when < row["created_at"] or when > row["expires_at"]:
                raise StateConflict("activation record is outside its validity window")
            connection.execute(
                "UPDATE activation_records SET consumed_at=? WHERE activation_id=?",
                (when, activation_id),
            )
            self._append_event(
                connection,
                stream=row["account_key"],
                event_type="ACTIVATION_RECORD_CONSUMED",
                entity_type="activation",
                entity_id=activation_id,
                occurred_at=consumed_at,
                payload={"record_hash": row["record_hash"]},
            )
            return True

    def record_broker_order(self, order: BrokerOrder) -> bool:
        """Record only monotonic broker revisions and cumulative fill quantity."""

        values = {
            "broker_order_id": order.broker_order_id,
            "intent_id": order.intent_id,
            "account_key": order.account_key,
            "state": order.state.value,
            "quantity": order.quantity,
            "cumulative_filled_quantity": order.cumulative_filled_quantity,
            "revision": order.revision,
            "broker_updated_at": _iso(order.broker_updated_at),
            "received_at": _iso(order.received_at),
            "raw_hash": order.raw_hash,
        }
        with self.transaction() as connection:
            intent = connection.execute(
                "SELECT account_key, state FROM order_intents WHERE intent_id = ?",
                (order.intent_id,),
            ).fetchone()
            if intent is None:
                raise LiveStateError("broker order has no durable local intent")
            if intent["account_key"] != order.account_key:
                raise StateConflict("broker order account differs from intent")
            existing = connection.execute(
                "SELECT * FROM broker_orders WHERE broker_order_id = ?",
                (order.broker_order_id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO broker_orders(
                           broker_order_id, intent_id, account_key, state, quantity,
                           cumulative_filled_quantity, revision, broker_updated_at,
                           received_at, raw_hash
                       ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    tuple(values.values()),
                )
            else:
                if order.revision < existing["revision"]:
                    return False
                if order.revision == existing["revision"]:
                    selected = tuple(existing[key] for key in values)
                    if selected != tuple(values.values()):
                        raise StateConflict("same broker revision contains conflicting facts")
                    return False
                if (
                    existing["intent_id"] != order.intent_id
                    or existing["account_key"] != order.account_key
                    or existing["quantity"] != order.quantity
                ):
                    raise StateConflict("broker order immutable identity changed")
                if order.cumulative_filled_quantity < existing["cumulative_filled_quantity"]:
                    raise OutOfOrderEvent("cumulative fill quantity decreased")
                if _iso(order.broker_updated_at) < existing["broker_updated_at"]:
                    raise OutOfOrderEvent("broker update timestamp decreased")
                connection.execute(
                    """UPDATE broker_orders SET
                           state = ?, cumulative_filled_quantity = ?, revision = ?,
                           broker_updated_at = ?, received_at = ?, raw_hash = ?
                       WHERE broker_order_id = ?""",
                    (
                        order.state.value,
                        order.cumulative_filled_quantity,
                        order.revision,
                        _iso(order.broker_updated_at),
                        _iso(order.received_at),
                        order.raw_hash,
                        order.broker_order_id,
                    ),
                )

            current_intent = IntentState(intent["state"])
            if current_intent in {IntentState.SUBMITTING, IntentState.UNKNOWN}:
                connection.execute(
                    "UPDATE order_intents SET state = ?, updated_at = ? WHERE intent_id = ?",
                    (IntentState.ACKNOWLEDGED.value, _iso(order.received_at), order.intent_id),
                )
            elif current_intent is not IntentState.ACKNOWLEDGED:
                raise StateConflict(
                    f"broker acknowledgement is incompatible with {current_intent.value} intent"
                )
            self._append_event(
                connection,
                stream=order.account_key,
                event_type="BROKER_ORDER_RECONCILED",
                entity_type="broker_order",
                entity_id=order.broker_order_id,
                occurred_at=order.received_at,
                payload={
                    "intent_id": order.intent_id,
                    "state": order.state.value,
                    "revision": order.revision,
                    "cumulative_filled_quantity": order.cumulative_filled_quantity,
                    "raw_hash": order.raw_hash,
                },
            )
            return True

    def record_fill(self, fill: Fill) -> bool:
        values = {
            "fill_id": fill.fill_id,
            "broker_order_id": fill.broker_order_id,
            "account_key": fill.account_key,
            "quantity": fill.quantity,
            "price": format(fill.price, "f"),
            "executed_at": _iso(fill.executed_at),
            "received_at": _iso(fill.received_at),
        }
        with self.transaction() as connection:
            order = connection.execute(
                "SELECT account_key, quantity FROM broker_orders WHERE broker_order_id = ?",
                (fill.broker_order_id,),
            ).fetchone()
            if order is None:
                raise LiveStateError("fill has no reconciled broker order")
            if order["account_key"] != fill.account_key:
                raise StateConflict("fill account differs from broker order")
            inserted = self._insert_exact(
                connection,
                table="fills",
                key_column="fill_id",
                values=values,
            )
            if not inserted:
                return False
            total = connection.execute(
                "SELECT COALESCE(SUM(quantity), 0) FROM fills WHERE broker_order_id = ?",
                (fill.broker_order_id,),
            ).fetchone()[0]
            if int(total) > int(order["quantity"]):
                raise StateConflict("aggregate fills exceed authorized order quantity")
            self._append_event(
                connection,
                stream=fill.account_key,
                event_type="FILL_RECORDED",
                entity_type="fill",
                entity_id=fill.fill_id,
                occurred_at=fill.received_at,
                payload={
                    "broker_order_id": fill.broker_order_id,
                    "quantity": fill.quantity,
                    "price": format(fill.price, "f"),
                    "executed_at": _iso(fill.executed_at),
                },
            )
            return True

    def record_protection_obligation(self, obligation: ProtectionObligation) -> bool:
        with self.transaction() as connection:
            source = connection.execute(
                """SELECT f.account_key, f.quantity, p.symbol, p.structural_stop
                   FROM fills f
                   JOIN broker_orders o ON o.broker_order_id = f.broker_order_id
                   JOIN order_intents i ON i.intent_id = o.intent_id
                   JOIN plans p ON p.plan_id = i.plan_id
                   WHERE f.fill_id = ?""",
                (obligation.source_fill_id,),
            ).fetchone()
            if source is None:
                raise LiveStateError("protection obligation has no source fill")
            if (
                source["account_key"] != obligation.account_key
                or source["symbol"] != obligation.symbol
                or int(source["quantity"]) != obligation.required_quantity
                or Decimal(source["structural_stop"]) != obligation.stop_price
            ):
                raise StateConflict("protection obligation differs from source fill plan")
            if obligation.state is ProtectionState.WORKING and (
                obligation.working_quantity <= 0 or obligation.broker_order_id is None
            ):
                raise StateConflict("working protection requires broker order and quantity")

            existing = connection.execute(
                "SELECT * FROM protection_obligations WHERE obligation_id = ?",
                (obligation.obligation_id,),
            ).fetchone()
            if existing is not None:
                if obligation.revision < existing["revision"]:
                    return False
                current = (
                    existing["source_fill_id"],
                    existing["account_key"],
                    existing["symbol"],
                    existing["required_quantity"],
                    existing["stop_price"],
                )
                proposed = (
                    obligation.source_fill_id,
                    obligation.account_key,
                    obligation.symbol,
                    obligation.required_quantity,
                    format(obligation.stop_price, "f"),
                )
                if current != proposed:
                    raise StateConflict("protection obligation immutable identity changed")
                if obligation.revision == existing["revision"]:
                    exact = (
                        obligation.working_quantity,
                        obligation.state.value,
                        _iso(obligation.updated_at),
                        obligation.broker_order_id,
                    )
                    actual = (
                        existing["working_quantity"],
                        existing["state"],
                        existing["updated_at"],
                        existing["broker_order_id"],
                    )
                    if exact != actual:
                        raise StateConflict("same protection revision contains conflicting facts")
                    return False
                if _iso(obligation.updated_at) < existing["updated_at"]:
                    raise OutOfOrderEvent("protection update timestamp decreased")
                if existing["broker_order_id"] and obligation.broker_order_id != existing["broker_order_id"]:
                    raise StateConflict("protection broker order binding changed")
                connection.execute(
                    """UPDATE protection_obligations SET
                           working_quantity = ?, state = ?, revision = ?,
                           updated_at = ?, broker_order_id = ?
                       WHERE obligation_id = ?""",
                    (
                        obligation.working_quantity,
                        obligation.state.value,
                        obligation.revision,
                        _iso(obligation.updated_at),
                        obligation.broker_order_id,
                        obligation.obligation_id,
                    ),
                )
            else:
                connection.execute(
                    """INSERT INTO protection_obligations(
                           obligation_id, source_fill_id, account_key, symbol,
                           required_quantity, working_quantity, stop_price, state,
                           revision, updated_at, broker_order_id
                       ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        obligation.obligation_id,
                        obligation.source_fill_id,
                        obligation.account_key,
                        obligation.symbol,
                        obligation.required_quantity,
                        obligation.working_quantity,
                        format(obligation.stop_price, "f"),
                        obligation.state.value,
                        obligation.revision,
                        _iso(obligation.updated_at),
                        obligation.broker_order_id,
                    ),
                )
            self._append_event(
                connection,
                stream=obligation.account_key,
                event_type="PROTECTION_RECONCILED",
                entity_type="protection_obligation",
                entity_id=obligation.obligation_id,
                occurred_at=obligation.updated_at,
                payload={
                    "state": obligation.state.value,
                    "revision": obligation.revision,
                    "required_quantity": obligation.required_quantity,
                    "working_quantity": obligation.working_quantity,
                    "uncovered_quantity": obligation.uncovered_quantity,
                    "broker_order_id": obligation.broker_order_id,
                },
            )
            return True

    def apply_session_latch(self, latch: SessionLatch) -> bool:
        """Persist one-way account-day safety latches."""

        key = (latch.account_key, latch.trading_date.isoformat())
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM session_latches WHERE account_key = ? AND trading_date = ?",
                key,
            ).fetchone()
            proposed_flags = (
                int(latch.loss_locked),
                int(latch.objective_crossed),
                int(latch.pause_new_entries),
                int(latch.closeout_started),
                int(latch.hard_kill),
            )
            if existing is not None:
                if latch.revision < existing["revision"]:
                    return False
                current_flags = tuple(
                    int(existing[name])
                    for name in (
                        "loss_locked",
                        "objective_crossed",
                        "pause_new_entries",
                        "closeout_started",
                        "hard_kill",
                    )
                )
                if latch.revision == existing["revision"]:
                    exact = current_flags + (
                        int(existing["highest_realized_pnl_cents"]),
                        existing["first_objective_crossed_at"],
                        existing["updated_at"],
                    )
                    proposed = proposed_flags + (
                        to_cents(latch.highest_realized_pnl),
                        _iso(latch.first_objective_crossed_at)
                        if latch.first_objective_crossed_at is not None
                        else None,
                        _iso(latch.updated_at),
                    )
                    if exact != proposed:
                        raise StateConflict("same latch revision contains conflicting facts")
                    return False
                if any(old and not new for old, new in zip(current_flags, proposed_flags)):
                    raise StateConflict("irreversible session latch cannot be cleared")
                if to_cents(latch.highest_realized_pnl) < int(
                    existing["highest_realized_pnl_cents"]
                ):
                    raise StateConflict("highest realized P&L latch cannot decrease")
                existing_crossed_at = existing["first_objective_crossed_at"]
                proposed_crossed_at = (
                    _iso(latch.first_objective_crossed_at)
                    if latch.first_objective_crossed_at is not None
                    else None
                )
                if existing_crossed_at is not None and proposed_crossed_at != existing_crossed_at:
                    raise StateConflict("first objective crossing timestamp is immutable")
                if _iso(latch.updated_at) < existing["updated_at"]:
                    raise OutOfOrderEvent("latch timestamp decreased")
                connection.execute(
                    """UPDATE session_latches SET
                           loss_locked = ?, objective_crossed = ?, pause_new_entries = ?,
                           closeout_started = ?, hard_kill = ?,
                           highest_realized_pnl_cents = ?, first_objective_crossed_at = ?,
                           revision = ?, updated_at = ?
                       WHERE account_key = ? AND trading_date = ?""",
                    proposed_flags
                    + (
                        to_cents(latch.highest_realized_pnl),
                        _iso(latch.first_objective_crossed_at)
                        if latch.first_objective_crossed_at is not None
                        else None,
                        latch.revision,
                        _iso(latch.updated_at),
                        latch.account_key,
                        latch.trading_date.isoformat(),
                    ),
                )
            else:
                connection.execute(
                    """INSERT INTO session_latches(
                           account_key, trading_date, loss_locked, objective_crossed,
                           pause_new_entries, closeout_started, hard_kill,
                           highest_realized_pnl_cents, first_objective_crossed_at,
                           revision, updated_at
                       ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    key
                    + proposed_flags
                    + (
                        to_cents(latch.highest_realized_pnl),
                        _iso(latch.first_objective_crossed_at)
                        if latch.first_objective_crossed_at is not None
                        else None,
                        latch.revision,
                        _iso(latch.updated_at),
                    ),
                )
            self._append_event(
                connection,
                stream=latch.account_key,
                event_type="SESSION_LATCH_RECORDED",
                entity_type="session_latch",
                entity_id=f"{latch.account_key}:{latch.trading_date.isoformat()}",
                occurred_at=latch.updated_at,
                payload={
                    "trading_date": latch.trading_date.isoformat(),
                    "loss_locked": latch.loss_locked,
                    "objective_crossed": latch.objective_crossed,
                    "pause_new_entries": latch.pause_new_entries,
                    "closeout_started": latch.closeout_started,
                    "hard_kill": latch.hard_kill,
                    "highest_realized_pnl": format(latch.highest_realized_pnl, "f"),
                    "first_objective_crossed_at": (
                        _iso(latch.first_objective_crossed_at)
                        if latch.first_objective_crossed_at is not None
                        else None
                    ),
                    "revision": latch.revision,
                },
            )
            return True

    def record_incident(self, incident: Incident) -> bool:
        values = {
            "incident_id": incident.incident_id,
            "account_key": incident.account_key,
            "category": incident.category,
            "severity": incident.severity.value,
            "detail_json": canonical_json(incident.detail),
            "opened_at": _iso(incident.opened_at),
            "resolved_at": None,
        }
        with self.transaction() as connection:
            inserted = self._insert_exact(
                connection,
                table="incidents",
                key_column="incident_id",
                values=values,
            )
            if inserted:
                self._append_event(
                    connection,
                    stream=incident.account_key,
                    event_type="INCIDENT_OPENED",
                    entity_type="incident",
                    entity_id=incident.incident_id,
                    occurred_at=incident.opened_at,
                    payload={
                        "category": incident.category,
                        "severity": incident.severity.value,
                        "detail": incident.detail,
                    },
                )
            return inserted

    def resolve_incident(self, incident_id: str, *, resolved_at: datetime) -> bool:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT account_key, opened_at, resolved_at FROM incidents WHERE incident_id = ?",
                (incident_id,),
            ).fetchone()
            if row is None:
                raise LiveStateError(f"unknown incident {incident_id!r}")
            if row["resolved_at"] is not None:
                return False
            when = _iso(resolved_at)
            if when < row["opened_at"]:
                raise OutOfOrderEvent("incident resolution predates opening")
            connection.execute(
                "UPDATE incidents SET resolved_at = ? WHERE incident_id = ?",
                (when, incident_id),
            )
            self._append_event(
                connection,
                stream=row["account_key"],
                event_type="INCIDENT_RESOLVED",
                entity_type="incident",
                entity_id=incident_id,
                occurred_at=resolved_at,
                payload={},
            )
            return True

    def enqueue_notification(self, message: OutboxMessage) -> bool:
        values = {
            "message_id": message.message_id,
            "event_key": message.event_key,
            "account_key": message.account_key,
            "template": message.template,
            "payload_json": canonical_json(message.payload),
            "created_at": _iso(message.created_at),
            "state": message.state.value,
            "attempt_count": 0,
            "last_attempt_at": None,
            "next_attempt_at": _iso(message.created_at),
            "delivered_at": None,
            "last_error": None,
            "delivery_receipt": None,
            "claim_owner": None,
            "claim_expires_at": None,
            "delivery_route_id": None,
            "delivery_assurance": None,
            "delivery_receipt_hash": None,
            "delivery_payload_hash": None,
        }
        with self.transaction() as connection:
            existing_key = connection.execute(
                "SELECT message_id,account_key,template FROM notification_outbox WHERE event_key = ?",
                (message.event_key,),
            ).fetchone()
            if existing_key is not None:
                if existing_key["message_id"] != message.message_id:
                    raise StateConflict("notification event_key already belongs to another message")
                if (
                    existing_key["account_key"] != message.account_key
                    or existing_key["template"] != message.template
                ):
                    raise StateConflict("notification replay changes its account or event type")
                return False
            inserted = self._insert_exact(
                connection,
                table="notification_outbox",
                key_column="message_id",
                values=values,
            )
            if inserted:
                self._append_event(
                    connection,
                    stream=message.account_key,
                    event_type="NOTIFICATION_ENQUEUED",
                    entity_type="notification",
                    entity_id=message.message_id,
                    occurred_at=message.created_at,
                    payload={"event_key": message.event_key, "template": message.template},
                )
            return inserted

    def mark_notification_attempt(
        self,
        message_id: str,
        *,
        attempted_at: datetime,
        delivered: bool,
        error: str | None = None,
        next_attempt_at: datetime | None = None,
        delivery_receipt: str | None = None,
        claim_owner: str | None = None,
        delivery_route_id: str | None = None,
        delivery_assurance: str | None = None,
        delivery_receipt_hash: str | None = None,
        delivery_payload_hash: str | None = None,
    ) -> None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT account_key,state,claim_owner FROM notification_outbox WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            if row is None:
                raise LiveStateError(f"unknown notification {message_id!r}")
            if row["state"] == OutboxState.DELIVERED.value:
                return
            if row["claim_owner"] is not None and row["claim_owner"] != claim_owner:
                raise StateConflict("notification attempt does not own its durable claim")
            if claim_owner is not None and row["claim_owner"] is None:
                raise StateConflict("notification attempt has no durable claim")
            state = OutboxState.DELIVERED if delivered else OutboxState.PENDING
            if not delivered and next_attempt_at is None:
                next_attempt_at = attempted_at
            receipt_fields = (
                delivery_route_id,
                delivery_assurance,
                delivery_receipt_hash,
                delivery_payload_hash,
            )
            if delivered and any(item is not None for item in receipt_fields):
                if not all(item is not None for item in receipt_fields):
                    raise ValueError("structured notification receipt fields are all required")
                for field, value in (
                    ("delivery_route_id", delivery_route_id),
                    ("delivery_receipt_hash", delivery_receipt_hash),
                    ("delivery_payload_hash", delivery_payload_hash),
                ):
                    if len(str(value)) != 64 or any(
                        item not in "0123456789abcdef" for item in str(value)
                    ):
                        raise ValueError(f"{field} must be lowercase SHA-256")
            connection.execute(
                """UPDATE notification_outbox SET
                       state = ?, attempt_count = attempt_count + 1,
                       last_attempt_at = ?, next_attempt_at = ?, delivered_at = ?,
                       last_error = ?, delivery_receipt = ?, claim_owner = NULL,
                       claim_expires_at = NULL, delivery_route_id = ?,
                       delivery_assurance = ?, delivery_receipt_hash = ?,
                       delivery_payload_hash = ?
                   WHERE message_id = ?""",
                (
                    state.value,
                    _iso(attempted_at),
                    None if delivered else _iso(next_attempt_at),
                    _iso(attempted_at) if delivered else None,
                    None if delivered else _safe_notification_error(error),
                    str(delivery_receipt) if delivered and delivery_receipt is not None else None,
                    delivery_route_id if delivered else None,
                    delivery_assurance if delivered else None,
                    delivery_receipt_hash if delivered else None,
                    delivery_payload_hash if delivered else None,
                    message_id,
                ),
            )
            self._append_event(
                connection,
                stream=row["account_key"],
                event_type=("NOTIFICATION_DELIVERED" if delivered else "NOTIFICATION_RETRY_PENDING"),
                entity_type="notification",
                entity_id=message_id,
                occurred_at=attempted_at,
                payload={
                    "error": None if delivered else _safe_notification_error(error),
                    "route_id": delivery_route_id if delivered else None,
                    "assurance": delivery_assurance if delivered else None,
                    "receipt_hash": delivery_receipt_hash if delivered else None,
                },
            )

    def claim_due_outbox(
        self,
        *,
        now: datetime,
        claim_owner: str,
        claim_expires_at: datetime,
        limit: int = 20,
    ) -> list[Mapping[str, Any]]:
        """Atomically lease due messages to one bounded notification worker."""

        owner = str(claim_owner).strip()
        if not owner or len(owner) > 128 or any(
            not (item.isalnum() or item in "._:-") for item in owner
        ):
            raise ValueError("notification claim owner is invalid")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("outbox limit must be in [1, 100]")
        if now.tzinfo is None or claim_expires_at.tzinfo is None:
            raise ValueError("notification claim times must be timezone-aware")
        if not now < claim_expires_at <= now + timedelta(minutes=5):
            raise ValueError("notification claim lease must be in (0, 5 minutes]")
        current = _iso(now)
        expires = _iso(claim_expires_at)
        with self.transaction() as connection:
            rows = connection.execute(
                """SELECT * FROM notification_outbox
                   WHERE state=? AND (next_attempt_at IS NULL OR next_attempt_at<=?)
                     AND (claim_owner IS NULL OR claim_expires_at<=?)
                   ORDER BY created_at,message_id LIMIT ?""",
                (OutboxState.PENDING.value, current, current, limit),
            ).fetchall()
            result: list[Mapping[str, Any]] = []
            for row in rows:
                updated = connection.execute(
                    """UPDATE notification_outbox
                          SET claim_owner=?,claim_expires_at=?
                        WHERE message_id=? AND state=?
                          AND (claim_owner IS NULL OR claim_expires_at<=?)""",
                    (
                        owner,
                        expires,
                        row["message_id"],
                        OutboxState.PENDING.value,
                        current,
                    ),
                ).rowcount
                if updated != 1:
                    continue
                claimed = dict(row)
                claimed["claim_owner"] = owner
                claimed["claim_expires_at"] = expires
                result.append(claimed)
                self._append_event(
                    connection,
                    stream=row["account_key"],
                    event_type="NOTIFICATION_CLAIMED",
                    entity_type="notification",
                    entity_id=row["message_id"],
                    occurred_at=now,
                    payload={"claim_owner": owner, "claim_expires_at": expires},
                )
            return result

    def acquire_notification_worker_lease(
        self,
        *,
        account_key: str,
        worker_id: str,
        route_id: str,
        provider: str,
        destination_fingerprint: str,
        route_version: str,
        acquired_at: datetime,
        process_id: int | None = None,
        recover_stale_after: timedelta = timedelta(seconds=60),
    ) -> int:
        """Acquire the independent delivery-worker singleton for one account."""

        owner = str(worker_id).strip()
        if not owner or len(owner) > 128 or any(
            not (item.isalnum() or item in "._:-") for item in owner
        ):
            raise ValueError("notification worker ID is invalid")
        pid = os.getpid() if process_id is None else process_id
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise ValueError("notification worker process ID is invalid")
        if not timedelta(seconds=1) <= recover_stale_after <= timedelta(minutes=5):
            raise ValueError("notification worker stale interval must be in [1, 300] seconds")
        route_values = (
            str(route_id),
            str(provider),
            str(destination_fingerprint),
            str(route_version),
        )
        if (
            any(
                len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
                for value in (route_values[0], route_values[2])
            )
            or any(not item for item in route_values)
        ):
            raise ValueError("notification worker route identity is invalid")
        when = _iso(acquired_at)
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM notification_worker_lease WHERE account_key=?",
                (account_key,),
            ).fetchone()
            generation = 1
            if row is not None:
                generation = int(row["generation"]) + 1
                active = row["released_at"] is None
                heartbeat = datetime.fromisoformat(str(row["heartbeat_at"])).astimezone(
                    timezone.utc
                )
                age = acquired_at.astimezone(timezone.utc) - heartbeat
                if active and age <= recover_stale_after:
                    raise StateConflict("notification worker lease is already held")
            connection.execute(
                """INSERT INTO notification_worker_lease(
                       account_key,worker_id,process_id,generation,route_id,provider,
                       destination_fingerprint,route_version,started_at,heartbeat_at,
                       last_sent_count,last_failed_count,released_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,0,0,NULL)
                   ON CONFLICT(account_key) DO UPDATE SET
                       worker_id=excluded.worker_id,process_id=excluded.process_id,
                       generation=excluded.generation,route_id=excluded.route_id,
                       provider=excluded.provider,
                       destination_fingerprint=excluded.destination_fingerprint,
                       route_version=excluded.route_version,started_at=excluded.started_at,
                       heartbeat_at=excluded.heartbeat_at,last_sent_count=0,
                       last_failed_count=0,released_at=NULL""",
                (account_key, owner, pid, generation, *route_values, when, when),
            )
            self._append_event(
                connection,
                stream=account_key,
                event_type="NOTIFICATION_WORKER_ACQUIRED",
                entity_type="notification_worker",
                entity_id=owner,
                occurred_at=acquired_at,
                payload={
                    "process_id": pid,
                    "generation": generation,
                    "route_id": route_values[0],
                    "provider": route_values[1],
                    "destination_fingerprint": route_values[2],
                    "route_version": route_values[3],
                },
            )
            return generation

    def heartbeat_notification_worker(
        self,
        *,
        account_key: str,
        worker_id: str,
        generation: int,
        observed_at: datetime,
        sent_count: int,
        failed_count: int,
        process_id: int | None = None,
    ) -> None:
        pid = os.getpid() if process_id is None else process_id
        if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
            raise ValueError("notification worker generation is invalid")
        for name, value in (("sent_count", sent_count), ("failed_count", failed_count)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"notification worker {name} is invalid")
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM notification_worker_lease WHERE account_key=?",
                (account_key,),
            ).fetchone()
            if (
                row is None
                or row["released_at"] is not None
                or row["worker_id"] != worker_id
                or int(row["process_id"]) != pid
                or int(row["generation"]) != generation
            ):
                raise StateConflict("notification worker heartbeat does not own its lease")
            when = _iso(observed_at)
            if when < row["heartbeat_at"]:
                raise OutOfOrderEvent("notification worker heartbeat decreased")
            connection.execute(
                """UPDATE notification_worker_lease SET heartbeat_at=?,
                       last_sent_count=?,last_failed_count=? WHERE account_key=?""",
                (when, sent_count, failed_count, account_key),
            )

    def release_notification_worker_lease(
        self,
        *,
        account_key: str,
        worker_id: str,
        generation: int,
        released_at: datetime,
        process_id: int | None = None,
    ) -> None:
        pid = os.getpid() if process_id is None else process_id
        if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
            raise ValueError("notification worker generation is invalid")
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM notification_worker_lease WHERE account_key=?",
                (account_key,),
            ).fetchone()
            if row is None or row["released_at"] is not None:
                return
            if (
                row["worker_id"] != worker_id
                or int(row["process_id"]) != pid
                or int(row["generation"]) != generation
            ):
                raise StateConflict("notification worker release does not own its lease")
            when = _iso(released_at)
            if when < row["heartbeat_at"]:
                raise OutOfOrderEvent("notification worker release predates heartbeat")
            connection.execute(
                """UPDATE notification_worker_lease SET heartbeat_at=?,released_at=?
                   WHERE account_key=?""",
                (when, when, account_key),
            )
            self._append_event(
                connection,
                stream=account_key,
                event_type="NOTIFICATION_WORKER_RELEASED",
                entity_type="notification_worker",
                entity_id=worker_id,
                occurred_at=released_at,
                payload={"process_id": pid, "generation": int(row["generation"])},
            )

    def due_outbox(self, *, now: datetime, limit: int = 20) -> list[Mapping[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("outbox limit must be in [1, 100]")
        return [
            dict(row)
            for row in self._conn.execute(
                """SELECT * FROM notification_outbox
                   WHERE state=? AND (next_attempt_at IS NULL OR next_attempt_at<=?)
                     AND (claim_owner IS NULL OR claim_expires_at<=?)
                   ORDER BY created_at,message_id LIMIT ?""",
                (OutboxState.PENDING.value, _iso(now), _iso(now), limit),
            ).fetchall()
        ]

    def record_latency(self, sample: LatencySample) -> bool:
        values = {
            "sample_id": sample.sample_id,
            "account_key": sample.account_key,
            "stage": sample.stage,
            "duration_microseconds": sample.duration_microseconds,
            "observed_at": _iso(sample.observed_at),
            "correlation_id": sample.correlation_id,
        }
        with self.transaction() as connection:
            inserted = self._insert_exact(
                connection,
                table="latency_samples",
                key_column="sample_id",
                values=values,
            )
            if inserted:
                self._append_event(
                    connection,
                    stream=sample.account_key,
                    event_type="LATENCY_RECORDED",
                    entity_type="latency_sample",
                    entity_id=sample.sample_id,
                    occurred_at=sample.observed_at,
                    payload={
                        "stage": sample.stage,
                        "duration_microseconds": sample.duration_microseconds,
                        "correlation_id": sample.correlation_id,
                    },
                )
            return inserted

    def row(self, table: str, key_column: str, key: Any) -> sqlite3.Row | None:
        allowed = {
            "broker_snapshots": "snapshot_id",
            "plans": "plan_id",
            "risk_reservations": "reservation_id",
            "order_intents": "intent_id",
            "broker_orders": "broker_order_id",
            "fills": "fill_id",
            "protection_obligations": "obligation_id",
            "incidents": "incident_id",
            "notification_outbox": "message_id",
            "notification_worker_lease": "account_key",
            "latency_samples": "sample_id",
        }
        if allowed.get(table) != key_column:
            raise ValueError("unsupported table/key lookup")
        return self._conn.execute(
            f"SELECT * FROM {table} WHERE {key_column} = ?", (key,)
        ).fetchone()

    def rows(self, sql: str, parameters: Sequence[Any] = ()) -> list[sqlite3.Row]:
        """Restricted read helper for diagnostics and tests."""

        if not sql.lstrip().upper().startswith("SELECT"):
            raise ValueError("rows accepts SELECT statements only")
        return list(self._conn.execute(sql, tuple(parameters)).fetchall())


__all__ = [
    "LiveStateError",
    "LiveStateStore",
    "OutOfOrderEvent",
    "SCHEMA_VERSION",
    "StateConflict",
    "UnsupportedSchema",
    "canonical_json",
    "object_hash",
]
