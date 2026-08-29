"""Conservative stock-versus-option expectancy routing.

All feasible routes are returned for shadow logging, including rejected routes.
Only a positive after-cost expectancy route that passes hard gates and score
floors can be selected.
"""

from __future__ import annotations

from typing import Iterable

from .models import (
    HardGateEvidence,
    Instrument,
    RouteDecision,
    RouteEvaluation,
    RouteInput,
)


OPTION_INSTRUMENTS = (Instrument.LONG_CALL, Instrument.LONG_PUT)


def _validate(route: RouteInput) -> None:
    for name, value in (
        ("setup_score", route.setup_score),
        ("execution_score", route.execution_score),
    ):
        if not 0 <= float(value) <= 100:
            raise ValueError(f"{name} must be in [0, 100]")
    for name, value in (
        ("target_probability", route.target_probability),
        ("loss_probability", route.loss_probability),
    ):
        if not 0 <= float(value) <= 1:
            raise ValueError(f"{name} must be in [0, 1]")
    if route.target_probability + route.loss_probability > 1 + 1e-12:
        raise ValueError("target_probability + loss_probability cannot exceed 1")
    for name, value in (
        ("target_profit_dollars", route.target_profit_dollars),
        ("loss_if_wrong_dollars", route.loss_if_wrong_dollars),
        ("stress_loss_dollars", route.stress_loss_dollars),
        ("spread_cost_dollars", route.spread_cost_dollars),
        ("slippage_cost_dollars", route.slippage_cost_dollars),
        ("fees_dollars", route.fees_dollars),
        ("theta_cost_dollars", route.theta_cost_dollars),
        ("iv_change_cost_dollars", route.iv_change_cost_dollars),
        ("uncertainty_reserve_dollars", route.uncertainty_reserve_dollars),
    ):
        if float(value) < 0:
            raise ValueError(f"{name} cannot be negative")


def evaluate_route(
    route: RouteInput,
    *,
    hard_gates: HardGateEvidence,
    minimum_setup_score: float,
    minimum_execution_score: float,
    minimum_expectancy_r: float = 0.0,
) -> RouteEvaluation:
    _validate(route)
    reasons: list[str] = []
    if not route.feasible:
        reasons.append("ROUTE_INFEASIBLE")
    reasons.extend(hard_gates.failures)
    if route.setup_score < minimum_setup_score:
        reasons.append("SETUP_SCORE_BELOW_MINIMUM")
    if route.execution_score < minimum_execution_score:
        reasons.append("EXECUTION_SCORE_BELOW_MINIMUM")
    if route.stress_loss_dollars <= 0:
        reasons.append("STRESS_LOSS_REQUIRED")
    if route.loss_if_wrong_dollars > route.stress_loss_dollars + 1e-9:
        reasons.append("TACTICAL_LOSS_EXCEEDS_STRESS_LOSS")
    if route.instrument in OPTION_INSTRUMENTS:
        if route.premium_paid_dollars is None:
            reasons.append("FULL_PREMIUM_STRESS_EVIDENCE_MISSING")
        elif route.stress_loss_dollars + 1e-9 < route.premium_paid_dollars:
            reasons.append("FULL_PREMIUM_STRESS_NOT_COVERED")
    if route.instrument is Instrument.DEBIT_SPREAD:
        if route.premium_paid_dollars is None:
            reasons.append("FULL_DEBIT_STRESS_EVIDENCE_MISSING")
        elif route.stress_loss_dollars + 1e-9 < route.premium_paid_dollars:
            reasons.append("FULL_DEBIT_STRESS_NOT_COVERED")

    total_costs = sum(
        (
            route.spread_cost_dollars,
            route.slippage_cost_dollars,
            route.fees_dollars,
            route.theta_cost_dollars,
            route.iv_change_cost_dollars,
            route.uncertainty_reserve_dollars,
        )
    )
    net_expectancy = (
        route.target_probability * route.target_profit_dollars
        - route.loss_probability * route.loss_if_wrong_dollars
        - total_costs
    )
    expectancy_r = (
        net_expectancy / route.stress_loss_dollars
        if route.stress_loss_dollars > 0
        else float("-inf")
    )
    if expectancy_r <= minimum_expectancy_r:
        reasons.append("NON_POSITIVE_OR_INSUFFICIENT_NET_EXPECTANCY")
    return RouteEvaluation(
        instrument=route.instrument,
        feasible=route.feasible,
        net_expectancy_dollars=round(net_expectancy, 4),
        net_expectancy_r=round(expectancy_r, 6),
        stress_loss_dollars=round(route.stress_loss_dollars, 2),
        total_costs_dollars=round(total_costs, 4),
        rejection_reasons=tuple(dict.fromkeys(reasons)),
        metadata=dict(route.metadata),
    )


def select_instrument(
    routes: Iterable[RouteInput],
    *,
    hard_gates: HardGateEvidence,
    minimum_setup_score: float,
    minimum_execution_score: float,
    minimum_expectancy_r: float = 0.0,
) -> RouteDecision:
    """Return the best conservative net-expectancy route or explicit NO_TRADE."""

    evaluations = tuple(
        evaluate_route(
            route,
            hard_gates=hard_gates,
            minimum_setup_score=minimum_setup_score,
            minimum_execution_score=minimum_execution_score,
            minimum_expectancy_r=minimum_expectancy_r,
        )
        for route in routes
    )
    eligible = [item for item in evaluations if not item.rejection_reasons]
    if not eligible:
        return RouteDecision(
            selected_instrument=Instrument.NO_TRADE,
            evaluations=evaluations,
            reason="No feasible route passed hard gates, score floors, full-risk evidence, and conservative after-cost expectancy.",
        )
    selected = max(
        eligible,
        key=lambda item: (
            item.net_expectancy_r,
            item.net_expectancy_dollars,
            -item.total_costs_dollars,
        ),
    )
    return RouteDecision(
        selected_instrument=selected.instrument,
        evaluations=evaluations,
        reason="Selected the feasible route with the strongest conservative expected net return per stress-risk dollar.",
    )
