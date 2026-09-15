"""Concrete, lazy official-IBKR runtime for the attended production adapter.

The runtime has two deliberately separate local API connections.  The read
connection discovers and retains the exact live account only in memory, then
drives the whole-account and contract-details bridges.  The command connection
is not created until the caller supplies the existing mutation interlock and
dispatch authority used by :class:`IbkrSdkSession`.

Importing this module and constructing :class:`IbkrOfficialRuntime` neither
imports ``ibapi`` nor opens a socket.  The separately installed SDK snapshot is
re-attested immediately before its first import.  Public status, errors, and
order events are intentionally redacted; no callback text, advanced-reject
JSON, or full account identifier leaves this object.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import importlib
from pathlib import Path
import re
from threading import Condition, RLock, Thread, current_thread
import time
from typing import Callable, Literal

from ..provider_profile import IbkrLocalProviderProfile, InstalledSdkAttestation
from .base import (
    AttendedCancelReview,
    AttendedLocalReview,
    BrokerCapabilities,
    BrokerMutationBlocked,
    BrokerOperationResult,
    OrderRequest,
    OrderCoverageContract,
)
from .ibkr_account import IbkrStableAccountSnapshotReader
from .ibkr_instrument import IbkrInstrumentProvider
from .ibkr_read import (
    IBKR_API_READ_ONLY_MESSAGE,
    IbkrWholeAccountReadBridge,
    classify_ibkr_error_callback,
)
from .ibkr_sdk import DispatchAuthorizer, IbkrSdkSession, SUPPORTED_SDK_VERSION
from .ibkr_transport import (
    IbkrProductionTransport,
    autonomous_ibkr_descriptor,
    attended_ibkr_descriptor,
)
from ..ibkr_autonomous_authority import (
    IbkrAutonomousAuthorityBindings,
    VerifiedIbkrAutonomousAuthority,
)


RuntimeState = Literal["NEW", "READ_READY", "READY", "STOPPED", "FAILED"]
_LIVE_ACCOUNT = re.compile(r"^U[0-9]+$")
_ERROR_CODE = re.compile(r"^IBKR_RUNTIME_[A-Z0-9_]{1,96}$")
_ORDER_STATES = frozenset(
    {
        "ApiCancelled",
        "ApiPending",
        "Cancelled",
        "Filled",
        "Inactive",
        "PendingCancel",
        "PendingSubmit",
        "PreSubmitted",
        "Submitted",
    }
)


class IbkrRuntimeError(RuntimeError):
    """Stable local runtime failure which never includes provider text."""

    def __init__(self, code: str) -> None:
        normalized = str(code).strip().upper()
        if not _ERROR_CODE.fullmatch(normalized):
            raise ValueError("invalid IBKR runtime error code")
        self.code = normalized
        super().__init__(normalized)


@dataclass(frozen=True)
class SanitizedIbkrRuntimeError:
    connection: Literal["read", "command"]
    request_id: int
    code: int
    scope: str
    received_at: datetime
    reason: str = "UNSPECIFIED"


@dataclass(frozen=True)
class SanitizedIbkrOrderEvent:
    """Non-authoritative command-channel evidence without order payload data."""

    kind: Literal["open_order", "order_status", "completed_order"]
    order_id: int
    client_id: int | None
    status: str
    received_at: datetime


@dataclass(frozen=True)
class IbkrRuntimeStatus:
    state: RuntimeState
    profile_id: str
    account_masked: str
    endpoint: str
    read_client_id: int
    command_client_id: int
    read_generation: int
    command_generation: int
    read_connected: bool
    command_connected: bool
    account_authenticated: bool
    sdk_attested: bool
    error_codes: tuple[tuple[str, int, int, str], ...]
    last_observed_at: datetime | None
    runtime_error_code: str | None
    full_account_identifier_persisted: bool = False
    write_authority_granted: bool = False

    @property
    def phase(self) -> Literal["STAGED", "CONNECTED", "BLOCKED"]:
        if self.state == "FAILED":
            return "BLOCKED"
        if self.read_connected and self.account_authenticated:
            return "CONNECTED"
        return "STAGED"

    @property
    def connected(self) -> bool:
        return self.read_connected

    @property
    def authenticated(self) -> bool:
        return self.account_authenticated

    @property
    def observed_at(self) -> datetime | None:
        return self.last_observed_at

    @property
    def error_code(self) -> str | None:
        if self.runtime_error_code is not None:
            return self.runtime_error_code
        if not self.error_codes:
            return None
        connection, _request, code, scope = self.error_codes[-1]
        return f"IBKR_RUNTIME_{connection.upper()}_{scope.upper()}_{code}"

    def public_dict(self) -> dict[str, object]:
        return {
            "phase": self.phase,
            "connected": self.connected,
            "authenticated": self.authenticated,
            "observed_at": self.observed_at,
            "error_code": self.error_code,
            "profile_id": self.profile_id,
            "account_masked": self.account_masked,
            "endpoint": self.endpoint,
            "read_client_id": self.read_client_id,
            "command_client_id": self.command_client_id,
            "read_generation": self.read_generation,
            "command_generation": self.command_generation,
            "command_connected": self.command_connected,
            "sdk_attested": self.sdk_attested,
            "full_account_identifier_persisted": False,
            "write_authority_granted": False,
        }


@dataclass(frozen=True)
class IbkrRuntimeComponents:
    """Read-side objects safe to hand to the production composition.

    The exact account is intentionally absent.  Objects in this bundle retain
    it privately only because the existing normalized broker protocols require
    exact callback filtering at their internal boundary.
    """

    read_bridge: IbkrWholeAccountReadBridge
    instrument_provider: IbkrInstrumentProvider
    account_snapshot_reader: IbkrStableAccountSnapshotReader
    contract_factory: Callable[[], object]
    order_factory: Callable[[], object]
    account_masked: str
    account_binding_fingerprint: str
    read_generation: int


@dataclass(frozen=True)
class IbkrReadProbe:
    phase: Literal["CONNECTED", "BLOCKED"]
    connected: bool
    authenticated: bool
    observed_at: datetime | None
    error_code: str | None
    account_masked: str
    account_collection_id: str | None
    instrument_evidence_id: str | None
    symbol: str
    contract_read_receipt_id: str | None = None
    contract_regular_session_open: bool | None = None

    def public_dict(self) -> dict[str, object]:
        return {
            "phase": self.phase,
            "connected": self.connected,
            "authenticated": self.authenticated,
            "observed_at": self.observed_at,
            "error_code": self.error_code,
            "account_masked": self.account_masked,
            "account_collection_id": self.account_collection_id,
            "instrument_evidence_id": self.instrument_evidence_id,
            "symbol": self.symbol,
            "contract_read_receipt_id": self.contract_read_receipt_id,
            "contract_regular_session_open": self.contract_regular_session_open,
            "whole_broker_history_verified": False,
            "order_history_scope": "current_day_completed_orders_and_api_visible_executions",
            "full_account_identifier_persisted": False,
            "write_authority_granted": False,
        }


@dataclass(frozen=True)
class _AttestedSdkBundle:
    attestation: InstalledSdkAttestation
    client_type: type
    wrapper_type: type
    contract_type: type
    order_type: type
    execution_filter_type: type
    order_cancel_type: type


def _module_inside(module: object, root: Path) -> bool:
    filename = getattr(module, "__file__", None)
    if not isinstance(filename, str):
        return False
    try:
        Path(filename).resolve(strict=True).relative_to(root)
        return True
    except (OSError, ValueError):
        return False


def _load_attested_sdk(
    install_root: Path, profile: IbkrLocalProviderProfile
) -> _AttestedSdkBundle:
    """Re-attest, import, and source-pin the exact official SDK snapshot."""

    # Keep the import here: module import and runtime construction are inert.
    from ..provider_profile import activate_installed_sdk

    try:
        attestation = activate_installed_sdk(install_root, profile)
        ibapi = importlib.import_module("ibapi")
        client_module = importlib.import_module("ibapi.client")
        wrapper_module = importlib.import_module("ibapi.wrapper")
        contract_module = importlib.import_module("ibapi.contract")
        order_module = importlib.import_module("ibapi.order")
        execution_module = importlib.import_module("ibapi.execution")
        cancel_module = importlib.import_module("ibapi.order_cancel")
        modules = (
            ibapi,
            client_module,
            wrapper_module,
            contract_module,
            order_module,
            execution_module,
            cancel_module,
        )
        if (
            getattr(ibapi, "__version__", None) != profile.sdk_version
            or profile.sdk_version != SUPPORTED_SDK_VERSION
            or not all(_module_inside(module, attestation.import_root) for module in modules)
        ):
            raise IbkrRuntimeError("IBKR_RUNTIME_SDK_SOURCE_MISMATCH")
        return _AttestedSdkBundle(
            attestation=attestation,
            client_type=client_module.EClient,
            wrapper_type=wrapper_module.EWrapper,
            contract_type=contract_module.Contract,
            order_type=order_module.Order,
            execution_filter_type=execution_module.ExecutionFilter,
            order_cancel_type=cancel_module.OrderCancel,
        )
    except IbkrRuntimeError:
        raise
    except Exception:
        raise IbkrRuntimeError("IBKR_RUNTIME_SDK_NOT_ATTESTED") from None


class IbkrSdkObjectFactory:
    """Release-contained constructor over one type from the attested snapshot."""

    def __init__(self, sdk_type: type, kind: str) -> None:
        if not isinstance(sdk_type, type) or kind not in {
            "contract",
            "order",
            "execution_filter",
            "order_cancel",
        }:
            raise ValueError("invalid attested SDK object factory")
        self._sdk_type = sdk_type
        self._kind = kind

    def __call__(self) -> object:
        try:
            return self._sdk_type()
        except Exception:
            raise IbkrRuntimeError("IBKR_RUNTIME_SDK_OBJECT_CONSTRUCTION_FAILED") from None


class IbkrRuntimeClock:
    """Unique release-contained clock role over the runtime's trusted source."""

    def __init__(self, source: Callable[[], datetime]) -> None:
        if not callable(source):
            raise TypeError("IBKR runtime clock source must be callable")
        self._source = source

    def __call__(self) -> datetime:
        value = self._source()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise IbkrRuntimeError("IBKR_RUNTIME_CLOCK_INVALID")
        return value.astimezone(timezone.utc)


_READ_CALLBACK_MEMBERS = (
    "connectAck",
    "managedAccounts",
    "managedAccountsProtoBuf",
    "accountSummary",
    "accountSummaryProtoBuf",
    "accountSummaryEnd",
    "accountSummaryEndProtoBuf",
    "position",
    "positionProtoBuf",
    "positionEnd",
    "positionEndProtoBuf",
    "openOrder",
    "openOrderProtoBuf",
    "openOrderEnd",
    "openOrdersEndProtoBuf",
    "completedOrder",
    "completedOrderProtoBuf",
    "completedOrdersEnd",
    "completedOrdersEndProtoBuf",
    "orderStatus",
    "orderStatusProtoBuf",
    "execDetails",
    "executionDetailsProtoBuf",
    "execDetailsEnd",
    "executionDetailsEndProtoBuf",
    "commissionAndFeesReport",
    "commissionAndFeesReportProtoBuf",
    "commissionReport",
    "pnl",
    "pnlProtoBuf",
    "contractDetails",
    "contractDataProtoBuf",
    "contractDetailsEnd",
    "contractDataEndProtoBuf",
    "error",
    "errorProtoBuf",
    "connectionClosed",
)

_COMMAND_CALLBACK_MEMBERS = (
    "connectAck",
    "nextValidId",
    "nextValidIdProtoBuf",
    "managedAccounts",
    "managedAccountsProtoBuf",
    "openOrder",
    "openOrderProtoBuf",
    "openOrderEnd",
    "openOrdersEndProtoBuf",
    "completedOrder",
    "completedOrderProtoBuf",
    "completedOrdersEnd",
    "completedOrdersEndProtoBuf",
    "orderStatus",
    "orderStatusProtoBuf",
    "execDetails",
    "executionDetailsProtoBuf",
    "execDetailsEnd",
    "executionDetailsEndProtoBuf",
    "commissionAndFeesReport",
    "commissionAndFeesReportProtoBuf",
    "error",
    "errorProtoBuf",
    "connectionClosed",
)


def _install_callbacks(wrapper: object, router: object, members: tuple[str, ...]) -> None:
    for name in members:
        callback = getattr(router, name)
        setattr(wrapper, name, callback)


class _ReadCallbackRouter:
    """Generation-bound fanout to read and contract callback bridges."""

    def __init__(self, runtime: "IbkrOfficialRuntime", generation: int) -> None:
        self._runtime = runtime
        self._generation = generation
        self._targets: tuple[object, ...] = ()

    def bind_targets(self, targets: tuple[object, ...], managed_accounts: str) -> None:
        if len(targets) != 2 or any(target is None for target in targets):
            raise IbkrRuntimeError("IBKR_RUNTIME_READ_CALLBACK_BINDING_INVALID")
        self._targets = targets
        self._forward("connectAck")
        self._forward("managedAccounts", managed_accounts)

    def _discard_proto(self, payload: object) -> None:
        # IB API 10.50 emits a raw protobuf callback immediately before the
        # normalized callback for each of these messages.  The default
        # EWrapper implementations log the raw object, so the supported
        # runtime replaces every protobuf callback it can receive.
        del payload

    managedAccountsProtoBuf = _discard_proto
    accountSummaryProtoBuf = _discard_proto
    accountSummaryEndProtoBuf = _discard_proto
    positionProtoBuf = _discard_proto
    positionEndProtoBuf = _discard_proto
    openOrderProtoBuf = _discard_proto
    openOrdersEndProtoBuf = _discard_proto
    completedOrderProtoBuf = _discard_proto
    completedOrdersEndProtoBuf = _discard_proto
    orderStatusProtoBuf = _discard_proto
    executionDetailsProtoBuf = _discard_proto
    executionDetailsEndProtoBuf = _discard_proto
    pnlProtoBuf = _discard_proto
    contractDataProtoBuf = _discard_proto
    contractDataEndProtoBuf = _discard_proto

    def detach(self) -> None:
        self._targets = ()

    def _forward(self, name: str, *args: object) -> None:
        if not self._runtime._accept_read_generation(self._generation):
            return
        for target in self._targets:
            callback = getattr(target, name, None)
            if callable(callback):
                callback(*args)

    def connectAck(self) -> None:
        self._forward("connectAck")

    def managedAccounts(self, accountsList: str) -> None:
        self._runtime._observe_read_accounts(self._generation, accountsList)
        self._forward("managedAccounts", accountsList)

    def accountSummary(self, reqId: int, account: str, tag: str, value: str, currency: str) -> None:
        self._forward("accountSummary", reqId, account, tag, value, currency)

    def accountSummaryEnd(self, reqId: int) -> None:
        self._forward("accountSummaryEnd", reqId)

    def position(self, account: str, contract: object, position: object, avgCost: float) -> None:
        self._forward("position", account, contract, position, avgCost)

    def positionEnd(self) -> None:
        self._forward("positionEnd")

    def openOrder(self, orderId: int, contract: object, order: object, orderState: object) -> None:
        self._forward("openOrder", orderId, contract, order, orderState)

    def openOrderEnd(self) -> None:
        self._forward("openOrderEnd")

    def completedOrder(self, contract: object, order: object, orderState: object) -> None:
        self._forward("completedOrder", contract, order, orderState)

    def completedOrdersEnd(self) -> None:
        self._forward("completedOrdersEnd")

    def orderStatus(
        self, orderId: int, status: str, filled: object, remaining: object,
        avgFillPrice: float, permId: int, parentId: int, lastFillPrice: float,
        clientId: int, whyHeld: str, mktCapPrice: float = 0.0,
    ) -> None:
        self._forward(
            "orderStatus", orderId, status, filled, remaining, avgFillPrice,
            permId, parentId, lastFillPrice, clientId, whyHeld, mktCapPrice,
        )

    def execDetails(self, reqId: int, contract: object, execution: object) -> None:
        self._forward("execDetails", reqId, contract, execution)

    def execDetailsEnd(self, reqId: int) -> None:
        self._forward("execDetailsEnd", reqId)

    def commissionAndFeesReport(self, report: object) -> None:
        self._forward("commissionAndFeesReport", report)

    def commissionAndFeesReportProtoBuf(self, report: object) -> None:
        # The 10.50 decoder immediately emits the normalized callback too.
        # Suppress the default EWrapper logger so raw protobuf fields never
        # reach logs; the normalized callback above is the sole data path.
        del report

    def commissionReport(self, report: object) -> None:
        """Legacy test/provider alias; 10.50.2 uses commissionAndFeesReport."""

        self._forward("commissionReport", report)

    def pnl(self, reqId: int, dailyPnL: float, unrealizedPnL: float, realizedPnL: float) -> None:
        self._forward("pnl", reqId, dailyPnL, unrealizedPnL, realizedPnL)

    def contractDetails(self, reqId: int, contractDetails: object) -> None:
        self._forward("contractDetails", reqId, contractDetails)

    def contractDetailsEnd(self, reqId: int) -> None:
        self._forward("contractDetailsEnd", reqId)

    def error(self, reqId: object, *arguments: object) -> None:
        # Official 10.50.2 sends (reqId, errorTime, errorCode, errorString,
        # advancedJson); the older synthetic/legacy shape omitted errorTime.
        # Forward only a constant public phrase for a recognized cause. Raw
        # broker text and advanced JSON never reach downstream consumers.
        error_code, reason = classify_ibkr_error_callback(arguments)
        request_id, code = self._runtime._sanitize_error_values(reqId, error_code)
        self._runtime._record_error("read", request_id, code, "sdk_callback", reason)
        safe_message = IBKR_API_READ_ONLY_MESSAGE if reason == "API_READ_ONLY" else ""
        self._forward("error", request_id, code, safe_message, "")

    def errorProtoBuf(self, error: object) -> None:
        # The official decoder follows this with ``error(...)``.  Never let
        # the default wrapper log raw broker text or advanced reject JSON.
        del error

    def connectionClosed(self) -> None:
        self._runtime._observe_connection_closed("read", self._generation)
        self._forward("connectionClosed")


class _OfficialReadRequester:
    """Whitelisted read surface over one attested official ``EClient``."""

    def __init__(self, bundle: _AttestedSdkBundle, router: _ReadCallbackRouter) -> None:
        try:
            wrapper = bundle.wrapper_type()
            _install_callbacks(wrapper, router, _READ_CALLBACK_MEMBERS)
            client = bundle.client_type(wrapper)
            # Client 0 makes these nominal order-read methods state-changing:
            # they bind manual orders.  Never expose or dispatch them, even
            # accidentally from SDK startup.  reqAllOpenOrders is the separate
            # non-binding snapshot used by this read lane.
            for member in (
                "reqOpenOrders",
                "reqOpenOrdersProtoBuf",
                "reqAutoOpenOrders",
                "reqAutoOpenOrdersProtoBuf",
            ):
                setattr(client, member, self._reject_order_binding)
        except Exception:
            raise IbkrRuntimeError("IBKR_RUNTIME_READ_CLIENT_CONSTRUCTION_FAILED") from None
        self._router = router
        self._wrapper = wrapper
        self._client = client

    @staticmethod
    def _reject_order_binding(*args: object, **kwargs: object) -> None:
        raise IbkrRuntimeError("IBKR_RUNTIME_READ_ORDER_BINDING_FORBIDDEN")

    def release_components(self) -> tuple[tuple[str, object, tuple[str, ...]], ...]:
        return (("ibkr_read_callback_router", self._router, _READ_CALLBACK_MEMBERS),)

    def connect(self, host: str, port: int, client_id: int) -> None:
        try:
            self._client.connect(host, port, client_id)
            if (
                self._client.isConnected() is not True
                or getattr(self._client, "host", None) != host
                or getattr(self._client, "port", None) != port
                or getattr(self._client, "clientId", None) != client_id
            ):
                raise IbkrRuntimeError("IBKR_RUNTIME_READ_CONNECTION_NOT_ESTABLISHED")
        except IbkrRuntimeError:
            raise
        except Exception:
            raise IbkrRuntimeError("IBKR_RUNTIME_READ_CONNECTION_NOT_ESTABLISHED") from None

    def run(self) -> None:
        self._client.run()

    def disconnect(self) -> None:
        self._client.disconnect()

    def is_connected(self) -> bool:
        try:
            return self._client.isConnected() is True
        except Exception:
            return False

    def reqAccountSummary(self, reqId: int, groupName: str, tags: str) -> None:
        self._client.reqAccountSummary(reqId, groupName, tags)

    def cancelAccountSummary(self, reqId: int) -> None:
        self._client.cancelAccountSummary(reqId)

    def reqPositions(self) -> None:
        self._client.reqPositions()

    def cancelPositions(self) -> None:
        self._client.cancelPositions()

    def reqAllOpenOrders(self) -> None:
        self._client.reqAllOpenOrders()

    def reqCompletedOrders(self, apiOnly: bool) -> None:
        self._client.reqCompletedOrders(apiOnly)

    def reqExecutions(self, reqId: int, execFilter: object) -> None:
        self._client.reqExecutions(reqId, execFilter)

    def reqPnL(self, reqId: int, account: str, modelCode: str) -> None:
        self._client.reqPnL(reqId, account, modelCode)

    def cancelPnL(self, reqId: int) -> None:
        self._client.cancelPnL(reqId)

    def reqContractDetails(self, reqId: int, contract: object) -> None:
        self._client.reqContractDetails(reqId, contract)

    def cancelContractDetails(self, reqId: int) -> None:
        self._client.cancelContractDetails(reqId)


class _ContractDetailsRequester:
    """Distinct release role sharing the already inventoried read connection."""

    def __init__(self, reads: _OfficialReadRequester) -> None:
        self._reads = reads

    def release_components(self) -> tuple[tuple[str, object, tuple[str, ...]], ...]:
        # The shared requester is inventoried exactly once by the read bridge.
        # Returning it here would be correctly rejected as a duplicate object.
        return ()

    def reqContractDetails(self, reqId: int, contract: object) -> None:
        self._reads.reqContractDetails(reqId, contract)

    def cancelContractDetails(self, reqId: int) -> None:
        self._reads.cancelContractDetails(reqId)


class _CommandCallbackRouter:
    """Generation-bound, redacted command callbacks into ``IbkrSdkSession``."""

    def __init__(self, runtime: "IbkrOfficialRuntime") -> None:
        self._runtime = runtime
        self._session: IbkrSdkSession | None = None
        self._generation = 0

    def bind(self, session: IbkrSdkSession, generation: int) -> None:
        if generation <= 0 or session.generation != generation:
            raise IbkrRuntimeError("IBKR_RUNTIME_COMMAND_CALLBACK_BINDING_INVALID")
        self._session = session
        self._generation = generation

    def _discard_proto(self, payload: object) -> None:
        # Normalized callbacks remain authoritative; raw protobuf objects must
        # never fall through to the SDK's default INFO logger.
        del payload

    nextValidIdProtoBuf = _discard_proto
    managedAccountsProtoBuf = _discard_proto
    openOrderProtoBuf = _discard_proto
    openOrdersEndProtoBuf = _discard_proto
    completedOrderProtoBuf = _discard_proto
    completedOrdersEndProtoBuf = _discard_proto
    orderStatusProtoBuf = _discard_proto
    executionDetailsProtoBuf = _discard_proto
    executionDetailsEndProtoBuf = _discard_proto

    def detach(self) -> None:
        self._session = None
        self._generation = 0

    def _current(self) -> IbkrSdkSession | None:
        if not self._runtime._accept_command_generation(self._generation):
            return None
        return self._session

    def connectAck(self) -> None:
        return None

    def nextValidId(self, orderId: int) -> None:
        session = self._current()
        if session is None:
            return
        try:
            session.observe_next_valid_id(orderId, generation=self._generation)
            self._runtime._observe_command_ready(self._generation, next_id=True)
        except Exception:
            self._runtime._command_callback_failed(self._generation)

    def managedAccounts(self, accountsList: str) -> None:
        session = self._current()
        if session is None:
            return
        try:
            session.observe_managed_accounts(accountsList, generation=self._generation)
            self._runtime._observe_command_ready(self._generation, account=True)
        except Exception:
            self._runtime._command_callback_failed(self._generation)

    def openOrder(self, orderId: int, contract: object, order: object, orderState: object) -> None:
        del contract
        self._runtime._record_order_event(
            self._generation,
            "open_order",
            orderId,
            getattr(order, "clientId", None),
            getattr(orderState, "status", ""),
        )

    def openOrderEnd(self) -> None:
        return None

    def completedOrder(self, contract: object, order: object, orderState: object) -> None:
        del contract
        self._runtime._record_order_event(
            self._generation,
            "completed_order",
            getattr(order, "orderId", 0),
            getattr(order, "clientId", None),
            getattr(orderState, "status", ""),
        )

    def completedOrdersEnd(self) -> None:
        return None

    def orderStatus(
        self, orderId: int, status: str, filled: object, remaining: object,
        avgFillPrice: float, permId: int, parentId: int, lastFillPrice: float,
        clientId: int, whyHeld: str, mktCapPrice: float = 0.0,
    ) -> None:
        del filled, remaining, avgFillPrice, permId, parentId, lastFillPrice, whyHeld, mktCapPrice
        self._runtime._record_order_event(
            self._generation, "order_status", orderId, clientId, status
        )

    def error(self, reqId: object, *arguments: object) -> None:
        error_code, reason = classify_ibkr_error_callback(arguments)
        request_id, code = self._runtime._sanitize_error_values(reqId, error_code)
        self._runtime._record_error("command", request_id, code, "sdk_callback", reason)

    def execDetails(self, reqId: object, contract: object, execution: object) -> None:
        # A command connection can receive fills without issuing reqExecutions.
        # Whole-account reconciliation on the isolated read connection remains
        # authoritative, and the full contract/execution payload must not fall
        # through to EWrapper's INFO logger.
        del reqId, contract, execution

    def execDetailsEnd(self, reqId: object) -> None:
        del reqId

    def errorProtoBuf(self, error: object) -> None:
        # The normalized error callback that follows carries the safe numeric
        # fields.  Raw protobuf error payloads are deliberately discarded.
        del error

    def commissionAndFeesReport(self, report: object) -> None:
        # Command sessions do not consume fee reports.  Override the official
        # wrapper logger to avoid persisting execution metadata.
        del report

    def commissionAndFeesReportProtoBuf(self, report: object) -> None:
        del report

    def connectionClosed(self) -> None:
        session = self._current()
        if session is not None:
            session.observe_connection_closed(generation=self._generation)
        self._runtime._observe_connection_closed("command", self._generation)


class _OfficialCommandClient:
    """Guard-compatible facade over the attested official command ``EClient``."""

    def __init__(self, bundle: _AttestedSdkBundle, router: _CommandCallbackRouter) -> None:
        try:
            wrapper = bundle.wrapper_type()
            _install_callbacks(wrapper, router, _COMMAND_CALLBACK_MEMBERS)
            client = bundle.client_type(wrapper)
            inner_legacy = client.sendMsg
            inner_protobuf = client.sendMsgProtoBuf
            # The official encoder remains authoritative.  Its two send hooks
            # always resolve the current guarded facade methods dynamically.
            client.sendMsg = lambda msg_id, msg: self._forward_legacy(msg_id, msg)
            client.sendMsgProtoBuf = lambda msg_id, msg: self._forward_protobuf(msg_id, msg)
        except Exception:
            raise IbkrRuntimeError("IBKR_RUNTIME_COMMAND_CLIENT_CONSTRUCTION_FAILED") from None
        self._router = router
        self._wrapper = wrapper
        self._client = client
        self._inner_legacy = inner_legacy
        self._inner_protobuf = inner_protobuf
        self._legacy_guard: Callable[[int, object], object] | None = None
        self._protobuf_guard: Callable[[int, object], object] | None = None
        self.conn = getattr(client, "conn", None)
        self.host = getattr(client, "host", None)
        self.port = getattr(client, "port", None)
        self.clientId = getattr(client, "clientId", None)
        self.connectOptions = getattr(client, "connectOptions", None)
        self.optCapab = getattr(client, "optCapab", None)
        self.extraAuth = getattr(client, "extraAuth", None)
        self.asynchronous = getattr(client, "asynchronous", None)

    def release_components(self) -> tuple[tuple[str, object, tuple[str, ...]], ...]:
        return (("ibkr_command_callback_router", self._router, _COMMAND_CALLBACK_MEMBERS),)

    def install_titan_encoders(
        self,
        legacy: Callable[[int, object], object],
        protobuf: Callable[[int, object], object],
    ) -> None:
        if self._legacy_guard is not None or self._protobuf_guard is not None:
            raise IbkrRuntimeError("IBKR_RUNTIME_COMMAND_ENCODER_ALREADY_INSTALLED")
        self._legacy_guard = legacy
        self._protobuf_guard = protobuf

    def titan_encoders_are(self, legacy: object, protobuf: object) -> bool:
        return self._legacy_guard is legacy and self._protobuf_guard is protobuf

    def connect(self, host: str, port: int, clientId: int) -> object:
        result = self._client.connect(host, port, clientId)
        self.conn = getattr(self._client, "conn", None)
        self.host = getattr(self._client, "host", None)
        self.port = getattr(self._client, "port", None)
        self.clientId = getattr(self._client, "clientId", None)
        return result

    def disconnect(self) -> object:
        return self._client.disconnect()

    def isConnected(self) -> bool:
        return self._client.isConnected() is True

    def run(self) -> None:
        self._client.run()

    def sendMsg(self, msgId: int, msg: object) -> object:
        return self._inner_legacy(msgId, msg)

    def sendMsgProtoBuf(self, msgId: int, msg: object) -> object:
        return self._inner_protobuf(msgId, msg)

    def _forward_legacy(self, msgId: int, msg: object) -> object:
        if self._legacy_guard is not None:
            return self._legacy_guard(msgId, msg)
        return self._inner_legacy(msgId, msg)

    def _forward_protobuf(self, msgId: int, msg: object) -> object:
        if self._protobuf_guard is not None:
            return self._protobuf_guard(msgId, msg)
        return self._inner_protobuf(msgId, msg)

    def placeOrder(self, orderId: int, contract: object, order: object) -> object:
        return self._client.placeOrder(orderId, contract, order)

    def cancelOrder(self, orderId: int, orderCancel: object) -> object:
        return self._client.cancelOrder(orderId, orderCancel)


class _AttendedRuntimeFacade:
    """Exact-account-private adapter for :mod:`live.attended_control`."""

    def __init__(
        self,
        *,
        runtime: "IbkrOfficialRuntime",
        transport: IbkrProductionTransport,
    ) -> None:
        self._runtime = runtime
        self._transport = transport
        self._cancel_reviews: dict[str, AttendedCancelReview] = {}

    @property
    def account_key(self) -> str:
        return self._runtime._profile.account_key

    @property
    def account_masked(self) -> str:
        return f"****{self._runtime._profile.account_last4}"

    @property
    def capabilities(self) -> BrokerCapabilities:
        return self._transport.descriptor.capabilities

    def _account(self) -> str:
        with self._runtime._condition:
            exact = self._runtime._exact_account_id
            if exact is None or self._runtime._state != "READY":
                raise BrokerMutationBlocked("IBKR attended command runtime is not ready")
            return exact

    def review_order(self, request: OrderRequest) -> AttendedLocalReview:
        return self._transport.review_equity_order(self._account(), request)

    def prepare_mutation(self) -> None:
        """Acquire the same interlock that SDK dispatch will revalidate."""

        self._account()
        self._transport.session.validate_mutation_interlock()

    def place_order(
        self,
        request: OrderRequest,
        review: AttendedLocalReview,
        exact_confirmation: str,
    ) -> BrokerOperationResult:
        if not isinstance(review, AttendedLocalReview):
            raise BrokerMutationBlocked("IBKR exact attended review is required")
        return self._transport.place_equity_order(
            self._account(),
            request,
            review=review,
            explicit_confirmation=exact_confirmation,
        )

    def review_cancel(self, broker_order_id: str) -> AttendedCancelReview:
        review = self._transport.review_cancel_equity_order(
            self._account(), broker_order_id
        )
        self._cancel_reviews[broker_order_id] = review
        return review

    def cancel_order(
        self,
        broker_order_id: str,
        review: AttendedCancelReview,
        exact_confirmation: str,
    ) -> BrokerOperationResult:
        if (
            not isinstance(review, AttendedCancelReview)
            or self._cancel_reviews.pop(broker_order_id, None) != review
            or review.broker_order_id != broker_order_id
            or exact_confirmation != review.required_confirmation_phrase
        ):
            raise BrokerMutationBlocked("IBKR exact attended cancel review is required")
        return self._transport.cancel_equity_order(
            self._account(),
            broker_order_id,
            explicit_confirmation=exact_confirmation,
        )

    def review_protection(
        self,
        source_request: OrderRequest,
        source_plan_id: str,
        stop_template: OrderRequest,
        source_claimed_at: datetime,
    ) -> AttendedLocalReview:
        return self._transport.review_protection_equity_order(
            self._account(),
            source_request,
            source_plan_id,
            stop_template,
            source_claimed_at,
        )


class IbkrOfficialRuntime:
    """Bounded local lifecycle for the two official TWS API connections."""

    def __init__(
        self,
        *,
        profile: IbkrLocalProviderProfile,
        install_root: str | Path,
        read_timeout_seconds: float = 10.0,
        instrument_timeout_seconds: float = 10.0,
        connect_timeout_seconds: float = 10.0,
        shutdown_timeout_seconds: float = 5.0,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if not isinstance(profile, IbkrLocalProviderProfile):
            raise TypeError("IBKR local provider profile is required")
        if (
            profile.profile_id != "ibkr-local-live-ending-3103-v1"
            or profile.account_key != "ibkr-live-ending-3103"
            or profile.account_last4 != "3103"
            or profile.environment != "live"
            or profile.host != "127.0.0.1"
            or profile.port != 4001
            or profile.sdk_version != SUPPORTED_SDK_VERSION
            or type(profile.read_client_id) is not int
            or type(profile.command_client_id) is not int
            or not 0 <= profile.read_client_id <= 2_147_483_647
            or not 0 < profile.command_client_id < 2_147_483_647
            or profile.read_client_id == profile.command_client_id
        ):
            raise ValueError("IBKR runtime profile is not the reviewed live loopback profile")
        values = (
            read_timeout_seconds,
            instrument_timeout_seconds,
            connect_timeout_seconds,
            shutdown_timeout_seconds,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0 < float(value) <= 30
            for value in values
        ):
            raise ValueError("IBKR runtime timeouts must be in (0, 30]")
        if not callable(clock):
            raise TypeError("IBKR runtime clock must be callable")
        self._profile = profile
        self._install_root = Path(install_root).expanduser()
        self._read_timeout = float(read_timeout_seconds)
        self._instrument_timeout = float(instrument_timeout_seconds)
        self._connect_timeout = float(connect_timeout_seconds)
        self._shutdown_timeout = float(shutdown_timeout_seconds)
        self._clock = clock
        self._condition = Condition(RLock())
        self._state: RuntimeState = "NEW"
        self._bundle: _AttestedSdkBundle | None = None
        self._read_generation = 0
        self._command_generation = 0
        self._read_router: _ReadCallbackRouter | None = None
        self._command_router: _CommandCallbackRouter | None = None
        self._read_requester: _OfficialReadRequester | None = None
        self._command_client: _OfficialCommandClient | None = None
        self._read_thread: Thread | None = None
        self._command_thread: Thread | None = None
        self._managed_accounts: str | None = None
        self._exact_account_id: str | None = None
        self._account_fingerprint: str | None = None
        self._components: IbkrRuntimeComponents | None = None
        self._session: IbkrSdkSession | None = None
        self._fatal_code: str | None = None
        self._command_next_seen = False
        self._command_account_seen = False
        self._errors: deque[SanitizedIbkrRuntimeError] = deque(maxlen=256)
        self._order_events: deque[SanitizedIbkrOrderEvent] = deque(maxlen=256)
        self._last_observed_at: datetime | None = None
        self._attended_facade: _AttendedRuntimeFacade | None = None

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(profile_id={self._profile.profile_id!r}, "
            f"account='ending-{self._profile.account_last4}', state={self._state!r})"
        )

    @property
    def account_binding_fingerprint(self) -> str:
        with self._condition:
            if self._account_fingerprint is None:
                raise IbkrRuntimeError("IBKR_RUNTIME_ACCOUNT_NOT_DISCOVERED")
            return self._account_fingerprint

    @property
    def components(self) -> IbkrRuntimeComponents:
        with self._condition:
            if self._components is None:
                raise IbkrRuntimeError("IBKR_RUNTIME_READS_NOT_READY")
            return self._components

    @property
    def command_session(self) -> IbkrSdkSession:
        with self._condition:
            if self._session is None or self._state != "READY":
                raise IbkrRuntimeError("IBKR_RUNTIME_COMMAND_NOT_READY")
            return self._session

    @property
    def sanitized_errors(self) -> tuple[SanitizedIbkrRuntimeError, ...]:
        with self._condition:
            return tuple(self._errors)

    @property
    def sanitized_order_events(self) -> tuple[SanitizedIbkrOrderEvent, ...]:
        with self._condition:
            return tuple(self._order_events)

    def status(self) -> IbkrRuntimeStatus:
        with self._condition:
            read_connected = (
                self._read_requester is not None
                and self._read_requester.is_connected()
            )
            command_connected = (
                self._command_client is not None
                and self._command_client.isConnected()
            )
            return IbkrRuntimeStatus(
                state=self._state,
                profile_id=self._profile.profile_id,
                account_masked=f"ending-{self._profile.account_last4}",
                endpoint=f"{self._profile.host}:{self._profile.port}",
                read_client_id=self._profile.read_client_id,
                command_client_id=self._profile.command_client_id,
                read_generation=self._read_generation,
                command_generation=self._command_generation,
                read_connected=read_connected,
                command_connected=command_connected,
                account_authenticated=(
                    self._exact_account_id is not None and self._fatal_code is None
                ),
                sdk_attested=self._bundle is not None,
                error_codes=tuple(
                    (item.connection, item.request_id, item.code, item.scope)
                    for item in self._errors
                ),
                last_observed_at=self._last_observed_at,
                runtime_error_code=self._fatal_code,
            )

    def connect_reads(self) -> IbkrRuntimeComponents:
        """Attest/import the SDK and establish only the read connection."""

        with self._condition:
            if self._state not in ("NEW", "STOPPED"):
                raise IbkrRuntimeError("IBKR_RUNTIME_ALREADY_STARTED")
            self._prepare_generation()
            self._read_generation += 1
            generation = self._read_generation
        try:
            bundle = _load_attested_sdk(self._install_root, self._profile)
            router = _ReadCallbackRouter(self, generation)
            requester = _OfficialReadRequester(bundle, router)
            with self._condition:
                self._bundle = bundle
                self._read_router = router
                self._read_requester = requester
            requester.connect(
                self._profile.host,
                self._profile.port,
                self._profile.read_client_id,
            )
            thread = Thread(
                target=self._read_event_loop,
                args=(requester, generation),
                name=f"titan-ibkr-read-{self._profile.read_client_id}",
                daemon=True,
            )
            with self._condition:
                self._read_thread = thread
            thread.start()
            self._wait_for_read_account(generation)
            with self._condition:
                exact = self._exact_account_id
                accounts = self._managed_accounts
            if exact is None or accounts is None:
                raise IbkrRuntimeError("IBKR_RUNTIME_ACCOUNT_DISCOVERY_FAILED")

            account_masked = f"****{self._profile.account_last4}"
            read_clock = IbkrRuntimeClock(self._clock)
            instrument_clock = IbkrRuntimeClock(self._clock)
            execution_filter_factory = IbkrSdkObjectFactory(
                bundle.execution_filter_type, "execution_filter"
            )
            instrument_contract_factory = IbkrSdkObjectFactory(
                bundle.contract_type, "contract"
            )
            transport_contract_factory = IbkrSdkObjectFactory(
                bundle.contract_type, "contract"
            )
            order_factory = IbkrSdkObjectFactory(bundle.order_type, "order")
            contract_requester = _ContractDetailsRequester(requester)
            read_bridge = IbkrWholeAccountReadBridge(
                requester=requester,
                exact_account_id=exact,
                account_masked=account_masked,
                execution_filter_factory=execution_filter_factory,
                timeout_seconds=self._read_timeout,
                clock=read_clock,
            )
            instrument_provider = IbkrInstrumentProvider(
                requester=contract_requester,
                contract_factory=instrument_contract_factory,
                exact_account_id=exact,
                account_masked=account_masked,
                timeout_seconds=self._instrument_timeout,
                clock=instrument_clock,
            )
            stable_account_clock = IbkrRuntimeClock(self._clock)
            account_snapshot_reader = IbkrStableAccountSnapshotReader(
                reads=read_bridge,
                exact_account_id=exact,
                account_masked=account_masked,
                clock=stable_account_clock,
            )
            callbacks = (
                read_bridge.open_generation(generation),
                instrument_provider.open_generation(generation),
            )
            router.bind_targets(callbacks, accounts)
            fingerprint = self._account_fingerprint
            if fingerprint is None:
                raise IbkrRuntimeError("IBKR_RUNTIME_ACCOUNT_DISCOVERY_FAILED")
            components = IbkrRuntimeComponents(
                read_bridge=read_bridge,
                instrument_provider=instrument_provider,
                account_snapshot_reader=account_snapshot_reader,
                contract_factory=transport_contract_factory,
                order_factory=order_factory,
                account_masked=account_masked,
                account_binding_fingerprint=fingerprint,
                read_generation=generation,
            )
            with self._condition:
                if self._fatal_code is not None or not requester.is_connected():
                    raise IbkrRuntimeError("IBKR_RUNTIME_READ_CONNECTION_LOST")
                self._components = components
                self._managed_accounts = None
                self._state = "READ_READY"
                self._condition.notify_all()
            return components
        except Exception as exc:
            self._fail_and_stop(
                exc.code if isinstance(exc, IbkrRuntimeError)
                else "IBKR_RUNTIME_READ_BOOTSTRAP_FAILED"
            )
            if isinstance(exc, IbkrRuntimeError):
                raise
            raise IbkrRuntimeError("IBKR_RUNTIME_READ_BOOTSTRAP_FAILED") from None

    def attended_descriptor(
        self,
        *,
        authorization_binding_id: str,
        coverage: OrderCoverageContract,
    ) -> object:
        """Build the normalized attended descriptor without exposing account ID."""

        with self._condition:
            exact = self._exact_account_id
            fingerprint = self._account_fingerprint
            components = self._components
            state = self._state
        if (
            exact is None
            or fingerprint is None
            or components is None
            or state not in ("READ_READY", "READY")
            or not isinstance(coverage, OrderCoverageContract)
        ):
            raise IbkrRuntimeError("IBKR_RUNTIME_READS_NOT_READY")
        return attended_ibkr_descriptor(
            exact_account_id=exact,
            account_masked=components.account_masked,
            account_binding_fingerprint=fingerprint,
            authorization_binding_id=authorization_binding_id,
            coverage=coverage,
        )

    def autonomous_descriptor(
        self,
        *,
        authorization_binding_id: str,
        coverage: OrderCoverageContract,
        authority: VerifiedIbkrAutonomousAuthority,
        authority_bindings: IbkrAutonomousAuthorityBindings,
        now: datetime,
    ) -> object:
        """Build the autonomous descriptor without exposing the account ID.

        This is only a private-account bridge into the already authenticated
        descriptor factory.  The factory independently verifies the complete
        provider-authority contract and exact release/account/client bindings;
        this method does not activate the runtime or authorize an order.
        """

        with self._condition:
            exact = self._exact_account_id
            fingerprint = self._account_fingerprint
            components = self._components
            state = self._state
        if (
            exact is None
            or fingerprint is None
            or components is None
            or state not in ("READ_READY", "READY")
            or not isinstance(coverage, OrderCoverageContract)
            or not isinstance(authority, VerifiedIbkrAutonomousAuthority)
            or not isinstance(authority_bindings, IbkrAutonomousAuthorityBindings)
            or not isinstance(now, datetime)
            or now.tzinfo is None
        ):
            raise IbkrRuntimeError("IBKR_RUNTIME_READS_NOT_READY")
        return autonomous_ibkr_descriptor(
            exact_account_id=exact,
            account_masked=components.account_masked,
            account_binding_fingerprint=fingerprint,
            authorization_binding_id=authorization_binding_id,
            coverage=coverage,
            authority=authority,
            authority_bindings=authority_bindings,
            now=now,
        )

    def connect_command(
        self,
        *,
        mutation_interlock: Callable[[], None],
        authorize_dispatch: DispatchAuthorizer,
    ) -> IbkrSdkSession:
        """Connect the guarded command client; grants no reviewed write receipt."""

        if not callable(mutation_interlock) or not callable(authorize_dispatch):
            raise TypeError("IBKR command bootstrap requires interlock and authority")
        with self._condition:
            if (
                self._state != "READ_READY"
                or self._bundle is None
                or self._exact_account_id is None
                or self._account_fingerprint is None
                or self._session is not None
            ):
                raise IbkrRuntimeError("IBKR_RUNTIME_READS_NOT_READY")
            self._command_generation = 0
            self._command_next_seen = False
            self._command_account_seen = False
            bundle = self._bundle
            exact = self._exact_account_id
            fingerprint = self._account_fingerprint
        router = _CommandCallbackRouter(self)
        client = _OfficialCommandClient(bundle, router)
        cancel_factory = IbkrSdkObjectFactory(bundle.order_cancel_type, "order_cancel")
        session = IbkrSdkSession(
            client=client,
            sdk_version=self._profile.sdk_version,
            expected_account=exact,
            account_binding_fingerprint=fingerprint,
            environment="live",
            client_id=self._profile.command_client_id,
            order_cancel_factory=cancel_factory,
            api_host=self._profile.host,
            api_port=self._profile.port,
            mutation_interlock=mutation_interlock,
            authorize_dispatch=authorize_dispatch,
            clock=self._clock,
        )
        try:
            generation = session.connect()
            router.bind(session, generation)
            with self._condition:
                self._command_router = router
                self._command_client = client
                self._session = session
                self._command_generation = generation
            thread = Thread(
                target=self._command_event_loop,
                args=(client, generation),
                name=f"titan-ibkr-command-{self._profile.command_client_id}",
                daemon=True,
            )
            with self._condition:
                self._command_thread = thread
            thread.start()
            self._wait_for_command_ready(generation)
            with self._condition:
                if self._fatal_code is not None or not client.isConnected():
                    raise IbkrRuntimeError("IBKR_RUNTIME_COMMAND_CONNECTION_LOST")
                self._state = "READY"
                self._condition.notify_all()
            # IbkrSdkSession starts with no IbkrWriteEvidence; authority alone
            # cannot dispatch.  Each order still requires the exact attended
            # review and one-shot durable claim in the production transport.
            return session
        except Exception as exc:
            try:
                session.revoke_writes()
                session.disconnect()
            except Exception:
                pass
            router.detach()
            self._join_thread(self._command_thread)
            with self._condition:
                self._command_router = None
                self._command_client = None
                self._command_thread = None
                self._session = None
                self._command_generation = 0
                self._state = "READ_READY" if self._read_requester is not None else "FAILED"
            if isinstance(exc, IbkrRuntimeError):
                raise
            raise IbkrRuntimeError("IBKR_RUNTIME_COMMAND_BOOTSTRAP_FAILED") from None

    def prepare_disconnected_command(
        self,
        *,
        mutation_interlock: Callable[[], None],
        authorize_dispatch: DispatchAuthorizer,
    ) -> IbkrSdkSession:
        """Build the release-attested command graph without opening its socket.

        Readiness and activation need to prove the exact executable graph that
        the service will use, but those control-plane commands have no reason
        to establish a mutation-capable broker connection.  The returned
        session has installed write guards, no connection generation, no
        broker order ID, and no write evidence; every dispatch therefore fails
        closed even if a caller accidentally reaches it.
        """

        if not callable(mutation_interlock) or not callable(authorize_dispatch):
            raise TypeError("IBKR command bootstrap requires interlock and authority")
        with self._condition:
            if (
                self._state != "READ_READY"
                or self._bundle is None
                or self._exact_account_id is None
                or self._account_fingerprint is None
                or self._session is not None
            ):
                raise IbkrRuntimeError("IBKR_RUNTIME_READS_NOT_READY")
            bundle = self._bundle
            exact = self._exact_account_id
            fingerprint = self._account_fingerprint
        router = _CommandCallbackRouter(self)
        client = _OfficialCommandClient(bundle, router)
        cancel_factory = IbkrSdkObjectFactory(bundle.order_cancel_type, "order_cancel")
        session = IbkrSdkSession(
            client=client,
            sdk_version=self._profile.sdk_version,
            expected_account=exact,
            account_binding_fingerprint=fingerprint,
            environment="live",
            client_id=self._profile.command_client_id,
            order_cancel_factory=cancel_factory,
            api_host=self._profile.host,
            api_port=self._profile.port,
            mutation_interlock=mutation_interlock,
            authorize_dispatch=authorize_dispatch,
            clock=self._clock,
        )
        # Prove the injected authority identity while remaining disconnected.
        session.assert_dispatch_authorizer(authorize_dispatch)
        return session

    def bind_attended_transport(
        self,
        *,
        transport: IbkrProductionTransport,
    ) -> None:
        """Bind the already release-checked transport; never creates authority."""

        if not isinstance(transport, IbkrProductionTransport):
            raise TypeError("concrete IBKR production transport is required")
        with self._condition:
            exact = self._exact_account_id
            session = self._session
            if (
                self._state != "READY"
                or exact is None
                or session is None
                or transport.session is not session
                or transport.descriptor.exact_account_id != exact
                or transport.descriptor.account_binding_fingerprint
                != self._account_fingerprint
                or self._attended_facade is not None
            ):
                raise IbkrRuntimeError("IBKR_RUNTIME_ATTENDED_TRANSPORT_BINDING_INVALID")
            self._attended_facade = _AttendedRuntimeFacade(
                runtime=self,
                transport=transport,
            )

    def attended_runtime(self) -> _AttendedRuntimeFacade:
        with self._condition:
            if self._attended_facade is None or self._state != "READY":
                raise IbkrRuntimeError("IBKR_RUNTIME_ATTENDED_TRANSPORT_NOT_READY")
            return self._attended_facade

    def probe_reads(self, symbol: str = "SPY") -> IbkrReadProbe:
        """Exercise normalized account callbacks and one contract metadata read.

        ``get_account_base`` requests account summary, positions, open orders,
        completed orders, executions, and daily P&L before returning.  The
        result below exposes only nonsecret correlation receipts.  Completed
        orders are current-day only and executions have API-client visibility
        limits; completed callbacks do not prove whole-broker history coverage.
        Contract connectivity is distinct from regular-session eligibility.
        """

        normalized = str(symbol).strip().upper()
        with self._condition:
            components = self._components
            exact = self._exact_account_id
            connected = (
                self._read_requester is not None
                and self._read_requester.is_connected()
            )
        if components is None or exact is None or not connected:
            return IbkrReadProbe(
                phase="BLOCKED",
                connected=connected,
                authenticated=False,
                observed_at=None,
                error_code="IBKR_RUNTIME_READS_NOT_READY",
                account_masked=f"ending-{self._profile.account_last4}",
                account_collection_id=None,
                instrument_evidence_id=None,
                symbol=normalized,
            )
        try:
            observation = components.read_bridge.get_account_base(exact)
        except Exception as exc:
            return IbkrReadProbe(
                phase="BLOCKED",
                connected=(
                    self._read_requester is not None
                    and self._read_requester.is_connected()
                ),
                authenticated=False,
                observed_at=None,
                error_code=self._probe_error_code(exc, contract=False),
                account_masked=f"ending-{self._profile.account_last4}",
                account_collection_id=None,
                instrument_evidence_id=None,
                symbol=normalized,
            )
        try:
            instrument = components.instrument_provider.read_contract_metadata(
                normalized, now=self._now()
            )
            return IbkrReadProbe(
                phase="CONNECTED",
                connected=True,
                authenticated=True,
                observed_at=max(
                    observation.request_completed_at,
                    instrument.received_at,
                ),
                error_code=None,
                account_masked=f"ending-{self._profile.account_last4}",
                account_collection_id=observation.collection_id,
                instrument_evidence_id=None,
                symbol=instrument.identity.symbol,
                contract_read_receipt_id=instrument.receipt_id,
                contract_regular_session_open=instrument.regular_session_open,
            )
        except Exception as exc:
            return IbkrReadProbe(
                phase="BLOCKED",
                connected=(
                    self._read_requester is not None
                    and self._read_requester.is_connected()
                ),
                authenticated=True,
                observed_at=observation.request_completed_at,
                error_code=self._probe_error_code(exc, contract=True),
                account_masked=f"ending-{self._profile.account_last4}",
                account_collection_id=observation.collection_id,
                instrument_evidence_id=None,
                symbol=normalized,
            )

    def stop(self) -> None:
        """Revoke command evidence, disconnect both clients, and join threads."""

        with self._condition:
            session = self._session
            command_router = self._command_router
            read_router = self._read_router
            requester = self._read_requester
            command_thread = self._command_thread
            read_thread = self._read_thread
        if session is not None:
            try:
                session.revoke_writes()
                session.disconnect()
            except Exception:
                self._record_error("command", -1, 0, "disconnect_failed")
        if requester is not None:
            try:
                requester.disconnect()
            except Exception:
                self._record_error("read", -1, 0, "disconnect_failed")
        if command_router is not None:
            command_router.detach()
        if read_router is not None:
            read_router.detach()
        command_stopped = self._join_thread(command_thread)
        read_stopped = self._join_thread(read_thread)
        with self._condition:
            self._session = None
            self._command_client = None
            self._command_router = None
            self._read_requester = None
            self._read_router = None
            self._command_thread = None
            self._read_thread = None
            self._components = None
            self._managed_accounts = None
            self._exact_account_id = None
            self._account_fingerprint = None
            self._command_next_seen = False
            self._command_account_seen = False
            self._fatal_code = None
            self._attended_facade = None
            self._state = "STOPPED" if command_stopped and read_stopped else "FAILED"
            self._condition.notify_all()
        if not (command_stopped and read_stopped):
            raise IbkrRuntimeError("IBKR_RUNTIME_EVENT_LOOP_DID_NOT_STOP")

    def _prepare_generation(self) -> None:
        self._bundle = None
        self._fatal_code = None
        self._managed_accounts = None
        self._exact_account_id = None
        self._account_fingerprint = None
        self._components = None
        self._session = None
        self._attended_facade = None
        self._state = "NEW"

    def _account_digest(self, exact: str) -> str:
        material = "\0".join(
            (
                "titan-ibkr-account-binding-v1",
                self._profile.profile_id,
                self._profile.account_key,
                self._profile.environment,
                exact,
            )
        )
        return hashlib.sha256(material.encode("ascii")).hexdigest()

    def _select_account(self, accounts: object) -> str:
        if not isinstance(accounts, str):
            raise IbkrRuntimeError("IBKR_RUNTIME_MANAGED_ACCOUNTS_INVALID")
        values = tuple(item.strip() for item in accounts.split(",") if item.strip())
        matches = tuple(
            item
            for item in values
            if _LIVE_ACCOUNT.fullmatch(item)
            and item.endswith(self._profile.account_last4)
        )
        # The command session deliberately requires the same one-account list;
        # accepting a broader managed-account set would silently weaken that
        # binding and create an ambiguous order/account callback surface.
        if len(values) != 1 or len(matches) != 1 or values != matches:
            raise IbkrRuntimeError("IBKR_RUNTIME_MANAGED_ACCOUNT_NOT_UNIQUE")
        return matches[0]

    def _observe_read_accounts(self, generation: int, accounts: object) -> None:
        with self._condition:
            if generation != self._read_generation or self._state in ("STOPPED", "FAILED"):
                return
            try:
                exact = self._select_account(accounts)
            except IbkrRuntimeError as exc:
                self._fatal_code = exc.code
                self._condition.notify_all()
                return
            if self._exact_account_id is not None and self._exact_account_id != exact:
                self._fatal_code = "IBKR_RUNTIME_MANAGED_ACCOUNT_CHANGED"
            else:
                self._exact_account_id = exact
                self._account_fingerprint = self._account_digest(exact)
                self._managed_accounts = accounts
                self._last_observed_at = self._now()
            self._condition.notify_all()

    def _accept_read_generation(self, generation: int) -> bool:
        with self._condition:
            return (
                generation == self._read_generation
                and self._state not in ("STOPPED", "FAILED")
            )

    def _accept_command_generation(self, generation: int) -> bool:
        with self._condition:
            return generation > 0 and generation == self._command_generation

    def _observe_command_ready(
        self, generation: int, *, next_id: bool = False, account: bool = False
    ) -> None:
        with self._condition:
            if generation != self._command_generation:
                return
            self._command_next_seen = self._command_next_seen or next_id
            self._command_account_seen = self._command_account_seen or account
            self._condition.notify_all()

    def _command_callback_failed(self, generation: int) -> None:
        with self._condition:
            if generation == self._command_generation:
                self._fatal_code = "IBKR_RUNTIME_COMMAND_CALLBACK_REJECTED"
                self._condition.notify_all()

    def _observe_connection_closed(
        self, connection: Literal["read", "command"], generation: int
    ) -> None:
        with self._condition:
            expected = (
                self._read_generation if connection == "read" else self._command_generation
            )
            if generation == expected and self._state not in ("STOPPED", "FAILED"):
                self._fatal_code = f"IBKR_RUNTIME_{connection.upper()}_CONNECTION_CLOSED"
                self._condition.notify_all()

    def _read_event_loop(
        self, requester: _OfficialReadRequester, generation: int
    ) -> None:
        try:
            requester.run()
        except Exception:
            self._record_error("read", -1, 0, "event_loop_failed")
        finally:
            with self._condition:
                if (
                    generation == self._read_generation
                    and requester.is_connected()
                    and self._state not in ("STOPPED", "FAILED")
                ):
                    self._fatal_code = "IBKR_RUNTIME_READ_EVENT_LOOP_EXITED"
                self._condition.notify_all()

    def _command_event_loop(
        self, client: _OfficialCommandClient, generation: int
    ) -> None:
        try:
            client.run()
        except Exception:
            self._record_error("command", -1, 0, "event_loop_failed")
        finally:
            with self._condition:
                if (
                    generation == self._command_generation
                    and client.isConnected()
                    and self._state not in ("STOPPED", "FAILED")
                ):
                    self._fatal_code = "IBKR_RUNTIME_COMMAND_EVENT_LOOP_EXITED"
                self._condition.notify_all()

    def _wait_for_read_account(self, generation: int) -> None:
        deadline = time.monotonic() + self._connect_timeout
        with self._condition:
            while (
                generation == self._read_generation
                and self._exact_account_id is None
                and self._fatal_code is None
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._fatal_code = "IBKR_RUNTIME_ACCOUNT_DISCOVERY_TIMEOUT"
                    break
                self._condition.wait(remaining)
            if self._fatal_code is not None:
                raise IbkrRuntimeError(self._fatal_code)
            if self._exact_account_id is None:
                raise IbkrRuntimeError("IBKR_RUNTIME_ACCOUNT_DISCOVERY_FAILED")

    def _wait_for_command_ready(self, generation: int) -> None:
        deadline = time.monotonic() + self._connect_timeout
        with self._condition:
            while (
                generation == self._command_generation
                and not (self._command_next_seen and self._command_account_seen)
                and self._fatal_code is None
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._fatal_code = "IBKR_RUNTIME_COMMAND_HANDSHAKE_TIMEOUT"
                    break
                self._condition.wait(remaining)
            if self._fatal_code is not None:
                raise IbkrRuntimeError(self._fatal_code)
            if not (self._command_next_seen and self._command_account_seen):
                raise IbkrRuntimeError("IBKR_RUNTIME_COMMAND_HANDSHAKE_INCOMPLETE")

    def _join_thread(self, thread: Thread | None) -> bool:
        if thread is None:
            return True
        if thread is current_thread():
            return False
        thread.join(self._shutdown_timeout)
        return not thread.is_alive()

    def _fail_and_stop(self, code: str) -> None:
        with self._condition:
            self._fatal_code = code
            self._state = "FAILED"
            requester = self._read_requester
            thread = self._read_thread
            router = self._read_router
        if requester is not None:
            try:
                requester.disconnect()
            except Exception:
                pass
        if router is not None:
            router.detach()
        self._join_thread(thread)
        with self._condition:
            self._managed_accounts = None
            self._exact_account_id = None
            self._account_fingerprint = None
            self._components = None
            self._condition.notify_all()

    def _now(self) -> datetime:
        try:
            value = self._clock()
            if not isinstance(value, datetime) or value.tzinfo is None:
                raise ValueError
            return value.astimezone(timezone.utc)
        except Exception:
            return datetime.now(timezone.utc)

    @staticmethod
    def _sanitize_error_values(request_id: object, code: object) -> tuple[int, int]:
        request = request_id if type(request_id) is int else -1
        normalized = code if type(code) is int else 0
        if not -1 <= request <= 2_147_483_647:
            request = -1
        if not 0 <= normalized <= 2_147_483_647:
            normalized = 0
        return request, normalized

    @staticmethod
    def _error_code_from_callback(arguments: tuple[object, ...]) -> object:
        """Extract only the numeric code from supported SDK callback shapes."""

        return classify_ibkr_error_callback(arguments)[0]

    @staticmethod
    def _probe_error_code(error: BaseException, *, contract: bool) -> str:
        """Expose a numeric SDK code and an allowlisted cause, never raw text."""

        message = str(error)
        if contract:
            if message in {
                "IBKR_CONTRACT_NOT_REGULAR_HOURS_ELIGIBLE",
                "IBKR_CONTRACT_LOOKUP_NOT_UNIQUE",
                "IBKR_CONTRACT_DETAILS_TIMEOUT",
                "IBKR_CONTRACT_DETAILS_DISPATCH_FAILED",
                "IBKR_CONTRACT_QUERY_CONSTRUCTION_FAILED",
                "IBKR_INSTRUMENT_NOT_AUTHENTICATED",
                "IBKR_INSTRUMENT_GENERATION_LOST",
                "IBKR_INSTRUMENT_REQUEST_QUEUE_TIMEOUT",
                "IBKR_INSTRUMENT_SYMBOL_INVALID",
            }:
                return "IBKR_RUNTIME_" + message.removeprefix("IBKR_")
            matched = re.fullmatch(
                r"IBKR_CONTRACT_DETAILS_ERROR:([0-9]{1,10})",
                message,
            )
            if matched is not None:
                return "IBKR_RUNTIME_CONTRACT_SDK_CALLBACK_" + matched.group(1)
            return "IBKR_RUNTIME_CONTRACT_PROBE_FAILED"
        if message == "IBKR_READ_TIMEOUT:daily_realized_pnl":
            return "IBKR_RUNTIME_READ_DAILY_REALIZED_PNL_TIMEOUT"
        matched = re.fullmatch(
            r"IBKR_READ_CALLBACK_ERROR:([0-9]{1,10}):sdk_callback(:API_READ_ONLY)?",
            message,
        )
        if matched is not None:
            suffix = (
                "_API_READ_ONLY"
                if matched.group(1) == "321" and matched.group(2) is not None
                else ""
            )
            return "IBKR_RUNTIME_READ_SDK_CALLBACK_" + matched.group(1) + suffix
        return "IBKR_RUNTIME_READ_PROBE_FAILED"

    def _record_error(
        self,
        connection: Literal["read", "command"],
        request_id: int,
        code: int,
        scope: str,
        reason: str = "UNSPECIFIED",
    ) -> None:
        safe_scope = scope if scope in {
            "sdk_callback",
            "event_loop_failed",
            "disconnect_failed",
        } else "runtime"
        with self._condition:
            self._errors.append(
                SanitizedIbkrRuntimeError(
                    connection=connection,
                    request_id=request_id,
                    code=code,
                    scope=safe_scope,
                    received_at=self._now(),
                    reason=(
                        "API_READ_ONLY" if code == 321 and reason == "API_READ_ONLY"
                        else "UNSPECIFIED"
                    ),
                )
            )
            self._last_observed_at = self._errors[-1].received_at
            self._condition.notify_all()

    def _record_order_event(
        self,
        generation: int,
        kind: Literal["open_order", "order_status", "completed_order"],
        order_id: object,
        client_id: object,
        status: object,
    ) -> None:
        if not self._accept_command_generation(generation):
            return
        safe_order = order_id if type(order_id) is int and order_id > 0 else 0
        if safe_order == 0:
            return
        safe_client = (
            client_id
            if type(client_id) is int and 0 <= client_id <= 2_147_483_647
            else None
        )
        safe_status = status if isinstance(status, str) and status in _ORDER_STATES else "UNKNOWN"
        with self._condition:
            self._order_events.append(
                SanitizedIbkrOrderEvent(
                    kind=kind,
                    order_id=safe_order,
                    client_id=safe_client,
                    status=safe_status,
                    received_at=self._now(),
                )
            )
            self._last_observed_at = self._order_events[-1].received_at
            self._condition.notify_all()


__all__ = [
    "IbkrOfficialRuntime",
    "IbkrReadProbe",
    "IbkrRuntimeComponents",
    "IbkrRuntimeClock",
    "IbkrRuntimeError",
    "IbkrRuntimeStatus",
    "IbkrSdkObjectFactory",
    "SanitizedIbkrOrderEvent",
    "SanitizedIbkrRuntimeError",
    "build_ibkr_official_runtime",
]


def build_ibkr_official_runtime(
    *,
    profile: IbkrLocalProviderProfile,
    install_root: str | Path,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    **timeouts: float,
) -> IbkrOfficialRuntime:
    """Checked-in factory hook used by the local provider assembly."""

    return IbkrOfficialRuntime(
        profile=profile,
        install_root=install_root,
        clock=clock,
        **timeouts,
    )
