"""Deterministic redacted notifications backed by a durable outbox."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
from typing import Any, Callable, Mapping, Protocol, Sequence
from uuid import NAMESPACE_URL, uuid5

from .latency import LatencyRecorder
from .models import OutboxMessage
from .policy import sha256_json
from .state import LiveStateStore


CORE_EVENTS = frozenset(
    {
        "READINESS",
        "ENTRY_FILLED",
        "PROTECTION_WORKING",
        "EXIT_FILLED",
        "RISK_PAUSED",
        "UNRESOLVED_SUBMISSION",
        "UNPROTECTED_EXPOSURE",
        "AUTHENTICATION_INCIDENT",
        "CLOSEOUT_INCIDENT",
        "RUNTIME_INCIDENT",
        "RECOVERED",
        "END_OF_DAY",
    }
)
URGENT_EVENTS = frozenset(
    {
        "UNRESOLVED_SUBMISSION",
        "UNPROTECTED_EXPOSURE",
        "AUTHENTICATION_INCIDENT",
        "CLOSEOUT_INCIDENT",
        "RUNTIME_INCIDENT",
    }
)


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(word in lowered for word in ("secret", "token", "password", "credential")):
                result[str(key)] = "[REDACTED]"
            elif "account" in lowered and isinstance(item, str) and len(item) > 4:
                digits = "".join(character for character in item if character.isdigit())
                result[str(key)] = f"ending-{digits[-4:]}" if len(digits) >= 4 else "[REDACTED]"
            else:
                result[str(key)] = _redact(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r"\b\d{5,}\b", lambda match: "•" * (len(match.group()) - 4) + match.group()[-4:], value)
    return value


@dataclass(frozen=True)
class Notification:
    dedupe_key: str
    event_type: str
    severity: str
    subject: str
    body: str
    payload: Mapping[str, Any]


def build_notification(event_type: str, payload: Mapping[str, Any]) -> Notification:
    event = str(event_type).strip().upper()
    if event not in CORE_EVENTS:
        raise ValueError("scan chatter and unknown notification events are not allowed")
    clean = _redact(dict(payload))
    state = str(clean.get("state", "unknown")).upper()
    symbol = str(clean.get("symbol", "ACCOUNT")).upper()
    quantity = clean.get("quantity")
    protection = str(clean.get("protection_state", "not_applicable")).upper()
    reason = str(clean.get("reason", ""))
    summary_parts = [f"state={state}", f"symbol={symbol}"]
    if quantity is not None:
        summary_parts.append(f"quantity={quantity}")
    if event in {"ENTRY_FILLED", "PROTECTION_WORKING", "UNPROTECTED_EXPOSURE"}:
        summary_parts.append(f"protection={protection}")
    if reason:
        summary_parts.append(f"reason={reason}")
    subject = f"Titan {event.replace('_', ' ').title()} — {symbol}"
    body = " | ".join(summary_parts)
    identity = clean.get("event_id") or clean.get("intent_id") or clean.get("session_date")
    if not identity:
        identity = sha256_json(clean)
    return Notification(
        dedupe_key=f"{event}:{identity}",
        event_type=event,
        severity="urgent" if event in URGENT_EVENTS else "info",
        subject=subject,
        body=body,
        payload=clean,
    )


class NotificationSink(Protocol):
    def send(self, notification: Notification) -> str:
        """Deliver one notification and return a destination receipt."""


class OutboxStore(Protocol):
    def enqueue_notification(self, notification: Notification, created_at: datetime) -> str: ...
    def due_notifications(self, now: datetime, limit: int) -> Sequence[Mapping[str, Any]]: ...
    def notification_sent(self, outbox_id: str, receipt: str, delivered_at: datetime) -> None: ...
    def notification_failed(
        self,
        outbox_id: str,
        error: str,
        attempted_at: datetime,
        next_attempt_at: datetime,
    ) -> None: ...


class JsonlNotificationSink:
    """Zero-service-cost local sink for supervised delivery/heartbeat pickup."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def send(self, notification: Notification) -> str:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "dedupe_key": notification.dedupe_key,
            "event_type": notification.event_type,
            "severity": notification.severity,
            "subject": notification.subject,
            "body": notification.body,
            "payload": notification.payload,
        }
        encoded = (json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
        descriptor = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return sha256_json(row)


class LiveStateOutboxAdapter:
    """Adapt :class:`LiveStateStore` to the dispatcher without weakening durability."""

    def __init__(self, store: LiveStateStore, account_key: str):
        self.store = store
        self.account_key = str(account_key)

    def enqueue_notification(self, notification: Notification, created_at: datetime) -> str:
        message_id = str(uuid5(NAMESPACE_URL, f"titan-outbox:{notification.dedupe_key}"))
        self.store.enqueue_notification(
            OutboxMessage(
                message_id=message_id,
                event_key=notification.dedupe_key,
                account_key=self.account_key,
                template=notification.event_type,
                payload={
                    "severity": notification.severity,
                    "subject": notification.subject,
                    "body": notification.body,
                    "payload": notification.payload,
                },
                created_at=created_at,
            )
        )
        return message_id

    def due_notifications(self, now: datetime, limit: int) -> Sequence[Mapping[str, Any]]:
        result: list[Mapping[str, Any]] = []
        for row in self.store.due_outbox(now=now, limit=limit):
            try:
                decoded = json.loads(str(row["payload_json"]))
            except json.JSONDecodeError:
                decoded = None
            if (
                isinstance(decoded, Mapping)
                and set(("severity", "subject", "body", "payload")).issubset(decoded)
                and isinstance(decoded["payload"], Mapping)
            ):
                notification = Notification(
                    dedupe_key=str(row["event_key"]),
                    event_type=str(row["template"]),
                    severity=str(decoded["severity"]),
                    subject=str(decoded["subject"]),
                    body=str(decoded["body"]),
                    payload=dict(decoded["payload"]),
                )
            else:
                # Execution intents predate the dispatcher wrapper and are
                # deliberately written directly at the same durable boundary
                # as an UNKNOWN result.  Normalize those rows here so an
                # urgent ambiguous-submission alert can never crash draining.
                template = str(row["template"]).upper()
                event_type = (
                    "UNRESOLVED_SUBMISSION"
                    if "SUBMISSION_UNKNOWN" in template
                    else "RUNTIME_INCIDENT"
                )
                raw_payload = (
                    dict(decoded)
                    if isinstance(decoded, Mapping)
                    else {
                        "event_id": str(row["event_key"]),
                        "state": "blocked",
                        "symbol": "ACCOUNT",
                        "reason": "malformed durable notification payload",
                    }
                )
                raw_payload.setdefault("event_id", str(row["event_key"]))
                notification = build_notification(event_type, raw_payload)
            result.append(
                {
                    "outbox_id": row["message_id"],
                    "dedupe_key": notification.dedupe_key,
                    "event_type": notification.event_type,
                    "severity": notification.severity,
                    "subject": notification.subject,
                    "body": notification.body,
                    "payload_json": json.dumps(
                        notification.payload, sort_keys=True, separators=(",", ":")
                    ),
                    "attempts": row["attempt_count"],
                    # This is the durable event/outbox boundary, not the first
                    # delivery-attempt time.  It survives retry and restart.
                    "created_at": row["created_at"],
                }
            )
        return result

    def notification_sent(self, outbox_id: str, receipt: str, delivered_at: datetime) -> None:
        self.store.mark_notification_attempt(
            outbox_id,
            attempted_at=delivered_at,
            delivered=True,
            delivery_receipt=receipt,
        )

    def notification_failed(
        self,
        outbox_id: str,
        error: str,
        attempted_at: datetime,
        next_attempt_at: datetime,
    ) -> None:
        self.store.mark_notification_attempt(
            outbox_id,
            attempted_at=attempted_at,
            delivered=False,
            error=error,
            next_attempt_at=next_attempt_at,
        )


class OutboxDispatcher:
    def __init__(
        self,
        store: OutboxStore,
        sink: NotificationSink,
        *,
        latency: LatencyRecorder | None = None,
        completion_clock: Callable[[], datetime] | None = None,
    ):
        self.store = store
        self.sink = sink
        self.latency = latency
        self._completion_clock = completion_clock or (
            lambda: datetime.now(timezone.utc)
        )

    def enqueue(self, event_type: str, payload: Mapping[str, Any], now: datetime) -> str:
        if now.tzinfo is None:
            raise ValueError("notification time must be timezone-aware")
        return self.store.enqueue_notification(build_notification(event_type, payload), now)

    def drain(self, now: datetime, *, limit: int = 20) -> tuple[int, int]:
        if now.tzinfo is None:
            raise ValueError("notification time must be timezone-aware")
        sent = failed = 0
        for row in self.store.due_notifications(now, limit):
            notification = Notification(
                dedupe_key=str(row["dedupe_key"]),
                event_type=str(row["event_type"]),
                severity=str(row["severity"]),
                subject=str(row["subject"]),
                body=str(row["body"]),
                payload=json.loads(str(row["payload_json"])),
            )
            try:
                receipt = self.sink.send(notification)
            except Exception as exc:
                failed += 1
                attempts = int(row.get("attempts", 0)) + 1
                delay = min(300, 2 ** min(attempts, 8))
                self.store.notification_failed(
                    str(row["outbox_id"]),
                    f"{type(exc).__name__}: {exc}"[:500],
                    now,
                    now + timedelta(seconds=delay),
                )
            else:
                sent += 1
                self.store.notification_sent(str(row["outbox_id"]), receipt, now)
                try:
                    delivered_at = self._completion_clock()
                    if delivered_at.tzinfo is None:
                        raise ValueError("notification completion time must be aware")
                except Exception:
                    delivered_at = None
                if delivered_at is not None:
                    self._record_delivery_latency(row, notification, delivered_at)
        return sent, failed

    def _record_delivery_latency(
        self,
        row: Mapping[str, Any],
        notification: Notification,
        delivered_at: datetime,
    ) -> None:
        """Record only a successfully delivered durable event.

        Failed attempts have no notification terminal boundary and therefore
        create no ``confirmed_event_to_notification`` sample.  Telemetry
        failure cannot turn a successful delivery into an outbox retry.
        """

        if self.latency is None or row.get("created_at") is None:
            return
        try:
            confirmed_at = datetime.fromisoformat(str(row["created_at"]))
            if confirmed_at.tzinfo is None:
                return
            duration_ms = (delivered_at - confirmed_at).total_seconds() * 1000
            self.latency.record_duration(
                "confirmed_event_to_notification",
                duration_ms,
                observed_at=delivered_at,
                correlation_id=str(row["outbox_id"]),
                metadata={
                    "event_type": notification.event_type,
                    "dedupe_key": notification.dedupe_key,
                },
            )
        except Exception:
            return


__all__ = [
    "CORE_EVENTS",
    "JsonlNotificationSink",
    "LiveStateOutboxAdapter",
    "Notification",
    "NotificationSink",
    "OutboxDispatcher",
    "build_notification",
]
