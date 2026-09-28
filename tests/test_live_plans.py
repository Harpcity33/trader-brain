from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import unittest
from zoneinfo import ZoneInfo

from titan_brain.live.plans import ExpiringPlan
from titan_brain.live.policy import PolicyBundle


ROOT = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 8, 10, 0, tzinfo=ET)


class ExpiringPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = PolicyBundle.load(ROOT)

    def make_plan(self, **overrides) -> ExpiringPlan:
        raw = {
            "strategy_id": self.policy.strategy_id,
            "policy_hash": self.policy.policy_hash,
            "config_hash": self.policy.config_hash,
            "account_last4": "7153",
            "symbol": "XYZ",
            "instrument_id": "instrument-xyz",
            "setup_id": "ORB_BREAKOUT",
            "quantity": 2,
            "entry_limit": "12.00",
            "structural_stop": "11.50",
            "targets": ["13.00", "14.00"],
            "execution_reserve_per_share": "0.05",
            "market_hours": "regular_hours",
            "time_in_force": "gfd",
            "quality_tier": "normal",
            "completed_bar_end": NOW - timedelta(minutes=1),
            "quote_observed_at": NOW - timedelta(seconds=2),
            "created_at": NOW - timedelta(seconds=1),
            "expires_at": NOW + timedelta(seconds=20),
            "source_event_ids": ["massive-bar-1", "rh-quote-1"],
            "direction": "long",
            "allow_add": False,
            "allow_reentry": False,
        }
        raw.update(overrides)
        return ExpiringPlan.build(**raw)

    def test_canonical_round_trip_and_risk(self) -> None:
        plan = self.make_plan()
        plan.validate(self.policy, NOW)
        self.assertEqual(ExpiringPlan.from_mapping(plan.to_mapping()), plan)
        self.assertEqual(str(plan.planned_risk), "1.00")
        self.assertEqual(str(plan.stress_risk), "1.10")

    def test_tampering_and_expiry_fail_closed(self) -> None:
        plan = self.make_plan()
        payload = plan.to_mapping()
        payload["quantity"] = 3
        with self.assertRaisesRegex(ValueError, "plan_id"):
            ExpiringPlan.from_mapping(payload)
        with self.assertRaisesRegex(ValueError, "expired"):
            self.make_plan(expires_at=NOW - timedelta(microseconds=1)).validate(self.policy, NOW)

    def test_policy_hash_and_causality_are_bound(self) -> None:
        with self.assertRaisesRegex(ValueError, "binding"):
            self.make_plan(policy_hash="0" * 64).validate(self.policy, NOW)
        with self.assertRaisesRegex(ValueError, "future bar"):
            self.make_plan(completed_bar_end=NOW + timedelta(seconds=1)).validate(
                self.policy, NOW
            )

    def test_add_reentry_and_premarket_autonomy_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unauthorized"):
            self.make_plan(allow_add=True).validate(self.policy, NOW)
        premarket = datetime(2026, 9, 8, 8, 0, tzinfo=ET)
        with self.assertRaisesRegex(ValueError, "attended-only"):
            self.make_plan(
                market_hours="extended_hours",
                created_at=premarket - timedelta(seconds=1),
                expires_at=premarket + timedelta(seconds=20),
                completed_bar_end=premarket - timedelta(minutes=1),
                quote_observed_at=premarket - timedelta(seconds=2),
            ).validate(self.policy, premarket)


if __name__ == "__main__":
    unittest.main()
