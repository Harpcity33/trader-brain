from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from titan_runtime.massive import TitanWatcher
from titan_runtime.storage import Store


class MassiveWatcherTests(unittest.TestCase):
    @staticmethod
    def _candidate() -> dict:
        return {
            "pilot_id": "titan_momentum_equity",
            "book_mode": "SHADOW",
            "decision_contract_version": "titan_momentum_equity_2026-08-23_v1",
            "decision_contract_hash": "dba59bd7fb006e5c0ec8fd31fb467076638ba6de9e30442fdc5411449c4d7c21",
            "trade_authority": False,
            "broker_authority": False,
            "risk_authorization_authority": False,
            "buying_power_reservation_authority": False,
            "capital_allocation_authority": False,
            "symbol": "TEST",
            "observed_at": "2026-08-24T14:00:00+00:00",
            "state": "BREAKOUT",
            "lane": "under5",
            "signal_strength": 70,
            "price": 4.55,
            "base_high": 4.60,
            "base_end_ms": 1,
            "support": 4.40,
            "invalidation": 4.40,
            "limit_ceiling": 4.65,
            "direction": "UP",
            "disposition": "ENTRY_CANDIDATE",
        }

    def test_halt_synchronously_disarms_candidate_and_blocks_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "test.sqlite3")
            watcher = TitanWatcher.__new__(TitanWatcher)
            watcher.store = store
            watcher.config = SimpleNamespace(
                timezone="America/New_York",
                under5_max_quote_spread_pct=0.9375,
                max_quote_spread_pct=0.625,
                max_spread_to_structural_risk=0.15,
            )
            watcher.crossed_triggers = {"TEST": 4.60}
            emitted: list[str] = []
            watcher.emit = lambda event_type, *_args, **_kwargs: emitted.append(event_type)

            store.upsert_candidate(self._candidate())
            halt_ms = int(
                datetime.fromisoformat("2026-08-24T10:00:30-04:00")
                .astimezone(timezone.utc).timestamp() * 1000
            )
            watcher._handle_luld({"T": "TEST", "t": halt_ms, "i": [17]})
            self.assertIsNone(store.get_candidate("TEST"))
            self.assertNotIn("TEST", watcher.crossed_triggers)

            # Even if stale state is accidentally reintroduced, the synchronous
            # second-bar path must consult the durable halt state and fail closed.
            store.upsert_candidate(self._candidate())
            watcher._handle_second(
                {
                    "sym": "TEST",
                    "s": halt_ms + 1_000,
                    "e": halt_ms + 1_999,
                    "o": 4.59,
                    "h": 4.62,
                    "l": 4.58,
                    "c": 4.61,
                    "v": 1_000,
                }
            )
            self.assertNotIn("TRIGGER_CROSS", emitted)
            store.close()

    def test_payload_attribution_is_explicit_zero_authority_and_fail_closed(self) -> None:
        watcher = TitanWatcher.__new__(TitanWatcher)
        watcher.config = SimpleNamespace(
            pilot_id="titan_momentum_equity",
            book_mode="SHADOW",
            decision_contract_version="titan_momentum_equity_2026-08-23_v1",
            decision_contract_hash=(
                "dba59bd7fb006e5c0ec8fd31fb467076638ba6de9e30442fdc5411449c4d7c21"
            ),
        )
        attributed = watcher._attribute_payload({"symbol": "TEST"})
        self.assertEqual(attributed["pilot_id"], "titan_momentum_equity")
        self.assertEqual(attributed["book_mode"], "SHADOW")
        for field in (
            "trade_authority",
            "broker_authority",
            "risk_authorization_authority",
            "buying_power_reservation_authority",
            "capital_allocation_authority",
        ):
            self.assertFalse(attributed[field])
        with self.assertRaisesRegex(ValueError, "contradictory trade_authority"):
            watcher._attribute_payload({"trade_authority": True})


if __name__ == "__main__":
    unittest.main()
