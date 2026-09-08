"""Content-addressed release-manifest validation."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
from typing import Any, Mapping


MANIFEST_SCHEMA = "titan_full_live_release_2026-09-08_v1"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
GIT_COMMIT = re.compile(r"^[0-9a-f]{40}$")
MANIFEST_FIELDS = {
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
FILE_FIELDS = {"path", "sha256", "size", "mode"}


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def calculate_manifest_hash(manifest: Mapping[str, Any]) -> str:
    payload = dict(manifest)
    payload.pop("release_manifest_hash", None)
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _canonical_relative_path(value: object) -> str:
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
        raise ValueError("release manifest contains a non-canonical path")
    return relative


def _assert_no_symlink_components(root: Path, relative: str) -> Path:
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"release path contains a symlink: {relative}")
    if not current.is_file():
        raise ValueError(f"release file is missing or not regular: {relative}")
    return current


def _strict_installed_inventory(root: Path, expected: set[str], manifest: Mapping[str, Any]) -> None:
    embedded = root / "release-manifest.json"
    if not embedded.exists():
        # A repository source tree is also a supported verification root.  The
        # installed layout is distinguished by its embedded manifest and is
        # held to an exact inventory.
        return
    if embedded.is_symlink() or not embedded.is_file():
        raise ValueError("installed release manifest is not a regular file")
    expected_manifest = (canonical_json(manifest) + "\n").encode("utf-8")
    if embedded.read_bytes() != expected_manifest:
        raise ValueError("embedded release manifest differs from installed manifest")
    observed: set[str] = set()
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in directory_names:
            candidate = base / name
            if candidate.is_symlink():
                raise ValueError("installed release contains a symlink directory")
        for name in file_names:
            candidate = base / name
            if candidate.is_symlink() or not candidate.is_file():
                raise ValueError("installed release contains a non-regular file")
            observed.add(candidate.relative_to(root).as_posix())
    allowed = expected | {"release-manifest.json"}
    if observed != allowed:
        missing = sorted(allowed - observed)
        extra = sorted(observed - allowed)
        raise ValueError(f"installed release inventory differs; missing={missing}, extra={extra}")


def load_release_manifest(
    path: str | Path, *, verify_files_root: str | Path | None = None
) -> dict[str, Any]:
    source = Path(path)
    with source.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict) or raw.get("schema_version") != MANIFEST_SCHEMA:
        raise ValueError("unsupported full-live release manifest")
    if set(raw) != MANIFEST_FIELDS:
        missing = sorted(MANIFEST_FIELDS - set(raw))
        extra = sorted(set(raw) - MANIFEST_FIELDS)
        raise ValueError(f"release manifest fields differ; missing={missing}, extra={extra}")
    semantics = {
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
    for field, expected_value in semantics.items():
        if raw.get(field) != expected_value:
            raise ValueError(f"release manifest semantic mismatch: {field}")
    if not isinstance(raw.get("source_commit"), str) or not GIT_COMMIT.fullmatch(
        str(raw["source_commit"])
    ):
        raise ValueError("release manifest source_commit is invalid")
    expected = raw.get("release_manifest_hash")
    if not isinstance(expected, str) or not SHA256.fullmatch(expected):
        raise ValueError("release manifest hash is missing or invalid")
    if calculate_manifest_hash(raw) != expected:
        raise ValueError("release manifest is not self-consistent")
    for field in ("config_hash", "policy_hash", "source_tree"):
        if not isinstance(raw.get(field), str) or not SHA256.fullmatch(str(raw[field])):
            raise ValueError(f"release manifest {field} is invalid")
    files = raw.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("release manifest file inventory is empty")
    seen: set[str] = set()
    root = Path(verify_files_root).resolve() if verify_files_root is not None else None
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != FILE_FIELDS:
            raise ValueError("release manifest file entry must be an object")
        relative = _canonical_relative_path(entry.get("path"))
        if relative == "release-manifest.json" or relative in seen:
            raise ValueError("release manifest contains an unsafe or duplicate path")
        seen.add(relative)
        digest = entry.get("sha256")
        size = entry.get("size")
        if not isinstance(digest, str) or not SHA256.fullmatch(digest):
            raise ValueError("release manifest file digest is invalid")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError("release manifest file size is invalid")
        if entry.get("mode") not in {"0644", "0755"}:
            raise ValueError("release manifest file mode is invalid")
        if root is not None:
            target = _assert_no_symlink_components(root, relative)
            data = target.read_bytes()
            if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
                raise ValueError(f"installed release file differs from manifest: {relative}")
    ordered = [str(entry["path"]) for entry in files]
    if ordered != sorted(ordered):
        raise ValueError("release manifest file inventory is not sorted")
    if hashlib.sha256(canonical_json(files).encode("utf-8")).hexdigest() != raw["source_tree"]:
        raise ValueError("release manifest source_tree does not match its inventory")
    if root is not None:
        _strict_installed_inventory(root, seen, raw)
    return raw


__all__ = [
    "MANIFEST_SCHEMA",
    "calculate_manifest_hash",
    "canonical_json",
    "load_release_manifest",
]
