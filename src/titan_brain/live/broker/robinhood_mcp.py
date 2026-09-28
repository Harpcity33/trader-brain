"""Read-only normalization for Robinhood's documented Agentic MCP tools.

The caller owns a separately supported MCP session and its authentication. This
module never locates tokens, registers OAuth clients, or dispatches mutations.
Endpoint pagination receipts are deliberately not ProductionTransport snapshots:
advanced orders, client references, and cross-request atomicity are unavailable.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import inspect
import json
import re
import threading
import time
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import parse_qs, urlsplit

from ..money import finite_decimal
from ..models import BrokerOrderState
from .base import (
    BrokerAuthenticationError, BrokerCapabilityError, BrokerContractViolation,
    BrokerSide, EquityOrderType, FillSnapshot, MarketHours, OrderSnapshot, TimeInForce,
)


ENDPOINT = "https://agent.robinhood.com/mcp/trading"
READ_TOOLS = frozenset({
    "get_accounts", "get_portfolio", "get_equity_positions",
    "get_equity_orders", "get_equity_tradability",
})
UNSUPPORTED_CAPABILITIES = (
    "order_review", "order_placement", "order_cancellation", "order_replacement",
    "advanced_order_coverage", "option_position_coverage", "option_order_coverage",
    "client_reference_lookup", "atomic_account_snapshot", "provider_snapshot_token",
    "provider_observation_timestamp", "daily_weekly_pnl", "peak_equity",
    "authentication_refresh", "unattended_execution", "atomic_protection",
)


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BrokerContractViolation(f"{name} must be a nonempty string")
    return value.strip()


def _bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise BrokerContractViolation(f"{name} must be boolean")
    return value


def _decimal(value: Any, name: str, *, nonnegative: bool = False) -> Decimal:
    try:
        parsed = finite_decimal(value, field=name)
    except ValueError as exc:
        raise BrokerContractViolation(f"{name} is not a finite decimal") from exc
    if nonnegative and parsed < 0:
        raise BrokerContractViolation(f"{name} must be nonnegative")
    return parsed


def _optional_decimal(value: Any, name: str) -> Decimal | None:
    return None if value is None else _decimal(value, name)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise BrokerContractViolation("receipt/readiness clock must be timezone-aware")
    return value.astimezone(timezone.utc)


def _timestamp(value: Any, name: str) -> datetime:
    try:
        return _utc(datetime.fromisoformat(_text(value, name).replace("Z", "+00:00")))
    except (ValueError, TypeError) as exc:
        raise BrokerContractViolation(f"invalid {name}") from exc


def _object(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise BrokerContractViolation(f"{name} must be an object")
    return value


def _rows(value: Any, name: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(row, Mapping) for row in value):
        raise BrokerContractViolation(f"{name} must be an explicit list of objects; null is unknown")
    return value


def _symbol(value: Any) -> str:
    symbol = _text(value, "symbol").upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", symbol):
        raise BrokerContractViolation("invalid equity symbol")
    return symbol


@dataclass(frozen=True)
class McpReadiness:
    """Fresh status supplied by the supported session owner, never inferred here."""

    caller_id: str
    endpoint: str
    authenticated: bool
    observed_at: datetime


class SupportedReadCaller(Protocol):
    """Synchronous bridge to an already authorized native MCP session.

    call_tool receives a short MCP wire name and arguments. It returns the
    native result's model_dump(mode="json") or equivalent mapping. readiness()
    must report the actual session state, not a configured credential's presence.
    The caller must enforce its own transport deadline. The read client's retry
    budget bounds throttle waits, not time spent inside this synchronous call.
    """

    def readiness(self) -> McpReadiness: ...
    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class ReadReceipt:
    tool: str
    caller_id: str
    request_started_at: datetime
    request_completed_at: datetime
    attempts: int
    payload_sha256: str
    payload: Mapping[str, Any] = field(repr=False)


@dataclass(frozen=True)
class PagedEvidence:
    records: tuple[Mapping[str, Any], ...] = field(repr=False)
    receipts: tuple[ReadReceipt, ...]
    all_pages_consumed: bool = True
    # A sequential endpoint walk is not an immutable whole-account snapshot.
    atomic: bool = False


@dataclass(frozen=True)
class AccountEvidence:
    account_number: str = field(repr=False)
    account_masked: str
    account_type: str
    brokerage_account_type: str
    state: str
    agentic_allowed: bool
    deactivated: bool
    permanently_deactivated: bool
    unsettled_funds: Decimal | None

    @property
    def entry_account_eligible(self) -> bool:
        return self.agentic_allowed and self.state == "active" and not (
            self.deactivated or self.permanently_deactivated
        )


@dataclass(frozen=True)
class PortfolioEvidence:
    total_value: Decimal
    cash: Decimal
    currency: str
    buying_power: Decimal | None
    unleveraged_buying_power: Decimal | None
    asset_values: Mapping[str, Decimal | None]


@dataclass(frozen=True)
class PositionEvidence:
    symbol: str
    quantity: Decimal
    intraday_quantity: Decimal
    sellable_quantity: Decimal
    average_price: Decimal | None
    position_type: str
    holds: Mapping[str, Decimal]


@dataclass(frozen=True)
class EquityOrderEvidence:
    broker_order_id: str
    symbol: str
    side: str
    order_type: str
    raw_state: str
    requested_quantity: Decimal | None
    dollar_amount: Decimal | None
    cumulative_filled_quantity: Decimal
    average_price: Decimal | None
    limit_price: Decimal | None
    stop_price: Decimal | None
    market_hours: str
    time_in_force: str
    created_at: datetime
    last_transaction_at: datetime | None
    received_at: datetime
    fills: tuple[FillSnapshot, ...] | None
    fees: Decimal
    placed_agent: str
    _bound_account_number: str = field(repr=False)

    @property
    def account_masked(self) -> str:
        """Identity derived from the explicitly scoped original broker request."""
        return "••••" + self._bound_account_number[-4:]

    def to_order_snapshot(self, account_masked: str) -> OrderSnapshot:
        """Convert only when the stronger runtime record is fully representable."""
        if account_masked != self.account_masked:
            raise BrokerContractViolation("order cannot be relabeled to a different account")
        if self.requested_quantity is None:
            raise BrokerCapabilityError("dollar-sized order has no requested share quantity")
        if self.fills is None and self.cumulative_filled_quantity != 0:
            raise BrokerCapabilityError("cumulative fill is known but execution detail is unavailable")
        state = "PENDING" if self.raw_state == "new" else self.raw_state.upper()
        try:
            return OrderSnapshot(
                broker_order_id=self.broker_order_id, account_masked=self.account_masked,
                symbol=self.symbol, side=BrokerSide(self.side),
                order_type=EquityOrderType(self.order_type), state=BrokerOrderState(state),
                requested_quantity=self.requested_quantity,
                cumulative_filled_quantity=self.cumulative_filled_quantity,
                market_hours=MarketHours(self.market_hours), time_in_force=TimeInForce(self.time_in_force),
                broker_updated_at=self.last_transaction_at or self.created_at,
                received_at=self.received_at, limit_price=self.limit_price, stop_price=self.stop_price,
                client_ref_id=None, fills=self.fills or (),
            )
        except ValueError as exc:
            raise BrokerContractViolation(f"order cannot satisfy runtime snapshot: {exc}") from exc


@dataclass(frozen=True)
class TradabilityEvidence:
    symbol: str
    found: bool
    tradeable: bool | None
    state: str | None
    account_type_tradability: str | None
    regular_entry_eligible: bool | None
    extended_entry_eligible: bool | None
    fractional_eligible: bool | None
    halt_sessions: tuple[str, ...] | None


@dataclass(frozen=True)
class RobinhoodReadObservation:
    account: AccountEvidence
    portfolio: PortfolioEvidence
    positions: tuple[PositionEvidence, ...]
    orders: tuple[EquityOrderEvidence, ...]
    tradability: tuple[TradabilityEvidence, ...]
    receipts: tuple[ReadReceipt, ...]
    request_started_at: datetime
    request_completed_at: datetime
    collection_id: str
    unsupported_capabilities: tuple[str, ...] = UNSUPPORTED_CAPABILITIES
    atomic: bool = False
    provider_observed_at: datetime | None = None


class McpReadError(BrokerContractViolation):
    """Preserves the broker error and envelope without reporting an empty success."""

    def __init__(self, tool: str, message: str, raw: Any):
        super().__init__(f"{tool}: {message}")
        self.tool = tool
        self.broker_message = message
        self.raw = deepcopy(raw)


def _error_message(raw: Mapping[str, Any]) -> str | None:
    if raw.get("isError") is True or raw.get("is_error") is True or raw.get("status") == "error" or raw.get("error"):
        content = raw.get("content")
        parts = [item.get("text") for item in (content if isinstance(content, list) else [])
                 if isinstance(item, Mapping) and isinstance(item.get("text"), str)]
        return "\n".join(parts) or str(raw.get("error") or raw.get("message") or "MCP error")
    return None


def _payload(tool: str, raw: Any) -> Mapping[str, Any]:
    original = _object(raw, "MCP result")
    error = _error_message(original)
    if error:
        raise McpReadError(tool, error, original)
    if "structuredContent" in original and "structured_content" in original and original["structuredContent"] != original["structured_content"]:
        raise BrokerContractViolation("conflicting structured-content aliases")
    structured = original.get("structuredContent", original.get("structured_content"))
    if structured is not None:
        body = _object(structured, "structuredContent")
    elif "data" in original:
        body = original
    else:
        content = original.get("content")
        texts = [item.get("text") for item in (content if isinstance(content, list) else [])
                 if isinstance(item, Mapping) and item.get("type") == "text"]
        candidates = []
        for text in texts:
            try:
                candidate = json.loads(text, parse_float=Decimal)
            except (ValueError, TypeError):
                continue
            if isinstance(candidate, Mapping):
                candidates.append(candidate)
        if len(candidates) != 1:
            raise BrokerContractViolation("MCP result needs one unambiguous JSON object")
        body = candidates[0]
    error = _error_message(body)
    if error:
        raise McpReadError(tool, error, original)
    data = _object(body.get("data"), "tool data")
    error = _error_message(data)
    if error:
        raise McpReadError(tool, error, original)
    return deepcopy(data)


def _next_cursor(data: Mapping[str, Any]) -> str | None:
    value = data.get("next")
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise BrokerContractViolation("pagination next must be a URL or empty")
    try:
        parsed = urlsplit(value)
        query = parse_qs(parsed.query, keep_blank_values=True)
    except ValueError as exc:
        raise BrokerContractViolation("invalid pagination URL") from exc
    # Never fetch a server-supplied URL: only its opaque cursor is forwarded.
    cursors = query.get("cursor", [])
    if len(cursors) != 1:
        raise BrokerContractViolation("next URL must contain exactly one cursor")
    return cursors[0]


class RobinhoodMcpReadClient:
    """A real read client with no mutation dispatch path or credential access."""

    def __init__(
        self, caller: SupportedReadCaller, *, account_number: str,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        min_interval_seconds: float = 1.1, max_attempts: int = 3,
        max_retry_wait_seconds: float = 35, max_pages: int = 100,
        readiness_max_age_seconds: float = 60,
    ):
        self._caller = caller
        self._account_number = _text(account_number, "explicit account_number")
        if len(self._account_number) < 4 or not self._account_number[-4:].isdigit():
            raise BrokerContractViolation("account number requires four terminal digits")
        for name, value in (("min_interval_seconds", min_interval_seconds),
                            ("max_retry_wait_seconds", max_retry_wait_seconds),
                            ("readiness_max_age_seconds", readiness_max_age_seconds)):
            _decimal(value, name, nonnegative=True)
        for name, value in (("max_attempts", max_attempts), ("max_pages", max_pages)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        self._clock, self._monotonic, self._sleep = clock, monotonic, sleep
        self._interval, self._max_attempts = float(min_interval_seconds), max_attempts
        self._retry_budget, self._max_pages = float(max_retry_wait_seconds), max_pages
        self._readiness_age = timedelta(seconds=float(readiness_max_age_seconds))
        self._last_call: float | None = None
        self._bound_caller_id: str | None = None
        self._lock = threading.Lock()

    @property
    def unsupported_capabilities(self) -> tuple[str, ...]:
        return UNSUPPORTED_CAPABILITIES

    def _read(self, tool: str, arguments: Mapping[str, Any]) -> ReadReceipt:
        if tool not in READ_TOOLS:
            raise BrokerCapabilityError("read client forbids this tool")
        if "account_number" in arguments and arguments["account_number"] != self._account_number:
            raise BrokerContractViolation("request account does not match explicit binding")
        with self._lock:
            started, waited = _utc(self._clock()), 0.0
            for attempt in range(1, self._max_attempts + 1):
                if self._last_call is not None:
                    delay = self._interval - (self._monotonic() - self._last_call)
                    if delay > 0:
                        self._sleep(delay)
                status = self._caller.readiness()
                now = _utc(self._clock())
                if not isinstance(status, McpReadiness) or status.authenticated is not True:
                    raise BrokerAuthenticationError("supported MCP caller has no authenticated session")
                if status.endpoint != ENDPOINT:
                    raise BrokerAuthenticationError("MCP caller endpoint does not match Robinhood")
                age = now - _utc(status.observed_at)
                if age < timedelta(0) or age > self._readiness_age:
                    raise BrokerAuthenticationError("MCP session readiness is stale or future-dated")
                caller_id = _text(status.caller_id, "caller_id")
                if self._bound_caller_id is None:
                    self._bound_caller_id = caller_id
                elif caller_id != self._bound_caller_id:
                    raise BrokerAuthenticationError("MCP caller identity changed during client lifetime")
                self._last_call = self._monotonic()
                # Transport errors propagate; no credentials or success-shaped defaults.
                raw = self._caller.call_tool(tool, dict(arguments))
                if inspect.isawaitable(raw):
                    if inspect.iscoroutine(raw):
                        raw.close()
                    raise BrokerContractViolation("caller must synchronously bridge its MCP session")
                try:
                    data = _payload(tool, raw)
                except McpReadError as exc:
                    identified = re.search(r"(?:error|status|code|http)\D{0,3}429\b|throttl", exc.broker_message, re.I)
                    if not identified or attempt == self._max_attempts:
                        raise
                    countdown = re.search(r"available in\s+(\d+(?:\.\d+)?)\s*second", exc.broker_message, re.I)
                    delay = float(countdown.group(1)) + 1 if countdown else 30.0
                    if waited + delay > self._retry_budget:
                        raise
                    self._sleep(delay)
                    waited += delay
                    continue
                completed = _utc(self._clock())
                if completed < started:
                    raise BrokerContractViolation("read clock moved backward")
                digest = hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()
                return ReadReceipt(tool, caller_id, started, completed, attempt, digest, data)
        raise AssertionError("unreachable")

    def _pages(self, tool: str, key: str) -> PagedEvidence:
        records, receipts, cursors, identities = [], [], set(), set()
        arguments: dict[str, Any] = {"account_number": self._account_number}
        for _ in range(self._max_pages):
            receipt = self._read(tool, arguments)
            receipts.append(receipt)
            for row in _rows(receipt.payload.get(key), key):
                identity = _text(row.get("id" if key == "orders" else "symbol"), "record identity")
                if identity in identities:
                    raise BrokerContractViolation("duplicate record across non-atomic pages; recollect")
                identities.add(identity)
                records.append(deepcopy(row))
            cursor = _next_cursor(receipt.payload)
            if cursor is None:
                return PagedEvidence(tuple(records), tuple(receipts))
            if cursor in cursors:
                raise BrokerContractViolation("pagination cursor repeated")
            cursors.add(cursor)
            arguments["cursor"] = cursor
        raise BrokerContractViolation("pagination exceeded page budget; collection is incomplete")

    def get_account(self) -> tuple[AccountEvidence, ReadReceipt]:
        receipt = self._read("get_accounts", {})
        rows = _rows(receipt.payload.get("accounts"), "accounts")
        matches = [row for row in rows if row.get("account_number") == self._account_number]
        if len(matches) != 1:
            raise BrokerContractViolation("explicit account is missing or duplicated in broker accounts")
        row = matches[0]
        account = AccountEvidence(
            self._account_number, "••••" + self._account_number[-4:],
            _text(row.get("type"), "account type"),
            _text(row.get("brokerage_account_type"), "brokerage account type"),
            _text(row.get("state"), "account state"),
            _bool(row.get("agentic_allowed"), "agentic_allowed"),
            _bool(row.get("deactivated"), "deactivated"),
            _bool(row.get("permanently_deactivated"), "permanently_deactivated"),
            _optional_decimal(row.get("unsettled_funds"), "unsettled_funds"),
        )
        return account, receipt

    def get_portfolio(self) -> tuple[PortfolioEvidence, ReadReceipt]:
        receipt = self._read("get_portfolio", {"account_number": self._account_number})
        row = receipt.payload
        buying = row.get("buying_power")
        currency = _text(row.get("currency"), "currency")
        if buying is not None:
            buying = _object(buying, "buying_power")
            if buying.get("display_currency") != currency:
                raise BrokerContractViolation("buying-power currency differs from portfolio currency")
        portfolio = PortfolioEvidence(
            _decimal(row.get("total_value"), "total_value"), _decimal(row.get("cash"), "cash"),
            currency,
            None if buying is None else _decimal(buying.get("buying_power"), "buying_power"),
            None if buying is None else _decimal(buying.get("unleveraged_buying_power"), "unleveraged_buying_power"),
            {key: _optional_decimal(row.get(key), key) for key in (
                "equity_value", "options_value", "crypto_value", "event_contracts_value",
                "fixed_income_value", "futures_value", "mutual_funds_value", "pending_deposits",
            )},
        )
        return portfolio, receipt

    def get_positions(self) -> tuple[tuple[PositionEvidence, ...], PagedEvidence]:
        pages = self._pages("get_equity_positions", "positions")
        positions = []
        for row in pages.records:
            positions.append(PositionEvidence(
                _symbol(row.get("symbol")), _decimal(row.get("quantity"), "quantity"),
                _decimal(row.get("intraday_quantity"), "intraday_quantity"),
                _decimal(row.get("shares_available_for_sells"), "shares_available_for_sells", nonnegative=True),
                _optional_decimal(row.get("average_buy_price"), "average_buy_price"),
                _text(row.get("type"), "position type"),
                {key: _decimal(row.get(key), key, nonnegative=True) for key in (
                    "shares_held_for_sells", "shares_held_for_asset_transfer", "shares_held_for_options_events",
                    "shares_held_for_stock_grants", "shares_pending_from_options_events",
                )},
            ))
        return tuple(positions), pages

    def get_orders(self) -> tuple[tuple[EquityOrderEvidence, ...], PagedEvidence]:
        pages = self._pages("get_equity_orders", "orders")
        orders = tuple(_order(row, receipt.request_completed_at, account_number=self._account_number)
                       for receipt in pages.receipts for row in _rows(receipt.payload.get("orders"), "orders"))
        return orders, pages

    def get_tradability(self, symbols: tuple[str, ...], *, account_type: str) -> tuple[tuple[TradabilityEvidence, ...], tuple[ReadReceipt, ...]]:
        normalized = tuple(dict.fromkeys(_symbol(value) for value in symbols))
        records, receipts = {}, []
        for offset in range(0, len(normalized), 10):
            batch = normalized[offset:offset + 10]
            receipt = self._read("get_equity_tradability", {"account_number": self._account_number, "symbols": list(batch)})
            receipts.append(receipt)
            missing = receipt.payload.get("not_found") or []
            if not isinstance(missing, list) or any(not isinstance(x, str) for x in missing):
                raise BrokerContractViolation("not_found must be a symbol list")
            for symbol in missing:
                if symbol not in batch or symbol in records:
                    raise BrokerContractViolation("duplicate/unrequested tradability symbol")
                records[symbol] = TradabilityEvidence(symbol, False, None, None, None, False, False, False, None)
            for row in _rows(receipt.payload.get("results"), "tradability results"):
                symbol = _symbol(row.get("symbol"))
                if symbol not in batch or symbol in records:
                    raise BrokerContractViolation("duplicate/unrequested tradability symbol")
                tradeable = _bool(row.get("tradeable"), "tradeable")
                types = row.get("account_type_tradabilities")
                matches = [] if types is None else [item for item in _rows(types, "account_type_tradabilities") if item.get("account_type") == account_type]
                if len(matches) > 1:
                    raise BrokerContractViolation("duplicate account-type tradability")
                eligibility = matches[0].get("account_type_tradability") if matches else None
                state = row.get("state")
                base = False if not tradeable or state == "inactive" or eligibility in {"untradable", "position_closing_only"} else (True if state == "active" and eligibility == "tradable" else None)
                regular = base
                halt = row.get("internal_halt_sessions")
                if halt is not None and (not isinstance(halt, list) or any(not isinstance(x, str) for x in halt)):
                    raise BrokerContractViolation("halt sessions must be a list or null")
                if halt and "regular_hours" in halt:
                    regular = False
                all_day = row.get("all_day_tradability")
                extended = False if base is False or (all_day is not None and all_day != "all_day_tradability_tradable") else (base if all_day == "all_day_tradability_tradable" else None)
                if halt and set(halt) & {"extended_hours", "all_day_hours"}:
                    extended = False
                fractional = None if row.get("fractional_tradability") is None else row["fractional_tradability"] == "tradable"
                records[symbol] = TradabilityEvidence(symbol, True, tradeable, state, eligibility, regular, extended, fractional, None if halt is None else tuple(halt))
            if any(symbol not in records for symbol in batch):
                raise BrokerContractViolation("broker omitted requested tradability symbols")
        return tuple(records[symbol] for symbol in normalized), tuple(receipts)

    def collect(self, *, symbols: tuple[str, ...] = ()) -> RobinhoodReadObservation:
        account, account_receipt = self.get_account()
        portfolio, portfolio_receipt = self.get_portfolio()
        positions, position_pages = self.get_positions()
        orders, order_pages = self.get_orders()
        tradability, eligibility_receipts = self.get_tradability(symbols, account_type=account.brokerage_account_type)
        receipts = (account_receipt, portfolio_receipt, *position_pages.receipts, *order_pages.receipts, *eligibility_receipts)
        if len({receipt.caller_id for receipt in receipts}) != 1:
            raise BrokerAuthenticationError("collection cannot combine different MCP caller identities")
        if any(right.request_started_at < left.request_completed_at for left, right in zip(receipts, receipts[1:])):
            raise BrokerContractViolation("collection clock moved backward between requests")
        started, completed = receipts[0].request_started_at, receipts[-1].request_completed_at
        material = "|".join(f"{r.tool}:{r.payload_sha256}:{r.request_started_at.isoformat()}:{r.request_completed_at.isoformat()}" for r in receipts)
        collection_id = hashlib.sha256(material.encode()).hexdigest()
        return RobinhoodReadObservation(account, portfolio, positions, orders, tradability, receipts, started, completed, collection_id)


def _order(row: Mapping[str, Any], received_at: datetime, *, account_number: str) -> EquityOrderEvidence:
    # The current output contract scopes ownership by the request, not a row
    # field. If a future result does echo an account, it must agree exactly.
    if "account_number" in row and row["account_number"] != account_number:
        raise BrokerContractViolation("order row account conflicts with the bound request")
    kind = {("market", "immediate"): "market", ("limit", "immediate"): "limit",
            ("market", "stop"): "stop_market", ("limit", "stop"): "stop_limit"}.get((row.get("type"), row.get("trigger")))
    if kind is None:
        raise BrokerContractViolation("unknown equity type/trigger combination")
    fills_raw = row.get("executions")
    fills = None if fills_raw is None else tuple(FillSnapshot(
        _text(item.get("id"), "fill id"), _decimal(item.get("quantity"), "fill quantity"),
        _decimal(item.get("price"), "fill price"), _timestamp(item.get("timestamp"), "fill timestamp"),
        _decimal(item.get("fees"), "fill fees", nonnegative=True),
    ) for item in _rows(fills_raw, "executions"))
    if fills is not None and len({fill.fill_id for fill in fills}) != len(fills):
        raise BrokerContractViolation("duplicate execution ID")
    dollar = row.get("dollar_based_amount")
    if dollar is not None:
        dollar = _object(dollar, "dollar_based_amount")
        if dollar.get("currency_code") != "USD":
            raise BrokerContractViolation("unsupported dollar-order currency")
    result = EquityOrderEvidence(
        _text(row.get("id"), "order id"), _symbol(row.get("symbol")),
        _text(row.get("side"), "side"), kind, _text(row.get("state"), "order state"),
        _optional_decimal(row.get("quantity"), "quantity"),
        None if dollar is None else _decimal(dollar.get("amount"), "dollar amount", nonnegative=True),
        _decimal(row.get("cumulative_quantity"), "cumulative_quantity", nonnegative=True),
        _optional_decimal(row.get("average_price"), "average_price"),
        _optional_decimal(row.get("price"), "price"), _optional_decimal(row.get("stop_price"), "stop_price"),
        _text(row.get("market_hours"), "market_hours"), _text(row.get("time_in_force"), "time_in_force"),
        _timestamp(row.get("created_at"), "created_at"),
        None if row.get("last_transaction_at") is None else _timestamp(row["last_transaction_at"], "last_transaction_at"),
        received_at, fills, _decimal(row.get("fees"), "fees", nonnegative=True),
        _text(row.get("placed_agent"), "placed_agent"),
        account_number,
    )
    if result.requested_quantity is not None and (result.requested_quantity <= 0 or result.cumulative_filled_quantity > result.requested_quantity):
        raise BrokerContractViolation("invalid requested/cumulative order quantity")
    if fills is not None and sum((fill.quantity for fill in fills), Decimal(0)) != result.cumulative_filled_quantity:
        raise BrokerContractViolation("executions disagree with cumulative quantity")
    if result.created_at > received_at or (result.last_transaction_at is not None and result.last_transaction_at > received_at) or any(fill.executed_at > received_at for fill in fills or ()):
        raise BrokerContractViolation("broker order facts are future-dated")
    return result
