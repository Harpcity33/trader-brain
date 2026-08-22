from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CandidatePolicyDecision:
    entry_eligible: bool
    watch_eligible: bool
    rejection_reasons: tuple[str, ...]


def evaluate_shadow_candidate(signal: dict[str, Any], config: Any) -> CandidatePolicyDecision:
    """Apply structural/liquidity discovery gates without treating score as direction.

    The opportunity score remains available for ranking. Catalyst and state may be
    optionally restored as hard gates through configuration, but the evidence-backed
    shadow policy defaults both off. Exhaustion remains a separate tail/chase control.
    """
    price = float(signal.get("price") or 0)
    gap = signal.get("gap_pct")
    dollar_volume = float(signal.get("dollar_volume") or 0)
    strength = float(signal.get("signal_strength") or 0)
    lane = str(signal.get("lane") or "regular_equity")
    minimum_dollar_volume = (
        config.under5_min_dollar_volume
        if lane == "under5"
        else config.candidate_min_dollar_volume
    )
    price_ok = config.candidate_min_price <= price <= config.candidate_max_price
    direction_ok = not (signal.get("direction") == "DOWN" and price <= 5)
    gap_ok = gap is not None and abs(float(gap)) >= config.candidate_min_gap_pct
    watch_gap_ok = gap is not None and abs(float(gap)) >= config.watch_min_gap_pct
    volume_ok = dollar_volume >= minimum_dollar_volume
    watch_volume_ok = dollar_volume >= minimum_dollar_volume * 0.50
    score_ok = (
        strength >= config.candidate_min_signal_strength
        if config.score_is_entry_gate
        else True
    )
    watch_score_ok = (
        strength >= config.watch_min_signal_strength
        if config.score_is_entry_gate
        else True
    )
    news_ok = bool(signal.get("catalyst_verified")) if config.fresh_news_required else True
    allowed_states = set(getattr(config, "entry_states", ()))
    state_ok = (
        str(signal.get("state")) in allowed_states
        if config.state_is_entry_gate
        else True
    )
    structure_ok = bool(signal.get("entry_setup_eligible", True))

    entry_eligible = all(
        (price_ok, direction_ok, gap_ok, volume_ok, score_ok, news_ok, state_ok, structure_ok)
    )
    watch_eligible = all(
        (price_ok, direction_ok, watch_gap_ok, watch_volume_ok, watch_score_ok)
    )
    reasons: list[str] = []
    if not price_ok:
        reasons.append("price outside configured candidate range")
    if not direction_ok:
        reasons.append("downside board is limited to option-eligible above-$5 underlyings")
    if not gap_ok:
        reasons.append("move below entry-candidate gap threshold")
    if not volume_ok:
        reasons.append("dollar volume below entry threshold")
    if not score_ok:
        reasons.append("opportunity score below configured hard gate")
    if not news_ok:
        reasons.append("fresh catalyst required by configured hard gate")
    if not state_ok:
        reasons.append("state excluded by configured hard gate")
    if not structure_ok:
        reasons.append("exhaustion or structural entry lock active")
    return CandidatePolicyDecision(entry_eligible, watch_eligible, tuple(reasons))


def spread_to_risk_gate(
    *, trigger: float, invalidation: float, bid: float, ask: float,
    max_ratio: float,
) -> tuple[bool, float | None]:
    """Compare one full quoted spread with structural risk per share."""
    risk = trigger - invalidation
    spread = ask - bid
    if risk <= 0 or spread < 0:
        return False, None
    ratio = spread / risk
    return ratio <= max_ratio, ratio
