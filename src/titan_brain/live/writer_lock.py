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
import stat
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
_LOCK_FILE_MODE = 0o600
_LOCK_DIRECTORY_MODE = 0o700
_MAX_LOCK_METADATA_BYTES = 16 * 1024


def attended_coordinator_lock_key(account_key: str) -> str:
    """Return the non-broker-writer namespace for one attended read service."""

    normalized = str(account_key).strip()
    if not normalized:
        raise ValueError("account_key is required")
    return f"attended-read-coordinator::{normalized}"


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
        self._descriptor_identity: tuple[int, int] | None = None
        self._acquisition_id: str | None = None
        self._acquired_at: datetime | None = None
        self._writer_lease_generation: int | None = None

    @staticmethod
    def _safe_file(metadata: os.stat_result) -> bool:
        return bool(
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_nlink == 1
            and stat.S_IMODE(metadata.st_mode) == _LOCK_FILE_MODE
            and (
                not hasattr(os, "geteuid")
                or metadata.st_uid == os.geteuid()
            )
        )

    def _descriptor_matches_path(self, descriptor: int) -> bool:
        """Prove that ``path`` still names the exact safely held inode."""

        path_descriptor: int | None = None
        try:
            held = os.fstat(descriptor)
            before = self.path.lstat()
            if not self._safe_file(held) or not self._safe_file(before):
                return False
            held_identity = (held.st_dev, held.st_ino)
            if (
                self._descriptor_identity is not None
                and held_identity != self._descriptor_identity
            ) or (before.st_dev, before.st_ino) != held_identity:
                return False
            flags = os.O_RDONLY
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            path_descriptor = os.open(self.path, flags)
            opened = os.fstat(path_descriptor)
            after = self.path.lstat()
            return bool(
                self._safe_file(opened)
                and self._safe_file(after)
                and (opened.st_dev, opened.st_ino) == held_identity
                and (after.st_dev, after.st_ino) == held_identity
            )
        except OSError:
            return False
        finally:
            if path_descriptor is not None:
                os.close(path_descriptor)

    @staticmethod
    def _read_metadata_descriptor(descriptor: int) -> dict[str, Any]:
        try:
            chunks: list[bytes] = []
            offset = 0
            remaining = _MAX_LOCK_METADATA_BYTES + 1
            while remaining > 0:
                chunk = os.pread(descriptor, min(remaining, 4096), offset)
                if not chunk:
                    break
                chunks.append(chunk)
                offset += len(chunk)
                remaining -= len(chunk)
            encoded = b"".join(chunks)
            if not encoded or len(encoded) > _MAX_LOCK_METADATA_BYTES:
                return {}
            value = json.loads(encoded.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    @property
    def held(self) -> bool:
        descriptor = self._descriptor
        return bool(
            descriptor is not None and self._descriptor_matches_path(descriptor)
        )

    @property
    def acquisition_id(self) -> str | None:
        return self._acquisition_id if self.held else None

    @property
    def acquired_at(self) -> datetime | None:
        return self._acquired_at if self.held else None

    @property
    def writer_lease_generation(self) -> int | None:
        return self._writer_lease_generation if self.held else None

    def bind_writer_lease(self, generation: int) -> None:
        """Join this exact kernel-lock acquisition to one durable lease."""

        if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
            raise WriterLockError("writer lease generation must be positive")
        if not self.held:
            raise WriterLockError("cannot bind a writer lease without the exact held lock")
        if self._writer_lease_generation is not None:
            raise WriterLockError("writer lease generation is already bound")
        self._writer_lease_generation = generation

    def acquire(
        self,
        *,
        blocking: bool = False,
        acquired_at: datetime | None = None,
    ) -> "AccountWriterLock":
        if self._descriptor is not None:
            raise WriterLockError("this lock instance is already held")
        acquired = acquired_at or datetime.now(timezone.utc)
        if not isinstance(acquired, datetime) or acquired.tzinfo is None:
            raise ValueError("acquired_at must be timezone-aware")
        acquired = acquired.astimezone(timezone.utc)
        acquisition_id = str(uuid4())
        self.directory.mkdir(parents=True, exist_ok=True, mode=_LOCK_DIRECTORY_MODE)
        try:
            os.chmod(self.directory, _LOCK_DIRECTORY_MODE)
        except PermissionError:
            pass
        directory_metadata = self.directory.lstat()
        if (
            not stat.S_ISDIR(directory_metadata.st_mode)
            or stat.S_ISLNK(directory_metadata.st_mode)
            or stat.S_IMODE(directory_metadata.st_mode) != _LOCK_DIRECTORY_MODE
            or (
                hasattr(os, "geteuid")
                and directory_metadata.st_uid != os.geteuid()
            )
        ):
            raise WriterLockError("account writer lock directory is unsafe")

        with _PROCESS_GUARD:
            if self.path in _PROCESS_HELD_PATHS:
                raise WriterLockBusy(f"account writer lock is held: {self.path}")
            flags = os.O_RDWR | os.O_CREAT
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(self.path, flags, _LOCK_FILE_MODE)
            try:
                os.fchmod(descriptor, _LOCK_FILE_MODE)
                operation = fcntl.LOCK_EX
                if not blocking:
                    operation |= fcntl.LOCK_NB
                try:
                    fcntl.flock(descriptor, operation)
                except BlockingIOError as exc:
                    raise WriterLockBusy(f"account writer lock is held: {self.path}") from exc
                metadata = {
                    "schema_version": 2,
                    "account_fingerprint": self.account_fingerprint,
                    "broker_account_binding_fingerprint": self.broker_account_binding_fingerprint,
                    "authorization_binding_id": self.authorization_binding_id,
                    "owner_id": self.owner_id,
                    "acquisition_id": acquisition_id,
                    "pid": os.getpid(),
                    "hostname": socket.gethostname(),
                    "acquired_at": acquired.isoformat(),
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
                descriptor_metadata = os.fstat(descriptor)
                self._descriptor_identity = (
                    descriptor_metadata.st_dev,
                    descriptor_metadata.st_ino,
                )
                if not self._descriptor_matches_path(descriptor):
                    raise WriterLockError("account writer lock inode is unsafe")
            except BaseException:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)
                    self._descriptor_identity = None
                raise
            _PROCESS_HELD_PATHS.add(self.path)
            self._descriptor = descriptor
            self._acquisition_id = acquisition_id
            self._acquired_at = acquired
            self._writer_lease_generation = None
        return self

    def refresh(self) -> None:
        """Fsync the owner record; the kernel lock, not timestamps, is authority."""

        if self._descriptor is None:
            raise WriterLockError("lock is not held")
        if not self._descriptor_matches_path(self._descriptor):
            raise WriterLockError("account writer lock inode changed")
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
                self._descriptor_identity = None
                self._acquisition_id = None
                self._acquired_at = None
                self._writer_lease_generation = None
                _PROCESS_HELD_PATHS.discard(self.path)

    def holder_metadata(self) -> dict[str, Any]:
        descriptor = self._descriptor
        if descriptor is not None:
            if not self._descriptor_matches_path(descriptor):
                return {}
            return self._read_metadata_descriptor(descriptor)

        path_descriptor: int | None = None
        try:
            before = self.path.lstat()
            if not self._safe_file(before):
                return {}
            flags = os.O_RDONLY
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            path_descriptor = os.open(self.path, flags)
            opened = os.fstat(path_descriptor)
            after = self.path.lstat()
            if (
                not self._safe_file(opened)
                or not self._safe_file(after)
                or (before.st_dev, before.st_ino)
                != (opened.st_dev, opened.st_ino)
                or (after.st_dev, after.st_ino)
                != (opened.st_dev, opened.st_ino)
            ):
                return {}
            return self._read_metadata_descriptor(path_descriptor)
        except OSError:
            return {}
        finally:
            if path_descriptor is not None:
                os.close(path_descriptor)

    def __enter__(self) -> "AccountWriterLock":
        return self.acquire()

    def __exit__(self, *_: Any) -> None:
        self.release()


__all__ = [
    "AccountWriterLock",
    "WriterLockBusy",
    "WriterLockError",
    "account_writer_fingerprint",
    "attended_coordinator_lock_key",
    "user_account_writer_lock_directory",
]
