"""The final SDK preflight must reprice risk from its own fresh broker read."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from titan_brain.live.broker.base import (
    BrokerMutationBlocked, BrokerSide, EquityOrderType, FundsSnapshot, MarketHours, OrderRequest, TimeInForce,
)
from titan_brain.live.broker.ibkr_preflight import IbkrAttendedOrderPlan, IbkrAttendedPreflightBridge, IbkrOrderPurpose
from titan_brain.live.broker.ibkr_instrument import IbkrInstrumentProvider
from titan_brain.live.ibkr_autonomous_authority import IbkrAutonomousAuthorityBindings
from titan_brain.live.ibkr_autonomous_inputs import DurableIbkrAutonomousAcceptanceVerifier, DurableIbkrAutonomousRiskPolicyCheck
from titan_brain.live.ibkr_autonomous_interlock import AutonomousIbkrWriterInterlock
from titan_brain.live.ibkr_command_inputs import DurableIbkrRiskPolicyCheck, IbkrCommandInputError
from titan_brain.live.models import ExpiringPlan, IntentKind, OrderIntent, RiskReservation, SessionLatch
from titan_brain.live.policy import PolicyBundle
from titan_brain.live.risk_evidence_binding import risk_high_water_receipt_hash
from titan_brain.live.risk_runtime import entry_lifecycle_fee_reserve
from titan_brain.live.state import LiveStateStore, StateConflict, object_hash
from titan_brain.live.writer_lock import AccountWriterLock
from tests.test_live_ibkr_instrument_preflight import NOW, FakeContractRequester, snapshot as raw_snapshot
from tests.test_live_ibkr_autonomous_interlock import autonomous_policy, ACCOUNT_BINDING, AUTHORIZATION_BINDING, PROVIDER_CONTRACT, TRANSPORT, CLIENT_ID
from tests.live_activation_support import activate_canonical_runtime


ROOT = Path(__file__).resolve().parents[1]


class DollarFinalGateTests(unittest.TestCase):
    def setUp(self):
        self.policy = getattr(self, "policy", None) or PolicyBundle.load(ROOT, config_relative="config/full_live_ibkr.json")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "state.sqlite3"
        self.store = LiveStateStore(self.path)
        self.addCleanup(self.store.close)
        self.store.initialize_runtime(
            runtime_id=self.policy.runtime_id, account_key=self.policy.account_key,
            release_manifest_hash="a" * 64, config_hash=self.policy.config_hash,
            policy_hash=self.policy.policy_hash, initialized_at=NOW - timedelta(seconds=2),
        )
        with self.store.transaction() as connection:
            connection.execute("UPDATE runtime_identity SET mode='ACTIVE',authority_enabled=1")
        self.store.apply_session_latch(SessionLatch(
            account_key=self.policy.account_key, trading_date=NOW.date(),
            loss_locked=False, objective_crossed=False, pause_new_entries=False,
            closeout_started=False, hard_kill=False, highest_realized_pnl=Decimal("0"),
            first_objective_crossed_at=None, revision=0, updated_at=NOW,
        ))
        self.check = DurableIbkrRiskPolicyCheck(state_path=self.path, policy=self.policy)

    def snapshot(self, pnl="0", funds="10000", **overrides):
        peak = Decimal("10000")
        values = dict(
            account_masked="****3103", account_type="no_borrow_margin", account_state="active",
            funds=FundsSnapshot(total_value=peak, cash=funds, buying_power=funds,
                                unleveraged_buying_power=funds),
            daily_realized_pnl=Decimal(pnl), weekly_realized_pnl=Decimal(pnl),
            peak_equity=peak, weekly_realized_pnl_complete=True, peak_equity_complete=True,
            risk_baseline_identity_hash="a" * 64, risk_baseline_receipt_hash="b" * 64,
            risk_high_water_identity_hash="c" * 64, risk_high_water_lineage_hash="d" * 64,
            risk_high_water_receipt_hash=risk_high_water_receipt_hash(
                identity_hash="c" * 64, baseline_receipt_hash="b" * 64,
                lineage_hash="d" * 64, peak_equity=peak,
            ),
        )
        values.update(overrides)
        return replace(raw_snapshot(), **values)

    def prepare(self, quantity=2, entry="12", stop="11.50", symbol="TEST"):
        plan_id = object_hash({"symbol": symbol, "nonce": str(uuid4())})
        request = OrderRequest(
            account_masked="****3103", symbol=symbol, side=BrokerSide.BUY,
            order_type=EquityOrderType.LIMIT, quantity=quantity,
            market_hours=MarketHours.REGULAR, time_in_force=TimeInForce.GFD,
            client_ref_id=str(uuid4()), limit_price=Decimal(entry),
        )
        fee = entry_lifecycle_fee_reserve(self.policy, quantity=quantity)
        execution = Decimal(".05") * quantity
        plan = IbkrAttendedOrderPlan(
            plan_id=plan_id, purpose=IbkrOrderPurpose.ENTRY, request=request,
            structural_stop=Decimal(stop), targets=(Decimal(entry) + 1,),
            execution_reserve=execution, fee_reserve=fee,
            required_stop_request=OrderRequest(
                account_masked=request.account_masked, symbol=symbol, side=BrokerSide.SELL,
                order_type=EquityOrderType.STOP_MARKET, quantity=quantity,
                market_hours=MarketHours.REGULAR, time_in_force=TimeInForce.GTC,
                client_ref_id=str(uuid4()), stop_price=Decimal(stop),
            ),
        )
        order_tuple = {
            "account_key": self.policy.account_key, "account_masked": request.account_masked,
            "symbol": symbol, "side": request.side.value, "order_type": request.order_type.value,
            "quantity": quantity, "market_hours": request.market_hours.value,
            "time_in_force": request.time_in_force.value, "limit_price": format(request.limit_price, "f"),
            "stop_price": None, "client_ref_id": request.client_ref_id,
        }
        reservation_id = str(uuid4())
        created = NOW - timedelta(seconds=1)
        self.store.prepare_submission(
            plan=ExpiringPlan(
                plan_id=plan_id, account_key=self.policy.account_key,
                strategy_id=self.policy.strategy_id, symbol=symbol, setup_id="test",
                quantity=quantity, limit_price=Decimal(entry), structural_stop=Decimal(stop),
                market_hours="regular_hours", time_in_force="gfd", evidence_cutoff_at=created,
                created_at=created, expires_at=NOW + timedelta(seconds=20),
                policy_hash=self.policy.policy_hash, config_hash=self.policy.config_hash,
                evidence_hash="e" * 64, targets=plan.targets,
            ),
            reservation=RiskReservation(
                reservation_id=reservation_id, plan_id=plan_id, account_key=self.policy.account_key,
                planned_risk=plan.planned_downside, stress_risk=plan.stress_downside,
                execution_reserve=execution, notional=Decimal(entry) * quantity, created_at=created,
            ),
            intent=OrderIntent(
                intent_id=str(uuid4()), plan_id=plan_id, reservation_id=reservation_id,
                account_key=self.policy.account_key, kind=IntentKind.ENTRY,
                client_ref=request.client_ref_id, order_tuple=order_tuple,
                tuple_hash=object_hash(order_tuple), created_at=created,
                acknowledgement_deadline_at=NOW + timedelta(seconds=5),
            ),
        )
        return plan

    def assert_blocked(self, code, snapshot, plan):
        with self.assertRaisesRegex(IbkrCommandInputError, code):
            self.check(snapshot, plan, NOW)

    def test_fresh_pnl_drop_cannot_spend_previously_larger_headroom(self):
        plan = self.prepare(quantity=75, entry="6", stop="5")
        self.assertEqual(plan.stress_downside, Decimal("155.75"))
        self.assertIsNone(self.check(self.snapshot("100"), plan, NOW))
        self.assert_blocked("DOLLAR_HEADROOM_EXCEEDED", self.snapshot("0"), plan)

    def test_current_goal_crossing_applies_before_latch_is_persisted(self):
        plan = self.prepare(quantity=20)
        self.assertIsNone(self.check(self.snapshot("149.99"), plan, NOW))
        self.assert_blocked("DOLLAR_HEADROOM_EXCEEDED", self.snapshot("150"), plan)

    def test_latched_floor_counts_existing_and_candidate_fees_exactly_once(self):
        plan = self.prepare()
        other = self.prepare(quantity=4, stop="10", symbol="NEXT")
        total = plan.stress_downside + other.stress_downside
        with self.store.transaction() as connection:
            connection.execute("UPDATE session_latches SET objective_crossed=1")
        self.assertIsNone(self.check(self.snapshot(str(125 + total)), plan, NOW))
        self.assert_blocked("DOLLAR_HEADROOM_EXCEEDED", self.snapshot(str(125 + total - Decimal(".01"))), plan)

    def test_unleveraged_funds_include_all_pending_notional_and_fees(self):
        plan = self.prepare()
        self.prepare(quantity=4, stop="10", symbol="NEXT")
        self.assertIsNone(self.check(self.snapshot(funds="82"), plan, NOW))
        self.assert_blocked("UNLEVERAGED_FUNDS_EXCEEDED", self.snapshot(funds="81.99"), plan)

    def test_unknown_reservation_and_missing_or_stale_evidence_block(self):
        plan = self.prepare()
        other = self.prepare(symbol="NEXT")
        with self.store.transaction() as connection:
            connection.execute("UPDATE order_intents SET state='UNKNOWN' WHERE plan_id=?", (other.plan_id,))
        self.assert_blocked("EXPOSURE_UNRESOLVED", self.snapshot(), plan)
        self.assert_blocked("EVIDENCE_UNPROVEN", self.snapshot(**{
            field: None for field in (
                "risk_baseline_identity_hash", "risk_baseline_receipt_hash",
                "risk_high_water_identity_hash", "risk_high_water_lineage_hash",
                "risk_high_water_receipt_hash",
            )
        }), plan)
        self.assert_blocked("ACCOUNT_RISK_INCOMPLETE", self.snapshot(observed_at=NOW - timedelta(seconds=6)), plan)
        self.assert_blocked("ACCOUNT_RISK_INCOMPLETE", self.snapshot(advanced_orders_complete=False), plan)

    def test_fee_corruption_and_candidate_wrong_namespace_fail_closed(self):
        plan = self.prepare()
        self.assert_blocked("DOLLAR_RISK_FEE_MISMATCH", self.snapshot(), replace(plan, fee_reserve=plan.fee_reserve - 1))
        with self.store.transaction() as connection:
            connection.execute("UPDATE risk_reservations SET account_key='wrong-account'")
        self.assert_blocked("CANDIDATE_EXPOSURE_MISMATCH", self.snapshot(), plan)

    def test_risk_reducing_actions_do_not_require_entry_headroom(self):
        plan = self.prepare()
        stop_plan = IbkrAttendedOrderPlan(
            plan_id=plan.plan_id, purpose=IbkrOrderPurpose.PROTECTION,
            request=plan.required_stop_request, structural_stop=plan.structural_stop,
            targets=(), execution_reserve=plan.execution_reserve, fee_reserve=plan.fee_reserve,
            required_stop_request=None,
        )
        self.assertIsNone(self.check(object(), stop_plan, NOW))


class DollarObservationPersistenceTests(unittest.TestCase):
    snapshot = DollarFinalGateTests.snapshot
    prepare = DollarFinalGateTests.prepare

    def setUp(self):
        self.policy = autonomous_policy()
        DollarFinalGateTests.setUp(self)
        self.path = self.path.resolve()
        self.lock = AccountWriterLock(
            Path(self.temporary.name) / "locks", self.policy.account_key,
            owner_id="test-risk-observer",
            broker_account_binding_fingerprint=ACCOUNT_BINDING,
            authorization_binding_id=AUTHORIZATION_BINDING,
        )
        self.lock.acquire(acquired_at=NOW - timedelta(seconds=15))
        self.addCleanup(self.lock.release)
        generation = self.store.acquire_writer_lease(
            account_key=self.policy.account_key, owner_id=self.lock.owner_id,
            acquired_at=NOW - timedelta(seconds=15),
        )
        self.lock.bind_writer_lease(generation)
        with self.store.transaction() as connection:
            connection.execute("UPDATE runtime_identity SET mode='PAUSED',authority_enabled=0")
        activate_canonical_runtime(
            self.store, created_at=NOW - timedelta(seconds=1), activated_at=NOW,
            expires_at=NOW + timedelta(minutes=3), writer_owner_id=self.lock.owner_id,
            readiness_overrides={
                "execution_authority_mode": "unattended", "attended_mutation_supported": False,
                "unattended_mutation_supported": True, "per_mutation_confirmation_required": False,
                "broker_account_binding_fingerprint": ACCOUNT_BINDING,
                "broker_authorization_binding_id": AUTHORIZATION_BINDING,
            },
        )
        with self.store.transaction() as connection:
            connection.execute("UPDATE runtime_identity SET mode='ACTIVE'")
        self.interlock = AutonomousIbkrWriterInterlock(
            lock=self.lock, state=self.store, state_path=self.path,
            state_identity=(self.path.stat().st_dev, self.path.stat().st_ino),
            policy=self.policy, release_manifest_hash="a" * 64,
            authority_bindings=IbkrAutonomousAuthorityBindings(
                release_manifest_hash="a" * 64, config_hash=self.policy.config_hash,
                policy_binding_id=self.policy.policy_hash, account_key=self.policy.account_key,
                account_masked="****3103", account_binding_fingerprint=ACCOUNT_BINDING,
                authorization_binding_id=AUTHORIZATION_BINDING, provider_contract_id=PROVIDER_CONTRACT,
                transport_id=TRANSPORT, api_name="ibapi", api_version="10.50.2", environment="live", client_id=CLIENT_ID,
            ), clock=lambda: NOW,
        )
        # Receipt verification is hermetic here; production construction tests
        # separately exercise authenticated private receipt loading.
        self.receipt = object.__new__(DurableIbkrAutonomousAcceptanceVerifier)
        receipt_patch = patch.object(DurableIbkrAutonomousAcceptanceVerifier, "verify_policy_receipt", return_value=None)
        self.receipt_check = receipt_patch.start()
        self.addCleanup(receipt_patch.stop)
        self.risk = self.restart_risk()
        self.value = self.auth_snapshot()
        self.reads = 0
        self.requester = FakeContractRequester()
        self.instrument = IbkrInstrumentProvider(
            requester=self.requester, exact_account_id="U0003103", account_masked="****3103",
            contract_factory=SimpleNamespace, timeout_seconds=.1,
            clock=lambda: NOW,
        )
        self.requester.callbacks = self.instrument.open_generation(1)
        self.requester.callbacks.managedAccounts("U0003103")

    def restart_risk(self):
        result = DurableIbkrAutonomousRiskPolicyCheck(
            delegate=DurableIbkrRiskPolicyCheck(state_path=self.path, policy=self.policy),
            receipt_verifier=self.receipt, session_latch_interlock=self.interlock,
        )
        result.bind_entry_risk_activation(lineage_hash="d" * 64, minimum_peak=10000)
        return result

    def reopen_state(self):
        # Reopen the database, rather than relying on a cached latch object.
        previous = self.interlock
        self.store.close()
        self.store = LiveStateStore(self.path)
        self.addCleanup(self.store.close)
        self.interlock = AutonomousIbkrWriterInterlock(
            lock=self.lock, state=self.store, state_path=self.path,
            state_identity=previous.state_identity, policy=self.policy,
            release_manifest_hash=previous.release_manifest_hash,
            authority_bindings=previous.authority_bindings, clock=lambda: NOW,
        )
        self.risk = self.restart_risk()

    def auth_snapshot(self, pnl="0", **overrides):
        return self.snapshot(pnl, risk_evidence_source=(
            "ibkr:reqPnL.realizedPnL:current-day+authenticated-daily-baseline:fixture:"
            + "a" * 64 + ":" + "b" * 64
        ), **overrides)

    def read(self):
        self.reads += 1
        self.assertTrue(self.pending())  # Must commit the marker before reading.
        return self.value

    def pending(self):
        return self.store.rows("SELECT * FROM incidents WHERE category='IBKR_FINAL_RISK_OBSERVATION_UNRESOLVED' AND resolved_at IS NULL")

    def latch(self):
        return self.store.rows("SELECT * FROM session_latches WHERE account_key=?", (self.policy.account_key,))[0]

    def bridge(self, plan):
        return IbkrAttendedPreflightBridge(
            account_snapshot=self.read, instruments=self.instrument,
            plan_reader=lambda _: plan, risk_policy_check=self.risk,
            session_is_entry_eligible=lambda *_: True, account_masked="****3103",
            command_client_id=CLIENT_ID, policy_binding_id=self.policy.policy_hash,
            provider_contract_id=PROVIDER_CONTRACT, account_max_age_seconds=5,
            instrument_max_age_seconds=5, review_ttl_seconds=5,
            existing_order_reserve=Decimal("1"), plan_reader_role="ibkr_autonomous_plan_reader",
            clock=lambda: NOW,
        )

    def observe(self, snapshot, *, entry=True):
        token = self.risk.begin_account_snapshot(NOW, entry=entry)
        self.risk.observe_account_snapshot(snapshot, NOW, token=token, entry=entry)

    def test_loss_denial_recovery_and_restart_cannot_clear_loss(self):
        plan = self.prepare()
        self.value = self.auth_snapshot("-100")
        with self.assertRaisesRegex(BrokerMutationBlocked, "APPROVED_RISK_POLICY_DENIED"):
            self.bridge(plan).review(plan.request)
        self.assertEqual(self.latch()["loss_locked"], 1)
        self.assertFalse(self.pending())
        self.reopen_state()
        self.value = self.auth_snapshot("0")
        with self.assertRaisesRegex(BrokerMutationBlocked, "APPROVED_RISK_POLICY_DENIED"):
            self.bridge(plan).review(plan.request)
        self.assertEqual(self.latch()["loss_locked"], 1)

    def test_goal_persists_before_instrument_denial_and_after_restart(self):
        plan = self.prepare(quantity=20)
        self.value = self.auth_snapshot("150")
        self.requester.match_count = 0
        with self.assertRaisesRegex(BrokerMutationBlocked, "INSTRUMENT_REVALIDATION_FAILED"):
            self.bridge(plan).review(plan.request)
        self.assertEqual(self.latch()["objective_crossed"], 1)
        first = self.latch()["first_objective_crossed_at"]
        self.reopen_state()
        self.requester.match_count = 1
        self.value = self.auth_snapshot("145")
        with self.assertRaisesRegex(BrokerMutationBlocked, "APPROVED_RISK_POLICY_DENIED"):
            self.bridge(plan).review(plan.request)
        self.assertEqual(self.latch()["first_objective_crossed_at"], first)
        self.assertEqual(self.latch()["highest_realized_pnl_cents"], 15000)

    def test_scope_denial_still_persists_crossing(self):
        plan = self.prepare()
        self.value = self.auth_snapshot("150", option_position_count=1)
        with self.assertRaisesRegex(BrokerMutationBlocked, "OPTIONS_EXPOSURE"):
            self.bridge(plan).review(plan.request)
        self.assertEqual(self.latch()["objective_crossed"], 1)
        self.assertFalse(self.pending())

    def test_storage_failure_leaves_marker_across_restart_and_no_auto_clear(self):
        plan = self.prepare()
        self.value = self.auth_snapshot("-100")
        with patch.object(self.store, "apply_session_latch", side_effect=OSError("private")):
            with self.assertRaisesRegex(BrokerMutationBlocked, "OBSERVATION_NOT_COMMITTED"):
                self.bridge(plan).review(plan.request)
        self.assertEqual(len(self.pending()), 1)
        self.reopen_state()
        self.value = self.auth_snapshot("0")
        before = self.reads
        with self.assertRaisesRegex(BrokerMutationBlocked, "OBSERVATION_NOT_ARMED"):
            self.bridge(plan).review(plan.request)
        self.assertEqual(self.reads, before)
        # A subsequent valid risk-reducing observation clears only its own token.
        self.observe(self.value, entry=False)
        self.assertEqual(len(self.pending()), 1)
        with self.assertRaisesRegex(IbkrCommandInputError, "RECOVERY_REQUIRED"):
            self.check(self.value, plan, NOW)

    def test_crash_between_read_and_observer_requires_recovery(self):
        self.risk.begin_account_snapshot(NOW, entry=True)
        self.risk = self.restart_risk()
        with self.assertRaisesRegex(Exception, "OBSERVATION_BEGIN_FAILED"):
            self.risk.begin_account_snapshot(NOW, entry=True)
        self.assertEqual(len(self.pending()), 1)

    def test_unavailable_writer_stops_entry_before_read(self):
        plan = self.prepare()
        self.lock.release()
        with self.assertRaisesRegex(BrokerMutationBlocked, "OBSERVATION_NOT_ARMED"):
            self.bridge(plan).review(plan.request)
        self.assertEqual(self.reads, 0)
        self.assertFalse(self.pending())

    def test_missing_observer_or_marker_write_failure_stops_before_read(self):
        plan = self.prepare()
        self.risk = lambda *_args: None
        with self.assertRaisesRegex(BrokerMutationBlocked, "OBSERVATION_HOOKS_UNAVAILABLE"):
            self.bridge(plan).review(plan.request)
        self.risk = DurableIbkrAutonomousRiskPolicyCheck(delegate=self.check, receipt_verifier=self.receipt)
        with self.assertRaisesRegex(BrokerMutationBlocked, "OBSERVATION_NOT_ARMED"):
            self.bridge(plan).review(plan.request)
        self.risk = self.restart_risk()
        with patch.object(self.store, "record_incident", side_effect=OSError("private storage failure")):
            with self.assertRaisesRegex(BrokerMutationBlocked, "OBSERVATION_NOT_ARMED"):
                self.bridge(plan).review(plan.request)
        self.assertEqual(self.reads, 0)
        self.assertFalse(self.pending())
        self.assertTrue(self.risk._observation_storage_failed)

    def test_receipt_failure_before_and_after_latch_never_clears_marker(self):
        token = self.risk.begin_account_snapshot(NOW, entry=True)
        with patch.object(DurableIbkrAutonomousAcceptanceVerifier, "verify_policy_receipt", side_effect=RuntimeError("private receipt")):
            with self.assertRaisesRegex(Exception, "PERSISTENCE_FAILED"):
                self.risk.observe_account_snapshot(self.auth_snapshot("150"), NOW, token=token, entry=True)
        self.assertEqual(self.latch()["objective_crossed"], 0)
        self.assertTrue(self.pending())
        with patch.object(DurableIbkrAutonomousAcceptanceVerifier, "verify_policy_receipt", side_effect=[None, RuntimeError("expired")]):
            with self.assertRaisesRegex(Exception, "PERSISTENCE_FAILED"):
                self.risk.observe_account_snapshot(self.auth_snapshot("150"), NOW, token=token, entry=True)
        self.assertEqual(self.latch()["objective_crossed"], 1)
        self.assertTrue(self.pending())

    def test_marker_resolution_failure_after_commit_requires_recovery(self):
        with patch.object(self.store, "resolve_incident", side_effect=OSError("private")):
            with self.assertRaisesRegex(Exception, "PERSISTENCE_FAILED"):
                self.observe(self.auth_snapshot("-100"))
        self.assertEqual(self.latch()["loss_locked"], 1)
        self.reopen_state()
        self.assertTrue(self.pending())
        with self.assertRaisesRegex(Exception, "OBSERVATION_BEGIN_FAILED"):
            self.risk.begin_account_snapshot(NOW, entry=True)

    def test_post_response_clock_is_used_for_snapshot_and_final_risk(self):
        plan = self.prepare()
        received = NOW + timedelta(milliseconds=250)
        self.value = self.auth_snapshot("0", observed_at=received, received_at=received, risk_evidence_as_of=received)
        bridge = self.bridge(plan)
        times = iter((NOW, NOW + timedelta(milliseconds=500), NOW + timedelta(seconds=1)))
        bridge._clock = lambda: next(times)
        self.instrument._clock = lambda: NOW + timedelta(milliseconds=750)
        review = bridge.review(plan.request)
        self.assertEqual(review.request, plan.request)
        self.assertFalse(self.pending())
        self.assertEqual(self.latch()["updated_at"], (NOW + timedelta(milliseconds=500)).isoformat())

    def test_session_cutoff_after_read_preserves_crossing_before_denial(self):
        plan = self.prepare()
        started = NOW.replace(hour=19, minute=29, second=59)
        received = started + timedelta(seconds=1)
        self.value = self.auth_snapshot("150", observed_at=received, received_at=received, risk_evidence_as_of=received)
        bridge = self.bridge(plan)
        times = iter((started, received))
        bridge._clock = lambda: next(times)
        with self.assertRaisesRegex(BrokerMutationBlocked, "SESSION_CLOSED"):
            bridge.review(plan.request)
        self.assertEqual(self.latch()["objective_crossed"], 1)
        self.assertFalse(self.pending())

    def test_token_auth_date_and_receipt_failures_cannot_clear_marker(self):
        token = self.risk.begin_account_snapshot(NOW, entry=True)
        for sample in (
            self.auth_snapshot("150", account_masked="****9999"),
            self.auth_snapshot("150", risk_evidence_as_of=NOW - timedelta(days=1)),
            self.auth_snapshot("150", auth_point_in_time=False),
            self.auth_snapshot("150", risk_high_water_lineage_hash="e" * 64,
                               risk_high_water_receipt_hash="f" * 64),
        ):
            with self.assertRaisesRegex(Exception, "PERSISTENCE_FAILED"):
                self.risk.observe_account_snapshot(sample, NOW, token=token, entry=True)
        with self.assertRaisesRegex(Exception, "PERSISTENCE_FAILED"):
            self.risk.observe_account_snapshot(self.auth_snapshot("150"), NOW, token="wrong", entry=True)
        self.assertEqual(self.latch()["objective_crossed"], 0)
        self.assertEqual(len(self.pending()), 1)

    def test_preserves_unrelated_flags_and_retries_concurrent_revision(self):
        prior = self.latch()
        with self.store.transaction() as connection:
            connection.execute("UPDATE session_latches SET pause_new_entries=1,closeout_started=1,hard_kill=1")
        original = self.store.apply_session_latch
        calls = []
        def race(latch):
            calls.append(latch)
            if len(calls) == 1:
                original(replace(latch, highest_realized_pnl=Decimal("200")))
                raise StateConflict("same revision")
            return original(latch)
        with patch.object(self.store, "apply_session_latch", side_effect=race):
            self.observe(self.auth_snapshot("150"))
        row = self.latch()
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(row[key] for key in ("pause_new_entries", "closeout_started", "hard_kill", "objective_crossed")))
        self.assertEqual(row["highest_realized_pnl_cents"], 20000)
        self.assertGreater(row["revision"], prior["revision"])
        self.assertFalse(self.pending())

    def test_false_latch_write_does_not_resolve_marker(self):
        with patch.object(self.store, "apply_session_latch", return_value=False):
            with self.assertRaisesRegex(Exception, "PERSISTENCE_FAILED"):
                self.observe(self.auth_snapshot("150"))
        self.assertTrue(self.pending())

    def test_safety_observer_failure_does_not_deny_protection(self):
        plan = self.prepare()
        bridge = self.bridge(plan)
        self.value = self.auth_snapshot("-100")
        token = self.risk.begin_account_snapshot(NOW, entry=False)
        with patch.object(self.store, "apply_session_latch", side_effect=OSError("unavailable")):
            self.assertIsNone(bridge._snapshot(self.value, NOW, expected_account_masked="****3103", observation_token=token))
        self.assertTrue(self.pending())
        stop = replace(plan, purpose=IbkrOrderPurpose.PROTECTION,
                       request=plan.required_stop_request, required_stop_request=None, targets=())
        self.assertIsNone(self.risk(self.value, stop, NOW))


if __name__ == "__main__":
    unittest.main()
