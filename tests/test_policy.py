from types import SimpleNamespace
import unittest

from titan_runtime.policy import evaluate_shadow_candidate, spread_to_risk_gate


def config(**overrides):
    values = dict(
        under5_min_dollar_volume=10_000_000,
        candidate_min_dollar_volume=2_000_000,
        candidate_min_price=1.0,
        candidate_max_price=1000.0,
        candidate_min_gap_pct=4.0,
        watch_min_gap_pct=3.0,
        candidate_min_signal_strength=62.0,
        watch_min_signal_strength=50.0,
        score_is_entry_gate=False,
        fresh_news_required=False,
        state_is_entry_gate=False,
        entry_states=("BUILDING", "ACCELERATING", "BREAKOUT"),
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def signal(**overrides):
    values = dict(
        price=12.0, gap_pct=5.0, dollar_volume=3_000_000,
        signal_strength=20.0, lane="regular_equity", direction="UP",
        catalyst_verified=False, state="FADING", entry_setup_eligible=True,
    )
    values.update(overrides)
    return values


class PolicyTests(unittest.TestCase):
    def test_score_news_and_state_are_ranking_context_by_default(self):
        decision = evaluate_shadow_candidate(signal(), config())
        self.assertTrue(decision.entry_eligible)
        self.assertNotIn("opportunity score below configured hard gate", decision.rejection_reasons)

    def test_score_gate_can_be_restored_without_code_change(self):
        decision = evaluate_shadow_candidate(signal(), config(score_is_entry_gate=True))
        self.assertFalse(decision.entry_eligible)
        self.assertIn("opportunity score below configured hard gate", decision.rejection_reasons)

    def test_exhaustion_lock_remains_a_gate(self):
        decision = evaluate_shadow_candidate(signal(entry_setup_eligible=False), config())
        self.assertFalse(decision.entry_eligible)

    def test_under5_keeps_separate_liquidity_threshold(self):
        decision = evaluate_shadow_candidate(
            signal(price=4.5, lane="under5", dollar_volume=9_999_999), config()
        )
        self.assertFalse(decision.entry_eligible)

    def test_dynamic_spread_to_risk_gate(self):
        passed, ratio = spread_to_risk_gate(
            trigger=10.0, invalidation=9.8, bid=9.98, ask=10.0, max_ratio=0.15
        )
        self.assertTrue(passed)
        self.assertAlmostEqual(ratio, 0.10)
        failed, ratio = spread_to_risk_gate(
            trigger=10.0, invalidation=9.9, bid=9.97, ask=10.0, max_ratio=0.15
        )
        self.assertFalse(failed)
        self.assertAlmostEqual(ratio, 0.30)


if __name__ == "__main__":
    unittest.main()
