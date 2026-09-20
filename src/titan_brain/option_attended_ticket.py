"""Bound, attended option review ticket (offline).

Milestone 6 of the attended-options plan.

An attended ticket freezes EVERY material fact of a proposed option trade --
the exact contract, side, quantity, limit price, time-in-force, expiry /
overnight implication, all three loss figures, the accepted budgets, fees, the
age of the data it was built on, and the account/exposure fingerprint -- into a
single canonical record with a deterministic fingerprint and a short validity
window.  The owner's approval is bound to that fingerprint: if ANY material
fact changes (a requote, a quantity change, a budget change, a new position, a
different account/exposure), the prior approval is INVALIDATED and a fresh
ticket must be reviewed.  There is no silent widening, averaging down, or retry
that could duplicate an order.

This module is pure and offline: it builds and revalidates tickets and records
whether an approval still binds.  It performs NO broker submission and carries
NO live authority -- wiring it to an actual (paper, then live) submission path
is a later milestone.  An approved, still-valid ticket is a *precondition* for
review, never an instruction to trade.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Mapping


TICKET_SCHEMA = "attended_option_ticket_v1"


class TicketError(ValueError):
    """A well-formed, non-secret ticket error."""


def _text(value: object, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise TicketError(f"{field_name} must be a non-empty string")
    return value


def _aware(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TicketError(f"{field_name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class AttendedOptionTicket:
    """Canonical, fingerprinted set of material facts for one option trade.

    ``material_facts`` is a flat mapping of str -> (str|int|bool) values only
    (money is passed as strings), so the fingerprint is exact and float-free.
    ``account_exposure_fingerprint`` binds the ticket to the account state it
    was reviewed against.  ``issued_at`` + ``validity_seconds`` define the
    window in which an approval can bind.
    """

    contract_wire_fingerprint: str
    account_exposure_fingerprint: str
    material_facts: Mapping[str, object]
    issued_at: datetime
    validity_seconds: int

    def __post_init__(self) -> None:
        _text(self.contract_wire_fingerprint, "contract_wire_fingerprint")
        _text(self.account_exposure_fingerprint, "account_exposure_fingerprint")
        if not isinstance(self.material_facts, Mapping) or not self.material_facts:
            raise TicketError("material_facts must be a non-empty mapping")
        for key, value in self.material_facts.items():
            if type(key) is not str:
                raise TicketError("material_facts keys must be strings")
            if type(value) not in (str, int, bool):
                raise TicketError(
                    f"material_facts[{key!r}] must be str/int/bool (money as string)"
                )
        _aware(self.issued_at, "issued_at")
        if type(self.validity_seconds) is not int or isinstance(self.validity_seconds, bool) \
                or not 1 <= self.validity_seconds <= 900:
            raise TicketError("validity_seconds must be an integer in [1, 900]")

    def _canonical(self) -> Mapping[str, object]:
        return {
            "schema": TICKET_SCHEMA,
            "contract_wire_fingerprint": self.contract_wire_fingerprint,
            "account_exposure_fingerprint": self.account_exposure_fingerprint,
            "issued_at": self.issued_at.astimezone(timezone.utc).isoformat(),
            "validity_seconds": self.validity_seconds,
            "material_facts": {key: self.material_facts[key] for key in sorted(self.material_facts)},
        }

    def fingerprint(self) -> str:
        """Deterministic sha256 over every material fact + binding fields."""

        return hashlib.sha256(
            json.dumps(
                self._canonical(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode("utf-8")
        ).hexdigest()

    def expires_at(self) -> datetime:
        return self.issued_at.astimezone(timezone.utc) + timedelta(seconds=self.validity_seconds)

    def is_within_validity(self, *, now: datetime) -> bool:
        current = _aware(now, "now")
        issued = self.issued_at.astimezone(timezone.utc)
        return issued <= current < self.expires_at()


@dataclass(frozen=True)
class TicketApproval:
    """An owner approval bound to a specific ticket fingerprint."""

    ticket_fingerprint: str
    approved_at: datetime

    def __post_init__(self) -> None:
        _text(self.ticket_fingerprint, "ticket_fingerprint")
        _aware(self.approved_at, "approved_at")


@dataclass(frozen=True)
class ApprovalCheck:
    binds: bool
    reasons: tuple[str, ...]


def approve_ticket(ticket: AttendedOptionTicket, *, now: datetime) -> TicketApproval:
    """Record an approval bound to the ticket's current fingerprint.

    Refuses to approve outside the validity window (fail closed).
    """

    if not isinstance(ticket, AttendedOptionTicket):
        raise TicketError("ticket must be an AttendedOptionTicket")
    current = _aware(now, "now")
    if not ticket.is_within_validity(now=current):
        raise TicketError("cannot approve a ticket outside its validity window")
    return TicketApproval(ticket_fingerprint=ticket.fingerprint(), approved_at=current)


def approval_still_binds(
    *, approval: TicketApproval, current_ticket: AttendedOptionTicket, now: datetime
) -> ApprovalCheck:
    """Check whether a prior approval still binds the CURRENT ticket facts.

    The approval binds only if the current ticket fingerprints identically to
    what was approved AND the ticket is still within its validity window.  Any
    material change (requote, quantity, budget, position, account/exposure)
    changes the fingerprint and invalidates the approval -- there is no silent
    widening or reuse.
    """

    if not isinstance(approval, TicketApproval):
        raise TicketError("approval must be a TicketApproval")
    if not isinstance(current_ticket, AttendedOptionTicket):
        raise TicketError("current_ticket must be an AttendedOptionTicket")
    current = _aware(now, "now")
    reasons: list[str] = []
    if current_ticket.fingerprint() != approval.ticket_fingerprint:
        reasons.append("MATERIAL_FACTS_CHANGED_APPROVAL_INVALID")
    if not current_ticket.is_within_validity(now=current):
        reasons.append("TICKET_VALIDITY_WINDOW_EXPIRED")
    return ApprovalCheck(binds=not reasons, reasons=tuple(reasons))


__all__ = [
    "ApprovalCheck",
    "AttendedOptionTicket",
    "TicketApproval",
    "TicketError",
    "TICKET_SCHEMA",
    "approval_still_binds",
    "approve_ticket",
]
