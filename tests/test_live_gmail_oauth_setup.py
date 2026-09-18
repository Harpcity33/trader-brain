from __future__ import annotations

import base64
from contextlib import redirect_stderr, redirect_stdout
import ctypes
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

from titan_brain.live import gmail_oauth_setup as setup
from titan_brain.live.provider_clients import (
    CredentialUnavailable, GMAIL_KEYCHAIN_SERVICE_FIELDS, GMAIL_SEND_SCOPE,
    GmailDesktopOAuthAuthorizer, KeychainItem,
)


def client_bytes(**updates):
    values = {
        "client_id": "offline-test.apps.googleusercontent.com",
        "client_secret": "synthetic-client-secret",
        "auth_uri": setup.AUTH_ENDPOINT,
        "token_uri": setup.TOKEN_ENDPOINT,
        "redirect_uris": ["http://localhost"],
    }
    values.update(updates)
    return json.dumps({"installed": values}).encode()


def grant(**updates):
    values = {"access_token": "synthetic-access", "refresh_token": "synthetic-refresh",
              "scope": GMAIL_SEND_SCOPE, "token_type": "Bearer", "expires_in": 3600}
    values.update(updates)
    return values


def attempt(**updates):
    values = {"client": setup.DesktopClient.parse(client_bytes()),
              "redirect_uri": "http://127.0.0.1:43123/oauth2callback",
              "expected_sender": "owner@example.com"}
    values.update(updates)
    return setup.OAuthAttempt(**values)


def callback(current, code="synthetic-code", **updates):
    values = {"state": current.state, "code": code}
    values.update(updates)
    return "/oauth2callback?" + urlencode(values)


class Response(io.BytesIO):
    status = 200
    url = setup.TOKEN_ENDPOINT

    def geturl(self):
        return self.url


class MemoryKeychain:
    def __init__(self, *, fail_at=None):
        self.values = {}
        self.calls = []
        self.fail_at = fail_at

    def metadata_status(self, item):
        return "PRESENT" if item in self.values else "MISSING"

    def add(self, item, value):
        self.calls.append(item)
        if self.fail_at == len(self.calls):
            raise setup.SetupError("GMAIL_SETUP_KEYCHAIN_CREATE_FAILED")
        if item in self.values:
            raise AssertionError("overwriting an existing item")
        self.values[item] = value


class GmailOAuthSetupTests(unittest.TestCase):
    def test_desktop_client_valid_exact_endpoints_and_private_repr(self):
        client = setup.DesktopClient.parse(client_bytes())
        self.assertNotIn("synthetic-client-secret", repr(client))
        self.assertEqual(json.loads(client.raw_json)["installed"]["token_uri"], setup.TOKEN_ENDPOINT)

    def test_desktop_client_rejects_confused_endpoints_and_non_desktop(self):
        malformed = [
            client_bytes(token_uri="https://attacker.invalid/token"),
            client_bytes(auth_uri="https://accounts.google.com.attacker.invalid/auth"),
            client_bytes(redirect_uris=["http://localhost.attacker.invalid"]),
            client_bytes(redirect_uris=["http://localhost", "https://attacker.invalid"]),
            client_bytes(redirect_uris=[{}]), client_bytes(redirect_uris=[]),
            client_bytes(client_id="bad-client"), client_bytes(client_secret="bad\nsecret"),
            client_bytes(client_secret="nonascii-\u00e9"), b'{"web": {}}', b'[]', b'null',
            b'{"installed":{},"installed":{}}', b'\xff', b'x' * (setup.MAX_BYTES + 1),
        ]
        for raw in malformed:
            with self.subTest(raw=raw[:30]), self.assertRaisesRegex(setup.SetupError, "DESKTOP_CLIENT_INVALID"):
                setup.DesktopClient.parse(raw)

    def test_setup_and_existing_authorizer_accept_same_exact_loopback_declarations(self):
        for uris in (["http://localhost"], ["http://127.0.0.1"], ["http://[::1]"],
                     ["http://localhost", "http://127.0.0.1", "http://[::1]"]):
            with self.subTest(uris=uris):
                client = setup.DesktopClient.parse(client_bytes(redirect_uris=uris))
                reader = Mock(read_text=Mock(side_effect=[client.raw_json, "synthetic-refresh", "production"]))
                authorizer = GmailDesktopOAuthAuthorizer(reader, client_item=KeychainItem("test-client"),
                    refresh_token_item=KeychainItem("test-refresh"), consent_status_item=KeychainItem("test-consent"))
                self.assertEqual(authorizer.evidence.scopes, (GMAIL_SEND_SCOPE,))
        for uris in (["http://localhost.attacker.invalid"], ["http://localhost", "https://attacker.invalid"],
                     ["http://localhost@attacker.invalid"], ["http://localhost/path"], [None], [], "http://localhost"):
            with self.subTest(uris=uris):
                reader = Mock(read_text=Mock(side_effect=[client_bytes(redirect_uris=uris).decode(), "synthetic-refresh", "production"]))
                with self.assertRaisesRegex(CredentialUnavailable, "DESKTOP_CLIENT_INVALID"):
                    GmailDesktopOAuthAuthorizer(reader, client_item=KeychainItem("test-client"),
                        refresh_token_item=KeychainItem("test-refresh"), consent_status_item=KeychainItem("test-consent"))

    def test_owner_only_file_load_no_write_symlink_fifo_and_size_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "client.json"
            path.write_bytes(client_bytes())
            path.chmod(0o600)
            before = path.read_bytes()
            setup.DesktopClient.load(path)
            self.assertEqual(path.read_bytes(), before)
            link = Path(temporary) / "linked.json"
            link.symlink_to(path)
            with self.assertRaisesRegex(setup.SetupError, "FILE_UNAVAILABLE"):
                setup.DesktopClient.load(link)
            path.chmod(0o644)
            with self.assertRaisesRegex(setup.SetupError, "OWNER_ONLY"):
                setup.DesktopClient.load(path)
            path.chmod(0o600)
            path.write_bytes(b"x" * (setup.MAX_BYTES + 1))
            with self.assertRaisesRegex(setup.SetupError, "OVERSIZED"):
                setup.DesktopClient.load(path)
            fifo = Path(temporary) / "fifo"
            os.mkfifo(fifo, 0o600)
            with self.assertRaisesRegex(setup.SetupError, "OWNER_ONLY"):
                setup.DesktopClient.load(fifo)

    def test_pkce_state_offline_single_scope_and_random_secret_values(self):
        current = attempt()
        parsed = urlsplit(current.authorization_url())
        self.assertEqual(parsed.scheme + "://" + parsed.netloc + parsed.path, setup.AUTH_ENDPOINT)
        query = parse_qs(parsed.query)
        self.assertEqual(query["scope"], [GMAIL_SEND_SCOPE])
        self.assertEqual(query["access_type"], ["offline"])
        self.assertEqual(query["include_granted_scopes"], ["false"])
        self.assertEqual(query["prompt"], ["consent"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        expected = base64.urlsafe_b64encode(hashlib.sha256(current.verifier.encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(query["code_challenge"], [expected])
        self.assertNotIn(current.verifier, current.authorization_url())
        self.assertNotIn("synthetic-client-secret", current.authorization_url())
        self.assertNotIn(current.state, repr(current))
        self.assertNotEqual(current.state, attempt().state)

    def test_loopback_url_variants_rejected_with_sanitized_errors(self):
        for url in ("http://localhost:1/oauth2callback", "http://127.0.0.1.attacker.invalid:1/oauth2callback",
                    "http://user@127.0.0.1:1/oauth2callback", "https://127.0.0.1:1/oauth2callback",
                    "http://127.0.0.1:65536/oauth2callback", "http://127.0.0.1:invalid/oauth2callback",
                    "http://127.0.0.1:1/oauth2callback?x=y", "http://127.0.0.1:01/oauth2callback",
                    "http://127.0.0.1:1/other", "http://[broken", None):
            with self.subTest(url=url), self.assertRaisesRegex(setup.SetupError, "LOOPBACK_INVALID"):
                attempt(redirect_uri=url)

    def test_timeout_and_nonce_constraints(self):
        for value in (0, -1, 301, float("nan"), float("inf"), True, "300"):
            with self.subTest(value=value), self.assertRaisesRegex(setup.SetupError, "ATTEMPT_INVALID"):
                attempt(timeout_seconds=value)
        for kwargs in ({"state": "short"}, {"verifier": "short"}, {"state": None}, {"verifier": []},
                       {"clock": lambda: float("nan")}, {"clock": lambda: "not-time"}):
            with self.assertRaisesRegex(setup.SetupError, "ATTEMPT_INVALID"):
                attempt(**kwargs)

    def test_callback_is_exact_state_code_bound_and_one_shot(self):
        current = attempt()
        self.assertEqual(current.consume_callback(callback(current)), "synthetic-code")
        with self.assertRaisesRegex(setup.SetupError, "CALLBACK_REPLAY"):
            current.consume_callback(callback(current))
        requester = Mock(return_value=grant())
        with self.assertRaisesRegex(setup.SetupError, "CALLBACK_NOT_CONSUMED"):
            setup.exchange_and_verify(current, "different-code", request=requester)
        requester.assert_not_called()
        self.assertEqual(setup.exchange_and_verify(current, "synthetic-code", request=requester), "synthetic-refresh")
        first, second = [call.args[0] for call in requester.call_args_list]
        self.assertEqual(first["code_verifier"], current.verifier)
        self.assertEqual(first["redirect_uri"], current.redirect_uri)
        self.assertEqual(second["grant_type"], "refresh_token")
        self.assertEqual(second["refresh_token"], "synthetic-refresh")
        with self.assertRaisesRegex(setup.SetupError, "EXCHANGE_REPLAY"):
            setup.exchange_and_verify(current, "synthetic-code", request=requester)
        self.assertEqual(requester.call_count, 2)

    def test_invalid_callbacks_do_not_consume_or_expose_private_input(self):
        current = attempt()
        invalid = [callback(current, state="wrong-state"), callback(current) + "&state=duplicate",
                   callback(current, state="\u00e9"), callback(current, code="bad\ncode"),
                   callback(current, code="\u00e9"), callback(current, code=""),
                   "http://attacker.invalid" + callback(current), "//attacker.invalid/path",
                   callback(current) + "#fragment", "/oauth2callback?state=%FF",
                   "/oauth2callback?" + "&".join(f"a{x}=x" for x in range(17)),
                   "/oauth2callback?" + "a" * 8192, "/\n" + callback(current), None]
        for value in invalid:
            with self.subTest(value=str(value)[:50]), self.assertRaises(setup.SetupError) as caught:
                current.consume_callback(value)
            self.assertRegex(str(caught.exception), r"^GMAIL_SETUP_CALLBACK_[A-Z_]+$")
            self.assertFalse(current.consumed)

    def test_denied_callback_cannot_be_exchanged(self):
        current = attempt()
        with self.assertRaisesRegex(setup.SetupError, "OWNER_DENIED"):
            current.consume_callback(callback(current, error="access_denied"))
        request = Mock()
        with self.assertRaisesRegex(setup.SetupError, "CALLBACK_NOT_CONSUMED"):
            setup.exchange_and_verify(current, "synthetic-code", request=request)
        request.assert_not_called()

    def test_callback_and_exchange_expiry_recheck_monotonic_clock(self):
        now = [10.0]
        current = attempt(clock=lambda: now[0], timeout_seconds=2)
        now[0] = 12.0
        with self.assertRaisesRegex(setup.SetupError, "CALLBACK_EXPIRED"):
            current.consume_callback(callback(current))
        now[0] = 20.0
        current = attempt(clock=lambda: now[0], timeout_seconds=2)
        current.consume_callback(callback(current))
        now[0] = 22.0
        request = Mock()
        with self.assertRaisesRegex(setup.SetupError, "CALLBACK_EXPIRED"):
            setup.exchange_and_verify(current, "synthetic-code", request=request)
        request.assert_not_called()

    def test_uncertain_exchange_failure_cannot_replay(self):
        current = attempt()
        current.consume_callback(callback(current))
        request = Mock(side_effect=setup.SetupError("GMAIL_SETUP_TOKEN_REQUEST_FAILED"))
        with self.assertRaisesRegex(setup.SetupError, "TOKEN_REQUEST_FAILED"):
            setup.exchange_and_verify(current, "synthetic-code", request=request)
        with self.assertRaisesRegex(setup.SetupError, "EXCHANGE_REPLAY"):
            setup.exchange_and_verify(current, "synthetic-code", request=request)
        self.assertEqual(request.call_count, 1)

    def test_initial_and_refresh_must_both_be_exact_durable_grants(self):
        malformed = [grant(scope=None), grant(scope=""), grant(scope=GMAIL_SEND_SCOPE + " https://mail.google.com/"),
                     grant(scope=[GMAIL_SEND_SCOPE]), grant(scope=GMAIL_SEND_SCOPE + " " + GMAIL_SEND_SCOPE),
                     grant(token_type=4), grant(token_type="Basic"), grant(expires_in=True),
                     grant(expires_in="3600"), grant(expires_in=59), grant(expires_in=86401),
                     grant(access_token=""), grant(access_token="bad\r\nsecret"), grant(access_token="\u00e9"),
                     grant(refresh_token=""), grant(refresh_token_expires_in=86400), grant(error="bad-grant"), None]
        for body in malformed:
            with self.subTest(body=body), self.assertRaisesRegex(setup.SetupError, "EXACT_SEND_ONLY"):
                setup.validate_grant(body, require_refresh=True)
        current = attempt()
        current.consume_callback(callback(current))
        request = Mock(side_effect=[grant(), grant(scope="https://mail.google.com/")])
        with self.assertRaisesRegex(setup.SetupError, "EXACT_SEND_ONLY"):
            setup.exchange_and_verify(current, "synthetic-code", request=request)
        self.assertEqual(request.call_count, 2)

    def test_refresh_rotation_retains_provider_returned_replacement(self):
        current = attempt()
        current.consume_callback(callback(current))
        request = Mock(side_effect=[grant(), grant(refresh_token="replacement-refresh")])
        self.assertEqual(setup.exchange_and_verify(current, "synthetic-code", request=request), "replacement-refresh")

    def test_token_transport_has_fixed_url_method_body_bounds_and_no_redirect(self):
        fields = {"grant_type": "refresh_token", "refresh_token": "synthetic-refresh"}
        opener = Mock(return_value=Response(json.dumps(grant()).encode()))
        self.assertEqual(setup.token_request(fields, opener=opener)["access_token"], "synthetic-access")
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, setup.TOKEN_ENDPOINT)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(parse_qs(request.data.decode()), {k: [v] for k, v in fields.items()})
        self.assertNotIn("synthetic-refresh", request.full_url)
        self.assertEqual(opener.call_args.kwargs["timeout"], 15)
        with self.assertRaisesRegex(setup.SetupError, "REDIRECT_REFUSED"):
            setup._NoRedirect().redirect_request(None, None, 302, "", {}, "https://attacker.invalid")
        mocked = Mock(open=Mock(return_value=Response(json.dumps(grant()).encode())))
        with patch.object(setup, "build_opener", return_value=mocked) as build:
            setup.token_request(fields)
        self.assertIsInstance(build.call_args.args[0], setup._NoRedirect)

    def test_token_response_errors_and_provider_text_are_sanitized(self):
        bodies = [b"[]", b"null", b"invalid-synthetic-secret", b"\xff", b"x" * (setup.MAX_BYTES + 1),
                  b'{"access_token":"one","access_token":"two"}']
        for raw in bodies:
            with self.subTest(raw=raw[:30]), self.assertRaises(setup.SetupError) as caught:
                setup.token_request({}, opener=Mock(return_value=Response(raw)))
            self.assertNotIn("synthetic-secret", str(caught.exception))
        for changed in ({"status": 302}, {"url": "https://attacker.invalid/token"}):
            response = Response(b"{}")
            for key, value in changed.items():
                setattr(response, key, value)
            with self.assertRaisesRegex(setup.SetupError, "RESPONSE_INVALID"):
                setup.token_request({}, opener=Mock(return_value=response))
        with self.assertRaisesRegex(setup.SetupError, "TOKEN_REQUEST_FAILED") as caught:
            setup.token_request({}, opener=Mock(side_effect=OSError("synthetic-secret")))
        self.assertIsNone(caught.exception.__cause__)

    def profile(self):
        return {**{name: "ibkr-test-" + name for name in GMAIL_KEYCHAIN_SERVICE_FIELDS},
                "credential_account": "ibkr-live-ending-3103"}

    def save(self, keychain, **updates):
        values = {"sender": "owner@example.com", "destination": "dest@example.com", "consent_status": "production"}
        values.update(updates)
        setup.save_enrollment(self.profile(), setup.DesktopClient.parse(client_bytes()), "synthetic-refresh", keychain=keychain, **values)

    def test_enrollment_is_create_only_consent_last_no_files_or_commands(self):
        memory = MemoryKeychain()
        with patch.object(setup.os, "open", side_effect=AssertionError("file opened")), patch(
            "titan_brain.live.provider_clients.subprocess.run", side_effect=AssertionError("process launched")
        ):
            self.save(memory)
        expected = ["desktop_client_service", "refresh_token_service", "sender_service", "destination_service", "consent_status_service"]
        self.assertEqual([item.service for item in memory.calls], [self.profile()[field] for field in expected])
        self.assertEqual(len(memory.values), 5)
        with self.assertRaisesRegex(setup.SetupError, "EXISTING_ITEMS_REQUIRE_OWNER_REVIEW"):
            self.save(memory)
        self.assertEqual(len(memory.calls), 5)

    def test_partial_keychain_failures_never_write_consent_or_retry_overwrite(self):
        for failure in range(1, 6):
            memory = MemoryKeychain(fail_at=failure)
            with self.subTest(failure=failure), self.assertRaisesRegex(setup.SetupError, "KEYCHAIN_CREATE_FAILED"):
                self.save(memory)
            self.assertFalse(any("consent_status" in item.service for item in memory.values))
            self.assertEqual(len(memory.values), failure - 1)
            if memory.values:
                with self.assertRaisesRegex(setup.SetupError, "EXISTING_ITEMS_REQUIRE_OWNER_REVIEW"):
                    self.save(memory)
        memory = MemoryKeychain()
        for status in ("testing", "unknown"):
            with self.assertRaisesRegex(setup.SetupError, "DURABLE_PUBLISHING_STATUS"):
                self.save(memory, consent_status=status)
        self.assertFalse(memory.calls)

    def test_security_framework_signature_create_only_and_buffer_zeroing(self):
        item = KeychainItem("ibkr-test-service", "ibkr-live-ending-3103")
        seen = []
        buffers = []
        def add(*args):
            seen.append(args[:6] + (ctypes.string_at(args[6], args[5]), args[7]))
            buffers.append(args[6])
            return 0
        native = Mock(side_effect=add)
        library = SimpleNamespace(SecKeychainAddGenericPassword=native)
        keychain = setup.CreateOnlyMacOSKeychain()
        with patch.object(setup.sys, "platform", "darwin"), patch.object(setup.ctypes, "CDLL", return_value=library) as load, patch.object(
            keychain, "metadata_status", return_value="MISSING"
        ), patch.object(keychain, "read", return_value=b"synthetic-secret"), patch(
            "titan_brain.live.provider_clients.subprocess.run", side_effect=AssertionError("secret subprocess")
        ):
            keychain.add(item, b"synthetic-secret")
        self.assertEqual(load.call_args.args[0], "/System/Library/Frameworks/Security.framework/Security")
        self.assertEqual(native.restype, ctypes.c_int32)
        self.assertEqual(native.argtypes[-1], ctypes.POINTER(ctypes.c_void_p))
        self.assertEqual(seen[0][0], None)
        self.assertEqual(seen[0][6], b"synthetic-secret")
        self.assertEqual(seen[0][7], None)
        self.assertEqual(bytes(buffers[0]), b"\x00" * len(buffers[0]))

    def test_keychain_preexisting_native_race_and_readback_fail_closed(self):
        keychain = setup.CreateOnlyMacOSKeychain()
        item = KeychainItem("ibkr-test-service", "ibkr-live-ending-3103")
        with patch.object(setup.sys, "platform", "darwin"), patch.object(keychain, "metadata_status", return_value="PRESENT"), patch.object(setup.ctypes, "CDLL") as load:
            with self.assertRaisesRegex(setup.SetupError, "ALREADY_EXISTS_OR_UNAVAILABLE"):
                keychain.add(item, b"synthetic-secret")
            load.assert_not_called()
        for result, expected, readback in ((-25299, "CREATE_FAILED", b"synthetic-secret"), (0, "READBACK_FAILED", b"other")):
            native = Mock(return_value=result)
            with patch.object(setup.sys, "platform", "darwin"), patch.object(keychain, "metadata_status", return_value="MISSING"), patch.object(
                setup.ctypes, "CDLL", return_value=SimpleNamespace(SecKeychainAddGenericPassword=native)
            ), patch.object(keychain, "read", return_value=readback), self.assertRaisesRegex(setup.SetupError, expected):
                keychain.add(item, b"synthetic-secret")

    def test_loopback_timeout_cuts_off_incomplete_request_and_does_not_log_it(self):
        peers = []
        def announce(url):
            redirect = parse_qs(urlsplit(url).query)["redirect_uri"][0]
            endpoint = urlsplit(redirect)
            peer = socket.create_connection((endpoint.hostname, endpoint.port), timeout=1)
            peer.sendall(b"GET /oauth2callback?code=synthetic-private-code HTTP/1.1\r\n")
            peers.append(peer)
        output = io.StringIO()
        started = time.monotonic()
        try:
            with redirect_stderr(output), self.assertRaisesRegex(setup.SetupError, "CALLBACK_TIMEOUT"):
                setup.receive_callback(setup.DesktopClient.parse(client_bytes()), "owner@example.com", announce=announce, timeout=0.15)
        finally:
            for peer in peers:
                peer.close()
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertNotIn("synthetic-private-code", output.getvalue())

    def test_loopback_accepts_exact_host_and_rejects_duplicate_host(self):
        workers = []
        statuses = []
        def announce(url):
            query = parse_qs(urlsplit(url).query)
            redirect = urlsplit(query["redirect_uri"][0])
            target = "/oauth2callback?" + urlencode({"state": query["state"][0], "code": "synthetic-private-code"})
            def send():
                for duplicate in (True, False):
                    with socket.create_connection((redirect.hostname, redirect.port), timeout=1) as peer:
                        host = f"127.0.0.1:{redirect.port}"
                        lines = [f"GET {target} HTTP/1.1", f"Host: {host}"]
                        if duplicate:
                            lines.append(f"Host: {host}")
                        peer.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
                        statuses.append(peer.recv(8192).split(b"\r\n")[0])
            worker = threading.Thread(target=send)
            worker.start()
            workers.append(worker)
        output = io.StringIO()
        with redirect_stderr(output), redirect_stdout(output):
            received, code = setup.receive_callback(setup.DesktopClient.parse(client_bytes()), "owner@example.com", announce=announce, timeout=2)
        for worker in workers:
            worker.join(2)
            self.assertFalse(worker.is_alive())
        self.assertEqual(code, "synthetic-private-code")
        self.assertTrue(received.consumed)
        self.assertIn(b"400", statuses[0])
        self.assertIn(b"200", statuses[1])
        self.assertEqual(output.getvalue(), "")

    def test_main_requires_explicit_owner_write_flag_before_any_access(self):
        output = io.StringIO()
        with patch.object(setup.DesktopClient, "load") as load, patch.object(setup, "receive_callback") as receive, patch.object(setup, "CreateOnlyMacOSKeychain") as keychain, redirect_stdout(output):
            result = setup.main(["--source-root", "/unused", "--client-json", "/unused-client.json", "--sender", "owner@example.com", "--destination", "dest@example.com", "--consent-status", "production"])
        self.assertEqual(result, 2)
        load.assert_not_called()
        receive.assert_not_called()
        keychain.assert_not_called()
        self.assertFalse(json.loads(output.getvalue())["trading_activated"])
        self.assertFalse(json.loads(output.getvalue())["message_sent"])

    def test_main_success_stores_only_after_verified_exchange_and_never_enables_route(self):
        root = Path(__file__).resolve().parents[1]
        current = attempt()
        current.consume_callback(callback(current))
        reader = Mock(metadata_status=Mock(return_value="MISSING"))
        output = io.StringIO()
        with patch.object(setup.DesktopClient, "load", return_value=current.client), patch.object(
            setup, "receive_callback", return_value=(current, "synthetic-code")
        ), patch.object(setup, "CreateOnlyMacOSKeychain", return_value=reader), patch.object(
            setup, "exchange_and_verify", return_value="synthetic-refresh"
        ) as exchange, patch.object(setup, "save_enrollment") as save, redirect_stdout(output):
            result = setup.main(["--source-root", str(root), "--client-json", "/private/client.json",
                                 "--sender", "owner@example.com", "--destination", "dest@example.com",
                                 "--consent-status", "production", "--authorize-keychain-create"])
        self.assertEqual(result, 0)
        exchange.assert_called_once()
        save.assert_called_once()
        report = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(report["status"], "KEYCHAIN_ENROLLMENT_COMPLETE")
        self.assertFalse(report["route_ready"])
        self.assertFalse(report["message_sent"])
        self.assertFalse(report["trading_activated"])
        self.assertNotIn("synthetic-refresh", output.getvalue())
        self.assertNotIn("synthetic-code", output.getvalue())


if __name__ == "__main__":
    unittest.main()
