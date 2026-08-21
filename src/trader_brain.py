"""Deterministic Trader Brain screening and scoring primitives."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any

WEIGHTS = {
    "liquidity": 15,
    "relative_volume": 15,
    "technical_structure": 20,
    "catalyst_context": 15,
    "sector_market_sympathy": 10,
    "prior_90d_behavior": 15,
    "gap_behavior": 5,
    "other_massive_data": 5,
}


@dataclass(frozen=True)
class Candidate:
    ticker: str
    price: float
    volume: int
    factors: dict[str, float]
    has_fresh_news: bool = False


def eligibility(candidate: Candidate) -> tuple[bool, list[str]]:
    reasons = []
    if candidate.price <= 5.0:
        reasons.append("price_not_strictly_above_5")
    if candidate.volume < 750_000:
        reasons.append("volume_below_750000")
    return not reasons, reasons


def score(candidate: Candidate) -> dict[str, Any]:
    eligible, reasons = eligibility(candidate)
    if not eligible:
        raise ValueError(",".join(reasons))
    components = {}
    for name, cap in WEIGHTS.items():
        raw = candidate.factors.get(name)
        # Missing inputs are visible and neutral-low; news absence is never a gate.
        normalized = 0.5 if raw is None else min(1.0, max(0.0, float(raw)))
        components[name] = round(normalized * cap, 2)
    total = round(sum(components.values()), 2)
    return {
        "ticker": candidate.ticker,
        "eligible": True,
        "score_version": "0.1.0-hypothesis",
        "score": total,
        "components": components,
        "fresh_news": candidate.has_fresh_news,
        "missing_factors": [k for k in WEIGHTS if k not in candidate.factors],
    }


def screen_and_rank(candidates: list[Candidate]) -> dict[str, Any]:
    accepted, rejected = [], []
    for candidate in candidates:
        ok, reasons = eligibility(candidate)
        if ok:
            accepted.append(score(candidate))
        else:
            rejected.append({"ticker": candidate.ticker, "reasons": reasons, "input": asdict(candidate)})
    accepted.sort(key=lambda x: (-x["score"], x["ticker"]))
    return {"accepted": accepted, "rejected": rejected}
