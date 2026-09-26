"""Deterministic Massive-driven paper options engine.

Paper only. No broker mutation code is present in this module.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
import json
import math
import os
from pathlib import Path
import statistics
from typing import Any, Iterable, Mapping, Sequence
from urllib import parse as urlparse
from urllib import request as urlrequest
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
    timestamp: str


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


class MassiveClient:
    def __init__(self, api_key: str, base_url: str = "https://api.massive.com") -> None:
        if not api_key:
            raise ValueError("Massive API key is required")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    def _get(self, path: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        query = dict(params or {})
        query["apiKey"] = self.api_key
        url = f"{self.base_url}{path}?{urlparse.urlencode(query)}"
        req = urlrequest.Request(url, headers={"User-Agent": "TraderBrain/1.0"})
        with urlrequest.urlopen(req, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))

    def bars(self, ticker: str, minutes: int, trading_date: date) -> list[dict[str, Any]]:
        payload = self._get(
            f"/v2/aggs/ticker/{ticker}/range/{minutes}/minute/{trading_date.isoformat()}/{trading_date.isoformat()}",
            {"adjusted": "true", "sort": "asc", "limit": 50000},
        )
        return list(payload.get("results") or [])

    def option_chain(self, underlying: str, start_date: date, end_date: date) -> list[dict[str, Any]]:
        payload = self._get(
            f"/v3/snapshot/options/{underlying}",
            {
                "expiration_date.gte": start_date.isoformat(),
                "expiration_date.lte": end_date.isoformat(),
                "limit": 250,
                "sort": "expiration_date",
                "order": "asc",
            },
        )
        results = list(payload.get("results") or [])
        next_url = payload.get("next_url")
        pages = 0
        while next_url and pages < 2:
            # next_url from Massive is absolute and may already contain params.
            separator = "&" if "?" in next_url else "?"
            req = urlrequest.Request(
                f"{next_url}{separator}apiKey={urlparse.quote(self.api_key)}",
                headers={"User-Agent": "TraderBrain/1.0"},
            )
            with urlrequest.urlopen(req, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
            results.extend(payload.get("results") or [])
            next_url = payload.get("next_url")
            pages += 1
        return results

    def option_snapshot(self, underlying: str, contract: str) -> dict[str, Any]:
        return self._get(f"/v3/snapshot/options/{underlying}/{contract}")


def _median(values: Sequence[float]) -> float:
    return statistics.median(values) if values else 0.0


def _completed_bars(bars: Sequence[Mapping[str, Any]], now: datetime, minutes: int) -> list[dict[str, Any]]:
    cutoff_ms = int(now.timestamp() * 1000)
    duration_ms = minutes * 60 * 1000
    completed = []
    for bar in bars:
        start = int(bar.get("t", 0))
        if start + duration_ms <= cutoff_ms:
            completed.append(dict(bar))
    return completed


def signal_from_bars(
    ticker: str,
    bars: Sequence[Mapping[str, Any]],
    *,
    lane: str,
    minutes: int,
    now: datetime,
    minimum_relative_volume: float,
    minimum_bars: int,
    breakout_lookback_bars: int,
    minimum_setup_score: float,
) -> Signal | None:
    completed = _completed_bars(bars, now, minutes)
    if len(completed) < minimum_bars:
        return None
    latest = completed[-1]
    prior = completed[-(breakout_lookback_bars + 1):-1]
    if len(prior) < breakout_lookback_bars:
        return None
    latest_volume = float(latest.get("v", 0) or 0)
    comparison_volumes = [float(b.get("v", 0) or 0) for b in completed[-6:-1] if float(b.get("v", 0) or 0) > 0]
    baseline_volume = _median(comparison_volumes)
    if baseline_volume <= 0:
        return None
    rvol = latest_volume / baseline_volume
    if rvol < minimum_relative_volume:
        return None

    close = float(latest.get("c", 0) or 0)
    open_ = float(latest.get("o", 0) or 0)
    high = float(latest.get("h", 0) or 0)
    low = float(latest.get("l", 0) or 0)
    vwap = float(latest.get("vw", close) or close)
    if min(close, open_, high, low) <= 0:
        return None

    prior_high = max(float(b.get("h", 0) or 0) for b in prior)
    prior_low = min(float(b.get("l", math.inf) or math.inf) for b in prior)
    recent_closes = [float(b.get("c", 0) or 0) for b in completed[-4:-1]]
    trend_avg = sum(recent_closes) / len(recent_closes) if recent_closes else close

    direction = ""
    setup_id = ""
    invalidation = 0.0
    structure_points = 0.0
    if close > prior_high and close > open_ and close >= vwap and close >= trend_avg:
        direction = "call"
        setup_id = "ORB_BREAKOUT" if minutes == 5 else "VWAP_CONTINUATION"
        invalidation = min(low, prior_high)
        structure_points = 88.0
    elif close < prior_low and close < open_ and close <= vwap and close <= trend_avg:
        direction = "put"
        setup_id = "FAILED_BREAKOUT_REVERSAL" if minutes == 5 else "VWAP_CONTINUATION"
        invalidation = max(high, prior_low)
        structure_points = 88.0
    else:
        return None

    volume_points = min(100.0, 55.0 + 25.0 * max(0.0, rvol - 1.0))
    body_pct = abs(close - open_) / max(close, 0.01)
    impulse_points = min(100.0, 60.0 + body_pct * 5000)
    setup_score = round(0.45 * structure_points + 0.35 * volume_points + 0.20 * impulse_points, 2)
    if setup_score < minimum_setup_score:
        return None
    return Signal(
        ticker=ticker,
        lane=lane,
        direction=direction,
        setup_id=setup_id,
        setup_score=setup_score,
        relative_volume=round(rvol, 3),
        trigger_price=close,
        invalidation_price=round(invalidation, 4),
        timestamp=datetime.fromtimestamp(int(latest["t"]) / 1000, tz=NY).isoformat(),
    )


def _number(mapping: Mapping[str, Any] | None, key: str, default: float | None = None) -> float | None:
    if not isinstance(mapping, Mapping):
        return default
    value = mapping.get(key)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def contract_from_snapshot(item: Mapping[str, Any], underlying: str, today: date) -> Contract | None:
    details = item.get("details") or {}
    quote = item.get("last_quote") or {}
    greeks = item.get("greeks") or {}
    day = item.get("day") or {}
    ticker = details.get("ticker")
    expiration = details.get("expiration_date")
    contract_type = details.get("contract_type")
    strike = _number(details, "strike_price")
    bid = _number(quote, "bid")
    ask = _number(quote, "ask")
    delta = _number(greeks, "delta")
    if not ticker or not expiration or contract_type not in {"call", "put"}:
        return None
    if None in (strike, bid, ask, delta) or bid <= 0 or ask <= 0 or ask < bid:
        return None
    exp = date.fromisoformat(str(expiration))
    dte = (exp - today).days
    midpoint = (bid + ask) / 2
    spread_pct = (ask - bid) / midpoint if midpoint > 0 else math.inf
    return Contract(
        ticker=str(ticker),
        underlying=underlying,
        contract_type=str(contract_type),
        expiration_date=str(expiration),
        strike=float(strike),
        dte=dte,
        bid=float(bid),
        ask=float(ask),
        midpoint=round(midpoint, 4),
        spread_pct=spread_pct,
        delta=float(delta),
        gamma=_number(greeks, "gamma"),
        theta=_number(greeks, "theta"),
        vega=_number(greeks, "vega"),
        iv=_number(item, "implied_volatility"),
        open_interest=int(_number(item, "open_interest", 0) or 0),
        volume=int(_number(day, "volume", 0) or 0),
        quote_timestamp_ns=int(_number(quote, "last_updated", 0) or 0) or None,
    )


def select_contract(
    chain: Iterable[Mapping[str, Any]],
    *,
    underlying: str,
    signal: Signal,
    today: date,
    min_dte: int,
    max_dte: int,
    min_abs_delta: float,
    max_abs_delta: float,
    max_spread_pct: float,
    min_open_interest: int,
    min_volume: int,
    max_premium_dollars: float,
) -> Contract | None:
    candidates: list[Contract] = []
    for raw in chain:
        contract = contract_from_snapshot(raw, underlying, today)
        if contract is None or contract.contract_type != signal.direction:
            continue
        if not min_dte <= contract.dte <= max_dte:
            continue
        if not min_abs_delta <= abs(contract.delta) <= max_abs_delta:
            continue
        if contract.spread_pct > max_spread_pct:
            continue
        if contract.open_interest < min_open_interest or contract.volume < min_volume:
            continue
        if contract.ask * 100 > max_premium_dollars:
            continue
        candidates.append(contract)
    if not candidates:
        return None
    # Prefer delta near .55, tight spread, then activity.
    return min(
        candidates,
        key=lambda c: (
            abs(abs(c.delta) - 0.55),
            c.spread_pct,
            -(c.open_interest + c.volume),
            c.ask,
        ),
    )


def entry_fill(contract: Contract, slippage_fraction: float) -> float:
    spread = max(0.0, contract.ask - contract.bid)
    return round(contract.ask + spread * slippage_fraction, 4)


def exit_fill(bid: float, ask: float, slippage_fraction: float) -> float:
    spread = max(0.0, ask - bid)
    return round(max(0.01, bid - spread * slippage_fraction), 4)


def default_state(starting_equity: float, now: datetime) -> dict[str, Any]:
    monday = (now - timedelta(days=now.weekday())).date().isoformat()
    return {
        "version": 1,
        "starting_equity": starting_equity,
        "cash": starting_equity,
        "week_start": monday,
        "week_start_equity": starting_equity,
        "weekly_lock": False,
        "positions": [],
        "closed_trades": [],
        "notified_signals": [],
        "daily": {},
    }


def load_state(path: Path, starting_equity: float, now: datetime) -> dict[str, Any]:
    if not path.is_file():
        return default_state(starting_equity, now)
    state = json.loads(path.read_text(encoding="utf-8"))
    monday = (now - timedelta(days=now.weekday())).date().isoformat()
    if state.get("week_start") != monday and not state.get("positions"):
        state["week_start"] = monday
        state["week_start_equity"] = float(state.get("cash", starting_equity))
        state["weekly_lock"] = False
    return state


def save_state(path: Path, state: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def realized_pnl_this_week(state: Mapping[str, Any]) -> float:
    week_start = str(state["week_start"])
    return round(sum(float(t.get("pnl", 0)) for t in state.get("closed_trades", []) if str(t.get("closed_at", ""))[:10] >= week_start), 2)


def mark_equity(state: Mapping[str, Any], marks: Mapping[str, float] | None = None) -> float:
    marks = marks or {}
    value = float(state.get("cash", 0))
    for pos in state.get("positions", []):
        mark = float(marks.get(pos["contract"], pos.get("last_mark", pos["entry_fill"])))
        value += mark * 100 * int(pos["quantity"])
    return round(value, 2)


def weekly_risk_remaining(state: Mapping[str, Any], weekly_drawdown_pct: float, current_equity: float) -> float:
    floor = float(state["week_start_equity"]) * (1.0 - weekly_drawdown_pct / 100.0)
    return round(max(0.0, current_equity - floor), 2)


def update_weekly_lock(state: dict[str, Any], weekly_drawdown_pct: float, current_equity: float) -> bool:
    remaining = weekly_risk_remaining(state, weekly_drawdown_pct, current_equity)
    if remaining <= 0:
        state["weekly_lock"] = True
    return bool(state.get("weekly_lock"))


def signal_key(signal: Signal) -> str:
    return f"{signal.ticker}|{signal.lane}|{signal.direction}|{signal.timestamp}"


def open_paper_position(
    state: dict[str, Any],
    signal: Signal,
    contract: Contract,
    *,
    fill: float,
    stop_pct: float,
    target_pct: float,
    risk_pct: float,
    now: datetime,
) -> dict[str, Any]:
    cost = round(fill * 100, 2)
    if cost > float(state["cash"]):
        raise ValueError("insufficient paper cash")
    position = {
        "trade_id": f"{signal.ticker}-{now.strftime('%Y%m%d%H%M%S')}",
        "ticker": signal.ticker,
        "lane": signal.lane,
        "direction": signal.direction,
        "setup_id": signal.setup_id,
        "setup_score": signal.setup_score,
        "contract": contract.ticker,
        "expiration": contract.expiration_date,
        "strike": contract.strike,
        "delta": contract.delta,
        "iv": contract.iv,
        "quantity": 1,
        "entry_fill": fill,
        "last_mark": fill,
        "premium_cost": cost,
        "stop_price": round(fill * (1.0 - stop_pct), 4),
        "target_price": round(fill * (1.0 + target_pct), 4),
        "planned_risk_dollars": round(cost * stop_pct, 2),
        "planned_risk_pct": risk_pct,
        "opened_at": now.isoformat(),
        "signal": asdict(signal),
    }
    state["cash"] = round(float(state["cash"]) - cost, 2)
    state["positions"].append(position)
    state["notified_signals"].append(signal_key(signal))
    return position


def close_paper_position(
    state: dict[str, Any],
    trade_id: str,
    *,
    fill: float,
    reason: str,
    now: datetime,
) -> dict[str, Any]:
    for index, pos in enumerate(state.get("positions", [])):
        if pos["trade_id"] != trade_id:
            continue
        proceeds = round(fill * 100 * int(pos["quantity"]), 2)
        pnl = round(proceeds - float(pos["premium_cost"]), 2)
        closed = dict(pos)
        closed.update({"exit_fill": fill, "closed_at": now.isoformat(), "exit_reason": reason, "pnl": pnl})
        state["cash"] = round(float(state["cash"]) + proceeds, 2)
        state["closed_trades"].append(closed)
        del state["positions"][index]
        return closed
    raise KeyError(trade_id)
