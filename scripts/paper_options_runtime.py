#!/usr/bin/env python3
"""Trader Brain Paper Options v1 runtime orchestrator.

Paper-only. This process owns scheduling/state transitions but performs no live broker writes.
It fails closed unless required market-data and decision-review adapters are explicitly wired.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, time
import json
from pathlib import Path
import sys
import time as sleep_time
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "paper_options_v1_runtime.json"
NY = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class RuntimeState:
    paper_only: bool
    cadence_seconds: int
    weekly_drawdown_pct: float
    normal_trade_risk_pct: tuple[float, float]
    exceptional_trade_risk_pct_max: float


def load_config() -> dict[str, Any]:
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if cfg.get("mode") != "paper_only":
        raise RuntimeError("paper runtime refuses non-paper mode")
    return cfg


def state_from_config(cfg: dict[str, Any]) -> RuntimeState:
    return RuntimeState(
        paper_only=True,
        cadence_seconds=int(cfg["heartbeat"]["cadence_seconds"]),
        weekly_drawdown_pct=float(cfg["risk"]["weekly_drawdown_pct_of_monday_equity"]),
        normal_trade_risk_pct=tuple(float(v) for v in cfg["risk"]["normal_trade_risk_pct"]),
        exceptional_trade_risk_pct_max=float(cfg["risk"]["exceptional_trade_risk_pct_max"]),
    )


def in_regular_session(now: datetime) -> bool:
    local = now.astimezone(NY)
    return local.weekday() < 5 and time(9, 30) <= local.time().replace(tzinfo=None) < time(16, 0)


def heartbeat_once(cfg: dict[str, Any], now: datetime) -> dict[str, Any]:
    """One fail-closed orchestration tick.

    Market-data collection and model escalation must be supplied by adapters in the
    persistent host. Until then, the tick records NO_TRADE rather than inventing a signal.
    """
    local = now.astimezone(NY)
    return {
        "timestamp": local.isoformat(),
        "mode": "paper_only",
        "session_open": in_regular_session(local),
        "cadence_seconds": cfg["heartbeat"]["cadence_seconds"],
        "lanes": cfg["lanes"],
        "decision": "NO_TRADE",
        "reason": "runtime adapters not connected: Massive market data and High-reasoning review required",
        "fail_closed": True,
    }


def run_loop() -> int:
    cfg = load_config()
    state = state_from_config(cfg)
    while True:
        now = datetime.now(tz=NY)
        result = heartbeat_once(cfg, now)
        print(json.dumps(result, sort_keys=True), flush=True)
        sleep_time.sleep(state.cadence_seconds)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("check", "once", "loop"))
    args = parser.parse_args()
    cfg = load_config()
    if args.command == "check":
        print(json.dumps({"status": "READY_FOR_ADAPTERS", "config": str(CONFIG_PATH), "state": state_from_config(cfg).__dict__}, default=list, sort_keys=True))
        return 0
    if args.command == "once":
        print(json.dumps(heartbeat_once(cfg, datetime.now(tz=NY)), sort_keys=True))
        return 0
    return run_loop()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
    except Exception as exc:
        print(json.dumps({"status": "FAIL_CLOSED", "error": str(exc)}), file=sys.stderr)
        raise
