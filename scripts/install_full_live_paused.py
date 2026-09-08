#!/usr/bin/env python3
"""Verify and install a Titan full-live release in PAUSED mode.

This installer is intentionally incapable of loading launchd, starting the
runtime, contacting the broker, or writing outside the isolated full-live
subtree supplied by ``--root``.
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
import shutil
import sqlite3
import stat
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from typing import Any


MANIFEST_SCHEMA = "titan_full_live_release_2026-09-08_v1"
INSTALL_SCHEMA = "titan_full_live_paused_install_2026-09-08_v1"
LAUNCHD_LABEL = "com.harpcity.trader-brain-full-live"
MAX_ARCHIVE_MEMBER_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 128 * 1024 * 1024
HEX = frozenset("0123456789abcdef")


class InstallError(RuntimeError):
    pass


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
        raise InstallError(f"cannot read release archive: {exc}") from exc
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
        "src/titan_brain/live/release.py",
        "src/titan_brain/live/service.py",
        "deployment/com.harpcity.trader-brain-full-live.plist.in",
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
        raise InstallError(f"cannot prove existing runtime is paused: {exc}") from exc
    if len(rows) != 1:
        raise InstallError("cannot prove existing runtime is paused: runtime identity is missing")
    return str(rows[0][0])


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
    paths = (
        root / "control/install.lock",
        root / f"state/locks/account-{fingerprint}.writer.lock",
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


def _render_plist(root: Path, release_root: Path, python_executable: Path) -> bytes:
    template = release_root / "deployment/com.harpcity.trader-brain-full-live.plist.in"
    try:
        value = plistlib.loads(template.read_bytes())
    except (OSError, plistlib.InvalidFileException) as exc:
        raise InstallError(f"cannot load launchd template: {exc}") from exc
    substitutions = {
        "__PYTHON_EXECUTABLE__": str(python_executable),
        "__LAUNCHER__": str(root / "current/scripts/titan-full-live"),
        "__INSTALL_ROOT__": str(root),
        "__RELEASE_ROOT__": str(root / "current"),
        "__STDOUT_PATH__": str(root / "logs/launchd.stdout.log"),
        "__STDERR_PATH__": str(root / "logs/launchd.stderr.log"),
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
        str(root / "current/scripts/titan-full-live"),
        "serve",
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
        value.get("Label") != LAUNCHD_LABEL
        or "RunAtLoad" in value
        or value.get("Disabled") is not True
        or value.get("KeepAlive") != {"SuccessfulExit": False}
        or value.get("ProgramArguments") != expected_arguments
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
) -> dict[str, Any]:
    database = install_root / "state/full-live.sqlite3"
    current_mode = _database_mode(database)
    if current_mode is not None and current_mode != "PAUSED":
        raise InstallError(f"release switch requires PAUSED runtime; observed {current_mode}")

    release_id = str(manifest["release_manifest_hash"])
    destination = install_root / "releases" / release_id
    stage = Path(tempfile.mkdtemp(prefix=".install-", dir=install_root / "releases"))
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
            },
            "broker_accessed": False,
            "legacy_runtime_modified": False,
            "activation_required": True,
        }

        if destination.is_symlink():
            raise InstallError("existing release directory may not be a symlink")
        if destination.exists():
            _verify_existing_release(destination, manifest, manifest_bytes)
        else:
            os.replace(stage, destination)
            staged = False
        _verify_existing_release(destination, manifest, manifest_bytes)

        # Recheck under both deployment and account-writer interlocks at the
        # release-pointer commit boundary.
        current_mode = _database_mode(database)
        if current_mode is not None and current_mode != "PAUSED":
            raise InstallError(f"release switch requires PAUSED runtime; observed {current_mode}")

        current = install_root / "current"
        if current.exists() and not current.is_symlink():
            raise InstallError("current release pointer exists but is not a symlink")
        temporary_link = install_root / f".current-{release_id[:12]}"
        if os.path.lexists(temporary_link):
            temporary_link.unlink()
        temporary_link.symlink_to(Path("releases") / release_id, target_is_directory=True)
        os.replace(temporary_link, current)

        # These writes are validated and prepared before the pointer commit. A
        # crash can make CLI metadata inconsistent, but every CLI command then
        # fails closed on manifest/current binding instead of running mixed code.
        _atomic_write(install_root / "release-manifest.json", manifest_bytes, 0o600)
        plist_path = install_root / "launchd" / f"{LAUNCHD_LABEL}.plist"
        _atomic_write(plist_path, plist_bytes, 0o600)
        _atomic_write(
            install_root / "control/install-state.json",
            canonical_json(install_record),
            0o600,
        )
        return install_record
    finally:
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
        print(f"INSTALL_BLOCKED: {exc}", file=sys.stderr)
        return 78
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
