"""Canonical machine-readiness fixtures for full-live state-boundary tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from titan_brain.live.activation import (
    ACTIVATION_SCHEMA,
    MACHINE_EVIDENCE_SOURCE,
    ActivationRecord,
    ReadinessEvidence,
)
from titan_brain.live.models import BrokerSnapshot
from titan_brain.live.risk_evidence_binding import risk_high_water_receipt_hash
from titan_brain.live.state import LiveStateStore, object_hash


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("test timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def record_flat_reconciliation(
    store: LiveStateStore,
    *,
    account_key: str,
    received_at: datetime,
    label: str,
) -> tuple[BrokerSnapshot, str]:
    received = _utc(received_at)
    snapshot = BrokerSnapshot(
        snapshot_id=f"activation-flat-{label}",
        account_key=account_key,
        evidence_revision=f"activation-revision-{label}",
        observed_at=received,
        received_at=received,
        account_state="active",
        equity=Decimal("1000"),
        cash=Decimal("1000"),
        unleveraged_buying_power=Decimal("1000"),
        realized_pnl=Decimal("0"),
        equity_position_count=0,
        equity_order_count=0,
        equity_nonterminal_order_count=0,
        external_material_order_count=0,
        option_position_count=0,
        option_order_count=0,
        advanced_order_count=0,
        reconciliation_blocker_count=0,
        positions_reconciled=True,
        equity_orders_reconciled=True,
        option_positions_reconciled=True,
        option_orders_reconciled=True,
        advanced_orders_reconciled=True,
        realized_pnl_reconciled=True,
        positions_digest=object_hash([]),
        orders_digest=object_hash([]),
    )
    store.record_broker_snapshot(snapshot)
    store.reconcile_positions(
        snapshot_id=snapshot.snapshot_id,
        account_key=account_key,
        positions=(),
        reconciled_at=received,
    )
    audit = store.rows(
        """SELECT event_id FROM audit_events WHERE stream=?
             AND event_type='POSITIONS_RECONCILED' AND entity_id=?
             ORDER BY sequence DESC LIMIT 1""",
        (account_key, snapshot.snapshot_id),
    )[0]
    return snapshot, str(audit["event_id"])


def stage_canonical_activation(
    store: LiveStateStore,
    *,
    created_at: datetime,
    expires_at: datetime,
    writer_owner_id: str | None = None,
    readiness_overrides: dict[str, object] | None = None,
) -> tuple[ActivationRecord, str]:
    """Persist a v2 activation backed by a real durable flat snapshot."""

    created = _utc(created_at)
    runtime = dict(store.runtime_status() or {})
    if not runtime:
        raise ValueError("runtime must be initialized")
    account_key = str(runtime["account_key"])
    account_last4 = account_key.rsplit("-", 1)[-1]
    leases = store.rows(
        "SELECT * FROM account_writer_lease WHERE account_key=?", (account_key,)
    )
    if leases and leases[0]["released_at"] is None:
        owner = str(leases[0]["owner_id"])
    else:
        owner = writer_owner_id or "canonical-activation-test-writer"
        store.acquire_writer_lease(
            account_key=account_key,
            owner_id=owner,
            acquired_at=created - timedelta(seconds=2),
        )
        leases = store.rows(
            "SELECT * FROM account_writer_lease WHERE account_key=?", (account_key,)
        )
    lease = leases[0]
    if writer_owner_id is not None and owner != writer_owner_id:
        raise ValueError("requested writer owner differs from active test lease")
    snapshot, audit_event_id = record_flat_reconciliation(
        store,
        account_key=account_key,
        received_at=created - timedelta(seconds=1),
        label=created.strftime("%Y%m%dT%H%M%S%f"),
    )
    chain_valid, chain_length, chain_head = store.verify_event_chain()
    readiness = ReadinessEvidence(
        collected_at=created,
        release_manifest_hash=str(runtime["release_manifest_hash"]),
        config_hash=str(runtime["config_hash"]),
        policy_hash=str(runtime["policy_hash"]),
        runtime_id=str(runtime["runtime_id"]),
        database_schema_version=store.schema_version,
        account_key=account_key,
        account_last4=account_last4,
        runtime_identity_valid=True,
        evidence_source=MACHINE_EVIDENCE_SOURCE,
        broker_connector="synthetic-supported-test-broker",
        broker_read_attempted=True,
        broker_read_succeeded=True,
        broker_read_error_type=None,
        broker_account_last4=account_last4,
        broker_snapshot_received_at=snapshot.received_at,
        account_active=True,
        broker_authenticated=True,
        daemon_accessible_supported_client=True,
        unattended_mutation_supported=True,
        per_mutation_confirmation_required=False,
        durable_snapshot_id=snapshot.snapshot_id,
        durable_snapshot_received_at=snapshot.received_at,
        reconciliation_audit_event_id=audit_event_id,
        standard_orders_reconciled=True,
        option_positions_reconciled=True,
        option_orders_reconciled=True,
        advanced_orders_reconciled=True,
        positions_reconciled=True,
        realized_pnl_reconciled=True,
        reconciliation_blocker_count=0,
        durable_account_flat=True,
        unknown_submissions=0,
        uncovered_quantity=0,
        legacy_heartbeat_id="robinhood-momentum-engine",
        legacy_heartbeat_status="PAUSED",
        legacy_heartbeat_config_hash="d" * 64,
        old_writer_disabled=True,
        new_writer_lock_held=True,
        writer_lock_owner_id=owner,
        writer_lock_process_id=int(lease["process_id"]),
        local_state_writable=True,
        audit_chain_valid=chain_valid,
        audit_chain_length=chain_length,
        audit_chain_head=chain_head,
        market_data_connected=True,
        market_data_resynced=True,
        market_data_blockers=(),
        tradability_provider_ready=True,
        notification_sink="synthetic-receipted-test-sink",
        notification_destination_configured=True,
        notification_delivery_receipt_hash="e" * 64,
        notification_delivered_at=created - timedelta(seconds=1),
        notification_tested=True,
        broker_snapshot_age_seconds=1.0,
        durable_snapshot_age_seconds=1.0,
        quote_age_seconds=1.0,
        completed_bar_age_seconds=1.0,
        probe_errors=(),
        probe_started_at=created - timedelta(milliseconds=100),
        probe_completed_at=created,
        probe_elapsed_monotonic_seconds=0.1,
        probe_clock_stable=True,
        broker_account_binding_fingerprint="a" * 64,
        broker_authorization_binding_id="b" * 64,
        component_provenance_hash="c" * 64,
        coordinator_component_provenance_hash="d" * 64,
        execution_authority_mode="unattended",
        attended_mutation_supported=False,
        broker_command_connected=True,
        broker_command_next_valid_id_received=True,
        broker_command_account_authenticated=True,
        broker_command_write_authority_granted=False,
        entry_risk_evidence_ready=True,
        weekly_realized_pnl_complete=True,
        peak_equity_complete=True,
        risk_evidence_as_of=created - timedelta(seconds=1),
        risk_evidence_age_seconds=1.0,
        risk_baseline_identity_hash="4" * 64,
        risk_baseline_receipt_hash="5" * 64,
        risk_high_water_identity_hash="6" * 64,
        risk_high_water_lineage_hash="7" * 64,
        risk_high_water_peak_equity="1000",
        risk_high_water_receipt_hash=risk_high_water_receipt_hash(
            identity_hash="6" * 64,
            baseline_receipt_hash="5" * 64,
            lineage_hash="7" * 64,
            peak_equity=1000,
        ),
    )
    if readiness_overrides:
        readiness = replace(readiness, **readiness_overrides)
    provisional = ActivationRecord(
        activation_id="0" * 64,
        release_manifest_hash=str(runtime["release_manifest_hash"]),
        config_hash=str(runtime["config_hash"]),
        policy_hash=str(runtime["policy_hash"]),
        account_key=account_key,
        account_last4=account_last4,
        runtime_id=str(runtime["runtime_id"]),
        database_schema_version=store.schema_version,
        requested_mode="live",
        created_at=created,
        expires_at=_utc(expires_at),
        readiness_hash=readiness.evidence_hash,
        readiness_evidence=readiness,
        owner_acknowledged_blockers=(),
        schema_version=ACTIVATION_SCHEMA,
    )
    record = replace(
        provisional, activation_id=provisional.recomputed_activation_id()
    )
    store.record_activation(
        activation_id=record.activation_id,
        account_key=account_key,
        record=record.to_payload(),
        created_at=record.created_at,
        expires_at=record.expires_at,
    )
    return record, owner


def activate_canonical_runtime(
    store: LiveStateStore,
    *,
    created_at: datetime,
    activated_at: datetime,
    expires_at: datetime,
    writer_owner_id: str | None = None,
    readiness_overrides: dict[str, object] | None = None,
) -> tuple[ActivationRecord, str]:
    record, owner = stage_canonical_activation(
        store,
        created_at=created_at,
        expires_at=expires_at,
        writer_owner_id=writer_owner_id,
        readiness_overrides=readiness_overrides,
    )
    store.heartbeat_writer_lease(
        account_key=record.account_key,
        owner_id=owner,
        observed_at=_utc(activated_at),
    )
    store.activate_runtime(
        record.activation_id,
        activated_at=_utc(activated_at),
        confirmation_phrase=(
            f"ACTIVATE FULL LIVE {record.account_key} {record.activation_id}"
        ),
        writer_owner_id=owner,
    )
    return record, owner
