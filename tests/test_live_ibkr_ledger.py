"""Offline, temporary-directory-only tests for the IBKR durable journal."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from uuid import uuid4


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from titan_brain.live.broker.ibkr_ledger import (  # noqa: E402
    IbkrExecutionLedger,
    IbkrLedgerConflict,
    IbkrLedgerTransitionError,
    request_fingerprint,
)


class IbkrLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "isolated-ibkr.sqlite3"
        self.binding = dict(account_fingerprint="a" * 64, environment="paper", client_id=42)
        self.ledger = IbkrExecutionLedger(self.path, **self.binding)
        self.addCleanup(self.ledger.close)
        self.ref = str(uuid4())
        self.session = str(uuid4())
        self.fingerprint = request_fingerprint({"symbol": "TEST", "side": "BUY", "quantity": 2, "price": "10.25"})
        self.stamp = datetime(2026, 9, 14, 8, tzinfo=timezone.utc)

    def reserve(self, ref=None, floor=10, session=None):
        return self.ledger.allocate_intent(ref or self.ref, self.fingerprint, session or self.session, floor)

    def fill(self, **overrides):
        args = dict(client_ref_id=self.ref, exec_id="0000.abc.01.01", quantity="1", price="10.25", perm_id=700, executed_at=self.stamp)
        args.update(overrides)
        return self.ledger.record_fill(**args)

    def test_database_is_private_and_bound(self):
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("PRAGMA application_id").fetchone()[0], 0x5449424B)
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_binding_rejects_account_environment_and_client_changes(self):
        for changes in ({"account_fingerprint": "b" * 64}, {"environment": "live"}, {"client_id": 43}):
            with self.subTest(changes=changes), self.assertRaises(IbkrLedgerConflict):
                IbkrExecutionLedger(self.path, **(self.binding | changes))

    def test_public_binding_is_read_only(self):
        for key, value in self.binding.items():
            self.assertEqual(getattr(self.ledger, key), value)
            with self.assertRaises(AttributeError):
                setattr(self.ledger, key, value)

    def test_binding_rejects_plain_account_and_master_client(self):
        for changes in ({"account_fingerprint": "raw-account"}, {"account_fingerprint": "A" * 64}, {"environment": "unknown"}, {"client_id": 0}, {"client_id": True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                IbkrExecutionLedger(Path(self.temp.name) / "bad.sqlite3", **(self.binding | changes))

    def test_existing_unrelated_database_unchanged(self):
        path = Path(self.temp.name) / "not-ibkr.sqlite3"
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE unrelated(value TEXT)")
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaises(IbkrLedgerConflict):
            IbkrExecutionLedger(path, **self.binding)
        self.assertEqual(before, hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertFalse(Path(str(path) + "-wal").exists())

    def test_unrelated_crash_left_wal_is_rejected_without_any_sqlite_mutation(self):
        live = Path(self.temp.name) / "foreign-live.sqlite3"
        crashed = Path(self.temp.name) / "foreign-crashed.sqlite3"
        with sqlite3.connect(live) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE unrelated(value TEXT)")
            connection.execute("INSERT INTO unrelated VALUES ('not-ours')")
            connection.commit()
            # A stable copy of the committed WAL fixture simulates os._exit
            # without creating a subprocess or letting SQLite close the copy.
            for suffix in ("", "-wal", "-shm"):
                shutil.copyfile(Path(str(live) + suffix), Path(str(crashed) + suffix))
        before = {suffix: Path(str(crashed) + suffix).read_bytes() for suffix in ("", "-wal", "-shm")}
        with self.assertRaises(IbkrLedgerConflict):
            IbkrExecutionLedger(crashed, **self.binding)
        self.assertEqual(before, {suffix: Path(str(crashed) + suffix).read_bytes() for suffix in before})

    def test_wrong_binding_rejection_does_not_touch_own_active_wal(self):
        self.reserve()
        before = {suffix: Path(str(self.path) + suffix).read_bytes() for suffix in ("", "-wal", "-shm")}
        with self.assertRaises(IbkrLedgerConflict):
            IbkrExecutionLedger(self.path, **(self.binding | {"environment": "live"}))
        self.assertEqual(before, {suffix: Path(str(self.path) + suffix).read_bytes() for suffix in before})

    def test_initial_identity_is_in_main_before_constructor_returns(self):
        header = self.path.read_bytes()[:100]
        self.assertEqual(int.from_bytes(header[68:72], "big"), 0x5449424B)
        self.assertEqual(int.from_bytes(header[60:64], "big"), 1)
        with sqlite3.connect(self.path.as_uri() + "?mode=ro&immutable=1", uri=True) as immutable:
            self.assertEqual(immutable.execute("SELECT environment FROM binding").fetchone()[0], "paper")

    def test_hardlinked_main_and_symlinked_or_hardlinked_sidecars_rejected(self):
        link = Path(self.temp.name) / "hardlink.sqlite3"
        link.hardlink_to(self.path)
        with self.assertRaises(IbkrLedgerConflict):
            IbkrExecutionLedger(link, **self.binding)
        link.unlink()
        self.reserve()
        wal = Path(str(self.path) + "-wal")
        alias = Path(self.temp.name) / "wal-alias"
        alias.hardlink_to(wal)
        with self.assertRaises(IbkrLedgerConflict):
            IbkrExecutionLedger(self.path, **self.binding)
        alias.unlink()
        other = Path(self.temp.name) / "no-main.sqlite3"
        Path(str(other) + "-wal").symlink_to(wal)
        with self.assertRaises(IbkrLedgerConflict):
            IbkrExecutionLedger(other, **self.binding)

    def test_orphan_wal_and_rollback_journal_are_not_implicitly_recovered(self):
        orphan = Path(self.temp.name) / "orphan.sqlite3"
        Path(str(orphan) + "-wal").touch()
        with self.assertRaises(IbkrLedgerConflict):
            IbkrExecutionLedger(orphan, **self.binding)
        self.assertFalse(orphan.exists())
        Path(str(self.path) + "-journal").touch()
        with self.assertRaises(IbkrLedgerConflict):
            IbkrExecutionLedger(self.path, **self.binding)

    def test_explicit_file_path_required(self):
        for path in (Path(":memory:"), Path("relative.sqlite3"), Path(self.temp.name) / "missing" / "journal.sqlite3"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                IbkrExecutionLedger(path, **self.binding)
        link = Path(self.temp.name) / "link.sqlite3"
        link.symlink_to(self.path)
        with self.assertRaises(ValueError):
            IbkrExecutionLedger(link, **self.binding)

    def test_existing_empty_file_is_not_adopted(self):
        path = Path(self.temp.name) / "empty.sqlite3"
        path.touch()
        with self.assertRaises(IbkrLedgerConflict):
            IbkrExecutionLedger(path, **self.binding)

    def test_canonical_request_hash(self):
        self.assertEqual(request_fingerprint({"b": 2, "a": ["10.25", True, None]}), request_fingerprint({"a": ["10.25", True, None], "b": 2}))
        self.assertNotEqual(request_fingerprint({"q": "1"}), request_fingerprint({"q": 1}))
        for payload in ({"p": 1.1}, {"p": Decimal("1.1")}, {1: "a"}, {"a": (1, 2)}, [1]):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                request_fingerprint(payload)

    def test_reservation_and_exact_local_lookup(self):
        self.assertIsNone(self.ledger.lookup(self.ref))
        record = self.reserve()
        self.assertEqual(record.order_id, 10)
        self.assertEqual(record.status, "RESERVED")
        self.assertTrue(record.can_transmit)
        self.assertEqual(record, self.ledger.lookup(self.ref))
        self.assertEqual(record.request_fingerprint, self.fingerprint)

    def test_order_id_lookup_is_local_read_only(self):
        self.assertIsNone(self.ledger.lookup_order_id(10))
        record = self.reserve()
        before = self.ledger._db.total_changes
        self.assertEqual(record, self.ledger.lookup_order_id(10))
        self.assertIsNone(self.ledger.lookup_order_id(11))
        self.assertEqual(before, self.ledger._db.total_changes)
        with self.assertRaises(ValueError):
            self.ledger.lookup_order_id(-1)

    def test_intent_duplicate_idempotent_across_reconnect(self):
        original = self.reserve()
        self.assertEqual(original, self.reserve(session=str(uuid4()), floor=50))
        self.assertEqual(self.reserve(ref=str(uuid4()), floor=1).order_id, 50)

    def test_duplicate_changed_request_fails(self):
        self.reserve()
        with self.assertRaises(IbkrLedgerConflict):
            self.ledger.allocate_intent(self.ref, "b" * 64, self.session, 100)
        self.assertEqual(self.reserve(ref=str(uuid4()), floor=0).order_id, 11)

    def test_order_ids_account_for_broker_local_and_observed_floor(self):
        self.assertEqual(self.reserve(floor=20).order_id, 20)
        self.assertEqual(self.reserve(ref=str(uuid4()), floor=0).order_id, 21)
        self.ledger.observe_order_id(100)
        self.ledger.observe_order_id(5)
        self.assertEqual(self.reserve(ref=str(uuid4()), floor=0).order_id, 101)
        self.assertEqual(self.reserve(ref=str(uuid4()), floor=200).order_id, 200)

    def test_ids_never_reused_after_restart(self):
        self.reserve(floor=100)
        self.ledger.mark_sending(self.ref)
        with IbkrExecutionLedger(self.path, **self.binding) as reopened:
            self.assertEqual(reopened.lookup(self.ref).status, "SENDING")
            self.assertEqual(reopened.allocate_intent(str(uuid4()), self.fingerprint, str(uuid4()), 1).order_id, 101)
            with self.assertRaises(IbkrLedgerTransitionError):
                reopened.mark_sending(self.ref)

    def test_concurrent_allocations_are_atomic(self):
        def allocate(index):
            with IbkrExecutionLedger(self.path, **self.binding) as ledger:
                return ledger.allocate_intent(str(uuid4()), self.fingerprint, self.session, 300).order_id
        with ThreadPoolExecutor(max_workers=6) as pool:
            ids = list(pool.map(allocate, range(24)))
        self.assertEqual(sorted(ids), list(range(300, 324)))

    def test_concurrent_same_reference_has_one_allocation(self):
        def allocate(index):
            with IbkrExecutionLedger(self.path, **self.binding) as ledger:
                return ledger.allocate_intent(self.ref, self.fingerprint, self.session, 300).order_id
        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(allocate, range(12)))
        self.assertEqual(set(ids), {300})
        self.assertEqual(self.reserve(ref=str(uuid4()), floor=0).order_id, 301)

    def test_same_instance_is_thread_safe_for_sdk_callback_threads(self):
        def allocate(index):
            return self.ledger.allocate_intent(str(uuid4()), self.fingerprint, self.session, 300).order_id
        with ThreadPoolExecutor(max_workers=6) as pool:
            ids = list(pool.map(allocate, range(24)))
        self.assertEqual(sorted(ids), list(range(300, 324)))
        self.reserve()
        self.ledger.mark_sending(self.ref)
        def callback(index):
            self.ledger.record_event(self.ref, f"ack-{index}", "ACK", perm_id=700)
            self.assertTrue(self.ledger.lookup(self.ref).acknowledgement_seen)
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(callback, range(24)))
        self.assertEqual(self.ledger.lookup(self.ref).perm_id, 700)

    def test_order_id_exhaustion_never_wraps(self):
        self.ledger.observe_order_id(2_147_483_647)
        with self.assertRaises(IbkrLedgerConflict):
            self.reserve(floor=0)

    def test_zero_broker_floor_never_allocates_zero(self):
        self.assertEqual(self.reserve(floor=0).order_id, 1)

    def test_invalid_order_ids_and_uuids_fail(self):
        for value in (-1, True, 1.5, 2_147_483_648):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.reserve(floor=value)
        with self.assertRaises(ValueError):
            self.reserve(ref="not-a-uuid")
        with self.assertRaises(ValueError):
            self.reserve(session="not-a-uuid")

    def test_mark_sending_is_committed_before_return_and_one_shot(self):
        self.reserve()
        record = self.ledger.mark_sending(self.ref)
        self.assertTrue(record.send_started)
        self.assertFalse(record.can_transmit)
        self.assertEqual(record.status, "SENDING")
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT send_started FROM intents").fetchone()[0], 1)
        with self.assertRaises(IbkrLedgerTransitionError):
            self.ledger.mark_sending(self.ref)

    def test_reserved_intent_can_be_terminally_aborted_but_never_sent(self):
        self.reserve()
        record = self.ledger.abort_reserved_intent(self.ref)
        self.assertEqual(record.status, "ABORTED")
        self.assertFalse(record.can_transmit)
        self.assertEqual(self.ledger.abort_reserved_intent(self.ref), record)
        with self.assertRaises(IbkrLedgerTransitionError):
            self.ledger.mark_sending(self.ref)

    def test_sent_or_broker_observed_intent_cannot_be_aborted(self):
        self.reserve()
        self.ledger.mark_sending(self.ref)
        with self.assertRaises(IbkrLedgerTransitionError):
            self.ledger.abort_reserved_intent(self.ref)

    def test_unknown_never_grants_retry_even_after_rejection_or_ack(self):
        self.reserve()
        self.ledger.mark_sending(self.ref)
        self.ledger.record_event(self.ref, "unknown", "UNKNOWN")
        self.ledger.record_event(self.ref, "rejected", "REJECT")
        self.assertEqual(self.ledger.lookup(self.ref).status, "UNKNOWN")
        self.ledger.record_event(self.ref, "acknowledged", "ACK", perm_id=700)
        record = self.ledger.lookup(self.ref)
        self.assertEqual(record.status, "ACK")
        self.assertTrue(record.submission_unknown_seen)
        self.assertTrue(record.rejection_seen)
        with self.assertRaises(IbkrLedgerTransitionError):
            self.ledger.mark_sending(self.ref)

    def test_broker_evidence_before_local_send_claim_blocks_transmission(self):
        self.reserve()
        self.ledger.record_event(self.ref, "unexpected-ack", "ACK", perm_id=700)
        with self.assertRaises(IbkrLedgerTransitionError):
            self.ledger.mark_sending(self.ref)

    def test_event_duplicate_exact_match_only(self):
        self.reserve()
        first = self.ledger.record_event(self.ref, "ack", "ACK", perm_id=700)
        self.assertEqual(first, self.ledger.record_event(self.ref, "ack", "ACK", perm_id=700))
        with self.assertRaises(IbkrLedgerConflict):
            self.ledger.record_event(self.ref, "ack", "REJECT", perm_id=700)
        with self.assertRaises(IbkrLedgerConflict):
            self.ledger.record_event(self.ref, "ack", "ACK", perm_id=701)

    def test_event_id_cannot_move_to_another_intent(self):
        self.reserve()
        other = str(uuid4())
        self.reserve(ref=other)
        self.ledger.record_event(self.ref, "ack", "ACK")
        with self.assertRaises(IbkrLedgerConflict):
            self.ledger.record_event(other, "ack", "ACK")

    def test_permanent_id_cannot_change_or_have_two_owners(self):
        self.reserve()
        other = str(uuid4())
        self.reserve(ref=other)
        self.ledger.record_event(self.ref, "ack", "ACK", perm_id=700)
        with self.assertRaises(IbkrLedgerConflict):
            self.ledger.record_event(self.ref, "changed", "ACK", perm_id=701)
        with self.assertRaises(IbkrLedgerConflict):
            self.ledger.record_event(other, "other", "ACK", perm_id=700)
        self.assertIsNone(self.ledger.lookup(other).perm_id)
        self.assertEqual(self.ledger.lookup(other).status, "RESERVED")

    def test_partial_fill_survives_cancel_unknown_then_late_events(self):
        self.reserve()
        self.ledger.mark_sending(self.ref)
        self.fill()
        self.ledger.record_event(self.ref, "cancel-start", "PENDING_CANCEL")
        record = self.ledger.record_event(self.ref, "cancel-unknown", "CANCEL_UNKNOWN")
        self.assertEqual(record.status, "CANCEL_UNKNOWN")
        self.assertTrue(record.acknowledgement_seen)
        self.assertEqual(record.fill_count, 1)
        self.assertFalse(record.submission_unknown_seen)
        self.ledger.record_event(self.ref, "cancelled", "CANCELLED", perm_id=700)
        self.ledger.record_event(self.ref, "late-ack", "ACK", perm_id=700)
        self.ledger.record_event(self.ref, "late-reject", "REJECT")
        self.fill(exec_id="0000.abc.02.01")
        record = self.ledger.lookup(self.ref)
        self.assertEqual(record.status, "CANCELLED")
        self.assertEqual(record.fill_count, 2)
        self.assertTrue(record.cancellation_unknown_seen)
        self.assertFalse(record.can_transmit)

    def test_cancel_claim_requires_sent_owned_intent_and_is_one_shot(self):
        self.reserve()
        self.assertFalse(self.ledger.lookup(self.ref).can_cancel)
        with self.assertRaises(IbkrLedgerTransitionError):
            self.ledger.claim_cancel(self.ref)
        self.ledger.mark_sending(self.ref)
        self.assertTrue(self.ledger.lookup(self.ref).can_cancel)
        claimed = self.ledger.claim_cancel(self.ref)
        self.assertEqual(claimed.status, "CANCEL_SENDING")
        self.assertTrue(claimed.cancel_started)
        self.assertFalse(claimed.can_cancel)
        with self.assertRaises(IbkrLedgerTransitionError):
            self.ledger.claim_cancel(self.ref)

    def test_cancel_claim_is_durable_and_unknown_or_late_ack_never_retries(self):
        self.reserve()
        self.ledger.mark_sending(self.ref)
        self.ledger.claim_cancel(self.ref)
        self.ledger.record_event(self.ref, "cancel-unknown", "CANCEL_UNKNOWN")
        self.ledger.record_event(self.ref, "late-ack", "ACK", perm_id=700)
        with IbkrExecutionLedger(self.path, **self.binding) as reopened:
            record = reopened.lookup(self.ref)
            self.assertTrue(record.cancel_started)
            self.assertTrue(record.acknowledgement_seen)
            self.assertEqual(record.status, "CANCEL_UNKNOWN")
            with self.assertRaises(IbkrLedgerTransitionError):
                reopened.claim_cancel(self.ref)

    def test_submit_no_wire_is_terminal_without_submission_uncertainty(self):
        self.reserve()
        self.ledger.mark_sending(self.ref)
        record = self.ledger.record_submit_not_sent(self.ref)
        self.assertEqual(record.status, "ABORTED")
        self.assertTrue(record.send_started)
        self.assertFalse(record.submission_unknown_seen)
        self.assertFalse(record.can_transmit)
        with self.assertRaises(IbkrLedgerTransitionError):
            self.ledger.mark_sending(self.ref)

    def test_submit_no_wire_rejects_all_contradictory_broker_evidence(self):
        for ordinal, kind in enumerate((
            "ACK",
            "REJECT",
            "UNKNOWN",
            "PENDING_CANCEL",
            "CANCEL_UNKNOWN",
            "CANCELLED",
        )):
            with self.subTest(kind=kind):
                ref = str(uuid4())
                self.reserve(ref=ref)
                self.ledger.mark_sending(ref)
                self.ledger.record_event(
                    ref,
                    f"contradictory-{kind.lower()}",
                    kind,
                    perm_id=700 + ordinal if kind == "ACK" else None,
                )
                with self.assertRaises(IbkrLedgerTransitionError):
                    self.ledger.record_submit_not_sent(ref)

        fill_ref = str(uuid4())
        self.reserve(ref=fill_ref)
        self.ledger.mark_sending(fill_ref)
        self.ledger.record_fill(
            fill_ref,
            "contradictory-fill",
            1,
            "10.00",
            800,
            datetime.now(timezone.utc),
        )
        with self.assertRaises(IbkrLedgerTransitionError):
            self.ledger.record_submit_not_sent(fill_ref)

    def test_cancel_no_wire_releases_only_the_local_cancel_claim(self):
        self.reserve()
        self.ledger.mark_sending(self.ref)
        self.ledger.record_event(self.ref, "ack", "ACK", perm_id=700)
        self.ledger.claim_cancel(self.ref)
        record = self.ledger.record_cancel_not_sent(self.ref)
        self.assertEqual(record.status, "ACK")
        self.assertTrue(record.cancel_started)
        self.assertFalse(record.cancel_claim_active)
        self.assertFalse(record.cancellation_unknown_seen)
        self.assertTrue(record.can_cancel)
        retried = self.ledger.claim_cancel(self.ref)
        self.assertEqual(retried.cancel_attempt_count, 2)
        self.assertFalse(retried.can_cancel)

    def test_cancel_unknown_retry_requires_strictly_newer_positive_evidence(self):
        self.reserve()
        self.ledger.mark_sending(self.ref)
        self.ledger.record_event(self.ref, "ack", "ACK", perm_id=700)
        self.ledger.claim_cancel(self.ref)
        self.ledger.record_event(self.ref, "cancel-unknown", "CANCEL_UNKNOWN")
        with self.assertRaises(IbkrLedgerTransitionError):
            self.ledger.authorize_cancel_retry(
                self.ref,
                evidence_id="b" * 64,
                evidence_received_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            )
        evidence_at = datetime.now(timezone.utc) + timedelta(seconds=2)
        authorized = self.ledger.authorize_cancel_retry(
            self.ref,
            evidence_id="c" * 64,
            evidence_received_at=evidence_at,
        )
        self.assertEqual(authorized.status, "ACK")
        self.assertFalse(authorized.cancellation_unknown_seen)
        self.assertTrue(authorized.can_cancel)
        claimed = self.ledger.claim_cancel(self.ref)
        self.assertEqual(claimed.cancel_attempt_count, 2)
        self.assertEqual(claimed.status, "CANCEL_SENDING")

    def test_cancel_claim_rejects_prior_terminal_or_broker_cancellation_evidence(self):
        for kind in ("REJECT", "CANCELLED", "PENDING_CANCEL", "CANCEL_UNKNOWN"):
            with self.subTest(kind=kind):
                ref = str(uuid4())
                self.reserve(ref=ref)
                self.ledger.mark_sending(ref)
                self.ledger.record_event(ref, str(uuid4()), kind)
                with self.assertRaises(IbkrLedgerTransitionError):
                    self.ledger.claim_cancel(ref)

    def test_contradictory_rejection_evidence_does_not_grant_cancel_claim(self):
        for prior in ("ACK", "UNKNOWN"):
            with self.subTest(prior=prior):
                ref = str(uuid4())
                self.reserve(ref=ref)
                self.ledger.mark_sending(ref)
                self.ledger.record_event(ref, str(uuid4()), prior)
                self.ledger.record_event(ref, str(uuid4()), "REJECT")
                self.assertFalse(self.ledger.lookup(ref).can_cancel)
                with self.assertRaises(IbkrLedgerTransitionError):
                    self.ledger.claim_cancel(ref)

    def test_parallel_cancel_claims_have_one_winner(self):
        self.reserve()
        self.ledger.mark_sending(self.ref)
        def claim(index):
            with IbkrExecutionLedger(self.path, **self.binding) as ledger:
                try:
                    ledger.claim_cancel(self.ref)
                    return True
                except IbkrLedgerTransitionError:
                    return False
        with ThreadPoolExecutor(max_workers=6) as pool:
            outcomes = list(pool.map(claim, range(18)))
        self.assertEqual(outcomes.count(True), 1)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM events WHERE kind='CANCEL_SENDING'").fetchone()[0], 1)

    def test_cancelled_first_does_not_lose_late_fill(self):
        self.reserve()
        self.ledger.record_event(self.ref, "cancelled", "CANCELLED")
        self.fill()
        record = self.ledger.lookup(self.ref)
        self.assertEqual(record.status, "CANCELLED")
        self.assertTrue(record.acknowledgement_seen)
        self.assertEqual(record.fill_count, 1)

    def test_fill_duplicate_normalized_exact_match_only(self):
        self.reserve()
        original = self.fill()
        self.assertEqual(original, self.fill(quantity=Decimal("1.00"), price="10.250", executed_at=self.stamp.astimezone(timezone(timedelta(hours=-4)))))
        self.assertEqual(len(self.ledger.fills(self.ref)), 1)
        for changes in ({"quantity": "2"}, {"price": "10.26"}, {"perm_id": 701}, {"executed_at": self.stamp + timedelta(seconds=1)}):
            with self.subTest(changes=changes), self.assertRaises(IbkrLedgerConflict):
                self.fill(**changes)
        self.assertEqual(self.ledger.fills(self.ref), (original,))

    def test_fill_identity_cannot_move_to_another_intent(self):
        self.reserve()
        other = str(uuid4())
        self.reserve(ref=other)
        self.fill()
        with self.assertRaises(IbkrLedgerConflict):
            self.fill(client_ref_id=other)

    def test_fill_requires_exact_finite_positive_values(self):
        self.reserve()
        for changes in ({"quantity": "0"}, {"quantity": "-1"}, {"quantity": "NaN"}, {"price": "Infinity"}, {"price": 1.2}, {"price": True}, {"price": "1e100"}, {"perm_id": None}, {"perm_id": 0}, {"executed_at": self.stamp.replace(tzinfo=None)}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.fill(**changes)
        self.assertEqual(self.ledger.fills(self.ref), ())

    def test_commission_corrections_are_append_only_not_added_to_fills(self):
        self.reserve()
        fill = self.fill()
        original = self.ledger.record_commission(fill.exec_id, "fee-1", ".35", "USD")
        corrected = self.ledger.record_commission(fill.exec_id, "fee-2", ".42", "USD")
        rebate = self.ledger.record_commission(fill.exec_id, "fee-3", "-.01", "USD")
        self.assertLess(original.ordinal, corrected.ordinal)
        self.assertEqual(self.ledger.commissions(fill.exec_id), (original, corrected, rebate))
        self.assertEqual(self.ledger.fills(self.ref), (fill,))

    def test_duplicate_commission_exact_match_and_unknown_exec_rejected(self):
        self.reserve()
        fill = self.fill()
        first = self.ledger.record_commission(fill.exec_id, "fee-1", ".35", "USD")
        self.assertEqual(first, self.ledger.record_commission(fill.exec_id, "fee-1", Decimal("0.3500"), "USD"))
        with self.assertRaises(IbkrLedgerConflict):
            self.ledger.record_commission(fill.exec_id, "fee-1", ".36", "USD")
        with self.assertRaises(IbkrLedgerConflict):
            self.ledger.record_commission("unknown-exec", "fee-2", ".35", "USD")
        self.assertEqual(len(self.ledger.commissions(fill.exec_id)), 1)

    def test_unknown_intent_and_freeform_event_fail(self):
        with self.assertRaises(IbkrLedgerConflict):
            self.ledger.mark_sending(self.ref)
        with self.assertRaises(IbkrLedgerConflict):
            self.ledger.record_event(self.ref, "ack", "ACK")
        self.reserve()
        with self.assertRaises(ValueError):
            self.ledger.record_event(self.ref, "invalid", "broker error text")
        with self.assertRaises(ValueError):
            self.ledger.record_event(self.ref, "unbounded free text", "UNKNOWN")

    def test_request_payload_is_not_stored(self):
        self.reserve()
        with sqlite3.connect(self.path) as connection:
            rows = list(connection.iterdump())
        dump = "\n".join(rows)
        self.assertNotIn("TEST", dump)
        self.assertNotIn("10.25", dump)
        self.assertIn(self.fingerprint, dump)


if __name__ == "__main__":
    unittest.main()
