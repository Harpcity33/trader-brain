from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4
from zoneinfo import ZoneInfo

from titan_brain.live.broker import (
    AccountSnapshot,
    BrokerOperationResult,
    BrokerSide,
    EquityOrderType,
    FakeBrokerClient,
    FakeFault,
    FillSnapshot,
    FundsSnapshot,
    MarketHours,
    OperationStatus,
    OrderRequest,
    OrderSnapshot,
    PositionSnapshot,
    TimeInForce,
)
from titan_brain.live.authority import (
    MutationAuthorityDenied,
    MutationOperation,
    MutationPhase,
)
from titan_brain.live.exits import ExitAction, plan_safe_close
from titan_brain.live.lifecycle_actions import ProductionLifecycleActions
from titan_brain.live.models import (
    BrokerOrderState,
    ExpiringPlan,
    IntentKind,
    IntentState,
    OrderIntent,
    ProtectionState,
    RiskReservation,
    SessionLatch as DurableSessionLatch,
)
from titan_brain.live.plans import ExpiringPlan as SignedPlan
from titan_brain.live.policy import PolicyBundle, sha256_json
from titan_brain.live.protection import (
    ProtectionAction,
    assess_protection,
    ensure_entry_fill_obligations,
    load_open_obligations,
)
from titan_brain.live.reconcile import ingest_local_order
from titan_brain.live.risk_runtime import RiskDecision as RuntimeRiskDecision
from titan_brain.live.state import LiveStateStore, object_hash
from titan_brain.live.writer_lock import AccountWriterLock
from tests.live_activation_support import activate_canonical_runtime


ROOT = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 8, 10, 0, tzinfo=ET)
ACCOUNT_KEY = "ending-7153"
ACCOUNT_MASKED = "••••7153"


class MutableClock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int = 1) -> None:
        self.value += timedelta(seconds=seconds)


def enabled_policy() -> PolicyBundle:
    base = PolicyBundle.load(ROOT)
    config = copy.deepcopy(base.config)
    config["authority"]["live_entries_enabled"] = True
    config["authority"]["blockers"] = []
    config["execution"]["supported_unattended_mutation"] = True
    config["execution"]["per_mutation_user_confirmation_required"] = False
    config["execution"]["local_mutation_interlock_enabled"] = True
    config["evidence"]["max_spread_bps"] = "25"
    config["evidence"]["minimum_depth_multiple"] = "5"
    config["risk"]["limits_live_provenance_verified"] = True
    config["notifications"].update(
        {
            "delivery_sink": "gmail_api",
            "destination_bridge_configured": True,
            "provider": "gmail",
            "destination_fingerprint": "f" * 64,
            "route_version": "synthetic-test-v1",
            "required_assurance": "PROVIDER_ACCEPTED",
            "provider_composition_id": "titan.gmail_api.rfc2822.oauth_injected.v1",
            "authorization_binding_id": "d" * 64,
            "timeout_seconds": 5,
        }
    )
    config["discovery"].update(
        {
            "pipeline_configured": True,
            "provider_composition_id": "titan.massive_rest_stream.robinhood_instrument.quality.v1",
            "provider_binding_id": "e" * 64,
            "instrument_evidence_provider": "synthetic_test_only",
            "quality_revalidation_provider": "synthetic_test_only",
            "minimum_setup_score": 70,
            "minimum_execution_score": 65,
            "a_plus_setup_score": 95,
            "a_plus_execution_score": 90,
        }
    )
    config_hash = sha256_json(config)
    policy_hash = sha256_json(
        {
            "account": config["account"],
            "scope": config["scope"],
            "sessions": config["sessions"],
            "risk": config["risk"],
            "risk_hash": base.risk_hash,
            "strategy_id": config["strategy_id"],
        }
    )
    policy = replace(
        base, config=config, config_hash=config_hash, policy_hash=policy_hash
    )
    policy.validate()
    policy.require_activation_ready()
    return policy


def position(quantity: int, *, held: int = 0) -> PositionSnapshot:
    return PositionSnapshot(
        symbol="XYZ",
        quantity=Decimal(quantity),
        sellable_quantity=Decimal(quantity - held),
        held_for_sells=Decimal(held),
        average_price=Decimal("10.00"),
    )


def snapshot(
    when: datetime,
    *,
    orders: tuple[OrderSnapshot, ...],
    positions: tuple[PositionSnapshot, ...],
    advanced_complete: bool = True,
) -> AccountSnapshot:
    return AccountSnapshot(
        account_masked=ACCOUNT_MASKED,
        observed_at=when,
        received_at=when,
        account_state="active",
        account_type="limited_margin",
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
        daily_realized_pnl=Decimal("0.00"),
        weekly_realized_pnl=Decimal("0.00"),
        peak_equity=Decimal("1000.00"),
        daily_realized_pnl_complete=True,
        weekly_realized_pnl_complete=True,
        peak_equity_complete=True,
        risk_evidence_authoritative=True,
        risk_evidence_source="deterministic-test-ledger",
        risk_evidence_as_of=when,
    )


class PendingProtectionBroker(FakeBrokerClient):
    def place_equity_order(
        self, request, *, review, explicit_confirmation=None
    ) -> BrokerOperationResult:
        result = super().place_equity_order(
            request,
            review=review,
            explicit_confirmation=explicit_confirmation,
        )
        pending = replace(
            result.order,
            state=BrokerOrderState.PENDING,
            broker_updated_at=self._now(),
            received_at=self._now(),
        )
        self._orders[pending.broker_order_id] = pending
        return replace(result, order=pending)


class PositionDisappearsAfterReviewBroker(FakeBrokerClient):
    """Simulate a manual liquidation racing an in-flight sell review."""

    def review_equity_order(self, request):
        receipt = super().review_equity_order(request)
        self._snapshot = replace(self._snapshot, equity_positions=())
        return receipt


class StaleProviderFactsBroker(FakeBrokerClient):
    """Return a freshly received envelope containing stale provider facts."""

    stale_final_snapshot = True

    def get_account_snapshot(self, account_masked: str) -> AccountSnapshot:
        account = super().get_account_snapshot(account_masked)
        if not self.stale_final_snapshot:
            return account
        now = self._now()
        return replace(
            account,
            observed_at=now - timedelta(seconds=10),
            received_at=now,
        )


class RegressedEnvelopeBroker(FakeBrokerClient):
    """Return otherwise-recent facts with timestamps older than the input."""

    def get_account_snapshot(self, account_masked: str) -> AccountSnapshot:
        account = super().get_account_snapshot(account_masked)
        now = self._now()
        return replace(
            account,
            observed_at=now - timedelta(seconds=2),
            received_at=now - timedelta(seconds=1),
            risk_evidence_as_of=now - timedelta(seconds=2),
        )


class RegressedRiskEvidenceBroker(FakeBrokerClient):
    def get_account_snapshot(self, account_masked: str) -> AccountSnapshot:
        account = super().get_account_snapshot(account_masked)
        return replace(
            account,
            risk_evidence_as_of=self._now() - timedelta(seconds=10),
        )


class RereceivedActiveOrderBroker(FakeBrokerClient):
    rereceive_orders = False

    def get_account_snapshot(self, account_masked: str) -> AccountSnapshot:
        account = super().get_account_snapshot(account_masked)
        if not self.rereceive_orders:
            return account
        return replace(
            account,
            equity_orders=tuple(
                replace(order, received_at=self._now())
                for order in account.equity_orders
            ),
        )


class LifecycleFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.temp = Path(self.temporary.name)
        self.clock = MutableClock()
        self.policy = enabled_policy()
        self.store = LiveStateStore(self.temp / "state.sqlite3")
        self.store.initialize_runtime(
            runtime_id=self.policy.runtime_id,
            account_key=ACCOUNT_KEY,
            release_manifest_hash="a" * 64,
            config_hash=self.policy.config_hash,
            policy_hash=self.policy.policy_hash,
            initialized_at=NOW - timedelta(minutes=2),
        )
        self.lock = AccountWriterLock(
            self.temp / "locks", ACCOUNT_KEY, owner_id="lifecycle-test"
        )
        self.lock.acquire()
        self.store.acquire_writer_lease(
            account_key=ACCOUNT_KEY,
            owner_id=self.lock.owner_id,
            acquired_at=NOW - timedelta(minutes=1),
        )

    def tearDown(self) -> None:
        self.lock.release()
        self.store.close()
        self.temporary.cleanup()

    def arm(self) -> None:
        activate_canonical_runtime(
            self.store,
            created_at=NOW - timedelta(seconds=42),
            activated_at=NOW - timedelta(seconds=40),
            expires_at=NOW + timedelta(minutes=5),
        )

    def seed_entry(
        self, fill_quantities: tuple[int, ...] = (2,)
    ) -> tuple[ExpiringPlan, OrderSnapshot, AccountSnapshot]:
        total = sum(fill_quantities)
        plan = ExpiringPlan(
            plan_id="plan-xyz",
            account_key=ACCOUNT_KEY,
            strategy_id=self.policy.strategy_id,
            symbol="XYZ",
            setup_id="ORB_BREAKOUT",
            quantity=total,
            limit_price=Decimal("10.00"),
            structural_stop=Decimal("9.50"),
            market_hours="regular_hours",
            time_in_force="gfd",
            evidence_cutoff_at=NOW - timedelta(seconds=30),
            created_at=NOW - timedelta(seconds=29),
            expires_at=NOW + timedelta(minutes=10),
            policy_hash=self.policy.policy_hash,
            config_hash=self.policy.config_hash,
            evidence_hash="b" * 64,
        )
        reservation = RiskReservation(
            reservation_id="reservation-xyz",
            plan_id=plan.plan_id,
            account_key=ACCOUNT_KEY,
            planned_risk=Decimal("1.00"),
            stress_risk=Decimal("1.20"),
            execution_reserve=Decimal("0.20"),
            notional=Decimal(total * 10),
            created_at=NOW - timedelta(seconds=28),
        )
        client_ref = str(uuid4())
        order_tuple = {
            "account_key": ACCOUNT_KEY,
            "account_masked": ACCOUNT_MASKED,
            "symbol": "XYZ",
            "side": "buy",
            "order_type": "limit",
            "quantity": total,
            "market_hours": "regular_hours",
            "time_in_force": "gfd",
            "limit_price": "10.00",
            "stop_price": None,
            "client_ref_id": client_ref,
        }
        intent = OrderIntent(
            intent_id="intent-entry-xyz",
            plan_id=plan.plan_id,
            reservation_id=reservation.reservation_id,
            account_key=ACCOUNT_KEY,
            kind=IntentKind.ENTRY,
            client_ref=client_ref,
            order_tuple=order_tuple,
            tuple_hash=object_hash(order_tuple),
            created_at=NOW - timedelta(seconds=27),
            acknowledgement_deadline_at=NOW - timedelta(seconds=17),
        )
        self.store.prepare_submission(
            plan=plan, reservation=reservation, intent=intent
        )
        self.store.transition_intent(
            intent.intent_id,
            IntentState.SUBMITTING,
            occurred_at=NOW - timedelta(seconds=26),
        )
        fills = tuple(
            FillSnapshot(
                fill_id=f"entry-fill-{index}",
                quantity=Decimal(quantity),
                price=Decimal("10.00"),
                executed_at=NOW - timedelta(seconds=25 - index),
            )
            for index, quantity in enumerate(fill_quantities, start=1)
        )
        order = OrderSnapshot(
            broker_order_id="entry-order-xyz",
            account_masked=ACCOUNT_MASKED,
            symbol="XYZ",
            side=BrokerSide.BUY,
            order_type=EquityOrderType.LIMIT,
            state=BrokerOrderState.FILLED,
            requested_quantity=Decimal(total),
            cumulative_filled_quantity=Decimal(total),
            market_hours=MarketHours.REGULAR,
            time_in_force=TimeInForce.GFD,
            limit_price=Decimal("10.00"),
            client_ref_id=client_ref,
            broker_updated_at=NOW - timedelta(seconds=20),
            received_at=NOW - timedelta(seconds=20),
            fills=fills,
        )
        ingest_local_order(
            self.store,
            order=order,
            intent_id=intent.intent_id,
            account_key=ACCOUNT_KEY,
        )
        ensure_entry_fill_obligations(
            self.store,
            order=order,
            intent_id=intent.intent_id,
            account_key=ACCOUNT_KEY,
        )
        account = snapshot(
            self.clock(), orders=(order,), positions=(position(total),)
        )
        return plan, order, account

    def adapter(
        self, broker: FakeBrokerClient, *, allow_mutations: bool = True
    ) -> ProductionLifecycleActions:
        return ProductionLifecycleActions(
            policy=self.policy,
            state=self.store,
            broker=broker,
            writer_lock=self.lock,
            clock=self.clock,
            allow_mutations=allow_mutations,
        )

    def protection_decision(self, account: AccountSnapshot):
        position_by_symbol = {item.symbol: item for item in account.equity_positions}
        return assess_protection(
            position=position_by_symbol.get("XYZ"),
            orders=account.equity_orders,
            obligations=load_open_obligations(
                self.store, account_key=ACCOUNT_KEY, symbol="XYZ"
            ),
            symbol="XYZ",
        )


class ProductionLifecycleActionsTests(LifecycleFixture):
    def test_final_cancel_authority_accepts_monotone_rereceipt_and_returns_target(self) -> None:
        self.arm()
        _, _, account = self.seed_entry((1,))
        broker = RereceivedActiveOrderBroker(
            initial_snapshot=account, clock=self.clock
        )
        actions = self.adapter(broker)
        protected = actions.protect(
            snapshot=account,
            decision=self.protection_decision(account),
            now=self.clock(),
        )
        self.assertTrue(protected.startswith("PROTECTION:ACKNOWLEDGED"), protected)
        self.clock.advance()
        supplied = broker.get_account_snapshot(ACCOUNT_MASKED)
        target = next(
            order for order in supplied.equity_orders if not order.state.terminal
        )
        broker.rereceive_orders = True

        refreshed = actions.require_mutation_authority(
            snapshot=supplied,
            operation=MutationOperation.CANCEL,
            phase=MutationPhase.BEFORE_CANCEL,
            now=self.clock(),
            plan_id=self._plan_for_local_order_for_test(actions, target),
            kind=IntentKind.CANCEL,
            target=target,
        )

        refreshed_target = next(
            order
            for order in refreshed.equity_orders
            if order.broker_order_id == target.broker_order_id
        )
        self.assertGreater(refreshed_target.received_at, target.received_at)

    @staticmethod
    def _plan_for_local_order_for_test(
        actions: ProductionLifecycleActions, target: OrderSnapshot
    ) -> str:
        return actions._plan_for_local_order(target.broker_order_id)

    def test_final_entry_authority_rejects_fresh_receipt_with_stale_facts(self) -> None:
        self.arm()
        account = snapshot(self.clock(), orders=(), positions=())
        broker = StaleProviderFactsBroker(initial_snapshot=account, clock=self.clock)
        actions = self.adapter(broker)
        request = OrderRequest(
            account_masked=ACCOUNT_MASKED,
            symbol="XYZ",
            side=BrokerSide.BUY,
            order_type=EquityOrderType.LIMIT,
            quantity=1,
            market_hours=MarketHours.REGULAR,
            time_in_force=TimeInForce.GFD,
            client_ref_id=str(uuid4()),
            limit_price="10.00",
        )

        with self.assertRaises(MutationAuthorityDenied) as caught:
            actions.require_mutation_authority(
                snapshot=account,
                operation=MutationOperation.ENTRY_PLACE,
                phase=MutationPhase.BEFORE_PLACE,
                now=self.clock(),
                plan_id="plan-entry-stale-facts",
                kind=IntentKind.ENTRY,
                request=request,
            )

        self.assertIn("STALE_SNAPSHOT", caught.exception.failure_codes)
        self.assertIn(
            "FINAL_BROKER_SNAPSHOT_OBSERVED_REGRESSED",
            caught.exception.failure_codes,
        )

    def test_final_sell_authority_rejects_fresh_receipt_with_stale_facts(self) -> None:
        self.arm()
        _, _, account = self.seed_entry((1,))
        broker = StaleProviderFactsBroker(initial_snapshot=account, clock=self.clock)
        actions = self.adapter(broker)

        result = actions.protect(
            snapshot=account,
            decision=self.protection_decision(account),
            now=self.clock(),
        )

        self.assertIn("STALE_SNAPSHOT", result)
        self.assertIn("FINAL_BROKER_SNAPSHOT_OBSERVED_REGRESSED", result)
        self.assertFalse(
            any(call[0] == FakeBrokerClient.PLACE for call in broker.calls)
        )

    def test_final_sell_authority_rejects_observed_and_receipt_regressions(self) -> None:
        self.arm()
        _, _, account = self.seed_entry((1,))
        broker = RegressedEnvelopeBroker(initial_snapshot=account, clock=self.clock)
        actions = self.adapter(broker)

        result = actions.protect(
            snapshot=account,
            decision=self.protection_decision(account),
            now=self.clock(),
        )

        self.assertIn("FINAL_BROKER_SNAPSHOT_OBSERVED_REGRESSED", result)
        self.assertIn("FINAL_BROKER_SNAPSHOT_RECEIPT_REGRESSED", result)
        self.assertFalse(
            any(call[0] == FakeBrokerClient.PLACE for call in broker.calls)
        )

    def test_final_entry_authority_rejects_stale_regressed_risk_evidence(self) -> None:
        self.arm()
        account = snapshot(self.clock(), orders=(), positions=())
        broker = RegressedRiskEvidenceBroker(
            initial_snapshot=account, clock=self.clock
        )
        actions = self.adapter(broker)
        request = OrderRequest(
            account_masked=ACCOUNT_MASKED,
            symbol="XYZ",
            side=BrokerSide.BUY,
            order_type=EquityOrderType.LIMIT,
            quantity=1,
            market_hours=MarketHours.REGULAR,
            time_in_force=TimeInForce.GFD,
            client_ref_id=str(uuid4()),
            limit_price="10.00",
        )

        with self.assertRaises(MutationAuthorityDenied) as caught:
            actions.require_mutation_authority(
                snapshot=account,
                operation=MutationOperation.ENTRY_PLACE,
                phase=MutationPhase.BEFORE_PLACE,
                now=self.clock(),
                plan_id="plan-entry-stale-risk",
                kind=IntentKind.ENTRY,
                request=request,
            )

        self.assertIn("ENTRY_RISK_EVIDENCE_REGRESSED", caught.exception.failure_codes)
        self.assertIn("STALE_ENTRY_RISK_EVIDENCE", caught.exception.failure_codes)

    def test_final_cancel_authority_rejects_fresh_receipt_with_stale_facts(self) -> None:
        self.arm()
        _, _, account = self.seed_entry((1,))
        broker = StaleProviderFactsBroker(initial_snapshot=account, clock=self.clock)
        broker.stale_final_snapshot = False
        actions = self.adapter(broker)
        protected = actions.protect(
            snapshot=account,
            decision=self.protection_decision(account),
            now=self.clock(),
        )
        self.assertTrue(protected.startswith("PROTECTION:ACKNOWLEDGED"), protected)
        self.clock.advance()
        with_stop = broker.get_account_snapshot(ACCOUNT_MASKED)
        close = plan_safe_close(
            position=with_stop.equity_positions[0],
            orders=with_stop.equity_orders,
            symbol="XYZ",
            snapshot_received_at=with_stop.received_at,
        )
        self.assertEqual(close.action, ExitAction.CANCEL_EXIT_ORDERS)
        broker.stale_final_snapshot = True

        result = actions.closeout(snapshot=with_stop, decision=close, now=self.clock())

        self.assertIn("STALE_SNAPSHOT", result)
        self.assertIn("FINAL_BROKER_SNAPSHOT_OBSERVED_REGRESSED", result)
        self.assertFalse(any(call[0] == FakeBrokerClient.CANCEL for call in broker.calls))

    def test_fabricated_allowed_risk_cannot_bypass_current_daily_loss(self) -> None:
        """The production mutation capability, not its caller, owns risk truth."""

        self.arm()
        account = replace(
            snapshot(self.clock(), orders=(), positions=()),
            daily_realized_pnl=Decimal("-100.00"),
            weekly_realized_pnl=Decimal("-100.00"),
        )
        broker = FakeBrokerClient(initial_snapshot=account, clock=self.clock)
        actions = self.adapter(broker)
        self.store.apply_session_latch(
            DurableSessionLatch(
                account_key=ACCOUNT_KEY,
                trading_date=self.clock().date(),
                loss_locked=False,
                objective_crossed=False,
                pause_new_entries=False,
                closeout_started=False,
                hard_kill=False,
                highest_realized_pnl=Decimal("0"),
                first_objective_crossed_at=None,
                revision=0,
                updated_at=self.clock(),
            )
        )
        plan = SignedPlan.build(
            strategy_id=self.policy.strategy_id,
            policy_hash=self.policy.policy_hash,
            config_hash=self.policy.config_hash,
            account_last4=self.policy.account_last4,
            symbol="XYZ",
            instrument_id="rh-instrument-xyz",
            setup_id="ORB_BREAKOUT",
            quantity=2,
            entry_limit="10.05",
            structural_stop="9.50",
            targets=("11.00", "12.00"),
            execution_reserve_per_share="0.05",
            market_hours="regular_hours",
            time_in_force="gfd",
            quality_tier="normal",
            completed_bar_end=self.clock() - timedelta(minutes=1),
            quote_observed_at=self.clock() - timedelta(seconds=1),
            created_at=self.clock() - timedelta(seconds=1),
            expires_at=self.clock() + timedelta(seconds=20),
            source_event_ids=("massive-bar-1", "rh-quote-1"),
            direction="long",
            allow_add=False,
            allow_reentry=False,
        )
        request = OrderRequest(
            account_masked=ACCOUNT_MASKED,
            symbol="XYZ",
            side=BrokerSide.BUY,
            order_type=EquityOrderType.LIMIT,
            quantity=2,
            market_hours=MarketHours.REGULAR,
            time_in_force=TimeInForce.GFD,
            client_ref_id=str(uuid4()),
            limit_price="10.05",
        )
        fabricated = RuntimeRiskDecision(
            allowed=True,
            failures=(),
            proposal_planned_risk=plan.planned_risk,
            proposal_stress_risk=plan.stress_risk,
            proposal_reserve=plan.execution_reserve,
            remaining_daily_headroom=Decimal("50.00"),
            remaining_portfolio_headroom=Decimal("50.00"),
            remaining_stress_headroom=Decimal("50.00"),
            remaining_buying_power=Decimal("500.00"),
        )

        with self.assertRaises(MutationAuthorityDenied) as caught:
            actions.require_mutation_authority(
                snapshot=account,
                operation=MutationOperation.ENTRY_PLACE,
                phase=MutationPhase.BEFORE_PREPARE,
                now=self.clock(),
                plan_id=plan.plan_id,
                kind=IntentKind.ENTRY,
                request=request,
                plan=plan,
                risk_decision=fabricated,
            )

        self.assertIn("CURRENT_ACCOUNT_RISK_DENIES_ENTRY", caught.exception.failure_codes)
        self.assertIn(
            "RISK_DECISION_NOT_BOUND_TO_CURRENT_SNAPSHOT",
            caught.exception.failure_codes,
        )
        self.assertFalse(
            any(
                call[0] in {FakeBrokerClient.REVIEW, FakeBrokerClient.PLACE}
                for call in broker.calls
            )
        )

    def test_default_interlock_and_missing_runtime_authority_make_zero_calls(self) -> None:
        self.arm()
        _, _, account = self.seed_entry((1,))
        broker = FakeBrokerClient(initial_snapshot=account, clock=self.clock)
        decision = self.protection_decision(account)

        disabled = self.adapter(broker, allow_mutations=False)
        self.assertIn("LOCAL_MUTATION_INTERLOCK_DISABLED", disabled.protect(
            snapshot=account, decision=decision, now=self.clock()
        ))
        self.assertFalse(broker.calls)

        # A separate paused runtime has no owner-granted authority even when
        # the deployment interlock is true.
        self.store.close()
        self.lock.release()
        paused_store = LiveStateStore(self.temp / "paused.sqlite3")
        paused_store.initialize_runtime(
            runtime_id=self.policy.runtime_id,
            account_key=ACCOUNT_KEY,
            release_manifest_hash="a" * 64,
            config_hash=self.policy.config_hash,
            policy_hash=self.policy.policy_hash,
            initialized_at=NOW,
        )
        paused_lock = AccountWriterLock(
            self.temp / "paused-lock", ACCOUNT_KEY, owner_id="paused-test"
        )
        paused_lock.acquire()
        paused_store.acquire_writer_lease(
            account_key=ACCOUNT_KEY,
            owner_id=paused_lock.owner_id,
            acquired_at=NOW,
        )
        try:
            paused = ProductionLifecycleActions(
                policy=self.policy,
                state=paused_store,
                broker=broker,
                writer_lock=paused_lock,
                clock=self.clock,
                allow_mutations=True,
            )
            self.assertIn("RUNTIME_AUTHORITY_DISABLED", paused.protect(
                snapshot=account, decision=decision, now=self.clock()
            ))
            self.assertFalse(broker.calls)
        finally:
            paused_lock.release()
            paused_store.close()
        # Prevent the fixture teardown from closing/releasing twice.
        self.store = LiveStateStore(self.temp / "teardown.sqlite3")
        self.lock = AccountWriterLock(self.temp / "teardown-lock", ACCOUNT_KEY)

    def test_each_fill_gets_one_exact_stop_on_distinct_fresh_snapshots(self) -> None:
        self.arm()
        _, _, account = self.seed_entry((1, 1))
        broker = FakeBrokerClient(initial_snapshot=account, clock=self.clock)
        actions = self.adapter(broker)

        first = actions.protect(
            snapshot=account,
            decision=self.protection_decision(account),
            now=self.clock(),
        )
        self.assertTrue(first.startswith("PROTECTION:ACKNOWLEDGED"), first)
        obligations = load_open_obligations(
            self.store, account_key=ACCOUNT_KEY, symbol="XYZ"
        )
        self.assertEqual(
            sorted(item.state.value for item in obligations),
            sorted(
                (
                    ProtectionState.WORKING.value,
                    ProtectionState.REQUIRED.value,
                )
            ),
        )
        same_snapshot = actions.protect(
            snapshot=account,
            decision=self.protection_decision(account),
            now=self.clock(),
        )
        self.assertEqual(same_snapshot, "BLOCKED:SNAPSHOT_ALREADY_USED_FOR_MUTATION")

        self.clock.advance()
        newer = broker.get_account_snapshot(ACCOUNT_MASKED)
        second = actions.protect(
            snapshot=newer,
            decision=self.protection_decision(newer),
            now=self.clock(),
        )
        self.assertTrue(second.startswith("PROTECTION:ACKNOWLEDGED"), second)
        stops = tuple(
            order
            for order in broker.get_account_snapshot(ACCOUNT_MASKED).equity_orders
            if order.side is BrokerSide.SELL
        )
        self.assertEqual(len(stops), 2)
        self.assertTrue(
            all(
                (
                    order.order_type,
                    order.market_hours,
                    order.time_in_force,
                    order.stop_price,
                    order.requested_quantity,
                )
                == (
                    EquityOrderType.STOP_MARKET,
                    MarketHours.REGULAR,
                    TimeInForce.GTC,
                    Decimal("9.50"),
                    Decimal("1"),
                )
                for order in stops
            )
        )

    def test_final_boundary_refetch_blocks_position_change_during_review(self) -> None:
        self.arm()
        _, _, account = self.seed_entry((1,))
        broker = PositionDisappearsAfterReviewBroker(
            initial_snapshot=account, clock=self.clock
        )
        actions = self.adapter(broker)

        result = actions.protect(
            snapshot=account,
            decision=self.protection_decision(account),
            now=self.clock(),
        )

        self.assertIn("NO_BROKER_POSITION_TO_EXIT", result)
        self.assertEqual(
            sum(call[0] == FakeBrokerClient.PLACE for call in broker.calls), 0
        )
        # One final broker read follows each review attempt.  The important
        # invariant is that no request crosses the placement boundary after
        # the refreshed snapshot disproves sell capacity.
        self.assertGreaterEqual(
            sum(call[0] == FakeBrokerClient.SNAPSHOT for call in broker.calls), 1
        )

    def test_pending_is_not_working_and_confirmed_then_filled_converges(self) -> None:
        self.arm()
        _, _, account = self.seed_entry((1,))
        broker = PendingProtectionBroker(initial_snapshot=account, clock=self.clock)
        actions = self.adapter(broker)
        result = actions.protect(
            snapshot=account,
            decision=self.protection_decision(account),
            now=self.clock(),
        )
        self.assertTrue(result.startswith("PROTECTION:ACKNOWLEDGED"), result)
        obligation = load_open_obligations(
            self.store, account_key=ACCOUNT_KEY, symbol="XYZ"
        )[0]
        self.assertEqual(obligation.state, ProtectionState.SUBMITTED)
        self.assertEqual(obligation.working_quantity, 0)

        stop_id = result and next(
            order.broker_order_id
            for order in broker.get_account_snapshot(ACCOUNT_MASKED).equity_orders
            if order.side is BrokerSide.SELL
        )
        self.clock.advance()
        pending_order = broker._orders[stop_id]
        broker._orders[stop_id] = replace(
            pending_order,
            state=BrokerOrderState.CONFIRMED,
            broker_updated_at=self.clock(),
            received_at=self.clock(),
        )
        confirmed = broker.get_account_snapshot(ACCOUNT_MASKED)
        sync = actions.reconcile(snapshot=confirmed, now=self.clock())
        self.assertFalse(sync.blockers)
        obligation = load_open_obligations(
            self.store, account_key=ACCOUNT_KEY, symbol="XYZ"
        )[0]
        self.assertEqual(obligation.state, ProtectionState.WORKING)
        self.assertEqual(obligation.working_quantity, 1)

        self.clock.advance()
        broker.fill_order(stop_id, 1, "9.50")
        filled_snapshot = replace(
            broker.get_account_snapshot(ACCOUNT_MASKED), equity_positions=()
        )
        sync = actions.reconcile(snapshot=filled_snapshot, now=self.clock())
        self.assertFalse(sync.blockers)
        self.assertEqual(
            self.store.row(
                "protection_obligations", "obligation_id", obligation.obligation_id
            )["state"],
            ProtectionState.SATISFIED.value,
        )

    def test_unknown_protection_never_retries_or_overlaps_exit(self) -> None:
        self.arm()
        _, _, account = self.seed_entry((1,))
        broker = FakeBrokerClient(initial_snapshot=account, clock=self.clock)
        broker.inject_fault(
            FakeBrokerClient.PLACE, FakeFault.PLACE_UNKNOWN_AFTER_ACCEPT
        )
        actions = self.adapter(broker)
        decision = self.protection_decision(account)
        first = actions.protect(
            snapshot=account, decision=decision, now=self.clock()
        )
        self.assertTrue(first.startswith("PROTECTION:UNKNOWN"), first)
        place_count = sum(call[0] == FakeBrokerClient.PLACE for call in broker.calls)

        replay = actions.protect(
            snapshot=account, decision=decision, now=self.clock()
        )
        self.assertIn("SNAPSHOT_ALREADY_USED", replay)
        self.assertEqual(
            sum(call[0] == FakeBrokerClient.PLACE for call in broker.calls),
            place_count,
        )
        self.clock.advance()
        accepted = next(
            order
            for order in broker._orders.values()
            if order.side is BrokerSide.SELL
        )
        broker._orders[accepted.broker_order_id] = replace(
            accepted,
            broker_updated_at=self.clock(),
            received_at=self.clock(),
        )
        newer = broker.get_account_snapshot(ACCOUNT_MASKED)
        sync = actions.reconcile(snapshot=newer, now=self.clock())
        self.assertFalse(sync.blockers)
        self.assertEqual(
            load_open_obligations(
                self.store, account_key=ACCOUNT_KEY, symbol="XYZ"
            )[0].state,
            ProtectionState.WORKING,
        )
        self.assertEqual(
            sum(call[0] == FakeBrokerClient.PLACE for call in broker.calls),
            place_count,
        )

    def test_known_protection_rejection_requires_new_snapshot_then_safe_closes(self) -> None:
        self.arm()
        _, _, account = self.seed_entry((1,))
        broker = FakeBrokerClient(initial_snapshot=account, clock=self.clock)
        broker.inject_fault(FakeBrokerClient.PLACE, FakeFault.PLACE_REJECTED)
        actions = self.adapter(broker)
        result = actions.protect(
            snapshot=account,
            decision=self.protection_decision(account),
            now=self.clock(),
        )
        self.assertEqual(result, "EXIT:BLOCKED:SNAPSHOT_ALREADY_USED_FOR_MUTATION")
        placed = [call[1] for call in broker.calls if call[0] == FakeBrokerClient.PLACE]
        self.assertEqual(len(placed), 1)

        self.clock.advance()
        newer = broker.get_account_snapshot(ACCOUNT_MASKED)
        recovered = actions.protect(
            snapshot=newer,
            decision=self.protection_decision(newer),
            now=self.clock(),
        )
        self.assertTrue(recovered.startswith("EXIT:ACKNOWLEDGED"), recovered)
        placed = [call[1] for call in broker.calls if call[0] == FakeBrokerClient.PLACE]
        self.assertEqual(len(placed), 2)
        self.assertEqual(placed[-1][2:7], ("sell", "market", 1, "regular_hours", "gfd"))

    def test_closeout_cancels_then_waits_for_strictly_newer_snapshot(self) -> None:
        self.arm()
        _, _, account = self.seed_entry((1,))
        broker = FakeBrokerClient(initial_snapshot=account, clock=self.clock)
        actions = self.adapter(broker)
        protected = actions.protect(
            snapshot=account,
            decision=self.protection_decision(account),
            now=self.clock(),
        )
        self.assertTrue(protected.startswith("PROTECTION:ACKNOWLEDGED"), protected)

        self.clock.advance()
        with_stop = broker.get_account_snapshot(ACCOUNT_MASKED)
        close = plan_safe_close(
            position=with_stop.equity_positions[0],
            orders=with_stop.equity_orders,
            symbol="XYZ",
            snapshot_received_at=with_stop.received_at,
        )
        self.assertEqual(close.action, ExitAction.CANCEL_EXIT_ORDERS)
        cancelled = actions.closeout(
            snapshot=with_stop, decision=close, now=self.clock()
        )
        self.assertTrue(cancelled.startswith("CANCEL:ACKNOWLEDGED"), cancelled)
        place_count = sum(call[0] == FakeBrokerClient.PLACE for call in broker.calls)

        self.clock.advance()
        pending = broker.get_account_snapshot(ACCOUNT_MASKED)
        pending_close = plan_safe_close(
            position=pending.equity_positions[0],
            orders=pending.equity_orders,
            symbol="XYZ",
            snapshot_received_at=pending.received_at,
        )
        self.assertEqual(pending_close.action, ExitAction.WAIT_CANCEL_CONFIRMATION)
        self.assertIn(
            "CANCEL_PENDING_RECONCILIATION",
            actions.closeout(
                snapshot=pending, decision=pending_close, now=self.clock()
            ),
        )

        stop_id = close.cancel_order_ids[0]
        self.clock.advance()
        broker.settle_cancel(stop_id)
        terminal = broker.get_account_snapshot(ACCOUNT_MASKED)
        terminal_close = plan_safe_close(
            position=terminal.equity_positions[0],
            orders=terminal.equity_orders,
            symbol="XYZ",
            snapshot_received_at=terminal.received_at,
        )
        self.assertEqual(terminal_close.action, ExitAction.SUBMIT_SAFE_CLOSE)
        self.assertIn(
            "STRICTLY_NEWER_POST_MUTATION_SNAPSHOT_REQUIRED",
            actions.closeout(
                snapshot=terminal, decision=terminal_close, now=self.clock()
            ),
        )
        self.assertEqual(
            sum(call[0] == FakeBrokerClient.PLACE for call in broker.calls),
            place_count,
        )

        self.clock.advance()
        newer = broker.get_account_snapshot(ACCOUNT_MASKED)
        newer_close = plan_safe_close(
            position=newer.equity_positions[0],
            orders=newer.equity_orders,
            symbol="XYZ",
            snapshot_received_at=newer.received_at,
        )
        submitted = actions.closeout(
            snapshot=newer, decision=newer_close, now=self.clock()
        )
        self.assertTrue(submitted.startswith("EXIT:ACKNOWLEDGED"), submitted)
        self.assertEqual(
            sum(call[0] == FakeBrokerClient.PLACE for call in broker.calls),
            place_count + 1,
        )

    def test_manual_sell_overlap_and_stale_or_incomplete_evidence_block(self) -> None:
        self.arm()
        _, entry, account = self.seed_entry((1,))
        manual_sell = OrderSnapshot(
            broker_order_id="manual-sell",
            account_masked=ACCOUNT_MASKED,
            symbol="XYZ",
            side=BrokerSide.SELL,
            order_type=EquityOrderType.LIMIT,
            state=BrokerOrderState.CONFIRMED,
            requested_quantity=Decimal("1"),
            cumulative_filled_quantity=Decimal("0"),
            market_hours=MarketHours.REGULAR,
            time_in_force=TimeInForce.GFD,
            limit_price=Decimal("11.00"),
            client_ref_id=None,
            broker_updated_at=self.clock(),
            received_at=self.clock(),
        )
        overlap = replace(
            account,
            equity_orders=(entry, manual_sell),
            equity_positions=(position(1, held=1),),
        )
        broker = FakeBrokerClient(initial_snapshot=overlap, clock=self.clock)
        actions = self.adapter(broker)
        result = actions.protect(
            snapshot=overlap,
            decision=self.protection_decision(overlap),
            now=self.clock(),
        )
        self.assertIn("INSUFFICIENT_OR_UNCERTAIN_EXIT_CAPACITY", result)
        self.assertFalse(any(call[0] == FakeBrokerClient.PLACE for call in broker.calls))

        stale = replace(
            account,
            observed_at=self.clock() - timedelta(seconds=10),
            received_at=self.clock() - timedelta(seconds=10),
            risk_evidence_as_of=self.clock() - timedelta(seconds=10),
        )
        stale_result = actions.protect(
            snapshot=stale,
            decision=self.protection_decision(stale),
            now=self.clock(),
        )
        self.assertIn("STALE_SNAPSHOT", stale_result)
        incomplete = replace(account, advanced_orders_complete=False)
        incomplete_result = actions.protect(
            snapshot=incomplete,
            decision=self.protection_decision(incomplete),
            now=self.clock(),
        )
        self.assertIn("ADVANCED_RECONCILIATION_INCOMPLETE", incomplete_result)

    def test_lost_writer_lock_and_unowned_position_fail_closed(self) -> None:
        self.arm()
        _, _, account = self.seed_entry((1,))
        broker = FakeBrokerClient(initial_snapshot=account, clock=self.clock)
        actions = self.adapter(broker)
        self.lock.release()
        result = actions.protect(
            snapshot=account,
            decision=self.protection_decision(account),
            now=self.clock(),
        )
        self.assertIn("ACCOUNT_WRITER_KERNEL_LOCK_NOT_HELD", result)
        self.assertFalse(broker.calls)

        # Reacquire the same lock, then prove that a broker position larger
        # than the durable fill ledger is never silently adopted.
        self.lock.acquire()
        unowned = replace(account, equity_positions=(position(2),))
        decision = self.protection_decision(unowned)
        result = actions.protect(
            snapshot=unowned, decision=decision, now=self.clock()
        )
        self.assertIn("LIFECYCLE_PROTECTION_FAILED", result)
        self.assertFalse(broker.calls)


if __name__ == "__main__":
    unittest.main()
