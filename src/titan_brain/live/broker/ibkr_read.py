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

from dataclasses import dataclass, field
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


_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.\-]{0,14}$")
_INFO_CODES = frozenset({1101, 1102, 2104, 2106, 2107, 2108, 2158})
_SESSION_ERROR_CODES = frozenset({502, 503, 504, 1100, 1300})
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


@dataclass(frozen=True)
class SanitizedIbkrError:
    """Nonsecret error evidence; broker text and advanced JSON are discarded."""

    request_id: int
    code: int
    scope: str
    received_at: datetime
    reason: str = "UNSPECIFIED"


@dataclass
class _RawOrder:
    contract: object
    order: object
    order_state: object
    received_at: datetime
    source: str


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
    ends: set[str] = field(default_factory=set)
    summary: dict[str, tuple[str, str]] = field(default_factory=dict)
    positions: list[tuple[object, Decimal, Decimal, datetime]] = field(default_factory=list)
    orders: dict[str, _RawOrder] = field(default_factory=dict)
    order_statuses: dict[tuple[int, int], tuple[str, datetime]] = field(default_factory=dict)
    executions: dict[str, _RawExecution] = field(default_factory=dict)
    commissions: dict[str, tuple[Decimal, str, datetime]] = field(default_factory=dict)
    daily_realized_pnl: tuple[Decimal, datetime] | None = None
    error: SanitizedIbkrError | None = None


@dataclass(frozen=True)
class _CompletedCollection:
    collection_id: str
    observation: CollectedObservation
    pages: dict[OrderFamily, OrderFamilyPage]
    all_equity_orders: tuple[OrderSnapshot, ...]
    valuation_contracts: tuple[PositionValuationContract, ...] | None = None


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
        del filled, remaining, avgFillPrice, permId, parentId, lastFillPrice, whyHeld, mktCapPrice
        self._bridge._order_status(
            self._generation, clientId, orderId, status
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
        self._next_request_id = 1_000_000
        self._collection_nonce = 0
        self._errors: list[SanitizedIbkrError] = []
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

    def position_valuation_inputs(self, collection_id: str):
        """Retain real contract IDs for an explicit read-only valuation probe.

        Missing currency/conId cannot break the existing conservative account
        diagnostic, but never becomes a symbol- or average-cost substitution.
        """
        with self._condition:
            completed = self._last
            if completed is None or completed.collection_id != collection_id:
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
            self._last = None
            self._condition.notify_all()
        return _GenerationCallbacks(self, generation)

    def get_account_base(self, exact_account_id: str) -> CollectedObservation:
        self._assert_account(exact_account_id)
        with self._condition:
            if self._authenticated_generation != self._generation:
                raise BrokerCapabilityError("IBKR_READ_NOT_AUTHENTICATED")
            if self._active is not None:
                raise BrokerCapabilityError("IBKR_READ_COLLECTION_ALREADY_ACTIVE")
            summary_id = self._allocate_request_id()
            execution_id = self._allocate_request_id()
            pnl_id = self._allocate_request_id()
            collection = _Collection(
                generation=self._generation,
                summary_request_id=summary_id,
                execution_request_id=execution_id,
                pnl_request_id=pnl_id,
                started_at=self._now(),
            )
            self._active = collection

        try:
            self._issue_requests(collection)
            self._wait_for_collection(collection)
            # Freeze exactly at the completed callback boundary. Late ambient
            # orderStatus/commission events belong to the next collection.
            with self._condition:
                if self._active is not collection:
                    raise BrokerCapabilityError("IBKR_READ_COLLECTION_GENERATION_LOST")
                self._active = None
                completed = self._normalize(collection)
                if collection.generation != self._generation:
                    raise BrokerCapabilityError("IBKR_READ_COLLECTION_GENERATION_LOST")
                self._last = completed
        except Exception:
            self._best_effort_cancel(collection)
            with self._condition:
                if self._active is collection:
                    self._active = None
            raise
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

    def _issue_requests(self, collection: _Collection) -> None:
        try:
            self._requester.reqAccountSummary(
                collection.summary_request_id, "All", _SUMMARY_TAGS
            )
            self._requester.reqPositions()
            self._requester.reqAllOpenOrders()
            self._requester.reqCompletedOrders(False)
            self._requester.reqExecutions(
                collection.execution_request_id, self._execution_filter_factory()
            )
            # ``AccountSummary.RealizedPnL`` has an account/window-dependent
            # period and is deliberately not accepted as current-day authority.
            # The account-level PnL subscription exposes IBKR's dedicated daily
            # realized value and is bound to this collection's request ID.
            self._requester.reqPnL(
                collection.pnl_request_id, self._exact_account_id, ""
            )
        except Exception:
            raise BrokerCapabilityError("IBKR_READ_REQUEST_DISPATCH_FAILED") from None

    def _wait_for_collection(self, collection: _Collection) -> None:
        required = {
            "account_summary",
            "positions",
            "open_orders",
            "completed_orders",
            "executions",
        }
        deadline = time.monotonic() + self._timeout
        with self._condition:
            while True:
                if self._active is not collection or collection.generation != self._generation:
                    raise BrokerCapabilityError("IBKR_READ_COLLECTION_GENERATION_LOST")
                if collection.error is not None:
                    reason = (
                        ":API_READ_ONLY"
                        if collection.error.reason == "API_READ_ONLY"
                        else ""
                    )
                    raise BrokerCapabilityError(
                        f"IBKR_READ_CALLBACK_ERROR:{collection.error.code}:{collection.error.scope}{reason}"
                    )
                missing_commissions = set(collection.executions) - set(collection.commissions)
                if (
                    required.issubset(collection.ends)
                    and not missing_commissions
                    and collection.daily_realized_pnl is not None
                ):
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    missing = sorted(required - collection.ends)
                    if missing:
                        scope = ",".join(missing)
                    elif missing_commissions:
                        scope = "commission"
                    else:
                        scope = "daily_realized_pnl"
                    raise BrokerCapabilityError(f"IBKR_READ_TIMEOUT:{scope}")
                self._condition.wait(remaining)

    def _normalize(self, collection: _Collection) -> _CompletedCollection:
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
        if collection.daily_realized_pnl is None:
            raise BrokerContractViolation("IBKR_DAILY_REALIZED_PNL_MISSING")
        realized, realized_received_at = collection.daily_realized_pnl
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
            daily_realized_pnl=realized,
            daily_realized_pnl_complete=True,
            weekly_realized_pnl_complete=False,
            peak_equity_complete=False,
            risk_evidence_authoritative=True,
            risk_evidence_source="ibkr:reqPnL.realizedPnL:current-day",
            risk_evidence_as_of=realized_received_at,
        )
        self._collection_nonce += 1
        collection_id = hashlib.sha256(
            (
                f"ibkr-read-v1:{collection.generation}:{self._collection_nonce}:"
                f"{collection.summary_request_id}:{collection.execution_request_id}:"
                f"{collection.pnl_request_id}:"
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
    ) -> OrderSnapshot:
        order = raw.order
        requested = self._decimal(getattr(order, "totalQuantity", None), "totalQuantity")
        if requested <= 0:
            raise BrokerContractViolation("IBKR_ORDER_QUANTITY_INVALID")
        client_id = self._integer(getattr(order, "clientId", 0), "clientId", nonnegative=True)
        order_id = self._integer(getattr(order, "orderId", 0), "orderId", nonnegative=True)
        status_record = statuses.get((client_id, order_id))
        status = status_record[0] if status_record else self._order_status_text(raw.order_state)
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
        )

    def _provider_order_time(
        self,
        raw: _RawOrder,
        fills: tuple[FillSnapshot, ...],
        fallback: datetime,
    ) -> datetime:
        completed_time = getattr(raw.order_state, "completedTime", "")
        if isinstance(completed_time, str) and completed_time.strip():
            return self._ibkr_time(completed_time, fallback)
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
        status = statuses.get((client_id, order_id), (self._order_status_text(raw.order_state), raw.received_at))[0]
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
        value = getattr(order_state, "status", "")
        if not isinstance(value, str):
            raise BrokerContractViolation("IBKR_ORDER_STATUS_INVALID")
        return value.strip()

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
        try:
            self._requester.cancelPnL(collection.pnl_request_id)
        except Exception:
            pass

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
            if active is None or request_id != active.summary_request_id:
                return
            if account != self._exact_account_id:
                return
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
            except BrokerContractViolation:
                self._callback_contract_error(active, -1, "order_shape")
                return
            received = self._now()
            candidate = _RawOrder(contract, order, order_state, received, source)
            previous = active.orders.get(key)
            if previous is None or previous.received_at <= received:
                active.orders[key] = candidate

    def _order_status(
        self, generation: int, client_id: object, order_id: object, status: object
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
            active.order_statuses[key] = (status.strip(), self._now())

    def _execution(
        self, generation: int, request_id: int, contract: object, execution: object
    ) -> None:
        with self._condition:
            active = self._active_for(generation)
            if active is None or request_id != active.execution_request_id:
                return
            if getattr(execution, "acctNumber", None) != self._exact_account_id:
                return
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
                if normalized_request_id != active.pnl_request_id:
                    return
                normalized_realized = self._decimal(realized_pnl, "pnl.realizedPnL")
                # IB's UNSET_DOUBLE sentinel is a finite ~1.8e308 value. It is
                # absence, never valid financial evidence.
                if abs(normalized_realized) >= Decimal("1e300"):
                    raise BrokerContractViolation("IBKR_DAILY_PNL_UNAVAILABLE")
            except BrokerContractViolation:
                self._callback_contract_error(active, -1, "daily_pnl_shape")
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
            if marker == "account_summary" and request_id != active.summary_request_id:
                return
            if marker == "executions" and request_id != active.execution_request_id:
                return
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
    "IbkrReadRequester",
    "IbkrWholeAccountReadBridge",
    "SanitizedIbkrError",
    "classify_ibkr_error_callback",
]
