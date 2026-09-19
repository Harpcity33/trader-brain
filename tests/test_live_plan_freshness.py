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
    SymbolOpenOrder,
    SymbolPosition,
    account_exposure_fingerprint,
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


class SymbolExposureFingerprintTests(unittest.TestCase):
    def _sym_positions(self):
        return (SymbolPosition("ABC", 100), SymbolPosition("XYZ", 50))

    def _sym_orders(self):
        return (SymbolOpenOrder("stop-1", "ABC", "SELL", 100, D("48.00")),)

    def test_order_independent_and_valid_sha256(self):
        a = account_exposure_fingerprint((SymbolPosition("ABC", 100), SymbolPosition("XYZ", 50)), self._sym_orders())
        b = account_exposure_fingerprint((SymbolPosition("XYZ", 50), SymbolPosition("ABC", 100)), self._sym_orders())
        self.assertEqual(a, b)
        self.assertRegex(a, r"^[0-9a-f]{64}$")

    def test_changes_when_a_symbol_position_changes(self):
        base = account_exposure_fingerprint(self._sym_positions(), self._sym_orders())
        moved = account_exposure_fingerprint((SymbolPosition("ABC", 90), SymbolPosition("XYZ", 50)), self._sym_orders())
        gone = account_exposure_fingerprint((SymbolPosition("XYZ", 50),), self._sym_orders())
        self.assertNotEqual(base, moved)
        self.assertNotEqual(base, gone)

    def test_changes_when_an_open_order_changes(self):
        base = account_exposure_fingerprint(self._sym_positions(), self._sym_orders())
        cancelled = account_exposure_fingerprint(self._sym_positions(), ())
        self.assertNotEqual(base, cancelled)

    def test_symbol_scheme_never_collides_with_contract_id_scheme(self):
        # Empty exposure in both schemes must still differ (distinct domain tag).
        self.assertNotEqual(account_exposure_fingerprint((), ()), exposure_fingerprint((), ()))

    def test_duplicate_symbol_or_order_and_bad_inputs_fail_closed(self):
        with self.assertRaises(PlanFreshnessError):
            account_exposure_fingerprint((SymbolPosition("ABC", 1), SymbolPosition("ABC", 2)), ())
        with self.assertRaises(PlanFreshnessError):
            account_exposure_fingerprint((), (SymbolOpenOrder("o", "ABC", "SELL", 1), SymbolOpenOrder("o", "ABC", "SELL", 2)))
        with self.assertRaises(PlanFreshnessError):
            SymbolPosition("bad symbol", 1)
        with self.assertRaises(PlanFreshnessError):
            SymbolOpenOrder("o", "ABC", "HOLD", 1)

    def test_feeds_evaluate_plan_freshness_end_to_end(self):
        plan_fp = account_exposure_fingerprint(self._sym_positions(), self._sym_orders())
        changed = account_exposure_fingerprint((SymbolPosition("ABC", 90), SymbolPosition("XYZ", 50)), self._sym_orders())
        self.assertEqual(
            evaluate_plan_freshness(plan_bound_fingerprint=plan_fp, observed_fingerprint=plan_fp,
                                    created_at=CREATED, expires_at=EXPIRES, now=WITHIN),
            (),
        )
        self.assertIn(
            "EXTERNAL_EXPOSURE_CHANGED",
            evaluate_plan_freshness(plan_bound_fingerprint=plan_fp, observed_fingerprint=changed,
                                    created_at=CREATED, expires_at=EXPIRES, now=WITHIN),
        )


if __name__ == "__main__":
    unittest.main()
