import unittest

from trader_brain import Candidate, WEIGHTS, eligibility, score, screen_and_rank


def candidate(ticker="TEST", price=5.01, volume=750_000, news=False, value=0.5):
    return Candidate(ticker, price, volume, {k: value for k in WEIGHTS}, news)


class ValidationTests(unittest.TestCase):
    def test_weights_sum_to_100(self):
        self.assertEqual(sum(WEIGHTS.values()), 100)

    def test_price_exactly_five_fails(self):
        self.assertFalse(eligibility(candidate(price=5.00))[0])

    def test_price_just_above_five_passes(self):
        self.assertTrue(eligibility(candidate(price=5.01))[0])

    def test_volume_749999_fails(self):
        self.assertFalse(eligibility(candidate(volume=749_999))[0])

    def test_volume_750000_passes(self):
        self.assertTrue(eligibility(candidate(volume=750_000))[0])

    def test_no_news_is_not_a_gate(self):
        self.assertTrue(score(candidate(news=False))["eligible"])

    def test_score_is_bounded(self):
        self.assertEqual(score(candidate(value=-1))["score"], 0)
        self.assertEqual(score(candidate(value=2))["score"], 100)

    def test_deterministic_tie_break(self):
        ranked = screen_and_rank([candidate("ZZZ"), candidate("AAA")])["accepted"]
        self.assertEqual([x["ticker"] for x in ranked], ["AAA", "ZZZ"])


if __name__ == "__main__":
    unittest.main()
