"""Dependency-free, connection-bound health pacing for the legacy collector.

This module is also deployed byte-for-byte as ``titan_runtime/massive_health.py``.
It performs no I/O, reads no credentials, and grants no trading authority. The
caller supplies real receipt clocks only after its market-data handler succeeds;
the Store supplies checked_at at the actual write, never a fabricated timestamp.
"""

from __future__ import annotations

import math
import re
from typing import Any


_SYMBOL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,31}\Z")


def _number(value: Any, *, positive: bool = False) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and (value > 0 if positive else value >= 0)
    except OverflowError:
        return False


def _event_timestamp(event: dict[str, Any], now_ms: int) -> int | None:
    """Conservative validation, not instrument eligibility or quote authority."""
    if type(now_ms) is not int or now_ms <= 0:
        return None
    symbol = event.get("sym")
    if not isinstance(symbol, str) or _SYMBOL.fullmatch(symbol) is None:
        return None
    if event.get("otc", False) is not False:
        return None
    kind = event.get("ev")
    if kind == "Q":
        stamp = event.get("t")
        maximum_age_ms = 15_000
        if not all(_number(event.get(key), positive=True) for key in ("bp", "ap")):
            return None
        if not all(type(event.get(key)) is int and event[key] > 0 for key in ("bs", "as")):
            return None
        if event["ap"] < event["bp"]:
            return None
    elif kind in ("A", "AM"):
        stamp = event.get("e")
        start = event.get("s")
        window_ms = 1_000 if kind == "A" else 60_000
        maximum_age_ms = 15_000 if kind == "A" else 120_000
        if type(start) is not int or type(stamp) is not int:
            return None
        if start <= 0 or not 0 < stamp - start <= window_ms:
            return None
        if not all(_number(event.get(key), positive=True) for key in ("o", "h", "l", "c")):
            return None
        if not event["l"] <= min(event["o"], event["c"]) <= max(event["o"], event["c"]) <= event["h"]:
            return None
        if not _number(event.get("v")):
            return None
    else:
        return None
    if type(stamp) is not int or stamp <= 0:
        return None
    return stamp if -1_000 <= now_ms - stamp <= maximum_age_ms else None


class AuthenticatedMarketHealth:
    """Only new, validated data on the current authenticated socket can pulse.

    Status errors are sticky for that connection. A new connection, accepted
    authentication handshake, and fresh processed market data are all required
    for recovery. Status-only frames and transport pings never pulse health.
    This object is owned by the collector's single receive-loop thread.
    """

    interval_seconds = 5.0

    def __init__(self) -> None:
        self._connection: object | None = None
        self._authenticated = False
        self._failed = True
        self._last_pulse: float | None = None
        self._last_clock: float | None = None
        self._high_water: dict[str, int] = {}

    def begin_connection(self, connection: object) -> None:
        if connection is None:
            raise ValueError("connection identity is required")
        self._connection = connection
        self._authenticated = False
        self._failed = False
        self._last_pulse = None
        self._last_clock = None
        self._high_water.clear()

    def disconnect(self, connection: object) -> None:
        if connection is self._connection:
            self._authenticated = False
            self._failed = True

    def note_status(self, connection: object, status: object) -> bool:
        if connection is None or connection is not self._connection or self._failed:
            return False
        allowed = ("success",) if self._authenticated else ("connected", "auth_success", "success")
        if not isinstance(status, str) or status not in allowed:
            self.disconnect(connection)
            return False
        return True

    def authenticate(self, connection: object, frame: object) -> bool:
        if connection is None or connection is not self._connection or self._failed:
            return False
        events = frame if isinstance(frame, list) else [frame]
        if not events or not all(
            isinstance(event, dict)
            and event.get("ev") == "status"
            and event.get("status") in ("auth_success", "success")
            for event in events
        ) or not any(event.get("status") == "auth_success" for event in events):
            self.disconnect(connection)
            return False
        self._authenticated = True
        return True

    def processed_event(
        self, connection: object, event: object, *, now_ms: int, monotonic: float
    ) -> bool:
        if (
            connection is None or connection is not self._connection
            or self._failed or not self._authenticated or not isinstance(event, dict)
        ):
            return False
        if not _number(monotonic) or (
            self._last_clock is not None and monotonic < self._last_clock
        ):
            self.disconnect(connection)
            return False
        self._last_clock = monotonic
        stamp = _event_timestamp(event, now_ms)
        if stamp is None:
            return False
        kind = event["ev"]
        if stamp <= self._high_water.get(kind, 0):
            return False
        self._high_water[kind] = stamp
        if self._last_pulse is not None and monotonic - self._last_pulse < self.interval_seconds:
            return False
        self._last_pulse = monotonic
        return True
