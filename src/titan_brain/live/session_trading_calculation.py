"""Pure session trading-P&L arithmetic over concrete finite IBKR observations.

This implements the approved formula, not new broker authentication or trading
authority.  Finite reads cannot establish complete all-client/continuous history
or account-adjustment reconciliation.  Those limitations are unconditional even
if a caller removes an observation's blocker strings.  Available arithmetic is
therefore distinct from a complete session measurement; an empty observed ledger
can total zero without establishing that the actual account's session P&L is zero.

No runtime, disk, credential, policy mutation or order operations occur here.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
import hashlib
import json
import re
from zoneinfo import ZoneInfo

from .broker.ibkr_session_inputs import (
    IbkrFiniteSessionFacts,
    SessionExecutionFact,
    SessionInputObservation,
    SessionOrderFact,
    SessionPositionFact,
)
from .models import BrokerOrderState
from .session_trading_policy import (
    MODEL,
    SessionTradingBaseline,
    SessionTradingMeasurement,
    SessionTradingPolicy,
)


_NY = ZoneInfo("America/New_York")
_HASH = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,160}\Z", re.ASCII)
_MAX_AGE = timedelta(seconds=5)
_FINITE_ENDS = ("account_updates_multi", "completed_orders", "executions", "open_orders", "positions")
_SOURCE_BLOCKERS = (
    "ALL_CLIENT_EXECUTION_COVERAGE_UNPROVEN",
    "CONTINUOUS_EVENT_COVERAGE_UNPROVEN",
    "EXTERNAL_ADJUSTMENT_RECONCILIATION_UNAVAILABLE",
)


class SessionTradingCalculationError(ValueError):
    """Static input error: never includes private input values."""


def _fail(reason: str) -> None:
    raise SessionTradingCalculationError("SESSION_CALCULATION_" + reason)


def _hash(value: object) -> None:
    if type(value) is not str or _HASH.fullmatch(value) is None:
        _fail("HASH_INVALID")


def _token(value: object) -> None:
    if type(value) is not str or _TOKEN.fullmatch(value) is None:
        _fail("IDENTIFIER_INVALID")


def _utc(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _fail("TIME_INVALID")
    return value.astimezone(timezone.utc)


def _amount(value: object, *, positive: bool = False) -> None:
    if (type(value) is not Decimal or not value.is_finite()
            or len(value.as_tuple().digits) > 30 or value.as_tuple().exponent < -12
            or value.adjusted() > 18 or (positive and value <= 0)):
        _fail("AMOUNT_INVALID")


def _integer(value: object, *, minimum: int = 0) -> None:
    if type(value) is not int or not minimum <= value <= 2**63 - 1:
        _fail("INTEGER_INVALID")


def _rows(value: object, kind: type) -> None:
    if type(value) is not tuple or len(value) > 100_000 or any(type(item) is not kind for item in value):
        _fail("ROWS_INVALID")


def _text_decimal(value: Decimal) -> str:
    if value == 0:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _plain(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _plain(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, datetime):
        return _utc(value).isoformat()
    if type(value) is Decimal:
        _amount(value)
        return _text_decimal(value)
    if type(value) is tuple:
        return [_plain(item) for item in value]
    if value is None or type(value) in (str, int, bool):
        return value
    _fail("EVIDENCE_VALUE_INVALID")


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(_plain(value), sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SessionBidMark:
    account_binding_sha256: str = field(repr=False)
    contract_id: int
    currency: str
    bid: Decimal
    bid_size: int
    quoted_at: datetime
    received_at: datetime
    evidence_sha256: str

    def __post_init__(self) -> None:
        _hash(self.account_binding_sha256)
        _hash(self.evidence_sha256)
        _integer(self.contract_id, minimum=1)
        _integer(self.bid_size)
        _token(self.currency)
        _amount(self.bid, positive=True)
        _utc(self.quoted_at)
        _utc(self.received_at)


@dataclass(frozen=True)
class SessionTradingCalculation:
    account_binding_sha256: str = field(repr=False)
    baseline_identity_sha256: str
    evidence_sha256: str
    as_of: datetime
    received_at: datetime
    starting_nlv: Decimal
    calculated_pnl: Decimal | None
    performance_fraction: Decimal | None
    loss_boundary_observed: bool
    material_blockers: tuple[str, ...]
    source_blockers: tuple[str, ...]

    @property
    def diagnostic_only(self) -> bool:
        return True

    @property
    def live_authority(self) -> bool:
        return False

    @property
    def blockers(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.material_blockers) | set(_SOURCE_BLOCKERS) | set(self.source_blockers)))

    def to_incomplete_measurement(self) -> SessionTradingMeasurement:
        """Never promote observed-record arithmetic to complete risk evidence."""
        return SessionTradingMeasurement(
            model=MODEL,
            account_binding_sha256=self.account_binding_sha256,
            baseline_identity_sha256=self.baseline_identity_sha256,
            evidence_sha256=self.evidence_sha256,
            as_of=self.as_of,
            received_at=self.received_at,
            session_pnl=None,
            complete=False,
        )

    def public_dict(self) -> dict[str, object]:
        return {
            "model": MODEL,
            "diagnostic_only": True,
            "live_authority": False,
            "full_session_pnl_established": False,
            "measurement_scope": "OBSERVED_FINITE_RECORD_ARITHMETIC_ONLY",
            "timing_basis": "LOCAL_CALCULATION_TIME_NOT_ATOMIC_PROVIDER_SNAPSHOT",
            "arithmetic_available": self.calculated_pnl is not None,
            "calculated_pnl": None if self.calculated_pnl is None else str(self.calculated_pnl),
            "performance_fraction": None if self.performance_fraction is None else str(self.performance_fraction),
            "loss_boundary_observed": self.loss_boundary_observed,
            "loss_latch_persisted": False,
            "unobserved_breach_exclusion_established": False,
            "as_of": self.as_of.isoformat(),
            "received_at": self.received_at.isoformat(),
            "evidence_sha256": self.evidence_sha256,
            "material_blockers": self.material_blockers,
            "source_blockers": _SOURCE_BLOCKERS,
            "blockers": self.blockers,
        }


def _validate_observation(value: SessionInputObservation) -> None:
    if type(value) is not SessionInputObservation or type(value.facts) is not IbkrFiniteSessionFacts:
        _fail("OBSERVATION_TYPE_INVALID")
    _hash(value.account_binding_fingerprint)
    _integer(value.read_client_id)
    if value.prior_collection_id is not None:
        _hash(value.prior_collection_id)
    if type(value.sticky_read_gap) is not bool or type(value.unobserved_interval_since_prior_collection) is not bool:
        _fail("OBSERVATION_STATUS_INVALID")
    _rows(value.blockers, str)
    facts = value.facts
    _hash(facts.collection_id)
    _integer(facts.generation, minimum=1)
    for stamp in (facts.collection_started_at, facts.collection_completed_at, facts.net_liquidation_received_at):
        _utc(stamp)
    _amount(facts.net_liquidation)
    if type(facts.account_values_source) is not str:
        _fail("ACCOUNT_VALUES_SOURCE_INVALID")
    if facts.cash_value is not None:
        _amount(facts.cash_value)
    if facts.cash_currency is not None and (type(facts.cash_currency) is not str or len(facts.cash_currency) > 16):
        _fail("CURRENCY_INVALID")
    if facts.cash_received_at is not None:
        _utc(facts.cash_received_at)
    if type(facts.net_liquidation_currency) is not str or len(facts.net_liquidation_currency) > 16:
        _fail("CURRENCY_INVALID")
    _rows(facts.positions, SessionPositionFact)
    _rows(facts.executions, SessionExecutionFact)
    _rows(facts.orders, SessionOrderFact)
    _rows(facts.completed_reads, str)
    if type(facts.commission_conflict_observed) is not bool:
        _fail("COMMISSION_STATUS_INVALID")
    _integer(facts.orphan_commission_report_count)
    for position in facts.positions:
        if position.contract_id is not None:
            _integer(position.contract_id, minimum=1)
        _token(position.symbol)
        _token(position.security_type)
        if type(position.currency) is not str or len(position.currency) > 16:
            _fail("CURRENCY_INVALID")
        _amount(position.quantity)
        _utc(position.received_at)
    for execution in facts.executions:
        _token(execution.exec_id)
        if execution.contract_id is not None:
            _integer(execution.contract_id, minimum=1)
        for text in (execution.symbol, execution.security_type, execution.side, execution.source_time_basis):
            _token(text)
        for currency in (execution.currency, execution.commission_currency):
            if type(currency) is not str or len(currency) > 16:
                _fail("CURRENCY_INVALID")
        _amount(execution.quantity)
        _amount(execution.price, positive=True)
        _amount(execution.commission)
        if execution.source_executed_at is not None:
            _utc(execution.source_executed_at)
        _utc(execution.received_at)
        _utc(execution.commission_received_at)
    for order in facts.orders:
        _token(order.order_identity)
        _token(order.family)
        _token(order.state)
        if type(order.terminal_observed) is not bool or type(order.blocking_warning_present) is not bool:
            _fail("ORDER_STATUS_INVALID")


def session_baseline_evidence_sha256(first: SessionInputObservation, second: SessionInputObservation) -> str:
    """Bind two exact input records; a hash does not establish their authority."""
    _validate_observation(first)
    _validate_observation(second)
    return _digest(("session-trading-flat-pre-entry-evidence-v1", first, second))


def _execution_key(item: SessionExecutionFact) -> tuple[object, ...]:
    # Replay receipt times may differ; economics/provider time must not.
    return (
        item.contract_id, item.symbol, item.security_type, item.currency,
        item.side, item.quantity, item.price, item.source_executed_at,
        item.source_time_basis, item.commission, item.commission_currency,
    )


def _record_problems(value: SessionInputObservation, blockers: set[str]) -> None:
    facts = value.facts
    if facts.completed_reads != _FINITE_ENDS:
        blockers.add("FINITE_CALLBACK_SET_INCOMPLETE")
    if facts.account_values_source != "IBKR_ACCOUNT_UPDATES_MULTI_V1":
        blockers.add("ACCOUNT_VALUES_SOURCE_UNACCEPTED")
    if not facts.collection_started_at <= facts.net_liquidation_received_at <= facts.collection_completed_at:
        blockers.add("COLLECTION_TIME_INVALID")
    if value.sticky_read_gap:
        blockers.add("READ_GAP_REQUIRES_RECONCILIATION")
    if set(value.blockers) - set(_SOURCE_BLOCKERS):
        blockers.add("INPUT_ADAPTER_REPORTED_BLOCKERS")
    if facts.commission_conflict_observed or facts.orphan_commission_report_count:
        blockers.add("COMMISSION_RECONCILIATION_UNRESOLVED")
    for order in facts.orders:
        if order.blocking_warning_present:
            blockers.add("ORDER_WARNING_UNRESOLVED")
        try:
            state = BrokerOrderState(order.state)
        except ValueError:
            blockers.add("ORDER_STATE_UNKNOWN")
            continue
        if state is BrokerOrderState.UNKNOWN or state.terminal != order.terminal_observed:
            blockers.add("ORDER_STATE_UNKNOWN")


def validate_session_baseline_observations(
    first: SessionInputObservation, second: SessionInputObservation, *, now: datetime,
) -> None:
    """Validate local candidate preconditions before a diagnostic store write.

    Tolerates only the explicit finite-source limitations, which remain
    unresolved. Successful return is NOT baseline authentication or authority.
    The approved pre-entry baseline uses the original fresh callback receipt;
    it does not claim a provider economic timestamp or atomic current NLV.
    """
    _utc(now)
    _validate_observation(first)
    _validate_observation(second)
    problems: set[str] = set()
    _record_problems(first, problems)
    _record_problems(second, problems)
    left, right = first.facts, second.facts
    if (first.account_binding_fingerprint != second.account_binding_fingerprint
            or first.read_client_id != second.read_client_id or left.generation != right.generation
            or left.collection_id == right.collection_id or second.prior_collection_id != left.collection_id
            or not left.collection_completed_at <= right.collection_started_at <= right.collection_completed_at <= now
            or left.collection_started_at.astimezone(_NY).date() != now.astimezone(_NY).date()
            or right.collection_completed_at.astimezone(_NY).date() != now.astimezone(_NY).date()
            or right.collection_completed_at - left.collection_started_at > timedelta(seconds=30)
            or any(not timedelta(0) <= now - stamp <= _MAX_AGE for stamp in (right.collection_completed_at, right.net_liquidation_received_at))
            or left.net_liquidation_currency != "USD" or right.net_liquidation_currency != "USD"
            or left.net_liquidation != right.net_liquidation or right.net_liquidation <= 0
            or left.positions or right.positions or left.executions or right.executions
            or any(not item.terminal_observed for facts in (left, right) for item in facts.orders)):
        problems.add("BASELINE_OBSERVATIONS_UNACCEPTED")
    if problems:
        _fail("BASELINE_OBSERVATIONS_UNACCEPTED")


def calculate_session_trading_pnl(
    policy: SessionTradingPolicy,
    baseline: SessionTradingBaseline,
    *,
    baseline_observations: tuple[SessionInputObservation, SessionInputObservation],
    observation: SessionInputObservation,
    bid_marks: tuple[SessionBidMark, ...] = (),
    now: datetime,
    prior_observations: tuple[SessionInputObservation, ...] = (),
) -> SessionTradingCalculation:
    """Calculate only what explicit finite records support; never completeness.

    Baseline booleans alone are insufficient: its amount/time/account/evidence
    must match two stable flat input observations. Optional cumulative prior
    observations detect omissions/conflicts; their absence is not proof of
    continuous coverage, which remains unconditionally unestablished.
    """
    if type(policy) is not SessionTradingPolicy or type(baseline) is not SessionTradingBaseline:
        _fail("POLICY_BASELINE_TYPE_INVALID")
    _utc(now)
    _rows(baseline_observations, SessionInputObservation)
    if len(baseline_observations) != 2:
        _fail("TWO_BASELINE_OBSERVATIONS_REQUIRED")
    _rows(prior_observations, SessionInputObservation)
    _rows(bid_marks, SessionBidMark)
    for value in (*baseline_observations, *prior_observations, observation):
        _validate_observation(value)
    first, second = baseline_observations
    left, right, current = first.facts, second.facts, observation.facts
    material: set[str] = set()
    all_observations = (*baseline_observations, *prior_observations, observation)
    if baseline.policy_sha256 != policy.policy_sha256:
        material.add("BASELINE_POLICY_BINDING_MISMATCH")
    if baseline.evidence_sha256 != session_baseline_evidence_sha256(first, second):
        material.add("BASELINE_EVIDENCE_BINDING_MISMATCH")
    if not all((baseline.flat_start, baseline.pre_entry, baseline.initial_exposure_reconciled)):
        material.add("BASELINE_PRECONDITIONS_UNPROVEN")
    if baseline.frozen_at != right.collection_completed_at or baseline.starting_nlv != right.net_liquidation:
        material.add("BASELINE_AMOUNT_OR_FREEZE_TIME_MISMATCH")
    if left.net_liquidation != right.net_liquidation or left.net_liquidation_currency != "USD" or right.net_liquidation_currency != "USD":
        material.add("BASELINE_STABLE_USD_NLV_UNPROVEN")
    if left.net_liquidation <= 0 or right.net_liquidation <= 0:
        material.add("BASELINE_POSITIVE_NLV_UNPROVEN")
    if right.collection_completed_at - left.collection_started_at > timedelta(seconds=30):
        material.add("BASELINE_CAPTURE_WINDOW_TOO_WIDE")
    if not timedelta(0) <= baseline.frozen_at - right.net_liquidation_received_at <= _MAX_AGE:
        material.add("BASELINE_NLV_LOCAL_RECEIPT_STALE")
    if left.positions or right.positions or left.executions or right.executions:
        material.add("BASELINE_FLAT_PRE_ENTRY_NOT_OBSERVED")
    if any(not order.terminal_observed for value in (left, right) for order in value.orders):
        material.add("BASELINE_UNRESOLVED_ORDER_EXPOSURE")
    day = baseline.frozen_at.astimezone(_NY).date()
    last = None
    collections: set[str] = set()
    for value in all_observations:
        facts = value.facts
        _record_problems(value, material)
        if value.account_binding_fingerprint != baseline.account_binding_sha256:
            material.add("ACCOUNT_BINDING_MISMATCH")
        if facts.generation != right.generation:
            material.add("READ_GENERATION_CHANGED")
        if value.read_client_id != second.read_client_id:
            material.add("READ_CLIENT_CHANGED")
        if facts.collection_id in collections:
            material.add("COLLECTION_REPLAY_COLLISION")
        collections.add(facts.collection_id)
        if any(stamp.astimezone(_NY).date() != day for stamp in (facts.collection_started_at, facts.collection_completed_at)):
            material.add("SESSION_DAY_MISMATCH")
        if facts.collection_completed_at > now:
            material.add("OBSERVATION_FROM_FUTURE")
        if last is not None:
            if facts.collection_started_at < last.facts.collection_completed_at:
                material.add("OBSERVATION_TIME_REGRESSED")
            if value.prior_collection_id != last.facts.collection_id:
                material.add("OBSERVATION_LINEAGE_MISMATCH")
        last = value
    if now.astimezone(_NY).date() != day:
        material.add("SESSION_DAY_MISMATCH")
    if any(not timedelta(0) <= now - stamp <= _MAX_AGE for stamp in (current.collection_started_at, current.collection_completed_at)):
        material.add("CURRENT_OBSERVATION_STALE_OR_FUTURE")

    history: dict[str, tuple[object, ...]] = {}
    current_executions: dict[str, SessionExecutionFact] = {}
    families: dict[str, str] = {}
    for value in (*prior_observations, observation):
        current_executions = {}
        for execution in value.facts.executions:
            key = _execution_key(execution)
            if execution.exec_id in current_executions and _execution_key(current_executions[execution.exec_id]) != key:
                material.add("EXECUTION_REPLAY_CONFLICT")
            else:
                current_executions[execution.exec_id] = execution
            if execution.exec_id in history and history[execution.exec_id] != key:
                material.add("EXECUTION_HISTORY_CONFLICT")
            parts = execution.exec_id.split(".")
            if len(parts) == 4 and parts[-1].isascii() and parts[-1].isdigit():
                family = ".".join(parts[:-1])
                if family in families and families[family] != execution.exec_id:
                    material.add("EXECUTION_CORRECTION_UNRESOLVED")
                families[family] = execution.exec_id
        if history.keys() - current_executions.keys():
            material.add("EXECUTION_HISTORY_OMITTED")
        for identity, execution in current_executions.items():
            history.setdefault(identity, _execution_key(execution))

    quantities: dict[int, int] = {}
    positions: dict[int, int] = {}
    contract_facts: dict[int, tuple[str, str, str]] = {}
    marks: dict[int, SessionBidMark] = {}
    for mark in bid_marks:
        if mark.contract_id in marks and marks[mark.contract_id] != mark:
            material.add("BID_MARK_CONFLICT")
        marks[mark.contract_id] = mark
        if mark.account_binding_sha256 != baseline.account_binding_sha256 or mark.currency != "USD":
            material.add("BID_MARK_SCOPE_MISMATCH")
        if not mark.quoted_at <= mark.received_at <= now or now - mark.quoted_at > _MAX_AGE:
            material.add("BID_MARK_STALE_OR_FUTURE")
        if mark.quoted_at < baseline.frozen_at:
            material.add("BID_MARK_PRECEDES_BASELINE")
    for position in current.positions:
        if (position.contract_id is None or position.security_type != "STK" or position.currency != "USD"
                or position.quantity < 0 or position.quantity != position.quantity.to_integral_value()
                or position.quantity > 2_147_483_647):
            material.add("POSITION_SCOPE_INVALID")
            continue
        if position.contract_id in positions:
            material.add("POSITION_IDENTITY_DUPLICATE")
        positions[position.contract_id] = int(position.quantity)
        contract_facts[position.contract_id] = (position.symbol, position.security_type, position.currency)
        if not current.collection_started_at <= position.received_at <= current.collection_completed_at:
            material.add("POSITION_RECEIPT_OUTSIDE_COLLECTION")
    with localcontext() as context:
        context.prec = 80
        cash = Decimal(0)
        ordered = sorted(current_executions.values(), key=lambda value: (value.source_executed_at or current.collection_completed_at, value.exec_id))
        for execution in ordered:
            if (execution.contract_id is None or execution.security_type != "STK" or execution.currency != "USD"
                    or execution.side not in ("BUY", "SELL") or execution.quantity <= 0
                    or execution.quantity != execution.quantity.to_integral_value()
                    or execution.quantity > 2_147_483_647):
                material.add("EXECUTION_SCOPE_INVALID")
                continue
            if execution.source_time_basis != "PROVIDER_EXPLICIT_ZONE" or execution.source_executed_at is None:
                material.add("EXECUTION_TIME_PROVENANCE_UNRESOLVED")
            elif not baseline.frozen_at <= execution.source_executed_at <= current.collection_completed_at or execution.source_executed_at.astimezone(_NY).date() != day:
                material.add("EXECUTION_OUTSIDE_BASELINE_SESSION")
            elif execution.source_executed_at > min(execution.received_at, execution.commission_received_at):
                material.add("EXECUTION_PROVIDER_TIME_FOLLOWS_RECEIPT")
            identity = (execution.symbol, execution.security_type, execution.currency)
            if execution.contract_id in contract_facts and contract_facts[execution.contract_id] != identity:
                material.add("CONTRACT_FACTS_CONFLICT")
            contract_facts[execution.contract_id] = identity
            if execution.commission_currency != "USD":
                material.add("ACTUAL_COMMISSION_CURRENCY_UNPROVEN")
            if not current.collection_started_at <= execution.received_at <= current.collection_completed_at or not current.collection_started_at <= execution.commission_received_at <= current.collection_completed_at:
                material.add("EXECUTION_OR_COMMISSION_RECEIPT_OUTSIDE_COLLECTION")
            quantity = int(execution.quantity)
            sign = 1 if execution.side == "BUY" else -1
            quantities[execution.contract_id] = quantities.get(execution.contract_id, 0) + sign * quantity
            if not 0 <= quantities[execution.contract_id] <= 2_147_483_647:
                material.add("SHORT_OR_EXECUTION_CHRONOLOGY_UNRESOLVED")
            cash -= sign * quantity * execution.price
            cash -= execution.commission
        expected = {identity: quantity for identity, quantity in quantities.items() if quantity}
        actual = {identity: quantity for identity, quantity in positions.items() if quantity}
        if expected != actual:
            material.add("POSITIONS_DO_NOT_MATCH_SESSION_EXECUTIONS")
        for identity, quantity in expected.items():
            mark = marks.get(identity)
            if mark is None:
                material.add("RESIDUAL_POSITION_BID_MISSING")
                continue
            if mark.bid_size < quantity:
                material.add("RESIDUAL_POSITION_BID_SIZE_INSUFFICIENT")
            cash += quantity * mark.bid
        pnl = None if material else cash
        fraction = None if pnl is None else pnl / baseline.starting_nlv
        breach = pnl is not None and pnl <= -baseline.starting_nlv * Decimal("0.10")
    evidence = _digest(("session-trading-observed-calculation-v1", policy, baseline, all_observations, bid_marks, now))
    return SessionTradingCalculation(
        account_binding_sha256=baseline.account_binding_sha256,
        baseline_identity_sha256=baseline.identity_sha256,
        evidence_sha256=evidence,
        as_of=now,
        received_at=now,
        starting_nlv=baseline.starting_nlv,
        calculated_pnl=pnl,
        performance_fraction=fraction,
        loss_boundary_observed=breach,
        material_blockers=tuple(sorted(material)),
        source_blockers=_SOURCE_BLOCKERS,
    )
