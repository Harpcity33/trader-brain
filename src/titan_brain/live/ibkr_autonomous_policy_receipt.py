"""Authenticate owner-policy and effective-pricing evidence for IBKR.

Configuration describes the policy a release would use, but it cannot prove
that the owner approved that policy or that a configured commission reserve is
conservative for the bound account.  This module accepts only a canonical,
private, HMAC-authenticated receipt which joins those external facts to one
exact release, config, risk file, account, authorization, and transport.

The verifier is intentionally read-only.  It never creates a receipt or key,
changes a policy, activates a runtime, or contacts the broker.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
from typing import Mapping

from .policy import canonical_json


IBKR_AUTONOMOUS_POLICY_RECEIPT_SCHEMA = (
    "titan_ibkr_autonomous_owner_policy_pricing_receipt_2026-09-14_v1"
)
IBKR_AUTONOMOUS_MAX_POLICY_RECEIPT_TTL = timedelta(days=31)
IBKR_AUTONOMOUS_MAX_PRICING_VERIFICATION_AGE = timedelta(days=30)

_MAX_RECEIPT_BYTES = 64 * 1024
_CLOCK_SKEW = timedelta(seconds=2)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ACCOUNT_KEY = re.compile(r"ibkr-live-ending-([0-9]{4})\Z")
_ACCOUNT_MASK = re.compile(r"(?:\*{4}|•{4})([0-9]{4})\Z")
_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{2,191}\Z")
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{2,127}\Z")
_VERIFIED_SEAL = object()

_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "issued_at",
        "expires_at",
        "bindings",
        "owner_approval",
        "quality_policy",
        "target_exit_policy",
        "effective_pricing",
        "hmac_sha256",
    }
)
_BINDING_FIELDS = frozenset(
    {
        "release_manifest_hash",
        "config_hash",
        "policy_hash",
        "risk_hash",
        "account_key",
        "account_masked",
        "account_binding_fingerprint",
        "authorization_binding_id",
        "provider_contract_id",
        "transport_id",
    }
)
_OWNER_FIELDS = frozenset({"status", "approved_at", "reference", "scope"})
_QUALITY_FIELDS = frozenset(
    {
        "max_spread_bps",
        "spread_denominator",
        "minimum_depth_multiple",
        "depth_source",
        "quote_size_unit",
    }
)
_TARGET_FIELDS = frozenset(
    {
        "target_exit_mode",
        "target_index",
        "target_trigger",
        "quantity",
        "cancel_working_sells_before_exit",
        "require_strictly_newer_cancel_evidence",
        "deadline_feasibility_gate",
    }
)
_PRICING_FIELDS = frozenset(
    {
        "status",
        "verified_at",
        "provider",
        "source_kind",
        "source_reference",
        "source_receipt_sha256",
        "currency",
        "routing_scope",
        "fee_scope",
        "all_in_commission_floor_dollars",
        "floor_conservative_for_allowed_order_scope",
    }
)


class IbkrAutonomousPolicyReceiptError(RuntimeError):
    """Stable, redacted policy/pricing receipt failure."""

    def __init__(self, code: str) -> None:
        normalized = str(code).strip().upper()
        if not re.fullmatch(
            r"IBKR_AUTONOMOUS_POLICY_RECEIPT_[A-Z0-9_]{1,96}", normalized
        ):
            raise ValueError("invalid autonomous policy receipt error code")
        self.code = normalized
        super().__init__(normalized)


def _failure(code: str) -> IbkrAutonomousPolicyReceiptError:
    return IbkrAutonomousPolicyReceiptError(
        f"IBKR_AUTONOMOUS_POLICY_RECEIPT_{code}"
    )


def _sha256(value: object, field_name: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise _failure(f"{field_name.upper()}_INVALID")
    return value


def _identity(value: object, field_name: str) -> str:
    if type(value) is not str or _IDENTITY.fullmatch(value) is None:
        raise _failure(f"{field_name.upper()}_INVALID")
    return value


def _reference(value: object, field_name: str) -> str:
    if type(value) is not str or _REFERENCE.fullmatch(value) is None:
        raise _failure(f"{field_name.upper()}_INVALID")
    return value


def _time(value: object, field_name: str) -> datetime:
    if type(value) is not str:
        raise _failure(f"{field_name.upper()}_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _failure(f"{field_name.upper()}_INVALID") from exc
    if parsed.tzinfo is None:
        raise _failure(f"{field_name.upper()}_INVALID")
    return parsed.astimezone(timezone.utc)


def _current_time(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise _failure("CURRENT_TIME_INVALID")
    return value.astimezone(timezone.utc)


def _decimal(value: object, field_name: str) -> Decimal:
    # Receipt decimals are strings so their reviewed representation cannot be
    # silently changed by a JSON float round trip.
    if type(value) is not str or not value or value.strip() != value:
        raise _failure(f"{field_name.upper()}_INVALID")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise _failure(f"{field_name.upper()}_INVALID") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise _failure(f"{field_name.upper()}_INVALID")
    return parsed


def _mapping(
    value: object, fields: frozenset[str], field_name: str
) -> Mapping[str, object]:
    if type(value) is not dict or set(value) != fields:
        raise _failure(f"{field_name.upper()}_FIELDS_INVALID")
    return value


@dataclass(frozen=True)
class IbkrAutonomousPolicyReceiptBindings:
    """Exact release-owned values an authenticated receipt must approve."""

    release_manifest_hash: str
    config_hash: str
    policy_hash: str
    risk_hash: str
    account_key: str
    account_masked: str
    account_binding_fingerprint: str
    authorization_binding_id: str
    provider_contract_id: str
    transport_id: str
    max_spread_bps: Decimal
    spread_denominator: str
    minimum_depth_multiple: Decimal
    depth_source: str
    quote_size_unit: str
    target_exit_mode: str
    target_index: int
    target_trigger: str
    target_quantity: str
    cancel_working_sells_before_exit: bool
    require_strictly_newer_cancel_evidence: bool
    deadline_feasibility_gate: bool
    minimum_commission_reserve_per_order_dollars: Decimal

    def __post_init__(self) -> None:
        for name in (
            "release_manifest_hash",
            "config_hash",
            "policy_hash",
            "risk_hash",
            "account_binding_fingerprint",
            "authorization_binding_id",
            "provider_contract_id",
        ):
            _sha256(getattr(self, name), name)
        key_match = (
            _ACCOUNT_KEY.fullmatch(self.account_key)
            if type(self.account_key) is str
            else None
        )
        mask_match = (
            _ACCOUNT_MASK.fullmatch(self.account_masked)
            if type(self.account_masked) is str
            else None
        )
        if (
            key_match is None
            or mask_match is None
            or key_match.group(1) != mask_match.group(1)
        ):
            raise _failure("ACCOUNT_BINDING_INVALID")
        _identity(self.transport_id, "transport_id")
        for name in (
            "max_spread_bps",
            "minimum_depth_multiple",
            "minimum_commission_reserve_per_order_dollars",
        ):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise _failure(f"{name.upper()}_INVALID")
        if (
            self.spread_denominator != "executable_nbbo_midpoint"
            or self.depth_source != "fresh_executable_side_top_of_book"
            or self.quote_size_unit != "shares"
        ):
            raise _failure("QUALITY_POLICY_INVALID")
        if (
            self.target_exit_mode
            != "first_target_completed_minute_full_exit"
            or type(self.target_index) is not int
            or self.target_index != 0
            or self.target_trigger
            != "fresh_aligned_completed_one_minute_close_at_or_above_target"
            or self.target_quantity
            != "full_broker_confirmed_sellable_position"
            or self.cancel_working_sells_before_exit is not True
            or self.require_strictly_newer_cancel_evidence is not True
            or self.deadline_feasibility_gate is not True
        ):
            raise _failure("TARGET_EXIT_POLICY_INVALID")


@dataclass(frozen=True)
class VerifiedIbkrAutonomousPolicyReceipt:
    """Sealed result of authenticating an owner and pricing receipt."""

    bindings: IbkrAutonomousPolicyReceiptBindings
    issued_at: datetime
    expires_at: datetime
    owner_approved_at: datetime
    owner_approval_reference: str
    pricing_verified_at: datetime
    pricing_source_reference: str
    pricing_source_receipt_sha256: str
    receipt_hash: str
    _verification_seal: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._verification_seal is not _VERIFIED_SEAL:
            raise _failure("UNVERIFIED_OBJECT_CONSTRUCTION")

    def assert_current(
        self,
        now: datetime,
        expected: IbkrAutonomousPolicyReceiptBindings,
    ) -> None:
        """Recheck freshness and all caller-owned bindings before use."""

        current = _current_time(now)
        if type(expected) is not IbkrAutonomousPolicyReceiptBindings:
            raise _failure("EXPECTED_BINDINGS_INVALID")
        if self.bindings != expected:
            raise _failure("BINDING_MISMATCH")
        if current + _CLOCK_SKEW < self.issued_at:
            raise _failure("RECEIPT_NOT_YET_CURRENT")
        if current >= self.expires_at:
            raise _failure("RECEIPT_EXPIRED")
        if self.expires_at - self.issued_at > IBKR_AUTONOMOUS_MAX_POLICY_RECEIPT_TTL:
            raise _failure("RECEIPT_TTL_EXCEEDED")
        if current + _CLOCK_SKEW < self.owner_approved_at:
            raise _failure("OWNER_APPROVAL_NOT_YET_CURRENT")
        if current + _CLOCK_SKEW < self.pricing_verified_at:
            raise _failure("PRICING_NOT_YET_CURRENT")
        if (
            current - self.pricing_verified_at
            > IBKR_AUTONOMOUS_MAX_PRICING_VERIFICATION_AGE
        ):
            raise _failure("PRICING_VERIFICATION_STALE")


def _read_canonical_private_file(path: str | Path) -> bytes:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise _failure("FILE_PATH_UNSAFE")
    try:
        if candidate.resolve(strict=True) != candidate:
            raise _failure("FILE_PATH_UNSAFE")
        before = candidate.lstat()
    except IbkrAutonomousPolicyReceiptError:
        raise
    except OSError as exc:
        raise _failure("FILE_UNAVAILABLE") from exc
    mode = stat.S_IMODE(before.st_mode)
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or mode not in {0o400, 0o600}
        or before.st_size <= 0
        or before.st_size > _MAX_RECEIPT_BYTES
        or (hasattr(os, "geteuid") and before.st_uid != os.geteuid())
    ):
        raise _failure("FILE_UNSAFE")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise _failure("FILE_UNAVAILABLE") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            (opened.st_dev, opened.st_ino, opened.st_size)
            != (before.st_dev, before.st_ino, before.st_size)
            or stat.S_IMODE(opened.st_mode) not in {0o400, 0o600}
        ):
            raise _failure("FILE_CHANGED")
        chunks: list[bytes] = []
        remaining = _MAX_RECEIPT_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 8192))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            len(encoded) != before.st_size
            or len(encoded) > _MAX_RECEIPT_BYTES
            or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        ):
            raise _failure("FILE_CHANGED")
        return encoded
    finally:
        os.close(descriptor)


def _validate_receipt(
    raw: object,
    *,
    expected: IbkrAutonomousPolicyReceiptBindings,
) -> VerifiedIbkrAutonomousPolicyReceipt:
    if type(raw) is not dict or set(raw) != _TOP_LEVEL_FIELDS:
        raise _failure("FIELDS_INVALID")
    if raw["schema_version"] != IBKR_AUTONOMOUS_POLICY_RECEIPT_SCHEMA:
        raise _failure("SCHEMA_INVALID")
    bindings = _mapping(raw["bindings"], _BINDING_FIELDS, "bindings")
    owner = _mapping(raw["owner_approval"], _OWNER_FIELDS, "owner_approval")
    quality = _mapping(raw["quality_policy"], _QUALITY_FIELDS, "quality_policy")
    target = _mapping(
        raw["target_exit_policy"], _TARGET_FIELDS, "target_exit_policy"
    )
    pricing = _mapping(
        raw["effective_pricing"], _PRICING_FIELDS, "effective_pricing"
    )

    observed = IbkrAutonomousPolicyReceiptBindings(
        release_manifest_hash=bindings["release_manifest_hash"],
        config_hash=bindings["config_hash"],
        policy_hash=bindings["policy_hash"],
        risk_hash=bindings["risk_hash"],
        account_key=bindings["account_key"],
        account_masked=bindings["account_masked"],
        account_binding_fingerprint=bindings["account_binding_fingerprint"],
        authorization_binding_id=bindings["authorization_binding_id"],
        provider_contract_id=bindings["provider_contract_id"],
        transport_id=bindings["transport_id"],
        max_spread_bps=_decimal(quality["max_spread_bps"], "max_spread_bps"),
        spread_denominator=quality["spread_denominator"],
        minimum_depth_multiple=_decimal(
            quality["minimum_depth_multiple"], "minimum_depth_multiple"
        ),
        depth_source=quality["depth_source"],
        quote_size_unit=quality["quote_size_unit"],
        target_exit_mode=target["target_exit_mode"],
        target_index=target["target_index"],
        target_trigger=target["target_trigger"],
        target_quantity=target["quantity"],
        cancel_working_sells_before_exit=target[
            "cancel_working_sells_before_exit"
        ],
        require_strictly_newer_cancel_evidence=target[
            "require_strictly_newer_cancel_evidence"
        ],
        deadline_feasibility_gate=target["deadline_feasibility_gate"],
        minimum_commission_reserve_per_order_dollars=_decimal(
            pricing["all_in_commission_floor_dollars"],
            "all_in_commission_floor_dollars",
        ),
    )
    if observed != expected:
        raise _failure("BINDING_MISMATCH")

    issued_at = _time(raw["issued_at"], "issued_at")
    expires_at = _time(raw["expires_at"], "expires_at")
    approved_at = _time(owner["approved_at"], "owner_approved_at")
    pricing_verified_at = _time(pricing["verified_at"], "pricing_verified_at")
    if (
        expires_at <= issued_at
        or approved_at > issued_at
        or pricing_verified_at > issued_at
    ):
        raise _failure("TIME_ORDER_INVALID")
    if expires_at - issued_at > IBKR_AUTONOMOUS_MAX_POLICY_RECEIPT_TTL:
        raise _failure("RECEIPT_TTL_EXCEEDED")

    if (
        owner["status"] != "owner_approved"
        or owner["scope"]
        != "exact_release_config_policy_risk_quality_target_and_pricing"
    ):
        raise _failure("OWNER_APPROVAL_UNACCEPTED")
    owner_reference = _reference(owner["reference"], "owner_approval_reference")

    if (
        pricing["status"] != "provider_account_verified"
        or pricing["provider"] != "interactive_brokers"
        or pricing["source_kind"]
        != "authenticated_ibkr_account_effective_pricing_receipt"
        or pricing["currency"] != "USD"
        or pricing["routing_scope"] != "smart_routed_us_stock_orders"
        or pricing["fee_scope"]
        != "all_in_commission_and_regulatory_fees_per_order"
        or pricing["floor_conservative_for_allowed_order_scope"] is not True
    ):
        raise _failure("EFFECTIVE_PRICING_UNACCEPTED")
    pricing_reference = _reference(
        pricing["source_reference"], "pricing_source_reference"
    )
    pricing_hash = _sha256(
        pricing["source_receipt_sha256"], "pricing_source_receipt_sha256"
    )

    body = dict(raw)
    body.pop("hmac_sha256", None)
    return VerifiedIbkrAutonomousPolicyReceipt(
        bindings=observed,
        issued_at=issued_at,
        expires_at=expires_at,
        owner_approved_at=approved_at,
        owner_approval_reference=owner_reference,
        pricing_verified_at=pricing_verified_at,
        pricing_source_reference=pricing_reference,
        pricing_source_receipt_sha256=pricing_hash,
        receipt_hash=hashlib.sha256(
            canonical_json(body).encode("utf-8")
        ).hexdigest(),
        _verification_seal=_VERIFIED_SEAL,
    )


def load_verified_ibkr_autonomous_policy_receipt(
    path: str | Path,
    *,
    secret: bytes,
    expected: IbkrAutonomousPolicyReceiptBindings,
    now: datetime,
) -> VerifiedIbkrAutonomousPolicyReceipt:
    """Authenticate and freshness-check one exact private receipt."""

    if type(secret) is not bytes or len(secret) < 32:
        raise _failure("HMAC_KEY_INVALID")
    if type(expected) is not IbkrAutonomousPolicyReceiptBindings:
        raise _failure("EXPECTED_BINDINGS_INVALID")
    encoded = _read_canonical_private_file(path)
    try:
        raw = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _failure("JSON_INVALID") from exc
    if type(raw) is not dict:
        raise _failure("FIELDS_INVALID")
    if encoded != (canonical_json(raw) + "\n").encode("utf-8"):
        raise _failure("FILE_NOT_CANONICAL")
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
    if not hmac.compare_digest(calculated, supplied):
        raise _failure("HMAC_INVALID")
    receipt = _validate_receipt(raw, expected=expected)
    receipt.assert_current(now, expected)
    return receipt


__all__ = [
    "IBKR_AUTONOMOUS_MAX_POLICY_RECEIPT_TTL",
    "IBKR_AUTONOMOUS_MAX_PRICING_VERIFICATION_AGE",
    "IBKR_AUTONOMOUS_POLICY_RECEIPT_SCHEMA",
    "IbkrAutonomousPolicyReceiptBindings",
    "IbkrAutonomousPolicyReceiptError",
    "VerifiedIbkrAutonomousPolicyReceipt",
    "load_verified_ibkr_autonomous_policy_receipt",
]
