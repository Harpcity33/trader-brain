"""Deterministic, broker-equity-based risk calculations.

This module is intentionally pure: callers must supply broker-confirmed account
state and the module returns a fail-closed decision without placing an order.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Any, Mapping

from .models import Instrument, RiskRecord, Session


def _finite(name: str, value: float) -> float:
    """Return one finite numeric value or fail closed.

    IEEE-754 ``NaN`` and infinities make ordinary comparison-based guards
    unreliable.  Money and risk inputs cross a production safety boundary, so
    they are rejected before any arithmetic is attempted.
    """

    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def _non_negative(name: str, value: float) -> float:
    value = _finite(name, value)
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


@dataclass(frozen=True)
class RiskLimits:
    normal_planned_risk_pct: float
    a_plus_planned_risk_pct: float
    premarket_planned_risk_pct: float
    premarket_stress_risk_pct: float
    max_single_trade_stress_risk_pct: float
    max_total_open_planned_risk_pct: float
    max_total_open_stress_risk_pct: float
    daily_new_entry_lock_pct: float
    hard_daily_loss_kill_pct: float
    weekly_loss_lock_pct: float
    live_drawdown_review_pct: float
    absolute_dollar_ceilings: Mapping[str, float | None]
    positive_execution_reserve_required: bool = True
    premarket_stress_reserve_required: bool = True

    def __post_init__(self) -> None:
        for name in (
            "normal_planned_risk_pct",
            "a_plus_planned_risk_pct",
            "premarket_planned_risk_pct",
            "premarket_stress_risk_pct",
            "max_single_trade_stress_risk_pct",
            "max_total_open_planned_risk_pct",
            "max_total_open_stress_risk_pct",
            "daily_new_entry_lock_pct",
            "hard_daily_loss_kill_pct",
            "weekly_loss_lock_pct",
            "live_drawdown_review_pct",
        ):
            value = _finite(name, getattr(self, name))
            if not 0 < value <= 1:
                raise ValueError(f"{name} must be in (0, 1]")
        for name, value in self.absolute_dollar_ceilings.items():
            if value is not None:
                ceiling = _finite(name, value)
                if ceiling <= 0:
                    raise ValueError(f"{name} must be positive or null")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "RiskLimits":
        reserve = raw.get("reserve_policy", {})
        return cls(
            normal_planned_risk_pct=float(raw["normal_planned_risk_pct"]),
            a_plus_planned_risk_pct=float(raw["a_plus_planned_risk_pct"]),
            premarket_planned_risk_pct=float(raw["premarket_planned_risk_pct"]),
            premarket_stress_risk_pct=float(raw["premarket_stress_risk_pct"]),
            max_single_trade_stress_risk_pct=float(
                raw["max_single_trade_stress_risk_pct"]
            ),
            max_total_open_planned_risk_pct=float(
                raw["max_total_open_planned_risk_pct"]
            ),
            max_total_open_stress_risk_pct=float(
                raw["max_total_open_stress_risk_pct"]
            ),
            daily_new_entry_lock_pct=float(raw["daily_new_entry_lock_pct"]),
            hard_daily_loss_kill_pct=float(raw["hard_daily_loss_kill_pct"]),
            weekly_loss_lock_pct=float(raw["weekly_loss_lock_pct"]),
            live_drawdown_review_pct=float(raw["live_drawdown_review_pct"]),
            absolute_dollar_ceilings=dict(raw.get("absolute_dollar_ceilings", {})),
            positive_execution_reserve_required=bool(
                reserve.get("positive_execution_reserve_required", True)
            ),
            premarket_stress_reserve_required=bool(
                reserve.get(
                    "premarket_stress_must_include_liquidity_slippage_reserve", True
                )
            ),
        )

    @classmethod
    def from_json(cls, path: str | Path) -> "RiskLimits":
        with Path(path).open("r", encoding="utf-8") as handle:
            return cls.from_mapping(json.load(handle))

    def cap_dollars(
        self,
        usable_equity: float,
        pct_field: str,
        absolute_field: str,
    ) -> float:
        usable_equity = _finite("usable_equity", usable_equity)
        if usable_equity <= 0:
            raise ValueError("usable_equity must be broker-confirmed and positive")
        percentage_cap = usable_equity * float(getattr(self, pct_field))
        absolute_cap = self.absolute_dollar_ceilings.get(absolute_field)
        return (
            percentage_cap
            if absolute_cap is None
            else min(percentage_cap, float(absolute_cap))
        )


@dataclass(frozen=True)
class RiskContext:
    current_usable_equity: float
    daily_realized_pnl: float | None = None
    weekly_realized_pnl: float | None = None
    peak_equity: float | None = None
    open_planned_risk_dollars: float = 0.0
    pending_planned_risk_dollars: float = 0.0
    open_stress_risk_dollars: float = 0.0
    pending_stress_risk_dollars: float = 0.0
    existing_execution_reserve_dollars: float = 0.0

    def __post_init__(self) -> None:
        if _finite("current_usable_equity", self.current_usable_equity) <= 0:
            raise ValueError("current_usable_equity must be positive")
        for name in ("daily_realized_pnl", "weekly_realized_pnl"):
            value = getattr(self, name)
            if value is not None:
                _finite(name, value)
        for name in (
            "open_planned_risk_dollars",
            "pending_planned_risk_dollars",
            "open_stress_risk_dollars",
            "pending_stress_risk_dollars",
            "existing_execution_reserve_dollars",
        ):
            _non_negative(name, getattr(self, name))
        if self.peak_equity is not None:
            if _finite("peak_equity", self.peak_equity) <= 0:
                raise ValueError("peak_equity must be positive when supplied")

    @property
    def daily_realized_loss_dollars(self) -> float | None:
        if self.daily_realized_pnl is None:
            return None
        return max(0.0, -_finite("daily_realized_pnl", self.daily_realized_pnl))

    @property
    def weekly_realized_loss_dollars(self) -> float | None:
        if self.weekly_realized_pnl is None:
            return None
        return max(0.0, -_finite("weekly_realized_pnl", self.weekly_realized_pnl))

    @property
    def drawdown_dollars(self) -> float | None:
        if self.peak_equity is None:
            return None
        return max(
            0.0,
            _finite("peak_equity", self.peak_equity)
            - _finite("current_usable_equity", self.current_usable_equity),
        )


@dataclass(frozen=True)
class RiskAmounts:
    planned_risk_dollars: float
    stress_risk_dollars: float
    execution_reserve_dollars: float
    premium_paid_dollars: float | None = None


def calculate_equity_risk(
    *,
    shares: int,
    entry_price: float,
    structural_stop: float,
    liquidity_slippage_reserve_per_share: float,
) -> RiskAmounts:
    """Return stop-defined stock risk with an explicit positive reserve."""

    if isinstance(shares, bool) or int(shares) != shares or shares <= 0:
        raise ValueError("shares must be a positive whole number")
    entry_price = _finite("entry_price", entry_price)
    structural_stop = _finite("structural_stop", structural_stop)
    reserve_per_share = _finite(
        "liquidity_slippage_reserve_per_share",
        liquidity_slippage_reserve_per_share,
    )
    if entry_price <= 0 or structural_stop <= 0:
        raise ValueError("entry_price and structural_stop must be positive")
    if structural_stop >= entry_price:
        raise ValueError("long-equity structural_stop must be below entry_price")
    if reserve_per_share <= 0:
        raise ValueError("liquidity/slippage reserve must be positive")
    planned = (entry_price - structural_stop) * shares
    reserve = reserve_per_share * shares
    return RiskAmounts(
        planned_risk_dollars=planned,
        stress_risk_dollars=planned + reserve,
        execution_reserve_dollars=reserve,
    )


def calculate_long_option_risk(
    *,
    contracts: int,
    premium_per_share: float,
    tactical_loss_dollars: float,
    execution_reserve_dollars: float,
    contract_multiplier: int = 100,
    fees_dollars: float = 0.0,
) -> RiskAmounts:
    """Return long-option risk, enforcing full premium as stress loss."""

    if isinstance(contracts, bool) or int(contracts) != contracts or contracts <= 0:
        raise ValueError("contracts must be a positive whole number")
    if contract_multiplier <= 0:
        raise ValueError("contract_multiplier must be positive")
    premium = _finite("premium_per_share", premium_per_share) * contracts * contract_multiplier
    fees = _non_negative("fees_dollars", fees_dollars)
    full_premium_loss = premium + fees
    tactical_loss = _finite("tactical_loss_dollars", tactical_loss_dollars)
    reserve = _finite("execution_reserve_dollars", execution_reserve_dollars)
    if premium <= 0:
        raise ValueError("premium_per_share must be positive")
    if tactical_loss <= 0 or tactical_loss > full_premium_loss:
        raise ValueError("tactical loss must be positive and no greater than premium")
    if reserve <= 0:
        raise ValueError("execution reserve must be positive")
    return RiskAmounts(
        planned_risk_dollars=tactical_loss,
        stress_risk_dollars=full_premium_loss,
        execution_reserve_dollars=reserve,
        premium_paid_dollars=full_premium_loss,
    )


def calculate_debit_spread_risk(
    *,
    contracts: int,
    net_debit_per_share: float,
    tactical_loss_dollars: float,
    execution_reserve_dollars: float,
    contract_multiplier: int = 100,
    fees_dollars: float = 0.0,
) -> RiskAmounts:
    """Return defined-risk debit-spread amounts using the full debit as stress."""

    return calculate_long_option_risk(
        contracts=contracts,
        premium_per_share=net_debit_per_share,
        tactical_loss_dollars=tactical_loss_dollars,
        execution_reserve_dollars=execution_reserve_dollars,
        contract_multiplier=contract_multiplier,
        fees_dollars=fees_dollars,
    )


def _round_money(value: float) -> float:
    return round(_finite("money", value) + 0.0, 2)


def assess_new_trade(
    *,
    limits: RiskLimits,
    context: RiskContext,
    amounts: RiskAmounts,
    session: Session,
    instrument: Instrument,
    quality_tier: str = "normal",
    multileg_eligible: bool = False,
) -> RiskRecord:
    """Evaluate one proposal and return all required audit fields.

    The decision fails closed when a daily/weekly/drawdown lock is active, when
    the proposal breaches a per-trade cap, or when it cannot fit both remaining
    daily and portfolio planned-risk headroom.
    """

    equity = float(context.current_usable_equity)
    planned = _non_negative("planned_risk_dollars", amounts.planned_risk_dollars)
    stress = _non_negative("stress_risk_dollars", amounts.stress_risk_dollars)
    reserve = _non_negative(
        "execution_reserve_dollars", amounts.execution_reserve_dollars
    )
    failures: list[str] = []

    if limits.positive_execution_reserve_required and reserve <= 0:
        failures.append("POSITIVE_EXECUTION_RESERVE_REQUIRED")
    if stress < planned:
        failures.append("STRESS_RISK_BELOW_PLANNED_RISK")
    if (
        session is Session.PREMARKET
        and limits.premarket_stress_reserve_required
        and stress < planned + reserve
    ):
        failures.append("PREMARKET_STRESS_RESERVE_MISSING")
    if instrument is Instrument.DEBIT_SPREAD and not multileg_eligible:
        failures.append("MULTILEG_ELIGIBILITY_UNVERIFIED")
    if instrument in (Instrument.LONG_CALL, Instrument.LONG_PUT):
        if amounts.premium_paid_dollars is None:
            failures.append("FULL_PREMIUM_STRESS_EVIDENCE_MISSING")
        elif stress + 1e-9 < float(amounts.premium_paid_dollars):
            failures.append("FULL_PREMIUM_STRESS_NOT_COVERED")

    if session is Session.PREMARKET:
        planned_pct_field = "premarket_planned_risk_pct"
        planned_abs_field = "premarket_planned_risk_dollars"
        stress_pct_field = "premarket_stress_risk_pct"
        stress_abs_field = "premarket_stress_risk_dollars"
    elif quality_tier == "a_plus":
        planned_pct_field = "a_plus_planned_risk_pct"
        planned_abs_field = "a_plus_planned_risk_dollars"
        stress_pct_field = "max_single_trade_stress_risk_pct"
        stress_abs_field = "max_single_trade_stress_risk_dollars"
    elif quality_tier == "normal":
        planned_pct_field = "normal_planned_risk_pct"
        planned_abs_field = "normal_planned_risk_dollars"
        stress_pct_field = "max_single_trade_stress_risk_pct"
        stress_abs_field = "max_single_trade_stress_risk_dollars"
    else:
        raise ValueError("quality_tier must be 'normal' or 'a_plus'")

    planned_cap = limits.cap_dollars(
        equity, planned_pct_field, planned_abs_field
    )
    stress_cap = limits.cap_dollars(equity, stress_pct_field, stress_abs_field)
    daily_cap = limits.cap_dollars(
        equity, "daily_new_entry_lock_pct", "daily_new_entry_lock_dollars"
    )
    hard_daily_cap = limits.cap_dollars(
        equity, "hard_daily_loss_kill_pct", "hard_daily_loss_kill_dollars"
    )
    weekly_cap = limits.cap_dollars(
        equity, "weekly_loss_lock_pct", "weekly_loss_lock_dollars"
    )
    drawdown_cap = limits.cap_dollars(
        equity, "live_drawdown_review_pct", "live_drawdown_review_dollars"
    )
    portfolio_cap = limits.cap_dollars(
        equity,
        "max_total_open_planned_risk_pct",
        "max_total_open_planned_risk_dollars",
    )
    portfolio_stress_cap = limits.cap_dollars(
        equity,
        "max_total_open_stress_risk_pct",
        "max_total_open_stress_risk_dollars",
    )

    daily_loss = context.daily_realized_loss_dollars
    weekly_loss = context.weekly_realized_loss_dollars
    drawdown = context.drawdown_dollars
    if daily_loss is None:
        failures.append("DAILY_REALIZED_PNL_UNAVAILABLE")
        daily_loss_for_capacity = daily_cap
    else:
        daily_loss_for_capacity = daily_loss
    if weekly_loss is None:
        failures.append("WEEKLY_REALIZED_PNL_UNAVAILABLE")
    if drawdown is None:
        failures.append("PEAK_EQUITY_OR_DRAWDOWN_UNAVAILABLE")

    if daily_loss is not None and daily_loss >= daily_cap:
        failures.append("DAILY_NEW_ENTRY_LOCK")
    if daily_loss is not None and daily_loss >= hard_daily_cap:
        failures.append("HARD_DAILY_LOSS_KILL")
    if weekly_loss is not None and weekly_loss >= weekly_cap:
        failures.append("WEEKLY_LOSS_LOCK")
    if drawdown is not None and drawdown >= drawdown_cap:
        failures.append("LIVE_DRAWDOWN_REVIEW_REQUIRED")
    if planned > planned_cap + 1e-9:
        failures.append("PLANNED_RISK_CAP_EXCEEDED")
    if stress > stress_cap + 1e-9:
        failures.append("STRESS_RISK_CAP_EXCEEDED")

    existing_at_risk = (
        float(context.open_planned_risk_dollars)
        + float(context.pending_planned_risk_dollars)
        + float(context.existing_execution_reserve_dollars)
    )
    proposal_budget_use = planned + reserve
    remaining_daily = (
        daily_cap
        - daily_loss_for_capacity
        - existing_at_risk
        - proposal_budget_use
    )
    remaining_portfolio = portfolio_cap - existing_at_risk - proposal_budget_use
    existing_stress = (
        float(context.open_stress_risk_dollars)
        + float(context.pending_stress_risk_dollars)
    )
    remaining_portfolio_stress = portfolio_stress_cap - existing_stress - stress
    if remaining_daily < -1e-9:
        failures.append("INSUFFICIENT_DAILY_RISK_HEADROOM")
    if remaining_portfolio < -1e-9:
        failures.append("INSUFFICIENT_PORTFOLIO_RISK_HEADROOM")
    if remaining_portfolio_stress < -1e-9:
        failures.append("INSUFFICIENT_PORTFOLIO_STRESS_HEADROOM")

    # Preserve stable ordering while removing duplicate reasons.
    unique_failures = tuple(dict.fromkeys(failures))
    return RiskRecord(
        planned_risk_dollars=_round_money(planned),
        planned_risk_pct=planned / equity,
        stress_risk_dollars=_round_money(stress),
        stress_risk_pct=stress / equity,
        execution_reserve_dollars=_round_money(reserve),
        remaining_daily_risk=_round_money(remaining_daily),
        remaining_portfolio_risk=_round_money(remaining_portfolio),
        remaining_portfolio_stress_risk=_round_money(remaining_portfolio_stress),
        allowed=not unique_failures,
        failures=unique_failures,
    )
