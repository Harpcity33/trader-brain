"""Offline regression coverage for duplicate funded stores and false authority."""
import copy
from datetime import date, datetime, timedelta
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

from titan_brain import paper_options_engine as engine
from titan_brain.eod import build_eod_learning_packet
from titan_brain.paper_reporting import audit_snapshot, quarantine_legacy_eod, report_key

ROOT = Path(__file__).resolve().parents[1]


def module(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


runtime = module("reporting_runtime", "scripts/paper_options_runtime.py")
reconcile = module("reporting_reconcile", "scripts/reconcile_paper_reports.py")
NOW = datetime(2026, 10, 12, 16, 6, tzinfo=engine.NY)


def state_with_trade():
    state = engine.default_state(1000, NOW)
    signal = engine.Signal("TEST", "5m_momentum", "call", "ORB_BREAKOUT", 80, 2, 100, 99,
                           NOW.replace(hour=10).isoformat())
    contract = type("Contract", (), {"ticker": "O:TEST", "expiration_date": "2026-10-23",
                                     "strike": 100, "delta": .55, "iv": .4})()
    pos = engine.open_paper_position(state, signal, contract, fill=.8, stop_pct=.25,
                                    target_pct=.5, risk_pct=2, now=NOW.replace(hour=10), fee=.65)
    return state, pos


class ReportingTests(unittest.TestCase):
    def audit(self, state):
        return audit_snapshot(state, source_path="/tmp/test-book.json", trading_date=NOW.date())

    def test_balanced_book_is_still_quarantined(self):
        state = engine.default_state(1000, NOW)
        state.update(authoritative=True, learning_eligible=True, status="RECONCILED")
        report = self.audit(state)
        self.assertEqual(report["local_accounting_status"], "BALANCED_LOCAL_ONLY")
        self.assertEqual(report["status"], "QUARANTINED")
        self.assertFalse(report["learning_eligible"])
        self.assertIsNone(report["official_equity"])
        self.assertIsNone(report["official_daily_return_pct"])
        self.assertIsNone(report["daily_equity_change"])

    def test_fee_accounting_and_input_immutability(self):
        state, pos = state_with_trade()
        self.assertEqual(state["cash"], 919.35)
        report = self.audit(state)
        self.assertEqual(report["observed"]["fill_derived_cash"], 919.35)
        self.assertIn("OPEN_POSITION_MARK_FRESHNESS_UNVERIFIED", report["faults"])
        engine.close_paper_position(state, pos["trade_id"], fill=1.2, reason="TARGET", now=NOW, fee=.65)
        before = copy.deepcopy(state)
        report = self.audit(state)
        self.assertEqual(state, before)
        self.assertEqual(report["observed"]["cash"], 1038.70)
        self.assertEqual(report["observed"]["recorded_daily_realized_pnl"], 38.70)
        self.assertEqual(report["observed"]["cash_discrepancy"], 0)
        self.assertEqual(report["faults"], [])

    def test_conflicting_balances_never_selected_or_averaged(self):
        reports = []
        for balance in (819.44, 870.85):
            state = engine.default_state(1000, NOW)
            state["cash"] = balance
            report = self.audit(state)
            self.assertIn("CASH_HISTORY_MISMATCH", report["faults"])
            self.assertIsNone(report["official_equity"])
            reports.append(report)
        self.assertNotEqual(reports[0]["snapshot_sha256"], reports[1]["snapshot_sha256"])

    def test_source_identity_separates_copies_and_is_retry_stable(self):
        state = engine.default_state(1000, NOW)
        first = report_key("/tmp/options.json", state, NOW.date())
        other = report_key("/tmp/control/portfolio.json", state, NOW.date())
        self.assertNotEqual(first, other)
        state["cash"] = 990
        state["outbox"].append({"status": "sent"})
        self.assertEqual(first, report_key("/tmp/options.json", state, NOW.date()))

    def test_seed_and_legacy_identity(self):
        state = engine.default_state(1000, NOW)
        del state["strategy_id"]
        del state["ledger_id"]
        state["starting_equity"] = 500
        faults = self.audit(state)["faults"]
        self.assertIn("STARTING_EQUITY_NOT_1000", faults)
        self.assertIn("EXPERIMENT_ID_MISSING_OR_MISMATCHED", faults)
        self.assertIn("LEDGER_ID_MISSING", faults)

    def test_duplicate_trade_ids_and_fill_corruption(self):
        state, pos = state_with_trade()
        closed = engine.close_paper_position(state, pos["trade_id"], fill=1.2, reason="TARGET", now=NOW, fee=.65)
        state["closed_trades"].append(copy.deepcopy(closed))
        self.assertIn("DUPLICATE_TRADE_ID", self.audit(state)["faults"])
        state["closed_trades"] = [closed]
        closed["pnl"] += 10
        state["cash"] += 10
        faults = self.audit(state)["faults"]
        self.assertIn("CLOSED_PNL_MISMATCH", faults)
        self.assertIn("CASH_HISTORY_MISMATCH", faults)

    def test_rounding_tolerance_does_not_hide_accumulated_net_discrepancy(self):
        state, pos = state_with_trade()
        closed = engine.close_paper_position(state, pos["trade_id"], fill=1.2, reason="TARGET", now=NOW, fee=.65)
        another = dict(closed, trade_id="second-trade")
        state["closed_trades"].append(another)
        state["cash"] = 1077.40
        closed["pnl"] = another["pnl"] = 38.71
        self.assertIn("RECORDED_NET_HISTORY_MISMATCH", self.audit(state)["faults"])

    def test_invalid_values_and_unsupported_stock_rows(self):
        for value in (True, "NaN", "Infinity", "1e1000000", None):
            with self.subTest(value=value):
                state = engine.default_state(1000, NOW)
                state["cash"] = value
                self.assertIn("INVALID_ACCOUNTING_BASIS", self.audit(state)["faults"])
        state = engine.default_state(1000, NOW)
        state["closed_trades"] = [{"ticker": "TEST", "shares": 10, "pnl": 3}]
        report = self.audit(state)
        self.assertIn("INVALID_OR_UNSUPPORTED_TRADE_RECORD", report["faults"])
        self.assertNotIn("indicative_equity", report["observed"])

    def test_timestamps_and_new_york_trade_day(self):
        state, pos = state_with_trade()
        closed = engine.close_paper_position(state, pos["trade_id"], fill=1.2, reason="TARGET", now=NOW, fee=.65)
        closed["closed_at"] = "2026-10-13T00:30:00+00:00"  # Oct 12 in New York
        self.assertEqual(self.audit(state)["observed"]["recorded_daily_realized_pnl"], 38.70)
        for timestamp in ("2026-10-12T09:00:00-04:00", "2026-10-12T16:00:00"):
            closed["closed_at"] = timestamp
            self.assertIn("INVALID_OR_UNSUPPORTED_TRADE_RECORD", self.audit(state)["faults"])
        closed["closed_at"] = (NOW + timedelta(days=1)).isoformat()
        self.assertIn("SNAPSHOT_AFTER_REPORT_DATE", self.audit(state)["faults"])

    def test_pending_legacy_eod_preserved_but_not_sent(self):
        state = engine.default_state(1000, NOW)
        original = {"id": "eod:2026-10-12", "subject": "Trader Brain — End of Market Paper Report",
                    "body": "old totals", "kind": "REPORT", "created_at": NOW.isoformat()}
        state["outbox"] = [dict(original, status=s) for s in ("pending", "sent", "delivery_unknown")]
        sent = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            runtime.flush_outbox(state, path, NOW, sender=lambda *a: sent.append(a))
            self.assertEqual(json.loads(path.read_text())["outbox"][0]["status"], "quarantined_unsent")
        self.assertEqual(sent, [])
        self.assertEqual([r["status"] for r in state["outbox"]], ["quarantined_unsent", "sent", "delivery_unknown"])
        self.assertTrue(all(r["body"] == "old totals" for r in state["outbox"]))

    def test_eod_old_sent_flag_gets_one_corrected_source_report(self):
        state = engine.default_state(1000, NOW)
        runtime.daily_bucket(state, NOW.date())["eod_sent"] = True
        cfg, _ = runtime.load_config()
        for _ in range(2):
            runtime.eod_report(cfg, state, NOW, NOW.replace(minute=0), source_path="/tmp/book.json")
        self.assertEqual(len(state["outbox"]), 1)
        item = state["outbox"][0]
        self.assertIn("QUARANTINED", item["subject"])
        self.assertIn("Official experiment equity / daily return: UNAVAILABLE", item["body"])
        self.assertFalse(state["daily"][NOW.date().isoformat()]["learning_eligible"])

    def test_learning_packet_refuses_quarantine_and_forged_eligibility(self):
        for report in ({}, self.audit(engine.default_state(1000, NOW)),
                       {"status": "RECONCILED", "learning_eligible": True}):
            with self.assertRaisesRegex(ValueError, "quarantined"):
                build_eod_learning_packet(trading_date=NOW.date(), created_at=NOW, reviews=[], paper_report=report)

    def test_raw_inspection_never_creates_or_changes_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            self.assertIn("SOURCE_MISSING_UNREADABLE_OR_INVALID", reconcile.inspect_source(path, NOW.date())["faults"])
            self.assertFalse(path.exists())
            for content in ("", "{", "[]", json.dumps(engine.default_state(1000, NOW))):
                path.write_text(content)
                before = (path.read_bytes(), path.stat().st_mtime_ns)
                report = reconcile.inspect_source(path, NOW.date())
                self.assertIsNone(report["official_equity"])
                self.assertEqual(before, (path.read_bytes(), path.stat().st_mtime_ns))

    def test_wrong_seed_blocks_entries_but_preserves_exit_management(self):
        from types import SimpleNamespace
        state, pos = state_with_trade()
        state["starting_equity"] = 500
        state["cash"] = 419.35
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            engine.save_state(path, state)
            before = path.read_bytes()
            loaded = engine.load_state(path, 1000, NOW)
            self.assertEqual(loaded["starting_equity"], 500)
            self.assertEqual(path.read_bytes(), before)
        cfg, rules = runtime.load_config()
        signal = engine.Signal(**pos["signal"])
        self.assertIsNone(runtime.maybe_enter(None, cfg, rules, loaded, signal, NOW))
        self.assertEqual(loaded["daily"][NOW.date().isoformat()]["audit"][-1]["result"],
                         "EXPERIMENT_BASIS_MISMATCH")
        feed = SimpleNamespace(option_snapshot=lambda *a: {"results": {"last_quote": {
            "last_updated": int(NOW.timestamp() * 1e9), "timeframe": "REAL-TIME",
            "bid": .5, "ask": .52, "bid_size": 10}}})
        closed, healthy = runtime.manage_positions(feed, rules, loaded, NOW, NOW)
        self.assertTrue(healthy)
        self.assertEqual(len(closed), 1)
        self.assertEqual(loaded["positions"], [])
        self.assertEqual(loaded["starting_equity"], 500)



if __name__ == "__main__":
    unittest.main()
