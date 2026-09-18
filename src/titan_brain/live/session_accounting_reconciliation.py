"""Independent observed-cash identity, never session-P&L authority.

Actual closing cash is compared with actual opening cash, unique observed
execution cash flows, signed actual commissions, and explicitly supplied known
non-trading adjustments. NLV is deliberately NOT used. A matching identity does
not establish exhaustive history, simultaneous valuation, or authenticated input
custody; offsetting omissions can match. No storage, broker calls, or orders.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
import hashlib
import json
import re
from zoneinfo import ZoneInfo

from .broker.ibkr_session_inputs import (
    IbkrFiniteSessionFacts, SessionExecutionFact, SessionInputObservation,
    SessionOrderFact, SessionPositionFact,
)
from .models import BrokerOrderState


_HASH = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,160}\Z", re.ASCII)
_NY = ZoneInfo("America/New_York")
_ENDS = ("account_updates_multi", "completed_orders", "executions", "open_orders", "positions")
_SOURCE = "IBKR_ACCOUNT_UPDATES_MULTI_V1"
_SOURCE_BLOCKERS = (
    "ACCOUNT_VALUES_ECONOMIC_TIME_UNAVAILABLE", "ALL_CLIENT_EXECUTION_COVERAGE_UNPROVEN",
    "CONTINUOUS_EVENT_COVERAGE_UNPROVEN", "NONTRADING_ADJUSTMENT_COVERAGE_UNPROVEN",
)
_ADAPTER_LIMITS = frozenset({
    "ACCOUNT_VALUES_ECONOMIC_TIME_UNAVAILABLE", "ALL_CLIENT_EXECUTION_COVERAGE_UNPROVEN",
    "CONTINUOUS_EVENT_COVERAGE_UNPROVEN", "EXTERNAL_ADJUSTMENT_RECONCILIATION_UNAVAILABLE",
})
_KINDS = frozenset({"EXTERNAL_CASH_MOVEMENT", "DIVIDEND", "INTEREST", "WITHHOLDING_TAX", "NONTRADING_FEE"})


class SessionAccountingError(ValueError):
    """Fixed machine code only, never values or provider text."""


def _fail(code: str) -> None:
    raise SessionAccountingError("SESSION_ACCOUNTING_" + code)


def _time(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        _fail("TIME_INVALID")
    return value.astimezone(timezone.utc)


def _amount(value: object) -> Decimal:
    if (type(value) is not Decimal or not value.is_finite()
            or len(value.as_tuple().digits) > 30 or value.as_tuple().exponent < -12
            or value.adjusted() > 18):
        _fail("AMOUNT_INVALID")
    return value


def _identifier(value: object, *, digest: bool = False) -> None:
    if type(value) is not str or (_HASH if digest else _ID).fullmatch(value) is None:
        _fail("IDENTIFIER_INVALID")


def _rows(value: object, kind: type) -> None:
    if type(value) is not tuple or len(value) > 100_000 or any(type(item) is not kind for item in value):
        _fail("ROWS_INVALID")


@dataclass(frozen=True)
class SessionNonTradingAdjustment:
    """Caller-supplied known evidence, NOT authentication or completeness.

    Positive amount adds USD cash; negative removes it. This classification is
    explicit input, never inferred from a residual, and never counted as trading
    P&L. Source evidence and stable identity are required to deduplicate rows.
    """

    adjustment_id: str = field(repr=False)
    account_binding_sha256: str = field(repr=False)
    kind: str
    amount: Decimal = field(repr=False)
    currency: str
    effective_at: datetime
    received_at: datetime
    source_evidence_sha256: str

    def __post_init__(self) -> None:
        _identifier(self.adjustment_id)
        _identifier(self.account_binding_sha256, digest=True)
        _identifier(self.source_evidence_sha256, digest=True)
        _amount(self.amount)
        if type(self.kind) is not str or self.kind not in _KINDS:
            _fail("ADJUSTMENT_KIND_INVALID")
        if self.currency != "USD" or type(self.currency) is not str:
            _fail("ADJUSTMENT_CURRENCY_INVALID")
        if _time(self.effective_at) > _time(self.received_at):
            _fail("ADJUSTMENT_TIME_INVALID")


@dataclass(frozen=True)
class SessionAccountingReconciliation:
    account_binding_sha256: str = field(repr=False)
    baseline_collection_id: str
    current_collection_id: str
    evidence_sha256: str
    as_of: datetime
    baseline_cash: Decimal | None = field(repr=False)
    current_cash: Decimal | None = field(repr=False)
    gross_execution_cash_flow: Decimal | None = field(repr=False)
    actual_signed_commissions: Decimal | None = field(repr=False)
    trading_cash_flow: Decimal | None = field(repr=False)
    known_nontrading_cash_flow: Decimal | None = field(repr=False)
    expected_cash: Decimal | None = field(repr=False)
    unexplained_cash_residual: Decimal | None = field(repr=False)
    execution_count: int
    adjustment_count: int
    material_blockers: tuple[str, ...]

    @property
    def source_blockers(self) -> tuple[str, ...]:
        return _SOURCE_BLOCKERS

    @property
    def observed_cash_identity_matched(self) -> bool:
        return not self.material_blockers and self.unexplained_cash_residual == Decimal(0)

    def public_dict(self) -> dict[str, object]:
        # Financial amounts and provider identities remain in the typed local
        # result. Even a zero residual grants no authority or source coverage.
        return {
            "schema_version": "titan_session_accounting_reconciliation_2026-09-18_v1",
            "diagnostic_only": True, "live_authority": False,
            "session_measurement_authority": False, "whole_account_coverage_verified": False,
            "observed_cash_identity_matched": self.observed_cash_identity_matched,
            "arithmetic_available": self.unexplained_cash_residual is not None,
            "unexplained_cash_residual_present": self.unexplained_cash_residual is not None and self.unexplained_cash_residual != 0,
            "execution_count": self.execution_count, "adjustment_count": self.adjustment_count,
            "evidence_sha256": self.evidence_sha256, "as_of": self.as_of.isoformat(),
            "material_blockers": self.material_blockers, "source_blockers": self.source_blockers,
        }


def _execution_record(row: SessionExecutionFact) -> tuple:
    """Economic identity excludes changing receipt times of repeated callbacks."""
    return (row.contract_id, row.symbol, row.security_type, row.currency, row.side,
            row.quantity, row.price, row.source_executed_at, row.source_time_basis,
            row.commission, row.commission_currency)


def _plain(value: object) -> object:
    if type(value) is Decimal:
        return format(_amount(value), "f")
    if type(value) is datetime:
        return _time(value).isoformat()
    if type(value) in (tuple, list):
        return [_plain(item) for item in value]
    if type(value) is dict:
        return {key: _plain(item) for key, item in value.items()}
    if value is None or type(value) in (str, int, bool):
        return value
    _fail("EVIDENCE_TYPE_INVALID")


def reconcile_session_accounting(
    *, baseline_observations: tuple[SessionInputObservation, SessionInputObservation],
    observation: SessionInputObservation, now: datetime,
    prior_observations: tuple[SessionInputObservation, ...] = (),
    nontrading_adjustments: tuple[SessionNonTradingAdjustment, ...] = (),
) -> SessionAccountingReconciliation:
    """Cash residual from actual fields, without NLV or a residual-as-flow guess.

    Repeated identical executions/adjustments count once. Missing historical
    executions, corrections, uncertain scope/time/currency and unknown actual
    fees block arithmetic. A nonzero calculable residual is retained explicitly.
    """
    now = _time(now)
    _rows(baseline_observations, SessionInputObservation)
    _rows(prior_observations, SessionInputObservation)
    _rows(nontrading_adjustments, SessionNonTradingAdjustment)
    if len(baseline_observations) != 2:
        _fail("TWO_BASELINE_OBSERVATIONS_REQUIRED")
    if type(observation) is not SessionInputObservation:
        _fail("OBSERVATION_TYPE_INVALID")
    first, second = baseline_observations
    account = second.account_binding_fingerprint
    _identifier(account, digest=True)
    material: set[str] = set()
    observations = (*baseline_observations, *prior_observations, observation)
    if any(type(item.facts) is not IbkrFiniteSessionFacts for item in observations):
        _fail("FACTS_TYPE_INVALID")
    cash_records, execution_records, observation_scopes, exposure_records = [], [], [], []
    history: dict[str, SessionExecutionFact] = {}
    families: dict[str, str] = {}
    collections: set[str] = set()
    previous = None
    for index, item in enumerate(observations):
        if type(item.facts) is not IbkrFiniteSessionFacts:
            _fail("FACTS_TYPE_INVALID")
        facts = item.facts
        _identifier(item.account_binding_fingerprint, digest=True)
        _identifier(facts.collection_id, digest=True)
        start, end = _time(facts.collection_started_at), _time(facts.collection_completed_at)
        if type(item.read_client_id) is not int or item.read_client_id < 0 or type(facts.generation) is not int or facts.generation < 1:
            _fail("READ_IDENTITY_INVALID")
        if facts.completed_reads != _ENDS or facts.account_values_source != _SOURCE:
            material.add("ACCOUNT_VALUES_SOURCE_OR_COMPLETION_UNACCEPTED")
        if item.account_binding_fingerprint != account or item.read_client_id != second.read_client_id or facts.generation != second.facts.generation:
            material.add("OBSERVATION_SCOPE_CHANGED")
        if start > end or end > now:
            material.add("OBSERVATION_TIME_INVALID")
        if facts.collection_id in collections:
            material.add("OBSERVATION_ID_REUSED")
        collections.add(facts.collection_id)
        if previous is not None and (start < previous.facts.collection_completed_at or item.prior_collection_id != previous.facts.collection_id):
            material.add("OBSERVATION_LINEAGE_UNRESOLVED")
        previous = item
        if (type(item.sticky_read_gap) is not bool or type(item.unobserved_interval_since_prior_collection) is not bool
                or type(item.blockers) is not tuple or any(type(reason) is not str for reason in item.blockers)):
            _fail("OBSERVATION_STATUS_INVALID")
        if item.sticky_read_gap or set(item.blockers) - _ADAPTER_LIMITS:
            material.add("INPUT_OBSERVATION_REQUIRES_RECONCILIATION")
        if type(facts.commission_conflict_observed) is not bool or type(facts.orphan_commission_report_count) is not int or facts.orphan_commission_report_count < 0:
            _fail("COMMISSION_STATUS_INVALID")
        if facts.commission_conflict_observed or facts.orphan_commission_report_count:
            material.add("ACTUAL_COMMISSION_RECONCILIATION_UNRESOLVED")
        if facts.cash_value is None or facts.cash_currency is None or facts.cash_received_at is None:
            material.add("ACTUAL_CASH_VALUE_MISSING")
        else:
            _amount(facts.cash_value)
            received = _time(facts.cash_received_at)
            if facts.cash_currency != "USD" or type(facts.cash_currency) is not str:
                material.add("ACTUAL_CASH_USD_UNPROVEN")
            if not start <= received <= end:
                material.add("ACTUAL_CASH_RECEIPT_OUTSIDE_COLLECTION")
        cash_records.append((facts.collection_id, start, end, facts.cash_value, facts.cash_currency, facts.cash_received_at))
        observation_scopes.append((
            facts.collection_id, item.account_binding_fingerprint, item.read_client_id, facts.generation,
            item.prior_collection_id, item.sticky_read_gap, item.unobserved_interval_since_prior_collection,
            item.blockers, facts.account_values_source, facts.completed_reads,
            facts.commission_conflict_observed, facts.orphan_commission_report_count,
        ))
        _rows(facts.executions, SessionExecutionFact)
        _rows(facts.positions, SessionPositionFact)
        _rows(facts.orders, SessionOrderFact)
        exposure_records.append((facts.collection_id, tuple(vars(row) for row in facts.positions), tuple(vars(row) for row in facts.orders)))
        if any(type(row.terminal_observed) is not bool or type(row.blocking_warning_present) is not bool for row in facts.orders):
            _fail("ORDER_STATUS_INVALID")
        if any(row.blocking_warning_present for row in facts.orders):
            material.add("ORDER_WARNING_REQUIRES_RECONCILIATION")
        for row in facts.orders:
            try:
                state = BrokerOrderState(row.state)
            except (ValueError, TypeError):
                material.add("ORDER_STATE_UNKNOWN")
                continue
            if state is BrokerOrderState.UNKNOWN or state.terminal != row.terminal_observed:
                material.add("ORDER_STATE_UNKNOWN")
        current: dict[str, SessionExecutionFact] = {}
        for row in facts.executions:
            _identifier(row.exec_id)
            for amount in (row.quantity, row.price, row.commission):
                _amount(amount)
            received, fee_received = _time(row.received_at), _time(row.commission_received_at)
            if (type(row.contract_id) is not int or not 1 <= row.contract_id <= 2**63 - 1
                    or type(row.symbol) is not str or _ID.fullmatch(row.symbol) is None
                    or row.security_type != "STK" or row.currency != "USD" or row.side not in ("BUY", "SELL")
                    or not 0 < row.quantity <= 2_147_483_647 or row.quantity != row.quantity.to_integral_value() or row.price <= 0):
                material.add("EXECUTION_SCOPE_UNACCEPTED")
            if row.commission_currency != "USD":
                material.add("ACTUAL_COMMISSION_USD_UNPROVEN")
            if not start <= received <= end or not start <= fee_received <= end:
                material.add("EXECUTION_OR_FEE_RECEIPT_OUTSIDE_COLLECTION")
            executed = None if row.source_executed_at is None else _time(row.source_executed_at)
            if row.source_time_basis != "PROVIDER_EXPLICIT_ZONE" or executed is None:
                material.add("EXECUTION_TIME_PROVENANCE_UNRESOLVED")
            elif (executed < second.facts.collection_completed_at or executed > min(received, fee_received)
                  or (facts.cash_received_at is not None and executed > facts.cash_received_at)):
                material.add("EXECUTION_OUTSIDE_CASH_INTERVAL")
            key = _execution_record(row)
            for known in (current.get(row.exec_id), history.get(row.exec_id)):
                if known is not None and _execution_record(known) != key:
                    material.add("EXECUTION_OR_FEE_REVISION_UNRESOLVED")
            current.setdefault(row.exec_id, row)
            parts = row.exec_id.split(".")
            if len(parts) == 4 and parts[-1].isascii() and parts[-1].isdigit():
                family = ".".join(parts[:-1])
                if family in families and families[family] != row.exec_id:
                    material.add("EXECUTION_CORRECTION_UNRESOLVED")
                families[family] = row.exec_id
            execution_records.append((facts.collection_id, row.exec_id, key, received, fee_received))
        if history.keys() - current.keys():
            material.add("EXECUTION_HISTORY_OMITTED")
        history.update(current)
        if index < 2 and (facts.positions or facts.executions or any(not order.terminal_observed for order in facts.orders)):
            material.add("BASELINE_NOT_FLAT_PRE_ENTRY")
    left, base, closing = first.facts, second.facts, observation.facts
    day = base.collection_completed_at.astimezone(_NY).date()
    if any(stamp.astimezone(_NY).date() != day for item in observations for stamp in (item.facts.collection_started_at, item.facts.collection_completed_at)) or now.astimezone(_NY).date() != day:
        material.add("SESSION_DAY_MISMATCH")
    if left.cash_value != base.cash_value or left.cash_currency != base.cash_currency:
        material.add("BASELINE_CASH_NOT_STABLE")
    if base.collection_completed_at - left.collection_started_at > timedelta(seconds=30):
        material.add("BASELINE_CAPTURE_WINDOW_TOO_WIDE")
    if base.cash_received_at is not None and not timedelta(0) <= base.collection_completed_at - base.cash_received_at <= timedelta(seconds=5):
        material.add("BASELINE_CASH_LOCAL_RECEIPT_STALE")
    if any(not timedelta(0) <= now - stamp <= timedelta(seconds=5) for stamp in (closing.collection_started_at, closing.collection_completed_at, closing.cash_received_at) if stamp is not None):
        material.add("CURRENT_CASH_LOCAL_RECEIPT_STALE_OR_FUTURE")
    adjustments: dict[str, SessionNonTradingAdjustment] = {}
    for row in nontrading_adjustments:
        # Revalidate even if a caller has bypassed normal frozen construction.
        row.__post_init__()
        if row.account_binding_sha256 != account:
            material.add("NONTRADING_ADJUSTMENT_ACCOUNT_MISMATCH")
        if (base.cash_received_at is None or closing.cash_received_at is None
                or not base.cash_received_at < row.effective_at <= closing.cash_received_at
                or row.received_at > now):
            material.add("NONTRADING_ADJUSTMENT_OUTSIDE_CASH_INTERVAL")
        prior = adjustments.get(row.adjustment_id)
        if prior is not None and prior != row:
            material.add("NONTRADING_ADJUSTMENT_CONFLICT")
        adjustments.setdefault(row.adjustment_id, row)
    gross = fees = trade = nontrade = expected = residual = None
    quantities: dict[int, int] = {}
    contract_facts: dict[int, tuple[str, str, str]] = {}
    if not material:
        for row in sorted(history.values(), key=lambda item: (item.source_executed_at, item.exec_id)):
            identity = (row.symbol, row.security_type, row.currency)
            if row.contract_id in contract_facts and contract_facts[row.contract_id] != identity:
                material.add("EXECUTION_CONTRACT_IDENTITY_CONFLICT")
            contract_facts[row.contract_id] = identity
            quantity = quantities.get(row.contract_id, 0) + (int(row.quantity) if row.side == "BUY" else -int(row.quantity))
            quantities[row.contract_id] = quantity
            if not 0 <= quantity <= 2_147_483_647:
                material.add("EXECUTION_LONG_ONLY_CHRONOLOGY_UNRESOLVED")
    if not material:
        with localcontext() as context:
            context.prec = 80
            gross = sum(((1 if row.side == "SELL" else -1) * row.quantity * row.price for row in history.values()), Decimal(0))
            fees = sum((row.commission for row in history.values()), Decimal(0))
            trade = gross - fees
            nontrade = sum((row.amount for row in adjustments.values()), Decimal(0))
            expected = base.cash_value + trade + nontrade
            residual = closing.cash_value - expected
        if residual != 0:
            material.add("UNEXPLAINED_CASH_RESIDUAL")
    evidence = _plain(("session-accounting-v1", account, observation_scopes, cash_records, execution_records, exposure_records,
                       tuple(vars(adjustments[key]) for key in sorted(adjustments)), now,
                       tuple(sorted(material)), _SOURCE_BLOCKERS))
    digest = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    return SessionAccountingReconciliation(
        account, base.collection_id, closing.collection_id, digest, now,
        base.cash_value, closing.cash_value, gross, fees, trade, nontrade, expected, residual,
        len(history), len(adjustments), tuple(sorted(material)),
    )
