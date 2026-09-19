"""Hermetic tracker and exact patched legacy-method regressions; no real I/O."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from titan_brain.live.massive_health import AuthenticatedMarketHealth


ROOT = Path(__file__).resolve().parents[1]
VERSION = ROOT / "integrations/legacy_massive_health/v1"
NOW_MS = 1_800_000_000_000


def quote(stamp=NOW_MS, **fields):
    return {"ev": "Q", "sym": "TEST", "bp": 12.0, "ap": 12.01,
            "bs": 100, "as": 200, "t": stamp, **fields}


def aggregate(kind="A", stamp=NOW_MS, **fields):
    return {"ev": kind, "sym": "TEST", "s": stamp - (1000 if kind == "A" else 60000),
            "e": stamp, "o": 12.0, "h": 12.2, "l": 11.9, "c": 12.1,
            "v": 100, **fields}


def status(value):
    return {"ev": "status", "status": value}


def apply_exact_hunks(source, unified):
    """Apply exact unique preimages, never a fuzzy/rebased patch."""
    chunks = re.split(r"^@@ .*@@.*\n", unified, flags=re.M)[1:]
    for chunk in chunks:
        lines = chunk.splitlines(keepends=True)
        before = "".join(line[1:] for line in lines if line.startswith((" ", "-")))
        after = "".join(line[1:] for line in lines if line.startswith((" ", "+")))
        if source.count(before) != 1:
            raise ValueError("patch preimage is not exact and unique")
        source = source.replace(before, after, 1)
    return source


def legacy_methods():
    manifest = json.loads((VERSION / "manifest.json").read_text())
    original = (ROOT / "tests/fixtures/legacy_massive_health_v1_preimage.txt").read_text()
    changed = apply_exact_hunks(original, (VERSION / manifest["patch"]).read_text())
    first = changed[changed.index("    def _handle_status"):changed.index("    def _refresh_dynamic_subscriptions")]
    second = changed[changed.index("    def _connect"):]
    namespace = {
        "Any": object, "WebSocketError": RuntimeError,
        "time_module": SimpleNamespace(time=lambda: NOW_MS / 1000, monotonic=lambda: 100.0),
    }
    exec("from __future__ import annotations\nclass LegacySeam:\n" + first + second, namespace)
    return namespace


class LegacyMassiveHealthTests(unittest.TestCase):
    def setUp(self):
        for target in ("socket.create_connection", "subprocess.Popen"):
            guard = patch(target, side_effect=AssertionError("external I/O forbidden"))
            guard.start()
            self.addCleanup(guard.stop)
        self.health = AuthenticatedMarketHealth()
        self.connection = object()
        self.health.begin_connection(self.connection)

    def authenticate(self):
        self.assertTrue(self.health.authenticate(self.connection, [status("auth_success")]))

    def pulse(self, event=None, *, clock=100.0, now_ms=NOW_MS, connection=None):
        return self.health.processed_event(
            self.connection if connection is None else connection,
            quote() if event is None else event, now_ms=now_ms, monotonic=clock,
        )

    def test_data_requires_current_authenticated_connection(self):
        self.assertFalse(self.pulse())
        self.authenticate()
        self.assertFalse(self.pulse(connection=object()))
        self.assertTrue(self.pulse())

    def test_status_and_ping_never_pulse(self):
        self.authenticate()
        for event in (None, {}, [], status("success"), {"ev": "LULD", "t": NOW_MS}):
            with self.subTest(event=event):
                self.assertFalse(self.health.processed_event(
                    self.connection, event, now_ms=NOW_MS, monotonic=100,
                ))

    def test_new_valid_data_paces_at_five_seconds(self):
        self.authenticate()
        self.assertTrue(self.pulse())
        self.assertFalse(self.pulse(quote(NOW_MS + 4999), clock=104.999, now_ms=NOW_MS + 4999))
        self.assertTrue(self.pulse(quote(NOW_MS + 5000), clock=105, now_ms=NOW_MS + 5000))

    def test_duplicates_or_reordered_data_do_not_pulse(self):
        self.authenticate()
        self.assertTrue(self.pulse())
        self.assertFalse(self.pulse(clock=105, now_ms=NOW_MS + 5000))
        self.assertFalse(self.pulse(quote(NOW_MS - 1), clock=110, now_ms=NOW_MS + 5000))

    def test_aggregates_have_separate_progress_and_age_bounds(self):
        self.authenticate()
        self.assertTrue(self.pulse(aggregate()))
        self.assertTrue(self.pulse(aggregate("AM", NOW_MS - 60000), clock=105))
        self.assertFalse(self.pulse(aggregate("AM", NOW_MS - 120001), clock=110))
        self.assertFalse(self.pulse(aggregate("A", NOW_MS - 15001), clock=110))

    def test_invalid_data_never_pulses(self):
        self.authenticate()
        invalid = [
            quote(t=NOW_MS + 1001), quote(t=NOW_MS - 15001), quote(t=True),
            quote(t="1800000000000"), quote(bp=float("nan")), quote(ap=float("inf")),
            quote(bp=10 ** 10000), quote(bp=True), quote(ap=11), quote(bs=0),
            quote(sym=""), quote(sym="private\nvalue"), quote(otc=True),
            aggregate(s=NOW_MS), aggregate(s=NOW_MS - 1001), aggregate(v=-1),
            aggregate(h=11), aggregate(o="12"), aggregate(e=False),
        ]
        for index, event in enumerate(invalid):
            with self.subTest(index=index):
                self.assertFalse(self.pulse(event))
        self.assertTrue(self.pulse())

    def test_error_requires_new_connection_authentication_and_data(self):
        self.authenticate()
        self.assertTrue(self.pulse())
        self.assertFalse(self.health.note_status(self.connection, "error"))
        self.assertFalse(self.health.note_status(self.connection, "success"))
        self.assertFalse(self.health.authenticate(self.connection, status("auth_success")))
        self.assertFalse(self.pulse(quote(NOW_MS + 5000), clock=105, now_ms=NOW_MS + 5000))
        old_connection = self.connection
        self.connection = object()
        self.health.begin_connection(self.connection)
        self.assertFalse(self.pulse())
        self.authenticate()
        self.assertFalse(self.pulse(connection=old_connection))
        self.assertTrue(self.pulse())

    def test_disconnect_and_clock_reversal_fail_closed(self):
        self.authenticate()
        self.assertTrue(self.pulse())
        self.assertFalse(self.pulse(clock=99))
        self.assertFalse(self.pulse(quote(NOW_MS + 5000), clock=105, now_ms=NOW_MS + 5000))
        self.health.disconnect(self.connection)
        self.assertFalse(self.pulse())

    def test_mixed_or_malformed_auth_handshake_fails_closed(self):
        for frame in (
            [], None, status("success"), [status("auth_success"), status("error")],
            [status("auth_success"), quote()], {"status": "auth_success"},
        ):
            with self.subTest(frame=frame):
                self.health.begin_connection(self.connection)
                self.assertFalse(self.health.authenticate(self.connection, frame))
                self.assertFalse(self.pulse())

    def test_runtime_reauthentication_status_revokes_health(self):
        self.authenticate()
        self.assertFalse(self.health.note_status(self.connection, "auth_success"))
        self.assertFalse(self.pulse())

    def test_manifest_binds_helper_and_exact_patch_fixture(self):
        manifest = json.loads((VERSION / "manifest.json").read_text())
        self.assertEqual(
            hashlib.sha256((ROOT / manifest["helper_source"]).read_bytes()).hexdigest(),
            manifest["helper_sha256"],
        )
        fixture = ROOT / "tests/fixtures/legacy_massive_health_v1_preimage.txt"
        self.assertEqual(hashlib.sha256(fixture.read_bytes()).hexdigest(), manifest["fixture_sha256"])
        self.assertNotEqual(manifest["target_before_sha256"], manifest["rejected_older_canonical_sha256"])
        legacy_methods()

    def seam(self):
        namespace = legacy_methods()
        watcher = namespace["LegacySeam"]()
        watcher.stream_health = self.health
        watcher.client = self.connection
        watcher.store = Mock()
        watcher.logger = Mock()
        watcher.stop_event = Mock()
        for name in ("minute", "second", "quote", "luld"):
            setattr(watcher, "_handle_" + name, Mock())
        return watcher, namespace

    def test_patched_success_status_does_not_write_healthy(self):
        self.authenticate()
        watcher, _ = self.seam()
        watcher._process_message([status("success")])
        watcher._process_message(None)
        watcher.store.set_health.assert_not_called()
        watcher._process_message([quote()])
        watcher.store.set_health.assert_called_once_with(
            "massive_websocket", "healthy",
            {"source": "authenticated_processed_market_data", "event_type": "Q"},
        )

    def test_patched_whole_frame_error_cannot_be_hidden_by_data(self):
        for data_first in (True, False):
            self.health.begin_connection(self.connection)
            self.authenticate()
            watcher, _ = self.seam()
            frame = [quote(), status("error")]
            if not data_first:
                frame.reverse()
            with self.assertRaisesRegex(RuntimeError, "MASSIVE_STREAM_STATUS_REJECTED"):
                watcher._process_message(frame)
            watcher._handle_quote.assert_not_called()
            self.assertEqual([call.args[1] for call in watcher.store.set_health.call_args_list], ["degraded"])
            watcher._process_message([quote()])
            self.assertEqual(watcher.store.set_health.call_count, 1)

    def test_patched_handler_failure_and_invalid_data_do_not_pulse(self):
        self.authenticate()
        watcher, _ = self.seam()
        watcher._process_message([quote(t=NOW_MS - 60000)])
        watcher.store.set_health.assert_not_called()
        watcher._handle_quote.side_effect = RuntimeError("handler failed")
        with self.assertRaisesRegex(RuntimeError, "handler failed"):
            watcher._process_message([quote()])
        watcher.store.set_health.assert_not_called()

    def test_patched_connection_failure_closes_socket_and_revokes_health(self):
        watcher, namespace = self.seam()
        socket = Mock()
        socket.recv_json.side_effect = [status("connected"), [status("auth_success"), status("error")]]
        namespace["MinimalWebSocket"] = lambda _url: socket
        watcher.config = SimpleNamespace(websocket_url="wss://example.invalid/stocks")
        watcher.api_key = "synthetic-placeholder"
        with self.assertRaisesRegex(RuntimeError, "MASSIVE_STREAM_STATUS_REJECTED"):
            watcher._connect()
        socket.close.assert_called_once()
        self.assertIsNone(watcher.client)
        self.assertFalse(self.pulse(connection=socket))
        self.assertFalse(any(call.args[1] == "healthy" for call in watcher.store.set_health.call_args_list))

    def test_patched_connect_requires_data_after_authentication(self):
        watcher, namespace = self.seam()
        socket = Mock()
        socket.recv_json.side_effect = [status("connected"), status("auth_success")]
        namespace["MinimalWebSocket"] = lambda _url: socket
        watcher.config = SimpleNamespace(websocket_url="wss://example.invalid/stocks")
        watcher.api_key = "synthetic-placeholder"
        watcher.BASE_SUBSCRIPTIONS = ("AM.*",)
        watcher.dynamic_symbols = set()
        watcher._refresh_dynamic_subscriptions = Mock()
        watcher._connect()
        self.assertIs(watcher.client, socket)
        self.assertEqual([call.args[1] for call in watcher.store.set_health.call_args_list], ["degraded"])
        watcher._process_message(quote())
        self.assertEqual([call.args[1] for call in watcher.store.set_health.call_args_list], ["degraded", "healthy"])
        watcher._disconnect()
        socket.close.assert_called_once()
        self.assertFalse(self.pulse(connection=socket))


if __name__ == "__main__":
    unittest.main()
