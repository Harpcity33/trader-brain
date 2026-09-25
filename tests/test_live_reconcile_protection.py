from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from titan_brain.live.broker import (
    AccountSnapshot,
    BrokerCapabilities,
    BrokerOperationResult,
    BrokerSide,
    EquityOrderType,
    FillSnapshot,
    FundsSnapshot,
    MarketHours,
    OperationStatus,
    ClientRefRecoverySource,
    OrderCoverageContract,
    OrderFamily,
    OrderFamilyCoverage,
    OrderFamilyCoverageStatus,
    OrderSnapshot,
    PositionSnapshot,
    FakeBrokerClient,
    TimeInForce,
)
from titan_brain.live.exits import (
    CancelAction,
    ExitAction,
    ExitCapacityError,
    calculate_exit_capacity,
    plan_safe_close,
    reconcile_cancel_result,
    require_exit_capacity,
)
from titan_brain.live.models import (
    BrokerOrderState,
    ExpiringPlan,
    IntentKind,
    IntentState,
    OrderIntent,
    ProtectionObligation,
    ProtectionState,
    ReservationState,
    RiskReservation,
)
from titan_brain.live.protection import (
    ProtectionAction,
    assess_protection,
    ensure_entry_fill_obligations,
    is_verified_working_protection,
    load_open_obligations,
)
from titan_brain.live.reconcile import (
    ActivityOwner,
    AuthoritativeReconciler,
    ReconciliationConflict,
    ReconciliationPhase,
    UnknownResolutionState,
    ingest_local_order,
    resolve_unknown_intent,
    validate_authoritative_snapshot,
)
from titan_brain.live.state import LiveStateStore, object_hash


UTC = timezone.utc
NOW = datetime(2026, 9, 8, 14, 0, tzinfo=UTC)
ACCOUNT_MASKED = "••••7153"
ACCOUNT_KEY = "ending-7153"
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


def capabilities(*, ref_lookup: bool = True) -> BrokerCapabilities:
    coverage = OrderCoverageContract(
        contract_version="test-order-coverage-v1",
        evidence_observed_at=NOW,
        families=tuple(
            OrderFamilyCoverage(
                family=family,
                status=OrderFamilyCoverageStatus.COMPLETE_GENERAL,
                evidence_id=f"test:{family.value}",
                broker_authoritative=True,
                all_pages_consumed=True,
                includes_working_orders_across_dates=True,
                includes_parent_child_conditional=(
                    family is OrderFamily.ADVANCED_EQUITY
                ),
            )
            for family in OrderFamily
        ),
        client_ref_recovery_source=(
            ClientRefRecoverySource.DEDICATED_LOOKUP
            if ref_lookup
            else ClientRefRecoverySource.UNAVAILABLE
        ),
        broker_preserves_client_ref=ref_lookup,
        negative_client_ref_results_authoritative=ref_lookup,
    )
    return BrokerCapabilities(
        connector="test",
        account_masked=ACCOUNT_MASKED,
        supports_account_read=True,
        supports_equity_position_read=True,
        supports_equity_order_read=True,
        supports_option_position_read=True,
        supports_option_order_read=True,
        supports_advanced_order_read=True,
        supports_equity_review=True,
        supports_equity_place=True,
        supports_equity_cancel=True,
        daemon_transport_configured=True,
        supports_daemon_writes=True,
        supports_unattended_writes=True,
        supports_atomic_protection=False,
        supports_equity_replace=False,
        supports_streaming=False,
        supports_auth_refresh=True,
        supports_ref_id_lookup=ref_lookup,
        review_requires_explicit_confirmation=False,
        cancel_requires_explicit_confirmation=False,
        cancel_is_asynchronous=True,
        supported_order_types=tuple(EquityOrderType),
        supported_market_hours=tuple(MarketHours),
        supported_time_in_force=tuple(TimeInForce),
        order_coverage=coverage,
    )


def account_snapshot(
    *,
    received_at: datetime = NOW,
    orders: tuple[OrderSnapshot, ...] = (),
    positions: tuple[PositionSnapshot, ...] = (),
    advanced_complete: bool = True,
    risk_complete: bool = True,
) -> AccountSnapshot:
    return AccountSnapshot(
        account_masked=ACCOUNT_MASKED,
        observed_at=received_at,
        received_at=received_at,
        account_state="active",
        account_type="individual",
        funds=FundsSnapshot(
            total_value="1000.00",
            cash="900.00",
            buying_power="900.00",
            unleveraged_buying_power="900.00",
        ),
        equity_positions=positions,
        equity_orders=orders,
        option_position_count=0,
        option_order_count=0,
        advanced_order_count=0,
        standard_equity_positions_complete=True,
        standard_equity_orders_complete=True,
        option_positions_complete=True,
        option_orders_complete=True,
        advanced_orders_complete=advanced_complete,
        auth_point_in_time=True,
        daily_realized_pnl=Decimal("0.00") if risk_complete else None,
        weekly_realized_pnl=Decimal("0.00") if risk_complete else None,
        peak_equity=Decimal("1000.00") if risk_complete else None,
        daily_realized_pnl_complete=risk_complete,
        weekly_realized_pnl_complete=risk_complete,
        peak_equity_complete=risk_complete,
        risk_evidence_authoritative=risk_complete,
        risk_evidence_source="test_broker" if risk_complete else None,
        risk_evidence_as_of=received_at if risk_complete else None,
    )


def order_snapshot(
    *,
    order_id: str,
    state: BrokerOrderState,
    side: BrokerSide = BrokerSide.BUY,
    order_type: EquityOrderType = EquityOrderType.LIMIT,
    quantity: int = 5,
    fills: tuple[FillSnapshot, ...] = (),
    client_ref: str | None = None,
    updated_at: datetime = NOW,
) -> OrderSnapshot:
    cumulative = sum((fill.quantity for fill in fills), Decimal("0"))
    market_hours = MarketHours.REGULAR
    tif = TimeInForce.GTC if order_type is EquityOrderType.STOP_MARKET else TimeInForce.GFD
    return OrderSnapshot(
        broker_order_id=order_id,
        account_masked=ACCOUNT_MASKED,
        symbol="TEST",
        side=side,
        order_type=order_type,
        state=state,
        requested_quantity=Decimal(quantity),
        cumulative_filled_quantity=cumulative,
        market_hours=market_hours,
        time_in_force=tif,
        broker_updated_at=updated_at,
        received_at=updated_at,
        limit_price=Decimal("10.00") if order_type is EquityOrderType.LIMIT else None,
        stop_price=Decimal("9.50") if order_type is EquityOrderType.STOP_MARKET else None,
        client_ref_id=client_ref,
        fills=fills,
    )


def position(*, quantity: int = 5, sellable: int = 5, held: int = 0) -> PositionSnapshot:
    return PositionSnapshot(
        symbol="TEST",
        quantity=Decimal(quantity),
        sellable_quantity=Decimal(sellable),
        held_for_sells=Decimal(held),
        average_price=Decimal("10.00"),
    )


def obligation(
    *, quantity: int = 5, suffix: str = "1", state: ProtectionState = ProtectionState.REQUIRED
) -> ProtectionObligation:
    return ProtectionObligation(
        obligation_id=f"protect-{suffix}",
        source_fill_id=f"fill-{suffix}",
        account_key=ACCOUNT_KEY,
        symbol="TEST",
        required_quantity=quantity,
        working_quantity=0,
        stop_price=Decimal("9.50"),
        state=state,
        revision=0,
        updated_at=NOW,
    )


class StoreFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = LiveStateStore(Path(self.temporary.name) / "live.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def prepare_intent(
        self, *, unknown: bool = False, kind: IntentKind = IntentKind.ENTRY
    ) -> tuple[ExpiringPlan, OrderIntent]:
        plan = ExpiringPlan(
            plan_id="plan-1",
            account_key=ACCOUNT_KEY,
            strategy_id="full-live-test",
            symbol="TEST",
            setup_id="BREAKOUT",
            quantity=5,
            limit_price=Decimal("10.00"),
            structural_stop=Decimal("9.50"),
            market_hours="regular_hours",
            time_in_force="gfd",
            evidence_cutoff_at=NOW,
            created_at=NOW + timedelta(seconds=1),
            expires_at=NOW + timedelta(minutes=1),
            policy_hash=HASH_A,
            config_hash=HASH_B,
            evidence_hash=HASH_C,
        )
        reservation = RiskReservation(
            reservation_id="reservation-1",
            plan_id=plan.plan_id,
            account_key=ACCOUNT_KEY,
            planned_risk=Decimal("2.50"),
            stress_risk=Decimal("3.00"),
            execution_reserve=Decimal("0.50"),
            notional=Decimal("50.00"),
            created_at=NOW + timedelta(seconds=2),
        )
        tuple_value = {
            "account_key": ACCOUNT_KEY,
            "account_masked": ACCOUNT_MASKED,
            "symbol": "TEST",
            "side": "buy",
            "order_type": "limit",
            "quantity": 5,
            "market_hours": "regular_hours",
            "time_in_force": "gfd",
            "limit_price": "10.00",
            "stop_price": None,
            "client_ref_id": None,
        }
        client_ref = str(uuid4())
        tuple_value["client_ref_id"] = client_ref
        intent = OrderIntent(
            intent_id="intent-1",
            plan_id=plan.plan_id,
            reservation_id=(
                reservation.reservation_id if kind is IntentKind.ENTRY else None
            ),
            account_key=ACCOUNT_KEY,
            kind=kind,
            client_ref=client_ref,
            order_tuple=tuple_value,
            tuple_hash=object_hash(tuple_value),
            created_at=NOW + timedelta(seconds=3),
            acknowledgement_deadline_at=NOW + timedelta(seconds=13),
        )
        if kind is IntentKind.ENTRY:
            self.store.prepare_submission(
                plan=plan, reservation=reservation, intent=intent
            )
        else:
            seed_tuple = dict(tuple_value)
            seed_ref = str(uuid4())
            seed_tuple["client_ref_id"] = seed_ref
            seed = OrderIntent(
                intent_id="seed-entry-intent",
                plan_id=plan.plan_id,
                reservation_id=reservation.reservation_id,
                account_key=ACCOUNT_KEY,
                kind=IntentKind.ENTRY,
                client_ref=seed_ref,
                order_tuple=seed_tuple,
                tuple_hash=object_hash(seed_tuple),
                created_at=NOW + timedelta(seconds=3),
                acknowledgement_deadline_at=NOW + timedelta(seconds=13),
            )
            self.store.prepare_submission(
                plan=plan, reservation=reservation, intent=seed
            )
            self.store.prepare_safety_intent(intent)
        self.store.transition_intent(
            intent.intent_id,
            IntentState.SUBMITTING,
            occurred_at=NOW + timedelta(seconds=4),
        )
        if unknown:
            self.store.transition_intent(
                intent.intent_id,
                IntentState.UNKNOWN,
                occurred_at=NOW + timedelta(seconds=5),
            )
        return plan, intent


class ReconciliationTests(StoreFixture):
    def test_recovered_client_ref_must_match_entire_durable_order_tuple(self) -> None:
        _, intent = self.prepare_intent(unknown=True)
        mismatched = replace(
            order_snapshot(
                order_id="wrong-symbol",
                state=BrokerOrderState.CONFIRMED,
                client_ref=intent.client_ref,
                updated_at=NOW + timedelta(seconds=6),
            ),
            symbol="OTHER",
        )
        with self.assertRaisesRegex(
            ReconciliationConflict, "different immutable order tuple"
        ):
            resolve_unknown_intent(
                self.store,
                intent_id=intent.intent_id,
                snapshot=account_snapshot(
                    received_at=NOW + timedelta(seconds=6), orders=(mismatched,)
                ),
                capabilities=capabilities(),
            )

    def test_account_risk_evidence_defaults_fail_closed_but_fake_is_explicit(self) -> None:
        incomplete = account_snapshot(risk_complete=False)
        self.assertFalse(incomplete.daily_realized_pnl_ready)
        self.assertFalse(incomplete.entry_risk_evidence_ready)
        report = validate_authoritative_snapshot(
            incomplete,
            capabilities(),
            account_masked=ACCOUNT_MASKED,
            now=NOW,
        )
        self.assertIn("DAILY_REALIZED_PNL_INCOMPLETE", report.blockers)
        self.assertIn("DAILY_REALIZED_PNL_NON_AUTHORITATIVE", report.blockers)
        self.assertFalse(report.entries_allowed)
        # Missing P&L provenance gates new risk, not an otherwise safe exit.
        close = plan_safe_close(
            position=position(),
            orders=(),
            symbol="TEST",
            snapshot_received_at=incomplete.received_at,
        )
        self.assertEqual(close.action, ExitAction.SUBMIT_SAFE_CLOSE)

        fake = FakeBrokerClient(clock=lambda: NOW).get_account_snapshot(ACCOUNT_MASKED)
        self.assertTrue(fake.daily_realized_pnl_ready)
        self.assertTrue(fake.entry_risk_evidence_ready)
        self.assertEqual(fake.daily_realized_pnl, Decimal("0.00"))
        self.assertEqual(fake.peak_equity, Decimal("1000.00"))

    def test_risk_values_are_exact_and_complete_flags_require_values(self) -> None:
        precise = replace(
            account_snapshot(), daily_realized_pnl=Decimal("0.00125")
        )
        self.assertEqual(precise.daily_realized_pnl, Decimal("0.00125"))
        with self.assertRaisesRegex(ValueError, "must be finite"):
            replace(account_snapshot(), daily_realized_pnl=Decimal("NaN"))
        with self.assertRaisesRegex(ValueError, "cannot be complete"):
            replace(account_snapshot(), daily_realized_pnl=None)

    def test_startup_requires_complete_advanced_reconciliation(self) -> None:
        report = validate_authoritative_snapshot(
            account_snapshot(advanced_complete=False),
            capabilities(),
            account_masked=ACCOUNT_MASKED,
            phase=ReconciliationPhase.STARTUP,
            now=NOW,
        )
        self.assertFalse(report.entries_allowed)
        self.assertIn("ADVANCED_RECONCILIATION_INCOMPLETE", report.blockers)

    def test_manual_and_other_agent_activity_are_explicit_and_block_entries(self) -> None:
        external_ref = str(uuid4())
        snapshot = account_snapshot(
            orders=(
                order_snapshot(
                    order_id="manual-1",
                    state=BrokerOrderState.CONFIRMED,
                    client_ref=None,
                ),
                order_snapshot(
                    order_id="agent-1",
                    state=BrokerOrderState.CONFIRMED,
                    client_ref=external_ref,
                ),
            )
        )
        report = validate_authoritative_snapshot(
            snapshot,
            capabilities(),
            account_masked=ACCOUNT_MASKED,
            now=NOW,
        )
        self.assertFalse(report.entries_allowed)
        self.assertEqual(
            {item.owner for item in report.external_activity},
            {ActivityOwner.MANUAL, ActivityOwner.OTHER_AGENT},
        )
        self.assertIn("EXTERNAL_BROKER_ACTIVITY", report.blockers)

    def test_same_symbol_position_quantity_mismatch_is_manual_activity(self) -> None:
        report = validate_authoritative_snapshot(
            account_snapshot(positions=(position(quantity=5),)),
            capabilities(),
            account_masked=ACCOUNT_MASKED,
            now=NOW,
            managed_symbols=("TEST",),
            expected_position_quantities={"TEST": Decimal("4")},
        )
        self.assertIn("POSITION_OWNERSHIP_MISMATCH", report.blockers)

    def test_unknown_requires_strictly_newer_matching_evidence_and_never_retries(self) -> None:
        _, intent = self.prepare_intent(unknown=True)
        matching = order_snapshot(
            order_id="broker-1",
            state=BrokerOrderState.CONFIRMED,
            client_ref=intent.client_ref,
            updated_at=NOW + timedelta(seconds=5),
        )
        same_time = account_snapshot(
            received_at=NOW + timedelta(seconds=5), orders=(matching,)
        )
        waiting = resolve_unknown_intent(
            self.store,
            intent_id=intent.intent_id,
            snapshot=same_time,
            capabilities=capabilities(),
        )
        self.assertEqual(
            waiting.state, UnknownResolutionState.WAITING_FOR_NEWER_EVIDENCE
        )
        self.assertFalse(waiting.retry_same_intent_allowed)

        newer_order = replace(
            matching,
            broker_updated_at=NOW + timedelta(seconds=6),
            received_at=NOW + timedelta(seconds=6),
        )
        resolved = resolve_unknown_intent(
            self.store,
            intent_id=intent.intent_id,
            snapshot=account_snapshot(
                received_at=NOW + timedelta(seconds=6), orders=(newer_order,)
            ),
            capabilities=capabilities(),
        )
        self.assertEqual(resolved.state, UnknownResolutionState.MATCHED_ORDER)
        self.assertEqual(
            self.store.row("order_intents", "intent_id", intent.intent_id)["state"],
            IntentState.ACKNOWLEDGED.value,
        )

    def test_order_list_absence_is_not_negative_proof(self) -> None:
        _, intent = self.prepare_intent(unknown=True)
        snapshot = account_snapshot(received_at=NOW + timedelta(seconds=6))
        unresolved = resolve_unknown_intent(
            self.store,
            intent_id=intent.intent_id,
            snapshot=snapshot,
            capabilities=capabilities(),
        )
        self.assertEqual(unresolved.state, UnknownResolutionState.UNRESOLVED)
        self.assertEqual(
            self.store.row("order_intents", "intent_id", intent.intent_id)["state"],
            IntentState.UNKNOWN.value,
        )
        resolved = resolve_unknown_intent(
            self.store,
            intent_id=intent.intent_id,
            snapshot=snapshot,
            capabilities=capabilities(),
            confirmed_absent_client_refs=(intent.client_ref,),
        )
        self.assertEqual(resolved.state, UnknownResolutionState.CONFIRMED_ABSENT)
        self.assertFalse(resolved.retry_same_intent_allowed)

    def test_unknown_cancel_is_not_resolved_by_equity_client_ref_absence(self) -> None:
        _, intent = self.prepare_intent(unknown=True, kind=IntentKind.CANCEL)
        resolution = resolve_unknown_intent(
            self.store,
            intent_id=intent.intent_id,
            snapshot=account_snapshot(received_at=NOW + timedelta(seconds=6)),
            capabilities=capabilities(),
            confirmed_absent_client_refs=(intent.client_ref,),
        )
        self.assertEqual(resolution.state, UnknownResolutionState.UNRESOLVED)
        self.assertIn("exact target-order evidence", resolution.reason)
        self.assertEqual(
            self.store.row("order_intents", "intent_id", intent.intent_id)["state"],
            IntentState.UNKNOWN.value,
        )

    def test_slow_snapshot_receipt_does_not_freshen_unknown_resolution(self) -> None:
        _, intent = self.prepare_intent(unknown=True)
        snapshot = replace(
            account_snapshot(received_at=NOW + timedelta(seconds=10)),
            observed_at=NOW + timedelta(seconds=4),
        )
        resolution = resolve_unknown_intent(
            self.store,
            intent_id=intent.intent_id,
            snapshot=snapshot,
            capabilities=capabilities(),
            confirmed_absent_client_refs=(intent.client_ref,),
        )
        self.assertEqual(
            resolution.state, UnknownResolutionState.WAITING_FOR_NEWER_EVIDENCE
        )
        self.assertEqual(
            self.store.row("order_intents", "intent_id", intent.intent_id)["state"],
            IntentState.UNKNOWN.value,
        )

    def test_continuous_reconciler_blocks_out_of_order_snapshot(self) -> None:
        reconciler = AuthoritativeReconciler(
            account_masked=ACCOUNT_MASKED, account_key=ACCOUNT_KEY
        )
        first = reconciler.reconcile_snapshot(
            self.store,
            snapshot=account_snapshot(received_at=NOW),
            capabilities=capabilities(),
            now=NOW,
        )
        self.assertTrue(first.entries_allowed)
        older = reconciler.reconcile_snapshot(
            self.store,
            snapshot=account_snapshot(received_at=NOW - timedelta(seconds=1)),
            capabilities=capabilities(),
            now=NOW,
        )
        self.assertIn("OUT_OF_ORDER_SNAPSHOT", older.blockers)

    def test_future_envelope_cannot_ingest_or_resolve_an_unknown_order(self) -> None:
        _, intent = self.prepare_intent(unknown=True)
        future_at = NOW + timedelta(seconds=60)
        matching = order_snapshot(
            order_id="future-order",
            state=BrokerOrderState.CONFIRMED,
            client_ref=intent.client_ref,
            updated_at=future_at,
        )
        reconciler = AuthoritativeReconciler(
            account_masked=ACCOUNT_MASKED, account_key=ACCOUNT_KEY
        )
        report = reconciler.reconcile_snapshot(
            self.store,
            snapshot=account_snapshot(received_at=future_at, orders=(matching,)),
            capabilities=capabilities(),
            now=NOW + timedelta(seconds=6),
        )
        self.assertIn("SNAPSHOT_FROM_FUTURE", report.blockers)
        self.assertEqual(report.ingested_orders, ())
        self.assertEqual(report.unknown_resolutions, ())
        self.assertEqual(self.store.rows("SELECT * FROM broker_orders"), [])
        self.assertEqual(
            self.store.row("order_intents", "intent_id", intent.intent_id)["state"],
            IntentState.UNKNOWN.value,
        )

    def test_startup_durably_recovers_crash_left_submitting_then_requires_newer_negative(self) -> None:
        _, intent = self.prepare_intent(unknown=False)
        reconciler = AuthoritativeReconciler(
            account_masked=ACCOUNT_MASKED, account_key=ACCOUNT_KEY
        )
        report = reconciler.reconcile_snapshot(
            self.store,
            snapshot=account_snapshot(received_at=NOW + timedelta(seconds=6)),
            capabilities=capabilities(),
            now=NOW + timedelta(seconds=6),
            phase=ReconciliationPhase.STARTUP,
            confirmed_absent_client_refs=(intent.client_ref,),
        )
        self.assertFalse(report.entries_allowed)
        self.assertIn(intent.intent_id, report.unknown_intent_ids)
        self.assertIn("UNKNOWN_LOCAL_INTENT", report.blockers)
        self.assertEqual(
            self.store.row("order_intents", "intent_id", intent.intent_id)["state"],
            IntentState.UNKNOWN.value,
        )
        self.assertEqual(
            report.unknown_resolutions[0].state,
            UnknownResolutionState.WAITING_FOR_NEWER_EVIDENCE,
        )

        resolved = reconciler.reconcile_snapshot(
            self.store,
            snapshot=account_snapshot(received_at=NOW + timedelta(seconds=7)),
            capabilities=capabilities(),
            now=NOW + timedelta(seconds=7),
            confirmed_absent_client_refs=(intent.client_ref,),
        )
        self.assertTrue(resolved.entries_allowed)
        self.assertEqual(resolved.unknown_intent_ids, ())
        self.assertEqual(
            self.store.row("order_intents", "intent_id", intent.intent_id)["state"],
            IntentState.RECONCILED.value,
        )

    def test_delayed_publication_after_two_empty_reads_never_releases_or_retries(self) -> None:
        _, intent = self.prepare_intent(unknown=False)
        reconciler = AuthoritativeReconciler(
            account_masked=ACCOUNT_MASKED, account_key=ACCOUNT_KEY
        )
        for seconds in (6, 7):
            report = reconciler.reconcile_snapshot(
                self.store,
                snapshot=account_snapshot(
                    received_at=NOW + timedelta(seconds=seconds)
                ),
                capabilities=capabilities(),
                now=NOW + timedelta(seconds=seconds),
                # Eventually-consistent lookup classified this exact ref as
                # NOT_SEEN_YET, so no authoritative negative is supplied.
                confirmed_absent_client_refs=(),
            )
            self.assertIn("UNKNOWN_LOCAL_INTENT", report.blockers)
            self.assertEqual(
                self.store.row("order_intents", "intent_id", intent.intent_id)[
                    "state"
                ],
                IntentState.SUBMITTING.value,
            )
            self.assertEqual(
                self.store.row(
                    "risk_reservations", "reservation_id", "reservation-1"
                )["state"],
                ReservationState.RESERVED.value,
            )

        published = order_snapshot(
            order_id="delayed-broker-order",
            state=BrokerOrderState.CONFIRMED,
            client_ref=intent.client_ref,
            updated_at=NOW + timedelta(seconds=8),
        )
        recovered = reconciler.reconcile_snapshot(
            self.store,
            snapshot=account_snapshot(
                received_at=NOW + timedelta(seconds=8), orders=(published,)
            ),
            capabilities=capabilities(),
            now=NOW + timedelta(seconds=8),
        )
        self.assertNotIn("UNKNOWN_LOCAL_INTENT", recovered.blockers)
        self.assertEqual(
            self.store.row("order_intents", "intent_id", intent.intent_id)["state"],
            IntentState.ACKNOWLEDGED.value,
        )
        self.assertEqual(
            len(
                self.store.rows(
                    "SELECT * FROM order_intents WHERE intent_id=?",
                    (intent.intent_id,),
                )
            ),
            1,
        )

    def test_submitting_is_not_reclassified_by_incomplete_or_nonnewer_negative(self) -> None:
        _, intent = self.prepare_intent(unknown=False)
        reconciler = AuthoritativeReconciler(
            account_masked=ACCOUNT_MASKED, account_key=ACCOUNT_KEY
        )
        incomplete = replace(
            account_snapshot(received_at=NOW + timedelta(seconds=6)),
            standard_equity_orders_complete=False,
        )
        report = reconciler.reconcile_snapshot(
            self.store,
            snapshot=incomplete,
            capabilities=capabilities(),
            now=NOW + timedelta(seconds=6),
            confirmed_absent_client_refs=(intent.client_ref,),
        )
        self.assertIn("STANDARD_ORDERS_INCOMPLETE", report.blockers)
        self.assertEqual(
            self.store.row("order_intents", "intent_id", intent.intent_id)["state"],
            IntentState.SUBMITTING.value,
        )

        fresh_reconciler = AuthoritativeReconciler(
            account_masked=ACCOUNT_MASKED, account_key=ACCOUNT_KEY
        )
        same_boundary = account_snapshot(received_at=NOW + timedelta(seconds=4))
        report = fresh_reconciler.reconcile_snapshot(
            self.store,
            snapshot=same_boundary,
            capabilities=capabilities(),
            now=NOW + timedelta(seconds=4),
            confirmed_absent_client_refs=(intent.client_ref,),
        )
        self.assertIn("UNKNOWN_LOCAL_INTENT", report.blockers)
        self.assertEqual(
            self.store.row("order_intents", "intent_id", intent.intent_id)["state"],
            IntentState.SUBMITTING.value,
        )


class FillProtectionTests(StoreFixture):
    def test_duplicate_and_out_of_order_fills_create_one_obligation_per_delta(self) -> None:
        _, intent = self.prepare_intent()
        fill_one = FillSnapshot(
            fill_id="fill-1",
            quantity=Decimal("2"),
            price=Decimal("10.00"),
            executed_at=NOW + timedelta(seconds=5),
        )
        first = order_snapshot(
            order_id="broker-1",
            state=BrokerOrderState.PARTIALLY_FILLED,
            fills=(fill_one,),
            client_ref=intent.client_ref,
            updated_at=NOW + timedelta(seconds=6),
        )
        result = ensure_entry_fill_obligations(
            self.store, order=first, intent_id=intent.intent_id, account_key=ACCOUNT_KEY
        )
        self.assertEqual(len(result.new_obligation_ids), 1)

        later_envelope = replace(first, received_at=NOW + timedelta(seconds=7))
        duplicate = ensure_entry_fill_obligations(
            self.store,
            order=later_envelope,
            intent_id=intent.intent_id,
            account_key=ACCOUNT_KEY,
        )
        self.assertEqual(len(duplicate.existing_obligation_ids), 1)

        fill_two = FillSnapshot(
            fill_id="fill-2",
            quantity=Decimal("3"),
            price=Decimal("10.01"),
            executed_at=NOW + timedelta(seconds=8),
        )
        complete = order_snapshot(
            order_id="broker-1",
            state=BrokerOrderState.FILLED,
            fills=(fill_one, fill_two),
            client_ref=intent.client_ref,
            updated_at=NOW + timedelta(seconds=9),
        )
        newest = ensure_entry_fill_obligations(
            self.store,
            order=complete,
            intent_id=intent.intent_id,
            account_key=ACCOUNT_KEY,
        )
        self.assertEqual(len(newest.new_obligation_ids), 1)
        # A stale broker envelope cannot roll back state or duplicate facts.
        ingest_local_order(
            self.store, order=first, intent_id=intent.intent_id, account_key=ACCOUNT_KEY
        )
        self.assertEqual(len(self.store.rows("SELECT * FROM fills")), 2)
        obligations = load_open_obligations(
            self.store, account_key=ACCOUNT_KEY, symbol="TEST"
        )
        self.assertEqual(len(obligations), 2)
        self.assertEqual(sum(item.required_quantity for item in obligations), 5)

    def test_submitted_stop_is_not_working_protection(self) -> None:
        pending = order_snapshot(
            order_id="stop-pending",
            state=BrokerOrderState.PENDING,
            side=BrokerSide.SELL,
            order_type=EquityOrderType.STOP_MARKET,
        )
        decision = assess_protection(
            position=position(sellable=0, held=5),
            orders=(pending,),
            obligations=(obligation(),),
        )
        self.assertFalse(is_verified_working_protection(pending))
        self.assertEqual(decision.working_quantity, 0)
        self.assertEqual(decision.pending_quantity, 5)
        self.assertIn(ProtectionAction.VERIFY_PENDING_PROTECTION, decision.actions)
        self.assertTrue(decision.pause_new_entries)

    def test_missing_protection_is_created_before_rejection_requires_safe_close(self) -> None:
        rejected = order_snapshot(
            order_id="stop-rejected",
            state=BrokerOrderState.REJECTED,
            side=BrokerSide.SELL,
            order_type=EquityOrderType.STOP_MARKET,
        )
        missing = assess_protection(
            position=position(), orders=(), obligations=(obligation(),)
        )
        self.assertTrue(missing.pause_new_entries)
        self.assertFalse(missing.safe_close_required)
        self.assertIn(ProtectionAction.CREATE_PROTECTION_INTENT, missing.actions)

        failed = assess_protection(
            position=position(), orders=(rejected,), obligations=(obligation(),)
        )
        self.assertTrue(failed.pause_new_entries)
        self.assertTrue(failed.safe_close_required)
        self.assertIn(ProtectionAction.SAFE_CLOSE, failed.actions)

    def test_confirmed_gtc_stop_is_working_but_oversell_is_blocked(self) -> None:
        working = order_snapshot(
            order_id="stop-working",
            state=BrokerOrderState.CONFIRMED,
            side=BrokerSide.SELL,
            order_type=EquityOrderType.STOP_MARKET,
        )
        covered = assess_protection(
            position=position(sellable=0, held=5),
            orders=(working,),
            obligations=(obligation(),),
        )
        self.assertTrue(covered.protected)
        self.assertEqual(covered.actions, (ProtectionAction.NONE,))

        second = replace(working, broker_order_id="stop-overlap")
        overlap = assess_protection(
            position=position(sellable=0, held=5),
            orders=(working, second),
            obligations=(obligation(),),
        )
        self.assertFalse(overlap.protected)
        self.assertIn(ProtectionAction.RECONCILE_EXIT_CAPACITY, overlap.actions)

    def test_stop_widening_is_not_counted_as_working_protection(self) -> None:
        widened = replace(
            order_snapshot(
                order_id="stop-wide",
                state=BrokerOrderState.CONFIRMED,
                side=BrokerSide.SELL,
                order_type=EquityOrderType.STOP_MARKET,
            ),
            stop_price=Decimal("9.25"),
        )
        decision = assess_protection(
            position=position(sellable=0, held=5),
            orders=(widened,),
            obligations=(obligation(),),
        )
        self.assertEqual(decision.working_quantity, 0)
        self.assertTrue(decision.safe_close_required)


class ExitLifecycleTests(unittest.TestCase):
    def test_pending_exit_reserves_quantity_and_cannot_be_overlapped(self) -> None:
        pending = order_snapshot(
            order_id="sell-pending",
            state=BrokerOrderState.PENDING,
            side=BrokerSide.SELL,
        )
        capacity = calculate_exit_capacity(
            position=position(sellable=2, held=3), orders=(pending,), symbol="TEST"
        )
        self.assertEqual(capacity.active_sell_quantity, 5)
        self.assertEqual(capacity.available_to_submit, 0)
        with self.assertRaises(ExitCapacityError):
            require_exit_capacity(capacity, 1)

    def test_cancel_acceptance_is_nonterminal(self) -> None:
        before = order_snapshot(
            order_id="sell-1",
            state=BrokerOrderState.CONFIRMED,
            side=BrokerSide.SELL,
        )
        pending = replace(before, state=BrokerOrderState.PENDING_CANCELLED)
        result = BrokerOperationResult(
            operation="cancel_equity_order",
            status=OperationStatus.PENDING_CANCEL,
            observed_at=NOW,
            received_at=NOW,
            accepted=True,
            message="accepted",
            order=pending,
        )
        outcome = reconcile_cancel_result(before=before, result=result)
        self.assertEqual(outcome.action, CancelAction.WAIT_CONFIRMATION)
        self.assertFalse(outcome.terminal)

    def test_cancel_fill_race_and_partial_rest_cancel_force_recalculation(self) -> None:
        before = order_snapshot(
            order_id="entry-1",
            state=BrokerOrderState.CONFIRMED,
            quantity=5,
        )
        fill = FillSnapshot(
            fill_id="race-fill",
            quantity=Decimal("2"),
            price=Decimal("10.00"),
            executed_at=NOW + timedelta(seconds=1),
        )
        raced = order_snapshot(
            order_id="entry-1",
            state=BrokerOrderState.PARTIALLY_FILLED,
            quantity=5,
            fills=(fill,),
            updated_at=NOW + timedelta(seconds=1),
        )
        result = BrokerOperationResult(
            operation="cancel_equity_order",
            status=OperationStatus.REJECTED,
            observed_at=NOW + timedelta(seconds=1),
            received_at=NOW + timedelta(seconds=1),
            accepted=False,
            message="fill race",
            order=raced,
        )
        outcome = reconcile_cancel_result(before=before, result=result)
        self.assertEqual(outcome.action, CancelAction.FILL_RACE_RECALCULATE)
        self.assertEqual(outcome.filled_quantity_delta, 2)

        terminal = replace(
            raced,
            state=BrokerOrderState.PARTIALLY_FILLED_REST_CANCELLED,
            broker_updated_at=NOW + timedelta(seconds=2),
            received_at=NOW + timedelta(seconds=2),
        )
        settled = reconcile_cancel_result(
            before=raced,
            result=BrokerOperationResult(
                operation="cancel_equity_order",
                status=OperationStatus.CANCELLED,
                observed_at=NOW + timedelta(seconds=2),
                received_at=NOW + timedelta(seconds=2),
                accepted=True,
                message="settled",
                order=terminal,
            ),
        )
        self.assertEqual(
            settled.action, CancelAction.CANCEL_CONFIRMED_RECALCULATE
        )
        self.assertTrue(settled.terminal)

    def test_safe_close_waits_for_cancel_then_uses_refreshed_sellable_position(self) -> None:
        stopping = order_snapshot(
            order_id="stop-1",
            state=BrokerOrderState.PENDING_CANCELLED,
            side=BrokerSide.SELL,
            order_type=EquityOrderType.STOP_MARKET,
        )
        waiting = plan_safe_close(
            position=position(sellable=0, held=5),
            orders=(stopping,),
            symbol="TEST",
            snapshot_received_at=NOW,
        )
        self.assertEqual(waiting.action, ExitAction.WAIT_CANCEL_CONFIRMATION)
        self.assertEqual(waiting.quantity, 0)

        ready = plan_safe_close(
            position=position(sellable=5, held=0),
            orders=(),
            symbol="TEST",
            snapshot_received_at=NOW + timedelta(seconds=1),
            evidence_floor_at=NOW,
        )
        self.assertEqual(ready.action, ExitAction.SUBMIT_SAFE_CLOSE)
        self.assertEqual(ready.quantity, 5)

    def test_closeout_cancels_working_stop_before_nonoverlapping_safe_close(self) -> None:
        stopping = order_snapshot(
            order_id="stop-1",
            state=BrokerOrderState.CONFIRMED,
            side=BrokerSide.SELL,
            order_type=EquityOrderType.STOP_MARKET,
        )
        decision = plan_safe_close(
            position=position(sellable=0, held=5),
            orders=(stopping,),
            symbol="TEST",
            snapshot_received_at=NOW,
        )
        self.assertEqual(decision.action, ExitAction.CANCEL_EXIT_ORDERS)
        self.assertEqual(decision.cancel_order_ids, ("stop-1",))
        self.assertTrue(decision.requires_newer_snapshot)


if __name__ == "__main__":
    unittest.main()
