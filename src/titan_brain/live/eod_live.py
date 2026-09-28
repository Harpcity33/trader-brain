"""Create-only end-of-day evidence for the full-live lifecycle."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from .models import BrokerOrderState, IntentState, ProtectionState
from .state import LiveStateStore


EOD_SCHEMA = "titan_full_live_eod_2026-09-08_v1"


def _percentile(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * percentile)))
    return ordered[index]


def build_eod_evidence(
    store: LiveStateStore,
    *,
    account_key: str,
    trading_date: date,
    generated_at: datetime,
    max_snapshot_age: timedelta = timedelta(seconds=15),
) -> dict[str, Any]:
    if generated_at.tzinfo is None:
        raise ValueError("generated_at must be timezone-aware")
    if max_snapshot_age <= timedelta(0):
        raise ValueError("max_snapshot_age must be positive")
    runtime = store.runtime_status()
    if runtime is None or runtime["account_key"] != account_key:
        raise ValueError("runtime state is not bound to the requested account")
    broker_rows = store.rows(
        "SELECT * FROM broker_snapshots WHERE account_key=? "
        "ORDER BY observed_at DESC,received_at DESC,snapshot_id DESC LIMIT 1",
        (account_key,),
    )
    latest = dict(broker_rows[0]) if broker_rows else None
    positions = [
        dict(row)
        for row in store.rows(
            "SELECT * FROM positions WHERE account_key=? AND CAST(quantity AS REAL)<>0 ORDER BY symbol",
            (account_key,),
        )
    ]
    orders = [dict(row) for row in store.rows(
        "SELECT * FROM broker_orders WHERE account_key=? ORDER BY broker_updated_at,broker_order_id",
        (account_key,),
    )]
    active_orders = [
        row for row in orders if not BrokerOrderState(row["state"]).terminal
    ]
    ambiguous_intents = [
        dict(row)
        for row in store.rows(
            "SELECT intent_id,kind,state,client_ref,updated_at FROM order_intents "
            "WHERE account_key=? AND state IN (?,?) ORDER BY updated_at,intent_id",
            (account_key, IntentState.SUBMITTING.value, IntentState.UNKNOWN.value),
        )
    ]
    obligations = [
        dict(row)
        for row in store.rows(
            "SELECT * FROM protection_obligations WHERE account_key=? AND state NOT IN (?,?) "
            "ORDER BY symbol,obligation_id",
            (
                account_key,
                ProtectionState.SATISFIED.value,
                ProtectionState.CANCELLED.value,
            ),
        )
    ]
    incidents = [
        dict(row)
        for row in store.rows(
            "SELECT incident_id,category,severity,opened_at FROM incidents "
            "WHERE account_key=? AND resolved_at IS NULL ORDER BY opened_at,incident_id",
            (account_key,),
        )
    ]
    outbox = {
        str(row["state"]): int(row["count"])
        for row in store.rows(
            "SELECT state,COUNT(*) AS count FROM notification_outbox "
            "WHERE account_key=? GROUP BY state",
            (account_key,),
        )
    }
    latency: dict[str, dict[str, int | None]] = {}
    for row in store.rows(
        "SELECT stage,duration_microseconds FROM latency_samples WHERE account_key=?",
        (account_key,),
    ):
        latency.setdefault(str(row["stage"]), {"values": []})["values"].append(
            int(row["duration_microseconds"])
        )
    latency_summary = {
        stage: {
            "count": len(item["values"]),
            "p50_microseconds": _percentile(item["values"], 0.50),
            "p95_microseconds": _percentile(item["values"], 0.95),
            "max_microseconds": max(item["values"]) if item["values"] else None,
        }
        for stage, item in sorted(latency.items())
    }
    chain_valid, chain_length, chain_head = store.verify_event_chain()
    snapshot_complete = bool(
        latest
        and all(
            int(latest[name]) == 1
            for name in (
                "positions_reconciled",
                "equity_orders_reconciled",
                "option_positions_reconciled",
                "option_orders_reconciled",
                "advanced_orders_reconciled",
                "realized_pnl_reconciled",
            )
        )
        and int(latest["reconciliation_blocker_count"]) == 0
    )
    snapshot_fresh = bool(
        latest
        and timedelta(0)
        <= generated_at.astimezone(timezone.utc)
        - datetime.fromisoformat(str(latest["observed_at"])).astimezone(timezone.utc)
        <= max_snapshot_age
    )
    broker_scope_flat = bool(
        latest
        and all(
            int(latest[name]) == 0
            for name in (
                "equity_position_count",
                "equity_nonterminal_order_count",
                "external_material_order_count",
                "option_position_count",
                "option_order_count",
                "advanced_order_count",
            )
        )
    )
    flat_proven = bool(
        snapshot_complete
        and snapshot_fresh
        and broker_scope_flat
        and not positions
        and not active_orders
        and not ambiguous_intents
        and not obligations
    )
    return {
        "schema_version": EOD_SCHEMA,
        "account": account_key,
        "trading_date": trading_date.isoformat(),
        "generated_at": generated_at.astimezone(timezone.utc).isoformat(),
        "runtime": {
            "runtime_id": runtime["runtime_id"],
            "mode": runtime["mode"],
            "generation": int(runtime["generation"]),
            "release_manifest_hash": runtime["release_manifest_hash"],
            "config_hash": runtime["config_hash"],
            "policy_hash": runtime["policy_hash"],
        },
        "latest_broker_snapshot": latest,
        "broker_snapshot_complete": snapshot_complete,
        "broker_snapshot_fresh": snapshot_fresh,
        "broker_scope_flat": broker_scope_flat,
        "flat_proven": flat_proven,
        "nonzero_positions": positions,
        "active_orders": active_orders,
        "ambiguous_intents": ambiguous_intents,
        "open_protection_obligations": obligations,
        "open_incidents": incidents,
        "notification_counts": outbox,
        "latency": latency_summary,
        "audit_chain": {
            "valid": chain_valid,
            "length": chain_length,
            "head": chain_head,
        },
    }


def write_eod_evidence(path: str | Path, payload: Mapping[str, Any]) -> str:
    """Write one immutable evidence packet and return its SHA-256 digest."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = (
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    descriptor = os.open(target, flags, 0o600)
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("failed to write end-of-day evidence")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return hashlib.sha256(encoded).hexdigest()


__all__ = ["EOD_SCHEMA", "build_eod_evidence", "write_eod_evidence"]
