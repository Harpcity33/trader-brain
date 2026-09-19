"""Area 5 wiring: FullLiveEntryPipeline._risk_snapshot routes by risk model.

Proves the offline-safe session wiring: when the session-trading model is
selected, the pipeline sources SessionTradingState from the injected durable
store and uses the session snapshot builder; with no store or no stored session
it fails closed (never a legacy fallback); and the
SESSION_TRADING_RUNTIME_INTEGRATION_UNAVAILABLE gate REMAINS present throughout.
No broker, network, or credential access. The pipeline instance is built via
object.__new__ with only the attributes _risk_snapshot touches, to avoid
standing up the full collaborator graph.
"""

from types import SimpleNamespace
import unittest

from titan_brain.live.pipeline import FullLiveEntryPipeline
from titan_brain.live.risk_runtime import SessionAccountRiskSnapshot
from tests import test_live_session_pipeline as sp


class _FakeStore:
    """Minimal SessionTradingStore stand-in: returns a StoredSession-like object."""

    def __init__(self, state):
        self._state = state
        self.calls = []

    def load(self, *, account_binding_sha256, session_date):
        self.calls.append((account_binding_sha256, session_date))
        if self._state is None:
            return None
        return SimpleNamespace(state=self._state)


class SessionPipelineWiringTests(unittest.TestCase):
    def setUp(self):
        base = sp.SessionPipelineSnapshotTests()
        base.setUp()
        self.addCleanup(base.doCleanups)
        self.policy = base.policy
        self.state = base.state
        self.snapshot = base.snapshot
        self.session = base.session
        self.now = sp.NOW

    def _pipeline(self, store):
        p = object.__new__(FullLiveEntryPipeline)
        p.policy = self.policy
        p.state = self.state
        p.session_trading_store = store
        return p

    def _risk(self, store):
        return self._pipeline(store)._risk_snapshot(
            broker_snapshot=self.snapshot, now=self.now, prices=None, exclude_plan_id=None,
        )

    def test_session_policy_with_populated_store_routes_to_session_builder(self):
        # Sanity: this fixture's policy IS the session model.
        self.assertTrue(self.policy.session_trading_risk)
        snapshot, failures = self._risk(_FakeStore(self.session))
        # Flat, fully-reconciled snapshot + a completed measurement -> a real
        # SessionAccountRiskSnapshot (the session builder ran, not the legacy one).
        self.assertIsInstance(snapshot, SessionAccountRiskSnapshot)
        self.assertEqual(failures, ())

    def test_session_policy_with_no_store_fails_closed(self):
        snapshot, failures = self._risk(None)
        self.assertIsNone(snapshot)
        self.assertIn("SESSION_RISK_STATE_REQUIRED", failures)

    def test_session_policy_with_empty_store_fails_closed(self):
        snapshot, failures = self._risk(_FakeStore(None))
        self.assertIsNone(snapshot)
        self.assertIn("SESSION_RISK_STATE_REQUIRED", failures)

    def test_the_gate_remains_present_regardless_of_wiring(self):
        # Wiring the session path reachable must NOT lift the activation gate.
        self.assertIn(
            "SESSION_TRADING_RUNTIME_INTEGRATION_UNAVAILABLE",
            self.policy.activation_blockers,
        )

    def test_store_is_queried_with_the_production_account_binding(self):
        store = _FakeStore(self.session)
        self._risk(store)
        binding = self.policy.config["execution"]["production_account_binding_fingerprint"]
        self.assertEqual(len(store.calls), 1)
        self.assertEqual(store.calls[0][0], binding)


if __name__ == "__main__":
    unittest.main()
