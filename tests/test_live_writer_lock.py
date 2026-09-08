from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from titan_brain.live.writer_lock import AccountWriterLock, WriterLockBusy


class AccountWriterLockTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name) / "locks"

    def tearDown(self):
        self.temporary.cleanup()

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
