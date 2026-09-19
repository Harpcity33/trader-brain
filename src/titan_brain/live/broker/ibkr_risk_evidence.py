"""Authenticated prior-period IBKR risk evidence and monotone peak equity.

The official TWS ``reqPnL`` subscription supplies a current-day realized-P&L
value.  It does not supply the week-to-date value through a prior session or a
historical account-equity high-water mark.  This module deliberately does not
pretend otherwise.

Instead, a trusted external control plane may place one private, canonical,
HMAC-authenticated baseline receipt for an exact trading date.  The receipt
binds the release, configuration, policy, risk limits, redacted account and
non-reversible account binding.  It also binds the exact preceding trading
date, an account-authoritative week-to-date realized P&L through that date, and
an account high-water equity value to an immutable upstream receipt hash.
This module only authenticates and consumes that input; it never creates or
fetches the upstream baseline.

The distinct daily-starting-equity schema additionally authenticates the
00:00 America/New_York balance and cumulative external cash flows for an exact
current valuation.  Current flows expire after five seconds and never default
to zero.  The v4 ledger preserves the fixed baseline and flow watermark across
restart and installer-controlled release rebinding; retained historical flow
rows never become fresh authority by themselves.

The separate ``ibkr_flex`` reader ingests authenticatable statement-period
reporting data only. Its receipt time and period NAV must not be substituted
for the daily schema's midnight valuation or current exhaustive flow proof;
it deliberately cannot construct this module's verified baseline type.

The account-snapshot wrapper adds the freshly collected TWS current-day value
to the authenticated prior-day value.  A separate, identity-bound SQLite
ledger retains the maximum observed NetLiquidation so peak equity cannot
regress after a process restart.  A different receipt within one trading date,
an older trading date, or a new baseline below the locally retained prior peak
fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from threading import RLock
from typing import Callable, Mapping, Protocol

from ..calendar import ExchangeCalendar, NEW_YORK
from ..policy import IBKR_RISK_HIGH_WATER_LEDGER_RELATIVE_PATH, canonical_json
from ..provider_clients import KeychainItem
from ..risk_evidence_binding import risk_high_water_receipt_hash, daily_starting_equity_receipt_hash
from .base import AccountSnapshot, OrderCoverageContract


IBKR_DAILY_RISK_BASELINE_SCHEMA = (
    "titan_ibkr_daily_risk_baseline_2026-09-14_v1"
)
IBKR_DAILY_STARTING_EQUITY_BASELINE_SCHEMA = "titan_ibkr_daily_starting_equity_risk_baseline_2026-09-14_v1"
_APPLICATION_ID = 0x54495242  # TIRB: Titan IBKR risk baseline ledger.
_SCHEMA_VERSION = 4
_MAX_BASELINE_BYTES = 64 * 1024
_CLOCK_SKEW = timedelta(seconds=2)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ACCOUNT_KEY = re.compile(r"ibkr-live-ending-([0-9]{4})\Z")
_ACCOUNT_MASK = re.compile(r"(?:\*{4}|•{4})([0-9]{4})\Z")
_SOURCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{2,159}\Z")
_PLAIN_DECIMAL = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")
_ERROR = re.compile(r"IBKR_RISK_EVIDENCE_[A-Z0-9_]{1,96}\Z")
_VERIFIED_BASELINE_SEAL = object()

_TOP_LEVEL_FIELDS = frozenset(
    {"schema_version", "issued_at", "bindings", "evidence", "hmac_sha256"}
)
_BINDING_FIELDS = frozenset(
    {
        "release_manifest_hash",
        "config_hash",
        "policy_binding_id",
        "risk_binding_id",
        "account_key",
        "account_masked",
        "account_binding_fingerprint",
        "valid_for_trading_date",
        "prior_trading_date",
    }
)
_EVIDENCE_FIELDS = frozenset(
    {
        "currency",
        "week_to_date_realized_pnl_through_prior_trading_day",
        "prior_high_water_equity",
        "broker_authoritative",
        "account_scope",
        "provider_source",
        "provider_observed_at",
        "provider_receipt_sha256",
    }
)
_DAILY_EQUITY_FIELDS = frozenset({
    "daily_starting_equity", "daily_starting_equity_as_of",
    "daily_starting_equity_provider_receipt_sha256", "daily_external_cash_flow",
    "daily_external_cash_flow_as_of", "daily_external_cash_flow_provider_receipt_sha256",
    "valuation_total_equity", "valuation_observed_at",
})
_FLOW_FIELDS = frozenset({
    "daily_external_cash_flow", "daily_external_cash_flow_as_of",
    "daily_external_cash_flow_provider_receipt_sha256", "valuation_total_equity", "valuation_observed_at",
})


class IbkrRiskEvidenceError(RuntimeError):
    """Stable, redacted failure from baseline or high-water processing."""

    def __init__(self, code: str) -> None:
        normalized = str(code).strip().upper()
        if _ERROR.fullmatch(normalized) is None:
            raise ValueError("invalid IBKR risk-evidence error code")
        self.code = normalized
        super().__init__(normalized)


def _failure(code: str) -> IbkrRiskEvidenceError:
    return IbkrRiskEvidenceError(f"IBKR_RISK_EVIDENCE_{code}")


def _hash(value: object, field_name: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise _failure(f"{field_name.upper()}_INVALID")
    return value


def _date(value: object, field_name: str) -> date:
    if type(value) is not str:
        raise _failure(f"{field_name.upper()}_INVALID")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise _failure(f"{field_name.upper()}_INVALID") from None
    if parsed.isoformat() != value:
        raise _failure(f"{field_name.upper()}_INVALID")
    return parsed


def _time(value: object, field_name: str) -> datetime:
    if type(value) is not str:
        raise _failure(f"{field_name.upper()}_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _failure(f"{field_name.upper()}_INVALID") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _failure(f"{field_name.upper()}_INVALID")
    return parsed.astimezone(timezone.utc)


def _now(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise _failure("CLOCK_INVALID")
    return value.astimezone(timezone.utc)


def _decimal(value: object, field_name: str, *, positive: bool = False) -> Decimal:
    if type(value) is not str or _PLAIN_DECIMAL.fullmatch(value) is None:
        raise _failure(f"{field_name.upper()}_INVALID")
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError):
        raise _failure(f"{field_name.upper()}_INVALID") from None
    if (
        not parsed.is_finite()
        or (positive and parsed <= 0)
        or len(parsed.as_tuple().digits) > 40
        or abs(parsed.as_tuple().exponent) > 40
    ):
        raise _failure(f"{field_name.upper()}_INVALID")
    # Alternate zero spellings would give one fact multiple wire identities.
    # Trailing fractional zeroes remain meaningful provider precision.
    if parsed == 0 and value.startswith("-"):
        raise _failure(f"{field_name.upper()}_INVALID")
    return parsed


def _stored_decimal(value: object, field_name: str, *, positive: bool = False) -> Decimal:
    if not isinstance(value, str):
        raise _failure("LEDGER_CORRUPT")
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError):
        raise _failure("LEDGER_CORRUPT") from None
    if (
        not parsed.is_finite()
        or (positive and parsed <= 0)
        or _decimal_text(parsed) != value
    ):
        raise _failure("LEDGER_CORRUPT")
    return parsed


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if value == 0 else text


def _mapping(value: object, fields: frozenset[str], field_name: str) -> Mapping[str, object]:
    if type(value) is not dict or set(value) != fields:
        raise _failure(f"{field_name.upper()}_FIELDS_INVALID")
    return value


@dataclass(frozen=True)
class IbkrRiskLedgerBindings:
    """Stable, non-secret identity permanently assigned to one local ledger."""

    release_manifest_hash: str
    config_hash: str
    policy_binding_id: str
    risk_binding_id: str
    account_key: str
    account_masked: str
    account_binding_fingerprint: str

    def __post_init__(self) -> None:
        for name in (
            "release_manifest_hash",
            "config_hash",
            "policy_binding_id",
            "risk_binding_id",
            "account_binding_fingerprint",
        ):
            _hash(getattr(self, name), name)
        key = (
            _ACCOUNT_KEY.fullmatch(self.account_key)
            if type(self.account_key) is str
            else None
        )
        mask = (
            _ACCOUNT_MASK.fullmatch(self.account_masked)
            if type(self.account_masked) is str
            else None
        )
        if key is None or mask is None or key.group(1) != mask.group(1):
            raise _failure("ACCOUNT_BINDING_INVALID")

    def as_tuple(self) -> tuple[str, ...]:
        return (
            self.release_manifest_hash,
            self.config_hash,
            self.policy_binding_id,
            self.risk_binding_id,
            self.account_key,
            self.account_masked,
            self.account_binding_fingerprint,
        )


@dataclass(frozen=True)
class IbkrRiskEvidenceBindings:
    """Expected identity and exact session dates for one daily baseline."""

    release_manifest_hash: str
    config_hash: str
    policy_binding_id: str
    risk_binding_id: str
    account_key: str
    account_masked: str
    account_binding_fingerprint: str
    valid_for_trading_date: date
    prior_trading_date: date

    def __post_init__(self) -> None:
        IbkrRiskLedgerBindings(
            release_manifest_hash=self.release_manifest_hash,
            config_hash=self.config_hash,
            policy_binding_id=self.policy_binding_id,
            risk_binding_id=self.risk_binding_id,
            account_key=self.account_key,
            account_masked=self.account_masked,
            account_binding_fingerprint=self.account_binding_fingerprint,
        )
        if (
            type(self.valid_for_trading_date) is not date
            or type(self.prior_trading_date) is not date
            or self.prior_trading_date >= self.valid_for_trading_date
        ):
            raise _failure("TRADING_DATE_BINDING_INVALID")

    @property
    def ledger_bindings(self) -> IbkrRiskLedgerBindings:
        return IbkrRiskLedgerBindings(
            release_manifest_hash=self.release_manifest_hash,
            config_hash=self.config_hash,
            policy_binding_id=self.policy_binding_id,
            risk_binding_id=self.risk_binding_id,
            account_key=self.account_key,
            account_masked=self.account_masked,
            account_binding_fingerprint=self.account_binding_fingerprint,
        )


class IbkrDailyRiskBindingProvider:
    """Derive today's exact receipt dates from release-shipped NYSE evidence."""

    def __init__(
        self,
        *,
        ledger_bindings: IbkrRiskLedgerBindings,
        calendar: ExchangeCalendar,
    ) -> None:
        if type(ledger_bindings) is not IbkrRiskLedgerBindings:
            raise _failure("LEDGER_BINDINGS_INVALID")
        if not isinstance(calendar, ExchangeCalendar):
            raise _failure("TRADING_CALENDAR_INVALID")
        self.ledger_bindings = ledger_bindings
        self._calendar = calendar

    def release_components(self) -> tuple[tuple[str, object, tuple[str, ...]], ...]:
        return (
            (
                "ibkr_risk_trading_calendar",
                self._calendar,
                ("is_trading_day",),
            ),
        )

    def __call__(self, trading_date: date) -> IbkrRiskEvidenceBindings:
        if type(trading_date) is not date:
            raise _failure("TRADING_DATE_INVALID")
        try:
            if not self._calendar.is_trading_day(trading_date):
                raise _failure("TRADING_DATE_NOT_VERIFIED")
            prior = trading_date - timedelta(days=1)
            # A year-bounded calendar or unexpectedly long closure must fail
            # rather than silently treating a weekday as an exchange session.
            for _attempt in range(10):
                if self._calendar.is_trading_day(prior):
                    break
                prior -= timedelta(days=1)
            else:
                raise _failure("PRIOR_TRADING_DATE_UNAVAILABLE")
        except IbkrRiskEvidenceError:
            raise
        except Exception:
            raise _failure("TRADING_CALENDAR_UNAVAILABLE") from None
        stable = self.ledger_bindings
        return IbkrRiskEvidenceBindings(
            release_manifest_hash=stable.release_manifest_hash,
            config_hash=stable.config_hash,
            policy_binding_id=stable.policy_binding_id,
            risk_binding_id=stable.risk_binding_id,
            account_key=stable.account_key,
            account_masked=stable.account_masked,
            account_binding_fingerprint=stable.account_binding_fingerprint,
            valid_for_trading_date=trading_date,
            prior_trading_date=prior,
        )


@dataclass(frozen=True)
class VerifiedIbkrDailyRiskBaseline:
    """Immutable baseline that can only be built by this module's verifier."""

    bindings: IbkrRiskEvidenceBindings
    issued_at: datetime
    provider_observed_at: datetime
    week_to_date_realized_pnl_through_prior_trading_day: Decimal
    prior_high_water_equity: Decimal
    currency: str
    provider_source: str
    provider_receipt_sha256: str
    receipt_hash: str
    verified_at: datetime
    daily_starting_equity: Decimal | None = None
    daily_starting_equity_as_of: datetime | None = None
    daily_starting_equity_provider_receipt_sha256: str | None = None
    daily_external_cash_flow: Decimal | None = None
    daily_external_cash_flow_as_of: datetime | None = None
    daily_external_cash_flow_receipt_hash: str | None = None
    valuation_total_equity: Decimal | None = None
    valuation_observed_at: datetime | None = None
    _verification_seal: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._verification_seal is not _VERIFIED_BASELINE_SEAL:
            raise _failure("UNVERIFIED_BASELINE_CONSTRUCTION")


class RiskBaselineKeyReader(Protocol):
    def read(self, item: KeychainItem) -> bytes: ...


def _read_canonical_private_baseline(path: str | Path) -> bytes:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise _failure("BASELINE_FILE_PATH_UNSAFE")
    try:
        if candidate.resolve(strict=True) != candidate:
            raise _failure("BASELINE_FILE_PATH_UNSAFE")
        before = candidate.lstat()
    except IbkrRiskEvidenceError:
        raise
    except OSError:
        raise _failure("BASELINE_FILE_UNAVAILABLE") from None
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or stat.S_IMODE(before.st_mode) not in {0o400, 0o600}
        or before.st_size <= 0
        or before.st_size > _MAX_BASELINE_BYTES
        or (hasattr(os, "geteuid") and before.st_uid != os.geteuid())
    ):
        raise _failure("BASELINE_FILE_UNSAFE")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(candidate, flags)
    except OSError:
        raise _failure("BASELINE_FILE_UNAVAILABLE") from None
    try:
        opened = os.fstat(descriptor)
        if (
            (opened.st_dev, opened.st_ino, opened.st_size)
            != (before.st_dev, before.st_ino, before.st_size)
            or stat.S_IMODE(opened.st_mode) not in {0o400, 0o600}
        ):
            raise _failure("BASELINE_FILE_CHANGED")
        chunks: list[bytes] = []
        remaining = _MAX_BASELINE_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(8192, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            len(encoded) != before.st_size
            or len(encoded) > _MAX_BASELINE_BYTES
            or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        ):
            raise _failure("BASELINE_FILE_CHANGED")
        return encoded
    finally:
        os.close(descriptor)


def load_verified_ibkr_daily_risk_baseline(
    path: str | Path,
    *,
    secret: bytes,
    expected: IbkrRiskEvidenceBindings,
    now: datetime,
    required_schema: str | None = None,
) -> VerifiedIbkrDailyRiskBaseline:
    """Authenticate one externally produced baseline; never generate it."""

    if type(secret) is not bytes or len(secret) < 32:
        raise _failure("HMAC_KEY_INVALID")
    if type(expected) is not IbkrRiskEvidenceBindings:
        raise _failure("EXPECTED_BINDINGS_INVALID")
    if required_schema not in {None, IBKR_DAILY_RISK_BASELINE_SCHEMA, IBKR_DAILY_STARTING_EQUITY_BASELINE_SCHEMA}:
        raise _failure("REQUIRED_SCHEMA_INVALID")
    verified_at = _now(now)
    encoded = _read_canonical_private_baseline(path)
    try:
        raw = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _failure("BASELINE_JSON_INVALID") from None
    if type(raw) is not dict or set(raw) != _TOP_LEVEL_FIELDS:
        raise _failure("BASELINE_FIELDS_INVALID")
    starting_equity_schema = raw.get("schema_version") == IBKR_DAILY_STARTING_EQUITY_BASELINE_SCHEMA
    if raw.get("schema_version") not in {IBKR_DAILY_RISK_BASELINE_SCHEMA, IBKR_DAILY_STARTING_EQUITY_BASELINE_SCHEMA}:
        raise _failure("BASELINE_SCHEMA_INVALID")
    if required_schema is not None and raw["schema_version"] != required_schema:
        raise _failure("BASELINE_SCHEMA_BINDING_MISMATCH")
    canonical = (canonical_json(raw) + "\n").encode("utf-8")
    if encoded != canonical:
        raise _failure("BASELINE_FILE_NOT_CANONICAL")
    supplied = raw.get("hmac_sha256")
    if type(supplied) is not str or _SHA256.fullmatch(supplied) is None:
        raise _failure("HMAC_INVALID")
    body = dict(raw)
    body.pop("hmac_sha256", None)
    calculated = hmac.new(
        secret,
        canonical_json(body).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(supplied, calculated):
        raise _failure("HMAC_INVALID")

    bindings = _mapping(raw["bindings"], _BINDING_FIELDS, "baseline_bindings")
    observed = IbkrRiskEvidenceBindings(
        release_manifest_hash=bindings["release_manifest_hash"],
        config_hash=bindings["config_hash"],
        policy_binding_id=bindings["policy_binding_id"],
        risk_binding_id=bindings["risk_binding_id"],
        account_key=bindings["account_key"],
        account_masked=bindings["account_masked"],
        account_binding_fingerprint=bindings["account_binding_fingerprint"],
        valid_for_trading_date=_date(
            bindings["valid_for_trading_date"], "valid_for_trading_date"
        ),
        prior_trading_date=_date(
            bindings["prior_trading_date"], "prior_trading_date"
        ),
    )
    if observed != expected:
        raise _failure("BINDING_MISMATCH")
    if verified_at.astimezone(NEW_YORK).date() != expected.valid_for_trading_date:
        raise _failure("TRADING_DATE_MISMATCH")

    evidence = _mapping(raw["evidence"], _EVIDENCE_FIELDS | (_DAILY_EQUITY_FIELDS if starting_equity_schema else frozenset()), "evidence")
    if evidence["currency"] != "USD":
        raise _failure("CURRENCY_UNSUPPORTED")
    if evidence["broker_authoritative"] is not True:
        raise _failure("BROKER_AUTHORITY_UNPROVEN")
    if evidence["account_scope"] != "exact_account":
        raise _failure("ACCOUNT_SCOPE_UNPROVEN")
    source = evidence["provider_source"]
    if type(source) is not str or _SOURCE.fullmatch(source) is None:
        raise _failure("PROVIDER_SOURCE_INVALID")
    provider_receipt = _hash(
        evidence["provider_receipt_sha256"], "provider_receipt_sha256"
    )
    issued_at = _time(raw["issued_at"], "issued_at")
    provider_observed_at = _time(
        evidence["provider_observed_at"], "provider_observed_at"
    )
    if (
        issued_at > verified_at + _CLOCK_SKEW
        or provider_observed_at > issued_at
        or provider_observed_at > verified_at + _CLOCK_SKEW
        or provider_observed_at.astimezone(NEW_YORK).date()
        < expected.prior_trading_date
        or provider_observed_at.astimezone(NEW_YORK).date()
        > expected.valid_for_trading_date
        or issued_at.astimezone(NEW_YORK).date()
        < expected.prior_trading_date
        or issued_at.astimezone(NEW_YORK).date()
        > expected.valid_for_trading_date
    ):
        raise _failure("BASELINE_TIME_INVALID")
    week_to_date = _decimal(
        evidence["week_to_date_realized_pnl_through_prior_trading_day"],
        "week_to_date_realized_pnl",
    )
    prior_high_water = _decimal(
        evidence["prior_high_water_equity"],
        "prior_high_water_equity",
        positive=True,
    )
    envelope_hash = hashlib.sha256(
        canonical_json(body).encode("utf-8")
    ).hexdigest()
    daily = {}
    receipt_hash = envelope_hash
    if starting_equity_schema:
        starting_as_of = _time(evidence["daily_starting_equity_as_of"], "daily_starting_equity_as_of")
        expected_start = datetime.combine(expected.valid_for_trading_date, datetime.min.time(), NEW_YORK)
        flow_as_of = _time(evidence["daily_external_cash_flow_as_of"], "daily_external_cash_flow_as_of")
        valuation_as_of = _time(evidence["valuation_observed_at"], "valuation_observed_at")
        if (
            starting_as_of != expected_start
            or flow_as_of != valuation_as_of
            or flow_as_of.astimezone(NEW_YORK).date() != expected.valid_for_trading_date
            or not timedelta(0) <= verified_at - flow_as_of <= timedelta(seconds=5)
            or issued_at < flow_as_of or issued_at > verified_at
        ):
            raise _failure("DAILY_STARTING_EQUITY_TIME_UNPROVEN")
        daily = {
            "daily_starting_equity": _decimal(evidence["daily_starting_equity"], "daily_starting_equity", positive=True),
            "daily_starting_equity_as_of": starting_as_of,
            "daily_starting_equity_provider_receipt_sha256": _hash(evidence["daily_starting_equity_provider_receipt_sha256"], "starting_equity_provider_receipt"),
            "daily_external_cash_flow": _decimal(evidence["daily_external_cash_flow"], "daily_external_cash_flow"),
            "daily_external_cash_flow_as_of": flow_as_of,
            "daily_external_cash_flow_receipt_hash": envelope_hash,
            "valuation_total_equity": _decimal(evidence["valuation_total_equity"], "valuation_total_equity"),
            "valuation_observed_at": valuation_as_of,
        }
        _hash(evidence["daily_external_cash_flow_provider_receipt_sha256"], "cash_flow_provider_receipt")
        # Rotating current-flow evidence must not rotate the frozen baseline.
        immutable_body = {"schema_version": body["schema_version"], "bindings": body["bindings"],
                          "evidence": {key: value for key, value in evidence.items() if key not in _FLOW_FIELDS}}
        receipt_hash = hashlib.sha256(canonical_json(immutable_body).encode("utf-8")).hexdigest()
    return VerifiedIbkrDailyRiskBaseline(
        bindings=observed,
        issued_at=issued_at,
        provider_observed_at=provider_observed_at,
        week_to_date_realized_pnl_through_prior_trading_day=week_to_date,
        prior_high_water_equity=prior_high_water,
        currency="USD",
        provider_source=source,
        provider_receipt_sha256=provider_receipt,
        receipt_hash=receipt_hash,
        verified_at=verified_at,
        _verification_seal=_VERIFIED_BASELINE_SEAL,
        **daily,
    )


class DailyIbkrRiskBaselineAuthenticator:
    """Re-read the private key and authenticate the exact daily input."""

    def __init__(
        self,
        *,
        path: Path,
        key_reader: RiskBaselineKeyReader,
        key_item: KeychainItem,
        expected: IbkrRiskEvidenceBindings | IbkrDailyRiskBindingProvider,
        clock: Callable[[], datetime],
        required_schema: str | None = None,
    ) -> None:
        if not callable(getattr(key_reader, "read", None)):
            raise _failure("KEY_READER_INVALID")
        if not isinstance(key_item, KeychainItem):
            raise _failure("KEY_ITEM_INVALID")
        if type(expected) not in (
            IbkrRiskEvidenceBindings,
            IbkrDailyRiskBindingProvider,
        ):
            raise _failure("EXPECTED_BINDINGS_INVALID")
        if not callable(clock):
            raise _failure("CLOCK_INVALID")
        if required_schema not in {None, IBKR_DAILY_RISK_BASELINE_SCHEMA, IBKR_DAILY_STARTING_EQUITY_BASELINE_SCHEMA}:
            raise _failure("REQUIRED_SCHEMA_INVALID")
        self.path = Path(path)
        self.key_reader = key_reader
        self.key_item = key_item
        self.expected = expected
        self._clock = clock
        self.required_schema = required_schema

    def release_components(self) -> tuple[tuple[str, object, tuple[str, ...]], ...]:
        result: tuple[tuple[str, object, tuple[str, ...]], ...] = (
            ("ibkr_risk_baseline_key_loader", self.key_reader, ("read",)),
            ("ibkr_risk_baseline_clock", self._clock, ("__call__",)),
        )
        if isinstance(self.expected, IbkrDailyRiskBindingProvider):
            result += (
                (
                    "ibkr_daily_risk_binding_provider",
                    self.expected,
                    ("release_components", "__call__"),
                ),
            )
        return result

    @property
    def ledger_bindings(self) -> IbkrRiskLedgerBindings:
        if isinstance(self.expected, IbkrDailyRiskBindingProvider):
            return self.expected.ledger_bindings
        return self.expected.ledger_bindings

    def __call__(self) -> VerifiedIbkrDailyRiskBaseline:
        try:
            secret = self.key_reader.read(self.key_item)
        except Exception:
            raise _failure("KEY_UNAVAILABLE") from None
        if type(secret) is not bytes or len(secret) < 32:
            secret = b""
            raise _failure("HMAC_KEY_INVALID")
        try:
            current = _now(self._clock())
            expected = (
                self.expected(current.astimezone(NEW_YORK).date())
                if isinstance(self.expected, IbkrDailyRiskBindingProvider)
                else self.expected
            )
            return load_verified_ibkr_daily_risk_baseline(
                self.path,
                secret=secret,
                expected=expected,
                now=current,
                required_schema=self.required_schema,
            )
        except IbkrRiskEvidenceError:
            raise
        except Exception:
            raise _failure("BASELINE_REJECTED") from None
        finally:
            # Immutable bytes cannot be zeroized in place; never retain them.
            secret = b""


@dataclass(frozen=True)
class IbkrRiskHighWaterRecord:
    trading_date: date
    peak_equity: Decimal
    last_net_liquidation: Decimal
    baseline_receipt_hash: str
    observed_at: datetime
    identity_hash: str
    lineage_hash: str
    receipt_hash: str


def _baseline_identity_hash(bindings: IbkrRiskEvidenceBindings) -> str:
    return hashlib.sha256(
        canonical_json(
            {
                "schema_version": "titan_ibkr_risk_baseline_identity_2026-09-14_v1",
                "bindings": {
                    "release_manifest_hash": bindings.release_manifest_hash,
                    "config_hash": bindings.config_hash,
                    "policy_binding_id": bindings.policy_binding_id,
                    "risk_binding_id": bindings.risk_binding_id,
                    "account_key": bindings.account_key,
                    "account_masked": bindings.account_masked,
                    "account_binding_fingerprint": (
                        bindings.account_binding_fingerprint
                    ),
                    "valid_for_trading_date": (
                        bindings.valid_for_trading_date.isoformat()
                    ),
                    "prior_trading_date": bindings.prior_trading_date.isoformat(),
                },
            }
        ).encode("utf-8")
    ).hexdigest()


def _high_water_identity_hash(
    *, baseline: VerifiedIbkrDailyRiskBaseline, lineage_hash: str
) -> str:
    lineage = _hash(lineage_hash, "ledger_lineage_hash")
    bindings = baseline.bindings.ledger_bindings
    return hashlib.sha256(
        canonical_json(
            {
                "schema_version": "titan_ibkr_risk_high_water_identity_2026-09-14_v2",
                "ledger_bindings": {
                    "release_manifest_hash": bindings.release_manifest_hash,
                    "config_hash": bindings.config_hash,
                    "policy_binding_id": bindings.policy_binding_id,
                    "risk_binding_id": bindings.risk_binding_id,
                    "account_key": bindings.account_key,
                    "account_masked": bindings.account_masked,
                    "account_binding_fingerprint": (
                        bindings.account_binding_fingerprint
                    ),
                },
                "ledger_lineage_hash": lineage,
                "trading_date": baseline.bindings.valid_for_trading_date.isoformat(),
                "baseline_receipt_hash": baseline.receipt_hash,
            }
        ).encode("utf-8")
    ).hexdigest()


def _high_water_receipt_hash(
    *,
    identity_hash: str,
    baseline_receipt_hash: str,
    lineage_hash: str,
    peak_equity: Decimal,
) -> str:
    try:
        return risk_high_water_receipt_hash(
            identity_hash=identity_hash,
            baseline_receipt_hash=baseline_receipt_hash,
            lineage_hash=lineage_hash,
            peak_equity=peak_equity,
        )
    except ValueError:
        raise _failure("HIGH_WATER_RECEIPT_INVALID") from None


class IbkrRiskHighWaterLedger:
    """Separate SQLite high-water ledger bound to one complete risk identity.

    A release upgrade never rewrites that identity in place.  The PAUSED
    installer archives the prior release-scoped ledger and creates a new one
    with one immutable ``carry_forward`` row and retained daily-start rows.  The carry row is an equity floor,
    not a synthetic daily observation: it lets a same-session upgrade retain
    today's intraday peak while the new release authenticates its own daily
    baseline receipt.
    """

    def __init__(
        self,
        path: Path,
        *,
        bindings: IbkrRiskLedgerBindings,
        allow_create: bool = False,
    ) -> None:
        if type(bindings) is not IbkrRiskLedgerBindings:
            raise _failure("LEDGER_BINDINGS_INVALID")
        candidate = Path(path)
        if not candidate.is_absolute() or not candidate.parent.is_dir():
            raise _failure("LEDGER_PATH_UNSAFE")
        try:
            parent = candidate.parent.resolve(strict=True)
        except OSError:
            raise _failure("LEDGER_PATH_UNSAFE") from None
        candidate = parent / candidate.name
        if candidate.is_symlink():
            raise _failure("LEDGER_PATH_UNSAFE")
        self._validate_sidecars(candidate)
        if not candidate.exists() and any(
            Path(str(candidate) + suffix).exists() for suffix in ("-wal", "-shm")
        ):
            raise _failure("LEDGER_ORPHANED_SIDECAR")
        if not isinstance(allow_create, bool):
            raise _failure("LEDGER_CREATE_AUTHORITY_INVALID")
        created = False
        if candidate.exists():
            self._preflight_existing(candidate, bindings)
        else:
            if not allow_create:
                raise _failure("LEDGER_MISSING")
            try:
                descriptor = os.open(
                    candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                )
            except FileExistsError:
                self._preflight_existing(candidate, bindings)
            except OSError:
                raise _failure("LEDGER_UNAVAILABLE") from None
            else:
                os.close(descriptor)
                created = True
        self.path = candidate
        self.bindings = bindings
        self._lineage_hash = os.urandom(32).hex() if created else ""
        self._unavailable_error_code: str | None = None
        self._lock = RLock()
        self._closed = False
        try:
            self._db = sqlite3.connect(
                candidate.as_uri() + "?mode=rw",
                uri=True,
                timeout=10,
                isolation_level=None,
                check_same_thread=False,
            )
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            if created:
                self._initialize()
            else:
                self._validate_open_binding()
        except IbkrRiskEvidenceError:
            if hasattr(self, "_db"):
                self._db.close()
            raise
        except (OSError, sqlite3.Error):
            if hasattr(self, "_db"):
                self._db.close()
            raise _failure("LEDGER_UNAVAILABLE") from None

    @classmethod
    def fail_closed(
        cls,
        path: Path,
        *,
        bindings: IbkrRiskLedgerBindings,
        error: IbkrRiskEvidenceError,
    ) -> "IbkrRiskHighWaterLedger":
        """Return an inert, release-profile-stable ledger that cannot observe."""

        if type(bindings) is not IbkrRiskLedgerBindings:
            raise _failure("LEDGER_BINDINGS_INVALID")
        if not isinstance(error, IbkrRiskEvidenceError):
            raise _failure("LEDGER_UNAVAILABLE")
        ledger = object.__new__(cls)
        ledger.path = Path(path)
        ledger.bindings = bindings
        ledger._lineage_hash = ""
        ledger._unavailable_error_code = error.code
        ledger._lock = RLock()
        ledger._closed = False
        return ledger

    def release_components(self) -> tuple[tuple[str, object, tuple[str, ...]], ...]:
        # SQLite is a standard-library data store, not an injected executable
        # provider.  The ledger has no retained executable dependencies.
        return ()

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                database = getattr(self, "_db", None)
                if database is not None:
                    database.close()
                self._closed = True

    def __enter__(self) -> "IbkrRiskHighWaterLedger":
        return self

    def __exit__(self, *_unused: object) -> None:
        self.close()

    @staticmethod
    def _validate_sidecars(path: Path) -> None:
        journal = Path(str(path) + "-journal")
        if journal.exists() or journal.is_symlink():
            raise _failure("LEDGER_RECOVERY_REQUIRED")
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(path) + suffix)
            if sidecar.is_symlink():
                raise _failure("LEDGER_SIDECAR_UNSAFE")
            if sidecar.exists():
                metadata = sidecar.stat()
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise _failure("LEDGER_SIDECAR_UNSAFE")

    @classmethod
    def _preflight_existing(
        cls, path: Path, bindings: IbkrRiskLedgerBindings
    ) -> None:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags)
        except OSError:
            raise _failure("LEDGER_UNAVAILABLE") from None
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or (hasattr(os, "geteuid") and metadata.st_uid != os.geteuid())
            ):
                raise _failure("LEDGER_PATH_UNSAFE")
            header = os.read(descriptor, 100)
        finally:
            os.close(descriptor)
        if (
            len(header) != 100
            or header[:16] != b"SQLite format 3\x00"
            or int.from_bytes(header[68:72], "big") != _APPLICATION_ID
            or int.from_bytes(header[60:64], "big") != _SCHEMA_VERSION
        ):
            raise _failure("LEDGER_IDENTITY_INVALID")
        cls._validate_sidecars(path)
        try:
            readonly = sqlite3.connect(
                path.as_uri() + "?mode=ro&immutable=1", uri=True
            )
            row = readonly.execute(
                "SELECT release_manifest_hash,config_hash,policy_binding_id,"
                "risk_binding_id,account_key,account_masked,"
                "account_binding_fingerprint,lineage_hash FROM binding "
                "WHERE singleton=1"
            ).fetchone()
        except sqlite3.Error:
            raise _failure("LEDGER_IDENTITY_INVALID") from None
        finally:
            if "readonly" in locals():
                readonly.close()
        if (
            row is None
            or tuple(row[:-1]) != bindings.as_tuple()
            or _SHA256.fullmatch(str(row[-1])) is None
        ):
            raise _failure("LEDGER_BINDING_MISMATCH")
        after = path.stat()
        if (
            after.st_dev,
            after.st_ino,
            after.st_nlink,
            after.st_size,
        ) != (metadata.st_dev, metadata.st_ino, 1, metadata.st_size):
            raise _failure("LEDGER_FILE_CHANGED")

    def _initialize(self) -> None:
        try:
            self._db.execute("BEGIN IMMEDIATE")
            self._db.execute(
                "CREATE TABLE binding ("
                "singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
                "release_manifest_hash TEXT NOT NULL,config_hash TEXT NOT NULL,"
                "policy_binding_id TEXT NOT NULL,risk_binding_id TEXT NOT NULL,"
                "account_key TEXT NOT NULL,account_masked TEXT NOT NULL,"
                "account_binding_fingerprint TEXT NOT NULL,"
                "lineage_hash TEXT NOT NULL,"
                "latest_trading_date TEXT,highest_equity TEXT)"
            )
            self._db.execute(
                "CREATE TABLE daily_high_water ("
                "trading_date TEXT PRIMARY KEY,"
                "baseline_receipt_hash TEXT NOT NULL,"
                "baseline_provider_receipt_sha256 TEXT NOT NULL,"
                "baseline_prior_high_water_equity TEXT NOT NULL,"
                "peak_equity TEXT NOT NULL,last_net_liquidation TEXT NOT NULL,"
                "last_observed_at TEXT NOT NULL)"
            )
            self._db.execute(
                "CREATE TABLE daily_starting_equity (trading_date TEXT PRIMARY KEY,"
                "starting_equity TEXT NOT NULL,starting_equity_as_of TEXT NOT NULL,"
                "starting_equity_provider_receipt_sha256 TEXT NOT NULL,"
                "latest_external_cash_flow TEXT NOT NULL,latest_external_cash_flow_as_of TEXT NOT NULL,"
                "latest_external_cash_flow_receipt_sha256 TEXT NOT NULL)"
            )
            self._db.execute(
                "CREATE TABLE carry_forward ("
                "singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
                "source_release_manifest_hash TEXT NOT NULL,"
                "source_config_hash TEXT NOT NULL,"
                "source_policy_binding_id TEXT NOT NULL,"
                "source_risk_binding_id TEXT NOT NULL,"
                "source_account_key TEXT NOT NULL,"
                "source_account_masked TEXT NOT NULL,"
                "source_account_binding_fingerprint TEXT NOT NULL,"
                "source_latest_trading_date TEXT NOT NULL,"
                "source_highest_equity TEXT NOT NULL,"
                "source_ledger_sha256 TEXT NOT NULL,"
                "archive_relative_path TEXT NOT NULL,"
                "migrated_at TEXT NOT NULL)"
            )
            self._db.execute(
                "INSERT INTO binding VALUES (1,?,?,?,?,?,?,?,?,NULL,NULL)",
                (*self.bindings.as_tuple(), self._lineage_hash),
            )
            self._db.execute(f"PRAGMA application_id={_APPLICATION_ID}")
            self._db.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
            self._db.execute("COMMIT")
        except Exception:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise
        checkpoint = self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if tuple(checkpoint or ()) != (0, 0, 0):
            raise _failure("LEDGER_CHECKPOINT_FAILED")

    def _validate_open_binding(self) -> None:
        try:
            if self._db.execute("PRAGMA application_id").fetchone()[0] != _APPLICATION_ID:
                raise _failure("LEDGER_IDENTITY_INVALID")
            if self._db.execute("PRAGMA user_version").fetchone()[0] != _SCHEMA_VERSION:
                raise _failure("LEDGER_IDENTITY_INVALID")
            rows = self._db.execute(
                "SELECT release_manifest_hash,config_hash,policy_binding_id,"
                "risk_binding_id,account_key,account_masked,"
                "account_binding_fingerprint,lineage_hash FROM binding WHERE singleton=1"
            ).fetchall()
        except sqlite3.Error:
            raise _failure("LEDGER_CORRUPT") from None
        if (
            len(rows) != 1
            or tuple(rows[0][:-1]) != self.bindings.as_tuple()
            or _SHA256.fullmatch(str(rows[0][-1])) is None
        ):
            raise _failure("LEDGER_BINDING_MISMATCH")
        self._lineage_hash = str(rows[0][-1])

    def _carry_forward_floor(self) -> tuple[date, Decimal] | None:
        """Return and validate the installer's immutable release carry floor."""

        try:
            rows = self._db.execute("SELECT * FROM carry_forward").fetchall()
        except sqlite3.Error:
            raise _failure("LEDGER_CORRUPT") from None
        if not rows:
            return None
        if len(rows) != 1:
            raise _failure("LEDGER_CORRUPT")
        row = rows[0]
        if row["singleton"] != 1:
            raise _failure("LEDGER_CORRUPT")
        source_hashes = (
            row["source_release_manifest_hash"],
            row["source_config_hash"],
            row["source_policy_binding_id"],
            row["source_risk_binding_id"],
            row["source_account_binding_fingerprint"],
            row["source_ledger_sha256"],
        )
        if any(
            type(value) is not str or _SHA256.fullmatch(value) is None
            for value in source_hashes
        ):
            raise _failure("LEDGER_CORRUPT")
        if (
            row["source_account_key"] != self.bindings.account_key
            or row["source_account_masked"] != self.bindings.account_masked
            or row["source_account_binding_fingerprint"]
            != self.bindings.account_binding_fingerprint
        ):
            raise _failure("LEDGER_BINDING_MISMATCH")
        archive = row["archive_relative_path"]
        if (
            type(archive) is not str
            or not re.fullmatch(
                r"state/ibkr-risk-high-water-archive/"
                r"[0-9a-f]{64}-[0-9a-f]{64}\.sqlite3",
                archive,
            )
        ):
            raise _failure("LEDGER_CORRUPT")
        migrated_at = _time(
            row["migrated_at"], "carry_forward_migrated_at"
        )
        if row["migrated_at"] != migrated_at.isoformat():
            raise _failure("LEDGER_CORRUPT")
        return (
            _date(row["source_latest_trading_date"], "carry_forward_trading_date"),
            _stored_decimal(
                row["source_highest_equity"],
                "carry_forward_highest_equity",
                positive=True,
            ),
        )

    def observe(
        self,
        *,
        baseline: VerifiedIbkrDailyRiskBaseline,
        net_liquidation: Decimal,
        observed_at: datetime,
    ) -> IbkrRiskHighWaterRecord:
        """Durably advance, but never reduce, the account equity high water."""

        if self._unavailable_error_code is not None:
            raise IbkrRiskEvidenceError(self._unavailable_error_code)

        if (
            not isinstance(baseline, VerifiedIbkrDailyRiskBaseline)
            or baseline._verification_seal is not _VERIFIED_BASELINE_SEAL
        ):
            raise _failure("BASELINE_UNVERIFIED")
        if baseline.bindings.ledger_bindings != self.bindings:
            raise _failure("LEDGER_BINDING_MISMATCH")
        try:
            current = Decimal(net_liquidation)
        except (InvalidOperation, ValueError, TypeError):
            raise _failure("NET_LIQUIDATION_INVALID") from None
        if (
            isinstance(net_liquidation, (float, bool))
            or not current.is_finite()
            or (baseline.daily_starting_equity is None and current <= 0)
        ):
            raise _failure("NET_LIQUIDATION_INVALID")
        stamp = _now(observed_at)
        target = baseline.bindings.valid_for_trading_date
        if stamp.astimezone(NEW_YORK).date() != target:
            raise _failure("SNAPSHOT_TRADING_DATE_MISMATCH")
        target_text = target.isoformat()
        try:
            with self._lock:
                if self._closed:
                    raise _failure("LEDGER_CLOSED")
                self._db.execute("BEGIN IMMEDIATE")
                self._observe_daily_starting_equity(baseline)
                identity = self._db.execute(
                    "SELECT latest_trading_date,highest_equity FROM binding "
                    "WHERE singleton=1"
                ).fetchone()
                carry_forward = self._carry_forward_floor()
                daily_rows = self._db.execute(
                    "SELECT trading_date,peak_equity FROM daily_high_water "
                    "ORDER BY trading_date"
                ).fetchall()
                existing = self._db.execute(
                    "SELECT * FROM daily_high_water WHERE trading_date=?",
                    (target_text,),
                ).fetchone()
                if identity is None:
                    raise _failure("LEDGER_CORRUPT")
                latest_raw = identity["latest_trading_date"]
                historical_peak = (
                    None
                    if identity["highest_equity"] is None
                    else _stored_decimal(
                        identity["highest_equity"], "highest_equity", positive=True
                    )
                )
                retained_dates = tuple(row["trading_date"] for row in daily_rows)
                retained_peaks = tuple(
                    _stored_decimal(row["peak_equity"], "peak_equity", positive=True)
                    for row in daily_rows
                )
                if (
                    (latest_raw is None) != (historical_peak is None)
                    or (not daily_rows) != (latest_raw is None)
                    or (
                        daily_rows
                        and (
                            retained_dates[-1] != latest_raw
                            or max(retained_peaks) != historical_peak
                        )
                    )
                ):
                    raise _failure("LEDGER_CORRUPT")
                carry_date = (
                    None if carry_forward is None else carry_forward[0]
                )
                carry_peak = (
                    None if carry_forward is None else carry_forward[1]
                )
                effective_historical_peak = historical_peak
                if carry_peak is not None:
                    effective_historical_peak = (
                        carry_peak
                        if effective_historical_peak is None
                        else max(effective_historical_peak, carry_peak)
                    )
                    if target < carry_date:
                        raise _failure("LEDGER_DATE_REGRESSION")
                if latest_raw is not None:
                    latest = _date(latest_raw, "ledger_latest_trading_date")
                    if target < latest:
                        raise _failure("LEDGER_DATE_REGRESSION")
                    if target == latest and existing is None:
                        raise _failure("LEDGER_CORRUPT")
                if existing is None:
                    if latest_raw is not None and target_text <= latest_raw:
                        raise _failure("LEDGER_DATE_REGRESSION")
                    if (
                        effective_historical_peak is not None
                        and baseline.prior_high_water_equity
                        < effective_historical_peak
                        # A release may be upgraded during a session.  The
                        # signed daily baseline correctly covers only the
                        # prior session, while carry_forward includes today's
                        # already-observed intraday peak.
                        and (carry_date is None or target > carry_date)
                    ):
                        raise _failure("BASELINE_HIGH_WATER_REGRESSION")
                    peak = max(
                        baseline.prior_high_water_equity,
                        current,
                        *(
                            ()
                            if effective_historical_peak is None
                            else (effective_historical_peak,)
                        ),
                    )
                    self._db.execute(
                        "INSERT INTO daily_high_water VALUES (?,?,?,?,?,?,?)",
                        (
                            target_text,
                            baseline.receipt_hash,
                            baseline.provider_receipt_sha256,
                            _decimal_text(baseline.prior_high_water_equity),
                            _decimal_text(peak),
                            _decimal_text(current),
                            stamp.isoformat(timespec="microseconds"),
                        ),
                    )
                else:
                    if (
                        existing["baseline_receipt_hash"] != baseline.receipt_hash
                        or existing["baseline_provider_receipt_sha256"]
                        != baseline.provider_receipt_sha256
                        or _stored_decimal(
                            existing["baseline_prior_high_water_equity"],
                            "baseline_prior_high_water_equity",
                            positive=True,
                        )
                        != baseline.prior_high_water_equity
                    ):
                        raise _failure("BASELINE_RECEIPT_CHANGED")
                    prior_peak = _stored_decimal(
                        existing["peak_equity"], "peak_equity", positive=True
                    )
                    peak = max(
                        prior_peak, baseline.prior_high_water_equity, current
                    )
                    if peak < prior_peak:
                        raise _failure("HIGH_WATER_REGRESSION")
                    self._db.execute(
                        "UPDATE daily_high_water SET peak_equity=?,"
                        "last_net_liquidation=?,last_observed_at=? "
                        "WHERE trading_date=?",
                        (
                            _decimal_text(peak),
                            _decimal_text(current),
                            stamp.isoformat(timespec="microseconds"),
                            target_text,
                        ),
                    )
                global_peak = (
                    peak
                    if effective_historical_peak is None
                    else max(effective_historical_peak, peak)
                )
                if (
                    effective_historical_peak is not None
                    and global_peak < effective_historical_peak
                ):
                    raise _failure("HIGH_WATER_REGRESSION")
                self._db.execute(
                    "UPDATE binding SET latest_trading_date=?,highest_equity=? "
                    "WHERE singleton=1",
                    (target_text, _decimal_text(global_peak)),
                )
                self._db.execute("COMMIT")
        except IbkrRiskEvidenceError:
            with self._lock:
                if not self._closed and self._db.in_transaction:
                    self._db.execute("ROLLBACK")
            raise
        except sqlite3.Error:
            with self._lock:
                if not self._closed and self._db.in_transaction:
                    self._db.execute("ROLLBACK")
            raise _failure("LEDGER_WRITE_FAILED") from None
        identity_hash = _high_water_identity_hash(
            baseline=baseline,
            lineage_hash=self._lineage_hash,
        )
        return IbkrRiskHighWaterRecord(
            trading_date=target,
            peak_equity=peak,
            last_net_liquidation=current,
            baseline_receipt_hash=baseline.receipt_hash,
            observed_at=stamp,
            identity_hash=identity_hash,
            lineage_hash=self._lineage_hash,
            receipt_hash=_high_water_receipt_hash(
                identity_hash=identity_hash,
                baseline_receipt_hash=baseline.receipt_hash,
                lineage_hash=self._lineage_hash,
                peak_equity=peak,
            ),
        )

    def _observe_daily_starting_equity(self, baseline: VerifiedIbkrDailyRiskBaseline) -> None:
        """Retain fixed day-start and flow watermarks, never derive from NLV."""
        if baseline.daily_starting_equity is None:
            return
        target = baseline.bindings.valid_for_trading_date.isoformat()
        row = self._db.execute("SELECT * FROM daily_starting_equity WHERE trading_date=?", (target,)).fetchone()
        fixed = (
            _decimal_text(baseline.daily_starting_equity),
            baseline.daily_starting_equity_as_of.isoformat(),
            baseline.daily_starting_equity_provider_receipt_sha256,
        )
        flow = (
            _decimal_text(baseline.daily_external_cash_flow),
            baseline.daily_external_cash_flow_as_of.isoformat(),
            baseline.daily_external_cash_flow_receipt_hash,
        )
        if row is None:
            latest = self._db.execute("SELECT MAX(trading_date) FROM daily_starting_equity").fetchone()[0]
            if latest is not None and target < latest:
                raise _failure("DAILY_STARTING_EQUITY_DATE_REGRESSION")
            self._db.execute("INSERT INTO daily_starting_equity VALUES (?,?,?,?,?,?,?)", (target, *fixed, *flow))
            return
        if tuple(row[key] for key in ("starting_equity", "starting_equity_as_of", "starting_equity_provider_receipt_sha256")) != fixed:
            raise _failure("DAILY_STARTING_EQUITY_CHANGED")
        prior_as_of = _time(row["latest_external_cash_flow_as_of"], "prior_cash_flow_as_of")
        if baseline.daily_external_cash_flow_as_of < prior_as_of:
            raise _failure("CASH_FLOW_EVIDENCE_REGRESSION")
        if baseline.daily_external_cash_flow_as_of == prior_as_of and tuple(row[key] for key in (
            "latest_external_cash_flow", "latest_external_cash_flow_as_of", "latest_external_cash_flow_receipt_sha256"
        )) != flow:
            raise _failure("CASH_FLOW_EVIDENCE_EQUIVOCATION")
        self._db.execute(
            "UPDATE daily_starting_equity SET latest_external_cash_flow=?,latest_external_cash_flow_as_of=?,"
            "latest_external_cash_flow_receipt_sha256=? WHERE trading_date=?", (*flow, target),
        )


class IbkrRiskEvidenceEnricher:
    """Apply authenticated daily risk evidence to any fresh TWS snapshot.

    ``enrich`` is the integration seam for production transports.  In
    particular, enriching only the stable reader used during assembly is not
    sufficient: recurring coordinator snapshots are assembled independently
    by ``SupportedProductionBrokerAdapter``.  A transport can retain this
    object and apply ``enrich`` to every returned account-base observation.
    """

    def __init__(
        self,
        *,
        baseline_authenticator: DailyIbkrRiskBaselineAuthenticator,
        high_water_ledger: IbkrRiskHighWaterLedger,
        snapshot_max_age_seconds: float,
    ) -> None:
        if not isinstance(
            baseline_authenticator, DailyIbkrRiskBaselineAuthenticator
        ):
            raise _failure("BASELINE_AUTHENTICATOR_INVALID")
        if not isinstance(high_water_ledger, IbkrRiskHighWaterLedger):
            raise _failure("HIGH_WATER_LEDGER_INVALID")
        if (
            isinstance(snapshot_max_age_seconds, bool)
            or not isinstance(snapshot_max_age_seconds, (int, float))
            or not 0 < float(snapshot_max_age_seconds) <= 30
        ):
            raise _failure("SNAPSHOT_MAX_AGE_INVALID")
        if (
            baseline_authenticator.ledger_bindings
            != high_water_ledger.bindings
        ):
            raise _failure("LEDGER_BINDING_MISMATCH")
        self._baseline_authenticator = baseline_authenticator
        self._high_water_ledger = high_water_ledger
        self._snapshot_max_age = timedelta(
            seconds=float(snapshot_max_age_seconds)
        )

    def release_components(self) -> tuple[tuple[str, object, tuple[str, ...]], ...]:
        return (
            (
                "ibkr_daily_risk_baseline_authenticator",
                self._baseline_authenticator,
                ("release_components", "__call__"),
            ),
            (
                "ibkr_risk_high_water_ledger",
                self._high_water_ledger,
                ("release_components", "observe", "close"),
            ),
        )

    def close(self) -> None:
        self._high_water_ledger.close()

    def enrich(self, raw: AccountSnapshot) -> AccountSnapshot:
        """Return one enriched snapshot or fail without weakening evidence."""

        if not isinstance(raw, AccountSnapshot):
            raise _failure("SNAPSHOT_INVALID")
        # Authenticate after collection so verified_at is also a trustworthy
        # upper bound for freshness of the completed TWS callback generation.
        baseline = self._baseline_authenticator()
        expected = baseline.bindings
        if raw.account_masked != expected.account_masked:
            raise _failure("SNAPSHOT_ACCOUNT_MISMATCH")
        if (
            raw.weekly_realized_pnl is not None
            or raw.weekly_realized_pnl_complete
            or raw.peak_equity is not None
            or raw.peak_equity_complete
            or raw.daily_starting_equity is not None
            or raw.daily_external_cash_flow is not None
            or raw.daily_starting_equity_receipt_hash is not None
        ):
            raise _failure("RAW_SNAPSHOT_ALREADY_ENRICHED")
        if (
            not raw.daily_realized_pnl_ready
            or raw.daily_realized_pnl is None
            or raw.risk_evidence_source
            != "ibkr:reqPnL.realizedPnL:current-day"
            or raw.risk_evidence_as_of is None
        ):
            raise _failure("CURRENT_DAY_REALIZED_PNL_UNPROVEN")
        current = baseline.verified_at
        if (
            raw.received_at > current + _CLOCK_SKEW
            or raw.risk_evidence_as_of > current + _CLOCK_SKEW
            or current - raw.received_at > self._snapshot_max_age
            or current - raw.risk_evidence_as_of > self._snapshot_max_age
        ):
            raise _failure("CURRENT_DAY_REALIZED_PNL_STALE")
        if (
            raw.received_at.astimezone(NEW_YORK).date()
            != expected.valid_for_trading_date
            or raw.risk_evidence_as_of.astimezone(NEW_YORK).date()
            != expected.valid_for_trading_date
        ):
            raise _failure("SNAPSHOT_TRADING_DATE_MISMATCH")
        weekly = (
            baseline.week_to_date_realized_pnl_through_prior_trading_day
            + raw.daily_realized_pnl
        )
        daily_fields = {}
        if baseline.daily_starting_equity is not None:
            if (
                baseline.valuation_total_equity != raw.funds.total_value
                or baseline.valuation_observed_at != raw.observed_at
                or baseline.daily_external_cash_flow_as_of != raw.observed_at
                or raw.received_at - raw.observed_at > self._snapshot_max_age
            ):
                raise _failure("CASH_FLOW_VALUATION_BINDING_MISMATCH")
            daily_fields = {
                "daily_starting_equity": baseline.daily_starting_equity,
                "daily_external_cash_flow": baseline.daily_external_cash_flow,
                "daily_starting_equity_as_of": baseline.daily_starting_equity_as_of,
                "daily_external_cash_flow_as_of": baseline.daily_external_cash_flow_as_of,
                "daily_external_cash_flow_receipt_hash": baseline.daily_external_cash_flow_receipt_hash,
                "daily_starting_equity_receipt_hash": daily_starting_equity_receipt_hash(
                    baseline_receipt_hash=baseline.receipt_hash,
                    cash_flow_receipt_hash=baseline.daily_external_cash_flow_receipt_hash,
                    starting_equity=baseline.daily_starting_equity,
                    external_cash_flow=baseline.daily_external_cash_flow,
                    starting_equity_as_of=baseline.daily_starting_equity_as_of,
                    cash_flow_as_of=baseline.daily_external_cash_flow_as_of,
                    total_equity=raw.funds.total_value,
                ),
            }
        record = self._high_water_ledger.observe(
            baseline=baseline,
            net_liquidation=raw.funds.total_value,
            observed_at=raw.received_at,
        )
        source = (
            "ibkr:reqPnL.realizedPnL:current-day+"
            f"authenticated-daily-baseline:{baseline.provider_source}:"
            f"{baseline.provider_receipt_sha256}:{baseline.receipt_hash}"
        )
        try:
            return replace(
                raw,
                weekly_realized_pnl=weekly,
                peak_equity=record.peak_equity,
                weekly_realized_pnl_complete=True,
                peak_equity_complete=True,
                risk_evidence_authoritative=True,
                risk_evidence_source=source,
                risk_evidence_as_of=raw.risk_evidence_as_of,
                risk_baseline_identity_hash=_baseline_identity_hash(
                    baseline.bindings
                ),
                risk_baseline_receipt_hash=baseline.receipt_hash,
                risk_high_water_identity_hash=record.identity_hash,
                risk_high_water_lineage_hash=record.lineage_hash,
                risk_high_water_receipt_hash=record.receipt_hash,
                **daily_fields,
            )
        except Exception:
            raise _failure("SNAPSHOT_ENRICHMENT_FAILED") from None


class IbkrRiskEvidenceAccountSnapshotReader:
    """Stable reader that keeps broker facts available when entry evidence fails.

    ``enrich`` remains the strict authentication boundary used when a caller
    needs a complete entry-risk snapshot.  ``__call__`` is the lifecycle read
    boundary: an unavailable daily baseline or high-water write must not hide
    the underlying TWS account, position, or order facts.  In that case it
    returns the raw snapshot with every baseline-derived entry field cleared.
    """

    def __init__(
        self,
        *,
        snapshot_reader: Callable[[], AccountSnapshot],
        enricher: IbkrRiskEvidenceEnricher,
    ) -> None:
        if not callable(snapshot_reader):
            raise _failure("SNAPSHOT_READER_INVALID")
        if not isinstance(enricher, IbkrRiskEvidenceEnricher):
            raise _failure("ENRICHER_INVALID")
        self._snapshot_reader = snapshot_reader
        self._enricher = enricher

    @property
    def coverage(self) -> OrderCoverageContract:
        """Delegate order visibility without strengthening its semantics."""

        try:
            coverage = self._snapshot_reader.coverage  # type: ignore[attr-defined]
        except Exception:
            raise _failure("COVERAGE_UNAVAILABLE") from None
        if not isinstance(coverage, OrderCoverageContract):
            raise _failure("COVERAGE_INVALID")
        return coverage

    def release_components(self) -> tuple[tuple[str, object, tuple[str, ...]], ...]:
        return (
            (
                "ibkr_raw_account_snapshot_reader",
                self._snapshot_reader,
                ("release_components", "coverage", "__call__"),
            ),
            (
                "ibkr_risk_evidence_enricher",
                self._enricher,
                ("release_components", "enrich", "close"),
            ),
        )

    def close(self) -> None:
        self._enricher.close()

    def enrich(self, raw: AccountSnapshot) -> AccountSnapshot:
        """Expose the exact owned enricher for recurring transport snapshots."""

        return self._enricher.enrich(raw)

    def __call__(self) -> AccountSnapshot:
        try:
            raw = self._snapshot_reader()
        except Exception:
            raise _failure("SNAPSHOT_UNAVAILABLE") from None
        if not isinstance(raw, AccountSnapshot):
            raise _failure("SNAPSHOT_INVALID")
        try:
            return self.enrich(raw)
        except IbkrRiskEvidenceError:
            # Current-day reqPnL and all reconciliation fields remain broker
            # facts.  Weekly P&L and peak equity are entry-only authority and
            # must be explicitly unusable after any authentication/ledger
            # failure, including when a malformed source supplied values.
            return replace(
                raw,
                weekly_realized_pnl=None,
                peak_equity=None,
                weekly_realized_pnl_complete=False,
                peak_equity_complete=False,
                risk_baseline_identity_hash=None,
                risk_baseline_receipt_hash=None,
                risk_high_water_identity_hash=None,
                risk_high_water_lineage_hash=None,
                risk_high_water_receipt_hash=None,
                daily_starting_equity=None,
                daily_external_cash_flow=None,
                daily_starting_equity_as_of=None,
                daily_external_cash_flow_as_of=None,
                daily_external_cash_flow_receipt_hash=None,
                daily_starting_equity_receipt_hash=None,
            )


__all__ = [
    "DailyIbkrRiskBaselineAuthenticator",
    "IBKR_DAILY_RISK_BASELINE_SCHEMA",
    "IBKR_DAILY_STARTING_EQUITY_BASELINE_SCHEMA",
    "IbkrDailyRiskBindingProvider",
    "IbkrRiskEvidenceAccountSnapshotReader",
    "IbkrRiskEvidenceBindings",
    "IbkrRiskEvidenceEnricher",
    "IbkrRiskEvidenceError",
    "IbkrRiskHighWaterLedger",
    "IbkrRiskHighWaterRecord",
    "IbkrRiskLedgerBindings",
    "RiskBaselineKeyReader",
    "VerifiedIbkrDailyRiskBaseline",
    "load_verified_ibkr_daily_risk_baseline",
]
