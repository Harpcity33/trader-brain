"""Area 5 (A5-c): bind the handoff FSM to session risk state.

Proves the pure adapter builds RiskInvariants from a SessionTradingState (with
the non-COMPLETED incident count) and drives a deliberate recovery takeover
through the fenced FSM, carrying baseline identity / loss latch / incident count
across unchanged and failing closed on a non-drained or stale takeover. No
store, lease, broker, or credential access. Lifts no gate.
"""

from dataclasses import replace
from datetime import timedelta
import unittest

from titan_brain.live.handoff import HandoffError, HandoffPhase
from titan_brain.live.handoff_binding import (
    begin_recovery_handoff,
    risk_invariants_from_session,
    unresolved_incident_count,
)
from titan_brain.live.session_trading_policy import (
    ObservationStatus, begin_observation, fail_observation,
)
from tests import test_live_session_trading_policy as sf


NOW = sf.START


class HandoffBindingTests(unittest.TestCase):
    def _clean_state(self):
        # A healthy observed session (no unresolved incidents, not latched).
        return sf.observe(sf.initial())

    def test_risk_invariants_from_a_clean_session(self):
        state = self._clean_state()
        inv = risk_invariants_from_session(state)
        self.assertEqual(inv.baseline_identity_sha256, state.baseline.identity_sha256)
        self.assertFalse(inv.loss_latched)
        self.assertEqual(inv.unresolved_incident_count, 0)

    def test_unresolved_count_includes_pending_and_failed(self):
        # A pending (begun, not completed) observation is unresolved.
        pending = begin_observation(sf.initial(), token="pending-1", now=NOW)
        self.assertEqual(unresolved_incident_count(pending), 1)
        # A failed/gap incident is unresolved and sticky.
        failed = fail_observation(pending, token="pending-1", gap=True)
        self.assertEqual(unresolved_incident_count(failed), 1)
        self.assertIs(failed.incidents[0].status, ObservationStatus.FAILED)

    def test_full_recovery_handoff_carries_invariants_and_transfers_control(self):
        state = self._clean_state()
        result = begin_recovery_handoff(
            account_key="ibkr-live-ending-3103",
            outgoing_owner_id="auto-1", incoming_owner_id="human-1",
            outgoing_generation=7, incoming_generation=8,
            session_state=state, in_flight_orders=0, reconciled_fresh=True, now=NOW,
        )
        self.assertIs(result.phase, HandoffPhase.RESUMED)
        self.assertEqual(result.controllers_that_may_issue_exits(), frozenset({"human-1"}))
        self.assertEqual(result.lease_generation, 8)
        # Invariants match the session state, carried across unchanged.
        self.assertEqual(result.invariants, risk_invariants_from_session(state))

    def test_takeover_with_in_flight_orders_fails_closed(self):
        with self.assertRaises(HandoffError):
            begin_recovery_handoff(
                account_key="ibkr-live-ending-3103",
                outgoing_owner_id="auto-1", incoming_owner_id="human-1",
                outgoing_generation=7, incoming_generation=8,
                session_state=self._clean_state(), in_flight_orders=1,
                reconciled_fresh=True, now=NOW,
            )

    def test_takeover_without_fresh_reconciliation_fails_closed(self):
        with self.assertRaises(HandoffError):
            begin_recovery_handoff(
                account_key="ibkr-live-ending-3103",
                outgoing_owner_id="auto-1", incoming_owner_id="human-1",
                outgoing_generation=7, incoming_generation=8,
                session_state=self._clean_state(), in_flight_orders=0,
                reconciled_fresh=False, now=NOW,
            )

    def test_non_monotone_generation_fails_closed(self):
        with self.assertRaises(HandoffError):
            begin_recovery_handoff(
                account_key="ibkr-live-ending-3103",
                outgoing_owner_id="auto-1", incoming_owner_id="human-1",
                outgoing_generation=8, incoming_generation=8,
                session_state=self._clean_state(), in_flight_orders=0,
                reconciled_fresh=True, now=NOW,
            )

    def test_adapter_rejects_non_session_state(self):
        with self.assertRaises(TypeError):
            risk_invariants_from_session(object())


if __name__ == "__main__":
    unittest.main()
