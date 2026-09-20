"""Tests for the bound attended option review ticket (milestone 6)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from titan_brain.option_attended_ticket import (
    AttendedOptionTicket,
    TicketError,
    approval_still_binds,
    approve_ticket,
)


NOW = datetime(2026, 10, 5, 16, 0, 0, tzinfo=timezone.utc)


def _facts(**overrides):
    facts = {
        "symbol": "TEST",
        "right": "C",
        "strike": "12.50",
        "expiry": "20261014",
        "side": "BUY",
        "quantity": 2,
        "limit_price": "1.65",
        "time_in_force": "DAY",
        "overnight_hold": True,
        "planned_loss": "200",
        "stress_loss": "600",
        "premium_exposure": "1500",
        "max_planned_loss": "500",
        "max_stress_loss": "1000",
        "max_premium_exposure": "2000",
        "entry_fees": "1.30",
        "data_age_seconds": 3,
    }
    facts.update(overrides)
    return facts


def _ticket(**overrides):
    fields = dict(
        contract_wire_fingerprint="a" * 64,
        account_exposure_fingerprint="b" * 64,
        material_facts=_facts(),
        issued_at=NOW,
        validity_seconds=60,
    )
    if "facts" in overrides:
        fields["material_facts"] = overrides.pop("facts")
    fields.update(overrides)
    return AttendedOptionTicket(**fields)


class AttendedTicketTests(unittest.TestCase):
    def test_fingerprint_stable_and_hex(self) -> None:
        t = _ticket()
        self.assertRegex(t.fingerprint(), r"^[0-9a-f]{64}$")
        self.assertEqual(t.fingerprint(), _ticket().fingerprint())

    def test_approval_binds_when_unchanged_and_in_window(self) -> None:
        t = _ticket()
        approval = approve_ticket(t, now=NOW + timedelta(seconds=5))
        check = approval_still_binds(approval=approval, current_ticket=t, now=NOW + timedelta(seconds=10))
        self.assertTrue(check.binds)
        self.assertEqual(check.reasons, ())

    def test_quantity_change_invalidates_approval(self) -> None:
        t = _ticket()
        approval = approve_ticket(t, now=NOW)
        changed = _ticket(facts=_facts(quantity=3))
        check = approval_still_binds(approval=approval, current_ticket=changed, now=NOW + timedelta(seconds=5))
        self.assertFalse(check.binds)
        self.assertIn("MATERIAL_FACTS_CHANGED_APPROVAL_INVALID", check.reasons)

    def test_requote_limit_change_invalidates(self) -> None:
        t = _ticket()
        approval = approve_ticket(t, now=NOW)
        changed = _ticket(facts=_facts(limit_price="1.70"))
        self.assertFalse(approval_still_binds(approval=approval, current_ticket=changed, now=NOW).binds)

    def test_budget_change_invalidates(self) -> None:
        t = _ticket()
        approval = approve_ticket(t, now=NOW)
        changed = _ticket(facts=_facts(max_stress_loss="800"))
        self.assertFalse(approval_still_binds(approval=approval, current_ticket=changed, now=NOW).binds)

    def test_account_exposure_change_invalidates(self) -> None:
        t = _ticket()
        approval = approve_ticket(t, now=NOW)
        changed = _ticket(account_exposure_fingerprint="c" * 64)
        self.assertFalse(approval_still_binds(approval=approval, current_ticket=changed, now=NOW).binds)

    def test_contract_change_invalidates(self) -> None:
        t = _ticket()
        approval = approve_ticket(t, now=NOW)
        changed = _ticket(contract_wire_fingerprint="d" * 64)
        self.assertFalse(approval_still_binds(approval=approval, current_ticket=changed, now=NOW).binds)

    def test_expired_window_invalidates(self) -> None:
        t = _ticket(validity_seconds=30)
        approval = approve_ticket(t, now=NOW)
        check = approval_still_binds(approval=approval, current_ticket=t, now=NOW + timedelta(seconds=31))
        self.assertFalse(check.binds)
        self.assertIn("TICKET_VALIDITY_WINDOW_EXPIRED", check.reasons)

    def test_cannot_approve_outside_window(self) -> None:
        t = _ticket(validity_seconds=30)
        with self.assertRaisesRegex(TicketError, "validity window"):
            approve_ticket(t, now=NOW + timedelta(seconds=31))

    def test_float_in_material_facts_rejected(self) -> None:
        with self.assertRaises(TicketError):
            _ticket(facts=_facts(limit_price=1.65))  # float not allowed

    def test_empty_facts_rejected(self) -> None:
        with self.assertRaises(TicketError):
            _ticket(facts={})

    def test_validity_bounds_enforced(self) -> None:
        with self.assertRaises(TicketError):
            _ticket(validity_seconds=0)
        with self.assertRaises(TicketError):
            _ticket(validity_seconds=901)

    def test_naive_now_rejected(self) -> None:
        t = _ticket()
        with self.assertRaises(TicketError):
            approve_ticket(t, now=datetime(2026, 10, 5, 16, 0, 0))


if __name__ == "__main__":
    unittest.main()
