"""Create-only operator controls consumed by the sole database writer.

The persistent service owns the account-scoped kernel lock and is the only
process allowed to change live runtime state.  Operator commands therefore
write hash-bound requests to a private inbox instead of opening a second
SQLite writer.  Applying a request and moving it to ``processed`` is
idempotent across a crash between those two operations.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any, Mapping
from uuid import uuid4

from .models import EngineMode
from .state import LiveStateStore, StateConflict, canonical_json


CONTROL_SCHEMA = "titan_full_live_control_2026-09-08_v1"
CONTROL_COMMANDS = frozenset(
    {"PAUSE_NEW_ENTRIES", "MANAGED_CLOSEOUT", "DEACTIVATE_FLAT"}
)
MAX_CONTROL_BYTES = 64 * 1024


class ControlError(RuntimeError):
    """A control request is malformed, stale, or cannot be applied safely."""


@dataclass(frozen=True)
class ControlResult:
    request_id: str
    command: str
    status: str
    changed: bool
    detail: str


def _aware(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ControlError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ControlError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class ControlInbox:
    """Private, release-bound control request spool."""

    def __init__(
        self,
        root: str | Path,
        *,
        account_key: str,
        runtime_id: str,
        release_manifest_hash: str,
        max_snapshot_age: timedelta,
    ) -> None:
        self.root = Path(root).resolve()
        self.account_key = str(account_key)
        self.runtime_id = str(runtime_id)
        self.release_manifest_hash = str(release_manifest_hash)
        self.max_snapshot_age = max_snapshot_age
        if not self.account_key or not self.runtime_id:
            raise ValueError("control account_key and runtime_id are required")
        if len(self.release_manifest_hash) != 64:
            raise ValueError("control release manifest hash must be SHA-256")
        if self.max_snapshot_age <= timedelta(0):
            raise ValueError("control max_snapshot_age must be positive")
        self.inbox = self.root / "inbox"
        self.processed = self.root / "processed"
        self.rejected = self.root / "rejected"
        for directory in (self.root, self.inbox, self.processed, self.rejected):
            if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
                raise ControlError(f"control path is not a real directory: {directory}")
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(directory, 0o700)

    def submit(
        self,
        command: str,
        *,
        reason: str,
        requested_at: datetime,
        activated_at: str,
        arguments: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        command_value = str(command)
        if command_value not in CONTROL_COMMANDS:
            raise ControlError(f"unsupported control command: {command_value}")
        reason_value = str(reason).strip()
        if not reason_value or len(reason_value) > 500:
            raise ControlError("control reason must contain 1-500 characters")
        if requested_at.tzinfo is None:
            raise ControlError("control requested_at must be timezone-aware")
        activated = _aware(activated_at, "activated_at")
        args = dict(arguments or {})
        if command_value == "DEACTIVATE_FLAT":
            if set(args) != {"flatness_snapshot_id"} or not str(
                args["flatness_snapshot_id"]
            ).strip():
                raise ControlError(
                    "DEACTIVATE_FLAT requires exactly flatness_snapshot_id"
                )
        elif args:
            raise ControlError(f"{command_value} does not accept arguments")
        body = {
            "schema_version": CONTROL_SCHEMA,
            "account_key": self.account_key,
            "runtime_id": self.runtime_id,
            "release_manifest_hash": self.release_manifest_hash,
            "activated_at": activated.isoformat(),
            "command": command_value,
            "reason": reason_value,
            "requested_at": requested_at.astimezone(timezone.utc).isoformat(),
            "arguments": args,
            "nonce": str(uuid4()),
        }
        request_id = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        payload = {**body, "request_id": request_id}
        encoded = (canonical_json(payload) + "\n").encode("utf-8")
        if len(encoded) > MAX_CONTROL_BYTES:
            raise ControlError("control request exceeds size limit")
        micros = int(requested_at.astimezone(timezone.utc).timestamp() * 1_000_000)
        path = self.inbox / f"{micros:020d}-{request_id}.json"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise ControlError("failed to persist control request")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_directory(self.inbox)
        return payload

    def drain(self, store: LiveStateStore, *, now: datetime) -> tuple[ControlResult, ...]:
        if now.tzinfo is None:
            raise ControlError("control drain time must be timezone-aware")
        results: list[ControlResult] = []
        for path in sorted(self.inbox.glob("*.json")):
            request_id = path.stem.rsplit("-", 1)[-1]
            command = "UNKNOWN"
            try:
                payload = self._load(path)
                request_id = str(payload["request_id"])
                command = str(payload["command"])
                changed = self._apply(store, payload, now=now)
                destination = self.processed / path.name
                status = "APPLIED"
                detail = "runtime control consumed by sole writer"
            except Exception as exc:
                changed = False
                destination = self.rejected / path.name
                status = "REJECTED"
                detail = f"{type(exc).__name__}: {str(exc)[:500]}"
            os.replace(path, destination)
            _fsync_directory(destination.parent)
            results.append(
                ControlResult(
                    request_id=request_id,
                    command=command,
                    status=status,
                    changed=changed,
                    detail=detail,
                )
            )
        return tuple(results)

    def _load(self, path: Path) -> Mapping[str, Any]:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_CONTROL_BYTES:
            raise ControlError("control request is not a bounded regular file")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ControlError("control request permissions are not private")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, OSError) as exc:
            raise ControlError("control request is not valid JSON") from exc
        expected = {
            "schema_version",
            "account_key",
            "runtime_id",
            "release_manifest_hash",
            "activated_at",
            "command",
            "reason",
            "requested_at",
            "arguments",
            "nonce",
            "request_id",
        }
        if not isinstance(payload, dict) or set(payload) != expected:
            raise ControlError("control request schema differs")
        body = dict(payload)
        observed_id = str(body.pop("request_id"))
        expected_id = hashlib.sha256(
            canonical_json(body).encode("utf-8")
        ).hexdigest()
        if observed_id != expected_id:
            raise ControlError("control request hash differs")
        if payload["schema_version"] != CONTROL_SCHEMA:
            raise ControlError("unsupported control request schema")
        return payload

    def _apply(
        self,
        store: LiveStateStore,
        payload: Mapping[str, Any],
        *,
        now: datetime,
    ) -> bool:
        current = now.astimezone(timezone.utc)
        requested = _aware(str(payload["requested_at"]), "requested_at")
        if requested > current + timedelta(seconds=2):
            raise ControlError("control request is from the future")
        if current - requested > timedelta(minutes=5):
            raise ControlError("control request expired")
        for field, expected in (
            ("account_key", self.account_key),
            ("runtime_id", self.runtime_id),
            ("release_manifest_hash", self.release_manifest_hash),
        ):
            if payload[field] != expected:
                raise ControlError(f"control request {field} binding differs")
        runtime = store.runtime_status()
        if runtime is None or not bool(runtime["authority_enabled"]):
            raise ControlError("control request has no active runtime authority")
        if runtime["account_key"] != self.account_key or runtime["runtime_id"] != self.runtime_id:
            raise ControlError("durable runtime identity differs from control binding")
        if runtime["release_manifest_hash"] != self.release_manifest_hash:
            raise ControlError("durable release binding differs from control binding")
        if runtime["activated_at"] != payload["activated_at"]:
            raise ControlError("control request belongs to another activation generation")
        command = str(payload["command"])
        if command not in CONTROL_COMMANDS:
            raise ControlError("unsupported control command")
        reason = f"CONTROL_REQUEST:{payload['request_id']}:{payload['reason']}"
        mode = EngineMode(str(runtime["mode"]))
        if command == "PAUSE_NEW_ENTRIES":
            if mode in {
                EngineMode.PAUSE_NEW_ENTRIES,
                EngineMode.MANAGED_CLOSEOUT,
                EngineMode.INCIDENT,
            }:
                return False
            return store.set_runtime_mode(
                EngineMode.PAUSE_NEW_ENTRIES.value,
                occurred_at=current,
                reason=reason,
            )
        if command == "MANAGED_CLOSEOUT":
            if mode is EngineMode.MANAGED_CLOSEOUT:
                return False
            return store.set_runtime_mode(
                EngineMode.MANAGED_CLOSEOUT.value,
                occurred_at=current,
                reason=reason,
            )
        arguments = payload["arguments"]
        if not isinstance(arguments, dict) or set(arguments) != {"flatness_snapshot_id"}:
            raise ControlError("deactivation arguments differ")
        store.deactivate_runtime_authority(
            deactivated_at=current,
            reason=reason,
            flatness_snapshot_id=str(arguments["flatness_snapshot_id"]),
            max_snapshot_age=self.max_snapshot_age,
        )
        return True


__all__ = [
    "CONTROL_COMMANDS",
    "CONTROL_SCHEMA",
    "ControlError",
    "ControlInbox",
    "ControlResult",
]
