"""A1.1: reporting-setting persistence across a controlled restart (evaluator).

Proves live/reporting_persistence.py decides PERSISTED / CHANGED / UNVERIFIED
correctly and fails closed on missing/mis-scoped/mis-ordered evidence. It never
asserts persistence without genuine before/after readings. No live Gateway.
"""

from datetime import datetime, timedelta, timezone
import unittest

from titan_brain.live.reporting_persistence import (
    ReportingObservation,
    ReportingPersistenceError,
    ReportingVerdict,
    evaluate_reporting_persistence,
)


ACCOUNT = "ibkr-live-ending-3103"
BEFORE_AT = datetime(2026, 9, 18, 14, 0, tzinfo=timezone.utc)
AFTER_AT = BEFORE_AT + timedelta(minutes=2)
EXPECTED = 19735


def _obs(client_id, *, at, post_restart=False, account=ACCOUNT):
    return ReportingObservation(account_binding=account, master_client_id=client_id,
                                observed_at=at, post_restart=post_restart)


class ReportingPersistenceTests(unittest.TestCase):
    def test_persisted_when_same_expected_id_across_a_real_restart(self):
        result = evaluate_reporting_persistence(
            expected_master_client_id=EXPECTED,
            before=_obs(EXPECTED, at=BEFORE_AT),
            after=_obs(EXPECTED, at=AFTER_AT, post_restart=True),
        )
        self.assertIs(result.verdict, ReportingVerdict.PERSISTED)
        self.assertTrue(result.persisted)
        self.assertEqual(result.reasons, ())

    def test_changed_when_id_differs_across_restart(self):
        result = evaluate_reporting_persistence(
            expected_master_client_id=EXPECTED,
            before=_obs(EXPECTED, at=BEFORE_AT),
            after=_obs(0, at=AFTER_AT, post_restart=True),
        )
        self.assertIs(result.verdict, ReportingVerdict.CHANGED)
        self.assertIn("CLIENT_ID_CHANGED_ACROSS_RESTART", result.reasons)
        self.assertIn("CLIENT_ID_NOT_EXPECTED", result.reasons)

    def test_changed_when_stable_but_not_the_expected_id(self):
        result = evaluate_reporting_persistence(
            expected_master_client_id=EXPECTED,
            before=_obs(11111, at=BEFORE_AT),
            after=_obs(11111, at=AFTER_AT, post_restart=True),
        )
        self.assertIs(result.verdict, ReportingVerdict.CHANGED)
        self.assertIn("CLIENT_ID_NOT_EXPECTED", result.reasons)

    def test_unverified_when_after_not_post_restart(self):
        result = evaluate_reporting_persistence(
            expected_master_client_id=EXPECTED,
            before=_obs(EXPECTED, at=BEFORE_AT),
            after=_obs(EXPECTED, at=AFTER_AT, post_restart=False),
        )
        self.assertIs(result.verdict, ReportingVerdict.UNVERIFIED)
        self.assertIn("AFTER_NOT_POST_RESTART", result.reasons)

    def test_unverified_when_a_reading_is_absent(self):
        result = evaluate_reporting_persistence(
            expected_master_client_id=EXPECTED,
            before=_obs(None, at=BEFORE_AT),
            after=_obs(EXPECTED, at=AFTER_AT, post_restart=True),
        )
        self.assertIs(result.verdict, ReportingVerdict.UNVERIFIED)
        self.assertIn("BEFORE_READING_ABSENT", result.reasons)

    def test_unverified_on_account_scope_mismatch(self):
        result = evaluate_reporting_persistence(
            expected_master_client_id=EXPECTED,
            before=_obs(EXPECTED, at=BEFORE_AT),
            after=_obs(EXPECTED, at=AFTER_AT, post_restart=True, account="ibkr-live-ending-7153"),
        )
        self.assertIs(result.verdict, ReportingVerdict.UNVERIFIED)
        self.assertIn("ACCOUNT_SCOPE_MISMATCH", result.reasons)

    def test_unverified_when_after_precedes_before(self):
        result = evaluate_reporting_persistence(
            expected_master_client_id=EXPECTED,
            before=_obs(EXPECTED, at=AFTER_AT),
            after=_obs(EXPECTED, at=BEFORE_AT, post_restart=True),
        )
        self.assertIs(result.verdict, ReportingVerdict.UNVERIFIED)
        self.assertIn("OBSERVATION_ORDER_INVALID", result.reasons)

    def test_never_persisted_without_both_readings_even_if_expected(self):
        # An absent 'after' reading can never yield PERSISTED.
        result = evaluate_reporting_persistence(
            expected_master_client_id=EXPECTED,
            before=_obs(EXPECTED, at=BEFORE_AT),
            after=_obs(None, at=AFTER_AT, post_restart=True),
        )
        self.assertIsNot(result.verdict, ReportingVerdict.PERSISTED)

    def test_invalid_inputs_fail_closed(self):
        with self.assertRaises(ReportingPersistenceError):
            evaluate_reporting_persistence(expected_master_client_id=-1,
                                           before=_obs(EXPECTED, at=BEFORE_AT),
                                           after=_obs(EXPECTED, at=AFTER_AT, post_restart=True))
        with self.assertRaises(ReportingPersistenceError):
            ReportingObservation(account_binding=ACCOUNT, master_client_id=EXPECTED,
                                 observed_at=datetime(2026, 9, 18, 14, 0))  # naive


if __name__ == "__main__":
    unittest.main()
