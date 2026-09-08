import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from titan_brain.live.broker import (  # noqa: E402
    AccountSnapshot,
    BrokerClient,
    BrokerContractViolation,
    BrokerMutationBlocked,
    BrokerSide,
    BrokerUnknownSubmission,
    EquityOrderType,
    FakeBrokerClient,
    FakeFault,
    FundsSnapshot,
    MarketHours,
    OperationStatus,
    OrderRequest,
    ReviewReceipt,
    RobinhoodBrokerAdapter,
    RobinhoodConnectorContract,
    TimeInForce,
)


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 8, 13, 35, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, *, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def limit_request(
    *,
    client_ref_id: str = "00000000-0000-4000-8000-000000000001",
    limit_price: str = "10.25",
) -> OrderRequest:
    return OrderRequest(
        account_masked="••••7153",
        symbol="TEST",
        side=BrokerSide.BUY,
        order_type=EquityOrderType.LIMIT,
        quantity=10,
        market_hours=MarketHours.REGULAR,
        time_in_force=TimeInForce.GFD,
        client_ref_id=client_ref_id,
        limit_price=Decimal(limit_price),
    )


class BrokerBoundaryTests(unittest.TestCase):
    def test_order_request_rejects_session_and_numeric_ambiguity(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be limit"):
            OrderRequest(
                account_masked="••••7153",
                symbol="TEST",
                side=BrokerSide.BUY,
                order_type=EquityOrderType.MARKET,
                quantity=1,
                market_hours=MarketHours.EXTENDED,
                time_in_force=TimeInForce.GFD,
                client_ref_id="00000000-0000-4000-8000-000000000001",
            )
        with self.assertRaises(ValueError):
            limit_request(limit_price="NaN")

    def test_fake_is_protocol_compatible_and_ids_are_deterministic(self) -> None:
        clock = MutableClock()
        broker = FakeBrokerClient(clock=clock)
        self.assertIsInstance(broker, BrokerClient)
        request = limit_request()
        review = broker.review_equity_order(request)
        result = broker.place_equity_order(request, review=review)
        self.assertEqual(result.status, OperationStatus.ACKNOWLEDGED)
        self.assertEqual(result.order.broker_order_id, "fake-order-0001")
        self.assertEqual(result.order.cumulative_filled_quantity, Decimal("0"))

        partial = broker.fill_order("fake-order-0001", 4, "10.24")
        self.assertEqual(partial.state.value, "PARTIALLY_FILLED")
        self.assertEqual(partial.fills[0].fill_id, "fake-fill-0001")
        cancel = broker.cancel_equity_order(
            "••••7153", "fake-order-0001"
        )
        self.assertEqual(cancel.status, OperationStatus.PENDING_CANCEL)
        settled = broker.settle_cancel("fake-order-0001")
        self.assertEqual(settled.state.value, "PARTIALLY_FILLED_REST_CANCELLED")

    def test_fake_binds_exact_unexpired_review_and_confirmation(self) -> None:
        clock = MutableClock()
        broker = FakeBrokerClient(clock=clock, require_explicit_confirmation=True)
        request = limit_request()
        review = broker.review_equity_order(request)
        with self.assertRaisesRegex(BrokerContractViolation, "exact review confirmation"):
            broker.place_equity_order(request, review=review, explicit_confirmation="yes")
        result = broker.place_equity_order(
            request,
            review=review,
            explicit_confirmation=review.required_confirmation_phrase,
        )
        self.assertTrue(result.accepted)

        changed = limit_request(
            client_ref_id="00000000-0000-4000-8000-000000000002",
            limit_price="10.26",
        )
        with self.assertRaisesRegex(BrokerContractViolation, "exact order tuple"):
            broker.place_equity_order(
                changed,
                review=review,
                explicit_confirmation=review.required_confirmation_phrase,
            )

        fresh_review = broker.review_equity_order(changed)
        clock.advance(seconds=31)
        with self.assertRaisesRegex(BrokerContractViolation, "expired"):
            broker.place_equity_order(
                changed,
                review=fresh_review,
                explicit_confirmation=fresh_review.required_confirmation_phrase,
            )

    def test_unknown_after_acceptance_is_not_retryable_and_is_reconcilable(self) -> None:
        clock = MutableClock()
        broker = FakeBrokerClient(clock=clock)
        request = limit_request()
        review = broker.review_equity_order(request)
        broker.inject_fault(FakeBrokerClient.PLACE, FakeFault.PLACE_UNKNOWN_AFTER_ACCEPT)
        with self.assertRaises(BrokerUnknownSubmission) as caught:
            broker.place_equity_order(request, review=review)
        self.assertFalse(caught.exception.retry_safe)
        self.assertTrue(caught.exception.submission_may_have_reached_broker)
        snapshot = broker.get_account_snapshot("••••7153")
        self.assertEqual(len(snapshot.equity_orders), 1)
        self.assertEqual(snapshot.equity_orders[0].client_ref_id, request.client_ref_id)

    def test_cancel_fill_race_returns_newer_filled_evidence(self) -> None:
        clock = MutableClock()
        broker = FakeBrokerClient(clock=clock)
        request = limit_request()
        review = broker.review_equity_order(request)
        placed = broker.place_equity_order(request, review=review)
        broker.inject_fault(FakeBrokerClient.CANCEL, FakeFault.CANCEL_FILL_RACE)
        result = broker.cancel_equity_order(
            "••••7153", placed.order.broker_order_id
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.order.state.value, "FILLED")
        self.assertEqual(result.order.cumulative_filled_quantity, Decimal("10"))

    def test_robinhood_contract_separates_tool_support_from_daemon_authority(self) -> None:
        contract = RobinhoodConnectorContract()
        capabilities = contract.capabilities()
        self.assertTrue(capabilities.supports_equity_place)
        self.assertTrue(capabilities.review_requires_explicit_confirmation)
        self.assertFalse(capabilities.daemon_transport_configured)
        self.assertFalse(capabilities.supports_daemon_writes)
        self.assertFalse(capabilities.supports_unattended_writes)
        self.assertFalse(capabilities.supports_advanced_order_read)
        self.assertFalse(capabilities.can_prove_whole_broker_reconciliation)
        self.assertEqual(contract.server_version, "1.4.0")
        self.assertIn(
            (
                "place_equity_order",
                "get explicit user confirmation before calling this tool",
            ),
            contract.server_advertised_confirmation_text,
        )
        self.assertIn(
            ("cancel_equity_order", "Always confirm with the user before calling"),
            contract.server_advertised_confirmation_text,
        )
        self.assertTrue(
            any(
                "get_advanced_orders is not advertised" in item
                for item in contract.unsupported_operations
            )
        )
        self.assertTrue(
            any(
                "output contains no broker-preserved client ref_id" in item
                for item in contract.unsupported_operations
            )
        )

    def test_robinhood_adapter_blocks_review_place_and_cancel(self) -> None:
        adapter = RobinhoodBrokerAdapter()
        request = limit_request()
        with self.assertRaises(BrokerMutationBlocked):
            adapter.review_equity_order(request)
        dummy_review = ReviewReceipt(
            request=request,
            reviewed_at=datetime(2026, 9, 8, 13, 35, tzinfo=timezone.utc),
            expires_at=None,
            disclosure="",
            order_checks=(),
            required_confirmation_phrase=None,
            broker_review_id=None,
            broker_bound=False,
        )
        with self.assertRaises(BrokerMutationBlocked):
            adapter.place_equity_order(request, review=dummy_review)
        with self.assertRaises(BrokerMutationBlocked):
            adapter.cancel_equity_order("••••7153", "order-1")
        self.assertEqual(len(adapter.blocked_mutations), 3)

    def test_robinhood_adapter_accepts_only_normalized_supplied_read_evidence(self) -> None:
        observed = datetime(2026, 9, 7, 22, 51, 12, tzinfo=timezone.utc)
        snapshot = AccountSnapshot(
            account_masked="••••7153",
            observed_at=observed,
            received_at=observed,
            account_state="active",
            account_type="individual/limited_margin/self_directed",
            funds=FundsSnapshot(
                total_value="1000.00",
                cash="1000.00",
                buying_power="1000.0000",
                unleveraged_buying_power="1000.0000",
                unsettled_funds="250.0000",
                unsettled_funds_is_order_gating=False,
            ),
            equity_positions=(),
            equity_orders=(),
            option_position_count=0,
            option_order_count=0,
            advanced_order_count=0,
            standard_equity_positions_complete=True,
            standard_equity_orders_complete=True,
            option_positions_complete=True,
            option_orders_complete=True,
            advanced_orders_complete=False,
            auth_point_in_time=True,
        )
        adapter = RobinhoodBrokerAdapter(snapshot_provider=lambda _: snapshot)
        result = adapter.get_account_snapshot("••••7153")
        self.assertFalse(result.whole_broker_reconciled)
        self.assertTrue(result.auth_point_in_time)
        self.assertFalse(result.funds.unsettled_funds_is_order_gating)

    def test_capability_evidence_is_masked_and_point_in_time(self) -> None:
        path = (
            Path(__file__).resolve().parents[1]
            / "validation"
            / "full-live"
            / "2026-09-08"
            / "CAPABILITIES.json"
        )
        raw = path.read_text(encoding="utf-8")
        document = json.loads(raw)
        self.assertEqual(document["account_masked"], "ending-7153")
        self.assertTrue(
            document["authenticated_read_only_account_evidence"]["point_in_time_only"]
        )
        self.assertFalse(
            document["authenticated_read_only_account_evidence"]
            ["positions_and_orders"]["whole_broker_flatness_proven"]
        )
        self.assertFalse(
            document["current_authenticated_connector_contract"]
            ["safe_for_unattended_stock_launcher"]
        )
        self.assertNotIn("account_number", raw.lower())


if __name__ == "__main__":
    unittest.main()
