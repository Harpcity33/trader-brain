"""Offline, whole-frame Massive status and handshake rejection regressions."""

import traceback
import unittest
from unittest.mock import patch

from titan_brain.live.massive_adapter import MassiveStoreError
from titan_brain.live.provider_clients import (
    MassiveWebSocketStreamTransport, WebSocketProtocolError, _MinimalWebSocket,
)
from tests.test_live_massive_frame_buffer import FakeAuthorizer, FakeSocket, NOW, quote


PRIVATE_MESSAGE = "provider-private-token-must-not-be-printed"


def status(value="success", **fields):
    return {"ev": "status", "status": value, "message": PRIVATE_MESSAGE, **fields}


class MassiveStreamStatusTests(unittest.TestCase):
    def setUp(self):
        for target in (
            "socket.socket.connect", "socket.socket.connect_ex",
            "socket.create_connection", "subprocess.Popen",
        ):
            guard = patch(target, side_effect=AssertionError("external I/O forbidden"))
            guard.start()
            self.addCleanup(guard.stop)

    def stream(self, frames, *, handshake=None, reconnect_frames=()):
        self.sockets = []
        scripts = iter((frames, reconnect_frames))

        def factory(_url, *, timeout_seconds):
            sock = FakeSocket(next(scripts))
            if handshake is not None:
                sock.frames[:2] = handshake
            self.sockets.append(sock)
            return sock

        stream = MassiveWebSocketStreamTransport(
            FakeAuthorizer(), websocket_factory=factory, clock=lambda: NOW,
        )
        stream.set_symbols(("AAPL",))
        self.addCleanup(stream.close)
        return stream

    def assert_invalidated(self, stream):
        observed = stream.status(now=NOW)
        self.assertFalse(observed.connected)
        self.assertFalse(observed.authenticated)
        self.assertFalse(observed.snapshot_resynced)
        self.assertIsNone(observed.latest_quote_at)
        self.assertIsNone(observed.latest_completed_bar_at)
        self.assertEqual(tuple(stream._pending_records), ())
        self.assertNotIn(PRIVATE_MESSAGE, str(observed))

    def assert_safe_failure(self, operation, code):
        try:
            operation()
        except MassiveStoreError as exc:
            self.assertEqual(str(exc), code)
            self.assertIsNone(exc.__cause__)
            self.assertNotIn(PRIVATE_MESSAGE, "".join(traceback.format_exception(exc)))
        else:
            self.fail("malformed or rejected frame did not fail closed")

    def test_negative_or_unknown_runtime_status_anywhere_rejects_whole_frame(self):
        for value in (
            "auth_failed", "failed", "error", "not_authorized", "max_connections",
            "denied", "unexpected_new_status", "connected", "auth_success",
        ):
            for frame in ([quote(1), status(value)], [status(value), quote(1)]):
                with self.subTest(status=value, data_first=frame[0]["ev"] == "Q"):
                    stream = self.stream([frame])
                    self.assert_safe_failure(
                        lambda: stream.drain(limit=1, timeout_seconds=0),
                        "MASSIVE_STREAM_STATUS_REJECTED",
                    )
                    self.assert_invalidated(stream)
                    self.assertTrue(self.sockets[-1].closed)

    def test_failure_after_prior_ready_frame_invalidates_health_and_reconnects_cleanly(self):
        stream = self.stream(
            [[quote(1)], [quote(2), status("error"), quote(3)]],
            reconnect_frames=[[quote(4)]],
        )
        self.assertEqual(stream.drain(limit=1, timeout_seconds=0)[0]["q"], 1)
        self.assertTrue(stream.status(now=NOW).snapshot_resynced)
        self.assert_safe_failure(
            lambda: stream.drain(limit=1, timeout_seconds=0),
            "MASSIVE_STREAM_STATUS_REJECTED",
        )
        self.assert_invalidated(stream)
        self.assertEqual(stream.drain(limit=1, timeout_seconds=0)[0]["q"], 4)
        self.assertEqual(len(self.sockets), 2)

    def test_later_bad_frame_aborts_whole_drain_batch_and_clears_prior_health(self):
        stream = self.stream([[quote(1)], [quote(2), status("error")]])
        self.assert_safe_failure(
            lambda: stream.drain(limit=2, timeout_seconds=0),
            "MASSIVE_STREAM_STATUS_REJECTED",
        )
        self.assert_invalidated(stream)

    def test_malformed_runtime_envelopes_fail_closed_after_prior_readiness(self):
        for frame in (
            {}, "invalid", 123, [], [quote(2), None], [quote(2), {}],
            [quote(2), {"ev": None}], [quote(2), {"ev": " "}],
        ):
            with self.subTest(frame=frame):
                stream = self.stream([[quote(1)], frame])
                stream.drain(limit=1, timeout_seconds=0)
                self.assert_safe_failure(
                    lambda: stream.drain(limit=1, timeout_seconds=0),
                    "MASSIVE_STREAM_FRAME_INVALID",
                )
                self.assert_invalidated(stream)

    def test_malformed_status_record_cannot_hide_behind_first_quote(self):
        for record in (
            {"ev": "status"}, status(None), status(True), status(123),
            status({}), status(""), status(" "),
        ):
            with self.subTest(record=record):
                stream = self.stream([[quote(1), record]])
                self.assert_safe_failure(
                    lambda: stream.drain(limit=1, timeout_seconds=0),
                    "MASSIVE_STREAM_STATUS_INVALID",
                )
                self.assert_invalidated(stream)

    def test_status_whitespace_cannot_bypass_rejection(self):
        stream = self.stream([[quote(1), status(" error ", ev=" STATUS ")]])
        self.assert_safe_failure(
            lambda: stream.drain(limit=1, timeout_seconds=0),
            "MASSIVE_STREAM_STATUS_REJECTED",
        )
        self.assert_invalidated(stream)

    def test_success_status_preserves_frame_tail_and_never_creates_health_alone(self):
        stream = self.stream([[status()], [status(), quote(1), quote(2), status()]])
        stream.probe_authentication()
        self.assertFalse(stream.status(now=NOW).snapshot_resynced)
        self.assertEqual(stream.drain(limit=1, timeout_seconds=0)[0]["q"], 1)
        reads = self.sockets[0].reads
        self.assertEqual(stream.drain(limit=1, timeout_seconds=0)[0]["q"], 2)
        self.assertEqual(self.sockets[0].reads, reads)

    def test_runtime_status_only_does_not_mark_data_resynced(self):
        stream = self.stream([[status()]])
        self.assertEqual(stream.drain(limit=1, timeout_seconds=0), ())
        self.assertTrue(stream.status(now=NOW).authenticated)
        self.assertFalse(stream.status(now=NOW).snapshot_resynced)

    def test_handshake_success_then_error_in_same_frame_never_authenticates(self):
        for stage in ("connected", "auth_success"):
            for records in ([status(stage), status("error")], [status("error"), status(stage)]):
                with self.subTest(stage=stage, records=records):
                    handshake = (
                        [records, [status("auth_success")]] if stage == "connected"
                        else [[status("connected")], records]
                    )
                    stream = self.stream([], handshake=handshake)
                    self.assert_safe_failure(
                        stream.probe_authentication, "MASSIVE_STREAM_CONNECTION_FAILED",
                    )
                    self.assert_invalidated(stream)
                    self.assertTrue(self.sockets[-1].closed)
                    self.assertNotIn("subscribe", [item["action"] for item in self.sockets[-1].sent])

    def test_handshake_mixed_data_is_explicit_failure_in_both_orders(self):
        for records in ([status("auth_success"), quote(1)], [quote(1), status("auth_success")]):
            with self.subTest(records=records):
                stream = self.stream([], handshake=[[status("connected")], records])
                self.assert_safe_failure(
                    stream.probe_authentication, "MASSIVE_STREAM_CONNECTION_FAILED",
                )
                self.assert_invalidated(stream)

    def test_handshake_malformed_tail_cannot_be_ignored_after_success(self):
        for malformed in (None, {}, {"ev": "status"}, status(True)):
            with self.subTest(malformed=malformed):
                stream = self.stream([], handshake=[
                    [status("connected")], [status("auth_success"), malformed],
                ])
                self.assert_safe_failure(
                    stream.probe_authentication, "MASSIVE_STREAM_CONNECTION_FAILED",
                )
                self.assert_invalidated(stream)

    def test_read_error_and_close_error_never_expose_provider_text(self):
        stream = self.stream([[quote(1)], OSError(PRIVATE_MESSAGE)])
        stream.drain(limit=1, timeout_seconds=0)
        with patch.object(self.sockets[0], "close", side_effect=OSError(PRIVATE_MESSAGE)):
            self.assert_safe_failure(
                lambda: stream.drain(limit=1, timeout_seconds=0),
                "MASSIVE_STREAM_READ_FAILED",
            )
        self.assert_invalidated(stream)

    def test_connect_factory_error_is_sanitized_and_invalidates_state(self):
        stream = self.stream([])
        with patch.object(stream, "_websocket_factory", side_effect=OSError(PRIVATE_MESSAGE)):
            self.assert_safe_failure(
                stream.probe_authentication, "MASSIVE_STREAM_CONNECTION_FAILED",
            )
        self.assert_invalidated(stream)

    def test_json_null_is_invalid_not_an_idle_timeout(self):
        class Socket:
            def settimeout(self, timeout):
                pass

        websocket = _MinimalWebSocket("wss://socket.massive.com/stocks", timeout_seconds=1)
        websocket.socket = Socket()
        with patch.object(websocket, "_read_exact", side_effect=[bytes((0x81, 4)), b"null"]):
            with self.assertRaisesRegex(WebSocketProtocolError, "FRAME_INVALID"):
                websocket.receive_json(timeout_seconds=1)


if __name__ == "__main__":
    unittest.main()
