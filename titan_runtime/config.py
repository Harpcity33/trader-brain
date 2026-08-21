from __future__ import annotations

from dataclasses import dataclass
from datetime import time
import json
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RuntimeConfig:
    project_root: Path
    database_path: Path
    event_export_path: Path
    log_path: Path
    websocket_url: str
    rest_base_url: str
    keychain_service: str
    mode: str
    timezone: str
    start_time: time
    stop_time: time
    snapshot_refresh_seconds: int
    stale_data_seconds: int
    quote_watch_count: int
    eligible_ticker_types: tuple[str, ...]
    candidate_min_signal_strength: float
    candidate_min_price: float
    candidate_max_price: float
    candidate_min_gap_pct: float
    watch_min_gap_pct: float
    candidate_min_dollar_volume: float
    watch_min_signal_strength: float
    under5_min_dollar_volume: float
    max_quote_spread_pct: float
    under5_max_quote_spread_pct: float
    retention_days: int

    @classmethod
    def load(cls, path: str | Path) -> "RuntimeConfig":
        source = Path(path).expanduser().resolve()
        raw: dict[str, Any] = json.loads(source.read_text())
        root = source.parent.parent

        def project_path(value: str) -> Path:
            candidate = Path(value).expanduser()
            return candidate if candidate.is_absolute() else (root / candidate).resolve()

        def clock(value: str) -> time:
            hour, minute = (int(part) for part in value.split(":"))
            return time(hour=hour, minute=minute)

        cfg = cls(
            project_root=root,
            database_path=project_path(raw["database_path"]),
            event_export_path=project_path(raw["event_export_path"]),
            log_path=project_path(raw["log_path"]),
            websocket_url=raw["websocket_url"],
            rest_base_url=raw["rest_base_url"].rstrip("/"),
            keychain_service=raw["keychain_service"],
            mode=raw["mode"],
            timezone=raw["timezone"],
            start_time=clock(raw["start_time_et"]),
            stop_time=clock(raw["stop_time_et"]),
            snapshot_refresh_seconds=int(raw["snapshot_refresh_seconds"]),
            stale_data_seconds=int(raw["stale_data_seconds"]),
            quote_watch_count=int(raw["quote_watch_count"]),
            eligible_ticker_types=tuple(raw.get("eligible_ticker_types", ["CS", "ADRC"])),
            candidate_min_signal_strength=float(raw["candidate_min_signal_strength"]),
            candidate_min_price=float(raw["candidate_min_price"]),
            candidate_max_price=float(raw["candidate_max_price"]),
            candidate_min_gap_pct=float(raw["candidate_min_gap_pct"]),
            watch_min_gap_pct=float(raw.get("watch_min_gap_pct", raw["candidate_min_gap_pct"])),
            candidate_min_dollar_volume=float(raw["candidate_min_dollar_volume"]),
            watch_min_signal_strength=float(
                raw.get("watch_min_signal_strength", raw["candidate_min_signal_strength"])
            ),
            under5_min_dollar_volume=float(raw["under5_min_dollar_volume"]),
            max_quote_spread_pct=float(raw["max_quote_spread_pct"]),
            under5_max_quote_spread_pct=float(raw["under5_max_quote_spread_pct"]),
            retention_days=int(raw["retention_days"]),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.mode != "shadow":
            raise ValueError("Titan Massive runtime is deliberately restricted to shadow mode")
        if not self.websocket_url.startswith("wss://socket.massive.com/"):
            raise ValueError("websocket_url must use Massive's TLS endpoint")
        if not self.rest_base_url.startswith("https://api.massive.com"):
            raise ValueError("rest_base_url must use Massive's TLS endpoint")
        if self.quote_watch_count < 1:
            raise ValueError("quote_watch_count must be positive")
        if not self.eligible_ticker_types:
            raise ValueError("eligible_ticker_types cannot be empty")
        if self.stale_data_seconds < 15:
            raise ValueError("stale_data_seconds must be at least 15")
        if not 0 <= self.watch_min_signal_strength <= self.candidate_min_signal_strength:
            raise ValueError("watch_min_signal_strength must be between 0 and candidate_min_signal_strength")
        if not 0 <= self.watch_min_gap_pct <= self.candidate_min_gap_pct:
            raise ValueError("watch_min_gap_pct must be between 0 and candidate_min_gap_pct")
