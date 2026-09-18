"""Session risk constraints reuse exposure accounting, never legacy P&L claims."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal, localcontext
from pathlib import Path
import unittest
from unittest.mock import patch

from titan_brain.live.broker.base import AccountSnapshot, FundsSnapshot
from titan_brain.live.pipeline import build_account_risk_snapshot, build_session_account_risk_snapshot
from titan_brain.live.risk_runtime import RiskExposure, SessionAccountRiskSnapshot
from titan_brain.live.state import LiveStateStore
from tests import test_live_session_policy_bundle as bundle_fixtures
from tests import test_live_session_trading_policy as session_fixtures


D = Decimal
NOW = session_fixtures.START


class SessionPipelineSnapshotTests(unittest.TestCase):
    def setUp(self):
        fixture = bundle_fixtures.SessionPolicyBundleTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.policy = fixture.load()
        self.state = LiveStateStore(Path(fixture.directory.name) / "execution.sqlite3")
        self.addCleanup(self.state.close)
        self.session = session_fixtures.observe(session_fixtures.initial(
            account_binding_sha256=self.policy.config["execution"]["production_account_binding_fingerprint"],
        ))
        self.snapshot = AccountSnapshot(
            account_masked=f"••••{self.policy.account_last4}", observed_at=NOW, received_at=NOW,
            account_state="active", account_type="no_borrow_margin",
            funds=FundsSnapshot(total_value=D(10000), cash=D(9000), buying_power=D(9000), unleveraged_buying_power=D(9000)),
            equity_positions=(), equity_orders=(), option_position_count=0, option_order_count=0,
            advanced_order_count=0, standard_equity_positions_complete=True,
            standard_equity_orders_complete=True, option_positions_complete=True,
            option_orders_complete=True, advanced_orders_complete=True, auth_point_in_time=True,
        )

    def build(self, **changes):
        args = dict(policy=self.policy, state=self.state, broker_snapshot=self.snapshot,
                    session_state=self.session, account_binding_sha256=self.session.baseline.account_binding_sha256, now=NOW)
        args.update(changes)
        return build_session_account_risk_snapshot(**args)

    def test_flat_session_path_needs_no_fabricated_legacy_fields(self):
        result, failures = self.build()
        self.assertEqual(failures, ())
        self.assertIs(type(result), SessionAccountRiskSnapshot)
        self.assertIs(result.session_state, self.session)
        self.assertEqual(result.cash, D(9000))
        for field in ("daily_realized_pnl", "weekly_realized_pnl", "peak_equity"):
            self.assertIsNone(getattr(self.snapshot, field))
            self.assertFalse(hasattr(result, field))
        self.assertFalse(self.snapshot.entry_risk_evidence_ready)
        self.assertIn("SESSION_TRADING_RUNTIME_INTEGRATION_UNAVAILABLE", self.policy.activation_blockers)

    def test_legacy_builder_cannot_fall_through_for_session_policy(self):
        result, failures = build_account_risk_snapshot(policy=self.policy, state=self.state, broker_snapshot=self.snapshot, now=NOW)
        self.assertIsNone(result)
        self.assertEqual(failures, ("SESSION_RISK_SNAPSHOT_PATH_REQUIRED",))

    def test_binding_policy_and_missing_state_fail_closed(self):
        initial = session_fixtures.initial()
        wrong_policy = replace(initial, baseline=replace(initial.baseline, policy_sha256="f" * 64))
        for change in ({"account_binding_sha256": "f" * 64}, {"session_state": None},
                       {"session_state": wrong_policy}):
            with self.subTest(change=tuple(change)):
                result, failures = self.build(**change)
                self.assertIsNone(result)
                self.assertTrue(failures)

    def test_self_consistent_foreign_private_binding_is_not_policy_binding(self):
        foreign = session_fixtures.observe(session_fixtures.initial(account_binding_sha256="f" * 64))
        result, failures = self.build(session_state=foreign, account_binding_sha256="f" * 64)
        self.assertIsNone(result)
        self.assertEqual(failures, ("SESSION_RISK_BINDING_MISMATCH",))

    def test_missing_measurement_and_pending_gap_survive_snapshot(self):
        from titan_brain.live.session_trading_policy import begin_observation, fail_observation
        missing = session_fixtures.initial()
        pending = begin_observation(self.session, token="unfinished", now=NOW)
        failed = fail_observation(pending, token="unfinished", gap=True)
        for candidate in (missing, pending, failed):
            result, failures = self.build(session_state=candidate)
            self.assertIsNone(result)
            self.assertTrue(failures)

    def test_stale_future_wrong_account_and_unreconciled_fields_rejected(self):
        changes = [dict(observed_at=NOW - timedelta(seconds=6), received_at=NOW - timedelta(seconds=6)),
                   dict(observed_at=NOW + timedelta(seconds=1), received_at=NOW + timedelta(seconds=1)),
                   dict(account_masked="••••1234"), dict(auth_point_in_time=False),
                   dict(option_order_count=1), dict(advanced_order_count=1)]
        changes.extend({field: False} for field in (
            "standard_equity_orders_complete", "standard_equity_positions_complete",
            "option_orders_complete", "option_positions_complete", "advanced_orders_complete",
        ))
        for change in changes:
            with self.subTest(change=change):
                result, failures = self.build(broker_snapshot=replace(self.snapshot, **change))
                self.assertIsNone(result)
                self.assertTrue(failures)

    def test_account_loss_latch_survives_later_gain(self):
        breached = session_fixtures.observe(session_fixtures.initial(
            account_binding_sha256=self.session.baseline.account_binding_sha256,
        ), pnl="-1000")
        recovered = session_fixtures.observe(breached, pnl="100", at=NOW + timedelta(seconds=1), token="later")
        result, failures = self.build(session_state=recovered, now=NOW + timedelta(seconds=1))
        self.assertIsNone(result)
        self.assertIn("SESSION_LOSS_LATCHED", failures)

    def test_existing_exposure_helper_is_shared_not_bypassed(self):
        pending = RiskExposure.build(reference="reservation:synthetic", category="pending", planned_risk=D(2),
                                     stress_risk=D(5), execution_reserve=D(1), fee_reserve=D(2), notional=D(100), protected=True)
        with patch("titan_brain.live.pipeline._build_account_risk_exposures", return_value=((pending,), ())) as helper:
            result, failures = self.build(exclude_plan_id="synthetic-plan")
        self.assertEqual(failures, ())
        self.assertEqual(result.exposures, (pending,))
        self.assertEqual(helper.call_args.kwargs["exclude_plan_id"], "synthetic-plan")

    def test_durable_reservation_cents_are_exact_under_low_and_high_context(self):
        # Synthetic joined durable row, not a mocked exposure: exercise the
        # real cents conversion, fee derivation and common exposure assembly.
        row = {
            "reservation_id": "synthetic-reservation", "plan_id": "synthetic-plan",
            "intent_id": "synthetic-intent", "intent_state": "READY", "symbol": "TEST",
            "plan_quantity": 2, "plan_account_key": self.policy.account_key,
            "intent_account_key": self.policy.account_key, "intent_plan_id": "synthetic-plan",
            "plan_strategy_id": self.policy.strategy_id, "plan_policy_hash": self.policy.policy_hash,
            "plan_config_hash": self.policy.config_hash,
            "planned_risk_cents": 10234, "execution_reserve_cents": 100,
            "stress_risk_cents": 10734, "notional_cents": 25234,
        }

        def rows(sql, _parameters=()):
            return [row] if "SELECT r.*" in sql else []

        snapshots = []
        with patch.object(self.state, "rows", side_effect=rows):
            for precision in (3, 6, 28, 80):
                with self.subTest(precision=precision), localcontext() as context:
                    context.prec = precision
                    result, failures = self.build()
                    self.assertEqual(context.prec, precision)
                    self.assertEqual(failures, ())
                    self.assertIs(type(result), SessionAccountRiskSnapshot)
                    self.assertEqual(len(result.exposures), 1)
                    exposure = result.exposures[0]
                    self.assertEqual(exposure.category, "pending")
                    self.assertEqual(exposure.planned_risk, D("102.34"))
                    self.assertEqual(exposure.execution_reserve, D("1.00"))
                    self.assertEqual(exposure.fee_reserve, D("4.00"))
                    self.assertEqual(exposure.stress_risk, D("107.34"))
                    self.assertEqual(exposure.notional, D("252.34"))
                    snapshots.append(result)
        self.assertTrue(all(snapshot == snapshots[0] for snapshot in snapshots))

    def test_open_risk_does_not_reuse_original_entry_downside(self):
        for category in ("open", "manual"):
            exposure = RiskExposure.build(reference="synthetic", category=category, planned_risk=D(2),
                                          stress_risk=D(5), execution_reserve=D(1), fee_reserve=D(2), notional=D(100), protected=True)
            with patch("titan_brain.live.pipeline._build_account_risk_exposures", return_value=((exposure,), ())):
                result, failures = self.build()
            self.assertIsNone(result)
            self.assertIn("SESSION_OPEN_RISK_REVALUATION_REQUIRED", failures)


if __name__ == "__main__":
    unittest.main()
