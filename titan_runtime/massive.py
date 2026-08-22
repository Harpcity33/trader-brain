from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, time, timedelta, timezone
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
from statistics import median
import subprocess
import threading
import time as time_module
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .config import RuntimeConfig
from .policy import evaluate_shadow_candidate
from .ranking import apply_weighted_opportunity_scale, build_preliminary_trade_plan
from .signals import Bar, compute_market_signal, trigger_cross_payload
from .storage import Store, utc_now
from .websocket_client import MinimalWebSocket, WebSocketError


def load_api_key(service: str) -> str:
    env_value = os.environ.get("MASSIVE_API_KEY", "").strip()
    if env_value:
        return env_value
    result = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-s", service, "-w"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(
            f"Massive API key not found. Add it to macOS Keychain with service '{service}'."
        )
    return result.stdout.strip()


def setup_logger(config: RuntimeConfig) -> logging.Logger:
    config.log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("titan.massive")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)sZ %(levelname)s %(message)s")
    handler = RotatingFileHandler(config.log_path, maxBytes=5_000_000, backupCount=5)
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)
    return logger


class ProcessLock:
    def __init__(self, path: Path):
        self.path = path
        self.handle: Any = None

    def __enter__(self) -> "ProcessLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another Titan Massive watcher is already running") from exc
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(str(os.getpid()))
        self.handle.flush()
        return self

    def __exit__(self, *_: Any) -> None:
        if self.handle:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


class MassiveREST:
    def __init__(self, config: RuntimeConfig, api_key: str):
        self.config = config
        self.api_key = api_key

    def full_snapshot(self) -> list[dict[str, Any]]:
        query = urlencode({"apiKey": self.api_key})
        url = f"{self.config.rest_base_url}/v2/snapshot/locale/us/markets/stocks/tickers?{query}"
        request = Request(url, headers={"User-Agent": "titan-momentum-watcher/0.1"})
        with urlopen(request, timeout=45) as response:
            payload = json.load(response)
        if payload.get("status") not in ("OK", "DELAYED"):
            raise RuntimeError(f"Massive snapshot returned status {payload.get('status')!r}")
        return payload.get("tickers") or []

    def active_equity_universe(self, ticker_types: tuple[str, ...]) -> list[dict[str, Any]]:
        """Fetch every active eligible U.S. equity reference record with pagination."""
        results: list[dict[str, Any]] = []
        for ticker_type in ticker_types:
            query = urlencode(
                {
                    "market": "stocks", "locale": "us", "active": "true",
                    "type": ticker_type, "limit": 1000, "sort": "ticker",
                    "order": "asc", "apiKey": self.api_key,
                }
            )
            url: str | None = f"{self.config.rest_base_url}/v3/reference/tickers?{query}"
            while url:
                separator = "&" if "?" in url else "?"
                request_url = url if "apiKey=" in url else f"{url}{separator}apiKey={self.api_key}"
                request = Request(request_url, headers={"User-Agent": "titan-momentum-watcher/0.2"})
                with urlopen(request, timeout=45) as response:
                    payload = json.load(response)
                if payload.get("status") not in ("OK", "DELAYED"):
                    raise RuntimeError(
                        f"Massive reference tickers returned status {payload.get('status')!r}"
                    )
                results.extend(payload.get("results") or [])
                url = payload.get("next_url")
        unique = {str(item["ticker"]): item for item in results if item.get("ticker")}
        return [unique[symbol] for symbol in sorted(unique)]

    def ticker_overview(self, symbol: str) -> dict[str, Any]:
        query = urlencode({"apiKey": self.api_key})
        url = f"{self.config.rest_base_url}/v3/reference/tickers/{symbol}?{query}"
        request = Request(url, headers={"User-Agent": "titan-momentum-watcher/0.2"})
        with urlopen(request, timeout=45) as response:
            payload = json.load(response)
        if payload.get("status") not in ("OK", "DELAYED"):
            raise RuntimeError(f"Massive ticker overview returned status {payload.get('status')!r}")
        return payload.get("results") or {}

    def daily_aggregates(self, symbol: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
        query = urlencode(
            {"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": self.api_key}
        )
        url = (
            f"{self.config.rest_base_url}/v2/aggs/ticker/{symbol}/range/1/day/"
            f"{start_date}/{end_date}?{query}"
        )
        request = Request(url, headers={"User-Agent": "titan-momentum-watcher/0.2"})
        with urlopen(request, timeout=45) as response:
            payload = json.load(response)
        if payload.get("status") not in ("OK", "DELAYED"):
            raise RuntimeError(f"Massive daily aggregates returned status {payload.get('status')!r}")
        return payload.get("results") or []


def historical_analog_features(rows: list[dict[str, Any]], direction: str) -> dict[str, Any]:
    analogs: list[tuple[float, bool]] = []
    previous_close: float | None = None
    for row in rows:
        open_price = float(row.get("o") or 0)
        high = float(row.get("h") or 0)
        low = float(row.get("l") or 0)
        close = float(row.get("c") or 0)
        if previous_close and open_price > 0:
            gap_pct = (open_price / previous_close - 1) * 100
            matching = gap_pct >= 4 if direction == "UP" else gap_pct <= -4
            if matching:
                if direction == "UP":
                    follow_through = max(0.0, (high / open_price - 1) * 100)
                    faded = close < open_price
                else:
                    follow_through = max(0.0, (1 - low / open_price) * 100)
                    faded = close > open_price
                analogs.append((follow_through, faded))
        if close > 0:
            previous_close = close
    if not analogs:
        return {"historical_follow_through": None, "gap_fade_risk": None, "analog_count": 0}
    median_follow = median(value for value, _ in analogs)
    follow_score = max(0.0, min(100.0, median_follow / 8.0 * 100))
    fade_risk = sum(1 for _, faded in analogs if faded) / len(analogs) * 100
    return {
        "historical_follow_through": round(follow_score, 2),
        "historical_median_follow_through_pct": round(median_follow, 3),
        "gap_fade_risk": round(fade_risk, 2),
        "analog_count": len(analogs),
    }


class CandidateEnricher(threading.Thread):
    """On-demand Massive fundamentals and 90-day analog enrichment for every candidate."""

    def __init__(self, config: RuntimeConfig, api_key: str, stop_event: threading.Event, logger: logging.Logger):
        super().__init__(name="titan-candidate-enricher", daemon=True)
        self.config = config
        self.api_key = api_key
        self.stop_event = stop_event
        self.logger = logger
        self.jobs: queue.Queue[tuple[str, str]] = queue.Queue()
        self.queued: set[tuple[str, str]] = set()
        self.lock = threading.Lock()

    def request(self, symbol: str, direction: str) -> None:
        job = (symbol, direction)
        with self.lock:
            if job in self.queued:
                return
            self.queued.add(job)
        self.jobs.put(job)

    def run(self) -> None:
        rest = MassiveREST(self.config, self.api_key)
        store = Store(self.config.database_path)
        try:
            while not self.stop_event.is_set():
                try:
                    symbol, direction = self.jobs.get(timeout=1)
                except queue.Empty:
                    continue
                try:
                    today = datetime.now(ZoneInfo(self.config.timezone)).date()
                    overview = rest.ticker_overview(symbol)
                    rows = rest.daily_aggregates(
                        symbol, (today - timedelta(days=150)).isoformat(),
                        (today - timedelta(days=1)).isoformat(),
                    )
                    features = historical_analog_features(rows, direction)
                    payload = {
                        "symbol": symbol, "direction": direction, "as_of_date": today.isoformat(),
                        "market_cap": overview.get("market_cap"),
                        "weighted_shares_outstanding": overview.get("weighted_shares_outstanding"),
                        "sic_code": overview.get("sic_code"),
                        "sic_description": overview.get("sic_description"),
                        **features,
                    }
                    store.upsert_candidate_enrichment(payload)
                    self.logger.info(
                        "candidate_enriched symbol=%s direction=%s analogs=%d",
                        symbol, direction, payload["analog_count"],
                    )
                except Exception as exc:
                    self.logger.warning(
                        "candidate_enrichment_failed symbol=%s error=%s", symbol, str(exc)[:300]
                    )
                finally:
                    self.jobs.task_done()
        finally:
            store.close()


class SnapshotRefresher(threading.Thread):
    def __init__(self, config: RuntimeConfig, api_key: str, stop_event: threading.Event, logger: logging.Logger):
        super().__init__(name="titan-snapshot-refresher", daemon=True)
        self.config = config
        self.api_key = api_key
        self.stop_event = stop_event
        self.logger = logger
        self.last_success: float | None = None

    def run(self) -> None:
        rest = MassiveREST(self.config, self.api_key)
        store = Store(self.config.database_path)
        try:
            while not self.stop_event.is_set():
                try:
                    rows = rest.full_snapshot()
                    count = store.upsert_snapshots(rows)
                    self.last_success = time_module.time()
                    store.set_health("massive_rest", "healthy", {"snapshot_tickers": count})
                    self.logger.info("snapshot_refresh_complete tickers=%d", count)
                except Exception as exc:
                    store.set_health("massive_rest", "degraded", {"error": str(exc)[:300]})
                    self.logger.warning("snapshot_refresh_failed error=%s", str(exc)[:300])
                self.stop_event.wait(self.config.snapshot_refresh_seconds)
        finally:
            store.close()


class TitanWatcher:
    BASE_SUBSCRIPTIONS = ("AM.*", "LULD.*")

    def __init__(self, config: RuntimeConfig, api_key: str):
        self.config = config
        self.api_key = api_key
        self.store = Store(config.database_path)
        self.logger = setup_logger(config)
        self.stop_event = threading.Event()
        self.snapshot_refresher = SnapshotRefresher(config, api_key, self.stop_event, self.logger)
        self.candidate_enricher = CandidateEnricher(
            config, api_key, self.stop_event, self.logger
        )
        self.client: MinimalWebSocket | None = None
        self.dynamic_symbols: set[str] = set()
        self.crossed_triggers: dict[str, float] = {}
        self.bars: dict[str, deque[Bar]] = defaultdict(lambda: deque(maxlen=30))
        self.last_message_monotonic = time_module.monotonic()
        self.last_stale_event_at = 0.0
        self.et = ZoneInfo(config.timezone)
        self.universe_ready = False

    def _bootstrap_market_scope(self) -> None:
        """Refresh the full universe and baseline at the 04:00 live-data start."""
        rest = MassiveREST(self.config, self.api_key)
        universe = rest.active_equity_universe(self.config.eligible_ticker_types)
        universe_count = self.store.replace_eligible_universe(universe)
        snapshots = rest.full_snapshot()
        snapshot_count = self.store.upsert_snapshots(snapshots)
        coverage = self.store.snapshot_coverage()
        coverage.update(
            {
                "reference_records": universe_count,
                "raw_snapshot_records": snapshot_count,
                "eligible_ticker_types": list(self.config.eligible_ticker_types),
                "current_session_plan_count": 0,
                "note": (
                    "04:00 startup refreshes the eligible universe and baseline, then current-session "
                    "plans develop as live bars arrive and remain non-authoritative until all Titan gates pass."
                ),
            }
        )
        self.store.set_health("market_scope_coverage", "healthy", coverage)
        self.store.set_metadata("universe_bootstrapped_at", utc_now())
        self.universe_ready = universe_count > 0
        self.logger.info(
            "market_scope_bootstrap universe=%d snapshots=%d broad_qualifiers=%d",
            universe_count, snapshot_count, coverage.get("broad_qualifier_count", 0),
        )

    def _session_start_ms(self) -> int:
        now_et = datetime.now(self.et)
        start_et = datetime.combine(now_et.date(), time(4, 0), tzinfo=self.et)
        return int(start_et.timestamp() * 1000)

    def _load_session_bars(self, symbol: str) -> deque[Bar]:
        existing = self.bars[symbol]
        if existing:
            return existing
        rows = self.store.recent_bars_since(symbol, self._session_start_ms(), 30)
        existing.extend(Bar.from_row(row) for row in rows)
        return existing

    def _export_event(self, event: dict[str, Any]) -> None:
        self.config.event_export_path.parent.mkdir(parents=True, exist_ok=True)
        with self.config.event_export_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, separators=(",", ":")) + "\n")

    def emit(
        self,
        event_type: str,
        symbol: str | None,
        priority: int,
        payload: dict[str, Any],
        dedupe_key: str,
    ) -> None:
        event_id = self.store.emit_event(event_type, symbol, priority, payload, dedupe_key)
        if event_id:
            self._export_event(
                {
                    "event_id": event_id,
                    "created_at": utc_now(),
                    "event_type": event_type,
                    "symbol": symbol,
                    "priority": priority,
                    "payload": payload,
                }
            )
            self.logger.info("event_emitted type=%s symbol=%s priority=%d", event_type, symbol, priority)

    def _handle_minute(self, event: dict[str, Any]) -> None:
        if not event.get("sym") or event.get("otc"):
            return
        self.store.insert_minute_bar(event)
        if not self.universe_ready or not self.store.is_eligible_security(event["sym"]):
            return
        bar = Bar(
            symbol=event["sym"], start_ms=int(event["s"]), end_ms=int(event["e"]),
            open=float(event["o"]), high=float(event["h"]), low=float(event["l"]),
            close=float(event["c"]), volume=float(event.get("dv") or event.get("v") or 0),
            window_vwap=event.get("vw"), session_vwap=event.get("a"),
            accumulated_volume=float(event.get("dav") or event.get("av") or 0),
            official_open=event.get("op"), otc=bool(event.get("otc")),
        )
        bars = self._load_session_bars(bar.symbol)
        if bars and bars[-1].start_ms == bar.start_ms:
            bars[-1] = bar
        else:
            bars.append(bar)
        snapshot = self.store.get_snapshot(bar.symbol)
        quote = self.store.get_quote(bar.symbol)
        threshold = self.config.under5_max_quote_spread_pct if bar.close < 5 else self.config.max_quote_spread_pct
        signal = compute_market_signal(list(bars), snapshot, quote, threshold)
        if not signal:
            return

        previous = self.store.get_candidate(bar.symbol)
        previous_payload: dict[str, Any] = {}
        if previous and previous.get("payload_json"):
            try:
                previous_payload = json.loads(previous["payload_json"])
            except (TypeError, ValueError):
                previous_payload = {}
        policy = evaluate_shadow_candidate(signal, self.config)
        entry_eligible = policy.entry_eligible
        watch_eligible = policy.watch_eligible
        if not entry_eligible and not watch_eligible:
            self.store.delete_candidate(bar.symbol)
            self.crossed_triggers.pop(bar.symbol, None)
            return

        rejection_reasons = list(policy.rejection_reasons)
        signal["opportunity_score_role"] = (
            "ranking_only" if not self.config.score_is_entry_gate else "hard_gate"
        )
        signal["fresh_news_required"] = self.config.fresh_news_required
        signal["state_is_entry_gate"] = self.config.state_is_entry_gate
        signal["disposition"] = "ENTRY_CANDIDATE" if entry_eligible else "ENTRY_REJECTED_KEEP_WATCH"
        signal["entry_rejection_reasons"] = rejection_reasons
        signal["watch_rearm_requirement"] = (
            "A fresh controlled base, reclaim, or renewed acceleration must independently satisfy every structural and liquidity gate."
            if not entry_eligible else None
        )
        enrichment = self.store.get_candidate_enrichment(bar.symbol, signal["direction"])
        current_date = datetime.now(self.et).date().isoformat()
        if enrichment and enrichment.get("as_of_date") == current_date:
            for key in (
                "historical_follow_through", "historical_median_follow_through_pct",
                "gap_fade_risk", "analog_count", "market_cap",
                "weighted_shares_outstanding", "sic_code", "sic_description",
            ):
                if enrichment.get(key) is not None:
                    signal[key] = enrichment[key]
        else:
            self.candidate_enricher.request(bar.symbol, signal["direction"])
        apply_weighted_opportunity_scale(signal)
        self.store.upsert_candidate(signal)
        self.store.save_prepared_trade_plan(build_preliminary_trade_plan(signal))
        if signal.get("base_high") != (previous or {}).get("base_high"):
            self.crossed_triggers.pop(bar.symbol, None)
        if not entry_eligible:
            self.crossed_triggers.pop(bar.symbol, None)
        crossed_threshold = bool(
            entry_eligible
            and self.config.score_is_entry_gate
            and (not previous or previous["signal_strength"] < self.config.candidate_min_signal_strength)
        )
        became_entry_candidate = bool(
            entry_eligible and previous_payload.get("disposition") != "ENTRY_CANDIDATE"
        )
        became_watch = bool(
            not entry_eligible
            and previous_payload.get("disposition") != "ENTRY_REJECTED_KEEP_WATCH"
        )
        became_session_eligible = bool(
            signal.get("session_lane_eligible")
            and previous
            and not previous_payload.get("session_lane_eligible", False)
        )
        if crossed_threshold or became_entry_candidate or (became_session_eligible and entry_eligible):
            self.emit(
                "LEADER_CANDIDATE", bar.symbol, 50, signal,
                f"leader:{bar.symbol}:{bar.start_ms}",
            )
        elif became_watch:
            watch_payload = dict(signal)
            watch_payload["event"] = "MOMENTUM_WATCH"
            watch_payload["warning"] = (
                "Immediate entry rejected. Keep the symbol subscribed for a materially new valid structure; "
                "this event is not trade authority."
            )
            self.emit(
                "MOMENTUM_WATCH", bar.symbol, 45, watch_payload,
                f"watch:{bar.symbol}:{bar.start_ms}:{signal['disposition']}",
            )
        if entry_eligible and signal.get("base_high"):
            base_payload = dict(signal)
            base_payload["event"] = "BASE_READY"
            blockers = list(base_payload.get("session_blockers") or [])
            base_payload["warning"] = (
                "Not ARMED: "
                + ("; ".join(blockers) + "; " if blockers else "")
                + "catalyst context, Level 2, risk and broker review remain unresolved; score is ranking-only."
            )
            priority = 70 if signal.get("session_lane_eligible") else 55
            self.emit(
                "BASE_READY", bar.symbol, priority, base_payload,
                f"base:{bar.symbol}:{int(signal['base_end_ms'])}:{signal['base_high']:.6f}",
            )

    def _handle_second(self, event: dict[str, Any]) -> None:
        symbol = event.get("sym")
        if not symbol:
            return
        self.store.insert_second_bar(event)
        candidate = self.store.get_candidate(symbol)
        if not candidate or not candidate.get("base_high"):
            return
        try:
            candidate_payload = json.loads(candidate.get("payload_json") or "{}")
        except (TypeError, ValueError):
            candidate_payload = {}
        if (
            candidate_payload.get("disposition") != "ENTRY_CANDIDATE"
            or candidate_payload.get("direction") != "UP"
        ):
            return
        if self.crossed_triggers.get(symbol) == candidate.get("base_high"):
            return
        quote = self.store.get_quote(symbol)
        max_spread_pct = (
            self.config.under5_max_quote_spread_pct
            if candidate.get("lane") == "under5"
            else self.config.max_quote_spread_pct
        )
        payload = trigger_cross_payload(
            candidate,
            event,
            quote,
            max_spread_pct=max_spread_pct,
            max_spread_to_risk_ratio=self.config.max_spread_to_structural_risk,
        )
        if not payload:
            return
        if payload["inside_half_atr_chase_ceiling"] and payload["volume_pace_expanding"]:
            if not payload.get("session_lane_eligible"):
                priority = 60
            else:
                priority = 95 if payload["inside_review_limit_ceiling"] else 85
            second_bucket = int(event.get("s", 0) // 1000)
            self.emit(
                "TRIGGER_CROSS", symbol, priority, payload,
                f"cross:{symbol}:{candidate['base_high']:.6f}:{second_bucket}",
            )
            self.crossed_triggers[symbol] = candidate["base_high"]

    def _handle_quote(self, event: dict[str, Any]) -> None:
        if event.get("sym"):
            self.store.upsert_quote(event)

    def _handle_luld(self, event: dict[str, Any]) -> None:
        symbol = event.get("T")
        indicators = event.get("i") or []
        for indicator in indicators:
            if indicator not in (17, 18):
                continue
            self.store.insert_halt(event, int(indicator))
            event_type = "HALT" if indicator == 17 else "RESUMPTION"
            payload = {
                "schema_version": 1,
                "symbol": symbol,
                "event": event_type,
                "timestamp_ms": event.get("t"),
                "upper_band": event.get("h"),
                "lower_band": event.get("l"),
                "required_action": (
                    "Disarm entries and reassess protection; do not assume stop execution while halted."
                    if indicator == 17
                    else "Restart the 15-minute post-halt waiting clock before any under-$5 entry."
                ),
            }
            self.emit(
                event_type, symbol, 100, payload,
                f"luld:{symbol}:{event.get('t')}:{indicator}",
            )

    def _handle_status(self, event: dict[str, Any]) -> None:
        status = event.get("status")
        message = str(event.get("message") or "")
        if status in ("connected", "auth_success", "success"):
            self.store.set_health("massive_websocket", "healthy", {"status": status})
        elif status:
            self.store.set_health("massive_websocket", "degraded", {"status": status, "message": message})
            self.logger.warning("websocket_status status=%s message=%s", status, message)
            if status == "error" and "access real-time data" in message.lower():
                payload = {
                    "schema_version": 1,
                    "message": message,
                    "required_action": (
                        "Confirm Stocks Advanced and e-sign the stock exchange agreements in the "
                        "Massive dashboard. Do not fall back to delayed data for live decisions."
                    ),
                }
                self.emit(
                    "DATA_ENTITLEMENT_MISSING", None, 100, payload,
                    f"entitlement:{datetime.now(self.et).date().isoformat()}",
                )
                self.stop_event.set()

    def _process_message(self, message: Any) -> None:
        events = message if isinstance(message, list) else [message]
        for event in events:
            if not isinstance(event, dict):
                continue
            event_type = event.get("ev")
            if event_type == "AM":
                self._handle_minute(event)
            elif event_type == "A":
                self._handle_second(event)
            elif event_type == "Q":
                self._handle_quote(event)
            elif event_type == "LULD":
                self._handle_luld(event)
            elif event_type == "status":
                self._handle_status(event)

    def _refresh_dynamic_subscriptions(self) -> None:
        if not self.client:
            return
        desired = set(self.store.all_candidate_symbols())
        additions = desired - self.dynamic_symbols
        removals = self.dynamic_symbols - desired
        def chunks(symbols: set[str], size: int = 200):
            ordered = sorted(symbols)
            for start in range(0, len(ordered), size):
                yield ordered[start:start + size]
        for batch in chunks(additions):
            params = ",".join([*(f"Q.{symbol}" for symbol in batch), *(f"A.{symbol}" for symbol in batch)])
            self.client.send_json({"action": "subscribe", "params": params})
        for batch in chunks(removals):
            params = ",".join([*(f"Q.{symbol}" for symbol in batch), *(f"A.{symbol}" for symbol in batch)])
            self.client.send_json({"action": "unsubscribe", "params": params})
        if additions or removals:
            self.dynamic_symbols = desired
            status = "healthy" if len(desired) <= self.config.quote_watch_count else "capacity_warning"
            self.store.set_health(
                "promoted_candidate_coverage",
                status,
                {
                    "promoted_candidates": len(desired),
                    "quote_and_second_subscriptions": len(desired),
                    "coverage_pct": 100.0,
                    "attention_budget": self.config.quote_watch_count,
                    "warning": (
                        "All promoted candidates remain subscribed, but count exceeds the tested attention budget."
                        if status != "healthy" else None
                    ),
                },
            )
            self.logger.info("dynamic_universe size=%d added=%d removed=%d", len(desired), len(additions), len(removals))

    def _connect(self) -> None:
        client = MinimalWebSocket(self.config.websocket_url)
        client.connect()
        connected = client.recv_json(timeout=10)
        self._process_message(connected)
        client.send_json({"action": "auth", "params": self.api_key})
        auth = client.recv_json(timeout=10)
        self._process_message(auth)
        auth_events = auth if isinstance(auth, list) else [auth]
        if not any(isinstance(item, dict) and item.get("status") == "auth_success" for item in auth_events):
            client.close()
            raise WebSocketError("Massive WebSocket authentication did not succeed")
        client.send_json({"action": "subscribe", "params": ",".join(self.BASE_SUBSCRIPTIONS)})
        self.client = client
        self.dynamic_symbols.clear()
        self._refresh_dynamic_subscriptions()
        self.last_message_monotonic = time_module.monotonic()
        self.logger.info("websocket_authenticated subscription_request=%s", ",".join(self.BASE_SUBSCRIPTIONS))

    def _disconnect(self) -> None:
        if self.client:
            self.client.close()
            self.client = None

    def run(self, max_seconds: int | None = None, stop_at_window_end: bool = True) -> None:
        started = time_module.monotonic()
        self.store.clear_candidates()
        self.store.set_metadata("runtime_mode", self.config.mode)
        self.store.set_metadata("runtime_started_at", utc_now())
        self.store.prune(self.config.retention_days)
        try:
            self._bootstrap_market_scope()
        except Exception as exc:
            self.store.set_health("market_scope_coverage", "degraded", {"error": str(exc)[:300]})
            self.logger.warning("market_scope_bootstrap_failed error=%s", str(exc)[:300])
        self.snapshot_refresher.start()
        self.candidate_enricher.start()
        backoff = 1
        last_dynamic_refresh = 0.0
        try:
            while not self.stop_event.is_set():
                if max_seconds and time_module.monotonic() - started >= max_seconds:
                    break
                if stop_at_window_end:
                    now_et = datetime.now(self.et)
                    if now_et.weekday() >= 5 or now_et.timetz().replace(tzinfo=None) > self.config.stop_time:
                        self.logger.info("runtime_window_complete current_time_et=%s", now_et.isoformat())
                        break
                try:
                    if not self.client:
                        self._connect()
                        backoff = 1
                    message = self.client.recv_json(timeout=15)
                    now_mono = time_module.monotonic()
                    if message is None:
                        self.client.ping()
                    else:
                        self.last_message_monotonic = now_mono
                        self._process_message(message)
                    if now_mono - last_dynamic_refresh >= 10:
                        self._refresh_dynamic_subscriptions()
                        last_dynamic_refresh = now_mono
                    stale_for = now_mono - self.last_message_monotonic
                    if stale_for >= self.config.stale_data_seconds and now_mono - self.last_stale_event_at >= self.config.stale_data_seconds:
                        payload = {
                            "schema_version": 1,
                            "stale_seconds": round(stale_for, 1),
                            "required_action": "Suspend entries until market data is fresh and health is verified.",
                        }
                        self.store.set_health("market_data_freshness", "stale", payload)
                        self.emit("DATA_STALE", None, 100, payload, f"stale:{int(time_module.time() // self.config.stale_data_seconds)}")
                        self.last_stale_event_at = now_mono
                    elif stale_for < self.config.stale_data_seconds:
                        self.store.set_health("market_data_freshness", "healthy", {"stale_seconds": round(stale_for, 1)})
                except KeyboardInterrupt:
                    break
                except Exception as exc:
                    self.store.set_health("massive_websocket", "degraded", {"error": str(exc)[:300]})
                    self.emit(
                        "DATA_CONNECTION_LOST", None, 100,
                        {"error": str(exc)[:300], "required_action": "Suspend entries until reconnection and fresh data are verified."},
                        f"disconnect:{int(time_module.time() // 60)}",
                    )
                    self.logger.warning("websocket_error error=%s retry_seconds=%d", str(exc)[:300], backoff)
                    self._disconnect()
                    self.stop_event.wait(backoff)
                    backoff = min(backoff * 2, 30)
        finally:
            self.stop_event.set()
            self._disconnect()
            if self.snapshot_refresher.is_alive():
                self.snapshot_refresher.join(timeout=5)
            if self.candidate_enricher.is_alive():
                self.candidate_enricher.join(timeout=5)
            self.store.set_metadata("runtime_stopped_at", utc_now())
            self.store.close()


def within_runtime_window(config: RuntimeConfig, now: datetime | None = None) -> bool:
    current = now or datetime.now(ZoneInfo(config.timezone))
    if current.weekday() >= 5:
        return False
    local_time = current.timetz().replace(tzinfo=None)
    return config.start_time <= local_time <= config.stop_time
