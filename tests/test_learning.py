import unittest

from titan_brain.learning import (
    assess_promotion_candidate,
    maturity_stage,
    time_ordered_walk_forward_splits,
)


class LearningTests(unittest.TestCase):
    def test_maturity_boundaries(self):
        self.assertEqual(maturity_stage(29), "EXPLORATORY")
        self.assertEqual(maturity_stage(30), "DEVELOPING")
        self.assertEqual(maturity_stage(100), "PROVISIONAL_EVIDENCE")
        self.assertEqual(maturity_stage(200), "PROMOTION_CANDIDATE_SAMPLE_SIZE")

    def test_walk_forward_never_leaks_future_into_train(self):
        splits = time_ordered_walk_forward_splits(
            30, minimum_train=10, test_window=5, step=5
        )
        self.assertEqual(len(splits), 4)
        for split in splits:
            self.assertLess(max(split.train_indices), min(split.test_indices))

    def test_one_good_day_cannot_promote(self):
        result = assess_promotion_candidate(
            observation_count=1,
            net_expectancy_r=4.0,
            max_drawdown_r=0,
            regime_ids=["risk_on"],
            outlier_dependency=False,
            execution_quality_acceptable=True,
            failure_conditions_documented=True,
            out_of_sample_positive=True,
        )
        self.assertFalse(result.promotion_candidate)
        self.assertFalse(result.automatic_production_mutation)

    def test_multi_regime_candidate_can_pass_but_never_auto_mutates(self):
        result = assess_promotion_candidate(
            observation_count=240,
            net_expectancy_r=0.22,
            max_drawdown_r=8.0,
            regime_ids=["risk_on", "risk_off", "high_vol"],
            outlier_dependency=False,
            execution_quality_acceptable=True,
            failure_conditions_documented=True,
            out_of_sample_positive=True,
        )
        self.assertTrue(result.promotion_candidate)
        self.assertFalse(result.automatic_production_mutation)


if __name__ == "__main__":
    unittest.main()

