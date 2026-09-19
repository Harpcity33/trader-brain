"""Owner-run, send-only desktop OAuth enrollment; never activates trading.

Google protocol: https://developers.google.com/identity/protocols/oauth2/native-app
Tokens stay in memory until create-only storage in the five scoped Keychain
items. No secrets are put in command arguments, files, stdout, or error text.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
from dataclasses import dataclass, field
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import sys
import threading
import time
from typing import Callable
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .provider_clients import (
    GMAIL_KEYCHAIN_SERVICE_FIELDS, GMAIL_SEND_SCOPE, KeychainItem, MacOSKeychain,
    gmail_desktop_loopback_uris_valid, select_account_gmail_profile,
)

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
# Google Desktop client exports may retain the legacy URL as metadata. Never
# navigate to an imported URL: OAuthAttempt always uses AUTH_ENDPOINT above.
DESKTOP_AUTH_METADATA_ENDPOINTS = frozenset({
    AUTH_ENDPOINT, "https://accounts.google.com/o/oauth2/auth",
})
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
MAX_BYTES = 65536
CALLBACK_CLEANUP_SCRIPT = 'history.replaceState(null, "", "/oauth2callback");'
CALLBACK_CLEANUP_HASH = base64.b64encode(hashlib.sha256(CALLBACK_CLEANUP_SCRIPT.encode("ascii")).digest()).decode("ascii")


class SetupError(RuntimeError):
    """Only stable, non-secret error codes escape this module."""


def _json_object(raw: bytes) -> dict:
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value

    if type(raw) is not bytes or len(raw) > MAX_BYTES:
        raise ValueError("invalid JSON size")
    value = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError("invalid JSON object")
    return value


def _opaque(value: object, maximum: int = 4096) -> bool:
    return type(value) is str and 0 < len(value) <= maximum and all(33 <= ord(c) <= 126 for c in value)


def address(value: str) -> str:
    if type(value) is not str or len(value) > 254:
        raise SetupError("GMAIL_SETUP_ADDRESS_INVALID")
    value = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9.-]+\.[a-z]{2,}", value):
        raise SetupError("GMAIL_SETUP_ADDRESS_INVALID")
    return value


@dataclass(frozen=True)
class DesktopClient:
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    raw_json: str = field(repr=False)

    @classmethod
    def load(cls, path: Path) -> "DesktopClient":
        try:
            # A FIFO/device must not block before fstat can reject it.
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise SetupError("GMAIL_SETUP_CLIENT_FILE_MUST_BE_OWNER_ONLY")
                raw = source.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise SetupError("GMAIL_SETUP_CLIENT_FILE_OVERSIZED")
            return cls.parse(raw)
        except SetupError:
            raise
        except (OSError, ValueError):
            raise SetupError("GMAIL_SETUP_CLIENT_FILE_UNAVAILABLE") from None

    @classmethod
    def parse(cls, raw: bytes) -> "DesktopClient":
        try:
            parsed = _json_object(raw)
            value = parsed["installed"]
            client_id, client_secret = value["client_id"], value["client_secret"]
            if (not isinstance(client_id, str) or len(client_id) > 512 or not re.fullmatch(r"[a-zA-Z0-9._-]+\.apps\.googleusercontent\.com", client_id)
                    or not _opaque(client_secret)
                    or value["token_uri"] != TOKEN_ENDPOINT
                    or value["auth_uri"] not in DESKTOP_AUTH_METADATA_ENDPOINTS
                    or not gmail_desktop_loopback_uris_valid(value["redirect_uris"])
                    or "web" in parsed):
                raise ValueError()
            return cls(client_id, client_secret, json.dumps(parsed, separators=(",", ":")))
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise SetupError("GMAIL_SETUP_DESKTOP_CLIENT_INVALID") from None


@dataclass
class OAuthAttempt:
    client: DesktopClient = field(repr=False)
    redirect_uri: str
    expected_sender: str = field(repr=False)
    state: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    verifier: str = field(default_factory=lambda: secrets.token_urlsafe(64), repr=False)
    consumed: bool = False
    timeout_seconds: float = field(default=300, repr=False)
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)
    deadline: float = field(init=False, repr=False)
    _accepted_code_hash: bytes | None = field(default=None, init=False, repr=False)
    _exchange_started: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        try:
            parsed = urlsplit(self.redirect_uri)
            if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
                    or not parsed.port or parsed.path != "/oauth2callback"
                    or parsed.query or parsed.fragment or parsed.username or parsed.password
                    or self.redirect_uri != f"http://127.0.0.1:{parsed.port}/oauth2callback"):
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise SetupError("GMAIL_SETUP_LOOPBACK_INVALID") from None
        if (type(self.timeout_seconds) not in (int, float) or not 0 < self.timeout_seconds <= 300
                or type(self.state) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", self.state)
                or type(self.verifier) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", self.verifier)):
            raise SetupError("GMAIL_SETUP_ATTEMPT_INVALID")
        started = self.clock()
        if type(started) not in (int, float) or not math.isfinite(started):
            raise SetupError("GMAIL_SETUP_ATTEMPT_INVALID")
        self.deadline = started + self.timeout_seconds
        self.expected_sender = address(self.expected_sender)

    def require_unexpired(self) -> None:
        now = self.clock()
        if type(now) not in (int, float) or not math.isfinite(now) or now >= self.deadline:
            raise SetupError("GMAIL_SETUP_CALLBACK_EXPIRED")

    def authorization_url(self) -> str:
        self.require_unexpired()
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
        return AUTH_ENDPOINT + "?" + urlencode({
            "client_id": self.client.client_id, "redirect_uri": self.redirect_uri,
            "response_type": "code", "scope": GMAIL_SEND_SCOPE,
            "state": self.state, "code_challenge": challenge,
            "code_challenge_method": "S256", "access_type": "offline",
            "prompt": "consent", "login_hint": self.expected_sender,
            "include_granted_scopes": "false",
        })

    def consume_callback(self, target: str) -> str:
        self.require_unexpired()
        if self.consumed:
            raise SetupError("GMAIL_SETUP_CALLBACK_REPLAY")
        try:
            if not _opaque(target, 8192):
                raise ValueError()
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc or parsed.path != "/oauth2callback" or parsed.fragment:
                raise ValueError()
            query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=16, errors="strict")
        except (ValueError, TypeError, UnicodeError):
            raise SetupError("GMAIL_SETUP_CALLBACK_INVALID") from None
        state = query.get("state", [""])[0]
        if not _opaque(state, 128):
            raise SetupError("GMAIL_SETUP_CALLBACK_STATE_INVALID")
        if any(len(values) != 1 for values in query.values()) or not hmac.compare_digest(state, self.state):
            raise SetupError("GMAIL_SETUP_CALLBACK_STATE_INVALID")
        if "error" in query:
            self.consumed = True
            raise SetupError("GMAIL_SETUP_OWNER_DENIED")
        code = query.get("code", [""])[0]
        if not _opaque(code):
            raise SetupError("GMAIL_SETUP_CALLBACK_CODE_INVALID")
        self.consumed = True
        self._accepted_code_hash = hashlib.sha256(code.encode("ascii")).digest()
        return code


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise SetupError("GMAIL_SETUP_TOKEN_REDIRECT_REFUSED")


def token_request(fields: dict[str, str], *, opener=None) -> dict:
    """Fixed verified HTTPS endpoint; never forward a token on redirection."""
    request = Request(TOKEN_ENDPOINT, data=urlencode(fields).encode("ascii"), method="POST",
                      headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    try:
        open_request = opener or build_opener(_NoRedirect()).open
        with open_request(request, timeout=15) as response:
            if response.status != 200 or response.geturl() != TOKEN_ENDPOINT:
                raise SetupError("GMAIL_SETUP_TOKEN_RESPONSE_INVALID")
            raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise SetupError("GMAIL_SETUP_TOKEN_RESPONSE_OVERSIZED")
        return _json_object(raw)
    except SetupError:
        raise
    except Exception:
        raise SetupError("GMAIL_SETUP_TOKEN_REQUEST_FAILED") from None


def validate_grant(body: dict, *, require_refresh: bool) -> None:
    try:
        valid = (body.get("token_type", "").lower() == "bearer"
                 and _opaque(body.get("access_token"))
                 and type(body.get("scope")) is str and body["scope"].split() == [GMAIL_SEND_SCOPE]
                 and type(body.get("expires_in")) is int and 60 <= body["expires_in"] <= 86400
                 and "error" not in body and "refresh_token_expires_in" not in body
                 and ("refresh_token" not in body or _opaque(body["refresh_token"]))
                 and (not require_refresh or _opaque(body.get("refresh_token"))))
    except (TypeError, AttributeError):
        valid = False
    if not valid:
        raise SetupError("GMAIL_SETUP_EXACT_SEND_ONLY_DURABLE_GRANT_REQUIRED")


def exchange_and_verify(attempt: OAuthAttempt, code: str, *, request=token_request) -> str:
    attempt.require_unexpired()
    if (not attempt.consumed or attempt._accepted_code_hash is None or not _opaque(code)
            or not hmac.compare_digest(attempt._accepted_code_hash, hashlib.sha256(code.encode("ascii")).digest())):
        raise SetupError("GMAIL_SETUP_CALLBACK_NOT_CONSUMED")
    if attempt._exchange_started:
        raise SetupError("GMAIL_SETUP_EXCHANGE_REPLAY")
    # Mark before I/O: an uncertain response must not replay an authorization code.
    attempt._exchange_started = True
    common = {"client_id": attempt.client.client_id, "client_secret": attempt.client.client_secret}
    first = request({**common, "grant_type": "authorization_code", "code": code,
                     "code_verifier": attempt.verifier, "redirect_uri": attempt.redirect_uri})
    validate_grant(first, require_refresh=True)
    refreshed = request({**common, "grant_type": "refresh_token", "refresh_token": first["refresh_token"]})
    validate_grant(refreshed, require_refresh=False)
    return refreshed.get("refresh_token", first["refresh_token"])


class CreateOnlyMacOSKeychain(MacOSKeychain):
    """Owner-interactive setup only; runtime Keychain access stays unchanged.

    Native reads authenticate this application normally and let macOS own the
    access dialog's lifetime, instead of killing a security subprocess after
    ten seconds. No access-control or interaction settings are changed.
    """

    def metadata_status(self, item: KeychainItem) -> str:
        if not item.account:
            return "UNAVAILABLE"
        return super().metadata_status(item)

    def read(self, item: KeychainItem) -> bytes:
        if sys.platform != "darwin" or not item.account:
            raise SetupError("GMAIL_SETUP_KEYCHAIN_READ_UNAVAILABLE")
        try:
            security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
            find = security.SecKeychainFindGenericPassword
            find.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p,
                             ctypes.c_uint32, ctypes.c_char_p,
                             ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_void_p),
                             ctypes.POINTER(ctypes.c_void_p)]
            find.restype = ctypes.c_int32
            free = security.SecKeychainItemFreeContent
            free.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            free.restype = ctypes.c_int32
            service, account = item.service.encode(), item.account.encode()
            length, data = ctypes.c_uint32(), ctypes.c_void_p()
            try:
                status = find(None, len(service), service, len(account), account,
                              ctypes.byref(length), ctypes.byref(data), None)
                if status != 0:
                    raise SetupError("GMAIL_SETUP_KEYCHAIN_READ_FAILED")
                if not data.value or not 0 < length.value <= min(self.maximum_bytes, MAX_BYTES):
                    raise SetupError("GMAIL_SETUP_KEYCHAIN_ITEM_INVALID")
                value = ctypes.string_at(data.value, length.value)
                if b"\x00" in value:
                    raise SetupError("GMAIL_SETUP_KEYCHAIN_ITEM_INVALID")
                return value
            finally:
                if data.value:
                    # The SDK owns this allocation; wipe the returned data and
                    # release it with the paired Security.framework function.
                    ctypes.memset(data.value, 0, length.value)
                    if free(None, data) != 0:
                        raise SetupError("GMAIL_SETUP_KEYCHAIN_READ_FAILED")
        except SetupError:
            raise
        except Exception:
            raise SetupError("GMAIL_SETUP_KEYCHAIN_READ_FAILED") from None

    def add(self, item: KeychainItem, value: bytes) -> None:
        if (sys.platform != "darwin" or not item.account or type(value) is not bytes
                or not value or len(value) > MAX_BYTES or b"\x00" in value):
            raise SetupError("GMAIL_SETUP_KEYCHAIN_WRITE_UNAVAILABLE")
        if self.metadata_status(item) != "MISSING":
            raise SetupError("GMAIL_SETUP_KEYCHAIN_ITEM_ALREADY_EXISTS_OR_UNAVAILABLE")
        try:
            security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
            add = security.SecKeychainAddGenericPassword
            # Apple SecKeychain.h: final parameter is SecKeychainItemRef*, optional.
            add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p,
                            ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32,
                            ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
            add.restype = ctypes.c_int32
            service, account = item.service.encode(), item.account.encode()
            secret_buffer = ctypes.create_string_buffer(value)
            try:
                result = add(None, len(service), service, len(account), account, len(value), secret_buffer, None)
            finally:
                ctypes.memset(secret_buffer, 0, len(secret_buffer))
        except Exception:
            raise SetupError("GMAIL_SETUP_KEYCHAIN_CREATE_FAILED") from None
        if result != 0:
            raise SetupError("GMAIL_SETUP_KEYCHAIN_CREATE_FAILED")
        try:
            matches = hmac.compare_digest(self.read(item), value)
        except Exception:
            raise SetupError("GMAIL_SETUP_KEYCHAIN_READBACK_FAILED_REVIEW_PARTIAL_ENROLLMENT") from None
        if not matches:
            raise SetupError("GMAIL_SETUP_KEYCHAIN_READBACK_FAILED_REVIEW_PARTIAL_ENROLLMENT")


def require_enrollment_state(profile: dict, client: DesktopClient, *, keychain: CreateOnlyMacOSKeychain,
                             authorize_recover_client_only: bool = False) -> list[KeychainItem]:
    """Permit empty enrollment, or explicitly authorized matching-client-only recovery."""
    if type(profile.get("credential_account")) is not str or not profile["credential_account"]:
        raise SetupError("GMAIL_SETUP_KEYCHAIN_ACCOUNT_REQUIRED")
    items = [KeychainItem(profile[name], profile["credential_account"]) for name in GMAIL_KEYCHAIN_SERVICE_FIELDS]
    expected = ["PRESENT", "MISSING", "MISSING", "MISSING", "MISSING"] if authorize_recover_client_only else ["MISSING"] * 5
    error = ("GMAIL_SETUP_RECOVERY_REQUIRES_MATCHING_CLIENT_ONLY" if authorize_recover_client_only
             else "GMAIL_SETUP_EXISTING_ITEMS_REQUIRE_OWNER_REVIEW")
    try:
        if [keychain.metadata_status(item) for item in items] != expected:
            raise SetupError(error)
        if authorize_recover_client_only:
            try:
                stored = DesktopClient.parse(keychain.read(items[0]))
                # Compare all imported fields, accepting only JSON formatting
                # and key-order differences. Neither values nor hashes escape.
                def canonical(value: DesktopClient) -> bytes:
                    return json.dumps(_json_object(value.raw_json.encode("utf-8")),
                                      sort_keys=True, separators=(",", ":")).encode("utf-8")
                matches = hmac.compare_digest(canonical(stored), canonical(client))
            except Exception:
                raise SetupError("GMAIL_SETUP_RECOVERY_CLIENT_UNVERIFIABLE") from None
            if not matches:
                raise SetupError("GMAIL_SETUP_RECOVERY_CLIENT_MISMATCH")
            # Owner authentication can take time: repeat the complete metadata
            # check after reading the client, before any consent or writes.
            if [keychain.metadata_status(item) for item in items] != expected:
                raise SetupError(error)
    except SetupError:
        raise
    except Exception:
        raise SetupError(error) from None
    return items


def save_enrollment(profile: dict, client: DesktopClient, refresh: str, *, sender: str,
                    destination: str, consent_status: str, keychain: CreateOnlyMacOSKeychain,
                    authorize_recover_client_only: bool = False) -> None:
    if consent_status not in {"production", "internal"}:
        raise SetupError("GMAIL_SETUP_DURABLE_PUBLISHING_STATUS_REQUIRED")
    if not _opaque(refresh):
        raise SetupError("GMAIL_SETUP_EXACT_SEND_ONLY_DURABLE_GRANT_REQUIRED")
    values = [client.raw_json, refresh, consent_status, address(sender), address(destination)]
    items = require_enrollment_state(profile, client, keychain=keychain,
                                     authorize_recover_client_only=authorize_recover_client_only)
    # Commit consent last, after four verified writes. Failures before that
    # leave no consent; failure after the final write requires owner review
    # because credentials may be complete. No route readiness is issued here.
    for index in ((1, 3, 4, 2) if authorize_recover_client_only else (0, 1, 3, 4, 2)):
        keychain.add(items[index], values[index].encode("utf-8"))


def receive_callback(client: DesktopClient, sender: str, *, announce: Callable[[str], None], timeout=300) -> tuple[OAuthAttempt, str]:
    result: list[str] = []
    failures: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            lifetime = min(1.0, max(0.01, attempt.deadline - time.monotonic()))
            self.request.settimeout(lifetime)
            super().setup()
            # Socket timeouts alone reset on every byte: also cut off a peer
            # that trickles an incomplete request indefinitely.
            def expire_connection():
                try:
                    self.request.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            self._connection_timer = threading.Timer(lifetime, expire_connection)
            self._connection_timer.daemon = True
            self._connection_timer.start()

        def finish(self):
            try:
                super().finish()
            finally:
                self._connection_timer.cancel()

        def log_message(self, *args):
            pass  # Request targets contain the private authorization code.

        def do_GET(self):
            if self.headers.get_all("Host") != [f"127.0.0.1:{self.server.server_port}"]:
                self.send_error(400)
                return
            try:
                code = attempt.consume_callback(self.path)
            except SetupError as exc:
                if str(exc) in {"GMAIL_SETUP_OWNER_DENIED", "GMAIL_SETUP_CALLBACK_EXPIRED"}:
                    failures.append(str(exc))
                self.send_response(400)
                message = "Authorization not accepted. Return to Titan setup."
            else:
                result.append(code)
                self.send_response(200)
                message = "Authorization received. Return to Titan setup; no email or trade was sent."
            # Every interpolated value is static; no request data is reflected.
            # Remove the consumed code from the visible URL and history entry.
            payload = ("<!doctype html><html><head><meta charset=\"utf-8\">"
                       "<title>Titan Gmail setup</title><script>" + CALLBACK_CLEANUP_SCRIPT
                       + "</script></head><body><p>" + message + "</p></body></html>").encode("utf-8")
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'; script-src 'sha256-" + CALLBACK_CLEANUP_HASH + "'")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    class QuietServer(HTTPServer):
        def handle_error(self, request, client_address):
            # Never log request targets, callback codes, or provider payloads.
            pass

    with QuietServer(("127.0.0.1", 0), Handler) as server:
        server.timeout = 0.25
        attempt = OAuthAttempt(client, f"http://127.0.0.1:{server.server_port}/oauth2callback", sender,
                               timeout_seconds=timeout)
        announce(attempt.authorization_url())
        while not result and not failures and time.monotonic() < attempt.deadline:
            server.handle_request()
    if failures or not result:
        raise SetupError(failures[0] if failures else "GMAIL_SETUP_CALLBACK_TIMEOUT")
    return attempt, result[0]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Owner-run free Gmail send-only setup; never starts trading or sends a message.")
    parser.add_argument("--source-root", type=Path, required=True, help="verified installed release or reviewed repository")
    parser.add_argument("--client-json", type=Path, required=True, help="owner-only downloaded Google Desktop client JSON")
    parser.add_argument("--sender", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--consent-status", choices=("production", "internal"), required=True, help="owner-verified Google publishing status; Testing is not durable")
    parser.add_argument("--authorize-keychain-create", action="store_true", help="owner authorizes create-only storage of five IBKR-scoped items")
    parser.add_argument("--authorize-recover-client-only", action="store_true",
                        help="owner authorizes preserving one matching desktop client and creating only four missing items after fresh Google consent")
    args = parser.parse_args(argv)
    try:
        if not args.authorize_keychain_create:
            raise SetupError("GMAIL_SETUP_OWNER_KEYCHAIN_AUTHORIZATION_REQUIRED")
        client = DesktopClient.load(args.client_json)
        from .policy import PolicyBundle
        policy = PolicyBundle.load(args.source_root, config_relative="config/full_live_ibkr.json")
        bindings = json.loads((args.source_root / "config/provider_bindings.json").read_text())
        _, profile = select_account_gmail_profile(bindings, policy.config)
        keychain = CreateOnlyMacOSKeychain()
        require_enrollment_state(profile, client, keychain=keychain,
                                 authorize_recover_client_only=args.authorize_recover_client_only)
        sender, destination = address(args.sender), address(args.destination)
        print("Open the following Google consent URL in your system browser (Safari/Chrome), not an embedded browser. Verify the chosen Google account. Do not share the callback URL.", flush=True)
        attempt, code = receive_callback(client, sender, announce=lambda url: print(url, flush=True))
        refresh = exchange_and_verify(attempt, code)
        save_enrollment(profile, client, refresh, sender=sender, destination=destination,
                        consent_status=args.consent_status, keychain=keychain,
                        authorize_recover_client_only=args.authorize_recover_client_only)
        print(json.dumps({"status": "KEYCHAIN_ENROLLMENT_COMPLETE", "scope": GMAIL_SEND_SCOPE,
                          "account_namespace": profile["credential_account"], "oauth_refresh_verified": True,
                          "publishing_status_source": "owner_attestation", "message_sent": False,
                          "route_ready": False, "trading_activated": False}))
        return 0
    except (SetupError, KeyboardInterrupt) as exc:
        print(json.dumps({"status": "BLOCKED", "error": str(exc) if isinstance(exc, SetupError) else "GMAIL_SETUP_INTERRUPTED",
                          "message_sent": False, "trading_activated": False}))
        return 2
    except Exception:
        print(json.dumps({"status": "BLOCKED", "error": "GMAIL_SETUP_FAILED_REVIEW_PARTIAL_ENROLLMENT", "message_sent": False, "trading_activated": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
