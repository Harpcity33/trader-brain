"""Default-deny, dependency-injected boundary for the official TWS Python SDK.

Importing this module and constructing a session neither imports ``ibapi`` nor
opens a connection. The caller supplies an isolated, non-logging SDK client and
routes its callbacks with the generation returned by :meth:`connect`. Raw SDK
callbacks/logs must not be exported by that worker. No runtime factory, account
discovery, paid data request, polling loop, or trading activation lives here.

The reviewed 10.50.2 SDK uses placeOrder(id, contract, order) and
cancelOrder(id, OrderCancel()). Its legacy/protobuf outbound IDs are 3/203,
4/204 and START_API 71/271. An SDK return is NOT broker acceptance. The durable
coordinator must commit an intent before dispatch and reconcile broker events.
Neither this boundary nor an acceptance receipt establishes atomic brackets,
partial-fill protection, or unattended-trading support.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
import re
import sys
from threading import RLock, get_ident
from types import SimpleNamespace
from typing import Callable, Literal, Protocol

from .base import BrokerMutationBlocked, BrokerUnknownSubmission


SUPPORTED_SDK_VERSION = "10.50.2"
Environment = Literal["live", "paper"]
Operation = Literal["submit", "cancel"]


def _positive_id(value: object) -> int:
    if type(value) is not int or not 0 < value <= 2_147_483_647:
        raise ValueError("a positive 32-bit identifier is required")
    return value


def _receipt(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("a nonsecret SHA-256 receipt is required")
    return value


def _time(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("a timezone-aware timestamp is required")
    return value.astimezone(timezone.utc)


def _canonical_value(value: object) -> object:
    """Normalize public SDK data without invoking arbitrary repr/str methods."""
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) in (float, Decimal):
        if type(value) is float and not math.isfinite(value):
            raise ValueError("non-finite SDK number")
        decimal = Decimal(str(value))
        if not decimal.is_finite():
            raise ValueError("non-finite SDK number")
        # Avoid ambient Decimal context precision in normalize(). Whole shares
        # and equivalent SDK double representations must fingerprint equally.
        number = format(decimal, "f")
        if "." in number:
            number = number.rstrip("0").rstrip(".")
        return {"decimal": number if number not in ("-0", "") else "0"}
    if type(value) in (list, tuple):
        return [_canonical_value(item) for item in value]
    if type(value) is dict and all(type(key) is str for key in value):
        return {key: _canonical_value(item) for key, item in sorted(value.items())}
    # The sole nested object in a fresh supported official Order. All advanced
    # order structures are either absent or empty, not serialized by repr.
    if type(value).__name__ == "SoftDollarTier" and type(value).__module__ == "ibapi.softdollartier":
        fields = vars(value)
        if fields != {"name": "", "val": "", "displayName": ""}:
            raise ValueError("unsupported SDK soft-dollar tier")
        return {"softDollarTier": fields}
    raise ValueError("unsupported SDK payload structure")


def sdk_order_fingerprint(contract: object, order: object, account_binding_fingerprint: str) -> str:
    """Digest the exact supported mutable SDK objects, excluding raw account ID.

    Core order/contract fields are required and narrowly validated. Every other
    public SDK field is also included, so changing an optional default invalidates
    the reviewed payload even when a new field has no special-case validator.
    This digest is not authorization or proof that a broker preset is inactive.
    """
    try:
        _receipt(account_binding_fingerprint)
        contract_fields, order_fields = dict(vars(contract)), dict(vars(order))
        if any(type(key) is not str or key.startswith("_") for key in (*contract_fields, *order_fields)):
            raise ValueError("unsupported SDK attribute")
        _positive_id(contract_fields["conId"])
        _positive_id(order_fields["clientId"])
        _positive_id(order_fields["orderId"])
        if (
            contract_fields["secType"] != "STK" or contract_fields["currency"] != "USD"
            or contract_fields["exchange"] != "SMART"
            or not isinstance(contract_fields["symbol"], str)
            or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", contract_fields["symbol"])
            or not isinstance(contract_fields["primaryExchange"], str)
            or not re.fullmatch(r"[A-Z][A-Z0-9.]{0,15}", contract_fields["primaryExchange"])
            or contract_fields["primaryExchange"] in {"SMART", "BEST", "OVERNIGHT", "IBKRATS"}
        ):
            raise ValueError("unsupported stock contract")
        if (
            order_fields["action"] not in ("BUY", "SELL")
            or order_fields["orderType"] not in ("MKT", "LMT", "STP", "STP LMT")
            or order_fields["tif"] not in ("DAY", "GTC")
            or type(order_fields["outsideRth"]) is not bool
            or (order_fields["outsideRth"] and order_fields["orderType"] != "LMT")
            or type(order_fields["transmit"]) is not bool
            or not isinstance(order_fields["account"], str)
            or not re.fullmatch(r"(?:U|DU)[0-9]+", order_fields["account"])
            or not isinstance(order_fields["orderRef"], str)
            or not re.fullmatch(r"[ -~]{1,64}", order_fields["orderRef"])
        ):
            raise ValueError("unsupported order tuple")
        quantity = order_fields["totalQuantity"]
        if type(quantity) not in (int, Decimal) or not Decimal(quantity).is_finite() or not 0 < quantity <= 2_147_483_647 or quantity != int(quantity):
            raise ValueError("positive whole-share quantity required")
        order_fields["totalQuantity"] = Decimal(int(quantity))
        for field, required in (("lmtPrice", order_fields["orderType"] in ("LMT", "STP LMT")),
                                ("auxPrice", order_fields["orderType"] in ("STP", "STP LMT"))):
            value = order_fields.get(field, sys.float_info.max)
            if type(value) not in (int, float, Decimal) or not Decimal(str(value)).is_finite():
                raise ValueError("invalid SDK price")
            if required and not 0 < value < sys.float_info.max:
                raise ValueError("required SDK price missing")
            if not required and value != sys.float_info.max:
                raise ValueError("irrelevant SDK price must remain unset")
            order_fields[field] = Decimal(str(value))
        required_inert = {
            "whatIf": False, "includeOvernight": False,
            "parentId": 0, "ocaGroup": "", "ocaType": 0,
            "conditions": [], "conditionsCancelOrder": False,
            "conditionsIgnoreRth": False, "triggerMethod": 0,
            "overridePercentageConstraints": False, "advancedErrorOverride": "",
        }
        optional_inert = {
            "conditionsIncludeOvernight": False, "faGroup": "", "faMethod": "",
            "faPercentage": "", "modelCode": "", "clearingAccount": "",
            "clearingIntent": "", "settlingFirm": "", "customerAccount": "",
            "algoStrategy": "", "algoParams": None, "algoId": "",
            "orderMiscOptions": None, "smartComboRoutingParams": None,
            "orderComboLegs": None, "hedgeType": "", "hedgeParam": "",
            "activeStartTime": "", "activeStopTime": "", "goodAfterTime": "",
            "goodTillDate": "", "autoCancelDate": "", "autoCancelParent": False,
            "adjustedOrderType": "", "deltaNeutralOrderType": "",
            "cashQty": sys.float_info.max, "parentPermId": 0,
            "slOrderId": 2_147_483_647, "slOrderType": "",
            "ptOrderId": 2_147_483_647, "ptOrderType": "",
            "deactivate": False, "whatIfType": 2_147_483_647,
        }
        for name, default in required_inert.items():
            if name not in order_fields or type(order_fields[name]) is not type(default) or order_fields[name] != default:
                raise ValueError("unsupported order control")
        for name, default in optional_inert.items():
            if name in order_fields and (type(order_fields[name]) is not type(default) or order_fields[name] != default):
                raise ValueError("unsupported optional order control")
        for name, default in {"comboLegs": [], "deltaNeutralContract": None, "includeExpired": False,
                              "secIdType": "", "secId": "", "localSymbol": "",
                              "tradingClass": "", "lastTradeDateOrContractMonth": "",
                              "lastTradeDate": "", "right": "", "multiplier": ""}.items():
            if name in contract_fields and (type(contract_fields[name]) is not type(default) or contract_fields[name] != default):
                raise ValueError("unsupported optional contract control")
        # Do not even feed a raw account identifier into the digest. The session
        # separately checks its exact private identity on every dispatch gate.
        order_fields["account"] = {"account_binding_fingerprint": account_binding_fingerprint}
        payload = {"schema": "ibkr-sdk-order-v1", "contract": _canonical_value(contract_fields),
                   "order": _canonical_value(order_fields)}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
    except Exception:
        raise BrokerMutationBlocked("IBKR_ORDER_PAYLOAD_UNSUPPORTED") from None


def _pinned_sdk_defaults() -> tuple[dict[str, object], dict[str, object]]:
    """Public defaults observed from official 10.50.2 data objects offline.

    This static inventory intentionally refuses unknown future SDK fields.
    It is not loaded by importing the SDK, and has no account-specific values.
    """
    contract: dict[str, object] = {}
    contract.update(dict.fromkeys(
        "comboLegs "
        .split(), []))
    contract.update(dict.fromkeys(
        "comboLegsDescrip currency description exchange issuerId "
        "lastTradeDate lastTradeDateOrContractMonth localSymbol multiplier primaryExchange "
        "right secId secIdType secType symbol "
        "tradingClass "
        .split(), ""))
    contract.update(dict.fromkeys(
        "conId "
        .split(), 0))
    contract.update(dict.fromkeys(
        "deltaNeutralContract "
        .split(), None))
    contract.update(dict.fromkeys(
        "includeExpired "
        .split(), False))
    contract.update(dict.fromkeys(
        "strike "
        .split(), _canonical_value(sys.float_info.max)))
    order: dict[str, object] = {}
    order.update(dict.fromkeys(
        "account action activeStartTime activeStopTime adjustedOrderType "
        "advancedErrorOverride algoId algoStrategy autoCancelDate bondAccruedInterest "
        "clearingAccount clearingIntent customerAccount deltaNeutralClearingAccount deltaNeutralClearingIntent "
        "deltaNeutralDesignatedLocation deltaNeutralOpenClose deltaNeutralOrderType deltaNeutralSettlingFirm designatedLocation "
        "extOperator faGroup faMethod faPercentage goodAfterTime "
        "goodTillDate hedgeParam hedgeType manualOrderTime mifid2DecisionAlgo "
        "mifid2DecisionMaker mifid2ExecutionAlgo mifid2ExecutionTrader modelCode ocaGroup "
        "openClose orderRef orderType ptOrderType referenceExchangeId "
        "rule80A scaleTable settlingFirm shareholder slOrderType "
        "submitter tif "
        .split(), ""))
    order.update(dict.fromkeys(
        "adjustableTrailingUnit auctionStrategy clientId deltaNeutralConId deltaNeutralShortSaleSlot "
        "discretionaryAmt displaySize ocaType orderId origin "
        "parentId parentPermId permId refFuturesConId referenceContractId "
        "shortSaleSlot triggerMethod "
        .split(), 0))
    order.update(dict.fromkeys(
        "adjustedStopLimitPrice adjustedStopPrice adjustedTrailingAmount auxPrice basisPoints "
        "cashQty competeAgainstBestOffset delta deltaNeutralAuxPrice lmtPrice "
        "lmtPriceOffset midOffsetAtHalf midOffsetAtWhole percentOffset scalePriceAdjustValue "
        "scalePriceIncrement scaleProfitOffset startingPrice stockRangeLower stockRangeUpper "
        "stockRefPrice trailStopPrice trailingPercent triggerPrice volatility "
        .split(), _canonical_value(sys.float_info.max)))
    order.update(dict.fromkeys(
        "algoParams orderComboLegs orderMiscOptions routeMarketableToBbo seekPriceImprovement "
        "smartComboRoutingParams usePriceMgmtAlgo "
        .split(), None))
    order.update(dict.fromkeys(
        "allOrNone allowPreOpen autoCancelParent blockOrder conditionsCancelOrder "
        "conditionsIgnoreRth conditionsIncludeOvernight continuousUpdate deactivate deltaNeutralShortSale "
        "discretionaryUpToLimitPrice dontUseAutoPriceForHedge hidden ignoreOpenAuction imbalanceOnly "
        "includeOvernight isOmsContainer isPeggedChangeAmountDecrease notHeld optOutSmartRouting "
        "outsideRth overridePercentageConstraints postOnly professionalCustomer randomizePrice "
        "randomizeSize scaleAutoReset scaleRandomPercent solicited sweepToFill "
        "whatIf "
        .split(), False))
    order.update(dict.fromkeys(
        "basisPointsType duration hedgeMaxSize manualOrderIndicator minCompeteSize "
        "minQty minTradeQty postToAts ptOrderId referencePriceType "
        "scaleInitFillQty scaleInitLevelSize scaleInitPosition scalePriceAdjustInterval scaleSubsLevelSize "
        "slOrderId volatilityType whatIfType "
        .split(), 2147483647))
    order.update(dict.fromkeys(
        "conditions "
        .split(), []))
    order.update(dict.fromkeys(
        "exemptCode "
        .split(), -1))
    order.update(dict.fromkeys(
        "filledQuantity totalQuantity "
        .split(), _canonical_value(Decimal("170141183460469231731687303715884105727"))))
    order.update(dict.fromkeys(
        "peggedChangeAmount referenceChangeAmount "
        .split(), _canonical_value(Decimal("0"))))
    order.update(dict.fromkeys(
        "softDollarTier "
        .split(), {"softDollarTier": {"displayName": "", "name": "", "val": ""}}))
    order.update(dict.fromkeys(
        "transmit "
        .split(), True))
    return contract, order

def assert_sdk_order_matches_plan(plan: object, contract: object, order: object,
                                  account_binding_fingerprint: str) -> str:
    """Verify actual objects against the immutable plan and pinned inert defaults.

    A caller-provided digest alone cannot establish plan identity. Expected
    fields are derived independently from immutable request values, never by
    calling the encoder or caller's SDK factories. Every optional field must equal the reviewed SDK default;
    missing optional inert defaults are tolerated for offline value objects.
    """
    from .ibkr_orders import IbkrOrderPlan

    try:
        if type(plan) is not IbkrOrderPlan:
            raise ValueError("exact immutable IBKR plan required")
        _receipt(account_binding_fingerprint)
        request, identity = plan.request, plan.contract
        expected_contract = dict(conId=identity.con_id, symbol=identity.symbol,
            secType=identity.sec_type, currency=identity.currency,
            exchange=identity.exchange, primaryExchange=identity.primary_exchange)
        expected_order = dict(orderId=plan.order_id, clientId=plan.client_id,
            account=plan.account_id, action={"buy": "BUY", "sell": "SELL"}[request.side.value],
            totalQuantity=Decimal(request.quantity),
            orderType={"market": "MKT", "limit": "LMT", "stop_market": "STP",
                       "stop_limit": "STP LMT"}[request.order_type.value],
            tif={"gfd": "DAY", "gtc": "GTC"}[request.time_in_force.value],
            outsideRth=request.market_hours.value == "extended_hours",
            includeOvernight=False, orderRef=request.client_ref_id, transmit=plan.transmit,
            whatIf=False, parentId=0, ocaGroup="", ocaType=0, conditions=[],
            conditionsCancelOrder=False, conditionsIgnoreRth=False,
            triggerMethod=0, overridePercentageConstraints=False, advancedErrorOverride="")
        if request.limit_price is not None:
            expected_order["lmtPrice"] = request.limit_price
        if request.stop_price is not None:
            expected_order["auxPrice"] = request.stop_price
        actual_contract, actual_order = dict(vars(contract)), dict(vars(order))
        snapshot_order = dict(actual_order)
        if actual_order.get("account") != plan.account_id:
            raise ValueError("private account binding mismatch")
        pinned_contract, pinned_order = _pinned_sdk_defaults()
        if set(actual_contract) - set(pinned_contract) or set(actual_order) - set(pinned_order):
            raise ValueError("unknown SDK field cannot be approved")
        if set(expected_contract) - set(actual_contract) or set(expected_order) - set(actual_order):
            raise ValueError("explicit plan field is missing")
        # Account identity is compared privately above, never returned, persisted,
        # or incorporated in the digest. Normalize equivalent numeric encodings.
        actual_order.pop("account")
        expected_order_fields = dict(expected_order)
        expected_order_fields.pop("account")
        pinned_order.pop("account")
        for values in (actual_order, expected_order_fields):
            if "totalQuantity" in values and type(values["totalQuantity"]) in (int, Decimal):
                values["totalQuantity"] = Decimal(values["totalQuantity"])
            for name in ("lmtPrice", "auxPrice"):
                if name in values and type(values[name]) in (int, float, Decimal):
                    values[name] = Decimal(str(values[name]))
        expected_contract_fields = dict(pinned_contract)
        expected_contract_fields.update(_canonical_value(expected_contract))
        expected_order_values = dict(pinned_order)
        expected_order_values.update(_canonical_value(expected_order_fields))
        for actual, expected in ((actual_contract, expected_contract_fields),
                                 (actual_order, expected_order_values)):
            normalized = _canonical_value(actual)
            # JSON comparison preserves distinctions such as False versus 0
            # that Python dictionary equality would otherwise conflate.
            for name, value in normalized.items():
                if json.dumps(value, sort_keys=True) != json.dumps(expected[name], sort_keys=True):
                    raise ValueError("SDK object differs from the reviewed plan")
        # Hash the fields just verified, not another read of caller-owned mutable
        # objects. The SDK dispatch later hashes its actual objects independently.
        return sdk_order_fingerprint(
            SimpleNamespace(**actual_contract),
            SimpleNamespace(**snapshot_order),
            account_binding_fingerprint,
        )
    except Exception:
        raise BrokerMutationBlocked("IBKR_SDK_PLAN_MISMATCH") from None


# A release collector binding the session itself must include these semantics;
# inventorying only its class file misses instance-level overrides. The staged
# production transport separately refuses release acceptance until the complete
# worker/callback/read/preflight dependency graph has been reviewed.
IBKR_SDK_SEMANTIC_MEMBERS = (
    "connect", "disconnect", "observe_connection_closed", "observe_next_valid_id",
    "observe_managed_accounts", "authorize_writes", "revoke_writes", "assert_account",
    "api_host", "api_port", "command_lane_status",
    "assert_write_binding", "assert_dispatch_authorizer", "validate_mutation_interlock",
    "submit", "cancel",
    "_check_write", "_assert_payload", "_wire_send", "_encoder", "_dispatch",
    "_assert_session", "_assert_evidence", "_install_encoders", "_encoders_intact", "_deny_wire",
    "_connected", "_invalidate", "release_components",
)


@dataclass(frozen=True)
class IbkrWriteEvidence:
    """Externally reviewed receipt, never synthesized from configuration flags.

    The authorizer must authenticate its origin and scope and recheck the live
    ledger/preflight/owned-order state. Field validity alone is not authority.
    """

    authorization_binding_id: str
    account_binding_fingerprint: str
    environment: Environment
    client_id: int
    reviewed_contract_id: str
    issued_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        for name in ("authorization_binding_id", "account_binding_fingerprint", "reviewed_contract_id"):
            _receipt(getattr(self, name))
        _positive_id(self.client_id)
        if self.environment not in ("live", "paper"):
            raise ValueError("unsupported broker environment")
        issued, expires = _time(self.issued_at), _time(self.expires_at)
        if expires <= issued:
            raise ValueError("write evidence must have a positive validity interval")
        object.__setattr__(self, "issued_at", issued)
        object.__setattr__(self, "expires_at", expires)


@dataclass(frozen=True)
class IbkrCommandLaneStatus:
    """Non-authorizing proof of the current guarded command handshake."""

    command_connected: bool
    next_valid_id_received: bool
    account_authenticated: bool
    write_authority_granted: bool


@dataclass(frozen=True)
class IbkrDispatchRequest:
    operation: Operation
    order_id: int
    generation: int
    environment: Environment
    client_id: int
    account_binding_fingerprint: str
    payload_fingerprint: str | None = None


@dataclass(frozen=True)
class IbkrDispatchReceipt:
    """Local socket handoff only; no accepted, cancelled, or filled assertion."""

    operation: Operation
    order_id: int
    generation: int
    message_sent: bool


class InjectedIbkrClient(Protocol):
    def connect(self, host: str, port: int, clientId: int) -> object: ...
    def disconnect(self) -> object: ...
    def isConnected(self) -> bool: ...
    def sendMsg(self, msgId: int, msg: str) -> object: ...
    def sendMsgProtoBuf(self, msgId: int, msg: bytes) -> object: ...
    def placeOrder(self, orderId: int, contract: object, order: object) -> object: ...
    def cancelOrder(self, orderId: int, orderCancel: object) -> object: ...


DispatchAuthorizer = Callable[[IbkrDispatchRequest, IbkrWriteEvidence], None]


@dataclass
class _PendingDispatch:
    request: IbkrDispatchRequest
    thread_id: int
    order: object | None = None
    contract: object | None = None
    attempted: bool = False
    sent: bool = False
    violation: bool = False
    inside_encoder: bool = False


class IbkrSdkSession:
    """Single-account, single-client session with a one-message write gate.

    ``authorize_dispatch`` must reject cancel IDs not owned by the durable
    ledger; observing an order never authorizes it. The two callbacks are called
    once before the SDK method and again immediately before the socket sender.
    They must return ``None`` on approval or raise. A bool is not approval.

    A disconnected client exclusively owned by this session must be injected.
    Explicit connect attaches guards to both SDK encoders and the connection's
    raw sender. Only the SDK's pinned initial handshake precedes the raw guard.
    The caller owns bounded thread/process lifetime and sanitized callbacks.
    """

    def __init__(
        self, *, client: InjectedIbkrClient, sdk_version: str,
        expected_account: str, account_binding_fingerprint: str,
        environment: Environment, client_id: int,
        order_cancel_factory: Callable[[], object],
        api_host: str = "127.0.0.1",
        api_port: int | None = None,
        mutation_interlock: Callable[[], None] | None = None,
        authorize_dispatch: DispatchAuthorizer | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if sdk_version != SUPPORTED_SDK_VERSION:
            raise ValueError("unsupported official SDK version")
        if environment not in ("live", "paper"):
            raise ValueError("unsupported broker environment")
        pattern = r"U[0-9]+" if environment == "live" else r"DU[0-9]+"
        if not isinstance(expected_account, str) or not re.fullmatch(pattern, expected_account):
            raise ValueError("an exact account matching the environment is required")
        _receipt(account_binding_fingerprint)
        _positive_id(client_id)
        if api_host != "127.0.0.1":
            raise ValueError("IBKR API host must be the IPv4 loopback address")
        if api_port is None:
            api_port = 4001 if environment == "live" else 4002
        if type(api_port) is not int or not 0 < api_port <= 65_535:
            raise ValueError("IBKR API port must be an explicit TCP port")
        if not callable(order_cancel_factory) or not callable(clock):
            raise ValueError("SDK factory and clock must be callable")
        if mutation_interlock is not None and not callable(mutation_interlock):
            raise ValueError("mutation interlock must be callable")
        if authorize_dispatch is not None and not callable(authorize_dispatch):
            raise ValueError("dispatch authorizer must be callable")
        self._client = client
        self._expected_account = expected_account
        self._fingerprint = account_binding_fingerprint
        self._environment = environment
        self._client_id = client_id
        self._api_host = api_host
        self._api_port = api_port
        self._cancel_factory = order_cancel_factory
        self._interlock = mutation_interlock
        self._authorizer = authorize_dispatch
        self._clock = clock
        self._lock = RLock()
        self._generation = 0
        self._next_id: int | None = None
        self._account_verified = False
        self._evidence: IbkrWriteEvidence | None = None
        self._pending: _PendingDispatch | None = None
        self._connecting = False
        self._start_count = 0
        self._guards_installed = False
        self._connection: object | None = None
        self._legacy_guard: object | None = None
        self._protobuf_guard: object | None = None
        self._wire_guard: object | None = None

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def next_valid_id(self) -> int | None:
        with self._lock:
            return self._next_id

    def command_lane_status(self) -> IbkrCommandLaneStatus:
        """Inspect the live handshake without installing or exercising writes."""

        with self._lock:
            connected = bool(
                self._generation > 0
                and self._connected()
                and self._connection is not None
                and getattr(self._client, "conn", None) is self._connection
                and getattr(self._client, "host", None) == self._api_host
                and getattr(self._client, "port", None) == self._api_port
                and getattr(self._client, "clientId", None) == self._client_id
                and self._encoders_intact()
                and getattr(self._connection, "sendMsg", None) is self._wire_guard
            )
            return IbkrCommandLaneStatus(
                command_connected=connected,
                next_valid_id_received=bool(
                    connected and self._next_id is not None
                ),
                account_authenticated=bool(
                    connected and self._account_verified
                ),
                write_authority_granted=self._evidence is not None,
            )

    @property
    def client_id(self) -> int:
        return self._client_id

    @property
    def environment(self) -> Environment:
        return self._environment

    @property
    def api_host(self) -> str:
        return self._api_host

    @property
    def api_port(self) -> int:
        return self._api_port

    @property
    def account_binding_fingerprint(self) -> str:
        return self._fingerprint

    def assert_account(self, exact_account: str) -> None:
        if not isinstance(exact_account, str) or exact_account != self._expected_account:
            raise BrokerMutationBlocked("IBKR_ACCOUNT_BINDING_MISMATCH")

    def release_components(self) -> tuple[tuple[str, object, tuple[str, ...]], ...]:
        """Inventory actual executable leaves; this is not release acceptance.

        Callers must bind this wrapper using IBKR_SDK_SEMANTIC_MEMBERS. Closures,
        bound methods, the SDK distribution, and shared dependency identities
        still require the collector's normal validation; nothing is waived.
        """
        client_members = [
            "connect", "disconnect", "isConnected", "sendMsg", "sendMsgProtoBuf",
            "placeOrder", "cancelOrder",
        ]
        if callable(getattr(self._client, "run", None)):
            client_members.append("run")
        if callable(getattr(self._client, "install_titan_encoders", None)):
            client_members.extend(("install_titan_encoders", "titan_encoders_are"))
        leaves = (
            ("ibkr_sdk_client", self._client, tuple(client_members)),
            ("ibkr_sdk_cancel_factory", self._cancel_factory, ("__call__",)),
            ("ibkr_sdk_mutation_interlock", self._interlock, ("__call__",)),
            ("ibkr_sdk_dispatch_authorizer", self._authorizer, ("__call__",)),
            ("ibkr_sdk_clock", self._clock, ("__call__",)),
        )
        return tuple(leaf for leaf in leaves if leaf[1] is not None)

    def _invalidate(self) -> None:
        self._next_id = None
        self._account_verified = False
        self._evidence = None

    def _connected(self) -> bool:
        try:
            return self._client.isConnected() is True
        except Exception:
            # Never expose SDK exception text through a read-only readiness
            # check; the isolated event bridge owns sanitized diagnostics.
            return False

    def _install_encoders(self) -> None:
        if self._guards_installed:
            return
        legacy = self._client.sendMsg
        protobuf = self._client.sendMsgProtoBuf
        self._legacy_guard = lambda msg_id, msg: self._encoder(msg_id, msg, legacy, False)
        self._protobuf_guard = lambda msg_id, msg: self._encoder(msg_id, msg, protobuf, True)
        installer = getattr(self._client, "install_titan_encoders", None)
        if callable(installer):
            installer(self._legacy_guard, self._protobuf_guard)
        else:
            self._client.sendMsg = self._legacy_guard
            self._client.sendMsgProtoBuf = self._protobuf_guard
        self._guards_installed = True

    def _encoders_intact(self) -> bool:
        checker = getattr(self._client, "titan_encoders_are", None)
        if callable(checker):
            try:
                return checker(self._legacy_guard, self._protobuf_guard) is True
            except Exception:
                return False
        return (
            self._client.sendMsg is self._legacy_guard
            and self._client.sendMsgProtoBuf is self._protobuf_guard
        )

    def connect(self) -> int:
        """Explicit endpoint-pinned connection; no authorization survives it."""
        with self._lock:
            if self._pending is not None or self._connected():
                raise BrokerMutationBlocked("IBKR_EXISTING_CONNECTION_REFUSED")
            self._invalidate()
            self._generation += 1
            self._connection = None
            self._install_encoders()
            self._connecting, self._start_count = True, 0
            try:
                # Prevent optional authentication/capability negotiation from
                # extending this deliberately reviewed synchronous handshake.
                for name in ("connectOptions", "optCapab", "extraAuth", "asynchronous"):
                    if getattr(self._client, name, None):
                        raise BrokerMutationBlocked("IBKR_UNREVIEWED_CONNECTION_OPTIONS")
                self._client.connect(self._api_host, self._api_port, self._client_id)
                if not self._connected() or self._start_count != 1:
                    raise BrokerMutationBlocked("IBKR_CONNECTION_HANDSHAKE_INCOMPLETE")
                connection = getattr(self._client, "conn", None)
                if connection is None or not callable(getattr(connection, "sendMsg", None)):
                    raise BrokerMutationBlocked("IBKR_SOCKET_GUARD_UNAVAILABLE")
                original = connection.sendMsg
                self._wire_guard = lambda payload: self._wire_send(payload, original)
                connection.sendMsg = self._wire_guard
                self._connection = connection
                return self._generation
            except Exception:
                self._invalidate()
                try:
                    self._client.disconnect()
                except Exception:
                    pass
                raise BrokerMutationBlocked("IBKR_CONNECTION_NOT_ESTABLISHED") from None
            finally:
                self._connecting = False

    def disconnect(self) -> None:
        with self._lock:
            self._invalidate()
            self._connection = None
            try:
                self._client.disconnect()
            except Exception:
                raise BrokerMutationBlocked("IBKR_DISCONNECT_NOT_CONFIRMED") from None

    def observe_connection_closed(self, *, generation: int) -> None:
        with self._lock:
            if generation == self._generation:
                self._invalidate()
                self._connection = None

    def observe_next_valid_id(self, order_id: int, *, generation: int) -> None:
        with self._lock:
            if generation != self._generation or self._generation == 0:
                return
            try:
                order_id = _positive_id(order_id)
            except ValueError:
                self._invalidate()
                raise BrokerMutationBlocked("IBKR_INVALID_NEXT_ORDER_ID") from None
            self._next_id = max(order_id, self._next_id or order_id)

    def observe_managed_accounts(self, accounts: str | tuple[str, ...], *, generation: int) -> None:
        with self._lock:
            if generation != self._generation or self._generation == 0:
                return
            values = tuple(accounts.split(",")) if isinstance(accounts, str) else accounts
            if not isinstance(values, tuple) or values != (self._expected_account,):
                self._invalidate()
                raise BrokerMutationBlocked("IBKR_MANAGED_ACCOUNT_MISMATCH")
            self._account_verified = True

    def authorize_writes(self, evidence: IbkrWriteEvidence) -> None:
        """Install a reviewed receipt only after current-session identity proof."""
        with self._lock:
            self._evidence = None
            self._assert_session()
            self._assert_evidence(evidence)
            if self._interlock is None or self._authorizer is None:
                raise BrokerMutationBlocked("IBKR_DISPATCH_AUTHORIZER_REQUIRED")
            self._evidence = evidence

    def revoke_writes(self) -> None:
        with self._lock:
            self._evidence = None

    def assert_write_binding(self, authorization_binding_id: str, reviewed_contract_id: str) -> None:
        """Read-only check for the coordinator before its durable SENDING mark."""
        with self._lock:
            self._assert_session()
            self._assert_evidence(self._evidence)
            if (
                self._evidence.authorization_binding_id != authorization_binding_id
                or self._evidence.reviewed_contract_id != reviewed_contract_id
            ):
                raise BrokerMutationBlocked("IBKR_WRITE_RECEIPT_BINDING_MISMATCH")

    def assert_dispatch_authorizer(self, expected_callable: DispatchAuthorizer) -> None:
        """Check the actual callable identity, not merely a claimed capability."""
        with self._lock:
            if not callable(expected_callable) or self._authorizer is not expected_callable:
                raise BrokerMutationBlocked("IBKR_DISPATCH_AUTHORIZER_BINDING_MISMATCH")

    def validate_mutation_interlock(self) -> None:
        """Acquire/revalidate the release-bound interlock before claim creation.

        Dispatch performs the same check again at the wire boundary.  This
        earlier check exists solely to keep an attended review unconsumed when
        another process owns the account-global writer lock.
        """

        with self._lock:
            self._assert_session()
            self._assert_evidence(self._evidence)
            evidence = self._evidence
            if self._interlock is None:
                raise BrokerMutationBlocked("IBKR_DISPATCH_AUTHORIZER_REQUIRED")
            try:
                if self._interlock() is not None:
                    raise BrokerMutationBlocked("IBKR_INTERLOCK_MUST_NOT_BE_BOOLEAN")
            except Exception:
                raise BrokerMutationBlocked("IBKR_DISPATCH_AUTHORIZATION_DENIED") from None
            self._assert_session()
            self._assert_evidence(self._evidence)
            if self._evidence is not evidence:
                raise BrokerMutationBlocked("IBKR_WRITE_EVIDENCE_CHANGED")

    def _assert_session(self) -> None:
        if (
            not self._connected() or self._connection is None
            or getattr(self._client, "conn", None) is not self._connection
            or getattr(self._client, "host", None) != self._api_host
            or getattr(self._client, "port", None) != self._api_port
            or getattr(self._client, "clientId", None) != self._client_id
            or not self._encoders_intact()
            or getattr(self._connection, "sendMsg", None) is not self._wire_guard
            or self._next_id is None or not self._account_verified
        ):
            self._evidence = None
            raise BrokerMutationBlocked("IBKR_SESSION_NOT_READY")

    def _assert_evidence(self, evidence: IbkrWriteEvidence | None) -> None:
        if not isinstance(evidence, IbkrWriteEvidence):
            raise BrokerMutationBlocked("IBKR_REVIEWED_WRITE_EVIDENCE_REQUIRED")
        if (
            evidence.account_binding_fingerprint != self._fingerprint
            or evidence.environment != self._environment
            or evidence.client_id != self._client_id
            or not evidence.issued_at <= _time(self._clock()) < evidence.expires_at
        ):
            raise BrokerMutationBlocked("IBKR_WRITE_EVIDENCE_INVALID")

    def _check_write(self, pending: _PendingDispatch) -> None:
        self._assert_session()
        self._assert_evidence(self._evidence)
        evidence = self._evidence
        if self._interlock is None or self._authorizer is None:
            raise BrokerMutationBlocked("IBKR_DISPATCH_AUTHORIZER_REQUIRED")
        if pending.order is not None:
            self.assert_account(getattr(pending.order, "account", None))
            if getattr(pending.order, "whatIf", None) is not False:
                raise BrokerMutationBlocked("IBKR_WHAT_IF_FORBIDDEN")
            self._assert_payload(pending)
        try:
            if self._interlock() is not None:
                raise BrokerMutationBlocked("IBKR_INTERLOCK_MUST_NOT_BE_BOOLEAN")
            if self._authorizer(pending.request, self._evidence) is not None:
                raise BrokerMutationBlocked("IBKR_AUTHORIZER_MUST_NOT_BE_BOOLEAN")
        except Exception:
            raise BrokerMutationBlocked("IBKR_DISPATCH_AUTHORIZATION_DENIED") from None
        # Callbacks may synchronously revoke authority or invalidate the session.
        self._assert_session()
        self._assert_evidence(self._evidence)
        if self._evidence is not evidence:
            raise BrokerMutationBlocked("IBKR_WRITE_EVIDENCE_CHANGED")
        if pending.order is not None:
            self.assert_account(getattr(pending.order, "account", None))
            if getattr(pending.order, "whatIf", None) is not False:
                raise BrokerMutationBlocked("IBKR_WHAT_IF_FORBIDDEN")
            self._assert_payload(pending)

    def _assert_payload(self, pending: _PendingDispatch) -> None:
        if (
            getattr(pending.order, "clientId", None) != self._client_id
            or getattr(pending.order, "orderId", None) != pending.request.order_id
            or sdk_order_fingerprint(pending.contract, pending.order, self._fingerprint)
            != pending.request.payload_fingerprint
        ):
            raise BrokerMutationBlocked("IBKR_ORDER_PAYLOAD_CHANGED")

    def _deny_wire(self) -> None:
        self._evidence = None
        if self._pending is not None:
            self._pending.violation = True
        raise BrokerMutationBlocked("IBKR_OUTGOING_MESSAGE_BLOCKED")

    def _encoder(self, msg_id: int, payload: object, sender: Callable, protobuf: bool) -> object:
        with self._lock:
            if self._connecting:
                if type(msg_id) is not int or msg_id != (271 if protobuf else 71) or self._start_count:
                    self._deny_wire()
                self._start_count += 1
                return sender(msg_id, payload)
            pending = self._pending
            wanted = (3 if pending and pending.request.operation == "submit" else 4) + (200 if protobuf else 0)
            if (
                pending is None or type(msg_id) is not int or msg_id != wanted
                or pending.thread_id != get_ident() or pending.attempted
                or pending.inside_encoder or pending.violation
            ):
                self._deny_wire()
            pending.inside_encoder = True
            try:
                return sender(msg_id, payload)
            finally:
                pending.inside_encoder = False

    def _wire_send(self, payload: bytes, sender: Callable[[bytes], int]) -> int:
        with self._lock:
            pending = self._pending
            if (
                pending is None or pending.thread_id != get_ident()
                or not pending.inside_encoder or pending.attempted or pending.violation
                or not isinstance(payload, bytes) or not payload
            ):
                self._deny_wire()
            try:
                self._check_write(pending)
            except Exception:
                pending.violation = True
                raise BrokerMutationBlocked("IBKR_WIRE_AUTHORIZATION_DENIED") from None
            pending.attempted = True
            sent = sender(payload)
            if type(sent) is not int or sent != len(payload):
                raise BrokerUnknownSubmission("IBKR_SOCKET_HANDOFF_INCOMPLETE")
            pending.sent = True
            return sent

    def _dispatch(self, operation: Operation, order_id: int, contract: object = None, order: object = None) -> IbkrDispatchReceipt:
        order_id = _positive_id(order_id)
        with self._lock:
            if self._pending is not None:
                raise BrokerMutationBlocked("IBKR_REENTRANT_DISPATCH_FORBIDDEN")
            payload_fingerprint = sdk_order_fingerprint(contract, order, self._fingerprint) if operation == "submit" else None
            pending = _PendingDispatch(IbkrDispatchRequest(operation, order_id, self._generation, self._environment, self._client_id, self._fingerprint, payload_fingerprint), get_ident(), order, contract)
            self._pending = pending
            try:
                self._check_write(pending)
                if operation == "submit" and order_id < self._next_id:
                    raise BrokerMutationBlocked("IBKR_REUSED_ORDER_ID_FORBIDDEN")
                if operation == "submit":
                    # Burn IDs even for local SDK failure; the durable ledger
                    # owns retry/reconciliation, not this socket boundary.
                    self._next_id = order_id + 1
                    self._client.placeOrder(order_id, contract, order)
                else:
                    self._client.cancelOrder(order_id, self._cancel_factory())
                if pending.violation or not pending.sent:
                    raise BrokerMutationBlocked("IBKR_SDK_DID_NOT_DISPATCH")
                return IbkrDispatchReceipt(operation, order_id, self._generation, True)
            except Exception:
                if pending.attempted:
                    raise BrokerUnknownSubmission("IBKR_DISPATCH_REQUIRES_RECONCILIATION") from None
                raise BrokerMutationBlocked("IBKR_DISPATCH_NOT_SENT") from None
            finally:
                self._pending = None

    def submit(self, order_id: int, contract: object, order: object) -> IbkrDispatchReceipt:
        return self._dispatch("submit", order_id, contract, order)

    def cancel(self, order_id: int) -> IbkrDispatchReceipt:
        return self._dispatch("cancel", order_id)
