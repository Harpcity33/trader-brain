from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from titan_brain.metrics import (  # noqa: E402
    REQUIRED_OPTION_FIELDS,
    aggregate_all_setups,
    aggregate_setup_metrics,
    build_trade_metrics,
)
from titan_brain.models import Instrument, SetupID  # noqa: E402


def trade(
    net: float,
    *,
    setup: SetupID = SetupID.FIRST_PULLBACK,
    process_grade: float = 90,
):
    return build_trade_metrics(
        setup_id=setup,
        instrument=Instrument.STOCK,
        planned_risk_dollars=100,
        planned_reward_dollars=200,
        gross_pnl=net + 5,
        net_pnl=net,
        mae_loss_dollars=50,
        mfe_profit_dollars=200,
        spread_cost=2,
        slippage_cost=3,
        process_grade=process_grade,
        setup_score=80,
        execution_score=75,
        market_regime="risk_on",
    )


class MetricsTests(unittest.TestCase):
    def test_trade_record_tracks_r_and_execution_costs(self) -> None:
        record = trade(100)
        self.assertEqual(record.planned_R, 2.0)
        self.assertEqual(record.realized_R, 1.0)
        self.assertEqual(record.MAE_R, -0.5)
        self.assertEqual(record.MFE_R, 2.0)
        self.assertEqual(record.exit_efficiency, 0.5)
        self.assertEqual(record.spread_cost + record.slippage_cost, 5.0)

    def test_options_require_complete_option_metric_shape(self) -> None:
        with self.assertRaises(ValueError):
            build_trade_metrics(
                setup_id=SetupID.VWAP_RECLAIM,
                instrument=Instrument.LONG_CALL,
                planned_risk_dollars=100,
                planned_reward_dollars=200,
                gross_pnl=20,
                net_pnl=15,
                mae_loss_dollars=40,
                mfe_profit_dollars=60,
                spread_cost=3,
                slippage_cost=2,
                process_grade=90,
                setup_score=80,
                execution_score=70,
                market_regime="range",
                option_fields={},
            )
        fields = {name: None for name in REQUIRED_OPTION_FIELDS}
        record = build_trade_metrics(
            setup_id=SetupID.VWAP_RECLAIM,
            instrument=Instrument.LONG_CALL,
            planned_risk_dollars=100,
            planned_reward_dollars=200,
            gross_pnl=20,
            net_pnl=15,
            mae_loss_dollars=40,
            mfe_profit_dollars=60,
            spread_cost=3,
            slippage_cost=2,
            process_grade=90,
            setup_score=80,
            execution_score=70,
            market_regime="range",
            option_fields=fields,
        )
        self.assertEqual(set(record.option_fields), REQUIRED_OPTION_FIELDS)

    def test_setup_statistics_prioritize_net_expectancy_and_drawdown(self) -> None:
        records = [trade(100), trade(-50), trade(200), trade(-100, process_grade=50)]
        stats = aggregate_setup_metrics(
            records,
            setup_id=SetupID.FIRST_PULLBACK,
            rolling_window=2,
        )
        self.assertEqual(stats.sample_size, 4)
        self.assertEqual(stats.win_rate, 0.5)
        self.assertEqual(stats.average_winner_R, 1.5)
        self.assertEqual(stats.average_loser_R, -0.75)
        self.assertEqual(stats.payoff_ratio, 2.0)
        self.assertEqual(stats.net_expectancy_R, 0.375)
        self.assertEqual(stats.profit_factor, 2.0)
        self.assertEqual(stats.max_drawdown_R, 1.0)
        self.assertEqual(stats.rolling_expectancy_R, 0.5)
        self.assertEqual(stats.process_adherence, 0.75)

    def test_statistics_stay_partitioned_by_setup(self) -> None:
        records = [
            trade(100, setup=SetupID.FIRST_PULLBACK),
            trade(-100, setup=SetupID.VWAP_RECLAIM),
        ]
        grouped = aggregate_all_setups(records)
        self.assertEqual(grouped[SetupID.FIRST_PULLBACK].net_expectancy_R, 1.0)
        self.assertEqual(grouped[SetupID.VWAP_RECLAIM].net_expectancy_R, -1.0)
        with self.assertRaises(ValueError):
            aggregate_setup_metrics(records, setup_id=SetupID.FIRST_PULLBACK)


if __name__ == "__main__":
    unittest.main()
