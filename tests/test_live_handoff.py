"""A1.4: deliberate controller handoff (manual takeover / resume) safety.

Proves the five guarantees of live/handoff.py with no broker, credential, or
network access: no overlapping-exit window, resume-only-after-fence, monotone
lease, preserved risk invariants, and single-owner-on-abort.
"""

from dataclasses import replace
from datetime import datetime, timezone
import unittest

from titan_brain.live.handoff import (
    HandoffError,
    HandoffPhase,
    RiskInvariants,
    abort,
    confirm_drained,
    request_takeover,
    resume,
    start_handoff,
    transfer_lease,
)


NOW = datetime(2026, 9, 18, 14, 0, tzinfo=timezone.utc)
INV = RiskInvariants(baseline_identity_sha256="a" * 64, loss_latched=False, unresolved_incident_count=0)


def _active():
    return start_handoff(account_key="ibkr-live-ending-3103", outgoing_owner_id="auto-1",
                         lease_generation=7, invariants=INV)


def _through_fenced():
    s = _active()
    s = request_takeover(s, incoming_owner_id="human-1", now=NOW)
    s = confirm_drained(s, in_flight_orders=0)
    return s


def _through_resumed():
    s = _through_fenced()
    s = transfer_lease(s, new_lease_generation=8, invariants=INV)
    s = resume(s, reconciled_fresh=True, invariants=INV)
    return s


class HandoffSafetyTests(unittest.TestCase):
    def test_happy_path_transfers_control_to_incoming_only(self):
        s = _through_resumed()
        self.assertIs(s.phase, HandoffPhase.RESUMED)
        self.assertEqual(s.controllers_that_may_issue_exits(), frozenset({"human-1"}))
        self.assertEqual(s.lease_generation, 8)

    def test_no_state_ever_lets_two_owners_issue_exits(self):
        # Walk every reachable phase; the exit-permitted owner set is always <=1.
        states = [_active()]
        s = request_takeover(states[0], incoming_owner_id="human-1", now=NOW)
        states.append(s)  # DRAINING
        s = confirm_drained(s, in_flight_orders=0)
        states.append(s)  # FENCED
        s = transfer_lease(s, new_lease_generation=8, invariants=INV)
        states.append(s)  # TRANSFERRED
        s = resume(s, reconciled_fresh=True, invariants=INV)
        states.append(s)  # RESUMED
        states.append(abort(_through_fenced(), surviving_owner_id="auto-1"))  # ABORTED
        for st in states:
            with self.subTest(phase=st.phase):
                self.assertLessEqual(len(st.controllers_that_may_issue_exits()), 1)
        # The fenced/draining/transferred windows permit NOBODY.
        self.assertEqual(states[1].controllers_that_may_issue_exits(), frozenset())  # DRAINING
        self.assertEqual(states[2].controllers_that_may_issue_exits(), frozenset())  # FENCED
        self.assertEqual(states[3].controllers_that_may_issue_exits(), frozenset())  # TRANSFERRED

    def test_incoming_cannot_resume_before_fence(self):
        s = request_takeover(_active(), incoming_owner_id="human-1", now=NOW)  # DRAINING
        with self.assertRaises(HandoffError):
            transfer_lease(s, new_lease_generation=8, invariants=INV)  # not FENCED yet
        with self.assertRaises(HandoffError):
            resume(s, reconciled_fresh=True, invariants=INV)

    def test_drain_incomplete_keeps_fence_open(self):
        s = request_takeover(_active(), incoming_owner_id="human-1", now=NOW)
        with self.assertRaises(HandoffError):
            confirm_drained(s, in_flight_orders=1)

    def test_lease_generation_must_strictly_increase(self):
        s = _through_fenced()
        for bad in (7, 6, 0, -1):
            with self.subTest(bad=bad), self.assertRaises(HandoffError):
                transfer_lease(s, new_lease_generation=bad, invariants=INV)

    def test_transfer_cannot_alter_risk_invariants(self):
        s = _through_fenced()
        for change in ({"loss_latched": True}, {"unresolved_incident_count": 1},
                       {"baseline_identity_sha256": "b" * 64}):
            with self.subTest(change=change), self.assertRaises(HandoffError):
                transfer_lease(s, new_lease_generation=8, invariants=replace(INV, **change))

    def test_resume_requires_fresh_reconciliation(self):
        s = transfer_lease(_through_fenced(), new_lease_generation=8, invariants=INV)
        with self.assertRaises(HandoffError):
            resume(s, reconciled_fresh=False, invariants=INV)

    def test_loss_latch_and_incidents_carry_across_handoff(self):
        latched = RiskInvariants("c" * 64, loss_latched=True, unresolved_incident_count=3)
        s = start_handoff(account_key="ibkr-live-ending-3103", outgoing_owner_id="auto-1",
                          lease_generation=7, invariants=latched)
        s = request_takeover(s, incoming_owner_id="human-1", now=NOW)
        s = confirm_drained(s, in_flight_orders=0)
        s = transfer_lease(s, new_lease_generation=8, invariants=latched)
        s = resume(s, reconciled_fresh=True, invariants=latched)
        self.assertTrue(s.invariants.loss_latched)
        self.assertEqual(s.invariants.unresolved_incident_count, 3)

    def test_abort_leaves_exactly_one_surviving_owner(self):
        s = _through_fenced()
        aborted = abort(s, surviving_owner_id="auto-1")
        self.assertIs(aborted.phase, HandoffPhase.ABORTED)
        self.assertIsNone(aborted.incoming_owner_id)
        self.assertEqual(aborted.controllers_that_may_issue_exits(), frozenset())
        # A survivor who was not party to the handoff is refused.
        with self.assertRaises(HandoffError):
            abort(s, surviving_owner_id="stranger")
        # No transition proceeds after abort (crash-mid-handoff fails closed).
        for fn in (lambda: confirm_drained(aborted, in_flight_orders=0),
                   lambda: transfer_lease(aborted, new_lease_generation=9, invariants=INV),
                   lambda: resume(aborted, reconciled_fresh=True, invariants=INV)):
            with self.assertRaises(HandoffError):
                fn()

    def test_owners_must_be_distinct(self):
        with self.assertRaises(HandoffError):
            request_takeover(_active(), incoming_owner_id="auto-1", now=NOW)


if __name__ == "__main__":
    unittest.main()
