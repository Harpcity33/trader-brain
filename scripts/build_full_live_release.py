#!/usr/bin/env python3
"""Build a deterministic, content-addressed Titan full-live release.

Only Python's standard library is used. Archive members have normalized
ownership, permissions, ordering, and timestamps, so identical source content
and source revision produce identical bytes on repeated builds.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import tarfile
import tempfile
from typing import Mapping


MANIFEST_SCHEMA = "titan_full_live_release_2026-09-08_v1"
RELEASE_NAME = "titan-full-live"
FIXED_EPOCH = 0
MAX_SOURCE_FILE_BYTES = 16 * 1024 * 1024
HEX = frozenset("0123456789abcdef")
FIXED_RELEASE_PATHS = (
    "pyproject.toml",
    "README.md",
    "ARCHITECTURE.md",
    "CODEX_FULL_LIVE_AUTONOMY_2026-09-08.md",
    "scripts/titan-full-live",
    "validation/full-live/2026-09-08/OPERATIONS.md",
)


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


def validate_source_revision(value: str) -> str:
    normalized = str(value).strip().lower()
    if len(normalized) != 40 or any(character not in HEX for character in normalized):
        raise ValueError("source revision must be an exact 40-character Git commit")
    return normalized


def _git(root: Path, *arguments: str, input_bytes: bytes | None = None) -> bytes:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *arguments],
            input=input_bytes,
            check=False,
            capture_output=True,
        )
    except FileNotFoundError as exc:
        raise ValueError("Git is required to establish release provenance") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        if detail:
            detail = detail.splitlines()[0][:300]
        raise ValueError(
            "Git provenance check failed"
            + (f": {detail}" if detail else "")
        )
    return completed.stdout


def _repository_head(root: Path) -> str:
    if not root.is_dir():
        raise ValueError("source root must be an existing directory")
    top_level = Path(
        _git(root, "rev-parse", "--show-toplevel").decode("utf-8").strip()
    ).resolve()
    if top_level != root:
        raise ValueError("source root must be the Git repository top level")
    return validate_source_revision(
        _git(root, "rev-parse", "--verify", "HEAD^{commit}")
        .decode("ascii")
        .strip()
    )


def discover_source_revision(root: Path) -> str:
    """Return the exact repository HEAD; unversioned input is never releasable."""

    return _repository_head(root.resolve())


def _is_release_path(relative: str) -> bool:
    pure = PurePosixPath(relative)
    parts = pure.parts
    if relative in FIXED_RELEASE_PATHS:
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


def _assert_clean_release_source(root: Path) -> None:
    tracked_changes = _git(
        root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=no",
        "--",
        ".",
    )
    if tracked_changes:
        raise ValueError("tracked worktree must be clean before building a release")
    untracked = _git(
        root,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
        "--",
        ".",
    )
    untracked_release_paths = sorted(
        path.decode("utf-8")
        for path in untracked.split(b"\0")
        if path and _is_release_path(path.decode("utf-8"))
    )
    if untracked_release_paths:
        raise ValueError(
            "release inputs must be committed before building: "
            + ", ".join(untracked_release_paths)
        )


def _resolve_source_revision(root: Path, requested: str | None) -> str:
    head = _repository_head(root)
    if requested is None:
        return head
    candidate = validate_source_revision(requested)
    if candidate != head:
        raise ValueError("source revision must resolve exactly to repository HEAD")
    resolved = validate_source_revision(
        _git(root, "rev-parse", "--verify", f"{candidate}^{{commit}}")
        .decode("ascii")
        .strip()
    )
    if resolved != head:
        raise ValueError("source revision must resolve exactly to repository HEAD")
    return head


def _git_tree_entries(root: Path, revision: str) -> dict[str, tuple[str, str, str]]:
    raw = _git(root, "ls-tree", "-r", "-z", revision)
    entries: dict[str, tuple[str, str, str]] = {}
    for encoded in raw.split(b"\0"):
        if not encoded:
            continue
        metadata, separator, encoded_path = encoded.partition(b"\t")
        if not separator:
            raise ValueError("Git tree returned malformed release provenance")
        fields = metadata.decode("ascii").split(" ")
        if len(fields) != 3:
            raise ValueError("Git tree returned malformed release metadata")
        relative = encoded_path.decode("utf-8")
        pure = PurePosixPath(relative)
        if (
            not relative
            or relative != pure.as_posix()
            or pure.is_absolute()
            or "." in pure.parts
            or ".." in pure.parts
            or "\\" in relative
        ):
            raise ValueError("Git tree contains a non-canonical release path")
        entries[relative] = (fields[0], fields[1], fields[2])
    return entries


def _read_git_blobs(root: Path, object_ids: list[str]) -> dict[str, bytes]:
    unique = sorted(set(object_ids))
    raw = _git(
        root,
        "cat-file",
        "--batch",
        input_bytes=("\n".join(unique) + "\n").encode("ascii"),
    )
    result: dict[str, bytes] = {}
    offset = 0
    for requested in unique:
        end = raw.find(b"\n", offset)
        if end < 0:
            raise ValueError("Git blob stream ended before its object header")
        header = raw[offset:end].decode("ascii").split(" ")
        offset = end + 1
        if len(header) != 3 or header[0] != requested or header[1] != "blob":
            raise ValueError("Git object is not an expected release blob")
        size = int(header[2])
        if size > MAX_SOURCE_FILE_BYTES:
            raise ValueError("release input exceeds size limit")
        data = raw[offset : offset + size]
        offset += size
        if len(data) != size or raw[offset : offset + 1] != b"\n":
            raise ValueError("Git blob stream has an invalid payload boundary")
        offset += 1
        result[requested] = data
    if offset != len(raw):
        raise ValueError("Git blob stream contains unexpected trailing data")
    return result


def collect_committed_files(
    root: Path, source_revision: str
) -> tuple[list[dict[str, object]], dict[str, bytes]]:
    entries = _git_tree_entries(root, source_revision)
    missing = sorted(set(FIXED_RELEASE_PATHS) - set(entries))
    if missing:
        raise FileNotFoundError(f"required committed release files are missing: {missing}")
    selected = sorted(relative for relative in entries if _is_release_path(relative))
    if not selected:
        raise ValueError("committed release file inventory is empty")
    for relative in selected:
        mode, object_type, _object_id = entries[relative]
        if mode == "120000":
            raise ValueError(f"release inputs may not be symlinks: {relative}")
        if object_type != "blob" or mode not in {"100644", "100755"}:
            raise ValueError(f"release input is not a regular Git blob: {relative}")
    blobs = _read_git_blobs(root, [entries[path][2] for path in selected])
    payloads = {relative: blobs[entries[relative][2]] for relative in selected}
    records = [
        {
            "path": relative,
            "sha256": sha256_bytes(payloads[relative]),
            "size": len(payloads[relative]),
            "mode": "0755" if relative == "scripts/titan-full-live" else "0644",
        }
        for relative in selected
    ]
    return records, payloads


def build_manifest(
    files: list[dict[str, object]],
    payloads: Mapping[str, bytes],
    source_revision: str,
) -> tuple[dict[str, object], bytes]:
    config = json.loads(payloads["config/full_live.json"].decode("utf-8"))
    risk_relative = str(config["risk"]["limits_path"])
    if risk_relative not in payloads:
        raise ValueError("configured risk limits file is not in the committed release")
    risk = json.loads(payloads[risk_relative].decode("utf-8"))
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
    descriptor: dict[str, object] = {
        "schema_version": MANIFEST_SCHEMA,
        "release_name": RELEASE_NAME,
        "source_commit": source_revision,
        "source_tree": sha256_bytes(canonical_json(files)),
        "config_hash": config_hash,
        "policy_hash": policy_hash,
        "reproducible_epoch": FIXED_EPOCH,
        "python_requires": ">=3.11",
        "entrypoint": "scripts/titan-full-live",
        "default_mode": "PAUSED",
        "install_subtree": "Application Support/Titan Momentum/full-live",
        "launchd_template": "deployment/com.harpcity.trader-brain-full-live.plist.in",
        "files": files,
    }
    manifest_hash = sha256_bytes(canonical_json(descriptor))
    manifest = {**descriptor, "release_manifest_hash": manifest_hash}
    return manifest, canonical_json(manifest) + b"\n"


def _tar_info(name: str, data: bytes, mode: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    info.mode = mode
    info.mtime = FIXED_EPOCH
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.type = tarfile.REGTYPE
    return info


def write_archive(
    archive: Path,
    manifest: dict[str, object],
    manifest_bytes: bytes,
    payloads: Mapping[str, bytes],
) -> None:
    archive.parent.mkdir(parents=True, exist_ok=True)
    descriptor = {str(item["path"]): item for item in manifest["files"]}  # type: ignore[index]
    file_descriptor, temporary_name = tempfile.mkstemp(prefix=".full-live-", dir=archive.parent)
    os.close(file_descriptor)
    temporary = Path(temporary_name)
    try:
        with temporary.open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as bundle:
                    bundle.addfile(
                        _tar_info("release-manifest.json", manifest_bytes, 0o644),
                        __import__("io").BytesIO(manifest_bytes),
                    )
                    for relative in sorted(descriptor):
                        data = payloads[relative]
                        mode = int(str(descriptor[relative]["mode"]), 8)
                        bundle.addfile(_tar_info(relative, data, mode), __import__("io").BytesIO(data))
        os.replace(temporary, archive)
    finally:
        if temporary.exists():
            temporary.unlink()


def _atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
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


def build(source_root: Path, output_dir: Path, source_revision: str | None = None) -> dict[str, str]:
    root = source_root.resolve()
    revision = _resolve_source_revision(root, source_revision)
    _assert_clean_release_source(root)
    files, payloads = collect_committed_files(root, revision)
    manifest, manifest_bytes = build_manifest(files, payloads, revision)
    release_id = str(manifest["release_manifest_hash"])
    archive = output_dir.resolve() / f"{RELEASE_NAME}-{release_id[:20]}.tar.gz"
    # Recheck immediately before publishing. Archive bytes come only from Git
    # objects at ``revision``, so a later worktree race cannot contaminate the
    # committed payload even if the build is interrupted here.
    if _resolve_source_revision(root, revision) != revision:
        raise ValueError("repository HEAD changed during release build")
    _assert_clean_release_source(root)
    write_archive(archive, manifest, manifest_bytes, payloads)
    archive_hash = sha256_file(archive)
    manifest_path = Path(str(archive) + ".manifest.json")
    checksum_path = Path(str(archive) + ".sha256")
    _atomic_write(manifest_path, manifest_bytes)
    _atomic_write(checksum_path, f"{archive_hash}  {archive.name}\n".encode("ascii"))
    return {
        "archive": str(archive),
        "archive_sha256": archive_hash,
        "manifest": str(manifest_path),
        "manifest_sha256": sha256_bytes(manifest_bytes),
        "release_id": release_id,
        "source_commit": revision,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output-dir", type=Path, default=Path("dist/full-live"))
    parser.add_argument(
        "--source-revision",
        help="exact 40-character repository HEAD (defaults to the verified HEAD)",
    )
    arguments = parser.parse_args(argv)
    result = build(arguments.source_root, arguments.output_dir, arguments.source_revision)
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
