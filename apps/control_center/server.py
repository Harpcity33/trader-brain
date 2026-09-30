"""Private mobile control API: same-origin sessions, CSRF, bounded bodies, TLS on LAN."""
from __future__ import annotations

from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
from pathlib import Path
import secrets
import ssl
import threading
import time
from urllib import parse
import uuid

from .transport import Unavailable

WEB = Path(__file__).parent / "web"
ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "application/javascript"),
          "/app.css": ("app.css", "text/css"),
          "/sw.js": ("sw.js", "application/javascript"),
          "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
          "/icon.svg": ("icon.svg", "image/svg+xml"),
          "/deck.js": ("deck.js", "application/javascript"),
          "/brand-mark.png": ("brand-mark.png", "image/png"),
          "/brand-full.png": ("brand-full.png", "image/png"),
          "/icon-192.png": ("icon-192.png", "image/png"),
          "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png")}

class Server(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, engine, token, origin):
        if len(token) < 32:
            raise ValueError("dashboard token must contain at least 32 characters")
        self.engine, self.token, self.origin = engine, token, origin.rstrip("/")
        self.allowed_host = parse.urlsplit(self.origin).netloc
        self.sessions = {}
        self.attempts = {}
        self.auth_lock = threading.Lock()
        super().__init__(address, Handler)

class Handler(BaseHTTPRequestHandler):
    server_version = "TraderBrain"
    sys_version = ""
    def setup(self):
        super().setup()
        self.connection.settimeout(8)
    def log_message(self, *args): pass

    def respond(self, status, body, content_type="application/json", cookie=None):
        raw = json.dumps(body, allow_nan=False).encode() if content_type == "application/json" else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if cookie: self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(raw)

    def check_host(self):
        return self.headers.get("Host") == self.server.allowed_host

    def session(self):
        cookie = SimpleCookie()
        try: cookie.load(self.headers.get("Cookie", ""))
        except Exception: return None
        identifier = cookie.get("tb_session")
        if not identifier: return None
        with self.server.auth_lock:
            value = self.server.sessions.get(identifier.value)
            if value and value["expires"] > time.monotonic():
                return value
        return None

    def do_GET(self):
        if not self.check_host(): return self.respond(403, {"error":"HOST_REJECTED"})
        path = parse.urlsplit(self.path).path
        if path in ASSETS:
            name, content_type = ASSETS[path]
            return self.respond(200, (WEB/name).read_bytes(), content_type)
        session = self.session()
        if not session: return self.respond(401, {"error":"SIGN_IN_REQUIRED"})
        if path == "/api/session": return self.respond(200, {"csrf":session["csrf"]})
        if path == "/api/status":
            try: return self.respond(200, self.server.engine.snapshot())
            except Exception: return self.respond(503, {"error":"STATUS_UNAVAILABLE"})
        return self.respond(404, {"error":"NOT_FOUND"})

    def do_POST(self):
        if not self.check_host() or self.headers.get("Origin") != self.server.origin:
            return self.respond(403, {"error":"ORIGIN_REJECTED"})
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self.respond(415, {"error":"JSON_REQUIRED"})
        if self.headers.get("Transfer-Encoding"):
            return self.respond(400, {"error":"CHUNKED_REQUEST_REJECTED"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096: raise ValueError()
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict): raise ValueError()
        except Exception: return self.respond(400, {"error":"INVALID_REQUEST"})
        path = parse.urlsplit(self.path).path
        if path == "/api/login":
            ip = self.client_address[0]
            now = time.monotonic()
            with self.server.auth_lock:
                times = [t for t in self.server.attempts.get(ip, []) if now-t < 60]
                if len(times) >= 5: return self.respond(429, {"error":"TRY_AGAIN_LATER"})
                value = body.get("token")
                if not isinstance(value, str) or not secrets.compare_digest(value, self.server.token):
                    self.server.attempts[ip] = times + [now]
                    return self.respond(401, {"error":"INVALID_PAIRING_TOKEN"})
                self.server.sessions = {k:v for k,v in self.server.sessions.items() if v["expires"] > now}
                if len(self.server.sessions) >= 256: return self.respond(429, {"error":"SESSION_LIMIT"})
                identifier, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                self.server.sessions[identifier] = {"csrf":csrf,"expires":now+8*3600}
            secure = "; Secure" if self.server.origin.startswith("https:") else ""
            return self.respond(200, {"csrf":csrf}, cookie=f"tb_session={identifier}; Path=/; HttpOnly; SameSite=Strict; Max-Age=28800{secure}")
        session = self.session()
        if not session: return self.respond(401, {"error":"SIGN_IN_REQUIRED"})
        csrf = self.headers.get("X-TB-CSRF", "")
        if not secrets.compare_digest(csrf, session["csrf"]):
            return self.respond(403, {"error":"CSRF_REJECTED"})
        if path == "/api/logout":
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            with self.server.auth_lock: self.server.sessions.pop(cookie["tb_session"].value, None)
            return self.respond(200, {"ok":True}, cookie="tb_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")
        if path != "/api/command": return self.respond(404, {"error":"NOT_FOUND"})
        try:
            identifier = str(uuid.UUID(body["id"]))
            action = body["action"]
            if action == "flatten" and body.get("confirmation") != "CLOSE PAPER POSITIONS":
                return self.respond(400, {"error":"PAPER_LIQUIDATION_CONFIRMATION_REQUIRED"})
            command = self.server.engine.store.enqueue(identifier, action)
        except (ValueError, KeyError, TypeError): return self.respond(400, {"error":"INVALID_COMMAND"})
        self.server.engine.wake.set()
        return self.respond(202, command)


def serve(engine, token, *, host="127.0.0.1", port=8765, origin=None, cert=None, key=None, tailnet_proxy=False):
    if host not in {"127.0.0.1", "localhost", "::1"} and (not cert or not key):
        raise Unavailable("LAN_REQUIRES_TLS_CERTIFICATE")
    if bool(cert) != bool(key): raise Unavailable("TLS_CERT_AND_KEY_REQUIRED")
    origin = origin or f"{'https' if cert else 'http'}://{host}:{port}"
    parts = parse.urlsplit(origin)
    if tailnet_proxy:
        # This listener is reachable only on literal loopback, behind Tailscale Serve TLS.
        # External Host and Origin remain exact; forwarding headers grant no authority.
        label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
        domain = rf"{label}\.{label}\.ts\.net"
        if (host != "127.0.0.1" or cert or key or parts.scheme != "https"
                or not re.fullmatch(domain, parts.hostname or "")
                or parts.port is not None or parts.path or parts.query
                or parts.fragment or parts.username or parts.password):
            raise Unavailable("INVALID_PRIVATE_TAILNET_PROXY")
    elif (parts.scheme != ("https" if cert else "http") or not parts.hostname or parts.path or parts.query
            or parts.fragment or parts.username or parts.password):
        raise Unavailable("INVALID_DASHBOARD_ORIGIN")
    server = Server((host, port), engine, token, origin)
    if cert:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    return server
