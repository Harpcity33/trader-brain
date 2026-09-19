from __future__ import annotations

import os
from pathlib import Path
import pwd
import subprocess
import sys
import tempfile
import unittest

from titan_brain.live.writer_lock import (
    AccountWriterLock,
    WriterLockBusy,
    user_account_writer_lock_directory,
)


class AccountWriterLockTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name) / "locks"

    def tearDown(self):
        self.temporary.cleanup()

    def test_production_lock_root_ignores_home_environment_override(self):
        expected_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(strict=True)
        prior = os.environ.get("HOME")
        try:
            os.environ["HOME"] = str(Path(self.temporary.name) / "attacker-home")
            self.assertEqual(
                user_account_writer_lock_directory(),
                expected_home
                / "Library/Application Support/Titan Momentum/account-writer-locks",
            )
        finally:
            if prior is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = prior

    def test_same_account_has_exactly_one_local_owner(self):
        first = AccountWriterLock(self.directory, "ending-7153", owner_id="owner-one")
        second = AccountWriterLock(self.directory, "ending-7153", owner_id="owner-two")
        first.acquire()
        try:
            with self.assertRaises(WriterLockBusy):
                second.acquire()
            metadata = first.holder_metadata()
            self.assertEqual(metadata["owner_id"], "owner-one")
            self.assertNotIn("ending-7153", first.path.read_text(encoding="utf-8"))
            self.assertEqual(first.path.stat().st_mode & 0o777, 0o600)
        finally:
            first.release()
        second.acquire()
        self.assertTrue(second.held)
        second.release()

    def test_distinct_accounts_use_distinct_locks(self):
        first = AccountWriterLock(self.directory, "ending-7153")
        second = AccountWriterLock(self.directory, "ending-0001")
        with first, second:
            self.assertTrue(first.held)
            self.assertTrue(second.held)
            self.assertNotEqual(first.path, second.path)

    def test_authorization_rotation_cannot_partition_account_lock(self):
        first = AccountWriterLock(
            self.directory,
            "ending-7153",
            owner_id="old-authorization",
            broker_account_binding_fingerprint="a" * 64,
            authorization_binding_id="b" * 64,
        )
        rotated = AccountWriterLock(
            self.directory,
            "ending-7153",
            owner_id="rotated-authorization",
            broker_account_binding_fingerprint="a" * 64,
            authorization_binding_id="c" * 64,
        )
        attended_or_installer = AccountWriterLock(
            self.directory, "ending-7153", owner_id="installer"
        )
        self.assertEqual(first.path, rotated.path)
        self.assertEqual(first.path, attended_or_installer.path)
        first.acquire()
        try:
            with self.assertRaises(WriterLockBusy):
                rotated.acquire()
            with self.assertRaises(WriterLockBusy):
                attended_or_installer.acquire()
        finally:
            first.release()

    def test_kernel_lock_blocks_a_separate_process(self):
        lock = AccountWriterLock(self.directory, "ending-7153")
        lock.acquire()
        script = """
import fcntl
import os
import sys
path = sys.argv[1]
descriptor = os.open(path, os.O_RDWR)
try:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(73)
    raise SystemExit(0)
finally:
    os.close(descriptor)
"""
        try:
            result = subprocess.run(
                [sys.executable, "-c", script, str(lock.path)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 73, result.stderr)
        finally:
            lock.release()

    def test_context_manager_releases_after_exception(self):
        first = AccountWriterLock(self.directory, "ending-7153")
        with self.assertRaisesRegex(RuntimeError, "boom"):
            with first:
                raise RuntimeError("boom")
        second = AccountWriterLock(self.directory, "ending-7153")
        with second:
            self.assertTrue(second.held)


if __name__ == "__main__":
    unittest.main()
