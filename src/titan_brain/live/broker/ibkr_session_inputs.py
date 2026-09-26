"""Read-only, bounded inputs for the approved session-measurement workstream.

These are immutable broker observations, NOT the old production AccountSnapshot
or a new policy/risk authority.  Collection end markers establish completion of
the requested callback set, not all-client history, uninterrupted event coverage,
an atomic valuation time or exhaustive account-adjustment reconciliation.
No caller can supply a `complete=True` switch to promote these observations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import re
from threading import RLock
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .ibkr_read import IbkrFiniteSessionExposure, IbkrReadCollectionDiagnostic
    from .ibkr_runtime import IbkrOfficialRuntime


_NY = ZoneInfo("America/New_York")
_FINITE_ENDS = ("account_updates_multi", "completed_orders", "executions", "open_orders", "positions")
_SOURCE_LIMITS = (
    "ALL_CLIENT_EXECUTION_COVERAGE_UNPROVEN",
    "CONTINUOUS_EVENT_COVERAGE_UNPROVEN",
    "EXTERNAL_ADJUSTMENT_RECONCILIATION_UNAVAILABLE",
)
# This model freezes genuine pre-entry NLV; it does not revalue performance
# from current NLV. A missing broker economic timestamp is represented by the
# honest local-receipt timing label, not an extra unconditional policy gate.
# Scope, freshness, flatness and same-account history still require validation.
_CAPTURE_FAILURE_CODES = frozenset({
    "IBKR_SESSION_INPUT_CAPTURE_FAILED",
    "IBKR_SESSION_INPUT_TIME_INVALID",
    "IBKR_SESSION_INPUT_AMOUNT_INVALID",
    "IBKR_SESSION_INPUT_RUNTIME_NOT_CURRENT",
    "IBKR_SESSION_INPUT_ACCOUNT_BINDING_CHANGED",
    "IBKR_SESSION_INPUT_GENERATION_CHANGED",
    "IBKR_SESSION_INPUT_FINITE_SET_INVALID",
    "IBKR_SESSION_INPUT_COLLECTION_TIME_INVALID",
    "IBKR_SESSION_INPUT_COLLECTION_NOT_MONOTONE",
    "IBKR_RUNTIME_READS_NOT_READY",
    "IBKR_RUNTIME_ACCOUNT_NOT_DISCOVERED",
    "IBKR_READ_NOT_AUTHENTICATED",
    "IBKR_READ_COLLECTION_ALREADY_ACTIVE",
    "IBKR_READ_COLLECTION_GENERATION_LOST",
    "IBKR_READ_AUTHENTICATION_LOST",
    "IBKR_READ_REQUEST_DISPATCH_FAILED",
    "IBKR_SESSION_ACCOUNT_UPDATES_CLEANUP_UNCONFIRMED",
    "IBKR_SESSION_ACCOUNT_UPDATES_UNSUPPORTED",
    "IBKR_SESSION_EXECUTION_SOURCE_TIME_INVALID",
    "IBKR_SESSION_EXPOSURE_INVALID",
    "IBKR_SESSION_EXPOSURE_USD_UNPROVEN",
    "IBKR_SESSION_EXPOSURE_EXECUTION_METADATA_UNPROVEN",
    "IBKR_ACCOUNT_SUMMARY_INCOMPLETE",
    "IBKR_ACCOUNT_TYPE_MISSING",
    "IBKR_ORDER_IDENTITY_COLLISION",
    "IBKR_NON_USD_COMMISSION_UNSUPPORTED",
    "IBKR_EXECUTION_WITHOUT_ORDER_EVIDENCE",
    "IBKR_DUPLICATE_POSITION_CALLBACK",
    "IBKR_UNSUPPORTED_MATERIAL_POSITION",
    "IBKR_SHORT_POSITION_CANNOT_BE_NORMALIZED",
    "IBKR_EXECUTION_VALUES_INVALID",
    "IBKR_TIMESTAMP_LACKS_TIMEZONE",
    "IBKR_TIMESTAMP_FORMAT_UNSUPPORTED",
    "IBKR_TIMESTAMP_ZONE_UNSUPPORTED",
})
_CAPTURE_CALLBACK_SCOPES = frozenset({
    "request", "sdk_callback", "session", "connection_closed",
    "connection_generation_changed", "account_summary_shape",
    "account_summary_conflict", "position_shape", "order_shape",
    "order_status_shape", "execution_shape", "duplicate_execution", "commission_shape",
    "account_updates_scope", "account_updates_shape", "account_updates_not_ready",
    "account_updates_currency", "account_updates_conflict",
})
_CAPTURE_TIMEOUT_PHASES = frozenset(_FINITE_ENDS) | frozenset({
    *(name + "_dispatch" for name in _FINITE_ENDS),
    "commission", "collection_completion", "collection_freeze", "session_facts_normalization",
})


class IbkrSessionInputError(RuntimeError):
    """A fixed-code failure that never carries broker text or account IDs."""


def _capture_failure_code(error: Exception) -> str:
    """Reduce one local exception to a closed machine vocabulary, not its text.

    Do not call arbitrary exception __str__, pass through an IBKR-prefixed
    string, or classify a P&L timeout on a path that never requests P&L.
    """
    generic = "IBKR_SESSION_INPUT_CAPTURE_FAILED"
    if len(error.args) != 1 or type(error.args[0]) is not str or len(error.args[0]) > 256:
        return generic
    message = error.args[0]
    if message in _CAPTURE_FAILURE_CODES:
        return message
    callback = re.fullmatch(
        r"IBKR_READ_CALLBACK_ERROR:([0-9]{1,10}):([a-z_]+)(:API_READ_ONLY)?", message,
    )
    if callback is not None:
        code, scope, reason = int(callback.group(1)), callback.group(2), callback.group(3)
        if code > 2_147_483_647 or scope not in _CAPTURE_CALLBACK_SCOPES:
            return generic
        if reason is not None and (code != 321 or scope not in {"request", "sdk_callback"}):
            return generic
        suffix = "_API_READ_ONLY" if reason is not None else ""
        return f"IBKR_SESSION_INPUT_READ_CALLBACK_{scope.upper()}_{code}{suffix}"
    if message.startswith("IBKR_READ_TIMEOUT:"):
        phases = message.removeprefix("IBKR_READ_TIMEOUT:").split(",")
        if len(phases) == 1 and phases[0] in _CAPTURE_TIMEOUT_PHASES:
            return "IBKR_SESSION_INPUT_READ_TIMEOUT_" + phases[0].upper()
        if len(phases) > 1 and len(phases) == len(set(phases)) and set(phases) <= set(_FINITE_ENDS):
            return "IBKR_SESSION_INPUT_READ_TIMEOUT_MULTIPLE_FINITE_CALLBACKS"
    return generic


def is_public_session_input_failure_code(value: object) -> bool:
    """Validate exactly the emitted safe code vocabulary for public schemas."""
    if type(value) is not str or len(value) > 128:
        return False
    if value in _CAPTURE_FAILURE_CODES:
        return True
    if value.startswith("IBKR_SESSION_INPUT_READ_TIMEOUT_"):
        phase = value.removeprefix("IBKR_SESSION_INPUT_READ_TIMEOUT_")
        return phase == "MULTIPLE_FINITE_CALLBACKS" or phase in {
            item.upper() for item in _CAPTURE_TIMEOUT_PHASES
        }
    callback = re.fullmatch(
        r"IBKR_SESSION_INPUT_READ_CALLBACK_([A-Z_]+)_(0|[1-9][0-9]{0,9})(_API_READ_ONLY)?",
        value,
    )
    if callback is None:
        return False
    scope, code, reason = callback.group(1).lower(), int(callback.group(2)), callback.group(3)
    return (
        scope in _CAPTURE_CALLBACK_SCOPES
        and code <= 2_147_483_647
        and (reason is None or (code == 321 and scope in {"request", "sdk_callback"}))
    )


@dataclass(frozen=True)
class SessionPositionFact:
    contract_id: int | None
    symbol: str
    security_type: str
    currency: str
    quantity: Decimal
    received_at: datetime


@dataclass(frozen=True)
class SessionExecutionFact:
    exec_id: str
    contract_id: int | None
    symbol: str
    security_type: str
    currency: str
    side: str
    quantity: Decimal
    price: Decimal
    source_executed_at: datetime | None
    source_time_basis: Literal[
        "PROVIDER_EXPLICIT_ZONE", "CONFIGURED_SESSION_ZONE_INTERPRETATION", "ABSENT"
    ]
    received_at: datetime
    commission: Decimal
    commission_currency: str
    commission_received_at: datetime


@dataclass(frozen=True)
class SessionOrderFact:
    order_identity: str
    contract_id: int | None
    family: str
    terminal_observed: bool
    state: str
    blocking_warning_present: bool


@dataclass(frozen=True)
class IbkrFiniteSessionFacts:
    """Deep-copied finite callback values; no full account ID or P&L field."""

    generation: int
    collection_id: str
    collection_started_at: datetime
    collection_completed_at: datetime
    net_liquidation: Decimal
    net_liquidation_currency: str
    net_liquidation_received_at: datetime
    positions: tuple[SessionPositionFact, ...]
    executions: tuple[SessionExecutionFact, ...]
    orders: tuple[SessionOrderFact, ...]
    completed_reads: tuple[str, ...]
    commission_conflict_observed: bool
    orphan_commission_report_count: int
    account_values_source: str = "IBKR_ACCOUNT_SUMMARY_V1"
    cash_value: Decimal | None = None
    cash_currency: str | None = None
    cash_received_at: datetime | None = None

    def public_dict(self) -> dict[str, object]:
        # Financial values/contract and execution identifiers are available to
        # the local typed consumer, not sprayed into public probe summaries.
        return {
            "collection_id": self.collection_id,
            "account_values_source": self.account_values_source,
            "read_generation": self.generation,
            "collection_started_at": self.collection_started_at.isoformat(),
            "collection_completed_at": self.collection_completed_at.isoformat(),
            "timing_basis": "local_receipt_not_atomic_broker_valuation",
            "net_liquidation_currency": self.net_liquidation_currency,
            "position_count": len(self.positions),
            "execution_count": len(self.executions),
            "execution_time_bases": tuple(sorted({item.source_time_basis for item in self.executions})),
            "order_count": len(self.orders),
            "active_order_count": sum(not item.terminal_observed for item in self.orders),
            "completed_reads": self.completed_reads,
            "commission_conflict_observed": self.commission_conflict_observed,
            "orphan_commission_report_count": self.orphan_commission_report_count,
            "daily_pnl_status": "not_requested",
            "diagnostic_only": True,
            "live_authority": False,
        }


@dataclass(frozen=True)
class SessionInputObservation:
    account_binding_fingerprint: str = field(repr=False)
    facts: IbkrFiniteSessionFacts = field(repr=False)
    read_client_id: int
    prior_collection_id: str | None
    unobserved_interval_since_prior_collection: bool
    sticky_read_gap: bool
    blockers: tuple[str, ...]

    def public_dict(self) -> dict[str, object]:
        return {
            **self.facts.public_dict(),
            "read_client_id": self.read_client_id,
            "prior_collection_id": self.prior_collection_id,
            "unobserved_interval_since_prior_collection": self.unobserved_interval_since_prior_collection,
            "sticky_read_gap": self.sticky_read_gap,
            "blockers": self.blockers,
            "baseline_authority": False,
            "session_measurement_authority": False,
            "whole_account_coverage_verified": False,
        }


@dataclass(frozen=True)
class SessionInputExposureObservation:
    """Runtime-bound input lineage and its exact same finite exposure read.

    Neither the wrapper nor its unpromoted base/pages issues readiness or
    complete risk evidence. Use the facts' limitations and receipt-time basis.
    """

    observation: SessionInputObservation = field(repr=False)
    exposure: "IbkrFiniteSessionExposure" = field(repr=False)

    def __post_init__(self) -> None:
        from .ibkr_read import IbkrFiniteSessionExposure

        if (type(self.observation) is not SessionInputObservation
                or type(self.exposure) is not IbkrFiniteSessionExposure
                or self.observation.facts is not self.exposure.facts):
            raise IbkrSessionInputError("IBKR_SESSION_EXPOSURE_INVALID")

    def public_dict(self) -> dict[str, object]:
        # Exposure values, identifiers and page contents remain local.
        return self.observation.public_dict()


@dataclass(frozen=True)
class PreEntryBalanceCandidate:
    """Two stable, flat finite reads; never a frozen approved live baseline."""

    account_binding_fingerprint: str = field(repr=False)
    first_collection_id: str
    second_collection_id: str
    observed_at: datetime
    starting_nlv: Decimal | None
    observation_preconditions_met: bool
    blockers: tuple[str, ...]

    def public_dict(self) -> dict[str, object]:
        return {
            "first_collection_id": self.first_collection_id,
            "second_collection_id": self.second_collection_id,
            "observed_at": self.observed_at.isoformat(),
            "starting_balance_candidate_present": self.starting_nlv is not None,
            "observation_preconditions_met": self.observation_preconditions_met,
            "blockers": self.blockers,
            "baseline_authority": False,
            "baseline_frozen": False,
            "diagnostic_only": True,
            "live_authority": False,
        }


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise IbkrSessionInputError("IBKR_SESSION_INPUT_TIME_INVALID")
    return value.astimezone(timezone.utc)


def _decimal_text(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise IbkrSessionInputError("IBKR_SESSION_INPUT_AMOUNT_INVALID")
    if value == 0:
        return "0"
    rendered = format(value, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _execution_identity(item: SessionExecutionFact) -> str:
    # Local receipt times may change on a replay. Economic values and the
    # provider execution time must not. Keep reported commission sign/currency.
    values = (
        item.contract_id, item.symbol, item.security_type, item.currency,
        item.side, _decimal_text(item.quantity), _decimal_text(item.price),
        None if item.source_executed_at is None else _utc(item.source_executed_at).isoformat(),
        item.source_time_basis,
        _decimal_text(item.commission), item.commission_currency,
    )
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode("ascii")).hexdigest()


class IbkrSessionInputAdapter:
    """Owns read-only captures from one already-initialized concrete runtime.

    Construction opens no socket. `capture` makes only finite read requests;
    it never creates a command connection. Read failures/generation changes
    leave a sticky gap for this adapter's lifetime. Reconstructing an adapter
    cannot prove continuity: that limitation is unconditional in every result.
    """

    def __init__(self, runtime: "IbkrOfficialRuntime") -> None:
        from .ibkr_runtime import IbkrOfficialRuntime

        if type(runtime) is not IbkrOfficialRuntime:
            raise TypeError("concrete IBKR runtime required for session input capture")
        self._runtime = runtime
        self._lock = RLock()
        self._prior: SessionInputObservation | None = None
        self._binding: str | None = None
        self._generation: int | None = None
        self._sticky_gap = False
        self._history_conflict = False
        self._seen_executions: dict[str, str] = {}
        self._last_capture_diagnostic: IbkrReadCollectionDiagnostic | None = None
        self._last_capture_failure_code: str | None = None

    @property
    def last_capture_failure_code(self) -> str | None:
        """Last attempt's fixed failure code; no broker text, IDs or authority."""
        with self._lock:
            return self._last_capture_failure_code

    @property
    def last_capture_diagnostic(self) -> IbkrReadCollectionDiagnostic | None:
        """This capture's immutable, value-free read status, never readiness.

        A failure before dispatch has no collection diagnostic. Attribution is
        by an unshared attempt token, not the bridge's most recent global read.
        """
        with self._lock:
            return self._last_capture_diagnostic

    def _runtime_state(self):
        status = self._runtime.status()
        components = self._runtime.components
        binding = self._runtime.account_binding_fingerprint
        if (
            not status.read_connected
            or not status.account_authenticated
            or not status.sdk_attested
            or status.runtime_error_code is not None
            or components.read_generation != status.read_generation
            or components.read_bridge.generation != status.read_generation
            or binding != components.account_binding_fingerprint
            or re.fullmatch(r"[0-9a-f]{64}", binding) is None
        ):
            raise IbkrSessionInputError("IBKR_SESSION_INPUT_RUNTIME_NOT_CURRENT")
        return status, components, binding

    def capture(self) -> SessionInputObservation:
        return self._capture(include_exposure=False)

    def capture_with_exposure(self) -> SessionInputExposureObservation:
        """Separate finite-exposure route; does not call/replace ``capture``."""
        return self._capture(include_exposure=True)

    def _capture(self, *, include_exposure: bool) -> SessionInputObservation | SessionInputExposureObservation:
        with self._lock:
            self._last_capture_diagnostic = None
            self._last_capture_failure_code = None
            try:
                status, components, binding = self._runtime_state()
                if self._binding is not None and self._binding != binding:
                    raise IbkrSessionInputError("IBKR_SESSION_INPUT_ACCOUNT_BINDING_CHANGED")
                if self._generation is not None and self._generation != status.read_generation:
                    self._sticky_gap = True
                diagnostic_token = object()
                try:
                    if include_exposure:
                        exposure = components.read_bridge.collect_session_exposure(diagnostic_token=diagnostic_token)
                        facts = exposure.facts
                    else:
                        facts = components.read_bridge.collect_session_facts(diagnostic_token=diagnostic_token)
                finally:
                    self._last_capture_diagnostic = components.read_bridge.read_diagnostic_for(
                        diagnostic_token,
                    )
                after, current, current_binding = self._runtime_state()
                if (
                    current is not components
                    or current_binding != binding
                    or after.read_generation != status.read_generation
                    or facts.generation != status.read_generation
                ):
                    raise IbkrSessionInputError("IBKR_SESSION_INPUT_GENERATION_CHANGED")
                result = self._bind_facts(facts, status.read_client_id, binding)
                captured = SessionInputExposureObservation(result, exposure) if include_exposure else result
                self._binding, self._generation = binding, status.read_generation
                self._prior = result
                return captured
            except Exception as error:
                self._sticky_gap = True
                self._last_capture_failure_code = _capture_failure_code(error)
                raise IbkrSessionInputError("IBKR_SESSION_INPUT_CAPTURE_FAILED") from None

    def _bind_facts(self, facts: IbkrFiniteSessionFacts, client_id: int, binding: str) -> SessionInputObservation:
        if (type(facts) is not IbkrFiniteSessionFacts or facts.completed_reads != _FINITE_ENDS
                or facts.account_values_source != "IBKR_ACCOUNT_UPDATES_MULTI_V1"):
            raise IbkrSessionInputError("IBKR_SESSION_INPUT_FINITE_SET_INVALID")
        started, completed = _utc(facts.collection_started_at), _utc(facts.collection_completed_at)
        if started > completed or not started <= _utc(facts.net_liquidation_received_at) <= completed:
            raise IbkrSessionInputError("IBKR_SESSION_INPUT_COLLECTION_TIME_INVALID")
        blockers = set(_SOURCE_LIMITS)
        if facts.net_liquidation <= 0 or not facts.net_liquidation.is_finite():
            blockers.add("STARTING_NLV_NOT_POSITIVE_FINITE")
        if facts.net_liquidation_currency != "USD":
            blockers.add("NET_LIQUIDATION_USD_NOT_ESTABLISHED")
        if facts.commission_conflict_observed:
            self._history_conflict = True
        if facts.orphan_commission_report_count:
            blockers.add("ORPHAN_COMMISSION_SCOPE_UNKNOWN")
        if any(item.blocking_warning_present for item in facts.orders):
            blockers.add("ORDER_WARNING_REQUIRES_RECONCILIATION")
        for position in facts.positions:
            if (
                position.contract_id is None or position.security_type != "STK"
                or position.currency != "USD" or position.quantity < 0
                or position.quantity != position.quantity.to_integral_value()
            ):
                blockers.add("POSITION_OUTSIDE_USD_WHOLE_SHARE_LONG_SCOPE")
        current: dict[str, str] = {}
        correction_families: dict[str, str] = {}
        for execution in facts.executions:
            if (
                execution.contract_id is None or execution.security_type != "STK"
                or execution.currency != "USD" or execution.side not in ("BUY", "SELL")
                or execution.quantity <= 0 or execution.quantity != execution.quantity.to_integral_value()
            ):
                blockers.add("EXECUTION_OUTSIDE_USD_WHOLE_SHARE_SCOPE")
            if execution.commission_currency != "USD":
                blockers.add("ACTUAL_COMMISSION_USD_NOT_ESTABLISHED")
            if execution.source_executed_at is None:
                blockers.add("EXECUTION_PROVIDER_TIME_UNAVAILABLE")
            elif _utc(execution.source_executed_at) > completed:
                blockers.add("EXECUTION_PROVIDER_TIME_FUTURE")
            if execution.source_time_basis == "CONFIGURED_SESSION_ZONE_INTERPRETATION":
                blockers.add("EXECUTION_TIMEZONE_CONFIGURED_NOT_PROVIDER_PROVEN")
            elif execution.source_time_basis not in ("PROVIDER_EXPLICIT_ZONE", "ABSENT"):
                blockers.add("EXECUTION_TIME_BASIS_INVALID")
            if not started <= _utc(execution.received_at) <= completed or not started <= _utc(execution.commission_received_at) <= completed:
                blockers.add("EXECUTION_RECEIPT_OUTSIDE_COLLECTION")
            identity = _execution_identity(execution)
            if execution.exec_id in current and current[execution.exec_id] != identity:
                self._history_conflict = True
            current[execution.exec_id] = identity
            parts = execution.exec_id.split(".")
            if len(parts) == 4 and parts[-1].isascii() and parts[-1].isdigit():
                family = ".".join(parts[:-1])
                if family in correction_families and correction_families[family] != execution.exec_id:
                    self._history_conflict = True
                correction_families[family] = execution.exec_id
        if self._seen_executions.keys() - current.keys():
            self._sticky_gap = True
            blockers.add("PREVIOUS_EXECUTION_NOT_IN_CURRENT_BOUNDED_READ")
        for identity, digest in current.items():
            if identity in self._seen_executions and self._seen_executions[identity] != digest:
                self._history_conflict = True
            else:
                self._seen_executions[identity] = digest
        if self._history_conflict:
            blockers.add("EXECUTION_OR_COMMISSION_REVISION_UNRESOLVED")
        prior = self._prior
        if prior is not None:
            if started < _utc(prior.facts.collection_completed_at) or facts.collection_id == prior.facts.collection_id:
                raise IbkrSessionInputError("IBKR_SESSION_INPUT_COLLECTION_NOT_MONOTONE")
            if started.astimezone(_NY).date() != _utc(prior.facts.collection_started_at).astimezone(_NY).date():
                self._sticky_gap = True
                blockers.add("SESSION_DAY_CHANGED_REQUIRES_NEW_BASELINE_WORKFLOW")
        if self._sticky_gap:
            blockers.add("READ_GAP_REQUIRES_RECONCILIATION")
        return SessionInputObservation(
            account_binding_fingerprint=binding,
            facts=facts,
            read_client_id=client_id,
            prior_collection_id=None if prior is None else prior.facts.collection_id,
            unobserved_interval_since_prior_collection=prior is not None,
            sticky_read_gap=self._sticky_gap,
            blockers=tuple(sorted(blockers)),
        )

    def capture_pre_entry_candidate(self) -> PreEntryBalanceCandidate:
        """Observe a stable flat candidate; do not write/freeze a live baseline."""
        with self._lock:
            first, second = self.capture(), self.capture()
            blockers = set(first.blockers) | set(second.blockers)
            local_problems: set[str] = set()
            left, right = first.facts, second.facts
            if (
                first.account_binding_fingerprint != second.account_binding_fingerprint
                or left.generation != right.generation
            ):
                local_problems.add("PRE_ENTRY_ACCOUNT_OR_GENERATION_MOVED")
            if left.net_liquidation != right.net_liquidation or left.net_liquidation_currency != right.net_liquidation_currency:
                local_problems.add("PRE_ENTRY_NLV_NOT_STABLE")
            if right.net_liquidation_currency != "USD" or right.net_liquidation <= 0:
                local_problems.add("PRE_ENTRY_USD_NLV_UNPROVEN")
            if left.positions or right.positions:
                local_problems.add("PRE_ENTRY_FLAT_POSITIONS_NOT_OBSERVED")
            if any(not item.terminal_observed for item in (*left.orders, *right.orders)):
                local_problems.add("PRE_ENTRY_ACTIVE_OR_UNKNOWN_ORDERS_OBSERVED")
            if left.executions or right.executions or self._seen_executions:
                local_problems.add("PRE_ENTRY_CURRENT_DAY_EXECUTIONS_OBSERVED")
            if _utc(left.collection_started_at).astimezone(_NY).date() != _utc(right.collection_completed_at).astimezone(_NY).date():
                local_problems.add("PRE_ENTRY_COLLECTION_CROSSES_SESSION_DAY")
            if right.collection_completed_at - left.collection_started_at > timedelta(seconds=30):
                local_problems.add("PRE_ENTRY_COLLECTION_WINDOW_TOO_WIDE")
            # A candidate may be useful while source guarantees remain unknown,
            # but local scope/gap/conflict failures may not masquerade as one.
            extra = (set(first.blockers) | set(second.blockers)) - set(_SOURCE_LIMITS)
            local_problems.update(extra)
            blockers.update(local_problems)
            return PreEntryBalanceCandidate(
                account_binding_fingerprint=second.account_binding_fingerprint,
                first_collection_id=left.collection_id,
                second_collection_id=right.collection_id,
                observed_at=right.collection_completed_at,
                starting_nlv=right.net_liquidation if not local_problems else None,
                observation_preconditions_met=not local_problems,
                blockers=tuple(sorted(blockers)),
            )
