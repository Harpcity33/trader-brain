"""Private append-only evidence ledger for session calculation and recovery.

Persists the actual typed finite observations, including execution identifiers,
signed fees, original receipt times and limitations. It does not make finite
history exhaustive, turn local timestamps into broker timestamps, or authorize
trading. The risk-store pending marker must commit before collection; evidence
must commit here before completing that marker. A crash between those commits
therefore leaves an unresolved risk incident, never a successful missing read.

The unkeyed chain verifies local consistency, not hostile whole-file rollback
or broker authentication. Old observations are never replaced by a later replay.
"""

from __future__ import annotations

from dataclasses import fields
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from threading import RLock
from zoneinfo import ZoneInfo

from .broker.ibkr_session_inputs import (
    IbkrFiniteSessionFacts, SessionExecutionFact, SessionInputObservation,
    SessionOrderFact, SessionPositionFact,
)
from .session_trading_calculation import _validate_observation


SCHEMA = "titan_session_observation_ledger_2026-09-18_v1"
_APP_ID = 0x54534F4C
_ZERO = "0" * 64
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_MAX_BYTES = 16_777_216
_MAX_ROWS = 100_000
_NY = ZoneInfo("America/New_York")
_TYPES = {kind.__name__: kind for kind in (
    SessionInputObservation, IbkrFiniteSessionFacts, SessionExecutionFact,
    SessionPositionFact, SessionOrderFact,
)}
_DDL = {
    "scope": "CREATE TABLE scope (schema TEXT NOT NULL, account_binding TEXT NOT NULL, session_day TEXT NOT NULL)",
    "observations": "CREATE TABLE observations (sequence INTEGER PRIMARY KEY, collection_id TEXT NOT NULL UNIQUE, previous_hash TEXT NOT NULL, payload TEXT NOT NULL, receipt_hash TEXT NOT NULL UNIQUE)",
}


class SessionObservationLedgerError(ValueError):
    """Fixed-code error, without private payload or SQLite text."""


def _fail(code):
    raise SessionObservationLedgerError("SESSION_OBSERVATION_LEDGER_" + code) from None


def _canonical(value):
    result = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(result.encode("utf-8")) > _MAX_BYTES:
        _fail("RECORD_TOO_LARGE")
    return result


def _encode(value):
    kind = type(value)
    if kind.__name__ in _TYPES and _TYPES[kind.__name__] is kind:
        return {"record_type": kind.__name__, "fields": {field.name: _encode(getattr(value, field.name)) for field in fields(value)}}
    if kind is datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            _fail("TIME_INVALID")
        return {"utc": value.astimezone(timezone.utc).isoformat(timespec="microseconds")}
    if kind is Decimal:
        if not value.is_finite() or len(value.as_tuple().digits) > 30 or value.as_tuple().exponent < -12 or value.adjusted() > 18:
            _fail("AMOUNT_INVALID")
        return {"decimal": str(value)}
    if kind is tuple:
        return [_encode(item) for item in value]
    if value is None or kind in (str, int, bool):
        return value
    _fail("TYPE_INVALID")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("DUPLICATE_JSON_FIELD")
        result[key] = value
    return result


def _decode(value, depth=0):
    if depth > 12:
        _fail("RECORD_DEPTH_INVALID")
    if type(value) is list:
        return tuple(_decode(item, depth + 1) for item in value)
    if type(value) is dict:
        if set(value) == {"utc"} and type(value["utc"]) is str and len(value["utc"]) <= 40:
            result = datetime.fromisoformat(value["utc"])
            if _encode(result) != value:
                _fail("TIME_INVALID")
            return result
        if set(value) == {"decimal"} and type(value["decimal"]) is str and len(value["decimal"]) <= 64:
            result = Decimal(value["decimal"])
            if _encode(result) != value:
                _fail("AMOUNT_INVALID")
            return result
        if set(value) == {"record_type", "fields"} and type(value["record_type"]) is str:
            kind = _TYPES.get(value["record_type"])
            if kind is not None and type(value["fields"]) is dict and set(value["fields"]) == {field.name for field in fields(kind)}:
                return kind(**{key: _decode(item, depth + 1) for key, item in value["fields"].items()})
        _fail("RECORD_FIELDS_INVALID")
    if value is None or type(value) in (str, int, bool):
        return value
    _fail("TYPE_INVALID")


def _receipt(sequence, account, day, prior, payload):
    return hashlib.sha256(_canonical((SCHEMA, sequence, account, day, prior, payload)).encode()).hexdigest()


class SessionObservationLedger:
    """One account/day file, exclusive creation, strict append order and replay.

    Same-process appends use a verified in-memory tip. External SQLite writes,
    file metadata changes or same-connection changes invalidate that cache and
    trigger a full chain check. No disk checkpoint can skip original evidence.
    """

    def __init__(self, path: Path, *, account_binding_sha256: str, session_date: date, create=False):
        self._lock = RLock()
        self._db = None
        self._cache = None
        self._observations = []
        self._receipts = []
        try:
            # Filesystem validation itself can fail with a private path in the
            # exception; keep it inside the same fixed-code boundary as open.
            if (type(account_binding_sha256) is not str or _HASH.fullmatch(account_binding_sha256) is None
                    or type(session_date) is not date or type(create) is not bool
                    or not isinstance(path, Path) or not path.is_absolute() or path.is_symlink()
                    or path.parent.resolve(strict=True) != path.parent):
                _fail("SCOPE_OR_PATH_INVALID")
            self._path = path
            self._account = account_binding_sha256
            self._day = session_date.isoformat()
            if create:
                descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                os.close(descriptor)
            info = self._file_info()
            self._identity = (info.st_dev, info.st_ino)
            self._db = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, isolation_level=None, check_same_thread=False)
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA trusted_schema=OFF")
            if create:
                self._db.execute("BEGIN EXCLUSIVE")
                self._db.execute(f"PRAGMA application_id={_APP_ID}")
                self._db.execute("PRAGMA user_version=1")
                for sql in _DDL.values():
                    self._db.execute(sql)
                self._db.execute("INSERT INTO scope VALUES (?,?,?)", (SCHEMA, self._account, self._day))
                self._db.execute("COMMIT")
                directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            self.history()
        except BaseException as error:
            self.close()
            if isinstance(error, SessionObservationLedgerError) or not isinstance(error, Exception):
                raise
            _fail("OPEN_FAILED")

    def _file_info(self):
        info = self._path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            _fail("FILE_INVALID")
        return info

    def _signature(self):
        if self._db is None:
            _fail("CLOSED")
        info = self._file_info()
        if (info.st_dev, info.st_ino) != self._identity:
            _fail("FILE_CHANGED")
        return (info.st_size, info.st_mtime_ns, info.st_ctime_ns, self._db.total_changes,
                self._db.execute("PRAGMA data_version").fetchone()[0])

    def _verify(self):
        # Connection PRAGMAs need not change total_changes/data_version or file
        # metadata. Never let a cached tip bypass the commit-durability contract.
        if (self._db.execute("PRAGMA synchronous").fetchone() != (2,)
                or self._db.execute("PRAGMA journal_mode").fetchone() != ("delete",)):
            _fail("DURABILITY_INVALID")
        current = self._signature()
        if current == self._cache:
            return
        schema = dict(self._db.execute("SELECT name,sql FROM sqlite_master WHERE type='table'"))
        if (schema != _DDL or self._db.execute("PRAGMA application_id").fetchone() != (_APP_ID,)
                or self._db.execute("PRAGMA user_version").fetchone() != (1,)
                or self._db.execute("SELECT count(*) FROM sqlite_master WHERE type NOT IN ('table','index') OR (type='index' AND name NOT IN ('sqlite_autoindex_observations_1','sqlite_autoindex_observations_2'))").fetchone() != (0,)
                or self._db.execute("SELECT * FROM scope").fetchall() != [(SCHEMA, self._account, self._day)]
                or self._db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]):
            _fail("INTEGRITY_INVALID")
        observations, receipts = [], []
        prior = _ZERO
        for sequence, collection, previous, payload, receipt in self._db.execute("SELECT * FROM observations ORDER BY sequence"):
            if sequence != len(observations) or sequence >= _MAX_ROWS or previous != prior or type(payload) is not str or len(payload.encode()) > _MAX_BYTES:
                _fail("CHAIN_INVALID")
            value = _decode(json.loads(payload, object_pairs_hook=_pairs))
            if payload != _canonical(_encode(value)) or receipt != _receipt(sequence, self._account, self._day, prior, payload):
                _fail("CHAIN_INVALID")
            self._validate_next(value, observations[-1] if observations else None)
            if value.facts.collection_id != collection:
                _fail("COLLECTION_INVALID")
            observations.append(value)
            receipts.append(receipt)
            prior = receipt
        self._observations, self._receipts = observations, receipts
        self._cache = current

    def _validate_next(self, value, previous):
        _validate_observation(value)
        facts = value.facts
        if (value.account_binding_fingerprint != self._account
                or facts.collection_started_at.astimezone(_NY).date().isoformat() != self._day
                or facts.collection_completed_at.astimezone(_NY).date().isoformat() != self._day
                or facts.collection_started_at > facts.collection_completed_at):
            _fail("OBSERVATION_SCOPE_INVALID")
        if previous is None:
            if value.prior_collection_id is not None:
                _fail("INITIAL_LINEAGE_INVALID")
        elif (value.prior_collection_id != previous.facts.collection_id
                or facts.collection_started_at < previous.facts.collection_completed_at
                or value.read_client_id != previous.read_client_id
                or facts.generation != previous.facts.generation):
            _fail("OBSERVATION_LINEAGE_INVALID")

    def _operation(self, action, *, write=False):
        with self._lock:
            try:
                self._signature()
                # Even a cached read holds the writer lock while checking its
                # version; an external commit must not slip between verification
                # and the tip recorded for the next operation.
                self._db.execute("BEGIN IMMEDIATE")
                self._verify()
                result = action()
                verified_data_version = self._db.execute("PRAGMA data_version").fetchone()[0]
                self._db.execute("COMMIT")
                self._cache = (*self._signature()[:-1], verified_data_version)
                return result
            except BaseException as error:
                self._cache = None
                if self._db is not None and self._db.in_transaction:
                    try:
                        self._db.execute("ROLLBACK")
                    except sqlite3.Error:
                        self.close()
                        _fail("ROLLBACK_UNCONFIRMED")
                if isinstance(error, SessionObservationLedgerError) or not isinstance(error, Exception):
                    raise
                _fail("OPERATION_FAILED")

    def append(self, observation: SessionInputObservation, *, expected_previous_receipt: str) -> str:
        """Return the committed receipt; stale writers cannot fork the ledger."""
        def action():
            prior = self._receipts[-1] if self._receipts else _ZERO
            if expected_previous_receipt != prior:
                _fail("STALE_WRITER")
            if len(self._observations) >= _MAX_ROWS:
                _fail("CAPACITY_EXCEEDED")
            self._validate_next(observation, self._observations[-1] if self._observations else None)
            payload = _canonical(_encode(observation))
            sequence = len(self._observations)
            receipt = _receipt(sequence, self._account, self._day, prior, payload)
            self._db.execute("INSERT INTO observations VALUES (?,?,?,?,?)", (sequence, observation.facts.collection_id, prior, payload, receipt))
            self._observations.append(observation)
            self._receipts.append(receipt)
            return receipt
        return self._operation(action, write=True)

    @property
    def head_receipt(self):
        return self._operation(lambda: self._receipts[-1] if self._receipts else _ZERO)

    def history(self):
        return self._operation(lambda: tuple(self._observations))

    def close(self):
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None
            self._cache = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
