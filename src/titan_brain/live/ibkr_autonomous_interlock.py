"""Durable single-writer interlock for autonomous IBKR mutations.

This component is deliberately narrower than the service runner.  The runner
owns acquisition/release of both the account-global kernel lock and the
SQLite writer lease.  The interlock only proves that those exact, already-held
authorities still agree with the installed release immediately before an SDK
mutation, then fsyncs the kernel-lock owner record.  It never acquires a lock,
creates or consumes an activation, refreshes the database lease, or changes a
runtime mode.

Configuration flags are necessary bindings, not authority.  A mutation also
requires an exact held kernel lock, a fresh same-process durable lease, and one
canonical, consumed owner activation whose machine evidence explicitly
attests supported unattended operation without per-order confirmation.  The
owner activation remains the authority root until durable deactivation or a
release rebind.  Trading-day entry authority is checked separately at the
purpose-aware risk boundary; this purpose-agnostic wire interlock must not
strand carried exposure by expiring protection and exit authority at midnight.
All public failures are stable redacted codes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import socket
import stat
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .activation import ACTIVATION_SCHEMA, ActivationRecord
from .ibkr_autonomous_authority import IbkrAutonomousAuthorityBindings
from .policy import PolicyBundle, canonical_json
from .state import LiveStateStore, SCHEMA_VERSION, object_hash
from .writer_lock import AccountWriterLock


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
_ERROR = re.compile(r"IBKR_AUTONOMOUS_INTERLOCK_[A-Z0-9_]{1,96}\Z")
_LOCK_METADATA_FIELDS = frozenset(
    {
        "schema_version",
        "account_fingerprint",
        "broker_account_binding_fingerprint",
        "authorization_binding_id",
        "owner_id",
        "acquisition_id",
        "pid",
        "hostname",
        "acquired_at",
    }
)
_MUTATION_MODES = frozenset(
    {
        "RECONCILING",
        "ACTIVE",
        "PAUSE_NEW_ENTRIES",
        "MANAGED_CLOSEOUT",
        # INCIDENT cannot discover entries, but the lifecycle may still need a
        # conservative protection or closeout mutation for existing exposure.
        "INCIDENT",
    }
)


class IbkrAutonomousInterlockError(RuntimeError):
    """Stable mutation-denial code that never includes a path or row value."""

    def __init__(self, code: str) -> None:
        normalized = str(code).strip().upper()
        if _ERROR.fullmatch(normalized) is None:
            raise ValueError("invalid autonomous IBKR interlock error code")
        self.code = normalized
        super().__init__(normalized)


def _failure(code: str) -> IbkrAutonomousInterlockError:
    return IbkrAutonomousInterlockError(f"IBKR_AUTONOMOUS_INTERLOCK_{code}")


def _utc(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise _failure(f"{field.upper()}_INVALID")
    return value.astimezone(timezone.utc)


def _stored_time(value: object, field: str) -> datetime:
    if type(value) is not str:
        raise _failure(f"{field.upper()}_INVALID")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise _failure(f"{field.upper()}_INVALID") from None
    if parsed.tzinfo is None:
        raise _failure(f"{field.upper()}_INVALID")
    current = parsed.astimezone(timezone.utc)
    if value != current.isoformat():
        raise _failure(f"{field.upper()}_INVALID")
    return current


def _exact_sha256(value: object) -> bool:
    return type(value) is str and _SHA256.fullmatch(value) is not None


class AutonomousIbkrWriterInterlock:
    """Revalidate an already-owned autonomous writer at every mutation edge.

    ``lock`` must be the exact :class:`AccountWriterLock` instance owned by the
    enclosing ``ServiceRunner``.  This object intentionally has no ``close``
    method: service lifecycle code, not broker composition, owns that lock.
    """

    def __init__(
        self,
        *,
        lock: AccountWriterLock,
        state: LiveStateStore,
        state_path: str | Path,
        state_identity: tuple[int, int],
        policy: PolicyBundle,
        release_manifest_hash: str,
        authority_bindings: IbkrAutonomousAuthorityBindings,
        clock,
        maximum_lease_age: timedelta | None = None,
    ) -> None:
        if not isinstance(lock, AccountWriterLock):
            raise _failure("LOCK_INSTANCE_INVALID")
        if not isinstance(state, LiveStateStore):
            raise _failure("STATE_INSTANCE_INVALID")
        if (
            not isinstance(state_identity, tuple)
            or len(state_identity) != 2
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in state_identity
            )
        ):
            raise _failure("STATE_IDENTITY_INVALID")
        if not isinstance(policy, PolicyBundle):
            raise _failure("POLICY_INSTANCE_INVALID")
        if type(authority_bindings) is not IbkrAutonomousAuthorityBindings:
            raise _failure("AUTHORITY_BINDINGS_INVALID")
        if not callable(clock):
            raise _failure("CLOCK_INVALID")
        if not _exact_sha256(release_manifest_hash):
            raise _failure("RELEASE_BINDING_INVALID")

        try:
            execution = policy.config["execution"]
            sessions = policy.config["sessions"]
            account = policy.config["account"]
            session_timezone = ZoneInfo(str(sessions["timezone"]))
            default_lease_age = timedelta(
                seconds=max(
                    10.0,
                    float(execution["reconcile_interval_seconds"]) * 3.0,
                )
            )
        except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError):
            raise _failure("POLICY_BINDING_INVALID") from None
        lease_age = default_lease_age if maximum_lease_age is None else maximum_lease_age
        if (
            not isinstance(lease_age, timedelta)
            or lease_age <= timedelta(0)
            or lease_age > default_lease_age
        ):
            raise _failure("LEASE_AGE_INVALID")

        expected_mask = f"****{policy.account_last4}"
        policy_bindings = (
            authority_bindings.release_manifest_hash == release_manifest_hash,
            authority_bindings.config_hash == policy.config_hash,
            authority_bindings.policy_binding_id == policy.policy_hash,
            authority_bindings.account_key == policy.account_key,
            authority_bindings.account_masked == expected_mask,
            authority_bindings.account_binding_fingerprint
            == execution.get("production_account_binding_fingerprint"),
            authority_bindings.authorization_binding_id
            == execution.get("production_authorization_binding_id"),
            authority_bindings.provider_contract_id
            == execution.get("ibkr_provider_contract_id"),
            authority_bindings.transport_id
            == execution.get("production_transport_id"),
            authority_bindings.environment == "live",
            account.get("required_last4") == policy.account_last4,
            account.get("margin_debit_allowed") is False,
            sessions.get("premarket_mode") == "analysis_only",
            sessions.get("premarket_orders_enabled") is False,
            policy.execution_authority_mode == "unattended",
            execution.get("supported_unattended_mutation") is True,
            execution.get("per_mutation_user_confirmation_required") is False,
            execution.get("local_mutation_interlock_enabled") is True,
            execution.get("one_account_writer_required") is True,
            execution.get("durable_intent_before_submit") is True,
            execution.get("automatic_retry_unknown_submission") is False,
        )
        if not all(policy_bindings):
            raise _failure("POLICY_BINDING_INVALID")
        if (
            lock.broker_account_binding_fingerprint
            != authority_bindings.account_binding_fingerprint
            or lock.authorization_binding_id
            != authority_bindings.authorization_binding_id
        ):
            raise _failure("LOCK_BINDING_INVALID")

        self.lock = lock
        self.state = state
        self.state_identity = state_identity
        try:
            resolved_state_path = Path(state_path).resolve(strict=True)
            if Path(self.state.path).resolve(strict=True) != resolved_state_path:
                raise _failure("STATE_BINDING_INVALID")
        except IbkrAutonomousInterlockError:
            raise
        except OSError:
            raise _failure("STATE_UNAVAILABLE") from None
        self.state_path = resolved_state_path
        self.policy = policy
        self.release_manifest_hash = release_manifest_hash
        self.authority_bindings = authority_bindings
        self._clock = clock
        self.maximum_lease_age = lease_age
        self.session_timezone = session_timezone

    def release_components(self) -> tuple[tuple[str, object, tuple[str, ...]], ...]:
        """Expose the exact kernel-lock dependency to release attestation."""

        return (
            (
                "ibkr_account_writer_lock",
                self.lock,
                (
                    "held",
                    "holder_metadata",
                    "refresh",
                    "acquisition_id",
                    "acquired_at",
                    "writer_lease_generation",
                ),
            ),
        )

    def _holder_metadata(self, now: datetime) -> Mapping[str, object]:
        if not self.lock.held:
            raise _failure("LOCK_NOT_HELD")
        metadata = self.lock.holder_metadata()
        if type(metadata) is not dict or set(metadata) != _LOCK_METADATA_FIELDS:
            raise _failure("LOCK_METADATA_INVALID")
        pid = metadata.get("pid")
        acquisition_id = metadata.get("acquisition_id")
        acquired_at = _stored_time(metadata.get("acquired_at"), "lock_acquired_at")
        if acquired_at > now:
            raise _failure("LOCK_ACQUIRED_IN_FUTURE")
        if (
            metadata.get("schema_version") != 2
            or metadata.get("account_fingerprint") != self.lock.account_fingerprint
            or metadata.get("broker_account_binding_fingerprint")
            != self.authority_bindings.account_binding_fingerprint
            or metadata.get("authorization_binding_id")
            != self.authority_bindings.authorization_binding_id
            or metadata.get("owner_id") != self.lock.owner_id
            or type(acquisition_id) is not str
            or _UUID.fullmatch(acquisition_id) is None
            or acquisition_id != self.lock.acquisition_id
            or acquired_at != self.lock.acquired_at
            or type(pid) is not int
            or pid != os.getpid()
            or metadata.get("hostname") != socket.gethostname()
        ):
            raise _failure("LOCK_METADATA_INVALID")
        return metadata

    @staticmethod
    def _safe_state_file(metadata: os.stat_result) -> bool:
        return bool(
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_nlink == 1
            and stat.S_IMODE(metadata.st_mode) & 0o022 == 0
            and (
                not hasattr(os, "geteuid")
                or metadata.st_uid == os.geteuid()
            )
        )

    def _open_state_guard(self) -> int:
        try:
            path = self.state_path.resolve(strict=True)
            before = self.state_path.lstat()
        except OSError:
            raise _failure("STATE_UNAVAILABLE") from None
        if (
            path != self.state_path
            or stat.S_ISLNK(before.st_mode)
            or not self._safe_state_file(before)
        ):
            raise _failure("STATE_FILE_UNSAFE")
        if (before.st_dev, before.st_ino) != self.state_identity:
            raise _failure("STATE_FILE_CHANGED")
        flags = os.O_RDONLY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.state_path, flags)
            opened = os.fstat(descriptor)
            after = self.state_path.lstat()
        except OSError:
            if "descriptor" in locals():
                os.close(descriptor)
            raise _failure("STATE_UNAVAILABLE") from None
        if (
            not self._safe_state_file(opened)
            or not self._safe_state_file(after)
            or (opened.st_dev, opened.st_ino) != self.state_identity
            or (after.st_dev, after.st_ino) != self.state_identity
        ):
            os.close(descriptor)
            raise _failure("STATE_FILE_CHANGED")
        return descriptor

    def _validate_state_guard(self, descriptor: int) -> None:
        try:
            opened = os.fstat(descriptor)
            path = self.state_path.lstat()
        except OSError:
            raise _failure("STATE_UNAVAILABLE") from None
        if (
            not self._safe_state_file(opened)
            or not self._safe_state_file(path)
            or (opened.st_dev, opened.st_ino) != self.state_identity
            or (path.st_dev, path.st_ino) != self.state_identity
        ):
            raise _failure("STATE_FILE_CHANGED")

    def _read_state(
        self,
    ) -> tuple[Mapping[str, object], Mapping[str, object], Mapping[str, object]]:
        descriptor = self._open_state_guard()
        try:
            (
                schema_version,
                schema_rows,
                runtimes,
                leases,
                activations,
            ) = self.state.autonomous_interlock_snapshot(
                account_key=self.policy.account_key
            )
            self._validate_state_guard(descriptor)
        except IbkrAutonomousInterlockError:
            raise
        except Exception:
            raise _failure("STATE_UNAVAILABLE") from None
        finally:
            os.close(descriptor)
        if (
            type(schema_version) is not int
            or schema_version != SCHEMA_VERSION
            or len(schema_rows) != 1
            or type(schema_rows[0]["version"]) is not int
            or schema_rows[0]["version"] != SCHEMA_VERSION
        ):
            raise _failure("STATE_SCHEMA_MISMATCH")
        if len(runtimes) != 1:
            raise _failure("RUNTIME_NOT_UNIQUE")
        if len(leases) != 1:
            raise _failure("WRITER_LEASE_NOT_UNIQUE")
        if len(activations) != 1:
            raise _failure("ACTIVATION_NOT_UNIQUE")
        return dict(runtimes[0]), dict(leases[0]), dict(activations[0])

    def _validate_runtime(self, runtime: Mapping[str, object]) -> datetime:
        if runtime.get("authority_enabled") != 1:
            raise _failure("RUNTIME_AUTHORITY_DISABLED")
        if runtime.get("mode") not in _MUTATION_MODES:
            raise _failure("RUNTIME_MODE_BLOCKED")
        bindings = (
            runtime.get("runtime_id") == self.policy.runtime_id,
            runtime.get("account_key") == self.policy.account_key,
            runtime.get("release_manifest_hash") == self.release_manifest_hash,
            runtime.get("config_hash") == self.policy.config_hash,
            runtime.get("policy_hash") == self.policy.policy_hash,
        )
        if not all(bindings):
            raise _failure("RUNTIME_BINDING_INVALID")
        return _stored_time(runtime.get("activated_at"), "runtime_activated_at")

    def _validate_lease(
        self,
        lease: Mapping[str, object],
        now: datetime,
        holder: Mapping[str, object],
    ) -> None:
        try:
            generation = lease["generation"]
            process_id = lease["process_id"]
            acquired_at = _stored_time(lease["acquired_at"], "lease_acquired_at")
            heartbeat_at = _stored_time(lease["heartbeat_at"], "lease_heartbeat_at")
        except (KeyError, TypeError):
            raise _failure("WRITER_LEASE_INVALID") from None
        if (
            lease.get("account_key") != self.policy.account_key
            or lease.get("owner_id") != self.lock.owner_id
            or type(process_id) is not int
            or process_id != os.getpid()
            or type(generation) is not int
            or generation <= 0
            or generation != self.lock.writer_lease_generation
            or lease.get("released_at") is not None
            or acquired_at > heartbeat_at
            or acquired_at
            != _stored_time(holder.get("acquired_at"), "lock_acquired_at")
        ):
            raise _failure("WRITER_LEASE_BINDING_INVALID")
        age = now - heartbeat_at
        if age < timedelta(0):
            raise _failure("WRITER_LEASE_HEARTBEAT_FUTURE")
        if age > self.maximum_lease_age:
            raise _failure("WRITER_LEASE_HEARTBEAT_STALE")

    def _validate_activation(
        self,
        row: Mapping[str, object],
        *,
        runtime_activated_at: datetime,
        now: datetime,
    ) -> None:
        try:
            raw = json.loads(str(row["record_json"]))
            if type(raw) is not dict:
                raise ValueError("not an object")
            record = ActivationRecord.from_payload(raw)
            created_at = _stored_time(row["created_at"], "activation_created_at")
            expires_at = _stored_time(row["expires_at"], "activation_expires_at")
            consumed_at = _stored_time(row["consumed_at"], "activation_consumed_at")
        except IbkrAutonomousInterlockError:
            raise
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise _failure("ACTIVATION_INVALID") from None
        if (
            canonical_json(record.to_payload()) != row.get("record_json")
            or object_hash(raw) != row.get("record_hash")
            or record.schema_version != ACTIVATION_SCHEMA
            or record.activation_id != row.get("activation_id")
            or record.recomputed_activation_id() != record.activation_id
            or record.readiness_hash != record.readiness_evidence.evidence_hash
            or record.account_key != row.get("account_key")
            or record.created_at != created_at
            or record.expires_at != expires_at
            or consumed_at != runtime_activated_at
            or not created_at <= consumed_at <= expires_at
            or record.requested_mode != "live"
            or record.owner_acknowledged_blockers
        ):
            raise _failure("ACTIVATION_HASH_OR_CANONICAL_MISMATCH")
        binding_checks = (
            record.release_manifest_hash == self.release_manifest_hash,
            record.config_hash == self.policy.config_hash,
            record.policy_hash == self.policy.policy_hash,
            record.account_key == self.policy.account_key,
            record.account_last4 == self.policy.account_last4,
            record.runtime_id == self.policy.runtime_id,
            record.database_schema_version == SCHEMA_VERSION,
        )
        readiness = record.readiness_evidence
        readiness_bindings = (
            readiness.release_manifest_hash == self.release_manifest_hash,
            readiness.config_hash == self.policy.config_hash,
            readiness.policy_hash == self.policy.policy_hash,
            readiness.runtime_id == self.policy.runtime_id,
            readiness.database_schema_version == SCHEMA_VERSION,
            readiness.account_key == self.policy.account_key,
            readiness.account_last4 == self.policy.account_last4,
            readiness.broker_account_binding_fingerprint
            == self.authority_bindings.account_binding_fingerprint,
            readiness.broker_authorization_binding_id
            == self.authority_bindings.authorization_binding_id,
            _exact_sha256(readiness.component_provenance_hash),
            _exact_sha256(readiness.coordinator_component_provenance_hash),
        )
        if not all(binding_checks) or not all(readiness_bindings):
            raise _failure("ACTIVATION_BINDING_INVALID")
        if (
            readiness.execution_authority_mode != "unattended"
            or readiness.unattended_mutation_supported is not True
            or readiness.per_mutation_confirmation_required is not False
            or readiness.attended_mutation_supported is not False
        ):
            raise _failure("ACTIVATION_AUTHORITY_UNSUPPORTED")
        # Do not impose a wall-clock day expiry here.  This interlock guards
        # every SDK mutation and cannot distinguish an entry from a protective
        # stop, target exit, cancellation, or mandatory closeout.  The exact
        # consumed activation remains bound to the runtime, release, account,
        # provider evidence, and live writer owner above.  Entry mutations have
        # the additional current-trading-day latch check in
        # DurableIbkrRiskPolicyCheck, while a carried position retains its
        # protection/exit path after midnight and into the next session.

    def __call__(self) -> None:
        """Deny unless the complete current autonomous authority join holds."""

        now = _utc(self._clock(), "current_time")
        initial_holder = dict(self._holder_metadata(now))
        runtime, lease, activation = self._read_state()
        runtime_activated_at = self._validate_runtime(runtime)
        self._validate_lease(lease, now, initial_holder)
        self._validate_activation(
            activation,
            runtime_activated_at=runtime_activated_at,
            now=now,
        )

        # Re-read the lock owner after the SQLite snapshot.  Refresh is the
        # only side effect and occurs only after every durable check passed.
        final_holder = dict(self._holder_metadata(now))
        if final_holder != initial_holder:
            raise _failure("LOCK_METADATA_CHANGED")
        try:
            self.lock.refresh()
        except Exception:
            raise _failure("LOCK_REFRESH_FAILED") from None
        return None


__all__ = [
    "AutonomousIbkrWriterInterlock",
    "IbkrAutonomousInterlockError",
]
