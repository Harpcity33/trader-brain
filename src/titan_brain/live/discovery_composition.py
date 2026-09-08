"""Release-shipped composition for live Massive and Robinhood evidence.

This module contains orchestration semantics only. Provider credentials remain
in injected, already-authorized clients, while RuntimeComposition proves every
executable provider object against the signed release inventory before use.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, Protocol

from .broker import BrokerClient
from .latency import LatencyRecorder
from .market_data import MarketDataCache
from .massive_adapter import MassiveRestStreamSource
from .pipeline import (
    FullLiveDiscoveryExecutor,
    FullLiveEntryPipeline,
    LiveValidationEvidence,
    PipelineThresholds,
    PreparedStructure,
    REQUIRED_HARD_GATE_FACTS,
    RobinhoodInstrumentEvidenceProvider,
)


SUPPORTED_DISCOVERY_COMPOSITION_ID = (
    "titan.massive_rest_stream.robinhood_instrument.quality.v1"
)


def _aware(value: object, field: str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field} is not ISO-8601") from exc
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


class ProductionEvidenceReader(Protocol):
    """Credential-neutral provider reader used by the concrete composition."""

    def readiness(
        self, *, as_of: datetime, timeout_seconds: float
    ) -> Mapping[str, Any]: ...


class QualityEvidenceRecordReader(ProductionEvidenceReader, Protocol):
    def get_quality_evidence(
        self,
        source_plan_id: str,
        symbol: str,
        *,
        as_of: datetime,
        timeout_seconds: float,
    ) -> Mapping[str, Any]: ...


class ProductionRobinhoodInstrumentReader(ProductionEvidenceReader, Protocol):
    def get_equity_instrument(
        self,
        symbol: str,
        *,
        as_of: datetime,
        timeout_seconds: float,
    ) -> Mapping[str, Any]: ...


class NormalizedQualityEvidenceProvider:
    """Strict adapter for independently recomputed live-quality evidence."""

    REQUIRED_FIELDS = frozenset(
        {
            "evidence_id",
            "source_plan_id",
            "symbol",
            "observed_at",
            "completed_bar_end",
            "entry_limit",
            "structural_stop",
            "targets",
            "execution_reserve_per_share",
            "setup_components",
            "execution_components",
            "hard_gate_facts",
            "shadow_proposal_grants_authority",
        }
    )

    def __init__(
        self,
        reader: QualityEvidenceRecordReader,
        *,
        timeout_seconds: float = 3.0,
    ) -> None:
        if not 0 < float(timeout_seconds) <= 30:
            raise ValueError("quality evidence timeout must be in (0, 30]")
        self.reader = reader
        self.timeout_seconds = float(timeout_seconds)

    def revalidate_structure(
        self, structure: PreparedStructure, *, now: datetime
    ) -> LiveValidationEvidence | None:
        current = _aware(now, "quality.now")
        try:
            raw = self.reader.get_quality_evidence(
                structure.source_plan_id,
                structure.symbol,
                as_of=current,
                timeout_seconds=self.timeout_seconds,
            )
            if not isinstance(raw, Mapping) or set(raw) != self.REQUIRED_FIELDS:
                return None
            if raw["shadow_proposal_grants_authority"] is not False:
                return None
            targets_raw = raw["targets"]
            if not isinstance(targets_raw, (list, tuple)) or not targets_raw:
                return None
            setup = raw["setup_components"]
            execution = raw["execution_components"]
            hard_gates = raw["hard_gate_facts"]
            if not all(isinstance(item, Mapping) for item in (setup, execution, hard_gates)):
                return None
            if set(hard_gates) != REQUIRED_HARD_GATE_FACTS or any(
                hard_gates[name] is not True for name in REQUIRED_HARD_GATE_FACTS
            ):
                return None
            evidence = LiveValidationEvidence(
                evidence_id=str(raw["evidence_id"]).strip(),
                source_plan_id=str(raw["source_plan_id"]).strip(),
                symbol=str(raw["symbol"]).strip().upper(),
                observed_at=_aware(raw["observed_at"], "quality.observed_at"),
                completed_bar_end=_aware(
                    raw["completed_bar_end"], "quality.completed_bar_end"
                ),
                entry_limit=Decimal(str(raw["entry_limit"])),
                structural_stop=Decimal(str(raw["structural_stop"])),
                targets=tuple(Decimal(str(value)) for value in targets_raw),
                execution_reserve_per_share=Decimal(
                    str(raw["execution_reserve_per_share"])
                ),
                setup_components={str(k): float(v) for k, v in setup.items()},
                execution_components={
                    str(k): float(v) for k, v in execution.items()
                },
                hard_gate_facts={str(k): bool(v) for k, v in hard_gates.items()},
                shadow_proposal_grants_authority=False,
            )
            if (
                not evidence.evidence_id
                or evidence.source_plan_id != structure.source_plan_id
                or evidence.symbol != structure.symbol
                or evidence.observed_at > current
                or evidence.completed_bar_end > evidence.observed_at
                or evidence.entry_limit <= Decimal("5")
                or evidence.structural_stop <= 0
                or evidence.structural_stop >= evidence.entry_limit
                or any(target <= evidence.entry_limit for target in evidence.targets)
                or evidence.execution_reserve_per_share <= 0
            ):
                return None
            return evidence
        except (ArithmeticError, TypeError, ValueError):
            return None


class SupportedDiscoveryProviderComposition:
    """Concrete production composition; credentials stay in injected readers."""

    def __init__(
        self,
        *,
        source: MassiveRestStreamSource,
        instrument_reader: ProductionRobinhoodInstrumentReader,
        quality_reader: QualityEvidenceRecordReader,
        provider_binding_id: str,
        timeout_seconds: float = 3.0,
    ) -> None:
        if not isinstance(source, MassiveRestStreamSource):
            raise ValueError("production discovery requires MassiveRestStreamSource")
        if len(provider_binding_id) != 64 or any(
            value not in "0123456789abcdef" for value in provider_binding_id
        ):
            raise ValueError("provider binding must be a non-secret SHA-256 receipt")
        if source.rest.authorization.binding_id != provider_binding_id:
            raise ValueError(
                "Massive authorization differs from the provider binding"
            )
        if not 0 < float(timeout_seconds) <= 30:
            raise ValueError("provider timeout must be in (0, 30]")
        self._source = source
        self.instrument_reader = instrument_reader
        self.quality_reader = quality_reader
        self.provider_binding_id = provider_binding_id
        self.timeout_seconds = float(timeout_seconds)

    @property
    def identity(self) -> str:
        return SUPPORTED_DISCOVERY_COMPOSITION_ID

    @property
    def market_source(self) -> MassiveRestStreamSource:
        return self._source

    def release_components(self) -> tuple[tuple[str, object, tuple[str, ...]], ...]:
        """Expose executable dependencies for signed-inventory verification."""

        return (
            (
                "market_source",
                self._source,
                (
                    "health",
                    "prepared_structures",
                    "hydrate_cache",
                    "release_components",
                ),
            ),
            (
                "robinhood_instrument_reader",
                self.instrument_reader,
                ("readiness", "get_equity_instrument"),
            ),
            (
                "quality_evidence_reader",
                self.quality_reader,
                ("readiness", "get_quality_evidence"),
            ),
        )

    def tradability_ready(self, *, now: datetime) -> bool:
        current = _aware(now, "provider readiness time")
        try:
            records = (
                self.instrument_reader.readiness(
                    as_of=current, timeout_seconds=self.timeout_seconds
                ),
                self.quality_reader.readiness(
                    as_of=current, timeout_seconds=self.timeout_seconds
                ),
            )
            for record in records:
                if not isinstance(record, Mapping):
                    return False
                if (
                    record.get("ready") is not True
                    or record.get("authenticated") is not True
                    or str(record.get("provider_binding_id", ""))
                    != self.provider_binding_id
                ):
                    return False
                observed = _aware(record.get("observed_at"), "provider observed_at")
                age = (current - observed).total_seconds()
                if age < -1 or age > self.timeout_seconds:
                    return False
            return True
        except Exception:
            return False

    def build_executor(
        self,
        *,
        policy: object,
        state: object,
        broker: BrokerClient,
        writer_lock: object,
        latency: LatencyRecorder | None,
        authority: object,
    ) -> FullLiveDiscoveryExecutor:
        # Imports are concrete and release-contained; the writer lock is
        # revalidated by ``authority`` at every mutation boundary.
        instrument = RobinhoodInstrumentEvidenceProvider(
            self.instrument_reader, timeout_seconds=self.timeout_seconds
        )
        quality = NormalizedQualityEvidenceProvider(
            self.quality_reader, timeout_seconds=self.timeout_seconds
        )
        cache = MarketDataCache()
        pipeline = FullLiveEntryPipeline(
            policy=policy,
            market_data=cache,
            state=state,
            broker=broker,
            authority=authority,
            instrument_evidence=instrument,
            quality_evidence=quality,
            thresholds=PipelineThresholds.from_policy(policy),
            latency=latency,
        )
        return FullLiveDiscoveryExecutor(source=self._source, pipeline=pipeline)


__all__ = [
    "NormalizedQualityEvidenceProvider",
    "ProductionRobinhoodInstrumentReader",
    "QualityEvidenceRecordReader",
    "SUPPORTED_DISCOVERY_COMPOSITION_ID",
    "SupportedDiscoveryProviderComposition",
]
