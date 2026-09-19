"""Versioned session-trading-P&L policy contract; no live authority or wiring.

This module validates policy bindings and defines pure, monotone risk-state
transitions. It does not authenticate approval bytes, broker observations or
baseline claims, calculate execution P&L, read credentials, persist state, or
authorize orders. In particular a shadow result is not an accepted measurement.

A future integration must authenticate inputs and atomically persist the state
returned by begin_observation BEFORE starting a read, then persist completion
before using its risk facts. Failed/missing reads survive as unresolved incidents;
neither a healthy later read nor a process restart is permission to discard them.
That durable/authenticated integration is intentionally not implemented here.
Existing NAV/flow, weekly, high-water, pricing and authority fields stay distinct.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from enum import Enum
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Mapping
from zoneinfo import ZoneInfo


SCHEMA = "titan_session_trading_pre_trade_balance_2026-09-18_v1"
MODEL = "session_trading_pnl_against_pre_trade_balance"
STATE_SCHEMA = "titan_session_trading_risk_state_2026-09-18_v1"
OWNER_AMENDMENT_PATH = "validation/full-live/2026-09-18/OWNER_SESSION_TRADING_PNL_AMENDMENT_2026-09-18.md"
OWNER_AMENDMENT_SHA256 = "da60ce0060764aa4cffdb04c1de04e4af89ffeb914b2ef479d3c90f2e1db1577"
POLICY_RELATIVE_PATH = "config/risk_limits_ibkr_session_trading.json"
_APPROVAL_FILES = {
    "validation/full-live/2026-09-14/PROPOSED_OWNER_POLICY_2026-09-14.md": "cc9de013e880864b8d7400837a71c8742b3116a2fe7269f252cfc88bd50617cf",
    "validation/full-live/2026-09-14/OWNER_POLICY_APPROVAL_2026-09-14.md": "d27a1cc0c79629292f1353652440c984d3c7d510bcf3e08c4c595f25da7f4aae",
    "validation/full-live/2026-09-14/OWNER_DAILY_STARTING_EQUITY_POLICY_AMENDMENT_2026-09-14.md": "78571bb3f19157d5f2a8d81976ba7a4a4f0bdc782683b276130b59ca1627c2ad",
    OWNER_AMENDMENT_PATH: OWNER_AMENDMENT_SHA256,
}
_NY = ZoneInfo("America/New_York")
_HASH = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,160}\Z", re.ASCII)
_MAX_AGE = timedelta(seconds=5)
_FIXED = {
    "schema_version": SCHEMA,
    "model": MODEL,
    "currency": "USD",
    "session_timezone": "America/New_York",
    "denominator_basis": "fixed_flat_pre_entry_net_liquidation_value",
    "performance_basis": "stock_execution_cash_flows_minus_reported_fees_plus_residual_long_bid_value",
    "daily_loss_fraction": "0.10",
    "daily_profit_aspiration_fraction": "0.15",
    "post_goal_floor": None,
    "intraday_baseline_reset_allowed": False,
    "external_cash_flows_in_performance": False,
    "includes_open_pnl": True,
    "includes_incurred_fees": True,
    "daily_loss_action": "irreversible_entry_lock_and_guarded_closeout",
    "profit_goal_action": "aspirational_only_no_forced_trade_or_profit_floor",
    "maximum_entry_risk_capacity": "max_zero_min_fixed_loss_budget_and_remaining_session_headroom",
    "all_open_pending_uncovered_unresolved_downside_required": True,
    "positive_execution_reserve_required": True,
    "commission_reserve_required": True,
    "unleveraged_cash_and_buying_power_required": True,
    "loss_lock_is_irreversible_for_session": True,
    "missing_data_incident_policy": "pending_before_read_failed_or_gap_sticky",
}


class SessionTradingPolicyError(ValueError):
    """Fixed, non-private contract error."""


def _fail(reason: str) -> None:
    raise SessionTradingPolicyError("SESSION_TRADING_" + reason)


def _hash(value: object) -> None:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        _fail("HASH_INVALID")


def _time(value: object) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _fail("TIME_INVALID")


def _amount(value: object, *, positive: bool = False) -> None:
    if (type(value) is not Decimal or not value.is_finite()
            or len(value.as_tuple().digits) > 30 or value.as_tuple().exponent < -12
            or value.adjusted() > 18 or (positive and value <= 0)):
        _fail("AMOUNT_INVALID")


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _digest(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SessionTradingPolicy:
    policy_sha256: str
    amendment_path: str
    amendment_sha256: str

    def __post_init__(self) -> None:
        _hash(self.policy_sha256)
        _hash(self.amendment_sha256)
        if not isinstance(self.amendment_path, str):
            _fail("AMENDMENT_PATH_INVALID")
        path = PurePosixPath(self.amendment_path)
        if (path.is_absolute()
                or ".." in path.parts or path.as_posix() != self.amendment_path
                or path.suffix != ".md"):
            _fail("AMENDMENT_PATH_INVALID")


def load_session_trading_policy(
    raw: Mapping[str, object], *, expected_amendment_path: str = OWNER_AMENDMENT_PATH,
    expected_amendment_sha256: str = OWNER_AMENDMENT_SHA256,
) -> SessionTradingPolicy:
    """Strict semantics/binding check, NOT authentication of owner approval.

    Expected bindings are pinned, not caller-selected authority. The caller
    must independently verify original approval bytes, or use the root loader.
    This mapping parser alone never claims those files have been verified.
    """
    _hash(expected_amendment_sha256)
    if (expected_amendment_path != OWNER_AMENDMENT_PATH
            or expected_amendment_sha256 != OWNER_AMENDMENT_SHA256):
        _fail("AMENDMENT_BINDING_MISMATCH")
    expected = dict(_FIXED, owner_risk_amendment={
        "amendment_path": expected_amendment_path,
        "amendment_sha256": expected_amendment_sha256,
    })
    try:
        # JSON comparison deliberately distinguishes false/0 and true/1.
        if type(raw) is not dict or _digest(raw) != _digest(expected):
            _fail("POLICY_CONTRACT_MISMATCH")
    except (TypeError, ValueError, OverflowError):
        _fail("POLICY_CONTRACT_MISMATCH")
    return SessionTradingPolicy(_digest(expected), expected_amendment_path, expected_amendment_sha256)


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("DUPLICATE_POLICY_KEY")
        result[key] = value
    return result


def load_session_trading_policy_from_root(root: Path) -> SessionTradingPolicy:
    """Verify pinned policy-approval bytes, not broker data or live authority.

    This read-only source/release helper does not select the new model in the
    existing live configuration, migrate any state, or start services.
    """
    try:
        if not isinstance(root, Path) or not root.is_absolute() or root.resolve(strict=True) != root:
            _fail("ROOT_INVALID")

        def read(relative: str) -> bytes:
            path = root / relative
            if path.resolve(strict=True) != path or not path.is_file() or path.stat().st_size > 1_048_576:
                _fail("POLICY_FILE_INVALID")
            data = path.read_bytes()
            if len(data) > 1_048_576:
                _fail("POLICY_FILE_INVALID")
            return data

        for relative, expected_hash in _APPROVAL_FILES.items():
            if hashlib.sha256(read(relative)).hexdigest() != expected_hash:
                _fail("APPROVAL_BYTES_MISMATCH")
        raw = json.loads(read(POLICY_RELATIVE_PATH).decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
        return load_session_trading_policy(raw)
    except SessionTradingPolicyError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError):
        _fail("POLICY_FILES_UNAVAILABLE")


@dataclass(frozen=True)
class SessionTradingBaseline:
    """Immutable input claims; construction does not authenticate broker facts."""
    policy_sha256: str
    account_binding_sha256: str
    evidence_sha256: str
    frozen_at: datetime
    starting_nlv: Decimal
    flat_start: bool
    pre_entry: bool
    initial_exposure_reconciled: bool

    def __post_init__(self) -> None:
        for value in (self.policy_sha256, self.account_binding_sha256, self.evidence_sha256):
            _hash(value)
        _time(self.frozen_at)
        _amount(self.starting_nlv, positive=True)
        if any(type(value) is not bool for value in (
                self.flat_start, self.pre_entry, self.initial_exposure_reconciled)):
            _fail("BASELINE_CLAIM_INVALID")

    @property
    def identity_sha256(self) -> str:
        return _digest({
            "schema": STATE_SCHEMA, "policy": self.policy_sha256,
            "account": self.account_binding_sha256, "evidence": self.evidence_sha256,
            "frozen_at": self.frozen_at.astimezone(timezone.utc).isoformat(),
            "starting_nlv": _decimal_text(self.starting_nlv),
            "flat_start": self.flat_start, "pre_entry": self.pre_entry,
            "initial_exposure_reconciled": self.initial_exposure_reconciled,
        })


@dataclass(frozen=True)
class SessionTradingMeasurement:
    """Separate adapter claim, not a ShadowResult or authentication receipt.

    P&L already includes incurred reported fees exactly once. No current NLV,
    external-flow value, reqPnL realized value, or fee reserve substitutes for
    this metric. Future-entry/exit reserves are charged separately downstream.
    """
    model: str
    account_binding_sha256: str
    baseline_identity_sha256: str
    evidence_sha256: str
    as_of: datetime
    received_at: datetime
    session_pnl: Decimal | None
    complete: bool

    def __post_init__(self) -> None:
        for value in (self.account_binding_sha256, self.baseline_identity_sha256, self.evidence_sha256):
            _hash(value)
        for value in (self.as_of, self.received_at):
            _time(value)
        if self.model != MODEL or type(self.complete) is not bool:
            _fail("MEASUREMENT_CONTRACT_INVALID")
        if self.session_pnl is not None:
            _amount(self.session_pnl)
        if self.complete != (self.session_pnl is not None):
            _fail("MEASUREMENT_COMPLETENESS_INVALID")


class ObservationStatus(str, Enum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class ObservationIncident:
    token: str
    started_at: datetime
    status: ObservationStatus = ObservationStatus.PENDING
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.token, str) or _TOKEN.fullmatch(self.token) is None:
            _fail("TOKEN_INVALID")
        _time(self.started_at)
        if type(self.status) is not ObservationStatus:
            _fail("INCIDENT_STATUS_INVALID")
        allowed = {"MISSING_DATA", "GAP", "INCOMPLETE", "BINDING", "TIME", "NONMONOTONE"}
        if ((self.status is ObservationStatus.FAILED and self.reason not in allowed)
                or (self.status is not ObservationStatus.FAILED and self.reason is not None)):
            _fail("INCIDENT_REASON_INVALID")


@dataclass(frozen=True)
class SessionTradingState:
    baseline: SessionTradingBaseline
    incidents: tuple[ObservationIncident, ...] = ()
    last_measurement: SessionTradingMeasurement | None = None
    last_observation_token: str | None = None
    loss_latched: bool = False
    profit_aspiration_observed: bool = False

    def __post_init__(self) -> None:
        if (type(self.baseline) is not SessionTradingBaseline
                or type(self.incidents) is not tuple or len(self.incidents) > 100_000
                or any(type(item) is not ObservationIncident for item in self.incidents)
                or len({item.token for item in self.incidents}) != len(self.incidents)
                or type(self.loss_latched) is not bool or type(self.profit_aspiration_observed) is not bool
                or (self.last_measurement is not None and type(self.last_measurement) is not SessionTradingMeasurement)):
            _fail("STATE_INVALID")
        if not all((self.baseline.flat_start, self.baseline.pre_entry, self.baseline.initial_exposure_reconciled)):
            _fail("STATE_BASELINE_INVALID")
        measurement = self.last_measurement
        if measurement is None:
            if self.last_observation_token is not None:
                _fail("STATE_MEASUREMENT_INVALID")
        elif (not measurement.complete
                or measurement.baseline_identity_sha256 != self.baseline.identity_sha256
                or measurement.account_binding_sha256 != self.baseline.account_binding_sha256
                or not self.baseline.frozen_at <= measurement.as_of <= measurement.received_at
                or measurement.as_of.astimezone(_NY).date() != self.baseline.frozen_at.astimezone(_NY).date()
                or not any(item.token == self.last_observation_token
                           and item.status is ObservationStatus.COMPLETED
                           and item.started_at <= measurement.as_of for item in self.incidents)):
            _fail("STATE_MEASUREMENT_INVALID")
        if measurement is not None:
            with localcontext() as context:
                context.prec = 80
                if (measurement.session_pnl <= -(self.baseline.starting_nlv * Decimal("0.10"))
                        and not self.loss_latched):
                    _fail("STATE_LOSS_LATCH_INCONSISTENT")


def start_session(policy: SessionTradingPolicy, baseline: SessionTradingBaseline) -> SessionTradingState:
    """Construct initial state only; durable caller must reject same-day resets.

    A new policy/release identity is not a new account trading day. This pure
    constructor has no store and cannot determine whether that day exists.
    """
    if (type(policy) is not SessionTradingPolicy or type(baseline) is not SessionTradingBaseline
            or baseline.policy_sha256 != policy.policy_sha256
            or not all((baseline.flat_start, baseline.pre_entry, baseline.initial_exposure_reconciled))):
        _fail("BASELINE_UNACCEPTED")
    return SessionTradingState(baseline)


def begin_observation(state: SessionTradingState, *, token: str, now: datetime) -> SessionTradingState:
    """Caller must durably commit the returned pending marker before the read."""
    _time(now)
    baseline = state.baseline
    if (now < baseline.frozen_at or now.astimezone(_NY).date() != baseline.frozen_at.astimezone(_NY).date()
            or any(item.token == token or item.started_at > now for item in state.incidents)):
        _fail("OBSERVATION_BEGIN_INVALID")
    return replace(state, incidents=state.incidents + (ObservationIncident(token, now),))


def _pending(state: SessionTradingState, token: str) -> ObservationIncident:
    for item in state.incidents:
        if item.token == token and item.status is ObservationStatus.PENDING:
            return item
    _fail("PENDING_TOKEN_REQUIRED")


def fail_observation(state: SessionTradingState, *, token: str, gap: bool = False) -> SessionTradingState:
    """No automatic resolution API exists for a failed/gap incident."""
    if type(gap) is not bool:
        _fail("GAP_INVALID")
    return _finish(state, _pending(state, token), "GAP" if gap else "MISSING_DATA")


def _finish(state: SessionTradingState, pending: ObservationIncident, reason: str | None) -> SessionTradingState:
    item = replace(pending, status=ObservationStatus.FAILED if reason else ObservationStatus.COMPLETED, reason=reason)
    return replace(state, incidents=tuple(item if row.token == pending.token else row for row in state.incidents))


def complete_observation(
    state: SessionTradingState, *, token: str, measurement: SessionTradingMeasurement,
    now: datetime,
) -> SessionTradingState:
    """Resolve only this pending read; earlier unknown intervals stay blocked."""
    _time(now)
    pending = _pending(state, token)
    if type(measurement) is not SessionTradingMeasurement:
        return _finish(state, pending, "INCOMPLETE")
    baseline = state.baseline
    reason = None
    if (measurement.account_binding_sha256 != baseline.account_binding_sha256
            or measurement.baseline_identity_sha256 != baseline.identity_sha256):
        reason = "BINDING"
    elif (not baseline.frozen_at <= pending.started_at <= measurement.as_of <= measurement.received_at <= now
            or now.astimezone(_NY).date() != baseline.frozen_at.astimezone(_NY).date()
            or any(now - stamp > _MAX_AGE for stamp in (pending.started_at, measurement.as_of, measurement.received_at))):
        reason = "TIME"
    elif not measurement.complete:
        reason = "INCOMPLETE"
    elif state.last_measurement is not None and (
            measurement.as_of < state.last_measurement.as_of
            or (measurement.as_of == state.last_measurement.as_of and measurement != state.last_measurement)):
        reason = "NONMONOTONE"
    updated = _finish(state, pending, reason)
    if reason:
        return updated
    with localcontext() as context:
        context.prec = 80
        breached = measurement.session_pnl <= -(baseline.starting_nlv * Decimal("0.10"))
        aspiration = measurement.session_pnl >= baseline.starting_nlv * Decimal("0.15")
    return replace(updated, last_measurement=measurement, last_observation_token=token,
                   loss_latched=state.loss_latched or breached,
                   profit_aspiration_observed=state.profit_aspiration_observed or aspiration)


@dataclass(frozen=True)
class SessionTradingDecision:
    entry_blockers: tuple[str, ...]
    aggregate_headroom_before_exposure_and_reserves: Decimal
    guarded_closeout_required: bool
    profit_aspiration_observed: bool

    @property
    def live_authority(self) -> bool:
        return False


def evaluate_session_state(state: SessionTradingState, *, now: datetime) -> SessionTradingDecision:
    """A risk constraint only: even empty blockers NEVER authorize an order.

    Existing open/pending/uncovered/unresolved downside, positive execution and
    future commission reserves, cash/capacity and every other gate still apply.
    This does not deduct incurred fees a second time from the already-net P&L.
    """
    _time(now)
    blockers = set()
    if any(item.status is not ObservationStatus.COMPLETED for item in state.incidents):
        blockers.add("UNRESOLVED_OBSERVATION_INCIDENT")
    if state.loss_latched:
        blockers.add("SESSION_LOSS_LATCHED")
    if now.astimezone(_NY).date() != state.baseline.frozen_at.astimezone(_NY).date():
        blockers.add("SESSION_DATE_MISMATCH")
    measurement = state.last_measurement
    if measurement is None:
        blockers.add("SESSION_MEASUREMENT_MISSING")
    elif any(not timedelta(0) <= now - stamp <= _MAX_AGE
             for stamp in (measurement.as_of, measurement.received_at)):
        blockers.add("SESSION_MEASUREMENT_STALE")
    with localcontext() as context:
        context.prec = 80
        budget = state.baseline.starting_nlv * Decimal("0.10")
        capacity = Decimal(0) if blockers else max(Decimal(0), min(budget, budget + measurement.session_pnl))
    return SessionTradingDecision(tuple(sorted(blockers)), capacity,
                                 state.loss_latched, state.profit_aspiration_observed)
