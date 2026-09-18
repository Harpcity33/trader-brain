"""Hermetic, read-only IBKR TWS callback collection.

This module intentionally does not import ``ibapi``.  A production composition
supplies an official ``EClient``-compatible requester and installs the
generation-bound callback object returned by :meth:`open_generation` as its
``EWrapper``.  Tests can therefore exercise the complete callback lifecycle
without a socket or the SDK package.

The bridge issues only account/order/execution read requests.  It never calls
``placeOrder``, ``cancelOrder`` or a global-cancel endpoint.  IBKR's completed
order endpoint is bounded by the broker and is not proof that an unmatched
``orderRef`` never reached IBKR; negative reference results are consequently
always returned as ``not_seen_yet``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import re
from threading import Condition, RLock
import time
from types import SimpleNamespace
from typing import Callable, Protocol, runtime_checkable
from uuid import UUID
from zoneinfo import ZoneInfo

from ..models import BrokerOrderState
from .base import (
    AccountSnapshot,
    BrokerCapabilityError,
    BrokerContractViolation,
    BrokerSide,
    ClientRefLookupResult,
    EquityOrderType,
    FillSnapshot,
    FundsSnapshot,
    MarketHours,
    OrderFamily,
    OrderSnapshot,
    PositionSnapshot,
    TimeInForce,
)
from .production import CollectedObservation, OrderFamilyPage
from .ibkr_position_valuation import PositionValuationContract, PositionValuationError
from .ibkr_protection_evidence import (
    IbkrOrderStatusFact,
    IbkrProtectionEvidence,
    capture_ibkr_order_status_fact,
    capture_ibkr_protection_evidence,
    protection_evidence_facts,
)


_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.\-]{0,14}$")
_INFO_CODES = frozenset({1101, 1102, 2104, 2106, 2107, 2108, 2158})
_SESSION_ERROR_CODES = frozenset({502, 503, 504, 1100, 1300})
_PNL_ATTEMPT_LIMIT = 2
_FINITE_READ_ENDS = frozenset({
    "account_summary", "positions", "open_orders", "completed_orders", "executions",
})
_SESSION_READ_ENDS = (_FINITE_READ_ENDS - {"account_summary"}) | {"account_updates_multi"}
_MAX_WARNING_TEXT_LENGTH = 4096
IBKR_API_READ_ONLY_MESSAGE = "The API interface is currently in Read-Only mode."
_READ_ONLY_ERROR = re.compile(
    r"(?:Error validating request[.:]\s*-?'[A-Za-z0-9_]{1,8}'\s*:\s*cause\s*-\s*)?"
    r"The API interface is currently in Read-Only mode\.?\s*"
)
_SUMMARY_TAGS = ",".join(
    (
        "AccountType",
        "NetLiquidation",
        "TotalCashValue",
        "AvailableFunds",
        "BuyingPower",
        "SettledCash",
    )
)


def classify_ibkr_error_callback(arguments: tuple[object, ...]) -> tuple[int, str]:
    """Reduce supported callback shapes to a code and a fixed public reason.

    Code 321 alone means validation failed, not that Read-Only API is enabled.
    Inspect only bounded plain text for the known provider cause; never retain
    broker text, advanced JSON, or invoke payload string conversion methods.
    """
    code, message = 0, None
    if (
        len(arguments) in (3, 4)
        and type(arguments[0]) is int
        and type(arguments[1]) is int
    ):
        code, message = arguments[1], arguments[2]
    elif len(arguments) in (2, 3) and type(arguments[0]) is int:
        code, message = arguments[0], arguments[1]
    reason = "UNSPECIFIED"
    if (
        code == 321
        and type(message) is str
        and len(message) <= 4096
        and _READ_ONLY_ERROR.fullmatch(message.strip()) is not None
    ):
        reason = "API_READ_ONLY"
    return code, reason


@runtime_checkable
class IbkrReadRequester(Protocol):
    """The read-only subset of the official ``EClient`` used by this bridge."""

    def reqAccountSummary(self, reqId: int, groupName: str, tags: str) -> None: ...
    def cancelAccountSummary(self, reqId: int) -> None: ...
    def reqPositions(self) -> None: ...
    def cancelPositions(self) -> None: ...
    def reqAllOpenOrders(self) -> None: ...
    def reqCompletedOrders(self, apiOnly: bool) -> None: ...
    def reqExecutions(self, reqId: int, execFilter: object) -> None: ...
    def reqPnL(self, reqId: int, account: str, modelCode: str) -> None: ...
    def cancelPnL(self, reqId: int) -> None: ...


@runtime_checkable
class IbkrAccountUpdatesRequester(Protocol):
    """Separate account-scoped value feed; not the capped summary feed."""

    def reqAccountUpdatesMulti(self, reqId: int, account: str, modelCode: str, ledgerAndNLV: bool) -> None: ...
    def cancelAccountUpdatesMulti(self, reqId: int) -> None: ...


@dataclass(frozen=True)
class SanitizedIbkrError:
    """Nonsecret error evidence; broker text and advanced JSON are discarded."""

    request_id: int
    code: int
    scope: str
    received_at: datetime
    reason: str = "UNSPECIFIED"


@dataclass(frozen=True)
class IbkrFiniteReadDiagnostic:
    """Status of finite callbacks, never a production snapshot or risk receipt."""

    observed_at: datetime
    completed_reads: tuple[str, ...]

    def public_dict(self) -> dict[str, object]:
        return {
            "observed_at": self.observed_at,
            "completed_reads": self.completed_reads,
            "finite_data_normalized": True,
            "diagnostic_only": True,
            "daily_pnl_status": "not_requested",
            "strict_account_read_complete": False,
            "daily_starting_equity_ready": False,
            "whole_broker_history_verified": False,
            "write_authority_granted": False,
        }


@dataclass(frozen=True)
class IbkrReadChannelDiagnostic:
    """Local receipt timing only; contains no account or financial values.

    Dispatch outcome describes the last attempt. First callback receipt is
    collection-wide and can therefore precede a P&L retry's dispatch.
    """

    channel: str
    request_attempts: int
    first_dispatch_started_ms: int | None
    last_dispatch_started_ms: int | None
    last_dispatch_returned_ms: int | None
    first_callback_ms: int | None
    end_callback_ms: int | None
    observation: str

    def public_dict(self) -> dict[str, object]:
        return dict(vars(self))


@dataclass(frozen=True)
class IbkrReadCollectionDiagnostic:
    channels: tuple[IbkrReadChannelDiagnostic, ...]
    elapsed_ms: int
    normalization_completed: bool
    commission_reports_missing: bool

    def public_dict(self) -> dict[str, object]:
        return {
            "channels": tuple(item.public_dict() for item in self.channels),
            "elapsed_ms": self.elapsed_ms,
            "normalization_completed": self.normalization_completed,
            "commission_reports_missing": self.commission_reports_missing,
            "timing_basis": "local_monotonic_receipt_not_broker_source_time",
            "dispatch_return_is_broker_acknowledgement": False,
            "provider_cause_verified": False,
            "diagnostic_only": True,
            "strict_account_read_complete": False,
            "write_authority_granted": False,
            "daily_starting_equity_ready": False,
        }


@dataclass
class _RawOrder:
    contract: object
    order: object
    status: str
    completed_time: str
    blocking_warning_present: bool
    received_at: datetime
    source: str
    protection_evidence: IbkrProtectionEvidence | None = None


@dataclass
class _RawExecution:
    contract: object
    execution: object
    received_at: datetime


@dataclass
class _Collection:
    generation: int
    summary_request_id: int
    execution_request_id: int
    pnl_request_id: int
    started_at: datetime
    account_values_channel: str = "account_summary"
    diagnostic_nonce: int = 0
    diagnostic_token: object | None = field(default=None, repr=False)
    started_monotonic: float = field(default_factory=lambda: time.monotonic())
    channel_attempts: dict[str, int] = field(default_factory=dict)
    channel_timings: dict[tuple[str, str], int] = field(default_factory=dict)
    ends: set[str] = field(default_factory=set)
    summary: dict[str, tuple[str, str]] = field(default_factory=dict)
    summary_received_at: dict[str, datetime] = field(default_factory=dict)
    positions: list[tuple[object, Decimal, Decimal, datetime]] = field(default_factory=list)
    orders: dict[str, _RawOrder] = field(default_factory=dict)
    order_statuses: dict[tuple[int, int], tuple[str, datetime]] = field(default_factory=dict)
    protection_statuses: dict[tuple[int, int], IbkrOrderStatusFact | None] = field(default_factory=dict)
    protection_status_conflicts: set[tuple[int, int]] = field(default_factory=set)
    protection_order_conflicts: set[str] = field(default_factory=set)
    executions: dict[str, _RawExecution] = field(default_factory=dict)
    commissions: dict[str, tuple[Decimal, str, datetime]] = field(default_factory=dict)
    commission_conflict_observed: bool = False
    daily_realized_pnl: tuple[Decimal, datetime] | None = None
    pnl_request_ids: list[int] = field(default_factory=list)
    cancelled_pnl_request_ids: set[int] = field(default_factory=set)
    unavailable_pnl_request_ids: set[int] = field(default_factory=set)
    error: SanitizedIbkrError | None = None


@dataclass(frozen=True)
class _CompletedCollection:
    collection_id: str
    observation: CollectedObservation
    pages: dict[OrderFamily, OrderFamilyPage]
    all_equity_orders: tuple[OrderSnapshot, ...]
    valuation_contracts: tuple[PositionValuationContract, ...] | None = None


@dataclass(frozen=True)
class IbkrFiniteSessionExposure:
    """One non-atomic finite read, never legacy risk or whole-account authority.

    Pages describe the callback families requested in this collection, not
    exhaustive all-client history. The base deliberately keeps its orders
    unassembled and every legacy risk value/provenance unavailable. Local
    receipt timestamps and a local collection hash are not provider versions.
    """

    facts: "IbkrFiniteSessionFacts" = field(repr=False)
    observation: CollectedObservation = field(repr=False)
    order_family_pages: tuple[OrderFamilyPage, ...] = field(repr=False)

    def __post_init__(self) -> None:
        from .ibkr_session_inputs import (
            IbkrFiniteSessionFacts, SessionExecutionFact, SessionOrderFact, SessionPositionFact,
        )

        def invalid() -> None:
            raise BrokerContractViolation("IBKR_SESSION_EXPOSURE_INVALID")

        if (type(self.facts) is not IbkrFiniteSessionFacts
                or type(self.observation) is not CollectedObservation
                or type(self.order_family_pages) is not tuple):
            invalid()
        facts, observation = self.facts, self.observation
        snapshot = observation.snapshot
        for values, kind in ((facts.positions, SessionPositionFact), (facts.executions, SessionExecutionFact), (facts.orders, SessionOrderFact)):
            if type(values) is not tuple or any(type(item) is not kind for item in values):
                invalid()
        if (type(snapshot) is not AccountSnapshot
                or observation.collection_id != facts.collection_id
                or observation.request_started_at != facts.collection_started_at
                or observation.request_completed_at != facts.collection_completed_at
                or snapshot.observed_at != facts.collection_completed_at
                or snapshot.received_at != facts.collection_completed_at
                or facts.account_values_source != "IBKR_ACCOUNT_UPDATES_MULTI_V1"
                or facts.completed_reads != tuple(sorted(_SESSION_READ_ENDS))
                or facts.net_liquidation_currency != "USD"
                or facts.cash_currency != "USD"
                or snapshot.funds.currency != "USD"
                or snapshot.funds.total_value != facts.net_liquidation
                or snapshot.funds.cash != facts.cash_value
                or snapshot.equity_orders
                or snapshot.option_order_count != 0 or snapshot.advanced_order_count != 0
                or snapshot.auth_point_in_time is not True
                or snapshot.standard_equity_positions_complete is not True
                or snapshot.option_positions_complete is not True):
            invalid()
        absent = (
            "daily_realized_pnl", "weekly_realized_pnl", "peak_equity",
            "risk_evidence_source", "risk_evidence_as_of", "risk_baseline_identity_hash",
            "risk_baseline_receipt_hash", "risk_high_water_identity_hash",
            "risk_high_water_lineage_hash", "risk_high_water_receipt_hash",
            "daily_starting_equity", "daily_external_cash_flow",
            "daily_starting_equity_as_of", "daily_external_cash_flow_as_of",
            "daily_external_cash_flow_receipt_hash", "daily_starting_equity_receipt_hash",
        )
        false = (
            "standard_equity_orders_complete", "option_orders_complete", "advanced_orders_complete",
            "daily_realized_pnl_complete", "weekly_realized_pnl_complete", "peak_equity_complete",
            "risk_evidence_authoritative", "daily_realized_pnl_ready", "daily_starting_equity_ready",
            "entry_risk_evidence_ready", "authenticated_entry_risk_evidence_ready",
        )
        if (any(getattr(snapshot, name) is not None for name in absent)
                or any(getattr(snapshot, name) is not False for name in false)):
            invalid()
        pages = self.order_family_pages
        if (len(pages) != len(OrderFamily)
                or any(type(page) is not OrderFamilyPage for page in pages)
                or tuple(page.family for page in pages) != tuple(OrderFamily)):
            invalid()
        for page in pages:
            if (page.collection_id != facts.collection_id or page.snapshot_token is not None
                    or page.account_masked != snapshot.account_masked
                    or page.observed_at != facts.collection_completed_at
                    or page.received_at != facts.collection_completed_at
                    or page.provider_watermark != observation.order_event_watermark
                    or page.page_id != hashlib.sha256(f"{facts.collection_id}:{page.family.value}".encode("ascii")).hexdigest()
                    or page.page_index != 0 or page.page_complete is not True
                    or page.next_cursor is not None
                    or page.active_order_count != sum(item.family == page.family.value and not item.terminal_observed for item in facts.orders)):
                invalid()
        expected_orders = {
            (item.family, item.order_identity) for item in facts.orders
            if item.family != OrderFamily.OPTION.value
        }
        actual_orders = {(page.family.value, order.broker_order_id) for page in pages for order in page.orders}
        if (actual_orders != expected_orders
                or len(actual_orders) != sum(len(page.orders) for page in pages)
                or len({item.order_identity for item in facts.orders}) != len(facts.orders)):
            invalid()
        expected_positions = sorted((item.symbol, item.quantity) for item in facts.positions if item.security_type == "STK")
        if (sorted((item.symbol, item.quantity) for item in snapshot.equity_positions) != expected_positions
                or snapshot.option_position_count != sum(item.security_type in {"OPT", "FOP"} for item in facts.positions)):
            invalid()
        by_execution = {item.exec_id: item for item in facts.executions}
        for page in pages:
            for order in page.orders:
                for fill in order.fills:
                    fact = by_execution.get(fill.fill_id)
                    if (fact is None or fill.quantity != fact.quantity or fill.price != fact.price
                            or fill.provider_commission != fact.commission
                            or fill.provider_commission_currency != fact.commission_currency
                            or fill.executed_at != fact.source_executed_at):
                        invalid()

    @property
    def timing_basis(self) -> str:
        return "local_receipt_not_atomic_broker_valuation"

    @property
    def diagnostic_only(self) -> bool:
        return True

    @property
    def live_authority(self) -> bool:
        return False


class _GenerationCallbacks:
    """Official EWrapper-shaped callbacks bound to one connection generation."""

    def __init__(self, bridge: "IbkrWholeAccountReadBridge", generation: int) -> None:
        self._bridge = bridge
        self._generation = generation

    def connectAck(self) -> None:
        self._bridge._connect_ack(self._generation)

    def managedAccounts(self, accountsList: str) -> None:
        self._bridge._managed_accounts(self._generation, accountsList)

    def accountSummary(
        self, reqId: int, account: str, tag: str, value: str, currency: str
    ) -> None:
        self._bridge._account_summary(
            self._generation, reqId, account, tag, value, currency
        )

    def accountSummaryEnd(self, reqId: int) -> None:
        self._bridge._end(self._generation, "account_summary", reqId)

    def accountUpdateMulti(self, reqId: int, account: str, modelCode: str, key: str, value: str, currency: str) -> None:
        self._bridge._account_update_multi(self._generation, reqId, account, modelCode, key, value, currency)

    def accountUpdateMultiEnd(self, reqId: int) -> None:
        self._bridge._end(self._generation, "account_updates_multi", reqId)

    def position(self, account: str, contract: object, position: object, avgCost: float) -> None:
        self._bridge._position(
            self._generation, account, contract, position, avgCost
        )

    def positionEnd(self) -> None:
        self._bridge._end(self._generation, "positions", None)

    def openOrder(
        self, orderId: int, contract: object, order: object, orderState: object
    ) -> None:
        self._bridge._order(
            self._generation, "open", orderId, contract, order, orderState
        )

    def openOrderEnd(self) -> None:
        self._bridge._end(self._generation, "open_orders", None)

    def completedOrder(self, contract: object, order: object, orderState: object) -> None:
        self._bridge._order(
            self._generation,
            "completed",
            getattr(order, "orderId", 0),
            contract,
            order,
            orderState,
        )

    def completedOrdersEnd(self) -> None:
        self._bridge._end(self._generation, "completed_orders", None)

    def orderStatus(
        self,
        orderId: int,
        status: str,
        filled: object,
        remaining: object,
        avgFillPrice: float,
        permId: int,
        parentId: int,
        lastFillPrice: float,
        clientId: int,
        whyHeld: str,
        mktCapPrice: float = 0.0,
    ) -> None:
        del avgFillPrice, lastFillPrice, mktCapPrice
        self._bridge._order_status(
            self._generation, clientId, orderId, status,
            filled=filled, remaining=remaining, perm_id=permId,
            parent_id=parentId, why_held=whyHeld,
        )

    def execDetails(self, reqId: int, contract: object, execution: object) -> None:
        self._bridge._execution(
            self._generation, reqId, contract, execution
        )

    def execDetailsEnd(self, reqId: int) -> None:
        self._bridge._end(self._generation, "executions", reqId)

    def commissionReport(self, commissionReport: object) -> None:
        self._bridge._commission(self._generation, commissionReport)

    def commissionAndFeesReport(self, report: object) -> None:
        self._bridge._commission(self._generation, report)

    def pnl(
        self,
        reqId: int,
        dailyPnL: float,
        unrealizedPnL: float,
        realizedPnL: float,
    ) -> None:
        del dailyPnL, unrealizedPnL
        self._bridge._daily_pnl(self._generation, reqId, realizedPnL)

    def error(self, reqId: object, *arguments: object) -> None:
        # IB API 10.50 adds ``errorTime`` before ``errorCode``.  Accept that
        # shape and the older callback used by hermetic providers while never
        # retaining broker text or advanced reject JSON.
        code, reason = classify_ibkr_error_callback(arguments)
        request_id = reqId if type(reqId) is int else -1
        self._bridge._error(self._generation, request_id, code, reason)

    def connectionClosed(self) -> None:
        self._bridge._connection_closed(self._generation)


class IbkrWholeAccountReadBridge:
    """Synchronous normalized reads driven by official asynchronous callbacks.

    One bridge owns one dedicated requester and permits one collection at a
    time.  ``open_generation`` must be called after every successful socket
    connection and its returned callback object must be installed before any
    read.  Callbacks from an older object are ignored after reconnect.
    """

    def __init__(
        self,
        *,
        requester: IbkrReadRequester,
        exact_account_id: str,
        account_masked: str,
        execution_filter_factory: Callable[[], object] = SimpleNamespace,
        timeout_seconds: float = 10.0,
        clock: Callable[[], datetime] | None = None,
        session_timezone: str = "America/New_York",
    ) -> None:
        if not isinstance(requester, IbkrReadRequester):
            raise TypeError("IBKR read requester does not implement the read-only SDK subset")
        if not isinstance(exact_account_id, str) or not re.fullmatch(r"(?:U|DU)[0-9]+", exact_account_id):
            raise ValueError("exact IBKR account ID is required")
        if not isinstance(account_masked, str) or not re.fullmatch(r"(?:\*{4}|•{4})[0-9]{4}", account_masked):
            raise ValueError("masked account must expose exactly four trailing digits")
        if exact_account_id[-4:] != account_masked[-4:]:
            raise ValueError("exact and masked IBKR account bindings disagree")
        if not callable(execution_filter_factory):
            raise TypeError("execution_filter_factory must be callable")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._requester = requester
        self._exact_account_id = exact_account_id
        self._account_masked = account_masked
        self._execution_filter_factory = execution_filter_factory
        self._timeout = float(timeout_seconds)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._session_tz = ZoneInfo(session_timezone)
        self._condition = Condition(RLock())
        self._generation = 0
        self._authenticated_generation: int | None = None
        self._active: _Collection | None = None
        self._last: _CompletedCollection | None = None
        self._last_read_diagnostic: IbkrReadCollectionDiagnostic | None = None
        self._last_read_diagnostic_token: object | None = None
        self._diagnostic_nonce = 0
        self._next_request_id = 1_000_000
        self._collection_nonce = 0
        self._errors: list[SanitizedIbkrError] = []
        self._session_cleanup_failed = False
        # IBKR openOrder/orderStatus callbacks do not expose a provider update
        # timestamp. Preserve the first receipt for an unchanged broker fact so
        # the transport's required double collection can prove stability. A
        # changed fingerprint receives a new receipt timestamp.
        self._order_fact_times: dict[str, tuple[str, datetime]] = {}

    @property
    def generation(self) -> int:
        with self._condition:
            return self._generation

    @property
    def sanitized_errors(self) -> tuple[SanitizedIbkrError, ...]:
        with self._condition:
            return tuple(self._errors)

    @property
    def last_read_diagnostic(self) -> IbkrReadCollectionDiagnostic | None:
        """Last attempt's immutable status, never an account snapshot receipt."""
        with self._condition:
            return self._last_read_diagnostic

    def read_diagnostic_for(self, token: object) -> IbkrReadCollectionDiagnostic | None:
        """Return only this caller's attempt, never a newer concurrent read."""
        with self._condition:
            if token is None or token is not self._last_read_diagnostic_token:
                return None
            return self._last_read_diagnostic

    def position_valuation_inputs(self, collection_id: str):
        """Retain real contract IDs for an explicit read-only valuation probe.

        Missing currency/conId cannot break the existing conservative account
        diagnostic, but never becomes a symbol- or average-cost substitution.
        """
        with self._condition:
            completed = self._last
            if (
                completed is None
                or completed.collection_id != collection_id
                or self._authenticated_generation != self._generation
            ):
                raise BrokerContractViolation("IBKR_POSITION_VALUATION_COLLECTION_CHANGED")
            if completed.valuation_contracts is None:
                raise BrokerContractViolation("IBKR_POSITION_VALUATION_CONTRACT_SCOPE_UNPROVEN")
            return completed.valuation_contracts, completed.observation.snapshot

    def release_components(self) -> tuple[tuple[str, object, tuple[str, ...]], ...]:
        """Return retained SDK dependencies for outer lifecycle teardown.

        The bridge itself never disconnects or mutates the injected requester;
        production composition owns that lifecycle and can de-duplicate shared
        requester instances before releasing them.
        """
        return (
            (
                "ibkr_read_requester",
                self._requester,
                (
                    "reqAccountSummary",
                    "cancelAccountSummary",
                    *(("reqAccountUpdatesMulti", "cancelAccountUpdatesMulti")
                      if isinstance(self._requester, IbkrAccountUpdatesRequester) else ()),
                    "reqPositions",
                    "cancelPositions",
                    "reqAllOpenOrders",
                    "reqCompletedOrders",
                    "reqExecutions",
                    "reqPnL",
                    "cancelPnL",
                ),
            ),
            (
                "ibkr_execution_filter_factory",
                self._execution_filter_factory,
                ("__call__",),
            ),
            ("ibkr_read_clock", self._clock, ("__call__",)),
        )

    def open_generation(self, generation: int) -> _GenerationCallbacks:
        """Bind callbacks to a strictly newer connection generation."""
        if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
            raise ValueError("IBKR connection generation must be a positive integer")
        with self._condition:
            if generation <= self._generation:
                raise BrokerContractViolation("IBKR read generation did not advance")
            if self._active is not None:
                self._active.error = SanitizedIbkrError(
                    request_id=-1,
                    code=1100,
                    scope="connection_generation_changed",
                    received_at=self._now(),
                )
            self._generation = generation
            self._authenticated_generation = None
            self._session_cleanup_failed = False
            self._last = None
            self._last_read_diagnostic = None
            self._last_read_diagnostic_token = None
            self._condition.notify_all()
        return _GenerationCallbacks(self, generation)

    def get_account_base(
        self, exact_account_id: str, *, diagnostic_token: object | None = None
    ) -> CollectedObservation:
        self._assert_account(exact_account_id)
        with self._condition:
            self._last_read_diagnostic = None
            self._last_read_diagnostic_token = None
            if self._authenticated_generation != self._generation:
                raise BrokerCapabilityError("IBKR_READ_NOT_AUTHENTICATED")
            if self._active is not None:
                raise BrokerCapabilityError("IBKR_READ_COLLECTION_ALREADY_ACTIVE")
            # A new attempt supersedes the prior bounded collection.  Retire
            # its pages/reference lookup/valuation inputs before dispatch so a
            # failed refresh cannot leave older evidence discoverable as the
            # latest broker result.
            self._last = None
            summary_id = self._allocate_request_id()
            execution_id = self._allocate_request_id()
            pnl_id = self._allocate_request_id()
            self._diagnostic_nonce += 1
            collection = _Collection(
                generation=self._generation,
                summary_request_id=summary_id,
                execution_request_id=execution_id,
                pnl_request_id=pnl_id,
                started_at=self._now(),
                diagnostic_nonce=self._diagnostic_nonce,
                diagnostic_token=diagnostic_token,
                pnl_request_ids=[pnl_id],
            )
            self._active = collection
            # Bound the complete broker collection, including synchronous SDK
            # request dispatch, retry cancellation/dispatch, and the final
            # callback freeze.  Dispatch latency must not silently create a
            # fresh timeout window after work has already begun.
            deadline = time.monotonic() + self._timeout

        try:
            self._issue_requests(collection, deadline)
            self._wait_for_collection(collection, deadline)
            # Freeze exactly at the completed callback boundary. Late ambient
            # orderStatus/commission events belong to the next collection.
            with self._condition:
                # _wait_for_collection necessarily releases the condition when
                # it returns.  Revalidate every failure-bearing fact after
                # reacquiring it, before clearing _active or normalizing.  A
                # late UNSET/nonfinite/moved P&L callback, authentication loss,
                # or connection close at this boundary must invalidate the
                # collection instead of allowing an earlier value to escape.
                self._assert_collection_current(collection)
                if time.monotonic() >= deadline:
                    raise BrokerCapabilityError(
                        "IBKR_READ_TIMEOUT:collection_freeze"
                    )
                self._active = None
                completed = self._normalize(collection)
                self._last = completed
        except Exception:
            self._finish_read_diagnostic(collection, normalized=False)
            self._best_effort_cancel(collection)
            with self._condition:
                if self._active is collection:
                    self._active = None
            raise
        self._finish_read_diagnostic(collection, normalized=True)
        self._best_effort_cancel(collection)
        return completed.observation

    def list_order_family_page(
        self, exact_account_id: str, family: OrderFamily, cursor: str | None
    ) -> OrderFamilyPage:
        self._assert_account(exact_account_id)
        normalized_family = OrderFamily(family)
        if cursor is not None:
            raise BrokerContractViolation("IBKR read collection has exactly one page per family")
        with self._condition:
            if self._last is None or self._authenticated_generation != self._generation:
                raise BrokerCapabilityError("IBKR_READ_COLLECTION_UNAVAILABLE")
            return self._last.pages[normalized_family]

    def diagnose_finite_reads(
        self, exact_account_id: str, *, diagnostic_token: object | None = None
    ) -> IbkrFiniteReadDiagnostic:
        """Validate bounded account/order reads without subscribing to P&L.

        The result exposes status only. Neither it nor its temporary normalized
        data is published to production pages, reference recovery or valuation
        inputs. The ordinary account read still requires its real P&L callback.
        """
        self._assert_account(exact_account_id)
        with self._condition:
            self._last_read_diagnostic = None
            self._last_read_diagnostic_token = None
            if self._authenticated_generation != self._generation:
                raise BrokerCapabilityError("IBKR_READ_NOT_AUTHENTICATED")
            if self._active is not None:
                raise BrokerCapabilityError("IBKR_READ_COLLECTION_ALREADY_ACTIVE")
            self._last = None
            self._diagnostic_nonce += 1
            collection = _Collection(
                generation=self._generation,
                summary_request_id=self._allocate_request_id(),
                execution_request_id=self._allocate_request_id(),
                # No P&L request exists in this diagnostic. Empty subscription
                # tracking also prevents cancellation of an unissued request.
                pnl_request_id=-1,
                started_at=self._now(),
                diagnostic_nonce=self._diagnostic_nonce,
                diagnostic_token=diagnostic_token,
            )
            self._active = collection
            deadline = time.monotonic() + self._timeout
        normalized = False
        try:
            self._issue_finite_requests(collection, deadline)
            with self._condition:
                while True:
                    self._assert_collection_current(collection)
                    missing = _FINITE_READ_ENDS - collection.ends
                    missing_commissions = set(collection.executions) - set(collection.commissions)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        scope = ",".join(sorted(missing)) if missing else (
                            "commission" if missing_commissions else "collection_freeze"
                        )
                        raise BrokerCapabilityError(f"IBKR_READ_TIMEOUT:{scope}")
                    if not missing and not missing_commissions:
                        break
                    self._condition.wait(remaining)
                # Freeze under the same lock as the final failure/generation
                # checks; no late callback can turn a partial read into proof.
                self._active = None
                completed = self._normalize_finite(collection)
                diagnostic = IbkrFiniteReadDiagnostic(
                    observed_at=completed.observation.request_completed_at,
                    completed_reads=tuple(sorted(_FINITE_READ_ENDS)),
                )
                normalized = True
        finally:
            self._finish_read_diagnostic(collection, normalized=normalized)
            self._best_effort_cancel(collection)
            with self._condition:
                if self._active is collection:
                    self._active = None
        return diagnostic

    def lookup_equity_orders_by_client_ref(
        self, exact_account_id: str, client_refs: tuple[str, ...]
    ) -> ClientRefLookupResult:
        self._assert_account(exact_account_id)
        requested: list[str] = []
        for value in client_refs:
            try:
                normalized = str(UUID(str(value)))
            except (ValueError, AttributeError) as exc:
                raise BrokerContractViolation("IBKR client-ref lookup requires UUIDs") from exc
            requested.append(normalized)
        if len(requested) != len(set(requested)):
            raise BrokerContractViolation("IBKR client-ref lookup contains duplicates")
        with self._condition:
            last = self._last
            if last is None or self._authenticated_generation != self._generation:
                raise BrokerCapabilityError("IBKR_READ_COLLECTION_UNAVAILABLE")
            by_ref: dict[str, OrderSnapshot] = {}
            for order in last.all_equity_orders:
                if order.client_ref_id is None:
                    continue
                if order.client_ref_id in by_ref:
                    raise BrokerContractViolation("IBKR returned duplicate orderRef values")
                by_ref[order.client_ref_id] = order
            found = tuple(by_ref[item] for item in requested if item in by_ref)
            not_seen = tuple(item for item in requested if item not in by_ref)
            snapshot = last.observation.snapshot
        return ClientRefLookupResult(
            account_masked=self._account_masked,
            requested_client_refs=tuple(requested),
            found_orders=found,
            confirmed_absent_client_refs=(),
            not_seen_yet_client_refs=not_seen,
            observed_at=snapshot.observed_at,
            received_at=snapshot.received_at,
            complete=True,
        )

    def collect_session_facts(self, *, diagnostic_token: object | None = None) -> "IbkrFiniteSessionFacts":
        """Copy typed finite session inputs without P&L or production publication.

        The exact managed account remains private.  The caller must bind this
        result to its concrete runtime/generation; finite completion does not
        establish all-client, continuous-event or account-adjustment coverage.
        """
        return self._collect_session_inputs(diagnostic_token=diagnostic_token, include_exposure=False)

    def collect_session_exposure(self, *, diagnostic_token: object | None = None) -> IbkrFiniteSessionExposure:
        """Return same-collection facts/base/pages without publishing ``_last``."""
        return self._collect_session_inputs(diagnostic_token=diagnostic_token, include_exposure=True)

    def _collect_session_inputs(
        self, *, diagnostic_token: object | None, include_exposure: bool,
    ) -> "IbkrFiniteSessionFacts | IbkrFiniteSessionExposure":
        with self._condition:
            self._last_read_diagnostic = None
            self._last_read_diagnostic_token = None
            if self._authenticated_generation != self._generation:
                raise BrokerCapabilityError("IBKR_READ_NOT_AUTHENTICATED")
            if self._active is not None:
                raise BrokerCapabilityError("IBKR_READ_COLLECTION_ALREADY_ACTIVE")
            if self._session_cleanup_failed:
                raise BrokerCapabilityError("IBKR_SESSION_ACCOUNT_UPDATES_CLEANUP_UNCONFIRMED")
            if not isinstance(self._requester, IbkrAccountUpdatesRequester):
                raise BrokerCapabilityError("IBKR_SESSION_ACCOUNT_UPDATES_UNSUPPORTED")
            self._last = None
            self._diagnostic_nonce += 1
            collection = _Collection(
                generation=self._generation,
                summary_request_id=self._allocate_request_id(),
                execution_request_id=self._allocate_request_id(),
                pnl_request_id=-1,
                started_at=self._now(),
                account_values_channel="account_updates_multi",
                diagnostic_nonce=self._diagnostic_nonce,
                diagnostic_token=diagnostic_token,
            )
            self._active = collection
            deadline = time.monotonic() + self._timeout
        normalized = False
        try:
            self._issue_finite_requests(collection, deadline)
            with self._condition:
                while True:
                    self._assert_collection_current(collection)
                    missing = _SESSION_READ_ENDS - collection.ends
                    missing_commissions = set(collection.executions) - set(collection.commissions)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        scope = ",".join(sorted(missing)) if missing else (
                            "commission" if missing_commissions else "collection_freeze"
                        )
                        raise BrokerCapabilityError(f"IBKR_READ_TIMEOUT:{scope}")
                    if not missing and not missing_commissions:
                        break
                    self._condition.wait(remaining)
                # Keep ownership until cancellation dispatch finishes, so a
                # second collection cannot race this attempt's cleanup.
                completed = self._normalize_finite(collection)
                facts = self._copy_session_facts(collection, completed)
                result = self._copy_session_exposure(collection, completed, facts) if include_exposure else facts
                self._assert_before_deadline(deadline, "session_facts_normalization")
                normalized = True
        finally:
            self._finish_read_diagnostic(collection, normalized=normalized)
            # Cancellation dispatch is not a broker acknowledgement. A local
            # exception or synchronous SDK error must nevertheless not be
            # hidden as a successful reusable session. Both cancels are tried.
            cleanup_ok = self._cancel_session_reads(collection)
            with self._condition:
                if self._active is collection:
                    self._active = None
            if normalized and not cleanup_ok:
                raise BrokerCapabilityError("IBKR_SESSION_ACCOUNT_UPDATES_CLEANUP_UNCONFIRMED")
        return result

    def _copy_session_exposure(
        self, collection: _Collection, completed: _CompletedCollection, facts: "IbkrFiniteSessionFacts",
    ) -> IbkrFiniteSessionExposure:
        # Legacy normalization defaults some missing currency/time metadata.
        # That behavior is not changed, but cannot escape this new USD bundle.
        monetary = {"NetLiquidation", "TotalCashValue", "AvailableFunds", "BuyingPower", "SettledCash"}
        if any(currency != "USD" for key, (_value, currency) in collection.summary.items() if key in monetary):
            raise BrokerContractViolation("IBKR_SESSION_EXPOSURE_USD_UNPROVEN")
        if (any(item.currency != "USD" for item in facts.positions)
                or any(self._text(item.contract, "currency", optional=True).upper() != "USD" for item in collection.orders.values())):
            raise BrokerContractViolation("IBKR_SESSION_EXPOSURE_USD_UNPROVEN")
        if any(item.commission_currency != "USD" or item.currency != "USD" or item.source_executed_at is None for item in facts.executions):
            raise BrokerContractViolation("IBKR_SESSION_EXPOSURE_EXECUTION_METADATA_UNPROVEN")
        return IbkrFiniteSessionExposure(
            facts=facts, observation=completed.observation,
            order_family_pages=tuple(completed.pages[family] for family in OrderFamily),
        )

    def _copy_session_facts(self, collection: _Collection, completed: _CompletedCollection) -> "IbkrFiniteSessionFacts":
        # Import locally to keep callback/type definitions independent of the
        # optional adapter.  No old AccountSnapshot or mutable SDK object leaves
        # this API, and actual commission currency is never inferred as USD.
        from .ibkr_session_inputs import (
            IbkrFiniteSessionFacts,
            SessionExecutionFact,
            SessionOrderFact,
            SessionPositionFact,
        )

        def contract_id(contract: object) -> int | None:
            value = getattr(contract, "conId", None)
            if value is None:
                return None
            return self._integer(value, "conId", nonnegative=True) or None

        positions = tuple(
            SessionPositionFact(
                contract_id=contract_id(contract),
                symbol=self._symbol(contract),
                security_type=self._text(contract, "secType").upper(),
                currency=self._text(contract, "currency", optional=True).upper(),
                quantity=quantity,
                received_at=received,
            )
            for contract, quantity, _cost, received in collection.positions
            if quantity != 0
        )
        executions = []
        for exec_id, raw in collection.executions.items():
            execution = raw.execution
            commission, commission_currency, commission_received = collection.commissions[exec_id]
            source_time = getattr(execution, "time", None)
            # The legacy normalizer can use local receipt as a missing-time
            # fallback.  This new input contract must expose that absence.
            if source_time is not None and not isinstance(source_time, (str, datetime)):
                raise BrokerContractViolation("IBKR_SESSION_EXECUTION_SOURCE_TIME_INVALID")
            source_executed_at = (
                None if source_time is None or (isinstance(source_time, str) and not source_time.strip())
                else self._ibkr_time(source_time, raw.received_at)
            )
            if source_executed_at is None:
                source_time_basis = "ABSENT"
            elif isinstance(source_time, datetime):
                if source_time.tzinfo is None or source_time.utcoffset() is None:
                    raise BrokerContractViolation("IBKR_SESSION_EXECUTION_SOURCE_TIME_INVALID")
                source_time_basis = "PROVIDER_EXPLICIT_ZONE"
            else:
                # Classify the parser's actual branch, not merely the presence
                # of some text after HH:MM:SS.  The legacy parser interprets
                # blank/EST/EDT/US-Eastern aliases using _session_tz.  Even an
                # explicit Eastern label therefore retains that dependency.
                match = re.match(r"^(\d{8})[- ]+\s*(\d{2}:\d{2}:\d{2})(?:\s+(.+))?$", source_time.strip())
                if match is None:
                    raise BrokerContractViolation("IBKR_SESSION_EXECUTION_SOURCE_TIME_INVALID")
                source_zone = (match.group(3) or "").strip()
                source_time_basis = (
                    "CONFIGURED_SESSION_ZONE_INTERPRETATION"
                    if source_zone in {"US/Eastern", "America/New_York", "EST", "EDT", ""}
                    else "PROVIDER_EXPLICIT_ZONE"
                )
            side = self._text(execution, "side").upper()
            executions.append(SessionExecutionFact(
                exec_id=exec_id,
                contract_id=contract_id(raw.contract),
                symbol=self._symbol(raw.contract),
                security_type=self._text(raw.contract, "secType").upper(),
                currency=self._text(raw.contract, "currency", optional=True).upper(),
                side={"BOT": "BUY", "SLD": "SELL"}.get(side, side),
                quantity=self._decimal(getattr(execution, "shares", None), "execution.shares"),
                price=self._decimal(getattr(execution, "price", None), "execution.price"),
                source_executed_at=source_executed_at,
                source_time_basis=source_time_basis,
                received_at=raw.received_at,
                commission=commission,
                commission_currency=commission_currency,
                commission_received_at=commission_received,
            ))
        normalized_orders = {item.broker_order_id: item for item in completed.all_equity_orders}
        orders = []
        for identity, raw in collection.orders.items():
            normalized_order = normalized_orders.get(identity)
            orders.append(SessionOrderFact(
                order_identity=identity,
                contract_id=contract_id(raw.contract),
                family=self._classify_order(raw.contract, raw.order).value,
                terminal_observed=(
                    normalized_order.state.terminal if normalized_order is not None
                    else self._raw_order_terminal(raw, collection.order_statuses)
                ),
                state=normalized_order.state.value if normalized_order is not None else "UNNORMALIZED_OPTION_ORDER",
                blocking_warning_present=raw.blocking_warning_present,
            ))
        return IbkrFiniteSessionFacts(
            generation=collection.generation,
            collection_id=completed.collection_id,
            collection_started_at=collection.started_at,
            collection_completed_at=completed.observation.request_completed_at,
            net_liquidation=completed.observation.snapshot.funds.total_value,
            net_liquidation_currency=collection.summary["NetLiquidation"][1],
            net_liquidation_received_at=collection.summary_received_at["NetLiquidation"],
            positions=positions,
            executions=tuple(sorted(executions, key=lambda item: item.exec_id)),
            orders=tuple(sorted(orders, key=lambda item: item.order_identity)),
            completed_reads=tuple(sorted(_SESSION_READ_ENDS)),
            commission_conflict_observed=collection.commission_conflict_observed,
            orphan_commission_report_count=len(set(collection.commissions) - set(collection.executions)),
            account_values_source="IBKR_ACCOUNT_UPDATES_MULTI_V1",
            cash_value=self._decimal(collection.summary["TotalCashValue"][0], "cash_value"),
            cash_currency=collection.summary["TotalCashValue"][1],
            cash_received_at=collection.summary_received_at["TotalCashValue"],
        )

    @staticmethod
    def _assert_before_deadline(deadline: float, phase: str) -> None:
        if time.monotonic() >= deadline:
            raise BrokerCapabilityError(f"IBKR_READ_TIMEOUT:{phase}")

    def _note_read_event(self, collection: _Collection, channel: str, event: str) -> None:
        with self._condition:
            elapsed = max(0, int((time.monotonic() - collection.started_monotonic) * 1000))
            if event == "dispatch_started":
                collection.channel_attempts[channel] = collection.channel_attempts.get(channel, 0) + 1
                collection.channel_timings[(channel, "last_dispatch_started")] = elapsed
                collection.channel_timings.pop((channel, "last_dispatch_returned"), None)
            elif event == "dispatch_returned":
                collection.channel_timings[(channel, "last_dispatch_returned")] = elapsed
            collection.channel_timings.setdefault((channel, event), elapsed)

    def _dispatch_read(self, collection: _Collection, channel: str, request: Callable[[], None]) -> None:
        self._note_read_event(collection, channel, "dispatch_started")
        request()
        self._note_read_event(collection, channel, "dispatch_returned")

    def _finish_read_diagnostic(self, collection: _Collection, *, normalized: bool) -> None:
        # Freeze before cancellation: ambient cancellation callbacks cannot
        # rewrite what was observed during this bounded collection attempt.
        with self._condition:
            if (collection.generation != self._generation
                    or collection.diagnostic_nonce != self._diagnostic_nonce):
                return
            channels = []
            required = _SESSION_READ_ENDS if collection.account_values_channel == "account_updates_multi" else _FINITE_READ_ENDS
            for channel in sorted(required | {"daily_realized_pnl"}):
                timing = lambda event: collection.channel_timings.get((channel, event))
                if timing("dispatch_started") is None:
                    observation = "not_requested"
                elif timing("last_dispatch_returned") is None:
                    observation = "dispatch_did_not_return"
                elif channel in collection.ends:
                    observation = "end_callback_received"
                elif channel == "daily_realized_pnl" and collection.daily_realized_pnl is not None:
                    observation = "usable_callback_received"
                elif channel == "daily_realized_pnl" and collection.unavailable_pnl_request_ids:
                    observation = "value_unavailable"
                elif timing("first_callback") is None:
                    observation = "no_matching_callback_received"
                else:
                    observation = "required_completion_not_received"
                channels.append(IbkrReadChannelDiagnostic(
                    channel=channel,
                    request_attempts=collection.channel_attempts.get(channel, 0),
                    first_dispatch_started_ms=timing("dispatch_started"),
                    last_dispatch_started_ms=timing("last_dispatch_started"),
                    last_dispatch_returned_ms=timing("last_dispatch_returned"),
                    first_callback_ms=timing("first_callback"),
                    end_callback_ms=timing("end_callback"),
                    observation=observation,
                ))
            self._last_read_diagnostic = IbkrReadCollectionDiagnostic(
                channels=tuple(channels),
                elapsed_ms=max(0, int((time.monotonic() - collection.started_monotonic) * 1000)),
                normalization_completed=normalized,
                commission_reports_missing=bool(set(collection.executions) - set(collection.commissions)),
            )
            self._last_read_diagnostic_token = collection.diagnostic_token

    def _issue_finite_requests(self, collection: _Collection, deadline: float) -> None:
        try:
            if collection.account_values_channel == "account_updates_multi":
                # A fresh exact-account request returns real account values,
                # not a timestamp-refreshed cache of the summary subscription.
                # False selects account values as well as currency positions.
                self._dispatch_read(collection, "account_updates_multi", lambda: self._requester.reqAccountUpdatesMulti(
                    collection.summary_request_id, self._exact_account_id, "", False
                ))
            else:
                self._dispatch_read(collection, "account_summary", lambda: self._requester.reqAccountSummary(
                    collection.summary_request_id, "All", _SUMMARY_TAGS
                ))
            self._assert_before_deadline(deadline, collection.account_values_channel + "_dispatch")
            self._dispatch_read(collection, "positions", self._requester.reqPositions)
            self._assert_before_deadline(deadline, "positions_dispatch")
            self._dispatch_read(collection, "open_orders", self._requester.reqAllOpenOrders)
            self._assert_before_deadline(deadline, "open_orders_dispatch")
            self._dispatch_read(collection, "completed_orders", lambda: self._requester.reqCompletedOrders(False))
            self._assert_before_deadline(deadline, "completed_orders_dispatch")
            self._dispatch_read(collection, "executions", lambda: self._requester.reqExecutions(
                collection.execution_request_id, self._execution_filter_factory()
            ))
            self._assert_before_deadline(deadline, "executions_dispatch")
        except BrokerCapabilityError:
            raise
        except Exception:
            raise BrokerCapabilityError("IBKR_READ_REQUEST_DISPATCH_FAILED") from None

    def _issue_requests(self, collection: _Collection, deadline: float) -> None:
        self._issue_finite_requests(collection, deadline)
        try:
            # ``AccountSummary.RealizedPnL`` has an account/window-dependent
            # period and is deliberately not accepted as current-day authority.
            # The account-level PnL subscription exposes IBKR's dedicated daily
            # realized value and is bound to this collection's request ID.
            self._dispatch_read(collection, "daily_realized_pnl", lambda: self._requester.reqPnL(
                collection.pnl_request_id, self._exact_account_id, ""
            ))
            self._assert_before_deadline(
                deadline, "daily_realized_pnl_initial_dispatch"
            )
        except BrokerCapabilityError:
            raise
        except Exception:
            raise BrokerCapabilityError("IBKR_READ_PNL_INITIAL_DISPATCH_FAILED") from None

    def _wait_for_collection(self, collection: _Collection, deadline: float) -> None:
        required = _FINITE_READ_ENDS
        # IBKR documents reqPnL as a subscription and notes that aggregate
        # account P&L can take several seconds.  Give the initial subscription
        # half of the existing bounded deadline.  Only when every finite read
        # has completed do we cancel it and make one fresh-ID retry; the retry
        # never lengthens the caller's configured timeout.
        retry_at = deadline - (self._timeout / _PNL_ATTEMPT_LIMIT)
        while True:
            retry: tuple[int, int] | None = None
            with self._condition:
                self._assert_collection_current(collection)
                missing_commissions = set(collection.executions) - set(collection.commissions)
                finite_reads_complete = (
                    required.issubset(collection.ends) and not missing_commissions
                )
                now = time.monotonic()
                remaining = deadline - now
                if remaining <= 0:
                    missing = sorted(required - collection.ends)
                    if missing:
                        scope = ",".join(missing)
                    elif missing_commissions:
                        scope = "commission"
                    elif collection.daily_realized_pnl is not None:
                        scope = "collection_completion"
                    else:
                        detail = (
                            "unavailable"
                            if collection.unavailable_pnl_request_ids
                            else "no_callback"
                        )
                        attempt = (
                            "retry_exhausted"
                            if len(collection.pnl_request_ids) >= _PNL_ATTEMPT_LIMIT
                            else "initial"
                        )
                        scope = f"daily_realized_pnl_{attempt}_{detail}"
                    raise BrokerCapabilityError(f"IBKR_READ_TIMEOUT:{scope}")
                if finite_reads_complete and collection.daily_realized_pnl is not None:
                    return
                if (
                    finite_reads_complete
                    and collection.daily_realized_pnl is None
                    and len(collection.pnl_request_ids) < _PNL_ATTEMPT_LIMIT
                    and now >= retry_at
                ):
                    old_request_id = collection.pnl_request_id
                    new_request_id = self._allocate_request_id()
                    retry = (old_request_id, new_request_id)
                if retry is None:
                    wake_at = (
                        min(deadline, retry_at)
                        if (
                            finite_reads_complete
                            and collection.daily_realized_pnl is None
                            and len(collection.pnl_request_ids) < _PNL_ATTEMPT_LIMIT
                        )
                        else deadline
                    )
                    self._condition.wait(max(0.0, wake_at - now))
                    continue
            if retry is not None:
                self._retry_daily_pnl(collection, *retry, deadline)

    def _assert_collection_current(self, collection: _Collection) -> None:
        """Validate collection identity, callback health, and authentication.

        The caller must hold ``self._condition`` so this check and any
        subsequent freeze are one atomic callback boundary.
        """

        if (
            self._active is not collection
            or collection.generation != self._generation
        ):
            raise BrokerCapabilityError("IBKR_READ_COLLECTION_GENERATION_LOST")
        if collection.error is not None:
            reason = (
                ":API_READ_ONLY"
                if collection.error.reason == "API_READ_ONLY"
                else ""
            )
            raise BrokerCapabilityError(
                f"IBKR_READ_CALLBACK_ERROR:{collection.error.code}:"
                f"{collection.error.scope}{reason}"
            )
        if self._authenticated_generation != self._generation:
            raise BrokerCapabilityError("IBKR_READ_AUTHENTICATION_LOST")

    def _retry_daily_pnl(
        self,
        collection: _Collection,
        old_request_id: int,
        new_request_id: int,
        deadline: float,
    ) -> None:
        """Atomically retire one subscription and dispatch the sole retry."""

        with self._condition:
            if (
                self._active is not collection
                or collection.generation != self._generation
                or collection.daily_realized_pnl is not None
                or collection.pnl_request_id != old_request_id
                or len(collection.pnl_request_ids) >= _PNL_ATTEMPT_LIMIT
            ):
                if collection.daily_realized_pnl is not None:
                    return
                raise BrokerCapabilityError("IBKR_READ_COLLECTION_GENERATION_LOST")
            self._assert_before_deadline(
                deadline, "daily_realized_pnl_retry_cancel"
            )
            # Retire the old ID before invoking cancelPnL.  A conforming or
            # test-double requester may synchronously deliver a final callback
            # from inside cancellation; it belongs to the retired subscription
            # and must not be relabeled as evidence for the fresh request.
            collection.pnl_request_id = new_request_id
            try:
                self._requester.cancelPnL(old_request_id)
            except Exception:
                raise BrokerCapabilityError("IBKR_READ_PNL_RETRY_CANCEL_FAILED") from None
            collection.cancelled_pnl_request_ids.add(old_request_id)
            self._assert_collection_current(collection)
            self._assert_before_deadline(
                deadline, "daily_realized_pnl_retry_cancel"
            )
            collection.pnl_request_ids.append(new_request_id)
            try:
                self._dispatch_read(collection, "daily_realized_pnl", lambda: self._requester.reqPnL(
                    new_request_id, self._exact_account_id, ""
                ))
            except Exception:
                raise BrokerCapabilityError("IBKR_READ_PNL_RETRY_DISPATCH_FAILED") from None
            self._assert_collection_current(collection)
            self._assert_before_deadline(
                deadline, "daily_realized_pnl_retry_dispatch"
            )

    def _normalize(self, collection: _Collection) -> _CompletedCollection:
        completed = self._normalize_finite(collection)
        if collection.daily_realized_pnl is None:
            raise BrokerContractViolation("IBKR_DAILY_REALIZED_PNL_MISSING")
        realized, realized_received_at = collection.daily_realized_pnl
        snapshot = replace(
            completed.observation.snapshot,
            daily_realized_pnl=realized,
            daily_realized_pnl_complete=True,
            risk_evidence_authoritative=True,
            risk_evidence_source="ibkr:reqPnL.realizedPnL:current-day",
            risk_evidence_as_of=realized_received_at,
        )
        return replace(completed, observation=replace(completed.observation, snapshot=snapshot))

    def _normalize_finite(self, collection: _Collection) -> _CompletedCollection:
        """Shared validation; missing risk facts retain AccountSnapshot defaults."""
        completed_at = self._now()
        summary = self._normalize_summary(collection.summary)
        raw_by_family: dict[OrderFamily, list[_RawOrder]] = {
            family: [] for family in OrderFamily
        }
        for raw in collection.orders.values():
            raw_by_family[self._classify_order(raw.contract, raw.order)].append(raw)

        order_aliases: dict[str, str] = {}
        for key, raw in collection.orders.items():
            for alias in self._order_aliases(raw.order):
                if alias in order_aliases and order_aliases[alias] != key:
                    raise BrokerContractViolation("IBKR_ORDER_IDENTITY_COLLISION")
                order_aliases[alias] = key

        execution_by_order: dict[
            str, list[tuple[str, _RawExecution, Decimal, str, datetime]]
        ] = {}
        for exec_id, raw_execution in collection.executions.items():
            commission, currency, commission_received = collection.commissions[exec_id]
            if currency not in ("", "USD"):
                raise BrokerContractViolation("IBKR_NON_USD_COMMISSION_UNSUPPORTED")
            key = next(
                (
                    order_aliases[alias]
                    for alias in self._execution_aliases(raw_execution.execution)
                    if alias in order_aliases
                ),
                None,
            )
            if key is None:
                raise BrokerContractViolation("IBKR_EXECUTION_WITHOUT_ORDER_EVIDENCE")
            execution_by_order.setdefault(key, []).append(
                (
                    exec_id,
                    raw_execution,
                    commission,
                    currency or "USD",
                    commission_received,
                )
            )

        normalized: dict[OrderFamily, list[OrderSnapshot]] = {
            family: [] for family in OrderFamily
        }
        for family in (OrderFamily.STANDARD_EQUITY, OrderFamily.ADVANCED_EQUITY):
            for raw in raw_by_family[family]:
                normalized[family].append(
                    self._normalize_order(
                        raw,
                        execution_by_order.get(self._raw_order_key(raw.order), ()),
                        collection.order_statuses,
                        protection_statuses=collection.protection_statuses,
                        protection_status_conflicts=collection.protection_status_conflicts,
                        protection_order_conflicts=collection.protection_order_conflicts,
                        collection_started_at=collection.started_at,
                        collection_completed_at=completed_at,
                    )
                )

        active_option_count = sum(
            not self._raw_order_terminal(item, collection.order_statuses)
            for item in raw_by_family[OrderFamily.OPTION]
        )
        positions, option_position_count = self._normalize_positions(
            collection.positions,
            tuple(normalized[OrderFamily.STANDARD_EQUITY])
            + tuple(normalized[OrderFamily.ADVANCED_EQUITY]),
        )
        account_type, total_value, cash, buying_power, unleveraged, unsettled = summary
        snapshot = AccountSnapshot(
            account_masked=self._account_masked,
            observed_at=completed_at,
            received_at=completed_at,
            # A completed collection is possible only for the exact managed
            # account authenticated on this live connection generation.  The
            # generation remains cryptographically bound into collection_id;
            # AccountSnapshot.account_state uses the broker-neutral lifecycle
            # vocabulary consumed by reconciliation and readiness.
            account_state="active",
            account_type=account_type,
            funds=FundsSnapshot(
                total_value=total_value,
                cash=cash,
                buying_power=buying_power,
                unleveraged_buying_power=unleveraged,
                unsettled_funds=unsettled,
                unsettled_funds_is_order_gating=unsettled is not None,
            ),
            equity_positions=positions,
            equity_orders=(),
            option_position_count=option_position_count,
            option_order_count=0,
            advanced_order_count=0,
            standard_equity_positions_complete=True,
            standard_equity_orders_complete=False,
            option_positions_complete=True,
            option_orders_complete=False,
            advanced_orders_complete=False,
            auth_point_in_time=True,
        )
        self._collection_nonce += 1
        collection_id = hashlib.sha256(
            (
                f"ibkr-read-v1:{collection.generation}:{self._collection_nonce}:"
                f"{collection.summary_request_id}:{collection.execution_request_id}:"
                f"{','.join(str(item) for item in collection.pnl_request_ids)}:"
                f"{collection.started_at.isoformat()}:{completed_at.isoformat()}"
            ).encode("ascii")
        ).hexdigest()
        all_equity = tuple(
            item
            for family in (OrderFamily.STANDARD_EQUITY, OrderFamily.ADVANCED_EQUITY)
            for item in normalized[family]
        )
        watermark = self._order_watermark(all_equity, active_option_count)
        observation = CollectedObservation(
            snapshot=snapshot,
            collection_id=collection_id,
            request_started_at=collection.started_at,
            request_completed_at=completed_at,
            order_event_watermark=watermark,
        )
        pages: dict[OrderFamily, OrderFamilyPage] = {}
        for family in OrderFamily:
            orders = tuple(normalized[family]) if family is not OrderFamily.OPTION else ()
            active_count = (
                active_option_count
                if family is OrderFamily.OPTION
                else sum(not item.state.terminal for item in orders)
            )
            pages[family] = OrderFamilyPage(
                account_masked=self._account_masked,
                family=family,
                snapshot_token=None,
                collection_id=collection_id,
                page_id=hashlib.sha256(
                    f"{collection_id}:{family.value}".encode("ascii")
                ).hexdigest(),
                page_index=0,
                page_complete=True,
                provider_watermark=watermark,
                orders=orders,
                active_order_count=active_count,
                observed_at=completed_at,
                received_at=completed_at,
                next_cursor=None,
            )
        try:
            valuation_contracts = tuple(
                PositionValuationContract.from_callback(contract, quantity, received)
                for contract, quantity, _cost, received in collection.positions
                if quantity != 0
            )
        except PositionValuationError:
            valuation_contracts = None
        return _CompletedCollection(
            collection_id=collection_id,
            observation=observation,
            pages=pages,
            all_equity_orders=all_equity,
            valuation_contracts=valuation_contracts,
        )

    def _normalize_summary(
        self, values: dict[str, tuple[str, str]]
    ) -> tuple[str, Decimal, Decimal, Decimal, Decimal, Decimal | None]:
        required = {
            "AccountType",
            "NetLiquidation",
            "TotalCashValue",
            "AvailableFunds",
            "BuyingPower",
        }
        if not required.issubset(values):
            raise BrokerContractViolation("IBKR_ACCOUNT_SUMMARY_INCOMPLETE")
        account_type = values["AccountType"][0].strip()
        if not account_type:
            raise BrokerContractViolation("IBKR_ACCOUNT_TYPE_MISSING")
        total = self._decimal(values["NetLiquidation"][0], "NetLiquidation")
        cash = self._decimal(values["TotalCashValue"][0], "TotalCashValue")
        available = self._decimal(values["AvailableFunds"][0], "AvailableFunds")
        buying_power = self._decimal(values["BuyingPower"][0], "BuyingPower")
        settled = (
            self._decimal(values["SettledCash"][0], "SettledCash")
            if "SettledCash" in values
            else None
        )
        no_borrow_candidates = [cash, available, buying_power]
        if settled is not None:
            no_borrow_candidates.append(settled)
        unleveraged = min(no_borrow_candidates)
        unsettled = max(cash - settled, Decimal("0")) if settled is not None else None
        return account_type, total, cash, buying_power, unleveraged, unsettled

    def _normalize_positions(
        self,
        raw_positions: list[tuple[object, Decimal, Decimal, datetime]],
        equity_orders: tuple[OrderSnapshot, ...],
    ) -> tuple[tuple[PositionSnapshot, ...], int]:
        held: dict[str, Decimal] = {}
        for order in equity_orders:
            if order.side is BrokerSide.SELL and not order.state.terminal:
                remaining = order.requested_quantity - order.cumulative_filled_quantity
                held[order.symbol] = held.get(order.symbol, Decimal("0")) + remaining
        equities: list[PositionSnapshot] = []
        option_count = 0
        seen: set[tuple[str, str]] = set()
        for contract, quantity, average_price, _received in raw_positions:
            if quantity == 0:
                continue
            sec_type = self._text(contract, "secType").upper()
            symbol = self._symbol(contract)
            key = (sec_type, symbol)
            if key in seen:
                raise BrokerContractViolation("IBKR_DUPLICATE_POSITION_CALLBACK")
            seen.add(key)
            if sec_type in {"OPT", "FOP"}:
                option_count += 1
                continue
            if sec_type != "STK":
                raise BrokerContractViolation("IBKR_UNSUPPORTED_MATERIAL_POSITION")
            if quantity < 0:
                raise BrokerContractViolation("IBKR_SHORT_POSITION_CANNOT_BE_NORMALIZED")
            reserved = min(quantity, held.get(symbol, Decimal("0")))
            equities.append(
                PositionSnapshot(
                    symbol=symbol,
                    quantity=quantity,
                    sellable_quantity=quantity - reserved,
                    held_for_sells=reserved,
                    average_price=average_price if average_price > 0 else None,
                    asset_class="equity",
                )
            )
        return tuple(sorted(equities, key=lambda item: item.symbol)), option_count

    def _normalize_order(
        self,
        raw: _RawOrder,
        raw_fills: tuple[tuple[str, _RawExecution, Decimal, str, datetime], ...]
        | list[tuple[str, _RawExecution, Decimal, str, datetime]],
        statuses: dict[tuple[int, int], tuple[str, datetime]],
        *,
        protection_statuses: dict[tuple[int, int], IbkrOrderStatusFact | None],
        protection_status_conflicts: set[tuple[int, int]],
        protection_order_conflicts: set[str],
        collection_started_at: datetime,
        collection_completed_at: datetime,
    ) -> OrderSnapshot:
        order = raw.order
        requested = self._decimal(getattr(order, "totalQuantity", None), "totalQuantity")
        if requested <= 0:
            raise BrokerContractViolation("IBKR_ORDER_QUANTITY_INVALID")
        client_id = self._integer(getattr(order, "clientId", 0), "clientId", nonnegative=True)
        order_id = self._integer(getattr(order, "orderId", 0), "orderId", nonnegative=True)
        status_record = statuses.get((client_id, order_id))
        status = status_record[0] if status_record else raw.status
        fills = tuple(
            sorted(
                (
                    self._normalize_fill(exec_id, item, commission, currency)
                    for (
                        exec_id,
                        item,
                        commission,
                        currency,
                        _commission_received,
                    ) in raw_fills
                ),
                key=lambda item: (item.executed_at, item.fill_id),
            )
        )
        cumulative = sum((item.quantity for item in fills), Decimal("0"))
        if cumulative > requested:
            raise BrokerContractViolation("IBKR_EXECUTIONS_EXCEED_ORDER_QUANTITY")
        state = self._state(status, cumulative, requested)
        if raw.blocking_warning_present and state in {
            BrokerOrderState.CONFIRMED,
            BrokerOrderState.PARTIALLY_FILLED,
        }:
            # A warning invalidates positive evidence that an order is working,
            # but cannot negate independent terminal status or complete fill
            # evidence. This keeps warned stops out of verified protection
            # without turning cancelled/filled orders back into active orders.
            state = BrokerOrderState.UNKNOWN
        received = max(
            [raw.received_at]
            + ([status_record[1]] if status_record else [])
            + [
                max(item.received_at, commission_received)
                for (
                    _exec_id,
                    item,
                    _commission,
                    _currency,
                    commission_received,
                ) in raw_fills
            ]
        )
        order_type, limit_price, stop_price = self._order_type(order)
        tif = self._text(order, "tif").upper()
        try:
            time_in_force = {"DAY": TimeInForce.GFD, "GTC": TimeInForce.GTC}[tif]
        except KeyError:
            raise BrokerContractViolation("IBKR_ORDER_TIF_UNSUPPORTED") from None
        outside_rth = self._boolean(getattr(order, "outsideRth", False), "outsideRth")
        market_hours = MarketHours.EXTENDED if outside_rth else MarketHours.REGULAR
        if self._boolean(getattr(order, "includeOvernight", False), "includeOvernight"):
            market_hours = MarketHours.ALL_DAY
        action = self._text(order, "action").upper()
        try:
            side = {"BUY": BrokerSide.BUY, "SELL": BrokerSide.SELL}[action]
        except KeyError:
            raise BrokerContractViolation("IBKR_ORDER_SIDE_UNSUPPORTED") from None
        order_ref = self._client_ref(getattr(order, "orderRef", ""))
        identity = self._raw_order_key(order)
        evidence = raw.protection_evidence
        if evidence is not None:
            evidence = replace(
                evidence,
                blocking_warning_present=raw.blocking_warning_present,
                status=(None if (client_id, order_id) in protection_status_conflicts or identity in protection_order_conflicts else protection_statuses.get((client_id, order_id))),
                collection_started_at=collection_started_at,
                collection_completed_at=collection_completed_at,
            )
        evidence_facts = protection_evidence_facts(evidence)
        fact_fingerprint = hashlib.sha256(
            repr(
                (
                    identity,
                    self._symbol(raw.contract),
                    side.value,
                    order_type.value,
                    str(requested),
                    time_in_force.value,
                    market_hours.value,
                    str(limit_price) if limit_price is not None else None,
                    str(stop_price) if stop_price is not None else None,
                    order_ref,
                    status,
                    raw.blocking_warning_present,
                    evidence_facts,
                    tuple(
                        (
                            fill.fill_id,
                            str(fill.quantity),
                            str(fill.price),
                            fill.executed_at.isoformat(),
                            str(fill.fee),
                            (
                                str(fill.provider_commission)
                                if fill.provider_commission is not None
                                else None
                            ),
                            fill.provider_commission_currency,
                        )
                        for fill in fills
                    ),
                )
            ).encode("ascii")
        ).hexdigest()
        provider_time = self._provider_order_time(raw, fills, received)
        updated = self._stable_order_time(identity, fact_fingerprint, provider_time)
        if updated > received:
            raise BrokerContractViolation("IBKR_ORDER_TIMESTAMP_FOLLOWS_RECEIPT")
        return OrderSnapshot(
            broker_order_id=identity,
            account_masked=self._account_masked,
            symbol=self._symbol(raw.contract),
            side=side,
            order_type=order_type,
            state=state,
            requested_quantity=requested,
            cumulative_filled_quantity=cumulative,
            market_hours=market_hours,
            time_in_force=time_in_force,
            broker_updated_at=updated,
            received_at=received,
            limit_price=limit_price,
            stop_price=stop_price,
            client_ref_id=order_ref,
            fills=fills,
            broker_perm_id=(
                self._integer(getattr(order, "permId", 0), "permId", nonnegative=True)
                or None
            ),
            broker_contract_id=(
                self._integer(getattr(raw.contract, "conId", 0), "conId", nonnegative=True)
                or None
            ),
            ibkr_protection_evidence=evidence,
        )

    def _provider_order_time(
        self,
        raw: _RawOrder,
        fills: tuple[FillSnapshot, ...],
        fallback: datetime,
    ) -> datetime:
        if raw.completed_time:
            return self._ibkr_time(raw.completed_time, fallback)
        if fills and self._raw_order_terminal(raw, {}):
            return max(fill.executed_at for fill in fills)
        return fallback

    def _stable_order_time(
        self, identity: str, fingerprint: str, candidate: datetime
    ) -> datetime:
        previous = self._order_fact_times.get(identity)
        if previous is not None and previous[0] == fingerprint:
            return previous[1]
        self._order_fact_times[identity] = (fingerprint, candidate)
        return candidate

    def _normalize_fill(
        self,
        exec_id: str,
        raw: _RawExecution,
        commission: Decimal,
        currency: str,
    ) -> FillSnapshot:
        execution = raw.execution
        quantity = self._decimal(getattr(execution, "shares", None), "execution.shares")
        price = self._decimal(getattr(execution, "price", None), "execution.price")
        if quantity <= 0 or price <= 0:
            raise BrokerContractViolation("IBKR_EXECUTION_VALUES_INVALID")
        # Negative commissions are rebates. Zero is conservative for cost and
        # satisfies the normalized fee invariant without inventing a credit.
        fee = max(commission, Decimal("0"))
        return FillSnapshot(
            fill_id=exec_id,
            quantity=quantity,
            price=price,
            executed_at=self._ibkr_time(getattr(execution, "time", None), raw.received_at),
            fee=fee,
            provider_commission=commission,
            provider_commission_currency=currency,
            broker_perm_id=(
                self._integer(
                    getattr(execution, "permId", 0),
                    "execution.permId",
                    nonnegative=True,
                )
                or None
            ),
        )

    def _classify_order(self, contract: object, order: object) -> OrderFamily:
        sec_type = self._text(contract, "secType").upper()
        if sec_type in {"OPT", "FOP"}:
            return OrderFamily.OPTION
        if sec_type != "STK":
            raise BrokerContractViolation("IBKR_UNSUPPORTED_MATERIAL_ORDER_FAMILY")
        advanced = any(
            (
                self._integer(getattr(order, "parentId", 0), "parentId", nonnegative=True) != 0,
                bool(self._text(order, "ocaGroup", optional=True)),
                bool(getattr(order, "conditions", ()) or ()),
                bool(self._text(order, "algoStrategy", optional=True)),
                bool(self._text(order, "hedgeType", optional=True)),
                self._boolean(getattr(order, "includeOvernight", False), "includeOvernight"),
            )
        )
        return OrderFamily.ADVANCED_EQUITY if advanced else OrderFamily.STANDARD_EQUITY

    def _raw_order_terminal(
        self,
        raw: _RawOrder,
        statuses: dict[tuple[int, int], tuple[str, datetime]],
    ) -> bool:
        client_id = self._integer(getattr(raw.order, "clientId", 0), "clientId", nonnegative=True)
        order_id = self._integer(getattr(raw.order, "orderId", 0), "orderId", nonnegative=True)
        status = statuses.get((client_id, order_id), (raw.status, raw.received_at))[0]
        return status.lower().replace(" ", "") in {
            "filled",
            "cancelled",
            "apicancelled",
            "inactive",
        }

    @staticmethod
    def _state(status: str, cumulative: Decimal, requested: Decimal) -> BrokerOrderState:
        value = status.lower().replace(" ", "")
        if cumulative == requested and requested > 0:
            return BrokerOrderState.FILLED
        if value == "filled":
            # A bounded execution query did not prove the fill quantity.
            return BrokerOrderState.UNKNOWN
        if value in {"cancelled", "apicancelled"}:
            return (
                BrokerOrderState.PARTIALLY_FILLED_REST_CANCELLED
                if cumulative > 0
                else BrokerOrderState.CANCELLED
            )
        if value == "pendingcancel":
            return BrokerOrderState.PENDING_CANCELLED
        if value in {"pendingsubmit", "apipending"}:
            return BrokerOrderState.PENDING
        if value == "presubmitted":
            return BrokerOrderState.QUEUED
        if value == "submitted":
            return BrokerOrderState.PARTIALLY_FILLED if cumulative > 0 else BrokerOrderState.CONFIRMED
        if value == "inactive":
            return BrokerOrderState.UNKNOWN
        return BrokerOrderState.UNKNOWN

    def _order_type(
        self, order: object
    ) -> tuple[EquityOrderType, Decimal | None, Decimal | None]:
        value = self._text(order, "orderType").upper()
        if value == "MKT":
            return EquityOrderType.MARKET, None, None
        if value == "LMT":
            return EquityOrderType.LIMIT, self._positive_price(order, "lmtPrice"), None
        if value == "STP":
            return EquityOrderType.STOP_MARKET, None, self._positive_price(order, "auxPrice")
        if value == "STP LMT":
            return (
                EquityOrderType.STOP_LIMIT,
                self._positive_price(order, "lmtPrice"),
                self._positive_price(order, "auxPrice"),
            )
        raise BrokerContractViolation("IBKR_ORDER_TYPE_UNSUPPORTED")

    def _positive_price(self, value: object, field_name: str) -> Decimal:
        price = self._decimal(getattr(value, field_name, None), field_name)
        if price <= 0:
            raise BrokerContractViolation("IBKR_ORDER_PRICE_INVALID")
        return price

    def _ibkr_time(self, value: object, fallback: datetime) -> datetime:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                raise BrokerContractViolation("IBKR_TIMESTAMP_LACKS_TIMEZONE")
            return value.astimezone(timezone.utc)
        if not isinstance(value, str) or not value.strip():
            return fallback
        text = value.strip()
        match = re.match(r"^(\d{8})[- ]+\s*(\d{2}:\d{2}:\d{2})(?:\s+(.+))?$", text)
        if not match:
            raise BrokerContractViolation("IBKR_TIMESTAMP_FORMAT_UNSUPPORTED")
        naive = datetime.strptime(f"{match.group(1)} {match.group(2)}", "%Y%m%d %H:%M:%S")
        zone_name = (match.group(3) or "").strip()
        if zone_name in {"UTC", "GMT"}:
            zone = timezone.utc
        elif zone_name in {"US/Eastern", "America/New_York", "EST", "EDT", ""}:
            zone = self._session_tz
        else:
            try:
                zone = ZoneInfo(zone_name)
            except Exception:
                raise BrokerContractViolation("IBKR_TIMESTAMP_ZONE_UNSUPPORTED") from None
        return naive.replace(tzinfo=zone).astimezone(timezone.utc)

    @staticmethod
    def _client_ref(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        normalized = value.strip().lower()
        if not _UUID.fullmatch(normalized):
            return None
        return str(UUID(normalized))

    @staticmethod
    def _integer(value: object, field_name: str, *, nonnegative: bool = False) -> int:
        if isinstance(value, bool):
            raise BrokerContractViolation(f"IBKR_{field_name.upper()}_INVALID")
        try:
            result = int(value)
        except (TypeError, ValueError):
            raise BrokerContractViolation(f"IBKR_{field_name.upper()}_INVALID") from None
        if result != value or (nonnegative and result < 0):
            raise BrokerContractViolation(f"IBKR_{field_name.upper()}_INVALID")
        return result

    @staticmethod
    def _decimal(value: object, field_name: str) -> Decimal:
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            raise BrokerContractViolation(f"IBKR_{field_name.upper()}_INVALID") from None
        if not result.is_finite():
            raise BrokerContractViolation(f"IBKR_{field_name.upper()}_INVALID")
        return result

    @staticmethod
    def _boolean(value: object, field_name: str) -> bool:
        if type(value) is not bool:
            raise BrokerContractViolation(f"IBKR_{field_name.upper()}_INVALID")
        return value

    @staticmethod
    def _text(value: object, field_name: str, *, optional: bool = False) -> str:
        field_value = getattr(value, field_name, "" if optional else None)
        if not isinstance(field_value, str) or (not optional and not field_value.strip()):
            raise BrokerContractViolation(f"IBKR_{field_name.upper()}_INVALID")
        return field_value.strip()

    def _symbol(self, contract: object) -> str:
        symbol = self._text(contract, "symbol").upper()
        if not _SYMBOL.fullmatch(symbol):
            raise BrokerContractViolation("IBKR_SYMBOL_INVALID")
        return symbol

    def _raw_order_key(self, order: object) -> str:
        client_id = self._integer(getattr(order, "clientId", 0), "clientId", nonnegative=True)
        order_id = self._integer(getattr(order, "orderId", 0), "orderId", nonnegative=True)
        if order_id > 0:
            return f"ibkr:{client_id}:{order_id}"
        perm_id = self._integer(getattr(order, "permId", 0), "permId", nonnegative=True)
        if perm_id > 0:
            return f"ibkr:perm:{perm_id}"
        raise BrokerContractViolation("IBKR_ORDER_IDENTITY_MISSING")

    def _order_aliases(self, order: object) -> tuple[str, ...]:
        aliases = [self._raw_order_key(order)]
        perm_id = self._integer(getattr(order, "permId", 0), "permId", nonnegative=True)
        if perm_id > 0:
            aliases.append(f"ibkr:perm:{perm_id}")
        return tuple(dict.fromkeys(aliases))

    def _execution_aliases(self, execution: object) -> tuple[str, ...]:
        client_id = self._integer(getattr(execution, "clientId", 0), "execution.clientId", nonnegative=True)
        order_id = self._integer(getattr(execution, "orderId", 0), "execution.orderId", nonnegative=True)
        aliases: list[str] = []
        if order_id > 0:
            aliases.append(f"ibkr:{client_id}:{order_id}")
        perm_id = self._integer(getattr(execution, "permId", 0), "execution.permId", nonnegative=True)
        if perm_id > 0:
            aliases.append(f"ibkr:perm:{perm_id}")
        if not aliases:
            raise BrokerContractViolation("IBKR_EXECUTION_ORDER_IDENTITY_MISSING")
        return tuple(aliases)

    @staticmethod
    def _order_status_text(order_state: object) -> str:
        try:
            value = getattr(order_state, "status", "")
        except Exception:
            raise BrokerContractViolation("IBKR_ORDER_STATUS_INVALID") from None
        if not isinstance(value, str):
            raise BrokerContractViolation("IBKR_ORDER_STATUS_INVALID")
        return value.strip()

    @staticmethod
    def _order_completed_time_text(order_state: object) -> str:
        """Copy only bounded provider time; never retain the raw order-state object."""

        try:
            value = getattr(order_state, "completedTime", "")
        except Exception:
            raise BrokerContractViolation(
                "IBKR_ORDER_COMPLETED_TIME_INVALID"
            ) from None
        if not isinstance(value, str):
            return ""
        if len(value) > 256:
            raise BrokerContractViolation("IBKR_ORDER_COMPLETED_TIME_INVALID")
        return value.strip()

    @staticmethod
    def _order_state_blocking_warning_present(order_state: object) -> bool:
        """Reduce an untrusted broker warning to presence without retaining text."""

        missing = object()
        try:
            value = getattr(order_state, "warningText", missing)
        except Exception:
            return True
        if value is missing or type(value) is not str:
            return True
        if len(value) > _MAX_WARNING_TEXT_LENGTH:
            return True
        return bool(value.strip())

    @staticmethod
    def _order_watermark(orders: tuple[OrderSnapshot, ...], option_count: int) -> str:
        facts = sorted(
            (
                order.broker_order_id,
                order.state.value,
                str(order.cumulative_filled_quantity),
                order.broker_updated_at.isoformat(),
            )
            for order in orders
        )
        return hashlib.sha256(repr((facts, option_count)).encode("ascii")).hexdigest()

    def _allocate_request_id(self) -> int:
        result = self._next_request_id
        self._next_request_id += 1
        return result

    def _best_effort_cancel(self, collection: _Collection) -> None:
        try:
            self._requester.cancelAccountSummary(collection.summary_request_id)
        except Exception:
            pass
        try:
            self._requester.cancelPositions()
        except Exception:
            pass
        for request_id in tuple(collection.pnl_request_ids):
            if request_id in collection.cancelled_pnl_request_ids:
                continue
            try:
                self._requester.cancelPnL(request_id)
            except Exception:
                continue
            collection.cancelled_pnl_request_ids.add(request_id)

    def _cancel_session_reads(self, collection: _Collection) -> bool:
        """Bounded cleanup dispatch, not an assertion of server-side removal."""
        with self._condition:
            prior_error = collection.error
        ok = True
        for cancel in (
            lambda: self._requester.cancelAccountUpdatesMulti(collection.summary_request_id),
            self._requester.cancelPositions,
        ):
            try:
                cancel()
            except Exception:
                ok = False
        with self._condition:
            # The diagnostic error list is a capped ring. Its length/slice is
            # not an event cursor. This attempt still owns _active, so every
            # non-informational callback installs its immutable error here.
            if (collection.error is not prior_error
                    or self._active is not collection
                    or self._generation != collection.generation
                    or self._authenticated_generation != collection.generation):
                ok = False
            if not ok:
                self._session_cleanup_failed = True
        return ok

    def _assert_account(self, exact_account_id: str) -> None:
        if exact_account_id != self._exact_account_id:
            raise BrokerContractViolation("IBKR private account binding mismatch")

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise BrokerContractViolation("IBKR read clock must be timezone-aware")
        return value.astimezone(timezone.utc)

    def _active_for(self, generation: int) -> _Collection | None:
        if generation != self._generation:
            return None
        return self._active

    def _connect_ack(self, generation: int) -> None:
        # A socket acknowledgement is not account authentication.
        if generation != self._generation:
            return

    def _managed_accounts(self, generation: int, accounts: str) -> None:
        if generation != self._generation or not isinstance(accounts, str):
            return
        account_set = {item.strip() for item in accounts.split(",") if item.strip()}
        with self._condition:
            if self._exact_account_id in account_set:
                self._authenticated_generation = generation
            else:
                self._authenticated_generation = None
                self._last = None
                self._record_error(-1, 0, "managed_account_mismatch")
            self._condition.notify_all()

    def _account_summary(
        self,
        generation: int,
        request_id: int,
        account: str,
        tag: str,
        value: str,
        currency: str,
    ) -> None:
        with self._condition:
            active = self._active_for(generation)
            if active is None or active.account_values_channel != "account_summary" or request_id != active.summary_request_id:
                return
            if account != self._exact_account_id:
                return
            self._note_read_event(active, "account_summary", "first_callback")
            if not all(isinstance(item, str) for item in (tag, value, currency)):
                self._callback_contract_error(active, request_id, "account_summary_shape")
                return
            if currency not in ("", "USD", "BASE"):
                return
            previous = active.summary.get(tag)
            current = (value, currency)
            if previous is not None and previous != current:
                self._callback_contract_error(active, request_id, "account_summary_conflict")
                return
            active.summary[tag] = current
            active.summary_received_at.setdefault(tag, self._now())

    def _account_update_multi(
        self, generation: int, request_id: int, account: str, model_code: str,
        key: str, value: str, currency: str,
    ) -> None:
        with self._condition:
            active = self._active_for(generation)
            if active is None or active.account_values_channel != "account_updates_multi" or request_id != active.summary_request_id:
                return
            self._note_read_event(active, "account_updates_multi", "first_callback")
            if account != self._exact_account_id or model_code != "":
                self._callback_contract_error(active, request_id, "account_updates_scope")
                return
            if not all(type(item) is str for item in (key, value, currency)):
                self._callback_contract_error(active, request_id, "account_updates_shape")
                return
            if key.casefold() == "accountready":
                if value.casefold() != "true":
                    self._callback_contract_error(active, request_id, "account_updates_not_ready")
                return
            # Do not combine ledger/segment values with whole-account totals.
            if key not in _SUMMARY_TAGS.split(","):
                return
            if currency not in ("", "USD", "BASE"):
                self._callback_contract_error(active, request_id, "account_updates_currency")
                return
            previous = active.summary.get(key)
            current = (value, currency)
            if previous is not None and previous != current:
                self._callback_contract_error(active, request_id, "account_updates_conflict")
                return
            active.summary[key] = current
            active.summary_received_at.setdefault(key, self._now())

    def _position(
        self,
        generation: int,
        account: str,
        contract: object,
        quantity: object,
        average_cost: object,
    ) -> None:
        with self._condition:
            active = self._active_for(generation)
            if active is None or account != self._exact_account_id:
                return
            self._note_read_event(active, "positions", "first_callback")
            try:
                normalized_quantity = self._decimal(quantity, "position")
                normalized_cost = self._decimal(average_cost, "average_cost")
            except BrokerContractViolation:
                self._callback_contract_error(active, -1, "position_shape")
                return
            active.positions.append((contract, normalized_quantity, normalized_cost, self._now()))

    def _order(
        self,
        generation: int,
        source: str,
        callback_order_id: object,
        contract: object,
        order: object,
        order_state: object,
    ) -> None:
        with self._condition:
            active = self._active_for(generation)
            if active is None:
                return
            if getattr(order, "account", None) != self._exact_account_id:
                return
            self._note_read_event(active, "open_orders" if source == "open" else "completed_orders", "first_callback")
            try:
                callback_id = self._integer(callback_order_id, "callback_order_id", nonnegative=True)
                embedded_id = self._integer(getattr(order, "orderId", callback_id), "orderId", nonnegative=True)
                if callback_id > 0 and embedded_id not in (0, callback_id):
                    raise BrokerContractViolation("IBKR_CALLBACK_ORDER_ID_MISMATCH")
                if embedded_id == 0 and callback_id > 0:
                    try:
                        setattr(order, "orderId", callback_id)
                    except Exception:
                        raise BrokerContractViolation("IBKR_CALLBACK_ORDER_ID_MISSING") from None
                key = self._raw_order_key(order)
                status = self._order_status_text(order_state)
                completed_time = self._order_completed_time_text(order_state)
                blocking_warning_present = (
                    self._order_state_blocking_warning_present(order_state)
                )
            except BrokerContractViolation:
                self._callback_contract_error(active, -1, "order_shape")
                return
            received = self._now()
            candidate = _RawOrder(
                contract=contract,
                order=order,
                status=status,
                completed_time=completed_time,
                blocking_warning_present=blocking_warning_present,
                received_at=received,
                source=source,
                protection_evidence=capture_ibkr_protection_evidence(
                    contract=contract, order=order, broker_order_id=key,
                    expected_account_id=self._exact_account_id, account_masked=self._account_masked,
                    source=source, open_order_status=status,
                    blocking_warning_present=blocking_warning_present,
                    open_order_received_at=received, status=None,
                    collection_started_at=active.started_at, collection_completed_at=received,
                ),
            )
            previous = active.orders.get(key)
            if previous is not None and protection_evidence_facts(previous.protection_evidence) != protection_evidence_facts(candidate.protection_evidence):
                active.protection_order_conflicts.add(key)
            if previous is None or previous.received_at <= received:
                if previous is not None and previous.blocking_warning_present:
                    candidate = replace(candidate, blocking_warning_present=True)
                active.orders[key] = candidate
            elif blocking_warning_present and not previous.blocking_warning_present:
                # Receipt clocks can move backwards. A duplicate callback can
                # add a blocking fact, but never erase one already observed in
                # this complete collection.
                active.orders[key] = replace(
                    previous, blocking_warning_present=True
                )

    def _order_status(
        self, generation: int, client_id: object, order_id: object, status: object,
        *, filled: object, remaining: object, perm_id: object, parent_id: object,
        why_held: object,
    ) -> None:
        with self._condition:
            active = self._active_for(generation)
            if active is None:
                return
            try:
                key = (
                    self._integer(client_id, "clientId", nonnegative=True),
                    self._integer(order_id, "orderId", nonnegative=True),
                )
                if not isinstance(status, str):
                    raise BrokerContractViolation("IBKR_ORDER_STATUS_INVALID")
            except BrokerContractViolation:
                self._callback_contract_error(active, -1, "order_status_shape")
                return
            received_at = self._now()
            active.order_statuses[key] = (status.strip(), received_at)
            fact = capture_ibkr_order_status_fact(
                order_id=order_id, client_id=client_id, perm_id=perm_id, parent_id=parent_id,
                status=status, filled=filled, remaining=remaining, why_held=why_held,
                received_at=received_at,
            )
            previous = active.protection_statuses.get(key)
            if key in active.protection_statuses and (
                previous is None or fact is None
                or replace(previous, received_at=received_at) != fact
                or previous.received_at > received_at
            ):
                active.protection_status_conflicts.add(key)
            active.protection_statuses[key] = fact

    def _execution(
        self, generation: int, request_id: int, contract: object, execution: object
    ) -> None:
        with self._condition:
            active = self._active_for(generation)
            if active is None or request_id != active.execution_request_id:
                return
            if getattr(execution, "acctNumber", None) != self._exact_account_id:
                return
            self._note_read_event(active, "executions", "first_callback")
            exec_id = getattr(execution, "execId", None)
            if not isinstance(exec_id, str) or not exec_id.strip():
                self._callback_contract_error(active, request_id, "execution_shape")
                return
            exec_id = exec_id.strip()
            if exec_id in active.executions:
                self._callback_contract_error(active, request_id, "duplicate_execution")
                return
            active.executions[exec_id] = _RawExecution(contract, execution, self._now())

    def _commission(self, generation: int, report: object) -> None:
        with self._condition:
            active = self._active_for(generation)
            if active is None:
                return
            exec_id = getattr(report, "execId", None)
            currency = getattr(report, "currency", "")
            try:
                if not isinstance(exec_id, str) or not exec_id.strip() or not isinstance(currency, str):
                    raise BrokerContractViolation("IBKR_COMMISSION_SHAPE_INVALID")
                amount = getattr(report, "commissionAndFees", None)
                if amount is None:
                    amount = getattr(report, "commission", None)
                commission = self._decimal(amount, "commission")
            except BrokerContractViolation:
                self._callback_contract_error(active, -1, "commission_shape")
                return
            previous = active.commissions.get(exec_id.strip())
            if previous is not None and previous[:2] != (commission, currency.strip().upper()):
                # Retain this diagnostic fact for the additive session-input
                # path; do not change the existing strict reader's behavior.
                active.commission_conflict_observed = True
            active.commissions[exec_id.strip()] = (commission, currency.strip().upper(), self._now())
            self._condition.notify_all()

    def _daily_pnl(
        self, generation: int, request_id: object, realized_pnl: object
    ) -> None:
        with self._condition:
            active = self._active_for(generation)
            if active is None:
                return
            try:
                normalized_request_id = self._integer(
                    request_id, "pnl.request_id", nonnegative=True
                )
            except BrokerContractViolation:
                self._callback_contract_error(
                    active, -1, "daily_pnl_request_id_shape"
                )
                return
            if normalized_request_id != active.pnl_request_id:
                # The initial subscription is explicitly retired before a retry.
                # Its delayed callback must never satisfy the fresh request.
                return
            self._note_read_event(active, "daily_realized_pnl", "first_callback")
            try:
                normalized_realized = self._decimal(realized_pnl, "pnl.realizedPnL")
            except BrokerContractViolation:
                self._callback_contract_error(
                    active, normalized_request_id, "daily_pnl_nonfinite_or_shape"
                )
                return
            # IB's UNSET_DOUBLE sentinel is a finite ~1.8e308 value. It is
            # explicit absence, never valid financial evidence.  Preserve that
            # distinction and let the bounded subscription retry run once.
            if abs(normalized_realized) >= Decimal("1e300"):
                if active.daily_realized_pnl is not None:
                    self._callback_contract_error(
                        active,
                        normalized_request_id,
                        "daily_pnl_became_unavailable",
                    )
                    return
                active.unavailable_pnl_request_ids.add(normalized_request_id)
                self._condition.notify_all()
                return
            received = self._now()
            previous = active.daily_realized_pnl
            if previous is not None and previous[0] != normalized_realized:
                # A moving value means this was not an atomic account snapshot.
                # Fail closed and require a fresh complete collection.
                self._callback_contract_error(
                    active, normalized_request_id, "daily_pnl_moved"
                )
                return
            active.daily_realized_pnl = (normalized_realized, received)
            self._condition.notify_all()

    def _end(self, generation: int, marker: str, request_id: int | None) -> None:
        with self._condition:
            active = self._active_for(generation)
            if active is None:
                return
            if marker in {"account_summary", "account_updates_multi"}:
                if marker != active.account_values_channel or request_id != active.summary_request_id:
                    return
            if marker == "executions" and request_id != active.execution_request_id:
                return
            self._note_read_event(active, marker, "first_callback")
            self._note_read_event(active, marker, "end_callback")
            active.ends.add(marker)
            self._condition.notify_all()

    def _error(
        self, generation: int, request_id: object, code: object,
        reason: str = "UNSPECIFIED",
    ) -> None:
        try:
            normalized_request_id = int(request_id)
            normalized_code = int(code)
        except (TypeError, ValueError):
            return
        with self._condition:
            if generation != self._generation:
                return
            active = self._active
            active_request_ids = (
                {
                    active.summary_request_id,
                    active.execution_request_id,
                    active.pnl_request_id,
                }
                if active is not None
                else set()
            )
            if normalized_code in _INFO_CODES:
                self._record_error(normalized_request_id, normalized_code, "informational")
                return
            if normalized_code in _SESSION_ERROR_CODES:
                error = self._record_error(
                    normalized_request_id, normalized_code, "session"
                )
                # A transport/session error invalidates the account binding
                # even when no collection happens to be active.  A later read
                # must reauthenticate on a strictly newer generation.
                self._authenticated_generation = None
                self._last = None
                if active is not None:
                    active.error = error
                self._condition.notify_all()
                return
            if active is not None:
                if normalized_request_id in active_request_ids:
                    scope = "request"
                else:
                    # IBKR emits some request failures (including completed-
                    # order validation failures) with reqId=-1.  While a
                    # collection is active, any non-informational SDK error is
                    # therefore collection-fatal.  Ignoring it would turn an
                    # exact broker refusal into a misleading timeout.
                    scope = "sdk_callback"
                error = self._record_error(
                    normalized_request_id, normalized_code, scope, reason
                )
                active.error = error
                self._condition.notify_all()

    def _connection_closed(self, generation: int) -> None:
        with self._condition:
            if generation != self._generation:
                return
            self._authenticated_generation = None
            self._last = None
            error = self._record_error(-1, 1100, "connection_closed")
            if self._active is not None:
                self._active.error = error
            self._condition.notify_all()

    def _callback_contract_error(
        self, active: _Collection, request_id: int, scope: str
    ) -> None:
        error = self._record_error(request_id, 0, scope)
        active.error = error
        self._condition.notify_all()

    def _record_error(
        self, request_id: int, code: int, scope: str, reason: str = "UNSPECIFIED"
    ) -> SanitizedIbkrError:
        error = SanitizedIbkrError(
            request_id=request_id,
            code=code,
            scope=scope,
            received_at=self._now(),
            reason=(
                "API_READ_ONLY" if code == 321 and reason == "API_READ_ONLY"
                else "UNSPECIFIED"
            ),
        )
        self._errors.append(error)
        if len(self._errors) > 256:
            del self._errors[:-256]
        return error


__all__ = [
    "IbkrFiniteReadDiagnostic",
    "IbkrReadChannelDiagnostic",
    "IbkrReadCollectionDiagnostic",
    "IbkrReadRequester",
    "IbkrWholeAccountReadBridge",
    "SanitizedIbkrError",
    "classify_ibkr_error_callback",
]
