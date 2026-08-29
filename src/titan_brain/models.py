"""Typed records shared by Titan's deterministic research and risk core.

The records deliberately contain no broker mutation methods.  They are pure data
objects so the research, equity, and options processes can fail independently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Tuple


class SetupID(str, Enum):
    OPENING_DRIVE_CONTINUATION = "OPENING_DRIVE_CONTINUATION"
    ORB_BREAKOUT = "ORB_BREAKOUT"
    FIRST_PULLBACK = "FIRST_PULLBACK"
    VWAP_RECLAIM = "VWAP_RECLAIM"
    VWAP_CONTINUATION = "VWAP_CONTINUATION"
    CATALYST_CONTINUATION = "CATALYST_CONTINUATION"
    GAP_CONTINUATION = "GAP_CONTINUATION"
    FAILED_BREAKOUT_REVERSAL = "FAILED_BREAKOUT_REVERSAL"
    SECTOR_SYMPATHY = "SECTOR_SYMPATHY"
    POST_EARNINGS_CONTINUATION = "POST_EARNINGS_CONTINUATION"
    OTHER_APPROVED_SETUP = "OTHER_APPROVED_SETUP"


class Session(str, Enum):
    PREMARKET = "premarket"
    REGULAR = "regular_hours"


class Instrument(str, Enum):
    STOCK = "stock"
    LONG_CALL = "long_call"
    LONG_PUT = "long_put"
    DEBIT_SPREAD = "debit_spread"
    NO_TRADE = "no_trade"


@dataclass(frozen=True)
class HardGateEvidence:
    """Safety evidence evaluated independently of any numerical score."""

    fresh_data: bool
    tradable: bool
    sufficient_liquidity: bool
    acceptable_spread: bool
    broker_state_known: bool
    account_eligible: bool
    session_order_valid: bool
    risk_within_limits: bool
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return not self.failures

    @property
    def failures(self) -> Tuple[str, ...]:
        checks = {
            "STALE_DATA": self.fresh_data,
            "NOT_TRADABLE": self.tradable,
            "INSUFFICIENT_LIQUIDITY": self.sufficient_liquidity,
            "EXCESSIVE_SPREAD": self.acceptable_spread,
            "UNKNOWN_BROKER_STATE": self.broker_state_known,
            "ACCOUNT_INELIGIBLE": self.account_eligible,
            "INVALID_SESSION_OR_ORDER_TYPE": self.session_order_valid,
            "RISK_LIMIT_VIOLATION": self.risk_within_limits,
        }
        return tuple(name for name, passed in checks.items() if not passed)


@dataclass(frozen=True)
class RiskRecord:
    planned_risk_dollars: float
    planned_risk_pct: float
    stress_risk_dollars: float
    stress_risk_pct: float
    execution_reserve_dollars: float
    remaining_daily_risk: float
    remaining_portfolio_risk: float
    remaining_portfolio_stress_risk: float
    allowed: bool
    failures: Tuple[str, ...] = ()


@dataclass(frozen=True)
class RouteInput:
    """Conservative inputs for one feasible instrument route.

    ``loss_if_wrong_dollars`` is the modeled loss at tactical invalidation.
    Long-option ``stress_loss_dollars`` is separately required to equal the full
    premium at risk; that prevents a tactical stop estimate from understating
    capital loss.
    """

    instrument: Instrument
    feasible: bool
    setup_score: float
    execution_score: float
    target_probability: float
    loss_probability: float
    target_profit_dollars: float
    loss_if_wrong_dollars: float
    stress_loss_dollars: float
    spread_cost_dollars: float = 0.0
    slippage_cost_dollars: float = 0.0
    fees_dollars: float = 0.0
    theta_cost_dollars: float = 0.0
    iv_change_cost_dollars: float = 0.0
    uncertainty_reserve_dollars: float = 0.0
    premium_paid_dollars: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RouteEvaluation:
    instrument: Instrument
    feasible: bool
    net_expectancy_dollars: float
    net_expectancy_r: float
    stress_loss_dollars: float
    total_costs_dollars: float
    rejection_reasons: Tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RouteDecision:
    selected_instrument: Instrument
    evaluations: Tuple[RouteEvaluation, ...]
    reason: str


@dataclass(frozen=True)
class TradeMetricRecord:
    setup_id: SetupID
    instrument: Instrument
    planned_R: float
    realized_R: float
    MAE_R: float
    MFE_R: float
    exit_efficiency: float
    gross_PnL: float
    net_PnL: float
    spread_cost: float
    slippage_cost: float
    process_grade: float
    setup_score: float
    execution_score: float
    market_regime: str
    option_fields: Mapping[str, float | int | str | None] = field(default_factory=dict)


@dataclass(frozen=True)
class SetupStatistics:
    setup_id: SetupID
    sample_size: int
    win_rate: float
    average_winner_R: float
    average_loser_R: float
    payoff_ratio: float
    net_expectancy_R: float
    profit_factor: float
    max_drawdown_R: float
    recovery_time: int | None
    rolling_expectancy_R: float
    process_adherence: float
