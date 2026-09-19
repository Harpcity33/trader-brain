"""Synthetic same-collection exposure reads; no broker, credentials or orders."""

from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from decimal import Decimal
import json
import unittest
from unittest.mock import patch

from titan_brain.live.broker.base import BrokerCapabilityError, BrokerContractViolation, OrderFamily
from titan_brain.live.broker.ibkr_read import IbkrFiniteSessionExposure
from titan_brain.live.broker.ibkr_session_inputs import (
    IbkrFiniteSessionFacts, IbkrSessionInputError, SessionInputExposureObservation, SessionInputObservation,
)
from tests import test_live_ibkr_session_inputs as fixtures


D = Decimal


class SessionExposureTests(unittest.TestCase):
    setUp = fixtures.IbkrSessionInputsTests.setUp
    connect = fixtures.IbkrSessionInputsTests.connect
    fill = fixtures.IbkrSessionInputsTests.fill

    def test_route_is_inert_until_connected_and_never_calls_capture_seam(self):
        with self.assertRaises(IbkrSessionInputError):
            self.adapter.capture_with_exposure()
        self.loader.assert_not_called()
        self.connect()
        with patch.object(self.adapter, "capture", side_effect=AssertionError("old capture seam")):
            captured = self.adapter.capture_with_exposure()
        self.assertIs(type(captured), SessionInputExposureObservation)

    def test_actual_same_collection_pages_and_unpromoted_base_are_immutable(self):
        self.connect()
        self.client.position_quantity = D(1)
        raw = self.fill()
        captured = self.adapter.capture_with_exposure()
        exposure, bound = captured.exposure, captured.observation
        self.assertIs(bound.facts, exposure.facts)
        self.assertEqual(bound.account_binding_fingerprint, self.runtime.account_binding_fingerprint)
        self.assertEqual(bound.facts.generation, self.runtime.status().read_generation)
        snapshot = exposure.observation.snapshot
        self.assertEqual(snapshot.funds.total_value, D(10000))
        self.assertEqual(snapshot.funds.cash, D(10000))
        self.assertEqual(snapshot.account_type, "CASH")
        self.assertEqual(snapshot.equity_positions[0].quantity, D(1))
        self.assertEqual(snapshot.equity_orders, ())
        self.assertEqual(tuple(page.family for page in exposure.order_family_pages), tuple(OrderFamily))
        for page in exposure.order_family_pages:
            self.assertEqual(page.collection_id, bound.facts.collection_id)
            self.assertEqual(page.observed_at, bound.facts.collection_completed_at)
            self.assertEqual(page.received_at, bound.facts.collection_completed_at)
            self.assertIsNone(page.snapshot_token)
            self.assertIsNone(page.next_cursor)
        orders = tuple(order for page in exposure.order_family_pages for order in page.orders)
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0].fills[0].provider_commission, D("-0.25"))
        self.assertEqual(orders[0].fills[0].provider_commission_currency, "USD")
        for value, attr in ((exposure, "facts"), (snapshot, "daily_realized_pnl"), (exposure.order_family_pages[0], "collection_id"), (orders[0].fills[0], "price")):
            with self.assertRaises(FrozenInstanceError):
                setattr(value, attr, None)
        raw.price = D(999)
        self.client.raw_contract.symbol = "CHANGED"
        self.assertEqual(orders[0].fills[0].price, D(10))
        self.assertEqual(bound.facts.positions[0].symbol, "TEST")
        self.assertEqual(exposure.timing_basis, "local_receipt_not_atomic_broker_valuation")
        self.assertTrue(exposure.diagnostic_only)
        self.assertFalse(exposure.live_authority)
        self.assertNotIn("reqPnL", [call[0] for call in self.client.calls])
        self.assertFalse(self.runtime.status().command_connected)
        self.assertEqual(self.client.mutations, [])

    def test_no_legacy_risk_values_receipts_flags_or_readiness_escape(self):
        self.connect()
        snapshot = self.adapter.capture_with_exposure().exposure.observation.snapshot
        for name in (
            "daily_realized_pnl", "weekly_realized_pnl", "peak_equity", "daily_starting_equity",
            "daily_external_cash_flow", "risk_evidence_source", "risk_evidence_as_of",
            "risk_baseline_identity_hash", "risk_baseline_receipt_hash", "risk_high_water_identity_hash",
            "risk_high_water_lineage_hash", "risk_high_water_receipt_hash", "daily_starting_equity_as_of",
            "daily_external_cash_flow_as_of", "daily_starting_equity_receipt_hash", "daily_external_cash_flow_receipt_hash",
        ):
            self.assertIsNone(getattr(snapshot, name), name)
        for name in (
            "daily_realized_pnl_complete", "weekly_realized_pnl_complete", "peak_equity_complete",
            "risk_evidence_authoritative", "daily_realized_pnl_ready", "daily_starting_equity_ready",
            "entry_risk_evidence_ready", "authenticated_entry_risk_evidence_ready",
            "standard_equity_orders_complete", "option_orders_complete", "advanced_orders_complete",
        ):
            self.assertIs(getattr(snapshot, name), False, name)

    def test_old_strict_cache_pages_references_and_valuation_stay_unavailable(self):
        self.connect()
        bridge = self.components.read_bridge
        with patch.object(self.client, "reqPnL", side_effect=lambda req, account, model: self.client.wrapper.pnl(req, 0.0, 0.0, 0.0)):
            old = bridge.get_account_base(fixtures.SYNTHETIC_ACCOUNT)
        captured = self.adapter.capture_with_exposure()
        self.assertNotEqual(captured.exposure.facts.collection_id, old.collection_id)
        self.assertIsNone(bridge._last)
        with self.assertRaises(BrokerCapabilityError):
            bridge.list_order_family_page(fixtures.SYNTHETIC_ACCOUNT, OrderFamily.STANDARD_EQUITY, None)
        with self.assertRaises(BrokerCapabilityError):
            bridge.lookup_equity_orders_by_client_ref(fixtures.SYNTHETIC_ACCOUNT, ())
        with self.assertRaises(BrokerContractViolation):
            bridge.position_valuation_inputs(captured.exposure.facts.collection_id)
        with self.assertRaises(BrokerCapabilityError):
            bridge.get_account_base(fixtures.SYNTHETIC_ACCOUNT)

    def test_old_and_new_capture_routes_share_lineage_without_api_change(self):
        self.connect()
        first = self.adapter.capture()
        second = self.adapter.capture_with_exposure()
        third = self.adapter.capture()
        self.assertIs(type(first), SessionInputObservation)
        self.assertIs(type(third), SessionInputObservation)
        self.assertEqual(second.observation.prior_collection_id, first.facts.collection_id)
        self.assertEqual(third.prior_collection_id, second.observation.facts.collection_id)
        self.assertEqual(len({first.facts.collection_id, second.observation.facts.collection_id, third.facts.collection_id}), 3)
        facts = self.components.read_bridge.collect_session_facts()
        self.assertIs(type(facts), IbkrFiniteSessionFacts)
        self.assertFalse(hasattr(facts, "observation"))

    def test_public_result_keeps_finite_limitations_without_exposure_values(self):
        self.connect()
        self.fill()
        captured = self.adapter.capture_with_exposure()
        public = captured.public_dict()
        self.assertEqual(public, captured.observation.public_dict())
        self.assertIn("ALL_CLIENT_EXECUTION_COVERAGE_UNPROVEN", public["blockers"])
        self.assertIn("CONTINUOUS_EVENT_COVERAGE_UNPROVEN", public["blockers"])
        self.assertIn("EXTERNAL_ADJUSTMENT_RECONCILIATION_UNAVAILABLE", public["blockers"])
        self.assertFalse(public["whole_account_coverage_verified"])
        encoded = json.dumps(public)
        for private in (fixtures.SYNTHETIC_ACCOUNT, self.runtime.account_binding_fingerprint, "synthetic-fill-1"):
            self.assertNotIn(private, encoded)
        self.assertNotIn("funds", public)
        self.assertNotIn("order_family_pages", public)

    def test_cross_collection_pages_and_input_wrapper_are_rejected(self):
        self.connect()
        first, second = self.adapter.capture_with_exposure(), self.adapter.capture_with_exposure()
        with self.assertRaisesRegex(BrokerContractViolation, "IBKR_SESSION_EXPOSURE_INVALID"):
            replace(first.exposure, order_family_pages=second.exposure.order_family_pages)
        with self.assertRaisesRegex(IbkrSessionInputError, "IBKR_SESSION_EXPOSURE_INVALID"):
            SessionInputExposureObservation(first.observation, second.exposure)

    def test_wrong_page_account_time_family_or_shape_is_rejected(self):
        self.connect()
        exposure = self.adapter.capture_with_exposure().exposure
        page = exposure.order_family_pages[0]
        invalid_pages = (
            [page, *exposure.order_family_pages[1:]],
            (page, page, *exposure.order_family_pages[2:]),
            (replace(page, account_masked="****0000"), *exposure.order_family_pages[1:]),
            (replace(page, observed_at=page.observed_at + timedelta(seconds=1), received_at=page.received_at + timedelta(seconds=1)), *exposure.order_family_pages[1:]),
            (replace(page, page_complete=False), *exposure.order_family_pages[1:]),
            (replace(page, page_id="f" * 64), *exposure.order_family_pages[1:]),
            (replace(page, next_cursor="synthetic"), *exposure.order_family_pages[1:]),
        )
        for pages in invalid_pages:
            with self.subTest(pages=type(pages).__name__), self.assertRaises(BrokerContractViolation):
                replace(exposure, order_family_pages=pages)
        pages = tuple(replace(item, active_order_count=1) if item.family is OrderFamily.OPTION else item for item in exposure.order_family_pages)
        with self.assertRaises(BrokerContractViolation):
            replace(exposure, order_family_pages=pages)

    def test_risk_promotion_or_unrelated_base_is_rejected(self):
        self.connect()
        exposure = self.adapter.capture_with_exposure().exposure
        snapshot = exposure.observation.snapshot
        changes = (
            {"daily_realized_pnl": D(0)}, {"weekly_realized_pnl": D(0)}, {"peak_equity": D(10000)},
            {"daily_starting_equity": D(10000)}, {"daily_external_cash_flow": D(0)},
            {"risk_evidence_source": "synthetic"}, {"standard_equity_orders_complete": True},
            {"auth_point_in_time": False}, {"funds": replace(snapshot.funds, cash=D(9999))},
        )
        for change in changes:
            with self.subTest(change=tuple(change)), self.assertRaises(BrokerContractViolation):
                replace(exposure, observation=replace(exposure.observation, snapshot=replace(snapshot, **change)))
        with self.assertRaises(BrokerContractViolation):
            replace(exposure, facts=replace(exposure.facts, positions=[]))

    def test_missing_currency_or_execution_time_never_borrows_legacy_defaults(self):
        self.connect()
        for currency in ("BASE", ""):
            self.client.nlv_currency = currency
            with self.subTest(currency=currency), self.assertRaises(IbkrSessionInputError):
                self.adapter.capture_with_exposure()
            self.assertEqual(self.adapter.last_capture_failure_code, "IBKR_SESSION_EXPOSURE_USD_UNPROVEN")
            self.assertEqual(self.adapter.capture().facts.net_liquidation_currency, currency)
        self.client.nlv_currency = "USD"
        self.fill(currency="")
        with self.assertRaises(IbkrSessionInputError):
            self.adapter.capture_with_exposure()
        self.assertEqual(self.adapter.last_capture_failure_code, "IBKR_SESSION_EXPOSURE_EXECUTION_METADATA_UNPROVEN")
        self.assertEqual(self.adapter.capture().facts.executions[0].commission_currency, "")
        self.client.commissions["synthetic-fill-1"] = (D(1), "USD")
        self.client.executions[0].time = ""
        with self.assertRaises(IbkrSessionInputError):
            self.adapter.capture_with_exposure()
        self.assertIsNone(self.adapter.capture().facts.executions[0].source_executed_at)

    def test_configured_zone_time_remains_explicitly_blocked_not_provider_proven(self):
        self.connect()
        self.fill(time="20260914 09:59:00")
        captured = self.adapter.capture_with_exposure()
        self.assertIn("EXECUTION_TIMEZONE_CONFIGURED_NOT_PROVIDER_PROVEN", captured.observation.blockers)
        self.assertEqual(captured.observation.facts.executions[0].source_time_basis, "CONFIGURED_SESSION_ZONE_INTERPRETATION")

    def test_failed_capture_token_is_current_and_next_success_retains_gap(self):
        self.connect()
        first = self.adapter.capture_with_exposure()
        old_diagnostic = self.adapter.last_capture_diagnostic
        self.client.omit_end = "completed_orders"
        with self.assertRaises(IbkrSessionInputError):
            self.adapter.capture_with_exposure()
        self.assertIsNot(self.adapter.last_capture_diagnostic, old_diagnostic)
        self.assertEqual(self.adapter.last_capture_failure_code, "IBKR_SESSION_INPUT_READ_TIMEOUT_COMPLETED_ORDERS")
        self.client.omit_end = None
        next_read = self.adapter.capture_with_exposure()
        self.assertEqual(next_read.observation.prior_collection_id, first.observation.facts.collection_id)
        self.assertTrue(next_read.observation.sticky_read_gap)
        self.assertIsNone(self.adapter.last_capture_failure_code)

    def test_generation_and_authentication_loss_cannot_publish_exposure(self):
        self.connect()
        self.adapter.capture_with_exposure()
        bridge = self.components.read_bridge
        with patch.object(self.client, "reqExecutions", side_effect=lambda *args: bridge.open_generation(bridge.generation + 1)):
            with self.assertRaises(IbkrSessionInputError):
                self.adapter.capture_with_exposure()
        self.assertIsNone(bridge._last)
        self.assertIsNone(self.adapter.last_capture_diagnostic)
        self.assertTrue(self.adapter._sticky_gap)

    def test_post_collection_runtime_binding_change_is_rejected(self):
        self.connect()
        collect = self.components.read_bridge.collect_session_exposure
        def changed(**kwargs):
            result = collect(**kwargs)
            self.runtime._account_fingerprint = "f" * 64
            return result
        with patch.object(self.components.read_bridge, "collect_session_exposure", side_effect=changed):
            with self.assertRaises(IbkrSessionInputError):
                self.adapter.capture_with_exposure()
        self.assertIsNone(self.adapter._prior)

    def test_post_collection_disconnect_cannot_publish_exposure(self):
        self.connect()
        collect = self.components.read_bridge.collect_session_exposure
        def changed(**kwargs):
            result = collect(**kwargs)
            self.client.wrapper.connectionClosed()
            return result
        with patch.object(self.components.read_bridge, "collect_session_exposure", side_effect=changed):
            with self.assertRaises(IbkrSessionInputError):
                self.adapter.capture_with_exposure()
        self.assertIsNone(self.adapter._prior)

    def test_cleanup_failure_blocks_new_route_and_stale_diagnostic_reuse(self):
        self.connect()
        with patch.object(self.client, "cancelAccountUpdatesMulti", side_effect=RuntimeError("private")):
            with self.assertRaises(IbkrSessionInputError):
                self.adapter.capture_with_exposure()
        self.assertEqual(self.adapter.last_capture_failure_code, "IBKR_SESSION_ACCOUNT_UPDATES_CLEANUP_UNCONFIRMED")
        self.assertIsNone(self.components.read_bridge._last)
        with self.assertRaises(IbkrSessionInputError):
            self.adapter.capture_with_exposure()
        self.assertIsNone(self.adapter.last_capture_diagnostic)


if __name__ == "__main__":
    unittest.main()
