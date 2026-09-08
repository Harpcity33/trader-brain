"""Separate monotonic latency stages; no stage is a broker-fill promise."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import math
import time
from typing import Any, Iterable, Iterator, Mapping, Protocol
from uuid import uuid4

from .models import LatencySample
from .state import LiveStateStore


STAGES = frozenset(
    {
        "event_to_receipt",
        "signal_compute",
        "preflight_risk",
        "durable_intent_write",
        "submit_to_ack",
        "ack_to_fill",
        "fill_to_working_protection",
        "confirmed_event_to_notification",
    }
)


class LatencyStore(Protocol):
    def record_latency(
        self,
        stage: str,
        duration_ms: float,
        observed_at: datetime,
        correlation_id: str | None,
        metadata: Mapping[str, Any],
    ) -> None: ...


@dataclass(frozen=True)
class LatencySummary:
    stage: str
    count: int
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    maximum_ms: float | None


@dataclass(frozen=True)
class LatencySpan:
    """A monotonic stage start that records nothing until explicitly finished.

    Explicit completion matters at broker boundaries: an interrupted or
    ambiguous submission must not be mislabeled as ``submit_to_ack`` merely
    because a transport call returned control or raised an exception.
    """

    stage: str
    started_ns: int
    observed_at: datetime
    correlation_id: str | None
    metadata: Mapping[str, Any]


@dataclass(frozen=True)
class LatencyMeasurement:
    """A completed stage held in memory until safe persistence."""

    stage: str
    duration_ms: float
    observed_at: datetime
    correlation_id: str | None
    metadata: Mapping[str, Any]


def _percentile(sorted_values: list[float], percent: float) -> float | None:
    if not sorted_values:
        return None
    rank = (len(sorted_values) - 1) * percent
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return sorted_values[lower]
    weight = rank - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def summarize(stage: str, samples_ms: Iterable[float]) -> LatencySummary:
    if stage not in STAGES:
        raise ValueError("unknown latency stage")
    values = sorted(float(value) for value in samples_ms)
    if any(not math.isfinite(value) or value < 0 for value in values):
        raise ValueError("latency samples must be finite and non-negative")
    return LatencySummary(
        stage=stage,
        count=len(values),
        p50_ms=_percentile(values, 0.50),
        p95_ms=_percentile(values, 0.95),
        p99_ms=_percentile(values, 0.99),
        maximum_ms=max(values) if values else None,
    )


class LatencyRecorder:
    def __init__(self, store: LatencyStore, clock_ns=time.monotonic_ns):
        self.store = store
        self.clock_ns = clock_ns

    @contextmanager
    def measure(
        self,
        stage: str,
        *,
        observed_at: datetime,
        correlation_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Iterator[None]:
        span = self.start(
            stage,
            observed_at=observed_at,
            correlation_id=correlation_id,
            metadata=metadata,
        )
        try:
            yield
        finally:
            self.finish(span)

    def start(
        self,
        stage: str,
        *,
        observed_at: datetime,
        correlation_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> LatencySpan:
        """Start a stage without implying that its terminal event occurred."""

        self._validate(stage, observed_at)
        return LatencySpan(
            stage=stage,
            started_ns=self.clock_ns(),
            observed_at=observed_at,
            correlation_id=correlation_id,
            metadata=dict(metadata or {}),
        )

    def finish(
        self,
        span: LatencySpan,
        *,
        observed_at: datetime | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> float:
        """Finish and persist an explicitly completed monotonic stage."""

        measurement = self.stop(
            span,
            observed_at=observed_at,
            metadata=metadata,
        )
        self.record_measurement(measurement)
        return measurement.duration_ms

    def stop(
        self,
        span: LatencySpan,
        *,
        observed_at: datetime | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> LatencyMeasurement:
        """Capture a terminal boundary without doing storage I/O.

        Broker code uses this immediately when an exact acknowledgement is
        received, then persists the sample only after order evidence has been
        handled.  That keeps telemetry writes out of the acknowledgement-to-
        durability safety path without moving the measured endpoint.
        """

        if not isinstance(span, LatencySpan):
            raise TypeError("span must be a LatencySpan")
        finished_at = observed_at or span.observed_at
        self._validate(span.stage, finished_at)
        finished_ns = self.clock_ns()
        duration_ms = (finished_ns - span.started_ns) / 1_000_000
        if duration_ms < 0:
            raise RuntimeError("monotonic clock moved backwards")
        combined = dict(span.metadata)
        combined.update(dict(metadata or {}))
        return LatencyMeasurement(
            stage=span.stage,
            duration_ms=duration_ms,
            observed_at=finished_at,
            correlation_id=span.correlation_id,
            metadata=combined,
        )

    def record_measurement(self, measurement: LatencyMeasurement) -> None:
        if not isinstance(measurement, LatencyMeasurement):
            raise TypeError("measurement must be a LatencyMeasurement")
        self.record_duration(
            measurement.stage,
            measurement.duration_ms,
            observed_at=measurement.observed_at,
            correlation_id=measurement.correlation_id,
            metadata=measurement.metadata,
        )

    def record_duration(
        self,
        stage: str,
        duration_ms: float,
        *,
        observed_at: datetime,
        correlation_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        """Persist a measured duration when its endpoints are already known.

        This is used for durable outbox delivery, where the event timestamp is
        persisted before a later process attempt successfully delivers it.
        """

        self._validate(stage, observed_at)
        value = float(duration_ms)
        if not math.isfinite(value) or value < 0:
            raise ValueError("latency duration must be finite and non-negative")
        self.store.record_latency(
            stage,
            value,
            observed_at,
            correlation_id,
            dict(metadata or {}),
        )

    @staticmethod
    def _validate(stage: str, observed_at: datetime) -> None:
        if stage not in STAGES:
            raise ValueError("unknown latency stage")
        if not isinstance(observed_at, datetime) or observed_at.tzinfo is None:
            raise ValueError("latency observation must be timezone-aware")


class LiveStateLatencyAdapter:
    def __init__(self, store: LiveStateStore, account_key: str):
        self.store = store
        self.account_key = str(account_key)

    def record_latency(
        self,
        stage: str,
        duration_ms: float,
        observed_at: datetime,
        correlation_id: str | None,
        metadata: Mapping[str, Any],
    ) -> None:
        # Metadata belongs in the audit/event correlation domain; the compact
        # sample table stores only stage, duration, and the correlation key.
        self.store.record_latency(
            LatencySample(
                sample_id=str(uuid4()),
                account_key=self.account_key,
                stage=stage,
                duration_microseconds=round(duration_ms * 1000),
                observed_at=observed_at,
                correlation_id=correlation_id,
            )
        )


__all__ = [
    "LatencyRecorder",
    "LatencyMeasurement",
    "LatencySpan",
    "LatencySummary",
    "LiveStateLatencyAdapter",
    "STAGES",
    "summarize",
]
