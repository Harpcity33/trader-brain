from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from statistics import mean
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from .policy import spread_to_risk_gate


@dataclass(frozen=True)
class Bar:
    symbol: str
    start_ms: int
    end_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    window_vwap: float | None = None
    session_vwap: float | None = None
    accumulated_volume: float | None = None
    official_open: float | None = None
    otc: bool = False

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Bar":
        return cls(
            symbol=row["symbol"], start_ms=int(row["start_ms"]), end_ms=int(row["end_ms"]),
            open=float(row["open"]), high=float(row["high"]), low=float(row["low"]),
            close=float(row["close"]), volume=float(row["volume"]),
            window_vwap=row.get("window_vwap"), session_vwap=row.get("session_vwap"),
            accumulated_volume=row.get("accumulated_volume"),
            official_open=row.get("official_open"), otc=bool(row.get("otc")),
        )


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _safe_ratio(numerator: float, denominator: float, default: float = 0.0) -> float:
    return numerator / denominator if denominator else default


def session_lane_context(
    lane: str,
    reference_ms: int,
    timezone_name: str = "America/New_York",
) -> dict[str, Any]:
    """Describe schedule eligibility without granting trade authority.

    The watcher remains shadow-only. This context prevents an observation from
    looking executable when Titan's session or lane rules explicitly prohibit it.
    """
    observed = datetime.fromtimestamp(reference_ms / 1000, tz=timezone.utc).astimezone(
        ZoneInfo(timezone_name)
    )
    local_time = observed.timetz().replace(tzinfo=None)
    blockers: list[str] = []
    next_window: str | None = None

    if observed.weekday() >= 5:
        phase = "closed"
        blockers.append("US equity entries are disabled on weekends")
        next_window = "next US trading weekday"
    elif time(7, 5) <= local_time <= time(9, 20):
        phase = "premarket_pilot"
        if lane == "under5":
            blockers.append("under-$5 equities are prohibited in the premarket pilot")
            next_window = "09:35 ET after a fresh regular-session structure"
    elif time(9, 20) < local_time < time(9, 35):
        phase = "opening_transition"
        blockers.append("new equity entries are disabled from 09:20 through 09:34 ET")
        next_window = "09:35 ET after a fresh regular-session structure"
    elif time(9, 35) <= local_time <= time(15, 30):
        phase = "regular_entry_window"
    else:
        phase = "closed"
        blockers.append("outside Titan's equity entry windows")
        next_window = "next authorized equity entry window"

    return {
        "session_phase": phase,
        "session_lane_eligible": not blockers,
        "session_blockers": blockers,
        "next_eligible_window": next_window,
    }


def short_atr(bars: Sequence[Bar], lookback: int = 5) -> float:
    sample = bars[-lookback:]
    return mean(max(bar.high - bar.low, 0.0) for bar in sample) if sample else 0.0


def detect_controlled_base(bars: Sequence[Bar]) -> dict[str, float] | None:
    """Detect the two completed holding/contraction bars required by Titan.

    This is market-structure evidence only. It does not declare an ARMED trade;
    catalyst, broker, Level 2, risk, and contemporaneous review gates remain external.
    """
    if len(bars) < 5:
        return None
    lead = bars[-5:-2]
    first, second = bars[-2], bars[-1]
    lead_volume = mean(bar.volume for bar in lead)
    if lead_volume <= 0:
        return None

    first_range = max(first.high - first.low, 1e-9)
    first_upper_half = first.close >= first.low + 0.5 * first_range
    prior_support = min(bar.low for bar in lead[-2:])
    support_preserved = first.low >= prior_support
    higher_or_holding = second.low >= first.low * 0.9975
    controlled_volume = first.volume <= lead_volume * 1.10 and second.volume <= lead_volume * 1.10
    no_expanding_sell = not (second.close < first.low and second.volume > first.volume * 1.25)
    compact = (max(first.high, second.high) - min(first.low, second.low)) <= max(
        short_atr(bars) * 1.5, first.close * 0.012
    )
    if not ((first_upper_half or support_preserved) and higher_or_holding and controlled_volume and no_expanding_sell and compact):
        return None

    return {
        "base_high": max(first.high, second.high),
        "support": min(first.low, second.low),
        "invalidation": min(first.low, second.low),
        "pullback_volume_per_second": mean([first.volume, second.volume]) / 60.0,
        "base_start_ms": first.start_ms,
        "base_end_ms": second.end_ms,
    }


def classify_state(
    bars: Sequence[Bar], volume_accel: float, price_accel: float, direction: str = "UP"
) -> str:
    if len(bars) < 3:
        return "BUILDING"
    latest = bars[-1]
    atr = max(short_atr(bars), latest.close * 0.001)
    if direction == "DOWN":
        extension = (max(bar.high for bar in bars[-5:]) - latest.close) / atr
        rejection_wick = _safe_ratio(
            min(latest.open, latest.close) - latest.low, latest.high - latest.low
        )
        failure = latest.close > bars[-2].high
        directional_price_accel = -price_accel
    else:
        extension = (latest.close - min(bar.low for bar in bars[-5:])) / atr
        rejection_wick = _safe_ratio(
            latest.high - max(latest.open, latest.close), latest.high - latest.low
        )
        failure = latest.close < bars[-2].low
        directional_price_accel = price_accel
    if failure and volume_accel > 1.25:
        return "FADING"
    if extension >= 4.0 or (rejection_wick >= 0.40 and volume_accel >= 2.0):
        return "PARABOLIC"
    if directional_price_accel > 0.008 and volume_accel >= 1.4:
        return "BREAKOUT"
    if directional_price_accel > 0.003 and volume_accel >= 1.05:
        return "ACCELERATING"
    return "BUILDING"


def consecutive_expansion_bars(bars: Sequence[Bar], direction: str) -> int:
    """Count the latest directional expansion bars without implying exhaustion alone."""
    count = 0
    for previous, current in reversed(list(zip(bars[:-1], bars[1:]))):
        progressed = current.close > previous.close if direction == "UP" else current.close < previous.close
        range_expanded = (current.high - current.low) >= (previous.high - previous.low) * 0.90
        volume_confirmed = current.volume >= previous.volume * 0.90
        if not (progressed and range_expanded and volume_confirmed):
            break
        count += 1
    return count


def compute_market_signal(
    bars: Sequence[Bar],
    snapshot: dict[str, Any] | None,
    quote: dict[str, Any] | None,
    max_spread_pct: float,
) -> dict[str, Any] | None:
    """Return a market-data signal, deliberately not Titan's Acceleration Score."""
    if len(bars) < 3:
        return None
    latest = bars[-1]
    if latest.otc or latest.close <= 0:
        return None

    snapshot = snapshot or {}
    prev_close = float(snapshot.get("prev_close") or 0)
    gap_pct = ((latest.close / prev_close) - 1) * 100 if prev_close > 0 else None
    direction = "DOWN" if gap_pct is not None and gap_pct < 0 else "UP"
    board = "DOWNSIDE_LONG_PUT" if direction == "DOWN" else "LONG_MOMENTUM"
    accumulated = float(latest.accumulated_volume or snapshot.get("day_volume") or 0)
    dollar_volume = accumulated * latest.close
    prev_volumes = [bar.volume for bar in bars[-4:-1] if bar.volume >= 0]
    volume_accel = _safe_ratio(latest.volume, mean(prev_volumes), 1.0) if prev_volumes else 1.0
    price_accel = _safe_ratio(latest.close - bars[-3].close, bars[-3].close)

    prev_day_volume = float(snapshot.get("prev_day_volume") or 0)
    relative_volume = _safe_ratio(accumulated, prev_day_volume) if prev_day_volume else None
    atr = short_atr(bars)
    session_vwap = latest.session_vwap or latest.window_vwap or latest.close
    controls_vwap = latest.close <= session_vwap if direction == "DOWN" else latest.close >= session_vwap
    directional_price_accel = -price_accel if direction == "DOWN" else price_accel

    spread_pct = quote.get("spread_pct") if quote else None
    quote_fresh = False
    if quote and quote.get("timestamp_ms"):
        quote_age_ms = max(0, int(datetime.now(timezone.utc).timestamp() * 1000) - int(quote["timestamp_ms"]))
        quote_fresh = quote_age_ms <= 15_000
    liquidity_pass = bool(quote_fresh and spread_pct is not None and 0 <= spread_pct <= max_spread_pct)

    move_points = 20 * _clamp((abs(gap_pct or 0) - 2) / 18)
    dollar_points = 20 * _clamp(dollar_volume / 25_000_000)
    volume_points = 15 * _clamp((volume_accel - 0.8) / 2.2)
    price_points = 15 * _clamp((directional_price_accel + 0.002) / 0.025)
    if direction == "DOWN":
        directional_structure = sum(
            1 for left, right in zip(bars[-5:-1], bars[-4:]) if right.high <= left.high
        )
    else:
        directional_structure = sum(
            1 for left, right in zip(bars[-5:-1], bars[-4:]) if right.low >= left.low
        )
    structure_points = 15 * _clamp(
        (directional_structure / 4) * 0.65 + (0.35 if controls_vwap else 0)
    )
    liquidity_points = 10 * (1.0 if liquidity_pass else (0.35 if spread_pct is None else 0.0))
    extension_from_vwap = session_vwap - latest.close if direction == "DOWN" else latest.close - session_vwap
    extension_atr = _safe_ratio(extension_from_vwap, atr) if atr > 0 else 0.0
    capacity_points = 5 * _clamp((4.0 - max(extension_atr, 0)) / 4.0)
    signal_strength = round(
        move_points + dollar_points + volume_points + price_points + structure_points + liquidity_points + capacity_points,
        1,
    )

    expansion_bars = consecutive_expansion_bars(bars, direction)
    exhaustion_lock = bool(expansion_bars >= 3 and extension_atr > 4.0)
    # Downside candidates are surfaced for independent long-put validation. The
    # current watcher does not manufacture an inverse trigger from an upside base.
    base = detect_controlled_base(bars) if direction == "UP" else None
    limit_ceiling = None
    if base and atr > 0:
        spread_abs = 0.0
        if quote and quote.get("bid") and quote.get("ask"):
            spread_abs = max(0.0, float(quote["ask"]) - float(quote["bid"]))
        buffer = min(0.10 * atr, 2 * spread_abs) if spread_abs > 0 else 0.05 * atr
        limit_ceiling = base["base_high"] + buffer

    state = classify_state(bars, volume_accel, price_accel, direction)
    lane = "under5" if latest.close < 5 else "regular_equity"
    observed_at = datetime.fromtimestamp(latest.end_ms / 1000, tz=timezone.utc).isoformat()
    payload: dict[str, Any] = {
        "schema_version": 1,
        "symbol": latest.symbol,
        "observed_at": observed_at,
        "state": state,
        "lane": lane,
        "direction": direction,
        "board": board,
        "signal_strength": signal_strength,
        "signal_disclaimer": "Market-data strength only; not the Titan Acceleration Score and never trade authority.",
        "price": latest.close,
        "gap_pct": round(gap_pct, 3) if gap_pct is not None else None,
        "dollar_volume": round(dollar_volume, 2),
        "volume_acceleration": round(volume_accel, 3),
        "price_acceleration": round(price_accel, 5),
        "relative_volume": round(relative_volume, 3) if relative_volume is not None else None,
        "spread_pct": round(spread_pct, 4) if spread_pct is not None else None,
        "short_atr": round(atr, 6),
        "session_vwap": session_vwap,
        "extension_atr": round(extension_atr, 3),
        "consecutive_expansion_bars": expansion_bars,
        "exhaustion_lock": exhaustion_lock,
        "entry_setup_eligible": not exhaustion_lock,
        "disposition": "ENTRY_REJECTED_KEEP_WATCH" if exhaustion_lock else "ENTRY_CANDIDATE",
        "quote_fresh": quote_fresh,
        "preliminary_liquidity_pass": liquidity_pass,
        "catalyst_verified": False,
        "requires_live_agent_validation": [
            "broker account and orders", "fresh catalyst when required or structure-only qualification",
            "Level 2 depth",
            "dilution/listing/promotion risk", "sector breadth", "90-day analogs",
            "full Acceleration Score", "position sizing and protective-stop feasibility"
        ],
    }
    payload.update(session_lane_context(lane, latest.end_ms))
    if base:
        payload.update(base)
        payload["limit_ceiling"] = round(limit_ceiling, 6) if limit_ceiling is not None else None
    else:
        payload.update({"base_high": None, "support": None, "invalidation": None, "limit_ceiling": None})
    if direction == "DOWN":
        payload["downside_trigger_required"] = True
        payload["downside_authority"] = (
            "Discovery only. Any execution is limited to a separately qualified single-leg long put."
        )
    return payload


def trigger_cross_payload(
    candidate: dict[str, Any],
    second_bar: dict[str, Any],
    quote: dict[str, Any] | None,
    max_spread_pct: float | None = None,
    max_spread_to_risk_ratio: float | None = None,
    now_ms: int | None = None,
) -> dict[str, Any] | None:
    trigger = candidate.get("base_high")
    atr = float(candidate.get("short_atr") or 0)
    ceiling = candidate.get("limit_ceiling")
    if not trigger or atr <= 0 or second_bar.get("h", 0) < trigger:
        return None

    # A completed/partial second bar touching the trigger is not enough. Titan's
    # contract requires a contemporaneous live ask at or through the trigger.
    ask = quote.get("ask") if quote else None
    bid = quote.get("bid") if quote else None
    spread_pct = quote.get("spread_pct") if quote else None
    quote_timestamp_ms = int((quote or {}).get("timestamp_ms") or 0)
    reference_ms = now_ms or int(datetime.now(timezone.utc).timestamp() * 1000)
    quote_fresh = bool(quote_timestamp_ms and 0 <= reference_ms - quote_timestamp_ms <= 15_000)
    spread_to_risk_pass = True
    spread_to_risk_ratio = None
    if max_spread_to_risk_ratio is not None:
        invalidation = candidate.get("invalidation")
        if bid is None or ask is None or invalidation is None:
            spread_to_risk_pass = False
        else:
            spread_to_risk_pass, spread_to_risk_ratio = spread_to_risk_gate(
                trigger=float(trigger),
                invalidation=float(invalidation),
                bid=float(bid),
                ask=float(ask),
                max_ratio=max_spread_to_risk_ratio,
            )
    liquidity_pass = bool(
        quote_fresh
        and spread_pct is not None
        and (max_spread_pct is None or 0 <= float(spread_pct) <= max_spread_pct)
        and spread_to_risk_pass
    )
    if ask is None or float(ask) < float(trigger) or not liquidity_pass:
        return None

    live_price = float(ask)
    extension_atr = (live_price - trigger) / atr
    pace = float(second_bar.get("dv") or second_bar.get("v") or 0)
    pullback_pace = 0.0
    details: dict[str, Any] = {}
    try:
        details = candidate.get("payload_json")
        if isinstance(details, str):
            import json
            details = json.loads(details)
        pullback_pace = float((details or {}).get("pullback_volume_per_second") or 0)
    except (ValueError, TypeError):
        pass

    return {
        "schema_version": 1,
        "symbol": candidate["symbol"],
        "event": "TRIGGER_CROSS",
        "trigger": trigger,
        "live_price": live_price,
        "ask": ask,
        "reviewed_limit_ceiling": ceiling,
        "short_atr": atr,
        "extension_atr": round(extension_atr, 3),
        "inside_half_atr_chase_ceiling": extension_atr <= 0.5,
        "inside_review_limit_ceiling": ceiling is not None and live_price <= ceiling,
        "second_volume": pace,
        "pullback_volume_per_second": pullback_pace,
        "volume_pace_expanding": pace > pullback_pace,
        "spread_pct": spread_pct,
        "spread_to_structural_risk": (
            round(spread_to_risk_ratio, 4) if spread_to_risk_ratio is not None else None
        ),
        "max_spread_to_structural_risk": max_spread_to_risk_ratio,
        "spread_to_risk_pass": spread_to_risk_pass,
        "quote_fresh": quote_fresh,
        "preliminary_liquidity_pass": liquidity_pass,
        "price_cross_confirmed": True,
        "session_phase": details.get("session_phase"),
        "session_lane_eligible": bool(details.get("session_lane_eligible", False)),
        "session_blockers": details.get("session_blockers") or [],
        "next_eligible_window": details.get("next_eligible_window"),
        "signal_disclaimer": "Observation only. Robinhood review and every Titan gate remain mandatory.",
    }
