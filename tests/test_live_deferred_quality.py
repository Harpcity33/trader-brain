"""Post-sizing facts remain unasserted until actual risk/depth validation."""
from dataclasses import asdict, replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from titan_brain.live.broker import FakeBrokerClient
from titan_brain.live.discovery_composition import NormalizedQualityEvidenceProvider
from titan_brain.live.pipeline import (
    FullLiveEntryPipeline, PipelineStatus, PipelineThresholds,
    POST_SIZING_HARD_GATE_FACTS,
)
from titan_brain.live.risk_runtime import SessionLatch
from titan_brain.live.state import LiveStateStore
from tests.test_live_pipeline import (
    NOW, ExplicitTestMutationAuthority, StaticInstrumentProvider,
    StaticQualityProvider, cache_for, enabled_policy, structure, validation_for,
)


class RecordReader:
    def __init__(self, record):
        self.record = record

    def get_quality_evidence(self, *args, **kwargs):
        return self.record


class DeferredQualityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = LiveStateStore(Path(temp.name) / "test.sqlite3")
        self.addCleanup(self.store.close)
        self.policy = enabled_policy()
        self.broker = FakeBrokerClient(clock=lambda: NOW)
        self.snapshot = replace(
            self.broker.get_account_snapshot("••••7153"),
            account_type="limited_margin",
        )
        self.item = structure()
        original = validation_for(self.item)
        self.evidence = replace(
            original,
            hard_gate_facts={
                name: False if name in POST_SIZING_HARD_GATE_FACTS else value
                for name, value in original.hard_gate_facts.items()
            },
            deferred_hard_gate_facts=POST_SIZING_HARD_GATE_FACTS,
        )

    def run_candidate(self, *, evidence=None, cache=None, snapshot=None):
        pipeline = FullLiveEntryPipeline(
            policy=self.policy,
            market_data=cache or cache_for("XYZ"),
            state=self.store, broker=self.broker,
            authority=ExplicitTestMutationAuthority(),
            instrument_evidence=StaticInstrumentProvider(),
            quality_evidence=StaticQualityProvider({"XYZ": evidence or self.evidence}),
            thresholds=PipelineThresholds(70, 65, 95, 90), clock=lambda: NOW,
        )
        return pipeline.run_once(
            structures=[self.item], broker_snapshot=snapshot or self.snapshot,
            latch=SessionLatch(NOW.date()),
        )

    def assert_no_attempt(self, result):
        self.assertIsNone(result.attempted_plan_id)
        self.assertFalse(any(
            name in {FakeBrokerClient.REVIEW, FakeBrokerClient.PLACE}
            for name, *_ in self.broker.calls
        ))

    def test_actual_capacity_and_depth_allow_sized_order(self):
        result = self.run_candidate()
        self.assertEqual(result.status, PipelineStatus.ACKNOWLEDGED)
        self.assertEqual(result.selected.quantity, 54)
        self.assertFalse(self.evidence.hard_gate_facts["remaining_capacity"])
        self.assertFalse(self.evidence.hard_gate_facts["adequate_displayed_depth"])

    def test_no_cash_still_blocks_before_review(self):
        snapshot = replace(self.snapshot, funds=replace(
            self.snapshot.funds, cash=Decimal("0"),
            buying_power=Decimal("0"), unleveraged_buying_power=Decimal("0"),
        ))
        self.assert_no_attempt(self.run_candidate(snapshot=snapshot))

    def test_real_depth_caps_actual_quantity(self):
        cache = cache_for("XYZ")
        cache.quotes["XYZ"] = replace(cache.quotes["XYZ"], bid_size=10, ask_size=10)
        result = self.run_candidate(cache=cache)
        self.assertEqual(result.status, PipelineStatus.ACKNOWLEDGED)
        self.assertEqual(result.selected.quantity, 2)

    def test_insufficient_depth_still_blocks_before_review(self):
        cache = cache_for("XYZ")
        cache.quotes["XYZ"] = replace(cache.quotes["XYZ"], bid_size=2, ask_size=2)
        self.assert_no_attempt(self.run_candidate(cache=cache))

    def test_stale_quote_cannot_be_deferred(self):
        cache = cache_for("XYZ")
        cache.quotes["XYZ"] = replace(
            cache.quotes["XYZ"], venue_bid_at=NOW-timedelta(seconds=10),
            venue_ask_at=NOW-timedelta(seconds=10),
        )
        self.assert_no_attempt(self.run_candidate(cache=cache))

    def test_fresh_ask_cannot_hide_stale_bid_depth(self):
        cache = cache_for("XYZ")
        cache.quotes["XYZ"] = replace(
            cache.quotes["XYZ"], venue_bid_at=NOW-timedelta(seconds=60),
        )
        result = self.run_candidate(cache=cache)
        self.assert_no_attempt(result)
        self.assertIn("QUOTE_STALE", result.candidates[0].failures)

    def test_fresh_bid_cannot_hide_stale_ask(self):
        cache = cache_for("XYZ")
        cache.quotes["XYZ"] = replace(
            cache.quotes["XYZ"], venue_ask_at=NOW-timedelta(seconds=60),
        )
        result = self.run_candidate(cache=cache)
        self.assert_no_attempt(result)
        self.assertIn("QUOTE_STALE", result.candidates[0].failures)

    def test_oldest_side_age_does_not_hide_future_side(self):
        cache = cache_for("XYZ")
        cache.quotes["XYZ"] = replace(
            cache.quotes["XYZ"], venue_ask_at=NOW+timedelta(seconds=60),
        )
        result = self.run_candidate(cache=cache)
        self.assert_no_attempt(result)
        self.assertIn("QUOTE_FUTURE_DATED", result.candidates[0].failures)

    def test_non_sizing_gate_cannot_be_deferred(self):
        evidence = replace(
            self.evidence,
            deferred_hard_gate_facts=frozenset({"acceptable_spread"}),
        )
        result = self.run_candidate(evidence=evidence)
        self.assert_no_attempt(result)
        self.assertIn("DEFERRED_HARD_GATE_FACTS_INVALID", result.candidates[0].failures)

    def test_deferred_gate_cannot_also_claim_success(self):
        evidence = replace(self.evidence, hard_gate_facts=dict(
            self.evidence.hard_gate_facts, remaining_capacity=True,
        ))
        self.assert_no_attempt(self.run_candidate(evidence=evidence))

    def test_legacy_false_gate_remains_a_rejection(self):
        evidence = replace(self.evidence, deferred_hard_gate_facts=frozenset())
        self.assert_no_attempt(self.run_candidate(evidence=evidence))

    def test_missing_gate_cannot_be_deferred(self):
        gates = dict(self.evidence.hard_gate_facts)
        gates.pop("remaining_capacity")
        self.assert_no_attempt(self.run_candidate(evidence=replace(
            self.evidence, hard_gate_facts=gates,
        )))

    def record(self):
        raw = asdict(self.evidence)
        raw["deferred_hard_gate_facts"] = sorted(POST_SIZING_HARD_GATE_FACTS)
        return raw

    def normalize(self, raw):
        return NormalizedQualityEvidenceProvider(RecordReader(raw)).revalidate_structure(
            self.item, now=NOW,
        )

    def test_normalizer_preserves_explicit_unasserted_facts(self):
        normalized = self.normalize(self.record())
        self.assertIsNotNone(normalized)
        self.assertEqual(normalized.deferred_hard_gate_facts, POST_SIZING_HARD_GATE_FACTS)
        self.assertFalse(normalized.hard_gate_facts["remaining_capacity"])

    def test_normalizer_rejects_malformed_or_unsupported_deferrals(self):
        for deferred in (None, "remaining_capacity", [True],
                         ["remaining_capacity", "remaining_capacity"],
                         ["fresh_executable_quote"]):
            with self.subTest(deferred=deferred):
                self.assertIsNone(self.normalize(dict(
                    self.record(), deferred_hard_gate_facts=deferred,
                )))

    def test_normalizer_does_not_accept_other_false_facts(self):
        raw = self.record()
        raw["hard_gate_facts"]["acceptable_spread"] = False
        self.assertIsNone(self.normalize(raw))

    def test_normalizer_rejects_claimed_deferred_success(self):
        raw = self.record()
        raw["hard_gate_facts"]["remaining_capacity"] = True
        self.assertIsNone(self.normalize(raw))
