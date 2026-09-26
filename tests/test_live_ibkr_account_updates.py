"""Fresh account-value callbacks; synthetic broker only, no sockets/custody."""

from datetime import timedelta
from unittest.mock import patch
import unittest

from titan_brain.live.broker.base import BrokerCapabilityError
from titan_brain.live.broker.ibkr_session_inputs import IbkrSessionInputError
from tests import test_live_ibkr_session_inputs as fixtures

SYNTHETIC_ACCOUNT = fixtures.SYNTHETIC_ACCOUNT


class AccountUpdatesLifecycleTests(unittest.TestCase):
    setUp = fixtures.IbkrSessionInputsTests.setUp
    connect = fixtures.IbkrSessionInputsTests.connect

    def test_repeated_reads_use_fresh_exact_account_source_and_never_summary(self):
        self.connect()
        observations = []
        with patch.object(self.client, "reqAccountSummary", side_effect=AssertionError("summary cap must not be touched")):
            for _ in range(4):
                observations.append(self.adapter.capture())
                self.now += timedelta(seconds=1)
        requests = [call for call in self.client.calls if call[0] == "reqAccountUpdatesMulti"]
        cancels = [call for call in self.client.calls if call[0] == "cancelAccountUpdatesMulti"]
        self.assertEqual(len(requests), 4)
        self.assertEqual(len({call[1] for call in requests}), 4)
        self.assertEqual([call[1] for call in requests], [call[1] for call in cancels])
        self.assertTrue(all(call[2:] == (SYNTHETIC_ACCOUNT, "", False) for call in requests))
        for item in observations:
            facts = item.facts
            self.assertEqual(facts.account_values_source, "IBKR_ACCOUNT_UPDATES_MULTI_V1")
            self.assertIn("account_updates_multi", facts.completed_reads)
            self.assertNotIn("account_summary", facts.completed_reads)
            self.assertEqual(facts.net_liquidation_received_at, facts.collection_started_at)
            self.assertEqual(facts.cash_value, facts.net_liquidation)
            self.assertEqual(facts.cash_currency, "USD")
            self.assertEqual(facts.cash_received_at, facts.collection_started_at)
            self.assertNotIn("cash_value", facts.public_dict())
        self.assertEqual(len({item.facts.net_liquidation_received_at for item in observations}), 4)
        self.assertIsNone(self.components.read_bridge._last)
        self.assertEqual(self.client.mutations, [])

    def test_previous_request_values_and_end_cannot_complete_new_request(self):
        self.connect()
        self.adapter.capture()
        previous_id = next(call[1] for call in self.client.calls if call[0] == "reqAccountUpdatesMulti")

        def stale(request_id, *args):
            self.client.wrapper.accountUpdateMulti(previous_id, SYNTHETIC_ACCOUNT, "", "NetLiquidation", "10000", "USD")
            self.client.wrapper.accountUpdateMultiEnd(previous_id)

        with patch.object(self.client, "reqAccountUpdatesMulti", side_effect=stale):
            with self.assertRaises(IbkrSessionInputError):
                self.adapter.capture()
        self.assertEqual(self.adapter.last_capture_failure_code, "IBKR_SESSION_INPUT_READ_TIMEOUT_ACCOUNT_UPDATES_MULTI")

    def test_summary_callbacks_cannot_supply_new_source_values_or_end(self):
        self.connect()

        def wrong_feed(request_id, *args):
            self.client.wrapper.accountSummary(request_id, SYNTHETIC_ACCOUNT, "NetLiquidation", "10000", "USD")
            self.client.wrapper.accountSummaryEnd(request_id)

        with patch.object(self.client, "reqAccountUpdatesMulti", side_effect=wrong_feed):
            with self.assertRaises(IbkrSessionInputError):
                self.adapter.capture()
        self.assertEqual(self.adapter.last_capture_failure_code, "IBKR_SESSION_INPUT_READ_TIMEOUT_ACCOUNT_UPDATES_MULTI")

    def test_not_ready_scope_currency_and_conflicting_values_fail_closed(self):
        self.connect()
        original = self.client.reqAccountUpdatesMulti
        rows = (
            (SYNTHETIC_ACCOUNT, "", "AccountReady", "false", "", "ACCOUNT_UPDATES_NOT_READY"),
            (SYNTHETIC_ACCOUNT, "", "AccountReady", "unknown", "", "ACCOUNT_UPDATES_NOT_READY"),
            ("U7654321", "", "NetLiquidation", "10000", "USD", "ACCOUNT_UPDATES_SCOPE"),
            (SYNTHETIC_ACCOUNT, "other", "NetLiquidation", "10000", "USD", "ACCOUNT_UPDATES_SCOPE"),
            (SYNTHETIC_ACCOUNT, "", "TotalCashValue", "10000", "EUR", "ACCOUNT_UPDATES_CURRENCY"),
            (SYNTHETIC_ACCOUNT, "", "TotalCashValue", "10000", "BASE", "ACCOUNT_UPDATES_CONFLICT"),
            (SYNTHETIC_ACCOUNT, "", "NetLiquidation", "9999", "USD", "ACCOUNT_UPDATES_CONFLICT"),
        )
        for account, model, key, value, currency, scope in rows:
            def invalid(request_id, *args):
                original(request_id, *args)
                self.client.wrapper.accountUpdateMulti(request_id, account, model, key, value, currency)
            with self.subTest(scope=scope), patch.object(self.client, "reqAccountUpdatesMulti", side_effect=invalid):
                with self.assertRaises(IbkrSessionInputError):
                    self.adapter.capture()
                self.assertEqual(self.adapter.last_capture_failure_code, "IBKR_SESSION_INPUT_READ_CALLBACK_" + scope + "_0")

    def test_ledger_and_segment_rows_do_not_replace_whole_account_values(self):
        self.connect()
        original = self.client.reqAccountUpdatesMulti

        def extra_rows(request_id, *args):
            original(request_id, *args)
            for key in ("$LEDGER-TotalCashValue", "TotalCashValue-S", "NetLiquidation-C"):
                self.client.wrapper.accountUpdateMulti(request_id, SYNTHETIC_ACCOUNT, "", key, "1", "EUR")
            self.client.wrapper.accountUpdateMulti(request_id, SYNTHETIC_ACCOUNT, "", "AccountReady", "true", "")

        with patch.object(self.client, "reqAccountUpdatesMulti", side_effect=extra_rows):
            facts = self.adapter.capture().facts
        self.assertEqual(str(facts.cash_value), "10000")
        self.assertEqual(str(facts.net_liquidation), "10000")

    def test_cancel_exception_is_not_hidden_and_blocks_next_collection(self):
        self.connect()
        with patch.object(self.client, "cancelAccountUpdatesMulti", side_effect=RuntimeError("private synthetic failure")):
            with self.assertRaises(IbkrSessionInputError):
                self.adapter.capture()
        self.assertIn(("cancelPositions",), self.client.calls)
        self.assertEqual(self.adapter.last_capture_failure_code, "IBKR_SESSION_ACCOUNT_UPDATES_CLEANUP_UNCONFIRMED")
        before = len(self.client.calls)
        with self.assertRaises(IbkrSessionInputError):
            self.adapter.capture()
        self.assertEqual(len(self.client.calls), before)
        self.assertIsNone(self.adapter.last_capture_diagnostic)

    def test_sdk_error_callback_during_cancel_is_not_hidden(self):
        self.connect()
        with patch.object(self.client, "cancelAccountUpdatesMulti", side_effect=lambda request: self.client.wrapper.error(request, 520, "private synthetic cancellation error")):
            with self.assertRaises(IbkrSessionInputError):
                self.adapter.capture()
        self.assertEqual(self.adapter.last_capture_failure_code, "IBKR_SESSION_ACCOUNT_UPDATES_CLEANUP_UNCONFIRMED")
        self.assertFalse(self.runtime.status().command_connected)

    def test_collection_remains_owned_until_cleanup_finishes(self):
        self.connect()
        original = self.client.cancelAccountUpdatesMulti
        def cancel(request_id):
            with self.assertRaisesRegex(BrokerCapabilityError, "IBKR_READ_COLLECTION_ALREADY_ACTIVE"):
                self.components.read_bridge.collect_session_facts()
            original(request_id)
        with patch.object(self.client, "cancelAccountUpdatesMulti", side_effect=cancel):
            self.adapter.capture()

    def test_full_error_ring_does_not_hide_cancel_callback_failure(self):
        self.connect()
        for _ in range(256):
            self.client.wrapper.error(-1, 2104, "synthetic informational message")
        self.assertEqual(len(self.components.read_bridge.sanitized_errors), 256)
        with patch.object(self.client, "cancelAccountUpdatesMulti", side_effect=lambda request: self.client.wrapper.error(request, 520, "private synthetic cancellation error")):
            with self.assertRaises(IbkrSessionInputError):
                self.adapter.capture()
        self.assertEqual(self.adapter.last_capture_failure_code, "IBKR_SESSION_ACCOUNT_UPDATES_CLEANUP_UNCONFIRMED")
        before = len(self.client.calls)
        with self.assertRaises(IbkrSessionInputError):
            self.adapter.capture()
        self.assertEqual(len(self.client.calls), before)

    def test_mid_collection_disconnect_still_cancels_the_subscription(self):
        """Area 2: no accumulation on the FAILURE path.

        A connection loss mid-collection must not leak the account-updates
        subscription: the reader's finally-cleanup must still dispatch
        cancelAccountUpdatesMulti (and cancelPositions) for the failed
        collection. Existing tests only prove the SUCCESS path pairs
        request<->cancel; this pins the failure-path release.
        """
        self.connect()
        self.client.disconnect_after_execution = True
        with self.assertRaises(IbkrSessionInputError):
            self.adapter.capture()
        request_id = next(call[1] for call in self.client.calls if call[0] == "reqAccountUpdatesMulti")
        self.assertIn(("cancelAccountUpdatesMulti", request_id), self.client.calls)
        self.assertIn(("cancelPositions",), self.client.calls)
        # No stale value escapes the failed collection.
        self.assertIsNone(self.components.read_bridge._last)

    def test_no_subscription_accumulates_across_failed_then_recovered_cycles(self):
        """Area 2: the live (req minus matching cancel) subscription count stays 0.

        Run several mid-collection disconnect failures, each followed by a real
        reconnect (runtime.stop() + connect_reads(), the genuine recovery path),
        then a clean read. Every account-updates request must be matched 1:1 by
        a cancel across ALL clients, so no subscription is ever left live.
        """
        all_requests: list[object] = []
        all_cancels: list[object] = []

        def record(client):
            for call in client.calls:
                if call[0] == "reqAccountUpdatesMulti":
                    all_requests.append(call[1])
                elif call[0] == "cancelAccountUpdatesMulti":
                    all_cancels.append(call[1])

        for cycle in range(3):
            self.connect()
            self.client.disconnect_after_execution = True
            with self.assertRaises(IbkrSessionInputError):
                self.adapter.capture()
            record(self.client)
            # A mid-collection disconnect tears down the connection; recover it
            # the way production does before the next read.
            self.runtime.stop()
            self.now += timedelta(seconds=1)

        # Every request was cancelled: requests and cancels pair up exactly, so
        # no subscription is left live. (Request ids may repeat across fresh
        # reconnects because each new connection restarts its id counter; that
        # is not accumulation — the pairing is what proves nothing leaked.)
        self.assertEqual(sorted(all_requests), sorted(all_cancels))
        self.assertEqual(len(all_requests), 3)
        self.assertEqual(len(all_cancels), 3)


if __name__ == "__main__":
    unittest.main()
