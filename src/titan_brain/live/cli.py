"""Operator CLI for an installed, fail-closed full-live release."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import tomllib
from typing import Any, Callable, Mapping, Sequence

from .activation import (
    MACHINE_EVIDENCE_SOURCE,
    ActivationRecord,
    ReadinessEvidence,
)
from .broker.robinhood import RobinhoodBrokerAdapter
from .composition import RuntimeComposition, RuntimeCompositionError
from .control import ControlInbox
from .eod_live import build_eod_evidence, write_eod_evidence
from .latency import LatencyRecorder, LiveStateLatencyAdapter
from .lifecycle_actions import ProductionLifecycleActions
from .massive_adapter import LocalMassiveReadOnlySource
from .models import EngineMode
from .money import to_cents
from .notification_worker import (
    NotificationWorkerSettings,
    notification_worker_health,
    run_notification_worker,
)
from .notifications import (
    DeliveryReceipt,
    Notification,
    NotificationRoute,
    notification_payload_hash,
    notification_route_from_config,
    receipt_satisfies_route,
)
from .policy import PolicyBundle
from .reconcile import (
    NON_INGESTIBLE_SNAPSHOT_BLOCKERS,
    AuthoritativeReconciler,
    ReconciliationPhase,
)
from .release import load_release_manifest
from .service import (
    FullLiveService,
    ServiceRunner,
    _order_payload,
    build_enqueue_only_outbox,
    build_local_outbox,
    persist_account_snapshot,
)
from .state import LiveStateStore, object_hash
from .writer_lock import (
    AccountWriterLock,
    WriterLockBusy,
    user_account_writer_lock_directory,
)


ACCOUNT_KEY = "ending-7153"
_PROBE_CLOCK_JUMP_TOLERANCE_SECONDS = 1.0
_NOTIFICATION_RECEIPT_MAX_AGE_SECONDS = 300.0
_LEGACY_RETIREMENT_SCHEMA = "titan_legacy_writer_retirement_v2"
_LEGACY_GATEWAY_CONTRACT = "titan_account_writer_lock_v1"
_LEGACY_SCHEDULER_CONTROL_PLANE = "codex_app_automation_control_plane"
_LEGACY_SCHEDULER_MAX_EVIDENCE_AGE_SECONDS = 10.0
_LEGACY_WRITER_PROCESS_MARKERS = (
    "titan_runtime.mcp_server",
    "titan-momentum-watcher",
    "com.titan.momentum-watcher",
)


class CommandBlocked(RuntimeError):
    pass


def _external_failure_code(prefix: str, error: BaseException) -> str:
    """Return a bounded machine code without serializing exception text."""

    error_type = re.sub(
        r"[^A-Za-z0-9_]+", "_", type(error).__name__
    ).strip("_")
    return f"{prefix}:{error_type or 'Error'}"[:160]


def _safe_cli_error(error: BaseException) -> str:
    """Expose only owned command messages or explicit all-caps policy codes."""

    if isinstance(error, CommandBlocked):
        # CommandBlocked is constructed only in this module.  External
        # exceptions are converted to machine codes before being wrapped.
        return str(error)
    if isinstance(error, ValueError):
        candidate = str(error).strip()
        if re.fullmatch(r"[A-Z][A-Z0-9_]*(?:[;,:][A-Z0-9_.:-]+)*", candidate):
            return candidate
    return _external_failure_code("COMMAND_FAILED", error)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _runtime_composition(args: argparse.Namespace) -> RuntimeComposition:
    composition = getattr(args, "runtime_composition", None)
    if composition is None:
        composition = RuntimeComposition()
        setattr(args, "runtime_composition", composition)
    if not isinstance(composition, RuntimeComposition):
        raise CommandBlocked("runtime composition is not normalized")
    return composition


def _account_writer_lock(
    layout: "InstallLayout",
    policy: PolicyBundle,
    *,
    owner_id: str | None = None,
) -> AccountWriterLock:
    execution = policy.config["execution"]
    production = execution.get("broker_adapter") == "supported_production_transport"
    return AccountWriterLock(
        layout.lock_path,
        ACCOUNT_KEY,
        owner_id=owner_id,
        broker_account_binding_fingerprint=(
            str(execution.get("production_account_binding_fingerprint", ""))
            if production
            else None
        ),
        authorization_binding_id=(
            str(execution.get("production_authorization_binding_id", ""))
            if production
            else None
        ),
    )


def _iso(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


class InstallLayout:
    def __init__(self, install_root: str | Path):
        self.root = Path(install_root).expanduser().resolve()
        self.release_root = self.root / "current"
        self.manifest_path = self.root / "release-manifest.json"
        self.state_path = self.root / "state/full-live.sqlite3"
        # This path is fixed for the local OS user and does not derive from the
        # selected install root.  Every release, credential rotation, alternate
        # checkout, and installer therefore contends on one account inode.
        self.lock_path = user_account_writer_lock_directory()
        self.notification_path = self.root / "state/notifications.jsonl"
        self.eod_path = self.root / "state/eod"
        self.control_path = self.root / "control"

    def load_release(self, *, verify_files: bool = True) -> tuple[dict[str, Any], PolicyBundle]:
        manifest = load_release_manifest(
            self.manifest_path,
            verify_files_root=self.release_root if verify_files else None,
        )
        policy = PolicyBundle.load(self.release_root)
        if manifest["config_hash"] != policy.config_hash:
            raise ValueError("installed config does not match the release manifest")
        if manifest["policy_hash"] != policy.policy_hash:
            raise ValueError("installed policy does not match the release manifest")
        return manifest, policy


def _activation_from_row(row: Mapping[str, Any]) -> ActivationRecord:
    raw = json.loads(str(row["record_json"]))
    if not isinstance(raw, dict):
        raise ValueError("stored activation record is invalid")
    if str(row["record_hash"]) != object_hash(raw):
        raise ValueError("stored activation record hash does not match its JSON")
    record = ActivationRecord.from_payload(raw)
    if record.activation_id != str(row["activation_id"]):
        raise ValueError("stored activation row key does not match its record")
    return record


def _activated_runtime_profile_hash(
    store: LiveStateStore, runtime: Mapping[str, Any]
) -> str | None:
    """Recover the sole consumed activation's exact runtime profile."""

    if not bool(runtime.get("authority_enabled")):
        return None
    activated_at = runtime.get("activated_at")
    if activated_at is None:
        raise CommandBlocked("armed runtime has no activation timestamp")
    rows = store.rows(
        "SELECT * FROM activation_records WHERE account_key=? AND consumed_at=?",
        (ACCOUNT_KEY, str(activated_at)),
    )
    if len(rows) != 1:
        raise CommandBlocked("armed runtime has no unique consumed activation")
    try:
        record = _activation_from_row(rows[0])
    except (TypeError, ValueError) as exc:
        raise CommandBlocked("armed runtime activation record is invalid") from exc
    profile_hash = record.readiness_evidence.component_provenance_hash
    if profile_hash is None:
        raise CommandBlocked("armed runtime activation has no composition profile")
    return profile_hash


def _record_payload(record: ActivationRecord) -> dict[str, Any]:
    return record.to_payload()


def _open_state(layout: InstallLayout) -> LiveStateStore:
    if not layout.state_path.exists():
        raise CommandBlocked("runtime state is not initialized; run init-state first")
    return LiveStateStore(layout.state_path)


def _age_seconds(now: datetime, observed_at: datetime | None) -> float | None:
    if observed_at is None:
        return None
    return (now - observed_at.astimezone(timezone.utc)).total_seconds()


class _ReadinessProbeTimer:
    """Wall/monotonic probe clock with fail-closed jump detection."""

    def __init__(
        self,
        *,
        started_at: datetime,
        clock: Callable[[], datetime],
        monotonic_clock: Callable[[], float],
    ) -> None:
        if started_at.tzinfo is None:
            raise ValueError("readiness collection time must be timezone-aware")
        self.started_at = started_at.astimezone(timezone.utc)
        self._clock = clock
        self._monotonic_clock = monotonic_clock
        self._last_wall = self.started_at
        self._started_monotonic = self._read_monotonic()
        self._last_monotonic = self._started_monotonic
        self.errors: list[str] = []

    def _read_monotonic(self) -> float:
        value = float(self._monotonic_clock())
        if not (value >= 0 and value < float("inf")):
            raise ValueError("readiness monotonic clock must be finite and nonnegative")
        return value

    def sample(self, stage: str) -> datetime:
        observed = self._clock()
        if not isinstance(observed, datetime) or observed.tzinfo is None:
            self.errors.append(f"probe_clock:{stage}:INVALID_WALL_CLOCK")
            return self._last_wall
        wall = observed.astimezone(timezone.utc)
        try:
            monotonic_value = self._read_monotonic()
        except (TypeError, ValueError, OverflowError):
            self.errors.append(f"probe_clock:{stage}:INVALID_MONOTONIC_CLOCK")
            return self._last_wall
        monotonic_delta = monotonic_value - self._last_monotonic
        wall_delta = (wall - self._last_wall).total_seconds()
        if monotonic_delta < 0:
            self.errors.append(f"probe_clock:{stage}:MONOTONIC_ROLLBACK")
        if wall_delta < 0:
            self.errors.append(f"probe_clock:{stage}:WALL_CLOCK_ROLLBACK")
        if abs(wall_delta - monotonic_delta) > _PROBE_CLOCK_JUMP_TOLERANCE_SECONDS:
            self.errors.append(f"probe_clock:{stage}:WALL_MONOTONIC_DIVERGENCE")
        self._last_wall = wall
        self._last_monotonic = monotonic_value
        return wall

    @property
    def elapsed_monotonic_seconds(self) -> float:
        return self._last_monotonic - self._started_monotonic

    @property
    def clock_stable(self) -> bool:
        return not self.errors


def _probe_legacy_heartbeat(
    path: Path | None = None,
) -> tuple[str, str, str | None, bool, str | None]:
    """Read the known account-writing heartbeat from its authoritative file."""

    heartbeat_id = "robinhood-momentum-engine"
    target = path or (
        Path.home() / ".codex/automations" / heartbeat_id / "automation.toml"
    )
    if target.is_symlink():
        return heartbeat_id, "UNSAFE_SYMLINK", None, False, "legacy_heartbeat:SYMLINK"
    try:
        encoded = target.read_bytes()
    except FileNotFoundError:
        # Absence is not retirement evidence: the scheduler may have loaded
        # the job already, or the file may have been moved while work remains
        # in flight.
        return heartbeat_id, "ABSENT", None, False, "legacy_heartbeat:ABSENT"
    except OSError as exc:
        return heartbeat_id, "UNREADABLE", None, False, f"legacy_heartbeat:{type(exc).__name__}"
    digest = hashlib.sha256(encoded).hexdigest()
    try:
        raw = tomllib.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return heartbeat_id, "INVALID", digest, False, f"legacy_heartbeat:{type(exc).__name__}"
    if raw.get("id") != heartbeat_id or raw.get("kind") != "heartbeat":
        return heartbeat_id, "IDENTITY_MISMATCH", digest, False, "legacy_heartbeat:IDENTITY_MISMATCH"
    status = str(raw.get("status", "MISSING")).upper()
    return heartbeat_id, status, digest, status in {"PAUSED", "DISABLED"}, None


@dataclass(frozen=True)
class LegacySchedulerRuntimeEvidence:
    """Authoritative scheduler control-plane observation for one automation.

    A TOML file is configuration evidence, not proof of the scheduler's loaded
    runtime state.  This record is intentionally not constructible from that
    file.  A future supported Codex scheduler status adapter must supply the
    control-plane runtime and query receipt identifiers; until then production
    probing returns unavailable and retirement fails closed.
    """

    automation_id: str
    scheduler_runtime_id: str
    status: str
    config_hash: str
    active_execution_count: int
    observed_at: datetime
    query_receipt_hash: str
    source: str = _LEGACY_SCHEDULER_CONTROL_PLANE

    def to_payload(self) -> dict[str, Any]:
        return {
            "automation_id": self.automation_id,
            "scheduler_runtime_id": self.scheduler_runtime_id,
            "status": self.status,
            "config_hash": self.config_hash,
            "active_execution_count": self.active_execution_count,
            "observed_at": self.observed_at.astimezone(timezone.utc).isoformat(),
            "query_receipt_hash": self.query_receipt_hash,
            "source": self.source,
        }


def _probe_legacy_scheduler_runtime(
    *,
    now: datetime,
    observed: LegacySchedulerRuntimeEvidence | None = None,
) -> tuple[LegacySchedulerRuntimeEvidence | None, str | None]:
    """Validate exact scheduler runtime evidence or report it unavailable.

    Production has no local, read-only Codex scheduler control-plane client in
    this release.  Therefore its default is an explicit blocker.  ``observed``
    is an in-process dependency seam used by deterministic tests and by a
    future release-contained control-plane adapter; it is not exposed through
    CLI flags, environment variables, config, or persisted files.
    """

    if observed is None:
        return None, "legacy_retirement:SCHEDULER_RUNTIME_IDENTITY_UNAVAILABLE"
    if not isinstance(observed, LegacySchedulerRuntimeEvidence):
        return None, "legacy_retirement:SCHEDULER_RUNTIME_IDENTITY_INVALID"
    normalized_now = now.astimezone(timezone.utc)
    if observed.observed_at.tzinfo is None:
        return None, "legacy_retirement:SCHEDULER_RUNTIME_IDENTITY_INVALID"
    if (
        observed.source != _LEGACY_SCHEDULER_CONTROL_PLANE
        or observed.automation_id != "robinhood-momentum-engine"
        or not observed.scheduler_runtime_id.strip()
        or observed.status.upper() not in {"PAUSED", "DISABLED"}
        or isinstance(observed.active_execution_count, bool)
        or observed.active_execution_count != 0
    ):
        return None, "legacy_retirement:SCHEDULER_RUNTIME_IDENTITY_INVALID"
    for value in (
        observed.config_hash,
        observed.query_receipt_hash,
    ):
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            return None, "legacy_retirement:SCHEDULER_RUNTIME_IDENTITY_INVALID"
    age = (normalized_now - observed.observed_at.astimezone(timezone.utc)).total_seconds()
    if age < 0 or age > _LEGACY_SCHEDULER_MAX_EVIDENCE_AGE_SECONDS:
        return None, "legacy_retirement:SCHEDULER_RUNTIME_IDENTITY_STALE"
    return observed, None


def _probe_legacy_writer_processes(
    process_listing: str | None = None,
) -> tuple[tuple[dict[str, Any], ...], str | None]:
    """Enumerate known legacy writer process identities without signalling them."""

    if process_listing is None:
        try:
            completed = subprocess.run(
                ["/bin/ps", "-axo", "pid=,command="],
                check=False,
                capture_output=True,
                text=True,
                timeout=2.0,
            )
        except (OSError, subprocess.SubprocessError):
            return (), "legacy_retirement:PROCESS_ENUMERATION_FAILED"
        if completed.returncode != 0:
            return (), "legacy_retirement:PROCESS_ENUMERATION_FAILED"
        process_listing = completed.stdout
    observations: list[dict[str, Any]] = []
    for line in process_listing.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) != 2 or not fields[0].isdigit():
            continue
        pid = int(fields[0])
        command = fields[1]
        if pid == os.getpid():
            continue
        for marker in _LEGACY_WRITER_PROCESS_MARKERS:
            if marker in command:
                observations.append(
                    {
                        "pid": pid,
                        "marker": marker,
                        # Commands may include credentials.  Persist/return
                        # only an integrity fingerprint, never raw argv.
                        "command_sha256": hashlib.sha256(
                            command.encode("utf-8")
                        ).hexdigest(),
                    }
                )
                break
    observations.sort(key=lambda item: (item["pid"], item["marker"]))
    return tuple(observations), None


def _legacy_drain_proven(
    *,
    broker_snapshot: Any | None,
    durable_account_flat: bool,
    standard_orders_reconciled: bool,
    option_positions_reconciled: bool,
    option_orders_reconciled: bool,
    advanced_orders_reconciled: bool,
    positions_reconciled: bool,
    realized_pnl_reconciled: bool,
    reconciliation_blocker_count: int,
    unknown_submissions: int,
    uncovered_quantity: int,
) -> bool:
    return bool(
        broker_snapshot is not None
        and durable_account_flat
        and standard_orders_reconciled
        and option_positions_reconciled
        and option_orders_reconciled
        and advanced_orders_reconciled
        and positions_reconciled
        and realized_pnl_reconciled
        and reconciliation_blocker_count == 0
        and unknown_submissions == 0
        and uncovered_quantity == 0
    )


def _legacy_retirement_payload(
    *,
    manifest: Mapping[str, Any],
    policy: PolicyBundle,
    legacy_heartbeat_id: str,
    legacy_heartbeat_status: str,
    legacy_heartbeat_config_hash: str,
    scheduler_runtime: LegacySchedulerRuntimeEvidence,
    durable_snapshot_id: str,
    reconciliation_audit_event_id: str,
    writer_lock: AccountWriterLock,
    writer_lock_owner_id: str,
    writer_lock_process_id: int,
    recorded_at: datetime,
) -> dict[str, Any]:
    return {
        "schema_version": _LEGACY_RETIREMENT_SCHEMA,
        "account_key": ACCOUNT_KEY,
        "runtime_id": policy.runtime_id,
        "release_manifest_hash": str(manifest["release_manifest_hash"]),
        "config_hash": policy.config_hash,
        "policy_hash": policy.policy_hash,
        "legacy_heartbeat_id": legacy_heartbeat_id,
        "legacy_heartbeat_status": legacy_heartbeat_status,
        "legacy_heartbeat_config_hash": legacy_heartbeat_config_hash,
        "scheduler_disabled": True,
        "scheduler_runtime": scheduler_runtime.to_payload(),
        "process_markers": list(_LEGACY_WRITER_PROCESS_MARKERS),
        "observed_legacy_processes": [],
        "processes_quiescent": True,
        "broker_snapshot_id": durable_snapshot_id,
        "reconciliation_audit_event_id": reconciliation_audit_event_id,
        "inflight_drained": True,
        "shared_gateway_contract": _LEGACY_GATEWAY_CONTRACT,
        "gateway_account_fingerprint": writer_lock.account_fingerprint,
        "gateway_owner_id": writer_lock_owner_id,
        "gateway_process_id": writer_lock_process_id,
        "gateway_exclusive": True,
        "recorded_at": recorded_at.astimezone(timezone.utc).isoformat(),
    }


def _verify_legacy_retirement_receipt(
    *,
    store: LiveStateStore,
    manifest: Mapping[str, Any],
    policy: PolicyBundle,
    writer_lock: AccountWriterLock,
    legacy_heartbeat_id: str,
    legacy_heartbeat_status: str,
    legacy_heartbeat_config_hash: str | None,
    scheduler_disabled: bool,
    scheduler_runtime: LegacySchedulerRuntimeEvidence | None,
    process_observations: Sequence[Mapping[str, Any]],
    process_error: str | None,
    current_drain_proven: bool,
    now: datetime,
) -> tuple[bool, str | None]:
    """Validate an append-only receipt and re-probe every mutable fact."""

    scheduler_runtime, scheduler_error = _probe_legacy_scheduler_runtime(
        now=now,
        observed=scheduler_runtime,
    )
    if scheduler_error is not None:
        return False, scheduler_error
    if (
        not scheduler_disabled
        or legacy_heartbeat_config_hash is None
        or scheduler_runtime is None
    ):
        return False, "legacy_retirement:SCHEDULER_NOT_DISABLED"
    if (
        scheduler_runtime.automation_id != legacy_heartbeat_id
        or scheduler_runtime.status.upper() != legacy_heartbeat_status.upper()
        or scheduler_runtime.config_hash != legacy_heartbeat_config_hash
        or scheduler_runtime.active_execution_count != 0
    ):
        return False, "legacy_retirement:SCHEDULER_RUNTIME_BINDING_MISMATCH"
    if process_error is not None:
        return False, process_error
    if process_observations:
        return False, "legacy_retirement:LEGACY_PROCESS_STILL_RUNNING"
    if not current_drain_proven:
        return False, "legacy_retirement:INFLIGHT_DRAIN_UNPROVEN"
    if not writer_lock.held:
        return False, "legacy_retirement:SHARED_GATEWAY_NOT_OWNED"

    rows = store.rows(
        "SELECT entity_id,payload_json FROM audit_events WHERE stream=? "
        "AND event_type='LEGACY_ACCOUNT_WRITER_RETIRED' "
        "AND entity_type='legacy_writer_retirement' ORDER BY sequence DESC LIMIT 1",
        (ACCOUNT_KEY,),
    )
    if not rows:
        return False, "legacy_retirement:RECEIPT_MISSING"
    try:
        payload = json.loads(str(rows[0]["payload_json"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False, "legacy_retirement:RECEIPT_INVALID"
    if not isinstance(payload, dict) or str(rows[0]["entity_id"]) != object_hash(payload):
        return False, "legacy_retirement:RECEIPT_HASH_MISMATCH"
    expected_bindings = {
        "schema_version": _LEGACY_RETIREMENT_SCHEMA,
        "account_key": ACCOUNT_KEY,
        "runtime_id": policy.runtime_id,
        "release_manifest_hash": str(manifest["release_manifest_hash"]),
        "config_hash": policy.config_hash,
        "policy_hash": policy.policy_hash,
        "legacy_heartbeat_id": legacy_heartbeat_id,
        "legacy_heartbeat_status": legacy_heartbeat_status,
        "legacy_heartbeat_config_hash": legacy_heartbeat_config_hash,
        "scheduler_disabled": True,
        "process_markers": list(_LEGACY_WRITER_PROCESS_MARKERS),
        "observed_legacy_processes": [],
        "processes_quiescent": True,
        "inflight_drained": True,
        "shared_gateway_contract": _LEGACY_GATEWAY_CONTRACT,
        "gateway_account_fingerprint": writer_lock.account_fingerprint,
        "gateway_exclusive": True,
    }
    expected_fields = set(expected_bindings).union(
        {
            "scheduler_runtime",
            "broker_snapshot_id",
            "reconciliation_audit_event_id",
            "gateway_owner_id",
            "gateway_process_id",
            "recorded_at",
        }
    )
    if set(payload) != expected_fields:
        return False, "legacy_retirement:RECEIPT_INCOMPLETE"
    if any(payload.get(key) != value for key, value in expected_bindings.items()):
        return False, "legacy_retirement:RECEIPT_BINDING_MISMATCH"
    for field in (
        "broker_snapshot_id",
        "reconciliation_audit_event_id",
        "gateway_owner_id",
        "gateway_process_id",
        "recorded_at",
    ):
        if field not in payload:
            return False, "legacy_retirement:RECEIPT_INCOMPLETE"
    try:
        recorded_at = _iso(str(payload["recorded_at"]), "legacy_retirement.recorded_at")
        scheduler_payload = payload["scheduler_runtime"]
        if not isinstance(scheduler_payload, dict) or set(scheduler_payload) != {
            "automation_id",
            "scheduler_runtime_id",
            "status",
            "config_hash",
            "active_execution_count",
            "observed_at",
            "query_receipt_hash",
            "source",
        }:
            raise ValueError("scheduler runtime evidence is invalid")
        recorded_scheduler = LegacySchedulerRuntimeEvidence(
            automation_id=str(scheduler_payload["automation_id"]),
            scheduler_runtime_id=str(scheduler_payload["scheduler_runtime_id"]),
            status=str(scheduler_payload["status"]),
            config_hash=str(scheduler_payload["config_hash"]),
            active_execution_count=scheduler_payload["active_execution_count"],
            observed_at=_iso(
                str(scheduler_payload["observed_at"]),
                "legacy_retirement.scheduler_runtime.observed_at",
            ),
            query_receipt_hash=str(scheduler_payload["query_receipt_hash"]),
            source=str(scheduler_payload["source"]),
        )
        recorded_scheduler, recorded_scheduler_error = _probe_legacy_scheduler_runtime(
            now=recorded_at,
            observed=recorded_scheduler,
        )
        if recorded_scheduler_error is not None or recorded_scheduler is None:
            raise ValueError("recorded scheduler runtime evidence is invalid")
        raw_gateway_pid = payload["gateway_process_id"]
        if isinstance(raw_gateway_pid, bool) or not isinstance(raw_gateway_pid, int):
            raise ValueError("gateway process id must be an integer")
        gateway_pid = raw_gateway_pid
    except (TypeError, ValueError):
        return False, "legacy_retirement:RECEIPT_INVALID"
    if gateway_pid <= 0 or recorded_at > now.astimezone(timezone.utc) + timedelta(seconds=1):
        return False, "legacy_retirement:RECEIPT_INVALID"
    current_scheduler_binding = (
        scheduler_runtime.automation_id,
        scheduler_runtime.scheduler_runtime_id,
        scheduler_runtime.status.upper(),
        scheduler_runtime.config_hash,
        scheduler_runtime.active_execution_count,
        scheduler_runtime.source,
    )
    recorded_scheduler_binding = (
        recorded_scheduler.automation_id,
        recorded_scheduler.scheduler_runtime_id,
        recorded_scheduler.status.upper(),
        recorded_scheduler.config_hash,
        recorded_scheduler.active_execution_count,
        recorded_scheduler.source,
    )
    if current_scheduler_binding != recorded_scheduler_binding:
        return False, "legacy_retirement:SCHEDULER_RUNTIME_BINDING_MISMATCH"
    snapshot_id = str(payload["broker_snapshot_id"])
    audit_id = str(payload["reconciliation_audit_event_id"])
    snapshot_rows = store.rows(
        "SELECT 1 AS ok FROM broker_snapshots WHERE account_key=? AND snapshot_id=?",
        (ACCOUNT_KEY, snapshot_id),
    )
    audit_rows = store.rows(
        "SELECT 1 AS ok FROM audit_events WHERE stream=? AND event_id=? "
        "AND event_type='POSITIONS_RECONCILED' AND entity_type='broker_snapshot' "
        "AND entity_id=?",
        (ACCOUNT_KEY, audit_id, snapshot_id),
    )
    if len(snapshot_rows) != 1 or len(audit_rows) != 1:
        return False, "legacy_retirement:DRAIN_EVIDENCE_MISSING"
    return True, None


def _fresh_snapshot_matches_durable(snapshot: Any, durable: Mapping[str, Any]) -> bool:
    """Compare every persisted material broker fact, excluding read timestamps."""

    positions = [
        {
            "symbol": position.symbol,
            "quantity": format(position.quantity, "f"),
            "sellable_quantity": format(position.sellable_quantity, "f"),
            "held_for_sells": format(position.held_for_sells, "f"),
            "average_price": (
                format(position.average_price, "f")
                if position.average_price is not None
                else None
            ),
            "asset_class": position.asset_class,
        }
        for position in snapshot.equity_positions
    ]
    orders = [_order_payload(order) for order in snapshot.equity_orders]
    active_position_count = sum(
        1 for position in snapshot.equity_positions if position.quantity != 0
    )
    nonterminal_order_count = sum(
        1 for order in snapshot.equity_orders if not order.state.terminal
    )
    return all(
        (
            str(durable["account_state"]) == snapshot.account_state,
            int(durable["equity_cents"]) == to_cents(snapshot.funds.total_value),
            int(durable["cash_cents"]) == to_cents(snapshot.funds.cash),
            int(durable["unleveraged_buying_power_cents"])
            == to_cents(snapshot.funds.unleveraged_buying_power),
            int(durable["realized_pnl_cents"])
            == to_cents(snapshot.daily_realized_pnl or Decimal("0")),
            int(durable["equity_position_count"]) == active_position_count,
            int(durable["equity_order_count"]) == len(snapshot.equity_orders),
            int(durable["equity_nonterminal_order_count"])
            == nonterminal_order_count,
            int(durable["option_position_count"]) == snapshot.option_position_count,
            int(durable["option_order_count"]) == snapshot.option_order_count,
            int(durable["advanced_order_count"]) == snapshot.advanced_order_count,
            str(durable["positions_digest"]) == object_hash(positions),
            str(durable["orders_digest"]) == object_hash(orders),
        )
    )


def _verified_notification_receipt(
    store: LiveStateStore,
    *,
    account_key: str,
    route: NotificationRoute,
) -> tuple[str, datetime] | None:
    """Return only an exact current-route, event/payload-bound receipt."""

    delivered = store.rows(
        "SELECT n.message_id,n.event_key,n.template,n.payload_json,n.delivered_at,"
        "n.delivery_receipt,n.delivery_route_id,n.delivery_assurance,"
        "n.delivery_receipt_hash,n.delivery_payload_hash "
        "FROM notification_outbox n JOIN audit_events e "
        "ON e.entity_type='notification' AND e.entity_id=n.message_id "
        "AND e.event_type='NOTIFICATION_DELIVERED' "
        "WHERE n.account_key=? AND n.template='READINESS' AND n.state='DELIVERED' "
        "AND n.delivery_receipt IS NOT NULL ORDER BY n.delivered_at DESC LIMIT 20",
        (account_key,),
    )
    for row in delivered:
        try:
            wrapper = json.loads(str(row["payload_json"]))
            if not isinstance(wrapper, Mapping):
                raise ValueError("notification payload wrapper is invalid")
            payload = wrapper["payload"]
            if not isinstance(payload, Mapping):
                raise ValueError("notification event payload is invalid")
            exact_notification = Notification(
                dedupe_key=str(row["event_key"]),
                event_type=str(row["template"]),
                severity=str(wrapper["severity"]),
                subject=str(wrapper["subject"]),
                body=str(wrapper["body"]),
                payload=dict(payload),
            )
            expected_payload_hash = notification_payload_hash(exact_notification)
            receipt = DeliveryReceipt.from_json(str(row["delivery_receipt"]))
            durable_delivered_at = _iso(
                str(row["delivered_at"]), "notification.delivered_at"
            )
            if (
                str(row["delivery_route_id"] or "") != route.route_id
                or str(row["delivery_route_id"] or "") != receipt.route_id
                or str(row["delivery_assurance"] or "")
                != receipt.assurance.value
                or str(row["delivery_receipt_hash"] or "")
                != receipt.receipt_hash
                or str(row["delivery_payload_hash"] or "")
                != expected_payload_hash
                or receipt.payload_hash != expected_payload_hash
                or abs(
                    (receipt.accepted_at - durable_delivered_at).total_seconds()
                )
                > 30
                or not receipt_satisfies_route(
                    receipt,
                    route,
                    event_key=exact_notification.dedupe_key,
                    payload_hash=expected_payload_hash,
                )
            ):
                continue
        except (KeyError, TypeError, ValueError):
            continue
        return receipt.receipt_hash, receipt.accepted_at
    return None


def _machine_readiness(
    *,
    layout: InstallLayout,
    manifest: Mapping[str, Any],
    policy: PolicyBundle,
    store: LiveStateStore,
    writer_lock: AccountWriterLock,
    now: datetime,
    broker: Any | None = None,
    market_source: Any | None = None,
    legacy_heartbeat_path: Path | None = None,
    persist_fresh_broker_read: bool = True,
    clock: Callable[[], datetime] | None = None,
    monotonic_clock: Callable[[], float] | None = None,
    legacy_process_listing: str | None = None,
    legacy_scheduler_runtime_evidence: LegacySchedulerRuntimeEvidence | None = None,
    runtime_composition: RuntimeComposition | None = None,
) -> ReadinessEvidence:
    """Collect activation facts from live dependencies and durable state.

    All probes fail closed.  Optional dependency arguments exist only for
    deterministic tests; production commands construct the installed adapters.
    """

    probe_timer = _ReadinessProbeTimer(
        started_at=now,
        # Existing deterministic callers that supply only ``now`` retain a
        # static clock.  Production commands explicitly inject ``_now``.
        clock=clock or (lambda: now),
        monotonic_clock=monotonic_clock or time.monotonic,
    )
    current = probe_timer.started_at
    probe_errors: list[str] = []
    runtime = store.runtime_status()
    runtime_identity_valid = runtime is not None and all(
        runtime[field] == expected
        for field, expected in (
            ("release_manifest_hash", manifest["release_manifest_hash"]),
            ("config_hash", policy.config_hash),
            ("policy_hash", policy.policy_hash),
            ("runtime_id", policy.runtime_id),
            ("account_key", ACCOUNT_KEY),
        )
    )

    local_state_writable = False
    try:
        selected = store.rows("SELECT 1 AS ok")
        local_state_writable = bool(
            len(selected) == 1
            and int(selected[0]["ok"]) == 1
            and layout.state_path.is_file()
            and not layout.state_path.is_symlink()
            and os.access(layout.state_path, os.W_OK)
            and os.access(layout.state_path.parent, os.W_OK)
        )
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
        probe_errors.append(f"local_state:{type(exc).__name__}")

    try:
        audit_chain_valid, audit_chain_length, audit_chain_head = store.verify_event_chain()
    except (RuntimeError, sqlite3.Error, ValueError) as exc:
        audit_chain_valid, audit_chain_length, audit_chain_head = False, 0, "0" * 64
        probe_errors.append(f"audit_chain:{type(exc).__name__}")
    current = probe_timer.sample("local_state_and_audit")

    lock_metadata = writer_lock.holder_metadata() if writer_lock.held else {}
    lock_pid = lock_metadata.get("pid")
    writer_lock_process_id = (
        lock_pid
        if isinstance(lock_pid, int) and not isinstance(lock_pid, bool) and lock_pid > 0
        else None
    )
    writer_lock_owner_id = (
        str(lock_metadata["owner_id"])
        if isinstance(lock_metadata.get("owner_id"), str)
        and str(lock_metadata["owner_id"]).strip()
        else None
    )
    new_writer_lock_held = bool(
        writer_lock.held
        and writer_lock_owner_id == writer_lock.owner_id
        and writer_lock_process_id == os.getpid()
        and lock_metadata.get("account_fingerprint") == writer_lock.account_fingerprint
    )

    composition = runtime_composition or RuntimeComposition()
    composition_error: str | None = None
    try:
        composition.bind_release(manifest, release_root=layout.release_root)
    except RuntimeCompositionError as exc:
        composition_error = _external_failure_code(
            "RUNTIME_COMPOSITION_INVALID", exc
        )
        probe_errors.append(composition_error)
    if broker is not None:
        broker_client = broker
    elif composition_error is not None:
        # Never execute an un-inventoried injected component just to populate
        # readiness.  The attended adapter is a release-contained, read-blocked
        # placeholder and the explicit provenance error keeps readiness false.
        broker_client = RobinhoodBrokerAdapter()
    else:
        broker_client = composition.broker_client(
            policy.config["execution"], account_masked=f"••••{policy.account_last4}"
        )
    capabilities = broker_client.capabilities
    descriptor = getattr(broker_client, "descriptor", None)
    broker_account_binding_fingerprint = getattr(
        descriptor, "account_binding_fingerprint", None
    )
    broker_authorization_binding_id = getattr(
        descriptor, "authorization_binding_id", None
    )
    broker_snapshot = None
    broker_error: str | None = None
    broker_account_last4: str | None = None
    broker_snapshot_received_at: datetime | None = None
    account_active = False
    broker_authenticated = False
    persisted_fresh_snapshot_id: str | None = None
    try:
        broker_snapshot = broker_client.get_account_snapshot(f"••••{policy.account_last4}")
        policy.require_account(broker_snapshot.account_masked, broker_snapshot.account_type)
        match = re.search(r"([0-9]{4})$", broker_snapshot.account_masked)
        broker_account_last4 = match.group(1) if match else None
        # Multi-page account evidence is only as fresh as its earliest
        # authoritative observation, not the final page's local receipt.
        broker_snapshot_received_at = broker_snapshot.observed_at.astimezone(timezone.utc)
        account_active = str(broker_snapshot.account_state).strip().lower() in {
            "active",
            "open",
        }
        broker_authenticated = bool(broker_snapshot.auth_point_in_time)
    except Exception as exc:  # connector failures are readiness evidence, not authority
        broker_error = type(exc).__name__
        probe_errors.append(f"broker_read:{broker_error}")
    finally:
        current = probe_timer.sample("broker_read")

    if broker_snapshot is not None and persist_fresh_broker_read:
        try:
            reconciler = AuthoritativeReconciler(
                account_masked=f"••••{policy.account_last4}",
                account_key=ACCOUNT_KEY,
                max_snapshot_age=timedelta(
                    seconds=int(
                        policy.config["evidence"]["broker_snapshot_max_age_seconds"]
                    )
                ),
            )
            reconciliation_report = reconciler.reconcile_snapshot(
                store,
                snapshot=broker_snapshot,
                capabilities=capabilities,
                now=current,
                phase=ReconciliationPhase.STARTUP,
            )
            invalid_envelope = NON_INGESTIBLE_SNAPSHOT_BLOCKERS.intersection(
                reconciliation_report.blockers
            )
            persisted = persist_account_snapshot(
                store,
                account_key=ACCOUNT_KEY,
                snapshot=broker_snapshot,
                reconciliation_report=reconciliation_report,
                accept_reconciliation_envelope=not invalid_envelope,
            )
            persisted_fresh_snapshot_id = persisted.snapshot_id
        except Exception as exc:
            probe_errors.append(f"broker_reconciliation:{type(exc).__name__}")
        finally:
            current = probe_timer.sample("broker_reconciliation")

    daemon_accessible = bool(
        capabilities.supports_account_read
        and capabilities.daemon_transport_configured
    )
    unattended_supported = bool(
        capabilities.supports_equity_review
        and capabilities.supports_equity_place
        and capabilities.supports_equity_cancel
        and capabilities.supports_daemon_writes
        and capabilities.supports_unattended_writes
    )
    confirmation_required = bool(
        capabilities.review_requires_explicit_confirmation
        or capabilities.cancel_requires_explicit_confirmation
    )

    durable_rows = store.rows(
        "SELECT * FROM broker_snapshots WHERE account_key=? "
        "ORDER BY observed_at DESC,received_at DESC,snapshot_id DESC LIMIT 1",
        (ACCOUNT_KEY,),
    )
    durable = durable_rows[0] if durable_rows else None
    durable_snapshot_id = str(durable["snapshot_id"]) if durable is not None else None
    durable_snapshot_received_at = (
        _iso(str(durable["observed_at"]), "durable_snapshot.observed_at")
        if durable is not None
        else None
    )
    reconciliation_event = None
    if durable_snapshot_id is not None:
        rows = store.rows(
            "SELECT event_id FROM audit_events WHERE stream=? "
            "AND event_type='POSITIONS_RECONCILED' AND entity_type='broker_snapshot' "
            "AND entity_id=? ORDER BY sequence DESC LIMIT 1",
            (ACCOUNT_KEY, durable_snapshot_id),
        )
        reconciliation_event = str(rows[0]["event_id"]) if rows else None
    fresh_material_matches = bool(
        broker_snapshot is not None
        and durable is not None
        and _fresh_snapshot_matches_durable(broker_snapshot, durable)
        and (
            not persist_fresh_broker_read
            or persisted_fresh_snapshot_id == durable_snapshot_id
        )
    )
    if broker_snapshot is not None and not fresh_material_matches:
        probe_errors.append("broker_read:DURABLE_MATERIAL_MISMATCH")
    actual_complete = broker_snapshot is not None
    positions_reconciled = bool(
        actual_complete
        and fresh_material_matches
        and broker_snapshot.standard_equity_positions_complete
        and durable is not None
        and durable["positions_reconciled"]
        and reconciliation_event is not None
    )
    standard_orders_reconciled = bool(
        actual_complete
        and fresh_material_matches
        and broker_snapshot.standard_equity_orders_complete
        and durable is not None
        and durable["equity_orders_reconciled"]
    )
    option_positions_reconciled = bool(
        actual_complete
        and fresh_material_matches
        and broker_snapshot.option_positions_complete
        and durable is not None
        and durable["option_positions_reconciled"]
    )
    option_orders_reconciled = bool(
        actual_complete
        and fresh_material_matches
        and broker_snapshot.option_orders_complete
        and durable is not None
        and durable["option_orders_reconciled"]
    )
    advanced_orders_reconciled = bool(
        actual_complete
        and fresh_material_matches
        and broker_snapshot.advanced_orders_complete
        and durable is not None
        and durable["advanced_orders_reconciled"]
    )
    realized_pnl_reconciled = bool(
        actual_complete
        and fresh_material_matches
        and broker_snapshot.daily_realized_pnl_ready
        and durable is not None
        and durable["realized_pnl_reconciled"]
    )
    reconciliation_blocker_count = (
        int(durable["reconciliation_blocker_count"]) if durable is not None else 1
    )
    durable_account_flat = bool(
        durable is not None
        and all(
            int(durable[field]) == 0
            for field in (
                "equity_position_count",
                "equity_nonterminal_order_count",
                "external_material_order_count",
                "option_position_count",
                "option_order_count",
                "advanced_order_count",
                "reconciliation_blocker_count",
            )
        )
    )
    unknown_submissions = int(
        store.rows(
            "SELECT COUNT(*) AS n FROM order_intents WHERE account_key=? "
            "AND state IN ('SUBMITTING','UNKNOWN')",
            (ACCOUNT_KEY,),
        )[0]["n"]
    )

    uncovered_quantity = 0
    try:
        protected = {
            str(row["symbol"]): Decimal(str(row["quantity"]))
            for row in store.rows(
                "SELECT symbol,SUM(working_quantity) AS quantity "
                "FROM protection_obligations WHERE account_key=? AND state='WORKING' "
                "GROUP BY symbol",
                (ACCOUNT_KEY,),
            )
        }
        for row in store.rows(
            "SELECT symbol,quantity FROM positions WHERE account_key=? "
            "AND CAST(quantity AS REAL)<>0",
            (ACCOUNT_KEY,),
        ):
            quantity = Decimal(str(row["quantity"]))
            missing = (
                max(quantity - protected.get(str(row["symbol"]), Decimal("0")), Decimal("0"))
                if quantity > 0
                else abs(quantity)
            )
            uncovered_quantity += int(missing.to_integral_value(rounding=ROUND_CEILING))
    except Exception as exc:
        uncovered_quantity = max(uncovered_quantity, 1)
        probe_errors.append(f"protection_state:{type(exc).__name__}")

    (
        legacy_heartbeat_id,
        legacy_heartbeat_status,
        legacy_heartbeat_config_hash,
        legacy_scheduler_disabled,
        legacy_error,
    ) = _probe_legacy_heartbeat(legacy_heartbeat_path)
    if legacy_error is not None:
        probe_errors.append(legacy_error)
    scheduler_runtime, scheduler_runtime_error = _probe_legacy_scheduler_runtime(
        now=current,
        observed=legacy_scheduler_runtime_evidence,
    )
    if scheduler_runtime_error is not None:
        probe_errors.append(scheduler_runtime_error)
    scheduler_runtime_disabled = bool(
        scheduler_runtime is not None
        and scheduler_runtime.automation_id == legacy_heartbeat_id
        and scheduler_runtime.status.upper() == legacy_heartbeat_status.upper()
        and scheduler_runtime.config_hash == legacy_heartbeat_config_hash
        and scheduler_runtime.active_execution_count == 0
    )
    if scheduler_runtime is not None and not scheduler_runtime_disabled:
        probe_errors.append("legacy_retirement:SCHEDULER_RUNTIME_BINDING_MISMATCH")
    process_observations, process_error = _probe_legacy_writer_processes(
        legacy_process_listing
    )
    current = probe_timer.sample("legacy_retirement_reads")
    current_drain_proven = _legacy_drain_proven(
        broker_snapshot=broker_snapshot,
        durable_account_flat=durable_account_flat,
        standard_orders_reconciled=standard_orders_reconciled,
        option_positions_reconciled=option_positions_reconciled,
        option_orders_reconciled=option_orders_reconciled,
        advanced_orders_reconciled=advanced_orders_reconciled,
        positions_reconciled=positions_reconciled,
        realized_pnl_reconciled=realized_pnl_reconciled,
        reconciliation_blocker_count=reconciliation_blocker_count,
        unknown_submissions=unknown_submissions,
        uncovered_quantity=uncovered_quantity,
    )
    try:
        old_writer_disabled, retirement_error = _verify_legacy_retirement_receipt(
            store=store,
            manifest=manifest,
            policy=policy,
            writer_lock=writer_lock,
            legacy_heartbeat_id=legacy_heartbeat_id,
            legacy_heartbeat_status=legacy_heartbeat_status,
            legacy_heartbeat_config_hash=legacy_heartbeat_config_hash,
            scheduler_disabled=(
                legacy_scheduler_disabled and scheduler_runtime_disabled
            ),
            scheduler_runtime=scheduler_runtime,
            process_observations=process_observations,
            process_error=process_error,
            current_drain_proven=current_drain_proven,
            now=current,
        )
    except (RuntimeError, sqlite3.Error, TypeError, ValueError) as exc:
        old_writer_disabled = False
        retirement_error = f"legacy_retirement:{type(exc).__name__}"
    if retirement_error is not None:
        probe_errors.append(retirement_error)

    market_data_connected = False
    market_data_resynced = False
    market_data_blockers: tuple[str, ...] = ("MASSIVE_HEALTH_NOT_PROBED",)
    latest_quote_at: datetime | None = None
    latest_completed_bar_at: datetime | None = None
    try:
        market_config = policy.config["market_data"]
        composed_market_source = (
            composition.market_source(policy.config["discovery"])
            if runtime_composition is not None
            else None
        )
        source = market_source or composed_market_source or LocalMassiveReadOnlySource(
            market_config["database_path"],
            pilot_id=str(market_config["producer_pilot_id"]),
            book_mode=str(market_config["producer_book_mode"]),
            decision_contract_hash=str(market_config["producer_decision_contract_hash"]),
            health_max_age_seconds=int(market_config["health_max_age_seconds"]),
            candidate_max_age_seconds=int(market_config["candidate_max_age_seconds"]),
        )
        market_probe_started = probe_timer.sample("market_read_start")
        health = source.health(now=market_probe_started)
        current = probe_timer.sample("market_read")
        market_data_blockers = tuple(health.blockers)
        market_data_connected = not any(
            item.startswith("MASSIVE_STORE_UNAVAILABLE") for item in health.blockers
        )
        market_data_resynced = bool(health.producer_fresh)
        latest_quote_at = health.latest_quote_at
        latest_completed_bar_at = health.latest_completed_bar_at
    except Exception as exc:
        market_data_blockers = (f"MASSIVE_PROBE_FAILED:{type(exc).__name__}",)
        probe_errors.append(f"market_data:{type(exc).__name__}")
        current = probe_timer.sample("market_read_failed")

    discovery = policy.config["discovery"]
    if runtime_composition is not None:
        try:
            tradability_provider_ready = composition.tradability_ready(
                discovery, now=current
            )
        except Exception as exc:
            tradability_provider_ready = False
            probe_errors.append(f"discovery_provider:{type(exc).__name__}")
    else:
        tradability_provider_ready = bool(
            callable(getattr(broker_client, "get_equity_tradability", None))
            and discovery.get("pipeline_configured") is True
            and discovery.get("instrument_evidence_provider") != "unavailable"
        )

    notification = policy.config["notifications"]
    notification_sink = str(notification["delivery_sink"])
    notification_route: NotificationRoute | None = None
    notification_destination_configured = False
    if notification_sink != "local_jsonl_staging":
        try:
            composed_sink = composition.notification_sink(
                notification,
                local_jsonl_path=layout.notification_path,
            )
            notification_route = notification_route_from_config(notification)
            notification_destination_configured = bool(
                notification.get("destination_bridge_configured") is True
                and composed_sink.route == notification_route
                and notification_route.provider != "local_jsonl"
            )
        except Exception as exc:
            probe_errors.append(f"notification_route:{type(exc).__name__}")
    notification_receipt_hash: str | None = None
    notification_delivered_at: datetime | None = None
    notification_tested = False
    notification_worker_healthy = False
    if notification_destination_configured and notification_route is not None:
        verified_receipt = _verified_notification_receipt(
            store,
            account_key=ACCOUNT_KEY,
            route=notification_route,
        )
        if verified_receipt is not None:
            notification_receipt_hash, notification_delivered_at = verified_receipt
    current = probe_timer.sample("notification_receipt_read")

    # Reconciliation persistence appends audit events, so bind the evidence to
    # the chain *after* the exact broker read has been durably recorded.
    try:
        audit_chain_valid, audit_chain_length, audit_chain_head = store.verify_event_chain()
    except (RuntimeError, sqlite3.Error, ValueError) as exc:
        audit_chain_valid, audit_chain_length, audit_chain_head = False, 0, "0" * 64
        probe_errors.append(f"audit_chain_post_reconciliation:{type(exc).__name__}")

    current = probe_timer.sample("probe_complete")
    probe_errors.extend(probe_timer.errors)
    if notification_destination_configured and notification_route is not None:
        notification_worker_healthy, worker_errors = notification_worker_health(
            store,
            account_key=ACCOUNT_KEY,
            route=notification_route,
            now=current,
        )
        probe_errors.extend(worker_errors)
    if notification_delivered_at is not None:
        delivery_age = _age_seconds(current, notification_delivered_at)
        notification_tested = bool(
            notification_destination_configured
            and notification_worker_healthy
            and delivery_age is not None
            and 0 <= delivery_age <= _NOTIFICATION_RECEIPT_MAX_AGE_SECONDS
        )
    quote_age_seconds = _age_seconds(current, latest_quote_at)
    completed_bar_age_seconds = _age_seconds(current, latest_completed_bar_at)

    return ReadinessEvidence(
        collected_at=current,
        release_manifest_hash=str(manifest["release_manifest_hash"]),
        config_hash=policy.config_hash,
        policy_hash=policy.policy_hash,
        runtime_id=policy.runtime_id,
        database_schema_version=store.schema_version,
        account_key=ACCOUNT_KEY,
        account_last4=policy.account_last4,
        runtime_identity_valid=runtime_identity_valid,
        evidence_source=MACHINE_EVIDENCE_SOURCE,
        broker_connector=str(capabilities.connector),
        broker_read_attempted=True,
        broker_read_succeeded=broker_snapshot is not None,
        broker_read_error_type=broker_error,
        broker_account_last4=broker_account_last4,
        broker_snapshot_received_at=broker_snapshot_received_at,
        account_active=account_active,
        broker_authenticated=broker_authenticated,
        daemon_accessible_supported_client=daemon_accessible,
        unattended_mutation_supported=unattended_supported,
        per_mutation_confirmation_required=confirmation_required,
        durable_snapshot_id=durable_snapshot_id,
        durable_snapshot_received_at=durable_snapshot_received_at,
        reconciliation_audit_event_id=reconciliation_event,
        standard_orders_reconciled=standard_orders_reconciled,
        option_positions_reconciled=option_positions_reconciled,
        option_orders_reconciled=option_orders_reconciled,
        advanced_orders_reconciled=advanced_orders_reconciled,
        positions_reconciled=positions_reconciled,
        realized_pnl_reconciled=realized_pnl_reconciled,
        reconciliation_blocker_count=reconciliation_blocker_count,
        durable_account_flat=durable_account_flat,
        unknown_submissions=unknown_submissions,
        uncovered_quantity=uncovered_quantity,
        legacy_heartbeat_id=legacy_heartbeat_id,
        legacy_heartbeat_status=legacy_heartbeat_status,
        legacy_heartbeat_config_hash=legacy_heartbeat_config_hash,
        old_writer_disabled=old_writer_disabled,
        new_writer_lock_held=new_writer_lock_held,
        writer_lock_owner_id=writer_lock_owner_id,
        writer_lock_process_id=writer_lock_process_id,
        local_state_writable=local_state_writable,
        audit_chain_valid=audit_chain_valid,
        audit_chain_length=audit_chain_length,
        audit_chain_head=audit_chain_head,
        market_data_connected=market_data_connected,
        market_data_resynced=market_data_resynced,
        market_data_blockers=market_data_blockers,
        tradability_provider_ready=tradability_provider_ready,
        notification_sink=notification_sink,
        notification_destination_configured=notification_destination_configured,
        notification_delivery_receipt_hash=notification_receipt_hash,
        notification_delivered_at=notification_delivered_at,
        notification_tested=notification_tested,
        broker_snapshot_age_seconds=_age_seconds(current, broker_snapshot_received_at),
        durable_snapshot_age_seconds=_age_seconds(current, durable_snapshot_received_at),
        quote_age_seconds=quote_age_seconds,
        completed_bar_age_seconds=completed_bar_age_seconds,
        probe_errors=tuple(dict.fromkeys(probe_errors)),
        probe_started_at=probe_timer.started_at,
        probe_completed_at=current,
        probe_elapsed_monotonic_seconds=probe_timer.elapsed_monotonic_seconds,
        probe_clock_stable=probe_timer.clock_stable,
        broker_account_binding_fingerprint=broker_account_binding_fingerprint,
        broker_authorization_binding_id=broker_authorization_binding_id,
        component_provenance_hash=(
            composition.component_provenance_hash
            if broker_account_binding_fingerprint is not None
            and broker_authorization_binding_id is not None
            else None
        ),
    )


def _read_runtime_and_lease(
    layout: InstallLayout,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Read control-plane state without opening a second SQLite writer."""

    if not layout.state_path.exists():
        raise CommandBlocked("runtime state is not initialized; run init-state first")
    connection = sqlite3.connect(
        f"file:{layout.state_path}?mode=ro", uri=True, timeout=1.0
    )
    connection.row_factory = sqlite3.Row
    try:
        runtime_rows = connection.execute(
            "SELECT * FROM runtime_identity WHERE singleton=1"
        ).fetchall()
        lease_rows = connection.execute(
            """SELECT * FROM account_writer_lease
                 WHERE account_key=? AND released_at IS NULL""",
            (ACCOUNT_KEY,),
        ).fetchall()
    finally:
        connection.close()
    if len(runtime_rows) != 1 or len(lease_rows) > 1:
        raise CommandBlocked("runtime identity or writer lease is ambiguous")
    return dict(runtime_rows[0]), (dict(lease_rows[0]) if lease_rows else None)


def _queue_runtime_control(
    layout: InstallLayout,
    *,
    command: str,
    reason: str,
    arguments: Mapping[str, Any] | None = None,
    composition: RuntimeComposition,
) -> Mapping[str, Any]:
    manifest, policy = layout.load_release()
    try:
        composition.bind_release(manifest, release_root=layout.release_root)
    except RuntimeCompositionError as exc:
        raise CommandBlocked(
            _external_failure_code("RUNTIME_COMPOSITION_INVALID", exc)
        ) from exc
    runtime, lease = _read_runtime_and_lease(layout)
    for field, expected in (
        ("release_manifest_hash", manifest["release_manifest_hash"]),
        ("config_hash", policy.config_hash),
        ("policy_hash", policy.policy_hash),
        ("runtime_id", policy.runtime_id),
        ("account_key", ACCOUNT_KEY),
    ):
        if runtime[field] != expected:
            raise CommandBlocked(f"runtime binding mismatch: {field}")
    if not bool(runtime["authority_enabled"]) or runtime["activated_at"] is None:
        raise CommandBlocked("runtime has no activated authority")

    # A queued safety command is useful only when the sole writer is actually
    # present to consume it.  The kernel lock is authority; the matching DB
    # lease proves it belongs to the live service rather than another tool.
    probe = _account_writer_lock(layout, policy, owner_id="control-probe")
    try:
        probe.acquire(blocking=False)
    except WriterLockBusy:
        holder = probe.holder_metadata()
    else:
        probe.release()
        raise CommandBlocked("full-live service writer is not running")
    if lease is None or lease["owner_id"] != holder.get("owner_id"):
        raise CommandBlocked("kernel writer lock and database lease do not match")

    inbox = composition.control_inbox(
        layout.control_path,
        account_key=ACCOUNT_KEY,
        runtime_id=policy.runtime_id,
        release_manifest_hash=manifest["release_manifest_hash"],
        max_snapshot_age=timedelta(
            seconds=int(policy.config["evidence"]["broker_snapshot_max_age_seconds"])
        ),
        execution_config=policy.config["execution"],
    )
    payload = inbox.submit(
        command,
        reason=reason,
        requested_at=_now(),
        activated_at=str(runtime["activated_at"]),
        arguments=arguments,
    )
    return {
        "queued": True,
        "applied": False,
        "request_id": payload["request_id"],
        "command": command,
        "service_writer_verified": True,
        "verification": "re-run status and inspect runtime mode plus control queue counts",
    }


def command_init_state(args: argparse.Namespace) -> int:
    layout = InstallLayout(args.install_root)
    manifest, policy = layout.load_release()
    with _account_writer_lock(layout, policy):
        with LiveStateStore(layout.state_path) as store:
            created = store.initialize_runtime(
                runtime_id=policy.runtime_id,
                account_key=ACCOUNT_KEY,
                release_manifest_hash=manifest["release_manifest_hash"],
                config_hash=policy.config_hash,
                policy_hash=policy.policy_hash,
                initialized_at=_now(),
            )
            status = dict(store.runtime_status() or {})
    _print({"created": created, "state_path": str(layout.state_path), "runtime": status})
    return 0


def _doctor(
    layout: InstallLayout,
    *,
    runtime_composition: RuntimeComposition | None = None,
) -> dict[str, Any]:
    blockers: list[str] = []
    chain_ok = False
    chain_length = 0
    chain_head: str | None = None
    manifest: dict[str, Any] | None = None
    policy: PolicyBundle | None = None
    try:
        manifest, policy = layout.load_release()
    except Exception as exc:
        blockers.append(_external_failure_code("RELEASE_INVALID", exc))
    runtime: Mapping[str, Any] | None = None
    if not layout.state_path.exists():
        blockers.append("STATE_NOT_INITIALIZED")
    else:
        try:
            with LiveStateStore(layout.state_path) as store:
                runtime = store.runtime_status()
                chain_ok, chain_length, chain_head = store.verify_event_chain()
            if runtime is None:
                blockers.append("RUNTIME_IDENTITY_MISSING")
            if not chain_ok:
                blockers.append("AUDIT_CHAIN_INVALID")
        except Exception as exc:
            blockers.append(_external_failure_code("STATE_INVALID", exc))
    if manifest and policy:
        blockers.extend(policy.activation_blockers)
        if not policy.live_entries_configured:
            blockers.append("LIVE_ENTRIES_DISABLED_IN_SIGNED_CONFIG")
        if runtime is not None:
            bindings = {
                "release_manifest_hash": manifest["release_manifest_hash"],
                "config_hash": policy.config_hash,
                "policy_hash": policy.policy_hash,
                "runtime_id": policy.runtime_id,
                "account_key": ACCOUNT_KEY,
            }
            for field, expected in bindings.items():
                if runtime[field] != expected:
                    blockers.append(f"RUNTIME_BINDING_MISMATCH:{field}")
    broker_config = (
        policy.config["execution"]
        if policy is not None
        else {"broker_adapter": "robinhood_codex_connector"}
    )
    composition = runtime_composition or RuntimeComposition()
    composition_error: str | None = None
    if manifest is not None:
        try:
            composition.bind_release(manifest, release_root=layout.release_root)
        except RuntimeCompositionError as exc:
            composition_error = _external_failure_code(
                "RUNTIME_COMPOSITION_INVALID", exc
            )
            blockers.append(composition_error)
    account_masked = f"••••{policy.account_last4}" if policy is not None else "••••0000"
    if composition_error is not None:
        capabilities = RobinhoodBrokerAdapter().capabilities
    else:
        try:
            capabilities = composition.broker_client(
                broker_config, account_masked=account_masked
            ).capabilities
        except Exception as exc:
            blockers.append(
                _external_failure_code("BROKER_COMPOSITION_INVALID", exc)
            )
            capabilities = RobinhoodBrokerAdapter().capabilities
    if not capabilities.daemon_transport_configured:
        blockers.append("DAEMON_BROKER_TRANSPORT_UNAVAILABLE")
    if not capabilities.supports_unattended_writes:
        blockers.append("UNATTENDED_BROKER_WRITES_UNSUPPORTED")
    if capabilities.review_requires_explicit_confirmation:
        blockers.append("PER_MUTATION_CONFIRMATION_REQUIRED")
    if not capabilities.can_prove_whole_broker_reconciliation:
        blockers.append("WHOLE_BROKER_RECONCILIATION_UNSUPPORTED")
    market_report: dict[str, Any] | None = None
    if policy is not None:
        market_config = policy.config["market_data"]
        try:
            market_source = composition.market_source(policy.config["discovery"])
            if market_source is None:
                if policy.config["discovery"].get("pipeline_configured") is True:
                    raise RuntimeCompositionError(
                        "configured discovery pipeline has no release-bound provider"
                    )
                market_source = LocalMassiveReadOnlySource(
                    market_config["database_path"],
                    pilot_id=str(market_config["producer_pilot_id"]),
                    book_mode=str(market_config["producer_book_mode"]),
                    decision_contract_hash=str(
                        market_config["producer_decision_contract_hash"]
                    ),
                    health_max_age_seconds=int(market_config["health_max_age_seconds"]),
                    candidate_max_age_seconds=int(
                        market_config["candidate_max_age_seconds"]
                    ),
                )
            feed = market_source.health(now=_now())
            blockers.extend(feed.blockers)
            market_report = {
                "adapter": market_config["adapter"],
                "database_path": feed.database_path,
                "producer_fresh": feed.producer_fresh,
                "latest_quote_at": feed.latest_quote_at,
                "latest_completed_bar_at": feed.latest_completed_bar_at,
                "component_states": dict(feed.component_states),
                "blockers": list(feed.blockers),
                "source_is_execution_authority": False,
            }
        except Exception as exc:
            error = _external_failure_code("MASSIVE_ADAPTER_INVALID", exc)
            blockers.append(error)
            market_report = {"adapter": market_config.get("adapter"), "blockers": [error]}
    return {
        "schema_version": "titan_full_live_doctor_2026-09-08_v1",
        "install_root": str(layout.root),
        "release_valid": manifest is not None and policy is not None,
        "release_manifest_hash": manifest.get("release_manifest_hash") if manifest else None,
        "source_commit": manifest.get("source_commit") if manifest else None,
        "state_present": layout.state_path.exists(),
        "runtime": dict(runtime) if runtime is not None else None,
        "audit_chain": {
            "valid": chain_ok,
            "length": chain_length,
            "head": chain_head,
        },
        "broker": {
            "connector": capabilities.connector,
            "daemon_transport_configured": capabilities.daemon_transport_configured,
            "unattended_writes": capabilities.supports_unattended_writes,
            "per_mutation_confirmation_required": capabilities.review_requires_explicit_confirmation,
            "whole_broker_reconciliation": capabilities.can_prove_whole_broker_reconciliation,
        },
        "market_data": market_report,
        "ready_for_owner_activation": not blockers,
        "blockers": list(dict.fromkeys(blockers)),
    }


def command_doctor(args: argparse.Namespace) -> int:
    report = _doctor(
        InstallLayout(args.install_root),
        runtime_composition=_runtime_composition(args),
    )
    _print(report)
    return 0 if report["ready_for_owner_activation"] else 2


def command_status(args: argparse.Namespace) -> int:
    layout = InstallLayout(args.install_root)
    report = _doctor(layout, runtime_composition=_runtime_composition(args))
    if layout.state_path.exists():
        with LiveStateStore(layout.state_path) as store:
            report["counts"] = {
                "nonzero_positions": store.rows(
                    "SELECT COUNT(*) AS n FROM positions WHERE CAST(quantity AS REAL)<>0"
                )[0]["n"],
                "unknown_or_submitting_intents": store.rows(
                    "SELECT COUNT(*) AS n FROM order_intents WHERE state IN ('UNKNOWN','SUBMITTING')"
                )[0]["n"],
                "open_incidents": store.rows(
                    "SELECT COUNT(*) AS n FROM incidents WHERE resolved_at IS NULL"
                )[0]["n"],
                "pending_notifications": store.rows(
                    "SELECT COUNT(*) AS n FROM notification_outbox WHERE state='PENDING'"
                )[0]["n"],
            }
    report["control_requests"] = {
        name: len(tuple((layout.control_path / name).glob("*.json")))
        if (layout.control_path / name).is_dir()
        else 0
        for name in ("inbox", "processed", "rejected")
    }
    _print(report)
    return 0


def command_serve(args: argparse.Namespace) -> int:
    layout = InstallLayout(args.install_root)
    manifest, policy = layout.load_release()
    store = _open_state(layout)
    try:
        runtime = store.runtime_status()
        if runtime is None:
            raise CommandBlocked("runtime identity is missing")
        for field, expected in (
            ("release_manifest_hash", manifest["release_manifest_hash"]),
            ("config_hash", policy.config_hash),
            ("policy_hash", policy.policy_hash),
            ("account_key", ACCOUNT_KEY),
        ):
            if runtime[field] != expected:
                raise CommandBlocked(f"runtime binding mismatch: {field}")
        activated_profile_hash = _activated_runtime_profile_hash(store, runtime)
        composition = _runtime_composition(args)
        try:
            composition.bind_release(manifest, release_root=layout.release_root)
        except RuntimeCompositionError as exc:
            raise CommandBlocked(
                _external_failure_code("RUNTIME_COMPOSITION_INVALID", exc)
            ) from exc
        broker = composition.broker_client(
            policy.config["execution"], account_masked=f"••••{policy.account_last4}"
        )
        writer_lock = _account_writer_lock(layout, policy)
        latency = LatencyRecorder(LiveStateLatencyAdapter(store, ACCOUNT_KEY))
        # Validate the exact signed route and injected provider binding at
        # startup. The trading coordinator still receives only enqueue
        # authority; the independent notification worker owns provider I/O.
        notification_sink = composition.notification_sink(
            policy.config["notifications"],
            local_jsonl_path=layout.notification_path,
        )
        lifecycle = ProductionLifecycleActions(
            policy=policy,
            state=store,
            broker=broker,
            writer_lock=writer_lock,
            discovery=None,
            latency=latency,
            allow_mutations=bool(
                policy.config["execution"].get(
                    "local_mutation_interlock_enabled", False
                )
            ),
        )
        discovery = composition.build_discovery_executor(
            policy=policy,
            state=store,
            broker=broker,
            writer_lock=writer_lock,
            latency=latency,
            authority=lifecycle,
        )
        lifecycle.discovery = discovery
        control_inbox = composition.control_inbox(
            layout.control_path,
            account_key=ACCOUNT_KEY,
            runtime_id=policy.runtime_id,
            release_manifest_hash=manifest["release_manifest_hash"],
            max_snapshot_age=timedelta(
                seconds=int(
                    policy.config["evidence"]["broker_snapshot_max_age_seconds"]
                )
            ),
            execution_config=policy.config["execution"],
        )
        if activated_profile_hash is not None:
            try:
                composition.assert_runtime_profile(activated_profile_hash)
            except RuntimeCompositionError as exc:
                raise CommandBlocked(
                    _external_failure_code("RUNTIME_COMPOSITION_INVALID", exc)
                ) from exc
        service = FullLiveService(
            policy=policy,
            state=store,
            broker=broker,
            notifications=build_enqueue_only_outbox(store, ACCOUNT_KEY),
            notification_route=notification_sink.route,
            actions=lifecycle,
        )
        runner = ServiceRunner(
            service=service,
            lock=writer_lock,
            interval_seconds=float(policy.config["execution"]["reconcile_interval_seconds"]),
            control_inbox=control_inbox,
        )
        result = runner.run(once=args.once)
        if result is not None:
            _print(asdict(result))
        return 0
    finally:
        store.close()


def command_prepare_activation(args: argparse.Namespace) -> int:
    layout = InstallLayout(args.install_root)
    manifest, policy = layout.load_release()
    probe_started = _now()
    lock = _account_writer_lock(
        layout, policy, owner_id="activation-readiness-prepare"
    )
    with lock:
        with _open_state(layout) as store:
            store.acquire_writer_lease(
                account_key=ACCOUNT_KEY,
                owner_id=lock.owner_id,
                acquired_at=probe_started,
                recover_stale=True,
            )
            try:
                runtime = store.runtime_status()
                if runtime is None or runtime["mode"] != EngineMode.PAUSED.value:
                    raise CommandBlocked("runtime must be PAUSED before activation preparation")
                if bool(runtime["authority_enabled"]):
                    raise CommandBlocked("runtime authority is already enabled")
                readiness = _machine_readiness(
                    layout=layout,
                    manifest=manifest,
                    policy=policy,
                    store=store,
                    writer_lock=lock,
                    now=probe_started,
                    clock=_now,
                    runtime_composition=_runtime_composition(args),
                )
                created = readiness.collected_at
                expires = created + timedelta(seconds=args.ttl_seconds)
                record = ActivationRecord.build(
                    release_manifest_hash=manifest["release_manifest_hash"],
                    policy=policy,
                    database_schema_version=store.schema_version,
                    created_at=created,
                    expires_at=expires,
                    readiness=readiness,
                )
                record.validate(
                    policy=policy,
                    release_manifest_hash=manifest["release_manifest_hash"],
                    database_schema_version=store.schema_version,
                    now=readiness.collected_at,
                    already_consumed=False,
                    current_readiness=readiness,
                )
                payload = _record_payload(record)
                store.record_activation(
                    activation_id=record.activation_id,
                    account_key=ACCOUNT_KEY,
                    record=payload,
                    created_at=created,
                    expires_at=expires,
                )
            finally:
                store.release_writer_lease(
                    account_key=ACCOUNT_KEY,
                    owner_id=lock.owner_id,
                    released_at=_now(),
                )
    phrase = f"ACTIVATE FULL LIVE ending-7153 {record.activation_id}"
    _print(
        {
            "activation": payload,
            "readiness_hash": readiness.evidence_hash,
            "exact_confirmation_phrase": phrase,
        }
    )
    return 0


def command_readiness(args: argparse.Namespace) -> int:
    """Print freshly machine-collected evidence without staging authority."""

    layout = InstallLayout(args.install_root)
    manifest, policy = layout.load_release()
    observed = _now()
    lock = _account_writer_lock(
        layout, policy, owner_id="activation-readiness-inspect"
    )
    with lock:
        with _open_state(layout) as store:
            store.acquire_writer_lease(
                account_key=ACCOUNT_KEY,
                owner_id=lock.owner_id,
                acquired_at=observed,
                recover_stale=True,
            )
            try:
                readiness = _machine_readiness(
                    layout=layout,
                    manifest=manifest,
                    policy=policy,
                    store=store,
                    writer_lock=lock,
                    now=observed,
                    clock=_now,
                    runtime_composition=_runtime_composition(args),
                )
                blockers = readiness.blockers(policy, now=_now())
            finally:
                store.release_writer_lease(
                    account_key=ACCOUNT_KEY,
                    owner_id=lock.owner_id,
                    released_at=_now(),
                )
    _print(
        {
            "machine_collected": True,
            "readiness_hash": readiness.evidence_hash,
            "readiness": readiness.to_payload(),
            "ready_for_owner_activation": not blockers,
            "blockers": list(blockers),
        }
    )
    return 0 if not blockers else 2


def command_record_legacy_retirement(args: argparse.Namespace) -> int:
    """Record machine-observed writer retirement; never stop or signal a process."""

    layout = InstallLayout(args.install_root)
    manifest, policy = layout.load_release()
    started = _now()
    lock = _account_writer_lock(
        layout, policy, owner_id="legacy-retirement-recorder"
    )
    with lock:
        with _open_state(layout) as store:
            store.acquire_writer_lease(
                account_key=ACCOUNT_KEY,
                owner_id=lock.owner_id,
                acquired_at=started,
                recover_stale=True,
            )
            try:
                runtime = store.runtime_status()
                if (
                    runtime is None
                    or runtime["mode"] != EngineMode.PAUSED.value
                    or bool(runtime["authority_enabled"])
                ):
                    raise CommandBlocked(
                        "legacy retirement can be recorded only while authority is PAUSED"
                    )
                scheduler_runtime, scheduler_error = _probe_legacy_scheduler_runtime(
                    now=_now()
                )
                if scheduler_error is not None or scheduler_runtime is None:
                    raise CommandBlocked(
                        scheduler_error
                        or "legacy scheduler runtime identity is unavailable"
                    )
                evidence = _machine_readiness(
                    layout=layout,
                    manifest=manifest,
                    policy=policy,
                    store=store,
                    writer_lock=lock,
                    now=started,
                    clock=_now,
                    legacy_scheduler_runtime_evidence=scheduler_runtime,
                    runtime_composition=_runtime_composition(args),
                )
                process_observations, process_error = _probe_legacy_writer_processes()
                scheduler_disabled = bool(
                    evidence.legacy_heartbeat_status in {"PAUSED", "DISABLED"}
                    and scheduler_runtime.automation_id
                    == evidence.legacy_heartbeat_id
                    and scheduler_runtime.status.upper()
                    == evidence.legacy_heartbeat_status
                    and scheduler_runtime.config_hash
                    == evidence.legacy_heartbeat_config_hash
                    and scheduler_runtime.active_execution_count == 0
                )
                drain_proven = all(
                    (
                        evidence.broker_read_succeeded,
                        evidence.standard_orders_reconciled,
                        evidence.option_positions_reconciled,
                        evidence.option_orders_reconciled,
                        evidence.advanced_orders_reconciled,
                        evidence.positions_reconciled,
                        evidence.realized_pnl_reconciled,
                        evidence.durable_account_flat,
                        evidence.reconciliation_blocker_count == 0,
                        evidence.unknown_submissions == 0,
                        evidence.uncovered_quantity == 0,
                    )
                )
                if not scheduler_disabled or evidence.legacy_heartbeat_config_hash is None:
                    raise CommandBlocked("legacy scheduler disablement is not proven")
                if process_error is not None:
                    raise CommandBlocked(process_error)
                if process_observations:
                    raise CommandBlocked("known legacy writer process is still running")
                if not drain_proven:
                    raise CommandBlocked("broker in-flight drain is not proven")
                if not evidence.new_writer_lock_held:
                    raise CommandBlocked("shared account gateway is not exclusively owned")
                if (
                    evidence.durable_snapshot_id is None
                    or evidence.reconciliation_audit_event_id is None
                    or evidence.writer_lock_owner_id is None
                    or evidence.writer_lock_process_id is None
                ):
                    raise CommandBlocked("retirement evidence is incomplete")
                recorded_at = _now()
                payload = _legacy_retirement_payload(
                    manifest=manifest,
                    policy=policy,
                    legacy_heartbeat_id=evidence.legacy_heartbeat_id,
                    legacy_heartbeat_status=evidence.legacy_heartbeat_status,
                    legacy_heartbeat_config_hash=evidence.legacy_heartbeat_config_hash,
                    scheduler_runtime=scheduler_runtime,
                    durable_snapshot_id=evidence.durable_snapshot_id,
                    reconciliation_audit_event_id=evidence.reconciliation_audit_event_id,
                    writer_lock=lock,
                    writer_lock_owner_id=evidence.writer_lock_owner_id,
                    writer_lock_process_id=evidence.writer_lock_process_id,
                    recorded_at=recorded_at,
                )
                receipt_id = object_hash(payload)
                store.append_event(
                    stream=ACCOUNT_KEY,
                    event_type="LEGACY_ACCOUNT_WRITER_RETIRED",
                    entity_type="legacy_writer_retirement",
                    entity_id=receipt_id,
                    occurred_at=recorded_at,
                    payload=payload,
                )
            finally:
                store.release_writer_lease(
                    account_key=ACCOUNT_KEY,
                    owner_id=lock.owner_id,
                    released_at=_now(),
                )
    _print(
        {
            "recorded": True,
            "receipt_id": receipt_id,
            "scheduler_changed": False,
            "process_signalled": False,
            "broker_mutation_attempted": False,
            "next_step": "run readiness again to re-probe retirement and all live facts",
        }
    )
    return 0


def command_activate(args: argparse.Namespace) -> int:
    layout = InstallLayout(args.install_root)
    manifest, policy = layout.load_release()
    now = _now()
    expected_phrase = f"ACTIVATE FULL LIVE ending-7153 {args.activation_id}"
    if args.confirm != expected_phrase:
        raise CommandBlocked("exact activation confirmation phrase does not match")
    lock = _account_writer_lock(
        layout, policy, owner_id="activation-readiness-consume"
    )
    with lock:
        with _open_state(layout) as store:
            store.acquire_writer_lease(
                account_key=ACCOUNT_KEY,
                owner_id=lock.owner_id,
                acquired_at=now,
                recover_stale=True,
            )
            try:
                rows = store.rows(
                    "SELECT * FROM activation_records WHERE activation_id=?",
                    (args.activation_id,),
                )
                if len(rows) != 1:
                    raise CommandBlocked("activation record was not found")
                record = _activation_from_row(rows[0])
                readiness = _machine_readiness(
                    layout=layout,
                    manifest=manifest,
                    policy=policy,
                    store=store,
                    writer_lock=lock,
                    now=now,
                    clock=_now,
                    runtime_composition=_runtime_composition(args),
                    # The activation record is bound to the exact durable
                    # snapshot staged by prepare-activation.  A second live
                    # read must match all material facts, but must not replace
                    # that hash-bound snapshot before state.py consumes it.
                    persist_fresh_broker_read=False,
                )
                record.validate(
                    policy=policy,
                    release_manifest_hash=manifest["release_manifest_hash"],
                    database_schema_version=store.schema_version,
                    now=readiness.collected_at,
                    already_consumed=rows[0]["consumed_at"] is not None,
                    current_readiness=readiness,
                )
                store.activate_runtime(
                    args.activation_id,
                    activated_at=readiness.collected_at,
                    confirmation_phrase=args.confirm,
                    writer_owner_id=lock.owner_id,
                )
                runtime = dict(store.runtime_status() or {})
            finally:
                store.release_writer_lease(
                    account_key=ACCOUNT_KEY,
                    owner_id=lock.owner_id,
                    released_at=_now(),
                )
    _print(
        {
            "activated": True,
            "runtime": runtime,
            "note": "authority is armed in RECONCILING; ACTIVE requires a fresh service-side whole-broker reconciliation",
        }
    )
    return 0


def command_pause_new_entries(args: argparse.Namespace) -> int:
    layout = InstallLayout(args.install_root)
    result = _queue_runtime_control(
        layout,
        command="PAUSE_NEW_ENTRIES",
        reason=args.reason,
        composition=_runtime_composition(args),
    )
    _print({**result, "exit_authority_preserved": True})
    return 0


def command_managed_closeout(args: argparse.Namespace) -> int:
    layout = InstallLayout(args.install_root)
    _print(
        _queue_runtime_control(
            layout,
            command="MANAGED_CLOSEOUT",
            reason=args.reason,
            composition=_runtime_composition(args),
        )
    )
    return 0


def command_deactivate(args: argparse.Namespace) -> int:
    layout = InstallLayout(args.install_root)
    phrase = f"DEACTIVATE FULL LIVE ending-7153 FLAT {args.flatness_snapshot_id}"
    if args.confirm != phrase:
        raise CommandBlocked("exact deactivation confirmation phrase does not match")
    _print(
        _queue_runtime_control(
            layout,
            command="DEACTIVATE_FLAT",
            reason=args.reason,
            arguments={"flatness_snapshot_id": args.flatness_snapshot_id},
            composition=_runtime_composition(args),
        )
    )
    return 0


def command_notification_test(args: argparse.Namespace) -> int:
    layout = InstallLayout(args.install_root)
    manifest, policy = layout.load_release()
    composition = _runtime_composition(args)
    try:
        composition.bind_release(manifest, release_root=layout.release_root)
    except RuntimeCompositionError as exc:
        raise CommandBlocked(
            _external_failure_code("RUNTIME_COMPOSITION_INVALID", exc)
        ) from exc
    sink = composition.notification_sink(
        policy.config["notifications"],
        local_jsonl_path=layout.notification_path,
    )
    lock = _account_writer_lock(
        layout, policy, owner_id="notification-readiness-test"
    )
    with lock:
        with _open_state(layout) as store:
            runtime = store.runtime_status()
            if (
                runtime is None
                or runtime["mode"] != EngineMode.PAUSED.value
                or bool(runtime["authority_enabled"])
            ):
                raise CommandBlocked(
                    "notification test requires the full-live service unarmed and PAUSED"
                )
            now = _now()
            store.acquire_writer_lease(
                account_key=ACCOUNT_KEY,
                owner_id=lock.owner_id,
                acquired_at=now,
                recover_stale=True,
            )
            try:
                publisher = build_enqueue_only_outbox(store, ACCOUNT_KEY)
                message_id = publisher.enqueue(
                    "READINESS",
                    {
                        "event_id": args.event_id,
                        "state": "notification_test",
                        "symbol": "ACCOUNT",
                        "reason": "owner requested destination verification",
                    },
                    now,
                )
            finally:
                store.release_writer_lease(
                    account_key=ACCOUNT_KEY,
                    owner_id=lock.owner_id,
                    released_at=_now(),
                )
    _print(
        {
            "message_id": message_id,
            "queued": True,
            "delivery_attempted_by_command": False,
            "provider": sink.route.provider,
            "delivery_route_id": sink.route.route_id,
            "required_assurance": sink.route.required_assurance.value,
            "provider_destination": sink.route.provider != "local_jsonl",
            "readiness_effect": (
                "independent_worker_must_deliver_and_readiness_will_revalidate_receipt"
                if sink.route.provider != "local_jsonl"
                else "local_staging_never_satisfies_destination_readiness"
            ),
        }
    )
    return 0


def command_notification_worker(args: argparse.Namespace) -> int:
    """Run the notification claimant independently from broker execution."""

    layout = InstallLayout(args.install_root)
    manifest, policy = layout.load_release()
    with _open_state(layout) as store:
        runtime = store.runtime_status()
        if runtime is None:
            raise CommandBlocked("runtime identity is missing")
        for field, expected in (
            ("release_manifest_hash", manifest["release_manifest_hash"]),
            ("config_hash", policy.config_hash),
            ("policy_hash", policy.policy_hash),
            ("runtime_id", policy.runtime_id),
            ("account_key", ACCOUNT_KEY),
        ):
            if runtime[field] != expected:
                raise CommandBlocked(f"runtime binding mismatch: {field}")
    composition = _runtime_composition(args)
    try:
        composition.bind_release(manifest, release_root=layout.release_root)
    except RuntimeCompositionError as exc:
        raise CommandBlocked(
            _external_failure_code("RUNTIME_COMPOSITION_INVALID", exc)
        ) from exc
    sink = composition.notification_sink(
        policy.config["notifications"],
        local_jsonl_path=layout.notification_path,
    )
    stop_event = threading.Event()
    previous: dict[int, Any] = {}

    def request_stop(_signum: int, _frame: Any) -> None:
        stop_event.set()

    if not args.once:
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, request_stop)
    try:
        sent, failed = run_notification_worker(
            state_path=layout.state_path,
            account_key=ACCOUNT_KEY,
            sink=sink,
            stop_event=stop_event,
            settings=NotificationWorkerSettings(
                interval_seconds=float(args.interval_seconds),
                batch_limit=int(args.batch_limit),
                claim_ttl_seconds=float(args.claim_ttl_seconds),
            ),
            once=bool(args.once),
        )
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    _print(
        {
            "provider": sink.route.provider,
            "delivery_route_id": sink.route.route_id,
            "sent": sent,
            "failed": failed,
            "independent_from_broker_writer": True,
        }
    )
    return 0 if failed == 0 else 2


def command_export_eod(args: argparse.Namespace) -> int:
    layout = InstallLayout(args.install_root)
    _, policy = layout.load_release()
    trading_date = date.fromisoformat(args.date)
    with _open_state(layout) as store:
        payload = build_eod_evidence(
            store,
            account_key=ACCOUNT_KEY,
            trading_date=trading_date,
            generated_at=_now(),
            max_snapshot_age=timedelta(
                seconds=int(
                    policy.config["evidence"]["broker_snapshot_max_age_seconds"]
                )
            ),
        )
    target = layout.eod_path / f"{trading_date.isoformat()}.json"
    digest = write_eod_evidence(target, payload)
    _print({"path": str(target), "sha256": digest, "flat_proven": payload["flat_proven"]})
    return 0


def _print(value: Any) -> None:
    print(json.dumps(value, sort_keys=True, indent=2, default=str, allow_nan=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="titan-full-live")
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name: str, help_text: str, handler: Any) -> argparse.ArgumentParser:
        item = commands.add_parser(name, help=help_text)
        item.add_argument("--install-root", required=True)
        item.set_defaults(handler=handler)
        return item

    command("init-state", "initialize the installed release PAUSED", command_init_state)
    command("doctor", "read-only readiness and capability report", command_doctor)
    command("status", "read-only runtime status", command_status)
    command(
        "readiness",
        "collect activation evidence directly from installed dependencies",
        command_readiness,
    )
    command(
        "record-legacy-retirement",
        "record hash-bound retirement after disabled scheduler and drained broker state",
        command_record_legacy_retirement,
    )
    serve = command("serve", "run the persistent coordinator", command_serve)
    serve.add_argument("--once", action="store_true")
    prepare = command(
        "prepare-activation",
        "stage one short-lived hash-bound activation record",
        command_prepare_activation,
    )
    prepare.add_argument("--ttl-seconds", type=int, default=300, choices=range(30, 601))
    activate = command("activate", "consume one activation and enter RECONCILING", command_activate)
    activate.add_argument("--activation-id", required=True)
    activate.add_argument("--confirm", required=True)
    pause = command(
        "pause-new-entries",
        "stop new entries while preserving protection and exit authority",
        command_pause_new_entries,
    )
    pause.add_argument("--reason", required=True)
    closeout = command(
        "managed-closeout",
        "enter managed closeout under existing activated authority",
        command_managed_closeout,
    )
    closeout.add_argument("--reason", required=True)
    deactivate = command(
        "deactivate",
        "revoke runtime authority after complete broker flatness",
        command_deactivate,
    )
    deactivate.add_argument("--flatness-snapshot-id", required=True)
    deactivate.add_argument("--reason", required=True)
    deactivate.add_argument("--confirm", required=True)
    notification = command(
        "notification-test",
        "enqueue one durable readiness notification for the independent worker",
        command_notification_test,
    )
    notification.add_argument("--event-id", required=True)
    notification_worker = command(
        "notification-worker",
        "run the independent durable notification delivery worker",
        command_notification_worker,
    )
    notification_worker.add_argument("--once", action="store_true")
    notification_worker.add_argument("--interval-seconds", type=float, default=1.0)
    notification_worker.add_argument("--batch-limit", type=int, default=20)
    notification_worker.add_argument("--claim-ttl-seconds", type=float, default=60.0)
    eod = command("export-eod", "write one create-only EOD packet", command_export_eod)
    eod.add_argument("--date", required=True)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    runtime_composition: RuntimeComposition | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.runtime_composition = runtime_composition or RuntimeComposition()
    try:
        return int(args.handler(args))
    except (CommandBlocked, ValueError, OSError, RuntimeError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_type": type(exc).__name__,
                    "error": _safe_cli_error(exc),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
