#!/usr/bin/env python3
"""Trader Brain Paper Options v1 persistent runtime.

PAPER ONLY. Uses Massive market data, deterministic signal rules, simulated fills,
local persistent state, and Gmail notifications. Contains no broker-write client.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from email.message import EmailMessage
import json
import os
from pathlib import Path
import smtplib
import ssl
import sys
import time as sleep_time
from typing import Any
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from titan_brain.paper_options_engine import (  # noqa: E402
    MassiveClient,
    close_paper_position,
    entry_fill,
    exit_fill,
    load_state,
    mark_equity,
    open_paper_position,
    save_state,
    select_contract,
    signal_from_bars,
    signal_key,
    update_weekly_lock,
    weekly_risk_remaining,
)

CONFIG_PATH = ROOT / "config" / "paper_options_v1_runtime.json"
ENGINE_CONFIG_PATH = ROOT / "config" / "paper_options_signal_engine.json"
LOCAL_ENV_PATH = Path.home() / ".config" / "trader-brain" / "paper-options.env"
DEFAULT_STATE_PATH = Path.home() / ".local" / "state" / "trader-brain" / "paper-options-v1.json"
NY = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class RuntimeState:
    paper_only: bool
    cadence_seconds: int
    weekly_drawdown_pct: float
    normal_trade_risk_pct: tuple[float, float]
    exceptional_trade_risk_pct_max: float


def load_local_env(path: Path = LOCAL_ENV_PATH) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


def load_config() -> tuple[dict[str, Any], dict[str, Any]]:
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    engine = json.loads(ENGINE_CONFIG_PATH.read_text(encoding="utf-8"))
    if cfg.get("mode") != "paper_only":
        raise RuntimeError("paper runtime refuses non-paper mode")
    return cfg, engine


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


def state_path() -> Path:
    custom = os.environ.get("TB_STATE_PATH", "").strip()
    return Path(custom).expanduser() if custom else DEFAULT_STATE_PATH


def massive_client() -> MassiveClient:
    return MassiveClient(
        require_env("MASSIVE_API_KEY"),
        os.environ.get("MASSIVE_API_BASE_URL", "https://api.massive.com"),
    )


def _json_request(req: urlrequest.Request, *, timeout: int = 20) -> dict[str, Any]:
    try:
        with urlrequest.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urlerror.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except urlerror.URLError as exc:
        raise RuntimeError(f"network error: {exc.reason}") from exc


def probe_massive() -> dict[str, Any]:
    key = require_env("MASSIVE_API_KEY")
    base = os.environ.get("MASSIVE_API_BASE_URL", "https://api.massive.com").rstrip("/")
    url = f"{base}/v2/aggs/ticker/SPY/prev?adjusted=true&apiKey={urlparse.quote(key)}"
    req = urlrequest.Request(url, headers={"User-Agent": "TraderBrain/1.0"})
    data = _json_request(req)
    status = str(data.get("status", "")).upper()
    if status not in {"OK", "DELAYED"} or not data.get("results"):
        raise RuntimeError(f"Massive probe returned status={status or 'UNKNOWN'} without SPY data")
    bar = data["results"][0]
    return {
        "ok": True,
        "ticker": data.get("ticker", "SPY"),
        "status": status,
        "previous_close_present": bar.get("c") is not None,
        "volume_present": bar.get("v") is not None,
    }


def send_gmail(subject: str, body: str) -> None:
    sender = require_env("TB_GMAIL_SENDER")
    password = require_env("TB_GMAIL_APP_PASSWORD").replace(" ", "")
    recipient = require_env("TB_GMAIL_RECIPIENT")
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, recipient, subject
    msg.set_content(body)
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context, timeout=20) as smtp:
        smtp.login(sender, password)
        smtp.send_message(msg)


def probe_gmail(*, send_test: bool = False) -> dict[str, Any]:
    sender = require_env("TB_GMAIL_SENDER")
    password = require_env("TB_GMAIL_APP_PASSWORD").replace(" ", "")
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context, timeout=20) as smtp:
        smtp.login(sender, password)
    if send_test:
        send_gmail(
            "Trader Brain — Local Runtime Test",
            "Trader Brain Paper Options v1 local Gmail transport is working.\n"
            "Mode: PAPER ONLY\nNo broker-write authority is enabled.",
        )
    return {"ok": True, "authenticated": True, "test_email_sent": send_test}


def doctor(*, send_test_email: bool = False) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    ok = True
    try:
        checks["massive"] = probe_massive()
    except Exception as exc:
        checks["massive"] = {"ok": False, "error": str(exc)}
        ok = False
    try:
        checks["gmail"] = probe_gmail(send_test=send_test_email)
    except Exception as exc:
        checks["gmail"] = {"ok": False, "error": str(exc)}
        ok = False
    return {
        "status": "READY_FOR_RUNTIME" if ok else "NOT_READY",
        "paper_only": True,
        "broker_write_authority": False,
        "checks": checks,
    }


def daily_bucket(state: dict[str, Any], trading_date: date) -> dict[str, Any]:
    key = trading_date.isoformat()
    daily = state.setdefault("daily", {})
    return daily.setdefault(
        key,
        {
            "premarket_sent": False,
            "eod_sent": False,
            "last_scan_5m": None,
            "last_scan_15m": None,
            "signals_seen": 0,
            "signals_qualified": 0,
            "entries": 0,
            "errors": [],
        },
    )


def _scan_bucket(now: datetime, minutes: int) -> str:
    local = now.astimezone(NY)
    bucket_minute = (local.minute // minutes) * minutes
    return local.replace(minute=bucket_minute, second=0, microsecond=0).isoformat()


def discovery_universe(client: MassiveClient, engine: dict[str, Any]) -> list[str]:
    fixed = list(engine["universe"])
    dynamic: list[str] = []
    for direction in ("gainers", "losers"):
        try:
            dynamic.extend(client.top_movers(direction)[:6])
        except Exception:
            pass
    combined = fixed[:12] + dynamic
    seen: set[str] = set()
    result: list[str] = []
    for symbol in combined:
        if not symbol or symbol in seen or len(symbol) > 6 or "." in symbol:
            continue
        seen.add(symbol)
        result.append(symbol)
        if len(result) >= 20:
            break
    return result


def scan_lane(
    client: MassiveClient,
    engine: dict[str, Any],
    now: datetime,
    *,
    minutes: int,
    lane: str,
) -> tuple[list[Any], list[str]]:
    key = "five_minute" if minutes == 5 else "fifteen_minute"
    rules = engine["signal"][key]
    signals, errors = [], []
    for ticker in discovery_universe(client, engine):
        try:
            bars = client.bars(ticker, minutes, now.date())
            signal = signal_from_bars(
                ticker,
                bars,
                lane=lane,
                minutes=minutes,
                now=now,
                minimum_relative_volume=float(rules["minimum_relative_volume"]),
                minimum_bars=int(rules["minimum_bars"]),
                breakout_lookback_bars=int(rules["breakout_lookback_bars"]),
                minimum_setup_score=float(rules["minimum_setup_score"]),
            )
            if signal:
                signals.append(signal)
        except Exception as exc:
            errors.append(f"{ticker}:{type(exc).__name__}")
    signals.sort(key=lambda s: (s.setup_score, s.relative_volume), reverse=True)
    return signals[: int(engine["max_candidates_per_lane"])], errors


def quote_is_fresh(contract: Any, now: datetime, stale_seconds: int) -> bool:
    if not contract.quote_timestamp_ns:
        return False
    quote_seconds = contract.quote_timestamp_ns / 1_000_000_000
    return 0 <= now.timestamp() - quote_seconds <= stale_seconds


def maybe_enter(
    client: MassiveClient,
    cfg: dict[str, Any],
    engine: dict[str, Any],
    state: dict[str, Any],
    signal: Any,
    now: datetime,
) -> dict[str, Any] | None:
    if signal_key(signal) in set(state.get("notified_signals", [])):
        return None
    if len(state.get("positions", [])) >= int(engine["max_open_positions"]):
        return None
    current_equity = mark_equity(state)
    if update_weekly_lock(state, float(cfg["risk"]["weekly_drawdown_pct_of_monday_equity"]), current_equity):
        return None
    remaining_weekly = weekly_risk_remaining(
        state,
        float(cfg["risk"]["weekly_drawdown_pct_of_monday_equity"]),
        current_equity,
    )
    if remaining_weekly <= 0:
        return None
    option_cfg = engine["options"]
    try:
        chain = client.option_chain(
            signal.ticker,
            now.date() + timedelta(days=int(option_cfg["min_dte"])),
            now.date() + timedelta(days=int(option_cfg["max_dte"])),
        )
        contract = select_contract(
            chain,
            underlying=signal.ticker,
            signal=signal,
            today=now.date(),
            min_dte=int(option_cfg["min_dte"]),
            max_dte=int(option_cfg["max_dte"]),
            min_abs_delta=float(option_cfg["min_abs_delta"]),
            max_abs_delta=float(option_cfg["max_abs_delta"]),
            max_spread_pct=float(option_cfg["max_spread_pct"]),
            min_open_interest=int(option_cfg["min_open_interest"]),
            min_volume=int(option_cfg["min_volume"]),
            max_premium_dollars=min(float(state["cash"]), remaining_weekly),
        )
    except Exception:
        return None
    if contract is None:
        return None
    if not quote_is_fresh(contract, now, int(engine["paper_execution"]["stale_quote_seconds"])):
        return None

    fill = entry_fill(contract, float(engine["paper_execution"]["entry_slippage_fraction_of_spread"]))
    premium_cost = fill * 100
    stop_pct = float(engine["paper_execution"]["stop_loss_pct_of_premium"])
    planned_loss = premium_cost * stop_pct
    normal_risk_pct = max(float(x) for x in cfg["risk"]["normal_trade_risk_pct"])
    allowed_risk_pct = (
        float(cfg["risk"]["exceptional_trade_risk_pct_max"])
        if signal.setup_score >= 90
        else normal_risk_pct
    )
    if planned_loss > current_equity * allowed_risk_pct / 100.0:
        return None
    if premium_cost > remaining_weekly:
        return None

    position = open_paper_position(
        state,
        signal,
        contract,
        fill=fill,
        stop_pct=stop_pct,
        target_pct=float(engine["paper_execution"]["take_profit_pct_of_premium"]),
        risk_pct=round(planned_loss / current_equity * 100, 3),
        now=now,
    )
    send_gmail(
        f"TRADER BRAIN BUY — {signal.ticker} {signal.direction.upper()} [{signal.lane}]",
        "\n".join(
            [
                "PAPER TRADE SIGNAL",
                f"Underlying: {signal.ticker}",
                f"Lane: {signal.lane}",
                f"Direction: {signal.direction.upper()}",
                f"Contract: {contract.ticker}",
                f"Expiration: {contract.expiration_date}",
                f"Strike: {contract.strike}",
                f"Bid/Ask: {contract.bid:.2f} / {contract.ask:.2f}",
                f"Paper limit/fill: {fill:.2f}",
                "Contracts: 1",
                f"Paper premium: ${premium_cost:.2f}",
                f"Planned stop: {position['stop_price']:.2f}",
                f"Target: {position['target_price']:.2f}",
                f"Setup score: {signal.setup_score:.1f}",
                f"Relative volume: {signal.relative_volume:.2f}x",
                f"Underlying trigger: {signal.trigger_price:.2f}",
                f"Underlying invalidation: {signal.invalidation_price:.2f}",
                f"Delta: {contract.delta:.3f}",
                f"IV: {contract.iv if contract.iv is not None else 'N/A'}",
                f"Weekly risk remaining before entry: ${remaining_weekly:.2f}",
                f"Timestamp: {now.isoformat()}",
                "",
                "Mode: PAPER ONLY. No broker order was placed.",
            ]
        ),
    )
    return position


def manage_positions(
    client: MassiveClient,
    engine: dict[str, Any],
    state: dict[str, Any],
    now: datetime,
) -> list[dict[str, Any]]:
    closed: list[dict[str, Any]] = []
    for pos in list(state.get("positions", [])):
        try:
            snap = client.option_snapshot(pos["ticker"], pos["contract"])
            result = snap.get("results") or {}
            quote = result.get("last_quote") or {}
            bid = float(quote.get("bid") or 0)
            ask = float(quote.get("ask") or 0)
            if bid <= 0 or ask <= 0 or ask < bid:
                continue
            mark = bid
            pos["last_mark"] = mark
            reason = None
            if mark <= float(pos["stop_price"]):
                reason = "STOP"
            elif mark >= float(pos["target_price"]):
                reason = "TARGET"
            elif now.time().replace(tzinfo=None) >= time(15, 50):
                reason = "EOD_FLATTEN"
            if reason:
                fill = exit_fill(
                    bid,
                    ask,
                    float(engine["paper_execution"]["exit_slippage_fraction_of_spread"]),
                )
                closed.append(
                    close_paper_position(
                        state,
                        pos["trade_id"],
                        fill=fill,
                        reason=reason,
                        now=now,
                    )
                )
        except Exception:
            continue
    return closed


def premarket_brief(
    client: MassiveClient,
    engine: dict[str, Any],
    state: dict[str, Any],
    now: datetime,
) -> None:
    bucket = daily_bucket(state, now.date())
    if bucket["premarket_sent"]:
        return
    if now.weekday() >= 5 or not (time(7, 0) <= now.time().replace(tzinfo=None) < time(9, 0)):
        return
    try:
        gainers = client.top_movers("gainers")[:6]
        losers = client.top_movers("losers")[:6]
    except Exception:
        gainers, losers = engine["universe"][:6], engine["universe"][6:12]
    fast = (gainers[:2] + losers[:1])[:3]
    options = (gainers[2:4] + losers[1:3])[:3]
    lines = [
        "TRADER BRAIN — PRE-MARKET PAPER BRIEF",
        f"Date: {now.date().isoformat()}",
        "",
        "Top 3 fast 5-minute watches:",
    ]
    for i, ticker in enumerate(fast, 1):
        lines.append(f"{i}. {ticker} — BUY trigger only after completed 5m breakout/breakdown + >=1.5x relative volume.")
    lines.extend(["", "Top 3 15-minute options watches:"])
    for i, ticker in enumerate(options, 1):
        lines.append(f"{i}. {ticker} — BUY trigger only after completed 15m confirmation + >=1.25x relative volume + qualifying option liquidity.")
    lines.extend(
        [
            "",
            "Massive is the quantitative source of truth. Watch status is not a BUY signal.",
            "Mode: PAPER ONLY.",
        ]
    )
    send_gmail("Trader Brain — Pre-Market Paper Brief", "\n".join(lines))
    bucket["premarket_sent"] = True


def grade_letter(value: float) -> str:
    if value >= 90:
        return "A"
    if value >= 80:
        return "B"
    if value >= 70:
        return "C"
    if value >= 60:
        return "D"
    return "F"


def eod_report(cfg: dict[str, Any], state: dict[str, Any], now: datetime) -> None:
    bucket = daily_bucket(state, now.date())
    if bucket["eod_sent"] or now.weekday() >= 5 or now.time().replace(tzinfo=None) < time(16, 5):
        return
    today = now.date().isoformat()
    trades = [t for t in state.get("closed_trades", []) if str(t.get("closed_at", "")).startswith(today)]
    pnl = round(sum(float(t.get("pnl", 0)) for t in trades), 2)
    wins = sum(1 for t in trades if float(t.get("pnl", 0)) > 0)
    losses = sum(1 for t in trades if float(t.get("pnl", 0)) < 0)
    win_rate = (wins / len(trades) * 100) if trades else 0.0
    process_score = 100.0 if not bucket.get("errors") else max(60.0, 100.0 - len(bucket["errors"]) * 5)
    signal_score = 50.0 if not trades else min(100.0, 60.0 + win_rate * 0.4)
    current_equity = mark_equity(state)
    remaining = weekly_risk_remaining(
        state,
        float(cfg["risk"]["weekly_drawdown_pct_of_monday_equity"]),
        current_equity,
    )
    lines = [
        "TRADER BRAIN — END OF MARKET PAPER REPORT",
        f"Date: {today}",
        f"Paper equity: ${current_equity:.2f}",
        f"Day P&L: ${pnl:.2f}",
        f"Closed trades: {len(trades)} | Wins: {wins} | Losses: {losses} | Win rate: {win_rate:.1f}%",
        f"Signals seen: {bucket.get('signals_seen', 0)}",
        f"Qualified entries: {bucket.get('entries', 0)}",
        f"Weekly risk remaining: ${remaining:.2f}",
        f"Weekly lock: {state.get('weekly_lock', False)}",
        "",
        f"Signal quality grade: {grade_letter(signal_score)}",
        f"Process/data grade: {grade_letter(process_score)}",
        "Risk discipline grade: A" if not state.get("weekly_lock") else "Risk discipline grade: REVIEW",
        "",
        "5m and 15m lanes remain separately logged in each trade record.",
        "Mode: PAPER ONLY. No broker orders were placed.",
    ]
    send_gmail("Trader Brain — End of Market Paper Report", "\n".join(lines))
    bucket["eod_sent"] = True


def heartbeat_once(cfg: dict[str, Any], engine: dict[str, Any], now: datetime) -> dict[str, Any]:
    local = now.astimezone(NY)
    path = state_path()
    state = load_state(path, float(cfg["starting_equity_usd"]), local)
    day = daily_bucket(state, local.date())
    client = massive_client()
    premarket_brief(client, engine, state, local)

    closed, entries = [], []
    scan_summary: dict[str, Any] = {"5m": "idle", "15m": "idle"}
    if in_regular_session(local):
        closed = manage_positions(client, engine, state, local)
        equity = mark_equity(state)
        update_weekly_lock(state, float(cfg["risk"]["weekly_drawdown_pct_of_monday_equity"]), equity)

        bucket5 = _scan_bucket(local, 5)
        if day.get("last_scan_5m") != bucket5 and local.minute % 5 in {0, 1, 2}:
            signals, errors = scan_lane(client, engine, local, minutes=5, lane="5m_momentum")
            day["last_scan_5m"] = bucket5
            day["signals_seen"] += len(signals)
            day["errors"].extend(errors[-10:])
            scan_summary["5m"] = len(signals)
            for signal in signals:
                position = maybe_enter(client, cfg, engine, state, signal, local)
                if position:
                    entries.append(position)
                    day["signals_qualified"] += 1
                    day["entries"] += 1
                    if len(state["positions"]) >= int(engine["max_open_positions"]):
                        break

        bucket15 = _scan_bucket(local, 15)
        if day.get("last_scan_15m") != bucket15 and local.minute % 15 in {0, 1, 2}:
            signals, errors = scan_lane(client, engine, local, minutes=15, lane="15m_options")
            day["last_scan_15m"] = bucket15
            day["signals_seen"] += len(signals)
            day["errors"].extend(errors[-10:])
            scan_summary["15m"] = len(signals)
            for signal in signals:
                position = maybe_enter(client, cfg, engine, state, signal, local)
                if position:
                    entries.append(position)
                    day["signals_qualified"] += 1
                    day["entries"] += 1
                    if len(state["positions"]) >= int(engine["max_open_positions"]):
                        break

    eod_report(cfg, state, local)
    current_equity = mark_equity(state)
    save_state(path, state)
    return {
        "timestamp": local.isoformat(),
        "mode": "paper_only",
        "broker_write_authority": False,
        "session_open": in_regular_session(local),
        "cadence_seconds": cfg["heartbeat"]["cadence_seconds"],
        "scans": scan_summary,
        "entries": [p["trade_id"] for p in entries],
        "closed": [p["trade_id"] for p in closed],
        "open_positions": len(state.get("positions", [])),
        "paper_equity": current_equity,
        "weekly_lock": bool(state.get("weekly_lock")),
        "decision": "PAPER_BUY" if entries else "NO_TRADE",
    }


def run_loop() -> int:
    cfg, engine = load_config()
    runtime_state = state_from_config(cfg)
    dependency_state = doctor(send_test_email=False)
    if dependency_state["status"] != "READY_FOR_RUNTIME":
        print(json.dumps(dependency_state, sort_keys=True), file=sys.stderr, flush=True)
        raise RuntimeError("dependency doctor failed; refusing to start heartbeat loop")
    while True:
        try:
            result = heartbeat_once(cfg, engine, datetime.now(tz=NY))
            print(json.dumps(result, sort_keys=True), flush=True)
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "timestamp": datetime.now(tz=NY).isoformat(),
                        "status": "FAIL_CLOSED",
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
                flush=True,
            )
        sleep_time.sleep(runtime_state.cadence_seconds)


def main() -> int:
    load_local_env()
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("check", "doctor", "doctor-email", "once", "loop"))
    args = parser.parse_args()
    cfg, engine = load_config()
    if args.command == "check":
        print(
            json.dumps(
                {
                    "status": "ENGINE_CONFIGURED",
                    "config": str(CONFIG_PATH),
                    "engine_config": str(ENGINE_CONFIG_PATH),
                    "local_env_found": LOCAL_ENV_PATH.is_file(),
                    "state_path": str(state_path()),
                    "state": state_from_config(cfg).__dict__,
                    "paper_only": True,
                    "broker_write_authority": False,
                },
                default=list,
                sort_keys=True,
            )
        )
        return 0
    if args.command == "doctor":
        result = doctor(send_test_email=False)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["status"] == "READY_FOR_RUNTIME" else 2
    if args.command == "doctor-email":
        result = doctor(send_test_email=True)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["status"] == "READY_FOR_RUNTIME" else 2
    if args.command == "once":
        print(json.dumps(heartbeat_once(cfg, engine, datetime.now(tz=NY)), sort_keys=True))
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
