import copy
from datetime import date, datetime, timedelta, timezone
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import Mock

from apps.control_center.transport import RobinhoodMCP, Unavailable, decode_json, rpc_result, trusted_robinhood
from apps.control_center.auth import TokenStore, private_write
from apps.control_center.providers import normalize, symbol_for, HybridFeed
from apps.control_center.server import serve
from apps.control_center.store import Store
from apps.control_center.alpaca import AlpacaPaper, PAPER

NOW=datetime(2026,9,28,14,0,tzinfo=timezone.utc)
REC={"id":"test-contract", "chain_symbol":"TEST", "type":"call", "expiration_date":"2026-10-09",
     "strike_price":"100.0000", "state":"active", "tradability":"tradable", "underlying_type":"equity",
     "trade_value_multiplier":"100.0000"}
QUOTE={"instrument_id":"test-contract","updated_at":NOW.isoformat(),"bid_price":".78","ask_price":".80",
       "bid_size":10,"ask_size":10,"open_interest":500,"volume":200,
       "delta":.55,"gamma":.03,"theta":-.01,"vega":.05,"implied_volatility":.4}

class MappingTests(unittest.TestCase):
    def test_occ_symbol(self): self.assertEqual(symbol_for(REC),'O:TEST261009C00100000')
    def test_fractional_strike(self):
        r={**REC,"strike_price":"100.125"};self.assertTrue(symbol_for(r).endswith('00100125'))
    def test_normalize_full_fields(self):
        n=normalize(REC,QUOTE);self.assertEqual(n['last_quote']['ask'],.8);self.assertEqual(n['greeks']['delta'],.55)
    def test_preserve_source_time(self):
        n=normalize(REC,QUOTE);self.assertEqual(n['last_quote']['last_updated'],int(NOW.timestamp()*1e9))
    def test_identity_rejected(self):
        with self.assertRaises(Unavailable): normalize(REC,{**QUOTE,'instrument_id':'other'})
    def test_nonstandard_multiplier(self):
        with self.assertRaises(Unavailable): normalize({**REC,'trade_value_multiplier':'150'},QUOTE)
    def test_nan_price(self):
        with self.assertRaises(Unavailable): normalize(REC,{**QUOTE,'ask_price':'NaN'})
    def test_infinite_greek(self):
        with self.assertRaises(Unavailable): normalize(REC,{**QUOTE,'delta':'Infinity'})
    def test_missing_greeks_not_invented(self):
        q=dict(QUOTE);del q['vega']
        with self.assertRaises(KeyError): normalize(REC,q)
    def test_crossed_book(self):
        with self.assertRaises(Unavailable): normalize(REC,{**QUOTE,'bid_price':'1'})
    def test_naive_clock(self):
        with self.assertRaises(Unavailable): normalize(REC,{**QUOTE,'updated_at':'2026-09-28T14:00:00'})
    def test_boolean_price(self):
        with self.assertRaises(Unavailable): normalize(REC,{**QUOTE,'ask_price':True})
    def test_negative_volume(self):
        with self.assertRaises(Unavailable): normalize(REC,{**QUOTE,'volume':-1})
    def test_zero_bid_preserved_for_exits(self):
        self.assertEqual(normalize(REC,{**QUOTE,'bid_price':0})['last_quote']['bid'],0)
    def test_invalid_occ_symbol(self):
        with self.assertRaises(Unavailable): symbol_for({**REC,'chain_symbol':'../foo'})

class TransportTests(unittest.TestCase):
    def test_json_rpc(self): self.assertEqual(rpc_result(b'{"id":1,"result":{"ok":true}}','application/json',1),{'ok':True})
    def test_sse_rpc(self): self.assertEqual(rpc_result(b'data: {"id":1,"result":{"ok":true}}\n\n','text/event-stream',1),{'ok':True})
    def test_wrong_rpc_id(self):
        with self.assertRaises(Unavailable):rpc_result(b'{"id":2,"result":{}}','application/json',1)
    def test_rpc_error(self):
        with self.assertRaises(Unavailable):rpc_result(b'{"id":1,"error":{"message":"secret"}}','application/json',1)
    def test_nonfinite_json(self):
        with self.assertRaises(Unavailable):decode_json(b'{"x":NaN}')
    def test_write_block_before_auth(self):
        tokens=Mock();mcp=RobinhoodMCP(tokens)
        for name in ('place_option_order','review_option_order','cancel_option_order','get_accounts','get_equity_quotes'):
            with self.assertRaises(Unavailable):mcp.call(name,{})
        tokens.bearer.assert_not_called()
    def test_allowlist_http_flow(self):
        class Http:
            def exchange(self,url,**k):
                b=k['body'];method=b['method']
                if method=='notifications/initialized':return 202,{},b''
                result=({'protocolVersion':'2025-06-18'} if method=='initialize' else
                        {'tools':[{'name':'get_option_quotes'}]} if method=='tools/list' else
                        {'structuredContent':{'data':{'results':[]}}})
                return 200,{'Content-Type':'application/json'},json.dumps({'id':b['id'],'result':result}).encode()
        tokens=Mock();tokens.bearer.return_value='test-token'
        self.assertEqual(RobinhoodMCP(tokens,Http()).call('get_option_quotes',{'instrument_ids':['x']}),{'results':[]})
    def test_auth_origin_restrictions(self):
        for u in ('http://agent.robinhood.com','https://robinhood.com.evil.test','https://evil.test','https://user@robinhood.com','https://robinhood.com:444'):
            with self.assertRaises(Unavailable):trusted_robinhood(u)
        self.assertEqual(trusted_robinhood('https://agent.robinhood.com/test'),'https://agent.robinhood.com/test')
    def test_missing_tokens(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(Unavailable):TokenStore(Path(d)/'missing').bearer()
    def test_private_token_write(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'tokens';private_write(p,{'access_token':'fake','token_type':'Bearer','expires_at':time.time()+3600})
            self.assertEqual(p.stat().st_mode&0o777,0o600);self.assertEqual(TokenStore(p).bearer(),'fake')
    def test_unsafe_token_permissions(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'tokens';private_write(p,{'access_token':'fake','token_type':'Bearer','expires_at':time.time()+3600});p.chmod(0o644)
            with self.assertRaises(Unavailable):TokenStore(p).bearer()

class StoreTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name)/'control.sqlite')
    def tearDown(self):self.tmp.cleanup()
    def test_default_paused(self):self.assertTrue(self.store.get('paused'))
    def test_idempotency(self):
        a=self.store.enqueue('same','pause');b=self.store.enqueue('same','pause');self.assertEqual(a,b);self.assertEqual(len(self.store.pending()),1)
    def test_conflict(self):
        self.store.enqueue('same','pause')
        with self.assertRaises(ValueError):self.store.enqueue('same','resume')
    def test_unapproved_command(self):
        with self.assertRaises(ValueError):self.store.enqueue('x','live')
    def test_restart_persistence(self):
        self.store.put('paused',False);self.assertFalse(Store(self.store.path).get('paused'))
    def test_private_database(self):self.assertEqual(self.store.path.stat().st_mode&0o777,0o600)

class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.engine=Mock();self.engine.store=Store(Path(self.tmp.name)/'db');self.engine.wake=threading.Event();self.engine.snapshot.return_value={'mode':'PAPER ONLY'}
        self.server=serve(self.engine,'x'*43,port=0,origin='http://127.0.0.1:8765')
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.cookie='';self.csrf=''
    def tearDown(self):self.server.shutdown();self.server.server_close();self.thread.join();self.tmp.cleanup()
    def req(self,path,body=None,headers=None):
        c=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        h={'Host':'127.0.0.1:8765','Origin':'http://127.0.0.1:8765','Content-Type':'application/json','Cookie':self.cookie,'X-TB-CSRF':self.csrf};h.update(headers or {})
        c.request('POST' if body is not None else 'GET',path,body=json.dumps(body) if body is not None else None,headers=h)
        r=c.getresponse();raw=r.read();result=(r.status,dict(r.headers),raw);c.close();return result
    def login(self):
        status,h,raw=self.req('/api/login',{'token':'x'*43});self.assertEqual(status,200);self.cookie=h['Set-Cookie'].split(';')[0];self.csrf=json.loads(raw)['csrf']
    def test_auth_required(self):self.assertEqual(self.req('/api/status')[0],401)
    def test_host_rebinding_blocked(self):self.assertEqual(self.req('/',headers={'Host':'evil.test'})[0],403)
    def test_origin_blocked(self):self.assertEqual(self.req('/api/login',{'token':'x'*43},{'Origin':'https://evil.test'})[0],403)
    def test_authenticated_read(self):self.login();self.assertEqual(self.req('/api/status')[0],200)
    def test_csrf_required(self):
        self.login();self.csrf='';self.assertEqual(self.req('/api/command',{'id':str(uuid.uuid4()),'action':'pause'})[0],403)
    def test_pause_queued(self):
        self.login();status,_,raw=self.req('/api/command',{'id':str(uuid.uuid4()),'action':'pause'});self.assertEqual(status,202);self.assertEqual(json.loads(raw)['status'],'queued')
    def test_flatten_confirmation_required(self):
        self.login();self.assertEqual(self.req('/api/command',{'id':str(uuid.uuid4()),'action':'flatten'})[0],400)
    def test_flatten_confirmation(self):
        self.login();self.assertEqual(self.req('/api/command',{'id':str(uuid.uuid4()),'action':'flatten','confirmation':'CLOSE PAPER POSITIONS'})[0],202)
    def test_live_command_blocked(self):
        self.login();self.assertEqual(self.req('/api/command',{'id':str(uuid.uuid4()),'action':'live'})[0],400)
    def test_no_secrets_in_static(self):
        self.assertNotIn(b'x'*43,self.req('/')[2])
    def test_tls_required_on_lan(self):
        with self.assertRaises(Unavailable):serve(self.engine,'x'*43,host='0.0.0.0',port=0)
    def test_logout(self):self.login();self.assertEqual(self.req('/api/logout',{})[0],200);self.assertEqual(self.req('/api/status')[0],401)
    def test_cache_prevention(self):self.login();self.assertEqual(self.req('/api/status')[1]['Cache-Control'],'no-store')
    def test_path_traversal(self):self.login();self.assertEqual(self.req('/../../etc/passwd')[0],404)

class AlpacaTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.calls=[];outer=self
        class Http:
            def exchange(self,url,**k):
                outer.calls.append((url,k))
                if url.endswith('/account'):r={'id':'paper-test','status':'ACTIVE','options_trading_level':2}
                else:r={'client_order_id':k.get('body',{}).get('client_order_id',''),'status':'accepted'}
                return 200,{},json.dumps(r).encode()
        self.broker=AlpacaPaper('fake-key','fake-secret','paper-test',Path(self.tmp.name)/'orders',http=Http())
        self.kw={'signal_id':'first','symbol':'TEST261009C00100000','limit_price':'.80',
                 'quote':{'source':'robinhood_mcp','indicative':False,'ask':'.80','bid':'.78','updated_at':NOW.isoformat()},
                 'now':NOW,'remaining_weekly_cents':10000}
    def tearDown(self):self.tmp.cleanup()
    def test_paper_origin_only(self):
        self.broker.submit_open(**self.kw);self.assertTrue(all(url.startswith(PAPER+'/') for url,_ in self.calls))
    def test_indicative_blocked(self):
        self.kw['quote']['indicative']=True
        with self.assertRaises(Unavailable):self.broker.submit_open(**self.kw)
        self.assertFalse(self.calls)
    def test_stale_blocked(self):
        self.kw['now']=NOW+timedelta(minutes=5)
        with self.assertRaises(Unavailable):self.broker.submit_open(**self.kw)
    def test_budget_blocked(self):
        self.kw['remaining_weekly_cents']=5000
        with self.assertRaises(Unavailable):self.broker.submit_open(**self.kw)
    def test_order_not_resubmitted(self):
        self.broker.submit_open(**self.kw);self.broker.submit_open(**self.kw)
        self.assertEqual(sum(k.get('method')=='POST' for _,k in self.calls),1)
    def test_shared_pending_budget(self):
        self.broker.submit_open(**self.kw);self.kw['signal_id']='second'
        with self.assertRaises(Unavailable):self.broker.submit_open(**self.kw)
    def test_ambiguous_send_preserved(self):
        old=self.broker.request
        def request(path,method='GET',**kwargs):
            if method=='POST':raise TimeoutError()
            return old(path,method,**kwargs)
        # patch transport instead, preserving the production method signature
        orig=self.broker.http.exchange
        def exchange(url,**kwargs):
            if kwargs.get('method')=='POST':raise TimeoutError()
            return orig(url,**kwargs)
        self.broker.http.exchange=exchange
        with self.assertRaises(Unavailable):self.broker.submit_open(**self.kw)
        result=self.broker.submit_open(**self.kw);self.assertEqual(result['state'],'unknown')

if __name__=='__main__':unittest.main()
