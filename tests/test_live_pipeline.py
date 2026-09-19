from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from titan_brain.live.broker import (
    FakeBrokerClient,
    FakeFault,
    PositionSnapshot,
)
from titan_brain.live.execution import ExecutionStatus
from titan_brain.live.latency import LatencyRecorder
from titan_brain.live.market_data import (
    CompletedBar,
    MarketDataCache,
    MarketSessionState,
    Quote,
)
from titan_brain.live.massive_adapter import PreparedStructure
from titan_brain.live.models import SessionLatch as DurableSessionLatch
from titan_brain.live.pipeline import (
    FullLiveDiscoveryExecutor,
    FullLiveEntryPipeline,
    InstrumentEvidence,
    LiveValidationEvidence,
    PipelineStatus,
    PipelineThresholds,
    PremarketAnalysisStatus,
    REQUIRED_HARD_GATE_FACTS,
    build_account_risk_snapshot,
)
from titan_brain.live.policy import PolicyBundle, sha256_json
from titan_brain.live.plans import ExpiringPlan
from titan_brain.live.risk_evidence_binding import (
    daily_starting_equity_receipt_hash,
    risk_high_water_receipt_hash,
)
from titan_brain.live.risk_runtime import (
    SessionLatch,
    entry_lifecycle_fee_reserve,
    evaluate_entry,
)
from titan_brain.live.state import LiveStateStore
from titan_brain.scoring import BASELINE_SETUP_WEIGHTS, EQUITY_EXECUTION_WEIGHTS


ROOT = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 8, 10, 0, tzinfo=ET)


class ExplicitTestMutationAuthority:
    def require_mutation_authority(self, **kwargs) -> None:
        return None


def enabled_policy() -> PolicyBundle:
    base = PolicyBundle.load(ROOT)
    config = copy.deepcopy(base.config)
    config["authority"]["live_entries_enabled"] = True
    config["authority"]["blockers"] = []
    config["execution"]["supported_unattended_mutation"] = True
    config["execution"]["per_mutation_user_confirmation_required"] = False
    config["execution"]["local_mutation_interlock_enabled"] = True
    config["exits"] = {
        "target_exit_mode": "first_target_completed_minute_full_exit",
        "target_index": 0,
        "target_trigger": "fresh_aligned_completed_one_minute_close_at_or_above_target",
        "quantity": "full_broker_confirmed_sellable_position",
        "cancel_working_sells_before_exit": True,
        "require_strictly_newer_cancel_evidence": True,
        "deadline_feasibility_gate": True,
    }
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
    result = replace(
        base,
        config=config,
        config_hash=config_hash,
        policy_hash=policy_hash,
    )
    result.validate()
    result.require_activation_ready()
    return result


def policy_with_commission_reserve() -> PolicyBundle:
    base = enabled_policy()
    config = copy.deepcopy(base.config)
    config["execution"][
        "minimum_commission_reserve_per_order_dollars"
    ] = "1.00"
    result = replace(
        base,
        config=config,
        config_hash=sha256_json(config),
    )
    result.validate()
    result.require_activation_ready()
    return result


def premarket_analysis_policy() -> PolicyBundle:
    base = enabled_policy()
    config = copy.deepcopy(base.config)
    config["sessions"].update(
        {
            "premarket_mode": "analysis_only",
            "premarket_orders_enabled": False,
            "premarket_analysis_interval_minutes": 30,
            "regular_entry_start": "09:35",
        }
    )
    config["discovery"].update(
        {
            "score_policy": "ranking_only",
            "minimum_setup_score": None,
            "minimum_execution_score": None,
            "a_plus_setup_score": None,
            "a_plus_execution_score": None,
            "a_plus_enabled": False,
        }
    )
    result = replace(
        base,
        config=config,
        config_hash=sha256_json(config),
    )
    result.validate()
    result.require_activation_ready()
    return result


def structure(
    symbol: str = "XYZ",
    *,
    source_plan_id: str | None = None,
    ranking_score: str = "50",
    setup_id: str = "ORB_BREAKOUT",
) -> PreparedStructure:
    source_id = source_plan_id or f"shadow-{symbol.lower()}"
    payload = {
        "symbol": symbol,
        "setup": setup_id,
        "book_mode": "SHADOW",
        "trade_authority": False,
        "broker_authority": False,
    }
    return PreparedStructure(
        source_plan_id=source_id,
        symbol=symbol,
        observed_at=NOW - timedelta(seconds=2),
        setup_id=setup_id,
        ranking_score=Decimal(ranking_score),
        entry_limit=Decimal("10.05"),
        structural_stop=Decimal("9.50"),
        targets=(Decimal("11.00"), Decimal("12.00")),
        payload_hash=(symbol.lower() * 64)[:64].replace("x", "a").replace("y", "b").replace("z", "c"),
        payload=payload,
    )


def cache_for(*symbols: str) -> MarketDataCache:
    cache = MarketDataCache()
    end = NOW - timedelta(minutes=1)
    for sequence, symbol in enumerate(symbols, start=1):
        cache.record_completed_bar(
            CompletedBar.build(
                symbol=symbol,
                start_at=end - timedelta(minutes=1),
                end_at=end,
                open="9.80",
                high="10.10",
                low="9.75",
                close="10.01",
                volume=800_000,
                sequence=sequence,
                source_event_id=f"massive-bar-{symbol}",
            ),
            received_at=NOW,
        )
        cache.record_quote(
            Quote.build(
                symbol=symbol,
                bid="10.00",
                ask="10.02",
                bid_size=500,
                ask_size=500,
                venue_bid_at=NOW - timedelta(seconds=1),
                venue_ask_at=NOW - timedelta(seconds=1),
                observed_at=NOW - timedelta(seconds=1),
                source="massive+robinhood",
                tradable=True,
            )
        )
    return cache


def premarket_cache_for(as_of: datetime, *symbols: str) -> MarketDataCache:
    cache = MarketDataCache()
    end = as_of - timedelta(minutes=1)
    for sequence, symbol in enumerate(symbols, start=1):
        cache.record_completed_bar(
            CompletedBar.build(
                symbol=symbol,
                start_at=end - timedelta(minutes=1),
                end_at=end,
                open="9.80",
                high="10.10",
                low="9.75",
                close="10.01",
                volume=800_000,
                sequence=sequence,
                source_event_id=f"massive-premarket-bar-{symbol}",
            ),
            received_at=as_of,
        )
        cache.record_quote(
            Quote.build(
                symbol=symbol,
                bid="10.00",
                ask="10.02",
                bid_size=500,
                ask_size=500,
                venue_bid_at=as_of - timedelta(seconds=1),
                venue_ask_at=as_of - timedelta(seconds=1),
                observed_at=as_of - timedelta(seconds=1),
                source="massive_stream_nbbo_top_of_book+ibkr_contract_details",
                tradable=False,
            )
        )
    return cache


def validation_for(
    item: PreparedStructure,
    *,
    setup_score: float = 85,
    execution_score: float = 85,
) -> LiveValidationEvidence:
    return LiveValidationEvidence(
        evidence_id=f"live-validation-{item.symbol}",
        source_plan_id=item.source_plan_id,
        symbol=item.symbol,
        observed_at=NOW - timedelta(seconds=1),
        completed_bar_end=NOW - timedelta(minutes=1),
        entry_limit=item.entry_limit,
        structural_stop=item.structural_stop,
        targets=item.targets,
        execution_reserve_per_share=Decimal("0.05"),
        setup_components={key: setup_score for key in BASELINE_SETUP_WEIGHTS},
        execution_components={
            key: execution_score for key in EQUITY_EXECUTION_WEIGHTS
        },
        hard_gate_facts={key: True for key in REQUIRED_HARD_GATE_FACTS},
        shadow_proposal_grants_authority=False,
    )


class StaticInstrumentProvider:
    def get_instrument_evidence(self, symbol, *, now):
        return InstrumentEvidence(
            evidence_id=f"rh-instrument-evidence-{symbol}",
            symbol=symbol,
            instrument_id=f"rh-instrument-{symbol}",
            observed_at=NOW - timedelta(seconds=1),
            source="robinhood",
            asset_type="stock",
            exchange_listed=True,
            robinhood_tradable=True,
            regular_hours_eligible=True,
        )

    def get_premarket_analysis_evidence(
        self, symbol, *, now, regular_session_open
    ):
        return InstrumentEvidence(
            evidence_id=f"ibkr-analysis-evidence-{symbol}",
            symbol=symbol,
            instrument_id=f"ibkr-contract-{symbol}",
            observed_at=now,
            source="ibkr:tws-contract-details",
            asset_type="stock",
            exchange_listed=True,
            robinhood_tradable=True,
            regular_hours_eligible=True,
            eligibility_at=regular_session_open,
            eligibility_scope="upcoming_regular_session_analysis",
        )


class StaticQualityProvider:
    def __init__(self, records):
        self.records = dict(records)

    def revalidate_structure(self, item, *, now):
        return self.records.get(item.symbol)


class MissingInstrumentProvider:
    def get_instrument_evidence(self, symbol, *, now):
        return None


class MemoryLatencyStore:
    def __init__(self) -> None:
        self.rows: list[tuple[object, ...]] = []

    def record_latency(self, *args) -> None:
        self.rows.append(args)


class StaticPreparedSource:
    def __init__(self, items, *, health_report=None):
        self.items = tuple(items)
        self.calls = []
        self.tradability_results = []
        self.health_report = health_report

    def health(self, *, now):
        self.calls.append("health")
        return self.health_report or type("Health", (), {"blockers": ()})()

    def prepared_structures(self, *, now, limit):
        self.calls.append("prepared_structures")
        return self.items[:limit]

    def hydrate_cache(
        self, cache, *, structures, session_start, now, tradability
    ):
        self.calls.append("hydrate_cache")
        for item in structures:
            eligible = tradability.is_tradable(item.symbol, as_of=now)
            self.tradability_results.append(eligible)
            if item.symbol in cache.quotes:
                cache.quotes[item.symbol] = replace(
                    cache.quotes[item.symbol], tradable=eligible
                )
        return ()


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = LiveStateStore(Path(self.temporary.name) / "live.sqlite3")
        self.policy = enabled_policy()
        self.broker = FakeBrokerClient(clock=lambda: NOW)
        self.authority = ExplicitTestMutationAuthority()
        self.snapshot = replace(
            self.broker.get_account_snapshot("••••7153"),
            account_type="limited_margin",
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def pipeline(
        self,
        items,
        *,
        policy=None,
        cache=None,
        broker=None,
        latency=None,
        thresholds=None,
    ):
        policy = policy or self.policy
        broker = broker or self.broker
        return FullLiveEntryPipeline(
            policy=policy,
            market_data=cache or cache_for(*(item.symbol for item in items)),
            state=self.store,
            broker=broker,
            authority=self.authority,
            instrument_evidence=StaticInstrumentProvider(),
            quality_evidence=StaticQualityProvider(
                {item.symbol: validation_for(item) for item in items}
            ),
            thresholds=thresholds or PipelineThresholds(70, 65, 95, 90),
            clock=lambda: NOW,
            latency=latency,
        )

    def record_current_latch(self) -> None:
        self.store.apply_session_latch(
            DurableSessionLatch(
                account_key="ending-7153",
                trading_date=NOW.date(),
                loss_locked=False,
                objective_crossed=False,
                pause_new_entries=False,
                closeout_started=False,
                hard_kill=False,
                highest_realized_pnl=Decimal("0"),
                first_objective_crossed_at=None,
                revision=0,
                updated_at=NOW,
            )
        )

    def test_synthetic_happy_path_sizes_and_crosses_durable_boundary(self) -> None:
        item = structure()
        result = self.pipeline([item]).run_once(
            structures=[item],
            broker_snapshot=self.snapshot,
            latch=SessionLatch(NOW.date()),
        )
        self.assertEqual(result.status, PipelineStatus.ACKNOWLEDGED)
        selected = result.selected
        self.assertIsNotNone(selected)
        self.assertEqual(selected.execution.status, ExecutionStatus.ACKNOWLEDGED)
        self.assertEqual(selected.quantity, 54)
        self.assertEqual(selected.plan.quality_tier, "normal")
        self.assertGreater(selected.plan.execution_reserve, 0)
        self.assertFalse(selected.plan.allow_add)
        self.assertFalse(selected.plan.allow_reentry)
        self.assertEqual(len(self.store.rows("SELECT * FROM plans")), 1)
        self.assertEqual(len(self.store.rows("SELECT * FROM risk_reservations")), 1)
        self.assertEqual(len(self.store.rows("SELECT * FROM order_intents")), 1)

    def test_active_reservation_fee_is_quantity_and_policy_bound(self) -> None:
        policy = policy_with_commission_reserve()
        item = structure()
        result = self.pipeline([item], policy=policy).run_once(
            structures=[item],
            broker_snapshot=self.snapshot,
            latch=SessionLatch(NOW.date()),
        )
        self.assertEqual(result.status, PipelineStatus.ACKNOWLEDGED)
        assert result.selected is not None

        snapshot, failures = build_account_risk_snapshot(
            policy=policy,
            state=self.store,
            broker_snapshot=self.snapshot,
            now=NOW,
        )
        self.assertEqual(failures, ())
        assert snapshot is not None
        self.assertEqual(len(snapshot.exposures), 1)
        exposure = snapshot.exposures[0]
        self.assertEqual(
            exposure.fee_reserve,
            entry_lifecycle_fee_reserve(
                policy,
                quantity=result.selected.quantity,
            ),
        )
        self.assertEqual(
            exposure.stress_risk,
            exposure.planned_risk
            + exposure.execution_reserve
            + exposure.fee_reserve,
        )

        mismatched_policy = replace(policy, config_hash="0" * 64)
        mismatched, binding_failures = build_account_risk_snapshot(
            policy=mismatched_policy,
            state=self.store,
            broker_snapshot=self.snapshot,
            now=NOW,
        )
        self.assertIsNone(mismatched)
        self.assertIn(
            "ACTIVE_RESERVATION_POLICY_BINDING_MISMATCH",
            binding_failures,
        )

        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE risk_reservations "
                "SET stress_risk_cents=stress_risk_cents+1"
            )
        inconsistent, fee_failures = build_account_risk_snapshot(
            policy=policy,
            state=self.store,
            broker_snapshot=self.snapshot,
            now=NOW,
        )
        self.assertIsNone(inconsistent)
        self.assertIn("ACTIVE_RESERVATION_FEE_BINDING_MISMATCH", fee_failures)

    def _daily_snapshot_with_position(self):
        policy = PolicyBundle.load(
            ROOT, config_relative="config/full_live_ibkr.json"
        )
        observed = NOW.astimezone(timezone.utc)
        masked = f"••••{policy.account_last4}"
        baseline_identity = "1" * 64
        baseline_receipt = "2" * 64
        high_water_identity = "3" * 64
        high_water_lineage = "4" * 64
        high_water_receipt = risk_high_water_receipt_hash(
            identity_hash=high_water_identity,
            baseline_receipt_hash=baseline_receipt,
            lineage_hash=high_water_lineage,
            peak_equity=Decimal("1000"),
        )
        cash_flow_receipt = "5" * 64
        starting_equity_as_of = observed.replace(
            hour=4, minute=0, second=0, microsecond=0
        )
        starting_equity_receipt = daily_starting_equity_receipt_hash(
            baseline_receipt_hash=baseline_receipt,
            cash_flow_receipt_hash=cash_flow_receipt,
            starting_equity=Decimal("1000"),
            external_cash_flow=Decimal("0"),
            starting_equity_as_of=starting_equity_as_of,
            cash_flow_as_of=observed,
            total_equity=self.snapshot.funds.total_value,
        )
        snapshot = replace(
            self.snapshot,
            account_masked=masked,
            account_type="no_borrow_margin",
            equity_positions=(
                PositionSnapshot(
                    symbol="XYZ",
                    quantity=Decimal("2"),
                    sellable_quantity=Decimal("0"),
                    held_for_sells=Decimal("2"),
                    average_price=Decimal("12"),
                ),
            ),
            daily_realized_pnl=Decimal("0"),
            weekly_realized_pnl=Decimal("0"),
            peak_equity=Decimal("1000"),
            daily_realized_pnl_complete=True,
            weekly_realized_pnl_complete=True,
            peak_equity_complete=True,
            risk_evidence_authoritative=True,
            risk_evidence_source="authenticated-test-risk-source",
            risk_evidence_as_of=observed,
            risk_baseline_identity_hash=baseline_identity,
            risk_baseline_receipt_hash=baseline_receipt,
            risk_high_water_identity_hash=high_water_identity,
            risk_high_water_lineage_hash=high_water_lineage,
            risk_high_water_receipt_hash=high_water_receipt,
            daily_starting_equity=Decimal("1000"),
            daily_external_cash_flow=Decimal("0"),
            daily_starting_equity_as_of=starting_equity_as_of,
            daily_external_cash_flow_as_of=observed,
            daily_external_cash_flow_receipt_hash=cash_flow_receipt,
            daily_starting_equity_receipt_hash=starting_equity_receipt,
        )
        return policy, snapshot

    def _record_excluded_entry(
        self, policy, snapshot, intent_state, *, filled=False, filled_quantity=2
    ):
        suffix = intent_state.lower()
        plan_id = f"excluded-{suffix}-plan"
        reservation_id = f"excluded-{suffix}-reservation"
        stamp = snapshot.observed_at.isoformat()
        fee_cents = int(entry_lifecycle_fee_reserve(policy, quantity=2) * 100)
        with self.store.transaction() as connection:
            connection.execute(
                """INSERT INTO plans(
                       plan_id,account_key,strategy_id,symbol,setup_id,quantity,
                       limit_price,structural_stop,market_hours,time_in_force,
                       evidence_cutoff_at,created_at,expires_at,policy_hash,
                       config_hash,evidence_hash,targets_json,state)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    plan_id, policy.account_key, policy.strategy_id,
                    "XYZ", "ORB_BREAKOUT", 2,
                    "12", "11.50", "regular_hours", "gfd", stamp, stamp, stamp,
                    policy.policy_hash, policy.config_hash, "6" * 64, "[]", "ACTIVE",
                ),
            )
            connection.execute(
                """INSERT INTO risk_reservations(
                       reservation_id,plan_id,account_key,planned_risk_cents,
                       stress_risk_cents,execution_reserve_cents,notional_cents,
                       created_at,state) VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    reservation_id, plan_id, policy.account_key,
                    100, 110 + fee_cents, 10, 2400, stamp, "BOUND",
                ),
            )
            connection.execute(
                """INSERT INTO order_intents(
                       intent_id,plan_id,reservation_id,account_key,kind,client_ref,
                       order_tuple_json,tuple_hash,created_at,
                       acknowledgement_deadline_at,state,updated_at)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    f"excluded-{suffix}-intent", plan_id, reservation_id,
                    policy.account_key, "ENTRY",
                    f"00000000-0000-4000-8000-00000000010{1 if intent_state == 'UNKNOWN' else 2}",
                    "{}", "7" * 64, stamp, stamp, intent_state, stamp,
                ),
            )
            if filled:
                connection.execute(
                    "INSERT INTO broker_orders VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        f"excluded-{suffix}-order", f"excluded-{suffix}-intent",
                        policy.account_key,
                        "FILLED" if filled_quantity == 2 else "PARTIALLY_FILLED",
                        2, filled_quantity, 1, stamp, stamp,
                        "8" * 64,
                    ),
                )
                connection.execute(
                    "INSERT INTO fills VALUES (?,?,?,?,?,?,?)",
                    (
                        f"excluded-{suffix}-fill", f"excluded-{suffix}-order",
                        policy.account_key, filled_quantity, "12", stamp, stamp,
                    ),
                )
        return plan_id

    def test_actual_position_blocks_unknown_or_submitting_excluded_replay(self) -> None:
        policy, snapshot = self._daily_snapshot_with_position()
        for intent_state in ("UNKNOWN", "SUBMITTING"):
            with self.subTest(intent_state=intent_state):
                plan_id = self._record_excluded_entry(policy, snapshot, intent_state)
                risk, failures = build_account_risk_snapshot(
                    policy=policy,
                    state=self.store,
                    broker_snapshot=snapshot,
                    now=NOW,
                    exclude_plan_id=plan_id,
                )

                self.assertIsNone(risk)
                self.assertIn("OPEN_POSITION_SOURCE_VALUATION_TIME_UNAVAILABLE", failures)
                self.assertIn("OPEN_POSITION_COMMON_NLV_EPOCH_UNAVAILABLE", failures)
                self.assertIn("OPEN_POSITION_REMAINING_FEE_BOUND_UNAVAILABLE", failures)
                self.assertIn("DAILY_EQUITY_OPEN_RISK_REVALUATION_REQUIRED", failures)

    def test_flat_snapshot_cannot_exclude_filled_durable_reservation(self) -> None:
        self._assert_flat_snapshot_cannot_exclude_filled_reservation("ACKNOWLEDGED")

    def test_flat_snapshot_cannot_exclude_unknown_filled_reservation(self) -> None:
        self._assert_flat_snapshot_cannot_exclude_filled_reservation("UNKNOWN")

    def test_flat_snapshot_cannot_exclude_submitting_filled_reservation(self) -> None:
        self._assert_flat_snapshot_cannot_exclude_filled_reservation("SUBMITTING")

    def _assert_flat_snapshot_cannot_exclude_filled_reservation(self, intent_state):
        policy, snapshot = self._daily_snapshot_with_position()
        snapshot = replace(snapshot, equity_positions=())
        plan_id = self._record_excluded_entry(
            policy, snapshot, intent_state, filled=True
        )
        risk, failures = build_account_risk_snapshot(
            policy=policy,
            state=self.store,
            broker_snapshot=snapshot,
            now=NOW,
            exclude_plan_id=plan_id,
        )

        self.assertIsNone(risk)
        self.assertIn("DAILY_EQUITY_OPEN_RISK_REVALUATION_REQUIRED", failures)

    def test_legacy_protected_partial_fill_preserves_unknown_replay_blocker(self) -> None:
        self.assertFalse(self.policy.daily_starting_equity_risk)
        plan_id = self._record_excluded_entry(
            self.policy, self.snapshot, "UNKNOWN", filled=True, filled_quantity=1
        )
        stamp = self.snapshot.observed_at.isoformat()
        with self.store.transaction() as connection:
            connection.execute(
                """INSERT INTO order_intents
                   SELECT 'partial-stop-intent',plan_id,NULL,account_key,'PROTECTION',
                          'partial-stop-ref',order_tuple_json,tuple_hash,created_at,
                          acknowledgement_deadline_at,'ACKNOWLEDGED',updated_at
                     FROM order_intents WHERE intent_id='excluded-unknown-intent'"""
            )
            connection.execute(
                "INSERT INTO broker_orders VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    "partial-stop", "partial-stop-intent", self.policy.account_key,
                    "CONFIRMED", 1, 0, 1, stamp, stamp, "9" * 64,
                ),
            )
            connection.execute(
                "INSERT INTO protection_obligations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "partial-protection", "excluded-unknown-fill",
                    self.policy.account_key, "XYZ", 1, 1, "11.50", "WORKING",
                    1, stamp, "partial-stop",
                ),
            )
        snapshot = replace(
            self.snapshot,
            equity_positions=(
                PositionSnapshot(
                    symbol="XYZ", quantity=Decimal("1"),
                    sellable_quantity=Decimal("0"), held_for_sells=Decimal("1"),
                    average_price=Decimal("12"),
                ),
            ),
        )
        plan = ExpiringPlan.build(
            strategy_id=self.policy.strategy_id,
            policy_hash=self.policy.policy_hash,
            config_hash=self.policy.config_hash,
            account_last4=self.policy.account_last4,
            symbol="NEXT", instrument_id="instrument-next", setup_id="ORB_BREAKOUT",
            quantity=2, entry_limit="12", structural_stop="11.50", targets=["13"],
            execution_reserve_per_share="0.05", market_hours="regular_hours",
            time_in_force="gfd", quality_tier="normal",
            completed_bar_end=NOW - timedelta(minutes=1),
            quote_observed_at=NOW - timedelta(seconds=1),
            created_at=NOW - timedelta(seconds=1),
            expires_at=NOW + timedelta(seconds=20), source_event_ids=["bar", "quote"],
        )
        for excluded in (None, plan_id):
            with self.subTest(exclude_plan_id=excluded):
                risk, failures = build_account_risk_snapshot(
                    policy=self.policy, state=self.store, broker_snapshot=snapshot,
                    now=NOW, exclude_plan_id=excluded,
                )
                self.assertEqual(failures, ())
                self.assertIsNotNone(risk)
                assert risk is not None
                self.assertEqual(len(risk.exposures), 1)
                self.assertEqual(risk.exposures[0].category, "unknown")
                decision = evaluate_entry(
                    policy=self.policy, snapshot=risk, plan=plan,
                    latch=SessionLatch(NOW.date()), now=NOW,
                )
                self.assertFalse(decision.allowed)
                self.assertIn("UNKNOWN_POSSIBLE_EXPOSURE", decision.failures)

    def test_pipeline_records_distinct_compute_risk_durable_and_ack_stages(self) -> None:
        item = structure()
        metrics = MemoryLatencyStore()
        ticks = iter(
            (
                0,
                1_000_000,
                2_000_000,
                5_000_000,
                6_000_000,
                10_000_000,
                11_000_000,
                16_000_000,
            )
        )
        result = self.pipeline(
            [item],
            latency=LatencyRecorder(metrics, clock_ns=lambda: next(ticks)),
        ).run_once(
            structures=[item],
            broker_snapshot=self.snapshot,
            latch=SessionLatch(NOW.date()),
        )
        self.assertEqual(result.status, PipelineStatus.ACKNOWLEDGED)
        self.assertEqual(
            [row[0] for row in metrics.rows],
            [
                "signal_compute",
                "preflight_risk",
                "durable_intent_write",
                "submit_to_ack",
            ],
        )
        self.assertNotIn("ack_to_fill", {row[0] for row in metrics.rows})
        self.assertNotIn(
            "fill_to_working_protection", {row[0] for row in metrics.rows}
        )

    def test_quality_ranking_beats_raw_shadow_rank_and_only_one_is_attempted(self) -> None:
        raw_leader = structure("ABC", ranking_score="99")
        quality_leader = structure("XYZ", ranking_score="1")
        cache = cache_for("ABC", "XYZ")
        provider = StaticQualityProvider(
            {
                "ABC": validation_for(raw_leader, setup_score=72, execution_score=68),
                "XYZ": validation_for(quality_leader, setup_score=92, execution_score=91),
            }
        )
        pipeline = FullLiveEntryPipeline(
            policy=self.policy,
            market_data=cache,
            state=self.store,
            broker=self.broker,
            authority=self.authority,
            instrument_evidence=StaticInstrumentProvider(),
            quality_evidence=provider,
            thresholds=PipelineThresholds(70, 65, 95, 90),
            clock=lambda: NOW,
        )
        result = pipeline.run_once(
            structures=[raw_leader, quality_leader],
            broker_snapshot=self.snapshot,
            latch=SessionLatch(NOW.date()),
        )
        self.assertEqual(result.selected.symbol, "XYZ")
        self.assertEqual(result.candidates[0].symbol, "XYZ")
        self.assertEqual(result.candidates[1].status, PipelineStatus.NOT_SELECTED)
        self.assertEqual(
            sum(call[0] == FakeBrokerClient.PLACE for call in self.broker.calls), 1
        )

    def test_checked_in_current_config_blocks_before_any_broker_order_call(self) -> None:
        current = PolicyBundle.load(ROOT)
        item = structure()
        broker = FakeBrokerClient(clock=lambda: NOW)
        snapshot = replace(
            broker.get_account_snapshot("••••7153"),
            account_type="limited_margin",
        )
        result = self.pipeline([item], policy=current, broker=broker).run_once(
            structures=[item],
            broker_snapshot=snapshot,
            latch=SessionLatch(NOW.date()),
        )
        self.assertEqual(result.status, PipelineStatus.BLOCKED)
        self.assertIn(
            "LIVE_ENTRIES_DISABLED_IN_SIGNED_CONFIG",
            result.candidates[0].failures,
        )
        self.assertFalse(
            any(
                call[0] in {FakeBrokerClient.REVIEW, FakeBrokerClient.PLACE}
                for call in broker.calls
            )
        )

    def test_lifecycle_executor_contract_and_missing_tradability_fail_closed(self) -> None:
        item = structure()
        self.record_current_latch()
        pipeline = self.pipeline([item])
        source = StaticPreparedSource([item])
        executor = FullLiveDiscoveryExecutor(source=source, pipeline=pipeline)
        actions = executor.execute(snapshot=self.snapshot, now=NOW)
        self.assertTrue(actions[0].startswith("ENTRY:ACKNOWLEDGED:"), actions)
        self.assertEqual(
            source.calls, ["health", "prepared_structures", "hydrate_cache"]
        )
        self.assertEqual(source.tradability_results, [True])
        self.assertEqual(
            PipelineThresholds.from_policy(self.policy),
            PipelineThresholds(70, 65, 95, 90),
        )

        with tempfile.TemporaryDirectory() as temp:
            store = LiveStateStore(Path(temp) / "state.sqlite3")
            try:
                store.apply_session_latch(
                    DurableSessionLatch(
                        account_key="ending-7153",
                        trading_date=NOW.date(),
                        loss_locked=False,
                        objective_crossed=False,
                        pause_new_entries=False,
                        closeout_started=False,
                        hard_kill=False,
                        highest_realized_pnl=Decimal("0"),
                        first_objective_crossed_at=None,
                        revision=0,
                        updated_at=NOW,
                    )
                )
                broker = FakeBrokerClient(clock=lambda: NOW)
                snapshot = replace(
                    broker.get_account_snapshot("••••7153"),
                    account_type="limited_margin",
                )
                missing = FullLiveEntryPipeline(
                    policy=self.policy,
                    market_data=cache_for("XYZ"),
                    state=store,
                    broker=broker,
                    authority=self.authority,
                    instrument_evidence=MissingInstrumentProvider(),
                    quality_evidence=StaticQualityProvider(
                        {"XYZ": validation_for(item)}
                    ),
                    thresholds=PipelineThresholds.from_policy(self.policy),
                    clock=lambda: NOW,
                )
                missing_source = StaticPreparedSource([item])
                blocked = FullLiveDiscoveryExecutor(
                    source=missing_source,
                    pipeline=missing,
                ).execute(snapshot=snapshot, now=NOW)
                self.assertIn("INSTRUMENT_EVIDENCE_MISSING", blocked[0])
                self.assertEqual(missing_source.tradability_results, [False])
                self.assertFalse(
                    any(
                        call[0] in {FakeBrokerClient.REVIEW, FakeBrokerClient.PLACE}
                        for call in broker.calls
                    )
                )
            finally:
                store.close()

    def test_lifecycle_executor_reports_closed_market_as_waiting_without_scanning(self) -> None:
        item = structure()
        self.record_current_latch()
        health = type(
            "Health",
            (),
            {
                "blockers": (),
                "service_healthy": True,
                "session_state": MarketSessionState.WAITING_FOR_SESSION,
                "entry_evidence_ready": False,
                "entry_blockers": ("WAITING_FOR_SESSION",),
            },
        )()
        source = StaticPreparedSource([item], health_report=health)
        actions = FullLiveDiscoveryExecutor(
            source=source,
            pipeline=self.pipeline([item]),
        ).execute(snapshot=self.snapshot, now=NOW)
        self.assertEqual(actions, ("DISCOVERY:WAITING_FOR_SESSION",))
        self.assertEqual(source.calls, ["health"])
        self.assertFalse(
            any(
                call[0] in {FakeBrokerClient.REVIEW, FakeBrokerClient.PLACE}
                for call in self.broker.calls
            )
        )

    def test_entry_health_blocker_bootstraps_read_only_hydration_without_entry(self) -> None:
        item = structure()
        self.record_current_latch()
        health = type(
            "Health",
            (),
            {
                "blockers": (),
                "service_healthy": True,
                "session_state": MarketSessionState.ENTRY_ELIGIBLE,
                "entry_evidence_ready": False,
                "entry_blockers": ("MASSIVE_REST_HEALTH_PENDING",),
            },
        )()
        source = StaticPreparedSource([item], health_report=health)
        actions = FullLiveDiscoveryExecutor(
            source=source,
            pipeline=self.pipeline([item]),
        ).execute(snapshot=self.snapshot, now=NOW)
        self.assertIn("MASSIVE_REST_HEALTH_PENDING", actions[0])
        self.assertEqual(
            source.calls, ["health", "prepared_structures", "hydrate_cache"]
        )
        self.assertFalse(
            any(
                call[0] in {FakeBrokerClient.REVIEW, FakeBrokerClient.PLACE}
                for call in self.broker.calls
            )
        )

    def test_lifecycle_executor_short_circuits_checked_in_blocked_config(self) -> None:
        current = PolicyBundle.load(ROOT)
        item = structure()
        broker = FakeBrokerClient(clock=lambda: NOW)
        snapshot = replace(
            broker.get_account_snapshot("••••7153"),
            account_type="limited_margin",
        )
        pipeline = FullLiveEntryPipeline(
            policy=current,
            market_data=cache_for("XYZ"),
            state=self.store,
            broker=broker,
            authority=self.authority,
            instrument_evidence=MissingInstrumentProvider(),
            quality_evidence=StaticQualityProvider({}),
            thresholds=PipelineThresholds(70, 65, 95, 90),
            clock=lambda: NOW,
        )
        source = StaticPreparedSource([item])
        actions = FullLiveDiscoveryExecutor(
            source=source,
            pipeline=pipeline,
        ).execute(snapshot=snapshot, now=NOW)
        self.assertIn("LIVE_ENTRIES_DISABLED_IN_SIGNED_CONFIG", actions[0])
        self.assertEqual(source.calls, [])
        self.assertFalse(
            any(
                call[0] in {FakeBrokerClient.REVIEW, FakeBrokerClient.PLACE}
                for call in broker.calls
            )
        )

    def test_raw_market_facts_override_claimed_hard_gate_success(self) -> None:
        scenarios = {}
        stale = cache_for("XYZ")
        stale.quotes["XYZ"] = replace(
            stale.quotes["XYZ"],
            venue_bid_at=NOW - timedelta(seconds=10),
            venue_ask_at=NOW - timedelta(seconds=10),
        )
        scenarios["stale"] = (stale, "QUOTE_STALE")
        missing = cache_for("XYZ")
        missing.bars["XYZ"].clear()
        scenarios["missing"] = (missing, "CAUSAL_COMPLETED_BAR_MISSING")
        crossed = cache_for("XYZ")
        crossed.quotes["XYZ"] = replace(
            crossed.quotes["XYZ"], bid=Decimal("10.03"), ask=Decimal("10.02")
        )
        scenarios["crossed"] = (crossed, "CROSSED_QUOTE")
        degraded = cache_for("XYZ")
        degraded.degraded["XYZ"] = "MARKET_DATA_SEQUENCE_GAP"
        scenarios["data_loss"] = (degraded, "MARKET_DATA_SEQUENCE_GAP")

        for label, (cache, code) in scenarios.items():
            with self.subTest(label=label):
                with tempfile.TemporaryDirectory() as temp:
                    store = LiveStateStore(Path(temp) / "state.sqlite3")
                    try:
                        item = structure()
                        pipeline = FullLiveEntryPipeline(
                            policy=self.policy,
                            market_data=cache,
                            state=store,
                            broker=self.broker,
                            authority=self.authority,
                            instrument_evidence=StaticInstrumentProvider(),
                            quality_evidence=StaticQualityProvider(
                                {"XYZ": validation_for(item)}
                            ),
                            thresholds=PipelineThresholds(70, 65, 95, 90),
                            clock=lambda: NOW,
                        )
                        result = pipeline.run_once(
                            structures=[item],
                            broker_snapshot=self.snapshot,
                            latch=SessionLatch(NOW.date()),
                        )
                        self.assertIn(code, result.candidates[0].failures)
                        self.assertIsNone(result.attempted_plan_id)
                    finally:
                        store.close()

    def test_missing_score_or_hard_gate_component_blocks_instead_of_defaulting(self) -> None:
        item = structure()
        incomplete = validation_for(item)
        incomplete = replace(
            incomplete,
            setup_components={
                key: value
                for key, value in incomplete.setup_components.items()
                if key != "catalyst_context"
            },
            hard_gate_facts={
                key: value
                for key, value in incomplete.hard_gate_facts.items()
                if key != "acceptable_extension"
            },
        )
        pipeline = FullLiveEntryPipeline(
            policy=self.policy,
            market_data=cache_for("XYZ"),
            state=self.store,
            broker=self.broker,
            authority=self.authority,
            instrument_evidence=StaticInstrumentProvider(),
            quality_evidence=StaticQualityProvider({"XYZ": incomplete}),
            thresholds=PipelineThresholds(70, 65, 95, 90),
            clock=lambda: NOW,
        )
        result = pipeline.run_once(
            structures=[item],
            broker_snapshot=self.snapshot,
            latch=SessionLatch(NOW.date()),
        )
        failures = result.candidates[0].failures
        self.assertIn("SETUP_SCORE_COMPONENT_SET_MISMATCH", failures)
        self.assertTrue(any(code.startswith("HARD_GATE_FACTS_INCOMPLETE") for code in failures))
        self.assertIsNone(result.attempted_plan_id)

    def test_manual_open_and_unknown_exposure_are_all_fail_closed(self) -> None:
        item = structure()
        manual = replace(
            self.snapshot,
            equity_positions=(
                PositionSnapshot(
                    symbol="MANU",
                    quantity=Decimal("2"),
                    sellable_quantity=Decimal("2"),
                    average_price=Decimal("20"),
                ),
            ),
        )
        manual_risk, failures = build_account_risk_snapshot(
            policy=self.policy,
            state=self.store,
            broker_snapshot=manual,
            now=NOW,
        )
        self.assertFalse(failures)
        self.assertEqual(manual_risk.exposures[0].category, "manual")
        result = self.pipeline([item]).run_once(
            structures=[item],
            broker_snapshot=manual,
            latch=SessionLatch(NOW.date()),
        )
        self.assertIn("UNPROTECTED_OPEN_EXPOSURE", result.candidates[0].failures)

        # The same classification remains explicit for locally owned risk: a
        # durable reservation plus a broker position is ``open`` and cannot be
        # treated as merely pending.  Missing per-fill protection keeps it
        # fail-closed.
        opened = self.pipeline([item]).run_once(
            structures=[item],
            broker_snapshot=self.snapshot,
            latch=SessionLatch(NOW.date()),
        )
        self.assertEqual(opened.selected.execution.status, ExecutionStatus.ACKNOWLEDGED)
        locally_open = replace(
            self.snapshot,
            equity_positions=(
                PositionSnapshot(
                    symbol="XYZ",
                    quantity=Decimal(opened.selected.quantity),
                    sellable_quantity=Decimal(opened.selected.quantity),
                    average_price=Decimal("10.02"),
                ),
            ),
        )
        open_risk, open_failures = build_account_risk_snapshot(
            policy=self.policy,
            state=self.store,
            broker_snapshot=locally_open,
            now=NOW,
        )
        self.assertFalse(open_failures)
        self.assertIn("open", {exposure.category for exposure in open_risk.exposures})
        self.assertFalse(next(exposure for exposure in open_risk.exposures if exposure.category == "open").protected)

        # Create one durable UNKNOWN entry.  It remains reserved and blocks a
        # different symbol even when the supplied broker envelope has not yet
        # discovered the lost acknowledgement.
        with tempfile.TemporaryDirectory() as unknown_temp:
            unknown_store = LiveStateStore(Path(unknown_temp) / "state.sqlite3")
            try:
                unknown_broker = FakeBrokerClient(clock=lambda: NOW)
                unknown_broker.inject_fault(
                    FakeBrokerClient.PLACE, FakeFault.PLACE_UNKNOWN_AFTER_ACCEPT
                )
                unknown_snapshot = replace(
                    unknown_broker.get_account_snapshot("••••7153"),
                    account_type="limited_margin",
                )
                unknown_pipeline = FullLiveEntryPipeline(
                    policy=self.policy,
                    market_data=cache_for("XYZ"),
                    state=unknown_store,
                    broker=unknown_broker,
                    authority=self.authority,
                    instrument_evidence=StaticInstrumentProvider(),
                    quality_evidence=StaticQualityProvider(
                        {"XYZ": validation_for(item)}
                    ),
                    thresholds=PipelineThresholds(70, 65, 95, 90),
                    clock=lambda: NOW,
                )
                first = unknown_pipeline.run_once(
                    structures=[item],
                    broker_snapshot=unknown_snapshot,
                    latch=SessionLatch(NOW.date()),
                )
                self.assertEqual(first.selected.execution.status, ExecutionStatus.UNKNOWN)
                other = structure("ABC")
                second_pipeline = FullLiveEntryPipeline(
                    policy=self.policy,
                    market_data=cache_for("ABC"),
                    state=unknown_store,
                    broker=unknown_broker,
                    authority=self.authority,
                    instrument_evidence=StaticInstrumentProvider(),
                    quality_evidence=StaticQualityProvider(
                        {"ABC": validation_for(other)}
                    ),
                    thresholds=PipelineThresholds(70, 65, 95, 90),
                    clock=lambda: NOW,
                )
                second = second_pipeline.run_once(
                    structures=[other],
                    broker_snapshot=unknown_snapshot,
                    latch=SessionLatch(NOW.date()),
                )
                self.assertIn("UNKNOWN_POSSIBLE_EXPOSURE", second.candidates[0].failures)
                risk_snapshot, risk_failures = build_account_risk_snapshot(
                    policy=self.policy,
                    state=unknown_store,
                    broker_snapshot=unknown_snapshot,
                    now=NOW,
                )
                self.assertFalse(risk_failures)
                self.assertIn(
                    "unknown", {exposure.category for exposure in risk_snapshot.exposures}
                )
            finally:
                unknown_store.close()

    def test_empty_input_is_true_no_trade_without_padding(self) -> None:
        pipeline = FullLiveEntryPipeline(
            policy=self.policy,
            market_data=MarketDataCache(),
            state=self.store,
            broker=self.broker,
            authority=self.authority,
            instrument_evidence=StaticInstrumentProvider(),
            quality_evidence=StaticQualityProvider({}),
            thresholds=PipelineThresholds(70, 65, 95, 90),
            clock=lambda: NOW,
        )
        result = pipeline.run_once(
            structures=[],
            broker_snapshot=self.snapshot,
            latch=SessionLatch(NOW.date()),
        )
        self.assertEqual(result.status, PipelineStatus.NO_TRADE)
        self.assertEqual(result.candidates, ())

    def test_plan_expiry_boundary_is_deterministic_and_fail_closed(self) -> None:
        # Area 6 pin: a produced plan expires at exactly created_at + plan_ttl,
        # and plan.validate is an exact, fail-closed boundary (tolerates the
        # boundary instant, rejects one tick past). No candidate is fabricated,
        # Massive stays the data source, and no readiness gate is moved.
        item = structure()
        pipeline = self.pipeline([item])
        result = pipeline.run_once(
            structures=[item],
            broker_snapshot=self.snapshot,
            latch=SessionLatch(NOW.date()),
        )
        self.assertEqual(result.status, PipelineStatus.ACKNOWLEDGED)
        assert result.selected is not None
        plan = result.selected.plan
        ttl = int(self.policy.config["evidence"]["plan_ttl_seconds"])
        # Deterministic expiry: expires_at == created_at + ttl, exactly.
        self.assertEqual(plan.expires_at, plan.created_at + timedelta(seconds=ttl))
        self.assertGreater(plan.expires_at, plan.created_at)

        # plan.validate boundary (plans.py: `now > expires_at`): the exact
        # expiry instant is still valid; one microsecond past fails closed.
        plan.validate(self.policy, plan.expires_at)
        with self.assertRaises((TypeError, ValueError)):
            plan.validate(self.policy, plan.expires_at + timedelta(microseconds=1))
        # A created_at-relative sanity anchor: well before expiry stays valid.
        plan.validate(self.policy, plan.created_at + timedelta(seconds=1))

    def test_exact_evidence_replay_never_calls_broker_twice(self) -> None:
        item = structure()
        pipeline = self.pipeline([item])
        first = pipeline.run_once(
            structures=[item],
            broker_snapshot=self.snapshot,
            latch=SessionLatch(NOW.date()),
        )
        calls = len(self.broker.calls)
        second = pipeline.run_once(
            structures=[item],
            broker_snapshot=self.snapshot,
            latch=SessionLatch(NOW.date()),
        )
        self.assertEqual(first.attempted_plan_id, second.attempted_plan_id)
        self.assertTrue(second.selected.execution.replay)
        self.assertEqual(len(self.broker.calls), calls)

    def test_premarket_remains_attended_only(self) -> None:
        premarket = NOW.replace(hour=8)
        item = replace(
            structure(),
            observed_at=premarket - timedelta(seconds=2),
        )
        cache = cache_for("XYZ")
        result = self.pipeline([item], cache=cache).run_once(
            structures=[item],
            broker_snapshot=self.snapshot,
            latch=SessionLatch(premarket.date()),
            now=premarket,
        )
        self.assertIn("REGULAR_ENTRY_LANE_CLOSED", result.candidates[0].failures)
        self.assertIsNone(result.attempted_plan_id)

    def test_premarket_analysis_schedule_exact_boundaries_and_dedup_contract(self) -> None:
        policy = premarket_analysis_policy()
        at_open = NOW.replace(hour=7, minute=0, second=0, microsecond=0)
        item = replace(structure(), observed_at=at_open)
        executor = FullLiveDiscoveryExecutor(
            source=StaticPreparedSource([item]),
            pipeline=self.pipeline(
                [item],
                policy=policy,
                cache=premarket_cache_for(at_open, "XYZ"),
                thresholds=PipelineThresholds.from_policy(policy),
            ),
        )

        first = executor.premarket_analysis_due(now=at_open)
        self.assertTrue(first.due)
        self.assertEqual(first.scheduled_for, at_open.astimezone(ZoneInfo("UTC")))
        self.assertEqual(
            first.next_due_at,
            (at_open + timedelta(minutes=30)).astimezone(ZoneInfo("UTC")),
        )
        at_next = at_open + timedelta(minutes=30)
        next_slot = executor.premarket_analysis_due(
            now=at_next,
            last_completed_slot=first.scheduled_for,
        )
        self.assertTrue(next_slot.due)
        self.assertEqual(next_slot.scheduled_for, at_next.astimezone(ZoneInfo("UTC")))
        duplicate = executor.premarket_analysis_due(
            now=at_next + timedelta(minutes=29, seconds=59),
            last_completed_slot=next_slot.scheduled_for,
        )
        self.assertFalse(duplicate.due)
        self.assertEqual(duplicate.reason, "PREMARKET_SLOT_COMPLETE")
        invalid = executor.premarket_analysis_due(
            now=at_next,
            last_completed_slot=at_open + timedelta(minutes=1),
        )
        self.assertFalse(invalid.due)
        self.assertEqual(
            invalid.reason, "PREMARKET_LAST_COMPLETED_SLOT_INVALID"
        )

    def test_premarket_analysis_is_deterministic_read_only_and_never_approves(self) -> None:
        policy = premarket_analysis_policy()
        premarket = NOW.replace(hour=8, minute=0)
        item = replace(
            structure(),
            observed_at=premarket - timedelta(seconds=2),
            targets=(Decimal("11.20"), Decimal("12.00")),
            payload={
                **structure().payload,
                "relative_volume_score": 80,
                "catalyst_context_score": 70,
                "sector_market_sympathy_score": 60,
                "prior_90_day_behavior_score": 50,
                "gap_behavior_score": 75,
            },
        )
        source = StaticPreparedSource([item])
        executor = FullLiveDiscoveryExecutor(
            source=source,
            pipeline=self.pipeline(
                [item],
                policy=policy,
                cache=premarket_cache_for(premarket, "XYZ"),
                thresholds=PipelineThresholds.from_policy(policy),
            ),
        )
        before = tuple(self.broker.calls)
        first = executor.analyze(now=premarket)
        second = executor.analyze(now=premarket)

        self.assertEqual(first.status, PremarketAnalysisStatus.COMPLETED)
        self.assertEqual(first.analysis_id, second.analysis_id)
        self.assertFalse(first.execution_authority)
        self.assertFalse(first.approved_to_buy)
        self.assertIn("no candidate is approved to buy", first.message)
        self.assertEqual(len(first.candidates), 1)
        candidate = first.candidates[0]
        self.assertEqual(candidate.rank, 1)
        self.assertEqual(candidate.instrument_source, "ibkr:tws-contract-details")
        self.assertEqual(
            candidate.regular_session_eligibility_at,
            NOW.replace(hour=9, minute=30).astimezone(ZoneInfo("UTC")),
        )
        self.assertFalse(candidate.execution_authority)
        self.assertFalse(candidate.approved_to_buy)
        self.assertNotIn("SETUP_SCORE_BELOW_MINIMUM", candidate.hard_gate_failures)
        self.assertNotIn(
            "EXECUTION_SCORE_BELOW_MINIMUM", candidate.hard_gate_failures
        )
        self.assertIn(
            "ACCOUNT_CAPACITY_NOT_EVALUATED", candidate.deferred_execution_gates
        )
        self.assertEqual(tuple(self.broker.calls), before)
        self.assertEqual(self.store.rows("SELECT * FROM plans"), [])
        self.assertEqual(self.store.rows("SELECT * FROM order_intents"), [])
        self.assertEqual(self.store.rows("SELECT * FROM risk_reservations"), [])
        self.assertIs(
            executor._premarket_analysis.market_data, executor.market_data
        )
        self.assertIs(
            executor._premarket_analysis.tradability, executor._tradability
        )

    def test_premarket_analysis_never_reads_candidates_outside_lane(self) -> None:
        policy = premarket_analysis_policy()
        item = structure()
        source = StaticPreparedSource([item])
        executor = FullLiveDiscoveryExecutor(
            source=source,
            pipeline=self.pipeline(
                [item],
                policy=policy,
                thresholds=PipelineThresholds.from_policy(policy),
            ),
        )
        result = executor.analyze(now=NOW.replace(hour=9, minute=25))
        self.assertEqual(result.status, PremarketAnalysisStatus.OUTSIDE_LANE)
        self.assertEqual(source.calls, [])
        self.assertFalse(result.execution_authority)

    def test_ranking_only_removes_only_score_floors_and_keeps_hard_gates(self) -> None:
        policy = premarket_analysis_policy()
        item = structure()
        evidence = validation_for(item, setup_score=1, execution_score=1)
        evidence = replace(
            evidence,
            hard_gate_facts={
                **evidence.hard_gate_facts,
                "acceptable_extension": False,
            },
        )
        pipeline = FullLiveEntryPipeline(
            policy=policy,
            market_data=cache_for("XYZ"),
            state=self.store,
            broker=self.broker,
            authority=self.authority,
            instrument_evidence=StaticInstrumentProvider(),
            quality_evidence=StaticQualityProvider({"XYZ": evidence}),
            thresholds=PipelineThresholds.from_policy(policy),
            clock=lambda: NOW,
        )
        scored = pipeline._score_candidate(item, NOW)
        self.assertNotIn("SETUP_SCORE_BELOW_MINIMUM", scored.failures)
        self.assertNotIn("EXECUTION_SCORE_BELOW_MINIMUM", scored.failures)
        self.assertIn("HARD_GATE_FAILED:acceptable_extension", scored.failures)
        self.assertEqual(
            PipelineThresholds.from_policy(policy),
            PipelineThresholds(
                None,
                None,
                None,
                None,
                score_policy="ranking_only",
                a_plus_enabled=False,
            ),
        )

    def test_pipeline_threshold_policy_rejects_every_ambiguous_shape(self) -> None:
        base = premarket_analysis_policy()
        malformed = (
            {"score_policy": "ranking_only", "minimum_setup_score": 1},
            {"score_policy": "ranking_only", "a_plus_enabled": True},
            {"score_policy": "unknown"},
            {
                "score_policy": "threshold_gated",
                "a_plus_enabled": True,
                "minimum_setup_score": 70,
                "minimum_execution_score": None,
                "a_plus_setup_score": 95,
                "a_plus_execution_score": 90,
            },
        )
        for update in malformed:
            with self.subTest(update=update):
                config = copy.deepcopy(base.config)
                config["discovery"].update(update)
                with self.assertRaises(ValueError):
                    PipelineThresholds.from_policy(replace(base, config=config))
        missing = copy.deepcopy(base.config)
        missing["discovery"].pop("minimum_setup_score")
        with self.assertRaisesRegex(ValueError, "score fields are missing"):
            PipelineThresholds.from_policy(replace(base, config=missing))


if __name__ == "__main__":
    unittest.main()
