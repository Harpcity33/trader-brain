"""Authenticated, fail-closed authority contract for autonomous IBKR writes.

This module verifies an authority artifact; it never creates one and never
turns configuration booleans into broker authority.  The artifact must be a
canonical, private, owner-controlled regular file authenticated with an
injected HMAC key.  Its assertions are deliberately narrower than IBKR's raw
API surface and bind one release, policy, account receipt, authorization,
provider contract, transport, SDK version, environment, and command client.

The verified object is suitable as a required dependency at a future
autonomous transport boundary.  Possessing it still does not activate a
runtime, authorize a particular order, prove a current account snapshot, or
prove that a submitted protection order is working.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
from typing import Mapping

from .policy import canonical_json


IBKR_AUTONOMOUS_AUTHORITY_SCHEMA = (
    "titan_ibkr_autonomous_provider_authority_2026-09-14_v1"
)
_MAX_AUTHORITY_BYTES = 64 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ACCOUNT_KEY = re.compile(r"ibkr-live-ending-([0-9]{4})\Z")
_ACCOUNT_MASK = re.compile(r"(?:\*{4}|•{4})([0-9]{4})\Z")
_SUPPORT_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{2,127}\Z")
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{2,127}\Z")
_API_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){1,3}\Z")
_CLOCK_SKEW = timedelta(seconds=2)
# These are hard maximum ages, not suggested/default lifetimes.  In
# particular, an authority issuer may choose a substantially shorter lifetime
# for a specific session.  The eight-hour ceilings are long enough for one
# regular-hours desk session while ensuring that yesterday's account-control
# observation can never authorize today's writes.
IBKR_AUTONOMOUS_MAX_AUTHORITY_TTL = timedelta(hours=8)
IBKR_AUTONOMOUS_MAX_PROVIDER_CONFIRMATION_AGE = timedelta(days=30)
IBKR_AUTONOMOUS_MAX_READ_ONLY_VERIFICATION_AGE = timedelta(hours=8)
_VERIFIED_SEAL = object()

_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "issued_at",
        "expires_at",
        "bindings",
        "support",
        "account_controls",
        "order_visibility",
        "execution",
        "scope",
        "protection",
        "precautions",
        "hmac_sha256",
    }
)
_BINDING_FIELDS = frozenset(
    {
        "release_manifest_hash",
        "config_hash",
        "policy_binding_id",
        "account_key",
        "account_masked",
        "account_binding_fingerprint",
        "authorization_binding_id",
        "provider_contract_id",
        "transport_id",
        "api_name",
        "api_version",
        "environment",
        "client_id",
    }
)
_SUPPORT_FIELDS = frozenset(
    {"reference", "status", "confirmed_at", "scope"}
)
_ACCOUNT_CONTROL_FIELDS = frozenset(
    {"read_only_api_enabled", "read_only_api_verified_at", "no_borrow_margin_account"}
)
_ORDER_VISIBILITY_FIELDS = frozenset(
    {
        "scope",
        "standard_equity_orders",
        "advanced_equity_orders",
        "option_orders",
        "working_orders_across_dates",
        "parent_child_conditional_orders",
        "completed_orders",
        "executions",
        "all_pages_consumed",
        "client_ref_recovery_source",
        "broker_preserves_client_ref",
        "negative_client_ref_results_authoritative",
    }
)
_EXECUTION_FIELDS = frozenset(
    {
        "daemon_writes_supported",
        "unattended_place_supported",
        "unattended_cancel_supported",
        "per_order_confirmation_required",
        "durable_intent_before_submit",
        "automatic_unknown_retry_allowed",
        "unknown_submission_behavior",
    }
)
_SCOPE_FIELDS = frozenset(
    {
        "allowed_security_types",
        "allowed_direction",
        "whole_shares_only",
        "allowed_market_hours",
        "margin_debit_allowed",
        "shorting_allowed",
        "options_allowed",
        "fractional_allowed",
        "extended_hours_orders_allowed",
        "overnight_allowed",
    }
)
_PROTECTION_FIELDS = frozenset(
    {
        "mode",
        "atomic_protection_claimed",
        "broker_working_evidence_required",
        "block_new_entries_while_unprotected_or_unresolved",
        "closeout_requires_broker_confirmed_flatness",
    }
)
_PRECAUTION_FIELDS = frozenset(
    {
        "external_market_data_transmission",
        "broker_order_precautions",
        "order_constraint_override_allowed",
        "advanced_error_override_allowed",
        "bypassed_precautions",
    }
)


class IbkrAutonomousAuthorityError(RuntimeError):
    """Stable public failure code with no secret or provider error text."""

    def __init__(self, code: str) -> None:
        normalized = str(code).strip().upper()
        if not re.fullmatch(r"IBKR_AUTONOMOUS_[A-Z0-9_]{1,96}", normalized):
            raise ValueError("invalid IBKR autonomous-authority error code")
        self.code = normalized
        super().__init__(normalized)


def _failure(code: str) -> IbkrAutonomousAuthorityError:
    return IbkrAutonomousAuthorityError(f"IBKR_AUTONOMOUS_{code}")


def _sha256(value: object, field: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise _failure(f"{field.upper()}_INVALID")
    return value


def _identity(value: object, field: str) -> str:
    if type(value) is not str or _IDENTITY.fullmatch(value) is None:
        raise _failure(f"{field.upper()}_INVALID")
    return value


def _time(value: object, field: str) -> datetime:
    if type(value) is not str:
        raise _failure(f"{field.upper()}_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _failure(f"{field.upper()}_INVALID") from exc
    if parsed.tzinfo is None:
        raise _failure(f"{field.upper()}_INVALID")
    return parsed.astimezone(timezone.utc)


def _current_time(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise _failure("CURRENT_TIME_INVALID")
    return value.astimezone(timezone.utc)


def _mapping(value: object, fields: frozenset[str], name: str) -> Mapping[str, object]:
    if type(value) is not dict or set(value) != fields:
        raise _failure(f"{name.upper()}_FIELDS_INVALID")
    return value


def _exact_bool(
    values: Mapping[str, object], field: str, expected: bool, group: str
) -> None:
    observed = values.get(field)
    if type(observed) is not bool or observed is not expected:
        raise _failure(f"{group.upper()}_UNACCEPTED")


@dataclass(frozen=True)
class IbkrAutonomousAuthorityBindings:
    """Exact non-secret identities expected by one release composition."""

    release_manifest_hash: str
    config_hash: str
    policy_binding_id: str
    account_key: str
    account_masked: str
    account_binding_fingerprint: str
    authorization_binding_id: str
    provider_contract_id: str
    transport_id: str
    api_name: str
    api_version: str
    environment: str
    client_id: int

    def __post_init__(self) -> None:
        for name in (
            "release_manifest_hash",
            "config_hash",
            "policy_binding_id",
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
        if key_match is None or mask_match is None or key_match.group(1) != mask_match.group(1):
            raise _failure("ACCOUNT_BINDING_INVALID")
        _identity(self.transport_id, "transport_id")
        _identity(self.api_name, "api_name")
        if type(self.api_version) is not str or _API_VERSION.fullmatch(self.api_version) is None:
            raise _failure("API_VERSION_INVALID")
        if self.environment != "live":
            raise _failure("ENVIRONMENT_INVALID")
        if type(self.client_id) is not int or not 0 < self.client_id < 2**31:
            raise _failure("CLIENT_ID_INVALID")


@dataclass(frozen=True)
class VerifiedIbkrAutonomousAuthority:
    """Immutable authenticated authority assertions for one exact command lane."""

    transport_id: str
    account_key: str
    account_masked: str
    account_binding_fingerprint: str
    authorization_binding_id: str
    provider_contract_id: str
    policy_binding_id: str
    environment: str
    client_id: int
    api_name: str
    api_version: str
    release_manifest_hash: str
    config_hash: str
    issued_at: datetime
    expires_at: datetime
    support_reference: str
    support_confirmed_at: datetime
    read_only_api_verified_at: datetime
    authority_hash: str
    _verification_seal: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._verification_seal is not _VERIFIED_SEAL:
            raise _failure("UNVERIFIED_OBJECT_CONSTRUCTION")

    @property
    def contract_id(self) -> str:
        """Stable authenticated document identity used by preflight receipts."""

        return self.authority_hash

    def assert_current(
        self,
        now: datetime,
        expected_release_manifest_hash: str,
        expected_config_hash: str,
        expected_policy_hash: str,
        expected_account_masked: str,
        expected_account_binding_fingerprint: str,
        expected_authorization_binding_id: str,
        expected_provider_contract_id: str,
        expected_transport_id: str,
        expected_environment: str,
        expected_client_id: int,
        *,
        expected_account_key: str | None = None,
        expected_api_name: str | None = None,
        expected_api_version: str | None = None,
    ) -> None:
        """Recheck freshness and caller-owned bindings immediately before use."""

        current = _current_time(now)
        if current + _CLOCK_SKEW < self.issued_at:
            raise _failure("AUTHORITY_NOT_YET_CURRENT")
        if current >= self.expires_at:
            raise _failure("AUTHORITY_EXPIRED")
        if self.expires_at - self.issued_at > IBKR_AUTONOMOUS_MAX_AUTHORITY_TTL:
            raise _failure("AUTHORITY_TTL_EXCEEDED")
        if (
            current - self.support_confirmed_at
            > IBKR_AUTONOMOUS_MAX_PROVIDER_CONFIRMATION_AGE
        ):
            raise _failure("PROVIDER_CONFIRMATION_STALE")
        if (
            current - self.read_only_api_verified_at
            > IBKR_AUTONOMOUS_MAX_READ_ONLY_VERIFICATION_AGE
        ):
            raise _failure("READ_ONLY_VERIFICATION_STALE")
        if type(expected_client_id) is not int:
            raise _failure("BINDING_MISMATCH")
        expected = (
            (self.release_manifest_hash, expected_release_manifest_hash),
            (self.config_hash, expected_config_hash),
            (self.policy_binding_id, expected_policy_hash),
            (self.account_masked, expected_account_masked),
            (self.account_binding_fingerprint, expected_account_binding_fingerprint),
            (self.authorization_binding_id, expected_authorization_binding_id),
            (self.provider_contract_id, expected_provider_contract_id),
            (self.transport_id, expected_transport_id),
            (self.environment, expected_environment),
            (self.client_id, expected_client_id),
        )
        optional = (
            (self.account_key, expected_account_key),
            (self.api_name, expected_api_name),
            (self.api_version, expected_api_version),
        )
        if any(observed != wanted for observed, wanted in expected) or any(
            wanted is not None and observed != wanted for observed, wanted in optional
        ):
            raise _failure("BINDING_MISMATCH")


def _read_canonical_private_file(path: str | Path) -> bytes:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise _failure("AUTHORITY_FILE_PATH_UNSAFE")
    try:
        if candidate.resolve(strict=True) != candidate:
            raise _failure("AUTHORITY_FILE_PATH_UNSAFE")
        before = candidate.lstat()
    except IbkrAutonomousAuthorityError:
        raise
    except OSError as exc:
        raise _failure("AUTHORITY_FILE_UNAVAILABLE") from exc
    mode = stat.S_IMODE(before.st_mode)
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or mode not in {0o400, 0o600}
        or before.st_size <= 0
        or before.st_size > _MAX_AUTHORITY_BYTES
        or (hasattr(os, "geteuid") and before.st_uid != os.geteuid())
    ):
        raise _failure("AUTHORITY_FILE_UNSAFE")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise _failure("AUTHORITY_FILE_UNAVAILABLE") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            (opened.st_dev, opened.st_ino, opened.st_size)
            != (before.st_dev, before.st_ino, before.st_size)
            or stat.S_IMODE(opened.st_mode) not in {0o400, 0o600}
        ):
            raise _failure("AUTHORITY_FILE_CHANGED")
        chunks: list[bytes] = []
        remaining = _MAX_AUTHORITY_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 8192))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            len(data) != before.st_size
            or len(data) > _MAX_AUTHORITY_BYTES
            or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        ):
            raise _failure("AUTHORITY_FILE_CHANGED")
        return data
    finally:
        os.close(descriptor)


def _validate_contract(
    raw: object,
    *,
    expected: IbkrAutonomousAuthorityBindings,
) -> VerifiedIbkrAutonomousAuthority:
    if type(raw) is not dict or set(raw) != _TOP_LEVEL_FIELDS:
        raise _failure("AUTHORITY_FIELDS_INVALID")
    if raw["schema_version"] != IBKR_AUTONOMOUS_AUTHORITY_SCHEMA:
        raise _failure("AUTHORITY_SCHEMA_INVALID")
    bindings = _mapping(raw["bindings"], _BINDING_FIELDS, "bindings")
    support = raw.get("support")
    if (
        type(support) is not dict
        or type(support.get("reference")) is not str
        or _SUPPORT_REFERENCE.fullmatch(support["reference"]) is None
    ):
        raise _failure("SUPPORT_REFERENCE_REQUIRED")
    support = _mapping(support, _SUPPORT_FIELDS, "support")
    account = _mapping(
        raw["account_controls"], _ACCOUNT_CONTROL_FIELDS, "account_controls"
    )
    visibility = _mapping(
        raw["order_visibility"], _ORDER_VISIBILITY_FIELDS, "order_visibility"
    )
    execution = _mapping(raw["execution"], _EXECUTION_FIELDS, "execution")
    scope = _mapping(raw["scope"], _SCOPE_FIELDS, "scope")
    protection = _mapping(raw["protection"], _PROTECTION_FIELDS, "protection")
    precautions = _mapping(raw["precautions"], _PRECAUTION_FIELDS, "precautions")

    observed_bindings = IbkrAutonomousAuthorityBindings(
        release_manifest_hash=bindings["release_manifest_hash"],
        config_hash=bindings["config_hash"],
        policy_binding_id=bindings["policy_binding_id"],
        account_key=bindings["account_key"],
        account_masked=bindings["account_masked"],
        account_binding_fingerprint=bindings["account_binding_fingerprint"],
        authorization_binding_id=bindings["authorization_binding_id"],
        provider_contract_id=bindings["provider_contract_id"],
        transport_id=bindings["transport_id"],
        api_name=bindings["api_name"],
        api_version=bindings["api_version"],
        environment=bindings["environment"],
        client_id=bindings["client_id"],
    )
    if observed_bindings != expected:
        raise _failure("BINDING_MISMATCH")

    issued = _time(raw["issued_at"], "issued_at")
    expires = _time(raw["expires_at"], "expires_at")
    support_confirmed = _time(support["confirmed_at"], "support_confirmed_at")
    read_only_verified = _time(
        account["read_only_api_verified_at"], "read_only_api_verified_at"
    )
    if expires <= issued or support_confirmed > issued or read_only_verified > issued:
        raise _failure("AUTHORITY_TIME_ORDER_INVALID")
    if expires - issued > IBKR_AUTONOMOUS_MAX_AUTHORITY_TTL:
        raise _failure("AUTHORITY_TTL_EXCEEDED")
    if support["status"] != "provider_confirmed_supported" or support["scope"] != (
        "unattended_regular_hours_api_orders_with_external_market_data"
    ):
        raise _failure("PROVIDER_SUPPORT_UNACCEPTED")

    _exact_bool(account, "read_only_api_enabled", False, "account_controls")
    _exact_bool(account, "no_borrow_margin_account", True, "account_controls")

    if any(
        visibility[field] != "exhaustive"
        for field in (
            "standard_equity_orders",
            "advanced_equity_orders",
            "option_orders",
        )
    ) or visibility["scope"] != "exact_account_all_clients":
        raise _failure("ORDER_VISIBILITY_UNACCEPTED")
    for field in (
        "working_orders_across_dates",
        "parent_child_conditional_orders",
        "completed_orders",
        "executions",
        "all_pages_consumed",
        "broker_preserves_client_ref",
    ):
        _exact_bool(visibility, field, True, "order_visibility")
    _exact_bool(
        visibility,
        "negative_client_ref_results_authoritative",
        False,
        "order_visibility",
    )
    if visibility["client_ref_recovery_source"] != "exhaustive_order_history":
        raise _failure("CLIENT_REF_RECOVERY_UNACCEPTED")

    for field in (
        "daemon_writes_supported",
        "unattended_place_supported",
        "unattended_cancel_supported",
        "durable_intent_before_submit",
    ):
        _exact_bool(execution, field, True, "execution")
    for field in (
        "per_order_confirmation_required",
        "automatic_unknown_retry_allowed",
    ):
        _exact_bool(execution, field, False, "execution")
    if execution["unknown_submission_behavior"] != "reconcile_without_retry":
        raise _failure("UNKNOWN_SUBMISSION_BEHAVIOR_UNACCEPTED")

    if (
        type(scope["allowed_security_types"]) is not list
        or scope["allowed_security_types"] != ["stock"]
        or scope["allowed_direction"] != "long"
        or type(scope["allowed_market_hours"]) is not list
        or scope["allowed_market_hours"] != ["regular_hours"]
    ):
        raise _failure("SCOPE_UNACCEPTED")
    _exact_bool(scope, "whole_shares_only", True, "scope")
    for field in (
        "margin_debit_allowed",
        "shorting_allowed",
        "options_allowed",
        "fractional_allowed",
        "extended_hours_orders_allowed",
        "overnight_allowed",
    ):
        _exact_bool(scope, field, False, "scope")

    if protection["mode"] != "sequential_verified":
        raise _failure("PROTECTION_UNACCEPTED")
    _exact_bool(protection, "atomic_protection_claimed", False, "protection")
    for field in (
        "broker_working_evidence_required",
        "block_new_entries_while_unprotected_or_unresolved",
        "closeout_requires_broker_confirmed_flatness",
    ):
        _exact_bool(protection, field, True, "protection")

    if (
        precautions["external_market_data_transmission"]
        != "provider_confirmed_without_manual_transmit_or_precaution_bypass"
        or precautions["broker_order_precautions"] != "enforced"
        or type(precautions["bypassed_precautions"]) is not list
        or precautions["bypassed_precautions"] != []
    ):
        raise _failure("PRECAUTIONS_UNACCEPTED")
    for field in (
        "order_constraint_override_allowed",
        "advanced_error_override_allowed",
    ):
        _exact_bool(precautions, field, False, "precautions")

    body = dict(raw)
    body.pop("hmac_sha256", None)
    return VerifiedIbkrAutonomousAuthority(
        transport_id=observed_bindings.transport_id,
        account_key=observed_bindings.account_key,
        account_masked=observed_bindings.account_masked,
        account_binding_fingerprint=observed_bindings.account_binding_fingerprint,
        authorization_binding_id=observed_bindings.authorization_binding_id,
        provider_contract_id=observed_bindings.provider_contract_id,
        policy_binding_id=observed_bindings.policy_binding_id,
        environment=observed_bindings.environment,
        client_id=observed_bindings.client_id,
        api_name=observed_bindings.api_name,
        api_version=observed_bindings.api_version,
        release_manifest_hash=observed_bindings.release_manifest_hash,
        config_hash=observed_bindings.config_hash,
        issued_at=issued,
        expires_at=expires,
        support_reference=support["reference"],
        support_confirmed_at=support_confirmed,
        read_only_api_verified_at=read_only_verified,
        authority_hash=hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest(),
        _verification_seal=_VERIFIED_SEAL,
    )


def load_verified_ibkr_autonomous_authority(
    path: str | Path,
    *,
    secret: bytes,
    expected: IbkrAutonomousAuthorityBindings,
    now: datetime,
) -> VerifiedIbkrAutonomousAuthority:
    """Authenticate, validate, bind, and freshness-check one authority file."""

    if type(secret) is not bytes or len(secret) < 32:
        raise _failure("HMAC_KEY_INVALID")
    if type(expected) is not IbkrAutonomousAuthorityBindings:
        raise _failure("EXPECTED_BINDINGS_INVALID")
    encoded = _read_canonical_private_file(path)
    try:
        raw = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _failure("AUTHORITY_JSON_INVALID") from exc
    if type(raw) is not dict:
        raise _failure("AUTHORITY_FIELDS_INVALID")
    canonical = (canonical_json(raw) + "\n").encode("utf-8")
    if encoded != canonical:
        raise _failure("AUTHORITY_FILE_NOT_CANONICAL")
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
    authority = _validate_contract(raw, expected=expected)
    authority.assert_current(
        now,
        expected.release_manifest_hash,
        expected.config_hash,
        expected.policy_binding_id,
        expected.account_masked,
        expected.account_binding_fingerprint,
        expected.authorization_binding_id,
        expected.provider_contract_id,
        expected.transport_id,
        expected.environment,
        expected.client_id,
        expected_account_key=expected.account_key,
        expected_api_name=expected.api_name,
        expected_api_version=expected.api_version,
    )
    return authority


__all__ = [
    "IBKR_AUTONOMOUS_AUTHORITY_SCHEMA",
    "IBKR_AUTONOMOUS_MAX_AUTHORITY_TTL",
    "IBKR_AUTONOMOUS_MAX_PROVIDER_CONFIRMATION_AGE",
    "IBKR_AUTONOMOUS_MAX_READ_ONLY_VERIFICATION_AGE",
    "IbkrAutonomousAuthorityBindings",
    "IbkrAutonomousAuthorityError",
    "VerifiedIbkrAutonomousAuthority",
    "load_verified_ibkr_autonomous_authority",
]
