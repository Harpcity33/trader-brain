"""Bounded HTTPS/Streamable HTTP transports. No model, broker-write or shell calls."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import threading
from typing import Any
from urllib import request, error, parse

ROBINHOOD = "https://agent.robinhood.com/mcp/trading"
READ_TOOLS = frozenset({"get_option_chains", "get_option_instruments", "get_option_quotes"})

class Unavailable(RuntimeError):
    """Only fixed, credential-free codes may be exposed to the UI or log."""

class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise Unavailable("REDIRECT_REFUSED")

def trusted_robinhood(url: str) -> str:
    p = parse.urlsplit(url)
    if (p.scheme != "https" or not p.hostname or p.username or p.password or p.fragment
            or p.port not in (None, 443)
            or not (p.hostname == "robinhood.com" or p.hostname.endswith(".robinhood.com"))):
        raise Unavailable("UNTRUSTED_AUTH_ORIGIN")
    return url

def decode_json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"), parse_constant=lambda x: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError):
        raise Unavailable("INVALID_JSON") from None

class HTTPS:
    def __init__(self, timeout: float = 12):
        self.opener = request.build_opener(NoRedirect())
        self.timeout = timeout

    def exchange(self, url: str, *, method="GET", headers=None, body=None):
        p = parse.urlsplit(url)
        if p.scheme != "https" or p.username or p.password:
            raise Unavailable("HTTPS_REQUIRED")
        h = dict(headers or {})
        expected_id = body.get("id") if isinstance(body, dict) else None
        if body is not None and not isinstance(body, bytes):
            body = json.dumps(body, allow_nan=False).encode()
            h["Content-Type"] = "application/json"
        req = request.Request(url, data=body, headers=h, method=method)
        try:
            with self.opener.open(req, timeout=self.timeout) as res:
                if "text/event-stream" in (res.headers.get("Content-Type") or "") and expected_id is not None:
                    raw, event = b"", b""
                    while len(raw) <= 8_000_000:
                        line = res.readline(65537)
                        if not line: break
                        raw += line
                        event += line
                        if line in (b"\n", b"\r\n"):
                            try:
                                rpc_result(event, "text/event-stream", expected_id)
                                break
                            except Unavailable:
                                event = b""
                else:
                    raw = res.read(8_000_001)
                if len(raw) > 8_000_000:
                    raise Unavailable("RESPONSE_TOO_LARGE")
                return res.status, dict(res.headers), raw
        except error.HTTPError as exc:
            # Return only status + headers. Never publish error bodies or request URLs.
            return exc.code, dict(exc.headers), b""
        except Unavailable:
            raise
        except Exception:
            raise Unavailable("TRANSPORT_FAILED") from None

    def json(self, url, **kwargs):
        status, headers, raw = self.exchange(url, **kwargs)
        if not 200 <= status < 300:
            raise Unavailable(f"HTTP_{status}")
        return decode_json(raw) if raw else None

def header(headers, name):
    return next((v for k, v in headers.items() if k.lower() == name.lower()), None)

def rpc_result(raw: bytes, response_type: str, request_id: int):
    if "text/event-stream" in response_type:
        objects = []
        data_lines = []
        # Each SSE event may contain multiple data lines.
        for line in raw.decode("utf-8").splitlines() + [""]:
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
            elif not line and data_lines:
                objects.append(decode_json("\n".join(data_lines).encode()))
                data_lines = []
    else:
        objects = [decode_json(raw)]
    for obj in objects:
        if isinstance(obj, dict) and obj.get("id") == request_id:
            if obj.get("error"):
                raise Unavailable("MCP_RPC_ERROR")
            if "result" not in obj:
                raise Unavailable("MCP_RESULT_MISSING")
            return obj["result"]
    raise Unavailable("MCP_RESPONSE_ID_MISMATCH")

class RobinhoodMCP:
    """Exactly three read tools. Bearer tokens come from THIS app's local OAuth store."""
    def __init__(self, tokens, http=None):
        self.tokens = tokens
        self.http = http or HTTPS()
        self.session = None
        self.protocol = "2025-06-18"
        self.sequence = 0
        self.started = False
        self.tools = set()
        self.lock = threading.RLock()

    def _rpc(self, method, params, notification=False):
        self.sequence += 1
        rid = self.sequence
        body = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            body["id"] = rid
        headers = {"Authorization": "Bearer " + self.tokens.bearer(),
                   "Accept": "application/json, text/event-stream"}
        if self.started:
            headers["MCP-Protocol-Version"] = self.protocol
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        status, response_headers, raw = self.http.exchange(ROBINHOOD, method="POST", headers=headers, body=body)
        if status == 401:
            self.started = False
            raise Unavailable("ROBINHOOD_SIGN_IN_REQUIRED")
        if status == 404 and self.session:
            self.started, self.session = False, None
            raise Unavailable("MCP_SESSION_EXPIRED")
        if not 200 <= status < 300:
            raise Unavailable(f"MCP_HTTP_{status}")
        self.session = header(response_headers, "Mcp-Session-Id") or self.session
        if notification:
            return None
        return rpc_result(raw, header(response_headers, "Content-Type") or "application/json", rid)

    def _initialize(self):
        result = self._rpc("initialize", {"protocolVersion": self.protocol, "capabilities": {},
                            "clientInfo": {"name": "trader-brain-read-only", "version": "1.0"}})
        if result.get("protocolVersion") not in {"2024-11-05", "2025-03-26", "2025-06-18"}:
            raise Unavailable("MCP_PROTOCOL_UNSUPPORTED")
        self.protocol = result["protocolVersion"]
        self.started = True
        self._rpc("notifications/initialized", {}, notification=True)
        cursor = None
        self.tools = set()
        for _ in range(20):
            result = self._rpc("tools/list", {"cursor": cursor} if cursor else {})
            self.tools.update(x["name"] for x in result.get("tools", []) if isinstance(x, dict) and "name" in x)
            cursor = result.get("nextCursor")
            if not cursor:
                return
        self.started = False
        raise Unavailable("MCP_TOOL_LIST_INCOMPLETE")

    def call(self, name, arguments):
        if name not in READ_TOOLS:
            raise Unavailable("BROKER_WRITE_OR_UNAPPROVED_TOOL_BLOCKED")
        with self.lock:
            if not self.started:
                self._initialize()
            if name not in self.tools:
                raise Unavailable("REQUIRED_READ_TOOL_MISSING")
            result = self._rpc("tools/call", {"name": name, "arguments": arguments})
            if result.get("isError"):
                raise Unavailable("ROBINHOOD_READ_FAILED")
            payload = result.get("structuredContent")
            if payload is None:
                for part in result.get("content", []):
                    if part.get("type") == "text":
                        try:
                            payload = decode_json(part["text"].encode())
                            break
                        except Unavailable:
                            continue
            if not isinstance(payload, dict):
                raise Unavailable("ROBINHOOD_SCHEMA_UNAVAILABLE")
            return payload.get("data", payload)
