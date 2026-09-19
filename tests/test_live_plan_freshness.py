"""A1.5: external-change invalidation for a sealed autonomous plan.

Proves live/plan_freshness.py: an out-of-band exposure change invalidates an
otherwise-unexpired plan; the validity window is enforced; the exposure
fingerprint is deterministic and order-independent; ambiguous/duplicate
observations fail closed. No broker, credential, or network access.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from titan_brain.live.plan_freshness import (
    ObservedOpenOrder,
    ObservedPosition,
    PlanFreshnessError,
    evaluate_plan_freshness,
    exposure_fingerprint,
)


D = Decimal
CREATED = datetime(2026, 9, 18, 14, 0, tzinfo=timezone.utc)
EXPIRES = CREATED + timedelta(seconds=30)
WITHIN = CREATED + timedelta(seconds=5)


def _positions():
    return (ObservedPosition(123, 100), ObservedPosition(456, 50))


def _orders():
    return (ObservedOpenOrder("stop-1", 123, "SELL", 100, D("48.00")),)


class ExposureFingerprintTests(unittest.TestCase):
    def test_fingerprint_is_order_independent(self):
        a = exposure_fingerprint((ObservedPosition(123, 100), ObservedPosition(456, 50)), _orders())
        b = exposure_fingerprint((ObservedPosition(456, 50), ObservedPosition(123, 100)), _orders())
        self.assertEqual(a, b)

    def test_fingerprint_changes_when_a_position_changes(self):
        base = exposure_fingerprint(_positions(), _orders())
        moved = exposure_fingerprint((ObservedPosition(123, 90), ObservedPosition(456, 50)), _orders())
        gone = exposure_fingerprint((ObservedPosition(456, 50),), _orders())
        self.assertNotEqual(base, moved)
        self.assertNotEqual(base, gone)

    def test_fingerprint_changes_when_an_open_order_changes(self):
        base = exposure_fingerprint(_positions(), _orders())
        cancelled = exposure_fingerprint(_positions(), ())
        self.assertNotEqual(base, cancelled)

    def test_duplicate_position_or_order_fails_closed(self):
        with self.assertRaises(PlanFreshnessError):
            exposure_fingerprint((ObservedPosition(123, 100), ObservedPosition(123, 50)), ())
        with self.assertRaises(PlanFreshnessError):
            exposure_fingerprint((), (ObservedOpenOrder("o", 123, "SELL", 1), ObservedOpenOrder("o", 123, "SELL", 2)))


class PlanFreshnessTests(unittest.TestCase):
    def test_fresh_plan_with_unchanged_exposure_has_no_blockers(self):
        fp = exposure_fingerprint(_positions(), _orders())
        self.assertEqual(
            evaluate_plan_freshness(plan_bound_fingerprint=fp, observed_fingerprint=fp,
                                    created_at=CREATED, expires_at=EXPIRES, now=WITHIN),
            (),
        )

    def test_external_exposure_change_invalidates_even_when_unexpired(self):
        plan_fp = exposure_fingerprint(_positions(), _orders())
        observed_fp = exposure_fingerprint((ObservedPosition(123, 90), ObservedPosition(456, 50)), _orders())
        blockers = evaluate_plan_freshness(
            plan_bound_fingerprint=plan_fp, observed_fingerprint=observed_fp,
            created_at=CREATED, expires_at=EXPIRES, now=WITHIN,
        )
        self.assertIn("EXTERNAL_EXPOSURE_CHANGED", blockers)

    def test_expired_plan_blocks(self):
        fp = exposure_fingerprint(_positions(), _orders())
        blockers = evaluate_plan_freshness(
            plan_bound_fingerprint=fp, observed_fingerprint=fp,
            created_at=CREATED, expires_at=EXPIRES, now=EXPIRES + timedelta(seconds=1),
        )
        self.assertIn("PLAN_EXPIRED", blockers)

    def test_not_yet_valid_plan_blocks(self):
        fp = exposure_fingerprint(_positions(), _orders())
        blockers = evaluate_plan_freshness(
            plan_bound_fingerprint=fp, observed_fingerprint=fp,
            created_at=CREATED, expires_at=EXPIRES, now=CREATED - timedelta(seconds=1),
        )
        self.assertIn("PLAN_NOT_YET_VALID", blockers)

    def test_invalid_validity_window_blocks(self):
        fp = exposure_fingerprint(_positions(), _orders())
        blockers = evaluate_plan_freshness(
            plan_bound_fingerprint=fp, observed_fingerprint=fp,
            created_at=CREATED, expires_at=CREATED, now=CREATED,
        )
        self.assertIn("PLAN_VALIDITY_WINDOW_INVALID", blockers)

    def test_expiry_and_external_change_can_both_block(self):
        plan_fp = exposure_fingerprint(_positions(), _orders())
        observed_fp = exposure_fingerprint((), ())
        blockers = evaluate_plan_freshness(
            plan_bound_fingerprint=plan_fp, observed_fingerprint=observed_fp,
            created_at=CREATED, expires_at=EXPIRES, now=EXPIRES + timedelta(seconds=1),
        )
        self.assertIn("PLAN_EXPIRED", blockers)
        self.assertIn("EXTERNAL_EXPOSURE_CHANGED", blockers)

    def test_non_sha256_fingerprint_fails_closed(self):
        fp = exposure_fingerprint(_positions(), _orders())
        with self.assertRaises(PlanFreshnessError):
            evaluate_plan_freshness(plan_bound_fingerprint="nope", observed_fingerprint=fp,
                                    created_at=CREATED, expires_at=EXPIRES, now=WITHIN)

    def test_naive_datetime_fails_closed(self):
        fp = exposure_fingerprint(_positions(), _orders())
        with self.assertRaises(PlanFreshnessError):
            evaluate_plan_freshness(plan_bound_fingerprint=fp, observed_fingerprint=fp,
                                    created_at=datetime(2026, 9, 18, 14, 0), expires_at=EXPIRES, now=WITHIN)


if __name__ == "__main__":
    unittest.main()
