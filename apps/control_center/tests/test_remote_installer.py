"""Exercise installer plist generation only; never run launchctl or network calls."""
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest

class NetworkPreservation(unittest.TestCase):
    def test_fresh_and_existing_private_network_settings(self):
        root=Path(__file__).resolve().parents[3]
        script=(root/'deploy/install_control_center_macos.sh').read_text()
        code=script.split("<<'PY'\n",1)[1].split('\nPY\n',1)[0]
        settings=['--host','0.0.0.0','--port','8765','--origin','https://192.168.1.10:8765',
                  '--cert','/private/server.crt','--key','/private/server.key',
                  '--tailnet-origin','https://test.tail1234.ts.net']
        for existing in (False,True):
            with self.subTest(existing=existing),tempfile.TemporaryDirectory() as folder:
                path=Path(folder)/'control.plist'
                if existing:
                    path.write_bytes(plistlib.dumps({'ProgramArguments':['/python','-B','-m','apps.control_center','serve']+settings}))
                result=subprocess.run([sys.executable,'-',str(root),folder,str(path)],input=code,text=True,capture_output=True)
                self.assertEqual(result.returncode,0,result.stderr)
                args=plistlib.loads(path.read_bytes())['ProgramArguments']
                self.assertEqual(args[5:],settings if existing else [])
                self.assertEqual(path.stat().st_mode&0o777,0o600)
