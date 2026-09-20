"""Part-2 attended-descriptor contract tests (attended-execution enablement).

Covers the gaps the coverage review flagged, without duplicating existing tests:
- the attended IBKR descriptor's capability flags directly (unattended /
  atomic-protection / replacement / extended-hours all denied);
- the two build_broker_client BrokerFactoryError branches not otherwise unit
  tested (transport_id mismatch, authorization-binding mismatch).

Offline; no broker connection.
"""

from __future__ import annotations

import unittest

from titan_brain.live.broker.ibkr_transport import attended_ibkr_descriptor, IBKR_TRANSPORT_ID
from titan_brain.live.broker.factory import build_broker_client, BrokerFactoryError
from titan_brain.live.broker.base import MarketHours

# Reuse the established fixture transport + coverage helpers.
from tests.test_live_production_broker import (
    FixtureProductionTransport,
    complete_history_contract,
    ACCOUNT,
    ACCOUNT_BINDING,
    AUTHORIZATION_BINDING,
)


class AttendedDescriptorContractTests(unittest.TestCase):
    def _descriptor(self):
        # A coverage contract the attended descriptor accepts (it refuses
        # authoritative negative lookup), so negative_results_authoritative=False.
        coverage = complete_history_contract(negative_results_authoritative=False)
        return attended_ibkr_descriptor(
            exact_account_id="private-account-uuid-1",
            account_masked=ACCOUNT,
            account_binding_fingerprint=ACCOUNT_BINDING,
            authorization_binding_id=AUTHORIZATION_BINDING,
            coverage=coverage,
        )

    def test_descriptor_denies_unattended_and_advanced_writes(self) -> None:
        caps = self._descriptor().capabilities
        self.assertFalse(caps.supports_unattended_writes)
        self.assertFalse(caps.supports_atomic_protection)
        self.assertFalse(caps.supports_equity_replace)
        self.assertTrue(caps.review_requires_explicit_confirmation)
        self.assertTrue(caps.cancel_requires_explicit_confirmation)

    def test_descriptor_is_regular_hours_only(self) -> None:
        caps = self._descriptor().capabilities
        self.assertEqual(tuple(caps.supported_market_hours), (MarketHours.REGULAR,))

    def test_descriptor_lists_forbidden_operations(self) -> None:
        caps = self._descriptor().capabilities
        forbidden = set(caps.unsupported_operations)
        for op in ("unattended_writes", "extended_hours", "overnight", "replace",
                   "atomic_bracket_or_oca_protection"):
            self.assertIn(op, forbidden)


class FactoryBindingMismatchTests(unittest.TestCase):
    def _config(self, **overrides):
        cfg = {
            "broker_adapter": "supported_production_transport",
            "production_transport_id": "fixture-supported-transport-v1",
            "production_account_binding_fingerprint": ACCOUNT_BINDING,
            "production_authorization_binding_id": AUTHORIZATION_BINDING,
        }
        cfg.update(overrides)
        return cfg

    def test_factory_rejects_transport_id_mismatch(self) -> None:
        transport = FixtureProductionTransport()  # transport_id = fixture-supported-transport-v1
        with self.assertRaisesRegex(BrokerFactoryError, "transport differs"):
            build_broker_client(
                self._config(production_transport_id="a-different-transport-id"),
                account_masked=ACCOUNT,
                production_transport=transport,
            )

    def test_factory_rejects_authorization_binding_mismatch(self) -> None:
        transport = FixtureProductionTransport()
        with self.assertRaises(BrokerFactoryError):
            build_broker_client(
                self._config(production_authorization_binding_id="f" * 64),
                account_masked=ACCOUNT,
                production_transport=transport,
            )

    def test_factory_builds_when_all_bindings_match(self) -> None:
        transport = FixtureProductionTransport()
        client = build_broker_client(
            self._config(),
            account_masked=ACCOUNT,
            production_transport=transport,
        )
        self.assertIsNotNone(client)


if __name__ == "__main__":
    unittest.main()
