"""Read-only IBKR contract identity and regular-session eligibility evidence.

The provider uses an injected official ``EClient``-compatible contract-details
requester.  It does not import ``ibapi``, request market data, or infer broker
tradability from Massive.  The generation-bound callback object has the same
method names/signatures as the relevant official ``EWrapper`` callbacks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import re
from threading import Condition, RLock
import time
from typing import Callable, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from .base import BrokerCapabilityError, BrokerContractViolation
from .ibkr_orders import IbkrContractIdentity


_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.\-]{0,14}$")
_PRIMARY = re.compile(r"^[A-Z][A-Z0-9.]{0,15}$")
_INFO_CODES = frozenset({1101, 1102, 2104, 2106, 2107, 2108, 2158})


@runtime_checkable
class IbkrContractDetailsRequester(Protocol):
    def reqContractDetails(self, reqId: int, contract: object) -> None: ...
    def cancelContractDetails(self, reqId: int) -> None: ...


@dataclass(frozen=True)
class IbkrContractReadReceipt:
    """Metadata connectivity only; deliberately not instrument eligibility.

    No ``evidence_id`` or affirmative ``regular_hours_eligible`` attribute is
    exposed, so this receipt cannot satisfy the execution evidence contract.
    """

    receipt_id: str
    identity: IbkrContractIdentity
    received_at: datetime
    eligibility_checked_at: datetime
    regular_session_open: bool


@dataclass(frozen=True)
class IbkrInstrumentEvidence:
    evidence_id: str
    identity: IbkrContractIdentity
    source: str
    exchange_listed: bool
    regular_hours_eligible: bool
    observed_at: datetime
    received_at: datetime
    eligibility_at: datetime | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", self.evidence_id):
            raise ValueError("instrument evidence ID must be a SHA-256 receipt")
        if not isinstance(self.identity, IbkrContractIdentity):
            raise ValueError("IBKR contract identity is required")
        if self.source != "ibkr:tws-contract-details":
            raise ValueError("instrument evidence source must be IBKR")
        if self.exchange_listed is not True or self.regular_hours_eligible is not True:
            raise ValueError("instrument evidence must be affirmatively eligible")
        for name in ("observed_at", "received_at"):
            value = getattr(self, name)
            if not isinstance(value, datetime) or value.tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware")
            object.__setattr__(self, name, value.astimezone(timezone.utc))
        eligibility_at = self.eligibility_at or self.observed_at
        if not isinstance(eligibility_at, datetime) or eligibility_at.tzinfo is None:
            raise ValueError("eligibility_at must be timezone-aware")
        object.__setattr__(self, "eligibility_at", eligibility_at.astimezone(timezone.utc))
        if self.received_at < self.observed_at:
            raise ValueError("instrument receipt cannot precede observation")


@dataclass
class _Request:
    generation: int
    request_id: int
    symbol: str
    started_at: datetime
    details: list[tuple[object, datetime]] = field(default_factory=list)
    ended: bool = False
    error_code: int | None = None


class _Callbacks:
    def __init__(self, provider: "IbkrInstrumentProvider", generation: int) -> None:
        self._provider = provider
        self._generation = generation

    def managedAccounts(self, accountsList: str) -> None:
        self._provider._managed_accounts(self._generation, accountsList)

    def contractDetails(self, reqId: int, contractDetails: object) -> None:
        self._provider._contract_details(
            self._generation, reqId, contractDetails
        )

    def contractDetailsEnd(self, reqId: int) -> None:
        self._provider._contract_details_end(self._generation, reqId)

    def error(self, reqId: object, *arguments: object) -> None:
        # IB API 10.50 adds ``errorTime`` before ``errorCode``.  Preserve
        # compatibility with the legacy callback shape without retaining any
        # broker text or advanced reject JSON.
        error_code: object = 0
        if (
            len(arguments) in (3, 4)
            and type(arguments[0]) is int
            and type(arguments[1]) is int
        ):
            error_code = arguments[1]
        elif len(arguments) in (2, 3) and type(arguments[0]) is int:
            error_code = arguments[0]
        request_id = reqId if type(reqId) is int else -1
        code = error_code if type(error_code) is int else 0
        self._provider._error(self._generation, request_id, code)

    def connectionClosed(self) -> None:
        self._provider._connection_closed(self._generation)


class IbkrInstrumentProvider:
    """Unique STK/USD/SMART contract lookup with IBKR-hours evidence."""

    def __init__(
        self,
        *,
        requester: IbkrContractDetailsRequester,
        contract_factory: Callable[[], object],
        exact_account_id: str,
        account_masked: str,
        timeout_seconds: float,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(requester, IbkrContractDetailsRequester):
            raise TypeError("IBKR contract-details requester is required")
        if not callable(contract_factory):
            raise TypeError("IBKR contract factory is required")
        if not isinstance(exact_account_id, str) or not re.fullmatch(r"(?:U|DU)[0-9]+", exact_account_id):
            raise ValueError("exact IBKR account ID is required")
        if not isinstance(account_masked, str) or not re.fullmatch(r"(?:\*{4}|•{4})[0-9]{4}", account_masked):
            raise ValueError("masked IBKR account ID is required")
        if exact_account_id[-4:] != account_masked[-4:]:
            raise ValueError("IBKR account bindings disagree")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
            raise ValueError("contract-details timeout must be positive")
        self._requester = requester
        self._contract_factory = contract_factory
        self._exact_account_id = exact_account_id
        self._account_masked = account_masked
        self._timeout = float(timeout_seconds)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._condition = Condition(RLock())
        self._generation = 0
        self._authenticated_generation: int | None = None
        self._authenticated_at: datetime | None = None
        self._active: _Request | None = None
        self._next_request_id = 2_000_000

    @property
    def source(self) -> str:
        return "ibkr:tws-contract-details"

    @property
    def authenticated(self) -> bool:
        with self._condition:
            return (
                self._generation > 0
                and self._authenticated_generation == self._generation
            )

    @property
    def authenticated_at(self) -> datetime | None:
        with self._condition:
            return self._authenticated_at

    def release_components(self) -> tuple[tuple[str, object, tuple[str, ...]], ...]:
        """Inventory retained contract-read leaves for release attestation."""
        return (
            (
                "ibkr_contract_details_requester",
                self._requester,
                ("reqContractDetails", "cancelContractDetails"),
            ),
            ("ibkr_instrument_contract_factory", self._contract_factory, ("__call__",)),
            ("ibkr_instrument_clock", self._clock, ("__call__",)),
        )

    def open_generation(self, generation: int) -> _Callbacks:
        if isinstance(generation, bool) or not isinstance(generation, int) or generation <= self._generation:
            raise BrokerContractViolation("IBKR instrument generation must strictly advance")
        with self._condition:
            if self._active is not None:
                self._active.error_code = 1100
            self._generation = generation
            self._authenticated_generation = None
            self._authenticated_at = None
            self._condition.notify_all()
        return _Callbacks(self, generation)

    def get_instrument(
        self, symbol: str, *, now: datetime
    ) -> IbkrInstrumentEvidence:
        current = self._utc(now)
        return self._get_instrument(
            symbol,
            observed_at=current,
            eligibility_at=current,
        )

    def read_contract_metadata(
        self, symbol: str, *, now: datetime
    ) -> IbkrContractReadReceipt:
        """Verify one complete, unique contract read, including when closed.

        This does not request market data or authorize an order.  Callers that
        need tradability must still use the strict ``get_instrument`` path.
        """
        current = self._utc(now)
        return self._read_contract(symbol, eligibility_at=current)

    def get_upcoming_regular_session_instrument(
        self,
        symbol: str,
        *,
        now: datetime,
        eligibility_at: datetime,
    ) -> IbkrInstrumentEvidence:
        """Read contract details now and verify one imminent regular session.

        This is analysis evidence only.  The observation timestamp remains the
        real callback receipt; a future session-open timestamp is never passed
        off as the current time.  A narrow three-hour bound prevents callers
        from treating old contract facts as evergreen eligibility.
        """

        current = self._utc(now)
        eligible = self._utc(eligibility_at)
        horizon = (eligible - current).total_seconds()
        if horizon < 0 or horizon > 3 * 60 * 60:
            raise BrokerContractViolation(
                "IBKR_UPCOMING_SESSION_ELIGIBILITY_TIME_INVALID"
            )
        return self._get_instrument(
            symbol,
            observed_at=current,
            eligibility_at=eligible,
        )

    def _get_instrument(
        self,
        symbol: str,
        *,
        observed_at: datetime,
        eligibility_at: datetime,
    ) -> IbkrInstrumentEvidence:
        self._utc(observed_at)
        receipt = self._read_contract(symbol, eligibility_at=eligibility_at)
        if receipt.regular_session_open is not True:
            raise BrokerCapabilityError("IBKR_CONTRACT_NOT_REGULAR_HOURS_ELIGIBLE")
        return IbkrInstrumentEvidence(
            evidence_id=receipt.receipt_id,
            identity=receipt.identity,
            source=self.source,
            exchange_listed=True,
            regular_hours_eligible=True,
            observed_at=receipt.received_at,
            received_at=receipt.received_at,
            eligibility_at=receipt.eligibility_checked_at,
        )

    def _read_contract(
        self, symbol: str, *, eligibility_at: datetime
    ) -> IbkrContractReadReceipt:
        eligibility_at = self._utc(eligibility_at)
        normalized = str(symbol).strip().upper()
        if not _SYMBOL.fullmatch(normalized):
            raise BrokerContractViolation("IBKR_INSTRUMENT_SYMBOL_INVALID")
        with self._condition:
            if self._authenticated_generation != self._generation:
                raise BrokerCapabilityError("IBKR_INSTRUMENT_NOT_AUTHENTICATED")
            deadline = time.monotonic() + self._timeout
            while self._active is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BrokerCapabilityError("IBKR_INSTRUMENT_REQUEST_QUEUE_TIMEOUT")
                self._condition.wait(remaining)
                if self._authenticated_generation != self._generation:
                    raise BrokerCapabilityError("IBKR_INSTRUMENT_NOT_AUTHENTICATED")
            request = _Request(
                generation=self._generation,
                request_id=self._next_request_id,
                symbol=normalized,
                started_at=self._now(),
            )
            self._next_request_id += 1
            self._active = request
        try:
            query = self._query(normalized)
            try:
                self._requester.reqContractDetails(request.request_id, query)
            except Exception:
                raise BrokerCapabilityError("IBKR_CONTRACT_DETAILS_DISPATCH_FAILED") from None
            self._wait(request)
            with self._condition:
                if self._active is not request:
                    raise BrokerCapabilityError("IBKR_INSTRUMENT_GENERATION_LOST")
                self._active = None
                self._condition.notify_all()
            return self._normalize(request, eligibility_at)
        finally:
            with self._condition:
                if self._active is request:
                    self._active = None
                    self._condition.notify_all()
            try:
                self._requester.cancelContractDetails(request.request_id)
            except Exception:
                pass

    def contract_for(self, symbol: str, *, now: datetime) -> IbkrContractIdentity:
        return self.get_instrument(symbol, now=now).identity

    def _query(self, symbol: str) -> object:
        try:
            query = self._contract_factory()
            query.symbol = symbol
            query.secType = "STK"
            query.currency = "USD"
            query.exchange = "SMART"
            query.primaryExchange = ""
            query.conId = 0
            return query
        except Exception:
            raise BrokerCapabilityError("IBKR_CONTRACT_QUERY_CONSTRUCTION_FAILED") from None

    def _wait(self, request: _Request) -> None:
        deadline = time.monotonic() + self._timeout
        with self._condition:
            while True:
                if self._active is not request or request.generation != self._generation:
                    raise BrokerCapabilityError("IBKR_INSTRUMENT_GENERATION_LOST")
                if request.error_code is not None:
                    raise BrokerCapabilityError(
                        f"IBKR_CONTRACT_DETAILS_ERROR:{request.error_code}"
                    )
                if request.ended:
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BrokerCapabilityError("IBKR_CONTRACT_DETAILS_TIMEOUT")
                self._condition.wait(remaining)

    def _normalize(
        self, request: _Request, eligibility_at: datetime
    ) -> IbkrContractReadReceipt:
        matches: list[tuple[IbkrContractIdentity, object, datetime]] = []
        for details, received in request.details:
            contract = getattr(details, "contract", None)
            if contract is None:
                continue
            try:
                identity = IbkrContractIdentity(
                    con_id=self._positive_int(getattr(contract, "conId", None)),
                    symbol=self._required_text(contract, "symbol").upper(),
                    primary_exchange=self._primary_exchange(contract),
                    sec_type=self._required_text(contract, "secType").upper(),
                    currency=self._required_text(contract, "currency").upper(),
                    exchange="SMART",
                )
            except (TypeError, ValueError, BrokerContractViolation):
                continue
            if identity.symbol != request.symbol:
                continue
            valid = self._valid_exchanges(details)
            if "SMART" not in valid or identity.primary_exchange not in valid:
                continue
            matches.append((identity, details, received))
        by_identity = {
            (item[0].con_id, item[0].symbol, item[0].primary_exchange): item
            for item in matches
        }
        if len(by_identity) != 1:
            raise BrokerContractViolation("IBKR_CONTRACT_LOOKUP_NOT_UNIQUE")
        identity, details, received = next(iter(by_identity.values()))
        regular_session_open = self._regular_hours_eligible(details, eligibility_at)
        evidence_id = hashlib.sha256(
            (
                f"ibkr-contract-v1:{identity.con_id}:{identity.symbol}:"
                f"{identity.primary_exchange}:{request.generation}:"
                f"{request.request_id}:{received.isoformat()}:"
                f"{eligibility_at.isoformat()}"
            ).encode("ascii")
        ).hexdigest()
        return IbkrContractReadReceipt(
            receipt_id=evidence_id,
            identity=identity,
            received_at=received,
            eligibility_checked_at=eligibility_at,
            regular_session_open=regular_session_open,
        )

    def _regular_hours_eligible(self, details: object, now: datetime) -> bool:
        hours = getattr(details, "liquidHours", None)
        zone_name = getattr(details, "timeZoneId", None)
        if not isinstance(hours, str) or not hours.strip() or not isinstance(zone_name, str):
            return False
        try:
            zone = ZoneInfo(self._normalize_zone(zone_name))
        except Exception:
            return False
        local_now = now.astimezone(zone)
        for segment in hours.split(";"):
            segment = segment.strip()
            if not segment or segment.endswith(":CLOSED") or "-" not in segment:
                continue
            start_raw, end_raw = segment.split("-", 1)
            try:
                start = self._hours_time(start_raw, zone)
                end = self._hours_time(end_raw, zone)
            except ValueError:
                continue
            if start <= local_now < end:
                return True
        return False

    @staticmethod
    def _hours_time(value: str, zone: ZoneInfo) -> datetime:
        raw = value.strip()
        for pattern in ("%Y%m%d:%H%M", "%Y%m%d:%H%M%S"):
            try:
                return datetime.strptime(raw, pattern).replace(tzinfo=zone)
            except ValueError:
                continue
        raise ValueError("unsupported IBKR hours timestamp")

    @staticmethod
    def _normalize_zone(value: str) -> str:
        normalized = value.strip()
        aliases = {
            "US/Eastern": "America/New_York",
            "EST": "America/New_York",
            "EDT": "America/New_York",
        }
        return aliases.get(normalized, normalized)

    def _primary_exchange(self, contract: object) -> str:
        value = self._required_text(contract, "primaryExchange").upper()
        if not _PRIMARY.fullmatch(value) or value in {"SMART", "BEST", "OVERNIGHT", "IBKRATS"}:
            raise BrokerContractViolation("IBKR_PRIMARY_EXCHANGE_INVALID")
        return value

    @staticmethod
    def _valid_exchanges(details: object) -> frozenset[str]:
        value = getattr(details, "validExchanges", None)
        if not isinstance(value, str):
            return frozenset()
        return frozenset(item.strip().upper() for item in value.split(",") if item.strip())

    @staticmethod
    def _required_text(value: object, field_name: str) -> str:
        result = getattr(value, field_name, None)
        if not isinstance(result, str) or not result.strip():
            raise BrokerContractViolation(f"IBKR_CONTRACT_{field_name.upper()}_INVALID")
        return result.strip()

    @staticmethod
    def _positive_int(value: object) -> int:
        if isinstance(value, bool):
            raise BrokerContractViolation("IBKR_CONTRACT_ID_INVALID")
        try:
            result = int(value)
        except (TypeError, ValueError):
            raise BrokerContractViolation("IBKR_CONTRACT_ID_INVALID") from None
        if result != value or result <= 0:
            raise BrokerContractViolation("IBKR_CONTRACT_ID_INVALID")
        return result

    def _managed_accounts(self, generation: int, accounts: str) -> None:
        if generation != self._generation or not isinstance(accounts, str):
            return
        account_set = {item.strip() for item in accounts.split(",") if item.strip()}
        with self._condition:
            if self._exact_account_id in account_set:
                self._authenticated_generation = generation
                self._authenticated_at = self._now()
            else:
                self._authenticated_generation = None
                self._authenticated_at = None
            self._condition.notify_all()

    def _contract_details(
        self, generation: int, request_id: int, details: object
    ) -> None:
        with self._condition:
            request = self._active
            if (
                request is None
                or generation != self._generation
                or request_id != request.request_id
            ):
                return
            request.details.append((details, self._now()))

    def _contract_details_end(self, generation: int, request_id: int) -> None:
        with self._condition:
            request = self._active
            if request is None or generation != self._generation or request_id != request.request_id:
                return
            request.ended = True
            self._condition.notify_all()

    def _error(self, generation: int, request_id: object, code: object) -> None:
        try:
            normalized_request = int(request_id)
            normalized_code = int(code)
        except (TypeError, ValueError):
            return
        with self._condition:
            request = self._active
            if generation != self._generation or normalized_code in _INFO_CODES:
                return
            if request is not None and (
                normalized_request == request.request_id or normalized_request == -1
            ):
                request.error_code = normalized_code
                self._condition.notify_all()

    def _connection_closed(self, generation: int) -> None:
        with self._condition:
            if generation != self._generation:
                return
            self._authenticated_generation = None
            self._authenticated_at = None
            if self._active is not None:
                self._active.error_code = 1100
            self._condition.notify_all()

    def _now(self) -> datetime:
        return self._utc(self._clock())

    @staticmethod
    def _utc(value: datetime) -> datetime:
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise BrokerContractViolation("IBKR instrument clock must be timezone-aware")
        return value.astimezone(timezone.utc)


__all__ = [
    "IbkrContractDetailsRequester",
    "IbkrInstrumentEvidence",
    "IbkrInstrumentProvider",
]
