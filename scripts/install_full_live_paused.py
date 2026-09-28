#!/usr/bin/env python3
"""Verify and install a Titan full-live release in PAUSED mode.

This installer is intentionally incapable of loading launchd, starting the
runtime, or contacting the broker.  It writes release state below ``--root``
and takes the fixed per-user coordinator and account-writer interlocks,
independent of the selected install root, so it cannot replace a release under
either a live reconciler or a broker mutation.
"""

from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import pwd
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo


MANIFEST_SCHEMA = "titan_full_live_release_2026-09-14_v2"
INSTALL_SCHEMA = "titan_full_live_paused_install_2026-09-14_v4"
LAUNCHD_LABEL = "com.harpcity.trader-brain-full-live"
NOTIFICATION_LAUNCHD_LABEL = "com.harpcity.trader-brain-full-live-notifications"
MAX_ARCHIVE_MEMBER_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 128 * 1024 * 1024
# Allow bounded tar/gzip framing overhead while keeping the authenticated
# archive snapshot small enough to hold as immutable bytes during validation.
MAX_ARCHIVE_FILE_BYTES = MAX_ARCHIVE_TOTAL_BYTES + (32 * 1024 * 1024)
HEX = frozenset("0123456789abcdef")
_USER_LOCK_RELATIVE_PATH = Path(
    "Library/Application Support/Titan Momentum/account-writer-locks"
)
_ATTENDED_COORDINATOR_LOCK_PREFIX = "attended-read-coordinator::"
_SUPPORTED_STATE_SCHEMA_VERSION = 3
_ZERO_AUDIT_HASH = "0" * 64
_IBKR_PROFILE_SCHEMA = "titan_ibkr_local_provider_profile_v2"
_IBKR_SDK_RECEIPT_SCHEMA = "titan_ibkr_sdk_snapshot_attestation_v1"
_IBKR_SDK_RECEIPT_RELATIVE = Path("control/ibkr-sdk-attestation.json")
_MAX_SDK_FILE_BYTES = 16 * 1024 * 1024
_MAX_SDK_TOTAL_BYTES = 128 * 1024 * 1024
_IBKR_RISK_LEDGER_APPLICATION_ID = 0x54495242
_IBKR_RISK_LEDGER_SCHEMA_VERSION = 4
_IBKR_RISK_LEDGER_RELATIVE = Path("state/ibkr-risk-high-water.sqlite3")
_IBKR_RISK_LEDGER_ARCHIVE_RELATIVE = Path(
    "state/ibkr-risk-high-water-archive"
)
_IBKR_RISK_LEGACY_BINDING_COLUMNS = (
    "singleton",
    "release_manifest_hash",
    "config_hash",
    "policy_binding_id",
    "risk_binding_id",
    "account_key",
    "account_masked",
    "account_binding_fingerprint",
    "latest_trading_date",
    "highest_equity",
)
_IBKR_RISK_BINDING_COLUMNS = (
    *_IBKR_RISK_LEGACY_BINDING_COLUMNS[:-2],
    "lineage_hash",
    *_IBKR_RISK_LEGACY_BINDING_COLUMNS[-2:],
)
_IBKR_RISK_DAILY_COLUMNS = (
    "trading_date",
    "baseline_receipt_hash",
    "baseline_provider_receipt_sha256",
    "baseline_prior_high_water_equity",
    "peak_equity",
    "last_net_liquidation",
    "last_observed_at",
)
_IBKR_RISK_STARTING_COLUMNS = (
    "trading_date", "starting_equity", "starting_equity_as_of",
    "starting_equity_provider_receipt_sha256", "latest_external_cash_flow",
    "latest_external_cash_flow_as_of", "latest_external_cash_flow_receipt_sha256",
)
_IBKR_RISK_CARRY_COLUMNS = (
    "singleton",
    "source_release_manifest_hash",
    "source_config_hash",
    "source_policy_binding_id",
    "source_risk_binding_id",
    "source_account_key",
    "source_account_masked",
    "source_account_binding_fingerprint",
    "source_latest_trading_date",
    "source_highest_equity",
    "source_ledger_sha256",
    "archive_relative_path",
    "migrated_at",
)
_INSTALLER_RELATIVE_PATH = "scripts/install_full_live_paused.py"
_FIXED_RELEASE_PATHS = frozenset(
    {
        "pyproject.toml",
        "README.md",
        "ARCHITECTURE.md",
        "CODEX_FULL_LIVE_AUTONOMY_2026-09-08.md",
        _INSTALLER_RELATIVE_PATH,
        "scripts/titan-full-live",
        "deployment/com.harpcity.trader-brain-full-live.plist.in",
        "deployment/com.harpcity.trader-brain-full-live-notifications.plist.in",
        "validation/full-live/2026-09-08/OPERATIONS.md",
        "validation/full-live/2026-09-14/PROPOSED_OWNER_POLICY_2026-09-14.md",
        "validation/full-live/2026-09-14/OWNER_POLICY_APPROVAL_2026-09-14.md",
        "validation/full-live/2026-09-14/OWNER_DAILY_STARTING_EQUITY_POLICY_AMENDMENT_2026-09-14.md",
    }
)
_REQUIRED_RUNTIME_MODULES = frozenset(
    {
        "titan_brain",
        "titan_brain.live",
        "titan_brain.live.cli",
        "titan_brain.live.local_assembly",
    }
)


def _configured_account_key(config: dict[str, Any]) -> str:
    """Read the non-secret release account namespace without legacy pinning."""

    try:
        account = config["account"]
        if not isinstance(account, dict):
            raise TypeError("account is not an object")
        last4 = str(account["required_last4"])
        masked = str(account["masked_identifier"])
        key = str(account.get("account_key", masked))
    except (KeyError, TypeError) as exc:
        raise InstallError("release account binding cannot be read") from exc
    if (
        not re.fullmatch(r"[0-9]{4}", last4)
        or masked != f"ending-{last4}"
        or not re.fullmatch(r"[a-z][a-z0-9_-]{2,127}", key)
        or re.search(r"[0-9]{5,}", key)
    ):
        raise InstallError("release account binding is invalid")
    return key
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


def _archive_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_mode),
        int(metadata.st_nlink),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        int(metadata.st_ctime_ns),
    )


def _assert_archive_path_identity(
    archive: Path, expected: tuple[int, int, int, int, int, int, int]
) -> None:
    try:
        observed = archive.lstat()
    except OSError as exc:
        raise InstallError("release archive path changed during verification") from exc
    if not stat.S_ISREG(observed.st_mode) or _archive_identity(observed) != expected:
        raise InstallError("release archive path changed during verification")


def _capture_verified_archive(archive: Path, expected_hash: str) -> tuple[bytes, str]:
    """Hash and retain the exact regular-file bytes that will be parsed."""

    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(archive, flags)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise InstallError("release archive must be a regular file")
        identity = _archive_identity(before)
        _assert_archive_path_identity(archive, identity)
        if before.st_size <= 0 or before.st_size > MAX_ARCHIVE_FILE_BYTES:
            raise InstallError("release archive file size is invalid")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            data = handle.read(MAX_ARCHIVE_FILE_BYTES + 1)
            after = os.fstat(handle.fileno())
        if len(data) != before.st_size or len(data) > MAX_ARCHIVE_FILE_BYTES:
            raise InstallError("release archive changed while being read")
        if _archive_identity(after) != identity:
            raise InstallError("release archive changed while being read")
        _assert_archive_path_identity(archive, identity)
    except InstallError:
        raise
    except OSError as exc:
        raise InstallError(
            _external_failure_code("RELEASE_ARCHIVE_UNREADABLE", exc)
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    actual_hash = sha256_bytes(data)
    if actual_hash != expected_hash:
        raise InstallError("release archive checksum mismatch")
    return data, actual_hash


def _safe_member_name(name: str) -> str:
    pure = PurePosixPath(name)
    if pure.is_absolute() or not pure.parts or ".." in pure.parts or "." in pure.parts:
        raise InstallError(f"unsafe archive member: {name!r}")
    normalized = pure.as_posix()
    if normalized != name or "\\" in name:
        raise InstallError(f"non-canonical archive member: {name!r}")
    return normalized


def _read_archive(archive_bytes: bytes) -> tuple[dict[str, Any], bytes, dict[str, bytes]]:
    payloads: dict[str, bytes] = {}
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as bundle:
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


def _is_release_source_path(relative: str) -> bool:
    """Mirror the builder's complete committed release-path selection."""

    pure = PurePosixPath(relative)
    parts = pure.parts
    if relative in _FIXED_RELEASE_PATHS:
        return True
    if len(parts) == 2 and parts[0] == "config" and pure.suffix == ".json":
        return True
    if (
        len(parts) >= 3
        and parts[0:2] == ("src", "titan_brain")
        and pure.suffix == ".py"
    ):
        return True
    if len(parts) == 2 and parts[0] == "deployment":
        return pure.suffix == ".md" or relative.endswith(".plist.in")
    return False


def _source_path_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_mode),
        int(metadata.st_nlink),
    )


def _assert_source_path_identity(
    path: Path,
    expected: tuple[int, int, int, int],
    *,
    label: str,
) -> None:
    try:
        observed = path.lstat()
    except OSError as exc:
        raise InstallError(f"trusted Git {label} was replaced during verification") from exc
    if _source_path_identity(observed) != expected:
        raise InstallError(f"trusted Git {label} was replaced during verification")


def _trusted_git(
    root: Path, *arguments: str, input_bytes: bytes | None = None
) -> bytes:
    """Run a read-only Git object query with replacement refs disabled."""

    environment = os.environ.copy()
    for name in tuple(environment):
        if name in {
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_COMMON_DIR",
            "GIT_CONFIG_COUNT",
            "GIT_CONFIG_PARAMETERS",
            "GIT_DIR",
            "GIT_GRAFT_FILE",
            "GIT_INDEX_FILE",
            "GIT_NAMESPACE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_REPLACE_REF_BASE",
            "GIT_SHALLOW_FILE",
            "GIT_WORK_TREE",
        } or name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            environment.pop(name, None)
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    environment["GIT_NO_LAZY_FETCH"] = "1"
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    environment["GIT_TERMINAL_PROMPT"] = "0"
    try:
        completed = subprocess.run(
            ["git", "--no-replace-objects", "-C", str(root), *arguments],
            input=input_bytes,
            check=False,
            capture_output=True,
            env=environment,
            timeout=15,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        raise InstallError("trusted Git repository cannot be read") from exc
    if completed.returncode != 0:
        raise InstallError("trusted Git repository query failed")
    return completed.stdout


def _validate_expected_source_revision(value: str) -> str:
    revision = str(value).strip().lower()
    if len(revision) != 40 or any(character not in HEX for character in revision):
        raise InstallError(
            "expected source revision must be an exact 40-character Git commit"
        )
    return revision


def _trusted_git_tree(
    root: Path, revision: str
) -> dict[str, tuple[str, str, str]]:
    raw = _trusted_git(root, "ls-tree", "-r", "-z", "--full-tree", revision)
    entries: dict[str, tuple[str, str, str]] = {}
    try:
        encoded_entries = raw.split(b"\0")
        for encoded in encoded_entries:
            if not encoded:
                continue
            metadata, separator, encoded_path = encoded.partition(b"\t")
            fields = metadata.decode("ascii").split(" ")
            relative = encoded_path.decode("utf-8")
            pure = PurePosixPath(relative)
            if (
                not separator
                or len(fields) != 3
                or not relative
                or relative != pure.as_posix()
                or pure.is_absolute()
                or "." in pure.parts
                or ".." in pure.parts
                or "\\" in relative
                or relative in entries
            ):
                raise ValueError("non-canonical Git tree entry")
            entries[relative] = (fields[0], fields[1], fields[2])
    except (UnicodeDecodeError, ValueError) as exc:
        raise InstallError("trusted Git commit tree is malformed") from exc
    return entries


def _trusted_git_blobs(root: Path, object_ids: list[str]) -> dict[str, bytes]:
    unique = sorted(set(object_ids))
    request = ("\n".join(unique) + "\n").encode("ascii")
    checked = _trusted_git(
        root,
        "cat-file",
        "--batch-check=%(objectname) %(objecttype) %(objectsize)",
        input_bytes=request,
    )
    sizes: dict[str, int] = {}
    try:
        lines = checked.decode("ascii").splitlines()
        if len(lines) != len(unique):
            raise ValueError("unexpected Git object count")
        total = 0
        for requested, line in zip(unique, lines, strict=True):
            fields = line.split(" ")
            if len(fields) != 3 or fields[0] != requested or fields[1] != "blob":
                raise ValueError("unexpected Git object metadata")
            size = int(fields[2])
            if size < 0 or size > MAX_ARCHIVE_MEMBER_BYTES:
                raise ValueError("Git blob exceeds release limit")
            total += size
            if total > MAX_ARCHIVE_TOTAL_BYTES:
                raise ValueError("Git blobs exceed release limit")
            sizes[requested] = size
    except (UnicodeDecodeError, ValueError) as exc:
        raise InstallError("trusted Git release blob inventory is invalid") from exc

    raw = _trusted_git(root, "cat-file", "--batch", input_bytes=request)
    result: dict[str, bytes] = {}
    offset = 0
    try:
        for requested in unique:
            end = raw.find(b"\n", offset)
            if end < 0:
                raise ValueError("missing Git object header")
            header = raw[offset:end].decode("ascii").split(" ")
            offset = end + 1
            if (
                len(header) != 3
                or header[0] != requested
                or header[1] != "blob"
                or int(header[2]) != sizes[requested]
            ):
                raise ValueError("unexpected Git object header")
            size = sizes[requested]
            data = raw[offset : offset + size]
            offset += size
            if len(data) != size or raw[offset : offset + 1] != b"\n":
                raise ValueError("invalid Git object payload boundary")
            offset += 1
            result[requested] = data
        if offset != len(raw):
            raise ValueError("unexpected Git object trailing data")
    except (UnicodeDecodeError, ValueError) as exc:
        raise InstallError("trusted Git release blobs cannot be read") from exc
    return result


def _verify_trusted_source_provenance(
    manifest: dict[str, Any],
    payloads: dict[str, bytes],
    *,
    trusted_source_root: Path,
    expected_source_revision: str,
) -> dict[str, Any]:
    """Bind the complete archive inventory to one explicit immutable Git tree."""

    requested_root = Path(trusted_source_root).expanduser()
    try:
        requested_metadata = requested_root.lstat()
    except OSError as exc:
        raise InstallError("trusted Git repository is missing") from exc
    if requested_root.is_symlink() or not stat.S_ISDIR(requested_metadata.st_mode):
        raise InstallError("trusted Git repository must be a real directory")
    root_identity = _source_path_identity(requested_metadata)
    try:
        root = requested_root.resolve(strict=True)
    except OSError as exc:
        raise InstallError("trusted Git repository cannot be resolved") from exc
    revision = _validate_expected_source_revision(expected_source_revision)
    if manifest.get("source_commit") != revision:
        raise InstallError(
            "release manifest source_commit does not match expected source revision"
        )

    try:
        top_level = Path(
            _trusted_git(root, "rev-parse", "--show-toplevel")
            .decode("utf-8")
            .strip()
        ).resolve(strict=True)
        git_directory = Path(
            _trusted_git(root, "rev-parse", "--absolute-git-dir")
            .decode("utf-8")
            .strip()
        ).resolve(strict=True)
        object_format = (
            _trusted_git(root, "rev-parse", "--show-object-format")
            .decode("ascii")
            .strip()
        )
        git_metadata = git_directory.lstat()
    except (OSError, UnicodeDecodeError) as exc:
        raise InstallError("trusted Git repository metadata cannot be read") from exc
    if top_level != root:
        raise InstallError("trusted source root must be the Git repository top level")
    if object_format != "sha1":
        raise InstallError("trusted Git repository object format is unsupported")
    if git_directory.is_symlink() or not stat.S_ISDIR(git_metadata.st_mode):
        raise InstallError("trusted Git metadata must be a real directory")
    git_identity = _source_path_identity(git_metadata)

    try:
        resolved_revision = (
            _trusted_git(root, "rev-parse", "--verify", f"{revision}^{{commit}}")
            .decode("ascii")
            .strip()
            .lower()
        )
    except UnicodeDecodeError as exc:
        raise InstallError("trusted Git source revision cannot be read") from exc
    if resolved_revision != revision:
        raise InstallError("expected source revision is not the exact trusted Git commit")

    tree = _trusted_git_tree(root, revision)
    missing_fixed = sorted(_FIXED_RELEASE_PATHS - set(tree))
    if missing_fixed:
        raise InstallError(
            f"trusted Git commit lacks required release sources: {missing_fixed}"
        )
    committed_release = {
        relative: entry
        for relative, entry in tree.items()
        if _is_release_source_path(relative)
    }
    manifest_records = {
        str(record["path"]): record for record in manifest["files"]
    }
    if set(manifest_records) != set(committed_release):
        missing = sorted(set(committed_release) - set(manifest_records))
        unexpected = sorted(set(manifest_records) - set(committed_release))
        raise InstallError(
            "release manifest inventory differs from trusted Git commit; "
            f"missing={missing}, unexpected={unexpected}"
        )

    object_ids: list[str] = []
    for relative, (mode, object_type, object_id) in committed_release.items():
        if object_type != "blob" or mode not in {"100644", "100755"}:
            raise InstallError(
                f"trusted Git release source is not a regular blob: {relative}"
            )
        expected_mode = "0755" if mode == "100755" else "0644"
        if manifest_records[relative].get("mode") != expected_mode:
            raise InstallError(
                f"release member mode differs from trusted Git commit: {relative}"
            )
        object_ids.append(object_id)

    blobs = _trusted_git_blobs(root, object_ids)
    trusted_records: list[dict[str, Any]] = []
    for relative in sorted(committed_release):
        mode, _object_type, object_id = committed_release[relative]
        committed = blobs[object_id]
        archived = payloads[relative]
        record = manifest_records[relative]
        if (
            archived != committed
            or record.get("size") != len(committed)
            or record.get("sha256") != sha256_bytes(committed)
        ):
            raise InstallError(
                f"release member differs from trusted Git commit: {relative}"
            )
        trusted_records.append(
            {
                "path": relative,
                "sha256": sha256_bytes(committed),
                "size": len(committed),
                "mode": "0755" if mode == "100755" else "0644",
            }
        )

    if manifest["files"] != trusted_records:
        raise InstallError(
            "release manifest file descriptor differs from trusted Git commit"
        )
    try:
        config_path = str(manifest["config_path"])
        config = json.loads(blobs[committed_release[config_path][2]])
        if not isinstance(config, dict):
            raise TypeError("config is not an object")
        approval = config.get("owner_policy_approval")
        if approval is not None:
            if not isinstance(approval, dict):
                raise InstallError("owner policy approval must be an object")
            for path_field, hash_field in (
                ("proposal_path", "proposal_sha256"),
                ("approval_record_path", "approval_record_sha256"),
            ):
                relative = approval.get(path_field)
                digest = approval.get(hash_field)
                if (
                    not isinstance(relative, str)
                    or relative not in _FIXED_RELEASE_PATHS
                    or relative not in committed_release
                    or not isinstance(digest, str)
                    or sha256_bytes(blobs[committed_release[relative][2]]) != digest
                ):
                    raise InstallError("owner policy approval artifact binding is invalid")
        amendment = config.get("owner_risk_policy_amendment")
        if amendment is not None:
            if not isinstance(amendment, dict):
                raise InstallError("owner risk policy amendment must be an object")
            relative = amendment.get("amendment_path")
            digest = amendment.get("amendment_sha256")
            if (
                not isinstance(relative, str)
                or relative not in _FIXED_RELEASE_PATHS
                or relative not in committed_release
                or not isinstance(digest, str)
                or sha256_bytes(blobs[committed_release[relative][2]]) != digest
            ):
                raise InstallError("owner risk policy amendment artifact binding is invalid")
        risk_relative = str(config["risk"]["limits_path"])
        risk = json.loads(blobs[committed_release[risk_relative][2]])
        if not isinstance(risk, dict):
            raise TypeError("risk limits are not an object")
        deployment = config.get("deployment")
        if deployment is not None and not isinstance(deployment, dict):
            raise TypeError("deployment is not an object")
        install_subtree = str(
            deployment.get("install_subtree")
            if isinstance(deployment, dict)
            else "Application Support/Titan Momentum/full-live"
        )
        config_hash = sha256_bytes(canonical_json(config))
        risk_hash = sha256_bytes(canonical_json(risk))
        policy_hash = sha256_bytes(
            canonical_json(
                {
                    "account": config["account"],
                    "scope": config["scope"],
                    "sessions": config["sessions"],
                    "risk": config["risk"],
                    "risk_hash": risk_hash,
                    "strategy_id": config["strategy_id"],
                }
            )
        )
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise InstallError(
            "trusted Git release configuration cannot produce a manifest"
        ) from exc

    trusted_descriptor: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA,
        "release_name": "titan-full-live",
        "config_path": config_path,
        "source_commit": revision,
        "source_tree": sha256_bytes(canonical_json(trusted_records)),
        "config_hash": config_hash,
        "policy_hash": policy_hash,
        "reproducible_epoch": 0,
        "python_requires": ">=3.11",
        "entrypoint": "scripts/titan-full-live",
        "default_mode": "PAUSED",
        "install_subtree": install_subtree,
        "launchd_template": (
            "deployment/com.harpcity.trader-brain-full-live.plist.in"
        ),
        "notification_launchd_template": (
            "deployment/com.harpcity.trader-brain-full-live-notifications.plist.in"
        ),
        "files": trusted_records,
    }
    descriptor = dict(manifest)
    descriptor.pop("release_manifest_hash")
    if descriptor != trusted_descriptor:
        raise InstallError(
            "release manifest descriptor differs from trusted Git commit"
        )
    if manifest["release_manifest_hash"] != sha256_bytes(
        canonical_json(trusted_descriptor)
    ):
        raise InstallError(
            "release manifest identity differs from trusted Git commit"
        )

    _assert_source_path_identity(
        requested_root, root_identity, label="repository"
    )
    _assert_source_path_identity(
        git_directory, git_identity, label="metadata directory"
    )
    final_revision = (
        _trusted_git(root, "rev-parse", "--verify", f"{revision}^{{commit}}")
        .decode("ascii")
        .strip()
        .lower()
    )
    if final_revision != revision:
        raise InstallError("trusted Git source revision was replaced during verification")
    return {
        "repository": str(root),
        "expected_revision": revision,
        "verified_release_files": len(committed_release),
        "git_replacement_objects_disabled": True,
    }


def _runtime_module_name(relative: str) -> tuple[str, bool] | None:
    """Map one release path to its import name without executing release code."""

    pure = PurePosixPath(relative)
    if len(pure.parts) < 3 or pure.parts[:2] != ("src", "titan_brain"):
        return None
    if pure.suffix != ".py":
        raise InstallError(
            f"release runtime inventory contains a non-Python source: {relative}"
        )
    module_parts = list(pure.parts[1:])
    is_package = module_parts[-1] == "__init__.py"
    if is_package:
        module_parts.pop()
    else:
        module_parts[-1] = PurePosixPath(module_parts[-1]).stem
    if not module_parts or any(not part.isidentifier() for part in module_parts):
        raise InstallError(f"release runtime module path is invalid: {relative}")
    return ".".join(module_parts), is_package


def _resolve_internal_import(
    *,
    importing_module: str,
    importing_is_package: bool,
    level: int,
    imported_module: str | None,
) -> str:
    if level == 0:
        return str(imported_module or "")
    package_parts = importing_module.split(".")
    if not importing_is_package:
        package_parts.pop()
    parent_hops = level - 1
    if parent_hops >= len(package_parts):
        raise InstallError(
            f"release runtime has an invalid relative import in {importing_module}"
        )
    if parent_hops:
        package_parts = package_parts[:-parent_hops]
    if imported_module:
        package_parts.extend(imported_module.split("."))
    return ".".join(package_parts)


def _verify_runtime_python_inventory(payloads: dict[str, bytes]) -> None:
    """Statically prove syntax and the complete internal import closure.

    The builder's contract is every ``src/titan_brain/**/*.py`` file.  The
    installer does not execute archive code before committing a release, but
    it parses every supplied runtime module and rejects any internal import
    whose module/package is absent.  This avoids a second hand-maintained list
    of implementation files while still detecting a self-consistent archive
    with (for example) the activation or broker adapter module removed.
    """

    modules: dict[str, tuple[str, bool]] = {}
    source_paths: list[tuple[str, str, bool]] = []
    for relative in sorted(payloads):
        mapped = _runtime_module_name(relative)
        if mapped is None:
            continue
        module_name, is_package = mapped
        if module_name in modules:
            raise InstallError(f"duplicate release runtime module: {module_name}")
        modules[module_name] = (relative, is_package)
        source_paths.append((relative, module_name, is_package))
    missing_roots = sorted(_REQUIRED_RUNTIME_MODULES - set(modules))
    if missing_roots:
        raise InstallError(
            f"release runtime inventory lacks required module roots: {missing_roots}"
        )

    # The launcher is part of the executable import boundary even though it is
    # not itself a package module.
    parse_targets = list(source_paths)
    parse_targets.append(("scripts/titan-full-live", "<release-launcher>", False))
    for relative, module_name, is_package in parse_targets:
        try:
            source = payloads[relative].decode("utf-8")
            tree = ast.parse(source, filename=relative)
        except (KeyError, UnicodeDecodeError, SyntaxError) as exc:
            raise InstallError(
                f"release runtime source cannot be parsed: {relative}"
            ) from exc
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if module_name == "<release-launcher>" and node.level:
                    raise InstallError("release launcher cannot use relative imports")
                targets.append(
                    _resolve_internal_import(
                        importing_module=module_name,
                        importing_is_package=is_package,
                        level=node.level,
                        imported_module=node.module,
                    )
                )
            for target in targets:
                if target == "titan_brain" or target.startswith("titan_brain."):
                    if target not in modules:
                        raise InstallError(
                            "release runtime internal import is missing: "
                            f"{relative} -> {target}"
                        )


def _verify_running_installer(
    manifest: dict[str, Any], payloads: dict[str, bytes]
) -> dict[str, Any]:
    """Bind the process performing installation to the release inventory."""

    source = Path(__file__).absolute()
    try:
        metadata = source.lstat()
        data = source.read_bytes()
    except OSError as exc:
        raise InstallError("running installer cannot be attested") from exc
    if source.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise InstallError("running installer must be a regular non-symlink file")
    if stat.S_IMODE(metadata.st_mode) != 0o755:
        raise InstallError("running installer must have exact mode 0755")
    records = {
        str(record.get("path")): record
        for record in manifest.get("files", ())
        if isinstance(record, dict)
    }
    record = records.get(_INSTALLER_RELATIVE_PATH)
    archived = payloads.get(_INSTALLER_RELATIVE_PATH)
    digest = sha256_bytes(data)
    if (
        record is None
        or archived is None
        or record.get("mode") != "0755"
        or record.get("size") != len(data)
        or record.get("sha256") != digest
        or data != archived
    ):
        raise InstallError(
            "running installer does not match the release-attested installer"
        )
    return {
        "schema_version": INSTALL_SCHEMA,
        "sha256": digest,
        "source_commit": manifest["source_commit"],
        "source_path": str(source),
        "python_executable": str(Path(sys.executable).resolve(strict=True)),
        "python_implementation": sys.implementation.name,
        "python_version": (
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        ),
    }


def _verify_manifest(manifest: dict[str, Any], payloads: dict[str, bytes]) -> None:
    expected_manifest_fields = {
        "schema_version",
        "release_name",
        "config_path",
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
        "launchd_template": "deployment/com.harpcity.trader-brain-full-live.plist.in",
        "notification_launchd_template": (
            "deployment/com.harpcity.trader-brain-full-live-notifications.plist.in"
        ),
    }
    for field, expected_value in required_values.items():
        if manifest.get(field) != expected_value:
            raise InstallError(f"release manifest {field} is invalid")
    config_path = _safe_member_name(str(manifest.get("config_path", "")))
    config_pure = PurePosixPath(config_path)
    if config_pure.parent != PurePosixPath("config") or config_pure.suffix != ".json":
        raise InstallError("release manifest config_path is invalid")
    if manifest.get("install_subtree") not in {
        "Application Support/Titan Momentum/full-live",
        "Application Support/Titan Momentum/full-live-ibkr-ending-3103",
    }:
        raise InstallError("release manifest install_subtree is invalid")
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
        _INSTALLER_RELATIVE_PATH,
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
    if config_path not in expected:
        raise InstallError("release inventory lacks the selected config profile")
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
    if expected[_INSTALLER_RELATIVE_PATH].get("mode") != "0755":
        raise InstallError("release installer must have mode 0755")
    _verify_runtime_python_inventory(payloads)


def _assert_install_root(root: Path, install_subtree: str) -> Path:
    expanded = root.expanduser()
    subtree = PurePosixPath(str(install_subtree))
    if (
        subtree.is_absolute()
        or ".." in subtree.parts
        or subtree.as_posix()
        not in {
            "Application Support/Titan Momentum/full-live",
            "Application Support/Titan Momentum/full-live-ibkr-ending-3103",
        }
    ):
        raise InstallError("release install subtree is invalid")
    required_suffix = subtree.parts
    if tuple(expanded.parts[-len(required_suffix):]) != required_suffix:
        raise InstallError(
            f"install root must be under the exact signed subtree {subtree.as_posix()!r}"
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
            "account_key": _configured_account_key(config),
            "release_manifest_hash": str(manifest["release_manifest_hash"]),
            "config_hash": str(manifest["config_hash"]),
            "policy_hash": str(manifest["policy_hash"]),
        }
    except (KeyError, TypeError) as exc:
        raise InstallError("target runtime identity is incomplete") from exc
    if not target["runtime_id"].strip() or not target["account_key"].strip():
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


def _ibkr_risk_hash(value: object, field: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in HEX for character in value)
    ):
        raise InstallError(f"IBKR risk ledger {field} is invalid")
    return value


def _ibkr_risk_decimal(value: object, field: str, *, positive: bool = True) -> Decimal:
    if type(value) is not str or not re.fullmatch(
        r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", value
    ):
        raise InstallError(f"IBKR risk ledger {field} is invalid")
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise InstallError(f"IBKR risk ledger {field} is invalid") from exc
    normalized = format(parsed, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    normalized = "0" if parsed == 0 else normalized
    if not parsed.is_finite() or (positive and parsed <= 0) or normalized != value:
        raise InstallError(f"IBKR risk ledger {field} is invalid")
    return parsed


def _ibkr_risk_decimal_text(value: Decimal) -> str:
    normalized = format(value, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return "0" if value == 0 else normalized


def _ibkr_risk_date(value: object, field: str) -> date:
    if type(value) is not str:
        raise InstallError(f"IBKR risk ledger {field} is invalid")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise InstallError(f"IBKR risk ledger {field} is invalid") from exc
    if parsed.isoformat() != value:
        raise InstallError(f"IBKR risk ledger {field} is invalid")
    return parsed


def _ibkr_risk_time(value: object, field: str) -> datetime:
    if type(value) is not str:
        raise InstallError(f"IBKR risk ledger {field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InstallError(f"IBKR risk ledger {field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InstallError(f"IBKR risk ledger {field} is invalid")
    return parsed.astimezone(timezone.utc)


def _ibkr_risk_target(
    *,
    manifest: dict[str, Any],
    config: dict[str, Any],
    payloads: dict[str, bytes],
) -> dict[str, str] | None:
    """Derive the exact runtime ledger identity from the verified release."""

    execution = config.get("execution")
    if not isinstance(execution, dict):
        return None
    raw_relative = execution.get("ibkr_risk_high_water_ledger_relative_path")
    if raw_relative is None:
        if (
            execution.get("broker_adapter") == "supported_production_transport"
            and execution.get("execution_authority_mode") == "unattended"
        ):
            raise InstallError(
                "IBKR risk high-water ledger must use canonical path "
                "state/ibkr-risk-high-water.sqlite3"
            )
        return None
    if type(raw_relative) is not str:
        raise InstallError("IBKR risk high-water ledger path is invalid")
    relative = Path(raw_relative)
    if (
        relative.is_absolute()
        or relative.parent != _IBKR_RISK_LEDGER_RELATIVE.parent
        or relative.suffix != ".sqlite3"
        or relative.as_posix() != raw_relative
        or relative.name == "full-live.sqlite3"
        or any(part in {"", ".", ".."} for part in relative.parts)
        or relative != _IBKR_RISK_LEDGER_RELATIVE
    ):
        raise InstallError(
            "IBKR risk high-water ledger must use canonical path "
            "state/ibkr-risk-high-water.sqlite3"
        )
    risk_config = config.get("risk")
    account = config.get("account")
    if not isinstance(risk_config, dict) or not isinstance(account, dict):
        raise InstallError("IBKR risk ledger release bindings are incomplete")
    risk_relative = str(risk_config.get("limits_path", ""))
    risk_bytes = payloads.get(risk_relative)
    if risk_bytes is None:
        raise InstallError("IBKR risk limits are absent from the release")
    try:
        risk = json.loads(risk_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallError("IBKR risk limits are invalid") from exc
    if not isinstance(risk, dict):
        raise InstallError("IBKR risk limits are invalid")
    last4 = str(account.get("required_last4", ""))
    account_key = _configured_account_key(config)
    fingerprint = _ibkr_risk_hash(
        execution.get("production_account_binding_fingerprint"),
        "account binding fingerprint",
    )
    return {
        "relative_path": _IBKR_RISK_LEDGER_RELATIVE.as_posix(),
        "release_manifest_hash": _ibkr_risk_hash(
            manifest.get("release_manifest_hash"), "release manifest hash"
        ),
        "config_hash": _ibkr_risk_hash(
            manifest.get("config_hash"), "config hash"
        ),
        "policy_binding_id": _ibkr_risk_hash(
            manifest.get("policy_hash"), "policy binding"
        ),
        "risk_binding_id": sha256_bytes(canonical_json(risk)),
        "account_key": account_key,
        "account_masked": f"****{last4}",
        "account_binding_fingerprint": fingerprint,
    }


def _ibkr_risk_binding_tuple(binding: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(binding[field])
        for field in (
            "release_manifest_hash",
            "config_hash",
            "policy_binding_id",
            "risk_binding_id",
            "account_key",
            "account_masked",
            "account_binding_fingerprint",
        )
    )


def _ibkr_risk_table_columns(
    connection: sqlite3.Connection, table: str
) -> tuple[str, ...]:
    if not table.replace("_", "").isalnum():
        raise InstallError("IBKR risk ledger table name is unsafe")
    return tuple(
        str(row[1])
        for row in connection.execute(
            f'PRAGMA table_info("{table}")'
        ).fetchall()
    )


def _validate_ibkr_risk_ledger_file(path: Path) -> os.stat_result:
    if path.is_symlink() or not path.is_file():
        raise InstallError("IBKR risk ledger must be a non-symlink regular file")
    metadata = path.stat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or (hasattr(os, "geteuid") and metadata.st_uid != os.geteuid())
    ):
        raise InstallError("IBKR risk ledger file ownership or mode is unsafe")
    for suffix in ("-journal", "-wal", "-shm"):
        sidecar = Path(str(path) + suffix)
        if sidecar.is_symlink():
            raise InstallError("IBKR risk ledger sidecar is unsafe")
        if sidecar.exists():
            sidecar_metadata = sidecar.stat()
            if (
                not stat.S_ISREG(sidecar_metadata.st_mode)
                or sidecar_metadata.st_nlink != 1
                or stat.S_IMODE(sidecar_metadata.st_mode) & 0o077
                or (
                    hasattr(os, "geteuid")
                    and sidecar_metadata.st_uid != os.geteuid()
                )
            ):
                raise InstallError("IBKR risk ledger sidecar is unsafe")
    return metadata


def _read_ibkr_risk_ledger(path: Path) -> dict[str, Any]:
    """Validate a closed ledger, including frozen v4 day-start/flow history."""

    before = _validate_ibkr_risk_ledger_file(path)
    connection = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA synchronous=FULL")
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint is not None and int(checkpoint[0]):
            raise InstallError("IBKR risk ledger WAL checkpoint is busy")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise InstallError("IBKR risk ledger failed SQLite quick_check")
        if (
            int(connection.execute("PRAGMA application_id").fetchone()[0])
            != _IBKR_RISK_LEDGER_APPLICATION_ID
        ):
            raise InstallError("IBKR risk ledger application identity is invalid")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version not in {1, 2, 3, _IBKR_RISK_LEDGER_SCHEMA_VERSION}:
            raise InstallError("IBKR risk ledger schema is unsupported")
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        expected_tables = {"binding", "daily_high_water"}
        if version >= 2:
            expected_tables.add("carry_forward")
        if version >= 4:
            expected_tables.add("daily_starting_equity")
        if tables != expected_tables:
            raise InstallError("IBKR risk ledger tables differ from its schema")
        if (
            _ibkr_risk_table_columns(connection, "binding")
            != (
                _IBKR_RISK_BINDING_COLUMNS
                if version >= 3
                else _IBKR_RISK_LEGACY_BINDING_COLUMNS
            )
            or _ibkr_risk_table_columns(connection, "daily_high_water")
            != _IBKR_RISK_DAILY_COLUMNS
            or (
                version >= 4 and _ibkr_risk_table_columns(connection, "daily_starting_equity") != _IBKR_RISK_STARTING_COLUMNS
            )
            or (
                version >= 2
                and _ibkr_risk_table_columns(connection, "carry_forward")
                != _IBKR_RISK_CARRY_COLUMNS
            )
        ):
            raise InstallError("IBKR risk ledger columns differ from its schema")
        binding_rows = connection.execute("SELECT * FROM binding").fetchall()
        if len(binding_rows) != 1 or binding_rows[0]["singleton"] != 1:
            raise InstallError("IBKR risk ledger binding is missing or ambiguous")
        binding = dict(binding_rows[0])
        for field in (
            "release_manifest_hash",
            "config_hash",
            "policy_binding_id",
            "risk_binding_id",
            "account_binding_fingerprint",
        ):
            _ibkr_risk_hash(binding.get(field), field)
        if version >= 3:
            _ibkr_risk_hash(binding.get("lineage_hash"), "lineage hash")
        if (
            not re.fullmatch(r"ibkr-live-ending-[0-9]{4}", str(binding["account_key"]))
            or not re.fullmatch(r"(?:\*{4}|•{4})[0-9]{4}", str(binding["account_masked"]))
            or str(binding["account_key"])[-4:]
            != str(binding["account_masked"])[-4:]
        ):
            raise InstallError("IBKR risk ledger account binding is invalid")
        daily = connection.execute(
            "SELECT * FROM daily_high_water ORDER BY trading_date"
        ).fetchall()
        daily_dates: list[date] = []
        daily_peaks: list[Decimal] = []
        for row in daily:
            daily_dates.append(_ibkr_risk_date(row["trading_date"], "trading date"))
            _ibkr_risk_hash(row["baseline_receipt_hash"], "baseline receipt")
            _ibkr_risk_hash(
                row["baseline_provider_receipt_sha256"], "provider receipt"
            )
            baseline_peak = _ibkr_risk_decimal(
                row["baseline_prior_high_water_equity"], "baseline high water"
            )
            peak = _ibkr_risk_decimal(row["peak_equity"], "daily peak")
            last = _ibkr_risk_decimal(
                row["last_net_liquidation"], "last net liquidation", positive=version < 4
            )
            _ibkr_risk_time(row["last_observed_at"], "last observed time")
            if peak < max(baseline_peak, last):
                raise InstallError("IBKR risk ledger daily peak regressed")
            daily_peaks.append(peak)
        if (
            daily_dates != sorted(set(daily_dates))
            or daily_peaks != sorted(daily_peaks)
        ):
            raise InstallError("IBKR risk ledger trading dates are invalid")
        latest_raw = binding["latest_trading_date"]
        highest_raw = binding["highest_equity"]
        if bool(daily) != (latest_raw is not None and highest_raw is not None):
            raise InstallError("IBKR risk ledger binding summary is inconsistent")
        current_latest = None
        current_peak = None
        if daily:
            current_latest = _ibkr_risk_date(latest_raw, "latest trading date")
            current_peak = _ibkr_risk_decimal(highest_raw, "highest equity")
            if current_latest != daily_dates[-1] or current_peak != max(daily_peaks):
                raise InstallError("IBKR risk ledger binding summary is inconsistent")

        carried_latest = None
        carried_peak = None
        if version >= 2:
            carry_rows = connection.execute("SELECT * FROM carry_forward").fetchall()
            if len(carry_rows) > 1 or (
                carry_rows and carry_rows[0]["singleton"] != 1
            ):
                raise InstallError("IBKR risk ledger carry-forward is ambiguous")
            if carry_rows:
                carry = carry_rows[0]
                for field in (
                    "source_release_manifest_hash",
                    "source_config_hash",
                    "source_policy_binding_id",
                    "source_risk_binding_id",
                    "source_account_binding_fingerprint",
                    "source_ledger_sha256",
                ):
                    _ibkr_risk_hash(carry[field], field)
                if (
                    carry["source_account_key"] != binding["account_key"]
                    or carry["source_account_masked"] != binding["account_masked"]
                    or carry["source_account_binding_fingerprint"]
                    != binding["account_binding_fingerprint"]
                    or not re.fullmatch(
                        r"state/ibkr-risk-high-water-archive/"
                        r"[0-9a-f]{64}-[0-9a-f]{64}\.sqlite3",
                        str(carry["archive_relative_path"]),
                    )
                ):
                    raise InstallError("IBKR risk ledger carry-forward binding is invalid")
                carried_latest = _ibkr_risk_date(
                    carry["source_latest_trading_date"], "carry trading date"
                )
                carried_peak = _ibkr_risk_decimal(
                    carry["source_highest_equity"], "carry high water"
                )
                if daily_dates and (
                    daily_dates[0] < carried_latest
                    or any(peak < carried_peak for peak in daily_peaks)
                ):
                    raise InstallError("IBKR risk ledger carry-forward floor regressed")
                migrated = _ibkr_risk_time(carry["migrated_at"], "migration time")
                if carry["migrated_at"] != migrated.isoformat():
                    raise InstallError("IBKR risk ledger migration time is not canonical")
        latest_candidates = [
            item for item in (current_latest, carried_latest) if item is not None
        ]
        peak_candidates = [
            item for item in (current_peak, carried_peak) if item is not None
        ]
        starting_rows = []
        if version >= 4:
            starting_rows = [dict(row) for row in connection.execute("SELECT * FROM daily_starting_equity ORDER BY trading_date").fetchall()]
            for row in starting_rows:
                day = _ibkr_risk_date(row["trading_date"], "day-start trading date")
                _ibkr_risk_decimal(row["starting_equity"], "starting equity")
                start = _ibkr_risk_time(row["starting_equity_as_of"], "starting equity as of")
                flow_as_of = _ibkr_risk_time(row["latest_external_cash_flow_as_of"], "cash-flow as of")
                _ibkr_risk_decimal(row["latest_external_cash_flow"], "cash flow", positive=False)
                for key in ("starting_equity_provider_receipt_sha256", "latest_external_cash_flow_receipt_sha256"):
                    _ibkr_risk_hash(row[key], key)
                if (
                    start != datetime.combine(day, datetime.min.time(), ZoneInfo("America/New_York"))
                    or flow_as_of.astimezone(ZoneInfo("America/New_York")).date() != day
                    or row["starting_equity_as_of"] != start.isoformat()
                    or row["latest_external_cash_flow_as_of"] != flow_as_of.isoformat()
                    or not latest_candidates or day > max(latest_candidates)
                ):
                    raise InstallError("IBKR frozen daily equity history is inconsistent")
        result = {
            "schema_version": version,
            "binding": binding,
            "latest_trading_date": max(latest_candidates) if latest_candidates else None,
            "highest_equity": max(peak_candidates) if peak_candidates else None,
            "daily_starting_equity": starting_rows,
        }
    except sqlite3.Error as exc:
        raise InstallError(
            _external_failure_code("IBKR_RISK_LEDGER_VALIDATION_FAILED", exc)
        ) from exc
    finally:
        connection.close()
    after = _validate_ibkr_risk_ledger_file(path)
    if (
        after.st_dev,
        after.st_ino,
        after.st_nlink,
    ) != (before.st_dev, before.st_ino, 1):
        raise InstallError("IBKR risk ledger file changed during validation")
    for suffix in ("-journal", "-wal", "-shm"):
        if Path(str(path) + suffix).exists():
            raise InstallError("IBKR risk ledger did not checkpoint cleanly")
    return result


def _create_ibkr_risk_ledger(
    path: Path,
    *,
    target: dict[str, str],
    source: dict[str, Any] | None = None,
    source_sha256: str | None = None,
    archive_relative: str | None = None,
    migrated_at: datetime | None = None,
) -> str:
    """Create v4; carry authenticated fixed day-start and flow watermarks."""

    lineage_hash = os.urandom(32).hex()
    _ibkr_risk_hash(lineage_hash, "lineage hash")
    connection = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    try:
        path.chmod(0o600)
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "CREATE TABLE binding ("
            "singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
            "release_manifest_hash TEXT NOT NULL,config_hash TEXT NOT NULL,"
            "policy_binding_id TEXT NOT NULL,risk_binding_id TEXT NOT NULL,"
            "account_key TEXT NOT NULL,account_masked TEXT NOT NULL,"
            "account_binding_fingerprint TEXT NOT NULL,"
            "lineage_hash TEXT NOT NULL,"
            "latest_trading_date TEXT,highest_equity TEXT)"
        )
        connection.execute(
            "CREATE TABLE daily_high_water ("
            "trading_date TEXT PRIMARY KEY,baseline_receipt_hash TEXT NOT NULL,"
            "baseline_provider_receipt_sha256 TEXT NOT NULL,"
            "baseline_prior_high_water_equity TEXT NOT NULL,"
            "peak_equity TEXT NOT NULL,last_net_liquidation TEXT NOT NULL,"
            "last_observed_at TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE daily_starting_equity (trading_date TEXT PRIMARY KEY,"
            "starting_equity TEXT NOT NULL,starting_equity_as_of TEXT NOT NULL,"
            "starting_equity_provider_receipt_sha256 TEXT NOT NULL,"
            "latest_external_cash_flow TEXT NOT NULL,latest_external_cash_flow_as_of TEXT NOT NULL,"
            "latest_external_cash_flow_receipt_sha256 TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE carry_forward ("
            "singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
            "source_release_manifest_hash TEXT NOT NULL,"
            "source_config_hash TEXT NOT NULL,"
            "source_policy_binding_id TEXT NOT NULL,"
            "source_risk_binding_id TEXT NOT NULL,"
            "source_account_key TEXT NOT NULL,"
            "source_account_masked TEXT NOT NULL,"
            "source_account_binding_fingerprint TEXT NOT NULL,"
            "source_latest_trading_date TEXT NOT NULL,"
            "source_highest_equity TEXT NOT NULL,"
            "source_ledger_sha256 TEXT NOT NULL,"
            "archive_relative_path TEXT NOT NULL,"
            "migrated_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO binding VALUES (1,?,?,?,?,?,?,?,?,NULL,NULL)",
            (*_ibkr_risk_binding_tuple(target), lineage_hash),
        )
        if source is not None:
            source_peak = source["highest_equity"]
            source_date = source["latest_trading_date"]
            if (source_peak is None) != (source_date is None):
                raise InstallError("IBKR risk ledger source summary is inconsistent")
        else:
            source_peak = None
            source_date = None
        if source_peak is not None:
            if (
                source_sha256 is None
                or archive_relative is None
                or migrated_at is None
            ):
                raise InstallError("IBKR risk ledger carry-forward is incomplete")
            source_binding = source["binding"]
            connection.execute(
                "INSERT INTO carry_forward VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    source_binding["release_manifest_hash"],
                    source_binding["config_hash"],
                    source_binding["policy_binding_id"],
                    source_binding["risk_binding_id"],
                    source_binding["account_key"],
                    source_binding["account_masked"],
                    source_binding["account_binding_fingerprint"],
                    source_date.isoformat(),
                    _ibkr_risk_decimal_text(source_peak),
                    source_sha256,
                    archive_relative,
                    migrated_at.astimezone(timezone.utc).isoformat(),
                ),
            )
        if source is not None:
            # These are retained historical facts, never fresh flow authority.
            # The runtime still requires a new release-bound receipt matching
            # the exact current broker valuation before readiness is true.
            connection.executemany(
                "INSERT INTO daily_starting_equity VALUES (?,?,?,?,?,?,?)",
                [tuple(row[key] for key in _IBKR_RISK_STARTING_COLUMNS)
                 for row in source.get("daily_starting_equity", ())],
            )
        connection.execute(
            f"PRAGMA application_id={_IBKR_RISK_LEDGER_APPLICATION_ID}"
        )
        connection.execute(
            f"PRAGMA user_version={_IBKR_RISK_LEDGER_SCHEMA_VERSION}"
        )
        connection.execute("COMMIT")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise InstallError("new IBKR risk ledger failed SQLite quick_check")
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()
    with path.open("rb") as handle:
        os.fsync(handle.fileno())
    return lineage_hash


def _archive_ibkr_risk_ledger(source: Path, archive: Path, digest: str) -> None:
    if archive.exists():
        _validate_ibkr_risk_ledger_file(archive)
        if sha256_file(archive) != digest:
            raise InstallError("IBKR risk ledger archive digest conflicts")
        return
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{archive.name}.", dir=archive.parent
    )
    temporary = Path(temporary_name)
    try:
        os.close(descriptor)
        shutil.copyfile(source, temporary)
        temporary.chmod(0o600)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        if sha256_file(temporary) != digest:
            raise InstallError("IBKR risk ledger archive copy changed")
        os.replace(temporary, archive)
    finally:
        if temporary.exists():
            temporary.unlink()


def _migrate_ibkr_risk_high_water_ledger(
    install_root: Path,
    *,
    manifest: dict[str, Any],
    config: dict[str, Any],
    payloads: dict[str, bytes],
    migrated_at: datetime,
) -> dict[str, Any]:
    """Archive and conservatively carry a release-bound high-water floor.

    The caller holds both fixed deployment/account writer interlocks and has
    already proven an unarmed PAUSED state with no live writer or notification
    lease.  This helper never starts a process, reads a key, consumes an
    activation, or contacts IBKR.
    """

    target = _ibkr_risk_target(
        manifest=manifest,
        config=config,
        payloads=payloads,
    )
    if target is None:
        return {
            "required": False,
            "performed": False,
            "reason": "RISK_LEDGER_NOT_CONFIGURED",
        }
    # Never derive durable risk state placement from a release-controlled
    # filename.  Policy validation independently requires this same path.
    ledger = install_root / _IBKR_RISK_LEDGER_RELATIVE
    if ledger.is_symlink():
        raise InstallError("IBKR risk ledger must be a non-symlink regular file")
    if not ledger.exists():
        for suffix in ("-journal", "-wal", "-shm"):
            if os.path.lexists(Path(str(ledger) + suffix)):
                raise InstallError("IBKR risk ledger orphaned sidecar is unsafe")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{ledger.name}.bootstrap-", dir=ledger.parent
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            temporary.unlink()
            lineage_hash = _create_ibkr_risk_ledger(
                temporary,
                target=target,
            )
            staged = _read_ibkr_risk_ledger(temporary)
            if (
                staged["schema_version"] != _IBKR_RISK_LEDGER_SCHEMA_VERSION
                or _ibkr_risk_binding_tuple(staged["binding"])
                != _ibkr_risk_binding_tuple(target)
                or staged["binding"].get("lineage_hash") != lineage_hash
                or staged["highest_equity"] is not None
                or staged["latest_trading_date"] is not None
            ):
                raise InstallError("new IBKR risk ledger binding is invalid")
            try:
                os.link(temporary, ledger)
            except FileExistsError as exc:
                raise InstallError(
                    "IBKR risk ledger appeared during bootstrap"
                ) from exc
            temporary.unlink()
            directory_descriptor = os.open(ledger.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            if temporary.exists():
                temporary.unlink()
        return {
            "required": True,
            "performed": True,
            "reason": "PAUSED_RISK_LEDGER_BOOTSTRAPPED",
            "target_schema_version": _IBKR_RISK_LEDGER_SCHEMA_VERSION,
            "target_release_manifest_hash": target["release_manifest_hash"],
            "latest_trading_date": None,
            "highest_equity": None,
            "high_water_never_lowered": True,
        }
    source = _read_ibkr_risk_ledger(ledger)
    source_binding = source["binding"]
    target_binding = _ibkr_risk_binding_tuple(target)
    if (
        source["schema_version"] == _IBKR_RISK_LEDGER_SCHEMA_VERSION
        and _ibkr_risk_binding_tuple(source_binding) == target_binding
    ):
        return {
            "required": False,
            "performed": False,
            "reason": "RISK_LEDGER_ALREADY_BOUND",
            "highest_equity": (
                None
                if source["highest_equity"] is None
                else format(source["highest_equity"], "f")
            ),
        }
    if (
        source_binding["account_key"] != target["account_key"]
        or source_binding["account_masked"] != target["account_masked"]
        or source_binding["account_binding_fingerprint"]
        != target["account_binding_fingerprint"]
    ):
        raise InstallError("IBKR risk ledger migration cannot change account binding")

    archive_root = _ensure_private_directory(
        install_root, _IBKR_RISK_LEDGER_ARCHIVE_RELATIVE.as_posix()
    )
    source_digest = sha256_file(ledger)
    archive_name = (
        f"{source_binding['release_manifest_hash']}-{source_digest}.sqlite3"
    )
    archive = archive_root / archive_name
    archive_relative = archive.relative_to(install_root).as_posix()
    _archive_ibkr_risk_ledger(ledger, archive, source_digest)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{ledger.name}.migration-", dir=ledger.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.unlink()
        lineage_hash = _create_ibkr_risk_ledger(
            temporary,
            target=target,
            source=source,
            source_sha256=source_digest,
            archive_relative=archive_relative,
            migrated_at=migrated_at,
        )
        staged = _read_ibkr_risk_ledger(temporary)
        if (
            staged["schema_version"] != _IBKR_RISK_LEDGER_SCHEMA_VERSION
            or _ibkr_risk_binding_tuple(staged["binding"]) != target_binding
            or staged["binding"].get("lineage_hash") != lineage_hash
            or staged["highest_equity"] != source["highest_equity"]
            or staged["latest_trading_date"] != source["latest_trading_date"]
            or staged["daily_starting_equity"] != source.get("daily_starting_equity", [])
            or (
                source["schema_version"] == _IBKR_RISK_LEDGER_SCHEMA_VERSION
                and source_binding.get("lineage_hash") == lineage_hash
            )
        ):
            raise InstallError("new IBKR risk ledger did not preserve its floor")
        if sha256_file(ledger) != source_digest:
            raise InstallError("IBKR risk ledger changed before release rotation")
        os.replace(temporary, ledger)
        directory_descriptor = os.open(ledger.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {
        "required": True,
        "performed": True,
        "reason": "PAUSED_RELEASE_SCOPED_ARCHIVE_CARRY_FORWARD",
        "source_schema_version": source["schema_version"],
        "target_schema_version": _IBKR_RISK_LEDGER_SCHEMA_VERSION,
        "source_release_manifest_hash": source_binding["release_manifest_hash"],
        "target_release_manifest_hash": target["release_manifest_hash"],
        "source_ledger_sha256": source_digest,
        "archive_relative_path": archive_relative,
        "latest_trading_date": (
            None
            if source["latest_trading_date"] is None
            else source["latest_trading_date"].isoformat()
        ),
        "highest_equity": (
            None
            if source["highest_equity"] is None
            else format(source["highest_equity"], "f")
        ),
        "high_water_never_lowered": True,
    }


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


def _ibkr_profile_requirements(config: dict[str, Any]) -> dict[str, Any] | None:
    """Read the signed, non-secret IBKR install requirements.

    The runtime policy performs the full semantic validation.  The standalone
    installer repeats the security-critical account/root/SDK checks so it
    cannot be tricked into copying a dependency for an unrelated release.
    """

    raw = config.get("local_provider_profile")
    if raw is None:
        return None
    deployment = config.get("deployment")
    account = config.get("account")
    if not all(isinstance(value, dict) for value in (raw, deployment, account)):
        raise InstallError("IBKR local provider profile is incomplete")
    assert isinstance(raw, dict)
    assert isinstance(deployment, dict)
    assert isinstance(account, dict)
    endpoint = raw.get("endpoint")
    sdk = raw.get("sdk")
    if not isinstance(endpoint, dict) or not isinstance(sdk, dict):
        raise InstallError("IBKR endpoint or SDK profile is incomplete")
    expected_fields = {
        "profile": {
            "schema_version", "profile_id", "provider", "environment",
            "endpoint", "read_client_id", "command_client_id",
            "account_binding_source", "persist_full_account_identifier", "sdk",
        },
        "account": {
            "account_key", "masked_identifier", "required_last4", "allowed_type",
            "margin_debit_allowed",
        },
        "deployment": {
            "profile_id", "install_subtree", "coordinator_launchd_label",
            "notification_launchd_label",
        },
        "endpoint": {"host", "port", "loopback_only"},
        "sdk": {
            "distribution", "ibapi_version", "protobuf_version",
            "expected_inventory_sha256",
            "installation_mode",
        },
    }
    observed_fields = {
        "profile": set(raw),
        "account": set(account),
        "deployment": set(deployment),
        "endpoint": set(endpoint),
        "sdk": set(sdk),
    }
    if observed_fields != expected_fields:
        raise InstallError("IBKR local provider profile has unapproved fields")
    requirements = {
        "profile_id": str(raw.get("profile_id", "")),
        "account_key": _configured_account_key(config),
        "install_subtree": str(deployment.get("install_subtree", "")),
        "coordinator_launchd_label": str(
            deployment.get("coordinator_launchd_label", "")
        ),
        "notification_launchd_label": str(
            deployment.get("notification_launchd_label", "")
        ),
        "ibapi_version": str(sdk.get("ibapi_version", "")),
        "protobuf_version": str(sdk.get("protobuf_version", "")),
        "sdk_inventory_hash": str(sdk.get("expected_inventory_sha256", "")),
        "host": str(endpoint.get("host", "")),
        "port": endpoint.get("port"),
        "read_client_id": raw.get("read_client_id"),
        "command_client_id": raw.get("command_client_id"),
    }
    if (
        raw.get("schema_version") != _IBKR_PROFILE_SCHEMA
        or raw.get("provider") != "interactive_brokers"
        or requirements["profile_id"] != deployment.get("profile_id")
        or requirements["account_key"] != "ibkr-live-ending-3103"
        or account.get("masked_identifier") != "ending-3103"
        or account.get("required_last4") != "3103"
        or account.get("allowed_type") != "no_borrow_margin"
        or account.get("margin_debit_allowed") is not False
        or requirements["install_subtree"]
        != "Application Support/Titan Momentum/full-live-ibkr-ending-3103"
        or requirements["host"] != "127.0.0.1"
        or endpoint.get("loopback_only") is not True
        or isinstance(requirements["port"], bool)
        or not isinstance(requirements["port"], int)
        or not 1 <= requirements["port"] <= 65535
        or isinstance(requirements["read_client_id"], bool)
        or not isinstance(requirements["read_client_id"], int)
        or not 0 <= requirements["read_client_id"] <= 2_147_483_647
        or isinstance(requirements["command_client_id"], bool)
        or not isinstance(requirements["command_client_id"], int)
        or not 1 <= requirements["command_client_id"] < 2_147_483_647
        or requirements["read_client_id"] == requirements["command_client_id"]
        or requirements["read_client_id"] == requirements["command_client_id"] + 1
        or requirements["ibapi_version"] != "10.50.2"
        or requirements["protobuf_version"] != "5.29.5"
        or len(requirements["sdk_inventory_hash"]) != 64
        or any(
            character not in HEX
            for character in requirements["sdk_inventory_hash"]
        )
        or raw.get("environment") != "live"
        or sdk.get("distribution") != "official_tws_python_api"
        or sdk.get("installation_mode") != "installer_attested_snapshot"
        or raw.get("persist_full_account_identifier") is not False
        or raw.get("account_binding_source")
        != "managed_accounts_runtime_last4_match"
    ):
        raise InstallError("IBKR local provider profile is invalid")
    for field in ("coordinator_launchd_label", "notification_launchd_label"):
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{2,127}", requirements[field]):
            raise InstallError("IBKR launchd identity is invalid")
    if requirements["coordinator_launchd_label"] == requirements["notification_launchd_label"]:
        raise InstallError("IBKR launchd identities must be distinct")
    return requirements


def _distribution_version(metadata: Path, expected_name: str) -> str:
    if metadata.is_symlink() or not metadata.is_file():
        raise InstallError("IBKR SDK distribution metadata is missing")
    try:
        lines = metadata.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise InstallError("IBKR SDK distribution metadata is unreadable") from exc
    names = [line[6:].strip() for line in lines if line.startswith("Name: ")]
    versions = [line[9:].strip() for line in lines if line.startswith("Version: ")]
    if names != [expected_name] or len(versions) != 1:
        raise InstallError("IBKR SDK distribution metadata is ambiguous")
    return versions[0]


def _sdk_source_inventory(
    site_packages: Path,
    *,
    ibapi_version: str,
    protobuf_version: str,
) -> list[dict[str, Any]]:
    roots = (
        Path("ibapi"),
        Path(f"ibapi-{ibapi_version}.dist-info"),
        Path("google/protobuf"),
        Path("google/_upb"),
        Path(f"protobuf-{protobuf_version}.dist-info"),
    )
    if _distribution_version(
        site_packages / roots[1] / "METADATA", "ibapi"
    ) != ibapi_version:
        raise InstallError("official IBKR SDK version mismatch")
    if _distribution_version(
        site_packages / roots[4] / "METADATA", "protobuf"
    ) != protobuf_version:
        raise InstallError("IBKR protobuf version mismatch")
    records: list[dict[str, Any]] = []
    total = 0
    for relative_root in roots:
        source_root = site_packages / relative_root
        if source_root.is_symlink() or not source_root.is_dir():
            raise InstallError("IBKR SDK package root is missing or unsafe")
        for directory, directory_names, file_names in os.walk(
            source_root, followlinks=False
        ):
            base = Path(directory)
            for name in tuple(directory_names):
                candidate = base / name
                if candidate.is_symlink():
                    raise InstallError("IBKR SDK package contains a symlink directory")
                if name == "__pycache__":
                    directory_names.remove(name)
            for name in sorted(file_names):
                if name.endswith(".pyc"):
                    continue
                candidate = base / name
                if candidate.is_symlink() or not candidate.is_file():
                    raise InstallError("IBKR SDK package contains a non-regular file")
                size = candidate.stat().st_size
                if size > _MAX_SDK_FILE_BYTES:
                    raise InstallError("IBKR SDK package file exceeds size limit")
                total += size
                if total > _MAX_SDK_TOTAL_BYTES:
                    raise InstallError("IBKR SDK package exceeds size limit")
                relative = candidate.relative_to(site_packages).as_posix()
                records.append(
                    {
                        "path": relative,
                        "size": size,
                        "sha256": sha256_file(candidate),
                    }
                )
    records.sort(key=lambda record: str(record["path"]))
    if not records or len({str(record["path"]) for record in records}) != len(records):
        raise InstallError("IBKR SDK package inventory is empty or ambiguous")
    return records


def _verify_sdk_snapshot(
    import_root: Path,
    records: list[dict[str, Any]],
) -> None:
    if import_root.is_symlink() or not import_root.is_dir():
        raise InstallError("IBKR SDK snapshot import root is unsafe")
    expected = {str(record["path"]) for record in records}
    observed: set[str] = set()
    for candidate in import_root.rglob("*"):
        if candidate.is_symlink():
            raise InstallError("IBKR SDK snapshot contains a symlink")
        if candidate.is_file():
            observed.add(candidate.relative_to(import_root).as_posix())
        elif not candidate.is_dir():
            raise InstallError("IBKR SDK snapshot contains a special file")
    if observed != expected:
        raise InstallError("IBKR SDK snapshot inventory differs")
    for record in records:
        candidate = import_root / str(record["path"])
        if (
            candidate.stat().st_size != int(record["size"])
            or sha256_file(candidate) != record["sha256"]
            or stat.S_IMODE(candidate.stat().st_mode) & 0o022
        ):
            raise InstallError("IBKR SDK snapshot failed file attestation")


def _install_ibkr_sdk_snapshot(
    install_root: Path,
    config: dict[str, Any],
    sdk_venv: Path | None,
) -> tuple[bytes, dict[str, Any]] | None:
    requirements = _ibkr_profile_requirements(config)
    if requirements is None:
        if sdk_venv is not None:
            raise InstallError("--ibkr-sdk-venv is valid only for an IBKR profile")
        return None
    if sdk_venv is None:
        raise InstallError("IBKR profile requires --ibkr-sdk-venv")
    venv = sdk_venv.expanduser().resolve(strict=True)
    if venv.is_symlink() or not venv.is_dir():
        raise InstallError("IBKR SDK venv must be a real directory")
    site_packages = (
        venv
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    if site_packages.is_symlink() or not site_packages.is_dir():
        raise InstallError("IBKR SDK venv does not match the installer Python minor version")
    records = _sdk_source_inventory(
        site_packages,
        ibapi_version=str(requirements["ibapi_version"]),
        protobuf_version=str(requirements["protobuf_version"]),
    )
    inventory_hash = sha256_bytes(canonical_json(records))
    if inventory_hash != requirements["sdk_inventory_hash"]:
        raise InstallError(
            "IBKR SDK inventory does not match the release-pinned dependency"
        )
    dependency_relative = Path("dependencies/ibkr-sdk") / inventory_hash
    destination = install_root / dependency_relative
    import_root = destination / "site-packages"
    if destination.exists():
        _verify_sdk_snapshot(import_root, records)
    else:
        dependency_parent = install_root / "dependencies/ibkr-sdk"
        dependency_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        stage = Path(
            tempfile.mkdtemp(prefix=f".sdk-{inventory_hash[:12]}-", dir=dependency_parent)
        )
        try:
            staged_import = stage / "site-packages"
            for record in records:
                relative = Path(str(record["path"]))
                source = site_packages / relative
                target = staged_import / relative
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copyfile(source, target)
                target.chmod(0o444)
            for directory in sorted(
                (candidate for candidate in stage.rglob("*") if candidate.is_dir()),
                key=lambda candidate: len(candidate.parts),
                reverse=True,
            ):
                directory.chmod(0o555)
            stage.chmod(0o555)
            _verify_sdk_snapshot(staged_import, records)
            os.replace(stage, destination)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
        _verify_sdk_snapshot(import_root, records)
    receipt = {
        "schema_version": _IBKR_SDK_RECEIPT_SCHEMA,
        "profile_id": requirements["profile_id"],
        "account_key": requirements["account_key"],
        "python_implementation": sys.implementation.name,
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        "import_root": (dependency_relative / "site-packages").as_posix(),
        "ibapi_version": requirements["ibapi_version"],
        "protobuf_version": requirements["protobuf_version"],
        "files": records,
        "inventory_hash": inventory_hash,
    }
    receipt_bytes = canonical_json(receipt) + b"\n"
    return receipt_bytes, {
        "kind": "ibkr_release_pinned_sdk_snapshot",
        "profile_id": requirements["profile_id"],
        "receipt_path": str(_IBKR_SDK_RECEIPT_RELATIVE),
        "receipt_sha256": sha256_bytes(receipt_bytes),
        "inventory_hash": inventory_hash,
        "ibapi_version": requirements["ibapi_version"],
        "protobuf_version": requirements["protobuf_version"],
        "source_venv_persisted": False,
    }


@contextmanager
def _deployment_interlock(root: Path, account_key: str):
    """Exclude installers, attended coordinators, and broker writers.

    The coordinator lock is always acquired before the broker-writer lock,
    matching the installed CLI's maintenance interlock.  Taking both even for
    an unattended release keeps upgrades safe across an authority-mode change.
    """

    coordinator_key = f"{_ATTENDED_COORDINATOR_LOCK_PREFIX}{account_key}"
    coordinator_fingerprint = hashlib.sha256(
        coordinator_key.encode("utf-8")
    ).hexdigest()[:24]
    writer_fingerprint = hashlib.sha256(account_key.encode("utf-8")).hexdigest()[
        :24
    ]
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
        global_lock_directory
        / f"account-{coordinator_fingerprint}.writer.lock",
        global_lock_directory / f"account-{writer_fingerprint}.writer.lock",
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
    expected_template_label = (
        NOTIFICATION_LAUNCHD_LABEL
        if command == "notification-worker"
        else LAUNCHD_LABEL
    )
    if not isinstance(value, dict) or value.get("Label") != expected_template_label:
        raise InstallError("launchd template identity is invalid")
    value = dict(value)
    value["Label"] = label
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
    external_dependency: tuple[bytes, dict[str, Any]] | None,
    installer_attestation: dict[str, Any],
    source_provenance: dict[str, Any],
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
        ibkr_profile = _ibkr_profile_requirements(config)
        coordinator_label = (
            str(ibkr_profile["coordinator_launchd_label"])
            if ibkr_profile is not None
            else LAUNCHD_LABEL
        )
        notification_label = (
            str(ibkr_profile["notification_launchd_label"])
            if ibkr_profile is not None
            else NOTIFICATION_LAUNCHD_LABEL
        )
        plist_bytes = _render_plist(
            install_root,
            stage,
            interpreter,
            label=coordinator_label,
        )
        notification_plist_bytes = _render_plist(
            install_root,
            stage,
            interpreter,
            template_relative=(
                "deployment/com.harpcity.trader-brain-full-live-notifications.plist.in"
            ),
            label=notification_label,
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
            "source_provenance": source_provenance,
            "installer_schema": installer_attestation["schema_version"],
            "installer_sha256": installer_attestation["sha256"],
            "installer_source_commit": installer_attestation["source_commit"],
            "installer_executable_path": installer_attestation["source_path"],
            "installer_python_executable": installer_attestation[
                "python_executable"
            ],
            "installer_python_implementation": installer_attestation[
                "python_implementation"
            ],
            "installer_python_version": installer_attestation[
                "python_version"
            ],
            "runtime_python_executable": str(interpreter),
            "install_root": str(install_root),
            "config_path": str(manifest["config_path"]),
            "deployment_profile_id": (
                str(ibkr_profile["profile_id"])
                if ibkr_profile is not None
                else "legacy-robinhood-full-live"
            ),
            "current_release": str(destination),
            "launchd": {
                "label": coordinator_label,
                "staged_plist": str(
                    install_root / "launchd" / f"{coordinator_label}.plist"
                ),
                "disabled": True,
                "run_at_load": False,
                "installer_called_launchctl": False,
                "actual_loaded_state": "NOT_QUERIED",
                "process_role": "trading_coordinator_enqueue_only",
                "notification_worker": {
                    "label": notification_label,
                    "staged_plist": str(
                        install_root
                        / "launchd"
                        / f"{notification_label}.plist"
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
            "risk_high_water_migration": None,
            "external_dependencies": (
                external_dependency[1] if external_dependency is not None else None
            ),
            "sdk_receipt_sha256": (
                external_dependency[1]["receipt_sha256"]
                if external_dependency is not None
                else None
            ),
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
        install_record["risk_high_water_migration"] = (
            _migrate_ibkr_risk_high_water_ledger(
                install_root,
                manifest=manifest,
                config=config,
                payloads=payloads,
                migrated_at=datetime.fromisoformat(installed_at),
            )
        )

        os.replace(temporary_link, current)

        # These writes are validated and prepared before the pointer commit. A
        # crash can make CLI metadata inconsistent, but every CLI command then
        # fails closed on manifest/current binding instead of running mixed code.
        _atomic_write(install_root / "release-manifest.json", manifest_bytes, 0o600)
        plist_path = install_root / "launchd" / f"{coordinator_label}.plist"
        _atomic_write(plist_path, plist_bytes, 0o600)
        notification_plist_path = (
            install_root / "launchd" / f"{notification_label}.plist"
        )
        _atomic_write(notification_plist_path, notification_plist_bytes, 0o600)
        if external_dependency is not None:
            _atomic_write(
                install_root / _IBKR_SDK_RECEIPT_RELATIVE,
                external_dependency[0],
                0o600,
            )
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
    root: Path | None,
    *,
    trusted_source_root: Path,
    expected_source_revision: str,
    expected_archive_sha256: str | None = None,
    python_executable: Path | None = None,
    ibkr_sdk_venv: Path | None = None,
) -> dict[str, Any]:
    archive = archive.resolve(strict=True)
    expected_hash = _expected_archive_hash(archive, expected_archive_sha256)
    archive_bytes, actual_hash = _capture_verified_archive(archive, expected_hash)
    manifest, manifest_bytes, payloads = _read_archive(archive_bytes)
    _verify_manifest(manifest, payloads)
    source_provenance = _verify_trusted_source_provenance(
        manifest,
        payloads,
        trusted_source_root=trusted_source_root,
        expected_source_revision=expected_source_revision,
    )
    interpreter = _validate_python_executable(python_executable)
    installer_attestation = _verify_running_installer(manifest, payloads)

    try:
        config_path = str(manifest["config_path"])
        config = json.loads(payloads[config_path])
        if not isinstance(config, dict):
            raise TypeError("config is not an object")
        account_key = _configured_account_key(config)
        if sha256_bytes(canonical_json(config)) != manifest["config_hash"]:
            raise InstallError("selected config does not match manifest config hash")
        deployment = config.get("deployment")
        configured_subtree = (
            str(deployment.get("install_subtree", ""))
            if isinstance(deployment, dict)
            else "Application Support/Titan Momentum/full-live"
        )
        if configured_subtree != manifest["install_subtree"]:
            raise InstallError("selected config and manifest install roots differ")
        _ibkr_profile_requirements(config)
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise InstallError("release account binding cannot be read") from exc
    selected_root = root or (Path.home() / str(manifest["install_subtree"]))
    install_root = _assert_install_root(selected_root, str(manifest["install_subtree"]))

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
        external_dependency = _install_ibkr_sdk_snapshot(
            install_root,
            config,
            ibkr_sdk_venv,
        )
        return _commit_install(
            install_root=install_root,
            manifest=manifest,
            manifest_bytes=manifest_bytes,
            payloads=payloads,
            archive_hash=actual_hash,
            interpreter=interpreter,
            config=config,
            external_dependency=external_dependency,
            installer_attestation=installer_attestation,
            source_provenance=source_provenance,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument(
        "--root",
        type=Path,
        help="exact signed install subtree (defaults below the current home directory)",
    )
    parser.add_argument("--expected-archive-sha256")
    parser.add_argument(
        "--trusted-source-root",
        type=Path,
        required=True,
        help="explicit trusted Git repository containing the release commit",
    )
    parser.add_argument(
        "--expected-source-revision",
        required=True,
        help="exact expected 40-character Git source commit",
    )
    parser.add_argument(
        "--python-executable",
        type=Path,
        help="Python >=3.11 interpreter recorded in the staged launchd plist",
    )
    parser.add_argument(
        "--ibkr-sdk-venv",
        type=Path,
        help="existing authorized venv containing official ibapi 10.50.2",
    )
    arguments = parser.parse_args(argv)
    try:
        result = install(
            arguments.archive,
            arguments.root,
            trusted_source_root=arguments.trusted_source_root,
            expected_source_revision=arguments.expected_source_revision,
            expected_archive_sha256=arguments.expected_archive_sha256,
            python_executable=arguments.python_executable,
            ibkr_sdk_venv=arguments.ibkr_sdk_venv,
        )
    except (InstallError, FileNotFoundError, OSError) as exc:
        print(_external_failure_code("INSTALL_BLOCKED", exc), file=sys.stderr)
        return 78
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
