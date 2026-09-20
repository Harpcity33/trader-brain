"""Synthetic, offline checks. No real account values or broker access."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from titan_brain.option_trade_analysis import analyze_option_trade


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 5, 16, 0, 5, tzinfo=timezone.utc)


def example():
    return json.loads((ROOT / "examples/attended_option_analysis.json").read_text())


class OptionTradeAnalysisTests(unittest.TestCase):
    def evaluate(self, data=None, now=NOW):
        return analyze_option_trade(example() if data is None else data, now=now)

    def assert_blocked(self, data, now=NOW):
        result = self.evaluate(data, now)
        self.assertFalse(result["analysis_complete"])
        self.assertIsNone(result["maximum_quantity_under_supplied_limits"])
        self.assertFalse(result["live_authority"])
        self.assertFalse(result["executable"])
        self.assertTrue(result["reasons"])

    def test_example_is_analysis_never_execution_authority(self):
        result = self.evaluate()
        self.assertTrue(result["analysis_complete"])
        self.assertTrue(result["analysis_only"])
        self.assertFalse(result["live_authority"])
        self.assertFalse(result["executable"])
        self.assertTrue(result["requested_quantity_fits_limits"])
        self.assertEqual(result["maximum_quantity_under_supplied_limits"], 2)
        self.assertTrue(result["limitations"])

    def test_repeatable_and_does_not_mutate_input(self):
        data = example()
        before = deepcopy(data)
        self.assertEqual(self.evaluate(data), self.evaluate(data))
        self.assertEqual(data, before)

    def test_three_loss_amounts_and_account_percentages_are_distinct(self):
        result = self.evaluate()
        figures = result["figures"]
        self.assertEqual(figures["purchase_premium"], "200")
        self.assertEqual(figures["planned_loss"], "52")
        self.assertEqual(figures["stress_loss"], "152")
        self.assertEqual(figures["premium_exposure"], "202")
        self.assertEqual(figures["planned_loss_equity_percent"], "5.2")
        self.assertEqual(result["scenario_results"][0]["premium_decline_percent"], "25")
        self.assertEqual(result["scenario_results"][2]["net_pnl"], "98")

    def test_worse_supplied_exit_changes_risk_not_a_fixed_percentage(self):
        data = example()
        data["scenarios"][0]["exit_bid"] = "0.70"
        result = self.evaluate(data)
        self.assertEqual(result["figures"]["planned_loss"], "132")
        self.assertEqual(result["figures"]["premium_exposure"], "202")
        self.assertEqual(result["maximum_quantity_under_supplied_limits"], 1)

    def test_zero_recovery_includes_full_premium_and_fees(self):
        data = example()
        data["scenarios"][1]["exit_bid"] = "0"
        result = self.evaluate(data)
        self.assertEqual(result["figures"]["stress_loss"], "202")
        self.assertEqual(result["maximum_quantity_under_supplied_limits"], 1)

    def test_fee_offset_case_does_not_break_maximum_search(self):
        data = example()
        data["fees"]["entry_per_order"] = "90"
        data["fees"]["exit_per_order"] = "10"
        data["budgets"]["planned_loss"] = "0"
        data["budgets"]["stress_loss"] = "0"
        data["budgets"]["premium_exposure"] = "900"
        for scenario in data["scenarios"]:
            scenario["exit_bid"] = "2.50"
        result = self.evaluate(data)
        self.assertEqual(result["maximum_quantity_under_supplied_limits"], 4)
        self.assertFalse(result["requested_quantity_fits_limits"])
        # A maximum is not a recommended quantity, nor proof every smaller size fits.
        self.assertFalse(result["executable"])

    def test_supported_quantity_cap_blocks_unbounded_request(self):
        data = example()
        data["trade"]["quantity"] = 100001
        self.assert_blocked(data)

    def test_exact_freshness_boundaries(self):
        data = example()
        data["quote"]["observed_at"] = (NOW - timedelta(seconds=30)).isoformat()
        data["account"]["observed_at"] = (NOW - timedelta(seconds=60)).isoformat()
        self.assertTrue(self.evaluate(data)["analysis_complete"])

    def test_expiry_window_uses_new_york_calendar_day(self):
        data = example()
        now = datetime(2026, 10, 6, 0, 30, tzinfo=timezone.utc)
        for section in ("account", "quote"):
            data[section]["observed_at"] = now.isoformat()
        data["contract"]["expiry"] = "2026-10-15"
        self.assertEqual(self.evaluate(data, now)["contract"]["calendar_dte"], 10)

    def test_requested_quantity_over_limits_is_not_approved(self):
        data = example()
        data["trade"]["quantity"] = 3
        result = self.evaluate(data)
        self.assertTrue(result["analysis_complete"])
        self.assertFalse(result["requested_quantity_fits_limits"])
        self.assertEqual(result["maximum_quantity_under_supplied_limits"], 2)

    def test_no_fixed_ten_percent_premium_cap(self):
        # One contract exposes more than ten percent of synthetic account equity.
        result = self.evaluate()
        self.assertTrue(result["requested_quantity_fits_limits"])

    def test_cash_cap_includes_fee_minimum(self):
        data = example()
        for key in ("cash", "settled_cash", "available_funds"):
            data["account"][key] = "400"
        data["account"]["pending_debits_not_in_balances"] = "0"
        result = self.evaluate(data)
        self.assertEqual(result["maximum_quantity_under_supplied_limits"], 1)

    def test_fee_minimum_is_per_order_not_per_contract(self):
        data = example()
        for key in ("cash", "settled_cash", "available_funds"):
            data["account"][key] = "402"
        data["account"]["pending_debits_not_in_balances"] = "0"
        self.assertEqual(self.evaluate(data)["maximum_quantity_under_supplied_limits"], 2)

    def test_per_contract_fees_change_whole_contract_capacity(self):
        data = example()
        data["fees"]["entry_per_contract"] = "100"
        self.assertEqual(self.evaluate(data)["maximum_quantity_under_supplied_limits"], 0)

    def test_pending_debits_are_not_spendable(self):
        data = example()
        data["account"]["pending_debits_not_in_balances"] = "850"
        result = self.evaluate(data)
        self.assertEqual(result["maximum_quantity_under_supplied_limits"], 0)
        self.assertFalse(result["requested_quantity_fits_limits"])

    def test_zero_modeled_loss_does_not_create_unlimited_capacity(self):
        data = example()
        for scenario in data["scenarios"]:
            scenario["exit_bid"] = "4"
        result = self.evaluate(data)
        self.assertEqual(result["maximum_quantity_under_supplied_limits"], 2)

    def test_multiplier_used_in_sizing(self):
        data = example()
        data["contract"]["multiplier"] = 200
        self.assertEqual(self.evaluate(data)["maximum_quantity_under_supplied_limits"], 1)

    def test_put_uses_same_premium_arithmetic(self):
        data = example()
        data["contract"]["right"] = "PUT"
        self.assertEqual(self.evaluate(data)["maximum_quantity_under_supplied_limits"], 2)

    def test_unknown_settlement_blocks_sizing(self):
        data = example()
        data["account"]["settled_cash"] = None
        self.assert_blocked(data)

    def test_unknown_commitments_block_sizing(self):
        data = example()
        data["account"]["pending_debits_not_in_balances"] = None
        self.assert_blocked(data)

    def test_stale_quote_blocks(self):
        self.assert_blocked(example(), NOW + timedelta(seconds=26))

    def test_stale_account_blocks_even_with_fresh_quote(self):
        data = example()
        data["quote"]["observed_at"] = (NOW + timedelta(seconds=60)).isoformat()
        self.assert_blocked(data, NOW + timedelta(seconds=60))

    def test_future_observations_block(self):
        for section in ("account", "quote"):
            with self.subTest(section=section):
                data = example()
                data[section]["observed_at"] = (NOW + timedelta(seconds=1)).isoformat()
                self.assert_blocked(data)

    def test_naive_clock_or_observation_blocks(self):
        self.assert_blocked(example(), NOW.replace(tzinfo=None))
        data = example()
        data["account"]["observed_at"] = "2026-10-05T16:00:00"
        self.assert_blocked(data)

    def test_zero_day_and_over_ten_day_expiries_block(self):
        for expiry in ("2026-10-05", "2026-10-16"):
            with self.subTest(expiry=expiry):
                data = example()
                data["contract"]["expiry"] = expiry
                self.assert_blocked(data)

    def test_ten_calendar_days_included(self):
        data = example()
        data["contract"]["expiry"] = "2026-10-15"
        self.assertTrue(self.evaluate(data)["analysis_complete"])

    def test_exit_on_expiry_date_not_supported(self):
        data = example()
        data["trade"]["latest_exit_at"] = "2026-10-09T14:00:00+00:00"
        self.assert_blocked(data)

    def test_expired_exit_plan_blocks(self):
        data = example()
        data["trade"]["latest_exit_at"] = NOW.isoformat()
        self.assert_blocked(data)

    def test_crossed_quote_blocks(self):
        data = example()
        data["quote"]["bid"] = "2.01"
        self.assert_blocked(data)

    def test_non_usd_account_or_contract_blocks(self):
        for section in ("account", "contract"):
            data = example()
            data[section]["currency"] = "EUR"
            self.assert_blocked(data)

    def test_unsafe_numeric_values_block(self):
        for value in (True, 1.5, "NaN", "Infinity", "-1", "bad"):
            with self.subTest(value=value):
                data = example()
                data["trade"]["entry_limit"] = value
                self.assert_blocked(data)

    def test_whole_positive_contracts_only(self):
        for value in (True, 0, -1, 1.5, "2"):
            with self.subTest(value=value):
                data = example()
                data["trade"]["quantity"] = value
                self.assert_blocked(data)

    def test_missing_or_duplicate_scenarios_block(self):
        data = example()
        data["scenarios"] = [data["scenarios"][0]]
        self.assert_blocked(data)
        data = example()
        data["scenarios"][1]["name"] = data["scenarios"][0]["name"]
        self.assert_blocked(data)

    def test_missing_thesis_or_assumptions_blocks(self):
        data = example()
        data["trade"]["thesis"] = "  "
        self.assert_blocked(data)
        data = example()
        data["scenarios"][0]["assumptions"] = ""
        self.assert_blocked(data)

    def test_malformed_top_level_blocks(self):
        for data in ([], "secret-not-for-echo", {}, None):
            with self.subTest(data=data):
                result = analyze_option_trade(data, now=NOW)
                self.assertFalse(result["analysis_complete"])
                self.assertNotIn("secret-not-for-echo", json.dumps(result))

    def test_cli_synthetic_simulation(self):
        run = subprocess.run(
            [sys.executable, "-I", "-S", "-B", str(ROOT / "scripts/titan-option-analysis"),
             str(ROOT / "examples/attended_option_analysis.json"), "--now", NOW.isoformat()],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        report = json.loads(run.stdout)
        self.assertEqual(report["clock_mode"], "simulation")
        self.assertFalse(report["executable"])

    def test_cli_malformed_json_fails_without_echoing_input(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.json"
            path.write_text("private-invalid-input")
            run = subprocess.run(
                [sys.executable, "-I", "-S", "-B", str(ROOT / "scripts/titan-option-analysis"), str(path)],
                capture_output=True, text=True, timeout=10,
            )
        self.assertEqual(run.returncode, 2)
        self.assertFalse(json.loads(run.stdout)["analysis_complete"])
        self.assertNotIn("private-invalid-input", run.stdout + run.stderr)

    def test_cli_duplicate_fields_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.json"
            data = json.dumps(example()).replace('"planned_loss": "150"', '"planned_loss": "150", "planned_loss": "999"')
            path.write_text(data)
            run = subprocess.run(
                [sys.executable, "-I", "-S", "-B", str(ROOT / "scripts/titan-option-analysis"), str(path),
                 "--now", NOW.isoformat()],
                capture_output=True, text=True, timeout=10,
            )
        self.assertEqual(run.returncode, 2)
        self.assertFalse(json.loads(run.stdout)["analysis_complete"])


if __name__ == "__main__":
    unittest.main()
