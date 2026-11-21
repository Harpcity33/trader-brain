"""Synthetic observations only; never connect to a broker or submit an order."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest
from titan_brain.pilot_validation import (
    AccountObservation, CandidateObservation, EvidenceError, PilotLimits,
    evaluate, fresh, number, reconcile_order,
)

NOW = datetime(2026, 9, 30, 14, 0, tzinfo=timezone.utc)
ACCOUNT = 'test-only-agentic-account'


def account(**updates):
    defaults = AccountObservation(
        account_number=ACCOUNT, observed_at=NOW, accessible=True, active=True,
        options_approved=True, buying_power='500.00', unleveraged_buying_power='500.00',
        account_equity='500.00', realized_week_pnl='0', open_and_pending_premium_risk='0',
        all_assets_reconciled=True, cash_flows_reconciled=True, all_pages_read=True,
        unknown_order_delivery=False,
    )
    return replace(defaults, **updates)


def candidate(**updates):
    defaults = CandidateObservation(
        option_id='test-option', quote_option_id='test-option', quote_at=NOW,
        bid='.44', ask='.45', limit_price='.45', planned_stop='.36', quantity=1,
        multiplier=100, bid_size=20, ask_size=20, days_to_expiration=9,
        source='robinhood_mcp', quote_is_real_time=True, contract_is_tradable=True,
        contract_type='call', signal_at=NOW-timedelta(seconds=20), signal_confirmed=True,
        market_confirmed_open=True,
    )
    return replace(defaults, **updates)


def order(**updates):
    return dict(account_number=ACCOUNT, option_id='test-option', ref_id='logical-order-1',
                order_id='broker-order-1', state='confirmed', quantity='2',
                filled_quantity='0', average_price='0', **updates)


def reconcile(previous, observation):
    return reconcile_order(previous, observation, expected_account=ACCOUNT,
                           expected_option_id='test-option', expected_ref_id='logical-order-1')


class PilotBudgetTests(unittest.TestCase):
    def report(self, a=None, c=None):
        return evaluate(ACCOUNT, a or account(), c or candidate(), NOW)

    def test_pass_is_not_live_authorization(self):
        result = self.report()
        self.assertEqual(result.status, 'OBSERVATIONS_PASS_NOT_LIVE_READY')
        self.assertFalse(result.live_submission_enabled)
        self.assertEqual(result.capital_ceiling, '500.00')
        self.assertEqual(result.weekly_loss_ceiling, '50.00')

    def test_full_premium_and_costs_reserved(self):
        result = self.report()
        self.assertEqual(number(result.full_premium_and_cost_reserve), number('46.30'))
        self.assertEqual(number(result.planned_stop_loss_and_cost), number('10.30'))

    def test_unfunded_account_stays_blocked(self):
        result = self.report(account(buying_power='0', unleveraged_buying_power='0', account_equity='0'))
        self.assertIn('ACCOUNT_UNFUNDED', result.reasons)
        self.assertIn('INSUFFICIENT_VERIFIED_FUNDS', result.reasons)

    def test_paper_balance_cannot_satisfy_wrong_live_account(self):
        self.assertIn('ACCOUNT_MISMATCH', self.report(account(account_number='paper')).reasons)

    def test_no_default_account_when_expected_missing(self):
        result = evaluate('', account(), candidate(), NOW)
        self.assertIn('ACCOUNT_MISMATCH', result.reasons)

    def test_margin_buying_power_does_not_replace_unleveraged_cash(self):
        result = self.report(account(buying_power='2000', unleveraged_buying_power='10'))
        self.assertIn('INSUFFICIENT_VERIFIED_FUNDS', result.reasons)

    def test_stop_is_not_worst_case_loss(self):
        result = self.report(c=candidate(bid='1.99', ask='2', limit_price='2', planned_stop='1.90'))
        self.assertIn('FULL_PREMIUM_EXCEEDS_WEEKLY_CAPACITY', result.reasons)
        self.assertNotIn('PLANNED_LOSS_EXCEEDS_CAP', result.reasons)

    def test_existing_losses_reduce_weekly_budget(self):
        result = self.report(account(realized_week_pnl='-20'))
        self.assertEqual(number(result.remaining_weekly_capacity), 30)
        self.assertIn('FULL_PREMIUM_EXCEEDS_WEEKLY_CAPACITY', result.reasons)

    def test_open_and_pending_exposure_cannot_be_reused(self):
        result = self.report(account(open_and_pending_premium_risk='10'))
        self.assertEqual(number(result.remaining_weekly_capacity), 40)
        self.assertIn('FULL_PREMIUM_EXCEEDS_WEEKLY_CAPACITY', result.reasons)

    def test_profits_do_not_expand_cap(self):
        self.assertEqual(number(self.report(account(realized_week_pnl='200')).remaining_weekly_capacity), 50)

    def test_more_deposits_do_not_expand_cap(self):
        result = self.report(account(buying_power='5000', unleveraged_buying_power='5000', account_equity='5000'))
        self.assertEqual(number(result.remaining_weekly_capacity), 50)
        self.assertEqual(number(result.capital_ceiling), 500)

    def test_loss_at_or_beyond_weekly_limit(self):
        for loss in ('-50', '-100'):
            with self.subTest(loss=loss):
                result = self.report(account(realized_week_pnl=loss))
                self.assertEqual(number(result.remaining_weekly_capacity), 0)
                self.assertEqual(result.status, 'BLOCKED')

    def test_fees_push_50_dollar_premium_over_budget(self):
        result = self.report(c=candidate(bid='.49', ask='.50', limit_price='.50', planned_stop='.40'))
        self.assertIn('FULL_PREMIUM_EXCEEDS_WEEKLY_CAPACITY', result.reasons)

    def test_contract_size_and_quantity_not_inferred(self):
        for changes in ({'quantity':2}, {'quantity':True}, {'multiplier':150}, {'multiplier':True}):
            with self.subTest(changes=changes):
                self.assertEqual(self.report(c=candidate(**changes)).status, 'BLOCKED')

    def test_negative_and_nan_numbers_fail_closed(self):
        for value in ('NaN', 'Infinity', '-1', None, True):
            with self.subTest(value=value):
                self.assertEqual(self.report(account(buying_power=value)).status, 'BLOCKED')

    def test_increased_limits_rejected(self):
        for changes in ({'capital':'501'}, {'weekly_loss':'51'}, {'planned_trade_loss':'16'},
                        {'maximum_contracts':2}, {'modeled_round_trip_cost':'0'}):
            with self.subTest(changes=changes), self.assertRaises(EvidenceError):
                PilotLimits(**changes)

    def test_lower_cap_supported_with_corresponding_limits(self):
        limits = PilotLimits(capital='100', weekly_loss='10', planned_trade_loss='3')
        self.assertEqual(limits.capital, '100')


class MarketAndAccountTests(unittest.TestCase):
    def reasons(self, a=None, c=None):
        return evaluate(ACCOUNT, a or account(), c or candidate(), NOW).reasons

    def test_stale_account(self):
        self.assertIn('ACCOUNT_OBSERVATION_STALE', self.reasons(account(observed_at=NOW-timedelta(seconds=16))))

    def test_future_quotes_not_promoted_to_now(self):
        self.assertIn('OPTION_QUOTE_STALE', self.reasons(c=candidate(quote_at=NOW+timedelta(seconds=1))))

    def test_stale_quotes(self):
        self.assertIn('OPTION_QUOTE_STALE', self.reasons(c=candidate(quote_at=NOW-timedelta(seconds=31))))

    def test_naive_time_rejected(self):
        self.assertFalse(fresh(NOW.replace(tzinfo=None), NOW, 30))

    def test_expired_signal(self):
        self.assertIn('SIGNAL_NOT_FRESH_AND_CONFIRMED', self.reasons(c=candidate(signal_at=NOW-timedelta(seconds=181))))

    def test_incomplete_candle(self):
        self.assertIn('SIGNAL_NOT_FRESH_AND_CONFIRMED', self.reasons(c=candidate(signal_confirmed=False)))

    def test_account_read_access_and_approval_required(self):
        for changes in ({'accessible':False}, {'active':False}, {'options_approved':False}):
            with self.subTest(changes=changes): self.assertTrue(self.reasons(account(**changes)))

    def test_all_pages_and_other_assets_required(self):
        for field in ('all_assets_reconciled', 'cash_flows_reconciled', 'all_pages_read'):
            with self.subTest(field=field):
                self.assertIn('RECONCILIATION_INCOMPLETE', self.reasons(account(**{field:False})))

    def test_truthy_strings_not_booleans(self):
        self.assertIn('ACCOUNT_NOT_ACCESSIBLE', self.reasons(account(accessible='true')))
        self.assertIn('UNKNOWN_ORDER_DELIVERY', self.reasons(account(unknown_order_delivery='false')))

    def test_unknown_delivery_keeps_gate_closed(self):
        self.assertIn('UNKNOWN_ORDER_DELIVERY', self.reasons(account(unknown_order_delivery=True)))

    def test_wrong_contract(self):
        self.assertIn('CONTRACT_IDENTITY_MISMATCH', self.reasons(c=candidate(quote_option_id='other')))

    def test_delayed_indicative_or_unverified_feed(self):
        for changes in ({'quote_is_real_time':False}, {'source':'indicative'}, {'source':'paper_estimate'}):
            with self.subTest(changes=changes):
                self.assertIn('EXECUTABLE_QUOTE_NOT_VERIFIED', self.reasons(c=candidate(**changes)))

    def test_missing_depth_and_bad_books(self):
        for changes in ({'bid_size':0}, {'ask_size':0}, {'bid':'.46'}, {'bid':'0'}, {'ask':'NaN'}):
            with self.subTest(changes=changes): self.assertTrue(self.reasons(c=candidate(**changes)))

    def test_spread_gate(self):
        self.assertIn('SPREAD_TOO_WIDE', self.reasons(c=candidate(bid='.10')))

    def test_expiration_gate(self):
        for dte in (0, 6, 22, True):
            with self.subTest(dte=dte):
                self.assertIn('EXPIRY_OUTSIDE_PILOT', self.reasons(c=candidate(days_to_expiration=dte)))

    def test_regular_session_required(self):
        self.assertIn('MARKET_NOT_CONFIRMED_OPEN', self.reasons(c=candidate(market_confirmed_open=False)))

    def test_readiness_report_does_not_echo_account_number(self):
        self.assertNotIn(ACCOUNT, repr(evaluate(ACCOUNT, account(), candidate(), NOW)))


class OrderEvidenceTests(unittest.TestCase):
    def observation(self, **updates):
        record=order();record.update(updates);return record

    def test_acknowledgement_is_not_a_fill(self):
        result=reconcile(None, order())
        self.assertEqual(result['incremental_filled_quantity'], '0')
        self.assertFalse(result['terminal_confirmed'])
        self.assertTrue(result['retain_pending_reserve'])

    def test_partial_fill_retains_unfilled_reserve(self):
        result=reconcile(None, self.observation(state='partially_filled', filled_quantity='1', average_price='.45'))
        self.assertEqual(result['remaining_quantity'], '1')
        self.assertTrue(result['retain_pending_reserve'])
        self.assertTrue(result['requires_position_reconciliation'])

    def test_duplicate_fill_does_not_double_count(self):
        first=reconcile(None, self.observation(state='filled', filled_quantity='2', average_price='.45'))
        second=reconcile(first, self.observation(state='filled', filled_quantity='2', average_price='.45'))
        self.assertEqual(first['incremental_filled_quantity'], '2')
        self.assertEqual(second['incremental_filled_quantity'], '0')

    def test_cancel_request_not_confirmation(self):
        result=reconcile(None, self.observation(state='pending_cancelled'))
        self.assertFalse(result['terminal_confirmed'])
        self.assertTrue(result['retain_pending_reserve'])

    def test_cancel_fill_race_observed_as_fill(self):
        first=reconcile(None, self.observation(state='pending_cancelled'))
        result=reconcile(first, self.observation(state='filled', filled_quantity='2', average_price='.45'))
        self.assertEqual(result['incremental_filled_quantity'], '2')
        self.assertTrue(result['requires_position_reconciliation'])

    def test_partial_cancel_still_leaves_position(self):
        first=reconcile(None, self.observation(state='partially_filled', filled_quantity='1', average_price='.45'))
        result=reconcile(first, self.observation(state='cancelled', filled_quantity='1', average_price='.45'))
        self.assertTrue(result['terminal_confirmed'])
        self.assertFalse(result['retain_pending_reserve'])
        self.assertTrue(result['requires_position_reconciliation'])

    def test_terminal_order_cannot_be_reopened(self):
        first=reconcile(None, self.observation(state='cancelled'))
        with self.assertRaises(EvidenceError): reconcile(first, order())

    def test_decreasing_fill_count_rejected(self):
        first=reconcile(None, self.observation(state='partially_filled', filled_quantity='1', average_price='.45'))
        with self.assertRaises(EvidenceError): reconcile(first, order())

    def test_wrong_account_contract_or_logical_order_rejected(self):
        for field in ('account_number','option_id','ref_id'):
            with self.subTest(field=field), self.assertRaises(EvidenceError):
                reconcile(None, self.observation(**{field:'wrong'}))

    def test_same_ref_cannot_change_broker_order_id(self):
        previous=reconcile(None, order())
        with self.assertRaises(EvidenceError): reconcile(previous,self.observation(order_id='different'))

    def test_bad_or_missing_data_rejected(self):
        records=[self.observation(state='mystery'), self.observation(state='filled'),
                 self.observation(filled_quantity='3',average_price='.45'),
                 self.observation(filled_quantity='.5',average_price='.45'),
                 self.observation(quantity='NaN'), self.observation(order_id='')]
        incomplete=order();del incomplete['option_id'];records.append(incomplete)
        for record in records:
            with self.subTest(record=record), self.assertRaises(EvidenceError): reconcile(None,record)

    def test_nonzero_price_without_fill_rejected(self):
        with self.assertRaises(EvidenceError): reconcile(None,self.observation(average_price='.45'))

    def test_unknown_partial_semantics_rejected(self):
        with self.assertRaises(EvidenceError): reconcile(None,self.observation(state='partially_filled',filled_quantity='0'))

    def test_report_is_copy_not_mutation_of_broker_input(self):
        original=order(); result=reconcile(None, original)
        self.assertNotIn('incremental_filled_quantity', original)
        self.assertIn('incremental_filled_quantity', result)


if __name__ == '__main__':
    unittest.main()
