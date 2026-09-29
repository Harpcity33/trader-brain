"""User-present Robinhood OAuth/PKCE. Never reads Codex, ChatGPT or browser secrets."""
from __future__ import annotations

import base64
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
import time
from urllib import parse
import webbrowser

from .transport import HTTPS, ROBINHOOD, Unavailable, header, trusted_robinhood


def private_write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".private-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump(value, output, allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _form(http, url, fields):
    return http.json(trusted_robinhood(url), method="POST",
                     headers={"Content-Type": "application/x-www-form-urlencoded"},
                     body=parse.urlencode(fields).encode())


class TokenStore:
    def __init__(self, path: Path, http=None):
        self.path = path
        self.http = http or HTTPS()
        self.lock = threading.RLock()

    def read(self):
        if not self.path.is_file():
            raise Unavailable("ROBINHOOD_SIGN_IN_REQUIRED")
        if self.path.stat().st_mode & 0o077:
            raise Unavailable("TOKEN_FILE_PERMISSIONS_UNSAFE")
        try:
            value = json.loads(self.path.read_text())
            if not value.get("access_token") or value.get("token_type", "").lower() != "bearer":
                raise ValueError()
            return value
        except (ValueError, KeyError):
            raise Unavailable("TOKEN_STORE_INVALID") from None

    def bearer(self):
        with self.lock:
            value = self.read()
            if value["expires_at"] <= time.time() + 60:
                if not value.get("refresh_token"):
                    raise Unavailable("ROBINHOOD_SIGN_IN_REQUIRED")
                renewed = _form(self.http, value["token_endpoint"], {
                    "grant_type": "refresh_token", "refresh_token": value["refresh_token"],
                    "client_id": value["client_id"], "resource": ROBINHOOD,
                })
                if not renewed.get("access_token") or renewed.get("token_type", "").lower() != "bearer":
                    raise Unavailable("OAUTH_REFRESH_FAILED")
                value.update(renewed)
                # No expiry returned: short lifetime, not an unlimited token assumption.
                value["expires_at"] = time.time() + float(renewed.get("expires_in", 300))
                private_write(self.path, value)
            return value["access_token"]


def discover(http):
    status, headers, _ = http.exchange(ROBINHOOD, headers={"Accept": "application/json, text/event-stream"})
    auth = header(headers, "WWW-Authenticate") or ""
    match = re.search(r'resource_metadata="([^"]+)"', auth)
    metadata_url = match.group(1) if match else "https://agent.robinhood.com/.well-known/oauth-protected-resource/mcp/trading"
    resource = http.json(trusted_robinhood(metadata_url))
    if resource.get("resource", "").rstrip("/") != ROBINHOOD:
        raise Unavailable("OAUTH_RESOURCE_MISMATCH")
    servers = resource.get("authorization_servers", [])
    if not servers:
        raise Unavailable("OAUTH_SERVER_NOT_ADVERTISED")
    issuer = trusted_robinhood(servers[0]).rstrip("/")
    parts = parse.urlsplit(issuer)
    metadata = http.json(f"{parts.scheme}://{parts.netloc}/.well-known/oauth-authorization-server{parts.path}")
    if metadata.get("issuer", "").rstrip("/") != issuer:
        raise Unavailable("OAUTH_ISSUER_MISMATCH")
    for key in ("authorization_endpoint", "token_endpoint"):
        trusted_robinhood(metadata[key])
    if "S256" not in metadata.get("code_challenge_methods_supported", []):
        raise Unavailable("OAUTH_PKCE_S256_NOT_ADVERTISED")
    return metadata


def login(path: Path, *, client_id: str | None = None):
    """Run locally, with the owner present for Robinhood's real authorization screen."""
    http = HTTPS()
    metadata = discover(http)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(32)
    received = {}

    class Callback(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = parse.urlsplit(self.path)
            fields = parse.parse_qs(parsed.query)
            if (parsed.path != "/callback" or not secrets.compare_digest(fields.get("state", [""])[0], state)):
                self.send_error(400)
                return
            received.update({"code": fields.get("code", [""])[0], "error": bool(fields.get("error"))})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b"Trader Brain received your sign-in response. Return to the local setup window.")
        def log_message(self, *args):
            pass  # Authorization codes and state must never enter logs.

    server = HTTPServer(("127.0.0.1", 0), Callback)
    server.timeout = 1
    redirect = f"http://127.0.0.1:{server.server_port}/callback"
    try:
        if not client_id:
            endpoint = metadata.get("registration_endpoint")
            if not endpoint:
                raise Unavailable("OAUTH_CLIENT_REGISTRATION_REQUIRED")
            registration = http.json(trusted_robinhood(endpoint), method="POST", body={
                "client_name": "Trader Brain read-only options", "redirect_uris": [redirect],
                "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            })
            if registration.get("token_endpoint_auth_method", "none") != "none":
                raise Unavailable("PUBLIC_OAUTH_CLIENT_NOT_SUPPORTED")
            client_id = registration["client_id"]
        query = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect,
                 "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
                 "resource": ROBINHOOD}
        # Scope is not guessed. The advertised provider login is presented to the owner.
        url = metadata["authorization_endpoint"] + "?" + parse.urlencode(query)
        if not webbrowser.open(url):
            raise Unavailable("LOCAL_BROWSER_REQUIRED")
        print("Complete the Robinhood authorization in the browser. No password goes into Terminal.")
        deadline = time.monotonic() + 300
        while not received and time.monotonic() < deadline:
            server.handle_request()
        if not received.get("code") or received.get("error"):
            raise Unavailable("OAUTH_NOT_COMPLETED")
        token = _form(http, metadata["token_endpoint"], {"grant_type": "authorization_code",
                      "code": received["code"], "client_id": client_id, "redirect_uri": redirect,
                      "code_verifier": verifier, "resource": ROBINHOOD})
        if not token.get("access_token") or token.get("token_type", "").lower() != "bearer":
            raise Unavailable("OAUTH_TOKEN_INVALID")
        token.update({"client_id": client_id, "token_endpoint": metadata["token_endpoint"],
                      "expires_at": time.time() + float(token.get("expires_in", 300))})
        private_write(path, token)
        print("Robinhood credentials saved locally. Only the three allowlisted options reads will be used.")
    finally:
        server.server_close()
