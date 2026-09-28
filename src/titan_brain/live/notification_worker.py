"""Independent notification-outbox worker process seam.

The caller owns process supervision and supplies a sink already composed from
signed configuration plus runtime-only authorization.  This module never
constructs a broker client and can therefore keep delivering reconciliation,
protection, exit and incident events while order execution is busy or paused.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3
import threading
from typing import Callable
from uuid import uuid4

from .notifications import (
    LiveStateOutboxAdapter,
    NotificationSink,
    NotificationWorker,
    OutboxDispatcher,
)
from .state import LiveStateStore


NOTIFICATION_WORKER_HEARTBEAT_MAX_AGE_SECONDS = 15.0


def _process_is_alive(process_id: int) -> bool:
    """Check a durable worker PID without signalling or process discovery."""

    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def notification_worker_health(
    store: LiveStateStore,
    *,
    account_key: str,
    route: object,
    now: datetime,
    heartbeat_max_age_seconds: float = NOTIFICATION_WORKER_HEARTBEAT_MAX_AGE_SECONDS,
    process_checker: Callable[[int], bool] | None = None,
) -> tuple[bool, tuple[str, ...]]:
    """Verify an exact-route live worker and a fully drained account outbox."""

    if now.tzinfo is None:
        raise ValueError("notification worker health time must be timezone-aware")
    if not 1 <= float(heartbeat_max_age_seconds) <= 300:
        raise ValueError("notification worker heartbeat maximum age is invalid")
    checker = process_checker or _process_is_alive
    expected_route = {
        "route_id": str(getattr(route, "route_id", "")),
        "provider": str(getattr(route, "provider", "")),
        "destination_fingerprint": str(
            getattr(route, "destination_fingerprint", "")
        ),
        "route_version": str(getattr(route, "route_version", "")),
    }
    errors: list[str] = []
    try:
        rows = store.rows(
            "SELECT * FROM notification_worker_lease WHERE account_key=?",
            (account_key,),
        )
        if len(rows) != 1:
            errors.append(
                "notification_worker:LEASE_MISSING"
                if not rows
                else "notification_worker:LEASE_AMBIGUOUS"
            )
        else:
            row = rows[0]
            if row["released_at"] is not None:
                errors.append("notification_worker:LEASE_RELEASED")
            if any(
                str(row[field]) != expected
                for field, expected in expected_route.items()
            ):
                errors.append("notification_worker:ROUTE_MISMATCH")
            try:
                heartbeat_at = datetime.fromisoformat(str(row["heartbeat_at"]))
                if heartbeat_at.tzinfo is None:
                    raise ValueError("worker heartbeat is naive")
                heartbeat_at = heartbeat_at.astimezone(timezone.utc)
                heartbeat_age = (
                    now.astimezone(timezone.utc) - heartbeat_at
                ).total_seconds()
                if not 0 <= heartbeat_age <= float(heartbeat_max_age_seconds):
                    errors.append("notification_worker:HEARTBEAT_STALE_OR_FUTURE")
            except (TypeError, ValueError):
                errors.append("notification_worker:HEARTBEAT_INVALID")
            process_id = row["process_id"]
            if (
                isinstance(process_id, bool)
                or not isinstance(process_id, int)
                or process_id <= 0
                or not checker(process_id)
            ):
                errors.append("notification_worker:PROCESS_NOT_ALIVE")
            if int(row["last_failed_count"]) != 0:
                errors.append("notification_worker:LAST_CYCLE_FAILED")

        backlog = store.rows(
            """SELECT COUNT(*) AS pending_count,
                      COALESCE(SUM(CASE WHEN claim_owner IS NOT NULL THEN 1 ELSE 0 END),0)
                        AS claimed_count
                 FROM notification_outbox
                WHERE account_key=? AND state='PENDING'""",
            (account_key,),
        )
        if len(backlog) != 1:
            errors.append("notification_worker:OUTBOX_HEALTH_AMBIGUOUS")
        else:
            if int(backlog[0]["pending_count"]) != 0:
                errors.append("notification_worker:OUTBOX_BACKLOG_PENDING")
            if int(backlog[0]["claimed_count"]) != 0:
                errors.append("notification_worker:OUTBOX_ACTIVE_CLAIMS")
    except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError) as exc:
        errors.append(f"notification_worker:PROBE_{type(exc).__name__.upper()}")
    normalized = tuple(dict.fromkeys(errors))
    return not normalized, normalized


@dataclass(frozen=True)
class NotificationWorkerSettings:
    interval_seconds: float = 1.0
    batch_limit: int = 20
    claim_ttl_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not 0.05 <= float(self.interval_seconds) <= 60:
            raise ValueError("notification interval must be in [0.05, 60]")
        if not 1 <= int(self.batch_limit) <= 100:
            raise ValueError("notification batch must be in [1, 100]")
        if not 0 < float(self.claim_ttl_seconds) <= 300:
            raise ValueError("notification claim TTL must be in (0, 300]")


def run_notification_worker(
    *,
    state_path: str | Path,
    account_key: str,
    sink: NotificationSink,
    stop_event: threading.Event,
    settings: NotificationWorkerSettings = NotificationWorkerSettings(),
    worker_id: str | None = None,
    on_started: Callable[[str], None] | None = None,
    once: bool = False,
    clock: Callable[[], datetime] | None = None,
) -> tuple[int, int]:
    """Run the independent durable worker until its supervisor requests stop."""

    identity = worker_id or f"notification-process-{uuid4().hex}"
    with LiveStateStore(state_path) as state:
        route = getattr(sink, "route", None)
        if route is None:
            raise ValueError("notification worker sink has no route identity")
        worker_clock = clock or (lambda: datetime.now(timezone.utc))
        acquired_at = worker_clock()
        if acquired_at.tzinfo is None:
            raise ValueError("notification worker clock must be timezone-aware")
        generation = state.acquire_notification_worker_lease(
            account_key=account_key,
            worker_id=identity,
            route_id=route.route_id,
            provider=route.provider,
            destination_fingerprint=route.destination_fingerprint,
            route_version=route.route_version,
            acquired_at=acquired_at,
            recover_stale_after=timedelta(
                seconds=max(
                    float(settings.claim_ttl_seconds),
                    float(settings.interval_seconds) * 3,
                )
            ),
        )
        dispatcher = OutboxDispatcher(
            LiveStateOutboxAdapter(state, account_key),
            sink,
            worker_id=identity,
            claim_ttl_seconds=settings.claim_ttl_seconds,
        )
        worker = NotificationWorker(
            dispatcher,
            interval_seconds=settings.interval_seconds,
            batch_limit=settings.batch_limit,
            clock=worker_clock,
        )
        total_sent = total_failed = 0
        try:
            if on_started is not None:
                on_started(identity)
            while True:
                sent, failed = worker.run_once()
                total_sent += sent
                total_failed += failed
                heartbeat_at = worker_clock()
                state.heartbeat_notification_worker(
                    account_key=account_key,
                    worker_id=identity,
                    generation=generation,
                    observed_at=heartbeat_at,
                    sent_count=sent,
                    failed_count=failed,
                )
                if once or stop_event.wait(settings.interval_seconds):
                    return total_sent, total_failed
        finally:
            released_at = worker_clock()
            state.release_notification_worker_lease(
                account_key=account_key,
                worker_id=identity,
                generation=generation,
                released_at=released_at,
            )


__all__ = [
    "NOTIFICATION_WORKER_HEARTBEAT_MAX_AGE_SECONDS",
    "NotificationWorkerSettings",
    "notification_worker_health",
    "run_notification_worker",
]
