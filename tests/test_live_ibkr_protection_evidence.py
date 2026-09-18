"""Synthetic protection facts only: never a broker connection or order write."""

from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from titan_brain.live.broker.base import (
    BrokerSide, EquityOrderType, FillSnapshot, MarketHours, OrderFamily,
    OrderSnapshot, PositionSnapshot, TimeInForce,
)
from titan_brain.live.broker.ibkr_protection_evidence import (
    IbkrProtectionEvidence, capture_ibkr_order_status_fact,
    capture_ibkr_protection_evidence, is_eligible_presubmitted_stop,
)
from titan_brain.live.broker.ibkr_read import IbkrWholeAccountReadBridge
from titan_brain.live.models import BrokerOrderState, ProtectionObligation, ProtectionState
from titan_brain.live.protection import assess_protection, is_verified_working_protection
from tests.test_live_ibkr_read import ACCOUNT, MASK, NOW, FakeReadRequester, order, state


D = Decimal


class IbkrProtectionEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.contract = SimpleNamespace(conId=123, symbol="TEST", secType="STK", currency="USD")
        self.raw = order(12, action="SELL", order_type="STP", quantity="3", tif="GTC", aux_price=9.5)
        self.raw.transmit = True
        self.raw.notHeld = False
        self.raw.goodAfterTime = ""
        self.raw.goodTillDate = ""

    def status(self, **changes):
        values = dict(order_id=12, client_id=7, perm_id=100, parent_id=0,
                      status="PreSubmitted", filled=D(0), remaining=D(3), why_held="", received_at=NOW)
        values.update(changes)
        return capture_ibkr_order_status_fact(**values)

    def evidence(self, **changes):
        values = dict(contract=self.contract, order=self.raw, broker_order_id="ibkr:7:12",
                      expected_account_id=ACCOUNT, account_masked=MASK, source="open",
                      open_order_status="PreSubmitted", blocking_warning_present=False,
                      open_order_received_at=NOW, status=self.status(), collection_started_at=NOW,
                      collection_completed_at=NOW)
        values.update(changes)
        return capture_ibkr_protection_evidence(**values)

    def snapshot(self, **changes):
        values = dict(broker_order_id="ibkr:7:12", account_masked=MASK, symbol="TEST",
                      side=BrokerSide.SELL, order_type=EquityOrderType.STOP_MARKET,
                      state=BrokerOrderState.QUEUED, requested_quantity=D(3), cumulative_filled_quantity=D(0),
                      market_hours=MarketHours.REGULAR, time_in_force=TimeInForce.GTC,
                      broker_updated_at=NOW, received_at=NOW, stop_price=D("9.5"),
                      broker_perm_id=100, broker_contract_id=123, ibkr_protection_evidence=self.evidence())
        values.update(changes)
        return OrderSnapshot(**values)

    def test_exact_standalone_presubmitted_stop_qualifies_without_state_upgrade(self):
        snapshot = self.snapshot()
        self.assertEqual(snapshot.state, BrokerOrderState.QUEUED)
        self.assertTrue(is_eligible_presubmitted_stop(snapshot))
        self.assertTrue(is_verified_working_protection(snapshot))

    def test_eligible_stop_counts_working_not_pending_and_still_cannot_oversell(self):
        obligation = ProtectionObligation(
            obligation_id="synthetic", source_fill_id="fill", account_key="ending-4567",
            symbol="TEST", required_quantity=3, stop_price=D("9.5"),
            state=ProtectionState.SUBMITTED, working_quantity=0, revision=0, updated_at=NOW,
        )
        result = assess_protection(
            position=PositionSnapshot("TEST", D(3), D(0), D(3)),
            orders=(self.snapshot(),), obligations=(obligation,),
        )
        self.assertTrue(result.protected)
        self.assertEqual(result.working_quantity, 3)
        self.assertEqual(result.pending_quantity, 0)
        result = assess_protection(
            position=PositionSnapshot("TEST", D(2), D(0), D(2)),
            orders=(self.snapshot(),), obligations=(obligation,),
        )
        self.assertFalse(result.protected)
        self.assertTrue(result.pause_new_entries)

    def test_no_evidence_or_missing_contract_cannot_promote_generic_queue(self):
        for changes in ({"ibkr_protection_evidence": None}, {"broker_contract_id": None}, {"broker_perm_id": None}):
            with self.subTest(changes=changes):
                self.assertFalse(is_verified_working_protection(self.snapshot(**changes)))

    def test_other_states_never_use_presubmitted_evidence_shortcut(self):
        for value in (BrokerOrderState.PENDING, BrokerOrderState.UNKNOWN, BrokerOrderState.UNCONFIRMED,
                      BrokerOrderState.PENDING_CANCELLED, BrokerOrderState.CANCELLED):
            with self.subTest(state=value):
                self.assertFalse(is_verified_working_protection(self.snapshot(state=value)))

    def test_any_unknown_or_unsafe_order_condition_blocks(self):
        flags = ("outside_rth", "include_overnight", "not_held", "oca_group_present", "conditions_present",
                 "algo_present", "hedge_present", "good_after_time_present", "good_till_date_present")
        for name in flags:
            for value in (True, None):
                with self.subTest(name=name, value=value):
                    evidence = replace(self.evidence(), **{name: value})
                    self.assertFalse(is_verified_working_protection(self.snapshot(ibkr_protection_evidence=evidence)))
        for value in (False, None):
            self.assertFalse(is_verified_working_protection(self.snapshot(ibkr_protection_evidence=replace(self.evidence(), transmit=value))))

    def test_warning_parent_held_missing_and_terminal_facts_block(self):
        cases = (
            {"blocking_warning_present": True}, {"parent_id": 11}, {"source": "completed"},
            {"open_order_status": "PendingSubmit"}, {"status": None},
            {"status": self.status(parent_id=11)}, {"status": self.status(why_held="locate")},
            {"status": self.status(why_held=None)}, {"status": self.status(status="Inactive")},
        )
        for values in cases:
            with self.subTest(values=values):
                evidence = replace(self.evidence(), **values)
                self.assertFalse(is_verified_working_protection(self.snapshot(ibkr_protection_evidence=evidence)))

    def test_exact_account_contract_symbol_and_all_order_identities_required(self):
        cases = (
            {"account_masked": "****9999"}, {"contract_id": 124}, {"symbol": "OTHER"},
            {"broker_order_id": "ibkr:8:12"}, {"perm_id": 101}, {"order_id": 13}, {"client_id": 8},
            {"status": self.status(perm_id=101)}, {"status": self.status(order_id=13)},
            {"status": self.status(client_id=8)}, {"security_type": "OPT"}, {"currency": "CAD"},
            {"action": "BUY"}, {"order_type": "STP LMT"}, {"time_in_force": "DAY"},
        )
        for values in cases:
            with self.subTest(values=values):
                self.assertFalse(is_verified_working_protection(self.snapshot(ibkr_protection_evidence=replace(self.evidence(), **values))))
        self.raw.account = "DU7654567"  # Same visible suffix is not exact binding.
        self.assertIsNone(self.evidence())

    def test_actual_fills_remaining_and_price_must_reconcile_exactly(self):
        for status in (self.status(remaining=D(0)), self.status(remaining=D(2)),
                       self.status(remaining=D("2.5")), self.status(filled=D(1)), self.status(filled=D("0.5"))):
            self.assertFalse(is_verified_working_protection(self.snapshot(ibkr_protection_evidence=replace(self.evidence(), status=status))))
        for values in ({"requested_quantity": D(4)}, {"stop_price": D(9)}, {"stop_price": D("NaN")}):
            self.assertFalse(is_verified_working_protection(self.snapshot(ibkr_protection_evidence=replace(self.evidence(), **values))))

    def test_partial_fill_requires_real_fill_and_exact_positive_remaining(self):
        fill = FillSnapshot("fill", D(1), D(10), NOW, broker_perm_id=100)
        evidence = replace(self.evidence(), status=self.status(filled=D(1), remaining=D(2)))
        snapshot = self.snapshot(fills=(fill,), cumulative_filled_quantity=D(1), ibkr_protection_evidence=evidence)
        self.assertTrue(is_verified_working_protection(snapshot))

    def test_status_and_open_order_receipts_are_fresh_and_same_collection(self):
        cases = (
            {"collection_started_at": NOW - timedelta(seconds=6)},
            {"collection_completed_at": NOW - timedelta(seconds=1)},
            {"collection_started_at": NOW + timedelta(seconds=1)},
            {"status": self.status(received_at=NOW - timedelta(seconds=1))},
            {"status": self.status(received_at=NOW + timedelta(seconds=1))},
            {"open_order_received_at": NOW - timedelta(seconds=1)},
        )
        for values in cases:
            with self.subTest(values=values):
                self.assertFalse(is_verified_working_protection(self.snapshot(ibkr_protection_evidence=replace(self.evidence(), **values))))

    def test_callback_copy_is_frozen_redacted_and_rejects_unknown_values(self):
        fact = self.status(why_held="synthetic private whyHeld text")
        self.assertTrue(fact.why_held_present)
        self.assertNotIn("synthetic private", repr(fact))
        with self.assertRaises(FrozenInstanceError):
            fact.remaining = D(0)
        for changes in ({"remaining": float("nan")}, {"filled": "NaN"}, {"order_id": True},
                        {"remaining": object()}, {"status": "private status"}, {"received_at": NOW.replace(tzinfo=None)}):
            self.assertIsNone(self.status(**changes))
        self.assertIsNone(self.status(why_held=object()).why_held_present)
        self.assertNotIn(ACCOUNT, repr(self.evidence()))

    def test_absent_raw_fields_are_unknown_never_safe_default(self):
        for field_name in ("transmit", "notHeld", "includeOvernight", "goodAfterTime", "conditions"):
            saved = getattr(self.raw, field_name)
            delattr(self.raw, field_name)
            self.assertFalse(is_verified_working_protection(self.snapshot()))
            setattr(self.raw, field_name, saved)
        self.raw.auxPrice = float("nan")
        self.assertIsNone(self.evidence())

    def test_base_boundary_rejects_untyped_evidence_or_invalid_contract_id(self):
        for value in ({}, True, "verified"):
            with self.assertRaises(ValueError):
                self.snapshot(ibkr_protection_evidence=value)
        for value in (True, 0, -1, "123"):
            with self.assertRaises(ValueError):
                self.snapshot(broker_contract_id=value)

    def collect(self, *, emit_status=True, status_changes=None, duplicate_changes=None, raw_changes=None, warning="", after_open=None, duplicate_order_changes=None):
        requester = FakeReadRequester()
        bridge = IbkrWholeAccountReadBridge(requester=requester, exact_account_id=ACCOUNT, account_masked=MASK,
                                          timeout_seconds=0.05, clock=lambda: NOW)
        callbacks = bridge.open_generation(1)
        requester.callbacks = callbacks
        callbacks.managedAccounts(ACCOUNT)
        for key, value in (raw_changes or {}).items():
            setattr(self.raw, key, value)

        def emit(values):
            data = dict(orderId=12, status="PreSubmitted", filled=D(0), remaining=D(3), avgFillPrice=0.0,
                        permId=100, parentId=0, lastFillPrice=0.0, clientId=7, whyHeld="")
            data.update(values)
            callbacks.orderStatus(**data)

        def open_orders():
            callbacks.openOrder(12, self.contract, self.raw, state("PreSubmitted", warning))
            if after_open is not None:
                after_open()
            if duplicate_order_changes is not None:
                for key, value in duplicate_order_changes.items():
                    setattr(self.raw, key, value)
                callbacks.openOrder(12, self.contract, self.raw, state("PreSubmitted", warning))
            if emit_status:
                emit(status_changes or {})
            if duplicate_changes is not None:
                emit(duplicate_changes)
            callbacks.openOrderEnd()

        with patch("socket.socket.connect", side_effect=AssertionError("network forbidden")), patch.object(requester, "reqAllOpenOrders", side_effect=open_orders):
            bridge.get_account_base(ACCOUNT)
        page = bridge.list_order_family_page(ACCOUNT, OrderFamily.STANDARD_EQUITY, None)
        return next(item for item in page.orders if item.broker_order_id == "ibkr:7:12")

    def test_real_bridge_copies_status_and_accepts_only_strict_stop(self):
        snapshot = self.collect()
        self.assertEqual(snapshot.state, BrokerOrderState.QUEUED)
        self.assertEqual(snapshot.broker_contract_id, 123)
        self.assertIsInstance(snapshot.ibkr_protection_evidence, IbkrProtectionEvidence)
        self.assertTrue(is_verified_working_protection(snapshot))
        self.raw.auxPrice = 1.0
        self.assertEqual(snapshot.ibkr_protection_evidence.stop_price, D("9.5"))

    def test_bridge_missing_or_malformed_status_cannot_promote_stop(self):
        self.assertFalse(is_verified_working_protection(self.collect(emit_status=False)))
        for changes in ({"remaining": D(2)}, {"remaining": None}, {"parentId": 11}, {"whyHeld": None}, {"permId": 0}):
            with self.subTest(changes=changes):
                self.assertFalse(is_verified_working_protection(self.collect(status_changes=changes)))

    def test_bridge_duplicate_equal_is_valid_conflict_or_warning_is_not(self):
        self.assertTrue(is_verified_working_protection(self.collect(duplicate_changes={})))
        self.assertFalse(is_verified_working_protection(self.collect(duplicate_changes={"remaining": D(2)})))
        self.assertFalse(is_verified_working_protection(self.collect(status_changes={"remaining": None}, duplicate_changes={})))
        self.assertFalse(is_verified_working_protection(self.collect(warning="synthetic warning")))

    def test_bridge_copies_order_facts_at_callback_not_after_sdk_object_mutation(self):
        snapshot = self.collect(raw_changes={"transmit": False}, after_open=lambda: setattr(self.raw, "transmit", True))
        self.assertIs(snapshot.ibkr_protection_evidence.transmit, False)
        self.assertFalse(is_verified_working_protection(snapshot))

    def test_same_collection_conflicting_open_order_facts_cannot_promote_stop(self):
        snapshot = self.collect(raw_changes={"transmit": False}, duplicate_order_changes={"transmit": True})
        self.assertIsNone(snapshot.ibkr_protection_evidence.status)
        self.assertFalse(is_verified_working_protection(snapshot))
        self.assertTrue(is_verified_working_protection(self.collect(duplicate_order_changes={})))


if __name__ == "__main__":
    unittest.main()
