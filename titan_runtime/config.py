from __future__ import annotations

from dataclasses import dataclass
from datetime import time
import json
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SizingPolicy:
    """Versioned planning limits used by non-authoritative prepared plans."""

    version: str
    probe_allocation_cap: float
    probe_risk_cap: float
    regular_allocation_ceiling: float
    regular_risk_ceiling: float
    under5_allocation_ceiling: float
    under5_risk_ceiling: float
    reference_risk_unit: float
    initial_risk: float
    strengthened_winner_risk_cap: float
    build_tranches_pct: tuple[int, ...]

    @classmethod
    def defaults(cls) -> "SizingPolicy":
        return cls(
            version="profit_seeking_sizing_2026-08-22_v1",
            probe_allocation_cap=437.50,
            probe_risk_cap=15.0,
            regular_allocation_ceiling=1_062.50,
            regular_risk_ceiling=37.50,
            under5_allocation_ceiling=625.0,
            under5_risk_ceiling=22.50,
            reference_risk_unit=20.0,
            initial_risk=15.0,
            strengthened_winner_risk_cap=30.0,
            build_tranches_pct=(40, 35, 25),
        )

    @classmethod
    def from_mapping(cls, raw: dict[str, Any] | None) -> "SizingPolicy":
        default = cls.defaults()
        values = raw or {}
        policy = cls(
            version=str(values.get("version", default.version)),
            probe_allocation_cap=float(
                values.get("probe_allocation_cap", default.probe_allocation_cap)
            ),
            probe_risk_cap=float(values.get("probe_risk_cap", default.probe_risk_cap)),
            regular_allocation_ceiling=float(
                values.get("regular_allocation_ceiling", default.regular_allocation_ceiling)
            ),
            regular_risk_ceiling=float(
                values.get("regular_risk_ceiling", default.regular_risk_ceiling)
            ),
            under5_allocation_ceiling=float(
                values.get("under5_allocation_ceiling", default.under5_allocation_ceiling)
            ),
            under5_risk_ceiling=float(
                values.get("under5_risk_ceiling", default.under5_risk_ceiling)
            ),
            reference_risk_unit=float(
                values.get("reference_risk_unit", default.reference_risk_unit)
            ),
            initial_risk=float(values.get("initial_risk", default.initial_risk)),
            strengthened_winner_risk_cap=float(
                values.get(
                    "strengthened_winner_risk_cap", default.strengthened_winner_risk_cap
                )
            ),
            build_tranches_pct=tuple(
                int(value)
                for value in values.get("build_tranches_pct", default.build_tranches_pct)
            ),
        )
        policy.validate()
        return policy

    def validate(self) -> None:
        numeric_limits = (
            self.probe_allocation_cap,
            self.probe_risk_cap,
            self.regular_allocation_ceiling,
            self.regular_risk_ceiling,
            self.under5_allocation_ceiling,
            self.under5_risk_ceiling,
            self.reference_risk_unit,
            self.initial_risk,
            self.strengthened_winner_risk_cap,
        )
        if not self.version.strip():
            raise ValueError("sizing_policy.version cannot be empty")
        if any(value <= 0 for value in numeric_limits):
            raise ValueError("all sizing policy limits must be positive")
        if self.probe_allocation_cap > self.regular_allocation_ceiling:
            raise ValueError("probe allocation cannot exceed the regular allocation ceiling")
        if self.probe_risk_cap > self.regular_risk_ceiling:
            raise ValueError("probe risk cannot exceed the regular risk ceiling")
        if self.initial_risk > self.regular_risk_ceiling:
            raise ValueError("initial risk cannot exceed the regular risk ceiling")
        if self.strengthened_winner_risk_cap > self.regular_risk_ceiling:
            raise ValueError("strengthened-winner risk cannot exceed the regular risk ceiling")
        if len(self.build_tranches_pct) != 3 or sum(self.build_tranches_pct) != 100:
            raise ValueError("build_tranches_pct must contain three values totaling 100")
        if any(value <= 0 for value in self.build_tranches_pct):
            raise ValueError("build tranche percentages must be positive")


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
    score_is_entry_gate: bool
    fresh_news_required: bool
    state_is_entry_gate: bool
    entry_states: tuple[str, ...]
    max_spread_to_structural_risk: float
    under5_min_dollar_volume: float
    max_quote_spread_pct: float
    under5_max_quote_spread_pct: float
    retention_days: int
    policy_version: str
    supersedes_policy_version: str | None
    sizing_policy: SizingPolicy

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
            score_is_entry_gate=bool(raw.get("score_is_entry_gate", True)),
            fresh_news_required=bool(raw.get("fresh_news_required", False)),
            state_is_entry_gate=bool(raw.get("state_is_entry_gate", False)),
            entry_states=tuple(raw.get("entry_states", ["BUILDING", "ACCELERATING", "BREAKOUT"])),
            max_spread_to_structural_risk=float(
                raw.get("max_spread_to_structural_risk", 0.15)
            ),
            under5_min_dollar_volume=float(raw["under5_min_dollar_volume"]),
            max_quote_spread_pct=float(raw["max_quote_spread_pct"]),
            under5_max_quote_spread_pct=float(raw["under5_max_quote_spread_pct"]),
            retention_days=int(raw["retention_days"]),
            policy_version=str(
                raw.get("policy_version", raw.get("shadow_policy_version", "unversioned"))
            ),
            supersedes_policy_version=(
                str(raw["supersedes_policy_version"])
                if raw.get("supersedes_policy_version") is not None
                else None
            ),
            sizing_policy=SizingPolicy.from_mapping(raw.get("sizing_policy")),
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
        if self.state_is_entry_gate and not self.entry_states:
            raise ValueError("entry_states cannot be empty when state_is_entry_gate is enabled")
        if not 0 < self.max_spread_to_structural_risk <= 1:
            raise ValueError("max_spread_to_structural_risk must be in (0, 1]")
        if not self.policy_version.strip():
            raise ValueError("policy_version cannot be empty")
        self.sizing_policy.validate()
