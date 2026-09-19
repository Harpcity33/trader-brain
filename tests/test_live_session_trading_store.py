"""Temporary-database tests only; no production state, network or credentials."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import selectors
import stat
import subprocess
import sys
import tempfile
import time
from threading import Barrier
import unittest
from unittest.mock import patch

from titan_brain.live import session_trading_store as store_module
from titan_brain.live.session_trading_policy import (
    MODEL, ObservationStatus, SessionTradingBaseline, SessionTradingMeasurement,
    evaluate_session_state, load_session_trading_policy_from_root,
)
from titan_brain.live.session_trading_store import (
    SessionTradingStore, SessionTradingStoreConflict, SessionTradingStoreError, StoredSession,
)


ROOT = Path(__file__).resolve().parents[1]
START = datetime(2026, 9, 18, 13, 30, tzinfo=timezone.utc)
DAY = date(2026, 9, 18)
ACCOUNT = "a" * 64
PRIVATE = "synthetic-private-financial-and-sqlite-message"
D = Decimal


def _full_session_benchmark(path):
    """Real temporary SQLite, FULL sync; no broker and no mocked disk commits."""
    policy = load_session_trading_policy_from_root(ROOT)
    baseline = SessionTradingBaseline(
        policy_sha256=policy.policy_sha256, account_binding_sha256=ACCOUNT,
        evidence_sha256="b" * 64, frozen_at=START, starting_nlv=D("10000"),
        flat_start=True, pre_entry=True, initial_exposure_reconciled=True,
    )
    counts = {"apply": 0}
    original = store_module._apply

    def apply(*args):
        counts["apply"] += 1
        return original(*args)

    started = time.monotonic()
    with patch.object(store_module, "_apply", side_effect=apply):
        with SessionTradingStore(path, create=True) as store:
            stored = store.start_session(policy, baseline)
            for number in range(11_700):
                now, token = START + timedelta(seconds=number * 2), f"cadence-{number}"
                stored = store.begin_observation(
                    account_binding_sha256=ACCOUNT, session_date=DAY,
                    expected_revision=stored.revision, token=token, now=now)
                measurement = SessionTradingMeasurement(
                    model=MODEL, account_binding_sha256=ACCOUNT,
                    baseline_identity_sha256=baseline.identity_sha256,
                    evidence_sha256="c" * 64, as_of=now, received_at=now,
                    session_pnl=D("-1000") if number == 100 else D("0"), complete=True,
                )
                stored = store.complete_observation(
                    account_binding_sha256=ACCOUNT, session_date=DAY,
                    expected_revision=stored.revision, token=token, measurement=measurement, now=now)
            written, append_apply_count = stored, counts["apply"]
        append_seconds = time.monotonic() - started
        counts["apply"] = 0
        started = time.monotonic()
        with SessionTradingStore(path) as store:
            recovered = store.load(account_binding_sha256=ACCOUNT, session_date=DAY)
        replay_seconds = time.monotonic() - started
    with sqlite3.connect(path) as connection:
        events = connection.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
        tokens = connection.execute("SELECT COUNT(*) FROM observation_tokens").fetchone()[0]
    return {
        "event_count": events, "token_count": tokens, "revision": recovered.revision,
        "projection_size": len(recovered.state.incidents), "loss_latched": recovered.state.loss_latched,
        "restart_equal": recovered == written, "append_apply_count": append_apply_count,
        "replay_apply_count": counts["apply"], "append_seconds": append_seconds,
        "cold_replay_seconds": replay_seconds, "database_bytes": path.stat().st_size,
    }


class SessionTradingStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name).resolve() / "synthetic-session.sqlite3"
        self.policy = load_session_trading_policy_from_root(ROOT)
        self.baseline = SessionTradingBaseline(
            policy_sha256=self.policy.policy_sha256, account_binding_sha256=ACCOUNT,
            evidence_sha256="b" * 64, frozen_at=START, starting_nlv=D("10000"),
            flat_start=True, pre_entry=True, initial_exposure_reconciled=True,
        )
        self.store = SessionTradingStore(self.path, create=True)
        self.addCleanup(self.close)
        self.no_network = patch("socket.socket.connect", side_effect=AssertionError("network forbidden"))
        self.no_network.start()
        self.addCleanup(self.no_network.stop)

    def close(self):
        if self.store is not None:
            self.store.close()
            self.store = None

    def reopen(self):
        self.close()
        self.store = SessionTradingStore(self.path)
        return self.load()

    def load(self):
        return self.store.load(account_binding_sha256=ACCOUNT, session_date=DAY)

    def start(self):
        return self.store.start_session(self.policy, self.baseline)

    def begin(self, stored, token="read-1", now=START):
        return self.store.begin_observation(
            account_binding_sha256=ACCOUNT, session_date=DAY,
            expected_revision=stored.revision, token=token, now=now)

    def measurement(self, stored, pnl="0", now=START, **changes):
        value = SessionTradingMeasurement(
            model=MODEL, account_binding_sha256=ACCOUNT,
            baseline_identity_sha256=stored.state.baseline.identity_sha256,
            evidence_sha256="c" * 64, as_of=now, received_at=now,
            session_pnl=None if pnl is None else D(pnl), complete=pnl is not None,
        )
        return replace(value, **changes)

    def complete(self, stored, pnl="0", token="read-1", now=START, **changes):
        return self.store.complete_observation(
            account_binding_sha256=ACCOUNT, session_date=DAY,
            expected_revision=stored.revision, token=token,
            measurement=self.measurement(stored, pnl, now, **changes), now=now)

    def fail(self, stored, token="read-1", now=START, gap=False):
        return self.store.fail_observation(
            account_binding_sha256=ACCOUNT, session_date=DAY,
            expected_revision=stored.revision, token=token, now=now, gap=gap)

    def sql(self, statement, values=()):
        with sqlite3.connect(self.path) as connection:
            return connection.execute(statement, values).fetchall()

    def assert_rejected_on_load_and_open(self):
        with self.assertRaises(SessionTradingStoreError):
            self.load()
        self.close()
        with self.assertRaises(SessionTradingStoreError):
            SessionTradingStore(self.path)

    def test_creation_is_explicit_create_only_private_and_context_managed(self):
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertIsNone(self.load())
        with self.assertRaises(SessionTradingStoreError):
            SessionTradingStore(self.path, create=True)
        with self.assertRaises(SessionTradingStoreError):
            SessionTradingStore(self.path.with_name("missing.sqlite3"))
        with SessionTradingStore(self.path) as opened:
            self.assertIsNone(opened.load(account_binding_sha256=ACCOUNT, session_date=DAY))
        with self.assertRaisesRegex(SessionTradingStoreError, "CLOSED"):
            opened.load(account_binding_sha256=ACCOUNT, session_date=DAY)
        opened.close()

    def test_initial_baseline_and_zero_revision_survive_reopen(self):
        started = self.start()
        self.assertEqual(started.revision, 0)
        self.assertEqual(started.session_date, DAY)
        self.assertEqual(started.state.baseline, self.baseline)
        self.assertEqual(self.reopen(), started)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM audit"), [(1,)])

    def test_start_is_idempotent_not_a_same_day_reset_after_loss(self):
        loss = self.complete(self.begin(self.start()), "-1000")
        self.reopen()
        retry = self.start()
        self.assertEqual(retry, loss)
        self.assertTrue(retry.state.loss_latched)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM sessions"), [(1,)])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM audit"), [(3,)])

    def test_same_account_day_rejects_new_balance_evidence_freeze_or_policy(self):
        initial = self.start()
        for baseline, policy in (
            (replace(self.baseline, starting_nlv=D("20000")), self.policy),
            (replace(self.baseline, evidence_sha256="d" * 64), self.policy),
            (replace(self.baseline, frozen_at=START + timedelta(minutes=1)), self.policy),
            (replace(self.baseline, policy_sha256="e" * 64), replace(self.policy, policy_sha256="e" * 64)),
            (self.baseline, replace(self.policy, amendment_sha256="f" * 64)),
        ):
            with self.subTest(baseline=baseline.identity_sha256), self.assertRaises(SessionTradingStoreConflict):
                self.store.start_session(policy, baseline)
            self.assertEqual(self.load(), initial)

    def test_scope_is_account_and_new_york_day_not_utc_day(self):
        self.start()
        same_ny_day = replace(self.baseline, frozen_at=datetime(2026, 9, 19, 1, 30, tzinfo=timezone.utc))
        with self.assertRaises(SessionTradingStoreConflict):
            self.store.start_session(self.policy, same_ny_day)
        new_day = replace(self.baseline, frozen_at=START + timedelta(days=1))
        newer = self.store.start_session(self.policy, new_day)
        self.assertEqual(newer.session_date, DAY + timedelta(days=1))
        other = replace(self.baseline, account_binding_sha256="d" * 64)
        self.store.start_session(self.policy, other)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM sessions"), [(3,)])

    def test_decimal_scale_timezone_and_low_context_do_not_change_identity(self):
        initial = self.start()
        equivalent = replace(self.baseline, starting_nlv=D("1.0000E4"), frozen_at=START.astimezone(timezone(timedelta(hours=-4))))
        with localcontext() as context:
            context.prec = 2
            self.assertEqual(self.store.start_session(self.policy, equivalent), initial)
            pending = self.begin(initial)
            result = self.complete(pending, "-999.990000")
        reloaded = self.reopen()
        self.assertEqual(result, reloaded)
        self.assertEqual(reloaded.state.last_measurement.session_pnl, D("-999.99"))
        self.assertEqual(evaluate_session_state(reloaded.state, now=START).aggregate_headroom_before_exposure_and_reserves, D("0.01"))
        encoded = self.sql("SELECT state_json FROM sessions")[0][0]
        self.assertNotIn("999.990000", encoded)
        self.assertIn("2026-09-18T13:30:00.000000+00:00", encoded)

    def test_pending_is_committed_and_visible_before_read_and_after_restart(self):
        pending = self.begin(self.start())
        with SessionTradingStore(self.path) as reader:
            observed = reader.load(account_binding_sha256=ACCOUNT, session_date=DAY)
        self.assertEqual(observed, pending)
        self.assertIs(self.reopen().state.incidents[0].status, ObservationStatus.PENDING)
        self.assertIn("UNRESOLVED_OBSERVATION_INCIDENT", evaluate_session_state(pending.state, now=START).entry_blockers)

    def test_pending_from_abandoned_read_is_not_cleared_by_later_success(self):
        self.begin(self.start(), token="abandoned")
        prior = self.reopen()
        now = START + timedelta(seconds=1)
        recovered = self.complete(self.begin(prior, token="healthy", now=now), token="healthy", now=now)
        self.assertIs(recovered.state.incidents[0].status, ObservationStatus.PENDING)
        self.assertIs(recovered.state.incidents[1].status, ObservationStatus.COMPLETED)
        self.assertIn("UNRESOLVED_OBSERVATION_INCIDENT", evaluate_session_state(recovered.state, now=now).entry_blockers)

    def test_failed_and_gap_incidents_remain_sticky_across_reload_and_health(self):
        stored = self.start()
        for number, gap in enumerate((False, True)):
            token = f"failed-{number}"
            now = START + timedelta(seconds=number)
            stored = self.fail(self.begin(stored, token=token, now=now), token=token, now=now, gap=gap)
            stored = self.reopen()
        now = START + timedelta(seconds=2)
        stored = self.complete(self.begin(stored, token="healthy", now=now), token="healthy", now=now)
        self.assertEqual([item.reason for item in stored.state.incidents[:2]], ["MISSING_DATA", "GAP"])
        with self.assertRaises(SessionTradingStoreError):
            self.complete(stored, token="failed-0", now=now)
        self.assertEqual(self.load(), stored)

    def test_observed_loss_and_profit_latches_cannot_clear_on_recovery_or_reload(self):
        stored = self.complete(self.begin(self.start()), "-1000")
        self.assertTrue(stored.state.loss_latched)
        stored = self.reopen()
        now = START + timedelta(seconds=1)
        stored = self.complete(self.begin(stored, token="gain", now=now), "5000", token="gain", now=now)
        self.assertTrue(stored.state.loss_latched)
        self.assertTrue(stored.state.profit_aspiration_observed)
        self.assertTrue(evaluate_session_state(self.reopen().state, now=now).guarded_closeout_required)

    def test_incomplete_wrong_binding_and_malformed_measurements_persist_failed_incident(self):
        stored = self.start()
        for number, kind in enumerate(("missing", "binding", "object")):
            now = START + timedelta(seconds=number)
            token = f"invalid-{number}"
            pending = self.begin(stored, token=token, now=now)
            value = (self.measurement(pending, None, now) if kind == "missing" else
                     self.measurement(pending, now=now, account_binding_sha256="f" * 64) if kind == "binding" else object())
            stored = self.store.complete_observation(
                account_binding_sha256=ACCOUNT, session_date=DAY, expected_revision=pending.revision,
                token=token, measurement=value, now=now)
            self.assertIs(stored.state.incidents[-1].status, ObservationStatus.FAILED)
        self.assertIsNone(self.reopen().state.last_measurement)

    def test_stale_time_and_same_time_equivocation_do_not_overwrite_measurement(self):
        stored = self.complete(self.begin(self.start()), "1")
        pending = self.begin(stored, token="conflict")
        conflicted = self.complete(pending, "2", token="conflict")
        self.assertEqual(conflicted.state.incidents[-1].reason, "NONMONOTONE")
        self.assertEqual(conflicted.state.last_measurement.session_pnl, D("1"))
        pending = self.begin(conflicted, token="slow")
        stale = self.store.complete_observation(
            account_binding_sha256=ACCOUNT, session_date=DAY, expected_revision=pending.revision,
            token="slow", measurement=self.measurement(pending), now=START + timedelta(seconds=6))
        self.assertEqual(stale.state.incidents[-1].reason, "TIME")
        self.assertEqual(stale.state.last_measurement.session_pnl, D("1"))

    def test_cas_rejects_stale_writer_without_appending_audit(self):
        initial = self.start()
        with SessionTradingStore(self.path) as other:
            pending = self.begin(initial)
            with self.assertRaisesRegex(SessionTradingStoreConflict, "STALE_REVISION"):
                other.begin_observation(account_binding_sha256=ACCOUNT, session_date=DAY,
                                        expected_revision=initial.revision, token="stale", now=START)
        self.assertEqual(self.load(), pending)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM audit"), [(2,)])

    def test_two_simultaneous_connections_cannot_both_win_same_revision(self):
        initial = self.start()
        barrier = Barrier(2)
        with SessionTradingStore(self.path) as other:
            def attempt(store, token):
                barrier.wait(timeout=5)
                try:
                    return store.begin_observation(account_binding_sha256=ACCOUNT, session_date=DAY,
                                                   expected_revision=initial.revision, token=token, now=START)
                except SessionTradingStoreConflict as exc:
                    return exc
            with ThreadPoolExecutor(max_workers=2) as workers:
                results = [future.result(timeout=10) for future in (
                    workers.submit(attempt, self.store, "writer-a"), workers.submit(attempt, other, "writer-b"))]
        self.assertEqual(sum(isinstance(item, StoredSession) for item in results), 1)
        self.assertEqual(sum(isinstance(item, SessionTradingStoreConflict) for item in results), 1)
        self.assertEqual(self.load().revision, 1)

    def test_audit_insert_failure_rolls_back_materialized_update_and_is_redacted(self):
        prior = self.start()
        with patch.object(self.store, "_insert_audit", side_effect=sqlite3.IntegrityError(PRIVATE)):
            with self.assertRaises(SessionTradingStoreError) as caught:
                self.begin(prior)
        self.assertNotIn(PRIVATE, str(caught.exception))
        self.assertEqual(self.reopen(), prior)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM audit"), [(1,)])

    def test_failed_initial_transaction_leaves_no_baseline_or_orphan_audit(self):
        with patch.object(self.store, "_insert_audit", side_effect=sqlite3.IntegrityError(PRIVATE)):
            with self.assertRaises(SessionTradingStoreError):
                self.start()
        self.assertIsNone(self.reopen())
        self.assertEqual(self.sql("SELECT COUNT(*) FROM audit"), [(0,)])
        self.assertEqual(self.start().revision, 0)

    def test_keyboard_interrupt_rolls_back_and_context_closes_connection(self):
        prior = self.start()
        with self.assertRaises(KeyboardInterrupt):
            with SessionTradingStore(self.path) as interrupted:
                with patch.object(interrupted, "_insert_audit", side_effect=KeyboardInterrupt):
                    interrupted.begin_observation(account_binding_sha256=ACCOUNT, session_date=DAY,
                                                  expected_revision=prior.revision, token="interrupted", now=START)
        with self.assertRaisesRegex(SessionTradingStoreError, "CLOSED"):
            interrupted.load(account_binding_sha256=ACCOUNT, session_date=DAY)
        self.assertEqual(self.reopen(), prior)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM audit"), [(1,)])

    def test_killed_child_keeps_committed_pending_marker_without_graceful_close(self):
        self.start()
        script = (
            "import sys; from pathlib import Path; from datetime import date,datetime; "
            "sys.path.insert(0, sys.argv[1]); "
            "from titan_brain.live.session_trading_store import SessionTradingStore; "
            "s=SessionTradingStore(Path(sys.argv[2])); "
            "r=s.load(account_binding_sha256='a'*64,session_date=date(2026,9,18)); "
            "s.begin_observation(account_binding_sha256='a'*64,session_date=date(2026,9,18),"
            "expected_revision=r.revision,token='child-pending',now=datetime.fromisoformat('2026-09-18T13:30:00+00:00')); "
            "print('MARKER_COMMITTED',flush=True); sys.stdin.read()"
        )
        # Real child process, but only this test's synthetic temporary database.
        # Killing it intentionally skips context exit/destructors/SDK cleanup.
        child = subprocess.Popen([sys.executable, "-I", "-S", "-B", "-c", script,
                                  str(ROOT / "src"), str(self.path)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                self.assertTrue(selector.select(timeout=5), "synthetic child did not commit in time")
                self.assertEqual(child.stdout.readline(), "MARKER_COMMITTED\n")
            child.kill()
            output, error = child.communicate(timeout=5)
            self.assertLess(child.returncode, 0)
            self.assertEqual((output, error), ("", ""))
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)
        recovered = self.reopen()
        self.assertEqual(recovered.revision, 1)
        self.assertIs(recovered.state.incidents[0].status, ObservationStatus.PENDING)
        self.assertIsNone(recovered.state.last_measurement)
        self.assertIn("UNRESOLVED_OBSERVATION_INCIDENT", evaluate_session_state(recovered.state, now=START).entry_blockers)

    def test_first_creation_fsyncs_parent_directory_existing_open_does_not(self):
        target = self.path.with_name("fsync.sqlite3")
        calls = []
        original = os.fsync

        def fsync(descriptor):
            calls.append(stat.S_ISDIR(os.fstat(descriptor).st_mode))
            return original(descriptor)

        with patch.object(store_module.os, "fsync", side_effect=fsync):
            with SessionTradingStore(target, create=True):
                pass
            with SessionTradingStore(target):
                pass
        self.assertEqual(calls, [True])

    def test_resource_cap_is_fail_closed_and_does_not_start_unfinishable_read(self):
        with patch.object(store_module, "_MAX_EVENTS", 5):
            stored = self.complete(self.begin(self.start()))
            now = START + timedelta(seconds=1)
            stored = self.complete(self.begin(stored, token="second", now=now), token="second", now=now)
            self.assertEqual(stored.revision, 4)
            with self.assertRaisesRegex(SessionTradingStoreError, "EVENT_LIMIT_REACHED"):
                self.begin(stored, token="no-room", now=now)
            self.assertEqual(self.load(), stored)
            self.assertEqual(self.sql("SELECT COUNT(*) FROM audit"), [(5,)])

    def test_resource_cap_reserves_finish_slots_for_multiple_pending_reads(self):
        with patch.object(store_module, "_MAX_EVENTS", 5):
            stored = self.begin(self.start(), token="first")
            stored = self.begin(stored, token="second")
            with self.assertRaisesRegex(SessionTradingStoreError, "EVENT_LIMIT_REACHED"):
                self.begin(stored, token="third")
            stored = self.fail(stored, token="first")
            stored = self.fail(stored, token="second")
            self.assertEqual(stored.revision, 4)
            self.assertEqual([item.status for item in stored.state.incidents], [ObservationStatus.FAILED] * 2)

    def test_completed_history_is_archived_without_token_reuse_or_loss_reset(self):
        stored = self.start()
        for number in range(10):
            now, token = START + timedelta(seconds=number), f"history-{number}"
            stored = self.complete(self.begin(stored, token=token, now=now),
                                   "-1000" if number == 0 else "0", token=token, now=now)
        self.assertEqual(len(stored.state.incidents), 1)
        self.assertEqual(stored.state.incidents[0].token, "history-9")
        self.assertTrue(stored.state.loss_latched)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM audit"), [(21,)])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM observation_tokens"), [(10,)])
        events = [json.loads(row[0]) for row in self.sql("SELECT event_json FROM audit ORDER BY revision")]
        self.assertEqual([event["payload"]["token"] for event in events if event["operation"] == "complete"],
                         [f"history-{number}" for number in range(10)])
        self.assertEqual(self.reopen(), stored)
        with self.assertRaisesRegex(SessionTradingStoreError, "TOKEN_REUSED"):
            self.begin(stored, token="history-0", now=now)
        self.assertEqual(self.load(), stored)

    def test_unresolved_projection_bound_preserves_pending_and_failed_reads(self):
        with patch.object(store_module, "_MAX_UNRESOLVED", 2):
            stored = self.begin(self.start(), token="first")
            stored = self.begin(stored, token="second")
            with self.assertRaisesRegex(SessionTradingStoreError, "UNRESOLVED_LIMIT_REACHED"):
                self.begin(stored, token="third")
            stored = self.fail(stored, token="first", gap=True)
            stored = self.fail(stored, token="second")
            with self.assertRaisesRegex(SessionTradingStoreError, "UNRESOLVED_LIMIT_REACHED"):
                self.begin(stored, token="later")
            self.assertEqual(self.reopen(), stored)
            self.assertEqual([item.reason for item in stored.state.incidents], ["GAP", "MISSING_DATA"])

    def test_verified_tip_does_not_replay_unchanged_history(self):
        stored = self.complete(self.begin(self.start()))
        with patch.object(store_module, "_apply", side_effect=AssertionError("unexpected replay")):
            self.assertEqual(self.load(), stored)
            self.assertEqual(self.load(), stored)
        # A file metadata change invalidates even an otherwise unchanged SQLite
        # data_version. Revalidation is allowed; old bytes are not blindly trusted.
        info = self.path.stat()
        os.utime(self.path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000))
        with patch.object(store_module, "_apply", wraps=store_module._apply) as replay:
            self.assertEqual(self.load(), stored)
            self.assertEqual(replay.call_count, 3)

    def test_external_valid_append_invalidates_cached_tip_and_replays(self):
        initial = self.start()
        with SessionTradingStore(self.path) as other:
            pending = other.begin_observation(account_binding_sha256=ACCOUNT, session_date=DAY,
                                             expected_revision=initial.revision, token="external", now=START)
        with patch.object(store_module, "_apply", wraps=store_module._apply) as replay:
            self.assertEqual(self.load(), pending)
            self.assertEqual(replay.call_count, 2)
        self.assertEqual(self.complete(pending, token="external").revision, 2)

    def test_corrupt_archived_event_invalidates_live_cache(self):
        stored = self.complete(self.begin(self.start()))
        now = START + timedelta(seconds=1)
        self.complete(self.begin(stored, token="later", now=now), token="later", now=now)
        self.sql("UPDATE audit SET event_sha256=? WHERE revision=1", ("0" * 64,))
        self.assert_rejected_on_load_and_open()

    def test_unexpected_private_connection_write_invalidates_cache(self):
        self.complete(self.begin(self.start()))
        # Not a public API: defend against accidental future internal raw SQL too.
        self.store._db.execute("UPDATE audit SET event_sha256=? WHERE revision=0", ("0" * 64,))
        self.assert_rejected_on_load_and_open()

    def test_missing_or_changed_archived_token_index_is_detected(self):
        self.complete(self.begin(self.start()))
        self.sql("DELETE FROM observation_tokens")
        self.assert_rejected_on_load_and_open()

    def test_v1_identity_is_rejected_without_migration_or_reset(self):
        prior = self.start()
        self.sql("PRAGMA user_version=1")
        self.assert_rejected_on_load_and_open()
        self.assertEqual(self.sql("SELECT revision,audit_head_sha256 FROM sessions"),
                         [(prior.revision, prior.audit_head_sha256)])

    def test_full_session_two_second_cadence_has_bounded_append_and_linear_replay(self):
        target = self.path.with_name("full-session.sqlite3")
        code = (
            "import json,socket,sys; from pathlib import Path; "
            "sys.path[:0]=[sys.argv[1],sys.argv[2]]; "
            "socket.socket.connect=lambda *a,**k: (_ for _ in ()).throw(AssertionError('network forbidden')); "
            "from test_live_session_trading_store import _full_session_benchmark; "
            "print(json.dumps(_full_session_benchmark(Path(sys.argv[3])),sort_keys=True))"
        )
        result = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", code,
                                 str(ROOT / "src"), str(ROOT / "tests"), str(target)],
                                input="", capture_output=True, text=True, timeout=120, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["event_count"], 23_401)
        self.assertEqual(report["token_count"], 11_700)
        self.assertEqual(report["revision"], 23_400)
        self.assertEqual(report["projection_size"], 1)
        self.assertTrue(report["restart_equal"])
        self.assertTrue(report["loss_latched"])
        # Two applications for initial proposal/write, one for each append;
        # exactly one application/event for full restart verification.
        self.assertEqual(report["append_apply_count"], 23_402)
        self.assertEqual(report["replay_apply_count"], 23_401)
        self.assertLess(report["database_bytes"], 100_000_000)
        print("FULL_SESSION_STORE_BENCHMARK " + json.dumps(report, sort_keys=True))

    def test_time_regression_rejected_without_losing_pending_marker(self):
        now = START + timedelta(seconds=1)
        pending = self.begin(self.start(), now=now)
        with self.assertRaisesRegex(SessionTradingStoreConflict, "TIME_REGRESSION"):
            self.fail(pending, now=START)
        self.assertEqual(self.load(), pending)

    def test_materialized_state_corruption_is_detected(self):
        self.start()
        self.sql("UPDATE sessions SET state_json=?", ("{}",))
        self.assert_rejected_on_load_and_open()

    def test_audit_payload_or_hash_corruption_is_detected(self):
        self.begin(self.start())
        self.sql("UPDATE audit SET event_json=? WHERE revision=1", ('{"private":"corrupted"}',))
        self.assert_rejected_on_load_and_open()

    def test_deleted_audit_event_or_wrong_head_is_detected(self):
        stored = self.complete(self.begin(self.start()))
        self.sql("DELETE FROM audit WHERE revision=1")
        self.assert_rejected_on_load_and_open()

    def test_recomputed_latest_checksums_cannot_hide_clearing_historical_loss(self):
        stored = self.complete(self.begin(self.start()), "-1000")
        now = START + timedelta(seconds=1)
        stored = self.complete(self.begin(stored, token="gain", now=now), "5000", token="gain", now=now)
        raw = json.loads(self.sql("SELECT state_json FROM sessions")[0][0])
        raw["loss_latched"] = False
        encoded = json.dumps(raw, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        event_json, previous = self.sql("SELECT event_json,previous_sha256 FROM audit WHERE revision=?", (stored.revision,))[0]
        head = store_module._event_hash((ACCOUNT, DAY.isoformat()), stored.revision, previous, json.loads(event_json), digest)
        self.sql("UPDATE sessions SET state_json=?,state_sha256=?,audit_head_sha256=?", (encoded, digest, head))
        self.sql("UPDATE audit SET state_sha256=?,event_sha256=? WHERE revision=?", (digest, head, stored.revision))
        self.assert_rejected_on_load_and_open()

    def test_duplicate_or_noncanonical_state_json_is_rejected(self):
        self.start()
        encoded = self.sql("SELECT state_json FROM sessions")[0][0]
        encoded = encoded.replace("{", '{"loss_latched":false,', 1)
        self.sql("UPDATE sessions SET state_json=?", (encoded,))
        self.assert_rejected_on_load_and_open()

    def test_noncanonical_session_date_is_not_silently_rekeyed(self):
        self.start()
        self.sql("UPDATE sessions SET session_date='20260918'")
        self.close()
        with self.assertRaises(SessionTradingStoreError):
            SessionTradingStore(self.path)

    def test_unrelated_database_and_schema_changes_are_rejected(self):
        unrelated = self.path.with_name("unrelated.sqlite3")
        with sqlite3.connect(unrelated) as connection:
            connection.execute("CREATE TABLE unrelated (value TEXT)")
        unrelated.chmod(0o600)
        with self.assertRaises(SessionTradingStoreError):
            SessionTradingStore(unrelated)
        self.sql("CREATE TABLE unexpected (value TEXT)")
        self.assert_rejected_on_load_and_open()

    def test_symlinks_hardlinks_and_public_permissions_are_rejected(self):
        link = self.path.with_name("symlink.sqlite3")
        link.symlink_to(self.path)
        with self.assertRaises(SessionTradingStoreError):
            SessionTradingStore(link)
        hard = self.path.with_name("hardlink.sqlite3")
        os.link(self.path, hard)
        with self.assertRaises(SessionTradingStoreError):
            SessionTradingStore(self.path)
        hard.unlink()
        self.path.chmod(0o644)
        with self.assertRaises(SessionTradingStoreError):
            SessionTradingStore(self.path)

    def test_path_replacement_is_rejected_by_already_open_handle(self):
        self.start()
        self.path.rename(self.path.with_suffix(".prior"))
        with SessionTradingStore(self.path, create=True):
            pass
        with self.assertRaisesRegex(SessionTradingStoreError, "FILE_CHANGED"):
            self.load()

    def test_public_status_does_not_include_financial_values_or_claim_authentication(self):
        stored = self.complete(self.begin(self.start()), "-123.45")
        public = stored.public_dict()
        for text in ("10000", "123.45", PRIVATE):
            self.assertNotIn(text, repr(public))
            self.assertNotIn(text, repr(stored))
        self.assertFalse(stored.live_authority)
        self.assertFalse(public["live_authority"])
        self.assertFalse(public["source_authentication_established"])
        self.assertFalse(public["rollback_protection_established"])

    def test_invalid_scope_revision_missing_session_or_unknown_token_is_rejected(self):
        with self.assertRaises(SessionTradingStoreError):
            self.store.load(account_binding_sha256="not-an-account-hash", session_date=DAY)
        with self.assertRaises(SessionTradingStoreError):
            self.store.load(account_binding_sha256=ACCOUNT, session_date=START)
        with self.assertRaises(SessionTradingStoreError):
            self.store.begin_observation(account_binding_sha256=ACCOUNT, session_date=DAY,
                                         expected_revision=0, token="none", now=START)
        initial = self.start()
        with self.assertRaises(SessionTradingStoreError):
            self.store.begin_observation(account_binding_sha256=ACCOUNT, session_date=DAY,
                                         expected_revision=True, token="none", now=START)
        with self.assertRaises(SessionTradingStoreError):
            self.complete(initial, token="not-pending")
        self.assertEqual(self.load(), initial)


if __name__ == "__main__":
    unittest.main()
