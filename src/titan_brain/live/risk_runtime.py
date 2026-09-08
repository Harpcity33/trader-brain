"""Exact-money, account-wide risk enforcement for the execution writer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable

from .money import decimal_value
from .plans import ExpiringPlan
from .policy import PolicyBundle


ZERO = Decimal("0")


@dataclass(frozen=True)
class RiskExposure:
    reference: str
    category: str
    planned_risk: Decimal
    stress_risk: Decimal
    execution_reserve: Decimal
    notional: Decimal
    protected: bool

    @classmethod
    def build(
        cls,
        *,
        reference: str,
        category: str,
        planned_risk: object,
        stress_risk: object,
        execution_reserve: object,
        notional: object,
        protected: bool,
    ) -> "RiskExposure":
        values = {
            name: decimal_value(value, name)
            for name, value in (
                ("planned_risk", planned_risk),
                ("stress_risk", stress_risk),
                ("execution_reserve", execution_reserve),
                ("notional", notional),
            )
        }
        if any(value < 0 for value in values.values()):
            raise ValueError("risk exposure values cannot be negative")
        if values["stress_risk"] < values["planned_risk"]:
            raise ValueError("stress risk cannot be below planned risk")
        if not reference or category not in {"open", "pending", "unknown", "manual"}:
            raise ValueError("invalid risk exposure identity")
        return cls(reference=reference, category=category, protected=protected is True, **values)


@dataclass(frozen=True)
class AccountRiskSnapshot:
    account_last4: str
    observed_at: datetime
    usable_equity: Decimal
    unleveraged_buying_power: Decimal
    cash: Decimal
    daily_realized_pnl: Decimal
    weekly_realized_pnl: Decimal
    peak_equity: Decimal
    exposures: tuple[RiskExposure, ...]
    account_active: bool
    restricted: bool
    standard_orders_reconciled: bool
    option_orders_reconciled: bool
    advanced_orders_reconciled: bool
    positions_reconciled: bool

    @classmethod
    def build(cls, **raw: object) -> "AccountRiskSnapshot":
        observed = raw["observed_at"]
        if not isinstance(observed, datetime) or observed.tzinfo is None:
            raise ValueError("broker risk snapshot time must be timezone-aware")
        exposures = raw.get("exposures", ())
        if not isinstance(exposures, (list, tuple)) or any(
            not isinstance(item, RiskExposure) for item in exposures
        ):
            raise ValueError("exposures must contain RiskExposure records")
        money = {
            name: decimal_value(raw[name], name)
            for name in (
                "usable_equity",
                "unleveraged_buying_power",
                "cash",
                "daily_realized_pnl",
                "weekly_realized_pnl",
                "peak_equity",
            )
        }
        for name in ("usable_equity", "unleveraged_buying_power", "cash"):
            if money[name] < 0:
                raise ValueError(f"{name} cannot be negative")
        if money["peak_equity"] <= 0:
            raise ValueError("peak_equity must be positive")
        if money["peak_equity"] < money["usable_equity"]:
            raise ValueError("peak_equity cannot be below usable_equity")
        return cls(
            account_last4=str(raw["account_last4"]),
            observed_at=observed,
            exposures=tuple(exposures),
            account_active=raw.get("account_active") is True,
            restricted=raw.get("restricted") is True,
            standard_orders_reconciled=raw.get("standard_orders_reconciled") is True,
            option_orders_reconciled=raw.get("option_orders_reconciled") is True,
            advanced_orders_reconciled=raw.get("advanced_orders_reconciled") is True,
            positions_reconciled=raw.get("positions_reconciled") is True,
            **money,
        )


@dataclass(frozen=True)
class SessionLatch:
    trading_date: date
    loss_lock: bool = False
    hard_kill: bool = False
    profit_goal_crossed: bool = False
    first_profit_crossed_at: datetime | None = None
    highest_realized_pnl: Decimal = ZERO


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    failures: tuple[str, ...]
    proposal_planned_risk: Decimal
    proposal_stress_risk: Decimal
    proposal_reserve: Decimal
    remaining_daily_headroom: Decimal
    remaining_portfolio_headroom: Decimal
    remaining_stress_headroom: Decimal
    remaining_buying_power: Decimal


def _cap(policy: PolicyBundle, equity: Decimal, percent_name: str, absolute_name: str) -> Decimal:
    percent = decimal_value(policy.risk_raw[percent_name], percent_name)
    result = equity * percent
    absolute = policy.risk_raw.get("absolute_dollar_ceilings", {}).get(absolute_name)
    if absolute is not None:
        result = min(result, decimal_value(absolute, absolute_name))
    return result


def update_session_latch(
    policy: PolicyBundle,
    prior: SessionLatch,
    *,
    realized_pnl: object,
    usable_equity: object,
    observed_at: datetime,
) -> SessionLatch:
    if observed_at.tzinfo is None or observed_at.date() != prior.trading_date:
        raise ValueError("latch update must use the same trading date and aware time")
    pnl = decimal_value(realized_pnl, "realized_pnl")
    equity = decimal_value(usable_equity, "usable_equity")
    if equity <= 0:
        raise ValueError("usable equity must be positive")
    absolute_daily = decimal_value(
        policy.config["risk"]["daily_realized_loss_lock_dollars"], "daily_lock"
    )
    daily_cap = min(
        absolute_daily,
        _cap(
            policy,
            equity,
            "daily_new_entry_lock_pct",
            "daily_new_entry_lock_dollars",
        ),
    )
    hard_cap = _cap(
        policy,
        equity,
        "hard_daily_loss_kill_pct",
        "hard_daily_loss_kill_dollars",
    )
    goal = decimal_value(policy.config["risk"]["profit_goal_dollars"], "profit_goal")
    crossed = prior.profit_goal_crossed or pnl >= goal
    first = prior.first_profit_crossed_at
    if crossed and first is None:
        first = observed_at
    return SessionLatch(
        trading_date=prior.trading_date,
        loss_lock=prior.loss_lock or pnl <= -daily_cap,
        hard_kill=prior.hard_kill or pnl <= -hard_cap,
        profit_goal_crossed=crossed,
        first_profit_crossed_at=first,
        highest_realized_pnl=max(prior.highest_realized_pnl, pnl),
    )


def evaluate_entry(
    *,
    policy: PolicyBundle,
    snapshot: AccountRiskSnapshot,
    plan: ExpiringPlan,
    latch: SessionLatch,
    now: datetime,
) -> RiskDecision:
    failures: list[str] = []
    if now.tzinfo is None or snapshot.observed_at.tzinfo is None:
        raise ValueError("risk times must be timezone-aware")
    if snapshot.account_last4 != policy.account_last4 or plan.account_last4 != policy.account_last4:
        failures.append("ACCOUNT_POLICY_MISMATCH")
    age = (now - snapshot.observed_at).total_seconds()
    if age < 0 or age > int(policy.config["evidence"]["broker_snapshot_max_age_seconds"]):
        failures.append("BROKER_RISK_SNAPSHOT_STALE")
    if not snapshot.account_active or snapshot.restricted:
        failures.append("ACCOUNT_INACTIVE_OR_RESTRICTED")
    if not all(
        (
            snapshot.standard_orders_reconciled,
            snapshot.option_orders_reconciled,
            snapshot.advanced_orders_reconciled,
            snapshot.positions_reconciled,
        )
    ):
        failures.append("WHOLE_BROKER_RECONCILIATION_INCOMPLETE")
    if snapshot.usable_equity <= 0:
        failures.append("USABLE_EQUITY_NOT_POSITIVE")
    if latch.trading_date != now.date():
        failures.append("SESSION_LATCH_DATE_MISMATCH")
    if latch.loss_lock:
        failures.append("IRREVERSIBLE_DAILY_NEW_ENTRY_LOCK")
    if latch.hard_kill:
        failures.append("HARD_DAILY_LOSS_KILL")
    if any(item.category == "unknown" for item in snapshot.exposures):
        failures.append("UNKNOWN_POSSIBLE_EXPOSURE")
    if any(item.category in {"open", "manual"} and not item.protected for item in snapshot.exposures):
        failures.append("UNPROTECTED_OPEN_EXPOSURE")

    existing_planned = sum(
        (item.planned_risk + item.execution_reserve for item in snapshot.exposures), ZERO
    )
    existing_stress = sum((item.stress_risk for item in snapshot.exposures), ZERO)
    existing_notional = sum((item.notional for item in snapshot.exposures), ZERO)
    planned = plan.planned_risk
    reserve = plan.execution_reserve
    stress = plan.stress_risk
    equity = snapshot.usable_equity
    quality_prefix = "a_plus" if plan.quality_tier == "a_plus" else "normal"
    per_trade = _cap(
        policy,
        equity,
        f"{quality_prefix}_planned_risk_pct",
        f"{quality_prefix}_planned_risk_dollars",
    )
    stress_cap = _cap(
        policy,
        equity,
        "max_single_trade_stress_risk_pct",
        "max_single_trade_stress_risk_dollars",
    )
    daily_cap = min(
        decimal_value(policy.config["risk"]["daily_realized_loss_lock_dollars"], "daily_lock"),
        _cap(policy, equity, "daily_new_entry_lock_pct", "daily_new_entry_lock_dollars"),
    )
    portfolio_cap = _cap(
        policy,
        equity,
        "max_total_open_planned_risk_pct",
        "max_total_open_planned_risk_dollars",
    )
    portfolio_stress_cap = _cap(
        policy,
        equity,
        "max_total_open_stress_risk_pct",
        "max_total_open_stress_risk_dollars",
    )
    if planned > per_trade:
        failures.append("PLANNED_RISK_CAP_EXCEEDED")
    if stress > stress_cap:
        failures.append("STRESS_RISK_CAP_EXCEEDED")
    realized_loss = max(ZERO, -snapshot.daily_realized_pnl)
    remaining_daily = daily_cap - realized_loss - existing_planned - planned - reserve
    remaining_portfolio = portfolio_cap - existing_planned - planned - reserve
    remaining_stress = portfolio_stress_cap - existing_stress - stress
    remaining_buying_power = snapshot.unleveraged_buying_power - plan.notional
    if remaining_daily < 0:
        failures.append("INSUFFICIENT_DAILY_RISK_HEADROOM")
    if remaining_portfolio < 0:
        failures.append("INSUFFICIENT_PORTFOLIO_RISK_HEADROOM")
    if remaining_stress < 0:
        failures.append("INSUFFICIENT_PORTFOLIO_STRESS_HEADROOM")
    if remaining_buying_power < 0 or plan.notional > snapshot.cash:
        failures.append("INSUFFICIENT_UNLEVERAGED_FUNDS")
    weekly_cap = _cap(
        policy,
        equity,
        "weekly_loss_lock_pct",
        "weekly_loss_lock_dollars",
    )
    if snapshot.weekly_realized_pnl <= -weekly_cap:
        failures.append("WEEKLY_LOSS_LOCK")
    drawdown_cap = _cap(
        policy,
        equity,
        "live_drawdown_review_pct",
        "live_drawdown_review_dollars",
    )
    if snapshot.peak_equity - snapshot.usable_equity >= drawdown_cap:
        failures.append("LIVE_DRAWDOWN_REVIEW_REQUIRED")
    if latch.profit_goal_crossed:
        floor = decimal_value(policy.config["risk"]["post_goal_floor_dollars"], "profit_floor")
        if snapshot.daily_realized_pnl - existing_planned - planned - reserve < floor:
            failures.append("POST_GOAL_125_FLOOR_NOT_PRESERVED")
    return RiskDecision(
        allowed=not failures,
        failures=tuple(dict.fromkeys(failures)),
        proposal_planned_risk=planned,
        proposal_stress_risk=stress,
        proposal_reserve=reserve,
        remaining_daily_headroom=remaining_daily,
        remaining_portfolio_headroom=remaining_portfolio,
        remaining_stress_headroom=remaining_stress,
        remaining_buying_power=remaining_buying_power,
    )


__all__ = [
    "AccountRiskSnapshot",
    "RiskDecision",
    "RiskExposure",
    "SessionLatch",
    "evaluate_entry",
    "update_session_latch",
]
