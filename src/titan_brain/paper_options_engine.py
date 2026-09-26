"""Deterministic PAPER-ONLY primitives; no brokerage client or AI API calls.

Prices and fills are a simulation, not an execution guarantee. Budget checks
reserve the entire open option value, not merely the planned stop distance.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, asdict
from datetime import date, datetime, time, timedelta
import fcntl
import json
import math
import os
from pathlib import Path
import statistics
import tempfile
from typing import Any, Iterable, Mapping, Sequence
from urllib import error, parse, request
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class Signal:
    ticker: str
    lane: str
    direction: str
    setup_id: str
    setup_score: float
    relative_volume: float
    trigger_price: float
    invalidation_price: float
    timestamp: str  # completed candle END, not start


@dataclass(frozen=True)
class Contract:
    ticker: str
    underlying: str
    contract_type: str
    expiration_date: str
    strike: float
    dte: int
    bid: float
    ask: float
    midpoint: float
    spread_pct: float
    delta: float
    gamma: float | None
    theta: float | None
    vega: float | None
    iv: float | None
    open_interest: int
    volume: int
    quote_timestamp_ns: int | None
    bid_size: int = 0
    ask_size: int = 0
    multiplier: int = 100
    timeframe: str = "UNKNOWN"


class DataUnavailable(RuntimeError):
    """Safe error code; never includes credentials, URLs, or response bodies."""


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DataUnavailable("MASSIVE_REDIRECT_REJECTED")


class MassiveClient:
    def __init__(self, api_key: str, base_url: str = "https://api.massive.com") -> None:
        if not api_key:
            raise ValueError("Massive API key is required")
        if base_url.rstrip("/") != "https://api.massive.com":
            raise ValueError("unapproved market-data origin")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.opener = request.build_opener(_NoRedirect())

    def _get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        if not path.startswith("/") or path.startswith("//"):
            raise DataUnavailable("INVALID_API_PATH")
        query = dict(params or {})
        query.pop("apiKey", None)
        req = request.Request(self.base_url + path + "?" + parse.urlencode(query),
                              headers={"Authorization": "Bearer " + self.api_key,
                                       "User-Agent": "TraderBrain-Paper/2"})
        try:
            with self.opener.open(req, timeout=8) as response:
                data = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            raise DataUnavailable(f"MASSIVE_HTTP_{exc.code}") from None
        except DataUnavailable:
            raise
        except Exception:
            raise DataUnavailable("MASSIVE_TRANSPORT_OR_JSON_ERROR") from None
        if isinstance(data, dict):
            status = str(data.get("status", "OK")).upper()
            if status != "OK" or data.get("error"):
                raise DataUnavailable("MASSIVE_RESPONSE_NOT_OK")
        elif not isinstance(data, list):
            raise DataUnavailable("MASSIVE_INVALID_RESPONSE")
        return data

    def bars(self, ticker: str, minutes: int, trading_date: date) -> list[dict[str, Any]]:
        symbol = parse.quote(ticker, safe="")
        day = trading_date.isoformat()
        data = self._get(f"/v2/aggs/ticker/{symbol}/range/{minutes}/minute/{day}/{day}",
                         {"adjusted": "true", "sort": "asc", "limit": 50000})
        return list(data.get("results") or [])

    def top_movers(self, direction: str) -> list[str]:
        if direction not in {"gainers", "losers"}:
            raise ValueError("invalid movers direction")
        data = self._get(f"/v2/snapshot/locale/us/markets/stocks/{direction}")
        return [x["ticker"] for x in data.get("tickers", [])
                if isinstance(x.get("ticker"), str)]

    def previous_bar(self, ticker: str) -> dict[str, Any]:
        data = self._get(f"/v2/aggs/ticker/{parse.quote(ticker, safe='')}/prev", {"adjusted": "true"})
        values = data.get("results") or []
        if not values:
            raise DataUnavailable("NO_PREVIOUS_BAR")
        return values[0]

    def market_status(self) -> dict[str, Any]:
        return self._get("/v1/marketstatus/now")

    def market_holidays(self) -> list[dict[str, Any]]:
        data = self._get("/v1/marketstatus/upcoming")
        if not isinstance(data, list):
            raise DataUnavailable("CALENDAR_UNAVAILABLE")
        return data

    def option_chain(self, underlying: str, start_date: date, end_date: date) -> list[dict[str, Any]]:
        path = f"/v3/snapshot/options/{parse.quote(underlying, safe='')}"
        data = self._get(path, {"expiration_date.gte": start_date.isoformat(),
                               "expiration_date.lte": end_date.isoformat(),
                               "limit": 250, "sort": "ticker", "order": "asc"})
        results = list(data.get("results") or [])
        seen = set()
        for _ in range(19):
            url = data.get("next_url")
            if not url:
                return results
            parts = parse.urlsplit(url)
            if (parts.scheme != "https" or parts.netloc != "api.massive.com"
                    or parts.path != path or url in seen):
                raise DataUnavailable("UNTRUSTED_OR_REPEATED_PAGINATION")
            seen.add(url)
            data = self._get(parts.path, dict(parse.parse_qsl(parts.query)))
            results.extend(data.get("results") or [])
        if data.get("next_url"):
            raise DataUnavailable("OPTION_CHAIN_INCOMPLETE")
        return results

    def option_snapshot(self, underlying: str, contract: str) -> dict[str, Any]:
        return self._get(f"/v3/snapshot/options/{parse.quote(underlying, safe='')}/{parse.quote(contract, safe=':')}")


def number(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("boolean is not a price")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite number")
    return result


def _completed_bars(bars: Sequence[Mapping[str, Any]], now: datetime, minutes: int) -> list[dict[str, Any]]:
    if now.tzinfo is None or minutes not in (5, 15):
        raise ValueError("aware clock and supported candle interval required")
    local = now.astimezone(NY)
    opening = datetime.combine(local.date(), time(9, 30), NY)
    closing = datetime.combine(local.date(), time(16), NY)
    duration = minutes * 60_000
    unique = {}
    for raw in bars:
        try:
            start = int(raw["t"])
            end = start + duration
            if not (opening.timestamp() * 1000 <= start < closing.timestamp() * 1000):
                continue
            if (start - int(opening.timestamp() * 1000)) % duration:
                continue
            if end > min(now.timestamp(), closing.timestamp()) * 1000:
                continue
            bar = {k: number(raw[k]) for k in ("o", "h", "l", "c", "v", "vw")}
            if (min(bar[k] for k in ("o", "h", "l", "c", "vw")) <= 0
                    or bar["v"] <= 0 or bar["l"] > min(bar["o"], bar["c"])
                    or bar["h"] < max(bar["o"], bar["c"])):
                continue
            bar["t"] = start
            unique[start] = bar
        except (ValueError, TypeError, KeyError, OverflowError):
            continue
    return [unique[k] for k in sorted(unique)]


def signal_from_bars(ticker: str, bars: Sequence[Mapping[str, Any]], *, lane: str,
                     minutes: int, now: datetime, minimum_relative_volume: float,
                     minimum_bars: int, breakout_lookback_bars: int,
                     minimum_setup_score: float) -> Signal | None:
    completed = _completed_bars(bars, now, minutes)
    if len(completed) < max(minimum_bars, breakout_lookback_bars + 1):
        return None
    latest = completed[-1]
    end = datetime.fromtimestamp((latest["t"] + minutes * 60000) / 1000, NY)
    if not 0 <= (now - end).total_seconds() <= 180:
        return None
    recent = completed[-max(minimum_bars, breakout_lookback_bars + 1):]
    if any(b["t"] - a["t"] != minutes * 60000 for a, b in zip(recent, recent[1:])):
        return None
    prior = completed[-(breakout_lookback_bars + 1):-1]
    baseline = statistics.median(b["v"] for b in completed[-6:-1])
    rvol = latest["v"] / baseline
    if rvol < minimum_relative_volume:
        return None
    close, opening, high, low = (latest[k] for k in ("c", "o", "h", "l"))
    vwap = sum(b["vw"] * b["v"] for b in completed) / sum(b["v"] for b in completed)
    trend = statistics.mean(b["c"] for b in completed[-4:-1])
    prior_high, prior_low = max(b["h"] for b in prior), min(b["l"] for b in prior)
    if close > prior_high and close > opening and close >= max(vwap, trend):
        direction, invalidation = "call", min(low, prior_high)
    elif close < prior_low and close < opening and close <= min(vwap, trend):
        direction, invalidation = "put", max(high, prior_low)
    else:
        return None
    score = round(.45 * 88 + .35 * min(100, 55 + 25 * max(0, rvol - 1))
                  + .20 * min(100, 60 + abs(close - opening) / close * 5000), 2)
    if score < minimum_setup_score:
        return None
    return Signal(ticker, lane, direction, "VWAP_CONTINUATION", score, round(rvol, 3),
                  close, round(invalidation, 4), end.isoformat())


def contract_from_snapshot(item: Mapping[str, Any], underlying: str, today: date) -> Contract | None:
    try:
        details, quote, greeks = item["details"], item["last_quote"], item["greeks"]
        bid, ask = number(quote["bid"]), number(quote["ask"])
        delta, gamma, theta, vega = (number(greeks[k]) for k in ("delta", "gamma", "theta", "vega"))
        iv, strike = number(item["implied_volatility"]), number(details["strike_price"])
        kind = details["contract_type"]
        expiration = date.fromisoformat(details["expiration_date"])
        multiplier = number(details["shares_per_contract"])
        bid_size, ask_size = int(quote["bid_size"]), int(quote["ask_size"])
        oi, volume = int(item["open_interest"]), int(item["day"]["volume"])
        ts = int(quote["last_updated"])
        if (multiplier != 100 or not 0 < bid <= ask or min(strike, iv) <= 0
                or gamma < 0 or vega < 0 or min(bid_size, ask_size) < 1
                or min(oi, volume) < 0 or kind not in {"call", "put"}
                or not 0 < abs(delta) <= 1 or (kind == "call") != (delta > 0)
                or not str(details["ticker"]).startswith("O:")):
            return None
        mid = (bid + ask) / 2
        return Contract(str(details["ticker"]), underlying, kind, expiration.isoformat(),
                        strike, (expiration - today).days, bid, ask, mid, (ask - bid) / mid,
                        delta, gamma, theta, vega, iv, oi, volume, ts, bid_size, ask_size,
                        100, str(quote.get("timeframe", "UNKNOWN")).upper())
    except (KeyError, ValueError, TypeError, OverflowError):
        return None


def quote_is_fresh(contract: Contract, now: datetime, stale_seconds: int = 30) -> bool:
    return bool(contract.quote_timestamp_ns and contract.timeframe == "REAL-TIME"
                and 0 <= now.timestamp() - contract.quote_timestamp_ns / 1e9 <= stale_seconds)


def select_contract(chain: Iterable[Mapping[str, Any]], *, underlying: str, signal: Signal,
                    today: date, min_dte: int, max_dte: int, min_abs_delta: float,
                    max_abs_delta: float, max_spread_pct: float, min_open_interest: int,
                    min_volume: int, max_premium_dollars: float,
                    now: datetime | None = None, stale_seconds: int = 30) -> Contract | None:
    candidates = []
    for item in chain:
        c = contract_from_snapshot(item, underlying, today)
        if c is None or c.contract_type != signal.direction:
            continue
        if (not min_dte <= c.dte <= max_dte or not min_abs_delta <= abs(c.delta) <= max_abs_delta
                or c.spread_pct > max_spread_pct or c.open_interest < min_open_interest
                or c.volume < min_volume or c.ask * 100 > max_premium_dollars
                or (now is not None and not quote_is_fresh(c, now, stale_seconds))):
            continue
        candidates.append(c)
    return min(candidates, key=lambda c: (abs(abs(c.delta) - .55), c.spread_pct,
                                          -(c.open_interest + c.volume), c.ask)) if candidates else None


def entry_fill(contract: Contract, slippage_fraction: float) -> float:
    fraction = number(slippage_fraction)
    if fraction < 0:
        raise ValueError("negative slippage")
    return round(contract.ask + (contract.ask - contract.bid) * fraction, 4)


def exit_fill(bid: float, ask: float, slippage_fraction: float) -> float:
    bid, ask, fraction = number(bid), number(ask), number(slippage_fraction)
    if not 0 <= bid <= ask or fraction < 0:
        raise ValueError("invalid exit quote")
    return round(max(0, bid - (ask - bid) * fraction), 4)


def default_state(starting_equity: float, now: datetime) -> dict[str, Any]:
    if number(starting_equity) <= 0 or now.tzinfo is None:
        raise ValueError("positive equity and aware timestamp required")
    local = now.astimezone(NY)
    return {"version": 2, "starting_equity": starting_equity, "cash": starting_equity,
            "week_start": (local - timedelta(days=local.weekday())).date().isoformat(),
            "week_start_equity": starting_equity, "weekly_lock": False,
            "positions": [], "closed_trades": [], "notified_signals": [], "daily": {}, "outbox": []}


def load_state(path: Path, starting_equity: float, now: datetime) -> dict[str, Any]:
    if not path.exists():
        return default_state(starting_equity, now)
    state = json.loads(path.read_text(encoding="utf-8"))
    if number(state["cash"]) < 0 or number(state["week_start_equity"]) <= 0:
        raise ValueError("invalid paper ledger")
    local = now.astimezone(NY)
    monday = (local - timedelta(days=local.weekday())).date().isoformat()
    if state["week_start"] != monday:
        if state["positions"]:
            state["weekly_lock"] = True  # carryover must be reconciled, never discarded
        else:
            state.update(week_start=monday, week_start_equity=state["cash"], weekly_lock=False)
    state.setdefault("outbox", [])
    for item in state["outbox"]:
        if item["status"] == "sending":
            item["status"] = "delivery_unknown"  # never blindly duplicate an ambiguous SMTP send
    return state


@contextmanager
def state_lock(path: Path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def save_state(path: Path, state: Mapping[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".paper-state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def realized_pnl_this_week(state: Mapping[str, Any]) -> float:
    return round(sum(number(t["pnl"]) for t in state["closed_trades"]
                     if t["closed_at"][:10] >= state["week_start"]), 2)


def mark_equity(state: Mapping[str, Any], marks: Mapping[str, float] | None = None) -> float:
    marks = marks or {}
    return round(number(state["cash"]) + sum(number(marks.get(p["contract"], p.get("last_mark", p["entry_fill"])))
                 * 100 * int(p["quantity"]) for p in state["positions"]), 2)


def weekly_risk_remaining(state: Mapping[str, Any], weekly_drawdown_pct: float, current_equity: float) -> float:
    pct, opening, equity = number(weekly_drawdown_pct), number(state["week_start_equity"]), number(current_equity)
    if not 0 < pct <= 100:
        raise ValueError("invalid weekly cap")
    budget = opening * pct / 100
    return round(max(0, min(budget, equity - (opening - budget))), 2)


def new_entry_capacity(state: Mapping[str, Any], weekly_pct: float, fee: float = 0) -> float:
    remaining = weekly_risk_remaining(state, weekly_pct, mark_equity(state))
    open_value = sum(number(p.get("last_mark", p["entry_fill"])) * 100 * int(p["quantity"])
                     + fee * int(p["quantity"]) for p in state["positions"])
    return round(max(0, min(number(state["cash"]), remaining - open_value)), 2)


def update_weekly_lock(state: dict[str, Any], weekly_drawdown_pct: float, current_equity: float) -> bool:
    floor = number(state["week_start_equity"]) * (1 - number(weekly_drawdown_pct) / 100)
    if number(current_equity) <= floor:
        state["weekly_lock"] = True
    return bool(state["weekly_lock"])


def signal_key(signal: Signal) -> str:
    return f"{signal.ticker}|{signal.lane}|{signal.direction}|{signal.timestamp}"


def open_paper_position(state: dict[str, Any], signal: Signal, contract: Contract, *, fill: float,
                        stop_pct: float, target_pct: float, risk_pct: float, now: datetime,
                        fee: float = 0) -> dict[str, Any]:
    fill, fee = number(fill), number(fee)
    if fill <= 0 or fee < 0 or not 0 < stop_pct < 1 or target_pct <= 0:
        raise ValueError("invalid paper entry")
    if signal_key(signal) in state["notified_signals"]:
        raise ValueError("duplicate paper entry")
    premium, cost = round(fill * 100, 2), round(fill * 100 + fee, 2)
    if cost > number(state["cash"]):
        raise ValueError("insufficient paper cash")
    position = {"trade_id": signal_key(signal), "ticker": signal.ticker, "lane": signal.lane,
                "direction": signal.direction, "setup_id": signal.setup_id, "setup_score": signal.setup_score,
                "contract": contract.ticker, "expiration": contract.expiration_date, "strike": contract.strike,
                "delta": contract.delta, "iv": contract.iv, "quantity": 1, "entry_fill": fill,
                "last_mark": getattr(contract, "bid", fill), "premium_cost": premium, "entry_fee": fee,
                "stop_price": round(fill * (1 - stop_pct), 4), "target_price": round(fill * (1 + target_pct), 4),
                "planned_risk_dollars": round(premium * stop_pct + 2 * fee, 2), "planned_risk_pct": risk_pct,
                "opened_at": now.isoformat(), "signal": asdict(signal), "sampled_mae": 0, "sampled_mfe": 0}
    state["cash"] = round(number(state["cash"]) - cost, 2)
    state["positions"].append(position)
    state["notified_signals"].append(signal_key(signal))
    return position


def close_paper_position(state: dict[str, Any], trade_id: str, *, fill: float, reason: str,
                         now: datetime, fee: float = 0) -> dict[str, Any]:
    fill, fee = number(fill), number(fee)
    if min(fill, fee) < 0:
        raise ValueError("invalid paper exit")
    for index, pos in enumerate(state["positions"]):
        if pos["trade_id"] == trade_id:
            proceeds = round(fill * 100 * pos["quantity"] - fee, 2)
            gross = round(fill * 100 * pos["quantity"] - pos["premium_cost"], 2)
            closed = dict(pos, exit_fill=fill, closed_at=now.isoformat(), exit_reason=reason,
                          gross_pnl=gross, exit_fee=fee,
                          pnl=round(gross - pos.get("entry_fee", 0) - fee, 2))
            state["cash"] = round(number(state["cash"]) + proceeds, 2)
            state["closed_trades"].append(closed)
            del state["positions"][index]
            return closed
    raise KeyError(trade_id)
