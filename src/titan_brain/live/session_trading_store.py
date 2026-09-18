"""Durable, non-authorizing storage for the separate session-trading contract.

Account binding + New York day, NOT release or policy, is the unique session
key. The first baseline is immutable. Every mutation requires the current
revision and appends an evidence event in the same SQLite transaction. Loading
verifies the unkeyed hash chain and replays the pure policy transitions on open
or external change. A process-local verified tip permits incremental validation
of subsequent appends. Disk snapshots are NEVER trusted without replay. The v2
risk projection retains all pending/failed incidents and the last completed
observation; all completed history and used tokens remain in the audit/index.
No pickle or arbitrary state-save API exists. Call begin_observation and wait
for its committed return BEFORE a read. V1 databases are rejected, not reset.

The chain detects inconsistent/corrupt changes, not an attacker rewriting the
whole database, restoration of an older valid database, or false input claims.
This is ordinary local durability, NOT HMAC custody, source authentication,
broker evidence verification, or live order authority. An external verifier and
rollback anchor remain separate requirements. Construction never opens a broker
connection; physical database creation must be explicitly requested.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from threading import RLock
from zoneinfo import ZoneInfo

from .session_trading_policy import (
    STATE_SCHEMA, ObservationIncident, ObservationStatus, SessionTradingBaseline,
    SessionTradingMeasurement, SessionTradingPolicy, SessionTradingPolicyError,
    SessionTradingState, begin_observation as policy_begin,
    complete_observation as policy_complete, fail_observation as policy_fail,
    start_session as policy_start,
)


STORE_SCHEMA = "titan_session_trading_sqlite_2026-09-18_v2"
AUDIT_SCHEMA = "titan_session_trading_audit_2026-09-18_v2"
_APPLICATION_ID = 0x54535452
_NY = ZoneInfo("America/New_York")
_ZERO_HASH = "0" * 64
_HASH = re.compile(r"[a-f0-9]{64}\Z", re.ASCII)
_MAX_JSON = 4_194_304
_MAX_EVENTS = 100_001
# Failed or abandoned reads already block entry. Bound this exceptional working
# set without dropping any incident; existing pending reads can still finish.
_MAX_UNRESOLVED = 512
_BASELINE_FIELDS = {
    "policy_sha256", "account_binding_sha256", "evidence_sha256", "frozen_at",
    "starting_nlv", "flat_start", "pre_entry", "initial_exposure_reconciled",
}
_MEASUREMENT_FIELDS = {
    "model", "account_binding_sha256", "baseline_identity_sha256", "evidence_sha256",
    "as_of", "received_at", "session_pnl", "complete",
}
_STATE_FIELDS = {
    "schema_version", "baseline", "incidents", "last_measurement",
    "last_observation_token", "loss_latched", "profit_aspiration_observed",
}
_DDL = {
    "metadata": "CREATE TABLE metadata (schema_version TEXT NOT NULL, version INTEGER NOT NULL)",
    "sessions": """CREATE TABLE sessions (
        account_binding_sha256 TEXT NOT NULL, session_date TEXT NOT NULL,
        baseline_identity_sha256 TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 0),
        state_json TEXT NOT NULL, state_sha256 TEXT NOT NULL, audit_head_sha256 TEXT NOT NULL,
        PRIMARY KEY(account_binding_sha256, session_date))""",
    "audit": """CREATE TABLE audit (
        account_binding_sha256 TEXT NOT NULL, session_date TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision >= 0), event_json TEXT NOT NULL,
        previous_sha256 TEXT NOT NULL, event_sha256 TEXT NOT NULL, state_sha256 TEXT NOT NULL,
        PRIMARY KEY(account_binding_sha256, session_date, revision),
        FOREIGN KEY(account_binding_sha256, session_date)
            REFERENCES sessions(account_binding_sha256, session_date))""",
    "observation_tokens": """CREATE TABLE observation_tokens (
        account_binding_sha256 TEXT NOT NULL, session_date TEXT NOT NULL,
        token TEXT NOT NULL, begin_revision INTEGER NOT NULL,
        PRIMARY KEY(account_binding_sha256, session_date, token),
        FOREIGN KEY(account_binding_sha256, session_date, begin_revision)
            REFERENCES audit(account_binding_sha256, session_date, revision))""",
}


class SessionTradingStoreError(RuntimeError):
    """Fixed-code error; never includes financial values or raw SQLite errors."""


class SessionTradingStoreConflict(SessionTradingStoreError):
    """Stale revision or attempted replacement of an existing day baseline."""


def _fail(code: str) -> None:
    raise SessionTradingStoreError("SESSION_TRADING_STORE_" + code) from None


def _hash(value: object) -> None:
    if type(value) is not str or _HASH.fullmatch(value) is None:
        _fail("HASH_INVALID")


def _scope(account: str, day: date) -> tuple[str, str]:
    _hash(account)
    if type(day) is not date:
        _fail("DATE_INVALID")
    return account, day.isoformat()


def _time_text(value: datetime) -> str:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        _fail("TIME_INVALID")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _time(value: object) -> datetime:
    if type(value) is not str or len(value) > 40:
        _fail("STATE_TIME_INVALID")
    try:
        result = datetime.fromisoformat(value)
        if _time_text(result) != value:
            _fail("STATE_TIME_INVALID")
        return result
    except ValueError:
        _fail("STATE_TIME_INVALID")


def _decimal_text(value: Decimal) -> str:
    if type(value) is not Decimal or not value.is_finite():
        _fail("DECIMAL_INVALID")
    if value == 0:
        return "0"
    result = format(value, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def _decimal(value: object) -> Decimal:
    if type(value) is not str or not 1 <= len(value) <= 64:
        _fail("STATE_DECIMAL_INVALID")
    try:
        result = Decimal(value)
        if _decimal_text(result) != value:
            _fail("STATE_DECIMAL_INVALID")
        return result
    except InvalidOperation:
        _fail("STATE_DECIMAL_INVALID")


def _mapping(value: object, fields: set[str]) -> dict:
    if type(value) is not dict or set(value) != fields:
        _fail("STATE_FIELDS_INVALID")
    return value


def _canonical(value: object) -> str:
    result = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    if len(result.encode("utf-8")) > _MAX_JSON:
        _fail("STATE_TOO_LARGE")
    return result


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _json(value: object) -> object:
    if type(value) is not str or len(value.encode("utf-8")) > _MAX_JSON:
        _fail("STATE_JSON_INVALID")
    result = json.loads(value, object_pairs_hook=_pairs)
    if _canonical(result) != value:
        _fail("STATE_JSON_NOT_CANONICAL")
    return result


def _baseline_record(value: SessionTradingBaseline) -> dict:
    if type(value) is not SessionTradingBaseline:
        _fail("BASELINE_TYPE_INVALID")
    return {**vars(value), "frozen_at": _time_text(value.frozen_at), "starting_nlv": _decimal_text(value.starting_nlv)}


def _baseline(value: object) -> SessionTradingBaseline:
    raw = _mapping(value, _BASELINE_FIELDS)
    return SessionTradingBaseline(**{**raw, "frozen_at": _time(raw["frozen_at"]), "starting_nlv": _decimal(raw["starting_nlv"])})


def _measurement_record(value: SessionTradingMeasurement | None) -> dict | None:
    if value is None:
        return None
    if type(value) is not SessionTradingMeasurement:
        _fail("MEASUREMENT_TYPE_INVALID")
    return {**vars(value), "as_of": _time_text(value.as_of), "received_at": _time_text(value.received_at),
            "session_pnl": None if value.session_pnl is None else _decimal_text(value.session_pnl)}


def _measurement(value: object) -> SessionTradingMeasurement | None:
    if value is None:
        return None
    raw = _mapping(value, _MEASUREMENT_FIELDS)
    return SessionTradingMeasurement(**{**raw, "as_of": _time(raw["as_of"]), "received_at": _time(raw["received_at"]),
        "session_pnl": None if raw["session_pnl"] is None else _decimal(raw["session_pnl"])})


def _state_record(value: SessionTradingState) -> dict:
    return {
        "schema_version": STATE_SCHEMA, "baseline": _baseline_record(value.baseline),
        "incidents": [{"token": item.token, "started_at": _time_text(item.started_at),
                       "status": item.status.value, "reason": item.reason} for item in value.incidents],
        "last_measurement": _measurement_record(value.last_measurement),
        "last_observation_token": value.last_observation_token,
        "loss_latched": value.loss_latched, "profit_aspiration_observed": value.profit_aspiration_observed,
    }


def _state(value: object) -> SessionTradingState:
    raw = _mapping(value, _STATE_FIELDS)
    if raw["schema_version"] != STATE_SCHEMA or type(raw["incidents"]) is not list or len(raw["incidents"]) > _MAX_EVENTS:
        _fail("STATE_SCHEMA_INVALID")
    incidents = []
    for row in raw["incidents"]:
        item = _mapping(row, {"token", "started_at", "status", "reason"})
        incidents.append(ObservationIncident(item["token"], _time(item["started_at"]), ObservationStatus(item["status"]), item["reason"]))
    return SessionTradingState(
        baseline=_baseline(raw["baseline"]), incidents=tuple(incidents),
        last_measurement=_measurement(raw["last_measurement"]), last_observation_token=raw["last_observation_token"],
        loss_latched=raw["loss_latched"], profit_aspiration_observed=raw["profit_aspiration_observed"],
    )


def _apply(state: SessionTradingState | None, event: object) -> SessionTradingState:
    raw = _mapping(event, {"operation", "at", "payload"})
    now = _time(raw["at"])
    operation, payload = raw["operation"], raw["payload"]
    if operation == "start":
        if state is not None:
            _fail("AUDIT_RESTART_INVALID")
        payload = _mapping(payload, {"policy", "baseline"})
        policy = SessionTradingPolicy(**_mapping(payload["policy"], {"policy_sha256", "amendment_path", "amendment_sha256"}))
        baseline = _baseline(payload["baseline"])
        if now != baseline.frozen_at:
            _fail("AUDIT_START_TIME_INVALID")
        return policy_start(policy, baseline)
    if state is None:
        _fail("AUDIT_START_MISSING")
    if operation == "begin":
        payload = _mapping(payload, {"token"})
        return policy_begin(state, token=payload["token"], now=now)
    if operation == "complete":
        payload = _mapping(payload, {"token", "measurement"})
        return policy_complete(state, token=payload["token"], measurement=_measurement(payload["measurement"]), now=now)
    if operation == "fail":
        payload = _mapping(payload, {"token", "gap"})
        return policy_fail(state, token=payload["token"], gap=payload["gap"])
    _fail("AUDIT_OPERATION_INVALID")


def _project(state: SessionTradingState) -> SessionTradingState:
    """Keep every risk-bearing incident, not an unbounded completed-read list.

    The token index and full journal, not this projection, enforce historical
    token uniqueness. Sticky flags survive even if their triggering successful
    observation is archived. Replay validates each transition BEFORE projecting.
    """
    return replace(state, incidents=tuple(
        item for item in state.incidents
        if item.status is not ObservationStatus.COMPLETED
        or item.token == state.last_observation_token
    ))


def _event_hash(scope: tuple[str, str], revision: int, previous: str, event: object, state_hash: str) -> str:
    return _digest(_canonical({
        "schema_version": AUDIT_SCHEMA, "account_binding_sha256": scope[0], "session_date": scope[1],
        "revision": revision, "previous_sha256": previous, "event": event, "state_sha256": state_hash,
    }))


@dataclass(frozen=True)
class StoredSession:
    """Current risk projection; completed observation history lives in audit."""
    state: SessionTradingState = field(repr=False)
    revision: int
    audit_head_sha256: str

    @property
    def live_authority(self) -> bool:
        return False

    @property
    def session_date(self) -> date:
        return self.state.baseline.frozen_at.astimezone(_NY).date()

    def public_dict(self) -> dict[str, object]:
        return {
            "schema_version": STORE_SCHEMA, "session_date": self.session_date.isoformat(),
            "revision": self.revision, "audit_head_sha256": self.audit_head_sha256,
            "baseline_identity_sha256": self.state.baseline.identity_sha256,
            "loss_latched": self.state.loss_latched,
            "pending_incident_count": sum(item.status is ObservationStatus.PENDING for item in self.state.incidents),
            "failed_incident_count": sum(item.status is ObservationStatus.FAILED for item in self.state.incidents),
            "live_authority": False, "source_authentication_established": False,
            "rollback_protection_established": False,
        }


@dataclass(frozen=True)
class _VerifiedTip:
    stored: StoredSession
    row: tuple
    audit_row: tuple


class SessionTradingStore:
    """Private SQLite file; CAS writes and FULL-synchronous commits.

    Keep one long-lived owner/connection for the sampling cadence; opening a
    new store or alternating external writers intentionally incurs cold replay.
    Healthy-session append work is bounded: a compact risk projection, indexed
    token lookup and one hash-chain append. Cold/external-change replay is linear
    in events (bounded by 100,001/account/day), with at most 512 unresolved reads
    in the projection. A verified in-memory tip is invalidated by external
    SQLite commits, file metadata changes, or unexpected private-connection
    writes. Each transaction still checks its materialized tip and latest audit
    row. The SQLite connection is private; no raw SQL mutation API is exposed.

    These checks detect ordinary corruption, not malicious whole-file rollback
    or an attacker controlling this process/SQLite. They establish no source
    authentication or trading authority. V1 stores require explicit, separately
    reviewed migration; opening one never silently resets the session.
    """

    def __init__(self, path: Path, *, create: bool = False) -> None:
        self._lock = RLock()
        self._closed = True
        self._cache: dict[tuple[str, str], _VerifiedTip] = {}
        self._verified_epoch = None
        self._integrity_checked = False
        if (not isinstance(path, Path) or not path.is_absolute() or type(create) is not bool
                or path.parent.resolve() != path.parent or path.is_symlink()):
            _fail("PATH_INVALID")
        self._path = path
        try:
            if create:
                descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                os.close(descriptor)
            info = self._file_info()
            self._identity = (info.st_dev, info.st_ino)
            self._db = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, isolation_level=None, check_same_thread=False)
            self._closed = False
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA trusted_schema=OFF")
            if create:
                self._db.execute("BEGIN EXCLUSIVE")
                try:
                    self._db.execute(f"PRAGMA application_id={_APPLICATION_ID}")
                    self._db.execute("PRAGMA user_version=2")
                    for sql in _DDL.values():
                        self._db.execute(sql)
                    self._db.execute("INSERT INTO metadata VALUES (?,2)", (STORE_SCHEMA,))
                    self._db.execute("COMMIT")
                    self._fsync_parent()
                except BaseException:
                    if self._db.in_transaction:
                        self._db.execute("ROLLBACK")
                    raise
            with self._transaction(write=False):
                self._verify_schema()
                for account, day in self._db.execute("SELECT account_binding_sha256,session_date FROM sessions").fetchall():
                    if type(day) is not str or date.fromisoformat(day).isoformat() != day:
                        _fail("CORRUPT")
                    self._load_verified(_scope(account, date.fromisoformat(day)))
                if self._db.execute("PRAGMA foreign_key_check").fetchall():
                    _fail("CORRUPT")
        except SessionTradingStoreError:
            self.close()
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError):
            self.close()
            _fail("OPEN_FAILED")
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> SessionTradingStore:
        if self._closed:
            _fail("CLOSED")
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                try:
                    self._db.close()
                except sqlite3.Error:
                    _fail("CLOSE_FAILED")
                finally:
                    self._closed = True

    def _fsync_parent(self) -> None:
        # SQLite FULL commits synchronize database contents; explicitly persist
        # the first-created filename as well before claiming successful create.
        descriptor = os.open(self._path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _file_info(self):
        info = self._path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            _fail("FILE_INVALID")
        return info

    @contextmanager
    def _transaction(self, *, write: bool):
        with self._lock:
            if self._closed:
                _fail("CLOSED")
            try:
                info = self._file_info()
                if (info.st_dev, info.st_ino) != self._identity:
                    _fail("FILE_CHANGED")
                self._db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                # Establish a read snapshot/lock BEFORE checking data_version.
                # DELETE journal mode prevents an external writer changing the
                # file during this transaction. Own commits do not increment
                # data_version; total_changes detects unexpected own-handle SQL.
                self._db.execute("SELECT schema_version,version FROM metadata").fetchall()
                data_version = self._db.execute("PRAGMA data_version").fetchone()[0]
                info = self._file_info()
                epoch = (data_version, self._db.total_changes, info.st_mtime_ns, info.st_ctime_ns, info.st_size)
                if epoch != self._verified_epoch:
                    self._cache.clear()
                    self._integrity_checked = False
                yield
                self._db.execute("COMMIT")
                info = self._file_info()
                # Retain the transaction's data_version, not a fresh post-commit
                # value that could inadvertently bless another writer's commit.
                self._verified_epoch = (data_version, self._db.total_changes,
                                        info.st_mtime_ns, info.st_ctime_ns, info.st_size)
            except BaseException as exc:
                self._cache.clear()
                self._verified_epoch = None
                self._integrity_checked = False
                rollback_failed = False
                try:
                    if self._db.in_transaction:
                        self._db.execute("ROLLBACK")
                except sqlite3.Error:
                    rollback_failed = True
                    try:
                        self._db.close()
                    except sqlite3.Error:
                        pass
                    self._closed = True
                if not isinstance(exc, Exception):
                    raise
                if rollback_failed:
                    _fail("ROLLBACK_UNCONFIRMED")
                if isinstance(exc, SessionTradingStoreError):
                    raise
                if isinstance(exc, SessionTradingPolicyError):
                    _fail("TRANSITION_REJECTED")
                _fail("TRANSACTION_FAILED")

    def _verify_schema(self) -> None:
        if (self._db.execute("PRAGMA application_id").fetchone() != (_APPLICATION_ID,)
                or self._db.execute("PRAGMA user_version").fetchone() != (2,)
                or self._db.execute("PRAGMA journal_mode").fetchone() != ("delete",)
                or self._db.execute("PRAGMA synchronous").fetchone() != (2,)
                or self._db.execute("PRAGMA foreign_keys").fetchone() != (1,)
                or self._db.execute("SELECT schema_version,version FROM metadata").fetchall() != [(STORE_SCHEMA, 2)]):
            _fail("IDENTITY_INVALID")
        actual = self._db.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
        if {(kind, name, sql) for kind, name, sql in actual} != {("table", name, sql) for name, sql in _DDL.items()}:
            _fail("IDENTITY_INVALID")
        if not self._integrity_checked:
            if (self._db.execute("PRAGMA quick_check").fetchall() != [("ok",)]
                    or self._db.execute("PRAGMA foreign_key_check").fetchall()):
                _fail("CORRUPT")
            self._integrity_checked = True

    def _load_verified(self, scope: tuple[str, str]) -> StoredSession | None:
        try:
            row = self._db.execute(
                "SELECT baseline_identity_sha256,revision,state_json,state_sha256,audit_head_sha256 FROM sessions "
                "WHERE account_binding_sha256=? AND session_date=?", scope).fetchone()
            cached = self._cache.get(scope)
            if cached is not None:
                latest = self._db.execute(
                    "SELECT revision,event_json,previous_sha256,event_sha256,state_sha256 FROM audit "
                    "WHERE account_binding_sha256=? AND session_date=? ORDER BY revision DESC LIMIT 1", scope).fetchone()
                if row != cached.row or latest != cached.audit_row:
                    _fail("CORRUPT")
                return cached.stored
            events = self._db.execute(
                "SELECT revision,event_json,previous_sha256,event_sha256,state_sha256 FROM audit "
                "WHERE account_binding_sha256=? AND session_date=? ORDER BY revision LIMIT ?", (*scope, _MAX_EVENTS + 1))
            if row is None:
                if events.fetchone() is not None:
                    _fail("CORRUPT")
                return None
            if type(row[1]) is not int or not 0 <= row[1] < _MAX_EVENTS:
                _fail("CORRUPT")
            previous, replayed, prior_time = _ZERO_HASH, None, None
            tokens, latest = {}, None
            for index, (revision, event_json, prior_hash, event_hash, state_hash) in enumerate(events):
                if type(revision) is not int or revision != index or index >= _MAX_EVENTS or prior_hash != previous:
                    _fail("CORRUPT")
                for value in (prior_hash, event_hash, state_hash):
                    _hash(value)
                event = _json(event_json)
                at = _time(_mapping(event, {"operation", "at", "payload"})["at"])
                if prior_time is not None and at < prior_time:
                    _fail("CORRUPT")
                if _event_hash(scope, revision, prior_hash, event, state_hash) != event_hash:
                    _fail("CORRUPT")
                if event["operation"] == "begin":
                    token = event["payload"]["token"]
                    pending = sum(item.status is ObservationStatus.PENDING for item in replayed.incidents)
                    if (token in tokens or revision + 1 + pending >= _MAX_EVENTS
                            or sum(item.status is not ObservationStatus.COMPLETED
                                   for item in replayed.incidents) >= _MAX_UNRESOLVED):
                        _fail("CORRUPT")
                    tokens[token] = revision
                replayed = _project(_apply(replayed, event))
                if _digest(_canonical(_state_record(replayed))) != state_hash:
                    _fail("CORRUPT")
                previous, prior_time = event_hash, at
                latest = (revision, event_json, prior_hash, event_hash, state_hash)
            if latest is None or latest[0] != row[1]:
                _fail("CORRUPT")
            indexed = self._db.execute(
                "SELECT token,begin_revision FROM observation_tokens WHERE account_binding_sha256=? AND session_date=?", scope).fetchall()
            if len(indexed) != len(tokens) or dict(indexed) != tokens:
                _fail("CORRUPT")
            materialized = _state(_json(row[2]))
            if (materialized != replayed or _canonical(_state_record(materialized)) != row[2]
                    or _digest(row[2]) != row[3] or row[3] != latest[4] or row[4] != previous
                    or materialized.baseline.identity_sha256 != row[0]
                    or _scope(materialized.baseline.account_binding_sha256, materialized.baseline.frozen_at.astimezone(_NY).date()) != scope):
                _fail("CORRUPT")
            stored = StoredSession(materialized, row[1], row[4])
            self._cache[scope] = _VerifiedTip(stored, row, latest)
            return stored
        except (ValueError, TypeError, KeyError, AttributeError, SessionTradingPolicyError):
            _fail("CORRUPT")

    def load(self, *, account_binding_sha256: str, session_date: date) -> StoredSession | None:
        scope = _scope(account_binding_sha256, session_date)
        with self._transaction(write=False):
            self._verify_schema()
            return self._load_verified(scope)

    def _insert_audit(self, scope: tuple[str, str], revision: int, event_json: str,
                      previous: str, event_hash: str, state_hash: str) -> None:
        self._db.execute("INSERT INTO audit VALUES (?,?,?,?,?,?,?)",
                         (*scope, revision, event_json, previous, event_hash, state_hash))

    def _write(self, scope: tuple[str, str], previous: StoredSession | None, event: dict) -> StoredSession:
        revision = 0 if previous is None else previous.revision + 1
        if revision >= _MAX_EVENTS:
            _fail("EVENT_LIMIT_REACHED")
        event_json = _canonical(event)
        if previous is not None:
            last = self._db.execute("SELECT event_json FROM audit WHERE account_binding_sha256=? AND session_date=? AND revision=?",
                                    (*scope, previous.revision)).fetchone()
            if _time(event["at"]) < _time(_json(last[0])["at"]):
                raise SessionTradingStoreConflict("SESSION_TRADING_STORE_TIME_REGRESSION")
        if event["operation"] == "begin":
            found = self._db.execute(
                "SELECT 1 FROM observation_tokens WHERE account_binding_sha256=? AND session_date=? AND token=?",
                (*scope, event["payload"]["token"])).fetchone()
            if found is not None:
                _fail("TOKEN_REUSED")
        state = _project(_apply(None if previous is None else previous.state, _json(event_json)))
        state_json = _canonical(_state_record(state))
        state_hash = _digest(state_json)
        old_head = _ZERO_HASH if previous is None else previous.audit_head_sha256
        event_hash = _event_hash(scope, revision, old_head, event, state_hash)
        if previous is None:
            self._db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)",
                             (*scope, state.baseline.identity_sha256, revision, state_json, state_hash, event_hash))
        else:
            if state.baseline != previous.state.baseline:
                _fail("BASELINE_CHANGED")
            changed = self._db.execute(
                "UPDATE sessions SET revision=?,state_json=?,state_sha256=?,audit_head_sha256=? "
                "WHERE account_binding_sha256=? AND session_date=? AND revision=?",
                (revision, state_json, state_hash, event_hash, *scope, previous.revision)).rowcount
            if changed != 1:
                raise SessionTradingStoreConflict("SESSION_TRADING_STORE_STALE_REVISION")
        self._insert_audit(scope, revision, event_json, old_head, event_hash, state_hash)
        if event["operation"] == "begin":
            self._db.execute("INSERT INTO observation_tokens VALUES (?,?,?,?)",
                             (*scope, event["payload"]["token"], revision))
        stored = StoredSession(state, revision, event_hash)
        self._cache[scope] = _VerifiedTip(
            stored, (state.baseline.identity_sha256, revision, state_json, state_hash, event_hash),
            (revision, event_json, old_head, event_hash, state_hash))
        return stored

    def start_session(self, policy: SessionTradingPolicy, baseline: SessionTradingBaseline) -> StoredSession:
        if type(policy) is not SessionTradingPolicy or type(baseline) is not SessionTradingBaseline:
            _fail("START_TYPE_INVALID")
        scope = _scope(baseline.account_binding_sha256, baseline.frozen_at.astimezone(_NY).date())
        event = {"operation": "start", "at": _time_text(baseline.frozen_at),
                 "payload": {"policy": vars(policy), "baseline": _baseline_record(baseline)}}
        with self._transaction(write=True):
            self._verify_schema()
            current = self._load_verified(scope)
            # Validate supplied policy even on an idempotent retry. A changed
            # policy/release never creates a second namespace for the same day.
            proposed = _apply(None, event)
            if current is not None:
                original_event = self._db.execute(
                    "SELECT event_json FROM audit WHERE account_binding_sha256=? AND session_date=? AND revision=0", scope).fetchone()
                original_policy = _json(original_event[0])["payload"]["policy"]
                if current.state.baseline != proposed.baseline or original_policy != vars(policy):
                    raise SessionTradingStoreConflict("SESSION_TRADING_STORE_BASELINE_IMMUTABLE")
                return current
            return self._write(scope, None, event)

    def _mutate(self, *, account_binding_sha256: str, session_date: date,
                expected_revision: int, operation: str, payload: dict, now: datetime) -> StoredSession:
        scope = _scope(account_binding_sha256, session_date)
        if type(expected_revision) is not int or not 0 <= expected_revision < _MAX_EVENTS:
            _fail("REVISION_INVALID")
        event = {"operation": operation, "at": _time_text(now), "payload": payload}
        with self._transaction(write=True):
            self._verify_schema()
            current = self._load_verified(scope)
            if current is None:
                _fail("SESSION_MISSING")
            if current.revision != expected_revision:
                raise SessionTradingStoreConflict("SESSION_TRADING_STORE_STALE_REVISION")
            if operation == "begin":
                # Reserve a completion/failure event for EVERY existing pending
                # read plus this new one; never begin a read which the known
                # journal bound would prevent us from closing transactionally.
                pending = sum(item.status is ObservationStatus.PENDING for item in current.state.incidents)
                if current.revision + 2 + pending >= _MAX_EVENTS:
                    _fail("EVENT_LIMIT_REACHED")
                if sum(item.status is not ObservationStatus.COMPLETED
                       for item in current.state.incidents) >= _MAX_UNRESOLVED:
                    _fail("UNRESOLVED_LIMIT_REACHED")
            return self._write(scope, current, event)

    def begin_observation(self, *, account_binding_sha256: str, session_date: date,
                          expected_revision: int, token: str, now: datetime) -> StoredSession:
        """Return only after the pending marker and audit event are committed."""
        return self._mutate(account_binding_sha256=account_binding_sha256, session_date=session_date,
                            expected_revision=expected_revision, operation="begin", payload={"token": token}, now=now)

    def complete_observation(self, *, account_binding_sha256: str, session_date: date,
                             expected_revision: int, token: str, measurement: SessionTradingMeasurement | None,
                             now: datetime) -> StoredSession:
        # A missing/incorrect object is never serialized or treated as zero;
        # persist the policy's permanent INCOMPLETE incident instead.
        raw = _measurement_record(measurement) if type(measurement) is SessionTradingMeasurement else None
        return self._mutate(account_binding_sha256=account_binding_sha256, session_date=session_date,
                            expected_revision=expected_revision, operation="complete",
                            payload={"token": token, "measurement": raw}, now=now)

    def fail_observation(self, *, account_binding_sha256: str, session_date: date,
                         expected_revision: int, token: str, now: datetime, gap: bool = False) -> StoredSession:
        return self._mutate(account_binding_sha256=account_binding_sha256, session_date=session_date,
                            expected_revision=expected_revision, operation="fail", payload={"token": token, "gap": gap}, now=now)
