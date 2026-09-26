"""Synthetic durable execution/fee evidence; no network or credentials."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
import os
import sqlite3
import tempfile
import traceback
import unittest

from titan_brain.live.session_observation_ledger import SessionObservationLedger, SessionObservationLedgerError
from tests import test_live_session_trading_calculation as fixture


class SessionObservationLedgerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name).resolve() / "evidence.sqlite3"
        self.options = {"account_binding_sha256": fixture.ACCOUNT, "session_date": fixture.FROZEN.date()}
        self.ledger = SessionObservationLedger(self.path, create=True, **self.options)
        self.addCleanup(self.ledger.close)
        self.first, self.second = fixture.baseline_pair()

    def append(self, value):
        return self.ledger.append(value, expected_previous_receipt=self.ledger.head_receipt)

    def pair(self):
        self.append(self.first)
        self.append(self.second)

    def test_original_fills_signed_fees_timestamps_and_blockers_survive_reopen(self):
        self.pair()
        value = fixture.observation(executions=(fixture.execution(commission=Decimal("-0.25")),))
        tip = self.append(value)
        self.ledger.close()
        with SessionObservationLedger(self.path, **self.options) as reopened:
            self.assertEqual(reopened.history(), (self.first, self.second, value))
            self.assertEqual(reopened.head_receipt, tip)
            self.assertEqual(reopened.history()[-1].facts.executions[0].commission, Decimal("-0.25"))
            self.assertIn("ALL_CLIENT_EXECUTION_COVERAGE_UNPROVEN", reopened.history()[-1].blockers)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_empty_ledger_has_zero_tip(self):
        self.assertEqual(self.ledger.history(), ())
        self.assertEqual(self.ledger.head_receipt, "0" * 64)

    def test_stale_writer_cannot_fork_or_replace_baseline_evidence(self):
        self.append(self.first)
        with self.assertRaisesRegex(SessionObservationLedgerError, "STALE_WRITER"):
            self.ledger.append(self.second, expected_previous_receipt="0" * 64)
        self.assertEqual(self.ledger.history(), (self.first,))

    def test_create_never_overwrites_existing_file(self):
        self.append(self.first)
        with self.assertRaises(SessionObservationLedgerError):
            SessionObservationLedger(self.path, create=True, **self.options)
        self.assertEqual(self.ledger.history(), (self.first,))

    def test_wrong_account_day_generation_client_lineage_or_time_is_rejected(self):
        self.append(self.first)
        cases = (
            replace(self.second, account_binding_fingerprint="f" * 64),
            replace(self.second, read_client_id=999),
            replace(self.second, prior_collection_id="f" * 64),
            replace(self.second, facts=replace(self.second.facts, generation=2)),
            replace(self.second, facts=replace(self.second.facts, collection_started_at=fixture.FROZEN - timedelta(days=1))),
        )
        for value in cases:
            with self.subTest(value=value.facts.collection_id):
                with self.assertRaises(SessionObservationLedgerError):
                    self.append(value)
                self.assertEqual(self.ledger.history(), (self.first,))

    def test_external_sqlite_write_invalidates_cache_and_detects_old_payload_change(self):
        self.pair()
        self.ledger.history()
        with sqlite3.connect(self.path) as other:
            other.execute("UPDATE observations SET payload='{}' WHERE sequence=0")
        with self.assertRaises(SessionObservationLedgerError):
            self.ledger.history()

    def test_same_connection_change_is_not_hidden_by_cache(self):
        self.pair()
        self.ledger._db.execute("UPDATE observations SET previous_hash=? WHERE sequence=1", ("f" * 64,))
        with self.assertRaises(SessionObservationLedgerError):
            self.ledger.history()

    def test_external_append_is_loaded_before_stale_cas_check(self):
        tip = self.append(self.first)
        with SessionObservationLedger(self.path, **self.options) as other:
            other.append(self.second, expected_previous_receipt=tip)
        self.assertEqual(self.ledger.history(), (self.first, self.second))
        with self.assertRaisesRegex(SessionObservationLedgerError, "STALE_WRITER"):
            self.ledger.append(fixture.observation(), expected_previous_receipt=tip)

    def test_duplicate_collection_id_cannot_replace_old_record(self):
        self.pair()
        value = fixture.observation(collection_id=self.first.facts.collection_id)
        with self.assertRaises(SessionObservationLedgerError):
            self.append(value)
        self.assertEqual(self.ledger.history(), (self.first, self.second))

    def test_wrong_reopen_scope_and_additional_trigger_fail_closed(self):
        with self.assertRaises(SessionObservationLedgerError):
            SessionObservationLedger(self.path, **dict(self.options, account_binding_sha256="f" * 64))
        with sqlite3.connect(self.path) as other:
            other.execute("CREATE TRIGGER private_trigger AFTER INSERT ON observations BEGIN DELETE FROM observations; END")
        with self.assertRaises(SessionObservationLedgerError):
            self.ledger.history()

    def test_file_replacement_and_wide_permissions_fail_closed(self):
        os.chmod(self.path, 0o644)
        with self.assertRaises(SessionObservationLedgerError):
            self.ledger.history()
        os.chmod(self.path, 0o600)
        self.path.rename(self.path.with_suffix(".saved"))
        with SessionObservationLedger(self.path, create=True, **self.options):
            pass
        with self.assertRaisesRegex(SessionObservationLedgerError, "FILE_CHANGED"):
            self.ledger.history()

    def test_symlink_and_relative_paths_are_rejected(self):
        alias = self.path.with_name("alias.sqlite3")
        alias.symlink_to(self.path)
        for path in (alias, Path("relative.sqlite3")):
            with self.assertRaises(SessionObservationLedgerError):
                SessionObservationLedger(path, **self.options)

    def test_missing_parent_error_is_fixed_code_without_private_path(self):
        private_name = "synthetic-private-missing-parent"
        target = self.path.parent / private_name / "evidence.sqlite3"
        for create in (False, True):
            with self.subTest(create=create):
                with self.assertRaisesRegex(SessionObservationLedgerError, "OPEN_FAILED") as caught:
                    SessionObservationLedger(target, create=create, **self.options)
                self.assertNotIn(private_name, str(caught.exception))
                self.assertNotIn(private_name, "".join(traceback.format_exception(caught.exception)))
                self.assertFalse(target.parent.exists())

    def test_cached_append_rejects_weakened_synchronous_without_writing(self):
        tip = self.append(self.first)
        for setting in ("OFF", "NORMAL"):
            with self.subTest(setting=setting):
                self.ledger._db.execute(f"PRAGMA synchronous={setting}")
                with self.assertRaisesRegex(SessionObservationLedgerError, "DURABILITY_INVALID"):
                    self.ledger.append(self.second, expected_previous_receipt=tip)
                self.assertEqual(self.ledger._db.execute("SELECT COUNT(*) FROM observations").fetchone(), (1,))
                self.ledger._db.execute("PRAGMA synchronous=FULL")
                self.assertEqual(self.ledger.history(), (self.first,))

    def test_cached_append_rejects_unsupported_journal_modes_without_writing(self):
        tip = self.append(self.first)
        for setting in ("OFF", "MEMORY", "WAL"):
            with self.subTest(setting=setting):
                self.assertEqual(self.ledger._db.execute(f"PRAGMA journal_mode={setting}").fetchone(), (setting.lower(),))
                with self.assertRaisesRegex(SessionObservationLedgerError, "DURABILITY_INVALID"):
                    self.ledger.append(self.second, expected_previous_receipt=tip)
                self.assertEqual(self.ledger._db.execute("SELECT COUNT(*) FROM observations").fetchone(), (1,))
                self.ledger._db.execute("PRAGMA journal_mode=DELETE")
                self.assertEqual(self.ledger.history(), (self.first,))

    def test_cold_open_rejects_persistent_wal_mode_without_resetting_it(self):
        self.append(self.first)
        self.ledger.close()
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone(), ("wal",))
        with self.assertRaisesRegex(SessionObservationLedgerError, "DURABILITY_INVALID"):
            SessionObservationLedger(self.path, **self.options)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone(), ("wal",))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM observations").fetchone(), (1,))


if __name__ == "__main__":
    unittest.main()
