from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from titan_brain.ledger import LedgerKind, TradeLedgerRecord, append_jsonl_create_or_append
from titan_brain.models import Instrument, SetupID


def record(kind=LedgerKind.LIVE_EQUITY, instrument=Instrument.STOCK, **overrides):
    values = {
        "trade_id": "trade-1",
        "strategy_id": "strategy-1",
        "ledger_kind": kind,
        "setup_id": SetupID.FIRST_PULLBACK,
        "instrument": instrument,
        "recorded_at": datetime(2026, 8, 28, 15, 0, tzinfo=timezone.utc),
        "planned_risk_dollars": 10,
        "planned_risk_pct": 0.01,
        "stress_risk_dollars": 12,
        "stress_risk_pct": 0.012,
        "setup_score": 80,
        "execution_score": 75,
        "market_regime": "RISK_ON",
        "actual_route": {"instrument": instrument.value},
        "shadow_routes": {"long_call": {"net_expected_r": 0.1}},
        "live_order_authority": kind is not LedgerKind.PAPER_AGGRESSIVE,
    }
    values.update(overrides)
    return TradeLedgerRecord(**values)


class LedgerTests(unittest.TestCase):
    def test_live_equity_rejects_option(self):
        with self.assertRaises(ValueError):
            record(instrument=Instrument.LONG_CALL)

    def test_live_options_rejects_stock(self):
        with self.assertRaises(ValueError):
            record(kind=LedgerKind.LIVE_OPTIONS, instrument=Instrument.STOCK)

    def test_aggressive_lab_cannot_claim_live_authority(self):
        with self.assertRaises(ValueError):
            record(
                kind=LedgerKind.PAPER_AGGRESSIVE,
                instrument=Instrument.LONG_CALL,
                live_order_authority=True,
            )

    def test_append_does_not_rewrite_prior_record(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "live-equity.jsonl"
            first = record()
            second = record(trade_id="trade-2")
            append_jsonl_create_or_append(path, first, expected_kind=LedgerKind.LIVE_EQUITY)
            first_bytes = path.read_bytes()
            append_jsonl_create_or_append(path, second, expected_kind=LedgerKind.LIVE_EQUITY)
            final_bytes = path.read_bytes()
            self.assertTrue(final_bytes.startswith(first_bytes))
            payloads = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([item["trade_id"] for item in payloads], ["trade-1", "trade-2"])

    def test_destination_kind_must_match(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                append_jsonl_create_or_append(
                    Path(directory) / "wrong.jsonl",
                    record(),
                    expected_kind=LedgerKind.LIVE_OPTIONS,
                )


if __name__ == "__main__":
    unittest.main()

