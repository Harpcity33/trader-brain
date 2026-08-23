from __future__ import annotations

from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from titan_runtime.cli import cmd_risk_gate
from titan_runtime.storage import Store


class CliTests(unittest.TestCase):
    def test_risk_gate_requires_exact_fresh_snapshot_and_positive_floor_buffer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "test.sqlite3"
            now = datetime.now(timezone.utc)
            baseline = now - timedelta(minutes=5)

            def evidence(snapshot: str) -> dict:
                return {
                    "snapshot": snapshot,
                    "account_state_readable": True,
                    "orders_reconciled": True,
                    "positions_reconciled": True,
                }

            base = {
                "account_key": "ending-7153",
                "session_date": now.date().isoformat(),
                "strategy_version": "titan_live_canonical_2026-08-22_v1",
                "start_of_day_equity": 5000,
                "baseline_confirmed_at": baseline.isoformat(),
                "current_equity": 5000,
                "realized_net_pnl": 0,
                "confirmed_cash_flow_adjustment": 0,
                "broker_confirmed_at": now.isoformat(),
                "broker_state": evidence("fresh"),
            }
            store = Store(database)
            store.upsert_risk_session(base)
            store.close()

            def gate(expected: str, max_age_seconds: int = 90) -> int:
                args = SimpleNamespace(
                    account_key="ending-7153",
                    session_date=now.date().isoformat(),
                    expected_broker_confirmed_at=expected,
                    max_age_seconds=max_age_seconds,
                )
                with patch(
                    "titan_runtime.cli.load",
                    return_value=SimpleNamespace(database_path=database),
                ), redirect_stdout(StringIO()):
                    return cmd_risk_gate(args)

            self.assertEqual(gate(now.isoformat()), 0)
            self.assertEqual(gate((now - timedelta(seconds=1)).isoformat()), 2)
            self.assertEqual(gate(now.isoformat(), max_age_seconds=-1), 2)

            store = Store(database)
            objective_time = datetime.now(timezone.utc)
            store.upsert_risk_session(
                {
                    **base,
                    "current_equity": 5160,
                    "realized_net_pnl": 160,
                    "broker_confirmed_at": objective_time.isoformat(),
                    "broker_state": evidence("objective"),
                }
            )
            floor_time = datetime.now(timezone.utc)
            floor = store.upsert_risk_session(
                {
                    **base,
                    "current_equity": 5125,
                    "realized_net_pnl": 125,
                    "broker_confirmed_at": floor_time.isoformat(),
                    "broker_state": evidence("floor"),
                }
            )
            store.close()
            self.assertFalse(floor["new_entries_allowed"])
            self.assertEqual(gate(floor_time.isoformat()), 2)


if __name__ == "__main__":
    unittest.main()
