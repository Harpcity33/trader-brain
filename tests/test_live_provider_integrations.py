from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
from pathlib import Path
import json
import unittest

from titan_brain.scoring import BASELINE_SETUP_WEIGHTS, EQUITY_EXECUTION_WEIGHTS
from titan_brain.live.composition import RuntimeComposition, RuntimeCompositionError
from titan_brain.live.discovery_composition import (
    NormalizedQualityEvidenceProvider,
    SUPPORTED_DISCOVERY_COMPOSITION_ID,
    SupportedDiscoveryProviderComposition,
)
from titan_brain.live.market_data import MarketDataCache, MarketSessionState
from titan_brain.live.massive_adapter import (
    MassiveAuthorizationEvidence,
    MassiveRestStreamSource,
    MassiveStoreError,
    MassiveStreamStatus,
    PreparedStructure,
    UrllibMassiveRestTransport,
)
from titan_brain.live.pipeline import (
    FullLiveDiscoveryExecutor,
    RobinhoodInstrumentEvidenceProvider,
)


NOW = datetime.now(timezone.utc).replace(microsecond=0)
BINDING = "a" * 64


def entry_session(_now):
    return MarketSessionState.ENTRY_ELIGIBLE


def composition_manifest() -> tuple[dict[str, object], Path]:
    root = Path(__file__).resolve().parents[1]
    sources = (
        Path(__file__).resolve(),
        root / "src/titan_brain/live/discovery_composition.py",
        root / "src/titan_brain/live/massive_adapter.py",
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
    return {"release_manifest_hash": "f" * 64, "files": files}, root


def authorization(binding: str = BINDING) -> MassiveAuthorizationEvidence:
    return MassiveAuthorizationEvidence(
        binding_id=binding,
        credential_source="owner-injected-existing-massive-authorization",
        scopes=("stocks:read",),
        authenticated=True,
    )


class CandidateSource:
    def __init__(self) -> None:
        self.calls = 0

    def prepared_structures(self, *, now, limit):
        self.calls += 1
        return (structure(),)[:limit]


def structure() -> PreparedStructure:
    return PreparedStructure(
        source_plan_id="candidate-1",
        symbol="XYZ",
        observed_at=NOW - timedelta(seconds=2),
        setup_id="controlled_base_breakout",
        ranking_score=Decimal("81"),
        entry_limit=Decimal("10.02"),
        structural_stop=Decimal("9.80"),
        targets=(Decimal("10.50"),),
        payload_hash="b" * 64,
        payload={"trade_authority": False, "broker_authority": False},
    )


class FakeRest:
    def __init__(self, *, binding: str = BINDING) -> None:
        self.authorization = authorization(binding)
        self.calls = []

    def get_json(self, path, *, parameters, timeout_seconds):
        self.calls.append((path, dict(parameters), timeout_seconds))
        if path == "/v1/marketstatus/now":
            return {"status": "open"}
        if path == "/v3/quotes/XYZ":
            return {
                "results": [
                    {
                        "sip_timestamp": int((NOW - timedelta(seconds=2)).timestamp() * 1_000_000_000),
                        "bid_price": 10.00,
                        "ask_price": 10.02,
                        "bid_size": 500,
                        "ask_size": 600,
                    }
                ]
            }
        if path.startswith("/v2/aggs/ticker/XYZ/range/1/minute/"):
            start = NOW.replace(second=0, microsecond=0) - timedelta(minutes=1)
            return {
                "results": [
                    {
                        "t": int(start.timestamp() * 1000),
                        "o": 9.90,
                        "h": 10.10,
                        "l": 9.85,
                        "c": 10.01,
                        "v": 800_000,
                    }
                ]
            }
        raise AssertionError(path)


class FakeStream:
    def __init__(
        self,
        *,
        binding: str = BINDING,
        connected: bool = True,
        events=(),
    ) -> None:
        self.authorization = authorization(binding)
        self.connected = connected
        self.events = tuple(events)
        self.drain_calls = []
        self.symbol_sets = []

    def status(self, *, now):
        return MassiveStreamStatus(
            checked_at=now,
            authorization_binding_id=self.authorization.binding_id,
            connected=self.connected,
            authenticated=self.connected,
            snapshot_resynced=self.connected,
            latest_quote_at=(now - timedelta(seconds=1)) if self.connected else None,
            latest_completed_bar_at=(now - timedelta(seconds=1)) if self.connected else None,
        )

    def drain(self, *, limit, timeout_seconds):
        self.drain_calls.append((limit, timeout_seconds))
        return self.events[:limit]

    def set_symbols(self, symbols):
        self.symbol_sets.append(tuple(symbols))

    def close(self):
        self.connected = False


class Tradable:
    def __init__(self) -> None:
        self.calls = []

    def is_tradable(self, symbol, *, as_of):
        self.calls.append((symbol, as_of))
        return symbol == "XYZ"


class Authorizer:
    evidence = authorization()

    def authorize(self, headers):
        return {**headers, "Authorization": "Bearer never-persist-this-secret"}


class Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read(self, _limit):
        return self.payload


class RobinhoodReader:
    def __init__(self, record=None, *, error=None) -> None:
        self.record = record
        self.error = error
        self.calls = []

    def get_equity_instrument(self, symbol, *, as_of, timeout_seconds):
        self.calls.append((symbol, as_of, timeout_seconds))
        if self.error is not None:
            raise self.error
        return dict(self.record or {})


def quality_record(**updates):
    record = {
        "evidence_id": "quality-evidence-1",
        "source_plan_id": "candidate-1",
        "symbol": "XYZ",
        "observed_at": NOW - timedelta(seconds=1),
        "completed_bar_end": NOW - timedelta(minutes=1, seconds=1),
        "entry_limit": "10.02",
        "structural_stop": "9.80",
        "targets": ["10.50"],
        "execution_reserve_per_share": "0.03",
        "setup_components": {name: 80 for name in BASELINE_SETUP_WEIGHTS},
        "execution_components": {name: 80 for name in EQUITY_EXECUTION_WEIGHTS},
        "hard_gate_facts": {
            "independent_geometry_revalidation": True,
            "causal_completed_bar_structure": True,
            "fresh_executable_quote": True,
            "robinhood_tradable": True,
            "acceptable_spread": True,
            "adequate_displayed_depth": True,
            "acceptable_extension": True,
            "favorable_reward_risk": True,
            "remaining_capacity": True,
            "current_session_eligible": True,
        },
        "shadow_proposal_grants_authority": False,
    }
    record.update(updates)
    return record


class QualityReader:
    def __init__(self, record=None, *, binding=BINDING) -> None:
        self.record = quality_record() if record is None else record
        self.binding = binding
        self.calls = []

    def readiness(self, *, as_of, timeout_seconds):
        return {
            "ready": True,
            "authenticated": True,
            "provider_binding_id": self.binding,
            "observed_at": as_of - timedelta(seconds=1),
        }

    def get_quality_evidence(
        self, source_plan_id, symbol, *, as_of, timeout_seconds
    ):
        self.calls.append((source_plan_id, symbol, as_of, timeout_seconds))
        return dict(self.record)


class ProductionRobinhoodReader(RobinhoodReader):
    def __init__(self, record=None, *, binding=BINDING) -> None:
        super().__init__(record)
        self.binding = binding

    def readiness(self, *, as_of, timeout_seconds):
        return {
            "ready": True,
            "authenticated": True,
            "provider_binding_id": self.binding,
            "observed_at": as_of - timedelta(seconds=1),
        }


class MinimalLivePolicy:
    config = {
        "discovery": {
            "minimum_setup_score": 70,
            "minimum_execution_score": 70,
            "a_plus_setup_score": 90,
            "a_plus_execution_score": 90,
        },
        "evidence": {"quote_max_age_seconds": 5},
    }


class ProviderIntegrationTests(unittest.TestCase):
    def source(self, *, rest=None, stream=None, session=None):
        source = MassiveRestStreamSource(
            candidates=CandidateSource(),
            rest=rest or FakeRest(),
            stream=stream or FakeStream(),
            session_state=session or (lambda _: MarketSessionState.ENTRY_ELIGIBLE),
            health_max_age_seconds=15,
            candidate_max_age_seconds=120,
        )
        self.addCleanup(source.close)
        return source

    def test_closed_session_is_waiting_not_service_failure(self) -> None:
        source = self.source(
            stream=FakeStream(connected=False),
            session=lambda _: MarketSessionState.WAITING_FOR_SESSION,
        )
        health = source.health(now=NOW)
        self.assertTrue(health.service_healthy)
        self.assertEqual(health.session_state, MarketSessionState.WAITING_FOR_SESSION)
        self.assertEqual(health.blockers, ())
        self.assertEqual(health.entry_blockers, ("WAITING_FOR_SESSION",))
        self.assertFalse(health.entry_evidence_ready)

    def test_open_session_keeps_stream_freshness_as_entry_gate(self) -> None:
        health = self.source(stream=FakeStream(connected=False)).health(now=NOW)
        self.assertTrue(health.service_healthy)
        self.assertIn("MASSIVE_STREAM_DISCONNECTED", health.entry_blockers)
        self.assertIn("MASSIVE_QUOTE_MISSING", health.entry_blockers)
        self.assertFalse(health.entry_evidence_ready)

    def test_rest_and_stream_must_use_same_injected_authorization_binding(self) -> None:
        with self.assertRaisesRegex(ValueError, "authorization bindings differ"):
            self.source(stream=FakeStream(binding="c" * 64))

    def test_rest_and_stream_hydrate_nbbo_with_independent_tradability(self) -> None:
        stream_quote = {
            "ev": "Q",
            "sym": "XYZ",
            "t": int((NOW - timedelta(seconds=1)).timestamp() * 1_000_000_000),
            "bp": 10.01,
            "ap": 10.03,
            "bs": 700,
            "as": 800,
        }
        source = self.source(stream=FakeStream(events=(stream_quote,)))
        cache = MarketDataCache()
        tradability = Tradable()
        failures = source.hydrate_cache(
            cache,
            structures=(structure(),),
            session_start=NOW - timedelta(minutes=31),
            now=NOW,
            tradability=tradability,
        )
        self.assertEqual(failures, ())
        self.assertTrue(source.wait_for_backfills(timeout_seconds=2))
        deadline = datetime.now(timezone.utc) + timedelta(seconds=1)
        while datetime.now(timezone.utc) < deadline:
            cached = cache.quote_for("XYZ")
            if cached is not None and cached.bid == Decimal("10.01"):
                break
            source._stop.wait(0.005)
        self.assertEqual(str(cache.quotes["XYZ"].bid), "10.01")
        self.assertTrue(cache.quotes["XYZ"].tradable)
        self.assertIn("nbbo_top_of_book", cache.quotes["XYZ"].source)
        self.assertIn("robinhood_instrument", cache.quotes["XYZ"].source)
        self.assertEqual(len(cache.bars["XYZ"]), 1)
        self.assertGreaterEqual(len(tradability.calls), 1)

    def test_bounded_urllib_transport_uses_only_injected_authorizer(self) -> None:
        captured = {}

        def opener(request, *, timeout):
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            captured["timeout"] = timeout
            return Response(json.dumps({"status": "OK"}).encode())

        transport = UrllibMassiveRestTransport(Authorizer(), opener=opener)
        result = transport.get_json(
            "/v1/marketstatus/now",
            parameters={"z": "2", "a": "1"},
            timeout_seconds=2,
        )
        self.assertEqual(result["status"], "OK")
        self.assertEqual(
            captured["url"],
            "https://api.massive.com/v1/marketstatus/now?a=1&z=2",
        )
        self.assertEqual(captured["timeout"], 2.0)
        self.assertIn("Authorization", captured["headers"])
        with self.assertRaises(ValueError):
            transport.get_json(
                "https://attacker.invalid/steal", parameters={}, timeout_seconds=2
            )

        def failing(_request, *, timeout):
            raise OSError("never-persist-this-secret")

        failed = UrllibMassiveRestTransport(Authorizer(), opener=failing)
        with self.assertRaises(MassiveStoreError) as raised:
            failed.get_json("/v1/marketstatus/now", parameters={}, timeout_seconds=2)
        self.assertNotIn("never-persist-this-secret", str(raised.exception))

    def test_robinhood_instrument_provider_requires_explicit_broker_facts(self) -> None:
        record = {
            "evidence_id": "rh-evidence-1",
            "symbol": "XYZ",
            "instrument_id": "rh-instrument-xyz",
            "observed_at": (NOW - timedelta(seconds=1)).isoformat(),
            "source": "robinhood_authenticated_instrument_read",
            "asset_type": "stock",
            "exchange_listed": True,
            "robinhood_tradable": True,
            "regular_hours_eligible": True,
        }
        reader = RobinhoodReader(record)
        provider = RobinhoodInstrumentEvidenceProvider(reader, timeout_seconds=2)
        evidence = provider.get_instrument_evidence("xyz", now=NOW)
        self.assertEqual(evidence.symbol, "XYZ")
        self.assertEqual(evidence.instrument_id, "rh-instrument-xyz")
        self.assertEqual(reader.calls, [("XYZ", NOW, 2.0)])

        missing = dict(record)
        missing.pop("robinhood_tradable")
        self.assertIsNone(
            RobinhoodInstrumentEvidenceProvider(
                RobinhoodReader(missing)
            ).get_instrument_evidence("XYZ", now=NOW)
        )
        massive_claim = dict(record, source="massive")
        self.assertIsNone(
            RobinhoodInstrumentEvidenceProvider(
                RobinhoodReader(massive_claim)
            ).get_instrument_evidence("XYZ", now=NOW)
        )
        self.assertIsNone(
            RobinhoodInstrumentEvidenceProvider(
                RobinhoodReader(error=TimeoutError("synthetic"))
            ).get_instrument_evidence("XYZ", now=NOW)
        )

    def test_release_shipped_quality_provider_normalizes_exact_live_record(self) -> None:
        reader = QualityReader()
        provider = NormalizedQualityEvidenceProvider(reader, timeout_seconds=2)
        evidence = provider.revalidate_structure(structure(), now=NOW)
        self.assertIsNotNone(evidence)
        assert evidence is not None
        self.assertEqual(evidence.source_plan_id, "candidate-1")
        self.assertEqual(evidence.symbol, "XYZ")
        self.assertEqual(evidence.entry_limit, Decimal("10.02"))
        self.assertEqual(set(evidence.setup_components), set(BASELINE_SETUP_WEIGHTS))
        self.assertEqual(
            set(evidence.execution_components), set(EQUITY_EXECUTION_WEIGHTS)
        )
        self.assertEqual(reader.calls, [("candidate-1", "XYZ", NOW, 2.0)])

        false_gate = quality_record()
        false_gate["hard_gate_facts"] = dict(
            false_gate["hard_gate_facts"], fresh_executable_quote=False
        )
        self.assertIsNone(
            NormalizedQualityEvidenceProvider(
                QualityReader(false_gate)
            ).revalidate_structure(structure(), now=NOW)
        )
        extra = quality_record(untrusted_extra_field=True)
        self.assertIsNone(
            NormalizedQualityEvidenceProvider(
                QualityReader(extra)
            ).revalidate_structure(structure(), now=NOW)
        )

    def test_concrete_composition_builds_authority_bound_pipeline(self) -> None:
        instrument_record = {
            "evidence_id": "rh-evidence-1",
            "symbol": "XYZ",
            "instrument_id": "rh-instrument-xyz",
            "observed_at": NOW - timedelta(seconds=1),
            "source": "robinhood_authenticated_instrument_read",
            "asset_type": "stock",
            "exchange_listed": True,
            "robinhood_tradable": True,
            "regular_hours_eligible": True,
        }
        provider = SupportedDiscoveryProviderComposition(
            source=self.source(),
            instrument_reader=ProductionRobinhoodReader(instrument_record),
            quality_reader=QualityReader(),
            provider_binding_id=BINDING,
            timeout_seconds=2,
        )
        self.assertEqual(provider.identity, SUPPORTED_DISCOVERY_COMPOSITION_ID)
        self.assertTrue(provider.tradability_ready(now=NOW))
        self.assertIsInstance(provider.market_source, MassiveRestStreamSource)
        self.assertEqual(
            {role for role, _component, _members in provider.release_components()},
            {
                "market_source",
                "robinhood_instrument_reader",
                "quality_evidence_reader",
            },
        )
        authority = object()
        executor = provider.build_executor(
            policy=MinimalLivePolicy(),
            state=object(),
            broker=object(),
            writer_lock=object(),
            latency=None,
            authority=authority,
        )
        self.assertIsInstance(executor, FullLiveDiscoveryExecutor)
        self.assertIs(executor.pipeline.authority, authority)
        self.assertIs(executor.pipeline.execution.authority, authority)

        mismatched = SupportedDiscoveryProviderComposition(
            source=self.source(),
            instrument_reader=ProductionRobinhoodReader(
                instrument_record, binding="f" * 64
            ),
            quality_reader=QualityReader(),
            provider_binding_id=BINDING,
        )
        self.assertFalse(mismatched.tradability_ready(now=NOW))

    def test_runtime_composition_rejects_captured_session_callable(self) -> None:
        marker = MarketSessionState.ENTRY_ELIGIBLE

        def captured_session(_now):
            return marker

        instrument = ProductionRobinhoodReader(
            {
                "evidence_id": "rh-evidence-1",
                "symbol": "XYZ",
                "instrument_id": "rh-instrument-xyz",
                "observed_at": NOW,
                "source": "robinhood_authenticated_instrument_read",
                "asset_type": "stock",
                "exchange_listed": True,
                "robinhood_tradable": True,
                "regular_hours_eligible": True,
            }
        )
        provider = SupportedDiscoveryProviderComposition(
            source=self.source(session=captured_session),
            instrument_reader=instrument,
            quality_reader=QualityReader(),
            provider_binding_id=BINDING,
        )
        manifest, root = composition_manifest()
        with self.assertRaisesRegex(
            RuntimeCompositionError, "FUNCTION_CAPTURE_OR_DEFAULT_NOT_ATTESTABLE"
        ):
            RuntimeComposition(discovery_provider=provider).bind_release(
                manifest, release_root=root
            )

    def test_runtime_composition_rejects_custom_massive_opener(self) -> None:
        transport = UrllibMassiveRestTransport(
            Authorizer(),
            opener=lambda _request, timeout: Response(b"{}"),
        )
        provider = SupportedDiscoveryProviderComposition(
            source=self.source(rest=transport, session=entry_session),
            instrument_reader=ProductionRobinhoodReader({}),
            quality_reader=QualityReader(),
            provider_binding_id=BINDING,
        )
        manifest, root = composition_manifest()
        with self.assertRaisesRegex(
            RuntimeCompositionError, "DEPENDENCY_ENUMERATION_FAILED"
        ):
            RuntimeComposition(discovery_provider=provider).bind_release(
                manifest, release_root=root
            )


if __name__ == "__main__":
    unittest.main()
