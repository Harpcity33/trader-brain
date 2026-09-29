"""Offline provider routing and authenticated-transport boundary tests."""
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from titan_brain.robinhood_options import (
    RobinhoodOptions, OptionsUnavailable, validate_quote, probe_robinhood_options,
    PaperMarketData, option_symbol, normalized_snapshot, instruments, quoted_snapshots,
)
from titan_brain.paper_options_engine import contract_from_snapshot

NOW = datetime(2026, 9, 28, 15, 0, tzinfo=timezone.utc)


def instrument(**updates):
    return {**dict(id='contract', chain_id='chain', chain_symbol='SPY', type='call',
                   expiration_date='2026-10-09', strike_price='500.0000',
                   trade_value_multiplier='100', state='active', tradability='tradable'), **updates}


def quote(**updates):
    return {**dict(instrument_id='contract', bid_price='1.1', ask_price='1.2',
                   bid_size=10, ask_size=10, open_interest=500, volume=100,
                   delta='.5', gamma='.1', theta='-.2', vega='.3', implied_volatility='.2',
                   updated_at=NOW.isoformat()), **updates}


class RobinhoodTests(unittest.TestCase):
    def test_forbidden_actions_never_reach_transport(self):
        client = RobinhoodOptions()
        client._request = Mock()
        for tool in ('place_option_order', 'review_option_order', 'cancel_option_order',
                     'get_option_positions', 'get_option_orders', 'create_alert', 'add_to_watchlist'):
            with self.assertRaises(OptionsUnavailable):
                client.call(tool, {})
        client._request.assert_not_called()

    def test_missing_mismatched_nan_and_future_quotes_rejected(self):
        for updates in ({'instrument_id': 'other'}, {'delta': None}, {'delta': 'nan'},
                        {'updated_at': (NOW + timedelta(minutes=2)).isoformat()}):
            with self.subTest(updates=updates), self.assertRaises(OptionsUnavailable):
                validate_quote(quote(**updates), 'contract', NOW)

    def test_stale_zero_and_crossed_quotes_are_not_entry_usable(self):
        for updates in ({'bid_price': '0'}, {'ask_price': '1.0'},
                        {'updated_at': (NOW - timedelta(days=2)).isoformat()}):
            self.assertFalse(validate_quote(quote(**updates), 'contract', NOW)['quote_usable_for_entry'])
        self.assertTrue(validate_quote(quote(), 'contract', NOW)['quote_usable_for_entry'])

    def test_readiness_probe_reads_only_market_data(self):
        client = Mock()
        client.call.side_effect = [
            {'chains': [{'symbol': 'SPY', 'id': 'chain', 'expiration_dates': ['2026-10-09']}]},
            {'instruments': [instrument()]}, {'results': [{'quote': quote()}]},
        ]
        self.assertTrue(probe_robinhood_options(500, client=client, now=NOW)['ok'])
        self.assertEqual([x.args[0] for x in client.call.call_args_list],
                         ['get_option_chains', 'get_option_instruments', 'get_option_quotes'])

    def test_transport_error_content_is_not_logged(self):
        client = RobinhoodOptions()
        client._request = Mock(return_value={'isError': True, 'content': [{'text': 'secret'}]})
        with self.assertRaises(OptionsUnavailable) as caught:
            client.call('get_option_chains', {})
        self.assertNotIn('secret', str(caught.exception))

    def test_timeout_fails_closed(self):
        client = RobinhoodOptions(timeout=0)
        client._send = Mock()
        with self.assertRaisesRegex(OptionsUnavailable, 'timed out'):
            client._request('initialize', {})

    def test_adapter_preserves_contract_identity_greeks_and_timestamp(self):
        snapshot = normalized_snapshot(instrument(), quote(), 'SPY')
        contract = contract_from_snapshot(snapshot, 'SPY', NOW.date())
        self.assertIsNotNone(contract)
        self.assertEqual(contract.ticker, 'O:SPY261009C00500000')
        self.assertEqual(contract.delta, .5)
        self.assertEqual(contract.quote_timestamp_ns, int(NOW.timestamp() * 1e9))
        self.assertEqual(contract.multiplier, 100)

    def test_adjusted_multiplier_remains_rejected_by_engine(self):
        snapshot = normalized_snapshot(instrument(trade_value_multiplier='150'), quote(), 'SPY')
        self.assertIsNone(contract_from_snapshot(snapshot, 'SPY', NOW.date()))

    def test_wrong_underlying_or_quote_id_rejected(self):
        for contract, value in ((instrument(chain_symbol='QQQ'), quote()), (instrument(), quote(instrument_id='wrong'))):
            with self.assertRaises(OptionsUnavailable):
                normalized_snapshot(contract, value, 'SPY')

    def test_pagination_keeps_filters_and_rejects_repeated_cursor(self):
        client = Mock()
        client.call.side_effect = [{'instruments': [instrument()], 'next': 'cursor'}, {'instruments': [], 'next': 'cursor'}]
        with self.assertRaisesRegex(OptionsUnavailable, 'Repeated'):
            instruments(client, {'chain_id': 'chain'})
        self.assertEqual(client.call.call_args.args[1], {'chain_id': 'chain', 'cursor': 'cursor'})

    def test_missing_quote_does_not_become_a_fabricated_snapshot(self):
        client = Mock()
        client.call.return_value = {'results': []}
        with self.assertRaises(OptionsUnavailable):
            quoted_snapshots(client, [instrument()], 'SPY')

    def test_chain_routes_stock_reference_and_options_to_separate_providers(self):
        stocks, client = Mock(), Mock()
        stocks.previous_bar.return_value = {'c': 500}
        client.call.side_effect = [
            {'chains': [{'symbol': 'SPY', 'id': 'chain', 'expiration_dates': ['2026-10-09']}]},
            {'instruments': []}, {'instruments': [instrument()]}, {'instruments': []},
            {'results': [{'quote': quote()}]},
        ]
        context = Mock()
        context.__enter__ = Mock(return_value=client)
        context.__exit__ = Mock(return_value=False)
        feed = PaperMarketData(stocks, lambda: context)
        result = feed.option_chain('SPY', date(2026, 10, 5), date(2026, 10, 19))
        self.assertEqual(result[0]['provider'], 'Robinhood')
        stocks.option_chain.assert_not_called()
        stocks.option_snapshot.assert_not_called()
        stocks.previous_bar.assert_called_once_with('SPY')

    def test_exact_snapshot_resolves_existing_occ_ledger_identifiers(self):
        stocks, client = Mock(), Mock()
        client.call.side_effect = [{'instruments': [instrument()]}, {'results': [{'quote': quote()}]}]
        context = Mock()
        context.__enter__ = Mock(return_value=client)
        context.__exit__ = Mock(return_value=False)
        result = PaperMarketData(stocks, lambda: context).option_snapshot('SPY', option_symbol(instrument()))
        self.assertEqual(result['results']['instrument_id'], 'contract')
        self.assertEqual(client.call.call_args_list[0].args[1]['strike_price'], '500')
        stocks.option_snapshot.assert_not_called()

    def test_provider_failure_never_falls_back_to_massive_options(self):
        stocks = Mock()
        stocks.previous_bar.return_value = {'c': 500}
        feed = PaperMarketData(stocks, Mock(side_effect=OptionsUnavailable('AUTH_REQUIRED')))
        with self.assertRaises(OptionsUnavailable):
            feed.option_chain('SPY', date(2026, 10, 5), date(2026, 10, 19))
        stocks.option_chain.assert_not_called()
        stocks.option_snapshot.assert_not_called()

    def test_missing_candidate_quote_excluded_without_inventing_data(self):
        client = Mock()
        client.call.return_value = {'results': [{'quote': quote()}]}
        result = quoted_snapshots(client, [instrument(), instrument(id='missing')], 'SPY')
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['instrument_id'], 'contract')

    def test_normalized_robinhood_data_drives_actual_paper_entry(self):
        import test_paper_runtime_acceptance as acceptance
        from titan_brain import paper_options_engine as engine
        r = acceptance.r
        now = acceptance.NOW
        cfg, settings = r.load_config()
        state = engine.default_state(1000, now)
        stock = acceptance.Feed(now)
        contract = instrument(chain_symbol='TEST', strike_price='100.0000')
        q = quote(bid_price='.78', ask_price='.8', delta='.55', updated_at=now.isoformat())
        def call(name, args):
            if name == 'get_option_chains':
                return {'chains': [{'symbol': 'TEST', 'id': 'chain', 'expiration_dates': ['2026-10-09']}]}
            if name == 'get_option_instruments':
                return {'instruments': [contract] if float(args['strike_price']) == 100 else []}
            if name == 'get_option_quotes':
                return {'results': [{'quote': q}]}
            raise AssertionError('Forbidden broker call')
        client = Mock()
        client.call.side_effect = call
        context = Mock()
        context.__enter__ = Mock(return_value=client)
        context.__exit__ = Mock(return_value=False)
        feed = PaperMarketData(stock, lambda: context)
        result = r.maybe_enter(feed, cfg, settings, state, acceptance.signal(), now, clock=lambda: now)
        self.assertIsNotNone(result)
        self.assertEqual(len(state['positions']), 1)
        self.assertEqual(result['contract'], 'O:TEST261009C00100000')
        self.assertEqual(state['outbox'][0]['kind'], 'BUY')
        # Only a local paper entry and unsent notification were created.
        self.assertEqual(state['outbox'][0]['status'], 'pending')

    def test_configuration_enforces_paper_authority_and_provider_routing(self):
        import test_paper_runtime_acceptance as acceptance
        r = acceptance.r
        cfg, engine = r.load_config()
        for update in ({'broker_write_authority': True}, {'broker_write_authority': None},
                       {'market_data': {'stocks': 'Massive', 'options': 'Massive'}}):
            with self.subTest(update=update), patch.object(Path, 'read_text', side_effect=[json.dumps({**cfg, **update}), json.dumps(engine)]):
                with self.assertRaises(ValueError):
                    r.load_config()

    def test_doctor_checks_robinhood_without_massive_options_or_email_send(self):
        import test_paper_runtime_acceptance as acceptance
        from unittest.mock import MagicMock
        r = acceptance.r
        stock = Mock()
        stock.previous_bar.return_value = {'c': 500}
        for failure in (False, True):
            with patch.object(r, 'massive_client', return_value=stock), patch.object(r, 'probe_robinhood_options') as options, patch.object(r, 'require_env', return_value='test-self'), patch.object(r.smtplib, 'SMTP_SSL', return_value=MagicMock()), patch.object(r, 'send_gmail') as send:
                options.return_value = {'ok': True}
                if failure:
                    options.side_effect = OptionsUnavailable('AUTH_REQUIRED')
                result = r.doctor()
                self.assertEqual(result['status'], 'NOT_READY' if failure else 'DEPENDENCIES_OK')
                self.assertIn('robinhood_options', result['checks'])
                stock.option_chain.assert_not_called()
                stock.option_snapshot.assert_not_called()
                send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
