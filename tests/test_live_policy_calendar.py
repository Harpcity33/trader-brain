from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
import unittest
from zoneinfo import ZoneInfo

from titan_brain.live.calendar import ExchangeCalendar
from titan_brain.live.policy import PolicyBundle


ROOT = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")


class PolicyCalendarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = PolicyBundle.load(ROOT)

    def test_policy_is_bound_and_deliberately_not_live(self) -> None:
        self.assertEqual(self.policy.account_last4, "7153")
        self.assertFalse(self.policy.live_entries_configured)
        self.assertIn("PER_MUTATION_CONFIRMATION_STILL_REQUIRED", self.policy.activation_blockers)
        with self.assertRaisesRegex(ValueError, "LIVE_ENTRIES_DISABLED"):
            self.policy.require_activation_ready()

    def test_account_mismatch_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "account"):
            self.policy.require_account("123456789", "limited_margin")

    def test_regular_entry_tuple_and_premarket_attended_boundary(self) -> None:
        self.policy.require_entry_tuple(
            quantity=3,
            limit_price=Decimal("12.50"),
            market_hours="regular_hours",
            order_type="limit",
            time_in_force="gfd",
            now=datetime(2026, 9, 8, 10, 0, tzinfo=ET),
        )
        with self.assertRaisesRegex(ValueError, "attended-only"):
            self.policy.require_entry_tuple(
                quantity=3,
                limit_price=Decimal("12.50"),
                market_hours="extended_hours",
                order_type="limit",
                time_in_force="gfd",
                now=datetime(2026, 9, 8, 8, 0, tzinfo=ET),
            )

    def test_protection_cannot_widen(self) -> None:
        self.policy.require_protection_tuple(
            quantity=2,
            stop_price="9.50",
            original_stop="9.50",
            entry_price="10.50",
            market_hours="regular_hours",
            order_type="stop_market",
            time_in_force="gtc",
        )
        with self.assertRaisesRegex(ValueError, "widened"):
            self.policy.require_protection_tuple(
                quantity=2,
                stop_price="9.25",
                original_stop="9.50",
                entry_price="10.50",
                market_hours="regular_hours",
                order_type="stop_market",
                time_in_force="gtc",
            )

    def test_holiday_early_close_and_dst_are_explicit(self) -> None:
        calendar = ExchangeCalendar.from_json(ROOT / "config/nyse_calendar_2026.json")
        self.assertFalse(calendar.is_trading_day(date(2026, 9, 7)))
        self.assertEqual(calendar.lane(datetime(2026, 9, 7, 10, tzinfo=ET)), "closed")
        early = calendar.session_times(date(2026, 11, 27))
        self.assertIsNotNone(early)
        assert early is not None
        self.assertEqual(early.close_at.hour, 13)
        self.assertEqual(early.closeout_start_at.hour, 12)
        self.assertEqual(early.closeout_start_at.minute, 50)
        winter = calendar.session_times(date(2026, 12, 1))
        summer = calendar.session_times(date(2026, 9, 8))
        assert winter is not None and summer is not None
        self.assertNotEqual(winter.open_at.utcoffset(), summer.open_at.utcoffset())


if __name__ == "__main__":
    unittest.main()
