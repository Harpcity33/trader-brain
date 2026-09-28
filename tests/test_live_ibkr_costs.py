from __future__ import annotations

from decimal import Decimal, localcontext
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from titan_brain.live.broker.ibkr_costs import (  # noqa: E402
    UnverifiedCommissionCap,
    estimate_us_smart_tier1_base_commission as estimate,
    estimate_us_smart_tier1_base_commission_total as total,
)
from titan_brain.live.money import NumericPolicyError  # noqa: E402


class IbkrBaseCommissionEstimateTests(unittest.TestCase):
    def test_minimum_is_per_order_not_flat_fee_at_all_sizes(self):
        self.assertEqual(estimate(10, "10"), Decimal("0.35"))
        self.assertEqual(estimate(100, "10"), Decimal("0.35"))
        self.assertEqual(estimate(101, "10"), Decimal("0.3535"))
        self.assertEqual(estimate(500, "10"), Decimal("1.75"))
        self.assertEqual(estimate(1000, "10"), Decimal("3.50"))

    def test_decimal_and_integral_text_inputs_are_exact(self):
        self.assertEqual(estimate("100.0", Decimal("10.25")), Decimal("0.35"))
        self.assertEqual(estimate(Decimal("5E2"), 10), Decimal("1.75"))

    def test_separate_entries_and_exits_have_separate_minimums(self):
        self.assertEqual(total([(100, "10"), (100, "10.10")]), Decimal("0.70"))
        self.assertEqual(
            total([(100, "10"), (50, "10.10"), (50, "10.20")]), Decimal("1.05")
        )
        self.assertEqual(total([(500, "10"), (500, "10.10")]), Decimal("3.50"))

    def test_total_supports_generator_and_empty_input(self):
        self.assertEqual(total((item for item in [(100, "10")] * 4)), Decimal("1.40"))
        self.assertEqual(total([]), Decimal("0"))

    def test_cap_discrepancy_fails_closed_including_above_five_dollars(self):
        for quantity, price in [(1, "6"), (10, "0.20"), (1, "69.99")]:
            with self.subTest(quantity=quantity, price=price):
                with self.assertRaisesRegex(UnverifiedCommissionCap, "billing clarification"):
                    estimate(quantity, price)

    def test_low_price_is_accepted_only_when_cap_readings_agree(self):
        self.assertEqual(estimate(100, "0.70"), Decimal("0.35"))
        self.assertEqual(estimate(1, "70"), Decimal("0.35"))

    def test_quantity_rejects_fractional_nonpositive_nonfinite_and_float(self):
        for invalid in [True, False, None, 100.0, 0, -1, "0.5", "NaN", "Infinity", "x", {}, "1E999999"]:
            with self.subTest(value=invalid):
                with self.assertRaises(NumericPolicyError):
                    estimate(invalid, "10")

    def test_price_rejects_nonpositive_nonfinite_and_float(self):
        for invalid in [True, False, None, 10.0, 0, -1, "0", "-0", "NaN", "sNaN", "Infinity", "x", {}]:
            with self.subTest(value=invalid):
                with self.assertRaises(NumericPolicyError):
                    estimate(100, invalid)

    def test_first_monthly_tier_is_explicit_and_boundary_is_inclusive(self):
        self.assertEqual(estimate(100, "10", monthly_volume_before=299_900), Decimal("0.35"))
        self.assertEqual(estimate(300_000, "10"), Decimal("1050"))
        for quantity, before in [(101, 299_900), (1, 300_000), (300_001, 0)]:
            with self.subTest(quantity=quantity, before=before):
                with self.assertRaises(NumericPolicyError):
                    estimate(quantity, "10", monthly_volume_before=before)

    def test_monthly_volume_rejects_ambiguous_inputs_even_without_orders(self):
        for invalid in [True, False, None, 0.0, -1, "0.5", "NaN", "Infinity", 300_001]:
            with self.subTest(value=invalid):
                with self.assertRaises(NumericPolicyError):
                    total([], monthly_volume_before=invalid)

    def test_total_tracks_cumulative_volume_and_does_not_return_partial_result(self):
        self.assertEqual(
            total([(100, "10"), (100, "10")], monthly_volume_before=299_800), Decimal("0.70")
        )
        with self.assertRaisesRegex(NumericPolicyError, "monthly volume tier"):
            total([(100, "10"), (100, "10")], monthly_volume_before=299_801)
        with self.assertRaises(UnverifiedCommissionCap):
            total([(100, "10"), (1, "6")])

    def test_no_cent_rounding_or_caller_decimal_context_leakage(self):
        with localcontext() as context:
            context.prec = 2
            self.assertEqual(estimate(101, "10.12345678901234567890"), Decimal("0.3535"))
            self.assertEqual(total([(101, "10"), (101, "10")]), Decimal("0.7070"))
            self.assertEqual(context.prec, 2)


if __name__ == "__main__":
    unittest.main()
