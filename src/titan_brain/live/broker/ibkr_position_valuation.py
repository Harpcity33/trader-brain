"""Real, read-only TWS position-value collection; never order authority.

``pnlSingle`` supplies quantity and market value, but no provider valuation
timestamp or common NetLiquidation epoch. Receipt time is kept separately and
is never promoted to either fact. Even exact arithmetic reconciliation is a
diagnostic, not proof that remaining stop/fee risk may authorize an entry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re
from threading import Condition, RLock
import time

from .base import AccountSnapshot


class PositionValuationError(RuntimeError):
    """Constant redacted diagnostic failure, never provider text."""


def _error(reason: str) -> PositionValuationError:
    return PositionValuationError("IBKR_POSITION_VALUATION_" + reason)


def _decimal(value: object, *, positive: bool = False) -> Decimal:
    # Official SDK callbacks use floats, including a DBL_MAX unset sentinel.
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise _error("NUMBER_INVALID")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise _error("NUMBER_INVALID") from None
    if not result.is_finite() or abs(result) >= Decimal("1e100") or result < 0 or (positive and result == 0):
        raise _error("NUMBER_INVALID")
    return result


@dataclass(frozen=True)
class PositionValuationContract:
    con_id: int
    symbol: str
    currency: str
    quantity: Decimal
    positions_received_at: datetime

    def __post_init__(self):
        if (
            type(self.con_id) is not int or self.con_id <= 0
            or type(self.symbol) is not str or re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", self.symbol) is None
            or self.currency != "USD"
            or not isinstance(self.positions_received_at, datetime) or self.positions_received_at.tzinfo is None
        ):
            raise _error("CONTRACT_SCOPE_UNPROVEN")
        shares = _decimal(self.quantity, positive=True)
        if shares != shares.to_integral_value():
            raise _error("WHOLE_SHARE_SCOPE_UNPROVEN")
        object.__setattr__(self, "quantity", shares)
        object.__setattr__(self, "positions_received_at", self.positions_received_at.astimezone(timezone.utc))

    @classmethod
    def from_callback(cls, contract, quantity, received_at):
        con_id = getattr(contract, "conId", None)
        symbol = getattr(contract, "symbol", None)
        if (
            type(con_id) is not int or con_id <= 0
            or type(symbol) is not str or re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", symbol) is None
            or getattr(contract, "secType", None) != "STK"
            or getattr(contract, "currency", None) != "USD"
            or not isinstance(received_at, datetime) or received_at.tzinfo is None
        ):
            raise _error("CONTRACT_SCOPE_UNPROVEN")
        shares = _decimal(quantity, positive=True)
        if shares != shares.to_integral_value():
            raise _error("WHOLE_SHARE_SCOPE_UNPROVEN")
        return cls(con_id, symbol, "USD", shares, received_at.astimezone(timezone.utc))


@dataclass(frozen=True)
class PositionValuationSample:
    contract: PositionValuationContract
    quantity: Decimal
    market_value: Decimal
    received_at: datetime
    # TWS pnlSingle has no such field. Never initialize it from our clock.
    provider_observed_at: None = None


@dataclass(frozen=True)
class PositionValuationReport:
    account_masked: str
    collection_id: str
    started_at: datetime
    completed_at: datetime
    samples: tuple[PositionValuationSample, ...]
    failures: tuple[str, ...]
    cash_plus_positions_matches_nlv: bool

    @property
    def remaining_risk_authorized(self) -> bool:
        return False

    def public_dict(self):
        return {
            "account_masked": self.account_masked,
            "collection_id": self.collection_id,
            "source": "ibkr:reqPnLSingle.value",
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "position_count": len(self.samples),
            "cash_plus_positions_matches_nlv": self.cash_plus_positions_matches_nlv,
            "source_valuation_timestamp_available": False,
            "coherent_nlv_epoch_proven": False,
            "remaining_risk_authorized": False,
            "write_authority_granted": False,
            "failures": list(self.failures),
        }


@dataclass
class _Pending:
    generation: int
    requests: dict[int, PositionValuationContract]
    samples: dict[int, PositionValuationSample] = field(default_factory=dict)
    error: PositionValuationError | None = None


class _Callbacks:
    def __init__(self, owner, generation):
        self.owner, self.generation = owner, generation

    def connectAck(self):
        return None

    def managedAccounts(self, accounts):
        with self.owner._condition:
            if self.generation == self.owner._generation:
                self.owner._authenticated = (
                    type(accounts) is str and self.owner._account in accounts.split(",")
                )
                self.owner._condition.notify_all()

    def pnlSingle(self, reqId, pos, dailyPnL, unrealizedPnL, realizedPnL, value):
        # P&L fields have different reset semantics and cannot prove day-start.
        del dailyPnL, unrealizedPnL, realizedPnL
        self.owner._sample(self.generation, reqId, pos, value)

    def error(self, reqId, *arguments):
        # Runtime router has already stripped message text; support both SDK
        # signatures for direct hermetic collection without retaining payloads.
        from .ibkr_read import classify_ibkr_error_callback
        code, _reason = classify_ibkr_error_callback(arguments)
        if code in {1101, 1102, 2104, 2106, 2107, 2108, 2158}:
            return
        self.owner._callback_error(self.generation, reqId)

    def connectionClosed(self):
        with self.owner._condition:
            if self.generation == self.owner._generation:
                self.owner._authenticated = False
        self.owner._callback_error(self.generation, -1)


class IbkrPositionValuationCollector:
    """Optional diagnostic on the existing authenticated read connection.

    No subscription starts during construction, runtime bootstrap, or normal
    account reads. Each explicit collect cancels every requested subscription.
    """

    def __init__(self, *, requester, exact_account_id, account_masked, clock, timeout_seconds=5):
        if type(exact_account_id) is not str or re.fullmatch(r"U[0-9]+", exact_account_id) is None:
            raise _error("ACCOUNT_INVALID")
        if account_masked not in {"****" + exact_account_id[-4:], "••••" + exact_account_id[-4:]}:
            raise _error("ACCOUNT_INVALID")
        if isinstance(timeout_seconds, bool) or not 0 < float(timeout_seconds) <= 5:
            raise _error("TIMEOUT_INVALID")
        self._requester, self._account, self._mask = requester, exact_account_id, account_masked
        self._clock, self._timeout = clock, float(timeout_seconds)
        self._condition = Condition(RLock())
        self._generation, self._next_id = 0, 3_000_000
        self._authenticated, self._active = False, None

    def _now(self):
        stamp = self._clock()
        if not isinstance(stamp, datetime) or stamp.tzinfo is None:
            raise _error("CLOCK_INVALID")
        return stamp.astimezone(timezone.utc)

    def open_generation(self, generation):
        with self._condition:
            if type(generation) is not int or generation <= self._generation:
                raise _error("GENERATION_INVALID")
            if self._active is not None:
                self._active.error = _error("GENERATION_LOST")
            self._generation, self._authenticated = generation, False
            self._condition.notify_all()
        return _Callbacks(self, generation)

    def _callback_error(self, generation, request_id):
        with self._condition:
            active = self._active
            if type(request_id) is int and active and generation == active.generation and (request_id == -1 or request_id in active.requests):
                active.error = _error("CALLBACK_FAILED")
                self._condition.notify_all()

    def _sample(self, generation, request_id, quantity, value):
        with self._condition:
            active = self._active
            if type(request_id) is not int or active is None or generation != self._generation or generation != active.generation or request_id not in active.requests:
                return
            contract = active.requests[request_id]
            try:
                shares, market_value = _decimal(quantity, positive=True), _decimal(value)
                if shares != contract.quantity:
                    raise _error("POSITION_QUANTITY_CHANGED")
                active.samples[request_id] = PositionValuationSample(contract, shares, market_value, self._now())
            except PositionValuationError as exc:
                active.error = exc
            self._condition.notify_all()

    def collect(self, *, contracts, snapshot: AccountSnapshot, collection_id: str):
        started = self._now()
        contracts = tuple(contracts)
        if (
            not isinstance(snapshot, AccountSnapshot) or snapshot.account_masked != self._mask
            or not snapshot.auth_point_in_time
            or any(not isinstance(item, PositionValuationContract) for item in contracts)
            or len({item.con_id for item in contracts}) != len(contracts)
            or len({item.symbol for item in contracts}) != len(contracts)
            or len([item for item in snapshot.equity_positions if item.quantity]) != len(contracts)
            or {(item.symbol, item.quantity) for item in contracts}
            != {(item.symbol, item.quantity) for item in snapshot.equity_positions if item.quantity}
            or snapshot.option_position_count or snapshot.funds.currency != "USD"
            or type(collection_id) is not str or re.fullmatch(r"[0-9a-f]{64}", collection_id) is None
        ):
            raise _error("ACCOUNT_POSITION_SCOPE_UNPROVEN")
        if any(not 0 <= (started - stamp).total_seconds() <= 5 for stamp in (
            snapshot.observed_at, snapshot.received_at, *(item.positions_received_at for item in contracts)
        )):
            raise _error("INPUT_RECEIPT_STALE")
        with self._condition:
            if not self._authenticated or self._active is not None:
                raise _error("SESSION_UNAVAILABLE")
            requests = {self._next_id + index: item for index, item in enumerate(contracts)}
            self._next_id += len(contracts) + 1
            active = _Pending(self._generation, requests)
            self._active = active
        issued = []
        cleanup_failed = False
        deadline = time.monotonic() + self._timeout
        try:
            for request_id, contract in requests.items():
                issued.append(request_id)
                self._requester.reqPnLSingle(request_id, self._account, "", contract.con_id)
            with self._condition:
                while len(active.samples) != len(requests):
                    if active.error is not None:
                        raise active.error
                    if not self._authenticated or self._generation != active.generation:
                        raise _error("GENERATION_LOST")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise _error("CALLBACK_TIMEOUT")
                    self._condition.wait(remaining)
                if active.error is not None:
                    raise active.error
                if not self._authenticated or self._generation != active.generation:
                    raise _error("GENERATION_LOST")
                completed = self._now()
                samples = tuple(active.samples[key] for key in requests)
            failures = [
                "IBKR_POSITION_VALUATION_SOURCE_TIME_UNPROVEN",
                "IBKR_POSITION_VALUATION_NLV_EPOCH_UNPROVEN",
                "IBKR_POSITION_VALUATION_REMAINING_FEES_UNPROVEN",
            ]
            if not snapshot.whole_broker_reconciled:
                failures.append("IBKR_POSITION_VALUATION_WHOLE_ACCOUNT_COVERAGE_UNPROVEN")
            if any(not 0 <= (completed - stamp).total_seconds() <= 5 for stamp in (
                started, snapshot.observed_at, snapshot.received_at,
                *(item.positions_received_at for item in contracts),
                *(item.received_at for item in samples),
            )):
                failures.append("IBKR_POSITION_VALUATION_RECEIPT_STALE")
            reconciled = snapshot.funds.cash + sum((item.market_value for item in samples), Decimal("0")) == snapshot.funds.total_value
            if not reconciled:
                failures.append("IBKR_POSITION_VALUATION_CASH_PLUS_POSITIONS_NLV_MISMATCH")
            return PositionValuationReport(self._mask, collection_id, started, completed, samples, tuple(failures), reconciled)
        except PositionValuationError:
            raise
        except Exception:
            raise _error("REQUEST_FAILED") from None
        finally:
            for request_id in issued:
                try:
                    self._requester.cancelPnLSingle(request_id)
                except Exception:
                    cleanup_failed = True
            with self._condition:
                self._active = None
            if cleanup_failed:
                raise _error("SUBSCRIPTION_CLEANUP_FAILED") from None


__all__ = ["IbkrPositionValuationCollector", "PositionValuationContract", "PositionValuationError", "PositionValuationReport"]
