"""Tests for option lifecycle + cumulative P&L accumulator (milestone 7)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from titan_brain.option_lifecycle import (
    LifecycleError,
    LifecycleEvent,
    replay_lifecycle,
)


T0 = datetime(2026, 10, 5, 14, 30, tzinfo=timezone.utc)


def _ev(event_type, offset_min, **kw):
    return LifecycleEvent(event_type=event_type, at=T0 + timedelta(minutes=offset_min),
                          multiplier=100, **kw)


class LifecycleTests(unittest.TestCase):
    def test_open_then_mark_unrealized_is_multiplier_aware(self) -> None:
        events = [
            _ev("open_fill", 0, quantity=2, price="1.00"),
            _ev("fee", 1, amount="1.30"),
            _ev("mark", 2, price="1.50"),
        ]
        state = replay_lifecycle(events, pretrade_baseline_cash="10000")
        self.assertEqual(state.open_contracts, 2)
        # unrealized = (1.50-1.00) * 2 * 100 = 100 ; lifetime = 100 - 1.30 fees
        self.assertEqual(state.lifetime_pnl, "98.7")
        self.assertEqual(state.realized_pnl, "0")

    def test_close_fill_realizes_pnl(self) -> None:
        events = [
            _ev("open_fill", 0, quantity=2, price="1.00"),
            _ev("close_fill", 5, quantity=2, price="1.75"),
        ]
        state = replay_lifecycle(events, pretrade_baseline_cash="10000")
        self.assertEqual(state.open_contracts, 0)
        # realized = (1.75-1.00)*2*100 = 150
        self.assertEqual(state.realized_pnl, "150")
        self.assertEqual(state.lifetime_pnl, "150")

    def test_replay_is_deterministic_across_restart(self) -> None:
        events = [
            _ev("open_fill", 0, quantity=3, price="2.00"),
            _ev("fee", 1, amount="2.00"),
            _ev("mark", 2, price="2.40"),
            _ev("close_fill", 3, quantity=1, price="2.50"),
            _ev("mark", 4, price="2.30"),
        ]
        first = replay_lifecycle(events, pretrade_baseline_cash="10000")
        second = replay_lifecycle(list(events), pretrade_baseline_cash="10000")
        self.assertEqual(first, second)

    def test_day_rollover_preserves_history_no_reset(self) -> None:
        # A losing position must keep its cost basis and realized history over
        # midnight. After rollover the lifetime P&L is unchanged by the rollover.
        events = [
            _ev("open_fill", 0, quantity=1, price="3.00"),
            _ev("mark", 1, price="1.00"),  # big loss
        ]
        before = replay_lifecycle(events, pretrade_baseline_cash="10000")
        events_with_rollover = events + [
            LifecycleEvent(event_type="day_rollover", at=T0 + timedelta(hours=20),
                           multiplier=100, session_date="2026-10-06"),
            _ev("mark", 60 * 21, price="1.00"),  # same mark next day
        ]
        after = replay_lifecycle(events_with_rollover, pretrade_baseline_cash="10000")
        # Lifetime (realized+unrealized-fees) unchanged: still the same big loss.
        self.assertEqual(before.lifetime_pnl, after.lifetime_pnl)
        self.assertEqual(after.lifetime_pnl, "-200")  # (1.00-3.00)*1*100
        self.assertEqual(after.session_date, "2026-10-06")
        # Daily marked P&L for the new day is 0 (mark unchanged since carry-in).
        self.assertEqual(after.daily_marked_pnl, "0")

    def test_unfilled_close_never_marks_flat(self) -> None:
        # Only a close_fill reduces open quantity. There is no "close order"
        # event that reduces it; a mark or fee does not close the position.
        events = [
            _ev("open_fill", 0, quantity=2, price="1.00"),
            _ev("fee", 1, amount="1.00"),      # e.g. a working close order's fee
            _ev("mark", 2, price="0.90"),
        ]
        state = replay_lifecycle(events, pretrade_baseline_cash="10000")
        # Still open: nothing but a fill can flatten it.
        self.assertEqual(state.open_contracts, 2)

    def test_partial_close_leaves_remainder_open(self) -> None:
        events = [
            _ev("open_fill", 0, quantity=3, price="1.00"),
            _ev("close_fill", 5, quantity=1, price="2.00"),
            _ev("mark", 6, price="1.50"),
        ]
        state = replay_lifecycle(events, pretrade_baseline_cash="10000")
        self.assertEqual(state.open_contracts, 2)
        self.assertEqual(state.realized_pnl, "100")  # (2-1)*1*100

    def test_late_fee_reduces_lifetime(self) -> None:
        base = [
            _ev("open_fill", 0, quantity=1, price="1.00"),
            _ev("close_fill", 5, quantity=1, price="2.00"),
        ]
        no_fee = replay_lifecycle(base, pretrade_baseline_cash="10000")
        with_fee = replay_lifecycle(base + [_ev("fee", 6, amount="1.30")], pretrade_baseline_cash="10000")
        self.assertEqual(no_fee.lifetime_pnl, "100")
        self.assertEqual(with_fee.lifetime_pnl, "98.7")

    def test_out_of_order_events_rejected(self) -> None:
        events = [
            _ev("open_fill", 5, quantity=1, price="1.00"),
            _ev("mark", 0, price="1.20"),
        ]
        with self.assertRaisesRegex(LifecycleError, "time order"):
            replay_lifecycle(events, pretrade_baseline_cash="10000")

    def test_close_exceeding_open_rejected(self) -> None:
        events = [
            _ev("open_fill", 0, quantity=1, price="1.00"),
            _ev("close_fill", 1, quantity=2, price="1.50"),
        ]
        with self.assertRaisesRegex(LifecycleError, "exceeds open"):
            replay_lifecycle(events, pretrade_baseline_cash="10000")

    def test_close_before_open_rejected(self) -> None:
        events = [_ev("close_fill", 0, quantity=1, price="1.50")]
        with self.assertRaises(LifecycleError):
            replay_lifecycle(events, pretrade_baseline_cash="10000")

    def test_float_price_rejected(self) -> None:
        with self.assertRaises(LifecycleError):
            replay_lifecycle([_ev("open_fill", 0, quantity=1, price=1.0)], pretrade_baseline_cash="10000")

    def test_empty_log_rejected(self) -> None:
        with self.assertRaises(LifecycleError):
            replay_lifecycle([], pretrade_baseline_cash="10000")


if __name__ == "__main__":
    unittest.main()
