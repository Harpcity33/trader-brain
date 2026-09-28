"""Broker-neutral pipeline adapter for concrete IBKR contract evidence."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from .broker.ibkr_instrument import IbkrInstrumentProvider
from .pipeline import InstrumentEvidence


class IbkrPipelineInstrumentEvidenceProvider:
    """Supply pipeline eligibility solely from IBKR contract-details reads.

    The v1 ``InstrumentEvidence`` wire shape retains a legacy field named
    ``robinhood_tradable``. It is populated from IBKR here and consumed only
    through the broker-neutral ``broker_tradable`` property; no Robinhood or
    Massive assertion participates in this decision.
    """

    def __init__(
        self,
        provider: IbkrInstrumentProvider,
        *,
        inventory_native_provider: bool = True,
    ) -> None:
        if not isinstance(provider, IbkrInstrumentProvider):
            raise TypeError("concrete IBKR instrument provider is required")
        self.provider = provider
        self.inventory_native_provider = bool(inventory_native_provider)

    def get_instrument_evidence(
        self, symbol: str, *, now: datetime
    ) -> InstrumentEvidence | None:
        try:
            evidence = self.provider.get_instrument(symbol, now=now)
            return self._pipeline_evidence(
                evidence,
                eligibility_scope="current_regular_session",
            )
        except Exception:
            return None

    def get_premarket_analysis_evidence(
        self,
        symbol: str,
        *,
        now: datetime,
        regular_session_open: datetime,
    ) -> InstrumentEvidence | None:
        """Verify the upcoming regular session without granting current authority."""

        try:
            evidence = self.provider.get_upcoming_regular_session_instrument(
                symbol,
                now=now,
                eligibility_at=regular_session_open,
            )
            return self._pipeline_evidence(
                evidence,
                eligibility_scope="upcoming_regular_session_analysis",
            )
        except Exception:
            return None

    @staticmethod
    def _pipeline_evidence(
        evidence: object,
        *,
        eligibility_scope: str,
    ) -> InstrumentEvidence:
        return InstrumentEvidence(
            evidence_id=evidence.evidence_id,
            symbol=evidence.identity.symbol,
            instrument_id=str(evidence.identity.con_id),
            observed_at=evidence.observed_at,
            source=evidence.source,
            asset_type="stock",
            exchange_listed=evidence.exchange_listed,
            # Serialized v1 compatibility field; see class docstring.
            robinhood_tradable=evidence.regular_hours_eligible,
            regular_hours_eligible=evidence.regular_hours_eligible,
            eligibility_at=evidence.eligibility_at,
            eligibility_scope=eligibility_scope,
        )

    def readiness(
        self, *, as_of: datetime, timeout_seconds: float
    ) -> Mapping[str, Any]:
        """Report connection authentication; symbol eligibility is per-read."""
        del timeout_seconds
        authenticated_at = self.provider.authenticated_at
        return {
            "ready": self.provider.authenticated,
            "authenticated": self.provider.authenticated,
            "observed_at": authenticated_at or as_of,
            "source": self.provider.source,
            "blocker": (
                None
                if self.provider.authenticated
                else "IBKR_INSTRUMENT_NOT_AUTHENTICATED"
            ),
        }

    def release_components(self) -> tuple[tuple[str, object, tuple[str, ...]], ...]:
        """Bind the concrete IBKR evidence source into the release graph."""
        if not self.inventory_native_provider:
            # The combined production tree owns this shared native provider
            # through the attended preflight bridge.
            return ()
        return ((
            "ibkr_instrument_provider",
            self.provider,
            (
                "release_components",
                "open_generation",
                "get_instrument",
                "get_upcoming_regular_session_instrument",
                "contract_for",
            ),
        ),)


__all__ = ["IbkrPipelineInstrumentEvidenceProvider"]
