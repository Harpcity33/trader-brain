"""Signed local-provider profiles and installed dependency attestation.

This module is deliberately standard-library-only.  The release launcher uses
it while Python is running with ``-I -S`` to validate the separately installed
IBKR SDK snapshot against the exact inventory hash pinned by the committed
release config before adding that snapshot to ``sys.path``.  The SDK is
therefore neither trusted because it happens to be importable nor accepted on
package-version metadata alone.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
from typing import Any, Mapping


IBKR_PROFILE_SCHEMA = "titan_ibkr_local_provider_profile_v2"
IBKR_SDK_RECEIPT_SCHEMA = "titan_ibkr_sdk_snapshot_attestation_v1"
IBKR_SDK_RECEIPT_RELATIVE = Path("control/ibkr-sdk-attestation.json")
INSTALL_STATE_RELATIVE = Path("control/install-state.json")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PROFILE_ID = re.compile(r"^[a-z][a-z0-9._-]{2,127}$")
_ACCOUNT_KEY = re.compile(r"^[a-z][a-z0-9_-]{2,127}$")
_LAUNCHD_LABEL = re.compile(r"^[a-z0-9][a-z0-9.-]{2,127}$")
_ALLOWED_IMPORT_PREFIXES = (
    "ibapi/",
    "ibapi-10.50.2.dist-info/",
    "google/protobuf/",
    "google/_upb/",
    "protobuf-5.29.5.dist-info/",
)


class ProviderProfileError(ValueError):
    """Raised when a signed profile or local dependency receipt is unsafe."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_relative(value: object) -> str:
    relative = str(value)
    pure = PurePosixPath(relative)
    if (
        not relative
        or relative != pure.as_posix()
        or pure.is_absolute()
        or "." in pure.parts
        or ".." in pure.parts
        or "\\" in relative
    ):
        raise ProviderProfileError("dependency receipt contains an unsafe path")
    return relative


@dataclass(frozen=True)
class IbkrLocalProviderProfile:
    profile_id: str
    account_key: str
    account_last4: str
    install_subtree: str
    coordinator_launchd_label: str
    notification_launchd_label: str
    host: str
    port: int
    read_client_id: int
    command_client_id: int
    environment: str
    sdk_version: str
    protobuf_version: str
    sdk_inventory_hash: str

    @property
    def attended_read_client_id(self) -> int:
        """Positive read ID reserved independently of the service read ID.

        A prospective service reader using client 0 must not change the
        isolated attended reader, which remains derived from the command ID.
        """

        if (
            type(self.command_client_id) is not int
            or not 1 <= self.command_client_id < 2_147_483_647
            or type(self.read_client_id) is not int
            or not 0 <= self.read_client_id <= 2_147_483_647
            or self.read_client_id == self.command_client_id
        ):
            raise ProviderProfileError(
                "IBKR attended read client id cannot be isolated"
            )
        value = self.command_client_id + 1
        if value == self.read_client_id:
            raise ProviderProfileError(
                "IBKR attended read client id cannot be isolated"
            )
        return value

    def for_attended_command(self) -> "IbkrLocalProviderProfile":
        """Return the release-derived command profile without mutating config."""

        return replace(self, read_client_id=self.attended_read_client_id)

    @classmethod
    def from_config(
        cls, config: Mapping[str, Any]
    ) -> "IbkrLocalProviderProfile | None":
        raw = config.get("local_provider_profile")
        if raw is None:
            return None
        if not isinstance(raw, Mapping):
            raise ProviderProfileError("local provider profile must be an object")
        deployment = config.get("deployment")
        account = config.get("account")
        endpoint = raw.get("endpoint")
        sdk = raw.get("sdk")
        if not all(
            isinstance(value, Mapping)
            for value in (deployment, account, endpoint, sdk)
        ):
            raise ProviderProfileError("IBKR profile sections are incomplete")
        assert isinstance(deployment, Mapping)
        assert isinstance(account, Mapping)
        assert isinstance(endpoint, Mapping)
        assert isinstance(sdk, Mapping)

        if raw.get("schema_version") != IBKR_PROFILE_SCHEMA:
            raise ProviderProfileError("unsupported IBKR local provider profile")
        if raw.get("provider") != "interactive_brokers":
            raise ProviderProfileError("IBKR provider identity is invalid")
        profile_id = str(raw.get("profile_id", ""))
        account_key = str(account.get("account_key", ""))
        last4 = str(account.get("required_last4", ""))
        masked = str(account.get("masked_identifier", ""))
        install_subtree = str(deployment.get("install_subtree", ""))
        coordinator_label = str(deployment.get("coordinator_launchd_label", ""))
        notification_label = str(deployment.get("notification_launchd_label", ""))
        host = str(endpoint.get("host", ""))
        port = endpoint.get("port")
        read_client_id = raw.get("read_client_id")
        command_client_id = raw.get("command_client_id")
        environment = str(raw.get("environment", ""))
        sdk_version = str(sdk.get("ibapi_version", ""))
        protobuf_version = str(sdk.get("protobuf_version", ""))
        sdk_inventory_hash = str(sdk.get("expected_inventory_sha256", ""))

        if not _PROFILE_ID.fullmatch(profile_id):
            raise ProviderProfileError("IBKR profile id is invalid")
        if not _ACCOUNT_KEY.fullmatch(account_key) or account_key != "ibkr-live-ending-3103":
            raise ProviderProfileError("IBKR state namespace is not the approved isolated key")
        if last4 != "3103" or masked != "ending-3103":
            raise ProviderProfileError("IBKR masked account binding is invalid")
        if (
            account.get("allowed_type") != "no_borrow_margin"
            or account.get("margin_debit_allowed") is not False
        ):
            raise ProviderProfileError("IBKR no-borrow account restriction is invalid")
        if install_subtree != "Application Support/Titan Momentum/full-live-ibkr-ending-3103":
            raise ProviderProfileError("IBKR install subtree is not isolated")
        if not _LAUNCHD_LABEL.fullmatch(coordinator_label):
            raise ProviderProfileError("IBKR coordinator launchd label is invalid")
        if not _LAUNCHD_LABEL.fullmatch(notification_label):
            raise ProviderProfileError("IBKR notification launchd label is invalid")
        if coordinator_label == notification_label:
            raise ProviderProfileError("IBKR launchd roles require separate labels")
        if deployment.get("profile_id") != profile_id:
            raise ProviderProfileError("IBKR deployment/profile identity mismatch")
        if host != "127.0.0.1" or endpoint.get("loopback_only") is not True:
            raise ProviderProfileError("IBKR endpoint must be explicit IPv4 loopback")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ProviderProfileError("IBKR endpoint port is invalid")
        # Client 0 is a valid read identity, but it does not itself establish
        # Master Client configuration or broker-wide visibility.  Selecting it
        # still changes the signed profile and requires a fresh release.
        for name, value, minimum in (
            ("read", read_client_id, 0),
            ("command", command_client_id, 1),
        ):
            if type(value) is not int or not minimum <= value <= 2_147_483_647:
                raise ProviderProfileError(f"IBKR {name} client id is invalid")
        if read_client_id == command_client_id:
            raise ProviderProfileError("IBKR read and command client ids must be isolated")
        if command_client_id == 2_147_483_647 or read_client_id == command_client_id + 1:
            raise ProviderProfileError(
                "IBKR attended read client id must be isolated from service and command ids"
            )
        if environment != "live":
            raise ProviderProfileError("IBKR profile environment is invalid")
        if sdk.get("distribution") != "official_tws_python_api":
            raise ProviderProfileError("IBKR SDK distribution is invalid")
        if sdk_version != "10.50.2" or protobuf_version != "5.29.5":
            raise ProviderProfileError("IBKR SDK dependency versions are not pinned")
        if not _SHA256.fullmatch(sdk_inventory_hash):
            raise ProviderProfileError("IBKR SDK dependency inventory is not pinned")
        if sdk.get("installation_mode") != "installer_attested_snapshot":
            raise ProviderProfileError("IBKR SDK must use an attested local snapshot")
        if raw.get("persist_full_account_identifier") is not False:
            raise ProviderProfileError("IBKR profile may not persist a full account id")
        if raw.get("account_binding_source") != "managed_accounts_runtime_last4_match":
            raise ProviderProfileError("IBKR runtime account binding source is invalid")
        expected_fields = {
            "profile": {
                "schema_version",
                "profile_id",
                "provider",
                "environment",
                "endpoint",
                "read_client_id",
                "command_client_id",
                "account_binding_source",
                "persist_full_account_identifier",
                "sdk",
            },
            "account": {
                "account_key",
                "masked_identifier",
                "required_last4",
                "allowed_type",
                "margin_debit_allowed",
            },
            "deployment": {
                "profile_id",
                "install_subtree",
                "coordinator_launchd_label",
                "notification_launchd_label",
            },
            "endpoint": {"host", "port", "loopback_only"},
            "sdk": {
                "distribution",
                "ibapi_version",
                "protobuf_version",
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
            raise ProviderProfileError(
                "IBKR profile contains an unapproved field or identifier"
            )
        return cls(
            profile_id=profile_id,
            account_key=account_key,
            account_last4=last4,
            install_subtree=install_subtree,
            coordinator_launchd_label=coordinator_label,
            notification_launchd_label=notification_label,
            host=host,
            port=port,
            read_client_id=read_client_id,
            command_client_id=command_client_id,
            environment=environment,
            sdk_version=sdk_version,
            protobuf_version=protobuf_version,
            sdk_inventory_hash=sdk_inventory_hash,
        )


@dataclass(frozen=True)
class InstalledSdkAttestation:
    profile_id: str
    import_root: Path
    inventory_hash: str
    receipt_hash: str
    file_count: int
    ibapi_version: str
    protobuf_version: str


def validate_installed_sdk(
    install_root: str | Path,
    profile: IbkrLocalProviderProfile,
) -> InstalledSdkAttestation:
    """Verify the complete separately installed SDK snapshot without import."""

    root = Path(install_root).expanduser().resolve(strict=True)
    receipt_path = root / IBKR_SDK_RECEIPT_RELATIVE
    state_path = root / INSTALL_STATE_RELATIVE
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise ProviderProfileError("IBKR SDK attestation receipt is missing")
    if state_path.is_symlink() or not state_path.is_file():
        raise ProviderProfileError("install state is missing for IBKR SDK attestation")
    receipt_bytes = receipt_path.read_bytes()
    try:
        receipt = json.loads(receipt_bytes)
        install_state = json.loads(state_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderProfileError("IBKR SDK attestation metadata is invalid") from exc
    if not isinstance(receipt, dict) or not isinstance(install_state, dict):
        raise ProviderProfileError("IBKR SDK attestation metadata is invalid")
    receipt_hash = hashlib.sha256(receipt_bytes).hexdigest()
    external = install_state.get("external_dependencies")
    if (
        not isinstance(external, Mapping)
        or external.get("kind") != "ibkr_release_pinned_sdk_snapshot"
        or external.get("profile_id") != profile.profile_id
        or external.get("inventory_hash") != profile.sdk_inventory_hash
        or external.get("receipt_sha256") != receipt_hash
    ):
        raise ProviderProfileError("IBKR SDK receipt is not bound to install state")
    required_fields = {
        "schema_version",
        "profile_id",
        "account_key",
        "python_implementation",
        "python_version",
        "import_root",
        "ibapi_version",
        "protobuf_version",
        "files",
        "inventory_hash",
    }
    if set(receipt) != required_fields or receipt.get("schema_version") != IBKR_SDK_RECEIPT_SCHEMA:
        raise ProviderProfileError("IBKR SDK receipt shape is invalid")
    if receipt.get("profile_id") != profile.profile_id or receipt.get("account_key") != profile.account_key:
        raise ProviderProfileError("IBKR SDK receipt profile binding mismatch")
    if receipt.get("python_implementation") != sys.implementation.name:
        raise ProviderProfileError("IBKR SDK Python implementation mismatch")
    expected_python = f"{sys.version_info.major}.{sys.version_info.minor}"
    if receipt.get("python_version") != expected_python:
        raise ProviderProfileError("IBKR SDK Python minor version mismatch")
    if receipt.get("ibapi_version") != profile.sdk_version or receipt.get("protobuf_version") != profile.protobuf_version:
        raise ProviderProfileError("IBKR SDK receipt version mismatch")
    if receipt.get("inventory_hash") != profile.sdk_inventory_hash:
        raise ProviderProfileError("IBKR SDK receipt does not match the release-pinned inventory")
    import_relative = _canonical_relative(receipt.get("import_root"))
    if not import_relative.startswith("dependencies/ibkr-sdk/") or not import_relative.endswith("/site-packages"):
        raise ProviderProfileError("IBKR SDK import root is outside its isolated subtree")
    import_root = root / import_relative
    if import_root.is_symlink() or not import_root.is_dir():
        raise ProviderProfileError("IBKR SDK snapshot import root is missing")
    try:
        import_root.resolve(strict=True).relative_to(root)
    except ValueError as exc:
        raise ProviderProfileError("IBKR SDK snapshot escaped the install root") from exc

    files = receipt.get("files")
    if not isinstance(files, list) or not files:
        raise ProviderProfileError("IBKR SDK receipt inventory is empty")
    normalized: list[dict[str, object]] = []
    seen: set[str] = set()
    for record in files:
        if not isinstance(record, dict) or set(record) != {"path", "size", "sha256"}:
            raise ProviderProfileError("IBKR SDK file receipt is invalid")
        relative = _canonical_relative(record.get("path"))
        if relative in seen or not any(relative.startswith(prefix) for prefix in _ALLOWED_IMPORT_PREFIXES):
            raise ProviderProfileError("IBKR SDK file inventory is unsafe")
        seen.add(relative)
        size = record.get("size")
        digest = record.get("sha256")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ProviderProfileError("IBKR SDK file size is invalid")
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise ProviderProfileError("IBKR SDK file digest is invalid")
        target = import_root / relative
        if target.is_symlink() or not target.is_file():
            raise ProviderProfileError("IBKR SDK snapshot file is missing")
        mode = stat.S_IMODE(target.stat().st_mode)
        if mode & 0o022:
            raise ProviderProfileError("IBKR SDK snapshot file is mutable by group or world")
        if target.stat().st_size != size or _sha256_file(target) != digest:
            raise ProviderProfileError("IBKR SDK snapshot file failed attestation")
        normalized.append({"path": relative, "size": size, "sha256": digest})
    if [item["path"] for item in normalized] != sorted(seen):
        raise ProviderProfileError("IBKR SDK receipt inventory is not sorted")
    observed: set[str] = set()
    for candidate in import_root.rglob("*"):
        if candidate.is_symlink():
            raise ProviderProfileError("IBKR SDK snapshot contains a symlink")
        if candidate.is_file():
            observed.add(candidate.relative_to(import_root).as_posix())
        elif not candidate.is_dir():
            raise ProviderProfileError("IBKR SDK snapshot contains a special file")
    if observed != seen:
        raise ProviderProfileError("IBKR SDK snapshot has missing or unapproved files")
    inventory_hash = hashlib.sha256(_canonical_json(normalized)).hexdigest()
    if receipt.get("inventory_hash") != inventory_hash:
        raise ProviderProfileError("IBKR SDK inventory hash mismatch")
    if inventory_hash != profile.sdk_inventory_hash:
        raise ProviderProfileError("IBKR SDK inventory differs from the release pin")
    return InstalledSdkAttestation(
        profile_id=profile.profile_id,
        import_root=import_root,
        inventory_hash=inventory_hash,
        receipt_hash=receipt_hash,
        file_count=len(normalized),
        ibapi_version=profile.sdk_version,
        protobuf_version=profile.protobuf_version,
    )


def activate_installed_sdk(
    install_root: str | Path,
    profile: IbkrLocalProviderProfile,
) -> InstalledSdkAttestation:
    """Add only a fully revalidated local SDK snapshot to isolated Python."""

    attestation = validate_installed_sdk(install_root, profile)
    import_root = str(attestation.import_root)
    if import_root not in sys.path:
        sys.path.insert(1, import_root)
    return attestation


def redacted_profile_status(
    install_root: str | Path,
    profile: IbkrLocalProviderProfile,
) -> dict[str, Any]:
    """Return non-secret endpoint/profile state without network access."""

    try:
        attestation = validate_installed_sdk(install_root, profile)
        sdk: dict[str, Any] = {
            "status": "ATTESTED",
            "ibapi_version": attestation.ibapi_version,
            "protobuf_version": attestation.protobuf_version,
            "inventory_hash": attestation.inventory_hash,
            "file_count": attestation.file_count,
        }
    except (OSError, ProviderProfileError):
        sdk = {
            "status": "BLOCKED",
            "reason": "IBKR_SDK_SNAPSHOT_NOT_ATTESTED",
            "ibapi_version": profile.sdk_version,
            "protobuf_version": profile.protobuf_version,
        }
    return {
        "schema_version": "titan_local_provider_status_v1",
        "profile_id": profile.profile_id,
        "provider": "interactive_brokers",
        "account_key": profile.account_key,
        "account_masked": f"ending-{profile.account_last4}",
        "environment": profile.environment,
        "endpoint": {
            "host": profile.host,
            "port": profile.port,
            "loopback_only": True,
            "network_probe_performed": False,
        },
        "client_ids": {
            "read": profile.read_client_id,
            "command": profile.command_client_id,
            "isolated": True,
        },
        "install_root": str(Path(install_root).expanduser().resolve()),
        "sdk": sdk,
        "full_account_identifier_persisted": False,
        "broker_authenticated": False,
        "broker_write_authority": False,
    }


__all__ = [
    "IBKR_PROFILE_SCHEMA",
    "IBKR_SDK_RECEIPT_RELATIVE",
    "IBKR_SDK_RECEIPT_SCHEMA",
    "IbkrLocalProviderProfile",
    "InstalledSdkAttestation",
    "ProviderProfileError",
    "activate_installed_sdk",
    "redacted_profile_status",
    "validate_installed_sdk",
]
