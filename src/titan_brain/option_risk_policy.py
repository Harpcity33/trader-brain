"""Versioned options risk policy and trade-specific loss budgeting (offline).

Milestone 5 of the attended-options plan.

The legacy stock/session risk path applies a fixed percentage loss rule and
maintains a durable loss latch and incident history.  The plan requires that,
for the OPTIONS path only, the fixed rule be replaced with explicit,
trade-specific dollar budgets -- WITHOUT deleting or weakening the legacy latch,
incident history, or the stock rule.  This module therefore introduces a
DISTINCT, versioned options policy object and a presenter, entirely separate
from the legacy policy; it touches neither ``policy.py`` nor
``session_trading_calculation.py``.

Responsibilities (pure, offline, analysis-only):

* hold a versioned options policy (accepted per-trade dollar budgets + an
  account-equity ceiling fraction), explicitly NOT a universal loss percentage;
* present the three loss figures (planned-exit, adverse-stress, full premium +
  fees) in dollars AND as a percentage of premium AND as a percentage of
  current account equity;
* aggregate this trade's stress loss with already-accepted CORRELATED exposure
  (same underlying) so a new entry is judged against combined risk;
* judge the trade against the budgets, failing closed on any breach.

It decides nothing about whether to trade and authorizes nothing.  Full-account
capacity is a ceiling recalculated from fresh facts -- never borrowed funds, an
all-in recommendation, or a guarantee that loss is limited to a stop.  Money is
Decimal end-to-end.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from typing import Mapping, Sequence


POLICY_VERSION = "titan_options_risk_policy_v1"


class OptionsPolicyError(ValueError):
    """A well-formed, non-secret options-policy error."""


def _money(value: object, field_name: str, *, minimum: Decimal | None = None) -> Decimal:
    if isinstance(value, bool) or type(value) not in (str, int):
        raise OptionsPolicyError(f"{field_name} must be a string or integer, not float/other")
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise OptionsPolicyError(f"{field_name} is not a valid decimal") from exc
    if not parsed.is_finite():
        raise OptionsPolicyError(f"{field_name} must be finite")
    if minimum is not None and parsed < minimum:
        raise OptionsPolicyError(f"{field_name} must be >= {minimum}")
    return parsed


@dataclass(frozen=True)
class OptionsRiskPolicy:
    """A versioned, trade-specific options risk policy.

    Budgets are explicit dollar amounts the owner accepted for THIS trade, not a
    universal percentage.  ``account_equity_ceiling_fraction`` is a full-account
    allocation ceiling (0 < f <= 1) recalculated from fresh equity -- a cap, not
    a target or a recommendation to allocate that much.
    """

    version: str
    max_planned_loss: Decimal
    max_stress_loss: Decimal
    max_premium_exposure: Decimal
    account_equity_ceiling_fraction: Decimal

    def __post_init__(self) -> None:
        if self.version != POLICY_VERSION:
            raise OptionsPolicyError("unrecognized options policy version")
        object.__setattr__(self, "max_planned_loss", _money(self.max_planned_loss, "max_planned_loss", minimum=Decimal(0)))
        object.__setattr__(self, "max_stress_loss", _money(self.max_stress_loss, "max_stress_loss", minimum=Decimal(0)))
        object.__setattr__(self, "max_premium_exposure", _money(self.max_premium_exposure, "max_premium_exposure", minimum=Decimal(0)))
        fraction = _money(self.account_equity_ceiling_fraction, "account_equity_ceiling_fraction", minimum=Decimal(0))
        if fraction <= 0 or fraction > 1:
            raise OptionsPolicyError("account_equity_ceiling_fraction must be in (0, 1]")
        object.__setattr__(self, "account_equity_ceiling_fraction", fraction)


@dataclass(frozen=True)
class CorrelatedExposure:
    """An already-accepted position on the same underlying (its stress loss)."""

    underlying_symbol: str
    accepted_stress_loss: Decimal


@dataclass(frozen=True)
class OptionsRiskAssessment:
    ok: bool
    blockers: tuple[str, ...]
    figures: Mapping[str, object]


def assess_options_trade_risk(
    *,
    policy: OptionsRiskPolicy,
    underlying_symbol: str,
    planned_loss: object,
    stress_loss: object,
    premium_exposure: object,
    account_equity: object,
    correlated: Sequence[CorrelatedExposure] = (),
) -> OptionsRiskAssessment:
    """Judge a trade's losses against the versioned budgets and equity ceiling.

    Presents each loss in dollars, as a percent of premium exposure, and as a
    percent of current account equity; aggregates this trade's stress loss with
    already-accepted correlated (same-underlying) stress; and fails closed on
    any budget or ceiling breach.  This does NOT touch the legacy stock/session
    loss latch or incident history.
    """

    if not isinstance(policy, OptionsRiskPolicy):
        raise OptionsPolicyError("policy must be an OptionsRiskPolicy")
    if not isinstance(underlying_symbol, str) or not underlying_symbol.strip():
        raise OptionsPolicyError("underlying_symbol is required")

    with localcontext() as context:
        context.prec = 80
        planned = _money(planned_loss, "planned_loss", minimum=Decimal(0))
        stress = _money(stress_loss, "stress_loss", minimum=Decimal(0))
        premium = _money(premium_exposure, "premium_exposure", minimum=Decimal(0))
        equity = _money(account_equity, "account_equity", minimum=Decimal(0))

        # Aggregate correlated same-underlying accepted stress with this trade.
        correlated_stress = Decimal(0)
        for item in correlated:
            if not isinstance(item, CorrelatedExposure):
                raise OptionsPolicyError("correlated items must be CorrelatedExposure")
            if item.underlying_symbol == underlying_symbol:
                correlated_stress += _money(item.accepted_stress_loss, "accepted_stress_loss", minimum=Decimal(0))
        aggregate_stress = stress + correlated_stress

        blockers: list[str] = []
        if planned > policy.max_planned_loss:
            blockers.append("PLANNED_LOSS_EXCEEDS_BUDGET")
        if stress > policy.max_stress_loss:
            blockers.append("STRESS_LOSS_EXCEEDS_BUDGET")
        if premium > policy.max_premium_exposure:
            blockers.append("PREMIUM_EXPOSURE_EXCEEDS_BUDGET")
        # Correlated aggregate stress must also fit the stress budget.
        if aggregate_stress > policy.max_stress_loss:
            blockers.append("CORRELATED_AGGREGATE_STRESS_EXCEEDS_BUDGET")
        # Full-account ceiling: premium exposure must not exceed the fraction of
        # current equity. A zero-equity account cannot support any exposure.
        ceiling = equity * policy.account_equity_ceiling_fraction
        if premium > ceiling:
            blockers.append("PREMIUM_EXPOSURE_EXCEEDS_EQUITY_CEILING")

        figures = {
            "policy_version": policy.version,
            "planned_loss": _num(planned),
            "stress_loss": _num(stress),
            "premium_exposure": _num(premium),
            "aggregate_correlated_stress_loss": _num(aggregate_stress),
            "account_equity": _num(equity),
            "equity_ceiling_amount": _num(ceiling),
            "planned_loss_pct_of_premium": _pct(planned, premium),
            "stress_loss_pct_of_premium": _pct(stress, premium),
            "premium_pct_of_premium": _pct(premium, premium),
            "planned_loss_pct_of_equity": _pct(planned, equity),
            "stress_loss_pct_of_equity": _pct(stress, equity),
            "premium_pct_of_equity": _pct(premium, equity),
            "aggregate_stress_pct_of_equity": _pct(aggregate_stress, equity),
            "disclaimer": (
                "budgets are accepted dollar limits; equity ceiling is a cap not a "
                "target; loss is not guaranteed to be limited to any stop; full "
                "premium can be lost; no borrowed funds are implied"
            ),
        }
        unique: list[str] = []
        for code in blockers:
            if code not in unique:
                unique.append(code)
        return OptionsRiskAssessment(ok=not unique, blockers=tuple(unique), figures=figures)


def _num(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text if text not in ("-0", "") else "0"


def _pct(part: Decimal, whole: Decimal):
    # Returns None when the denominator is non-positive (caller must handle).
    if whole <= 0:
        return None
    return _num((part / whole * Decimal(100)).quantize(Decimal("0.000001")))


__all__ = [
    "CorrelatedExposure",
    "OptionsRiskAssessment",
    "OptionsRiskPolicy",
    "OptionsPolicyError",
    "POLICY_VERSION",
    "assess_options_trade_risk",
]
