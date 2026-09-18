"""Hermetic IBKR contract-details and attended-preflight tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sys
from types import SimpleNamespace
from threading import Event, Thread
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from titan_brain.live.broker.base import (  # noqa: E402
    AccountSnapshot,
    AttendedLocalReview,
    BrokerCapabilityError,
    BrokerContractViolation,
    BrokerMutationBlocked,
    BrokerSide,
    EquityOrderType,
    FillSnapshot,
    FundsSnapshot,
    MarketHours,
    OrderRequest,
    OrderSnapshot,
    PositionSnapshot,
    TimeInForce,
)
from titan_brain.live.broker.ibkr_instrument import (  # noqa: E402
    IbkrInstrumentProvider,
)
from titan_brain.live.broker.ibkr_preflight import (  # noqa: E402
    IbkrAttendedOrderPlan,
    IbkrAttendedPreflightBridge,
    IbkrOrderPurpose,
)
from titan_brain.live.ibkr_instrument_provider import (  # noqa: E402
    IbkrPipelineInstrumentEvidenceProvider,
)
from titan_brain.live.calendar import ExchangeCalendar  # noqa: E402
from titan_brain.live.local_assembly import IbkrRegularHoursEligibility  # noqa: E402
from titan_brain.live.models import BrokerOrderState  # noqa: E402
from titan_brain.live.ibkr_autonomous_inputs import (  # noqa: E402
    DurableIbkrAutonomousAcceptanceVerifier,
    DurableIbkrAutonomousRiskPolicyCheck,
)
from titan_brain.live.risk_evidence_binding import (  # noqa: E402
    risk_high_water_receipt_hash,
)


NOW = datetime(2026, 9, 14, 14, 0, tzinfo=timezone.utc)  # 10:00 ET
ACCOUNT = "DU1234567"
MASK = "****4567"
BINDING = "a" * 64
PROVIDER = "b" * 64
COMMAND_CLIENT_ID = 19735
ENTRY_REF = "00000000-0000-4000-8000-000000000011"
STOP_REF = "00000000-0000-4000-8000-000000000012"
PLAN_ID = "c" * 64


class FakeContractRequester:
    def __init__(self):
        self.callbacks = None
        self.calls = []
        self.primary = "NASDAQ"
        self.con_id = 12345
        self.match_count = 1
        self.hours = "20260914:0930-20260914:1600"
        self.omit_end = False
        self.error = None

    def reqContractDetails(self, reqId, query):
        self.calls.append(
            (
                "reqContractDetails",
                reqId,
                query.symbol,
                query.secType,
                query.currency,
                query.exchange,
            )
        )
        for offset in range(self.match_count):
            returned = SimpleNamespace(
                conId=self.con_id + offset,
                symbol=query.symbol,
                secType="STK",
                currency="USD",
                exchange="SMART",
                primaryExchange=self.primary,
            )
            details = SimpleNamespace(
                contract=returned,
                validExchanges=f"SMART,{self.primary}",
                liquidHours=self.hours,
                timeZoneId="US/Eastern",
            )
            self.callbacks.contractDetails(reqId, details)
        if self.error is not None:
            code, text = self.error
            self.callbacks.error(reqId, code, text, "private-json")
        if not self.omit_end:
            self.callbacks.contractDetailsEnd(reqId)

    def cancelContractDetails(self, reqId):
        self.calls.append(("cancelContractDetails", reqId))


class BlockingFirstContractRequester(FakeContractRequester):
    def __init__(self):
        super().__init__()
        self.first_entered = Event()
        self.first_release = Event()
        self._dispatch_count = 0

    def reqContractDetails(self, reqId, query):
        self._dispatch_count += 1
        if self._dispatch_count == 1:
            self.first_entered.set()
            self.first_release.wait(timeout=1)
        super().reqContractDetails(reqId, query)


def request(
    *,
    side=BrokerSide.BUY,
    order_type=EquityOrderType.LIMIT,
    quantity=10,
    tif=TimeInForce.GFD,
    market_hours=MarketHours.REGULAR,
    limit="10.00",
    stop=None,
    ref=ENTRY_REF,
):
    return OrderRequest(
        account_masked=MASK,
        symbol="TEST",
        side=side,
        order_type=order_type,
        quantity=quantity,
        market_hours=market_hours,
        time_in_force=tif,
        client_ref_id=ref,
        limit_price=Decimal(limit) if limit is not None else None,
        stop_price=Decimal(stop) if stop is not None else None,
    )


def entry_plan(entry=None):
    entry = entry or request()
    protective = request(
        side=BrokerSide.SELL,
        order_type=EquityOrderType.STOP_MARKET,
        quantity=entry.quantity,
        tif=TimeInForce.GTC,
        limit=None,
        stop="9.00",
        ref=STOP_REF,
    )
    return IbkrAttendedOrderPlan(
        plan_id=PLAN_ID,
        purpose=IbkrOrderPurpose.ENTRY,
        request=entry,
        structural_stop=Decimal("9.00"),
        targets=(Decimal("12.00"), Decimal("13.00")),
        execution_reserve=Decimal("1.00"),
        fee_reserve=Decimal("0.50"),
        required_stop_request=protective,
    )


def working_buy():
    return OrderSnapshot(
        broker_order_id="ibkr:8:40",
        account_masked=MASK,
        symbol="OTHER",
        side=BrokerSide.BUY,
        order_type=EquityOrderType.LIMIT,
        state=BrokerOrderState.CONFIRMED,
        requested_quantity=Decimal("5"),
        cumulative_filled_quantity=Decimal("0"),
        market_hours=MarketHours.REGULAR,
        time_in_force=TimeInForce.GFD,
        broker_updated_at=NOW,
        received_at=NOW,
        limit_price=Decimal("10"),
    )


def cancellable_order(
    *,
    state=BrokerOrderState.CONFIRMED,
    client_ref=ENTRY_REF,
    broker_order_id=f"ibkr:{COMMAND_CLIENT_ID}:11",
    market_hours=MarketHours.REGULAR,
):
    return OrderSnapshot(
        broker_order_id=broker_order_id,
        account_masked=MASK,
        symbol="TEST",
        side=BrokerSide.BUY,
        order_type=EquityOrderType.LIMIT,
        state=state,
        requested_quantity=Decimal("10"),
        cumulative_filled_quantity=Decimal("0"),
        market_hours=market_hours,
        time_in_force=TimeInForce.GFD,
        broker_updated_at=NOW,
        received_at=NOW,
        limit_price=Decimal("10.00"),
        client_ref_id=client_ref,
    )


def filled_source_order(*, quantity="4", state=BrokerOrderState.PARTIALLY_FILLED):
    filled = Decimal(quantity)
    return OrderSnapshot(
        broker_order_id=f"ibkr:{COMMAND_CLIENT_ID}:11",
        account_masked=MASK,
        symbol="TEST",
        side=BrokerSide.BUY,
        order_type=EquityOrderType.LIMIT,
        state=state,
        requested_quantity=Decimal("10"),
        cumulative_filled_quantity=filled,
        market_hours=MarketHours.REGULAR,
        time_in_force=TimeInForce.GFD,
        broker_updated_at=NOW,
        received_at=NOW,
        limit_price=Decimal("10.00"),
        client_ref_id=ENTRY_REF,
        fills=(
            FillSnapshot(
                fill_id="exec-1",
                quantity=filled,
                price=Decimal("10.00"),
                executed_at=NOW,
            ),
        ),
    )


def snapshot(*, orders=(), positions=(), daily_authoritative=True, cash="500"):
    return AccountSnapshot(
        account_masked=MASK,
        observed_at=NOW,
        received_at=NOW,
        account_state="connected",
        account_type="MARGIN",
        funds=FundsSnapshot(
            total_value=Decimal("1000"),
            cash=Decimal(cash),
            buying_power=Decimal("1000"),
            unleveraged_buying_power=Decimal(cash),
        ),
        equity_positions=tuple(positions),
        equity_orders=tuple(orders),
        option_position_count=0,
        option_order_count=0,
        advanced_order_count=0,
        standard_equity_positions_complete=True,
        standard_equity_orders_complete=True,
        option_positions_complete=True,
        option_orders_complete=True,
        advanced_orders_complete=True,
        auth_point_in_time=True,
        daily_realized_pnl=Decimal("0"),
        daily_realized_pnl_complete=daily_authoritative,
        risk_evidence_authoritative=daily_authoritative,
        risk_evidence_source=(
            "ibkr:reqPnL.realizedPnL:current-day" if daily_authoritative else None
        ),
        risk_evidence_as_of=NOW if daily_authoritative else None,
    )


class IbkrInstrumentAndPreflightTests(unittest.TestCase):
    def setUp(self):
        self.no_socket = patch(
            "socket.socket.connect", side_effect=AssertionError("network forbidden")
        )
        self.no_socket.start()
        self.addCleanup(self.no_socket.stop)
        self.requester = FakeContractRequester()
        self.instrument = IbkrInstrumentProvider(
            requester=self.requester,
            contract_factory=SimpleNamespace,
            exact_account_id=ACCOUNT,
            account_masked=MASK,
            timeout_seconds=0.02,
            clock=lambda: NOW,
        )
        self.callbacks = self.instrument.open_generation(1)
        self.requester.callbacks = self.callbacks
        self.callbacks.managedAccounts(ACCOUNT)
        self.entry = request()
        self.plan = entry_plan(self.entry)
        self.snapshot_value = snapshot()
        self.snapshot_reads = 0
        self.risk_checks = 0

    def read_snapshot(self):
        self.snapshot_reads += 1
        return self.snapshot_value

    def test_sdk_1050_error_time_callback_accepts_informational_status(self):
        self.callbacks.error(
            27,
            1789394400,
            2104,
            "private broker text U9993103",
            "private-json",
        )
        evidence = self.instrument.get_instrument("TEST", now=NOW)
        self.assertEqual(evidence.identity.symbol, "TEST")

    def risk_check(self, observed, plan, now):
        self.risk_checks += 1
        self.assertIs(observed, self.snapshot_value)
        self.assertEqual(plan.plan_id, self.plan.plan_id)
        self.assertEqual(now, NOW)
        return None

    def plan_for_request(self, order_request):
        if self.plan.request.exact_tuple == order_request.exact_tuple:
            return self.plan
        stop = self.plan.required_stop_request
        if (
            self.plan.purpose is IbkrOrderPurpose.ENTRY
            and stop is not None
            and order_request.quantity <= stop.quantity
            and replace(stop, quantity=order_request.quantity).exact_tuple
            == order_request.exact_tuple
        ):
            return IbkrAttendedOrderPlan(
                plan_id=self.plan.plan_id,
                purpose=IbkrOrderPurpose.PROTECTION,
                request=order_request,
                structural_stop=self.plan.structural_stop,
                targets=(),
                execution_reserve=self.plan.execution_reserve,
                fee_reserve=self.plan.fee_reserve,
                required_stop_request=None,
            )
        return self.plan

    def preflight(self, **overrides):
        args = dict(
            account_snapshot=self.read_snapshot,
            instruments=self.instrument,
            plan_reader=self.plan_for_request,
            risk_policy_check=self.risk_check,
            session_is_entry_eligible=lambda current, purpose: True,
            account_masked=MASK,
            command_client_id=COMMAND_CLIENT_ID,
            policy_binding_id=BINDING,
            provider_contract_id=PROVIDER,
            account_max_age_seconds=2,
            instrument_max_age_seconds=2,
            review_ttl_seconds=5,
            existing_order_reserve=Decimal("0.25"),
            clock=lambda: NOW,
        )
        args.update(overrides)
        return IbkrAttendedPreflightBridge(**args)

    def test_contract_lookup_is_exact_ibkr_sourced_and_regular_hours_only(self):
        evidence = self.instrument.get_instrument("TEST", now=NOW)
        self.assertEqual(evidence.source, "ibkr:tws-contract-details")
        self.assertEqual(evidence.identity.con_id, 12345)
        self.assertEqual(evidence.identity.symbol, "TEST")
        self.assertEqual(evidence.identity.sec_type, "STK")
        self.assertEqual(evidence.identity.currency, "USD")
        self.assertEqual(evidence.identity.exchange, "SMART")
        self.assertEqual(evidence.identity.primary_exchange, "NASDAQ")
        self.assertTrue(evidence.exchange_listed)
        self.assertTrue(evidence.regular_hours_eligible)
        call = self.requester.calls[0]
        self.assertEqual(call[2:], ("TEST", "STK", "USD", "SMART"))

    def test_release_inventory_exposes_preflight_and_contract_read_dependencies(self):
        bridge = self.preflight()
        inventory = {
            role: (component, members)
            for role, component, members in bridge.release_components()
        }
        self.assertEqual(set(inventory), {
            "ibkr_account_snapshot_reader",
            "ibkr_instrument_provider",
            "ibkr_attended_plan_reader",
            "ibkr_risk_policy_check",
            "ibkr_session_eligibility_check",
            "ibkr_preflight_clock",
        })
        self.assertIs(inventory["ibkr_instrument_provider"][0], self.instrument)
        instrument_inventory = {
            role: (component, members)
            for role, component, members in self.instrument.release_components()
        }
        self.assertEqual(set(instrument_inventory), {
            "ibkr_contract_details_requester",
            "ibkr_instrument_contract_factory",
            "ibkr_instrument_clock",
        })
        self.assertIs(
            instrument_inventory["ibkr_contract_details_requester"][0],
            self.requester,
        )
        self.assertTrue(all(members for _component, members in inventory.values()))
        self.assertTrue(
            all(members for _component, members in instrument_inventory.values())
        )

    def test_pipeline_adapter_is_broker_neutral_and_ibkr_sourced(self):
        pipeline_provider = IbkrPipelineInstrumentEvidenceProvider(self.instrument)
        evidence = pipeline_provider.get_instrument_evidence("TEST", now=NOW)
        self.assertIsNotNone(evidence)
        assert evidence is not None
        self.assertEqual(evidence.source, "ibkr:tws-contract-details")
        self.assertEqual(evidence.instrument_id, "12345")
        self.assertTrue(evidence.broker_tradable)
        self.assertNotIn("massive", evidence.source.lower())

    def test_premarket_adapter_verifies_upcoming_open_without_current_authority(self):
        premarket = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)  # 08:00 ET
        regular_open = datetime(2026, 9, 14, 13, 30, tzinfo=timezone.utc)
        instrument = IbkrInstrumentProvider(
            requester=self.requester,
            contract_factory=SimpleNamespace,
            exact_account_id=ACCOUNT,
            account_masked=MASK,
            timeout_seconds=0.02,
            clock=lambda: premarket,
        )
        callbacks = instrument.open_generation(1)
        self.requester.callbacks = callbacks
        callbacks.managedAccounts(ACCOUNT)
        adapter = IbkrPipelineInstrumentEvidenceProvider(instrument)

        self.assertIsNone(adapter.get_instrument_evidence("TEST", now=premarket))
        evidence = adapter.get_premarket_analysis_evidence(
            "TEST",
            now=premarket,
            regular_session_open=regular_open,
        )
        self.assertIsNotNone(evidence)
        assert evidence is not None
        self.assertEqual(evidence.source, "ibkr:tws-contract-details")
        self.assertEqual(evidence.eligibility_at, regular_open)
        self.assertEqual(
            evidence.eligibility_scope, "upcoming_regular_session_analysis"
        )
        self.assertTrue(evidence.regular_hours_eligible)

    def test_upcoming_session_evidence_rejects_unbounded_future_time(self):
        with self.assertRaisesRegex(
            BrokerContractViolation, "ELIGIBILITY_TIME_INVALID"
        ):
            self.instrument.get_upcoming_regular_session_instrument(
                "TEST",
                now=NOW,
                eligibility_at=NOW + timedelta(hours=3, microseconds=1),
            )

    def test_concurrent_analysis_contract_reads_are_serialized_not_false_denied(self):
        requester = BlockingFirstContractRequester()
        provider = IbkrInstrumentProvider(
            requester=requester,
            contract_factory=SimpleNamespace,
            exact_account_id=ACCOUNT,
            account_masked=MASK,
            timeout_seconds=0.5,
            clock=lambda: NOW,
        )
        callbacks = provider.open_generation(1)
        requester.callbacks = callbacks
        callbacks.managedAccounts(ACCOUNT)
        observed = []
        failures = []

        def read(symbol):
            try:
                observed.append(provider.get_instrument(symbol, now=NOW))
            except Exception as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        first = Thread(target=read, args=("TEST",))
        second = Thread(target=read, args=("NEXT",))
        first.start()
        self.assertTrue(requester.first_entered.wait(timeout=0.2))
        second.start()
        requester.first_release.set()
        first.join(timeout=1)
        second.join(timeout=1)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual({item.identity.symbol for item in observed}, {"TEST", "NEXT"})
        self.assertEqual(
            len(
                [
                    call
                    for call in requester.calls
                    if call[0] == "reqContractDetails"
                ]
            ),
            2,
        )

    def test_contract_lookup_requires_unique_listing_and_end_marker(self):
        self.requester.match_count = 2
        with self.assertRaisesRegex(BrokerContractViolation, "NOT_UNIQUE"):
            self.instrument.get_instrument("TEST", now=NOW)
        self.requester.match_count = 1
        self.requester.omit_end = True
        with self.assertRaisesRegex(BrokerCapabilityError, "TIMEOUT"):
            self.instrument.get_instrument("TEST", now=NOW)

    def test_contract_lookup_rejects_closed_hours_and_sanitizes_errors(self):
        self.requester.hours = "20260914:CLOSED"
        with self.assertRaisesRegex(BrokerCapabilityError, "NOT_REGULAR_HOURS"):
            self.instrument.get_instrument("TEST", now=NOW)
        self.requester.hours = "20260914:0930-20260914:1600"
        secret = "private account diagnostic"
        self.requester.error = (200, secret)
        with self.assertRaises(BrokerCapabilityError) as caught:
            self.instrument.get_instrument("TEST", now=NOW)
        self.assertNotIn(secret, str(caught.exception))

    def test_metadata_receipt_is_distinct_from_execution_eligibility(self):
        for hours, expected_open in (
            ("20260914:0930-20260914:1600", True),
            ("20260914:CLOSED", False),
            ("malformed", False),
        ):
            with self.subTest(hours=hours):
                self.requester.hours = hours
                receipt = self.instrument.read_contract_metadata("TEST", now=NOW)
                self.assertRegex(receipt.receipt_id, r"^[0-9a-f]{64}$")
                self.assertIs(receipt.regular_session_open, expected_open)
                self.assertFalse(hasattr(receipt, "regular_hours_eligible"))
                self.assertFalse(hasattr(receipt, "evidence_id"))
                self.assertEqual(receipt.identity.symbol, "TEST")
                if not expected_open:
                    with self.assertRaisesRegex(BrokerCapabilityError, "NOT_REGULAR_HOURS"):
                        self.instrument.get_instrument("TEST", now=NOW)

    def test_metadata_read_still_requires_unique_listing_and_end_marker(self):
        self.requester.match_count = 2
        with self.assertRaisesRegex(BrokerContractViolation, "NOT_UNIQUE"):
            self.instrument.read_contract_metadata("TEST", now=NOW)
        self.requester.match_count = 1
        self.requester.omit_end = True
        with self.assertRaisesRegex(BrokerCapabilityError, "TIMEOUT"):
            self.instrument.read_contract_metadata("TEST", now=NOW)

    def test_stale_generation_cannot_authenticate_or_deliver_details(self):
        old = self.callbacks
        new = self.instrument.open_generation(2)
        self.requester.callbacks = new
        old.managedAccounts(ACCOUNT)
        with self.assertRaisesRegex(BrokerCapabilityError, "NOT_AUTHENTICATED"):
            self.instrument.get_instrument("TEST", now=NOW)
        new.managedAccounts(ACCOUNT)
        evidence = self.instrument.get_instrument("TEST", now=NOW)
        self.assertEqual(evidence.identity.con_id, 12345)

    def test_attended_entry_review_contains_exact_phrase_protection_and_capacity(self):
        self.snapshot_value = snapshot(orders=(working_buy(),))
        bridge = self.preflight()
        review = bridge.review(self.entry)
        self.assertIsInstance(review, AttendedLocalReview)
        self.assertFalse(review.broker_bound)
        self.assertEqual(
            review.required_confirmation_phrase,
            "CONFIRM BUY 10 TEST LIMIT 10.00 GFD REGULAR_HOURS",
        )
        self.assertEqual(review.preview["provider"], "IBKR TWS API (local attended preview; not broker acceptance)")
        self.assertEqual(review.preview["capacity"]["working_buy_commitments"], "50.25")
        self.assertEqual(review.preview["capacity"]["candidate_commitment"], "101.50")
        self.assertEqual(review.preview["capacity"]["remaining_after_candidate"], "348.25")
        self.assertEqual(review.preview["risk"]["planned_downside"], "10.00")
        self.assertEqual(review.preview["risk"]["stress_downside"], "11.50")
        self.assertEqual(review.preview["required_stop"]["stop_price"], "9.00")
        self.assertEqual(review.preview["required_stop"]["time_in_force"], "gtc")
        self.assertTrue(all(check.severity == "INFO" for check in review.order_checks))
        self.assertEqual(bridge.contract_for(self.entry).con_id, 12345)

    def test_place_boundary_revalidation_reloads_every_fact(self):
        bridge = self.preflight()
        review = bridge.review(self.entry)
        self.assertEqual(self.snapshot_reads, 1)
        self.assertEqual(len([call for call in self.requester.calls if call[0] == "reqContractDetails"]), 1)
        bridge.revalidate(self.entry, review)
        self.assertEqual(self.snapshot_reads, 2)
        self.assertEqual(self.risk_checks, 2)
        self.assertEqual(len([call for call in self.requester.calls if call[0] == "reqContractDetails"]), 2)
        self.requester.con_id = 99999
        with self.assertRaisesRegex(BrokerMutationBlocked, "FACTS_CHANGED"):
            bridge.revalidate(self.entry, review)

    def test_actual_entry_preflight_binds_activation_lineage_at_review_and_place(self):
        class StaticReceiptVerifier(DurableIbkrAutonomousAcceptanceVerifier):
            def __init__(self):
                pass

            def verify_policy_receipt(self):
                return None

        lineage = "e" * 64
        source = (
            "ibkr:reqPnL.realizedPnL:current-day+"
            "authenticated-daily-baseline:interactive-brokers:test:"
            + "a" * 64
            + ":"
            + "b" * 64
        )
        authenticated = replace(
            snapshot(),
            weekly_realized_pnl=Decimal("0"),
            weekly_realized_pnl_complete=True,
            peak_equity=Decimal("1200"),
            peak_equity_complete=True,
            risk_evidence_source=source,
            risk_baseline_identity_hash="c" * 64,
            risk_baseline_receipt_hash="b" * 64,
            risk_high_water_identity_hash="d" * 64,
            risk_high_water_lineage_hash=lineage,
            risk_high_water_receipt_hash=risk_high_water_receipt_hash(
                identity_hash="d" * 64,
                baseline_receipt_hash="b" * 64,
                lineage_hash=lineage,
                peak_equity=1200,
            ),
        )
        risk_check = DurableIbkrAutonomousRiskPolicyCheck(
            delegate=lambda *_args: None,
            receipt_verifier=StaticReceiptVerifier(),
        )
        risk_check.bind_entry_risk_activation(
            lineage_hash=lineage,
            minimum_peak=Decimal("1200"),
        )
        self.snapshot_value = authenticated
        bridge = self.preflight(
            risk_policy_check=risk_check,
            plan_reader_role="ibkr_autonomous_plan_reader",
        )
        review = bridge.review(self.entry)

        replacement_lineage = "f" * 64
        self.snapshot_value = replace(
            authenticated,
            risk_high_water_identity_hash="9" * 64,
            risk_high_water_lineage_hash=replacement_lineage,
            risk_high_water_receipt_hash=risk_high_water_receipt_hash(
                identity_hash="9" * 64,
                baseline_receipt_hash="b" * 64,
                lineage_hash=replacement_lineage,
                peak_equity=1200,
            ),
        )
        # Transport place invokes this same revalidation before registering or
        # dispatching its durable intent; a prepared review cannot bypass it.
        with self.assertRaisesRegex(
            BrokerMutationBlocked,
            "IBKR_APPROVED_RISK_POLICY_DENIED",
        ):
            bridge.revalidate(self.entry, review)

    def test_missing_daily_pnl_authority_and_incomplete_reconciliation_fail_closed(self):
        self.snapshot_value = snapshot(daily_authoritative=False)
        with self.assertRaisesRegex(BrokerMutationBlocked, "DAILY_REALIZED_PNL"):
            self.preflight().review(self.entry)
        self.snapshot_value = replace(
            snapshot(), advanced_orders_complete=False
        )
        with self.assertRaisesRegex(BrokerMutationBlocked, "RECONCILIATION_INCOMPLETE"):
            self.preflight().review(self.entry)

    def test_ambiguous_account_summary_pnl_source_is_rejected(self):
        self.snapshot_value = replace(
            snapshot(), risk_evidence_source="ibkr:AccountSummary.RealizedPnL"
        )
        with self.assertRaisesRegex(BrokerMutationBlocked, "PNL_SOURCE_INVALID"):
            self.preflight().review(self.entry)

    def test_authenticated_composite_risk_source_requires_complete_baseline(self):
        source = (
            "ibkr:reqPnL.realizedPnL:current-day+"
            "authenticated-daily-baseline:interactive-brokers:account-statement:"
            + "a" * 64
            + ":"
            + "b" * 64
        )
        self.snapshot_value = replace(
            snapshot(),
            weekly_realized_pnl=Decimal("0"),
            weekly_realized_pnl_complete=True,
            peak_equity=Decimal("1200"),
            peak_equity_complete=True,
            risk_evidence_source=source,
            risk_baseline_identity_hash="c" * 64,
            risk_baseline_receipt_hash="b" * 64,
            risk_high_water_identity_hash="d" * 64,
            risk_high_water_lineage_hash="e" * 64,
            risk_high_water_receipt_hash=risk_high_water_receipt_hash(
                identity_hash="d" * 64,
                baseline_receipt_hash="b" * 64,
                lineage_hash="e" * 64,
                peak_equity=1200,
            ),
        )
        self.assertIsInstance(
            self.preflight().review(self.entry), AttendedLocalReview
        )
        self.snapshot_value = replace(
            self.snapshot_value,
            peak_equity=None,
            peak_equity_complete=False,
        )
        with self.assertRaisesRegex(BrokerMutationBlocked, "PNL_SOURCE_INVALID"):
            self.preflight().review(self.entry)

    def test_autonomous_unready_entry_evidence_blocks_buys_not_protection_or_exit(self):
        policy_check = lambda *_args: None
        policy_check.begin_account_snapshot = lambda *_args, **_kwargs: None
        policy_check.observe_account_snapshot = lambda *_args, **_kwargs: None
        with self.assertRaisesRegex(
            BrokerMutationBlocked, "ENTRY_RISK_EVIDENCE_INCOMPLETE"
        ):
            self.preflight(
                plan_reader_role="ibkr_autonomous_plan_reader",
                risk_policy_check=policy_check,
            ).review(self.entry)

        position = PositionSnapshot(
            symbol="TEST",
            quantity=Decimal("10"),
            sellable_quantity=Decimal("10"),
            held_for_sells=Decimal("0"),
            average_price=Decimal("10"),
        )
        self.snapshot_value = snapshot(
            positions=(position,),
            daily_authoritative=False,
        )
        protection = request(
            side=BrokerSide.SELL,
            order_type=EquityOrderType.STOP_MARKET,
            quantity=10,
            tif=TimeInForce.GTC,
            limit=None,
            stop="9.00",
            ref="00000000-0000-4000-8000-000000000014",
        )
        self.plan = IbkrAttendedOrderPlan(
            plan_id=PLAN_ID,
            purpose=IbkrOrderPurpose.PROTECTION,
            request=protection,
            structural_stop=Decimal("9.00"),
            targets=(),
            execution_reserve=Decimal("1"),
            fee_reserve=Decimal("0.50"),
            required_stop_request=None,
        )
        protected = self.preflight(
            plan_reader_role="ibkr_autonomous_plan_reader"
        ).review(protection)
        self.assertEqual(protected.request, protection)

        exit_request = request(
            side=BrokerSide.SELL,
            order_type=EquityOrderType.MARKET,
            quantity=10,
            tif=TimeInForce.GFD,
            limit=None,
            stop=None,
            ref="00000000-0000-4000-8000-000000000015",
        )
        self.plan = IbkrAttendedOrderPlan(
            plan_id=PLAN_ID,
            purpose=IbkrOrderPurpose.EXIT,
            request=exit_request,
            structural_stop=None,
            targets=(),
            execution_reserve=Decimal("1"),
            fee_reserve=Decimal("0.50"),
            required_stop_request=None,
        )
        exited = self.preflight(
            plan_reader_role="ibkr_autonomous_plan_reader"
        ).review(exit_request)
        self.assertEqual(exited.request, exit_request)

    def test_no_borrow_capacity_subtracts_working_unknown_and_positive_reserves(self):
        unknown = replace(working_buy(), state=BrokerOrderState.UNKNOWN)
        self.snapshot_value = snapshot(orders=(unknown,), cash="151.74")
        with self.assertRaisesRegex(BrokerMutationBlocked, "CAPACITY_INSUFFICIENT"):
            self.preflight().review(self.entry)
        self.snapshot_value = snapshot(orders=(unknown,), cash="151.75")
        review = self.preflight().review(self.entry)
        self.assertEqual(review.preview["capacity"]["remaining_after_candidate"], "0.00")

    def test_premarket_and_extended_hours_are_never_reviewed(self):
        premarket = datetime(2026, 9, 14, 13, 0, tzinfo=timezone.utc)
        bridge = self.preflight(clock=lambda: premarket)
        with self.assertRaisesRegex(BrokerMutationBlocked, "SESSION_CLOSED"):
            bridge.review(self.entry)
        extended = request(market_hours=MarketHours.EXTENDED)
        with self.assertRaises(ValueError):
            entry_plan(extended)

    def test_entry_cutoff_does_not_disable_protection_exit_or_cancel(self):
        calendar = ExchangeCalendar.from_json(
            Path(__file__).resolve().parents[1] / "config/nyse_calendar_2026.json"
        )
        eligibility = IbkrRegularHoursEligibility(calendar)
        protection = self.plan.required_stop_request
        assert protection is not None
        protection_plan = IbkrAttendedOrderPlan(
            plan_id=PLAN_ID,
            purpose=IbkrOrderPurpose.PROTECTION,
            request=protection,
            structural_stop=Decimal("9.00"),
            targets=(),
            execution_reserve=Decimal("1"),
            fee_reserve=Decimal("0.50"),
            required_stop_request=None,
        )
        position = PositionSnapshot(
            symbol="TEST",
            quantity=Decimal("10"),
            sellable_quantity=Decimal("10"),
            held_for_sells=Decimal("0"),
            average_price=Decimal("10"),
        )
        late = datetime(2026, 9, 14, 19, 55, tzinfo=timezone.utc)
        self.plan = protection_plan
        self.snapshot_value = snapshot(positions=(position,))
        bridge = self.preflight(
            clock=lambda: late,
            session_is_entry_eligible=eligibility,
            account_max_age_seconds=30000,
            instrument_max_age_seconds=30000,
            risk_policy_check=lambda *_args: None,
        )
        self.assertEqual(bridge.review(protection).request, protection)

        exit_request = request(
            side=BrokerSide.SELL,
            order_type=EquityOrderType.MARKET,
            quantity=10,
            tif=TimeInForce.GFD,
            limit=None,
            stop=None,
            ref="00000000-0000-4000-8000-000000000013",
        )
        self.plan = IbkrAttendedOrderPlan(
            plan_id=PLAN_ID,
            purpose=IbkrOrderPurpose.EXIT,
            request=exit_request,
            structural_stop=None,
            targets=(),
            execution_reserve=Decimal("1"),
            fee_reserve=Decimal("0.50"),
            required_stop_request=None,
        )
        self.assertEqual(
            self.preflight(
                clock=lambda: late,
                session_is_entry_eligible=eligibility,
                account_max_age_seconds=30000,
                instrument_max_age_seconds=30000,
                risk_policy_check=lambda *_args: None,
            ).review(exit_request).request,
            exit_request,
        )

        self.plan = entry_plan(self.entry)
        self.snapshot_value = snapshot()
        after_entry_cutoff = datetime(2026, 9, 14, 19, 31, tzinfo=timezone.utc)
        with self.assertRaisesRegex(BrokerMutationBlocked, "SESSION_CLOSED"):
            self.preflight(
                clock=lambda: after_entry_cutoff,
                session_is_entry_eligible=eligibility,
                account_max_age_seconds=30000,
                instrument_max_age_seconds=30000,
                risk_policy_check=lambda *_args: None,
            ).review(self.entry)

        self.snapshot_value = snapshot(orders=(cancellable_order(),))
        self.assertIsNone(
            self.preflight(
                clock=lambda: late,
                session_is_entry_eligible=eligibility,
                account_max_age_seconds=30000,
                risk_policy_check=lambda *_args: None,
            ).authorize_cancel(ENTRY_REF, 11)
        )
        premarket = datetime(2026, 9, 14, 13, 0, tzinfo=timezone.utc)
        self.assertFalse(eligibility(premarket, IbkrOrderPurpose.PROTECTION))

    def test_management_session_stops_at_exact_normal_and_early_close(self):
        calendar = ExchangeCalendar.from_json(
            Path(__file__).resolve().parents[1] / "config/nyse_calendar_2026.json"
        )
        eligibility = IbkrRegularHoursEligibility(calendar)
        boundaries = (
            (
                datetime(2026, 9, 14, 19, 59, 59, tzinfo=timezone.utc),
                datetime(2026, 9, 14, 20, 0, tzinfo=timezone.utc),
            ),
            (
                datetime(2026, 11, 27, 17, 59, 59, tzinfo=timezone.utc),
                datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc),
            ),
        )
        for just_before_close, exact_close in boundaries:
            for purpose in (
                IbkrOrderPurpose.PROTECTION,
                IbkrOrderPurpose.EXIT,
                "cancel",
            ):
                with self.subTest(close=exact_close, purpose=purpose):
                    self.assertTrue(eligibility(just_before_close, purpose))
                    self.assertFalse(eligibility(exact_close, purpose))

    def test_plan_requires_exact_gtc_regular_hours_stop_and_positive_reserves(self):
        invalid_stop = request(
            side=BrokerSide.SELL,
            order_type=EquityOrderType.STOP_MARKET,
            quantity=9,
            tif=TimeInForce.GTC,
            limit=None,
            stop="9.00",
            ref=STOP_REF,
        )
        with self.assertRaisesRegex(ValueError, "exact regular-hours GTC"):
            replace(self.plan, required_stop_request=invalid_stop)
        with self.assertRaisesRegex(ValueError, "positive fee"):
            replace(self.plan, fee_reserve=Decimal("0"))

    def test_reduce_only_protection_requires_uncommitted_position_quantity(self):
        protection = self.plan.required_stop_request
        assert protection is not None
        protection_plan = IbkrAttendedOrderPlan(
            plan_id="protection-1",
            purpose=IbkrOrderPurpose.PROTECTION,
            request=protection,
            structural_stop=Decimal("9.00"),
            targets=(),
            execution_reserve=Decimal("1"),
            fee_reserve=Decimal("0.50"),
            required_stop_request=None,
        )
        self.plan = protection_plan
        position = PositionSnapshot(
            symbol="TEST",
            quantity=Decimal("10"),
            sellable_quantity=Decimal("10"),
            held_for_sells=Decimal("0"),
            average_price=Decimal("10"),
        )
        self.snapshot_value = snapshot(positions=(position,))
        bridge = self.preflight()
        review = bridge.review(protection)
        self.assertEqual(
            review.required_confirmation_phrase,
            "CONFIRM SELL 10 TEST STOP_MARKET 9.00 GTC REGULAR_HOURS",
        )
        self.snapshot_value = snapshot(
            positions=(replace(position, sellable_quantity=Decimal("0"), held_for_sells=Decimal("10")),)
        )
        with self.assertRaisesRegex(BrokerMutationBlocked, "SHARES_UNAVAILABLE"):
            bridge.revalidate(protection, review)

    def test_risk_policy_must_raise_on_denial_not_return_boolean(self):
        bridge = self.preflight(risk_policy_check=lambda *_args: False)
        with self.assertRaisesRegex(BrokerMutationBlocked, "MUST_RAISE"):
            bridge.review(self.entry)

    def test_cancel_authorization_requires_fresh_exact_command_client_order(self):
        self.snapshot_value = snapshot(
            orders=(cancellable_order(),),
            daily_authoritative=False,
        )
        bridge = self.preflight()
        evidence_id = bridge.cancel_evidence(ENTRY_REF, 11)
        self.assertRegex(evidence_id, r"^[0-9a-f]{64}$")
        self.assertEqual(self.snapshot_reads, 1)
        self.assertIsNone(bridge.authorize_cancel(ENTRY_REF, 11))
        self.assertEqual(self.snapshot_reads, 2)

        invalid_orders = (
            cancellable_order(state=BrokerOrderState.UNKNOWN),
            cancellable_order(state=BrokerOrderState.PENDING_CANCELLED),
            cancellable_order(state=BrokerOrderState.FILLED),
            cancellable_order(state=BrokerOrderState.CANCELLED),
            cancellable_order(broker_order_id="ibkr:19736:11"),
            cancellable_order(broker_order_id=f"ibkr:{COMMAND_CLIENT_ID}:12"),
            cancellable_order(market_hours=MarketHours.EXTENDED),
        )
        for order in invalid_orders:
            with self.subTest(state=order.state, broker_order_id=order.broker_order_id):
                self.snapshot_value = snapshot(orders=(order,))
                with self.assertRaises(BrokerMutationBlocked):
                    bridge.authorize_cancel(ENTRY_REF, 11)

    def test_cancel_authorization_rejects_missing_duplicate_stale_and_closed_evidence(self):
        bridge = self.preflight()
        for orders in (
            (),
            (cancellable_order(client_ref=STOP_REF),),
            (cancellable_order(), cancellable_order()),
        ):
            with self.subTest(order_count=len(orders)):
                self.snapshot_value = snapshot(orders=orders)
                with self.assertRaisesRegex(BrokerMutationBlocked, "UNRESOLVED"):
                    bridge.authorize_cancel(ENTRY_REF, 11)

        self.snapshot_value = replace(
            snapshot(orders=(cancellable_order(),)),
            observed_at=NOW - timedelta(seconds=3),
            received_at=NOW - timedelta(seconds=3),
            risk_evidence_as_of=NOW - timedelta(seconds=3),
        )
        with self.assertRaisesRegex(BrokerMutationBlocked, "STALE_OR_FUTURE"):
            bridge.authorize_cancel(ENTRY_REF, 11)

        self.snapshot_value = snapshot(orders=(cancellable_order(),))
        with self.assertRaisesRegex(BrokerMutationBlocked, "SESSION_CLOSED"):
            self.preflight(
                clock=lambda: datetime(2026, 9, 14, 13, 0, tzinfo=timezone.utc)
            ).authorize_cancel(ENTRY_REF, 11)

    def test_protection_review_recovers_partial_fill_without_old_runtime_review(self):
        # The entry review is deliberately issued by a different bridge.  Its
        # process-local `_issued` cache is not available to the protection lane.
        first_process = self.preflight()
        first_process.review(self.entry)

        position = PositionSnapshot(
            symbol="TEST",
            quantity=Decimal("4"),
            sellable_quantity=Decimal("4"),
            held_for_sells=Decimal("0"),
            average_price=Decimal("10"),
        )
        self.snapshot_value = snapshot(
            orders=(filled_source_order(quantity="4"),),
            positions=(position,),
        )
        second_process = self.preflight()
        stop = self.plan.required_stop_request
        assert stop is not None
        review = second_process.review_protection(
            self.entry,
            PLAN_ID,
            stop,
            NOW - timedelta(seconds=1),
        )
        self.assertEqual(review.request.quantity, 4)
        self.assertEqual(review.request.client_ref_id, STOP_REF)
        self.assertEqual(
            review.preview["protection_source"]["broker_confirmed_uncovered_quantity"],
            4,
        )
        self.assertTrue(any(check.code == "FILL_DELTA" for check in review.order_checks))
        self.assertEqual(second_process.contract_for(review.request).con_id, 12345)

        working_stop = OrderSnapshot(
            broker_order_id=f"ibkr:{COMMAND_CLIENT_ID}:12",
            account_masked=MASK,
            symbol="TEST",
            side=BrokerSide.SELL,
            order_type=EquityOrderType.STOP_MARKET,
            state=BrokerOrderState.CONFIRMED,
            requested_quantity=Decimal("4"),
            cumulative_filled_quantity=Decimal("0"),
            market_hours=MarketHours.REGULAR,
            time_in_force=TimeInForce.GTC,
            broker_updated_at=NOW,
            received_at=NOW,
            stop_price=Decimal("9.00"),
            client_ref_id=STOP_REF,
        )
        self.snapshot_value = snapshot(
            orders=(filled_source_order(quantity="7"), working_stop),
            positions=(
                replace(
                    position,
                    quantity=Decimal("7"),
                    sellable_quantity=Decimal("3"),
                    held_for_sells=Decimal("4"),
                ),
            ),
        )
        later_process = self.preflight()
        incremental = later_process.review_protection(
            self.entry,
            PLAN_ID,
            stop,
            NOW - timedelta(seconds=1),
        )
        self.assertEqual(incremental.request.quantity, 3)
        self.assertNotEqual(incremental.request.client_ref_id, STOP_REF)
        self.assertEqual(incremental.request.stop_price, Decimal("9.00"))

    def test_protection_review_rejects_no_fill_and_existing_or_ambiguous_coverage(self):
        stop = self.plan.required_stop_request
        assert stop is not None
        position = PositionSnapshot(
            symbol="TEST",
            quantity=Decimal("4"),
            sellable_quantity=Decimal("4"),
            held_for_sells=Decimal("0"),
            average_price=Decimal("10"),
        )
        self.snapshot_value = snapshot(
            orders=(cancellable_order(state=BrokerOrderState.UNKNOWN),),
            positions=(position,),
        )
        with self.assertRaisesRegex(BrokerMutationBlocked, "FILL_DELTA_UNPROVEN"):
            self.preflight().review_protection(
                self.entry, PLAN_ID, stop, NOW - timedelta(seconds=1)
            )

        self.snapshot_value = snapshot(
            orders=(
                replace(
                    filled_source_order(quantity="4"),
                    broker_order_id="ibkr:8:11",
                ),
            ),
            positions=(position,),
        )
        with self.assertRaisesRegex(BrokerMutationBlocked, "SOURCE_ORDER_NOT_OWNED"):
            self.preflight().review_protection(
                self.entry, PLAN_ID, stop, NOW - timedelta(seconds=1)
            )

        existing_stop = OrderSnapshot(
            broker_order_id=f"ibkr:{COMMAND_CLIENT_ID}:12",
            account_masked=MASK,
            symbol="TEST",
            side=BrokerSide.SELL,
            order_type=EquityOrderType.STOP_MARKET,
            state=BrokerOrderState.CONFIRMED,
            requested_quantity=Decimal("4"),
            cumulative_filled_quantity=Decimal("0"),
            market_hours=MarketHours.REGULAR,
            time_in_force=TimeInForce.GTC,
            broker_updated_at=NOW,
            received_at=NOW,
            stop_price=Decimal("9.00"),
            client_ref_id=STOP_REF,
        )
        self.snapshot_value = snapshot(
            orders=(filled_source_order(quantity="4"), existing_stop),
            positions=(
                replace(
                    position,
                    sellable_quantity=Decimal("0"),
                    held_for_sells=Decimal("4"),
                ),
            ),
        )
        with self.assertRaisesRegex(BrokerMutationBlocked, "UNCOVERED_QUANTITY_UNPROVEN"):
            self.preflight().review_protection(
                self.entry, PLAN_ID, stop, NOW - timedelta(seconds=1)
            )

        incompatible = replace(existing_stop, stop_price=Decimal("8.99"))
        self.snapshot_value = snapshot(
            orders=(filled_source_order(quantity="7"), incompatible),
            positions=(
                PositionSnapshot(
                    symbol="TEST",
                    quantity=Decimal("7"),
                    sellable_quantity=Decimal("3"),
                    held_for_sells=Decimal("4"),
                    average_price=Decimal("10"),
                ),
            ),
        )
        with self.assertRaisesRegex(BrokerMutationBlocked, "CONFLICTING_SELL_COVERAGE"):
            self.preflight().review_protection(
                self.entry, PLAN_ID, stop, NOW - timedelta(seconds=1)
            )

        over_coverage = replace(existing_stop, requested_quantity=Decimal("5"))
        self.snapshot_value = snapshot(
            orders=(filled_source_order(quantity="4"), over_coverage),
            positions=(
                PositionSnapshot(
                    symbol="TEST",
                    quantity=Decimal("5"),
                    sellable_quantity=Decimal("0"),
                    held_for_sells=Decimal("5"),
                    average_price=Decimal("10"),
                ),
            ),
        )
        with self.assertRaisesRegex(BrokerMutationBlocked, "COVERAGE_EXCEEDS_FILL"):
            self.preflight().review_protection(
                self.entry, PLAN_ID, stop, NOW - timedelta(seconds=1)
            )


if __name__ == "__main__":
    unittest.main()
