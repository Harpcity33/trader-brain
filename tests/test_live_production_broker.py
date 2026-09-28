from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from titan_brain.live.broker import (  # noqa: E402
    BrokerNativeReview,
    BrokerContractViolation,
    BrokerFactoryError,
    BrokerMutationBlocked,
    BrokerOperationResult,
    BrokerUnknownSubmission,
    BrokerSide,
    ClientRefLookupResult,
    ClientRefRecoverySource,
    CollectedObservation,
    EquityOrderType,
    FakeBrokerClient,
    FillSnapshot,
    MarketHours,
    LocalPreflightDecision,
    OperationStatus,
    OrderCoverageContract,
    OrderFamily,
    OrderFamilyCoverage,
    OrderFamilyCoverageStatus,
    OrderFamilyPage,
    OrderRequest,
    OrderSnapshot,
    ProductionAccountBase,
    ProviderSnapshot,
    ProductionTransportDescriptor,
    RobinhoodBrokerAdapter,
    SupportedProductionBrokerAdapter,
    TimeInForce,
    build_broker_client,
)
from titan_brain.live.models import BrokerOrderState  # noqa: E402
from titan_brain.live.composition import (  # noqa: E402
    RuntimeComposition,
    RuntimeCompositionError,
)
from titan_brain.live.reconcile import validate_authoritative_snapshot  # noqa: E402


NOW = datetime(2026, 9, 8, 14, 0, tzinfo=timezone.utc)
ACCOUNT = "••••7153"
ACCOUNT_BINDING = "b" * 64
AUTHORIZATION_BINDING = "c" * 64
REF_1 = "00000000-0000-4000-8000-000000000001"
REF_2 = "00000000-0000-4000-8000-000000000002"
REF_3 = "00000000-0000-4000-8000-000000000003"


def complete_history_contract(
    *, negative_results_authoritative: bool = True
) -> OrderCoverageContract:
    return OrderCoverageContract(
        contract_version="supported-test-history-v1",
        evidence_observed_at=NOW,
        families=tuple(
            OrderFamilyCoverage(
                family=family,
                status=OrderFamilyCoverageStatus.COMPLETE_GENERAL,
                evidence_id=f"fixture:{family.value}:all-pages",
                broker_authoritative=True,
                all_pages_consumed=True,
                includes_working_orders_across_dates=True,
                includes_parent_child_conditional=(
                    family is OrderFamily.ADVANCED_EQUITY
                ),
            )
            for family in OrderFamily
        ),
        client_ref_recovery_source=ClientRefRecoverySource.EXHAUSTIVE_ORDER_HISTORY,
        broker_preserves_client_ref=True,
        negative_client_ref_results_authoritative=negative_results_authoritative,
    )


def order(order_id: str, client_ref: str) -> OrderSnapshot:
    return OrderSnapshot(
        broker_order_id=order_id,
        account_masked=ACCOUNT,
        symbol="TEST",
        side=BrokerSide.BUY,
        order_type=EquityOrderType.LIMIT,
        state=BrokerOrderState.CONFIRMED,
        requested_quantity=Decimal("1"),
        cumulative_filled_quantity=Decimal("0"),
        market_hours=MarketHours.REGULAR,
        time_in_force=TimeInForce.GFD,
        broker_updated_at=NOW,
        received_at=NOW,
        limit_price=Decimal("10.00"),
        client_ref_id=client_ref,
    )


class FixtureProductionTransport:
    def __init__(
        self, *, loop: bool = False, require_confirmation: bool = False
    ) -> None:
        fake = FakeBrokerClient(
            clock=lambda: NOW,
            require_explicit_confirmation=require_confirmation,
        )
        self.fake = fake
        self.base = replace(
            fake.get_account_snapshot(ACCOUNT),
            equity_orders=(),
            option_order_count=0,
            advanced_order_count=0,
            standard_equity_orders_complete=False,
            option_orders_complete=False,
            advanced_orders_complete=False,
        )
        capabilities = replace(
            fake.capabilities,
            connector="supported-production-fixture",
            supports_advanced_order_read=False,
            supports_option_order_read=False,
            order_coverage=complete_history_contract(),
        )
        self._descriptor = ProductionTransportDescriptor(
            transport_id="fixture-supported-transport-v1",
            exact_account_id="private-account-uuid-1",
            account_binding_fingerprint=ACCOUNT_BINDING,
            authorization_binding_id=AUTHORIZATION_BINDING,
            capabilities=capabilities,
            maximum_order_pages_per_family=4,
        )
        self.loop = loop
        self.calls: list[tuple[OrderFamily, str | None]] = []
        self.clock_value = NOW

    def clock(self) -> datetime:
        return self.clock_value

    @property
    def descriptor(self) -> ProductionTransportDescriptor:
        return self._descriptor

    def release_components(self):
        # The fixture has no injected executable dependencies. Production
        # transports must explicitly enumerate any they retain.
        return ()

    def get_account_base(self, exact_account_id: str):
        self._account(exact_account_id)
        return ProductionAccountBase(
            snapshot=self.base,
            snapshot_token="fixture-provider-snapshot-1",
            snapshot_token_source="fixture:documented-version-field",
        )

    def list_order_family_page(
        self, exact_account_id: str, family: OrderFamily, cursor: str | None
    ) -> OrderFamilyPage:
        self._account(exact_account_id)
        self.calls.append((family, cursor))
        if family is OrderFamily.STANDARD_EQUITY and cursor is None:
            return self._page(family, "standard-1", (order("one", REF_1),), "")
        if family is OrderFamily.STANDARD_EQUITY and cursor == "":
            return self._page(
                family,
                "standard-2" if not self.loop else "standard-loop",
                (order("two", REF_2),),
                "" if self.loop else None,
            )
        return self._page(family, f"{family.value}-terminal", (), None)

    def lookup_equity_orders_by_client_ref(self, exact_account_id, client_refs):
        raise AssertionError("history-backed recovery must not call a dedicated endpoint")

    def review_equity_order(self, exact_account_id, request):
        self._account(exact_account_id)
        review = self.fake.review_equity_order(request)
        return BrokerNativeReview(
            request=review.request,
            reviewed_at=review.reviewed_at,
            expires_at=review.expires_at,
            disclosure=review.disclosure,
            order_checks=review.order_checks,
            required_confirmation_phrase=review.required_confirmation_phrase,
            broker_review_id=review.broker_review_id,
            broker_bound=review.broker_bound,
            preview=review.preview,
            received_at=review.received_at,
        )

    def place_equity_order(self, exact_account_id, request, **kwargs):
        self._account(exact_account_id)
        return self.fake.place_equity_order(request, **kwargs)

    def cancel_equity_order(self, exact_account_id, broker_order_id, **kwargs):
        self._account(exact_account_id)
        return self.fake.cancel_equity_order(ACCOUNT, broker_order_id, **kwargs)

    def _page(self, family, page_id, orders, next_cursor):
        page_index = 1 if family is OrderFamily.STANDARD_EQUITY and page_id.startswith("standard-2") else 0
        if page_id == "standard-loop":
            page_index = 1
        return OrderFamilyPage(
            account_masked=ACCOUNT,
            family=family,
            snapshot_token="fixture-provider-snapshot-1",
            page_id=page_id,
            orders=orders,
            active_order_count=sum(not item.state.terminal for item in orders),
            observed_at=self.clock_value,
            received_at=self.clock_value,
            next_cursor=next_cursor,
            page_index=page_index,
        )

    @staticmethod
    def _account(value: str) -> None:
        if value != "private-account-uuid-1":
            raise AssertionError("adapter exposed or changed the private account binding")


class SlowSecondPageTransport(FixtureProductionTransport):
    def list_order_family_page(self, exact_account_id, family, cursor):
        if family is OrderFamily.STANDARD_EQUITY and cursor == "":
            self.clock_value = NOW + timedelta(seconds=6)
        page = super().list_order_family_page(exact_account_id, family, cursor)
        return page


class CrossSnapshotPageTransport(FixtureProductionTransport):
    def list_order_family_page(self, exact_account_id, family, cursor):
        page = super().list_order_family_page(exact_account_id, family, cursor)
        if family is OrderFamily.STANDARD_EQUITY and cursor == "":
            return replace(page, snapshot_token="different-provider-snapshot")
        return page


class StaleAccountReceiptTransport(FixtureProductionTransport):
    def get_account_base(self, exact_account_id):
        envelope = super().get_account_base(exact_account_id)
        stale = NOW - timedelta(seconds=3)
        return replace(
            envelope,
            snapshot=replace(
                envelope.snapshot,
                observed_at=stale,
                received_at=stale,
                risk_evidence_as_of=stale,
            ),
        )


class FuturePageReceiptTransport(FixtureProductionTransport):
    def list_order_family_page(self, exact_account_id, family, cursor):
        page = super().list_order_family_page(exact_account_id, family, cursor)
        return replace(
            page,
            observed_at=NOW + timedelta(seconds=3),
            received_at=NOW + timedelta(seconds=3),
        )


class DedicatedLookupTransport(FixtureProductionTransport):
    def __init__(self, result: ClientRefLookupResult) -> None:
        super().__init__()
        coverage = replace(
            self.descriptor.capabilities.order_coverage,
            client_ref_recovery_source=ClientRefRecoverySource.DEDICATED_LOOKUP,
        )
        self._descriptor = replace(
            self.descriptor,
            capabilities=replace(
                self.descriptor.capabilities,
                order_coverage=coverage,
            ),
        )
        self.result = result

    def lookup_equity_orders_by_client_ref(self, exact_account_id, client_refs):
        self._account(exact_account_id)
        return self.result


class StalePlaceResultTransport(FixtureProductionTransport):
    def place_equity_order(self, exact_account_id, request, **kwargs):
        self._account(exact_account_id)
        stale_at = NOW - timedelta(seconds=3)
        return BrokerOperationResult(
            operation="place_equity_order",
            status=OperationStatus.REJECTED,
            observed_at=stale_at,
            received_at=stale_at,
            accepted=False,
            message="stale rejection",
        )


class FuturePlaceReceiptTransport(FixtureProductionTransport):
    def place_equity_order(self, exact_account_id, request, **kwargs):
        self._account(exact_account_id)
        return BrokerOperationResult(
            operation="place_equity_order",
            status=OperationStatus.REJECTED,
            observed_at=NOW,
            received_at=NOW + timedelta(seconds=3),
            accepted=False,
            message="future receipt",
        )


class ContradictoryRejectedPlaceTransport(FixtureProductionTransport):
    def place_equity_order(self, exact_account_id, request, **kwargs):
        self._account(exact_account_id)
        live_order = order("contradictory-live-order", request.client_ref_id)
        return BrokerOperationResult(
            operation="place_equity_order",
            status=OperationStatus.REJECTED,
            observed_at=NOW,
            received_at=NOW,
            accepted=False,
            message="rejected while returning a working order",
            order=live_order,
        )


class CollectedObservationTransport(FixtureProductionTransport):
    def __init__(self, *, move_on_second_collection: bool = False) -> None:
        super().__init__()
        self.collection_number = 0
        self.current_collection_id = "0" * 64
        self.move_on_second_collection = move_on_second_collection

    def get_account_base(self, exact_account_id):
        self._account(exact_account_id)
        self.collection_number += 1
        self.current_collection_id = f"{self.collection_number:064x}"
        return CollectedObservation(
            snapshot=self.base,
            collection_id=self.current_collection_id,
            request_started_at=NOW,
            request_completed_at=NOW,
        )

    def list_order_family_page(self, exact_account_id, family, cursor):
        page = super().list_order_family_page(exact_account_id, family, cursor)
        orders = page.orders
        active = page.active_order_count
        if (
            self.move_on_second_collection
            and self.collection_number == 2
            and family is OrderFamily.STANDARD_EQUITY
            and cursor is None
        ):
            extra = order("moving-order", REF_3)
            orders = orders + (extra,)
            active += 1
        return replace(
            page,
            snapshot_token=None,
            collection_id=self.current_collection_id,
            orders=orders,
            active_order_count=active,
        )


class EventuallyConsistentHistoryTransport(CollectedObservationTransport):
    def __init__(self) -> None:
        super().__init__()
        coverage = complete_history_contract(negative_results_authoritative=False)
        self._descriptor = replace(
            self.descriptor,
            capabilities=replace(
                self.descriptor.capabilities,
                order_coverage=coverage,
            ),
        )
        self.published = False

    def list_order_family_page(self, exact_account_id, family, cursor):
        page = super().list_order_family_page(exact_account_id, family, cursor)
        if family is OrderFamily.STANDARD_EQUITY:
            if cursor is None:
                orders = (order("delayed", REF_3),) if self.published else ()
                return replace(
                    page,
                    orders=orders,
                    active_order_count=len(orders),
                    next_cursor=None,
                )
            raise AssertionError("eventually consistent fixture has one page")
        return page


class MissingPageTransport(FixtureProductionTransport):
    def list_order_family_page(self, exact_account_id, family, cursor):
        page = super().list_order_family_page(exact_account_id, family, cursor)
        if family is OrderFamily.STANDARD_EQUITY and cursor == "":
            return replace(page, page_index=2)
        return page


class DirectApiNoPreviewTransport(FixtureProductionTransport):
    def review_equity_order(self, exact_account_id, request):
        self._account(exact_account_id)
        return LocalPreflightDecision(
            request=request,
            reviewed_at=NOW,
            received_at=NOW,
            expires_at=NOW + timedelta(seconds=30),
            disclosure="Local deterministic policy preflight; not broker-issued.",
            order_checks=(),
            required_confirmation_phrase=None,
            broker_review_id=None,
            broker_bound=False,
            preview={},
            decision_id="local-decision-1",
            policy_binding_id="policy-sha256:test",
            evidence_collection_id="collection-sha256:test",
            provider_contract_id="direct-api-contract-v1",
        )

    def place_equity_order(self, exact_account_id, request, **kwargs):
        self._account(exact_account_id)
        accepted = order("direct-api-order", request.client_ref_id)
        return BrokerOperationResult(
            operation="place_equity_order",
            status=OperationStatus.ACKNOWLEDGED,
            observed_at=NOW,
            received_at=NOW,
            accepted=True,
            message="accepted",
            order=accepted,
        )


class AttendedRouteLocalPreflightTransport(DirectApiNoPreviewTransport):
    def __init__(self) -> None:
        super().__init__(require_confirmation=True)


class ProductionBrokerContractTests(unittest.TestCase):
    @staticmethod
    def _place(adapter: SupportedProductionBrokerAdapter):
        request = OrderRequest(
            account_masked=ACCOUNT,
            symbol="TEST",
            side=BrokerSide.BUY,
            order_type=EquityOrderType.LIMIT,
            quantity=1,
            market_hours=MarketHours.REGULAR,
            time_in_force=TimeInForce.GFD,
            client_ref_id=REF_3,
            limit_price=Decimal("10.00"),
        )
        review = adapter.review_equity_order(request)
        return adapter.place_equity_order(request, review=review)

    def test_operation_result_must_be_temporally_bound_to_transport_call(self) -> None:
        with self.assertRaises(BrokerUnknownSubmission) as stale:
            self._place(
                SupportedProductionBrokerAdapter(
                    StalePlaceResultTransport(), clock=lambda: NOW
                )
            )
        self.assertTrue(stale.exception.submission_may_have_reached_broker)
        with self.assertRaises(BrokerUnknownSubmission) as future:
            self._place(
                SupportedProductionBrokerAdapter(
                    FuturePlaceReceiptTransport(), clock=lambda: NOW
                )
            )
        self.assertTrue(future.exception.submission_may_have_reached_broker)

    def test_rejected_place_cannot_carry_possible_broker_exposure(self) -> None:
        with self.assertRaises(BrokerUnknownSubmission) as rejected:
            self._place(
                SupportedProductionBrokerAdapter(
                    ContradictoryRejectedPlaceTransport(), clock=lambda: NOW
                )
            )
        self.assertTrue(rejected.exception.submission_may_have_reached_broker)

    def test_dedicated_lookup_receipt_is_bounded_to_call_completion(self) -> None:
        result = ClientRefLookupResult(
            account_masked=ACCOUNT,
            requested_client_refs=(REF_3,),
            found_orders=(),
            confirmed_absent_client_refs=(REF_3,),
            observed_at=NOW,
            received_at=NOW + timedelta(seconds=3),
            complete=True,
        )
        adapter = SupportedProductionBrokerAdapter(
            DedicatedLookupTransport(result), clock=lambda: NOW
        )
        with self.assertRaisesRegex(BrokerContractViolation, "receipt is in the future"):
            adapter.lookup_equity_orders_by_client_ref(ACCOUNT, (REF_3,))

    def test_lookup_rejects_order_received_after_lookup_completion(self) -> None:
        late_order = replace(
            order("late-lookup-order", REF_1),
            received_at=NOW + timedelta(seconds=1),
        )
        with self.assertRaisesRegex(ValueError, "follow lookup receipt"):
            ClientRefLookupResult(
                account_masked=ACCOUNT,
                requested_client_refs=(REF_1,),
                found_orders=(late_order,),
                confirmed_absent_client_refs=(),
                observed_at=NOW,
                received_at=NOW,
                complete=True,
            )

    def test_page_rejects_facts_after_provider_observation_or_receipt(self) -> None:
        future_order = replace(
            order("future-order", REF_1),
            broker_updated_at=NOW + timedelta(seconds=3),
            received_at=NOW + timedelta(seconds=3),
        )
        with self.assertRaisesRegex(ValueError, "provider observation"):
            OrderFamilyPage(
                account_masked=ACCOUNT,
                family=OrderFamily.STANDARD_EQUITY,
                snapshot_token="snapshot",
                page_id="future-order-page",
                orders=(future_order,),
                active_order_count=1,
                observed_at=NOW,
                received_at=NOW + timedelta(seconds=3),
                next_cursor=None,
            )

        late_receipt = replace(
            order("late-receipt", REF_1),
            received_at=NOW + timedelta(seconds=1),
        )
        with self.assertRaisesRegex(ValueError, "containing page receipt"):
            OrderFamilyPage(
                account_masked=ACCOUNT,
                family=OrderFamily.STANDARD_EQUITY,
                snapshot_token="snapshot",
                page_id="late-receipt-page",
                orders=(late_receipt,),
                active_order_count=1,
                observed_at=NOW,
                received_at=NOW,
                next_cursor=None,
            )

    def test_page_rejects_fill_after_provider_observation(self) -> None:
        future_fill = FillSnapshot(
            fill_id="future-fill",
            quantity=Decimal("1"),
            price=Decimal("10.00"),
            executed_at=NOW + timedelta(seconds=3),
        )
        filled = replace(
            order("filled", REF_1),
            state=BrokerOrderState.FILLED,
            cumulative_filled_quantity=Decimal("1"),
            received_at=NOW + timedelta(seconds=3),
            fills=(future_fill,),
        )
        with self.assertRaisesRegex(ValueError, "fill fact"):
            OrderFamilyPage(
                account_masked=ACCOUNT,
                family=OrderFamily.STANDARD_EQUITY,
                snapshot_token="snapshot",
                page_id="future-fill-page",
                orders=(filled,),
                active_order_count=0,
                observed_at=NOW,
                received_at=NOW + timedelta(seconds=3),
                next_cursor=None,
            )

    def test_read_receipts_are_bound_to_actual_transport_call(self) -> None:
        with self.assertRaisesRegex(BrokerContractViolation, "predates"):
            SupportedProductionBrokerAdapter(
                StaleAccountReceiptTransport(), clock=lambda: NOW
            ).get_account_snapshot(ACCOUNT)
        with self.assertRaisesRegex(BrokerContractViolation, "in the future"):
            SupportedProductionBrokerAdapter(
                FuturePageReceiptTransport(), clock=lambda: NOW
            ).get_account_snapshot(ACCOUNT)

    def test_account_base_rejects_risk_fact_after_provider_observation(self) -> None:
        transport = FixtureProductionTransport()
        snapshot = replace(
            transport.base,
            received_at=NOW + timedelta(seconds=3),
            risk_evidence_as_of=NOW + timedelta(seconds=3),
        )
        with self.assertRaisesRegex(ValueError, "risk fact"):
            ProductionAccountBase(
                snapshot=snapshot,
                snapshot_token="snapshot",
                snapshot_token_source="fixture:documented-version-field",
            )

    def test_client_ref_result_rejects_cross_account_found_order(self) -> None:
        with self.assertRaisesRegex(ValueError, "lookup account"):
            ClientRefLookupResult(
                account_masked=ACCOUNT,
                requested_client_refs=(REF_1,),
                found_orders=(replace(order("wrong-account", REF_1), account_masked="••••9999"),),
                confirmed_absent_client_refs=(),
                observed_at=NOW,
                received_at=NOW,
                complete=True,
            )

    def test_general_endpoint_can_prove_advanced_and_option_coverage(self) -> None:
        capabilities = FixtureProductionTransport().descriptor.capabilities
        self.assertFalse(capabilities.supports_advanced_order_read)
        self.assertFalse(capabilities.supports_option_order_read)
        self.assertTrue(capabilities.can_prove_whole_broker_reconciliation)

    def test_coverage_contract_rejects_unproven_advanced_semantics(self) -> None:
        families = list(complete_history_contract().families)
        advanced = next(
            index
            for index, item in enumerate(families)
            if item.family is OrderFamily.ADVANCED_EQUITY
        )
        with self.assertRaisesRegex(ValueError, "parent/child/conditional"):
            families[advanced] = replace(
                families[advanced], includes_parent_child_conditional=False
            )

    def test_snapshot_consumes_every_page_including_empty_cursor(self) -> None:
        transport = FixtureProductionTransport()
        adapter = SupportedProductionBrokerAdapter(transport, clock=transport.clock)
        snapshot = adapter.get_account_snapshot(ACCOUNT)
        self.assertEqual(
            [item.broker_order_id for item in snapshot.equity_orders], ["one", "two"]
        )
        self.assertIn((OrderFamily.STANDARD_EQUITY, ""), transport.calls)
        self.assertTrue(snapshot.whole_broker_reconciled)

    def test_slow_pagination_preserves_earliest_observation_for_freshness(self) -> None:
        transport = SlowSecondPageTransport()
        adapter = SupportedProductionBrokerAdapter(transport, clock=transport.clock)
        snapshot = adapter.get_account_snapshot(ACCOUNT)
        self.assertEqual(snapshot.observed_at, NOW)
        self.assertEqual(snapshot.received_at, NOW + timedelta(seconds=6))
        report = validate_authoritative_snapshot(
            snapshot,
            adapter.capabilities,
            account_masked=ACCOUNT,
            now=NOW + timedelta(seconds=6),
            max_age=timedelta(seconds=5),
        )
        self.assertIn("STALE_SNAPSHOT", report.blockers)

    def test_cursor_loop_fails_entire_snapshot(self) -> None:
        adapter = SupportedProductionBrokerAdapter(
            FixtureProductionTransport(loop=True), clock=lambda: NOW
        )
        with self.assertRaisesRegex(BrokerContractViolation, "cursor loop"):
            adapter.get_account_snapshot(ACCOUNT)

    def test_pagination_cannot_cross_provider_snapshot_token(self) -> None:
        adapter = SupportedProductionBrokerAdapter(
            CrossSnapshotPageTransport(), clock=lambda: NOW
        )
        with self.assertRaisesRegex(BrokerContractViolation, "snapshot tokens"):
            adapter.get_account_snapshot(ACCOUNT)

    def test_missing_page_index_fails_the_entire_collection(self) -> None:
        adapter = SupportedProductionBrokerAdapter(
            MissingPageTransport(), clock=lambda: NOW
        )
        with self.assertRaisesRegex(BrokerContractViolation, "skipped or reordered"):
            adapter.get_account_snapshot(ACCOUNT)

    def test_non_atomic_collection_requires_two_stable_reads(self) -> None:
        stable = CollectedObservationTransport()
        snapshot = SupportedProductionBrokerAdapter(
            stable, clock=stable.clock
        ).get_account_snapshot(ACCOUNT)
        self.assertEqual(len(snapshot.equity_orders), 2)
        self.assertEqual(stable.collection_number, 2)

        moving = CollectedObservationTransport(move_on_second_collection=True)
        with self.assertRaisesRegex(BrokerContractViolation, "state moved"):
            SupportedProductionBrokerAdapter(
                moving, clock=moving.clock
            ).get_account_snapshot(ACCOUNT)

    def test_eventually_consistent_history_absence_remains_not_seen_yet(self) -> None:
        transport = EventuallyConsistentHistoryTransport()
        adapter = SupportedProductionBrokerAdapter(transport, clock=lambda: NOW)
        first = adapter.lookup_equity_orders_by_client_ref(ACCOUNT, (REF_3,))
        second = adapter.lookup_equity_orders_by_client_ref(ACCOUNT, (REF_3,))
        for result in (first, second):
            self.assertEqual(result.found_orders, ())
            self.assertEqual(result.confirmed_absent_client_refs, ())
            self.assertEqual(result.not_seen_yet_client_refs, (REF_3,))
            self.assertTrue(result.complete)

        transport.published = True
        published = adapter.lookup_equity_orders_by_client_ref(ACCOUNT, (REF_3,))
        self.assertEqual(
            tuple(item.client_ref_id for item in published.found_orders), (REF_3,)
        )
        self.assertEqual(published.confirmed_absent_client_refs, ())
        self.assertEqual(published.not_seen_yet_client_refs, ())

    def test_exhaustive_history_recovers_exact_refs_and_authoritative_absence(self) -> None:
        adapter = SupportedProductionBrokerAdapter(
            FixtureProductionTransport(), clock=lambda: NOW
        )
        result = adapter.lookup_equity_orders_by_client_ref(
            ACCOUNT, (REF_2, REF_3)
        )
        self.assertEqual(
            tuple(item.client_ref_id for item in result.found_orders), (REF_2,)
        )
        self.assertEqual(result.confirmed_absent_client_refs, (REF_3,))
        self.assertTrue(result.complete)

    def test_broker_bound_review_is_one_shot_and_preserves_exact_confirmation(self) -> None:
        transport = FixtureProductionTransport(require_confirmation=True)
        adapter = SupportedProductionBrokerAdapter(transport, clock=lambda: NOW)
        request = OrderRequest(
            account_masked=ACCOUNT,
            symbol="TEST",
            side=BrokerSide.BUY,
            order_type=EquityOrderType.LIMIT,
            quantity=1,
            market_hours=MarketHours.REGULAR,
            time_in_force=TimeInForce.GFD,
            client_ref_id=REF_3,
            limit_price=Decimal("10.00"),
        )
        review = adapter.review_equity_order(request)
        with self.assertRaisesRegex(BrokerMutationBlocked, "exact review confirmation"):
            adapter.place_equity_order(
                request, review=review, explicit_confirmation="not the phrase"
            )
        placed = adapter.place_equity_order(
            request,
            review=review,
            explicit_confirmation=review.required_confirmation_phrase,
        )
        self.assertEqual(placed.order.client_ref_id, request.client_ref_id)
        with self.assertRaisesRegex(BrokerContractViolation, "exact unexpired"):
            adapter.place_equity_order(
                request,
                review=review,
                explicit_confirmation=review.required_confirmation_phrase,
            )

    def test_direct_api_without_preview_id_uses_explicit_local_preflight(self) -> None:
        transport = DirectApiNoPreviewTransport()
        adapter = SupportedProductionBrokerAdapter(transport, clock=lambda: NOW)
        request = OrderRequest(
            account_masked=ACCOUNT,
            symbol="TEST",
            side=BrokerSide.BUY,
            order_type=EquityOrderType.LIMIT,
            quantity=1,
            market_hours=MarketHours.REGULAR,
            time_in_force=TimeInForce.GFD,
            client_ref_id=REF_3,
            limit_price=Decimal("10.00"),
        )
        review = adapter.review_equity_order(request)
        self.assertIsInstance(review, LocalPreflightDecision)
        self.assertFalse(review.broker_bound)
        self.assertIsNone(review.broker_review_id)
        placed = adapter.place_equity_order(request, review=review)
        self.assertEqual(placed.order.client_ref_id, REF_3)

    def test_local_preflight_cannot_replace_attended_confirmation(self) -> None:
        adapter = SupportedProductionBrokerAdapter(
            AttendedRouteLocalPreflightTransport(), clock=lambda: NOW
        )
        request = OrderRequest(
            account_masked=ACCOUNT,
            symbol="TEST",
            side=BrokerSide.BUY,
            order_type=EquityOrderType.LIMIT,
            quantity=1,
            market_hours=MarketHours.REGULAR,
            time_in_force=TimeInForce.GFD,
            client_ref_id=REF_3,
            limit_price=Decimal("10.00"),
        )
        with self.assertRaisesRegex(
            BrokerContractViolation, "cannot replace mandatory"
        ):
            adapter.review_equity_order(request)

    def test_named_factory_preserves_attended_path_and_requires_injected_production(self) -> None:
        attended = build_broker_client(
            {"broker_adapter": "robinhood_codex_connector"},
            account_masked=ACCOUNT,
        )
        self.assertIsInstance(attended, RobinhoodBrokerAdapter)
        with self.assertRaisesRegex(BrokerFactoryError, "injected authorized transport"):
            build_broker_client(
                {
                    "broker_adapter": "supported_production_transport",
                    "production_transport_id": "fixture-supported-transport-v1",
                    "production_account_binding_fingerprint": ACCOUNT_BINDING,
                    "production_authorization_binding_id": AUTHORIZATION_BINDING,
                },
                account_masked=ACCOUNT,
            )
        production = build_broker_client(
            {
                "broker_adapter": "supported_production_transport",
                "production_transport_id": "fixture-supported-transport-v1",
                "production_account_binding_fingerprint": ACCOUNT_BINDING,
                "production_authorization_binding_id": AUTHORIZATION_BINDING,
            },
            account_masked=ACCOUNT,
            production_transport=FixtureProductionTransport(),
        )
        self.assertIsInstance(production, SupportedProductionBrokerAdapter)

    def test_factory_rejects_different_exact_account_with_same_display_suffix(self) -> None:
        transport = FixtureProductionTransport()
        transport._descriptor = replace(
            transport.descriptor,
            exact_account_id="different-private-account-uuid",
            account_binding_fingerprint="d" * 64,
        )
        with self.assertRaisesRegex(BrokerFactoryError, "exact-account binding"):
            build_broker_client(
                {
                    "broker_adapter": "supported_production_transport",
                    "production_transport_id": "fixture-supported-transport-v1",
                    "production_account_binding_fingerprint": ACCOUNT_BINDING,
                    "production_authorization_binding_id": AUTHORIZATION_BINDING,
                },
                account_masked=ACCOUNT,
                production_transport=transport,
            )

    def test_runtime_profile_binds_capability_descriptor_across_processes(self) -> None:
        root = Path(__file__).resolve().parents[1]
        sources = (
            Path(__file__).resolve(),
            root / "src/titan_brain/live/broker/production.py",
        )
        files = []
        for source in sources:
            data = source.read_bytes()
            files.append(
                {
                    "path": source.relative_to(root).as_posix(),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "size": len(data),
                }
            )
        manifest = {"release_manifest_hash": "a" * 64, "files": files}
        first_transport = FixtureProductionTransport()
        second_transport = FixtureProductionTransport()
        first = RuntimeComposition(production_transport=first_transport)
        second = RuntimeComposition(production_transport=second_transport)
        first.bind_release(manifest, release_root=root)
        second.bind_release(manifest, release_root=root)
        self.assertEqual(first.runtime_profile_hash, second.runtime_profile_hash)

        first_transport._descriptor = replace(
            first_transport.descriptor,
            capabilities=replace(
                first_transport.descriptor.capabilities,
                supports_streaming=not first_transport.descriptor.capabilities.supports_streaming,
            ),
        )
        with self.assertRaisesRegex(
            RuntimeCompositionError, "bound instance profile changed"
        ):
            _ = first.runtime_profile_hash

    def test_uninventoried_dependency_descriptor_is_not_executed(self) -> None:
        class HostileTransport(FixtureProductionTransport):
            enumerator_accessed = False

            @property
            def release_components(self):
                self.enumerator_accessed = True
                return lambda: ()

        root = Path(__file__).resolve().parents[1]
        source = root / "src/titan_brain/live/broker/production.py"
        data = source.read_bytes()
        manifest = {
            "release_manifest_hash": "a" * 64,
            "files": [
                {
                    "path": source.relative_to(root).as_posix(),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "size": len(data),
                }
            ],
        }
        transport = HostileTransport()
        with self.assertRaisesRegex(
            RuntimeCompositionError, "IMPLEMENTATION_NOT_IN_MANIFEST"
        ):
            RuntimeComposition(production_transport=transport).bind_release(
                manifest, release_root=root
            )
        self.assertFalse(transport.enumerator_accessed)


if __name__ == "__main__":
    unittest.main()
