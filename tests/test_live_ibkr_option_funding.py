"""Tests for the option permissions/data/funding evaluator (milestone 3)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from titan_brain.live.broker.ibkr_option_funding import (
    OptionEvidenceError,
    OptionFundingEvidence,
    evaluate_option_funding,
)


NOW = datetime(2026, 10, 5, 16, 0, 5, tzinfo=timezone.utc)


def _evidence(**overrides):
    fields = dict(
        account_currency="USD",
        option_permission_granted=True,
        market_data_entitled=True,
        positions_complete=True,
        pending_orders_complete=True,
        settlement_known=True,
        request_limit_ok=True,
        account_observed_at=NOW - timedelta(seconds=5),
        quote_quoted_at=NOW - timedelta(seconds=2),
        quote_received_at=NOW - timedelta(seconds=1),
        settled_cash="10000",
        reserved_funds="1000",
        pending_order_commitments="500",
        pending_debits_not_in_balances="250",
    )
    fields.update(overrides)
    return OptionFundingEvidence(**fields)


class OptionFundingTests(unittest.TestCase):
    def test_clean_evidence_yields_available_cash(self) -> None:
        result = evaluate_option_funding(_evidence(), now=NOW)
        self.assertTrue(result.ok)
        self.assertEqual(result.blockers, ())
        # 10000 - 1000 - 500 - 250 = 8250
        self.assertEqual(result.available_unborrowed_cash, Decimal("8250"))

    def test_permission_not_granted_blocks(self) -> None:
        r = evaluate_option_funding(_evidence(option_permission_granted=False), now=NOW)
        self.assertFalse(r.ok)
        self.assertIn("OPTION_PERMISSION_NOT_GRANTED", r.blockers)
        self.assertIsNone(r.available_unborrowed_cash)

    def test_permission_truthy_but_not_true_blocks(self) -> None:
        # A truthy non-True value must not pass (strict identity check).
        r = evaluate_option_funding(_evidence(option_permission_granted=1), now=NOW)
        self.assertIn("OPTION_PERMISSION_NOT_GRANTED", r.blockers)

    def test_no_market_data_entitlement_blocks(self) -> None:
        r = evaluate_option_funding(_evidence(market_data_entitled=False), now=NOW)
        self.assertIn("MARKET_DATA_NOT_ENTITLED", r.blockers)

    def test_incomplete_visibility_blocks(self) -> None:
        self.assertIn("POSITIONS_VISIBILITY_INCOMPLETE",
                      evaluate_option_funding(_evidence(positions_complete=False), now=NOW).blockers)
        self.assertIn("PENDING_ORDERS_VISIBILITY_INCOMPLETE",
                      evaluate_option_funding(_evidence(pending_orders_complete=False), now=NOW).blockers)

    def test_unknown_settlement_blocks(self) -> None:
        r = evaluate_option_funding(_evidence(settlement_known=False), now=NOW)
        self.assertIn("SETTLEMENT_UNKNOWN", r.blockers)

    def test_request_limit_failure_blocks(self) -> None:
        r = evaluate_option_funding(_evidence(request_limit_ok=False), now=NOW)
        self.assertIn("REQUEST_LIMIT_OR_COLLECTION_FAILED", r.blockers)

    def test_stale_account_balances_block(self) -> None:
        r = evaluate_option_funding(_evidence(account_observed_at=NOW - timedelta(seconds=120)), now=NOW)
        self.assertIn("ACCOUNT_BALANCES_STALE", r.blockers)

    def test_future_account_observation_blocks(self) -> None:
        r = evaluate_option_funding(_evidence(account_observed_at=NOW + timedelta(seconds=5)), now=NOW)
        self.assertIn("ACCOUNT_OBSERVATION_IN_FUTURE", r.blockers)

    def test_stale_quote_blocks(self) -> None:
        r = evaluate_option_funding(
            _evidence(quote_quoted_at=NOW - timedelta(seconds=90),
                      quote_received_at=NOW - timedelta(seconds=88)),
            now=NOW,
        )
        self.assertIn("QUOTE_STALE", r.blockers)

    def test_future_quote_blocks(self) -> None:
        r = evaluate_option_funding(
            _evidence(quote_quoted_at=NOW + timedelta(seconds=2),
                      quote_received_at=NOW + timedelta(seconds=3)),
            now=NOW,
        )
        self.assertIn("QUOTE_OBSERVATION_IN_FUTURE", r.blockers)

    def test_quote_chronology_invalid_blocks(self) -> None:
        r = evaluate_option_funding(
            _evidence(quote_quoted_at=NOW - timedelta(seconds=1),
                      quote_received_at=NOW - timedelta(seconds=5)),
            now=NOW,
        )
        self.assertIn("QUOTE_CHRONOLOGY_INVALID", r.blockers)

    def test_unknown_funding_facts_block(self) -> None:
        self.assertIn("SETTLED_CASH_UNKNOWN",
                      evaluate_option_funding(_evidence(settled_cash=None), now=NOW).blockers)
        self.assertIn("RESERVED_FUNDS_UNKNOWN",
                      evaluate_option_funding(_evidence(reserved_funds=None), now=NOW).blockers)
        self.assertIn("PENDING_ORDER_COMMITMENTS_UNKNOWN",
                      evaluate_option_funding(_evidence(pending_order_commitments=None), now=NOW).blockers)

    def test_insufficient_unborrowed_cash_blocks(self) -> None:
        r = evaluate_option_funding(
            _evidence(settled_cash="1000", reserved_funds="900",
                      pending_order_commitments="200", pending_debits_not_in_balances="0"),
            now=NOW,
        )
        self.assertIn("INSUFFICIENT_UNBORROWED_CASH", r.blockers)
        self.assertIsNone(r.available_unborrowed_cash)

    def test_negative_funding_fact_blocks(self) -> None:
        r = evaluate_option_funding(_evidence(reserved_funds="-5"), now=NOW)
        self.assertIn("NEGATIVE_FUNDING_FACT", r.blockers)

    def test_non_usd_blocks(self) -> None:
        r = evaluate_option_funding(_evidence(account_currency="EUR"), now=NOW)
        self.assertIn("ACCOUNT_CURRENCY_UNSUPPORTED", r.blockers)

    def test_float_money_rejected(self) -> None:
        with self.assertRaises(OptionEvidenceError):
            evaluate_option_funding(_evidence(settled_cash=10000.0), now=NOW)

    def test_naive_now_rejected(self) -> None:
        with self.assertRaises(OptionEvidenceError):
            evaluate_option_funding(_evidence(), now=datetime(2026, 10, 5, 16, 0, 5))

    def test_multiple_blockers_accumulate_and_dedup(self) -> None:
        r = evaluate_option_funding(
            _evidence(option_permission_granted=False, market_data_entitled=False,
                      settlement_known=False),
            now=NOW,
        )
        self.assertIn("OPTION_PERMISSION_NOT_GRANTED", r.blockers)
        self.assertIn("MARKET_DATA_NOT_ENTITLED", r.blockers)
        self.assertIn("SETTLEMENT_UNKNOWN", r.blockers)
        self.assertEqual(len(r.blockers), len(set(r.blockers)))


if __name__ == "__main__":
    unittest.main()
