"""Tests for the versioned options risk policy (milestone 5)."""

from __future__ import annotations

import unittest

from titan_brain.option_risk_policy import (
    CorrelatedExposure,
    OptionsPolicyError,
    OptionsRiskPolicy,
    POLICY_VERSION,
    assess_options_trade_risk,
)


def _policy(**overrides):
    fields = dict(
        version=POLICY_VERSION,
        max_planned_loss="500",
        max_stress_loss="1000",
        max_premium_exposure="2000",
        account_equity_ceiling_fraction="0.5",
    )
    fields.update(overrides)
    return OptionsRiskPolicy(**fields)


def _assess(**overrides):
    fields = dict(
        policy=_policy(),
        underlying_symbol="TEST",
        planned_loss="200",
        stress_loss="600",
        premium_exposure="1500",
        account_equity="10000",
        correlated=(),
    )
    fields.update(overrides)
    return assess_options_trade_risk(**fields)


class OptionsRiskPolicyTests(unittest.TestCase):
    def test_clean_trade_within_budgets(self) -> None:
        r = _assess()
        self.assertTrue(r.ok)
        self.assertEqual(r.blockers, ())
        self.assertEqual(r.figures["policy_version"], POLICY_VERSION)

    def test_percent_framings_of_premium_and_equity(self) -> None:
        r = _assess(planned_loss="150", premium_exposure="1500", account_equity="10000")
        # 150/1500 = 10% of premium ; 150/10000 = 1.5% of equity
        self.assertEqual(r.figures["planned_loss_pct_of_premium"], "10")
        self.assertEqual(r.figures["planned_loss_pct_of_equity"], "1.5")

    def test_planned_budget_breach_blocks(self) -> None:
        r = _assess(planned_loss="600")  # > 500 budget
        self.assertFalse(r.ok)
        self.assertIn("PLANNED_LOSS_EXCEEDS_BUDGET", r.blockers)

    def test_stress_budget_breach_blocks(self) -> None:
        r = _assess(stress_loss="1200")  # > 1000
        self.assertIn("STRESS_LOSS_EXCEEDS_BUDGET", r.blockers)

    def test_premium_budget_breach_blocks(self) -> None:
        r = _assess(premium_exposure="2500")  # > 2000 ; also > equity ceiling? 0.5*10000=5000, no
        self.assertIn("PREMIUM_EXPOSURE_EXCEEDS_BUDGET", r.blockers)

    def test_equity_ceiling_breach_blocks(self) -> None:
        # premium 1500 under budget 2000, but equity 2000 * 0.5 = 1000 ceiling < 1500
        r = _assess(premium_exposure="1500", account_equity="2000")
        self.assertIn("PREMIUM_EXPOSURE_EXCEEDS_EQUITY_CEILING", r.blockers)

    def test_zero_equity_cannot_support_exposure(self) -> None:
        r = _assess(account_equity="0", premium_exposure="1")
        self.assertIn("PREMIUM_EXPOSURE_EXCEEDS_EQUITY_CEILING", r.blockers)
        self.assertIsNone(r.figures["premium_pct_of_equity"])  # None denominator

    def test_correlated_same_underlying_aggregates(self) -> None:
        # this trade stress 600 + accepted 500 on TEST = 1100 > 1000 stress budget
        r = _assess(
            stress_loss="600",
            correlated=(CorrelatedExposure(underlying_symbol="TEST", accepted_stress_loss="500"),),
        )
        self.assertIn("CORRELATED_AGGREGATE_STRESS_EXCEEDS_BUDGET", r.blockers)
        self.assertEqual(r.figures["aggregate_correlated_stress_loss"], "1100")

    def test_correlated_other_underlying_does_not_aggregate(self) -> None:
        r = _assess(
            stress_loss="600",
            correlated=(CorrelatedExposure(underlying_symbol="OTHER", accepted_stress_loss="500"),),
        )
        self.assertNotIn("CORRELATED_AGGREGATE_STRESS_EXCEEDS_BUDGET", r.blockers)
        self.assertEqual(r.figures["aggregate_correlated_stress_loss"], "600")

    def test_unknown_policy_version_rejected(self) -> None:
        with self.assertRaisesRegex(OptionsPolicyError, "version"):
            _policy(version="something_else")

    def test_equity_fraction_bounds(self) -> None:
        with self.assertRaises(OptionsPolicyError):
            _policy(account_equity_ceiling_fraction="0")
        with self.assertRaises(OptionsPolicyError):
            _policy(account_equity_ceiling_fraction="1.5")

    def test_float_money_rejected(self) -> None:
        with self.assertRaises(OptionsPolicyError):
            _assess(planned_loss=200.0)

    def test_negative_loss_rejected(self) -> None:
        with self.assertRaises(OptionsPolicyError):
            _assess(stress_loss="-1")

    def test_disclaimer_present(self) -> None:
        r = _assess()
        self.assertIn("not a", r.figures["disclaimer"])
        self.assertIn("borrowed", r.figures["disclaimer"])

    def test_multiple_breaches_accumulate(self) -> None:
        r = _assess(planned_loss="600", stress_loss="1200", premium_exposure="2500")
        self.assertIn("PLANNED_LOSS_EXCEEDS_BUDGET", r.blockers)
        self.assertIn("STRESS_LOSS_EXCEEDS_BUDGET", r.blockers)
        self.assertIn("PREMIUM_EXPOSURE_EXCEEDS_BUDGET", r.blockers)
        self.assertEqual(len(r.blockers), len(set(r.blockers)))


if __name__ == "__main__":
    unittest.main()
