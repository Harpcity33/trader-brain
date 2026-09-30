"""Branding and private remote ingress regressions. No network providers or real accounts."""
from html.parser import HTMLParser
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock
from apps.control_center.server import ASSETS, WEB, serve
from apps.control_center.store import Store
from apps.control_center.transport import Unavailable

ORIGIN = 'https://trader-brain.tail1234.ts.net'

class PrivateIngress(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = Mock()
        self.engine.store = Store(Path(self.tmp.name)/'control.sqlite3')
        self.server = serve(self.engine, 'local-test-token-'*3, host='127.0.0.1', port=0, origin=ORIGIN, tailnet_proxy=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.tmp.cleanup()
    def request(self, path, body=None, **headers):
        connection = http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        values={'Host': 'trader-brain.tail1234.ts.net', 'Origin': ORIGIN, 'Content-Type':'application/json'}
        values.update(headers)
        connection.request('POST' if body is not None else 'GET',path,body=json.dumps(body) if body is not None else None,headers=values)
        result=connection.getresponse(); answer=(result.status,dict(result.headers),result.read()); connection.close(); return answer
    def test_remote_login_cookie_is_secure(self):
        status,headers,_=self.request('/api/login',{'token':'local-test-token-'*3})
        self.assertEqual(status,200)
        for attr in ('Secure','HttpOnly','SameSite=Strict'): self.assertIn(attr,headers['Set-Cookie'])
    def test_remote_api_requires_session(self): self.assertEqual(self.request('/api/status')[0],401)
    def test_remote_hostname_required(self): self.assertEqual(self.request('/',Host='127.0.0.1:8766')[0],403)
    def test_forwarded_header_cannot_bypass_hostname(self):
        self.assertEqual(self.request('/',**{'Host':'evil.test','X-Forwarded-Host':'trader-brain.tail1234.ts.net'})[0],403)
    def test_remote_origin_required(self): self.assertEqual(self.request('/api/login',{'token':'local-test-token-'*3},Origin='https://evil.test')[0],403)
    def test_real_brand_assets_served(self):
        for path in ('/brand-mark.png','/apple-touch-icon.png','/icon-192.png','/deck.js'):
            with self.subTest(path=path): self.assertEqual(self.request(path)[0],200)
    def test_token_never_in_public_assets(self): self.assertNotIn(b'local-test-token-',self.request('/')[2])
    def test_controls_still_require_csrf(self):
        _,headers,_=self.request('/api/login',{'token':'local-test-token-'*3})
        self.assertEqual(self.request('/api/command',{'action':'resume','id':'x'},Cookie=headers['Set-Cookie'].split(';')[0])[0],403)

class ProxyValidation(unittest.TestCase):
    def test_unsafe_remote_origins_refused(self):
        origins=['http://trader-brain.tail1234.ts.net','https://example.com','https://foo.ts.net',
                 'https://a.b.ts.net.evil.test','https://a.b.ts.net:443','https://a.b.ts.net/path',
                 'https://a.b.ts.net?token=x','https://user@a.b.ts.net','https://-a.b.ts.net','https://a.b.ts.net#x']
        for origin in origins:
            with self.subTest(origin=origin), self.assertRaises(Unavailable):
                serve(Mock(),'x'*43,host='127.0.0.1',port=0,origin=origin,tailnet_proxy=True)
    def test_proxy_cannot_listen_on_network_interface(self):
        for host in ('0.0.0.0','192.168.1.1','localhost','::1'):
            with self.subTest(host=host), self.assertRaises(Unavailable):
                serve(Mock(),'x'*43,host=host,port=0,origin=ORIGIN,tailnet_proxy=True)
    def test_https_origin_not_accepted_without_explicit_proxy_mode(self):
        with self.assertRaises(Unavailable): serve(Mock(),'x'*43,port=0,origin=ORIGIN)
    def test_existing_http_loopback_still_valid(self):
        server=serve(Mock(),'x'*43,port=0); server.server_close()

class BrandAssets(unittest.TestCase):
    def test_png_files_are_real(self):
        for name in ('brand-mark.png','icon-192.png','apple-touch-icon.png'):
            self.assertTrue((WEB/name).read_bytes().startswith(b'\x89PNG\r\n\x1a\n'))
    def test_manifest_icons_exist(self):
        manifest=json.loads((WEB/'manifest.webmanifest').read_text())
        for icon in manifest['icons']: self.assertIn(icon['src'],ASSETS)
    def test_source_logo_not_publicly_routed(self): self.assertNotIn('/brand-original.png',ASSETS)
    def test_application_elements_unique_and_complete(self):
        import re
        class IDs(HTMLParser):
            def __init__(self): super().__init__(); self.ids=[]
            def handle_starttag(self, tag, attrs):
                fields=dict(attrs)
                if 'id' in fields: self.ids.append(fields['id'])
        parser=IDs(); parser.feed((WEB/'index.html').read_text())
        self.assertEqual(len(parser.ids), len(set(parser.ids)))
        for element in re.findall(r"\$\('([^']+)'\)",(WEB/'app.js').read_text()):
            self.assertIn(element,parser.ids)
    def test_comparison_scope_is_explicit(self):
        source=(WEB/'index.html').read_text()
        self.assertIn('COMPARISON PAPER WORKSPACE',source)
        self.assertIn('These controls do not operate the original paper service.',source)
    def test_reduced_motion_supported(self):
        self.assertIn('prefers-reduced-motion:reduce',(WEB/'app.css').read_text())
    def test_private_account_endpoints_not_cached(self):
        script=(WEB/'sw.js').read_text()
        shell=script.split('const SHELL=',1)[1].split(';',1)[0]
        self.assertNotIn('/api/',shell)

if __name__ == '__main__': unittest.main()
