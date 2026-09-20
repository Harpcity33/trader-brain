"""Synthetic offline tests; no SDK import, broker connection, or live-state access."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/verify_recovered_ibkr_sdk.py"
SPEC = importlib.util.spec_from_file_location("recovery_verifier", SCRIPT)
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


class RecoveredSdkTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "snapshot"
        self.root.mkdir()
        self.records = [{"path": "ibapi/a.py", "size": 3,
                         "sha256": hashlib.sha256(b"abc").hexdigest()}]
        self.receipt = {"schema_version": "titan_ibkr_sdk_snapshot_attestation_v1",
                        "profile_id": "synthetic-profile", "account_key": "synthetic-account",
                        "python_implementation": "cpython", "python_version": "3.12",
                        "ibapi_version": "10.50.2", "protobuf_version": "5.29.5",
                        "files": self.records}
        self.pin = hashlib.sha256(verifier.canonical_json(self.records)).hexdigest()
        self.receipt["inventory_hash"] = self.pin
        self.receipt["import_root"] = f"dependencies/ibkr-sdk/{self.pin}/site-packages"
        self.sdk = self.root / self.receipt["import_root"]
        self.file = self.sdk / "ibapi/a.py"
        self.file.parent.mkdir(parents=True)
        self.file.write_bytes(b"abc")
        self.file.chmod(0o644)
        self.receipt_path = self.root / verifier.RECEIPT_PATH
        self.receipt_path.parent.mkdir()
        self.write_receipt()

    def write_receipt(self, raw=None):
        raw = raw if raw is not None else verifier.canonical_json(self.receipt) + b"\n"
        self.receipt_path.write_bytes(raw)
        self.receipt_path.chmod(0o644)
        self.receipt_pin = hashlib.sha256(raw).hexdigest()

    def verify(self, **overrides):
        pins = {"receipt_sha256": self.receipt_pin, "inventory_sha256": self.pin,
                "file_count": 1, "total_bytes": 3}
        pins.update(overrides)
        return verifier.verify_snapshot(self.root, **pins)

    def rejected(self, code=None):
        return self.assertRaisesRegex(verifier.VerificationError, code or ".*")

    def test_success_read_only(self):
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                  for path in (self.file, self.receipt_path)}
        self.assertTrue(self.verify()["ok"])
        self.assertEqual(before, {path: (path.read_bytes(), path.stat().st_mtime_ns)
                                  for path in before})

    def test_missing_file(self):
        self.file.unlink()
        with self.rejected("snapshot_missing_or_extra_paths"):
            self.verify()

    def test_extra_file(self):
        (self.root / "unapproved.txt").write_text("unexpected")
        with self.rejected("snapshot_missing_or_extra_paths"):
            self.verify()

    def test_extra_empty_directory(self):
        (self.root / "unexpected").mkdir()
        with self.rejected("snapshot_missing_or_extra_paths"):
            self.verify()

    def test_unsafe_paths(self):
        for path in ("../outside", "/outside", "ibapi/../a.py", "ibapi//a.py",
                     "ibapi/./a.py", "ibapi\\a.py", "C:/outside", "ibapi/a\x00.py", 1):
            with self.subTest(path=path):
                self.records[0]["path"] = path
                self.write_receipt()
                with self.rejected("invalid_relative_path"):
                    self.verify()

    def test_wrong_size(self):
        for content in (b"ab", b"abcd"):
            with self.subTest(content=content):
                self.file.write_bytes(content)
                with self.rejected("file_size_mismatch"):
                    self.verify()

    def test_wrong_digest(self):
        self.file.write_bytes(b"abd")
        with self.rejected("file_digest_mismatch"):
            self.verify()

    def test_wrong_receipt_bytes(self):
        self.receipt_path.write_bytes(self.receipt_path.read_bytes() + b" ")
        with self.rejected("receipt_digest_mismatch"):
            self.verify()

    def test_receipt_not_canonical(self):
        self.write_receipt(json.dumps(self.receipt).encode())
        with self.rejected("receipt_not_canonical"):
            self.verify()

    def test_duplicate_json_key(self):
        raw = self.receipt_path.read_bytes().replace(b'{', b'{"files":[],', 1)
        self.write_receipt(raw)
        with self.rejected("duplicate_json_key"):
            self.verify()

    def test_duplicate_inventory_entry(self):
        self.records.append(dict(self.records[0]))
        self.write_receipt()
        with self.rejected("inventory_path_unsafe_or_duplicate"):
            self.verify(file_count=2, total_bytes=6)

    def test_invalid_record_types(self):
        for field, value in (("size", True), ("size", -1), ("size", "3"),
                             ("sha256", 9), ("sha256", "A" * 64)):
            with self.subTest(field=field, value=value):
                original = self.records[0][field]
                self.records[0][field] = value
                self.write_receipt()
                with self.rejected("inventory_(size|digest)_invalid"):
                    self.verify()
                self.records[0][field] = original

    def test_wrong_inventory_digest_and_count(self):
        with self.rejected("inventory_digest_mismatch"):
            self.verify(inventory_sha256="0" * 64)
        with self.rejected("inventory_count_mismatch"):
            self.verify(file_count=2)
        with self.rejected("inventory_total_bytes_mismatch"):
            self.verify(total_bytes=4)

    def test_group_or_world_writable_file_and_receipt(self):
        for path in (self.file, self.receipt_path):
            for mode in (0o664, 0o646):
                with self.subTest(kind=path.name, mode=mode):
                    path.chmod(mode)
                    with self.rejected("snapshot_file_group_world_writable"):
                        self.verify()
                    path.chmod(0o644)

    def test_file_symlink(self):
        self.file.unlink()
        self.file.symlink_to(self.receipt_path)
        with self.rejected("snapshot_contains_symlink"):
            self.verify()

    def test_directory_symlink(self):
        (self.root / "alias").symlink_to(self.sdk, target_is_directory=True)
        with self.rejected("snapshot_contains_symlink"):
            self.verify()

    def test_special_file(self):
        os.mkfifo(self.root / "fifo")
        with self.rejected("snapshot_contains_special_file"):
            self.verify()

    def test_cli_failure_is_safe_json(self):
        result = subprocess.run([sys.executable, "-I", "-S", "-B", str(SCRIPT),
                                 "--snapshot-root", str(self.root)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout),
                         {"ok": False, "read_only": True, "code": "receipt_digest_mismatch"})
        self.assertEqual(result.stderr, "")
        self.assertNotIn(str(self.root), result.stdout)
        self.assertNotIn("synthetic-account", result.stdout)


if __name__ == "__main__":
    unittest.main()
