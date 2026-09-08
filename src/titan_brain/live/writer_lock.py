"""Account-scoped operating-system lock for the sole broker writer.

The lock file is stable and is never unlinked during normal release, avoiding
the inode-replacement race common to delete-on-unlock implementations.
"""

from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import socket
import threading
from typing import Any
from uuid import uuid4


class WriterLockError(RuntimeError):
    """Base account-writer lock error."""


class WriterLockBusy(WriterLockError):
    """Raised when another execution owner holds the account lock."""


_PROCESS_GUARD = threading.RLock()
_PROCESS_HELD_PATHS: set[Path] = set()
_USER_LOCK_RELATIVE_PATH = Path(
    "Library/Application Support/Titan Momentum/account-writer-locks"
)


def user_account_writer_lock_directory() -> Path:
    """Return the one machine-user lock directory used by every release.

    The location deliberately does not depend on an install root, release ID,
    credential, or runtime configuration.  Production callers have no CLI or
    environment override for this path; tests replace this function in-process
    so they never touch a real user's lock directory.
    """

    try:
        home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(strict=True)
    except (KeyError, OSError) as exc:
        raise WriterLockError(
            "cannot resolve the operating-system user's writer-lock home"
        ) from exc
    if home == home.parent:
        raise WriterLockError("cannot establish a private user home for writer lock")
    return home / _USER_LOCK_RELATIVE_PATH


def account_writer_fingerprint(
    account_key: str,
    *,
    broker_account_binding_fingerprint: str | None = None,
    authorization_binding_id: str | None = None,
) -> str:
    """Return the account-global lock namespace.

    Production binding receipts are validated and persisted as holder
    metadata, but they must never partition the kernel lock.  Authorization
    rotation (or two credentials for the same account) still represents one
    broker writer and therefore has to contend on the same inode as the
    attended path and the installer interlock.
    """

    normalized = str(account_key).strip()
    if not normalized:
        raise ValueError("account_key is required")
    if (broker_account_binding_fingerprint is None) != (
        authorization_binding_id is None
    ):
        raise ValueError(
            "broker account and authorization bindings must be supplied together"
        )
    if broker_account_binding_fingerprint is not None:
        account_binding = str(broker_account_binding_fingerprint)
        authorization_binding = str(authorization_binding_id)
        for value, field in (
            (account_binding, "broker_account_binding_fingerprint"),
            (authorization_binding, "authorization_binding_id"),
        ):
            if len(value) != 64 or any(
                character not in "0123456789abcdef" for character in value
            ):
                raise ValueError(f"{field} must be a nonsecret 256-bit receipt")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class AccountWriterLock:
    """Exclusive advisory lock for one account in a caller-selected directory.

    Production callers must pass :func:`user_account_writer_lock_directory`.
    Accepting a directory here keeps the primitive independently testable; it
    does not create a production path override.
    """

    def __init__(
        self,
        directory: str | Path,
        account_key: str,
        *,
        owner_id: str | None = None,
        broker_account_binding_fingerprint: str | None = None,
        authorization_binding_id: str | None = None,
    ) -> None:
        self.directory = Path(directory).resolve()
        self.broker_account_binding_fingerprint = broker_account_binding_fingerprint
        self.authorization_binding_id = authorization_binding_id
        self.account_fingerprint = account_writer_fingerprint(
            account_key,
            broker_account_binding_fingerprint=broker_account_binding_fingerprint,
            authorization_binding_id=authorization_binding_id,
        )
        self.owner_id = str(owner_id or uuid4())
        if not self.owner_id.strip():
            raise ValueError("owner_id is required")
        self.path = self.directory / f"account-{self.account_fingerprint[:24]}.writer.lock"
        self._descriptor: int | None = None

    @property
    def held(self) -> bool:
        return self._descriptor is not None

    def acquire(self, *, blocking: bool = False) -> "AccountWriterLock":
        if self.held:
            raise WriterLockError("this lock instance is already held")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.directory, 0o700)
        except PermissionError:
            pass

        with _PROCESS_GUARD:
            if self.path in _PROCESS_HELD_PATHS:
                raise WriterLockBusy(f"account writer lock is held: {self.path}")
            flags = os.O_RDWR | os.O_CREAT
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(self.path, flags, 0o600)
            try:
                os.fchmod(descriptor, 0o600)
                operation = fcntl.LOCK_EX
                if not blocking:
                    operation |= fcntl.LOCK_NB
                try:
                    fcntl.flock(descriptor, operation)
                except BlockingIOError as exc:
                    raise WriterLockBusy(f"account writer lock is held: {self.path}") from exc
                metadata = {
                    "schema_version": 1,
                    "account_fingerprint": self.account_fingerprint,
                    "broker_account_binding_fingerprint": self.broker_account_binding_fingerprint,
                    "authorization_binding_id": self.authorization_binding_id,
                    "owner_id": self.owner_id,
                    "pid": os.getpid(),
                    "hostname": socket.gethostname(),
                    "acquired_at": datetime.now(timezone.utc).isoformat(),
                }
                encoded = (
                    json.dumps(metadata, sort_keys=True, separators=(",", ":")) + "\n"
                ).encode("utf-8")
                os.ftruncate(descriptor, 0)
                os.lseek(descriptor, 0, os.SEEK_SET)
                view = memoryview(encoded)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise WriterLockError("failed to persist lock owner metadata")
                    view = view[written:]
                os.fsync(descriptor)
            except BaseException:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)
                raise
            _PROCESS_HELD_PATHS.add(self.path)
            self._descriptor = descriptor
        return self

    def refresh(self) -> None:
        """Fsync the owner record; the kernel lock, not timestamps, is authority."""

        if self._descriptor is None:
            raise WriterLockError("lock is not held")
        os.fsync(self._descriptor)

    def release(self) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            return
        with _PROCESS_GUARD:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
                self._descriptor = None
                _PROCESS_HELD_PATHS.discard(self.path)

    def holder_metadata(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        return value if isinstance(value, dict) else {}

    def __enter__(self) -> "AccountWriterLock":
        return self.acquire()

    def __exit__(self, *_: Any) -> None:
        self.release()


__all__ = [
    "AccountWriterLock",
    "WriterLockBusy",
    "WriterLockError",
    "account_writer_fingerprint",
    "user_account_writer_lock_directory",
]
