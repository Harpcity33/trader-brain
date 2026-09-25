"""Synthetic attended-control tests. No socket or real broker is used."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from titan_brain.live.attended_control import (
    AttendedControlError,
    AttendedOrderControl,
    AttendedReviewStore,
)
from titan_brain.live.broker.base import (
    AttendedCancelReview,
    AttendedLocalReview,
    BrokerMutationBlocked,
    BrokerOperationResult,
    BrokerSide,
    BrokerUnknownSubmission,
    ClientRefRecoverySource,
    EquityOrderType,
    MarketHours,
    OperationStatus,
    OrderCheck,
    OrderCoverageContract,
    OrderFamily,
    OrderFamilyCoverage,
    OrderFamilyCoverageStatus,
    OrderRequest,
    TimeInForce,
)
from titan_brain.live.broker.ibkr_transport import attended_ibkr_descriptor
from titan_brain.live.cli import build_parser


NOW = datetime(2026, 9, 14, 14, 0, tzinfo=timezone.utc)
MASK = "****3103"
PLAN_ID = "f" * 64
STOP_REF = "10000000-0000-4000-8000-000000000003"


def coverage() -> OrderCoverageContract:
    return OrderCoverageContract(
        contract_version="synthetic-attended-v1",
        evidence_observed_at=NOW,
        families=tuple(
            OrderFamilyCoverage(
                family=family,
                status=OrderFamilyCoverageStatus.COMPLETE_GENERAL,
                evidence_id=f"synthetic-{family.value}",
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
        negative_client_ref_results_authoritative=False,
    )


class FakeRuntime:
    account_key = "ibkr-live-ending-3103"
    account_masked = MASK

    def __init__(self, clock, broker_state=None):
        self.clock = clock
        self.broker_state = broker_state if broker_state is not None else {
            "filled_quantity": 0,
            "place_error": None,
        }
        self.capabilities = attended_ibkr_descriptor(
            exact_account_id="DU0000000",
            account_masked=MASK,
            account_binding_fingerprint="a" * 64,
            authorization_binding_id="b" * 64,
            coverage=coverage(),
        ).capabilities
        self.place_calls = []
        self.cancel_calls = []
        self.prepare_calls = 0
        self.prepare_error = None
        self.capacity = "1000.00"
        self.protection_review_calls = []

    def prepare_mutation(self):
        self.prepare_calls += 1
        if self.prepare_error is not None:
            raise self.prepare_error

    def review_order(self, request):
        now = self.clock()
        phrase = (
            f"CONFIRM IBKR {request.side.value.upper()} {request.quantity} "
            f"{request.symbol} {request.order_type.value.upper()} "
            f"{request.limit_price or request.stop_price} "
            f"{request.time_in_force.value.upper()} REGULAR_HOURS"
        )
        required_stop = None
        purpose = "protection" if request.side is BrokerSide.SELL else "entry"
        if request.side is BrokerSide.BUY:
            required_stop = {
                "side": "sell",
                "symbol": request.symbol,
                "quantity": request.quantity,
                "order_type": "stop_market",
                "limit_price": None,
                "stop_price": "9.50",
                "time_in_force": "gtc",
                "market_hours": "regular_hours",
                "client_ref_id": STOP_REF,
            }
        return AttendedLocalReview(
            request=request,
            reviewed_at=now,
            received_at=now,
            expires_at=now + timedelta(seconds=30),
            disclosure="Synthetic attended preview; acceptance and protection are unproven.",
            order_checks=(
                OrderCheck("WHOLE_ACCOUNT", "INFO", "All families reconciled."),
                OrderCheck("NO_BORROW", "INFO", "Capacity includes reserves."),
            ),
            required_confirmation_phrase=phrase,
            broker_review_id=None,
            broker_bound=False,
            preview={
                "session": "regular_hours",
                "order": {
                    "symbol": request.symbol,
                    "quantity": request.quantity,
                },
                "risk": {"plan_id": PLAN_ID, "purpose": purpose, "planned_downside": "10.00"},
                "capacity": {"remaining_after_candidate": self.capacity},
                "required_stop": required_stop,
                "alerts": (
                    "Direct API attempt remains unresolved until reconciliation.",
                    "A fill is not protected until a stop is confirmed working.",
                ),
            },
            decision_id=str(uuid4()),
            policy_binding_id="c" * 64,
            evidence_collection_id="d" * 64,
            provider_contract_id="e" * 64,
        )

    def place_order(self, request, review, exact_confirmation):
        self.place_calls.append((request, review, exact_confirmation))
        error = self.broker_state.get("place_error")
        if error is not None:
            raise error
        now = self.clock()
        return BrokerOperationResult(
            operation="place_equity_order",
            status=OperationStatus.UNKNOWN,
            observed_at=now,
            received_at=now,
            accepted=None,
            message="synthetic unresolved",
        )

    def review_cancel(self, broker_order_id):
        now = self.clock()
        return AttendedCancelReview(
            decision_id=str(uuid4()),
            account_masked=MASK,
            broker_order_id=broker_order_id,
            client_ref_id="10000000-0000-4000-8000-000000000001",
            reviewed_at=now,
            received_at=now,
            expires_at=now + timedelta(seconds=15),
            disclosure="Cancellation is asynchronous.",
            order_checks=(
                OrderCheck("OWNED_ORDER", "INFO", "The intent is locally owned."),
            ),
            required_confirmation_phrase=f"CONFIRM CANCEL {broker_order_id}",
            preview={
                "action": "cancel",
                "broker_order_id": broker_order_id,
                "alerts": ("Exposure remains until newer broker evidence.",),
            },
        )

    def cancel_order(self, broker_order_id, review, exact_confirmation):
        self.cancel_calls.append((broker_order_id, review, exact_confirmation))
        now = self.clock()
        return BrokerOperationResult(
            operation="cancel_equity_order",
            status=OperationStatus.UNKNOWN,
            observed_at=now,
            received_at=now,
            accepted=None,
            message="synthetic unresolved cancel",
        )

    def review_protection(
        self, source_request, source_plan_id, stop_template, source_claimed_at
    ):
        self.protection_review_calls.append(
            (source_request, source_plan_id, stop_template, source_claimed_at)
        )
        quantity = int(self.broker_state.get("filled_quantity", 0))
        if quantity <= 0:
            raise BrokerMutationBlocked("synthetic fill not confirmed")
        request = OrderRequest(
            account_masked=MASK,
            symbol=source_request.symbol,
            side=BrokerSide.SELL,
            order_type=EquityOrderType.STOP_MARKET,
            quantity=quantity,
            market_hours=MarketHours.REGULAR,
            time_in_force=TimeInForce.GTC,
            client_ref_id=stop_template.client_ref_id,
            stop_price=stop_template.stop_price,
        )
        if source_plan_id != PLAN_ID or stop_template.quantity != source_request.quantity:
            raise BrokerMutationBlocked("synthetic protection binding changed")
        return self.review_order(request)


class AttendedControlTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.now = NOW
        self.runtime = FakeRuntime(lambda: self.now)
        self.store = AttendedReviewStore(
            root=self.root,
            release_manifest_hash="1" * 64,
            config_hash="2" * 64,
            policy_hash="3" * 64,
            account_key="ibkr-live-ending-3103",
            account_masked=MASK,
        )
        self.control = AttendedOrderControl(
            self.store, self.runtime, clock=lambda: self.now
        )

    def tearDown(self):
        self.temporary.cleanup()

    def entry(self, *, market_hours=MarketHours.REGULAR):
        return OrderRequest(
            account_masked=MASK,
            symbol="XYZ",
            side=BrokerSide.BUY,
            order_type=EquityOrderType.LIMIT,
            quantity=10,
            market_hours=market_hours,
            time_in_force=TimeInForce.GFD,
            client_ref_id="10000000-0000-4000-8000-000000000002",
            limit_price=Decimal("10.00"),
        )

    def reopen(self, runtime):
        return AttendedOrderControl(
            AttendedReviewStore(
                root=self.root,
                release_manifest_hash="1" * 64,
                config_hash="2" * 64,
                policy_hash="3" * 64,
                account_key="ibkr-live-ending-3103",
                account_masked=MASK,
            ),
            runtime,
            clock=lambda: self.now,
        )

    def test_review_print_contract_and_restart_confirmation_are_one_shot(self):
        report = self.control.create_order_review(self.entry(), purpose="entry")
        self.assertEqual(report["requirement"], "BUY REVIEW REQUIRED")
        self.assertEqual(report["session_tag"], "regular_hours")
        self.assertEqual(len(report["alerts"]), 2)
        self.assertEqual(len(report["order_checks"]), 2)
        self.assertTrue(report["disclosure"])
        self.assertTrue(report["expires_at"])
        phrase = report["exact_confirmation_phrase"]
        review_id = report["review_id"]

        self.now += timedelta(seconds=1)
        confirm_runtime = FakeRuntime(
            lambda: self.now, broker_state=self.runtime.broker_state
        )
        restarted = AttendedOrderControl(
            AttendedReviewStore(
                root=self.root,
                release_manifest_hash="1" * 64,
                config_hash="2" * 64,
                policy_hash="3" * 64,
                account_key="ibkr-live-ending-3103",
                account_masked=MASK,
            ),
            confirm_runtime,
            clock=lambda: self.now,
        )
        with patch.object(socket, "socket", side_effect=AssertionError("network forbidden")):
            outcome = restarted.confirm_order(review_id, phrase)
        self.assertEqual(outcome["dispatch_state"], "BROKER_RECONCILIATION_REQUIRED")
        self.assertEqual(outcome["protection_state"], "AWAITING_CONFIRMED_FILL")
        self.assertFalse(outcome["protected"])
        self.assertEqual(len(self.runtime.place_calls), 0)
        self.assertEqual(len(confirm_runtime.place_calls), 1)
        self.assertEqual(confirm_runtime.prepare_calls, 1)
        with self.assertRaisesRegex(AttendedControlError, "NONRETRYABLE"):
            restarted.confirm_order(review_id, phrase)
        self.assertEqual(len(confirm_runtime.place_calls), 1)
        self.assertEqual(confirm_runtime.prepare_calls, 1)

    def test_expiry_and_changed_fresh_risk_burn_no_dispatch_claim(self):
        expired = self.control.create_order_review(self.entry(), purpose="entry")
        with self.assertRaisesRegex(AttendedControlError, "EXACT_CONFIRMATION_REQUIRED"):
            self.control.confirm_order(expired["review_id"], "CONFIRM SOMETHING ELSE")
        self.assertFalse(
            (self.root / f"control/attended/claims/{expired['review_id']}.json").exists()
        )
        self.now += timedelta(seconds=31)
        with self.assertRaisesRegex(AttendedControlError, "REVIEW_EXPIRED"):
            self.control.confirm_order(
                expired["review_id"], expired["exact_confirmation_phrase"]
            )
        self.assertFalse(
            (self.root / f"control/attended/claims/{expired['review_id']}.json").exists()
        )

        self.now = NOW
        changed = self.control.create_order_review(self.entry(), purpose="entry")
        self.runtime.capacity = "999.00"
        with self.assertRaisesRegex(AttendedControlError, "FRESH_REVIEW_CHANGED"):
            self.control.confirm_order(
                changed["review_id"], changed["exact_confirmation_phrase"]
            )
        self.assertEqual(self.runtime.place_calls, [])

    def test_tamper_is_detected_before_runtime_or_claim(self):
        report = self.control.create_order_review(self.entry(), purpose="entry")
        target = self.root / f"control/attended/reviews/{report['review_id']}.json"
        raw = json.loads(target.read_text(encoding="utf-8"))
        raw["purpose"] = "exit"
        target.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaisesRegex(AttendedControlError, "TAMPERED"):
            self.control.confirm_order(
                report["review_id"], report["exact_confirmation_phrase"]
            )
        self.assertEqual(self.runtime.place_calls, [])

    def test_cancel_has_separate_review_confirmation_and_replay_guard(self):
        target = "ibkr:19736:42"
        report = self.control.create_cancel_review(target)
        self.assertEqual(report["requirement"], "SELL / EXIT REVIEW REQUIRED")
        outcome = self.control.confirm_cancel(
            report["review_id"], report["exact_confirmation_phrase"]
        )
        self.assertFalse(outcome["cancel_confirmed"])
        self.assertEqual(len(self.runtime.cancel_calls), 1)
        self.assertEqual(self.runtime.prepare_calls, 1)
        with self.assertRaisesRegex(AttendedControlError, "NONRETRYABLE"):
            self.control.confirm_cancel(
                report["review_id"], report["exact_confirmation_phrase"]
            )
        self.assertEqual(len(self.runtime.cancel_calls), 1)
        self.assertEqual(self.runtime.prepare_calls, 1)

    def test_busy_writer_lock_leaves_order_and_cancel_reviews_unclaimed(self):
        order = self.control.create_order_review(self.entry(), purpose="entry")
        cancel = self.control.create_cancel_review("ibkr:19736:42")
        self.runtime.prepare_error = BrokerMutationBlocked("synthetic writer lock busy")

        for report, confirm in (
            (order, self.control.confirm_order),
            (cancel, self.control.confirm_cancel),
        ):
            with self.assertRaises(BrokerMutationBlocked):
                confirm(report["review_id"], report["exact_confirmation_phrase"])
            self.assertFalse(
                (self.root / f"control/attended/claims/{report['review_id']}.json").exists()
            )
            self.assertFalse(
                (self.root / f"control/attended/outcomes/{report['review_id']}.json").exists()
            )
        self.assertEqual(self.runtime.place_calls, [])
        self.assertEqual(self.runtime.cancel_calls, [])

        # Once lock contention clears, both original reviews remain usable and
        # each can still produce exactly one broker call.
        self.runtime.prepare_error = None
        self.control.confirm_order(order["review_id"], order["exact_confirmation_phrase"])
        self.control.confirm_cancel(cancel["review_id"], cancel["exact_confirmation_phrase"])
        self.assertEqual(len(self.runtime.place_calls), 1)
        self.assertEqual(len(self.runtime.cancel_calls), 1)

    def test_protection_survives_separate_processes_and_review_never_mutates(self):
        entry = self.control.create_order_review(self.entry(), purpose="entry")
        entry_runtime = FakeRuntime(
            lambda: self.now, broker_state=self.runtime.broker_state
        )
        self.reopen(entry_runtime).confirm_order(
            entry["review_id"], entry["exact_confirmation_phrase"]
        )
        no_fill_runtime = FakeRuntime(
            lambda: self.now, broker_state=self.runtime.broker_state
        )
        with self.assertRaises(BrokerMutationBlocked):
            self.reopen(no_fill_runtime).create_protection_review(entry["review_id"])
        self.assertEqual(no_fill_runtime.place_calls, [])
        self.assertEqual(no_fill_runtime.prepare_calls, 0)

        self.runtime.broker_state["filled_quantity"] = 4
        review_runtime = FakeRuntime(
            lambda: self.now, broker_state=self.runtime.broker_state
        )
        protection = self.reopen(review_runtime).create_protection_review(
            entry["review_id"]
        )
        self.assertEqual(protection["purpose"], "protection")
        self.assertEqual(review_runtime.place_calls, [])
        self.assertEqual(review_runtime.prepare_calls, 0)

        confirm_runtime = FakeRuntime(
            lambda: self.now, broker_state=self.runtime.broker_state
        )
        outcome = self.reopen(confirm_runtime).confirm_order(
            protection["review_id"], protection["exact_confirmation_phrase"]
        )
        self.assertEqual(confirm_runtime.place_calls[0][0].quantity, 4)
        self.assertEqual(confirm_runtime.prepare_calls, 1)
        self.assertEqual(
            outcome["protection_state"], "AWAITING_BROKER_WORKING_VERIFICATION"
        )
        self.assertFalse(outcome["protected"])

    def test_blocked_or_unknown_entry_outcome_alone_cannot_release_protection(self):
        for error, expected_state in (
            (BrokerMutationBlocked("synthetic blocked"), "BLOCKED_CONSUMED_REVIEW"),
            (BrokerUnknownSubmission("synthetic unknown"), "UNKNOWN_NONRETRYABLE"),
        ):
            with self.subTest(expected_state=expected_state):
                entry_request = self.entry()
                entry_request = OrderRequest(
                    **{
                        **entry_request.__dict__,
                        "client_ref_id": str(uuid4()),
                    }
                )
                entry = self.control.create_order_review(
                    entry_request, purpose="entry"
                )
                state = {"filled_quantity": 0, "place_error": error}
                confirm_runtime = FakeRuntime(lambda: self.now, broker_state=state)
                outcome = self.reopen(confirm_runtime).confirm_order(
                    entry["review_id"], entry["exact_confirmation_phrase"]
                )
                self.assertEqual(outcome["dispatch_state"], expected_state)
                state["place_error"] = None
                protection_runtime = FakeRuntime(lambda: self.now, broker_state=state)
                with self.assertRaises(BrokerMutationBlocked):
                    self.reopen(protection_runtime).create_protection_review(
                        entry["review_id"]
                    )
                self.assertEqual(protection_runtime.place_calls, [])
                self.assertEqual(protection_runtime.prepare_calls, 0)

    def test_premarket_order_is_refused_before_review(self):
        with self.assertRaisesRegex(AttendedControlError, "PREMARKET_ORDER_FORBIDDEN"):
            self.control.create_order_review(
                self.entry(market_hours=MarketHours.EXTENDED), purpose="entry"
            )

    def test_cli_exposes_only_explicit_review_and_confirm_surfaces(self):
        parser = build_parser()
        review = parser.parse_args(
            [
                "attended-review",
                "--install-root",
                str(self.root),
                "--purpose",
                "entry",
                "--side",
                "buy",
                "--symbol",
                "XYZ",
                "--quantity",
                "10",
                "--order-type",
                "limit",
                "--time-in-force",
                "gfd",
                "--limit-price",
                "10.00",
            ]
        )
        self.assertEqual(review.command, "attended-review")
        self.assertFalse(hasattr(review, "market_hours"))
        confirm = parser.parse_args(
            [
                "attended-confirm",
                "--install-root",
                str(self.root),
                "--review-id",
                "1" * 64,
                "--confirm",
                "CONFIRM EXACT TUPLE",
            ]
        )
        self.assertEqual(confirm.review_id, "1" * 64)
        self.assertEqual(confirm.confirm, "CONFIRM EXACT TUPLE")


if __name__ == "__main__":
    unittest.main()
