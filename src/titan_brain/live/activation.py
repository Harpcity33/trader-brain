"""Machine-attested, one-use owner cutover records for full-live mode.

Activation evidence is a canonical snapshot collected by the installed
runtime, never an operator-authored checklist.  The complete snapshot is
embedded in and hash-bound to the activation record.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
import re
from typing import Any, Mapping

from .policy import PolicyBundle, sha256_json


READINESS_SCHEMA = "titan_full_live_readiness_2026-09-08_v2"
ACTIVATION_SCHEMA = "titan_full_live_activation_2026-09-08_v2"
MACHINE_EVIDENCE_SOURCE = "installed_runtime_machine_probe"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_RUNTIME_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")


def _aware(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _optional_aware(value: datetime | None, field: str) -> datetime | None:
    return None if value is None else _aware(value, field)


def _parse_time(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a canonical ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be ISO-8601") from exc
    parsed = _aware(parsed, field)
    if value != parsed.isoformat():
        raise ValueError(f"{field} must use canonical UTC ISO-8601 form")
    return parsed


def _optional_time(value: object, field: str) -> datetime | None:
    return None if value is None else _parse_time(value, field)


def _strict_bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be boolean")
    return value


def _nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a nonnegative integer")
    return value


def _positive_int(value: object, field: str) -> int:
    value = _nonnegative_int(value, field)
    if value == 0:
        raise ValueError(f"{field} must be positive")
    return value


def _optional_age(value: object, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be numeric or null")
    number = float(value)
    if not isfinite(number):
        raise ValueError(f"{field} must be finite")
    return number


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field} must be nonempty canonical text")
    return value


def _optional_text(value: object, field: str) -> str | None:
    return None if value is None else _required_text(value, field)


def _string_tuple(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"{field} must be an array of nonempty strings")
    return tuple(value)


@dataclass(frozen=True)
class ReadinessEvidence:
    """Canonical facts acquired by the installed runtime under its writer lock."""

    collected_at: datetime
    release_manifest_hash: str
    config_hash: str
    policy_hash: str
    runtime_id: str
    database_schema_version: int
    account_key: str
    account_last4: str
    runtime_identity_valid: bool
    evidence_source: str
    broker_connector: str
    broker_read_attempted: bool
    broker_read_succeeded: bool
    broker_read_error_type: str | None
    broker_account_last4: str | None
    broker_snapshot_received_at: datetime | None
    account_active: bool
    broker_authenticated: bool
    daemon_accessible_supported_client: bool
    unattended_mutation_supported: bool
    per_mutation_confirmation_required: bool
    durable_snapshot_id: str | None
    durable_snapshot_received_at: datetime | None
    reconciliation_audit_event_id: str | None
    standard_orders_reconciled: bool
    option_positions_reconciled: bool
    option_orders_reconciled: bool
    advanced_orders_reconciled: bool
    positions_reconciled: bool
    realized_pnl_reconciled: bool
    reconciliation_blocker_count: int
    durable_account_flat: bool
    unknown_submissions: int
    uncovered_quantity: int
    legacy_heartbeat_id: str
    legacy_heartbeat_status: str
    legacy_heartbeat_config_hash: str | None
    old_writer_disabled: bool
    new_writer_lock_held: bool
    writer_lock_owner_id: str | None
    writer_lock_process_id: int | None
    local_state_writable: bool
    audit_chain_valid: bool
    audit_chain_length: int
    audit_chain_head: str
    market_data_connected: bool
    market_data_resynced: bool
    market_data_blockers: tuple[str, ...]
    tradability_provider_ready: bool
    notification_sink: str
    notification_destination_configured: bool
    notification_delivery_receipt_hash: str | None
    notification_delivered_at: datetime | None
    notification_tested: bool
    broker_snapshot_age_seconds: float | None
    durable_snapshot_age_seconds: float | None
    quote_age_seconds: float | None
    completed_bar_age_seconds: float | None
    probe_errors: tuple[str, ...]
    schema_version: str = READINESS_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "collected_at", _aware(self.collected_at, "collected_at"))
        for field in (
            "broker_snapshot_received_at",
            "durable_snapshot_received_at",
            "notification_delivered_at",
        ):
            object.__setattr__(self, field, _optional_aware(getattr(self, field), field))
        if self.schema_version != READINESS_SCHEMA:
            raise ValueError("unsupported readiness evidence schema")
        if self.evidence_source != MACHINE_EVIDENCE_SOURCE:
            raise ValueError("readiness evidence is not a machine probe")
        for field in ("release_manifest_hash", "config_hash", "policy_hash"):
            if not _SHA256.fullmatch(str(getattr(self, field))):
                raise ValueError(f"{field} must be lowercase SHA-256")
        if not _RUNTIME_ID.fullmatch(str(self.runtime_id)):
            raise ValueError("runtime_id is invalid")
        _positive_int(self.database_schema_version, "database_schema_version")
        if not re.fullmatch(r"[0-9]{4}", self.account_last4):
            raise ValueError("account_last4 must contain exactly four digits")
        if self.account_key != f"ending-{self.account_last4}":
            raise ValueError("account_key is not canonical for account_last4")
        if self.broker_account_last4 is not None and not re.fullmatch(
            r"[0-9]{4}", self.broker_account_last4
        ):
            raise ValueError("broker_account_last4 is invalid")
        for field in (
            "runtime_identity_valid",
            "broker_read_attempted",
            "broker_read_succeeded",
            "account_active",
            "broker_authenticated",
            "daemon_accessible_supported_client",
            "unattended_mutation_supported",
            "per_mutation_confirmation_required",
            "standard_orders_reconciled",
            "option_positions_reconciled",
            "option_orders_reconciled",
            "advanced_orders_reconciled",
            "positions_reconciled",
            "realized_pnl_reconciled",
            "durable_account_flat",
            "old_writer_disabled",
            "new_writer_lock_held",
            "local_state_writable",
            "audit_chain_valid",
            "market_data_connected",
            "market_data_resynced",
            "tradability_provider_ready",
            "notification_destination_configured",
            "notification_tested",
        ):
            _strict_bool(getattr(self, field), field)
        for field in (
            "reconciliation_blocker_count",
            "unknown_submissions",
            "uncovered_quantity",
            "audit_chain_length",
        ):
            _nonnegative_int(getattr(self, field), field)
        if self.writer_lock_process_id is not None:
            _positive_int(self.writer_lock_process_id, "writer_lock_process_id")
        for field in (
            "broker_snapshot_age_seconds",
            "durable_snapshot_age_seconds",
            "quote_age_seconds",
            "completed_bar_age_seconds",
        ):
            object.__setattr__(self, field, _optional_age(getattr(self, field), field))
        for field in (
            "broker_connector",
            "legacy_heartbeat_id",
            "legacy_heartbeat_status",
            "notification_sink",
        ):
            _required_text(getattr(self, field), field)
        for field in (
            "broker_read_error_type",
            "durable_snapshot_id",
            "reconciliation_audit_event_id",
            "writer_lock_owner_id",
        ):
            _optional_text(getattr(self, field), field)
        for field in (
            "legacy_heartbeat_config_hash",
            "notification_delivery_receipt_hash",
            "audit_chain_head",
        ):
            value = getattr(self, field)
            if value is not None and not _SHA256.fullmatch(str(value)):
                raise ValueError(f"{field} must be lowercase SHA-256 or null")
        object.__setattr__(self, "market_data_blockers", tuple(self.market_data_blockers))
        object.__setattr__(self, "probe_errors", tuple(self.probe_errors))
        if any(not isinstance(item, str) or not item for item in self.market_data_blockers):
            raise ValueError("market_data_blockers contains invalid values")
        if any(not isinstance(item, str) or not item for item in self.probe_errors):
            raise ValueError("probe_errors contains invalid values")

    @property
    def evidence_hash(self) -> str:
        return sha256_json(self.to_payload())

    def validate_bindings(
        self,
        *,
        policy: PolicyBundle,
        release_manifest_hash: str,
        database_schema_version: int,
    ) -> None:
        bindings = (
            self.release_manifest_hash == release_manifest_hash,
            self.config_hash == policy.config_hash,
            self.policy_hash == policy.policy_hash,
            self.runtime_id == policy.runtime_id,
            self.database_schema_version == database_schema_version,
            self.account_key == str(policy.config["account"]["masked_identifier"]),
            self.account_last4 == policy.account_last4,
        )
        if not all(bindings):
            raise ValueError("readiness evidence identity does not bind the installed runtime")
        if self.broker_account_last4 not in (None, policy.account_last4):
            raise ValueError("readiness evidence crosses broker accounts")

    def blockers(
        self, policy: PolicyBundle, *, now: datetime | None = None
    ) -> tuple[str, ...]:
        failures: list[str] = list(policy.activation_blockers)
        if now is not None:
            current = _aware(now, "readiness validation time")
            readiness_age = (current - self.collected_at).total_seconds()
            if readiness_age < 0 or readiness_age > int(
                policy.config["evidence"]["broker_snapshot_max_age_seconds"]
            ):
                failures.append("READINESS_EVIDENCE_STALE")
        if not self.runtime_identity_valid:
            failures.append("RUNTIME_IDENTITY_INVALID")
        if not self.broker_read_attempted:
            failures.append("BROKER_READ_NOT_ATTEMPTED")
        if not self.broker_read_succeeded:
            failures.append("BROKER_READ_FAILED")
        if not self.account_active or not self.broker_authenticated:
            failures.append("BROKER_ACCOUNT_OR_AUTH_NOT_READY")
        if not self.daemon_accessible_supported_client:
            failures.append("DAEMON_BROKER_CLIENT_UNAVAILABLE")
        if not self.unattended_mutation_supported:
            failures.append("UNATTENDED_MUTATION_UNSUPPORTED")
        if self.per_mutation_confirmation_required:
            failures.append("PER_MUTATION_CONFIRMATION_REQUIRED")
        if not all(
            (
                self.standard_orders_reconciled,
                self.option_positions_reconciled,
                self.option_orders_reconciled,
                self.advanced_orders_reconciled,
                self.positions_reconciled,
                self.realized_pnl_reconciled,
            )
        ):
            failures.append("WHOLE_BROKER_RECONCILIATION_INCOMPLETE")
        if (
            self.durable_snapshot_id is None
            or self.durable_snapshot_received_at is None
            or self.reconciliation_audit_event_id is None
        ):
            failures.append("DURABLE_RECONCILIATION_EVIDENCE_MISSING")
        if self.reconciliation_blocker_count:
            failures.append("DURABLE_RECONCILIATION_HAS_BLOCKERS")
        if not self.durable_account_flat:
            failures.append("ACCOUNT_NOT_FLAT_FOR_ACTIVATION")
        if self.unknown_submissions:
            failures.append("UNKNOWN_SUBMISSION_PRESENT")
        if self.uncovered_quantity:
            failures.append("UNPROTECTED_EXPOSURE_PRESENT")
        if not self.old_writer_disabled:
            failures.append("OLD_ACCOUNT_WRITER_STILL_ENABLED")
        if not self.new_writer_lock_held:
            failures.append("NEW_ACCOUNT_WRITER_LOCK_NOT_HELD")
        if not self.local_state_writable:
            failures.append("LOCAL_STATE_NOT_WRITABLE")
        if not self.audit_chain_valid:
            failures.append("AUDIT_CHAIN_INVALID")
        if not self.market_data_connected or not self.market_data_resynced:
            failures.append("MARKET_DATA_NOT_READY")
        if not self.tradability_provider_ready:
            failures.append("ROBINHOOD_TRADABILITY_UNAVAILABLE")
        if not self.notification_destination_configured or not self.notification_tested:
            failures.append("NOTIFICATION_DESTINATION_UNVERIFIED")
        max_broker_age = int(policy.config["evidence"]["broker_snapshot_max_age_seconds"])
        for value, maximum, failure in (
            (self.broker_snapshot_age_seconds, max_broker_age, "BROKER_SNAPSHOT_STALE"),
            (
                self.durable_snapshot_age_seconds,
                max_broker_age,
                "DURABLE_BROKER_SNAPSHOT_STALE",
            ),
            (
                self.quote_age_seconds,
                int(policy.config["evidence"]["quote_max_age_seconds"]),
                "MARKET_QUOTE_STALE",
            ),
            (
                self.completed_bar_age_seconds,
                int(policy.config["evidence"]["completed_bar_max_age_seconds"]),
                "COMPLETED_BAR_STALE",
            ),
        ):
            if value is None or not isfinite(value) or value < 0 or value > maximum:
                failures.append(failure)
        if self.probe_errors:
            failures.append("READINESS_PROBE_ERROR")
        return tuple(dict.fromkeys(failures))

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "evidence_source": self.evidence_source,
            "collected_at": self.collected_at.isoformat(),
            "release_manifest_hash": self.release_manifest_hash,
            "config_hash": self.config_hash,
            "policy_hash": self.policy_hash,
            "runtime_id": self.runtime_id,
            "database_schema_version": self.database_schema_version,
            "account_key": self.account_key,
            "account_last4": self.account_last4,
            "runtime_identity_valid": self.runtime_identity_valid,
            "broker_connector": self.broker_connector,
            "broker_read_attempted": self.broker_read_attempted,
            "broker_read_succeeded": self.broker_read_succeeded,
            "broker_read_error_type": self.broker_read_error_type,
            "broker_account_last4": self.broker_account_last4,
            "broker_snapshot_received_at": (
                self.broker_snapshot_received_at.isoformat()
                if self.broker_snapshot_received_at is not None
                else None
            ),
            "account_active": self.account_active,
            "broker_authenticated": self.broker_authenticated,
            "daemon_accessible_supported_client": self.daemon_accessible_supported_client,
            "unattended_mutation_supported": self.unattended_mutation_supported,
            "per_mutation_confirmation_required": self.per_mutation_confirmation_required,
            "durable_snapshot_id": self.durable_snapshot_id,
            "durable_snapshot_received_at": (
                self.durable_snapshot_received_at.isoformat()
                if self.durable_snapshot_received_at is not None
                else None
            ),
            "reconciliation_audit_event_id": self.reconciliation_audit_event_id,
            "standard_orders_reconciled": self.standard_orders_reconciled,
            "option_positions_reconciled": self.option_positions_reconciled,
            "option_orders_reconciled": self.option_orders_reconciled,
            "advanced_orders_reconciled": self.advanced_orders_reconciled,
            "positions_reconciled": self.positions_reconciled,
            "realized_pnl_reconciled": self.realized_pnl_reconciled,
            "reconciliation_blocker_count": self.reconciliation_blocker_count,
            "durable_account_flat": self.durable_account_flat,
            "unknown_submissions": self.unknown_submissions,
            "uncovered_quantity": self.uncovered_quantity,
            "legacy_heartbeat_id": self.legacy_heartbeat_id,
            "legacy_heartbeat_status": self.legacy_heartbeat_status,
            "legacy_heartbeat_config_hash": self.legacy_heartbeat_config_hash,
            "old_writer_disabled": self.old_writer_disabled,
            "new_writer_lock_held": self.new_writer_lock_held,
            "writer_lock_owner_id": self.writer_lock_owner_id,
            "writer_lock_process_id": self.writer_lock_process_id,
            "local_state_writable": self.local_state_writable,
            "audit_chain_valid": self.audit_chain_valid,
            "audit_chain_length": self.audit_chain_length,
            "audit_chain_head": self.audit_chain_head,
            "market_data_connected": self.market_data_connected,
            "market_data_resynced": self.market_data_resynced,
            "market_data_blockers": list(self.market_data_blockers),
            "tradability_provider_ready": self.tradability_provider_ready,
            "notification_sink": self.notification_sink,
            "notification_destination_configured": self.notification_destination_configured,
            "notification_delivery_receipt_hash": self.notification_delivery_receipt_hash,
            "notification_delivered_at": (
                self.notification_delivered_at.isoformat()
                if self.notification_delivered_at is not None
                else None
            ),
            "notification_tested": self.notification_tested,
            "broker_snapshot_age_seconds": self.broker_snapshot_age_seconds,
            "durable_snapshot_age_seconds": self.durable_snapshot_age_seconds,
            "quote_age_seconds": self.quote_age_seconds,
            "completed_bar_age_seconds": self.completed_bar_age_seconds,
            "probe_errors": list(self.probe_errors),
        }

    @classmethod
    def from_payload(cls, raw: Mapping[str, Any]) -> "ReadinessEvidence":
        if not isinstance(raw, Mapping):
            raise ValueError("readiness evidence must be an object")
        expected = set(cls._payload_fields())
        if set(raw) != expected:
            raise ValueError(
                "readiness evidence fields differ; "
                f"missing={sorted(expected - set(raw))}, extra={sorted(set(raw) - expected)}"
            )
        return cls(
            schema_version=_required_text(raw["schema_version"], "schema_version"),
            evidence_source=_required_text(raw["evidence_source"], "evidence_source"),
            collected_at=_parse_time(raw["collected_at"], "collected_at"),
            release_manifest_hash=_required_text(raw["release_manifest_hash"], "release_manifest_hash"),
            config_hash=_required_text(raw["config_hash"], "config_hash"),
            policy_hash=_required_text(raw["policy_hash"], "policy_hash"),
            runtime_id=_required_text(raw["runtime_id"], "runtime_id"),
            database_schema_version=_positive_int(raw["database_schema_version"], "database_schema_version"),
            account_key=_required_text(raw["account_key"], "account_key"),
            account_last4=_required_text(raw["account_last4"], "account_last4"),
            runtime_identity_valid=_strict_bool(raw["runtime_identity_valid"], "runtime_identity_valid"),
            broker_connector=_required_text(raw["broker_connector"], "broker_connector"),
            broker_read_attempted=_strict_bool(raw["broker_read_attempted"], "broker_read_attempted"),
            broker_read_succeeded=_strict_bool(raw["broker_read_succeeded"], "broker_read_succeeded"),
            broker_read_error_type=_optional_text(raw["broker_read_error_type"], "broker_read_error_type"),
            broker_account_last4=_optional_text(raw["broker_account_last4"], "broker_account_last4"),
            broker_snapshot_received_at=_optional_time(raw["broker_snapshot_received_at"], "broker_snapshot_received_at"),
            account_active=_strict_bool(raw["account_active"], "account_active"),
            broker_authenticated=_strict_bool(raw["broker_authenticated"], "broker_authenticated"),
            daemon_accessible_supported_client=_strict_bool(raw["daemon_accessible_supported_client"], "daemon_accessible_supported_client"),
            unattended_mutation_supported=_strict_bool(raw["unattended_mutation_supported"], "unattended_mutation_supported"),
            per_mutation_confirmation_required=_strict_bool(raw["per_mutation_confirmation_required"], "per_mutation_confirmation_required"),
            durable_snapshot_id=_optional_text(raw["durable_snapshot_id"], "durable_snapshot_id"),
            durable_snapshot_received_at=_optional_time(raw["durable_snapshot_received_at"], "durable_snapshot_received_at"),
            reconciliation_audit_event_id=_optional_text(raw["reconciliation_audit_event_id"], "reconciliation_audit_event_id"),
            standard_orders_reconciled=_strict_bool(raw["standard_orders_reconciled"], "standard_orders_reconciled"),
            option_positions_reconciled=_strict_bool(raw["option_positions_reconciled"], "option_positions_reconciled"),
            option_orders_reconciled=_strict_bool(raw["option_orders_reconciled"], "option_orders_reconciled"),
            advanced_orders_reconciled=_strict_bool(raw["advanced_orders_reconciled"], "advanced_orders_reconciled"),
            positions_reconciled=_strict_bool(raw["positions_reconciled"], "positions_reconciled"),
            realized_pnl_reconciled=_strict_bool(raw["realized_pnl_reconciled"], "realized_pnl_reconciled"),
            reconciliation_blocker_count=_nonnegative_int(raw["reconciliation_blocker_count"], "reconciliation_blocker_count"),
            durable_account_flat=_strict_bool(raw["durable_account_flat"], "durable_account_flat"),
            unknown_submissions=_nonnegative_int(raw["unknown_submissions"], "unknown_submissions"),
            uncovered_quantity=_nonnegative_int(raw["uncovered_quantity"], "uncovered_quantity"),
            legacy_heartbeat_id=_required_text(raw["legacy_heartbeat_id"], "legacy_heartbeat_id"),
            legacy_heartbeat_status=_required_text(raw["legacy_heartbeat_status"], "legacy_heartbeat_status"),
            legacy_heartbeat_config_hash=_optional_text(raw["legacy_heartbeat_config_hash"], "legacy_heartbeat_config_hash"),
            old_writer_disabled=_strict_bool(raw["old_writer_disabled"], "old_writer_disabled"),
            new_writer_lock_held=_strict_bool(raw["new_writer_lock_held"], "new_writer_lock_held"),
            writer_lock_owner_id=_optional_text(raw["writer_lock_owner_id"], "writer_lock_owner_id"),
            writer_lock_process_id=(None if raw["writer_lock_process_id"] is None else _positive_int(raw["writer_lock_process_id"], "writer_lock_process_id")),
            local_state_writable=_strict_bool(raw["local_state_writable"], "local_state_writable"),
            audit_chain_valid=_strict_bool(raw["audit_chain_valid"], "audit_chain_valid"),
            audit_chain_length=_nonnegative_int(raw["audit_chain_length"], "audit_chain_length"),
            audit_chain_head=_required_text(raw["audit_chain_head"], "audit_chain_head"),
            market_data_connected=_strict_bool(raw["market_data_connected"], "market_data_connected"),
            market_data_resynced=_strict_bool(raw["market_data_resynced"], "market_data_resynced"),
            market_data_blockers=_string_tuple(raw["market_data_blockers"], "market_data_blockers"),
            tradability_provider_ready=_strict_bool(raw["tradability_provider_ready"], "tradability_provider_ready"),
            notification_sink=_required_text(raw["notification_sink"], "notification_sink"),
            notification_destination_configured=_strict_bool(raw["notification_destination_configured"], "notification_destination_configured"),
            notification_delivery_receipt_hash=_optional_text(raw["notification_delivery_receipt_hash"], "notification_delivery_receipt_hash"),
            notification_delivered_at=_optional_time(raw["notification_delivered_at"], "notification_delivered_at"),
            notification_tested=_strict_bool(raw["notification_tested"], "notification_tested"),
            broker_snapshot_age_seconds=_optional_age(raw["broker_snapshot_age_seconds"], "broker_snapshot_age_seconds"),
            durable_snapshot_age_seconds=_optional_age(raw["durable_snapshot_age_seconds"], "durable_snapshot_age_seconds"),
            quote_age_seconds=_optional_age(raw["quote_age_seconds"], "quote_age_seconds"),
            completed_bar_age_seconds=_optional_age(raw["completed_bar_age_seconds"], "completed_bar_age_seconds"),
            probe_errors=_string_tuple(raw["probe_errors"], "probe_errors"),
        )

    @classmethod
    def _payload_fields(cls) -> tuple[str, ...]:
        return _READINESS_FIELDS


# Explicit field list is assigned after the class to keep parser acceptance
# stable even if a dataclass helper or annotation ordering changes.
_READINESS_FIELDS = (
    "schema_version", "evidence_source", "collected_at", "release_manifest_hash",
    "config_hash", "policy_hash", "runtime_id", "database_schema_version",
    "account_key", "account_last4", "runtime_identity_valid", "broker_connector",
    "broker_read_attempted", "broker_read_succeeded", "broker_read_error_type",
    "broker_account_last4", "broker_snapshot_received_at", "account_active",
    "broker_authenticated", "daemon_accessible_supported_client",
    "unattended_mutation_supported", "per_mutation_confirmation_required",
    "durable_snapshot_id", "durable_snapshot_received_at",
    "reconciliation_audit_event_id", "standard_orders_reconciled",
    "option_positions_reconciled", "option_orders_reconciled",
    "advanced_orders_reconciled", "positions_reconciled", "realized_pnl_reconciled",
    "reconciliation_blocker_count", "durable_account_flat", "unknown_submissions", "uncovered_quantity",
    "legacy_heartbeat_id", "legacy_heartbeat_status", "legacy_heartbeat_config_hash",
    "old_writer_disabled", "new_writer_lock_held", "writer_lock_owner_id",
    "writer_lock_process_id", "local_state_writable", "audit_chain_valid",
    "audit_chain_length", "audit_chain_head", "market_data_connected",
    "market_data_resynced", "market_data_blockers", "tradability_provider_ready",
    "notification_sink", "notification_destination_configured",
    "notification_delivery_receipt_hash", "notification_delivered_at",
    "notification_tested", "broker_snapshot_age_seconds",
    "durable_snapshot_age_seconds", "quote_age_seconds", "completed_bar_age_seconds",
    "probe_errors",
)
@dataclass(frozen=True)
class ActivationRecord:
    activation_id: str
    release_manifest_hash: str
    config_hash: str
    policy_hash: str
    account_key: str
    account_last4: str
    runtime_id: str
    database_schema_version: int
    requested_mode: str
    created_at: datetime
    expires_at: datetime
    readiness_hash: str
    readiness_evidence: ReadinessEvidence
    owner_acknowledged_blockers: tuple[str, ...]
    schema_version: str = ACTIVATION_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "created_at", _aware(self.created_at, "created_at"))
        object.__setattr__(self, "expires_at", _aware(self.expires_at, "expires_at"))
        object.__setattr__(self, "owner_acknowledged_blockers", tuple(self.owner_acknowledged_blockers))

    @classmethod
    def build(
        cls, *, release_manifest_hash: str, policy: PolicyBundle,
        database_schema_version: int, created_at: datetime, expires_at: datetime,
        readiness: ReadinessEvidence,
    ) -> "ActivationRecord":
        created = _aware(created_at, "created_at")
        expires = _aware(expires_at, "expires_at")
        if expires <= created:
            raise ValueError("activation expiry must follow creation")
        readiness.validate_bindings(policy=policy, release_manifest_hash=release_manifest_hash,
                                    database_schema_version=database_schema_version)
        values = {
            "release_manifest_hash": release_manifest_hash,
            "config_hash": policy.config_hash,
            "policy_hash": policy.policy_hash,
            "account_key": str(policy.config["account"]["masked_identifier"]),
            "account_last4": policy.account_last4,
            "runtime_id": policy.runtime_id,
            "database_schema_version": database_schema_version,
            "requested_mode": "live", "created_at": created, "expires_at": expires,
            "readiness_hash": readiness.evidence_hash, "readiness_evidence": readiness,
            "owner_acknowledged_blockers": (), "schema_version": ACTIVATION_SCHEMA,
        }
        provisional = cls(activation_id="0" * 64, **values)
        return cls(activation_id=provisional.recomputed_activation_id(), **values)

    def canonical_body(self) -> dict[str, Any]:
        payload = self.to_payload()
        payload.pop("activation_id")
        return payload

    def recomputed_activation_id(self) -> str:
        return sha256_json(self.canonical_body())

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "activation_id": self.activation_id,
            "release_manifest_hash": self.release_manifest_hash,
            "config_hash": self.config_hash,
            "policy_hash": self.policy_hash,
            "account_key": self.account_key,
            "account_last4": self.account_last4,
            "runtime_id": self.runtime_id,
            "database_schema_version": self.database_schema_version,
            "requested_mode": self.requested_mode,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "readiness_hash": self.readiness_hash,
            "readiness_evidence": self.readiness_evidence.to_payload(),
            "owner_acknowledged_blockers": list(self.owner_acknowledged_blockers),
        }

    @classmethod
    def from_payload(cls, raw: Mapping[str, Any]) -> "ActivationRecord":
        if not isinstance(raw, Mapping):
            raise ValueError("activation record must be an object")
        expected = {
            "schema_version", "activation_id", "release_manifest_hash", "config_hash",
            "policy_hash", "account_key", "account_last4", "runtime_id",
            "database_schema_version", "requested_mode", "created_at", "expires_at",
            "readiness_hash", "readiness_evidence", "owner_acknowledged_blockers",
        }
        if set(raw) != expected:
            raise ValueError(
                "activation record fields differ; "
                f"missing={sorted(expected - set(raw))}, extra={sorted(set(raw) - expected)}"
            )
        return cls(
            schema_version=_required_text(raw["schema_version"], "schema_version"),
            activation_id=_required_text(raw["activation_id"], "activation_id"),
            release_manifest_hash=_required_text(raw["release_manifest_hash"], "release_manifest_hash"),
            config_hash=_required_text(raw["config_hash"], "config_hash"),
            policy_hash=_required_text(raw["policy_hash"], "policy_hash"),
            account_key=_required_text(raw["account_key"], "account_key"),
            account_last4=_required_text(raw["account_last4"], "account_last4"),
            runtime_id=_required_text(raw["runtime_id"], "runtime_id"),
            database_schema_version=_positive_int(raw["database_schema_version"], "database_schema_version"),
            requested_mode=_required_text(raw["requested_mode"], "requested_mode"),
            created_at=_parse_time(raw["created_at"], "created_at"),
            expires_at=_parse_time(raw["expires_at"], "expires_at"),
            readiness_hash=_required_text(raw["readiness_hash"], "readiness_hash"),
            readiness_evidence=ReadinessEvidence.from_payload(raw["readiness_evidence"]),
            owner_acknowledged_blockers=_string_tuple(raw["owner_acknowledged_blockers"], "owner_acknowledged_blockers"),
        )

    def validate(
        self, *, policy: PolicyBundle, release_manifest_hash: str,
        database_schema_version: int, now: datetime, already_consumed: bool,
        current_readiness: ReadinessEvidence | None = None,
    ) -> None:
        current = _aware(now, "activation validation time")
        if self.schema_version != ACTIVATION_SCHEMA:
            raise ValueError("unsupported activation record")
        if not _SHA256.fullmatch(self.activation_id):
            raise ValueError("invalid activation identifier")
        if self.activation_id != self.recomputed_activation_id():
            raise ValueError("activation identifier does not match canonical record")
        if self.readiness_hash != self.readiness_evidence.evidence_hash:
            raise ValueError("activation readiness hash does not match embedded evidence")
        if self.created_at > current or current > self.expires_at:
            raise ValueError("activation record is not current")
        if already_consumed:
            raise ValueError("activation record was already consumed")
        if self.requested_mode != "live" or self.owner_acknowledged_blockers:
            raise ValueError("blockers cannot be acknowledged away")
        if self.account_key != f"ending-{self.account_last4}":
            raise ValueError("activation account identity is not canonical")
        bindings = (
            self.release_manifest_hash == release_manifest_hash,
            self.config_hash == policy.config_hash,
            self.policy_hash == policy.policy_hash,
            self.account_key == str(policy.config["account"]["masked_identifier"]),
            self.account_last4 == policy.account_last4,
            self.runtime_id == policy.runtime_id,
            self.database_schema_version == database_schema_version,
        )
        if not all(bindings):
            raise ValueError("activation record does not bind the installed release")
        if not _SHA256.fullmatch(self.release_manifest_hash):
            raise ValueError("invalid release manifest hash")
        self.readiness_evidence.validate_bindings(
            policy=policy, release_manifest_hash=release_manifest_hash,
            database_schema_version=database_schema_version,
        )
        policy.require_activation_ready()
        bound_blockers = self.readiness_evidence.blockers(policy, now=self.created_at)
        if bound_blockers:
            raise ValueError("ACTIVATION_BLOCKED: " + ",".join(bound_blockers))
        if current_readiness is not None:
            current_readiness.validate_bindings(
                policy=policy, release_manifest_hash=release_manifest_hash,
                database_schema_version=database_schema_version,
            )
            current_blockers = current_readiness.blockers(policy, now=current)
            if current_blockers:
                raise ValueError("ACTIVATION_CURRENT_READINESS_BLOCKED: " + ",".join(current_blockers))


__all__ = [
    "ACTIVATION_SCHEMA", "MACHINE_EVIDENCE_SOURCE", "READINESS_SCHEMA",
    "ActivationRecord", "ReadinessEvidence",
]
