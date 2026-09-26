"""Optional pinned official SDK wire test, with all sockets forbidden.

Set TITAN_TEST_IBKR_SDK_ROOT to the attested SDK's site-packages directory.
The SDK is imported only by an isolated, bounded subprocess, never the suite.
"""

import os
from pathlib import Path
import subprocess
import sys
import unittest


_HARNESS = r'''
import sys, logging, struct
from unittest.mock import patch
sys.path[:0] = [sys.argv[1], sys.argv[2]]
logging.disable(logging.CRITICAL)
import ibapi
from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.decoder import Decoder
from ibapi.execution import ExecutionFilter
from ibapi.message import OUT
from ibapi.protobuf.AccountUpdatesMultiRequest_pb2 import AccountUpdatesMultiRequest
from ibapi.protobuf.CancelAccountUpdatesMulti_pb2 import CancelAccountUpdatesMulti
from ibapi.protobuf.AccountUpdateMulti_pb2 import AccountUpdateMulti
from ibapi.protobuf.AccountUpdateMultiEnd_pb2 import AccountUpdateMultiEnd
from ibapi.protobuf.ExecutionRequest_pb2 import ExecutionRequest
from titan_brain.live.broker.ibkr_runtime import _OfficialReadRequester
from titan_brain.live.broker.ibkr_read import IbkrWholeAccountReadBridge
assert ibapi.__version__ == '10.50.2'
ACCOUNT = 'U1234567'

class Sink:
    def __init__(self):
        self.frames = []; self.subscriptions = set(); self.callbacks = None; self.decoder = None
    def isConnected(self): return True
    def sendMsg(self, payload):
        length, msgid = struct.unpack('!II', payload[:8])
        assert length == len(payload) - 4 and msgid >= 200
        kind, body, cb = msgid - 200, payload[8:], self.callbacks
        assert kind not in (OUT.REQ_ACCOUNT_SUMMARY, OUT.CANCEL_ACCOUNT_SUMMARY)
        request = None
        if kind == OUT.REQ_ACCOUNT_UPDATES_MULTI:
            row = AccountUpdatesMultiRequest.FromString(body); request = row.reqId
            assert row.account == ACCOUNT and row.modelCode == '' and row.ledgerAndNLV is False
            assert not self.subscriptions and request not in {r for k,r in self.frames if k == kind}
            self.subscriptions.add(request)
            for key,value,currency in [('AccountType','CASH',''),('NetLiquidation','10000','USD'),('TotalCashValue','10000','USD'),('AvailableFunds','10000','USD'),('BuyingPower','10000','USD'),('SettledCash','10000','USD')]:
                proto = AccountUpdateMulti(reqId=request, account=ACCOUNT, modelCode='', key=key, value=value, currency=currency)
                self.decoder.processAccountUpdateMultiMsgProtoBuf(proto.SerializeToString())
            self.decoder.processAccountUpdateMultiEndMsgProtoBuf(AccountUpdateMultiEnd(reqId=request).SerializeToString())
        elif kind == OUT.CANCEL_ACCOUNT_UPDATES_MULTI:
            request = CancelAccountUpdatesMulti.FromString(body).reqId
            assert request in self.subscriptions
            self.subscriptions.remove(request)
        elif kind == OUT.REQ_POSITIONS: cb.positionEnd()
        elif kind == OUT.CANCEL_POSITIONS: pass
        elif kind == OUT.REQ_ALL_OPEN_ORDERS: cb.openOrderEnd()
        elif kind == OUT.REQ_COMPLETED_ORDERS: cb.completedOrdersEnd()
        elif kind == OUT.REQ_EXECUTIONS:
            request = ExecutionRequest.FromString(body).reqId; cb.execDetailsEnd(request)
        else: raise AssertionError('unexpected request, including any trading operation')
        self.frames.append((kind,request)); return len(payload)

with patch('socket.socket', side_effect=AssertionError('sockets forbidden')):
    wrapper = EWrapper(); client = EClient(wrapper); client.serverVersion_ = 223
    client.setConnState(EClient.CONNECTED); sink = Sink(); client.conn = sink
    requester = object.__new__(_OfficialReadRequester); requester._client = client
    bridge = IbkrWholeAccountReadBridge(requester=requester, exact_account_id=ACCOUNT, account_masked='****4567', execution_filter_factory=ExecutionFilter, timeout_seconds=0.2)
    cb = bridge.open_generation(1); sink.callbacks = cb
    wrapper.accountUpdateMulti = cb.accountUpdateMulti; wrapper.accountUpdateMultiEnd = cb.accountUpdateMultiEnd
    wrapper.accountUpdateMultiProtoBuf = lambda payload: None
    wrapper.accountUpdateMultiEndProtoBuf = lambda payload: None
    wrapper.error = cb.error; sink.decoder = Decoder(wrapper, 223); cb.managedAccounts(ACCOUNT)
    for _ in range(5):
        facts = bridge.collect_session_facts()
        assert facts.account_values_source == 'IBKR_ACCOUNT_UPDATES_MULTI_V1'
        assert 'account_updates_multi' in facts.completed_reads
        assert not sink.subscriptions and not bridge.sanitized_errors and bridge._last is None
    assert len(sink.frames) == 35
print('PINNED_SDK_ACCOUNT_UPDATES_WIRE_PASS')
'''


class OfficialSdkAccountUpdatesWireTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("TITAN_TEST_IBKR_SDK_ROOT"), "optional pinned SDK path not supplied")
    def test_repeated_finite_reads_real_protobuf_request_cancel_and_decoder(self):
        root = Path(os.environ["TITAN_TEST_IBKR_SDK_ROOT"]).resolve(strict=True)
        source = Path(__file__).resolve().parents[1] / "src"
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-B", "-c", _HARNESS, str(root), str(source)],
            capture_output=True, text=True, timeout=10, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "PINNED_SDK_ACCOUNT_UPDATES_WIRE_PASS")


if __name__ == "__main__":
    unittest.main()
