"""Isolated, fail-closed IBKR submission journal; no SDK or broker operations.

An intent is committed before transmission, and ``mark_sending`` is a one-shot
durable claim. A crash between that claim and the socket write is deliberately
ambiguous: neither a restart nor an absent lookup result grants retry authority.
This journal is not a whole-account reconciliation or trading-readiness proof.

Only an opaque SHA-256 account binding is accepted. Callers must independently
verify the actual connected account, live/paper environment, and API client ID.
The ledger cannot authenticate those caller-supplied facts. Raw account IDs,
requests, broker error messages, credentials, and broker API objects are never
accepted or persisted here.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from threading import RLock
from typing import Any, Iterator, Mapping
from uuid import UUID


_HASH = re.compile(r"^[a-f0-9]{64}$")
_TOKEN = re.compile(r"^[A-Za-z0-9_.:\-]{1,160}$")
_MAX_ORDER_ID = 2_147_483_647
_APPLICATION_ID = 0x5449424B  # TIBK, distinct from Titan's other databases.
_SCHEMA_VERSION = 1
_KINDS = frozenset(
    {
        "ACK",
        "REJECT",
        "UNKNOWN",
        "ABORTED",
        "SUBMIT_NOT_SENT",
        "PENDING_CANCEL",
        "CANCEL_NOT_SENT",
        "CANCEL_UNKNOWN",
        "CANCEL_RETRY_AUTHORIZED",
        "CANCELLED",
    }
)


class IbkrLedgerError(RuntimeError):
    """Sanitized ledger contract failure, never a broker retry instruction."""


class IbkrLedgerConflict(IbkrLedgerError):
    """An existing identity or immutable fact does not match the new fact."""


class IbkrLedgerTransitionError(IbkrLedgerError):
    """The requested state change could repeat a transmission."""


def _uuid(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a UUID string")
    try:
        normalized = str(UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{label} must be a UUID string") from None
    if value != normalized:
        raise ValueError(f"{label} must be a canonical UUID string")
    return normalized


def _hash(value: str, label: str) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA-256 fingerprint")
    return value


def _token(value: str, label: str) -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise ValueError(f"{label} must be an opaque bounded identifier")
    return value


def _integer(value: int, label: str, *, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0) or value > _MAX_ORDER_ID:
        raise ValueError(f"{label} is outside the supported integer range")
    return value


def _perm_id(value: int | None) -> int | None:
    # Permanent IDs are not constrained to the API's signed orderId range.
    if value is not None and (type(value) is not int or not 0 < value < 2**63):
        raise ValueError("perm_id must be a positive SQLite-safe integer")
    return value


def _decimal(value: Decimal | str | int, label: str, *, positive: bool = False) -> str:
    if isinstance(value, (float, bool)) or not isinstance(value, (Decimal, str, int)):
        raise ValueError(f"{label} must be an exact finite decimal")
    try:
        normalized = Decimal(value)
    except (InvalidOperation, ValueError):
        raise ValueError(f"{label} must be an exact finite decimal") from None
    if not normalized.is_finite() or (positive and normalized <= 0):
        raise ValueError(f"{label} is outside its supported range")
    if len(normalized.as_tuple().digits) > 40 or abs(normalized.as_tuple().exponent) > 40:
        raise ValueError(f"{label} exceeds supported precision")
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if normalized == 0 else text


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("executed_at must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def request_fingerprint(payload: Mapping[str, Any]) -> str:
    """Hash a strict JSON tuple; callers encode monetary values as decimal strings.

    No floats, coercions, repr(), or SDK objects are admitted. The payload is not
    retained. Account/environment/contract/side/prices/session attributes that
    change order meaning must all be supplied by the caller's canonical codec.
    """
    def validate(value: Any) -> None:
        if value is None or type(value) in (str, bool, int):
            return
        if type(value) is list:
            for item in value:
                validate(item)
            return
        if type(value) is dict and all(type(key) is str for key in value):
            for item in value.values():
                validate(item)
            return
        raise ValueError("request fingerprint requires strict JSON without floats")

    if type(payload) is not dict:
        raise ValueError("request fingerprint requires a JSON object")
    validate(payload)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(b"titan-ibkr-request-v1\x00" + encoded).hexdigest()


@dataclass(frozen=True)
class IntentRecord:
    client_ref_id: str
    request_fingerprint: str
    order_id: int
    session_id: str
    status: str
    perm_id: int | None
    send_started: bool
    acknowledgement_seen: bool
    rejection_seen: bool
    submission_unknown_seen: bool
    cancel_started: bool
    cancel_claim_active: bool
    pending_cancel_seen: bool
    cancellation_unknown_seen: bool
    cancelled_seen: bool
    fill_count: int
    cancel_attempt_count: int
    cancel_not_sent_count: int
    cancel_unknown_count: int
    cancel_retry_authorized_count: int

    @property
    def can_transmit(self) -> bool:
        return self.status == "RESERVED" and not self.send_started

    @property
    def can_cancel(self) -> bool:
        return (
            self.send_started
            and self.status not in {"REJECT", "CANCELLED"}
            and not self.rejection_seen
            and not self.cancel_claim_active
            and not self.pending_cancel_seen
            and not self.cancellation_unknown_seen
        )


@dataclass(frozen=True)
class FillRecord:
    exec_id: str
    client_ref_id: str
    perm_id: int
    quantity: Decimal
    price: Decimal
    executed_at: str


@dataclass(frozen=True)
class CommissionRecord:
    report_id: str
    exec_id: str
    commission: Decimal
    currency: str
    ordinal: int


class IbkrExecutionLedger:
    """One explicit database permanently bound to one account/environment/client.

    Pass a newly chosen absolute path, not any existing Titan database. Reopening
    a journal requires the identical binding. ``lookup`` never contacts a broker
    and an absent local intent is not authoritative broker-order absence.
    """

    def __init__(self, path: Path, *, account_fingerprint: str, environment: str, client_id: int) -> None:
        self._account_fingerprint = _hash(account_fingerprint, "account_fingerprint")
        if environment not in {"live", "paper"}:
            raise ValueError("environment must be explicitly live or paper")
        self._environment = environment
        self._client_id = _integer(client_id, "client_id", positive=True)
        path = Path(path)
        if not path.is_absolute() or not path.parent.is_dir() or path.is_symlink():
            raise ValueError("ledger requires an explicit absolute non-symlink file path with an existing parent")
        path = path.parent.resolve(strict=True) / path.name
        self._validate_sidecars(path)
        if not path.exists() and any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm")):
            raise IbkrLedgerConflict("new ledger path has orphaned SQLite sidecars")
        created = False
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            self._preflight_existing(path)
        else:
            os.close(fd)
            created = True
        self.path = path
        self._lock = RLock()
        self._savepoint_serial = 0
        self._db = sqlite3.connect(str(path), timeout=10, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        try:
            if not created:
                # Repeat against the actual WAL-aware connection after the
                # non-writing main-file identity preflight. Binding is immutable
                # in this implementation; schema migration is not supported.
                if self._db.execute("PRAGMA application_id").fetchone()[0] != _APPLICATION_ID:
                    raise IbkrLedgerConflict("existing path is not an IBKR ledger")
                if self._db.execute("PRAGMA user_version").fetchone()[0] != _SCHEMA_VERSION:
                    raise IbkrLedgerConflict("unsupported IBKR ledger schema")
                self._validate_binding()
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            if created:
                self._initialize()
        except Exception:
            self._db.close()
            raise

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @property
    def account_fingerprint(self) -> str:
        return self._account_fingerprint

    @property
    def environment(self) -> str:
        return self._environment

    @property
    def client_id(self) -> int:
        return self._client_id

    def __enter__(self) -> "IbkrExecutionLedger":
        return self

    def __exit__(self, *unused: Any) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        # SDK callbacks normally run on a reader thread. Serialize this shared
        # connection as well as obtaining SQLite's cross-process writer lock.
        with self._lock:
            if not self._db.in_transaction:
                self._db.execute("BEGIN IMMEDIATE")
                try:
                    yield
                    self._db.execute("COMMIT")
                except BaseException:
                    self._db.execute("ROLLBACK")
                    raise
                return

            # Reconciliation applies multiple public append operations as one
            # transaction.  Nested append methods retain their local rollback
            # semantics through savepoints, while the outer transaction still
            # owns the only commit.  A later conflict therefore cannot leave an
            # earlier acknowledgement or fill durably visible.
            self._savepoint_serial += 1
            savepoint = f"titan_ibkr_ledger_{self._savepoint_serial}"
            self._db.execute(f"SAVEPOINT {savepoint}")
            try:
                yield
                self._db.execute(f"RELEASE SAVEPOINT {savepoint}")
            except BaseException:
                self._db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self._db.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise

    @contextmanager
    def reconciliation_batch(self) -> Iterator[None]:
        """Make one broker snapshot batch durable only if every fact agrees.

        This is an atomicity boundary, not mutation authority.  It only groups
        the ledger's existing monotone local evidence operations; it neither
        contacts IBKR nor grants submission or retry permission.
        """

        with self._transaction():
            yield

    def _initialize(self) -> None:
        statements = (
            "CREATE TABLE binding (singleton INTEGER PRIMARY KEY CHECK(singleton=1), account_fingerprint TEXT NOT NULL, environment TEXT NOT NULL, client_id INTEGER NOT NULL, next_order_id INTEGER NOT NULL)",
            "CREATE TABLE intents (client_ref_id TEXT PRIMARY KEY, request_fingerprint TEXT NOT NULL, order_id INTEGER NOT NULL UNIQUE, session_id TEXT NOT NULL, perm_id INTEGER UNIQUE, send_started INTEGER NOT NULL DEFAULT 0 CHECK(send_started IN(0,1)), created_at TEXT NOT NULL)",
            "CREATE TABLE events (ordinal INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE, client_ref_id TEXT NOT NULL REFERENCES intents(client_ref_id), kind TEXT NOT NULL, perm_id INTEGER, recorded_at TEXT NOT NULL)",
            "CREATE TABLE fills (exec_id TEXT PRIMARY KEY, client_ref_id TEXT NOT NULL REFERENCES intents(client_ref_id), perm_id INTEGER NOT NULL, quantity TEXT NOT NULL, price TEXT NOT NULL, executed_at TEXT NOT NULL, recorded_at TEXT NOT NULL)",
            "CREATE TABLE commissions (ordinal INTEGER PRIMARY KEY AUTOINCREMENT, report_id TEXT NOT NULL UNIQUE, exec_id TEXT NOT NULL REFERENCES fills(exec_id), commission TEXT NOT NULL, currency TEXT NOT NULL, recorded_at TEXT NOT NULL)",
            "CREATE INDEX events_by_intent ON events(client_ref_id)",
            "CREATE INDEX fills_by_intent ON fills(client_ref_id)",
            "CREATE INDEX commissions_by_execution ON commissions(exec_id)",
        )
        with self._transaction():
            for statement in statements:
                self._db.execute(statement)
            self._db.execute("INSERT INTO binding VALUES (1, ?, ?, ?, 1)", (self.account_fingerprint, self.environment, self.client_id))
            self._db.execute(f"PRAGMA application_id={_APPLICATION_ID}")
            self._db.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
        # An immutable read deliberately ignores WAL. Make the never-changing
        # account binding and schema identity durable in main before permitting
        # a second constructor to inspect this ledger without touching sidecars.
        checkpoint = self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if tuple(checkpoint) != (0, 0, 0):
            raise IbkrLedgerConflict("initial ledger identity checkpoint did not complete")

    @staticmethod
    def _validate_sidecars(path: Path) -> None:
        # Opening SQLite, even to reject a wrong database, can recover a hot
        # journal or alter shared-memory files. Such recovery is never implicit.
        journal = Path(str(path) + "-journal")
        if journal.exists() or journal.is_symlink():
            raise IbkrLedgerConflict("ledger rollback journal requires isolated recovery")
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(path) + suffix)
            if sidecar.is_symlink():
                raise IbkrLedgerConflict("ledger sidecar cannot be a symlink")
            if sidecar.exists():
                metadata = sidecar.stat()
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise IbkrLedgerConflict("ledger sidecar must be a uniquely linked regular file")

    def _preflight_existing(self, path: Path) -> None:
        # Do not open an arbitrary existing file with SQLite first: closing a
        # writable connection can checkpoint somebody else's crash-left WAL.
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise IbkrLedgerConflict("ledger must be a uniquely linked regular file")
            header = os.read(descriptor, 100)
        finally:
            os.close(descriptor)
        if len(header) != 100 or header[:16] != b"SQLite format 3\x00":
            raise IbkrLedgerConflict("existing path is not an initialized IBKR ledger")
        if int.from_bytes(header[68:72], "big") != _APPLICATION_ID:
            raise IbkrLedgerConflict("existing path is not an IBKR ledger")
        if int.from_bytes(header[60:64], "big") != _SCHEMA_VERSION:
            raise IbkrLedgerConflict("unsupported IBKR ledger schema")
        self._validate_sidecars(path)
        # mode=ro alone is insufficient: it can still create/update -shm.
        # Immutable mode reads only the initialized main file, with no recovery,
        # locks, or sidecar writes. Binding never changes after initialization.
        readonly = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            row = readonly.execute("SELECT account_fingerprint, environment, client_id FROM binding WHERE singleton=1").fetchone()
            if row != (self.account_fingerprint, self.environment, self.client_id):
                raise IbkrLedgerConflict("IBKR ledger account/environment/client binding mismatch")
        except sqlite3.DatabaseError:
            raise IbkrLedgerConflict("IBKR ledger immutable identity is corrupt") from None
        finally:
            readonly.close()
        current = path.stat()
        if (current.st_dev, current.st_ino, current.st_nlink) != (metadata.st_dev, metadata.st_ino, 1):
            raise IbkrLedgerConflict("ledger file identity changed during preflight")
        self._validate_sidecars(path)

    def _validate_binding(self) -> None:
        row = self._db.execute("SELECT account_fingerprint, environment, client_id FROM binding WHERE singleton=1").fetchone()
        if row is None or tuple(row) != (self.account_fingerprint, self.environment, self.client_id):
            raise IbkrLedgerConflict("IBKR ledger account/environment/client binding mismatch")

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds")

    def lookup(self, client_ref_id: str) -> IntentRecord | None:
        """Exact local read; a negative result is NOT broker absence proof."""
        ref = _uuid(client_ref_id, "client_ref_id")
        # A single SQLite statement provides one consistent snapshot even when
        # another connection appends a callback concurrently.
        with self._lock:
            row = self._db.execute(
                """SELECT i.*, COALESCE((SELECT group_concat(DISTINCT e.kind) FROM events e WHERE e.client_ref_id=i.client_ref_id), '') AS kinds,
                (SELECT count(*) FROM fills f WHERE f.client_ref_id=i.client_ref_id) AS fill_count,
                (SELECT count(*) FROM events e WHERE e.client_ref_id=i.client_ref_id AND e.kind='CANCEL_SENDING') AS cancel_attempt_count,
                (SELECT count(*) FROM events e WHERE e.client_ref_id=i.client_ref_id AND e.kind='CANCEL_NOT_SENT') AS cancel_not_sent_count,
                (SELECT count(*) FROM events e WHERE e.client_ref_id=i.client_ref_id AND e.kind='CANCEL_UNKNOWN') AS cancel_unknown_count,
                (SELECT count(*) FROM events e WHERE e.client_ref_id=i.client_ref_id AND e.kind='CANCEL_RETRY_AUTHORIZED') AS cancel_retry_authorized_count
                FROM intents i WHERE i.client_ref_id=?""", (ref,)
            ).fetchone()
        if row is None:
            return None
        kinds = set(row["kinds"].split(","))
        ack = "ACK" in kinds or row["fill_count"] > 0
        cancel_attempt_count = int(row["cancel_attempt_count"])
        cancel_not_sent_count = int(row["cancel_not_sent_count"])
        cancel_unknown_count = int(row["cancel_unknown_count"])
        cancel_retry_authorized_count = int(row["cancel_retry_authorized_count"])
        cancel_claim_active = cancel_attempt_count > (
            cancel_not_sent_count + cancel_unknown_count
        )
        cancellation_unknown = (
            cancel_unknown_count > cancel_retry_authorized_count
        )
        # Lifecycle evidence is monotone, not last-callback-wins. Cancellation
        # uncertainty does not change a known accepted entry into a rejection.
        if "CANCELLED" in kinds:
            status = "CANCELLED"
        elif cancellation_unknown:
            status = "CANCEL_UNKNOWN"
        elif "PENDING_CANCEL" in kinds:
            status = "PENDING_CANCEL"
        elif cancel_claim_active:
            status = "CANCEL_SENDING"
        elif ack:
            status = "ACK"
        elif "UNKNOWN" in kinds:
            status = "UNKNOWN"
        elif "ABORTED" in kinds or "SUBMIT_NOT_SENT" in kinds:
            status = "ABORTED"
        elif "REJECT" in kinds:
            status = "REJECT"
        else:
            status = "SENDING" if row["send_started"] else "RESERVED"
        return IntentRecord(
            ref, row["request_fingerprint"], row["order_id"], row["session_id"], status,
            row["perm_id"], bool(row["send_started"]), ack, "REJECT" in kinds,
            "UNKNOWN" in kinds, cancel_attempt_count > 0, cancel_claim_active,
            "PENDING_CANCEL" in kinds,
            cancellation_unknown, "CANCELLED" in kinds, row["fill_count"],
            cancel_attempt_count, cancel_not_sent_count, cancel_unknown_count,
            cancel_retry_authorized_count,
        )

    def allocate_intent(self, client_ref_id: str, request_fingerprint: str, session_id: str, broker_next_valid_id: int) -> IntentRecord:
        ref = _uuid(client_ref_id, "client_ref_id")
        fingerprint = _hash(request_fingerprint, "request_fingerprint")
        session = _uuid(session_id, "session_id")
        broker_floor = _integer(broker_next_valid_id, "broker_next_valid_id")
        with self._transaction():
            existing = self.lookup(ref)
            if existing is not None:
                if existing.request_fingerprint != fingerprint:
                    raise IbkrLedgerConflict("client reference already belongs to a different request")
                # Persist even newer broker floors on this idempotent path.
                self._db.execute("UPDATE binding SET next_order_id=max(next_order_id, ?) WHERE singleton=1", (broker_floor,))
                return existing
            local_floor = self._db.execute("SELECT next_order_id FROM binding WHERE singleton=1").fetchone()[0]
            allocated = max(local_floor, broker_floor)
            if allocated > _MAX_ORDER_ID:
                raise IbkrLedgerConflict("IBKR order ID range exhausted; no ID can be reused")
            self._db.execute("INSERT INTO intents (client_ref_id, request_fingerprint, order_id, session_id, created_at) VALUES (?, ?, ?, ?, ?)", (ref, fingerprint, allocated, session, self._now()))
            self._db.execute("UPDATE binding SET next_order_id=? WHERE singleton=1", (allocated + 1,))
            return self.lookup(ref)  # type: ignore[return-value]

    def lookup_order_id(self, order_id: int) -> IntentRecord | None:
        """Read-only lookup restricted to this journal's permanently bound client.

        An order ID alone is not globally unique. Callers must independently
        confirm callback account/client context before using this local lookup.
        """
        identity = _integer(order_id, "order_id")
        with self._lock:
            row = self._db.execute("SELECT client_ref_id FROM intents WHERE order_id=?", (identity,)).fetchone()
        return None if row is None else self.lookup(row[0])

    def observe_order_id(self, order_id: int) -> None:
        """Advance the allocation floor for every visible API order ID."""
        observed = _integer(order_id, "order_id")
        with self._transaction():
            self._db.execute("UPDATE binding SET next_order_id=max(next_order_id, ?) WHERE singleton=1", (observed + 1,))

    def _require(self, ref: str) -> IntentRecord:
        record = self.lookup(ref)
        if record is None:
            raise IbkrLedgerConflict("unknown local client reference")
        return record

    def mark_sending(self, client_ref_id: str) -> IntentRecord:
        ref = _uuid(client_ref_id, "client_ref_id")
        with self._transaction():
            if not self._require(ref).can_transmit:
                raise IbkrLedgerTransitionError("intent has prior transmission or broker evidence; retransmission forbidden")
            self._db.execute("UPDATE intents SET send_started=1 WHERE client_ref_id=?", (ref,))
            self._db.execute("INSERT INTO events(event_id, client_ref_id, kind, recorded_at) VALUES (?, ?, 'SENDING', ?)", (f"sending:{ref}", ref, self._now()))
            return self._require(ref)

    def abort_reserved_intent(self, client_ref_id: str) -> IntentRecord:
        """Permanently retire an exact identity proven not to have dispatched.

        This terminal marker is valid only while the ledger still proves the
        intent is RESERVED and ``send_started`` is false.  It prevents a fresh
        review or process from turning a known pre-dispatch validation failure
        into a later send of the same logical request.
        """

        ref = _uuid(client_ref_id, "client_ref_id")
        with self._transaction():
            record = self._require(ref)
            if record.status == "ABORTED":
                return record
            if not record.can_transmit:
                raise IbkrLedgerTransitionError(
                    "only a never-sent reserved intent can be aborted"
                )
            self._db.execute(
                "INSERT INTO events(event_id, client_ref_id, kind, recorded_at) "
                "VALUES (?, ?, 'ABORTED', ?)",
                (f"aborted:{ref}", ref, self._now()),
            )
            return self._require(ref)

    def record_submit_not_sent(self, client_ref_id: str) -> IntentRecord:
        """Resolve a post-claim SDK denial that conclusively touched no wire.

        The allocated order ID remains burned and the exact logical request is
        never retransmitted.  Unlike ``UNKNOWN``, this terminal local fact
        proves there is no broker exposure to reserve or reconcile.
        """

        ref = _uuid(client_ref_id, "client_ref_id")
        with self._transaction():
            record = self._require(ref)
            if record.status == "ABORTED":
                return record
            # A known-no-wire result is compatible only with the bare local
            # SENDING claim.  Every other state contains either an earlier
            # terminal marker or broker-originated/ambiguous lifecycle
            # evidence (including pending/cancelled facts that do not
            # necessarily carry an ACK).  Never append SUBMIT_NOT_SENT beside
            # contradictory broker evidence.
            if record.status != "SENDING" or not record.send_started:
                raise IbkrLedgerTransitionError(
                    "known-not-sent submit conflicts with broker uncertainty or evidence"
                )
            self._db.execute(
                "INSERT INTO events(event_id, client_ref_id, kind, recorded_at) "
                "VALUES (?, ?, 'SUBMIT_NOT_SENT', ?)",
                (f"submit-not-sent:{ref}", ref, self._now()),
            )
            return self._require(ref)

    def _bind_perm_id(self, ref: str, perm_id: int | None) -> None:
        record = self._require(ref)
        if perm_id is None:
            return
        if record.perm_id is not None and record.perm_id != perm_id:
            raise IbkrLedgerConflict("permanent order identity changed")
        owner = self._db.execute("SELECT client_ref_id FROM intents WHERE perm_id=?", (perm_id,)).fetchone()
        if owner is not None and owner[0] != ref:
            raise IbkrLedgerConflict("permanent order identity belongs to another intent")
        self._db.execute("UPDATE intents SET perm_id=? WHERE client_ref_id=?", (perm_id, ref))

    def claim_cancel(self, client_ref_id: str) -> IntentRecord:
        """Durably claim one currently authorized cancellation attempt.

        A claim is reusable only after either a conclusive local no-wire fact
        or a separately recorded, strictly newer positive broker observation
        resolves a prior ambiguous attempt.  Absence is never retry evidence.
        """
        ref = _uuid(client_ref_id, "client_ref_id")
        with self._transaction():
            record = self._require(ref)
            if not record.can_cancel:
                raise IbkrLedgerTransitionError("intent is not eligible for a first owned cancellation claim")
            attempt = record.cancel_attempt_count + 1
            self._db.execute("INSERT INTO events(event_id, client_ref_id, kind, recorded_at) VALUES (?, ?, 'CANCEL_SENDING', ?)", (f"cancel-sending:{ref}:{attempt}", ref, self._now()))
            return self._require(ref)

    def record_cancel_not_sent(self, client_ref_id: str) -> IntentRecord:
        """Resolve the active cancel claim after a conclusive SDK no-wire denial."""

        ref = _uuid(client_ref_id, "client_ref_id")
        with self._transaction():
            record = self._require(ref)
            if not record.cancel_claim_active or record.cancellation_unknown_seen:
                raise IbkrLedgerTransitionError(
                    "known-not-sent cancel requires one unresolved local claim"
                )
            attempt = record.cancel_attempt_count
            self._db.execute(
                "INSERT INTO events(event_id, client_ref_id, kind, recorded_at) "
                "VALUES (?, ?, 'CANCEL_NOT_SENT', ?)",
                (f"cancel-not-sent:{ref}:{attempt}", ref, self._now()),
            )
            return self._require(ref)

    def authorize_cancel_retry(
        self,
        client_ref_id: str,
        *,
        evidence_id: str,
        evidence_received_at: datetime,
    ) -> IntentRecord:
        """Resolve one ambiguous cancel from a newer positive working-order read.

        Callers must have matched the exact account/client/orderRef/orderId and
        a nonterminal cancellable broker state.  This method independently
        requires a new evidence identity received after the latest ambiguous
        cancel marker.  It never interprets a missing order as authorization.
        """

        ref = _uuid(client_ref_id, "client_ref_id")
        evidence = _hash(evidence_id, "evidence_id")
        received = _timestamp(evidence_received_at)
        with self._transaction():
            record = self._require(ref)
            if (
                not record.acknowledgement_seen
                or not record.cancellation_unknown_seen
                or record.cancelled_seen
                or record.pending_cancel_seen
                or record.rejection_seen
            ):
                raise IbkrLedgerTransitionError(
                    "cancel retry requires one unresolved acknowledged cancellation"
                )
            latest = self._db.execute(
                "SELECT recorded_at FROM events WHERE client_ref_id=? "
                "AND kind='CANCEL_UNKNOWN' ORDER BY ordinal DESC LIMIT 1",
                (ref,),
            ).fetchone()
            if latest is None or received <= str(latest["recorded_at"]):
                raise IbkrLedgerTransitionError(
                    "cancel retry evidence is not strictly newer than ambiguity"
                )
            ordinal = record.cancel_retry_authorized_count + 1
            self._db.execute(
                "INSERT INTO events(event_id, client_ref_id, kind, recorded_at) "
                "VALUES (?, ?, 'CANCEL_RETRY_AUTHORIZED', ?)",
                (f"cancel-retry-authorized:{ref}:{ordinal}:{evidence}", ref, received),
            )
            return self._require(ref)

    def record_event(self, client_ref_id: str, event_id: str, kind: str, perm_id: int | None = None) -> IntentRecord:
        ref = _uuid(client_ref_id, "client_ref_id")
        event = _token(event_id, "event_id")
        if kind not in _KINDS:
            raise ValueError("unsupported ledger event kind")
        permanent = _perm_id(perm_id)
        with self._transaction():
            self._require(ref)
            existing = self._db.execute("SELECT client_ref_id, kind, perm_id FROM events WHERE event_id=?", (event,)).fetchone()
            if existing is not None:
                if tuple(existing) != (ref, kind, permanent):
                    raise IbkrLedgerConflict("duplicate event identifier has conflicting facts")
                return self._require(ref)
            self._bind_perm_id(ref, permanent)
            self._db.execute("INSERT INTO events(event_id, client_ref_id, kind, perm_id, recorded_at) VALUES (?, ?, ?, ?, ?)", (event, ref, kind, permanent, self._now()))
            return self._require(ref)

    def record_fill(self, client_ref_id: str, exec_id: str, quantity: Decimal | str | int, price: Decimal | str | int, perm_id: int, executed_at: datetime) -> FillRecord:
        """Append immutable execution facts; a differing duplicate fails closed.

        Distinct correction execIds are distinct facts, not automatically netted.
        Consumers must reconcile execution corrections before calculating actual
        exposure; ``fill_count`` is evidence presence, never position quantity.
        """
        ref = _uuid(client_ref_id, "client_ref_id")
        execution = _token(exec_id, "exec_id")
        permanent = _perm_id(perm_id)
        if permanent is None:
            raise ValueError("a fill requires perm_id")
        amount = _decimal(quantity, "quantity", positive=True)
        fill_price = _decimal(price, "price", positive=True)
        stamp = _timestamp(executed_at)
        facts = (ref, permanent, amount, fill_price, stamp)
        with self._transaction():
            self._require(ref)
            existing = self._db.execute("SELECT client_ref_id, perm_id, quantity, price, executed_at FROM fills WHERE exec_id=?", (execution,)).fetchone()
            if existing is not None and tuple(existing) != facts:
                raise IbkrLedgerConflict("duplicate execution identifier has conflicting facts")
            self._bind_perm_id(ref, permanent)
            if existing is None:
                self._db.execute("INSERT INTO fills VALUES (?, ?, ?, ?, ?, ?, ?)", (execution, *facts, self._now()))
        return FillRecord(execution, ref, permanent, Decimal(amount), Decimal(fill_price), stamp)

    def fills(self, client_ref_id: str) -> tuple[FillRecord, ...]:
        ref = _uuid(client_ref_id, "client_ref_id")
        with self._lock:
            rows = self._db.execute("SELECT * FROM fills WHERE client_ref_id=? ORDER BY executed_at, exec_id", (ref,)).fetchall()
        return tuple(FillRecord(row["exec_id"], ref, row["perm_id"], Decimal(row["quantity"]), Decimal(row["price"]), row["executed_at"]) for row in rows)

    def record_commission(self, exec_id: str, report_id: str, commission: Decimal | str | int, currency: str) -> CommissionRecord:
        """Append a fee report or correction; never overwrite a fill/older fee.

        Stable report identity must be provided by the normalization layer.
        Receive order is not a guarantee that the last report is authoritative.
        A negative commission is retained because rebates can occur.
        """
        execution = _token(exec_id, "exec_id")
        report = _token(report_id, "report_id")
        amount = _decimal(commission, "commission")
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError("commission currency must be a three-letter code")
        facts = (execution, amount, currency)
        with self._transaction():
            if self._db.execute("SELECT 1 FROM fills WHERE exec_id=?", (execution,)).fetchone() is None:
                raise IbkrLedgerConflict("commission references an unknown execution; reconcile before recording")
            existing = self._db.execute("SELECT ordinal, exec_id, commission, currency FROM commissions WHERE report_id=?", (report,)).fetchone()
            if existing is not None:
                if tuple(existing)[1:] != facts:
                    raise IbkrLedgerConflict("duplicate fee report identifier has conflicting facts")
                ordinal = existing["ordinal"]
            else:
                result = self._db.execute("INSERT INTO commissions(report_id, exec_id, commission, currency, recorded_at) VALUES (?, ?, ?, ?, ?)", (report, *facts, self._now()))
                ordinal = result.lastrowid
        return CommissionRecord(report, execution, Decimal(amount), currency, ordinal)  # type: ignore[arg-type]

    def commissions(self, exec_id: str) -> tuple[CommissionRecord, ...]:
        execution = _token(exec_id, "exec_id")
        with self._lock:
            rows = self._db.execute("SELECT * FROM commissions WHERE exec_id=? ORDER BY ordinal", (execution,)).fetchall()
        return tuple(CommissionRecord(row["report_id"], execution, Decimal(row["commission"]), row["currency"], row["ordinal"]) for row in rows)
