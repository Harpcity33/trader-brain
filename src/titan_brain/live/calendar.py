"""Evidence-backed U.S. equity session calendar and lane transitions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import json
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo


NEW_YORK = ZoneInfo("America/New_York")


def _clock(value: str) -> time:
    try:
        hour, minute = (int(part) for part in value.split(":"))
        return time(hour, minute)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid HH:MM clock: {value!r}") from exc


@dataclass(frozen=True)
class SessionTimes:
    trading_date: date
    open_at: datetime
    close_at: datetime
    entry_cutoff_at: datetime
    closeout_start_at: datetime
    flat_deadline_at: datetime


class ExchangeCalendar:
    """Small deterministic calendar loaded from a checked-in NYSE artifact.

    The calendar is deliberately year-bounded.  A missing future year fails
    closed instead of assuming that a weekday is open.
    """

    def __init__(self, evidence: Mapping[str, Any]):
        self.evidence = dict(evidence)
        self.year = int(evidence["calendar_year"])
        self.open_clock = _clock(str(evidence["regular_open_et"]))
        self.close_clock = _clock(str(evidence["regular_close_et"]))
        self.closed_dates = frozenset(date.fromisoformat(value) for value in evidence["closed_dates"])
        self.early_closes = {
            date.fromisoformat(key): _clock(str(value))
            for key, value in dict(evidence.get("early_close_dates", {})).items()
        }
        if not str(evidence.get("source_url", "")).startswith("https://www.nyse.com/"):
            raise ValueError("calendar evidence must identify the NYSE source")

    @classmethod
    def from_json(cls, path: str | Path) -> "ExchangeCalendar":
        with Path(path).open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        if not isinstance(raw, dict):
            raise ValueError("calendar evidence must be an object")
        return cls(raw)

    def is_trading_day(self, value: date) -> bool:
        if value.year != self.year:
            raise ValueError(f"calendar has no verified evidence for {value.year}")
        return value.weekday() < 5 and value not in self.closed_dates

    def session_times(
        self,
        value: date,
        *,
        entry_cutoff: time = time(15, 30),
        closeout_minutes: int = 10,
        flat_minutes: int = 5,
    ) -> SessionTimes | None:
        if not self.is_trading_day(value):
            return None
        close_clock = self.early_closes.get(value, self.close_clock)
        open_at = datetime.combine(value, self.open_clock, NEW_YORK)
        close_at = datetime.combine(value, close_clock, NEW_YORK)
        configured_cutoff = datetime.combine(value, entry_cutoff, NEW_YORK)
        safety_cutoff = close_at - timedelta(minutes=30)
        return SessionTimes(
            trading_date=value,
            open_at=open_at,
            close_at=close_at,
            entry_cutoff_at=min(configured_cutoff, safety_cutoff),
            closeout_start_at=close_at - timedelta(minutes=closeout_minutes),
            flat_deadline_at=close_at - timedelta(minutes=flat_minutes),
        )

    def lane(self, now: datetime) -> str:
        if now.tzinfo is None:
            raise ValueError("session time must be timezone-aware")
        local = now.astimezone(NEW_YORK)
        session = self.session_times(local.date())
        if session is None:
            return "closed"
        clock = local.timetz().replace(tzinfo=None)
        if time(7, 0) <= clock < time(9, 25):
            return "premarket_attended"
        if time(9, 25) <= clock < time(9, 35):
            return "transition"
        if time(9, 35) <= clock < session.entry_cutoff_at.timetz().replace(tzinfo=None):
            return "regular_entry"
        if local < session.closeout_start_at:
            return "manage_only"
        if local < session.flat_deadline_at:
            return "closeout"
        if local < session.close_at:
            return "flat_deadline"
        return "closed"


__all__ = ["ExchangeCalendar", "NEW_YORK", "SessionTimes"]
