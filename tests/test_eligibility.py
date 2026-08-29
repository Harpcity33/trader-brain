from decimal import Decimal
import unittest

from titan_brain.eligibility import (
    EquityEligibilityEvidence,
    evaluate_professional_equity_eligibility,
)


def evidence(**overrides):
    values = {
        "ticker": "TEST",
        "price": "5.01",
        "session_volume_shares": 750_000,
        "robinhood_tradable": True,
        "quote_fresh": True,
        "bid": "5.00",
        "ask": "5.02",
        "fresh_news_available": False,
    }
    values.update(overrides)
    return EquityEligibilityEvidence(**values)


class EligibilityTests(unittest.TestCase):
    def test_exactly_five_dollars_fails(self):
        result = evaluate_professional_equity_eligibility(evidence(price="5.00"))
        self.assertFalse(result.eligible)
        self.assertIn("PRICE_NOT_STRICTLY_ABOVE_5", result.hard_gate_failures)

    def test_just_above_five_dollars_passes(self):
        result = evaluate_professional_equity_eligibility(evidence(price="5.0001"))
        self.assertTrue(result.eligible)

    def test_749999_volume_fails_and_750000_passes(self):
        low = evaluate_professional_equity_eligibility(
            evidence(session_volume_shares=749_999)
        )
        boundary = evaluate_professional_equity_eligibility(
            evidence(session_volume_shares=750_000)
        )
        self.assertFalse(low.eligible)
        self.assertTrue(boundary.eligible)

    def test_news_is_not_a_hard_gate(self):
        no_news = evaluate_professional_equity_eligibility(
            evidence(fresh_news_available=False)
        )
        missing_news = evaluate_professional_equity_eligibility(
            evidence(fresh_news_available=None)
        )
        self.assertTrue(no_news.eligible)
        self.assertTrue(missing_news.eligible)

    def test_missing_tradability_or_stale_quote_fails_closed(self):
        result = evaluate_professional_equity_eligibility(
            evidence(robinhood_tradable=None, quote_fresh=False)
        )
        self.assertEqual(
            result.hard_gate_failures,
            ("TRADABILITY_UNAVAILABLE", "QUOTE_NOT_FRESH"),
        )

    def test_spread_is_context_not_an_implicit_threshold(self):
        result = evaluate_professional_equity_eligibility(evidence())
        self.assertEqual(result.spread_dollars, Decimal("0.02"))
        self.assertGreater(result.spread_pct, Decimal("0"))


if __name__ == "__main__":
    unittest.main()

