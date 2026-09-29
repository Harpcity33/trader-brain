"""Tests against the actual baseline when running inside the repository (CI)."""
import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from apps.control_center.bridge import Engine, load_runtime
from apps.control_center.store import Store

ROOT=Path(__file__).resolve().parents[3]
BASE=ROOT/'tests/test_paper_runtime_acceptance.py'

@unittest.skipUnless(BASE.is_file(), 'Full repository required; exercised by GitHub CI')
class BaselineIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0,str(ROOT/'src'))
        spec=importlib.util.spec_from_file_location('tb_control_fixtures',BASE)
        cls.fixture=importlib.util.module_from_spec(spec);sys.modules[spec.name]=cls.fixture;spec.loader.exec_module(cls.fixture)

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.home=Path(self.tmp.name)
        self.store=Store(self.home/'control.sqlite3');self.sent=[]
        self.rt=load_runtime(ROOT)
        # The shared baseline fixture intentionally omits 1m exit observations.
        # Supply them here so a repeat heartbeat tests duplication, not a data fault.
        class CompleteFeed(self.fixture.Feed):
            def bars(self, ticker, minutes, day):
                if minutes == 1:
                    end=self.now.replace(second=0,microsecond=0)
                    return [dict(t=int((end-timedelta(minutes=1)).timestamp()*1000),
                                 o=101.2,h=101.5,l=101.1,c=101.4,v=1000,vw=101.3)]
                return super().bars(ticker, minutes, day)
        self.engine=Engine(ROOT,self.home,self.store,runtime=self.rt,feed=CompleteFeed(),sender=lambda *a:self.sent.append(a))
        self.engine.rules=copy.deepcopy(self.engine.rules);self.engine.rules['universe']=['TEST']
        self.now=self.fixture.NOW
    def tearDown(self):self.tmp.cleanup()

    def tick(self):self.engine.tick(self.now,clock=lambda:self.now)
    def test_weekend_offline(self):
        self.engine.feed=Mock();sat=self.now-timedelta(days=2)
        self.engine.tick(sat,clock=lambda:sat);self.engine.feed.market_status.assert_not_called()
        self.assertTrue(self.engine.path.is_file());self.assertFalse(self.engine.error)
    def test_default_pause_no_entry(self):
        self.tick();state=json.loads(self.engine.path.read_text());self.assertFalse(state['positions'])
    def test_resume_creates_paper_entry(self):
        self.store.enqueue('resume-test','resume');self.tick()
        state=json.loads(self.engine.path.read_text());self.assertEqual(len(state['positions']),1);self.assertEqual(len(self.sent),1)
    def test_heartbeat_no_duplicate(self):
        self.store.enqueue('resume-test','resume');self.tick();self.tick()
        state=json.loads(self.engine.path.read_text());self.assertEqual(len(state['positions']),1);self.assertEqual(len(self.sent),1)
    def test_manual_flatten_real_quote_not_mark(self):
        self.store.enqueue('resume-test','resume');self.tick();self.store.enqueue('close-test','flatten');self.tick()
        state=json.loads(self.engine.path.read_text());self.assertFalse(state['positions']);self.assertEqual(len(state['closed_trades']),1)
        self.assertTrue(self.store.get('paused'));self.assertFalse(self.store.get('flatten_requested'))
    def test_snapshot_no_secret_fields(self):
        self.tick();s=self.engine.snapshot();self.assertFalse(s['live_trading_enabled']);self.assertNotIn('outbox',s)
    def test_state_is_separate(self):self.assertEqual(self.engine.path,self.home/'portfolio.json');self.assertNotEqual(self.engine.path,self.rt.state_path())
    def test_live_config_unchanged(self):
        before=copy.deepcopy(self.engine.cfg);self.tick();self.assertEqual(self.engine.cfg,before)
    def test_commands_survive_restart(self):
        self.store.enqueue('p','pause');self.engine.apply_commands();self.assertEqual(self.store.history()[0]['status'],'applied')
        self.assertTrue(Store(self.store.path).get('paused'))
    def test_resume_refused_pending_liquidation(self):
        self.store.put('flatten_requested',True);self.store.enqueue('r','resume');self.engine.apply_commands()
        self.assertTrue(self.store.get('paused'));self.assertEqual(self.store.history()[0]['status'],'rejected')

    def test_missing_exit_data_is_a_fault_not_duplicate_buy(self):
        self.store.enqueue('resume-test','resume');self.tick()
        self.engine.feed=self.fixture.Feed();self.tick()
        state=json.loads(self.engine.path.read_text())
        self.assertEqual(len(state['positions']),1)
        self.assertEqual(sum('PAPER BUY' in message[0] for message in self.sent),1)
        self.assertGreater(self.store.get('heartbeat')['data_faults'],0)

    def test_new_install_quiet_during_premarket(self):
        self.engine.feed=Mock();early=self.now.replace(hour=8)
        self.engine.tick(early,clock=lambda:early)
        self.assertFalse(self.engine.feed.market_holidays.called)
        self.assertEqual(self.sent,[])
        self.assertEqual(self.store.get('heartbeat')['decision'],'PAUSED_NOT_STARTED')

    def test_pause_preserves_existing_position_exit(self):
        self.store.enqueue('r','resume');self.tick();self.store.enqueue('p','pause')
        item=self.fixture.snapshot();item['last_quote'].update(bid=.5,ask=.52)
        self.engine.feed.option_snapshot=lambda *a:{'results':item}
        self.tick();state=json.loads(self.engine.path.read_text())
        self.assertEqual(len(state['positions']),0)
        self.assertEqual(state['closed_trades'][0]['exit_reason'],'STOP')
        self.assertTrue(self.store.get('paused'))

    def test_provider_diagnostic_remains_specific_and_safe(self):
        from apps.control_center.transport import Unavailable
        from apps.control_center.bridge import safe_code
        self.engine.feed=None
        with patch.object(self.rt,'massive_client',return_value=Mock()), patch('apps.control_center.bridge.RobinhoodMCP') as m:
            m.return_value.call.side_effect=Unavailable('ROBINHOOD_SIGN_IN_REQUIRED')
            with self.assertRaises(self.rt.DataUnavailable) as caught:
                self.engine.client().option_chain('SPY',self.now.date(),self.now.date())
        self.assertEqual(safe_code(caught.exception),'ROBINHOOD_SIGN_IN_REQUIRED')

    def test_flatten_before_first_resume_with_empty_book(self):
        self.engine.feed=Mock()
        self.store.enqueue('empty-close','flatten');self.tick()
        self.assertFalse(self.store.get('flatten_requested'))
        self.assertEqual(self.store.get('flatten_status'),'NO_OPEN_PAPER_POSITIONS')
        self.assertFalse(self.engine.feed.market_holidays.called)
        self.assertEqual(self.sent,[])
        self.store.enqueue('r','resume');self.engine.apply_commands()
        self.assertFalse(self.store.get('paused'))
