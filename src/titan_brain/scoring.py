"""Independent setup-quality, execution-quality, and hard-gate decisions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Tuple

from .models import HardGateEvidence, SetupID


BASELINE_SETUP_WEIGHTS: Mapping[str, float] = {
    # Preserve the repository's documented baseline as the initial hypothesis.
    # Any future change must be versioned evidence, never a silent daily refit.
    "liquidity": 0.15,
    "relative_volume": 0.15,
    "technical_structure_vwap": 0.20,
    "catalyst_context": 0.15,
    "sector_market_sympathy": 0.10,
    "prior_90_day_behavior": 0.15,
    "gap_behavior": 0.05,
    "other_massive_data": 0.05,
}

EQUITY_EXECUTION_WEIGHTS: Mapping[str, float] = {
    "spread": 0.25,
    "displayed_depth": 0.20,
    "projected_slippage": 0.20,
    "volatility": 0.15,
    "order_size_liquidity": 0.10,
    "halt_risk": 0.10,
}

OPTION_EXECUTION_WEIGHTS: Mapping[str, float] = {
    "spread": 0.22,
    "spread_pct_premium": 0.18,
    "option_depth": 0.12,
    "underlying_liquidity": 0.12,
    "projected_slippage": 0.12,
    "contract_activity_open_interest": 0.10,
    "iv_greek_quality": 0.09,
    "expiration_risk": 0.05,
}


@dataclass(frozen=True)
class ScoreResult:
    score: float
    components: Mapping[str, float]
    weights: Mapping[str, float]


@dataclass(frozen=True)
class CandidateQualification:
    setup_id: SetupID
    setup_score: float
    execution_score: float
    hard_gates_passed: bool
    live_qualified: bool
    rejection_reasons: Tuple[str, ...]


def _weighted_score(
    components: Mapping[str, float], weights: Mapping[str, float]
) -> ScoreResult:
    if not weights:
        raise ValueError("weights cannot be empty")
    missing = tuple(key for key in weights if key not in components)
    extra = tuple(key for key in components if key not in weights)
    if missing:
        raise ValueError(f"missing score components: {', '.join(missing)}")
    if extra:
        raise ValueError(f"unexpected score components: {', '.join(extra)}")
    checked_components: dict[str, float] = {}
    checked_weights: dict[str, float] = {}
    for key, value in components.items():
        numeric = float(value)
        if not 0 <= numeric <= 100:
            raise ValueError(f"component {key} must be in [0, 100]")
        checked_components[key] = numeric
    for key, value in weights.items():
        numeric = float(value)
        if numeric < 0:
            raise ValueError(f"weight {key} cannot be negative")
        checked_weights[key] = numeric
    weight_total = sum(checked_weights.values())
    if weight_total <= 0:
        raise ValueError("weight total must be positive")
    score = sum(
        checked_components[key] * checked_weights[key] for key in checked_weights
    ) / weight_total
    return ScoreResult(
        score=round(score, 4),
        components=checked_components,
        weights=checked_weights,
    )


def score_setup(
    components: Mapping[str, float],
    weights: Mapping[str, float] = BASELINE_SETUP_WEIGHTS,
) -> ScoreResult:
    """Calculate SETUP_SCORE (0-100), never a probability claim."""

    return _weighted_score(components, weights)


def score_execution(
    components: Mapping[str, float], *, instrument_kind: str
) -> ScoreResult:
    """Calculate an independent EXECUTION_SCORE for stock or option routes."""

    if instrument_kind == "equity":
        weights = EQUITY_EXECUTION_WEIGHTS
    elif instrument_kind == "option":
        weights = OPTION_EXECUTION_WEIGHTS
    else:
        raise ValueError("instrument_kind must be 'equity' or 'option'")
    return _weighted_score(components, weights)


def qualify_candidate(
    *,
    setup_id: SetupID | str,
    setup_score: float,
    execution_score: float,
    hard_gates: HardGateEvidence,
    minimum_setup_score: float,
    minimum_execution_score: float,
) -> CandidateQualification:
    """Apply hard gates before scores; scores can never override a failed gate."""

    try:
        normalized_setup_id = (
            setup_id if isinstance(setup_id, SetupID) else SetupID(str(setup_id))
        )
    except ValueError as exc:
        raise ValueError("unknown setup_id; generic momentum is not accepted") from exc
    setup_score = float(setup_score)
    execution_score = float(execution_score)
    minimum_setup_score = float(minimum_setup_score)
    minimum_execution_score = float(minimum_execution_score)
    for name, value in (
        ("setup_score", setup_score),
        ("execution_score", execution_score),
        ("minimum_setup_score", minimum_setup_score),
        ("minimum_execution_score", minimum_execution_score),
    ):
        if not 0 <= value <= 100:
            raise ValueError(f"{name} must be in [0, 100]")

    reasons = list(hard_gates.failures)
    if setup_score < minimum_setup_score:
        reasons.append("SETUP_SCORE_BELOW_MINIMUM")
    if execution_score < minimum_execution_score:
        reasons.append("EXECUTION_SCORE_BELOW_MINIMUM")
    return CandidateQualification(
        setup_id=normalized_setup_id,
        setup_score=setup_score,
        execution_score=execution_score,
        hard_gates_passed=hard_gates.passed,
        live_qualified=not reasons,
        rejection_reasons=tuple(reasons),
    )
