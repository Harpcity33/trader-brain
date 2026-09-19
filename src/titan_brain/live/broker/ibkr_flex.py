"""Read-only IBKR Activity Flex ingestion, never live daily-risk authority.

Flex provides statement-period accounting. Neither a report's generation time
nor this reader's receipt time is an economic valuation/complete-through time.
The result intentionally cannot create a VerifiedIbkrDailyRiskBaseline or an
authenticated zero-current-flow assertion. Transport authentication, exact
account matching and a content hash do not change that limitation.

Configure an XML Activity query for one account with Account Information,
Change in NAV and Cash Transactions. Query-specific field availability still
requires a real report check. See validation/full-live/2026-09-15/
IBKR_FLEX_DAILY_EVIDENCE_SETUP_2026-09-15.md for sources and owner setup.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import math
import re
from threading import Lock
import time
from typing import Callable
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from xml.etree import ElementTree as ET

from ..calendar import NEW_YORK
FLEX_BASE_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
_MAX_REPORT_BYTES = 8 * 1024 * 1024
_MAX_RESPONSE_BYTES = 64 * 1024
_MAX_ELEMENTS = 50_000
_IDENTIFIER = re.compile(r"[0-9]{1,32}\Z")
_ACCOUNT = re.compile(r"[A-Z]{1,3}[0-9]{4,12}\Z")
_DECIMAL = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")
_ERROR_CODE = re.compile(r"[A-Z0-9_]{1,80}\Z")


class IbkrFlexError(RuntimeError):
    """Only a stable code is exposed; never URLs, tokens or response bodies."""

    def __init__(self, code: str) -> None:
        if not _ERROR_CODE.fullmatch(code):
            raise ValueError("invalid Flex error code")
        self.code = f"IBKR_FLEX_{code}"
        super().__init__(self.code)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise IbkrFlexError("CLOCK_INVALID")
    return value.astimezone(timezone.utc)


def _report_date(value: str | None) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{8}", value):
        raise IbkrFlexError("REPORT_DATE_INVALID")
    try:
        return date(int(value[:4]), int(value[4:6]), int(value[6:]))
    except ValueError:
        raise IbkrFlexError("REPORT_DATE_INVALID") from None


def _number(value: str | None, *, optional: bool = False) -> Decimal | None:
    # An omitted report field is unknown, not a broker-reported zero.
    if value is None and optional:
        return None
    if not isinstance(value, str) or len(value) > 80 or not _DECIMAL.fullmatch(value):
        raise IbkrFlexError("REPORT_AMOUNT_INVALID")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise IbkrFlexError("REPORT_AMOUNT_INVALID") from None
    if not result.is_finite() or len(result.as_tuple().digits) > 40:
        raise IbkrFlexError("REPORT_AMOUNT_INVALID")
    return result


def _xml(raw: bytes, limit: int) -> ET.Element:
    if type(raw) is not bytes or not raw or len(raw) > limit:
        raise IbkrFlexError("RESPONSE_SIZE_INVALID")
    try:
        decoded = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise IbkrFlexError("XML_INVALID") from None
    # No DTD/entity expansion, alternate encodings or external resource lookup.
    if "<!" in decoded or "\x00" in decoded:
        raise IbkrFlexError("XML_UNSAFE")
    try:
        root = ET.fromstring(decoded)
    except (ET.ParseError, ValueError):
        raise IbkrFlexError("XML_INVALID") from None
    if sum(1 for _ in root.iter()) > _MAX_ELEMENTS:
        raise IbkrFlexError("RESPONSE_SIZE_INVALID")
    if root.tag == "FlexStatementResponse":
        statuses = root.findall("Status")
        if len(statuses) != 1:
            raise IbkrFlexError("RESPONSE_STATUS_INVALID")
        if statuses[0].text == "Success":
            # A success envelope and provider error fields are contradictory.
            # Never select the favorable half of a malformed mixed response.
            if root.findall("ErrorCode") or root.findall("ErrorMessage"):
                raise IbkrFlexError("RESPONSE_STATUS_INVALID")
        else:
            codes = root.findall("ErrorCode")
            code = codes[0].text if len(codes) == 1 else None
            # Free-form ErrorMessage can contain account data or credentials.
            if code is not None and re.fullmatch(r"10[0-9]{2}", code):
                raise IbkrFlexError(f"PROVIDER_{code}")
            raise IbkrFlexError("PROVIDER_REJECTED")
    return root


@dataclass(frozen=True)
class IbkrFlexQuery:
    query_id: str = field(repr=False)
    expected_account_id: str = field(repr=False)
    from_date: date
    to_date: date

    def __post_init__(self) -> None:
        if type(self.query_id) is not str or not _IDENTIFIER.fullmatch(self.query_id):
            raise IbkrFlexError("QUERY_ID_INVALID")
        if type(self.expected_account_id) is not str or not _ACCOUNT.fullmatch(self.expected_account_id):
            raise IbkrFlexError("ACCOUNT_ID_INVALID")
        if (
            type(self.from_date) is not date
            or type(self.to_date) is not date
            or not 0 <= (self.to_date - self.from_date).days < 365
        ):
            raise IbkrFlexError("DATE_RANGE_INVALID")


@dataclass(frozen=True)
class IbkrFlexTicket:
    query: IbkrFlexQuery
    reference_code: str = field(repr=False)
    generation_response_sha256: str
    _reader_identity: object = field(repr=False, compare=False)


@dataclass(frozen=True)
class IbkrFlexPeriodNav:
    """Reported period values, not a midnight or current-day baseline."""

    starting_value: Decimal
    ending_value: Decimal
    deposits_withdrawals: Decimal | None
    internal_cash_transfers: Decimal | None
    asset_transfers: Decimal | None


@dataclass(frozen=True)
class IbkrFlexCashTransaction:
    currency: str
    amount: Decimal
    fx_rate_to_base: Decimal
    # Report Date attributes the row to the statement period.  It is private
    # bookkeeping metadata, not an economic/source timestamp.  Retain the
    # provider's type/time without guessing taxonomy or time zone.
    provider_report_date: date = field(repr=False)
    transaction_type: str = field(repr=False)
    provider_date_time: str = field(repr=False)
    source_row_ordinal: int


@dataclass(frozen=True)
class IbkrFlexActivityReport:
    account_last4: str
    from_date: date
    to_date: date
    base_currency: str
    response_sha256: str
    received_at: datetime
    nav: IbkrFlexPeriodNav
    cash_transactions: tuple[IbkrFlexCashTransaction, ...]
    cash_transactions_section_present: bool
    response_origin: str = "local_unverified_bytes"
    generation_response_sha256: str | None = None

    @property
    def daily_starting_equity_ready(self) -> bool:
        return False

    def require_live_daily_evidence(self) -> None:
        raise IbkrFlexError("LIVE_DAILY_EVIDENCE_UNSUPPORTED")

    def diagnostic_summary(self) -> dict[str, object]:
        """Bounded diagnostics without raw account IDs or report values.

        Response digests support private byte-integrity/equality checks.  They
        are linkable identifiers, not anonymization or a privacy boundary.
        """

        return {
            "provider": "ibkr_activity_flex",
            "account_last4": self.account_last4,
            "from_date": self.from_date.isoformat(),
            "to_date": self.to_date.isoformat(),
            "base_currency": self.base_currency,
            "response_sha256": self.response_sha256,
            "received_at": self.received_at.isoformat(),
            "response_origin": self.response_origin,
            "generation_response_sha256": self.generation_response_sha256,
            "cash_transactions_count": len(self.cash_transactions),
            "cash_transactions_section_present": self.cash_transactions_section_present,
            "reporting_only": True,
            "daily_starting_equity_ready": False,
            "live_cash_flow_complete_through": None,
            "midnight_valuation_at": None,
        }


def parse_ibkr_flex_activity_report(
    raw: bytes, *, query: IbkrFlexQuery, received_at: datetime
) -> IbkrFlexActivityReport:
    """Parse an exact single-account XML report; local bytes imply no auth.

    The default online reader uses the same parser after a TLS-authenticated
    request.  An injected opener remains explicitly unverified test/custom
    transport.  Neither entrypoint claims live completeness, even for an empty
    section.
    """

    if type(query) is not IbkrFlexQuery:
        raise IbkrFlexError("QUERY_INVALID")
    observed = _utc(received_at)
    root = _xml(raw, _MAX_REPORT_BYTES)
    if root.tag != "FlexQueryResponse" or root.get("type") != "AF":
        raise IbkrFlexError("ACTIVITY_REPORT_REQUIRED")
    containers = root.findall("FlexStatements")
    if len(containers) != 1 or len(list(root)) != 1 or containers[0].get("count") != "1":
        raise IbkrFlexError("SINGLE_ACCOUNT_REPORT_REQUIRED")
    statements = containers[0].findall("FlexStatement")
    if len(statements) != 1 or len(list(containers[0])) != 1:
        raise IbkrFlexError("SINGLE_ACCOUNT_REPORT_REQUIRED")
    statement = statements[0]
    if statement.get("accountId") != query.expected_account_id:
        raise IbkrFlexError("ACCOUNT_MISMATCH")
    # Reject nested/model/other-account data instead of silently selecting it.
    for element in root.iter():
        if element.get("accountId") not in {None, query.expected_account_id}:
            raise IbkrFlexError("ACCOUNT_MISMATCH")
        if element.get("model") not in {None, ""}:
            raise IbkrFlexError("WHOLE_ACCOUNT_SCOPE_UNPROVEN")
    start, end = _report_date(statement.get("fromDate")), _report_date(statement.get("toDate"))
    if (start, end) != (query.from_date, query.to_date):
        raise IbkrFlexError("REPORT_PERIOD_MISMATCH")
    accounts = statement.findall("AccountInformation")
    if len(accounts) != 1 or accounts[0].get("accountId") != query.expected_account_id:
        raise IbkrFlexError("ACCOUNT_INFORMATION_REQUIRED")
    if accounts[0].get("currency") != "USD":
        raise IbkrFlexError("BASE_CURRENCY_UNSUPPORTED")
    nav_rows = statement.findall("ChangeInNAV")
    if len(nav_rows) != 1:
        raise IbkrFlexError("PERIOD_NAV_REQUIRED")
    nav_row = nav_rows[0]
    if (
        nav_row.get("accountId") != query.expected_account_id
        or _report_date(nav_row.get("fromDate")) != start
        or _report_date(nav_row.get("toDate")) != end
        or nav_row.get("currency") not in {None, "USD"}
        or list(nav_row)
    ):
        raise IbkrFlexError("NAV_SCOPE_MISMATCH")
    nav = IbkrFlexPeriodNav(
        starting_value=_number(nav_row.get("startingValue")),
        ending_value=_number(nav_row.get("endingValue")),
        deposits_withdrawals=_number(nav_row.get("depositsWithdrawals"), optional=True),
        internal_cash_transfers=_number(nav_row.get("internalCashTransfers"), optional=True),
        asset_transfers=_number(nav_row.get("assetTransfers"), optional=True),
    )
    sections = statement.findall("CashTransactions")
    if len(sections) > 1:
        raise IbkrFlexError("CASH_SECTION_AMBIGUOUS")
    transactions = []
    if sections:
        for row_ordinal, row in enumerate(sections[0], start=1):
            if row.tag != "CashTransaction" or list(row):
                raise IbkrFlexError("CASH_ROW_INVALID")
            if row.get("accountId") != query.expected_account_id:
                raise IbkrFlexError("ACCOUNT_MISMATCH")
            currency, kind, when = row.get("currency"), row.get("type"), row.get("dateTime")
            if (
                not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency)
                or not isinstance(kind, str) or not 1 <= len(kind) <= 128
                or any(ord(char) < 32 for char in kind)
                or not isinstance(when, str) or not re.fullmatch(r"[0-9]{8};[0-9]{6}", when)
            ):
                raise IbkrFlexError("CASH_ROW_INVALID")
            try:
                datetime.strptime(when, "%Y%m%d;%H%M%S")
            except ValueError:
                raise IbkrFlexError("CASH_ROW_INVALID") from None
            try:
                report_date = _report_date(row.get("reportDate"))
            except IbkrFlexError:
                raise IbkrFlexError("CASH_REPORT_DATE_INVALID") from None
            if not start <= report_date <= end:
                raise IbkrFlexError("CASH_PERIOD_MISMATCH")
            rate = _number(row.get("fxRateToBase"))
            if rate <= 0 or (currency == "USD" and rate != 1):
                raise IbkrFlexError("CASH_FX_INVALID")
            transactions.append(IbkrFlexCashTransaction(
                currency=currency, amount=_number(row.get("amount")), fx_rate_to_base=rate,
                provider_report_date=report_date, transaction_type=kind,
                provider_date_time=when,
                source_row_ordinal=row_ordinal,
            ))
    return IbkrFlexActivityReport(
        account_last4=query.expected_account_id[-4:],
        from_date=start, to_date=end,
        base_currency="USD", response_sha256=hashlib.sha256(raw).hexdigest(),
        received_at=observed, nav=nav, cash_transactions=tuple(transactions),
        cash_transactions_section_present=bool(sections),
    )


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise IbkrFlexError("REDIRECT_REFUSED")


class IbkrFlexReportReader:
    """One read-only consumer for one token; callers schedule bounded retries.

    No automatic retry or report regeneration occurs. Reuse the same ticket
    after provider 1019; all attempts are spaced at least six seconds apart.
    A token shared with other processes needs external pacing coordination.
    """

    def __init__(
        self, *, token_reader: Callable[[], str],
        opener: Callable | None = None, timeout_seconds: float = 10,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not callable(token_reader) or not callable(monotonic):
            raise IbkrFlexError("READER_INVALID")
        if type(timeout_seconds) not in {int, float} or not 0 < timeout_seconds <= 30:
            raise IbkrFlexError("TIMEOUT_INVALID")
        self._token_reader = token_reader
        # Fixed HTTPS endpoints, normal certificate checks, no environment
        # proxies and no redirects which could forward a token-bearing URL.
        self._response_origin = (
            "injected_transport_unverified_bytes"
            if opener is not None
            else "flex_web_service_response"
        )
        self._opener = opener if opener is not None else build_opener(ProxyHandler({}), _NoRedirects()).open
        self._timeout = float(timeout_seconds)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic
        self._last_attempt: float | None = None
        self._lock = Lock()
        self._identity = object()

    def _get(self, endpoint: str, parameters: dict[str, str], limit: int) -> bytes:
        with self._lock:
            current = self._monotonic()
            if type(current) not in {int, float} or not math.isfinite(current):
                raise IbkrFlexError("CLOCK_INVALID")
            if self._last_attempt is not None and current - self._last_attempt < 6:
                raise IbkrFlexError("PACING_WAIT_REQUIRED")
            self._last_attempt = current
        try:
            token = self._token_reader()
        except Exception:
            raise IbkrFlexError("TOKEN_UNAVAILABLE") from None
        if type(token) is not str or not re.fullmatch(r"[0-9]{6,128}", token):
            raise IbkrFlexError("TOKEN_INVALID")
        request = Request(
            f"{FLEX_BASE_URL}/{endpoint}?{urlencode({'t': token, **parameters, 'v': '3'})}",
            headers={"Accept": "text/plain, application/xml", "User-Agent": "Titan-Flex-Reporting/1"},
            method="GET",
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                raw = response.read(limit + 1)
        except IbkrFlexError:
            raise
        except Exception:
            raise IbkrFlexError("TRANSPORT_FAILED") from None
        if type(raw) is not bytes or not raw or len(raw) > limit:
            raise IbkrFlexError("RESPONSE_SIZE_INVALID")
        return raw

    def request_report(self, query: IbkrFlexQuery) -> IbkrFlexTicket:
        if type(query) is not IbkrFlexQuery:
            raise IbkrFlexError("QUERY_INVALID")
        # Only completed reporting dates; this does not claim that all broker
        # accounting for those dates has already become final.
        if query.to_date >= _utc(self._clock()).astimezone(NEW_YORK).date():
            raise IbkrFlexError("HISTORICAL_PERIOD_REQUIRED")
        raw = self._get("SendRequest", {
            "q": query.query_id, "fd": query.from_date.strftime("%Y%m%d"),
            "td": query.to_date.strftime("%Y%m%d"),
        }, _MAX_RESPONSE_BYTES)
        root = _xml(raw, _MAX_RESPONSE_BYTES)
        refs = root.findall("ReferenceCode")
        if (
            root.tag != "FlexStatementResponse" or len(refs) != 1
            or refs[0].text is None or not _IDENTIFIER.fullmatch(refs[0].text)
        ):
            raise IbkrFlexError("REFERENCE_INVALID")
        # IBKR specifically says to ignore the response's legacy <url>.
        return IbkrFlexTicket(query, refs[0].text, hashlib.sha256(raw).hexdigest(), self._identity)

    def retrieve_report(self, ticket: IbkrFlexTicket) -> IbkrFlexActivityReport:
        if type(ticket) is not IbkrFlexTicket or ticket._reader_identity is not self._identity:
            raise IbkrFlexError("TICKET_MISMATCH")
        raw = self._get("GetStatement", {"q": ticket.reference_code}, _MAX_REPORT_BYTES)
        report = parse_ibkr_flex_activity_report(raw, query=ticket.query, received_at=self._clock())
        return replace(
            report, response_origin=self._response_origin,
            generation_response_sha256=ticket.generation_response_sha256,
        )
