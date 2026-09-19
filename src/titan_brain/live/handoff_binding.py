"""Bind the pure controller-handoff FSM to session risk state.

This is the adapter layer between the durable session risk state
(`SessionTradingState`) and the pure handoff state machine (`handoff.py`). It
translates the state's risk facts into the FSM's `RiskInvariants` and, given an
already-read outgoing/incoming writer-lease generation pair, drives a deliberate
controller takeover through the FSM's fenced sequence.

It is PURE: it reads attributes off objects it is handed and calls the pure FSM.
It opens no store, acquires no lease, contacts no broker, and reads no
credential — the caller supplies the session state and the lease generations
(both already obtained on the live path). It therefore lifts no readiness gate;
SESSION_TRADING_RUNTIME_INTEGRATION_UNAVAILABLE stays closed because the actual
lease<->session-store seam on the live recovery path is deliberately NOT rewired
here (that remains a later, owner-visible integration step).
"""

from __future__ import annotations

from datetime import datetime

from .handoff import (
    HandoffState,
    RiskInvariants,
    confirm_drained,
    request_takeover,
    resume,
    start_handoff,
    transfer_lease,
)
from .session_trading_policy import ObservationStatus, SessionTradingState


def unresolved_incident_count(state: SessionTradingState) -> int:
    """Count incidents that are not COMPLETED (PENDING + FAILED are unresolved)."""
    if type(state) is not SessionTradingState:
        raise TypeError("a SessionTradingState is required")
    return sum(1 for i in state.incidents if i.status is not ObservationStatus.COMPLETED)


def risk_invariants_from_session(state: SessionTradingState) -> RiskInvariants:
    """Build the FSM's RiskInvariants from the durable session risk state.

    Reads the exact facts a handoff must carry across unchanged: the baseline
    identity, the loss latch, and the count of unresolved (non-COMPLETED)
    observation incidents. Does not recompute or mutate anything.
    """
    if type(state) is not SessionTradingState:
        raise TypeError("a SessionTradingState is required")
    return RiskInvariants(
        baseline_identity_sha256=state.baseline.identity_sha256,
        loss_latched=state.loss_latched,
        unresolved_incident_count=unresolved_incident_count(state),
    )


def begin_recovery_handoff(
    *,
    account_key: str,
    outgoing_owner_id: str,
    incoming_owner_id: str,
    outgoing_generation: int,
    incoming_generation: int,
    session_state: SessionTradingState,
    in_flight_orders: int,
    reconciled_fresh: bool,
    now: datetime,
) -> HandoffState:
    """Drive a deliberate controller takeover through the fenced FSM sequence.

    All inputs are already-read values from the live path: the writer-lease
    generation pair (outgoing, incoming=+1 from acquire_writer_lease's RECOVERED
    branch), the session risk state (for invariants), the outgoing controller's
    in-flight order count (must be 0 to fence), and the incoming controller's
    fresh-reconciliation proof. Runs start -> request_takeover -> confirm_drained
    -> transfer_lease -> resume. Every safety property (no overlapping exits, no
    resume before fence, monotone generation, invariants carried unchanged) is
    enforced by the pure FSM and its tests. Returns the resulting HandoffState;
    fail-closed HandoffError propagates from the FSM on any violation.

    Pure: no store, lease, broker, or credential access. The live seam that
    would CALL this at the acquire_writer_lease RECOVERED branch is a separate,
    later integration step; this function only decides the transition.
    """
    invariants = risk_invariants_from_session(session_state)
    handoff = start_handoff(
        account_key=account_key,
        outgoing_owner_id=outgoing_owner_id,
        lease_generation=outgoing_generation,
        invariants=invariants,
    )
    handoff = request_takeover(handoff, incoming_owner_id=incoming_owner_id, now=now)
    handoff = confirm_drained(handoff, in_flight_orders=in_flight_orders)
    handoff = transfer_lease(handoff, new_lease_generation=incoming_generation, invariants=invariants)
    handoff = resume(handoff, reconciled_fresh=reconciled_fresh, invariants=invariants)
    return handoff
