from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from titan_brain.live.latency import (
    LatencyRecorder,
    LiveStateLatencyAdapter,
    summarize,
)
from titan_brain.live.models import OutboxMessage
from titan_brain.live.notifications import (
    JsonlNotificationSink,
    LiveStateOutboxAdapter,
    OutboxDispatcher,
    build_notification,
)
from titan_brain.live.state import LiveStateStore


ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 8, 10, tzinfo=ET)


class MemoryLatencyStore:
    def __init__(self):
        self.rows = []

    def record_latency(self, *args):
        self.rows.append(args)


class FailingSink:
    def send(self, notification):
        raise OSError("synthetic destination outage")


class NotificationLatencyTests(unittest.TestCase):
    def test_notification_is_deterministic_redacted_and_state_specific(self) -> None:
        notification = build_notification(
            "ENTRY_FILLED",
            {
                "event_id": "fill-1",
                "account_number": "1234569999",
                "symbol": "xyz",
                "quantity": 2,
                "state": "filled",
                "protection_state": "pending",
                "access_token": "do-not-leak",
            },
        )
        self.assertEqual(notification.dedupe_key, "ENTRY_FILLED:fill-1")
        self.assertEqual(notification.payload["account_number"], "ending-9999")
        self.assertEqual(notification.payload["access_token"], "[REDACTED]")
        self.assertIn("protection=PENDING", notification.body)

    def test_unknown_scan_chatter_is_suppressed(self) -> None:
        with self.assertRaisesRegex(ValueError, "scan chatter"):
            build_notification("WATCHLIST_UPDATE", {"symbol": "XYZ"})

    def test_local_sink_fsyncs_one_json_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "notifications.jsonl"
            sink = JsonlNotificationSink(path)
            receipt = sink.send(build_notification("READINESS", {"event_id": "ready", "state": "paused"}))
            self.assertEqual(len(receipt.receipt_hash), 64)
            self.assertEqual(receipt.route_id, sink.route.route_id)
            self.assertEqual(receipt.assurance.value, "LOCAL_STAGED")
            row = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(row["event_type"], "READINESS")

    def test_latency_stages_remain_separate(self) -> None:
        store = MemoryLatencyStore()
        ticks = iter((1_000_000_000, 1_012_500_000))
        with LatencyRecorder(store, clock_ns=lambda: next(ticks)).measure(
            "durable_intent_write", observed_at=NOW, correlation_id="intent-1"
        ):
            pass
        self.assertEqual(store.rows[0][0], "durable_intent_write")
        self.assertEqual(store.rows[0][1], 12.5)
        summary = summarize("submit_to_ack", [1, 2, 3, 4, 100])
        self.assertEqual(summary.count, 5)
        self.assertEqual(summary.p50_ms, 3)
        self.assertGreater(summary.p95_ms, 4)

    def test_explicit_span_records_nothing_until_terminal_boundary(self) -> None:
        store = MemoryLatencyStore()
        ticks = iter((1_000_000, 3_500_000))
        recorder = LatencyRecorder(store, clock_ns=lambda: next(ticks))
        span = recorder.start(
            "submit_to_ack", observed_at=NOW, correlation_id="intent-2"
        )
        self.assertEqual(store.rows, [])
        self.assertEqual(recorder.finish(span), 2.5)
        self.assertEqual(store.rows[0][0], "submit_to_ack")

    def test_live_state_adapter_persists_measured_stage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with LiveStateStore(Path(directory) / "state.sqlite3") as state:
                ticks = iter((5_000_000, 6_250_000))
                recorder = LatencyRecorder(
                    LiveStateLatencyAdapter(state, "ending-7153"),
                    clock_ns=lambda: next(ticks),
                )
                span = recorder.start(
                    "preflight_risk",
                    observed_at=NOW,
                    correlation_id="plan-1",
                )
                recorder.finish(span)
                rows = state.rows("SELECT * FROM latency_samples")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["stage"], "preflight_risk")
            self.assertEqual(rows[0]["duration_microseconds"], 1250)
            self.assertEqual(rows[0]["correlation_id"], "plan-1")

    def test_successful_outbox_delivery_records_confirmed_event_latency(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metrics = MemoryLatencyStore()
            with LiveStateStore(root / "state.sqlite3") as state:
                dispatcher = OutboxDispatcher(
                    LiveStateOutboxAdapter(state, "ending-7153"),
                    JsonlNotificationSink(root / "notifications.jsonl"),
                    latency=LatencyRecorder(metrics),
                    completion_clock=lambda: NOW + timedelta(seconds=2),
                )
                dispatcher.enqueue(
                    "EXIT_FILLED",
                    {"event_id": "exit-1", "state": "filled", "symbol": "XYZ"},
                    NOW,
                )
                self.assertEqual(dispatcher.drain(NOW + timedelta(seconds=2)), (1, 0))
            self.assertEqual(len(metrics.rows), 1)
            self.assertEqual(metrics.rows[0][0], "confirmed_event_to_notification")
            self.assertEqual(metrics.rows[0][1], 2000.0)

    def test_failed_delivery_has_no_notification_terminal_latency(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            metrics = MemoryLatencyStore()
            with LiveStateStore(Path(directory) / "state.sqlite3") as state:
                dispatcher = OutboxDispatcher(
                    LiveStateOutboxAdapter(state, "ending-7153"),
                    FailingSink(),
                    latency=LatencyRecorder(metrics),
                )
                dispatcher.enqueue(
                    "UNPROTECTED_EXPOSURE",
                    {
                        "event_id": "unprotected-1",
                        "state": "unprotected",
                        "symbol": "XYZ",
                    },
                    NOW,
                )
                self.assertEqual(dispatcher.drain(NOW), (0, 1))
            self.assertEqual(metrics.rows, [])

    def test_raw_unknown_submission_row_is_normalized_and_delivered_urgently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with LiveStateStore(root / "state.sqlite3") as state:
                state.enqueue_notification(
                    OutboxMessage(
                        message_id="unknown-message",
                        event_key="broker-submission-unknown:intent-1",
                        account_key="ending-7153",
                        template="BROKER_SUBMISSION_UNKNOWN",
                        payload={
                            "account": "1234569999",
                            "symbol": "XYZ",
                            "intent_id": "intent-1",
                            "state": "UNKNOWN",
                        },
                        created_at=NOW,
                    )
                )
                dispatcher = OutboxDispatcher(
                    LiveStateOutboxAdapter(state, "ending-7153"),
                    JsonlNotificationSink(root / "notifications.jsonl"),
                )
                self.assertEqual(dispatcher.drain(NOW), (1, 0))
                delivered = json.loads(
                    (root / "notifications.jsonl").read_text(encoding="utf-8")
                )
                self.assertEqual(delivered["event_type"], "UNRESOLVED_SUBMISSION")
                self.assertEqual(delivered["severity"], "urgent")
                self.assertEqual(delivered["payload"]["account"], "ending-9999")


if __name__ == "__main__":
    unittest.main()
