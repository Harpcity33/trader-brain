from __future__ import annotations

from math import floor
from typing import Any


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _scale(value: float | None, full_value: float) -> float | None:
    if value is None:
        return None
    return _clamp(float(value) / full_value * 100.0)


def apply_weighted_opportunity_scale(signal: dict[str, Any]) -> dict[str, Any]:
    """Add Trader Brain's weighted opportunity model without granting authority.

    Unknown catalyst, sector, historical, and float/dilution inputs remain unknown
    and contribute zero to the raw score.  They are never silently imputed.
    """
    dollar_volume = float(signal.get("dollar_volume") or 0)
    spread_pct = signal.get("spread_pct")
    relative_volume = signal.get("relative_volume")
    extension_atr = max(float(signal.get("extension_atr") or 0), 0.0)
    state = str(signal.get("state") or "BUILDING")

    liquidity = _clamp(dollar_volume / 25_000_000 * 100)
    if spread_pct is not None:
        lane_limit = 0.75 if signal.get("lane") == "under5" else 0.35
        liquidity *= _clamp(1.0 - float(spread_pct) / max(lane_limit, 0.01), 0.0, 1.0)
        liquidity = _clamp(liquidity)
    relative = _scale(relative_volume, 5.0)
    remaining_capacity = _clamp((4.0 - extension_atr) / 4.0 * 100)
    technical = {
        "BREAKOUT": 90.0,
        "ACCELERATING": 82.0,
        "BUILDING": 65.0,
        "PARABOLIC": 25.0,
        "EXHAUSTED": 10.0,
        "FADING": 5.0,
    }.get(state, 40.0)
    if signal.get("base_high"):
        technical = min(100.0, technical + 10.0)

    factors: dict[str, float | None] = {
        "catalyst_quality": signal.get("catalyst_quality"),
        "liquidity": round(liquidity, 2),
        "relative_volume": round(relative, 2) if relative is not None else None,
        "sector_breadth": signal.get("sector_breadth"),
        "remaining_move_capacity": round(remaining_capacity, 2),
        "technical_structure": round(technical, 2),
        "historical_follow_through": signal.get("historical_follow_through"),
    }
    weights = {
        "catalyst_quality": 0.25,
        "liquidity": 0.15,
        "relative_volume": 0.10,
        "sector_breadth": 0.10,
        "remaining_move_capacity": 0.20,
        "technical_structure": 0.10,
        "historical_follow_through": 0.10,
    }
    known_weight = sum(weights[key] for key, value in factors.items() if value is not None)
    base = sum(weights[key] * float(value or 0) for key, value in factors.items())

    extension_risk = _clamp(extension_atr / 4.0 * 100)
    spread_risk = None
    if spread_pct is not None:
        lane_limit = 0.75 if signal.get("lane") == "under5" else 0.35
        spread_risk = _clamp(float(spread_pct) / lane_limit * 100)
    penalties: dict[str, float | None] = {
        "extension_risk": round(extension_risk, 2),
        "gap_fade_risk": signal.get("gap_fade_risk"),
        "float_dilution_risk": signal.get("float_dilution_risk"),
        "spread_risk": round(spread_risk, 2) if spread_risk is not None else None,
    }
    penalty_weights = {
        "extension_risk": 0.30,
        "gap_fade_risk": 0.25,
        "float_dilution_risk": 0.25,
        "spread_risk": 0.20,
    }
    penalty = sum(
        penalty_weights[key] * float(value or 0) for key, value in penalties.items()
    )
    final_score = _clamp(base - 0.45 * penalty)
    available_score = _clamp(base / known_weight) if known_weight else 0.0

    price = float(signal.get("price") or 0)
    atr = float(signal.get("short_atr") or 0)
    modeled_capacity_pct = (
        _clamp((4.0 - extension_atr), 0.0, 4.0) * atr / price * 100
        if price > 0 and atr > 0
        else None
    )
    signal.update(
        {
            "weighted_scale_version": "trader_brain_2026-08-20_v1",
            "weighted_factors": factors,
            "weighted_penalties": penalties,
            "weighted_opportunity_score": round(final_score, 2),
            "available_evidence_score": round(available_score, 2),
            "weighted_evidence_coverage_pct": round(known_weight * 100, 1),
            "missing_weighted_inputs": [key for key, value in factors.items() if value is None]
            + [key for key, value in penalties.items() if value is None],
            "modeled_move_capacity_pct": (
                round(modeled_capacity_pct, 3) if modeled_capacity_pct is not None else None
            ),
            "modeled_gain_disclaimer": (
                "ATR-based remaining-capacity estimate, not a return forecast or trade authority."
            ),
        }
    )
    return signal


def build_preliminary_trade_plan(signal: dict[str, Any]) -> dict[str, Any]:
    symbol = str(signal["symbol"])
    direction = str(signal.get("direction") or "UP")
    lane = str(signal.get("lane") or "regular_equity")
    trigger = signal.get("base_high")
    stop = signal.get("invalidation")
    blockers = list(signal.get("entry_rejection_reasons") or [])
    if direction == "DOWN":
        blockers.append("exact long-put contract, chain liquidity, debit and option stop are unresolved")
    if lane == "under5":
        blockers.append("fresh independently verified catalyst and under-$5 compliance checks required")
    for item in signal.get("missing_weighted_inputs") or []:
        blockers.append(f"weighted input unresolved: {item}")
    if not trigger or stop is None:
        blockers.append("controlled base trigger and structural invalidation are not complete")
    if signal.get("exhaustion_lock"):
        blockers.append("exhaustion lock active")
    if not signal.get("quote_fresh"):
        blockers.append("fresh live quote unresolved")
    if not signal.get("preliminary_liquidity_pass"):
        blockers.append("live spread/liquidity gate unresolved")

    risk_per_share = None
    t1 = None
    t2 = None
    quantity_cap = 0
    allocation_cap = 400.0 if lane == "under5" else 700.0
    planned_risk_cap = 12.0 if lane == "under5" else 20.0
    if direction == "UP" and trigger and stop is not None and float(trigger) > float(stop):
        risk_per_share = float(trigger) - float(stop)
        capacity_pct = float(signal.get("modeled_move_capacity_pct") or 0)
        capacity_price = float(trigger) * (1 + capacity_pct / 100)
        t1 = float(trigger) + risk_per_share
        t2 = min(float(trigger) + 2 * risk_per_share, capacity_price) if capacity_price > t1 else t1
        entry_ceiling = float(signal.get("limit_ceiling") or trigger)
        quantity_cap = max(
            0,
            min(floor(allocation_cap / entry_ceiling), floor(planned_risk_cap / risk_per_share)),
        )
        if quantity_cap < 1:
            blockers.append("one whole share cannot fit preliminary allocation/risk caps")

    status = "PRELIMINARY"
    if blockers:
        status = "WATCH_ONLY"
    if direction == "DOWN":
        status = "UNDERLYING_WATCH_ONLY"
    return {
        "schema_version": 1,
        "symbol": symbol,
        "observed_at": signal["observed_at"],
        "status": status,
        "direction": direction,
        "lane": lane,
        "setup": "controlled_base_breakout" if trigger else "structure_forming",
        "weighted_opportunity_score": signal.get("weighted_opportunity_score"),
        "available_evidence_score": signal.get("available_evidence_score"),
        "weighted_evidence_coverage_pct": signal.get("weighted_evidence_coverage_pct"),
        "modeled_move_capacity_pct": signal.get("modeled_move_capacity_pct"),
        "trigger": trigger,
        "review_limit_ceiling": signal.get("limit_ceiling"),
        "structural_stop": stop,
        "risk_per_share": round(risk_per_share, 6) if risk_per_share is not None else None,
        "t1": round(t1, 6) if t1 is not None else None,
        "t2": round(t2, 6) if t2 is not None else None,
        "preliminary_quantity_cap": quantity_cap,
        "preliminary_allocation_cap": allocation_cap,
        "preliminary_risk_cap": planned_risk_cap,
        "blockers": sorted(set(blockers)),
        "skip_conditions": [
            "support or structural stop fails",
            "spread/depth deteriorates",
            "price exceeds chase ceiling",
            "volume pace fails",
            "exhaustion cluster or halt appears",
            "filing, catalyst, dilution, manipulation, eligibility, broker or risk checks fail",
        ],
        "trade_authority": False,
        "broker_review_complete": False,
        "protection_confirmed": False,
    }
