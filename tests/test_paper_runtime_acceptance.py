"""Offline acceptance tests. Fake market data/SMTP only; never a live order."""
import copy
from datetime import datetime, timedelta
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from titan_brain import paper_options_engine as e

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("paper_runtime_acceptance_target", ROOT / "scripts/paper_options_runtime.py")
r = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = r
spec.loader.exec_module(r)
NOW = datetime(2026, 9, 28, 10, 0, 2, tzinfo=e.NY)


def bars():
    base = NOW.replace(hour=9, minute=30, second=0)
    result = [dict(t=int((base + timedelta(minutes=i*5)).timestamp()*1000),
                   o=100+i*.1, h=100.3+i*.1, l=99.8+i*.1, c=100.1+i*.1, v=1000, vw=100+i*.1)
              for i in range(6)]
    result[-1].update(o=100.4, h=101.5, l=100.3, c=101.4, v=2500, vw=101.0)
    return result


def signal(ticker="TEST"):
    return e.Signal(ticker, "5m_momentum", "call", "VWAP_CONTINUATION", 82, 2.5, 101.4, 100.3,
                    NOW.replace(second=0).isoformat())


def snapshot(ticker="TEST", quote_time=NOW):
    return {"details": {"ticker": f"O:{ticker}261009C00100000", "strike_price": 100,
                        "expiration_date": "2026-10-09", "contract_type": "call", "shares_per_contract": 100},
            "last_quote": {"bid": .78, "ask": .8, "bid_size": 10, "ask_size": 10,
                           "last_updated": int(quote_time.timestamp()*1e9), "timeframe": "REAL-TIME"},
            "greeks": {"delta": .55, "gamma": .03, "theta": -.01, "vega": .05},
            "implied_volatility": .4, "open_interest": 500, "day": {"volume": 200}}


class Feed:
    def __init__(self, now=NOW):
        self.now = now
    def market_holidays(self):
        return []
    def market_status(self):
        return {"serverTime": self.now.isoformat(), "exchanges": {"nyse": "open", "nasdaq": "open"}}
    def top_movers(self, direction):
        return ["TEST"]
    def bars(self, ticker, minutes, day):
        return bars() if minutes == 5 else []
    def option_chain(self, ticker, start, end):
        return [snapshot(ticker, self.now)]
    def option_snapshot(self, ticker, contract):
        return {"results": snapshot(ticker, self.now)}
    def previous_bar(self, ticker):
        return dict(o=100, h=104, l=99, c=103, v=1_000_000,
                    t=int((self.now-timedelta(days=3)).timestamp()*1000))


class SafetyAcceptance(unittest.TestCase):
    def setUp(self):
        self.cfg, self.engine = r.load_config()
        self.engine = copy.deepcopy(self.engine)
        self.engine["universe"] = ["TEST"]
        self.state = e.default_state(1000, NOW)
        self.contract = e.contract_from_snapshot(snapshot(), "TEST", NOW.date())

    def make_signal(self, values=None, now=NOW):
        return e.signal_from_bars("TEST", values if values is not None else bars(), lane="5m_momentum",
                    minutes=5, now=now, minimum_relative_volume=1.5, minimum_bars=6,
                    breakout_lookback_bars=3, minimum_setup_score=72)

    def test_closed_regular_candle_signal(self):
        self.assertIsNotNone(self.make_signal())
        self.assertEqual(self.make_signal().timestamp, NOW.replace(second=0).isoformat())

    def test_incomplete_candle_rejected(self):
        self.assertIsNone(self.make_signal(now=NOW-timedelta(seconds=3)))

    def test_stale_candle_rejected(self):
        self.assertIsNone(self.make_signal(now=NOW+timedelta(minutes=10)))

    def test_missing_candle_rejected(self):
        self.assertIsNone(self.make_signal(bars()[1:]))

    def test_unsorted_bars_normalized(self):
        self.assertIsNotNone(self.make_signal(list(reversed(bars()))))

    def test_premarket_bars_rejected(self):
        values = bars()
        for b in values:
            b["t"] -= 3600_000
        self.assertIsNone(self.make_signal(values, now=NOW-timedelta(hours=1)))

    def test_missing_vwap_not_guessed(self):
        values = bars()
        del values[-1]["vw"]
        self.assertIsNone(self.make_signal(values))

    def test_nan_candle_rejected(self):
        values = bars()
        values[-1]["c"] = float("nan")
        self.assertIsNone(self.make_signal(values))

    def test_weekly_profits_do_not_expand_budget(self):
        self.assertEqual(e.weekly_risk_remaining(self.state, 10, 1150), 100)

    def test_open_risk_is_reserved(self):
        e.open_paper_position(self.state, signal(), self.contract, fill=.8, stop_pct=.25,
                              target_pct=.5, risk_pct=2, now=NOW)
        self.assertAlmostEqual(e.new_entry_capacity(self.state, 10), 20)

    def test_fees_are_reserved(self):
        e.open_paper_position(self.state, signal(), self.contract, fill=.8, stop_pct=.25,
                              target_pct=.5, risk_pct=2, now=NOW, fee=.65)
        self.assertAlmostEqual(e.new_entry_capacity(self.state, 10, .65), 18.70)

    def test_allocation_is_not_itself_a_loss_lock(self):
        self.state["cash"] = 900
        self.state["positions"] = [{"contract": "O:T", "last_mark": 1, "entry_fill": 1, "quantity": 1}]
        self.assertEqual(e.new_entry_capacity(self.state, 10), 0)
        self.assertFalse(e.update_weekly_lock(self.state, 10, 1000))

    def test_weekly_lock_latches(self):
        self.assertTrue(e.update_weekly_lock(self.state, 10, 900))
        self.assertTrue(e.update_weekly_lock(self.state, 10, 1000))

    def test_zero_bid_exit_not_fictitious_penny(self):
        self.assertEqual(e.exit_fill(0, .05, .25), 0)

    def test_negative_entry_rejected(self):
        with self.assertRaises(ValueError):
            e.open_paper_position(self.state, signal(), self.contract, fill=-1, stop_pct=.25,
                                  target_pct=.5, risk_pct=2, now=NOW)

    def test_duplicate_entry_rejected(self):
        kwargs = dict(fill=.8, stop_pct=.25, target_pct=.5, risk_pct=2, now=NOW)
        e.open_paper_position(self.state, signal(), self.contract, **kwargs)
        with self.assertRaises(ValueError):
            e.open_paper_position(self.state, signal(), self.contract, **kwargs)

    def test_full_greeks_required(self):
        item = snapshot()
        del item["greeks"]["vega"]
        self.assertIsNone(e.contract_from_snapshot(item, "TEST", NOW.date()))

    def test_nonstandard_multiplier_rejected(self):
        item = snapshot()
        item["details"]["shares_per_contract"] = 150
        self.assertIsNone(e.contract_from_snapshot(item, "TEST", NOW.date()))

    def test_nan_option_rejected(self):
        item = snapshot()
        item["last_quote"]["ask"] = float("nan")
        self.assertIsNone(e.contract_from_snapshot(item, "TEST", NOW.date()))

    def test_empty_depth_rejected(self):
        item = snapshot()
        item["last_quote"]["ask_size"] = 0
        self.assertIsNone(e.contract_from_snapshot(item, "TEST", NOW.date()))

    def test_quote_age_future_delayed(self):
        self.assertTrue(e.quote_is_fresh(self.contract, NOW))
        self.assertFalse(e.quote_is_fresh(self.contract, NOW+timedelta(minutes=5)))
        self.assertFalse(e.quote_is_fresh(self.contract, NOW-timedelta(seconds=1)))
        item = snapshot()
        item["last_quote"]["timeframe"] = "DELAYED"
        self.assertFalse(e.quote_is_fresh(e.contract_from_snapshot(item, "TEST", NOW.date()), NOW))

    def test_fresh_contract_not_hidden_by_stale_top_choice(self):
        rules = {k:v for k,v in self.engine["options"].items() if k != "max_contracts"}
        chosen = e.select_contract([snapshot(quote_time=NOW-timedelta(minutes=10)), snapshot()],
                    underlying="TEST", signal=signal(), today=NOW.date(), max_premium_dollars=100, now=NOW, **rules)
        self.assertIsNotNone(chosen)
        self.assertTrue(e.quote_is_fresh(chosen, NOW))

    def test_movers_method_uses_api(self):
        client = e.MassiveClient("test-not-a-real-key")
        client._get = Mock(return_value={"tickers": [{"ticker": "TEST"}]})
        self.assertEqual(client.top_movers("gainers"), ["TEST"])
        client._get.assert_called_once()

    def test_untrusted_pagination_rejected(self):
        client = e.MassiveClient("test-not-a-real-key")
        client._get = Mock(return_value={"results": [], "next_url": "https://evil.invalid/collect"})
        with self.assertRaises(e.DataUnavailable):
            client.option_chain("TEST", NOW.date(), NOW.date()+timedelta(days=10))
        self.assertEqual(client._get.call_count, 1)

    def test_weekend_smoke_does_not_use_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            feed, sender = Mock(), Mock()
            weekend = NOW-timedelta(days=2)
            outcome = r.heartbeat_once(self.cfg, self.engine, weekend, client=feed,
                        sender=sender, path=Path(tmp)/"state.json", clock=lambda: weekend)
            self.assertEqual(outcome["decision"], "NO_TRADE")
            self.assertFalse(outcome["openai_api_used"])
            self.assertEqual(feed.mock_calls, [])
            sender.assert_not_called()

    def test_holiday_no_scans(self):
        with tempfile.TemporaryDirectory() as tmp:
            feed = Mock()
            feed.market_holidays.return_value = [{"date": NOW.date().isoformat(), "exchange": "NYSE", "status": "closed"}]
            r.heartbeat_once(self.cfg, self.engine, NOW, client=feed, sender=Mock(),
                             path=Path(tmp)/"state.json", clock=lambda: NOW)
            feed.bars.assert_not_called()
            feed.option_chain.assert_not_called()

    def test_early_close_bounds(self):
        holidays = [{"date": NOW.date().isoformat(), "exchange": "NYSE", "status": "early-close", "close": "2026-09-28T17:00:00Z"}]
        self.assertEqual(r.session_bounds(NOW, holidays)[1].astimezone(e.NY).hour, 13)

    def test_end_to_end_single_paper_trade_and_email(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, sends = Path(tmp)/"state.json", []
            def send(subject, body, event_id):
                stored = json.loads(path.read_text())
                self.assertEqual(len(stored["positions"]), 1)
                sends.append(event_id)
            out = r.heartbeat_once(self.cfg, self.engine, NOW, client=Feed(), sender=send,
                                   path=path, clock=lambda: NOW)
            self.assertEqual(out["decision"], "PAPER_BUY")
            self.assertEqual(len(sends), 1)
            r.heartbeat_once(self.cfg, self.engine, NOW, client=Feed(), sender=send,
                             path=path, clock=lambda: NOW)
            self.assertEqual(len(json.loads(path.read_text())["positions"]), 1)
            # The second pass may log a missing underlying mark; never a duplicate BUY.
            self.assertEqual(len([x for x in sends if x.startswith("buy:")]), 1)

    def test_smtp_failure_keeps_trade_and_no_blind_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"state.json"
            sender = Mock(side_effect=RuntimeError("simulated mail failure"))
            r.heartbeat_once(self.cfg, self.engine, NOW, client=Feed(), sender=sender,
                             path=path, clock=lambda: NOW)
            saved = e.load_state(path, 1000, NOW)
            self.assertEqual(len(saved["positions"]), 1)
            self.assertEqual(saved["outbox"][0]["status"], "delivery_unknown")
            r.flush_outbox(saved, path, NOW, sender)
            self.assertEqual(sender.call_count, 1)

    def test_second_position_cannot_reuse_weekly_reserve(self):
        self.state["cash"] = 920
        self.state["positions"] = [{"ticker": "OTHER", "contract": "O:OTHER", "entry_fill": .8,
                                    "last_mark": .8, "quantity": 1}]
        self.assertIsNone(r.maybe_enter(Feed(), self.cfg, self.engine, self.state, signal(), NOW, clock=lambda: NOW))

    def test_no_entries_after_flatten_cutoff(self):
        late = NOW.replace(hour=15, minute=51)
        self.assertIsNone(r.maybe_enter(Feed(late), self.cfg, self.engine, self.state,
                       e.Signal(**dict(signal().__dict__, timestamp=late.isoformat())), late, clock=lambda: late))

    def test_stale_exit_quote_never_fabricates_fill(self):
        pos = e.open_paper_position(self.state, signal(), self.contract, fill=.8, stop_pct=.25,
                                    target_pct=.5, risk_pct=2, now=NOW)
        feed = Feed(NOW-timedelta(minutes=10))
        closed, healthy = r.manage_positions(feed, self.engine, self.state, NOW, NOW.replace(hour=16))
        self.assertFalse(healthy)
        self.assertEqual(closed, [])
        self.assertEqual(len(self.state["positions"]), 1)

    def test_private_atomic_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"state.json"
            e.save_state(path, self.state)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(e.load_state(path, 1000, NOW)["cash"], 1000)

    def test_corrupt_state_not_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"state.json"
            path.write_text("{broken")
            with self.assertRaises(json.JSONDecodeError):
                e.load_state(path, 1000, NOW)

    def test_competing_process_lock_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"state.json"
            with e.state_lock(path):
                with self.assertRaises(BlockingIOError):
                    with e.state_lock(path):
                        pass

    def test_unknown_smtp_error_does_not_echo_secret(self):
        self.assertEqual(r.safe_error(ValueError("SECRET")), "ValueError")

    def test_empty_day_is_not_graded_f(self):
        after = NOW.replace(hour=16, minute=6)
        r.eod_report(self.cfg, self.state, after, after.replace(minute=0))
        body = self.state["outbox"][0]["body"]
        self.assertIn("UNASSESSED", body)
        self.assertIn("Inactivity is not a failing grade", body)


if __name__ == "__main__":
    unittest.main()
