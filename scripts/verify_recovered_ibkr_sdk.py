#!/usr/bin/env python3
"""Offline, read-only verification of the exact recovered SDK snapshot.

Run on a stable, privately held extracted copy, not a concurrently changing tree.
This does not import SDK code, extract archives, install, or contact the broker.
The fixed CLI pins cannot be overridden; helper parameters only support tests.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat


RECEIPT_SHA256 = "996ce65c9f9ca192eae75609cfa1651745c0fc91e0372ec6d44a64bc0cfa45f3"
INVENTORY_SHA256 = "3869276715cc00367a2927bfeda3b8c9741b9d81f6df25a3d6f93aae52b70ddc"
FILE_COUNT = 303
TOTAL_BYTES = 2897207
RECEIPT_PATH = "control/ibkr-sdk-attestation.json"
PREFIXES = ("ibapi/", "ibapi-10.50.2.dist-info/", "google/protobuf/",
            "google/_upb/", "protobuf-5.29.5.dist-info/")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class VerificationError(ValueError):
    """A safe error code, never a path or account value."""


def require(condition, code):
    if not condition:
        raise VerificationError(code)


def canonical_json(value):
    # Matches scripts/install_full_live_paused.py canonical_json.
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("utf-8")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate_json_key")
        result[key] = value
    return result


def safe_relative(value):
    require(type(value) is str and bool(value), "invalid_relative_path")
    require(not any(ord(char) < 32 for char in value) and "\\" not in value
            and ":" not in value, "invalid_relative_path")
    parts = value.split("/")
    require(all(part not in ("", ".", "..") for part in parts)
            and not PurePosixPath(value).is_absolute(), "invalid_relative_path")
    return value


def scan_tree(root):
    require(stat.S_ISDIR(root.lstat().st_mode), "unsafe_snapshot_root")
    files, directories = set(), set()
    def walk_error(_error):
        raise VerificationError("snapshot_unreadable")
    for directory, names, filenames in os.walk(root, followlinks=False,
                                               onerror=walk_error):
        for name in names + filenames:
            path = Path(directory) / name
            mode = path.lstat().st_mode
            relative = path.relative_to(root).as_posix()
            require(not stat.S_ISLNK(mode), "snapshot_contains_symlink")
            require(stat.S_ISREG(mode) or stat.S_ISDIR(mode), "snapshot_contains_special_file")
            if stat.S_ISDIR(mode):
                directories.add(relative)
            else:
                require(not mode & 0o022, "snapshot_file_group_world_writable")
                files.add(relative)
    return files, directories


def read_regular(path, limit):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode), "snapshot_contains_special_file")
        require(not info.st_mode & 0o022, "snapshot_file_group_world_writable")
        require(info.st_size <= limit, "file_size_mismatch")
        payload = stream.read(limit + 1)
        require(len(payload) <= limit, "file_size_mismatch")
        return payload


def verify_snapshot(snapshot_root, *, receipt_sha256=RECEIPT_SHA256,
                    inventory_sha256=INVENTORY_SHA256, file_count=FILE_COUNT,
                    total_bytes=TOTAL_BYTES):
    root = Path(snapshot_root)
    observed_files, observed_dirs = scan_tree(root)
    require(RECEIPT_PATH in observed_files, "receipt_missing")
    raw = read_regular(root / RECEIPT_PATH, 131072)
    require(hashlib.sha256(raw).hexdigest() == receipt_sha256, "receipt_digest_mismatch")
    try:
        receipt = json.loads(raw, object_pairs_hook=unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerificationError("receipt_invalid_json") from exc
    fields = {"schema_version", "profile_id", "account_key", "python_implementation",
              "python_version", "import_root", "ibapi_version", "protobuf_version",
              "files", "inventory_hash"}
    require(type(receipt) is dict and set(receipt) == fields, "receipt_shape_invalid")
    require(all(type(receipt[field]) is str for field in fields - {"files"}),
            "receipt_metadata_type_invalid")
    require(raw == canonical_json(receipt) + b"\n", "receipt_not_canonical")
    require(receipt["schema_version"] == "titan_ibkr_sdk_snapshot_attestation_v1"
            and receipt["python_implementation"] == "cpython"
            and receipt["python_version"] == "3.12"
            and receipt["ibapi_version"] == "10.50.2"
            and receipt["protobuf_version"] == "5.29.5", "receipt_metadata_mismatch")
    require(receipt["inventory_hash"] == inventory_sha256, "inventory_digest_mismatch")
    relative_root = safe_relative(receipt["import_root"])
    require(relative_root == f"dependencies/ibkr-sdk/{inventory_sha256}/site-packages",
            "import_root_mismatch")
    records = receipt["files"]
    require(type(records) is list and len(records) == file_count, "inventory_count_mismatch")
    seen, total = set(), 0
    for record in records:
        require(type(record) is dict and set(record) == {"path", "size", "sha256"},
                "inventory_record_invalid")
        relative = safe_relative(record["path"])
        require(relative.startswith(PREFIXES) and relative not in seen,
                "inventory_path_unsafe_or_duplicate")
        require(type(record["size"]) is int and record["size"] >= 0, "inventory_size_invalid")
        require(type(record["sha256"]) is str and SHA256.fullmatch(record["sha256"]),
                "inventory_digest_invalid")
        seen.add(relative)
        total += record["size"]
    require([record["path"] for record in records] == sorted(seen), "inventory_not_sorted")
    require(total == total_bytes, "inventory_total_bytes_mismatch")
    require(hashlib.sha256(canonical_json(records)).hexdigest() == inventory_sha256,
            "inventory_digest_mismatch")
    expected_files = {f"{relative_root}/{path}" for path in seen} | {RECEIPT_PATH}
    expected_dirs = {parent.as_posix() for path in expected_files
                     for parent in PurePosixPath(path).parents if parent.as_posix() != "."}
    require(observed_files == expected_files and observed_dirs == expected_dirs,
            "snapshot_missing_or_extra_paths")
    for record in records:
        payload = read_regular(root / relative_root / record["path"], record["size"])
        require(len(payload) == record["size"], "file_size_mismatch")
        require(hashlib.sha256(payload).hexdigest() == record["sha256"], "file_digest_mismatch")
    return {"ok": True, "read_only": True, "receipt_sha256": receipt_sha256,
            "inventory_sha256": inventory_sha256, "file_count": file_count,
            "total_bytes": total_bytes}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-root", required=True, type=Path)
    args = parser.parse_args()
    try:
        summary = verify_snapshot(args.snapshot_root)
    except (VerificationError, OSError, ValueError) as exc:
        code = str(exc) if isinstance(exc, VerificationError) else "snapshot_unreadable_or_invalid"
        print(json.dumps({"ok": False, "read_only": True, "code": code}, sort_keys=True))
        return 1
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
