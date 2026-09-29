"""Run the existing tested paper strategy on a separate ledger, without a chat."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading
import time

from . import VERSION
from .transport import Unavailable
from .store import utc


def load_runtime(repo: Path):
    path = repo / "scripts/paper_options_runtime.py"
    spec = importlib.util.spec_from_file_location("tb_control_legacy_runtime", path)
    if not spec or not spec.loader: raise Unavailable("BASELINE_RUNTIME_MISSING")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def safe_code(exc):
    return str(exc) if isinstance(exc, Unavailable) else type(exc).__name__


class Engine:
    def __init__(self, repo, home, store, *, runtime=None, feed=None, sender=None):
        self.repo, self.home, self.store = Path(repo), Path(home), store
        self.runtime = runtime or load_runtime(self.repo)
        self.runtime.load_local_env()  # existing local Massive/Gmail settings, no shell execution
        self.cfg, self.rules = self.runtime.load_config()
        self.path = self.home / "portfolio.json"
        self.feed = feed
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.tick_lock = threading.Lock()
        self.last = None
        self.error = None
        self.sender = sender or self.runtime.send_gmail
        self.runtime_version = self.runtime.RUNTIME_VERSION
        self.store.put("build", {"control_version": VERSION, "strategy_version": self.runtime_version})

    def client(self):
        if self.feed is None:
            # Reuse the baseline's already-authenticated, read-only market-data route.
            # This avoids a second Robinhood OAuth/token store and keeps one source of truth:
            # Massive for stocks/calendar, Robinhood for options.
            factory = getattr(self.runtime, "market_data_client", None)
            if not callable(factory):
                raise Unavailable("BASELINE_MARKET_DATA_CLIENT_UNAVAILABLE")
            self.feed = factory()
        return self.feed

    def apply_commands(self):
        for command in self.store.pending():
            action = command["action"]
            if action == "resume" and self.store.get("flatten_requested", False):
                self.store.finish(command["id"], "rejected", "Finish the pending paper liquidation before resuming.")
                continue
            if action == "flatten":
                # These settings are idempotent if the process stops between writes.
                self.store.put("paused", True)
                self.store.put("flatten_requested", True)
            else:
                if action == "resume": self.store.put("activated", True)
                self.store.put("paused", action == "pause")
            self.store.finish(command["id"], "applied", "Paper service control applied; this is not a broker fill.")

    def flatten(self, now, client):
        """Use real fresh quotes; a request never fabricates liquidation at a stale mark."""
        rt = self.runtime
        bounds = rt.session_bounds(now, client.market_holidays())
        if not bounds or not (bounds[0] <= now < bounds[1]) or not rt.market_open(client, now):
            return "WAITING_FOR_OPEN_MARKET"
        with rt.state_lock(self.path):
            state = rt.load_state(self.path, 1000, now)
            # Existing manager's end-of-session path liquidates eligible paper positions.
            closed, healthy = rt.manage_positions(client, self.rules, state, now, now)
            rt.save_state(self.path, state)
            if not state["positions"]:
                self.store.put("flatten_requested", False)
                return "PAPER_POSITIONS_CLOSED"
            return "WAITING_FOR_FRESH_EXIT_QUOTES"

    def tick(self, now=None, *, clock=None):
        with self.tick_lock:
            self.apply_commands()
            now = now or datetime.now(self.runtime.NY)
            started = time.monotonic()
            cfg = copy.deepcopy(self.cfg)
            paused = self.store.get("paused", True)
            if paused:
                for lane in cfg["lanes"].values(): lane["enabled"] = False
            try:
                result = None
                if paused and not self.store.get("activated", False):
                    with self.runtime.state_lock(self.path):
                        state = self.runtime.load_state(self.path, 1000, now)
                        # Fresh parallel installs stay quiet: no duplicate email reports or API scans.
                        # Never suppress management if an existing portfolio contains positions.
                        if not state["positions"]:
                            if self.store.get("flatten_requested", False):
                                self.store.put("flatten_requested", False)
                                self.store.put("flatten_status", "NO_OPEN_PAPER_POSITIONS")
                            self.runtime.save_state(self.path, state)
                            result = {"runtime_version": self.runtime_version, "timestamp": now.isoformat(),
                                      "mode": "paper_only", "broker_write_authority": False,
                                      "openai_api_used": False, "session_open": False,
                                      "cadence_seconds": 120, "scans": {}, "entries": [], "closed": [],
                                      "open_positions": 0, "paper_equity": self.runtime.mark_equity(state),
                                      "weekly_lock": state["weekly_lock"], "data_faults": 0,
                                      "decision": "PAUSED_NOT_STARTED", "data_readiness": "NOT_TESTED"}
                if result is None:
                    client = self.client()
                    if self.store.get("flatten_requested", False):
                        self.store.put("flatten_status", self.flatten(now, client))
                    result = self.runtime.heartbeat_once(cfg, self.rules, now, client=client, path=self.path, sender=self.sender, clock=clock)
                result.update({"completed_at": utc(), "elapsed_seconds": round(time.monotonic()-started, 3),
                               "control_version": VERSION, "paused": paused})
                self.store.put("heartbeat", result)
                self.store.event("heartbeat", result)
                self.error = None
            except Exception as exc:
                self.error = safe_code(exc)
                self.store.put("last_error", {"at": utc(), "code": self.error})
                self.store.event("fault", {"code": self.error})
            self.last = utc()

    def run(self):
        deadline = 0.0
        while not self.stop.is_set():
            self.apply_commands()
            # Control changes are handled promptly; strategy decisions remain candle-based.
            if time.monotonic() >= deadline:
                started = time.monotonic()
                self.tick()
                deadline = max(time.monotonic() + 1, started + 120)
            self.wake.wait(min(2, max(0, deadline-time.monotonic())))
            self.wake.clear()

    def snapshot(self):
        rt = self.runtime
        heartbeat = self.store.get("heartbeat")
        state, state_error = None, None
        if self.path.exists():
            try: state = json.loads(self.path.read_text())
            except Exception: state_error = "PORTFOLIO_UNREADABLE"
        now = datetime.now(timezone.utc)
        age = None
        if heartbeat:
            age = max(0, (now-datetime.fromisoformat(heartbeat["completed_at"])).total_seconds())
        positions, trades, watches, rejections = [], [], [], {}
        equity = None
        capacity = None
        weekly = {"percent": 10, "starting_equity": None, "limit": None, "locked": None}
        lane_stats = []
        if state:
            equity = rt.mark_equity(state)
            capacity = rt.new_entry_capacity(state, 10, .65)
            weekly.update({"starting_equity": state.get("week_start_equity"),
                           "limit": round(state.get("week_start_equity", 1000)*.1,2),
                           "locked": bool(state.get("weekly_lock"))})
            allowed = {"trade_id", "ticker", "lane", "direction", "contract", "expiration", "strike", "quantity",
                       "entry_fill", "last_mark", "stop_price", "target_price", "planned_risk_dollars", "premium_cost",
                       "opened_at", "closed_at", "pnl", "exit_reason", "exit_fill"}
            positions = [{k:v for k,v in p.items() if k in allowed} for p in state.get("positions", [])]
            trades = [{k:v for k,v in p.items() if k in allowed} for p in state.get("closed_trades", [])[-50:]]
            days = state.get("daily", {})
            latest = days[sorted(days)[-1]] if days else {}
            watches = latest.get("research", {}).get("watches", [])[:6]
            from collections import Counter
            rejections = dict(Counter(a.get("result", "UNKNOWN") for a in latest.get("audit", [])))
            for lane in ("5m_momentum", "15m_options"):
                rows = [r for r in state.get("closed_trades", []) if r["lane"] == lane]
                lane_stats.append({"lane":lane,"closed":len(rows),"pnl":round(sum(r.get("pnl",0) for r in rows),2),
                                   "expectancy":round(sum(r.get("pnl",0) for r in rows)/len(rows),2) if rows else None})
        return {"version":VERSION, "strategy_version": self.runtime_version, "mode":"PAPER ONLY",
                "live_trading_enabled":False, "openai_api_used":False, "additional_subscriptions":False,
                "paused":self.store.get("paused",True), "flatten_requested":self.store.get("flatten_requested",False),
                "flatten_status":self.store.get("flatten_status"), "heartbeat":heartbeat, "heartbeat_age_seconds":age,
                "healthy":bool(age is not None and age < 150 and not self.error and not state_error and not heartbeat.get("data_faults",0)),
                "error":state_error or self.error, "last_error":self.store.get("last_error"),
                "paper_equity":equity, "weekly":weekly, "remaining_entry_capacity":capacity,
                "positions":positions,"closed_trades":trades,"watches":watches,"rejections":rejections,
                "lane_stats":lane_stats,"commands":self.store.history(),
                "data_sources":{"stocks":"Massive (existing subscription)","options":"Robinhood read-only MCP",
                                "alpaca":"Paper adapter available; not connected or executing"},
                "coverage_note":"Independent paper ledger; existing baseline is unchanged. Quote readiness is not implied by heartbeat.",
                "grading":"Quality grades unassessed. Paper results are not evidence of a live trading edge."}
