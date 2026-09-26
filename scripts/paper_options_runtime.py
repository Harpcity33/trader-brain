#!/usr/bin/env python3
"""Private, deterministic paper simulator. No OpenAI API or broker authority.

A configuration check is not a deployment attestation. Network/SMTP acceptance
is tested separately; CI uses fake feeds and never real credentials.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
from email.message import EmailMessage
import hashlib
import json
import os
from pathlib import Path
import smtplib
import ssl
import sys
import time as timer
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from titan_brain.paper_options_engine import (
    NY, MassiveClient, DataUnavailable, number, contract_from_snapshot, quote_is_fresh,
    signal_from_bars, signal_key, select_contract, entry_fill, exit_fill,
    load_state, save_state, state_lock, mark_equity, weekly_risk_remaining,
    new_entry_capacity, update_weekly_lock, open_paper_position, close_paper_position,
)

CONFIG_PATH = ROOT / "config/paper_options_v1_runtime.json"
ENGINE_CONFIG_PATH = ROOT / "config/paper_options_signal_engine.json"
LOCAL_ENV_PATH = Path.home() / ".config/trader-brain/paper-options.env"
DEFAULT_STATE_PATH = Path.home() / ".local/state/trader-brain/paper-options-v1.json"
RUNTIME_VERSION = "paper-v1.1-safety"


def load_local_env(path: Path = LOCAL_ENV_PATH) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError("invalid local environment line")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key.startswith(("TB_", "MASSIVE_")) and not os.environ.get(key):
            os.environ[key] = value  # no shell expansion or evaluation


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError("missing setting " + name)
    return value


def load_config() -> tuple[dict[str, Any], dict[str, Any]]:
    cfg = json.loads(CONFIG_PATH.read_text())
    engine = json.loads(ENGINE_CONFIG_PATH.read_text())
    if cfg.get("mode") != "paper_only" or number(cfg["starting_equity_usd"]) != 1000:
        raise ValueError("paper-only $1,000 experiment required")
    if number(cfg["risk"]["weekly_drawdown_pct_of_monday_equity"]) != 10:
        raise ValueError("weekly cap must remain 10 percent")
    if cfg["risk"].get("expand_weekly_budget_with_intrawweek_profits") is not False:
        raise ValueError("weekly risk budget expansion forbidden")
    if int(cfg["heartbeat"]["cadence_seconds"]) != 120:
        raise ValueError("heartbeat must be 120 seconds")
    if not 0 < max(cfg["risk"]["normal_trade_risk_pct"]) <= 3:
        raise ValueError("normal risk exceeds approved cap")
    if not 0 < cfg["risk"]["exceptional_trade_risk_pct_max"] <= 4:
        raise ValueError("exceptional risk exceeds approved cap")
    return cfg, engine


def state_path() -> Path:
    return Path(os.environ.get("TB_STATE_PATH") or DEFAULT_STATE_PATH).expanduser()


def massive_client() -> MassiveClient:
    return MassiveClient(require_env("MASSIVE_API_KEY"),
                         os.environ.get("MASSIVE_API_BASE_URL", "https://api.massive.com"))


def safe_error(exc: Exception) -> str:
    return str(exc) if isinstance(exc, DataUnavailable) else type(exc).__name__


def send_gmail(subject: str, body: str, message_id: str = "test") -> None:
    sender, recipient = require_env("TB_GMAIL_SENDER"), require_env("TB_GMAIL_RECIPIENT")
    if sender.casefold() != recipient.casefold() or "\n" in sender or "\r" in sender:
        raise ValueError("only self-sent notifications are authorized")
    password = "".join(require_env("TB_GMAIL_APP_PASSWORD").split())
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, recipient, subject
    msg["Message-ID"] = "<" + hashlib.sha256(message_id.encode()).hexdigest() + "@traderbrain.local>"
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15,
                         context=ssl.create_default_context()) as smtp:
        smtp.login(sender, password)
        if smtp.send_message(msg):
            raise RuntimeError("SMTP recipient rejected")


def doctor(*, send_test_email: bool = False) -> dict[str, Any]:
    checks = {}
    try:
        client = massive_client()
        previous = client.previous_bar("SPY")
        checks["massive_stocks"] = {"ok": True, "previous_close_present": "c" in previous}
        now = datetime.now(NY)
        chain = client.option_chain("SPY", now.date() + timedelta(days=7), now.date() + timedelta(days=21))
        quotes = [c for item in chain if (c := contract_from_snapshot(item, "SPY", now.date()))]
        checks["massive_options"] = {"ok": bool(quotes), "quote_and_greek_records": len(quotes),
                                      "fresh_quotes_now": sum(quote_is_fresh(c, now) for c in quotes),
                                      "note": "weekend/overnight quotes are not actionable"}
    except Exception as exc:
        checks["massive_required_data"] = {"ok": False, "error": safe_error(exc)}
    try:
        sender, recipient = require_env("TB_GMAIL_SENDER"), require_env("TB_GMAIL_RECIPIENT")
        if sender.casefold() != recipient.casefold():
            raise ValueError("self-send required")
        password = "".join(require_env("TB_GMAIL_APP_PASSWORD").split())
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15,
                             context=ssl.create_default_context()) as smtp:
            smtp.login(sender, password)
        if send_test_email:
            send_gmail("Trader Brain — Local Runtime Test", "PAPER ONLY. SMTP transport test; no order placed.",
                       datetime.now(NY).isoformat())
        checks["gmail"] = {"ok": True, "test_email_sent": send_test_email}
    except Exception as exc:
        checks["gmail"] = {"ok": False, "error": safe_error(exc)}
    return {"status": "DEPENDENCIES_OK" if all(c["ok"] for c in checks.values()) else "NOT_READY",
            "paper_only": True, "broker_write_authority": False, "checks": checks}


def daily_bucket(state: dict[str, Any], day: date) -> dict[str, Any]:
    bucket = state["daily"].setdefault(day.isoformat(), {})
    for key, default in {"premarket_sent": False, "eod_sent": False, "last_scan_5m": None,
                         "last_scan_15m": None, "signals_seen": 0, "entries": 0, "errors": [],
                         "audit": [], "scan_count": 0, "risk_violations": 0}.items():
        bucket.setdefault(key, default)
    return bucket


def session_bounds(now: datetime, holidays: list[dict[str, Any]]) -> tuple[datetime, datetime] | None:
    local = now.astimezone(NY)
    if local.weekday() >= 5:
        return None
    opening = datetime.combine(local.date(), time(9, 30), NY)
    closing = datetime.combine(local.date(), time(16), NY)
    for item in holidays:
        if item.get("date") != local.date().isoformat() or item.get("exchange", "").upper() not in {"NYSE", "NASDAQ"}:
            continue
        if item.get("status") == "closed":
            return None
        if item.get("status") == "early-close":
            closing = min(closing, datetime.fromisoformat(item["close"].replace("Z", "+00:00")))
        elif item.get("status") not in {"open", None}:
            raise DataUnavailable("UNKNOWN_CALENDAR_STATUS")
    return opening, closing


def market_open(client: MassiveClient, now: datetime) -> bool:
    data = client.market_status()
    try:
        server = datetime.fromisoformat(data["serverTime"].replace("Z", "+00:00"))
        if abs((now - server).total_seconds()) > 60:
            return False
        return all(data["exchanges"].get(k) == "open" for k in ("nyse", "nasdaq"))
    except (KeyError, ValueError, TypeError):
        return False


def _scan_bucket(now: datetime, minutes: int) -> str:
    return now.replace(minute=now.minute // minutes * minutes, second=0, microsecond=0).isoformat()


def queue_notice(state: dict[str, Any], event_id: str, subject: str, body: str,
                 now: datetime, kind: str = "REPORT") -> None:
    if any(n["id"] == event_id for n in state["outbox"]):
        return
    state["outbox"].append({"id": event_id, "subject": subject, "body": body, "kind": kind,
                            "created_at": now.isoformat(), "status": "pending"})


def flush_outbox(state: dict[str, Any], path: Path, now: datetime, sender=send_gmail) -> None:
    for item in state["outbox"]:
        if item["status"] != "pending":
            continue
        if item["kind"] == "BUY" and (now - datetime.fromisoformat(item["created_at"])).total_seconds() > 180:
            item["status"] = "expired_unsent"
            save_state(path, state)
            continue
        item["status"] = "sending"
        save_state(path, state)  # the simulated order and send intent are durable BEFORE SMTP
        try:
            sender(item["subject"], item["body"], item["id"])
            item["status"] = "sent"
        except Exception as exc:
            item["status"] = "delivery_unknown"
            item["error"] = safe_error(exc)
        save_state(path, state)


def record_fault(state: dict[str, Any], day: dict[str, Any], reason: str, now: datetime) -> None:
    if reason not in day["errors"]:
        day["errors"].append(reason)
    queue_notice(state, f"fault:{now.date()}:{reason}", "Trader Brain — Paper Runtime Fault",
                 f"PAPER ONLY. {reason}. Affected entries are blocked; no result is invented.", now, "FAULT")


def discovery_universe(client: MassiveClient, engine: dict[str, Any]) -> list[str]:
    symbols = list(engine["universe"])
    for direction in ("gainers", "losers"):
        symbols.extend(client.top_movers(direction)[:6])
    return list(dict.fromkeys(s for s in symbols if isinstance(s, str) and s.isalnum() and len(s) <= 6))


def scan_lane(client: MassiveClient, engine: dict[str, Any], now: datetime, *, minutes: int,
              lane: str, symbols: list[str]) -> tuple[list[Any], list[str]]:
    rules = engine["signal"]["five_minute" if minutes == 5 else "fifteen_minute"]
    def one(symbol):
        try:
            bars = client.bars(symbol, minutes, now.date())
            return signal_from_bars(symbol, bars, lane=lane, minutes=minutes, now=now, **rules), None
        except Exception as exc:
            return None, f"{symbol}:{safe_error(exc)}"
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(one, symbols))
    signals = [s for s, _ in outcomes if s is not None]
    signals.sort(key=lambda s: (s.setup_score, s.relative_volume), reverse=True)
    return signals, [e for _, e in outcomes if e]


def maybe_enter(client, cfg, engine, state, signal, now, *, clock=None) -> dict[str, Any] | None:
    clock = clock or (lambda: datetime.now(NY))
    day = daily_bucket(state, now.date())
    def reject(reason):
        day["audit"].append({"signal": signal_key(signal), "at": now.isoformat(), "result": reason})
        return None
    if signal_key(signal) in state["notified_signals"]:
        return reject("DUPLICATE_SIGNAL")
    if (state["weekly_lock"] or len(state["positions"]) >= engine["max_open_positions"]
            or any(p["ticker"] == signal.ticker for p in state["positions"])):
        return reject("RISK_OR_EXISTING_UNDERLYING")
    if any(n["kind"] == "BUY" and n["status"] != "sent" for n in state["outbox"]):
        return reject("PREVIOUS_BUY_DELIVERY_UNRESOLVED")
    option, execution = engine["options"], engine["paper_execution"]
    fee = float(execution.get("fee_per_contract_per_side", .65))  # explicit simulation assumption
    capacity = new_entry_capacity(state, cfg["risk"]["weekly_drawdown_pct_of_monday_equity"], fee)
    try:
        chain = client.option_chain(signal.ticker, now.date() + timedelta(days=option["min_dte"]),
                                    now.date() + timedelta(days=option["max_dte"]))
        fresh_now = clock().astimezone(NY)
        if not 0 <= (fresh_now - datetime.fromisoformat(signal.timestamp)).total_seconds() <= 180:
            return reject("STALE_SIGNAL")
        params = {k: v for k, v in option.items() if k != "max_contracts"}
        contract = select_contract(chain, underlying=signal.ticker, signal=signal, today=now.date(),
                                   max_premium_dollars=max(0, capacity - 2 * fee), now=fresh_now,
                                   stale_seconds=execution["stale_quote_seconds"], **params)
        if contract is None:
            return reject("NO_AFFORDABLE_FRESH_LIQUID_CONTRACT")
        # Re-fetch the EXACT selected contract immediately before its simulated order.
        item = client.option_snapshot(signal.ticker, contract.ticker).get("results") or {}
        contract = select_contract([item], underlying=signal.ticker, signal=signal, today=now.date(),
                                   max_premium_dollars=max(0, capacity - 2 * fee), now=clock(),
                                   stale_seconds=execution["stale_quote_seconds"], **params)
        if contract is None:
            return reject("FINAL_CONTRACT_RECHECK_FAILED")
        fresh_now = clock().astimezone(NY)
        if not 0 <= (fresh_now - datetime.fromisoformat(signal.timestamp)).total_seconds() <= 180:
            return reject("STALE_SIGNAL")
        bounds = session_bounds(fresh_now, client.market_holidays())
        if not bounds or not bounds[0] <= fresh_now < bounds[1] - timedelta(minutes=10):
            return reject("ENTRY_WINDOW_CLOSED")
        if not market_open(client, fresh_now):
            return reject("MARKET_NOT_CONFIRMED_OPEN")
        fresh_now = clock().astimezone(NY)
        if (not quote_is_fresh(contract, fresh_now, execution["stale_quote_seconds"])
                or fresh_now >= bounds[1] - timedelta(minutes=10)
                or not 0 <= (fresh_now - datetime.fromisoformat(signal.timestamp)).total_seconds() <= 180):
            return reject("FINAL_DATA_EXPIRED")
        fill = entry_fill(contract, execution["entry_slippage_fraction_of_spread"])
        full_risk = round(fill * 100 + 2 * fee, 2)
        planned = fill * 100 * execution["stop_loss_pct_of_premium"] + 2 * fee
        equity = mark_equity(state)
        pct = cfg["risk"]["exceptional_trade_risk_pct_max"] if signal.setup_score >= 90 else max(cfg["risk"]["normal_trade_risk_pct"])
        if full_risk > capacity or planned > equity * pct / 100:
            return reject("PORTFOLIO_OR_TRADE_RISK_CAP")
        pos = open_paper_position(state, signal, contract, fill=fill,
                                  stop_pct=execution["stop_loss_pct_of_premium"],
                                  target_pct=execution["take_profit_pct_of_premium"],
                                  risk_pct=planned / equity * 100, now=fresh_now, fee=fee)
        day["audit"].append({"signal": signal_key(signal), "result": "PAPER_BUY", "at": fresh_now.isoformat()})
        body = (f"PAPER ONLY — no broker order placed.\n{contract.ticker}\nLane: {signal.lane}\n"
                f"Candle confirmed: {signal.timestamp}\nBid/ask: {contract.bid:.2f}/{contract.ask:.2f}\n"
                f"Simulated ask-plus-slippage fill: {fill:.4f}; 1 contract\n"
                f"Stop/target premium: {pos['stop_price']:.4f}/{pos['target_price']:.4f}\n"
                f"Underlying trigger/invalidation: {signal.trigger_price}/{signal.invalidation_price}\n"
                f"Recent-bar volume ratio: {signal.relative_volume}; setup score: {signal.setup_score}\n"
                f"Premium plus fee reserve: ${full_risk:.2f}; planned loss: ${planned:.2f}\n"
                f"Quote timestamp (ns): {contract.quote_timestamp_ns}\n"
                "This is an experimental paper signal, not a guaranteed fill or recommendation to trade live.")
        queue_notice(state, "buy:" + pos["trade_id"],
                     f"Trader Brain PAPER BUY — {signal.ticker} {signal.direction.upper()} [{signal.lane}]",
                     body, fresh_now, "BUY")
        return pos
    except Exception as exc:
        record_fault(state, day, "OPTION_DATA:" + safe_error(exc), now)
        return reject("DATA_ERROR:" + safe_error(exc))


def manage_positions(client, engine, state, now, close_at) -> tuple[list[dict[str, Any]], bool]:
    closed, healthy = [], True
    rules = engine["paper_execution"]
    fee = float(rules.get("fee_per_contract_per_side", .65))
    day = daily_bucket(state, now.date())
    for pos in list(state["positions"]):
        try:
            quote = client.option_snapshot(pos["ticker"], pos["contract"])["results"]["last_quote"]
            age = now.timestamp() - int(quote["last_updated"]) / 1e9
            if quote.get("timeframe") != "REAL-TIME" or not 0 <= age <= rules["stale_quote_seconds"]:
                raise DataUnavailable("STALE_POSITION_QUOTE")
            bid, ask = number(quote["bid"]), number(quote["ask"])
            if not 0 <= bid <= ask:
                raise DataUnavailable("INVALID_POSITION_QUOTE")
            if bid > 0 and int(quote.get("bid_size", 0)) < pos["quantity"]:
                raise DataUnavailable("INSUFFICIENT_EXIT_DEPTH")
            pos["last_mark"] = bid
            sample = bid * 100 - pos["premium_cost"]
            pos["sampled_mae"] = min(pos.get("sampled_mae", 0), sample)
            pos["sampled_mfe"] = max(pos.get("sampled_mfe", 0), sample)
            reason = ("STOP" if bid <= pos["stop_price"] else "TARGET" if bid >= pos["target_price"]
                      else "EOD_FLATTEN" if now >= close_at - timedelta(minutes=10) else None)
            if reason is None:
                bars = client.bars(pos["ticker"], 1, now.date())
                valid = [b for b in bars if 0 <= now.timestamp() - (int(b["t"]) / 1000 + 60) <= 180]
                if not valid:
                    raise DataUnavailable("UNDERLYING_EXIT_DATA_STALE")
                price = number(max(valid, key=lambda b: b["t"])["c"])
                invalidation = pos["signal"]["invalidation_price"]
                if (pos["direction"] == "call" and price <= invalidation
                        or pos["direction"] == "put" and price >= invalidation):
                    reason = "UNDERLYING_INVALIDATION"
            if reason:
                closed.append(close_paper_position(state, pos["trade_id"],
                              fill=exit_fill(bid, ask, rules["exit_slippage_fraction_of_spread"]),
                              reason=reason, now=now, fee=fee))
        except Exception as exc:
            healthy = False
            record_fault(state, day, "POSITION_DATA:" + safe_error(exc), now)
    return closed, healthy


def premarket_brief(client, engine, state, now) -> None:
    day = daily_bucket(state, now.date())
    if day["premarket_sent"] or not time(7) <= now.time().replace(tzinfo=None) < time(9, 30):
        return
    if "research" not in day:
        ranked, errors = [], []
        for symbol in engine["universe"]:
            try:
                bar = client.previous_bar(symbol)
                close, opening, high, low, volume = (number(bar[k]) for k in ("c", "o", "h", "l", "v"))
                observed = datetime.fromtimestamp(int(bar["t"]) / 1000, NY)
                if min(close, opening, high, low, volume) <= 0 or not 1 <= (now.date() - observed.date()).days <= 5:
                    continue
                ranked.append({"ticker": symbol, "direction": "call" if close >= opening else "put",
                               "trigger": high if close >= opening else low, "invalidation": low if close >= opening else high,
                               "rank": abs(close / opening - 1) * (close * volume) ** .25,
                               "source_date": observed.date().isoformat()})
            except Exception as exc:
                errors.append(f"{symbol}:{safe_error(exc)}")
        ranked.sort(key=lambda x: x["rank"], reverse=True)
        day["research"] = {"collected_at": now.isoformat(), "watches": ranked[:6], "errors": errors}
    if now.time().replace(tzinfo=None) < time(8):
        return  # one consolidated brief after the requested 07:00-08:00 window
    research = day["research"]
    lines = ["PAPER OPTIONS — deterministic premarket screen (not an AI research session).",
             "Prior-session momentum/liquidity ranking; these are WATCH candidates, not BUY signals.",
             f"Data collected: {research['collected_at']}"]
    for label, picks in (("15-minute options watches", research["watches"][:3]),
                         ("5-minute day-trade watches (also options)", research["watches"][3:6])):
        lines.append("\n" + label)
        if not picks:
            lines.append("No data-qualified watch; never padded with guessed candidates.")
        for p in picks:
            lines.append(f"{p['ticker']} {p['direction'].upper()}: reference {p['trigger']:.2f}; "
                         f"invalidation {p['invalidation']:.2f}; source {p['source_date']}. "
                         "Require a new completed-bar continuation, volume confirmation, and affordable fresh option quote.")
    lines += ["\nIntraday scanner independently evaluates its whole universe; a watch is not an order.",
              "No live broker authority. Full option premium must fit remaining weekly capacity.",
              f"Unavailable source records: {len(research['errors'])}"]
    queue_notice(state, f"pre:{now.date()}", "Trader Brain — Pre-Market Paper Brief", "\n".join(lines), now)
    day["premarket_sent"] = True


def eod_report(cfg, state, now, close_at) -> None:
    day = daily_bucket(state, now.date())
    if day["eod_sent"] or now < close_at + timedelta(minutes=5):
        return
    trades = [t for t in state["closed_trades"] if t["closed_at"][:10] == now.date().isoformat()]
    lines = [f"PAPER REPORT — {now.date()}", f"Indicative equity: ${mark_equity(state):.2f}",
             f"Realized P&L after modeled slippage/fees: ${sum(t['pnl'] for t in trades):.2f}",
             f"Open positions (not presumed flat): {len(state['positions'])}",
             f"Remaining weekly new-entry capacity: ${new_entry_capacity(state, 10, .65):.2f}",
             f"Weekly loss lock: {state['weekly_lock']}", f"Completed lane scans: {day['scan_count']}"]
    for lane in ("5m_momentum", "15m_options"):
        items = [t for t in trades if t["lane"] == lane]
        gains = sum(max(0, t["pnl"]) for t in items)
        losses = -sum(min(0, t["pnl"]) for t in items)
        pnl = sum(t["pnl"] for t in items)
        factor = f"{gains / losses:.2f}" if losses else "N/A (no losses)"
        lines.append(f"{lane}: {len(items)} closed, net ${pnl:.2f}, profit factor {factor}, "
                     f"expectancy {pnl / len(items):.2f}" if items else f"{lane}: no closed trades; statistics N/A")
    lines += ["Signal/entry/exit quality grades: UNASSESSED — no validated grading rubric yet.",
              "Risk adherence: " + ("FAIL" if day["risk_violations"] else "PASS on recorded checks" if day["scan_count"] else "N/A"),
              "Data integrity: " + ("REVIEW" if day["errors"] else "no recorded errors; not a completeness guarantee"),
              "Inactivity is not a failing grade. Missed setups outside recorded scans: UNKNOWN.",
              f"Recorded rejection reasons: {dict(Counter(a['result'] for a in day['audit']))}",
              "MAE/MFE are sampled observations, not tick-level extremes.",
              "Learning review: compare recorded lane outcomes and data gaps; no automatic strategy/risk changes.",
              "Subscription costs are not deducted: allocations unavailable. No proven trading edge is claimed."]
    queue_notice(state, f"eod:{now.date()}", "Trader Brain — End of Market Paper Report", "\n".join(lines), now)
    day["eod_sent"] = True


def heartbeat_once(cfg, engine, now, *, client=None, path=None, sender=send_gmail, clock=None) -> dict[str, Any]:
    local = now.astimezone(NY)
    path = path or state_path()
    clock = clock or (lambda: datetime.now(NY))
    entries, closed, scans = [], [], {}
    session = False
    with state_lock(path):
        state = load_state(path, cfg["starting_equity_usd"], local)
        day = daily_bucket(state, local.date())
        # Weekends do not call market-data or SMTP, even during an offline CI smoke test.
        if local.weekday() < 5:
            client = client or massive_client()
            try:
                bounds = session_bounds(local, client.market_holidays())
                if bounds:
                    opening, closing = bounds
                    session = opening <= local < closing and market_open(client, local)
                    if session:
                        closed, healthy = manage_positions(client, engine, state, local, closing)
                        update_weekly_lock(state, 10, mark_equity(state))
                        eligible = (healthy and not state["weekly_lock"]
                                    and not Path.home().joinpath(".config/trader-brain/STOP_PAPER").exists()
                                    and local < closing - timedelta(minutes=10))
                        if eligible:
                            symbols = None
                            for minutes, lane, config_key in ((5, "5m_momentum", "momentum_5m"),
                                                               (15, "15m_options", "options_15m")):
                                if not cfg["lanes"][config_key]["enabled"]:
                                    continue
                                key, bucket = f"last_scan_{minutes}m", _scan_bucket(local, minutes)
                                if day[key] == bucket:
                                    continue
                                if symbols is None:
                                    symbols = discovery_universe(client, engine)
                                signals, errors = scan_lane(client, engine, local, minutes=minutes, lane=lane, symbols=symbols)
                                day[key] = bucket
                                day["scan_count"] += 1
                                day["signals_seen"] += len(signals)
                                scans[lane] = {"symbols": len(symbols), "signals": len(signals), "errors": len(errors)}
                                if errors:
                                    day["audit"].append({"at": local.isoformat(), "result": "SCAN_DATA_GAP", "details": errors})
                                    record_fault(state, day, "SCAN_DATA_UNAVAILABLE", local)
                                for signal in signals:
                                    pos = maybe_enter(client, cfg, engine, state, signal, clock(), clock=clock)
                                    if pos:
                                        entries.append(pos["trade_id"])
                                        day["entries"] += 1
                                        save_state(path, state)  # persist before any following network call
                                        flush_outbox(state, path, clock(), sender)
                    premarket_brief(client, engine, state, local)
                    if local >= closing and state["positions"]:
                        record_fault(state, day, "UNRESOLVED_AFTER_CLOSE_POSITION", local)
                    eod_report(cfg, state, local, closing)
            except Exception as exc:
                record_fault(state, day, safe_error(exc), local)
        save_state(path, state)
        if local.weekday() < 5:
            flush_outbox(state, path, clock(), sender)
        return {"runtime_version": RUNTIME_VERSION, "timestamp": local.isoformat(), "mode": "paper_only",
                "broker_write_authority": False, "openai_api_used": False, "session_open": session,
                "cadence_seconds": 120, "scans": scans, "entries": entries,
                "closed": [p["trade_id"] for p in closed], "open_positions": len(state["positions"]),
                "paper_equity": mark_equity(state), "weekly_lock": state["weekly_lock"],
                "decision": "PAPER_BUY" if entries else "NO_TRADE", "data_faults": len(day["errors"])}


def main() -> int:
    load_local_env()
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("check", "doctor", "doctor-email", "once", "loop"))
    args = parser.parse_args()
    cfg, engine = load_config()
    if args.command == "check":
        print(json.dumps({"status": "ENGINE_CONFIGURED", "runtime_version": RUNTIME_VERSION,
                          "paper_only": True, "broker_write_authority": False,
                          "openai_api_used": False, "state_path": str(state_path()),
                          "note": "configuration only; not a running-service attestation"}))
        return 0
    if args.command in {"doctor", "doctor-email"}:
        result = doctor(send_test_email=args.command == "doctor-email")
        print(json.dumps(result))
        return 0 if result["status"] == "DEPENDENCIES_OK" else 2
    while True:
        started = timer.monotonic()
        try:
            print(json.dumps(heartbeat_once(cfg, engine, datetime.now(NY))), flush=True)
        except Exception as exc:
            print(json.dumps({"status": "FAIL_CLOSED", "error": safe_error(exc)}), file=sys.stderr, flush=True)
            if args.command == "once":
                return 2
        if args.command == "once":
            return 0
        timer.sleep(max(1, cfg["heartbeat"]["cadence_seconds"] - (timer.monotonic() - started)))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
    except Exception as exc:
        print(json.dumps({"status": "FAIL_CLOSED", "error": safe_error(exc)}), file=sys.stderr)
        raise SystemExit(2)
