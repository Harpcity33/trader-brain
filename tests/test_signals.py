from __future__ import annotations

from dataclasses import replace
import unittest

from titan_runtime.massive import expand_equity_ticker_types
from titan_runtime.signals import (
    Bar,
    compute_market_signal,
    detect_controlled_base,
    relative_volume_context,
    session_lane_context,
    trigger_cross_payload,
)


def bar(index: int, o: float, h: float, l: float, c: float, v: float, accumulated: float) -> Bar:
    return Bar(
        symbol="TEST", start_ms=1_700_000_000_000 + index * 60_000,
        end_ms=1_700_000_060_000 + index * 60_000, open=o, high=h, low=l,
        close=c, volume=v, window_vwap=c, session_vwap=10.4,
        accumulated_volume=accumulated,
    )


class SignalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bars = [
            bar(0, 10.00, 10.20, 9.98, 10.18, 100_000, 1_000_000),
            bar(1, 10.18, 10.50, 10.15, 10.47, 180_000, 1_180_000),
            bar(2, 10.47, 10.75, 10.42, 10.70, 240_000, 1_420_000),
            bar(3, 10.69, 10.72, 10.55, 10.66, 120_000, 1_540_000),
            bar(4, 10.65, 10.74, 10.58, 10.70, 110_000, 1_650_000),
        ]

    def test_controlled_base_requires_two_holding_bars(self) -> None:
        base = detect_controlled_base(self.bars)
        self.assertIsNotNone(base)
        assert base is not None
        self.assertEqual(base["base_high"], 10.74)
        self.assertEqual(base["support"], 10.55)

    def test_market_signal_is_not_acceleration_score(self) -> None:
        snapshot = {"prev_close": 9.50, "prev_day_volume": 2_000_000, "day_volume": 1_650_000}
        quote = {
            "bid": 10.69, "ask": 10.70, "spread_pct": 0.0935,
            "timestamp_ms": 4_102_444_800_000,
        }
        signal = compute_market_signal(self.bars, snapshot, quote, 0.75)
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertIn("not the Titan Acceleration Score", signal["signal_disclaimer"])
        self.assertFalse(signal["catalyst_verified"])
        self.assertEqual(signal["base_high"], 10.74)
        self.assertGreater(signal["dollar_volume"], 10_000_000)
        self.assertIsNone(signal["relative_volume"])
        self.assertEqual(signal["relative_volume_quality"], "UNAVAILABLE")
        self.assertFalse(signal["relative_volume_legacy_full_day_used"])

    def test_exactly_five_dollars_uses_stricter_lane(self) -> None:
        bars = [replace(item, close=5.0, session_vwap=5.0) for item in self.bars]
        signal = compute_market_signal(
            bars,
            {"prev_close": 4.75, "day_volume": 1_650_000},
            None,
            0.9375,
        )
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal["lane"], "under5")

    def test_relative_volume_uses_same_minute_median_not_prior_full_day(self) -> None:
        snapshot = {
            "prev_close": 9.50,
            "prev_day_volume": 10_000_000,
            "day_volume": 1_650_000,
        }
        signal = compute_market_signal(
            self.bars,
            snapshot,
            None,
            0.75,
            same_minute_cumulative_history=[800_000, 1_000_000, 1_200_000],
        )
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal["relative_volume"], 1.65)
        self.assertEqual(signal["relative_volume_reference_median"], 1_000_000)
        self.assertEqual(signal["relative_volume_sample_size"], 3)
        self.assertEqual(signal["relative_volume_quality"], "LOW")
        self.assertFalse(signal["relative_volume_fallback_used"])

    def test_relative_volume_reports_insufficient_history_without_fallback(self) -> None:
        context = relative_volume_context(1_500_000, [900_000, 1_100_000])
        self.assertIsNone(context["relative_volume"])
        self.assertEqual(context["relative_volume_quality"], "INSUFFICIENT")
        self.assertEqual(
            context["relative_volume_fallback_reason"],
            "insufficient_prior_completed_sessions",
        )
        self.assertFalse(context["relative_volume_legacy_full_day_used"])

    def test_emerging_intraday_leader_is_context_not_entry_authority(self) -> None:
        bars = [replace(item, official_open=10.10) for item in self.bars[:3]]
        signal = compute_market_signal(
            bars,
            {"prev_close": 10.00, "day_open": 10.10},
            None,
            0.75,
            same_minute_cumulative_history=[700_000, 800_000, 900_000],
        )
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertTrue(signal["emerging_intraday_leader"])
        self.assertFalse(signal["emerging_intraday_leader_provisional"])
        self.assertEqual(
            signal["emerging_intraday_leader_context"]["role"],
            "ranking_context_only_never_entry_authority",
        )

    def test_equity_ticker_types_preserve_companies_and_expand_cef_alias(self) -> None:
        self.assertEqual(
            expand_equity_ticker_types(("CS", "ADRC", "ETF", "CEF", "FUND")),
            ("CS", "ADRC", "ETF", "FUND"),
        )

    def test_downside_mover_is_routed_to_long_put_discovery_only(self) -> None:
        bars = [
            bar(0, 10.00, 10.02, 9.72, 9.75, 120_000, 1_000_000),
            bar(1, 9.74, 9.76, 9.38, 9.42, 170_000, 1_170_000),
            bar(2, 9.41, 9.43, 9.02, 9.08, 230_000, 1_400_000),
            bar(3, 9.09, 9.20, 8.98, 9.02, 110_000, 1_510_000),
            bar(4, 9.03, 9.08, 8.92, 8.96, 100_000, 1_610_000),
        ]
        snapshot = {"prev_close": 10.50, "prev_day_volume": 2_000_000}
        quote = {
            "bid": 8.95, "ask": 8.96, "spread_pct": 0.1117,
            "timestamp_ms": 4_102_444_800_000,
        }
        signal = compute_market_signal(bars, snapshot, quote, 0.75)
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal["direction"], "DOWN")
        self.assertEqual(signal["board"], "DOWNSIDE_LONG_PUT")
        self.assertTrue(signal["downside_trigger_required"])
        self.assertIsNone(signal["base_high"])
        self.assertIn("single-leg long put", signal["downside_authority"])

    def test_three_expansion_bars_over_four_atr_enter_watch_not_chase(self) -> None:
        bars = [
            bar(0, 10.00, 10.20, 9.95, 10.15, 100_000, 1_000_000),
            bar(1, 10.15, 10.55, 10.10, 10.50, 120_000, 1_120_000),
            bar(2, 10.50, 11.15, 10.45, 11.10, 145_000, 1_265_000),
            bar(3, 11.10, 11.90, 11.05, 11.82, 175_000, 1_440_000),
            bar(4, 11.82, 12.75, 11.78, 12.68, 210_000, 1_650_000),
        ]
        bars = [replace(item, session_vwap=9.50) for item in bars]
        snapshot = {"prev_close": 9.50, "prev_day_volume": 2_000_000}
        quote = {
            "bid": 12.67, "ask": 12.68, "spread_pct": 0.0789,
            "timestamp_ms": 4_102_444_800_000,
        }
        signal = compute_market_signal(bars, snapshot, quote, 0.75)
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertGreaterEqual(signal["consecutive_expansion_bars"], 3)
        self.assertTrue(signal["exhaustion_lock"])
        self.assertFalse(signal["entry_setup_eligible"])
        self.assertEqual(signal["disposition"], "ENTRY_REJECTED_KEEP_WATCH")

    def test_trigger_cross_never_returns_trade_authority(self) -> None:
        candidate = {
            "symbol": "TEST", "base_high": 10.74, "short_atr": 0.25,
            "limit_ceiling": 10.76,
            "payload_json": (
                '{"pullback_volume_per_second":1800,'
                '"session_lane_eligible":true,"session_blockers":[]}'
            ),
        }
        second = {"h": 10.75, "c": 10.75, "v": 2500, "s": 1_700_000_500_000}
        quote = {
            "ask": 10.75, "spread_pct": 0.1,
            "timestamp_ms": 1_700_000_500_000,
        }
        payload = trigger_cross_payload(candidate, second, quote, 0.75, now_ms=1_700_000_500_500)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertTrue(payload["volume_pace_expanding"])
        self.assertTrue(payload["price_cross_confirmed"])
        self.assertFalse(payload["trade_authority"])
        self.assertIn("Observation only", payload["signal_disclaimer"])

    def test_trigger_cross_uses_five_eighth_atr_chase_ceiling(self) -> None:
        candidate = {
            "symbol": "TEST", "base_high": 10.0, "short_atr": 1.0,
            "limit_ceiling": 10.7,
            "payload_json": '{"pullback_volume_per_second":1000}',
        }
        second = {"h": 10.6, "c": 10.6, "v": 2000, "s": 1_700_000_500_000}
        quote = {
            "ask": 10.6, "spread_pct": 0.1,
            "timestamp_ms": 1_700_000_500_000,
        }
        payload = trigger_cross_payload(
            candidate, second, quote, 0.75, now_ms=1_700_000_500_500
        )
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["chase_extension_limit_atr"], 0.625)
        self.assertTrue(payload["inside_chase_ceiling"])

    def test_trigger_cross_rejects_bar_high_when_live_ask_is_below_trigger(self) -> None:
        candidate = {
            "symbol": "TEST", "base_high": 10.74, "short_atr": 0.25,
            "limit_ceiling": 10.76,
            "payload_json": '{"pullback_volume_per_second":1800}',
        }
        second = {"h": 10.75, "c": 10.73, "v": 2500, "s": 1_700_000_500_000}
        quote = {
            "ask": 10.73, "spread_pct": 0.1,
            "timestamp_ms": 1_700_000_500_000,
        }
        self.assertIsNone(
            trigger_cross_payload(candidate, second, quote, 0.75, now_ms=1_700_000_500_500)
        )

    def test_trigger_cross_rejects_stale_quote(self) -> None:
        candidate = {
            "symbol": "TEST", "base_high": 10.74, "short_atr": 0.25,
            "limit_ceiling": 10.76,
            "payload_json": '{"pullback_volume_per_second":1800}',
        }
        second = {"h": 10.75, "c": 10.75, "v": 2500, "s": 1_700_000_500_000}
        quote = {
            "ask": 10.75, "spread_pct": 0.1,
            "timestamp_ms": 1_700_000_480_000,
        }
        self.assertIsNone(
            trigger_cross_payload(candidate, second, quote, 0.75, now_ms=1_700_000_500_500)
        )


    def test_trigger_cross_applies_dynamic_spread_to_risk_gate(self) -> None:
        candidate = {
            "symbol": "TEST", "base_high": 10.74, "invalidation": 10.54,
            "short_atr": 0.25, "limit_ceiling": 10.76,
            "payload_json": (
                '{"pullback_volume_per_second":1800,'
                '"session_lane_eligible":true,"session_blockers":[]}'
            ),
        }
        second = {"h": 10.75, "c": 10.75, "v": 2500, "s": 1_700_000_500_000}
        good_quote = {
            "bid": 10.73, "ask": 10.75, "spread_pct": 0.1862,
            "timestamp_ms": 1_700_000_500_000,
        }
        payload = trigger_cross_payload(
            candidate, second, good_quote, 0.75, 0.15, now_ms=1_700_000_500_500
        )
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertTrue(payload["spread_to_risk_pass"])
        self.assertAlmostEqual(payload["spread_to_structural_risk"], 0.10)

        wide_quote = {
            "bid": 10.70, "ask": 10.75, "spread_pct": 0.4662,
            "timestamp_ms": 1_700_000_500_000,
        }
        self.assertIsNone(
            trigger_cross_payload(
                candidate, second, wide_quote, 0.75, 0.15,
                now_ms=1_700_000_500_500,
            )
        )

    def test_under5_premarket_is_flagged_as_ineligible(self) -> None:
        # 2026-08-20 08:20:00 America/New_York.
        context = session_lane_context("under5", 1_787_227_600_000)
        self.assertFalse(context["session_lane_eligible"])
        self.assertIn("under-$5", context["session_blockers"][0])

    def test_under5_regular_session_can_be_prepared(self) -> None:
        # 2026-08-20 10:00:00 America/New_York.
        context = session_lane_context("under5", 1_787_233_600_000)
        self.assertTrue(context["session_lane_eligible"])


if __name__ == "__main__":
    unittest.main()
