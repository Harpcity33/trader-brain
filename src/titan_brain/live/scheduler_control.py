"""Authenticated read-only Codex scheduler retirement evidence.

The trading runtime cannot safely infer scheduler state from ``automation.toml``:
the Codex control plane may already have loaded different state or may still be
running an execution.  This module defines the narrow boundary by which an
owner-authorized control-plane bridge can inject one short-lived, HMAC-signed
snapshot.  The release only verifies that snapshot.  It never edits, pauses,
resumes, or otherwise mutates a Codex automation.

Every accepted snapshot is bound to one installed release and must cover the
complete fixed set of account-adjacent automations.  Status and execution-count
checks remain in the caller so an authenticated ACTIVE/running observation is
reported as a precise readiness blocker instead of an authentication failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
from typing import Mapping, Protocol

from .policy import canonical_json
from .provider_clients import KeychainItem, MacOSKeychain


CODEX_SCHEDULER_EVIDENCE_SCHEMA = (
    "titan_codex_scheduler_retirement_evidence_2026-09-14_v1"
)
CODEX_SCHEDULER_EVIDENCE_RELATIVE_PATH = Path(
    "control/codex-scheduler-retirement-evidence.json"
)
CODEX_SCHEDULER_KEYCHAIN_SERVICE = (
    "titan-full-live-codex-scheduler-control-plane"
)
CODEX_SCHEDULER_CONTROL_PLANE_SOURCE = "codex_app_automation_control_plane"
REQUIRED_CODEX_AUTOMATION_IDS = (
    "robinhood-momentum-engine",
    "robinhood-titan-premarket-deep-dive",
)
CODEX_SCHEDULER_MAX_EVIDENCE_TTL = timedelta(seconds=10)

_MAX_EVIDENCE_BYTES = 64 * 1024
_CLOCK_SKEW = timedelta(seconds=1)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{1,191}\Z")
_ACCOUNT_KEY = re.compile(r"[a-z][a-z0-9_-]{2,127}\Z")
_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "issued_at",
        "expires_at",
        "bindings",
        "control_plane_source",
        "automations",
        "hmac_sha256",
    }
)
_BINDING_FIELDS = frozenset(
    {
        "release_manifest_hash",
        "config_hash",
        "policy_hash",
        "runtime_id",
        "account_key",
    }
)
_AUTOMATION_FIELDS = frozenset(
    {
        "automation_id",
        "scheduler_runtime_id",
        "status",
        "config_hash",
        "active_execution_count",
        "observed_at",
        "query_receipt_hash",
    }
)


class SchedulerEvidenceError(RuntimeError):
    """Stable sanitized error raised at the signed scheduler boundary."""

    def __init__(self, code: str) -> None:
        normalized = str(code).strip().upper()
        if not re.fullmatch(r"SCHEDULER_EVIDENCE_[A-Z0-9_]{1,96}", normalized):
            raise ValueError("invalid scheduler evidence error code")
        self.code = normalized
        super().__init__(normalized)


def _failure(code: str) -> SchedulerEvidenceError:
    return SchedulerEvidenceError(f"SCHEDULER_EVIDENCE_{code}")


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
    parsed = parsed.astimezone(timezone.utc)
    if value != parsed.isoformat():
        raise _failure(f"{field.upper()}_NOT_CANONICAL")
    return parsed


def _current(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise _failure("CURRENT_TIME_INVALID")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class SchedulerEvidenceBindings:
    release_manifest_hash: str
    config_hash: str
    policy_hash: str
    runtime_id: str
    account_key: str

    def __post_init__(self) -> None:
        for field in ("release_manifest_hash", "config_hash", "policy_hash"):
            _sha256(getattr(self, field), field)
        _identity(self.runtime_id, "runtime_id")
        if (
            type(self.account_key) is not str
            or _ACCOUNT_KEY.fullmatch(self.account_key) is None
            or re.search(r"[0-9]{5,}", self.account_key)
        ):
            raise _failure("ACCOUNT_KEY_INVALID")

    def to_payload(self) -> dict[str, object]:
        return {
            "release_manifest_hash": self.release_manifest_hash,
            "config_hash": self.config_hash,
            "policy_hash": self.policy_hash,
            "runtime_id": self.runtime_id,
            "account_key": self.account_key,
        }


@dataclass(frozen=True)
class SchedulerAutomationEvidence:
    automation_id: str
    scheduler_runtime_id: str
    status: str
    config_hash: str
    active_execution_count: int
    observed_at: datetime
    query_receipt_hash: str
    source: str = CODEX_SCHEDULER_CONTROL_PLANE_SOURCE

    def __post_init__(self) -> None:
        _identity(self.automation_id, "automation_id")
        _identity(self.scheduler_runtime_id, "scheduler_runtime_id")
        if self.automation_id not in REQUIRED_CODEX_AUTOMATION_IDS:
            raise _failure("AUTOMATION_ID_INVALID")
        if self.status not in {"ACTIVE", "PAUSED", "DISABLED"}:
            raise _failure("AUTOMATION_STATUS_INVALID")
        if (
            isinstance(self.active_execution_count, bool)
            or not isinstance(self.active_execution_count, int)
            or self.active_execution_count < 0
        ):
            raise _failure("ACTIVE_EXECUTION_COUNT_INVALID")
        _sha256(self.config_hash, "automation_config_hash")
        _sha256(self.query_receipt_hash, "query_receipt_hash")
        if self.source != CODEX_SCHEDULER_CONTROL_PLANE_SOURCE:
            raise _failure("CONTROL_PLANE_SOURCE_INVALID")
        object.__setattr__(
            self,
            "observed_at",
            _current(self.observed_at),
        )

    @property
    def retired(self) -> bool:
        return self.status in {"PAUSED", "DISABLED"}

    @property
    def stable_binding(self) -> tuple[object, ...]:
        return (
            self.automation_id,
            self.scheduler_runtime_id,
            self.status,
            self.config_hash,
            self.active_execution_count,
            self.source,
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "automation_id": self.automation_id,
            "scheduler_runtime_id": self.scheduler_runtime_id,
            "status": self.status,
            "config_hash": self.config_hash,
            "active_execution_count": self.active_execution_count,
            "observed_at": self.observed_at.isoformat(),
            "query_receipt_hash": self.query_receipt_hash,
            "source": self.source,
        }

    @classmethod
    def from_payload(cls, raw: object) -> "SchedulerAutomationEvidence":
        if type(raw) is not dict:
            raise _failure("AUTOMATION_FIELDS_INVALID")
        accepted = set(_AUTOMATION_FIELDS) | {"source"}
        if set(raw) not in (set(_AUTOMATION_FIELDS), accepted):
            raise _failure("AUTOMATION_FIELDS_INVALID")
        return cls(
            automation_id=raw["automation_id"],
            scheduler_runtime_id=raw["scheduler_runtime_id"],
            status=raw["status"],
            config_hash=raw["config_hash"],
            active_execution_count=raw["active_execution_count"],
            observed_at=_time(raw["observed_at"], "automation_observed_at"),
            query_receipt_hash=raw["query_receipt_hash"],
            source=raw.get("source", CODEX_SCHEDULER_CONTROL_PLANE_SOURCE),
        )


@dataclass(frozen=True)
class SchedulerRetirementEvidence:
    bindings: SchedulerEvidenceBindings
    issued_at: datetime
    expires_at: datetime
    automations: tuple[SchedulerAutomationEvidence, ...]
    signed_evidence_hash: str
    source: str = CODEX_SCHEDULER_CONTROL_PLANE_SOURCE
    schema_version: str = CODEX_SCHEDULER_EVIDENCE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != CODEX_SCHEDULER_EVIDENCE_SCHEMA:
            raise _failure("SCHEMA_INVALID")
        if self.source != CODEX_SCHEDULER_CONTROL_PLANE_SOURCE:
            raise _failure("CONTROL_PLANE_SOURCE_INVALID")
        object.__setattr__(self, "issued_at", _current(self.issued_at))
        object.__setattr__(self, "expires_at", _current(self.expires_at))
        object.__setattr__(self, "automations", tuple(self.automations))
        _sha256(self.signed_evidence_hash, "signed_evidence_hash")
        if self.expires_at <= self.issued_at:
            raise _failure("TIME_ORDER_INVALID")
        if self.expires_at - self.issued_at > CODEX_SCHEDULER_MAX_EVIDENCE_TTL:
            raise _failure("TTL_EXCEEDED")
        ids = tuple(item.automation_id for item in self.automations)
        if ids != REQUIRED_CODEX_AUTOMATION_IDS:
            raise _failure("AUTOMATION_SET_INVALID")
        if len({item.scheduler_runtime_id for item in self.automations}) != len(
            self.automations
        ):
            raise _failure("SCHEDULER_RUNTIME_ID_DUPLICATED")
        for automation in self.automations:
            if (
                automation.observed_at > self.issued_at + _CLOCK_SKEW
                or self.issued_at - automation.observed_at
                > CODEX_SCHEDULER_MAX_EVIDENCE_TTL
            ):
                raise _failure("AUTOMATION_OBSERVATION_STALE")

    @property
    def all_retired(self) -> bool:
        return all(item.retired for item in self.automations)

    @property
    def active_execution_count(self) -> int:
        return sum(item.active_execution_count for item in self.automations)

    @property
    def automation_config_hash(self) -> str:
        payload = [
            {
                "automation_id": item.automation_id,
                "config_hash": item.config_hash,
            }
            for item in self.automations
        ]
        return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()

    @property
    def stable_binding(self) -> tuple[object, ...]:
        return (
            self.bindings,
            self.source,
            tuple(item.stable_binding for item in self.automations),
        )

    def assert_current(
        self,
        *,
        now: datetime,
        expected: SchedulerEvidenceBindings,
    ) -> None:
        current = _current(now)
        if self.bindings != expected:
            raise _failure("BINDING_MISMATCH")
        if current + _CLOCK_SKEW < self.issued_at:
            raise _failure("NOT_YET_CURRENT")
        if current >= self.expires_at:
            raise _failure("EXPIRED")
        for automation in self.automations:
            if current + _CLOCK_SKEW < automation.observed_at:
                raise _failure("AUTOMATION_OBSERVATION_IN_FUTURE")
            if (
                current - automation.observed_at
                > CODEX_SCHEDULER_MAX_EVIDENCE_TTL
            ):
                raise _failure("AUTOMATION_OBSERVATION_STALE")

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "issued_at": self.issued_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "bindings": self.bindings.to_payload(),
            "control_plane_source": self.source,
            "automations": [item.to_payload() for item in self.automations],
            "signed_evidence_hash": self.signed_evidence_hash,
        }

    @classmethod
    def from_payload(cls, raw: object) -> "SchedulerRetirementEvidence":
        fields = {
            "schema_version",
            "issued_at",
            "expires_at",
            "bindings",
            "control_plane_source",
            "automations",
            "signed_evidence_hash",
        }
        if type(raw) is not dict or set(raw) != fields:
            raise _failure("RECEIPT_FIELDS_INVALID")
        bindings_raw = raw["bindings"]
        if type(bindings_raw) is not dict or set(bindings_raw) != _BINDING_FIELDS:
            raise _failure("BINDING_FIELDS_INVALID")
        automations_raw = raw["automations"]
        if type(automations_raw) is not list:
            raise _failure("AUTOMATION_SET_INVALID")
        return cls(
            schema_version=raw["schema_version"],
            issued_at=_time(raw["issued_at"], "issued_at"),
            expires_at=_time(raw["expires_at"], "expires_at"),
            bindings=SchedulerEvidenceBindings(
                release_manifest_hash=bindings_raw["release_manifest_hash"],
                config_hash=bindings_raw["config_hash"],
                policy_hash=bindings_raw["policy_hash"],
                runtime_id=bindings_raw["runtime_id"],
                account_key=bindings_raw["account_key"],
            ),
            source=raw["control_plane_source"],
            automations=tuple(
                SchedulerAutomationEvidence.from_payload(item)
                for item in automations_raw
            ),
            signed_evidence_hash=raw["signed_evidence_hash"],
        )


class SchedulerControlPlane(Protocol):
    """Read-only source of authenticated Codex scheduler observations."""

    def observe(
        self,
        *,
        now: datetime,
        expected: SchedulerEvidenceBindings,
    ) -> SchedulerRetirementEvidence: ...


def _read_canonical_private_file(path: str | Path) -> bytes:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise _failure("FILE_PATH_UNSAFE")
    try:
        if candidate.resolve(strict=True) != candidate:
            raise _failure("FILE_PATH_UNSAFE")
        before = candidate.lstat()
    except SchedulerEvidenceError:
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
        or before.st_size > _MAX_EVIDENCE_BYTES
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
        remaining = _MAX_EVIDENCE_BYTES + 1
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
            or len(data) > _MAX_EVIDENCE_BYTES
            or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        ):
            raise _failure("FILE_CHANGED")
        return data
    finally:
        os.close(descriptor)


def load_verified_scheduler_evidence(
    path: str | Path,
    *,
    secret: bytes,
    expected: SchedulerEvidenceBindings,
    now: datetime,
) -> SchedulerRetirementEvidence:
    """Authenticate and bind one canonical scheduler control-plane snapshot."""

    if type(secret) is not bytes or len(secret) < 32:
        raise _failure("HMAC_KEY_INVALID")
    if type(expected) is not SchedulerEvidenceBindings:
        raise _failure("EXPECTED_BINDINGS_INVALID")
    encoded = _read_canonical_private_file(path)
    try:
        raw = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _failure("JSON_INVALID") from exc
    if type(raw) is not dict or set(raw) != _TOP_LEVEL_FIELDS:
        raise _failure("FIELDS_INVALID")
    canonical = (canonical_json(raw) + "\n").encode("utf-8")
    if encoded != canonical:
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
    bindings_raw = body["bindings"]
    if type(bindings_raw) is not dict or set(bindings_raw) != _BINDING_FIELDS:
        raise _failure("BINDING_FIELDS_INVALID")
    observed_bindings = SchedulerEvidenceBindings(
        release_manifest_hash=bindings_raw["release_manifest_hash"],
        config_hash=bindings_raw["config_hash"],
        policy_hash=bindings_raw["policy_hash"],
        runtime_id=bindings_raw["runtime_id"],
        account_key=bindings_raw["account_key"],
    )
    if type(body["automations"]) is not list:
        raise _failure("AUTOMATION_SET_INVALID")
    evidence = SchedulerRetirementEvidence(
        schema_version=body["schema_version"],
        issued_at=_time(body["issued_at"], "issued_at"),
        expires_at=_time(body["expires_at"], "expires_at"),
        bindings=observed_bindings,
        source=body["control_plane_source"],
        automations=tuple(
            SchedulerAutomationEvidence.from_payload(item)
            for item in body["automations"]
        ),
        signed_evidence_hash=hashlib.sha256(encoded).hexdigest(),
    )
    evidence.assert_current(now=now, expected=expected)
    return evidence


class SignedFileSchedulerControlPlane:
    """Production verifier for an externally injected signed snapshot."""

    def __init__(
        self,
        path: str | Path,
        *,
        keychain: MacOSKeychain | None = None,
        key_item: KeychainItem,
    ) -> None:
        self.path = Path(path)
        self.keychain = keychain or MacOSKeychain()
        self.key_item = key_item

    def observe(
        self,
        *,
        now: datetime,
        expected: SchedulerEvidenceBindings,
    ) -> SchedulerRetirementEvidence:
        try:
            secret = self.keychain.read(self.key_item)
        except Exception as exc:
            raise _failure("KEYCHAIN_UNAVAILABLE") from exc
        return load_verified_scheduler_evidence(
            self.path,
            secret=secret,
            expected=expected,
            now=now,
        )


def installed_scheduler_control_plane(
    install_root: str | Path,
    *,
    account_key: str,
) -> SignedFileSchedulerControlPlane:
    """Construct the fixed production adapter without reading evidence yet."""

    root = Path(install_root).expanduser()
    if not root.is_absolute():
        raise _failure("INSTALL_ROOT_UNSAFE")
    return SignedFileSchedulerControlPlane(
        root / CODEX_SCHEDULER_EVIDENCE_RELATIVE_PATH,
        key_item=KeychainItem(
            service=CODEX_SCHEDULER_KEYCHAIN_SERVICE,
            account=account_key,
        ),
    )


__all__ = [
    "CODEX_SCHEDULER_CONTROL_PLANE_SOURCE",
    "CODEX_SCHEDULER_EVIDENCE_RELATIVE_PATH",
    "CODEX_SCHEDULER_EVIDENCE_SCHEMA",
    "CODEX_SCHEDULER_KEYCHAIN_SERVICE",
    "CODEX_SCHEDULER_MAX_EVIDENCE_TTL",
    "REQUIRED_CODEX_AUTOMATION_IDS",
    "SchedulerAutomationEvidence",
    "SchedulerControlPlane",
    "SchedulerEvidenceBindings",
    "SchedulerEvidenceError",
    "SchedulerRetirementEvidence",
    "SignedFileSchedulerControlPlane",
    "installed_scheduler_control_plane",
    "load_verified_scheduler_evidence",
]
