from __future__ import annotations

from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
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
                    "unleveraged_buying_power_dollars": 5000,
                    "current_gross_exposure_dollars": 0,
                    "working_entry_notional_dollars": 0,
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

            def gate(
                expected: str,
                max_age_seconds: int = 90,
                *,
                entry_check: bool = False,
                proposed_new_risk: float | None = None,
                reviewed_entry_price: float = 10,
                structural_stop_price: float = 9,
                quantity: float | None = None,
                contract_multiplier: float = 1,
                modeled_execution_loss: float = 0,
                stress_tail_loss: float | None = None,
                existing_open_downside: float | None = None,
                existing_pending_risk: float | None = None,
                execution_reserve: float | None = None,
            ) -> int:
                args = SimpleNamespace(
                    account_key="ending-7153",
                    session_date=now.date().isoformat(),
                    expected_broker_confirmed_at=expected,
                    max_age_seconds=max_age_seconds,
                    entry_check=entry_check,
                    instrument_key="equity:TEST" if entry_check else None,
                    thesis_key="TEST" if entry_check else None,
                    risk_action="ENTRY" if entry_check else None,
                    reviewed_entry_price=(reviewed_entry_price if entry_check else None),
                    structural_stop_price=(structural_stop_price if entry_check else None),
                    quantity=(
                        quantity if quantity is not None
                        else (proposed_new_risk if entry_check else None)
                    ),
                    contract_multiplier=(contract_multiplier if entry_check else None),
                    modeled_execution_loss_dollars=(
                        modeled_execution_loss if entry_check else None
                    ),
                    stress_tail_loss_dollars=(
                        stress_tail_loss
                        if stress_tail_loss is not None
                        else (proposed_new_risk if entry_check else None)
                    ),
                    existing_open_downside_dollars=existing_open_downside,
                    existing_pending_risk_dollars=existing_pending_risk,
                    execution_reserve_dollars=execution_reserve,
                )
                buffer = StringIO()
                with patch(
                    "titan_runtime.cli.load",
                    return_value=SimpleNamespace(database_path=database),
                ), redirect_stdout(buffer):
                    result = cmd_risk_gate(args)
                gate.last_output = buffer.getvalue()
                return result

            gate.last_output = ""

            self.assertEqual(gate(now.isoformat()), 0)
            self.assertEqual(
                gate(
                    now.isoformat(),
                    entry_check=True,
                    proposed_new_risk=80,
                    existing_open_downside=10,
                    existing_pending_risk=0,
                    execution_reserve=10,
                ),
                0,
            )
            self.assertIn("risk_gate_authorization", gate.last_output)
            self.assertIn('"reviewed_notional_dollars": 800.0', gate.last_output)
            first_authorization = json.loads(gate.last_output)[
                "risk_gate_authorization"
            ]["authorization_id"]
            self.assertEqual(
                gate(
                    now.isoformat(),
                    entry_check=True,
                    proposed_new_risk=1,
                    existing_open_downside=0,
                    existing_pending_risk=0,
                    execution_reserve=5,
                ),
                2,
            )
            self.assertIn("another pending order authorization is active", gate.last_output)
            store = Store(database)
            store.release_risk_authorization(first_authorization, "unit_test_abort")
            store.close()
            self.assertEqual(
                gate(
                    now.isoformat(),
                    entry_check=True,
                    proposed_new_risk=80.01,
                    existing_open_downside=10,
                    existing_pending_risk=0,
                    execution_reserve=10,
                ),
                2,
            )
            with self.assertRaisesRegex(ValueError, "requires explicit values"):
                gate(
                    now.isoformat(),
                    entry_check=True,
                    proposed_new_risk=80,
                    existing_open_downside=10,
                    execution_reserve=10,
                )
            with self.assertRaisesRegex(ValueError, "at least 5"):
                gate(
                    now.isoformat(),
                    entry_check=True,
                    proposed_new_risk=80,
                    existing_open_downside=10,
                    existing_pending_risk=0,
                    execution_reserve=4.99,
                )
            self.assertEqual(
                gate(
                    now.isoformat(),
                    entry_check=True,
                    proposed_new_risk=95,
                    reviewed_entry_price=10,
                    structural_stop_price=9.81,
                    quantity=500,
                    stress_tail_loss=95,
                    existing_open_downside=0,
                    existing_pending_risk=0,
                    execution_reserve=5,
                ),
                0,
            )
            self.assertIn('"reviewed_notional_dollars": 5000.0', gate.last_output)
            full_authorization = json.loads(gate.last_output)[
                "risk_gate_authorization"
            ]["authorization_id"]
            self.assertEqual(
                gate(
                    now.isoformat(),
                    entry_check=True,
                    proposed_new_risk=90,
                    reviewed_entry_price=10,
                    structural_stop_price=9.82,
                    quantity=500.1,
                    stress_tail_loss=90,
                    existing_open_downside=0,
                    existing_pending_risk=0,
                    execution_reserve=5,
                ),
                2,
            )
            self.assertIn("reviewed notional exceeds fresh unleveraged", gate.last_output)
            store = Store(database)
            store.release_risk_authorization(full_authorization, "unit_test_abort")
            store.close()

            # If another wake records a newer broker snapshot after the gate's
            # read but before its reservation transaction, the old calculation
            # must fail closed instead of receiving a durable authorization.
            original_reserve = Store.reserve_risk_authorization
            race_time = datetime.now(timezone.utc)

            def reserve_after_concurrent_snapshot(active_store, authorization):
                racing_store = Store(database)
                racing_store.upsert_risk_session(
                    {
                        **base,
                        "current_equity": 4999,
                        "realized_net_pnl": -1,
                        "broker_confirmed_at": race_time.isoformat(),
                        "broker_state": evidence("concurrent-wake"),
                    }
                )
                racing_store.close()
                return original_reserve(active_store, authorization)

            with patch.object(
                Store,
                "reserve_risk_authorization",
                new=reserve_after_concurrent_snapshot,
            ):
                self.assertEqual(
                    gate(
                        now.isoformat(),
                        entry_check=True,
                        proposed_new_risk=1,
                        existing_open_downside=0,
                        existing_pending_risk=0,
                        execution_reserve=5,
                    ),
                    2,
                )
            self.assertIn(
                "risk session changed before authorization could be reserved",
                gate.last_output,
            )
            self.assertEqual(
                gate(
                    race_time.isoformat(),
                    max_age_seconds=4,
                    entry_check=True,
                    proposed_new_risk=1,
                    existing_open_downside=0,
                    existing_pending_risk=0,
                    execution_reserve=5,
                ),
                2,
            )
            self.assertIn("within 6..90 seconds", gate.last_output)
            self.assertEqual(
                gate(race_time.isoformat(), max_age_seconds=91),
                2,
            )
            self.assertIn("within 6..90 seconds", gate.last_output)
            self.assertEqual(gate((now - timedelta(seconds=1)).isoformat()), 2)
            self.assertEqual(gate(now.isoformat(), max_age_seconds=-1), 2)

            # A current open winner is already reflected in account equity but
            # deliberately not credited by LOSS_GAUGE.  Only the open downside
            # beyond that uncredited profit may consume loss-lock capacity.
            winner_time = datetime.now(timezone.utc)
            store = Store(database)
            winner = store.upsert_risk_session(
                {
                    **base,
                    "current_equity": 5020,
                    "realized_net_pnl": 0,
                    "broker_confirmed_at": winner_time.isoformat(),
                    "broker_state": evidence("winner"),
                }
            )
            store.close()
            self.assertEqual(winner["loss_gauge"], 0)
            self.assertEqual(
                gate(
                    winner_time.isoformat(),
                    entry_check=True,
                    proposed_new_risk=85,
                    existing_open_downside=30,
                    existing_pending_risk=0,
                    execution_reserve=5,
                ),
                0,
            )
            self.assertIn('"open_loss_gauge_degradation_dollars": 10.0', gate.last_output)
            winner_authorization = json.loads(gate.last_output)[
                "risk_gate_authorization"
            ]["authorization_id"]
            store = Store(database)
            store.release_risk_authorization(winner_authorization, "unit_test_abort")
            store.close()

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
            store.close()
            self.assertEqual(
                gate(
                    objective_time.isoformat(),
                    entry_check=True,
                    proposed_new_risk=10,
                    existing_open_downside=20,
                    existing_pending_risk=0,
                    execution_reserve=5,
                ),
                0,
            )
            self.assertIn('"profit_floor_new_risk_capacity_dollars": 10.0', gate.last_output)
            objective_authorization = json.loads(gate.last_output)[
                "risk_gate_authorization"
            ]["authorization_id"]
            self.assertEqual(
                gate(
                    objective_time.isoformat(),
                    entry_check=True,
                    proposed_new_risk=10.01,
                    existing_open_downside=20,
                    existing_pending_risk=0,
                    execution_reserve=5,
                ),
                2,
            )
            store = Store(database)
            store.release_risk_authorization(objective_authorization, "unit_test_abort")
            store.close()
            store = Store(database)
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
