from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from titan_brain.paper_options_engine import (
    Signal,
    close_paper_position,
    default_state,
    entry_fill,
    load_state,
    open_paper_position,
    save_state,
    signal_from_bars,
    weekly_risk_remaining,
)


class DummyContract:
    ticker = "O:TEST"
    expiration_date = "2026-10-09"
    strike = 100.0
    delta = 0.55
    iv = 0.40


class PaperOptionsEngineTests(unittest.TestCase):
    def test_five_minute_breakout_signal(self):
        now = datetime(2026, 9, 28, 10, 5, tzinfo=timezone.utc)
        base = int(datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc).timestamp() * 1000)
        bars = []
        for i in range(6):
            bars.append(
                {
                    "t": base + i * 5 * 60 * 1000,
                    "o": 100 + i * 0.1,
                    "h": 100.3 + i * 0.1,
                    "l": 99.8 + i * 0.1,
                    "c": 100.1 + i * 0.1,
                    "v": 1000,
                    "vw": 100 + i * 0.1,
                }
            )
        bars[-1].update({"o": 100.4, "h": 101.5, "l": 100.3, "c": 101.4, "v": 2500, "vw": 101.0})
        signal = signal_from_bars(
            "TEST",
            bars,
            lane="5m_momentum",
            minutes=5,
            now=now,
            minimum_relative_volume=1.5,
            minimum_bars=6,
            breakout_lookback_bars=3,
            minimum_setup_score=70,
        )
        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "call")
        self.assertGreaterEqual(signal.relative_volume, 1.5)

    def test_weekly_risk_remaining(self):
        state = default_state(1000.0, datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(weekly_risk_remaining(state, 10.0, 1000.0), 100.0)
        self.assertEqual(weekly_risk_remaining(state, 10.0, 950.0), 50.0)
        self.assertEqual(weekly_risk_remaining(state, 10.0, 900.0), 0.0)

    def test_paper_open_and_close(self):
        now = datetime(2026, 9, 28, 14, 0, tzinfo=timezone.utc)
        state = default_state(1000.0, now)
        signal = Signal(
            ticker="TEST",
            lane="5m_momentum",
            direction="call",
            setup_id="ORB_BREAKOUT",
            setup_score=80,
            relative_volume=2.0,
            trigger_price=100,
            invalidation_price=99,
            timestamp=now.isoformat(),
        )
        pos = open_paper_position(
            state,
            signal,
            DummyContract(),
            fill=0.80,
            stop_pct=0.25,
            target_pct=0.50,
            risk_pct=2.0,
            now=now,
        )
        self.assertEqual(state["cash"], 920.0)
        closed = close_paper_position(state, pos["trade_id"], fill=1.20, reason="TARGET", now=now)
        self.assertEqual(closed["pnl"], 40.0)
        self.assertEqual(state["cash"], 1040.0)
        self.assertEqual(len(state["positions"]), 0)

    def test_state_round_trip(self):
        now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
        state = default_state(1000.0, now)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            save_state(path, state)
            loaded = load_state(path, 1000.0, now)
            self.assertEqual(loaded["cash"], 1000.0)


if __name__ == "__main__":
    unittest.main()
