"""Walk-forward evidence and explicit, non-automatic edge promotion policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence


@dataclass(frozen=True)
class WalkForwardSplit:
    train_indices: tuple[int, ...]
    test_indices: tuple[int, ...]


def time_ordered_walk_forward_splits(
    sample_count: int,
    *,
    minimum_train: int,
    test_window: int,
    step: int | None = None,
) -> tuple[WalkForwardSplit, ...]:
    """Build expanding-window splits without future leakage."""

    if sample_count < 0 or minimum_train <= 0 or test_window <= 0:
        raise ValueError("sample_count must be nonnegative and windows positive")
    advance = test_window if step is None else step
    if advance <= 0:
        raise ValueError("step must be positive")
    splits: list[WalkForwardSplit] = []
    train_end = minimum_train
    while train_end + test_window <= sample_count:
        splits.append(
            WalkForwardSplit(
                train_indices=tuple(range(0, train_end)),
                test_indices=tuple(range(train_end, train_end + test_window)),
            )
        )
        train_end += advance
    return tuple(splits)


def maturity_stage(observation_count: int) -> str:
    if observation_count < 0:
        raise ValueError("observation_count cannot be negative")
    if observation_count < 30:
        return "EXPLORATORY"
    if observation_count < 100:
        return "DEVELOPING"
    if observation_count < 200:
        return "PROVISIONAL_EVIDENCE"
    return "PROMOTION_CANDIDATE_SAMPLE_SIZE"


@dataclass(frozen=True)
class PromotionAssessment:
    stage: str
    promotion_candidate: bool
    reasons: tuple[str, ...]
    automatic_production_mutation: bool = False


def assess_promotion_candidate(
    *,
    observation_count: int,
    net_expectancy_r: float,
    max_drawdown_r: float,
    regime_ids: Sequence[str],
    outlier_dependency: bool,
    execution_quality_acceptable: bool,
    failure_conditions_documented: bool,
    out_of_sample_positive: bool,
) -> PromotionAssessment:
    """Assess evidence; never modify a promoted-edge file or live strategy."""

    stage = maturity_stage(observation_count)
    reasons: list[str] = []
    if observation_count < 200:
        reasons.append("FEWER_THAN_200_OBSERVATIONS")
    if float(net_expectancy_r) <= 0:
        reasons.append("NON_POSITIVE_NET_EXPECTANCY_AFTER_COSTS")
    if float(max_drawdown_r) < 0:
        raise ValueError("max_drawdown_r must be nonnegative")
    if len({item for item in regime_ids if str(item).strip()}) < 2:
        reasons.append("MULTI_REGIME_EVIDENCE_MISSING")
    if outlier_dependency:
        reasons.append("DEPENDENT_ON_OUTLIER")
    if not execution_quality_acceptable:
        reasons.append("EXECUTION_QUALITY_UNACCEPTABLE")
    if not failure_conditions_documented:
        reasons.append("FAILURE_CONDITIONS_UNDOCUMENTED")
    if not out_of_sample_positive:
        reasons.append("OUT_OF_SAMPLE_EVIDENCE_NOT_POSITIVE")
    return PromotionAssessment(
        stage=stage,
        promotion_candidate=not reasons,
        reasons=tuple(reasons),
        automatic_production_mutation=False,
    )


__all__ = [
    "PromotionAssessment",
    "WalkForwardSplit",
    "assess_promotion_candidate",
    "maturity_stage",
    "time_ordered_walk_forward_splits",
]

