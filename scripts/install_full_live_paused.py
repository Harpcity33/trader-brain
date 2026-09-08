#!/usr/bin/env python3
"""Verify and install a Titan full-live release in PAUSED mode.

This installer is intentionally incapable of loading launchd, starting the
runtime, or contacting the broker.  It writes release state below ``--root``
and takes the one fixed per-user account-writer interlock, independent of the
selected install root, so parallel installs cannot create independent writers.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import pwd
import re
import shutil
import sqlite3
import stat
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


MANIFEST_SCHEMA = "titan_full_live_release_2026-09-08_v1"
INSTALL_SCHEMA = "titan_full_live_paused_install_2026-09-08_v1"
LAUNCHD_LABEL = "com.harpcity.trader-brain-full-live"
NOTIFICATION_LAUNCHD_LABEL = "com.harpcity.trader-brain-full-live-notifications"
MAX_ARCHIVE_MEMBER_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 128 * 1024 * 1024
HEX = frozenset("0123456789abcdef")
_USER_LOCK_RELATIVE_PATH = Path(
    "Library/Application Support/Titan Momentum/account-writer-locks"
)
_SUPPORTED_STATE_SCHEMA_VERSION = 3
_ZERO_AUDIT_HASH = "0" * 64
_STATE_V2_OUTBOX_COLUMNS: tuple[tuple[str, str], ...] = (
    ("claim_owner", "TEXT"),
    ("claim_expires_at", "TEXT"),
    ("delivery_route_id", "TEXT"),
    ("delivery_assurance", "TEXT"),
    ("delivery_receipt_hash", "TEXT"),
    ("delivery_payload_hash", "TEXT"),
)
_STATE_V3_WORKER_DDL = """
CREATE TABLE IF NOT EXISTS notification_worker_lease (
    account_key TEXT PRIMARY KEY,
    worker_id TEXT NOT NULL,
    process_id INTEGER NOT NULL CHECK (process_id > 0),
    generation INTEGER NOT NULL CHECK (generation > 0),
    route_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    destination_fingerprint TEXT NOT NULL,
    route_version TEXT NOT NULL,
    started_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    last_sent_count INTEGER NOT NULL DEFAULT 0 CHECK (last_sent_count >= 0),
    last_failed_count INTEGER NOT NULL DEFAULT 0 CHECK (last_failed_count >= 0),
    released_at TEXT
);
"""
_STATE_BASE_TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "account_writer_lease": (
        "account_key", "owner_id", "process_id", "generation", "acquired_at",
        "heartbeat_at", "released_at",
    ),
    "activation_records": (
        "activation_id", "account_key", "record_hash", "created_at", "expires_at",
        "consumed_at", "record_json",
    ),
    "audit_events": (
        "sequence", "event_id", "stream", "event_type", "entity_type",
        "entity_id", "occurred_at", "payload_json", "previous_hash", "event_hash",
    ),
    "broker_orders": (
        "broker_order_id", "intent_id", "account_key", "state", "quantity",
        "cumulative_filled_quantity", "revision", "broker_updated_at", "received_at",
        "raw_hash",
    ),
    "broker_snapshots": (
        "snapshot_id", "account_key", "evidence_revision", "observed_at", "received_at",
        "account_state", "equity_cents", "cash_cents", "unleveraged_buying_power_cents",
        "realized_pnl_cents", "equity_position_count", "equity_order_count",
        "equity_nonterminal_order_count", "external_material_order_count",
        "option_position_count", "option_order_count", "advanced_order_count",
        "reconciliation_blocker_count", "positions_reconciled",
        "equity_orders_reconciled", "option_positions_reconciled",
        "option_orders_reconciled", "advanced_orders_reconciled",
        "realized_pnl_reconciled", "positions_digest", "orders_digest",
    ),
    "fills": (
        "fill_id", "broker_order_id", "account_key", "quantity", "price",
        "executed_at", "received_at",
    ),
    "incidents": (
        "incident_id", "account_key", "category", "severity", "detail_json",
        "opened_at", "resolved_at",
    ),
    "latency_samples": (
        "sample_id", "account_key", "stage", "duration_microseconds", "observed_at",
        "correlation_id",
    ),
    "order_intents": (
        "intent_id", "plan_id", "reservation_id", "account_key", "kind", "client_ref",
        "order_tuple_json", "tuple_hash", "created_at", "acknowledgement_deadline_at",
        "state", "updated_at",
    ),
    "plans": (
        "plan_id", "account_key", "strategy_id", "symbol", "setup_id", "quantity",
        "limit_price", "structural_stop", "market_hours", "time_in_force",
        "evidence_cutoff_at", "created_at", "expires_at", "policy_hash", "config_hash",
        "evidence_hash", "targets_json", "state",
    ),
    "positions": (
        "account_key", "symbol", "quantity", "sellable_quantity", "held_for_sells",
        "average_price", "source", "broker_updated_at", "received_at", "revision",
        "raw_hash", "snapshot_id",
    ),
    "protection_obligations": (
        "obligation_id", "source_fill_id", "account_key", "symbol", "required_quantity",
        "working_quantity", "stop_price", "state", "revision", "updated_at",
        "broker_order_id",
    ),
    "risk_reservations": (
        "reservation_id", "plan_id", "account_key", "planned_risk_cents",
        "stress_risk_cents", "execution_reserve_cents", "notional_cents", "created_at",
        "state",
    ),
    "runtime_identity": (
        "singleton", "runtime_id", "account_key", "release_manifest_hash", "config_hash",
        "policy_hash", "mode", "authority_enabled", "activated_at", "deactivated_at",
        "generation", "initialized_at", "updated_at",
    ),
    "schema_meta": ("singleton", "version", "applied_at"),
    "session_latches": (
        "account_key", "trading_date", "loss_locked", "objective_crossed",
        "pause_new_entries", "closeout_started", "hard_kill",
        "highest_realized_pnl_cents", "first_objective_crossed_at", "revision",
        "updated_at",
    ),
}
_STATE_V1_OUTBOX_COLUMNS = (
    "message_id", "event_key", "account_key", "template", "payload_json", "created_at",
    "state", "attempt_count", "last_attempt_at", "next_attempt_at", "delivered_at",
    "last_error", "delivery_receipt",
)
_STATE_V3_WORKER_COLUMNS = (
    "account_key", "worker_id", "process_id", "generation", "route_id", "provider",
    "destination_fingerprint", "route_version", "started_at", "heartbeat_at",
    "last_sent_count", "last_failed_count", "released_at",
)
_STATE_BASE_INDEXES = frozenset(
    {
        "broker_orders_account_state",
        "broker_snapshots_account_time",
        "incidents_account_open",
        "latency_stage_time",
        "order_intents_account_state",
        "outbox_pending",
        "positions_account",
        "protection_account_state",
    }
)
_STATE_REQUIRED_TRIGGERS = frozenset(
    {
        "audit_events_no_delete",
        "audit_events_no_update",
        "broker_snapshots_no_delete",
        "broker_snapshots_no_update",
        "fills_no_delete",
        "fills_no_update",
        "latency_samples_no_delete",
        "latency_samples_no_update",
    }
)
_STATE_SCHEMA_FINGERPRINTS: dict[int, frozenset[str]] = {
    # v1 is the schema actually produced by the prior installed release.  v2
    # and v3 hashes are the exact transactional ALTER/CREATE results. Semantic
    # punctuation normalization makes upgraded and fresh v3 DDL equivalent.
    1: frozenset({"6b47f7310e9246dd4c14897341614a95bad3f03afdf940dac036ef439648a34e"}),
    2: frozenset({"e416db5d715173d59af4ddc17c3b199f21b69d512f8e9dcf702c927b697616ee"}),
    3: frozenset({"579b158a20b69fbde7bc364738d80c2e54f2b8a189eaee1f36fc1d8d6c824f20"}),
}
_STATE_OUTBOX_SQL_HASHES: dict[int, frozenset[str]] = {
    1: frozenset({"69acf530aa0ce897be855b402a99ab4efba4dbbe9e28ec5ae14f3a385c1e684b"}),
    2: frozenset({"6b94cd0d40f3da55eefedb230de95f3a8a0d01c4a5a51722034d9042b43921ee"}),
    3: frozenset({"6b94cd0d40f3da55eefedb230de95f3a8a0d01c4a5a51722034d9042b43921ee"}),
}


class InstallError(RuntimeError):
    pass


def _external_failure_code(prefix: str, error: BaseException) -> str:
    """Return a stable failure code without serializing exception text."""

    error_type = re.sub(
        r"[^A-Za-z0-9_]+", "_", type(error).__name__
    ).strip("_")
    return f"{prefix}:{error_type or 'Error'}"[:160]


def _user_account_writer_lock_directory() -> Path:
    """Return the non-configurable production account-lock directory."""

    try:
        home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(strict=True)
    except (KeyError, OSError) as exc:
        raise InstallError(
            "cannot resolve the operating-system user's writer-lock home"
        ) from exc
    if home == home.parent:
        raise InstallError("cannot establish a private user home for writer lock")
    return home / _USER_LOCK_RELATIVE_PATH


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _expected_archive_hash(archive: Path, explicit: str | None) -> str:
    if explicit:
        expected = explicit.strip().lower()
    else:
        sidecar = Path(str(archive) + ".sha256")
        if not sidecar.is_file():
            raise InstallError(f"archive checksum sidecar is required: {sidecar}")
        parts = sidecar.read_text(encoding="ascii").strip().split()
        if len(parts) < 1:
            raise InstallError("archive checksum sidecar is empty")
        expected = parts[0].lower()
    if len(expected) != 64 or any(character not in "0123456789abcdef" for character in expected):
        raise InstallError("expected archive checksum is not SHA-256")
    return expected


def _safe_member_name(name: str) -> str:
    pure = PurePosixPath(name)
    if pure.is_absolute() or not pure.parts or ".." in pure.parts or "." in pure.parts:
        raise InstallError(f"unsafe archive member: {name!r}")
    normalized = pure.as_posix()
    if normalized != name or "\\" in name:
        raise InstallError(f"non-canonical archive member: {name!r}")
    return normalized


def _read_archive(archive: Path) -> tuple[dict[str, Any], bytes, dict[str, bytes]]:
    payloads: dict[str, bytes] = {}
    total = 0
    try:
        with tarfile.open(archive, mode="r:gz") as bundle:
            for member in bundle.getmembers():
                name = _safe_member_name(member.name)
                if not member.isfile() or member.issym() or member.islnk():
                    raise InstallError(f"archive member must be a regular file: {name}")
                if name in payloads:
                    raise InstallError(f"duplicate archive member: {name}")
                if member.size < 0 or member.size > MAX_ARCHIVE_MEMBER_BYTES:
                    raise InstallError(f"archive member size is invalid: {name}")
                total += member.size
                if total > MAX_ARCHIVE_TOTAL_BYTES:
                    raise InstallError("archive exceeds the extraction size limit")
                handle = bundle.extractfile(member)
                if handle is None:
                    raise InstallError(f"cannot read archive member: {name}")
                data = handle.read(MAX_ARCHIVE_MEMBER_BYTES + 1)
                if len(data) != member.size:
                    raise InstallError(f"archive member size changed while reading: {name}")
                payloads[name] = data
    except (tarfile.TarError, OSError) as exc:
        raise InstallError(
            _external_failure_code("RELEASE_ARCHIVE_UNREADABLE", exc)
        ) from exc
    manifest_bytes = payloads.pop("release-manifest.json", None)
    if manifest_bytes is None:
        raise InstallError("release archive has no release-manifest.json")
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallError("release manifest is not valid JSON") from exc
    if not isinstance(manifest, dict):
        raise InstallError("release manifest must be an object")
    return manifest, manifest_bytes, payloads


def _verify_manifest(manifest: dict[str, Any], payloads: dict[str, bytes]) -> None:
    expected_manifest_fields = {
        "schema_version",
        "release_name",
        "source_commit",
        "source_tree",
        "config_hash",
        "policy_hash",
        "reproducible_epoch",
        "python_requires",
        "entrypoint",
        "default_mode",
        "install_subtree",
        "launchd_template",
        "notification_launchd_template",
        "files",
        "release_manifest_hash",
    }
    if set(manifest) != expected_manifest_fields:
        missing = sorted(expected_manifest_fields - set(manifest))
        extra = sorted(set(manifest) - expected_manifest_fields)
        raise InstallError(f"release manifest fields differ; missing={missing}, extra={extra}")
    if manifest.get("schema_version") != MANIFEST_SCHEMA:
        raise InstallError("unsupported release manifest schema")
    release_id = manifest.get("release_manifest_hash")
    if (
        not isinstance(release_id, str)
        or len(release_id) != 64
        or any(character not in HEX for character in release_id)
    ):
        raise InstallError("release manifest has an invalid release manifest hash")
    descriptor = dict(manifest)
    descriptor.pop("release_manifest_hash", None)
    if sha256_bytes(canonical_json(descriptor)) != release_id:
        raise InstallError("release manifest hash is not self-consistent")
    for field in ("config_hash", "policy_hash", "source_tree"):
        value = manifest.get(field)
        if not isinstance(value, str) or len(value) != 64 or any(character not in HEX for character in value):
            raise InstallError(f"release manifest {field} is invalid")
    required_values = {
        "release_name": "titan-full-live",
        "reproducible_epoch": 0,
        "python_requires": ">=3.11",
        "entrypoint": "scripts/titan-full-live",
        "default_mode": "PAUSED",
        "install_subtree": "Application Support/Titan Momentum/full-live",
        "launchd_template": "deployment/com.harpcity.trader-brain-full-live.plist.in",
        "notification_launchd_template": (
            "deployment/com.harpcity.trader-brain-full-live-notifications.plist.in"
        ),
    }
    for field, expected_value in required_values.items():
        if manifest.get(field) != expected_value:
            raise InstallError(f"release manifest {field} is invalid")
    source_commit = manifest.get("source_commit")
    if (
        not isinstance(source_commit, str)
        or len(source_commit) != 40
        or any(character not in HEX for character in source_commit)
    ):
        raise InstallError("release manifest source_commit is invalid")
    records = manifest.get("files")
    if not isinstance(records, list) or not records:
        raise InstallError("release manifest files must be a non-empty list")
    expected: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict) or set(record) != {"path", "sha256", "size", "mode"}:
            raise InstallError("release manifest file record must be an object")
        name = _safe_member_name(str(record.get("path", "")))
        if name == "release-manifest.json" or name in expected:
            raise InstallError(f"duplicate or reserved manifest path: {name}")
        digest = record.get("sha256")
        size = record.get("size")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in HEX for character in digest)
        ):
            raise InstallError(f"release member digest is invalid: {name}")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise InstallError(f"release member size is invalid: {name}")
        expected[name] = record
    required_files = {
        "config/full_live.json",
        "config/risk_limits.json",
        "config/nyse_calendar_2026.json",
        "scripts/titan-full-live",
        "src/titan_brain/live/cli.py",
        "src/titan_brain/live/composition.py",
        "src/titan_brain/live/discovery_composition.py",
        "src/titan_brain/live/execution.py",
        "src/titan_brain/live/lifecycle_actions.py",
        "src/titan_brain/live/market_data.py",
        "src/titan_brain/live/massive_adapter.py",
        "src/titan_brain/live/notification_worker.py",
        "src/titan_brain/live/notifications.py",
        "src/titan_brain/live/policy.py",
        "src/titan_brain/live/release.py",
        "src/titan_brain/live/service.py",
        "src/titan_brain/live/state.py",
        "deployment/com.harpcity.trader-brain-full-live.plist.in",
        "deployment/com.harpcity.trader-brain-full-live-notifications.plist.in",
        "validation/full-live/2026-09-08/OPERATIONS.md",
    }
    if not required_files.issubset(expected):
        raise InstallError(
            f"release inventory lacks required runtime files: {sorted(required_files - set(expected))}"
        )
    if sha256_bytes(canonical_json(records)) != manifest["source_tree"]:
        raise InstallError("release source_tree does not match the file inventory")
    if set(expected) != set(payloads):
        missing = sorted(set(expected) - set(payloads))
        extra = sorted(set(payloads) - set(expected))
        raise InstallError(f"archive inventory mismatch; missing={missing}, extra={extra}")
    for name, record in expected.items():
        data = payloads[name]
        if record.get("size") != len(data) or record.get("sha256") != sha256_bytes(data):
            raise InstallError(f"release member failed integrity verification: {name}")
        if record.get("mode") not in {"0644", "0755"}:
            raise InstallError(f"release member has unsupported mode: {name}")


def _assert_install_root(root: Path) -> Path:
    expanded = root.expanduser()
    required_suffix = ("Application Support", "Titan Momentum", "full-live")
    if tuple(expanded.parts[-3:]) != required_suffix:
        raise InstallError(
            "install root must be under 'Application Support/Titan Momentum/full-live'"
        )
    resolved = expanded.resolve(strict=False)
    if resolved == Path.home().resolve() or resolved == resolved.parent:
        raise InstallError("refusing a broad install root")
    return resolved


def _ensure_private_directory(root: Path, relative: str) -> Path:
    path = root / relative
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise InstallError(f"install directory is not a real directory: {relative}")
    path.mkdir(mode=0o700, exist_ok=True)
    try:
        path.resolve().relative_to(root)
    except ValueError as exc:
        raise InstallError(f"install directory escaped full-live root: {relative}") from exc
    return path


def _database_mode(database: Path) -> str | None:
    if not database.exists():
        return None
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            rows = connection.execute("SELECT mode FROM runtime_identity WHERE singleton=1").fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise InstallError(
            _external_failure_code("RUNTIME_PAUSED_PROOF_FAILED", exc)
        ) from exc
    if len(rows) != 1:
        raise InstallError("cannot prove existing runtime is paused: runtime identity is missing")
    return str(rows[0][0])


def _state_schema_object_names(
    connection: sqlite3.Connection, object_type: str
) -> frozenset[str]:
    if object_type not in {"table", "index", "trigger", "view"}:
        raise ValueError("unsupported SQLite schema object type")
    return frozenset(
        str(row[0])
        for row in connection.execute(
            """SELECT name FROM sqlite_master
                 WHERE type=? AND name NOT LIKE 'sqlite_%'""",
            (object_type,),
        ).fetchall()
    )


def _state_table_columns(
    connection: sqlite3.Connection, table: str
) -> tuple[str, ...]:
    if not table.replace("_", "").isalnum():
        raise ValueError("unsafe SQLite table name")
    return tuple(
        str(row[1])
        for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    )


def _normalized_schema_sql(value: object) -> str:
    collapsed = " ".join(str(value or "").split())
    return re.sub(r"\s*([(),])\s*", r"\1", collapsed)


def _state_schema_fingerprint(connection: sqlite3.Connection) -> str:
    """Hash DDL, full column metadata, keys, indexes, and trigger bodies."""

    tables = sorted(_state_schema_object_names(connection, "table"))
    objects: list[dict[str, Any]] = []
    for row in connection.execute(
        """SELECT type,name,tbl_name,sql FROM sqlite_master
             WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"""
    ).fetchall():
        if str(row["type"]) == "table" and str(row["name"]) == "notification_outbox":
            # ALTER-based v2/v3 and newly-created v3 have equivalent column/key
            # metadata but necessarily different stored CREATE TABLE text.
            sql = "<validated-separately>"
        else:
            sql = _normalized_schema_sql(row["sql"])
        objects.append(
            {
                "type": str(row["type"]),
                "name": str(row["name"]),
                "table": str(row["tbl_name"]),
                "sql": sql,
            }
        )
    table_shapes: dict[str, Any] = {}
    for table in tables:
        xinfo = [
            {
                "cid": int(row[0]),
                "name": str(row[1]),
                "type": str(row[2]),
                "notnull": int(row[3]),
                "default": None if row[4] is None else str(row[4]),
                "pk": int(row[5]),
                "hidden": int(row[6]),
            }
            for row in connection.execute(
                f'PRAGMA table_xinfo("{table}")'
            ).fetchall()
        ]
        foreign_keys = sorted(
            (
                int(row[0]),
                int(row[1]),
                str(row[2]),
                str(row[3]),
                None if row[4] is None else str(row[4]),
                str(row[5]),
                str(row[6]),
                str(row[7]),
            )
            for row in connection.execute(
                f'PRAGMA foreign_key_list("{table}")'
            ).fetchall()
        )
        indexes: list[dict[str, Any]] = []
        for index in connection.execute(
            f'PRAGMA index_list("{table}")'
        ).fetchall():
            index_name = str(index[1])
            index_columns = [
                {
                    "seqno": int(row[0]),
                    "cid": int(row[1]),
                    "name": None if row[2] is None else str(row[2]),
                    "desc": int(row[3]),
                    "collation": None if row[4] is None else str(row[4]),
                    "key": int(row[5]),
                }
                for row in connection.execute(
                    f'PRAGMA index_xinfo("{index_name}")'
                ).fetchall()
            ]
            indexes.append(
                {
                    "name": index_name,
                    "unique": int(index[2]),
                    "origin": str(index[3]),
                    "partial": int(index[4]),
                    "columns": index_columns,
                }
            )
        table_shapes[table] = {
            "columns": xinfo,
            "foreign_keys": foreign_keys,
            "indexes": sorted(indexes, key=lambda value: value["name"]),
        }
    descriptor = {"objects": objects, "tables": table_shapes}
    return hashlib.sha256(canonical_json(descriptor)).hexdigest()


def _outbox_schema_sql_hash(connection: sqlite3.Connection) -> str:
    row = connection.execute(
        """SELECT sql FROM sqlite_master
             WHERE type='table' AND name='notification_outbox'"""
    ).fetchone()
    if row is None:
        raise InstallError("state notification outbox table is missing")
    return hashlib.sha256(
        _normalized_schema_sql(row[0]).encode("utf-8")
    ).hexdigest()


def _validate_state_schema(
    connection: sqlite3.Connection, *, expected_version: int
) -> None:
    """Require an exact known v1/v2/v3 state shape before any DDL or rebind."""

    if expected_version not in {1, 2, _SUPPORTED_STATE_SCHEMA_VERSION}:
        raise InstallError(
            f"cannot migrate unsupported state schema {expected_version}"
        )
    observed_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if observed_version != expected_version:
        raise InstallError(
            "state schema version changed while the deployment lock was held"
        )
    if _state_schema_object_names(connection, "view"):
        raise InstallError("state schema contains unsupported views")
    expected_tables = set(_STATE_BASE_TABLE_COLUMNS)
    expected_tables.add("notification_outbox")
    if expected_version >= 3:
        expected_tables.add("notification_worker_lease")
    observed_tables = _state_schema_object_names(connection, "table")
    if observed_tables != frozenset(expected_tables):
        raise InstallError(
            "state schema table inventory differs from the supported shape; "
            f"missing={sorted(expected_tables - set(observed_tables))}, "
            f"unexpected={sorted(set(observed_tables) - expected_tables)}"
        )
    for table, expected_columns in _STATE_BASE_TABLE_COLUMNS.items():
        observed_columns = _state_table_columns(connection, table)
        if observed_columns != expected_columns:
            raise InstallError(
                f"state schema columns differ for {table}; "
                f"expected={list(expected_columns)}, observed={list(observed_columns)}"
            )
    outbox_columns = _STATE_V1_OUTBOX_COLUMNS
    if expected_version >= 2:
        outbox_columns += tuple(name for name, _ in _STATE_V2_OUTBOX_COLUMNS)
    observed_outbox = _state_table_columns(connection, "notification_outbox")
    if observed_outbox != outbox_columns:
        raise InstallError(
            "state schema columns differ for notification_outbox; "
            f"expected={list(outbox_columns)}, observed={list(observed_outbox)}"
        )
    if expected_version >= 3:
        observed_worker = _state_table_columns(
            connection, "notification_worker_lease"
        )
        if observed_worker != _STATE_V3_WORKER_COLUMNS:
            raise InstallError(
                "state schema columns differ for notification_worker_lease; "
                f"expected={list(_STATE_V3_WORKER_COLUMNS)}, "
                f"observed={list(observed_worker)}"
            )

    expected_indexes = set(_STATE_BASE_INDEXES)
    if expected_version >= 2:
        expected_indexes.add("outbox_claimable")
    observed_indexes = _state_schema_object_names(connection, "index")
    if observed_indexes != frozenset(expected_indexes):
        raise InstallError(
            "state schema index inventory differs from the supported shape; "
            f"missing={sorted(expected_indexes - set(observed_indexes))}, "
            f"unexpected={sorted(set(observed_indexes) - expected_indexes)}"
        )
    observed_triggers = _state_schema_object_names(connection, "trigger")
    if observed_triggers != _STATE_REQUIRED_TRIGGERS:
        raise InstallError(
            "state schema trigger inventory differs from the supported shape; "
            f"missing={sorted(set(_STATE_REQUIRED_TRIGGERS) - set(observed_triggers))}, "
            f"unexpected={sorted(set(observed_triggers) - set(_STATE_REQUIRED_TRIGGERS))}"
        )
    metadata = connection.execute(
        "SELECT singleton,version,applied_at FROM schema_meta"
    ).fetchall()
    if len(metadata) != 1 or int(metadata[0]["singleton"]) != 1:
        raise InstallError("state schema metadata is missing or ambiguous")
    if int(metadata[0]["version"]) != expected_version:
        raise InstallError("state schema metadata disagrees with PRAGMA user_version")
    try:
        applied_at = datetime.fromisoformat(str(metadata[0]["applied_at"]))
    except ValueError as exc:
        raise InstallError("state schema metadata timestamp is invalid") from exc
    if applied_at.tzinfo is None:
        raise InstallError("state schema metadata timestamp must be timezone-aware")
    fingerprint = _state_schema_fingerprint(connection)
    if fingerprint not in _STATE_SCHEMA_FINGERPRINTS[expected_version]:
        raise InstallError(
            "state schema semantic fingerprint differs from every supported "
            f"v{expected_version} shape"
        )
    outbox_hash = _outbox_schema_sql_hash(connection)
    if outbox_hash not in _STATE_OUTBOX_SQL_HASHES[expected_version]:
        raise InstallError(
            "state notification-outbox DDL differs from every supported "
            f"v{expected_version} shape"
        )


def _upgrade_state_schema(
    connection: sqlite3.Connection,
    *,
    version_before: int,
    migrated_at: datetime,
) -> dict[str, Any]:
    """Mirror LiveStateStore's v1→v2→v3 DDL inside this transaction."""

    _validate_state_schema(connection, expected_version=version_before)
    if version_before == _SUPPORTED_STATE_SCHEMA_VERSION:
        return {
            "required": False,
            "performed": False,
            "version_before": version_before,
            "version_after": version_before,
            "path": [version_before],
        }
    path = [version_before]
    when = migrated_at.astimezone(timezone.utc).isoformat()
    if version_before == 1:
        for name, column_type in _STATE_V2_OUTBOX_COLUMNS:
            connection.execute(
                f"ALTER TABLE notification_outbox ADD COLUMN {name} {column_type}"
            )
        connection.execute(
            """CREATE INDEX outbox_claimable
                 ON notification_outbox(
                     state, next_attempt_at, claim_expires_at, created_at
                 )"""
        )
        path.append(2)
        updated = connection.execute(
            "UPDATE schema_meta SET version=2,applied_at=? WHERE singleton=1",
            (when,),
        )
        if updated.rowcount != 1:
            raise InstallError("state schema metadata update was not singular")
        connection.execute("PRAGMA user_version=2")
        _validate_state_schema(connection, expected_version=2)
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise InstallError(
                "state database failed SQLite quick_check after v2 upgrade"
            )
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise InstallError(
                "state database failed foreign-key validation after v2 upgrade"
            )
    connection.execute(_STATE_V3_WORKER_DDL)
    path.append(3)
    updated = connection.execute(
        "UPDATE schema_meta SET version=?,applied_at=? WHERE singleton=1",
        (_SUPPORTED_STATE_SCHEMA_VERSION, when),
    )
    if updated.rowcount != 1:
        raise InstallError("state schema metadata update was not singular")
    connection.execute(f"PRAGMA user_version={_SUPPORTED_STATE_SCHEMA_VERSION}")
    _validate_state_schema(
        connection, expected_version=_SUPPORTED_STATE_SCHEMA_VERSION
    )
    if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise InstallError("state database failed SQLite quick_check after schema upgrade")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise InstallError("state database failed foreign-key validation after schema upgrade")
    return {
        "required": True,
        "performed": True,
        "version_before": version_before,
        "version_after": _SUPPORTED_STATE_SCHEMA_VERSION,
        "path": path,
    }


def _state_canonical_json(value: object) -> str:
    """Match the runtime state's audit-chain JSON encoding exactly."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _verify_audit_chain(connection: sqlite3.Connection) -> tuple[int, str]:
    previous = _ZERO_AUDIT_HASH
    rows = connection.execute(
        "SELECT * FROM audit_events ORDER BY sequence"
    ).fetchall()
    for row in rows:
        if str(row["previous_hash"]) != previous:
            raise InstallError("existing state audit chain has a broken predecessor")
        try:
            payload = json.loads(str(row["payload_json"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise InstallError("existing state audit payload is invalid") from exc
        body = _state_canonical_json(
            {
                "event_id": row["event_id"],
                "stream": row["stream"],
                "event_type": row["event_type"],
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "occurred_at": row["occurred_at"],
                "payload": payload,
            }
        )
        expected = hashlib.sha256(
            previous.encode("ascii") + b"\n" + body.encode("utf-8")
        ).hexdigest()
        if str(row["event_hash"]) != expected:
            raise InstallError("existing state audit chain has an invalid event hash")
        previous = expected
    return len(rows), previous


def _append_state_migration_event(
    connection: sqlite3.Connection,
    *,
    account_key: str,
    event_type: str,
    entity_type: str,
    entity_id: str,
    occurred_at: str,
    payload: dict[str, Any],
) -> str:
    event_id = str(uuid4())
    payload_json = _state_canonical_json(payload)
    prior = connection.execute(
        "SELECT event_hash FROM audit_events ORDER BY sequence DESC LIMIT 1"
    ).fetchone()
    previous = _ZERO_AUDIT_HASH if prior is None else str(prior[0])
    body = _state_canonical_json(
        {
            "event_id": event_id,
            "stream": account_key,
            "event_type": event_type,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "occurred_at": occurred_at,
            "payload": payload,
        }
    )
    digest = hashlib.sha256(
        previous.encode("ascii") + b"\n" + body.encode("utf-8")
    ).hexdigest()
    connection.execute(
        """INSERT INTO audit_events(
               event_id,stream,event_type,entity_type,entity_id,occurred_at,
               payload_json,previous_hash,event_hash
           ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            event_id,
            account_key,
            event_type,
            entity_type,
            entity_id,
            occurred_at,
            payload_json,
            previous,
            digest,
        ),
    )
    return event_id


def _migrate_paused_runtime_identity(
    database: Path,
    *,
    manifest: dict[str, Any],
    config: dict[str, Any],
    migrated_at: datetime,
) -> dict[str, Any]:
    """Atomically rebind an unarmed PAUSED database to a verified release.

    The surrounding deployment interlock owns the fixed account kernel lock.
    This transaction independently rejects active runtime/worker leases,
    verifies the append-only audit chain, expires every unconsumed activation,
    bumps the generation, updates all release identity fields, and appends a
    hash-chained migration event.  Old filesystem control requests are already
    cryptographically invalid because they bind the prior release hash.
    """

    if not database.exists():
        return {
            "required": False,
            "performed": False,
            "reason": "STATE_NOT_INITIALIZED",
            "invalidated_activation_records": 0,
        }
    if database.is_symlink() or not database.is_file():
        raise InstallError("state database must be a non-symlink regular file")
    if migrated_at.tzinfo is None:
        raise InstallError("release migration time must be timezone-aware")
    try:
        target = {
            "runtime_id": str(config["runtime_id"]),
            "account_key": str(config["account"]["masked_identifier"]),
            "release_manifest_hash": str(manifest["release_manifest_hash"]),
            "config_hash": str(manifest["config_hash"]),
            "policy_hash": str(manifest["policy_hash"]),
        }
    except (KeyError, TypeError) as exc:
        raise InstallError("target runtime identity is incomplete") from exc
    if not target["runtime_id"].strip() or target["account_key"] != "ending-7153":
        raise InstallError("target runtime identity is invalid")
    for field in ("release_manifest_hash", "config_hash", "policy_hash"):
        value = target[field]
        if len(value) != 64 or any(character not in HEX for character in value):
            raise InstallError(f"target runtime identity {field} is invalid")

    connection = sqlite3.connect(
        str(database), timeout=5.0, isolation_level=None
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("BEGIN IMMEDIATE")
        try:
            schema_version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
            if schema_version not in {1, 2, _SUPPORTED_STATE_SCHEMA_VERSION}:
                raise InstallError(
                    f"cannot migrate unsupported state schema {schema_version}"
                )
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise InstallError("existing state database failed SQLite quick_check")
            _validate_state_schema(
                connection, expected_version=schema_version
            )
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise InstallError("existing state database failed foreign-key validation")
            runtime_rows = connection.execute(
                "SELECT * FROM runtime_identity WHERE singleton=1"
            ).fetchall()
            if len(runtime_rows) != 1:
                raise InstallError("cannot migrate runtime identity: row is missing or ambiguous")
            runtime = runtime_rows[0]
            if runtime["mode"] != "PAUSED" or bool(runtime["authority_enabled"]):
                raise InstallError("release migration requires unarmed PAUSED runtime")
            if str(runtime["account_key"]) != target["account_key"]:
                raise InstallError("release migration cannot change broker account binding")
            active_writer = connection.execute(
                "SELECT COUNT(*) FROM account_writer_lease WHERE released_at IS NULL"
            ).fetchone()[0]
            active_worker = 0
            if schema_version >= 3:
                active_worker = connection.execute(
                    """SELECT COUNT(*) FROM notification_worker_lease
                         WHERE released_at IS NULL"""
                ).fetchone()[0]
            if int(active_writer) or int(active_worker):
                raise InstallError("release migration requires all runtime leases released")
            if schema_version >= 2:
                active_claims = int(
                    connection.execute(
                        """SELECT COUNT(*) FROM notification_outbox
                             WHERE claim_owner IS NOT NULL
                                OR claim_expires_at IS NOT NULL"""
                    ).fetchone()[0]
                )
                if active_claims:
                    raise InstallError(
                        "release migration requires all notification claims released"
                    )
            before_count, before_head = _verify_audit_chain(connection)
            when = migrated_at.astimezone(timezone.utc).isoformat()
            schema_migration = _upgrade_state_schema(
                connection,
                version_before=schema_version,
                migrated_at=migrated_at,
            )
            if schema_migration["performed"]:
                schema_event_id = _append_state_migration_event(
                    connection,
                    account_key=target["account_key"],
                    event_type="STATE_SCHEMA_UPGRADED",
                    entity_type="database_schema",
                    entity_id=str(_SUPPORTED_STATE_SCHEMA_VERSION),
                    occurred_at=when,
                    payload={
                        "version_before": schema_version,
                        "version_after": _SUPPORTED_STATE_SCHEMA_VERSION,
                        "migration_path": schema_migration["path"],
                        "runtime_migrations_mirrored": (
                            ["SCHEMA_V2_MIGRATION", "SCHEMA_V3_MIGRATION"]
                            if schema_version == 1
                            else ["SCHEMA_V3_MIGRATION"]
                        ),
                        "runtime_data_preserved": True,
                        "audit_data_preserved": True,
                        "outbox_data_preserved": True,
                    },
                )
                schema_migration["audit_event_id"] = schema_event_id
                schema_count, schema_head = _verify_audit_chain(connection)
                if schema_count != before_count + 1:
                    raise InstallError(
                        "schema migration audit event was not appended exactly once"
                    )
            else:
                schema_count, schema_head = before_count, before_head
            existing = {
                field: str(runtime[field])
                for field in (
                    "runtime_id",
                    "account_key",
                    "release_manifest_hash",
                    "config_hash",
                    "policy_hash",
                )
            }
            identity_changed = existing != target
            if not identity_changed and not schema_migration["performed"]:
                connection.commit()
                return {
                    "required": False,
                    "performed": False,
                    "reason": "IDENTITY_ALREADY_MATCHED",
                    "invalidated_activation_records": 0,
                    "schema_migration": schema_migration,
                    "audit_chain_length": schema_count,
                    "audit_chain_head": schema_head,
                }

            pending_activations = int(
                connection.execute(
                    "SELECT COUNT(*) FROM activation_records WHERE consumed_at IS NULL"
                ).fetchone()[0]
            )
            connection.execute(
                "UPDATE activation_records SET consumed_at=? WHERE consumed_at IS NULL",
                (when,),
            )
            connection.execute(
                """UPDATE runtime_identity
                      SET runtime_id=?,release_manifest_hash=?,config_hash=?,policy_hash=?,
                          mode='PAUSED',authority_enabled=0,activated_at=NULL,
                          generation=generation+1,updated_at=?
                    WHERE singleton=1""",
                (
                    target["runtime_id"],
                    target["release_manifest_hash"],
                    target["config_hash"],
                    target["policy_hash"],
                    when,
                ),
            )
            event_id = _append_state_migration_event(
                connection,
                account_key=target["account_key"],
                event_type="RUNTIME_RELEASE_IDENTITY_MIGRATED_PAUSED",
                entity_type="release_manifest",
                entity_id=target["release_manifest_hash"],
                occurred_at=when,
                payload={
                    "from_identity": existing,
                    "to_identity": target,
                    "identity_changed": identity_changed,
                    "mode": "PAUSED",
                    "authority_enabled": False,
                    "generation_before": int(runtime["generation"]),
                    "generation_after": int(runtime["generation"]) + 1,
                    "invalidated_activation_records": pending_activations,
                    "prior_release_controls_invalidated_by_release_binding": True,
                },
            )
            after_count, after_head = _verify_audit_chain(connection)
            if after_count != schema_count + 1:
                raise InstallError("release migration audit event was not appended exactly once")
            connection.commit()
            return {
                "required": True,
                "performed": True,
                "reason": (
                    "PAUSED_IDENTITY_REBOUND"
                    if identity_changed
                    else "PAUSED_SCHEMA_AUTHORITY_REBOUND"
                ),
                "invalidated_activation_records": pending_activations,
                "schema_migration": schema_migration,
                "audit_event_id": event_id,
                "audit_chain_length": after_count,
                "audit_chain_head": after_head,
                "generation": int(runtime["generation"]) + 1,
            }
        except BaseException:
            connection.rollback()
            raise
    except sqlite3.Error as exc:
        raise InstallError(
            _external_failure_code("PAUSED_RUNTIME_MIGRATION_FAILED", exc)
        ) from exc
    finally:
        connection.close()


def _validate_python_executable(value: Path | None) -> Path:
    interpreter = (value or Path(sys.executable)).expanduser().resolve(strict=True)
    running = Path(sys.executable).resolve(strict=True)
    if interpreter != running:
        raise InstallError(
            "--python-executable must resolve to the Python interpreter running the installer"
        )
    if sys.version_info < (3, 11):
        raise InstallError("full-live installation requires Python 3.11 or newer")
    if not interpreter.is_file() or not os.access(interpreter, os.X_OK):
        raise InstallError("Python interpreter is not an executable regular file")
    return interpreter


@contextmanager
def _deployment_interlock(root: Path, account_key: str):
    """Exclude another installer and the account writer across release commit."""

    fingerprint = hashlib.sha256(account_key.encode("utf-8")).hexdigest()[:24]
    global_lock_directory = _user_account_writer_lock_directory()
    if global_lock_directory.is_symlink() or (
        global_lock_directory.exists() and not global_lock_directory.is_dir()
    ):
        raise InstallError(
            f"account-global lock path is not a real directory: {global_lock_directory}"
        )
    global_lock_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(global_lock_directory, 0o700)
    paths = (
        root / "control/install.lock",
        global_lock_directory / f"account-{fingerprint}.writer.lock",
    )
    descriptors: list[int] = []
    try:
        for path in paths:
            flags = os.O_RDWR | os.O_CREAT
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(path, flags, 0o600)
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                os.close(descriptor)
                raise InstallError(f"deployment interlock is held: {path}") from exc
            descriptors.append(descriptor)
        yield
    finally:
        for descriptor in reversed(descriptors):
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _render_plist(
    root: Path,
    release_root: Path,
    python_executable: Path,
    *,
    template_relative: str = "deployment/com.harpcity.trader-brain-full-live.plist.in",
    label: str = LAUNCHD_LABEL,
    command: str = "serve",
    stdout_name: str = "launchd.stdout.log",
    stderr_name: str = "launchd.stderr.log",
) -> bytes:
    template = release_root / template_relative
    try:
        value = plistlib.loads(template.read_bytes())
    except (OSError, plistlib.InvalidFileException) as exc:
        raise InstallError(
            _external_failure_code("LAUNCHD_TEMPLATE_INVALID", exc)
        ) from exc
    substitutions = {
        "__PYTHON_EXECUTABLE__": str(python_executable),
        "__LAUNCHER__": str(root / "current/scripts/titan-full-live"),
        "__INSTALL_ROOT__": str(root),
        "__RELEASE_ROOT__": str(root / "current"),
        "__STDOUT_PATH__": str(root / f"logs/{stdout_name}"),
        "__STDERR_PATH__": str(root / f"logs/{stderr_name}"),
    }

    def substitute(item: Any) -> Any:
        if isinstance(item, str):
            return substitutions.get(item, item)
        if isinstance(item, list):
            return [substitute(child) for child in item]
        if isinstance(item, dict):
            return {key: substitute(child) for key, child in item.items()}
        return item

    value = substitute(value)
    expected_arguments = [
        str(python_executable),
        "-I",
        "-S",
        "-B",
        str(root / "current/scripts/titan-full-live"),
        command,
        "--install-root",
        str(root),
    ]
    expected_keys = {
        "Label",
        "Disabled",
        "KeepAlive",
        "ProcessType",
        "ThrottleInterval",
        "ProgramArguments",
        "WorkingDirectory",
        "StandardOutPath",
        "StandardErrorPath",
    }
    if (
        set(value) != expected_keys
        or
        value.get("Label") != label
        or "RunAtLoad" in value
        or value.get("Disabled") is not True
        or value.get("KeepAlive") != {"SuccessfulExit": False}
        or value.get("ProgramArguments") != expected_arguments
        or value.get("WorkingDirectory") != str(root / "current")
        or value.get("StandardOutPath") != str(root / f"logs/{stdout_name}")
        or value.get("StandardErrorPath") != str(root / f"logs/{stderr_name}")
    ):
        raise InstallError("unsafe launchd configuration")
    return plistlib.dumps(value, fmt=plistlib.FMT_XML, sort_keys=True)


def _verify_existing_release(path: Path, manifest: dict[str, Any], manifest_bytes: bytes) -> None:
    if path.is_symlink() or not path.is_dir():
        raise InstallError("existing release path is not a real directory")
    installed_manifest = path / "release-manifest.json"
    if not installed_manifest.is_file() or installed_manifest.read_bytes() != manifest_bytes:
        raise InstallError("existing release directory does not match this manifest")
    expected_files = {"release-manifest.json"}
    expected_directories: set[str] = set()
    for record in manifest["files"]:
        relative = str(record["path"])
        expected_files.add(relative)
        parent = PurePosixPath(relative).parent
        while parent != PurePosixPath("."):
            expected_directories.add(parent.as_posix())
            parent = parent.parent
        candidate = path / str(record["path"])
        if not candidate.is_file() or candidate.is_symlink():
            raise InstallError(f"existing release file is missing or unsafe: {record['path']}")
        if candidate.stat().st_size != record["size"] or sha256_file(candidate) != record["sha256"]:
            raise InstallError(f"existing release file failed verification: {record['path']}")
        if stat.S_IMODE(candidate.stat().st_mode) != int(str(record["mode"]), 8):
            raise InstallError(f"existing release file mode differs from manifest: {record['path']}")
    for candidate in path.rglob("*"):
        relative = candidate.relative_to(path).as_posix()
        if candidate.is_symlink():
            raise InstallError(f"existing release contains a symlink: {relative}")
        if candidate.is_file():
            if relative not in expected_files:
                raise InstallError(f"existing release contains an unmanifested file: {relative}")
        elif candidate.is_dir():
            if relative not in expected_directories:
                raise InstallError(f"existing release contains an unmanifested directory: {relative}")
        else:
            raise InstallError(f"existing release contains a special file: {relative}")


def _commit_install(
    *,
    install_root: Path,
    manifest: dict[str, Any],
    manifest_bytes: bytes,
    payloads: dict[str, bytes],
    archive_hash: str,
    interpreter: Path,
    config: dict[str, Any],
) -> dict[str, Any]:
    database = install_root / "state/full-live.sqlite3"
    current_mode = _database_mode(database)
    if current_mode is not None and current_mode != "PAUSED":
        raise InstallError(f"release switch requires PAUSED runtime; observed {current_mode}")

    release_id = str(manifest["release_manifest_hash"])
    destination = install_root / "releases" / release_id
    stage = Path(tempfile.mkdtemp(prefix=".install-", dir=install_root / "releases"))
    temporary_link = install_root / f".current-{release_id[:12]}"
    staged = True
    try:
        for record in manifest["files"]:
            relative = str(record["path"])
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            target.write_bytes(payloads[relative])
            target.chmod(int(str(record["mode"]), 8))
        (stage / "release-manifest.json").write_bytes(manifest_bytes)
        (stage / "release-manifest.json").chmod(0o644)
        _verify_existing_release(stage, manifest, manifest_bytes)
        plist_bytes = _render_plist(install_root, stage, interpreter)
        notification_plist_bytes = _render_plist(
            install_root,
            stage,
            interpreter,
            template_relative=(
                "deployment/com.harpcity.trader-brain-full-live-notifications.plist.in"
            ),
            label=NOTIFICATION_LAUNCHD_LABEL,
            command="notification-worker",
            stdout_name="notification-worker.launchd.stdout.log",
            stderr_name="notification-worker.launchd.stderr.log",
        )
        installed_at = datetime.now(timezone.utc).isoformat()
        install_record: dict[str, Any] = {
            "schema_version": INSTALL_SCHEMA,
            "installed_at": installed_at,
            "installed_mode": "PAUSED",
            "runtime_mode_authority": "state/full-live.sqlite3:runtime_identity.mode",
            "release_id": release_id,
            "release_manifest_sha256": sha256_bytes(manifest_bytes),
            "archive_sha256": archive_hash,
            "source_commit": manifest["source_commit"],
            "install_root": str(install_root),
            "current_release": str(destination),
            "launchd": {
                "label": LAUNCHD_LABEL,
                "staged_plist": str(
                    install_root / "launchd" / f"{LAUNCHD_LABEL}.plist"
                ),
                "disabled": True,
                "run_at_load": False,
                "installer_called_launchctl": False,
                "actual_loaded_state": "NOT_QUERIED",
                "process_role": "trading_coordinator_enqueue_only",
                "notification_worker": {
                    "label": NOTIFICATION_LAUNCHD_LABEL,
                    "staged_plist": str(
                        install_root
                        / "launchd"
                        / f"{NOTIFICATION_LAUNCHD_LABEL}.plist"
                    ),
                    "disabled": True,
                    "run_at_load": False,
                    "installer_called_launchctl": False,
                    "actual_loaded_state": "NOT_QUERIED",
                    "process_role": "independent_notification_outbox_delivery",
                },
            },
            "broker_accessed": False,
            "legacy_runtime_modified": False,
            "activation_required": True,
            "runtime_identity_migration": None,
        }

        if destination.is_symlink():
            raise InstallError("existing release directory may not be a symlink")
        if destination.exists():
            _verify_existing_release(destination, manifest, manifest_bytes)
        else:
            os.replace(stage, destination)
            staged = False
        _verify_existing_release(destination, manifest, manifest_bytes)

        # Prove and stage every release-pointer filesystem invariant before
        # committing any database schema or identity migration.
        current = install_root / "current"
        if os.path.lexists(current) and not current.is_symlink():
            raise InstallError("current release pointer exists but is not a symlink")
        if os.path.lexists(temporary_link):
            if not temporary_link.is_symlink():
                raise InstallError("temporary current pointer is not a symlink")
            temporary_link.unlink()
        temporary_link.symlink_to(
            Path("releases") / release_id, target_is_directory=True
        )

        # Recheck under both deployment and account-writer interlocks at the
        # release-pointer commit boundary.
        current_mode = _database_mode(database)
        if current_mode is not None and current_mode != "PAUSED":
            raise InstallError(f"release switch requires PAUSED runtime; observed {current_mode}")

        migration = _migrate_paused_runtime_identity(
            database,
            manifest=manifest,
            config=config,
            migrated_at=datetime.fromisoformat(installed_at),
        )
        install_record["runtime_identity_migration"] = migration

        os.replace(temporary_link, current)

        # These writes are validated and prepared before the pointer commit. A
        # crash can make CLI metadata inconsistent, but every CLI command then
        # fails closed on manifest/current binding instead of running mixed code.
        _atomic_write(install_root / "release-manifest.json", manifest_bytes, 0o600)
        plist_path = install_root / "launchd" / f"{LAUNCHD_LABEL}.plist"
        _atomic_write(plist_path, plist_bytes, 0o600)
        notification_plist_path = (
            install_root / "launchd" / f"{NOTIFICATION_LAUNCHD_LABEL}.plist"
        )
        _atomic_write(notification_plist_path, notification_plist_bytes, 0o600)
        _atomic_write(
            install_root / "control/install-state.json",
            canonical_json(install_record),
            0o600,
        )
        return install_record
    finally:
        if os.path.lexists(temporary_link):
            temporary_link.unlink()
        if staged and stage.exists():
            shutil.rmtree(stage)


def install(
    archive: Path,
    root: Path,
    *,
    expected_archive_sha256: str | None = None,
    python_executable: Path | None = None,
) -> dict[str, Any]:
    archive = archive.resolve(strict=True)
    expected_hash = _expected_archive_hash(archive, expected_archive_sha256)
    actual_hash = sha256_file(archive)
    if actual_hash != expected_hash:
        raise InstallError("release archive checksum mismatch")
    manifest, manifest_bytes, payloads = _read_archive(archive)
    _verify_manifest(manifest, payloads)
    install_root = _assert_install_root(root)
    interpreter = _validate_python_executable(python_executable)

    try:
        config = json.loads(payloads["config/full_live.json"])
        account_key = str(config["account"]["masked_identifier"])
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise InstallError("release account binding cannot be read") from exc
    if account_key != "ending-7153":
        raise InstallError("release account binding is not ending-7153")

    install_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if install_root.is_symlink() or not install_root.is_dir():
        raise InstallError("install root must be a real directory")
    for relative in (
        "releases",
        "state",
        "state/locks",
        "state/eod",
        "logs",
        "launchd",
        "control",
        "control/inbox",
        "control/processed",
        "control/rejected",
    ):
        _ensure_private_directory(install_root, relative)

    with _deployment_interlock(install_root, account_key):
        return _commit_install(
            install_root=install_root,
            manifest=manifest,
            manifest_bytes=manifest_bytes,
            payloads=payloads,
            archive_hash=actual_hash,
            interpreter=interpreter,
            config=config,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.home() / "Library/Application Support/Titan Momentum/full-live",
    )
    parser.add_argument("--expected-archive-sha256")
    parser.add_argument(
        "--python-executable",
        type=Path,
        help="Python >=3.11 interpreter recorded in the staged launchd plist",
    )
    arguments = parser.parse_args(argv)
    try:
        result = install(
            arguments.archive,
            arguments.root,
            expected_archive_sha256=arguments.expected_archive_sha256,
            python_executable=arguments.python_executable,
        )
    except (InstallError, FileNotFoundError, OSError) as exc:
        print(_external_failure_code("INSTALL_BLOCKED", exc), file=sys.stderr)
        return 78
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
