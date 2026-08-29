"""R-based trade records and setup-level performance statistics."""

from __future__ import annotations

from collections import defaultdict
from statistics import fmean
from typing import Iterable, Mapping

from .models import Instrument, SetupID, SetupStatistics, TradeMetricRecord


REQUIRED_OPTION_FIELDS = frozenset(
    {
        "DTE",
        "strike",
        "delta",
        "gamma",
        "theta",
        "vega",
        "entry_IV",
        "exit_IV",
        "entry_bid",
        "entry_ask",
        "entry_mid",
        "actual_fill",
        "spread_pct",
        "underlying_return",
        "option_return",
        "implied_move",
        "realized_move",
    }
)


def _score(name: str, value: float) -> float:
    value = float(value)
    if not 0 <= value <= 100:
        raise ValueError(f"{name} must be in [0, 100]")
    return value


def build_trade_metrics(
    *,
    setup_id: SetupID | str,
    instrument: Instrument,
    planned_risk_dollars: float,
    planned_reward_dollars: float,
    gross_pnl: float,
    net_pnl: float,
    mae_loss_dollars: float,
    mfe_profit_dollars: float,
    spread_cost: float,
    slippage_cost: float,
    process_grade: float,
    setup_score: float,
    execution_score: float,
    market_regime: str,
    option_fields: Mapping[str, float | int | str | None] | None = None,
) -> TradeMetricRecord:
    """Normalize one immutable trade result around its predeclared risk unit."""

    try:
        normalized_setup = (
            setup_id if isinstance(setup_id, SetupID) else SetupID(str(setup_id))
        )
    except ValueError as exc:
        raise ValueError("unknown setup_id") from exc
    planned_risk = float(planned_risk_dollars)
    if planned_risk <= 0:
        raise ValueError("planned_risk_dollars must be positive")
    planned_reward = float(planned_reward_dollars)
    mae = float(mae_loss_dollars)
    mfe = float(mfe_profit_dollars)
    spread = float(spread_cost)
    slippage = float(slippage_cost)
    for name, value in (
        ("planned_reward_dollars", planned_reward),
        ("mae_loss_dollars", mae),
        ("mfe_profit_dollars", mfe),
        ("spread_cost", spread),
        ("slippage_cost", slippage),
    ):
        if value < 0:
            raise ValueError(f"{name} cannot be negative")
    if not str(market_regime).strip():
        raise ValueError("market_regime is required")

    normalized_option_fields = dict(option_fields or {})
    if instrument in (
        Instrument.LONG_CALL,
        Instrument.LONG_PUT,
        Instrument.DEBIT_SPREAD,
    ):
        missing = REQUIRED_OPTION_FIELDS.difference(normalized_option_fields)
        if missing:
            raise ValueError(
                "option metrics missing fields: " + ", ".join(sorted(missing))
            )

    net = float(net_pnl)
    exit_efficiency = 0.0 if mfe == 0 else max(-1.0, min(1.0, net / mfe))
    return TradeMetricRecord(
        setup_id=normalized_setup,
        instrument=instrument,
        planned_R=planned_reward / planned_risk,
        realized_R=net / planned_risk,
        MAE_R=-mae / planned_risk,
        MFE_R=mfe / planned_risk,
        exit_efficiency=exit_efficiency,
        gross_PnL=float(gross_pnl),
        net_PnL=net,
        spread_cost=spread,
        slippage_cost=slippage,
        process_grade=_score("process_grade", process_grade),
        setup_score=_score("setup_score", setup_score),
        execution_score=_score("execution_score", execution_score),
        market_regime=str(market_regime),
        option_fields=normalized_option_fields,
    )


def _drawdown_and_recovery(realized_rs: list[float]) -> tuple[float, int | None]:
    if not realized_rs:
        return 0.0, 0
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    max_drawdown_peak = 0.0
    trough_index = -1
    curve: list[float] = []
    for index, result in enumerate(realized_rs):
        cumulative += result
        curve.append(cumulative)
        if cumulative > peak:
            peak = cumulative
        drawdown = peak - cumulative
        if drawdown > max_drawdown:
            max_drawdown = drawdown
            max_drawdown_peak = peak
            trough_index = index
    if max_drawdown == 0:
        return 0.0, 0
    for index in range(trough_index + 1, len(curve)):
        if curve[index] >= max_drawdown_peak - 1e-12:
            return max_drawdown, index - trough_index
    return max_drawdown, None


def aggregate_setup_metrics(
    trades: Iterable[TradeMetricRecord],
    *,
    setup_id: SetupID | str,
    rolling_window: int = 20,
    process_grade_threshold: float = 80.0,
) -> SetupStatistics:
    """Calculate net-expectancy and survivability metrics for one setup."""

    normalized_setup = (
        setup_id if isinstance(setup_id, SetupID) else SetupID(str(setup_id))
    )
    if rolling_window <= 0:
        raise ValueError("rolling_window must be positive")
    threshold = _score("process_grade_threshold", process_grade_threshold)
    records = list(trades)
    if any(record.setup_id is not normalized_setup for record in records):
        raise ValueError("aggregate_setup_metrics cannot mix setup IDs")
    results = [record.realized_R for record in records]
    winners = [value for value in results if value > 0]
    losers = [value for value in results if value < 0]
    sample_size = len(results)
    average_winner = fmean(winners) if winners else 0.0
    average_loser = fmean(losers) if losers else 0.0
    payoff_ratio = (
        average_winner / abs(average_loser)
        if losers
        else (float("inf") if winners else 0.0)
    )
    gross_wins = sum(winners)
    gross_losses = abs(sum(losers))
    profit_factor = (
        gross_wins / gross_losses
        if gross_losses
        else (float("inf") if gross_wins else 0.0)
    )
    max_drawdown, recovery_time = _drawdown_and_recovery(results)
    rolling_values = results[-rolling_window:]
    return SetupStatistics(
        setup_id=normalized_setup,
        sample_size=sample_size,
        win_rate=(len(winners) / sample_size) if sample_size else 0.0,
        average_winner_R=average_winner,
        average_loser_R=average_loser,
        payoff_ratio=payoff_ratio,
        net_expectancy_R=fmean(results) if results else 0.0,
        profit_factor=profit_factor,
        max_drawdown_R=max_drawdown,
        recovery_time=recovery_time,
        rolling_expectancy_R=fmean(rolling_values) if rolling_values else 0.0,
        process_adherence=(
            sum(record.process_grade >= threshold for record in records) / sample_size
            if sample_size
            else 0.0
        ),
    )


def aggregate_all_setups(
    trades: Iterable[TradeMetricRecord],
    *,
    rolling_window: int = 20,
    process_grade_threshold: float = 80.0,
) -> Mapping[SetupID, SetupStatistics]:
    """Partition metrics by setup ID so strategy populations never commingle."""

    grouped: dict[SetupID, list[TradeMetricRecord]] = defaultdict(list)
    for trade in trades:
        grouped[trade.setup_id].append(trade)
    return {
        setup_id: aggregate_setup_metrics(
            records,
            setup_id=setup_id,
            rolling_window=rolling_window,
            process_grade_threshold=process_grade_threshold,
        )
        for setup_id, records in grouped.items()
    }
