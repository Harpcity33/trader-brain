"""Offline contract/state tests; no SDK, broker, credential or authority use."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
import json
from pathlib import Path
import pickle
import tempfile
import unittest

from titan_brain.live import session_trading_policy as policy_module
from titan_brain.live.session_trading_policy import (
    MODEL, OWNER_AMENDMENT_PATH, OWNER_AMENDMENT_SHA256, POLICY_RELATIVE_PATH,
    ObservationStatus, SessionTradingBaseline, SessionTradingMeasurement,
    SessionTradingPolicyError, begin_observation, complete_observation,
    evaluate_session_state, fail_observation, load_session_trading_policy,
    load_session_trading_policy_from_root, start_session,
)


ROOT = Path(__file__).resolve().parents[1]
START = datetime(2026, 9, 18, 13, 30, tzinfo=timezone.utc)
D = Decimal


def mapping():
    return dict(policy_module._FIXED, owner_risk_amendment={
        "amendment_path": OWNER_AMENDMENT_PATH,
        "amendment_sha256": OWNER_AMENDMENT_SHA256,
    })


def initial(**changes):
    policy = load_session_trading_policy(mapping())
    baseline = SessionTradingBaseline(
        policy_sha256=policy.policy_sha256, account_binding_sha256="a" * 64,
        evidence_sha256="b" * 64, frozen_at=START, starting_nlv=D("10000"),
        flat_start=True, pre_entry=True, initial_exposure_reconciled=True,
    )
    return start_session(policy, replace(baseline, **changes))


def measurement(state, pnl="0", *, at=START, **changes):
    result = SessionTradingMeasurement(
        model=MODEL, account_binding_sha256=state.baseline.account_binding_sha256,
        baseline_identity_sha256=state.baseline.identity_sha256,
        evidence_sha256="c" * 64, as_of=at, received_at=at,
        session_pnl=D(pnl) if pnl is not None else None, complete=pnl is not None,
    )
    return replace(result, **changes)


def observe(state=None, pnl="0", *, at=START, token="read-1"):
    state = initial() if state is None else state
    pending = begin_observation(state, token=token, now=at)
    return complete_observation(pending, token=token, measurement=measurement(state, pnl, at=at), now=at)


class SessionTradingContractTests(unittest.TestCase):
    def test_pinned_policy_and_real_approval_bytes_load_without_authority(self):
        loaded = load_session_trading_policy_from_root(ROOT)
        self.assertEqual(loaded, load_session_trading_policy(mapping()))
        self.assertEqual(loaded.amendment_sha256, OWNER_AMENDMENT_SHA256)
        self.assertFalse(evaluate_session_state(initial(), now=START).live_authority)

    def test_every_policy_field_is_exact_not_a_switch(self):
        for key in mapping():
            with self.subTest(key=key):
                raw = mapping()
                raw[key] = "changed"
                with self.assertRaises(SessionTradingPolicyError):
                    load_session_trading_policy(raw)
        for raw in (dict(mapping(), unexpected=True), {}):
            with self.assertRaises(SessionTradingPolicyError):
                load_session_trading_policy(raw)
        raw = mapping()
        raw["intraday_baseline_reset_allowed"] = 0
        with self.assertRaises(SessionTradingPolicyError):
            load_session_trading_policy(raw)

    def test_caller_cannot_relabel_a_different_approval_as_known_owner_choice(self):
        raw = mapping()
        raw["owner_risk_amendment"]["amendment_sha256"] = "f" * 64
        with self.assertRaises(SessionTradingPolicyError):
            load_session_trading_policy(raw, expected_amendment_sha256="f" * 64)

    def copy_policy(self, directory):
        root = Path(directory).resolve()
        for relative in (*policy_module._APPROVAL_FILES, POLICY_RELATIVE_PATH):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((ROOT / relative).read_bytes())
        return root

    def test_any_original_approval_or_amendment_byte_change_is_rejected(self):
        for relative in policy_module._APPROVAL_FILES:
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as directory:
                root = self.copy_policy(directory)
                path = root / relative
                path.write_bytes(path.read_bytes() + b"\n")
                with self.assertRaisesRegex(SessionTradingPolicyError, "APPROVAL_BYTES_MISMATCH"):
                    load_session_trading_policy_from_root(root)

    def test_duplicate_json_key_missing_file_and_symlink_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.copy_policy(directory)
            path = root / POLICY_RELATIVE_PATH
            data = path.read_text()
            path.write_text(data.replace("{", '{"model":"ignored-duplicate",', 1))
            with self.assertRaisesRegex(SessionTradingPolicyError, "DUPLICATE_POLICY_KEY"):
                load_session_trading_policy_from_root(root)
            path.unlink()
            with self.assertRaises(SessionTradingPolicyError):
                load_session_trading_policy_from_root(root)
            path.symlink_to(ROOT / POLICY_RELATIVE_PATH)
            with self.assertRaises(SessionTradingPolicyError):
                load_session_trading_policy_from_root(root)

    def test_only_positive_fixed_flat_pre_entry_baseline_starts(self):
        for change in ({"flat_start": False}, {"pre_entry": False}, {"initial_exposure_reconciled": False},
                       {"starting_nlv": D("0")}, {"starting_nlv": D("-1")}, {"policy_sha256": "d" * 64}):
            with self.subTest(change=change), self.assertRaises(SessionTradingPolicyError):
                initial(**change)

    def test_equivalent_baseline_decimal_representations_keep_identity(self):
        self.assertEqual(initial().baseline.identity_sha256, initial(starting_nlv=D("1.0000E4")).baseline.identity_sha256)

    def test_malformed_money_time_and_claims_are_rejected(self):
        for value in (True, 1.0, D("NaN"), D("Infinity"), D("1E9999"), D("1E-9999")):
            with self.subTest(value=value), self.assertRaises(SessionTradingPolicyError):
                initial(starting_nlv=value)
        with self.assertRaises(SessionTradingPolicyError):
            initial(frozen_at=START.replace(tzinfo=None))
        with self.assertRaises(SessionTradingPolicyError):
            initial(flat_start=1)


class SessionTradingStateTests(unittest.TestCase):
    def test_no_measurement_or_pending_read_blocks_even_with_pre_entry_baseline(self):
        state = initial()
        self.assertIn("SESSION_MEASUREMENT_MISSING", evaluate_session_state(state, now=START).entry_blockers)
        pending = begin_observation(observe(state), token="read-2", now=START)
        result = evaluate_session_state(pending, now=START)
        self.assertIn("UNRESOLVED_OBSERVATION_INCIDENT", result.entry_blockers)
        self.assertEqual(result.aggregate_headroom_before_exposure_and_reserves, D(0))

    def test_exact_capacity_and_no_profit_denominator_uplift(self):
        for pnl, expected in (("0", "1000"), ("-1", "999"), ("-999.99", "0.01"), ("5000", "1000")):
            with self.subTest(pnl=pnl):
                state = observe(pnl=pnl)
                result = evaluate_session_state(state, now=START)
                self.assertEqual(result.aggregate_headroom_before_exposure_and_reserves, D(expected))
                self.assertFalse(result.live_authority)
                self.assertEqual(state.baseline.starting_nlv, D("10000"))

    def test_incurred_fees_in_measurement_are_not_subtracted_again(self):
        result = evaluate_session_state(observe(pnl="-2"), now=START)
        self.assertEqual(result.aggregate_headroom_before_exposure_and_reserves, D("998"))

    def test_loss_boundary_is_exact_sticky_and_requires_guarded_closeout(self):
        for pnl, expected in (("-999.99", False), ("-1000", True), ("-2000", True)):
            with self.subTest(pnl=pnl):
                state = observe(pnl=pnl)
                self.assertEqual(state.loss_latched, expected)
                self.assertEqual(evaluate_session_state(state, now=START).guarded_closeout_required, expected)
        state = observe(pnl="-1000")
        # Local test serialization models carrying the same trusted state, not
        # authentication or a production persistence adapter.
        state = pickle.loads(pickle.dumps(state))
        at = START + timedelta(seconds=1)
        state = observe(state, "5000", at=at, token="recovery")
        result = evaluate_session_state(state, now=at)
        self.assertTrue(state.loss_latched)
        self.assertIn("SESSION_LOSS_LATCHED", result.entry_blockers)
        self.assertEqual(result.aggregate_headroom_before_exposure_and_reserves, D(0))

    def test_fifteen_percent_is_aspiration_only_not_profit_floor_or_forced_exit(self):
        state = observe(pnl="1500")
        self.assertTrue(state.profit_aspiration_observed)
        at = START + timedelta(seconds=1)
        state = observe(state, "50", at=at, token="after-goal")
        result = evaluate_session_state(state, now=at)
        self.assertTrue(result.profit_aspiration_observed)
        self.assertFalse(result.guarded_closeout_required)
        self.assertEqual(result.entry_blockers, ())
        self.assertEqual(result.aggregate_headroom_before_exposure_and_reserves, D("1000"))

    def test_failed_or_gap_read_cannot_be_cleared_by_same_or_later_health(self):
        for gap in (False, True):
            with self.subTest(gap=gap):
                state = begin_observation(initial(), token="failed", now=START)
                state = fail_observation(state, token="failed", gap=gap)
                state = pickle.loads(pickle.dumps(state))
                with self.assertRaises(SessionTradingPolicyError):
                    complete_observation(state, token="failed", measurement=measurement(state), now=START)
                state = observe(state, at=START + timedelta(seconds=1), token="healthy")
                self.assertIn("UNRESOLVED_OBSERVATION_INCIDENT", evaluate_session_state(state, now=START + timedelta(seconds=1)).entry_blockers)
                self.assertIs(state.incidents[0].status, ObservationStatus.FAILED)

    def test_completion_resolves_only_its_exact_pending_token(self):
        state = begin_observation(initial(), token="older", now=START)
        state = begin_observation(state, token="newer", now=START)
        state = complete_observation(state, token="newer", measurement=measurement(state), now=START)
        self.assertIs(state.incidents[0].status, ObservationStatus.PENDING)
        self.assertIn("UNRESOLVED_OBSERVATION_INCIDENT", evaluate_session_state(state, now=START).entry_blockers)
        with self.assertRaises(SessionTradingPolicyError):
            begin_observation(state, token="newer", now=START)

    def test_invalid_observation_becomes_sticky_incident_not_zero(self):
        for change, reason in (({"account_binding_sha256": "d" * 64}, "BINDING"),
                               ({"baseline_identity_sha256": "d" * 64}, "BINDING"),
                               ({"session_pnl": None, "complete": False}, "INCOMPLETE"),
                               ({"received_at": START + timedelta(seconds=1)}, "TIME")):
            with self.subTest(change=change):
                state = begin_observation(initial(), token="bad", now=START)
                state = complete_observation(state, token="bad", measurement=measurement(state, **change), now=START)
                self.assertEqual(state.incidents[0].reason, reason)
                self.assertIsNone(state.last_measurement)
                state = observe(state, token="healthy")
                self.assertIn("UNRESOLVED_OBSERVATION_INCIDENT", evaluate_session_state(state, now=START).entry_blockers)

    def test_shadow_or_other_object_never_becomes_session_measurement(self):
        state = begin_observation(initial(), token="bad", now=START)
        state = complete_observation(state, token="bad", measurement=object(), now=START)
        self.assertEqual(state.incidents[0].reason, "INCOMPLETE")

    def test_stale_read_and_current_date_change_block(self):
        state = begin_observation(initial(), token="slow", now=START)
        state = complete_observation(state, token="slow", measurement=measurement(state), now=START + timedelta(seconds=6))
        self.assertEqual(state.incidents[0].reason, "TIME")
        result = evaluate_session_state(observe(), now=START + timedelta(days=1))
        self.assertIn("SESSION_DATE_MISMATCH", result.entry_blockers)
        self.assertIn("SESSION_MEASUREMENT_STALE", result.entry_blockers)

    def test_restored_future_receipt_or_wall_clock_regression_blocks(self):
        state = observe()
        state = replace(state, last_measurement=replace(
            state.last_measurement, received_at=START + timedelta(seconds=1)))
        result = evaluate_session_state(state, now=START)
        self.assertIn("SESSION_MEASUREMENT_STALE", result.entry_blockers)
        self.assertEqual(result.aggregate_headroom_before_exposure_and_reserves, D(0))
        result = evaluate_session_state(observe(), now=START - timedelta(seconds=1))
        self.assertIn("SESSION_MEASUREMENT_STALE", result.entry_blockers)

    def test_conflicting_same_time_and_older_measurements_cannot_overwrite(self):
        at = START + timedelta(seconds=1)
        state = begin_observation(initial(), token="older", now=START)
        state = observe(state, "1", at=at, token="newer")
        state = complete_observation(state, token="older", measurement=measurement(state), now=at)
        self.assertEqual(state.incidents[0].reason, "NONMONOTONE")
        state = begin_observation(state, token="equivocation", now=at)
        state = complete_observation(state, token="equivocation", measurement=measurement(state, "2", at=at), now=at)
        self.assertEqual(state.incidents[-1].reason, "NONMONOTONE")
        self.assertEqual(state.last_measurement.session_pnl, D("1"))

    def test_reconstructed_state_cannot_change_baseline_or_attach_unbound_measurement(self):
        state = observe()
        for change in ({"starting_nlv": D("20000")}, {"account_binding_sha256": "d" * 64}, {"frozen_at": START + timedelta(seconds=1)}):
            with self.subTest(change=change), self.assertRaises(SessionTradingPolicyError):
                replace(state, baseline=replace(state.baseline, **change))
        with self.assertRaises(SessionTradingPolicyError):
            replace(state, incidents=())
        with self.assertRaises(SessionTradingPolicyError):
            replace(state, last_measurement=measurement(state, None))
        loss = observe(pnl="-1000")
        with self.assertRaisesRegex(SessionTradingPolicyError, "LOSS_LATCH_INCONSISTENT"):
            replace(loss, loss_latched=False)

    def test_low_caller_decimal_precision_cannot_round_loss_or_headroom(self):
        with localcontext() as context:
            context.prec = 2
            state = observe(pnl="-999.99")
            self.assertFalse(state.loss_latched)
            self.assertEqual(evaluate_session_state(state, now=START).aggregate_headroom_before_exposure_and_reserves, D("0.01"))


if __name__ == "__main__":
    unittest.main()
