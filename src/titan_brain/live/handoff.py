"""Deliberate broker-controller handoff (manual takeover / resume).

The account writer lock and the durable ``account_writer_lease`` already make it
impossible for two controllers to hold write authority at the same instant.
They do NOT, on their own, coordinate a *deliberate* transfer of control: an
outgoing autonomous controller could still have an exit in flight, or a stale
plan, at the moment a human operator (or a fresh autonomous process) wants to
take over. This module adds the missing protocol so a transfer cannot produce
competing exits, duplicate orders, overselling, or stale-plan reuse.

It is a PURE, monotone state machine, in the same spirit as
``session_trading_policy``: it authenticates nothing, reads no credentials,
opens no broker connection, and never issues an order. A caller binds each
transition to the real durable lease generation and to the preserved risk
state; this module only decides whether a transition is allowed and carries the
invariants forward unchanged.

State line (monotone; no backward edges):

    ACTIVE      one controller holds authority and may issue exits/entries
    DRAINING    outgoing controller stops issuing NEW orders, finishes in-flight
    FENCED      no controller may issue exits — the exit fence is closed
    TRANSFERRED incoming controller has taken the lease (generation strictly +1)
    RESUMED     incoming controller validated fresh reconciled state; may act
    ABORTED     terminal: the handoff failed closed; authority returns to the
                sole surviving owner recorded at abort (never two owners)

Guarantees enforced here and proven by tests:
  * an incoming controller can never reach RESUMED before the outgoing one is
    FENCED;
  * there is no state in which two distinct controllers may issue exits;
  * the frozen risk invariants (baseline identity, loss latch, unresolved
    incident count) are carried across a handoff unchanged — a takeover can
    never clear a latch or drop an incident;
  * a crash or abort mid-handoff leaves authority with exactly one owner;
  * the lease generation is strictly monotone across a completed transfer.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
import re


_TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,160}\Z", re.ASCII)


class HandoffError(ValueError):
    """Fixed-code handoff contract error; never carries private text."""


def _fail(reason: str) -> None:
    raise HandoffError("HANDOFF_" + reason)


def _token(value: object, code: str) -> None:
    if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
        _fail(code)


def _time(value: object) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _fail("TIME_INVALID")


class HandoffPhase(str, Enum):
    ACTIVE = "ACTIVE"
    DRAINING = "DRAINING"
    FENCED = "FENCED"
    TRANSFERRED = "TRANSFERRED"
    RESUMED = "RESUMED"
    ABORTED = "ABORTED"


# Which phases permit the CURRENT authority owner to issue exits. Deliberately
# excludes DRAINING (outgoing is winding down) and every post-fence phase until
# the incoming owner has RESUMED. FENCED/TRANSFERRED are exit-forbidden windows.
_MAY_ISSUE_EXITS = frozenset({HandoffPhase.ACTIVE, HandoffPhase.RESUMED})


@dataclass(frozen=True)
class RiskInvariants:
    """The safety facts a handoff must carry across unchanged.

    Sourced by the caller from the durable stores (session_trading_store /
    session_observation_ledger). This module never recomputes them; it only
    refuses any transition that would alter them.
    """

    baseline_identity_sha256: str
    loss_latched: bool
    unresolved_incident_count: int

    def __post_init__(self) -> None:
        _token(self.baseline_identity_sha256, "BASELINE_IDENTITY_INVALID")
        if len(self.baseline_identity_sha256) != 64:
            _fail("BASELINE_IDENTITY_INVALID")
        if type(self.loss_latched) is not bool:
            _fail("LOSS_LATCH_INVALID")
        if type(self.unresolved_incident_count) is not int or self.unresolved_incident_count < 0:
            _fail("INCIDENT_COUNT_INVALID")


@dataclass(frozen=True)
class HandoffState:
    account_key: str
    outgoing_owner_id: str
    incoming_owner_id: str | None
    lease_generation: int
    invariants: RiskInvariants
    phase: HandoffPhase = HandoffPhase.ACTIVE
    started_at: datetime | None = None

    def __post_init__(self) -> None:
        _token(self.account_key, "ACCOUNT_KEY_INVALID")
        _token(self.outgoing_owner_id, "OWNER_ID_INVALID")
        if self.incoming_owner_id is not None:
            _token(self.incoming_owner_id, "OWNER_ID_INVALID")
            if self.incoming_owner_id == self.outgoing_owner_id:
                _fail("OWNERS_NOT_DISTINCT")
        if isinstance(self.lease_generation, bool) or type(self.lease_generation) is not int or self.lease_generation <= 0:
            _fail("LEASE_GENERATION_INVALID")
        if type(self.invariants) is not RiskInvariants:
            _fail("INVARIANTS_INVALID")
        if type(self.phase) is not HandoffPhase:
            _fail("PHASE_INVALID")
        if self.started_at is not None:
            _time(self.started_at)
        # Post-request phases require a named incoming owner.
        if self.phase in (HandoffPhase.FENCED, HandoffPhase.TRANSFERRED, HandoffPhase.RESUMED) and self.incoming_owner_id is None:
            _fail("INCOMING_OWNER_REQUIRED")

    @property
    def outgoing_may_issue_exits(self) -> bool:
        """True only while the OUTGOING owner still legitimately holds control."""
        return self.phase is HandoffPhase.ACTIVE

    @property
    def incoming_may_issue_exits(self) -> bool:
        """True only after the incoming owner has fully RESUMED."""
        return self.phase is HandoffPhase.RESUMED

    def controllers_that_may_issue_exits(self) -> frozenset[str]:
        """The set of owner ids permitted to issue exits right now.

        Invariant: this set never contains more than one distinct owner.
        """
        if self.phase is HandoffPhase.ACTIVE:
            return frozenset({self.outgoing_owner_id})
        if self.phase is HandoffPhase.RESUMED and self.incoming_owner_id is not None:
            return frozenset({self.incoming_owner_id})
        # DRAINING, FENCED, TRANSFERRED, ABORTED: no owner may issue exits.
        return frozenset()


def start_handoff(
    *, account_key: str, outgoing_owner_id: str, lease_generation: int,
    invariants: RiskInvariants,
) -> HandoffState:
    """Begin in ACTIVE with a sole controller; no incoming owner yet."""
    return HandoffState(
        account_key=account_key, outgoing_owner_id=outgoing_owner_id,
        incoming_owner_id=None, lease_generation=lease_generation, invariants=invariants,
        phase=HandoffPhase.ACTIVE,
    )


def _require(state: HandoffState, expected: HandoffPhase) -> None:
    if type(state) is not HandoffState:
        _fail("STATE_INVALID")
    if state.phase is HandoffPhase.ABORTED:
        _fail("HANDOFF_ABORTED")
    if state.phase is not expected:
        _fail("PHASE_TRANSITION_INVALID")


def request_takeover(
    state: HandoffState, *, incoming_owner_id: str, now: datetime,
) -> HandoffState:
    """ACTIVE -> DRAINING. Outgoing must stop issuing NEW orders now."""
    _require(state, HandoffPhase.ACTIVE)
    _time(now)
    _token(incoming_owner_id, "OWNER_ID_INVALID")
    if incoming_owner_id == state.outgoing_owner_id:
        _fail("OWNERS_NOT_DISTINCT")
    return replace(state, incoming_owner_id=incoming_owner_id, phase=HandoffPhase.DRAINING, started_at=now)


def confirm_drained(state: HandoffState, *, in_flight_orders: int) -> HandoffState:
    """DRAINING -> FENCED. Only when the outgoing controller has no in-flight order.

    A non-zero in-flight count keeps the state in DRAINING (fail closed): the
    exit fence must not close while an order the outgoing owner launched could
    still act.
    """
    _require(state, HandoffPhase.DRAINING)
    if isinstance(in_flight_orders, bool) or type(in_flight_orders) is not int or in_flight_orders < 0:
        _fail("IN_FLIGHT_COUNT_INVALID")
    if in_flight_orders != 0:
        _fail("DRAIN_INCOMPLETE")
    return replace(state, phase=HandoffPhase.FENCED)


def transfer_lease(
    state: HandoffState, *, new_lease_generation: int, invariants: RiskInvariants,
) -> HandoffState:
    """FENCED -> TRANSFERRED. Incoming takes the lease at a strictly greater generation.

    The risk invariants observed by the incoming owner must EQUAL the ones
    carried into the handoff — a transfer can never clear a loss latch, change
    the baseline identity, or drop an unresolved incident.
    """
    _require(state, HandoffPhase.FENCED)
    if isinstance(new_lease_generation, bool) or type(new_lease_generation) is not int:
        _fail("LEASE_GENERATION_INVALID")
    if new_lease_generation <= state.lease_generation:
        _fail("LEASE_NOT_MONOTONE")
    if type(invariants) is not RiskInvariants or invariants != state.invariants:
        _fail("INVARIANTS_NOT_PRESERVED")
    return replace(state, lease_generation=new_lease_generation, phase=HandoffPhase.TRANSFERRED)


def resume(
    state: HandoffState, *, reconciled_fresh: bool, invariants: RiskInvariants,
) -> HandoffState:
    """TRANSFERRED -> RESUMED. Incoming may act only after fresh reconciliation.

    ``reconciled_fresh`` is the caller's proof (from the durable stores) that
    the incoming controller re-read and reconciled current exposure. Anything
    short of a genuine fresh reconciliation fails closed and the incoming owner
    stays exit-fenced.
    """
    _require(state, HandoffPhase.TRANSFERRED)
    if type(reconciled_fresh) is not bool:
        _fail("RECONCILE_FLAG_INVALID")
    if type(invariants) is not RiskInvariants or invariants != state.invariants:
        _fail("INVARIANTS_NOT_PRESERVED")
    if not reconciled_fresh:
        _fail("RESUME_REQUIRES_FRESH_RECONCILIATION")
    return replace(state, phase=HandoffPhase.RESUMED)


def abort(state: HandoffState, *, surviving_owner_id: str) -> HandoffState:
    """Any non-terminal phase -> ABORTED, leaving exactly ONE surviving owner.

    The surviving owner must be one of the two parties to the handoff. Control
    returns to that single owner; the invariants are preserved so a failed
    takeover can never be used to reset risk state.
    """
    if type(state) is not HandoffState:
        _fail("STATE_INVALID")
    if state.phase is HandoffPhase.ABORTED:
        _fail("HANDOFF_ABORTED")
    _token(surviving_owner_id, "OWNER_ID_INVALID")
    if surviving_owner_id not in {state.outgoing_owner_id, state.incoming_owner_id}:
        _fail("SURVIVING_OWNER_INVALID")
    # Record the survivor as the outgoing (sole) owner; clear any incoming.
    return replace(state, outgoing_owner_id=surviving_owner_id, incoming_owner_id=None,
                   phase=HandoffPhase.ABORTED)
