"""Synthetic-only acceptance cases. No brokerage account or network access."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from titan_brain.lifecycle_lab import (
    Journal, ObservationError, OrderEvidence, daily_budget, protection_review,
)

NOW = datetime(2026, 9, 30, 14, 0, tzinfo=timezone.utc)
ACCOUNT = "synthetic-account-only"
CONTRACT = "SYNTH261009C00100000"


def evidence(**updates):
    e = OrderEvidence(ACCOUNT, "open-1", "broker-1", CONTRACT, "buy", 2,
                      0, "0", "0", "accepted", NOW)
    return replace(e, **updates)


class OrderLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "review.sqlite3"
        self.book = Journal(self.path, account=ACCOUNT)
        self.book.expect("open-1", CONTRACT, "buy", 2)
    def tearDown(self): self.tmp.cleanup()
    def observed(self, **updates):
        event = evidence(**updates)
        return self.book.observe(event, now=event.observed_at)
    def recover(self, positions, **updates):
        return self.book.recover(**dict(account=ACCOUNT, positions=positions,
            observed_at=NOW, now=NOW, complete=True, **updates))
    def test_private_database(self): self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
    def test_account_not_stored_as_plaintext(self): self.assertNotIn(ACCOUNT.encode(), self.path.read_bytes())
    def test_other_account_cannot_open_database(self):
        with self.assertRaisesRegex(ObservationError, "DATABASE_ACCOUNT_MISMATCH"):
            Journal(self.path, account="another-synthetic-account")
    def test_unsafe_existing_permissions_refused(self):
        self.path.chmod(0o644)
        with self.assertRaisesRegex(ObservationError, "PERMISSIONS"):
            Journal(self.path, account=ACCOUNT)
    def test_reference_registration_is_idempotent(self):
        self.book.expect("open-1", CONTRACT, "buy", 2)
        self.assertEqual(len(self.book.snapshot()["expected"]), 1)
    def test_changed_reference_rejected(self):
        with self.assertRaisesRegex(ObservationError, "REFERENCE_REUSE"):
            self.book.expect("open-1", CONTRACT, "buy", 1)
    def test_acknowledgement_is_not_fill(self):
        report = self.observed()
        self.assertEqual(report["incremental_filled"], 0)
        self.assertEqual(self.recover({})["observed_fill_holdings"][CONTRACT], 0)
    def test_duplicate_does_not_double_count(self):
        args = dict(filled=1, notional="45", state="partially_filled")
        self.observed(**args)
        self.assertTrue(self.observed(**args)["duplicate"])
        self.assertEqual(self.book.snapshot()["evidence_events"], 1)
        self.assertEqual(self.recover({CONTRACT: 1})["observed_fill_holdings"][CONTRACT], 1)
    def test_new_timestamp_same_fill_is_zero_increment(self):
        self.observed(filled=1, notional="45", state="partially_filled")
        report = self.observed(filled=1, notional="45", state="partially_filled", observed_at=NOW+timedelta(seconds=1))
        self.assertEqual(report["incremental_filled"], 0)
    def test_cancelled_remainder_does_not_erase_filled_contract(self):
        self.observed(filled=1, notional="45", state="partially_filled")
        self.observed(filled=1, notional="45", state="cancelled", observed_at=NOW+timedelta(seconds=1))
        report = self.book.recover(account=ACCOUNT, positions={CONTRACT:1}, observed_at=NOW+timedelta(seconds=2), now=NOW+timedelta(seconds=2), complete=True)
        self.assertEqual(report["observed_fill_holdings"][CONTRACT], 1)
        self.assertEqual(report["working_orders"], [])
    def test_pending_cancel_still_reserves_unfilled_quantity(self):
        result = self.observed(filled=1, notional="45", state="pending_cancel")
        self.assertEqual(result["pending_quantity"], 1)
        self.assertEqual(len(self.recover({CONTRACT:1})["working_orders"]), 1)
    def test_fill_can_win_pending_cancel_race(self):
        self.observed(filled=1, notional="45", state="pending_cancel")
        result = self.observed(filled=2, notional="91", state="filled", observed_at=NOW+timedelta(seconds=1))
        self.assertEqual(result["incremental_filled"], 1)
    def test_terminal_change_requires_review(self):
        self.observed(state="cancelled")
        with self.assertRaisesRegex(ObservationError, "TERMINAL_CORRECTION"):
            self.observed(filled=2, notional="91", state="filled", observed_at=NOW+timedelta(seconds=1))
        self.assertEqual(self.book.snapshot()["latest"][0]["filled"], 0)
    def test_out_of_order_evidence_preserves_previous_record(self):
        self.observed(observed_at=NOW+timedelta(seconds=1))
        with self.assertRaisesRegex(ObservationError, "ORDERING"):
            self.observed(state="cancelled")
        self.assertEqual(self.book.snapshot()["evidence_events"], 1)
    def test_unexplained_fill_price_change_is_not_silently_applied(self):
        self.observed(filled=1, notional="45", state="partially_filled")
        with self.assertRaisesRegex(ObservationError, "ECONOMIC_CORRECTION"):
            self.observed(filled=1, notional="46", state="partially_filled", observed_at=NOW+timedelta(seconds=1))
    def test_cumulative_regression_rejected(self):
        self.observed(filled=1, notional="45", state="partially_filled")
        with self.assertRaisesRegex(ObservationError, "REGRESSION"):
            self.observed(observed_at=NOW+timedelta(seconds=1))
    def test_changed_order_id_refused(self):
        self.observed()
        with self.assertRaisesRegex(ObservationError, "BROKER_ID_CHANGED"):
            self.observed(broker_id="other", observed_at=NOW+timedelta(seconds=1))
    def test_order_id_cannot_belong_to_two_references(self):
        self.observed()
        self.book.expect("second", CONTRACT, "buy", 2)
        with self.assertRaisesRegex(ObservationError, "BROKER_ID_REUSED"):
            self.observed(ref="second")
    def test_bad_account_contract_side_or_quantity(self):
        for changes in ({"account":"other"}, {"contract":"OTHER"}, {"side":"sell"}, {"requested":1}):
            with self.subTest(changes=changes), self.assertRaises(ObservationError): self.observed(**changes)
    def test_bad_numbers(self):
        for changes in ({"filled":True},{"notional":"nan"},{"fees":"-1"},{"notional":"Infinity"},{"filled":3},{"filled":1}):
            with self.subTest(changes=changes), self.assertRaises(ObservationError): self.observed(**changes)
    def test_bad_state_fill_combinations(self):
        for changes in ({"state":"unknown"},{"state":"filled"},{"state":"partially_filled"}, {"state":"rejected","filled":1,"notional":"45"}):
            with self.subTest(changes=changes), self.assertRaises(ObservationError): self.observed(**changes)
    def test_stale_and_future_observations(self):
        for delta in (-16, 1):
            with self.subTest(delta=delta), self.assertRaisesRegex(ObservationError, "STALE"):
                self.book.observe(evidence(observed_at=NOW+timedelta(seconds=delta)),now=NOW)
    def test_naive_clock_rejected(self):
        with self.assertRaisesRegex(ObservationError, "AWARE_TIMESTAMP"):
            self.book.observe(evidence(),now=NOW.replace(tzinfo=None))
    def test_unknown_delivery_survives_restart_without_retransmission(self):
        reopened=Journal(self.path,account=ACCOUNT)
        result=reopened.recover(account=ACCOUNT,positions={},observed_at=NOW,now=NOW,complete=True)
        self.assertIn("UNKNOWN_ORDER_DELIVERY", result["issues"])
        self.assertFalse(result["automatic_resubmission"])
        self.assertEqual(result["broker_actions"],0)
    def test_fills_survive_restart(self):
        self.observed(filled=2,notional="91",state="filled")
        reopened=Journal(self.path,account=ACCOUNT)
        result=reopened.recover(account=ACCOUNT,positions={CONTRACT:2},observed_at=NOW,now=NOW,complete=True)
        self.assertEqual(result["status"],"RECONCILED_NOT_EXECUTION_AUTHORITY")
    def test_missing_position_is_mismatch_not_imagined_sale(self):
        self.observed(filled=2,notional="91",state="filled")
        self.assertIn("POSITION_MISMATCH",self.recover({})["issues"])
    def test_external_position_is_not_ignored(self):
        self.assertIn("POSITION_MISMATCH", self.recover({"OTHER":1})["issues"])
    def test_incomplete_snapshot_cannot_assert_flat(self):
        for complete in (False,"true"):
            result=self.book.recover(account=ACCOUNT,positions={},observed_at=NOW,now=NOW,complete=complete)
            self.assertEqual(result["status"],"REVIEW_REQUIRED")
            self.assertNotIn("observed_fill_holdings",result)
    def test_concurrent_duplicate_events(self):
        event=evidence(filled=2,notional="90",state="filled")
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _:self.book.observe(event,now=NOW),range(4)))
        self.assertEqual(sum(r["incremental_filled"] for r in results),2)
        self.assertEqual(self.book.snapshot()["evidence_events"],1)
    def test_closing_fill_changes_observed_holdings_only(self):
        self.observed(filled=2,notional="90",state="filled")
        self.book.expect("close-1",CONTRACT,"sell",2)
        self.observed(ref="close-1",broker_id="broker-close",side="sell",filled=2,notional="94",state="filled")
        self.assertEqual(self.recover({})["observed_fill_holdings"][CONTRACT],0)
    def test_unexplained_short_is_reviewed(self):
        self.book.expect("sell",CONTRACT,"sell",1)
        self.observed(ref="sell",broker_id="sell1",side="sell",requested=1,filled=1,notional="45",state="filled")
        self.assertIn("UNEXPLAINED_SHORT_EXPOSURE",self.recover({})["issues"])


class ProtectionReview(unittest.TestCase):
    def setUp(self):
        self.kw=dict(contract=CONTRACT,position_qty=1,position_at=NOW,quote_contract=CONTRACT,
                     bid="0.50",ask="0.52",bid_size=5,quote_at=NOW,stop="0.40",target="0.70",
                     close_cutoff=NOW+timedelta(hours=5),now=NOW,
                     closing_order_pending=False,working_protection_confirmed=True)
    def review(self,**updates): return protection_review(**{**self.kw,**updates})
    def test_stop_observed_without_placing_order(self):
        result=self.review(bid="0.39")
        self.assertIn("STOP_THRESHOLD_OBSERVED",result["reasons"])
        self.assertFalse(result["position_closed"]);self.assertFalse(result["protection_order_placed"])
        self.assertEqual(result["broker_actions"],0)
    def test_target_observed(self): self.assertIn("TARGET_THRESHOLD_OBSERVED",self.review(bid=".71",ask=".73")["reasons"])
    def test_no_trigger(self): self.assertEqual(self.review()["status"],"NO_TRIGGER_OBSERVED")
    def test_unverified_protection_never_called_protected(self):
        self.assertIn("WORKING_PROTECTION_NOT_VERIFIED",self.review(working_protection_confirmed=False)["reasons"])
    def test_stale_quote_not_exit(self):
        self.assertIn("EXIT_QUOTE_UNUSABLE",self.review(quote_at=NOW-timedelta(seconds=31))["reasons"])
    def test_wrong_contract_depth_crossed_and_nonfinite(self):
        for kw in ({"quote_contract":"OTHER"},{"bid_size":0},{"bid":"1"},{"ask":"NaN"}):
            with self.subTest(kw=kw): self.assertIn("EXIT_QUOTE_UNUSABLE",self.review(**kw)["reasons"])
    def test_closeout_review_survives_stale_quote(self):
        result=self.review(close_cutoff=NOW,quote_at=NOW-timedelta(seconds=31))
        self.assertIn("CLOSEOUT_TIME_REACHED",result["reasons"])
        self.assertIn("EXIT_QUOTE_UNUSABLE",result["reasons"])
    def test_pending_close_prevents_competing_action(self):
        self.assertIn("RECONCILE_EXISTING_CLOSE_BEFORE_ANOTHER",self.review(closing_order_pending=True)["reasons"])
    def test_zero_bid_not_promoted_to_penny(self):
        self.assertIn("STOP_THRESHOLD_OBSERVED",self.review(bid="0")["reasons"])
    def test_no_position(self): self.assertEqual(self.review(position_qty=0)["status"],"NO_POSITION_OBSERVED")
    def test_stale_position_not_assumed_flat(self):
        result=self.review(position_qty=0,position_at=NOW-timedelta(seconds=16))
        self.assertEqual(result["status"],"REVIEW_REQUIRED")
    def test_strict_boolean(self):
        with self.assertRaisesRegex(ObservationError,"BOOLEAN"):
            self.review(working_protection_confirmed="true")


class DailyPreference(unittest.TestCase):
    def report(self,**kwargs):
        return daily_budget(**{**dict(starting_equity="500",current_equity="500",net_external_flows="0"),**kwargs})
    def test_500_means_25_and_50(self):
        result=self.report()
        self.assertEqual(result["daily_loss_threshold"],"25.00")
        self.assertEqual(result["daily_gain_goal"],"50.00")
    def test_loss_includes_open_equity(self): self.assertTrue(self.report(current_equity="475")["loss_threshold_reached"])
    def test_loss_not_hit(self): self.assertFalse(self.report(current_equity="480")["loss_threshold_reached"])
    def test_profit_goal_not_a_guarantee_or_order(self):
        result=self.report(current_equity="550")
        self.assertTrue(result["gain_goal_reached"])
        self.assertFalse(result["goal_is_guaranteed"])
        self.assertEqual(result["broker_actions"],0)
    def test_deposit_is_not_profit(self):
        result=self.report(current_equity="600",net_external_flows="100")
        self.assertEqual(result["flow_adjusted_pnl"],"0")
        self.assertFalse(result["gain_goal_reached"])
    def test_withdrawal_is_not_loss(self): self.assertEqual(self.report(current_equity="400",net_external_flows="-100")["flow_adjusted_pnl"],"0")
    def test_no_prior_weekly_or_per_trade_constraints(self):
        result=self.report()
        self.assertNotIn("weekly_loss",result);self.assertNotIn("planned_trade_loss",result)
    def test_next_day_uses_new_start(self):
        result=self.report(starting_equity="475",current_equity="475")
        self.assertEqual(result["daily_loss_threshold"],"23.75")
    def test_zero_nan_bool_start_rejected(self):
        for value in ("0","NaN",True):
            with self.subTest(value=value), self.assertRaises(ObservationError): self.report(starting_equity=value)


if __name__=="__main__": unittest.main()
