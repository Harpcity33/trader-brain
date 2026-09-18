"""Proposed, permanently non-authorizing session trading-P&L diagnostic.

This is NOT whole-account NAV minus external flows and is NOT an approved risk
policy.  It measures USD stock execution cash flows, reported execution fees,
and residual long positions at fresh bids, from a claimed flat pre-entry start.
It excludes non-trade interest, dividends, tax, FX and other account adjustments.
Bid marks are estimates, not guaranteed liquidation proceeds.

Input status/provenance fields are caller assertions, not broker authentication
or provider completeness guarantees.  No existing broker, risk, custody, CLI or
authority component imports this module.  The optional SQLite store provides
ordinary crash/restart durability, not tamper-proof custody or live authority.
The latch records observed valid breaches only; it cannot prove that a breach
did not occur during missing-data intervals or between observations.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from zoneinfo import ZoneInfo


MODEL = "proposed_usd_flat_start_session_trading_pnl_shadow_v1"
_NY = ZoneInfo("America/New_York")
_TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,160}\Z", re.ASCII)
_APPLICATION_ID = 0x54535053


class ShadowInputError(ValueError):
    """Static, non-secret-bearing input or durable-state error."""


class EvidenceStatus(str, Enum):
    CONFIRMED = "CONFIRMED"
    UNKNOWN = "UNKNOWN"
    FAILED = "FAILED"


class InputOrigin(str, Enum):
    SYNTHETIC = "SYNTHETIC"
    OBSERVED_UNVERIFIED = "OBSERVED_UNVERIFIED"


def _token(value: object) -> None:
    if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
        raise ShadowInputError("SHADOW_IDENTIFIER_INVALID")


def _time(value: object) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ShadowInputError("SHADOW_TIME_INVALID")


def _money(value: object, *, positive: bool = False) -> None:
    # Bounded decimal inputs keep arithmetic exact with the local precision
    # below and reject sentinels/NaN/Infinity, floats and unbounded exponents.
    if (
        not isinstance(value, Decimal)
        or not value.is_finite()
        or len(value.as_tuple().digits) > 30
        or value.as_tuple().exponent < -12
        or value.adjusted() > 18
        or (positive and value <= 0)
    ):
        raise ShadowInputError("SHADOW_AMOUNT_INVALID")


def _integer(value: object, *, minimum: int = 0) -> None:
    if type(value) is not int or not minimum <= value <= 2_147_483_647:
        raise ShadowInputError("SHADOW_INTEGER_INVALID")


def _enum(value: object, kind: type[Enum]) -> None:
    if not isinstance(value, kind):
        raise ShadowInputError("SHADOW_STATUS_INVALID")


def _rows(value: object, kind: type) -> None:
    if not isinstance(value, tuple) or len(value) > 100_000:
        raise ShadowInputError("SHADOW_ROWS_INVALID")
    if any(type(item) is not kind for item in value):
        raise ShadowInputError("SHADOW_ROW_INVALID")


@dataclass(frozen=True)
class SessionBaseline:
    # Use an opaque non-secret account binding, not a credential or display ID.
    account_binding: str
    frozen_at: datetime
    starting_nlv: Decimal
    source_evidence_id: str
    origin: InputOrigin
    flat_start: EvidenceStatus
    pre_entry: EvidenceStatus
    initial_exposure_reconciled: EvidenceStatus

    def __post_init__(self) -> None:
        _token(self.account_binding)
        _token(self.source_evidence_id)
        _time(self.frozen_at)
        _money(self.starting_nlv, positive=True)
        _enum(self.origin, InputOrigin)
        for value in (self.flat_start, self.pre_entry, self.initial_exposure_reconciled):
            _enum(value, EvidenceStatus)

    @property
    def session_date(self) -> date:
        return self.frozen_at.astimezone(_NY).date()


@dataclass(frozen=True)
class Execution:
    account_binding: str
    exec_id: str
    contract_id: int
    side: str
    quantity: int
    price: Decimal
    executed_at: datetime
    currency: str = "USD"
    security_type: str = "STK"
    correction_of: str | None = None

    def __post_init__(self) -> None:
        for value in (self.account_binding, self.exec_id, self.currency, self.security_type):
            _token(value)
        _integer(self.contract_id, minimum=1)
        _integer(self.quantity, minimum=1)
        _money(self.price, positive=True)
        _time(self.executed_at)
        if self.side not in ("BUY", "SELL"):
            raise ShadowInputError("SHADOW_SIDE_INVALID")
        if self.correction_of is not None:
            _token(self.correction_of)


@dataclass(frozen=True)
class ExecutionFee:
    account_binding: str
    exec_id: str
    amount: Decimal
    reported_at: datetime
    currency: str = "USD"

    def __post_init__(self) -> None:
        for value in (self.account_binding, self.exec_id, self.currency):
            _token(value)
        _money(self.amount)  # A genuinely reported signed rebate is not zero.
        _time(self.reported_at)


@dataclass(frozen=True)
class Position:
    account_binding: str
    contract_id: int
    quantity: int
    currency: str = "USD"
    security_type: str = "STK"

    def __post_init__(self) -> None:
        for value in (self.account_binding, self.currency, self.security_type):
            _token(value)
        _integer(self.contract_id, minimum=1)
        _integer(self.quantity, minimum=-2_147_483_647)


@dataclass(frozen=True)
class BidMark:
    contract_id: int
    price: Decimal
    size: int
    quoted_at: datetime
    received_at: datetime
    source_evidence_id: str
    currency: str = "USD"

    def __post_init__(self) -> None:
        _integer(self.contract_id, minimum=1)
        _integer(self.size)
        _money(self.price, positive=True)
        _time(self.quoted_at)
        _time(self.received_at)
        _token(self.source_evidence_id)
        _token(self.currency)


@dataclass(frozen=True)
class ExecutionCoverage:
    # These bounds/statuses are explicit CLAIMS supplied by a future adapter.
    # The TWS end callback does not manufacture a global complete-through time.
    claimed_from: datetime
    claimed_through: datetime
    all_clients: EvidenceStatus
    continuous: EvidenceStatus
    corrections_reconciled: EvidenceStatus
    source_evidence_id: str

    def __post_init__(self) -> None:
        _time(self.claimed_from)
        _time(self.claimed_through)
        _token(self.source_evidence_id)
        for value in (self.all_clients, self.continuous, self.corrections_reconciled):
            _enum(value, EvidenceStatus)
        if self.claimed_from > self.claimed_through:
            raise ShadowInputError("SHADOW_COVERAGE_RANGE_INVALID")


@dataclass(frozen=True)
class SessionObservation:
    account_binding: str
    as_of: datetime
    origin: InputOrigin
    source_evidence_id: str
    executions: tuple[Execution, ...]
    fees: tuple[ExecutionFee, ...]
    positions: tuple[Position, ...]
    marks: tuple[BidMark, ...]
    coverage: ExecutionCoverage
    positions_reconciled: EvidenceStatus
    orders_exposure_reconciled: EvidenceStatus
    accounting_reconciled: EvidenceStatus
    unexplained_accounting_delta: Decimal | None
    # Informational only: never added to P&L or to the immutable denominator.
    # A supplied amount is not an assertion of exhaustive external-flow coverage.
    reported_external_cash_flow: Decimal | None = None

    def __post_init__(self) -> None:
        _token(self.account_binding)
        _token(self.source_evidence_id)
        _time(self.as_of)
        _enum(self.origin, InputOrigin)
        for value in (
            self.positions_reconciled,
            self.orders_exposure_reconciled,
            self.accounting_reconciled,
        ):
            _enum(value, EvidenceStatus)
        for value, kind in (
            (self.executions, Execution), (self.fees, ExecutionFee),
            (self.positions, Position), (self.marks, BidMark),
        ):
            _rows(value, kind)
        if type(self.coverage) is not ExecutionCoverage:
            raise ShadowInputError("SHADOW_COVERAGE_INVALID")
        for value in (self.unexplained_accounting_delta, self.reported_external_cash_flow):
            if value is not None:
                _money(value)


@dataclass(frozen=True)
class ShadowResult:
    as_of: datetime
    starting_nlv: Decimal
    session_pnl: Decimal | None
    performance_fraction: Decimal | None
    loss_latched: bool
    blockers: tuple[str, ...]
    synthetic_input: bool

    def public_dict(self) -> dict[str, object]:
        return {
            "model": MODEL,
            "diagnostic_only": True,
            "live_authority": False,
            "policy_approved": False,
            "source_authentication_established": False,
            "external_flow_completeness": "NOT_ESTABLISHED",
            "whole_account_return": False,
            "latch_scope": "OBSERVED_VALID_BREACHES_ONLY",
            "unobserved_breach_exclusion_established": False,
            "synthetic_input": self.synthetic_input,
            "as_of": self.as_of.isoformat(),
            "starting_nlv": str(self.starting_nlv),
            "session_pnl": None if self.session_pnl is None else str(self.session_pnl),
            "performance_fraction": (
                None if self.performance_fraction is None else str(self.performance_fraction)
            ),
            "loss_latched": self.loss_latched,
            "status": "BLOCKED" if self.blockers else "COMPUTABLE_SHADOW_ONLY",
            "blockers": list(self.blockers),
        }


def _unique(rows: tuple, key: str, reason: str, blockers: set[str]) -> dict:
    result: dict = {}
    for row in rows:
        identity = getattr(row, key)
        if identity in result and result[identity] != row:
            blockers.add(reason)
        else:
            result[identity] = row
    return result


def evaluate_session_pnl(
    baseline: SessionBaseline,
    observation: SessionObservation,
    *,
    now: datetime,
    max_quote_age: timedelta = timedelta(seconds=5),
    max_observation_age: timedelta = timedelta(seconds=5),
) -> ShadowResult:
    """Pure arithmetic over explicit evidence claims; never a readiness result."""
    if type(baseline) is not SessionBaseline or type(observation) is not SessionObservation:
        raise ShadowInputError("SHADOW_INPUT_TYPE_INVALID")
    _time(now)
    for value in (max_quote_age, max_observation_age):
        if not isinstance(value, timedelta) or not timedelta(0) < value <= timedelta(minutes=5):
            raise ShadowInputError("SHADOW_FRESHNESS_INVALID")
    problems: set[str] = set()
    for value, reason in (
        (baseline.flat_start, "FLAT_START_UNCONFIRMED"),
        (baseline.pre_entry, "PRE_ENTRY_START_UNCONFIRMED"),
        (baseline.initial_exposure_reconciled, "INITIAL_EXPOSURE_UNRECONCILED"),
        (observation.coverage.all_clients, "ALL_CLIENT_EXECUTIONS_UNPROVEN"),
        (observation.coverage.continuous, "EXECUTION_CONTINUITY_UNPROVEN"),
        (observation.coverage.corrections_reconciled, "CORRECTIONS_UNRECONCILED"),
        (observation.positions_reconciled, "POSITIONS_UNRECONCILED"),
        (observation.orders_exposure_reconciled, "ORDER_EXPOSURE_UNRECONCILED"),
        (observation.accounting_reconciled, "ACCOUNTING_UNRECONCILED"),
    ):
        if value is not EvidenceStatus.CONFIRMED:
            problems.add(reason)
    if observation.unexplained_accounting_delta is None:
        problems.add("ACCOUNTING_DELTA_UNKNOWN")
    elif observation.unexplained_accounting_delta != 0:
        problems.add("UNEXPLAINED_ACCOUNTING_MOVEMENT")
    if observation.account_binding != baseline.account_binding:
        problems.add("ACCOUNT_BINDING_MISMATCH")
    if observation.origin is not baseline.origin:
        problems.add("INPUT_ORIGIN_MISMATCH")
    if not baseline.frozen_at <= observation.as_of <= now:
        problems.add("OBSERVATION_TIME_INVALID")
    if observation.as_of.astimezone(_NY).date() != baseline.session_date:
        problems.add("SESSION_DATE_MISMATCH")
    if now - observation.as_of > max_observation_age:
        problems.add("OBSERVATION_STALE")
    if (
        observation.coverage.claimed_from > baseline.frozen_at
        or observation.coverage.claimed_through < observation.as_of
        or observation.coverage.claimed_through > now
    ):
        problems.add("EXECUTION_COVERAGE_RANGE_INSUFFICIENT")

    executions = _unique(observation.executions, "exec_id", "EXECUTION_DUPLICATE_CONFLICT", problems)
    fees = _unique(observation.fees, "exec_id", "FEE_DUPLICATE_CONFLICT", problems)
    positions = _unique(observation.positions, "contract_id", "POSITION_DUPLICATE_CONFLICT", problems)
    marks = _unique(observation.marks, "contract_id", "MARK_DUPLICATE_CONFLICT", problems)
    quantities: dict[int, int] = {}
    correction_families: dict[str, str] = {}
    with localcontext() as context:
        context.prec = 80
        cash = Decimal(0)
        for execution in sorted(executions.values(), key=lambda value: (value.executed_at, value.exec_id)):
            if execution.account_binding != baseline.account_binding:
                problems.add("FOREIGN_ACCOUNT_EXECUTION")
            if execution.currency != "USD" or execution.security_type != "STK":
                problems.add("UNSUPPORTED_EXECUTION_INSTRUMENT")
            if not baseline.frozen_at <= execution.executed_at <= observation.as_of:
                problems.add("EXECUTION_TIME_OUTSIDE_SESSION")
            if execution.correction_of is not None:
                problems.add("EXECUTION_CORRECTION_UNRESOLVED")
            # IBKR documents ordinary ExecIDs as four segments, with an
            # execution correction changing only the final numeric segment.
            # Do not count both versions even if an adapter omitted the
            # explicit correction_of marker.  This does not resolve or choose
            # a revision, and makes no claim about five-segment combo IDs.
            segments = execution.exec_id.split(".")
            if len(segments) == 4 and segments[-1].isascii() and segments[-1].isdigit():
                family = ".".join(segments[:-1])
                if family in correction_families and correction_families[family] != execution.exec_id:
                    problems.add("EXECUTION_CORRECTION_UNRESOLVED")
                correction_families[family] = execution.exec_id
            sign = 1 if execution.side == "BUY" else -1
            quantities[execution.contract_id] = quantities.get(execution.contract_id, 0) + sign * execution.quantity
            if quantities[execution.contract_id] < 0:
                problems.add("SHORT_OR_EXECUTION_SEQUENCE_UNRESOLVED")
            cash -= sign * execution.quantity * execution.price
            fee = fees.get(execution.exec_id)
            if fee is None:
                problems.add("EXECUTION_FEE_MISSING")
            else:
                if fee.account_binding != baseline.account_binding or fee.currency != "USD":
                    problems.add("EXECUTION_FEE_SCOPE_INVALID")
                if not execution.executed_at <= fee.reported_at <= observation.as_of:
                    problems.add("EXECUTION_FEE_TIME_INVALID")
                cash -= fee.amount
        if set(fees) - set(executions):
            problems.add("ORPHAN_EXECUTION_FEE")
        actual: dict[int, int] = {}
        for position in positions.values():
            if position.account_binding != baseline.account_binding:
                problems.add("FOREIGN_ACCOUNT_POSITION")
            if position.currency != "USD" or position.security_type != "STK" or position.quantity < 0:
                problems.add("UNSUPPORTED_POSITION")
            if position.quantity:
                actual[position.contract_id] = position.quantity
        expected = {key: value for key, value in quantities.items() if value}
        if actual != expected:
            problems.add("POSITION_EXECUTION_MISMATCH")
        for contract_id, quantity in expected.items():
            mark = marks.get(contract_id)
            if mark is None:
                problems.add("BID_MARK_MISSING")
                continue
            if mark.currency != "USD":
                problems.add("BID_MARK_CURRENCY_INVALID")
            if not mark.quoted_at <= mark.received_at <= observation.as_of:
                problems.add("BID_MARK_TIME_INVALID")
            if now - mark.quoted_at > max_quote_age:
                problems.add("BID_MARK_STALE")
            if mark.size < quantity:
                problems.add("BID_SIZE_INSUFFICIENT")
            cash += quantity * mark.price
        pnl = None if problems else cash
        ratio = None if pnl is None else pnl / baseline.starting_nlv
        breached = pnl is not None and pnl <= -(baseline.starting_nlv * Decimal("0.10"))
    return ShadowResult(
        as_of=observation.as_of,
        starting_nlv=baseline.starting_nlv,
        session_pnl=pnl,
        performance_fraction=ratio,
        loss_latched=breached,
        blockers=tuple(sorted(problems)),
        synthetic_input=(baseline.origin is InputOrigin.SYNTHETIC or observation.origin is InputOrigin.SYNTHETIC),
    )


def _plain(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _plain(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        # Numeric equality must imply replay identity: 1, 1.00 and 1E0 are
        # the same economic value.  Decimal.normalize() can round under the
        # caller's decimal context; fixed formatting and textual trimming do
        # not.  Validated inputs have bounded digits/exponents.
        if value == 0:
            return "0"
        rendered = format(value, "f")
        return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def _canonical(value: object) -> str:
    return json.dumps(_plain(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ShadowSessionStore:
    """Separate SQLite shadow journal: immutable baseline and irreversible latch.

    Creation is explicit and create-only.  Existing stores must already carry
    this module's application/schema identity.  The store is not an approved
    risk ledger and offers no authenticity or malicious-tamper resistance.
    """

    def __init__(self, path: Path, *, create: bool = False) -> None:
        if type(create) is not bool or not isinstance(path, Path) or not path.is_absolute():
            raise ShadowInputError("SHADOW_STORE_PATH_INVALID")
        if path.parent.resolve() != path.parent or path.is_symlink():
            raise ShadowInputError("SHADOW_STORE_PATH_INVALID")
        try:
            if create:
                descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                os.close(descriptor)
            info = path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077
            ):
                raise ShadowInputError("SHADOW_STORE_FILE_INVALID")
            self._connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, isolation_level=None)
            self._connection.execute("PRAGMA busy_timeout = 5000")
            self._connection.execute("PRAGMA synchronous = FULL")
            if create:
                self._initialize()
            self._verify()
        except (OSError, sqlite3.Error):
            if hasattr(self, "_connection"):
                self._connection.close()
            raise ShadowInputError("SHADOW_STORE_UNAVAILABLE") from None
        except ShadowInputError:
            if hasattr(self, "_connection"):
                self._connection.close()
            raise

    def _initialize(self) -> None:
        connection = self._connection
        connection.execute("BEGIN EXCLUSIVE")
        try:
            connection.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
            connection.execute("CREATE TABLE metadata (model TEXT NOT NULL, version INTEGER NOT NULL)")
            connection.execute("INSERT INTO metadata VALUES (?, 1)", (MODEL,))
            connection.execute("""CREATE TABLE sessions (
                account_binding TEXT NOT NULL, day TEXT NOT NULL,
                baseline TEXT NOT NULL, baseline_hash TEXT NOT NULL,
                last_as_of TEXT, last_hash TEXT,
                loss_latched INTEGER NOT NULL CHECK(loss_latched IN (0,1)),
                history_conflict INTEGER NOT NULL CHECK(history_conflict IN (0,1)),
                PRIMARY KEY(account_binding, day))""")
            for table in ("executions", "fees"):
                connection.execute(f"""CREATE TABLE {table} (
                    account_binding TEXT NOT NULL, day TEXT NOT NULL,
                    event_id TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(account_binding, day, event_id))""")
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

    def _verify(self) -> None:
        connection = self._connection
        if (
            connection.execute("PRAGMA application_id").fetchone() != (_APPLICATION_ID,)
            or connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]
            or connection.execute("SELECT model, version FROM metadata").fetchall() != [(MODEL, 1)]
        ):
            raise ShadowInputError("SHADOW_STORE_IDENTITY_INVALID")

    def close(self) -> None:
        self._connection.close()

    def observe(
        self,
        baseline: SessionBaseline,
        observation: SessionObservation,
        *,
        now: datetime,
    ) -> ShadowResult:
        """Record cumulative session events; omission cannot erase prior fills."""
        result = evaluate_session_pnl(baseline, observation, now=now)
        # Do not freeze an unproven/late/foreign baseline into durable lineage.
        if (
            baseline.flat_start is not EvidenceStatus.CONFIRMED
            or baseline.pre_entry is not EvidenceStatus.CONFIRMED
            or baseline.initial_exposure_reconciled is not EvidenceStatus.CONFIRMED
            or baseline.account_binding != observation.account_binding
            or baseline.frozen_at > observation.as_of
            or observation.as_of > now
            or observation.as_of.astimezone(_NY).date() != baseline.session_date
        ):
            raise ShadowInputError("SHADOW_BASELINE_NOT_RECORDABLE")
        scope = (baseline.account_binding, baseline.session_date.isoformat())
        baseline_json = _canonical(baseline)
        observation_hash = _digest(_canonical(observation))
        observed_at = observation.as_of.astimezone(timezone.utc).isoformat()
        connection = self._connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT baseline, baseline_hash, last_as_of, last_hash, loss_latched, history_conflict "
                "FROM sessions WHERE account_binding = ? AND day = ?", scope,
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO sessions VALUES (?, ?, ?, ?, NULL, NULL, 0, 0)",
                    (*scope, baseline_json, _digest(baseline_json)),
                )
                prior_latch = prior_conflict = False
            else:
                if row[0] != baseline_json or row[1] != _digest(row[0]):
                    raise ShadowInputError("SHADOW_BASELINE_IMMUTABLE")
                if row[4] not in (0, 1) or row[5] not in (0, 1):
                    raise ShadowInputError("SHADOW_STORE_STATE_INVALID")
                if row[2] is not None and (
                    observed_at < row[2]
                    or (observed_at == row[2] and observation_hash != row[3])
                ):
                    raise ShadowInputError("SHADOW_OBSERVATION_NOT_MONOTONE")
                prior_latch, prior_conflict = bool(row[4]), bool(row[5])
            problems = set(result.blockers)
            conflict = prior_conflict
            for table, events, missing, changed in (
                ("executions", observation.executions, "EXECUTION_HISTORY_REGRESSED", "EXECUTION_HISTORY_CONFLICT"),
                ("fees", observation.fees, "FEE_HISTORY_REGRESSED", "FEE_HISTORY_CONFLICT"),
            ):
                current: dict[str, str] = {}
                for event in events:
                    payload = _canonical(event)
                    if event.exec_id in current and current[event.exec_id] != payload:
                        conflict = True
                    else:
                        current[event.exec_id] = payload
                previous = dict(connection.execute(
                    f"SELECT event_id, payload FROM {table} WHERE account_binding = ? AND day = ?", scope,
                ))
                if previous.keys() - current.keys():
                    problems.add(missing)
                for identity, payload in current.items():
                    if identity in previous and previous[identity] != payload:
                        problems.add(changed)
                        conflict = True
                    elif identity not in previous:
                        connection.execute(
                            f"INSERT INTO {table} VALUES (?, ?, ?, ?)", (*scope, identity, payload),
                        )
            if conflict:
                problems.add("DURABLE_EVENT_CONFLICT_UNRESOLVED")
            # Invalid history cannot create a new measured latch, but no
            # blocked/recovered/changed observation may clear an existing one.
            result = replace(
                result,
                session_pnl=None if problems else result.session_pnl,
                performance_fraction=None if problems else result.performance_fraction,
                loss_latched=prior_latch or (not problems and result.loss_latched),
                blockers=tuple(sorted(problems)),
            )
            connection.execute(
                "UPDATE sessions SET last_as_of = ?, last_hash = ?, loss_latched = ?, history_conflict = ? "
                "WHERE account_binding = ? AND day = ?",
                (observed_at, observation_hash, int(result.loss_latched), int(conflict), *scope),
            )
            connection.execute("COMMIT")
            return result
        except (sqlite3.Error, ShadowInputError):
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise ShadowInputError("SHADOW_STORE_OBSERVATION_REJECTED") from None
