"""Operator CLI for an installed, fail-closed full-live release."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tomllib
from typing import Any, Mapping, Sequence

from .activation import (
    MACHINE_EVIDENCE_SOURCE,
    ActivationRecord,
    ReadinessEvidence,
)
from .broker.robinhood import RobinhoodBrokerAdapter
from .control import ControlInbox
from .eod_live import build_eod_evidence, write_eod_evidence
from .latency import LatencyRecorder, LiveStateLatencyAdapter
from .lifecycle_actions import ProductionLifecycleActions
from .massive_adapter import LocalMassiveReadOnlySource
from .models import EngineMode
from .money import to_cents
from .notifications import JsonlNotificationSink
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
    build_local_outbox,
    persist_account_snapshot,
)
from .state import LiveStateStore, object_hash
from .writer_lock import AccountWriterLock, WriterLockBusy


ACCOUNT_KEY = "ending-7153"


class CommandBlocked(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


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
        self.lock_path = self.root / "state/locks"
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
        return heartbeat_id, "ABSENT", None, True, None
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
) -> ReadinessEvidence:
    """Collect activation facts from live dependencies and durable state.

    All probes fail closed.  Optional dependency arguments exist only for
    deterministic tests; production commands construct the installed adapters.
    """

    if now.tzinfo is None:
        raise ValueError("readiness collection time must be timezone-aware")
    current = now.astimezone(timezone.utc)
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

    broker_client = broker or RobinhoodBrokerAdapter()
    capabilities = broker_client.capabilities
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
        broker_snapshot_received_at = broker_snapshot.received_at.astimezone(timezone.utc)
        account_active = str(broker_snapshot.account_state).strip().lower() in {
            "active",
            "open",
        }
        broker_authenticated = bool(broker_snapshot.auth_point_in_time)
    except Exception as exc:  # connector failures are readiness evidence, not authority
        broker_error = type(exc).__name__
        probe_errors.append(f"broker_read:{broker_error}")

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
        "ORDER BY received_at DESC,snapshot_id DESC LIMIT 1",
        (ACCOUNT_KEY,),
    )
    durable = durable_rows[0] if durable_rows else None
    durable_snapshot_id = str(durable["snapshot_id"]) if durable is not None else None
    durable_snapshot_received_at = (
        _iso(str(durable["received_at"]), "durable_snapshot.received_at")
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
        old_writer_disabled,
        legacy_error,
    ) = _probe_legacy_heartbeat(legacy_heartbeat_path)
    if legacy_error is not None:
        probe_errors.append(legacy_error)

    market_data_connected = False
    market_data_resynced = False
    market_data_blockers: tuple[str, ...] = ("MASSIVE_HEALTH_NOT_PROBED",)
    quote_age_seconds: float | None = None
    completed_bar_age_seconds: float | None = None
    try:
        market_config = policy.config["market_data"]
        source = market_source or LocalMassiveReadOnlySource(
            market_config["database_path"],
            pilot_id=str(market_config["producer_pilot_id"]),
            book_mode=str(market_config["producer_book_mode"]),
            decision_contract_hash=str(market_config["producer_decision_contract_hash"]),
            health_max_age_seconds=int(market_config["health_max_age_seconds"]),
            candidate_max_age_seconds=int(market_config["candidate_max_age_seconds"]),
        )
        health = source.health(now=current)
        market_data_blockers = tuple(health.blockers)
        market_data_connected = not any(
            item.startswith("MASSIVE_STORE_UNAVAILABLE") for item in health.blockers
        )
        market_data_resynced = bool(health.producer_fresh)
        quote_age_seconds = _age_seconds(current, health.latest_quote_at)
        completed_bar_age_seconds = _age_seconds(current, health.latest_completed_bar_at)
    except Exception as exc:
        market_data_blockers = (f"MASSIVE_PROBE_FAILED:{type(exc).__name__}",)
        probe_errors.append(f"market_data:{type(exc).__name__}")

    discovery = policy.config["discovery"]
    tradability_provider_ready = bool(
        callable(getattr(broker_client, "get_equity_tradability", None))
        and discovery.get("pipeline_configured") is True
        and discovery.get("instrument_evidence_provider") != "unavailable"
    )

    notification = policy.config["notifications"]
    notification_sink = str(notification["delivery_sink"])
    notification_destination_configured = bool(
        notification.get("destination_bridge_configured") is True
        and notification_sink != "local_jsonl_staging"
    )
    notification_receipt_hash: str | None = None
    notification_delivered_at: datetime | None = None
    notification_tested = False
    delivered = store.rows(
        "SELECT n.message_id,n.delivered_at,n.delivery_receipt "
        "FROM notification_outbox n JOIN audit_events e "
        "ON e.entity_type='notification' AND e.entity_id=n.message_id "
        "AND e.event_type='NOTIFICATION_DELIVERED' "
        "WHERE n.account_key=? AND n.template='READINESS' AND n.state='DELIVERED' "
        "AND n.delivery_receipt IS NOT NULL ORDER BY n.delivered_at DESC LIMIT 1",
        (ACCOUNT_KEY,),
    )
    if delivered:
        notification_delivered_at = _iso(
            str(delivered[0]["delivered_at"]), "notification.delivered_at"
        )
        receipt = str(delivered[0]["delivery_receipt"])
        notification_receipt_hash = hashlib.sha256(receipt.encode("utf-8")).hexdigest()
        delivery_age = _age_seconds(current, notification_delivered_at)
        notification_tested = bool(
            notification_destination_configured
            and delivery_age is not None
            and 0 <= delivery_age <= 86_400
        )

    # Reconciliation persistence appends audit events, so bind the evidence to
    # the chain *after* the exact broker read has been durably recorded.
    try:
        audit_chain_valid, audit_chain_length, audit_chain_head = store.verify_event_chain()
    except (RuntimeError, sqlite3.Error, ValueError) as exc:
        audit_chain_valid, audit_chain_length, audit_chain_head = False, 0, "0" * 64
        probe_errors.append(f"audit_chain_post_reconciliation:{type(exc).__name__}")

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
) -> Mapping[str, Any]:
    manifest, policy = layout.load_release()
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
    probe = AccountWriterLock(layout.lock_path, ACCOUNT_KEY, owner_id="control-probe")
    try:
        probe.acquire(blocking=False)
    except WriterLockBusy:
        holder = probe.holder_metadata()
    else:
        probe.release()
        raise CommandBlocked("full-live service writer is not running")
    if lease is None or lease["owner_id"] != holder.get("owner_id"):
        raise CommandBlocked("kernel writer lock and database lease do not match")

    inbox = ControlInbox(
        layout.control_path,
        account_key=ACCOUNT_KEY,
        runtime_id=policy.runtime_id,
        release_manifest_hash=manifest["release_manifest_hash"],
        max_snapshot_age=timedelta(
            seconds=int(policy.config["evidence"]["broker_snapshot_max_age_seconds"])
        ),
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
    with AccountWriterLock(layout.lock_path, ACCOUNT_KEY):
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


def _doctor(layout: InstallLayout) -> dict[str, Any]:
    blockers: list[str] = []
    chain_ok = False
    chain_length = 0
    chain_head: str | None = None
    manifest: dict[str, Any] | None = None
    policy: PolicyBundle | None = None
    try:
        manifest, policy = layout.load_release()
    except Exception as exc:
        blockers.append(f"RELEASE_INVALID:{type(exc).__name__}:{exc}")
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
            blockers.append(f"STATE_INVALID:{type(exc).__name__}:{exc}")
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
            error = f"MASSIVE_ADAPTER_INVALID:{type(exc).__name__}:{exc}"
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
    report = _doctor(InstallLayout(args.install_root))
    _print(report)
    return 0 if report["ready_for_owner_activation"] else 2


def command_status(args: argparse.Namespace) -> int:
    layout = InstallLayout(args.install_root)
    report = _doctor(layout)
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
        broker = RobinhoodBrokerAdapter()
        writer_lock = AccountWriterLock(layout.lock_path, ACCOUNT_KEY)
        latency = LatencyRecorder(LiveStateLatencyAdapter(store, ACCOUNT_KEY))
        lifecycle = ProductionLifecycleActions(
            policy=policy,
            state=store,
            broker=broker,
            writer_lock=writer_lock,
            latency=latency,
            allow_mutations=bool(
                policy.config["execution"].get(
                    "local_mutation_interlock_enabled", False
                )
            ),
        )
        service = FullLiveService(
            policy=policy,
            state=store,
            broker=broker,
            notifications=build_local_outbox(
                store,
                ACCOUNT_KEY,
                JsonlNotificationSink(layout.notification_path),
                latency=latency,
            ),
            actions=lifecycle,
        )
        runner = ServiceRunner(
            service=service,
            lock=writer_lock,
            interval_seconds=float(policy.config["execution"]["reconcile_interval_seconds"]),
            control_inbox=ControlInbox(
                layout.control_path,
                account_key=ACCOUNT_KEY,
                runtime_id=policy.runtime_id,
                release_manifest_hash=manifest["release_manifest_hash"],
                max_snapshot_age=timedelta(
                    seconds=int(
                        policy.config["evidence"]["broker_snapshot_max_age_seconds"]
                    )
                ),
            ),
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
    created = _now()
    expires = created + timedelta(seconds=args.ttl_seconds)
    lock = AccountWriterLock(
        layout.lock_path, ACCOUNT_KEY, owner_id="activation-readiness-prepare"
    )
    with lock:
        with _open_state(layout) as store:
            store.acquire_writer_lease(
                account_key=ACCOUNT_KEY,
                owner_id=lock.owner_id,
                acquired_at=created,
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
                    now=created,
                )
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
                    now=created,
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
    lock = AccountWriterLock(
        layout.lock_path, ACCOUNT_KEY, owner_id="activation-readiness-inspect"
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
                )
                blockers = readiness.blockers(policy, now=observed)
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


def command_activate(args: argparse.Namespace) -> int:
    layout = InstallLayout(args.install_root)
    manifest, policy = layout.load_release()
    now = _now()
    expected_phrase = f"ACTIVATE FULL LIVE ending-7153 {args.activation_id}"
    if args.confirm != expected_phrase:
        raise CommandBlocked("exact activation confirmation phrase does not match")
    lock = AccountWriterLock(
        layout.lock_path, ACCOUNT_KEY, owner_id="activation-readiness-consume"
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
                    now=now,
                    already_consumed=rows[0]["consumed_at"] is not None,
                    current_readiness=readiness,
                )
                store.activate_runtime(
                    args.activation_id,
                    activated_at=now,
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
        layout, command="PAUSE_NEW_ENTRIES", reason=args.reason
    )
    _print({**result, "exit_authority_preserved": True})
    return 0


def command_managed_closeout(args: argparse.Namespace) -> int:
    layout = InstallLayout(args.install_root)
    _print(
        _queue_runtime_control(
            layout, command="MANAGED_CLOSEOUT", reason=args.reason
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
        )
    )
    return 0


def command_notification_test(args: argparse.Namespace) -> int:
    layout = InstallLayout(args.install_root)
    lock = AccountWriterLock(
        layout.lock_path, ACCOUNT_KEY, owner_id="notification-readiness-test"
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
                dispatcher = build_local_outbox(
                    store,
                    ACCOUNT_KEY,
                    JsonlNotificationSink(layout.notification_path),
                )
                message_id = dispatcher.enqueue(
                    "READINESS",
                    {
                        "event_id": args.event_id,
                        "state": "notification_test",
                        "symbol": "ACCOUNT",
                        "reason": "owner requested destination verification",
                    },
                    now,
                )
                sent, failed = dispatcher.drain(now)
            finally:
                store.release_writer_lease(
                    account_key=ACCOUNT_KEY,
                    owner_id=lock.owner_id,
                    released_at=_now(),
                )
    _print(
        {
            "message_id": message_id,
            "sent_to_local_jsonl": sent,
            "failed": failed,
            "local_sink_path": str(layout.notification_path),
            "user_destination_bridge_verified": False,
            "readiness_effect": "does_not_set_notification_tested_true",
        }
    )
    return 0 if not failed else 2


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
        "write and deliver one durable local readiness notification",
        command_notification_test,
    )
    notification.add_argument("--event-id", required=True)
    eod = command("export-eod", "write one create-only EOD packet", command_export_eod)
    eod.add_argument("--date", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (CommandBlocked, ValueError, OSError, RuntimeError) as exc:
        print(
            json.dumps(
                {"ok": False, "error_type": type(exc).__name__, "error": str(exc)},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
