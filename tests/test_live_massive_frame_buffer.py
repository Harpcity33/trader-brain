"""Offline regression tests for bounded Massive JSON frame-tail retention."""

from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from titan_brain.live.massive_adapter import MassiveAuthorizationEvidence, MassiveStoreError
from titan_brain.live.provider_clients import MassiveWebSocketStreamTransport


NOW = datetime(2026, 9, 14, 8, 2, tzinfo=timezone.utc)


def quote(sequence, symbol="AAPL"):
    return {
        "ev": "Q", "sym": symbol, "q": sequence,
        "t": int(NOW.timestamp() * 1000),
        "bp": 100, "ap": 100.01, "bs": 100, "as": 100,
    }


class FakeAuthorizer:
    evidence = MassiveAuthorizationEvidence(
        binding_id="a" * 64,
        credential_source="offline-frame-buffer-test",
        scopes=("stocks:read",),
        authenticated=True,
    )

    def stream_credential(self):
        return "offline-test-only"


class FakeSocket:
    def __init__(self, frames):
        self.frames = [
            [{"ev": "status", "status": "connected"}],
            [{"ev": "status", "status": "auth_success"}],
            *frames,
        ]
        self.reads = 0
        self.closed = False
        self.sent = []

    def connect(self):
        pass

    def send_json(self, value):
        self.sent.append(dict(value))

    def receive_json(self, *, timeout_seconds):
        self.reads += 1
        value = self.frames.pop(0) if self.frames else None
        if isinstance(value, Exception):
            raise value
        return value() if callable(value) else value

    def close(self):
        self.closed = True


class MassiveFrameBufferTests(unittest.TestCase):
    def setUp(self):
        for target in ("socket.socket.connect", "socket.create_connection", "subprocess.Popen"):
            guard = patch(target, side_effect=AssertionError("external I/O forbidden"))
            guard.start()
            self.addCleanup(guard.stop)

    def stream(self, frames, *, later_frames=()):
        self.sockets = []
        scripts = iter((frames, *later_frames))

        def factory(_url, *, timeout_seconds):
            sock = FakeSocket(next(scripts))
            self.sockets.append(sock)
            return sock

        self.current_time = NOW
        stream = MassiveWebSocketStreamTransport(
            FakeAuthorizer(), websocket_factory=factory,
            clock=lambda: self.current_time,
        )
        stream.set_symbols(("AAPL",))
        self.addCleanup(stream.close)
        return stream

    @staticmethod
    def sequences(stream, limit=1):
        return [row["q"] for row in stream.drain(limit=limit, timeout_seconds=0)]

    def test_large_frame_tail_is_returned_fifo_without_another_read(self):
        stream = self.stream([[quote(1), quote(2), quote(3)]])
        self.assertEqual(self.sequences(stream), [1])
        reads = self.sockets[0].reads
        self.assertEqual(self.sequences(stream), [2])
        self.assertEqual(self.sequences(stream), [3])
        self.assertEqual(self.sockets[0].reads, reads)
        self.assertEqual(self.sequences(stream), [])

    def test_fifo_crosses_frames_and_honors_changing_limits(self):
        stream = self.stream([[quote(1), quote(2), quote(3)], [quote(4), quote(5)]])
        self.assertEqual(self.sequences(stream, 2), [1, 2])
        self.assertEqual(self.sequences(stream, 3), [3, 4, 5])
        self.assertEqual(self.sequences(stream), [])

    def test_status_and_unsubscribed_records_do_not_consume_output_limit(self):
        stream = self.stream([[
            {"ev": "status", "status": "success"}, quote(1),
            quote(90, "OTHER"), {"ev": "status", "status": "success"},
            {"ev": "UNSUPPORTED", "sym": "AAPL"}, quote(2),
        ]])
        self.assertEqual(self.sequences(stream), [1])
        self.assertEqual(self.sequences(stream), [2])
        self.assertEqual(self.sequences(stream), [])

    def test_identical_subscription_preserves_pending_tail(self):
        stream = self.stream([[quote(1), quote(2)]])
        self.assertEqual(self.sequences(stream), [1])
        stream.set_symbols(("AAPL",))
        self.assertEqual(self.sequences(stream), [2])

    def test_remove_and_readd_symbol_cannot_replay_old_pending_tail(self):
        stream = self.stream([[quote(1), quote(2)], [quote(3)]])
        self.assertEqual(self.sequences(stream), [1])
        stream.set_symbols(())
        stream.set_symbols(("AAPL",))
        self.assertEqual(self.sequences(stream), [3])

    def test_changed_subscription_discards_old_generation(self):
        stream = self.stream([[quote(1), quote(2)], [quote(3, "MSFT")]])
        self.assertEqual(self.sequences(stream), [1])
        stream.set_symbols(("MSFT",))
        self.assertFalse(stream.status(now=NOW).snapshot_resynced)
        self.assertEqual(self.sequences(stream), [3])

    def test_close_then_reconnect_discards_old_tail(self):
        stream = self.stream([[quote(1), quote(2)]], later_frames=([[quote(3)]],))
        self.assertEqual(self.sequences(stream), [1])
        stream.close()
        self.assertTrue(self.sockets[0].closed)
        self.assertEqual(self.sequences(stream), [3])
        self.assertEqual(len(self.sockets), 2)

    def test_receive_crossing_subscription_generation_fails_closed(self):
        stream = self.stream([])
        stream.probe_authentication()

        def during_receive():
            stream.set_symbols(())
            stream.set_symbols(("AAPL",))
            return [quote(1), quote(2)]

        self.sockets[0].frames.extend((during_receive, [quote(3)]))
        with self.assertRaisesRegex(MassiveStoreError, "LIFECYCLE_CHANGED"):
            self.sequences(stream)
        self.assertEqual(self.sequences(stream), [3])

    def test_buffered_am_uses_original_receipt_for_completion_health(self):
        minute_start = NOW
        minute_end = minute_start + timedelta(minutes=1)
        stream = self.stream([[quote(1), {
            "ev": "AM", "sym": "AAPL",
            "s": int(minute_start.timestamp() * 1000),
            "e": int((minute_end - timedelta(milliseconds=1)).timestamp() * 1000),
        }]])
        self.assertEqual(self.sequences(stream), [1])
        self.current_time = minute_end + timedelta(seconds=1)
        result = stream.drain(limit=1, timeout_seconds=0)
        self.assertEqual(result[0]["ev"], "AM")
        self.assertIsNone(stream.status(now=self.current_time).latest_completed_bar_at)

    def test_oversized_frame_fails_explicitly_and_clears_pending_state(self):
        stream = self.stream([[quote(1), quote(2), quote(3)]], later_frames=([[quote(4)]],))
        with patch.object(stream, "MAX_PENDING_RECORDS", 2):
            with self.assertRaisesRegex(MassiveStoreError, "PENDING_RECORD_OVERFLOW"):
                self.sequences(stream)
        self.assertTrue(self.sockets[0].closed)
        self.assertFalse(stream.status(now=NOW).authenticated)
        self.assertEqual(self.sequences(stream), [4])

    def test_read_failure_reconnect_cannot_replay_consumed_tail(self):
        stream = self.stream([[quote(1), quote(2)], OSError("offline")],
                             later_frames=([[quote(3)]],))
        self.assertEqual(self.sequences(stream), [1])
        self.assertEqual(self.sequences(stream), [2])
        with self.assertRaisesRegex(MassiveStoreError, "READ_FAILED"):
            self.sequences(stream)
        self.assertEqual(self.sequences(stream), [3])


if __name__ == "__main__":
    unittest.main()
