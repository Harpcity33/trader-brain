from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime, timedelta
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
    REQUIRED_HARD_GATE_FACTS,
    build_account_risk_snapshot,
)
from titan_brain.live.policy import PolicyBundle, sha256_json
from titan_brain.live.risk_runtime import SessionLatch
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

    def pipeline(self, items, *, policy=None, cache=None, broker=None, latency=None):
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
            thresholds=PipelineThresholds(70, 65, 95, 90),
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


if __name__ == "__main__":
    unittest.main()
