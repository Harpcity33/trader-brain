from __future__ import annotations

import base64
from contextlib import redirect_stderr, redirect_stdout
import ctypes
import hashlib
import io
import itertools
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

    def read(self, item):
        return self.values[item]

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

    def test_google_export_metadata_variants_keep_authorization_pinned_to_v2(self):
        for uri in ("https://accounts.google.com/o/oauth2/auth",
                    "https://accounts.google.com/o/oauth2/v2/auth"):
            with self.subTest(uri=uri):
                client = setup.DesktopClient.parse(client_bytes(auth_uri=uri))
                current = attempt(client=client)
                parsed = urlsplit(current.authorization_url())
                self.assertEqual(parsed.scheme + "://" + parsed.netloc + parsed.path,
                                 "https://accounts.google.com/o/oauth2/v2/auth")
                self.assertEqual(json.loads(client.raw_json)["installed"]["auth_uri"], uri)
                self.assertEqual(parse_qs(parsed.query)["scope"], [GMAIL_SEND_SCOPE])
                self.assertNotIn("synthetic-client-secret", current.authorization_url())

    def test_imported_authorization_metadata_requires_exact_google_allowlist(self):
        for uri in ("http://accounts.google.com/o/oauth2/auth",
                    "https://accounts.google.com.attacker.invalid/o/oauth2/auth",
                    "https://accounts.google.com@attacker.invalid/o/oauth2/auth",
                    "https://accounts.google.com/o/oauth2/auth/",
                    "https://accounts.google.com/o/oauth2/auth?redirect_uri=https://attacker.invalid",
                    "https://accounts.google.com/o/oauth2/auth#fragment",
                    "https://accounts.google.com/o/oauth2/other",
                    "https://accounts.google.com:443/o/oauth2/auth",
                    "https://accounts.google.com/o/oauth2/v2/auth?extra=1",
                    "https://ACCOUNTS.GOOGLE.COM/o/oauth2/auth",
                    "", None, [], {}):
            with self.subTest(uri=uri), self.assertRaisesRegex(setup.SetupError, "DESKTOP_CLIENT_INVALID"):
                setup.DesktopClient.parse(client_bytes(auth_uri=uri))

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

    def client_only_keychain(self, raw=None):
        memory = MemoryKeychain()
        item = KeychainItem(self.profile()["desktop_client_service"], self.profile()["credential_account"])
        memory.values[item] = raw if raw is not None else setup.DesktopClient.parse(client_bytes()).raw_json.encode()
        return memory, item

    def test_recovery_requires_explicit_flag_preserves_client_and_commits_consent_last(self):
        memory, client_item = self.client_only_keychain(client_bytes())
        original = memory.values[client_item]
        with self.assertRaisesRegex(setup.SetupError, "EXISTING_ITEMS_REQUIRE_OWNER_REVIEW"):
            self.save(memory)
        self.assertFalse(memory.calls)
        self.save(memory, authorize_recover_client_only=True)
        expected = ["refresh_token_service", "sender_service", "destination_service", "consent_status_service"]
        self.assertEqual([item.service for item in memory.calls], [self.profile()[field] for field in expected])
        self.assertEqual(memory.values[client_item], original)
        self.assertEqual(len(memory.values), 5)
        with self.assertRaisesRegex(setup.SetupError, "RECOVERY_REQUIRES_MATCHING_CLIENT_ONLY"):
            self.save(memory, authorize_recover_client_only=True)
        self.assertEqual(len(memory.calls), 4)

    def test_recovery_rejects_every_other_metadata_state_before_read_or_write(self):
        required = ("PRESENT", "MISSING", "MISSING", "MISSING", "MISSING")
        for statuses in itertools.product(("PRESENT", "MISSING", "UNAVAILABLE"), repeat=5):
            if statuses == required:
                continue
            memory = Mock(metadata_status=Mock(side_effect=statuses))
            with self.subTest(statuses=statuses), self.assertRaisesRegex(setup.SetupError, "RECOVERY_REQUIRES_MATCHING_CLIENT_ONLY"):
                self.save(memory, authorize_recover_client_only=True)
            memory.read.assert_not_called()
            memory.add.assert_not_called()

    def test_recovery_matches_complete_client_document_in_memory(self):
        for raw in (client_bytes(client_id="other.apps.googleusercontent.com"),
                    client_bytes(client_secret="different-secret"),
                    client_bytes(auth_uri="https://accounts.google.com/o/oauth2/auth"),
                    client_bytes(redirect_uris=["http://127.0.0.1"]),
                    client_bytes(project_id="unexpected-project")):
            memory, _ = self.client_only_keychain(raw)
            with self.subTest(raw=raw[:20]), self.assertRaisesRegex(setup.SetupError, "RECOVERY_CLIENT_MISMATCH") as caught:
                self.save(memory, authorize_recover_client_only=True)
            self.assertFalse(memory.calls)
            self.assertNotIn("secret", str(caught.exception))
        # JSON whitespace and object-key order do not change the imported client.
        reordered = json.dumps(json.loads(client_bytes()), indent=4, sort_keys=True).encode()
        memory, item = self.client_only_keychain(reordered)
        self.save(memory, authorize_recover_client_only=True)
        self.assertEqual(memory.values[item], reordered)

    def test_recovery_unreadable_blank_nonutf8_and_malformed_clients_fail_closed(self):
        for raw in (b"", b" ", b"\xff", b"null", b"[]", b'{"installed":{}}',
                    b'{"installed":{},"installed":{}}', client_bytes() + b"\x00"):
            memory, _ = self.client_only_keychain(raw)
            with self.subTest(raw=raw[:20]), self.assertRaisesRegex(setup.SetupError, "RECOVERY_CLIENT_UNVERIFIABLE"):
                self.save(memory, authorize_recover_client_only=True)
            self.assertFalse(memory.calls)
        memory, _ = self.client_only_keychain()
        with patch.object(memory, "read", side_effect=OSError("synthetic-private-native-error")), self.assertRaisesRegex(
            setup.SetupError, "^GMAIL_SETUP_RECOVERY_CLIENT_UNVERIFIABLE$"
        ) as caught:
            self.save(memory, authorize_recover_client_only=True)
        self.assertIsNone(caught.exception.__cause__)
        self.assertFalse(memory.calls)

    def test_recovery_rechecks_all_metadata_after_owner_authentication(self):
        memory, _ = self.client_only_keychain()
        original_read = memory.read
        def raced_read(item):
            value = original_read(item)
            other = KeychainItem(self.profile()["destination_service"], self.profile()["credential_account"])
            memory.values[other] = b"racing-destination"
            return value
        with patch.object(memory, "read", side_effect=raced_read), self.assertRaisesRegex(
            setup.SetupError, "RECOVERY_REQUIRES_MATCHING_CLIENT_ONLY"
        ):
            self.save(memory, authorize_recover_client_only=True)
        self.assertFalse(memory.calls)

    def test_recovery_write_failures_leave_client_untouched_and_consent_absent(self):
        for failure in range(1, 5):
            memory, item = self.client_only_keychain()
            original = memory.values[item]
            memory.fail_at = failure
            with self.subTest(failure=failure), self.assertRaisesRegex(setup.SetupError, "KEYCHAIN_CREATE_FAILED"):
                self.save(memory, authorize_recover_client_only=True)
            self.assertEqual(memory.values[item], original)
            self.assertFalse(any("consent_status" in saved.service for saved in memory.values))
            self.assertEqual(len(memory.values), failure)

    def native_reader(self, raw, *, status=0, free_status=0, null_data=False):
        buffer = ctypes.create_string_buffer(raw)
        def find(*args):
            ctypes.cast(args[5], ctypes.POINTER(ctypes.c_uint32))[0] = len(raw)
            if not null_data:
                ctypes.cast(args[6], ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(buffer)
            return status
        native = Mock(side_effect=find)
        free = Mock(return_value=free_status)
        return SimpleNamespace(SecKeychainFindGenericPassword=native, SecKeychainItemFreeContent=free), buffer

    def test_native_read_exact_account_signature_zero_free_and_no_subprocess(self):
        keychain = setup.CreateOnlyMacOSKeychain()
        item = KeychainItem("ibkr-test-service", "ibkr-live-ending-3103")
        library, buffer = self.native_reader(b"synthetic-native-secret")
        with patch.object(setup.sys, "platform", "darwin"), patch.object(setup.ctypes, "CDLL", return_value=library), patch(
            "titan_brain.live.provider_clients.subprocess.run", side_effect=AssertionError("secret subprocess")
        ):
            self.assertEqual(keychain.read(item), b"synthetic-native-secret")
        native = library.SecKeychainFindGenericPassword
        args = native.call_args.args
        self.assertEqual(args[:5], (None, len(item.service), item.service.encode(), len(item.account), item.account.encode()))
        self.assertIsNone(args[7])
        self.assertEqual(native.restype, ctypes.c_int32)
        self.assertEqual(native.argtypes[5:], [ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p)])
        library.SecKeychainItemFreeContent.assert_called_once()
        self.assertEqual(bytes(buffer), b"\x00" * len(buffer))

    def test_native_read_missing_denied_invalid_and_free_failures_are_sanitized(self):
        keychain = setup.CreateOnlyMacOSKeychain()
        item = KeychainItem("ibkr-test-service", "ibkr-live-ending-3103")
        cases = [(b"private-secret", {"status": status}, "READ_FAILED") for status in (-25300, -25293, -128, -25308)]
        cases += [(b"", {}, "ITEM_INVALID"), (b"bad\x00data", {}, "ITEM_INVALID"),
                  (b"x" * (setup.MAX_BYTES + 1), {}, "ITEM_INVALID"),
                  (b"private-secret", {"null_data": True}, "ITEM_INVALID"),
                  (b"private-secret", {"free_status": -50}, "READ_FAILED")]
        for raw, options, error in cases:
            library, buffer = self.native_reader(raw, **options)
            with self.subTest(options=options, length=len(raw)), patch.object(setup.sys, "platform", "darwin"), patch.object(
                setup.ctypes, "CDLL", return_value=library
            ), self.assertRaisesRegex(setup.SetupError, error) as caught:
                keychain.read(item)
            self.assertNotIn("private-secret", str(caught.exception))
            if not options.get("null_data"):
                library.SecKeychainItemFreeContent.assert_called_once()
                self.assertEqual(bytes(buffer), b"\x00" * len(buffer))
            else:
                library.SecKeychainItemFreeContent.assert_not_called()
        with patch.object(setup.sys, "platform", "darwin"), patch.object(setup.ctypes, "CDLL", side_effect=OSError("private-secret")), self.assertRaisesRegex(
            setup.SetupError, "^GMAIL_SETUP_KEYCHAIN_READ_FAILED$"
        ) as caught:
            keychain.read(item)
        self.assertIsNone(caught.exception.__cause__)

    def test_native_setup_refuses_unscoped_access_and_readback_failure_is_partial(self):
        keychain = setup.CreateOnlyMacOSKeychain()
        with patch.object(setup.sys, "platform", "darwin"), patch.object(setup.ctypes, "CDLL") as load, patch(
            "titan_brain.live.provider_clients.subprocess.run", side_effect=AssertionError("unscoped probe")
        ):
            self.assertEqual(keychain.metadata_status(KeychainItem("test-service")), "UNAVAILABLE")
            with self.assertRaisesRegex(setup.SetupError, "READ_UNAVAILABLE"):
                keychain.read(KeychainItem("test-service"))
            with self.assertRaisesRegex(setup.SetupError, "WRITE_UNAVAILABLE"):
                keychain.add(KeychainItem("test-service"), b"secret")
            load.assert_not_called()
        item = KeychainItem("ibkr-test-service", "ibkr-live-ending-3103")
        library = SimpleNamespace(SecKeychainAddGenericPassword=Mock(return_value=0))
        with patch.object(setup.sys, "platform", "darwin"), patch.object(keychain, "metadata_status", return_value="MISSING"), patch.object(
            setup.ctypes, "CDLL", return_value=library
        ), patch.object(keychain, "read", side_effect=setup.SetupError("GMAIL_SETUP_KEYCHAIN_READ_FAILED")), self.assertRaisesRegex(
            setup.SetupError, "^GMAIL_SETUP_KEYCHAIN_READBACK_FAILED_REVIEW_PARTIAL_ENROLLMENT$"
        ):
            keychain.add(item, b"synthetic-secret")
        library.SecKeychainAddGenericPassword.assert_called_once()

    def test_setup_native_add_and_unscoped_enrollment_errors_are_sanitized(self):
        keychain = setup.CreateOnlyMacOSKeychain()
        item = KeychainItem("ibkr-test-service", "ibkr-live-ending-3103")
        for library in (None, SimpleNamespace(SecKeychainAddGenericPassword=Mock(side_effect=OSError("private-secret")))):
            with self.subTest(library=library), patch.object(setup.sys, "platform", "darwin"), patch.object(
                keychain, "metadata_status", return_value="MISSING"
            ), patch.object(setup.ctypes, "CDLL", return_value=library), self.assertRaisesRegex(setup.SetupError, "^GMAIL_SETUP_KEYCHAIN_CREATE_FAILED$") as caught:
                keychain.add(item, b"synthetic-secret")
            self.assertIsNone(caught.exception.__cause__)
        memory = Mock()
        for account in (None, "", 42):
            with self.subTest(account=account), self.assertRaisesRegex(setup.SetupError, "KEYCHAIN_ACCOUNT_REQUIRED"):
                setup.require_enrollment_state({**self.profile(), "credential_account": account},
                                               setup.DesktopClient.parse(client_bytes()), keychain=memory,
                                               authorize_recover_client_only=True)
        memory.metadata_status.assert_not_called()
        memory.read.assert_not_called()

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
        responses = []
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
                        response = b""
                        while chunk := peer.recv(8192):
                            response += chunk
                        responses.append(response)
                        statuses.append(response.split(b"\r\n")[0])
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
        page = responses[1].decode()
        self.assertIn("<title>Titan Gmail setup</title>", page)
        self.assertIn(setup.CALLBACK_CLEANUP_SCRIPT, page)
        self.assertIn("Cache-Control: no-store", page)
        self.assertIn("Referrer-Policy: no-referrer", page)
        expected_hash = base64.b64encode(hashlib.sha256(setup.CALLBACK_CLEANUP_SCRIPT.encode()).digest()).decode()
        self.assertIn("script-src 'sha256-" + expected_hash + "'", page)
        self.assertNotIn("synthetic-private-code", page)
        self.assertNotIn(received.state, page)
        self.assertNotIn("unsafe-inline", page)

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

    def main_args(self, *, recovery=True):
        args = ["--source-root", str(Path(__file__).resolve().parents[1]),
                "--client-json", "/private/client.json", "--sender", "owner@example.com",
                "--destination", "dest@example.com", "--consent-status", "production",
                "--authorize-keychain-create"]
        return args + (["--authorize-recover-client-only"] if recovery else [])

    def test_main_recovery_requires_create_authorization_before_access(self):
        args = self.main_args()
        args.remove("--authorize-keychain-create")
        output = io.StringIO()
        with patch.object(setup.DesktopClient, "load") as load, patch.object(setup, "receive_callback") as receive, redirect_stdout(output):
            result = setup.main(args)
        self.assertEqual(result, 2)
        self.assertEqual(json.loads(output.getvalue())["error"], "GMAIL_SETUP_OWNER_KEYCHAIN_AUTHORIZATION_REQUIRED")
        load.assert_not_called()
        receive.assert_not_called()

    def test_main_recovery_checks_matching_client_before_fresh_consent_and_before_writes(self):
        client = setup.DesktopClient.parse(client_bytes())
        previous = attempt(client=client)
        previous.consume_callback(callback(previous, code="previous-code"))
        current = attempt(client=client)
        memory, client_item = self.client_only_keychain()
        reads = []
        original_read = memory.read
        def read(item):
            reads.append(item)
            return original_read(item)
        def receive(*args, **kwargs):
            self.assertEqual(reads, [client_item])
            self.assertNotEqual(current.state, previous.state)
            self.assertNotEqual(current.verifier, previous.verifier)
            self.assertEqual(parse_qs(urlsplit(current.authorization_url()).query)["prompt"], ["consent"])
            return current, current.consume_callback(callback(current, code="fresh-code"))
        requests = Mock(side_effect=[grant(), grant()])
        original_exchange = setup.exchange_and_verify
        output = io.StringIO()
        with patch.object(setup.DesktopClient, "load", return_value=client), patch.object(
            setup, "select_account_gmail_profile", return_value=("ibkr", self.profile())
        ), patch.object(setup, "CreateOnlyMacOSKeychain", return_value=memory), patch.object(memory, "read", side_effect=read), patch.object(
            setup, "receive_callback", side_effect=receive
        ), patch.object(setup, "exchange_and_verify", side_effect=lambda a, c: original_exchange(a, c, request=requests)), redirect_stdout(output):
            result = setup.main(self.main_args())
        self.assertEqual(result, 0)
        self.assertEqual(reads, [client_item, client_item])
        self.assertEqual(requests.call_args_list[0].args[0]["code"], "fresh-code")
        self.assertEqual(len(memory.calls), 4)
        report = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(report["status"], "KEYCHAIN_ENROLLMENT_COMPLETE")
        self.assertFalse(report["route_ready"])
        self.assertFalse(report["message_sent"])
        self.assertFalse(report["trading_activated"])
        for private in ("fresh-code", "previous-code", "synthetic-refresh", "synthetic-client-secret"):
            self.assertNotIn(private, output.getvalue())

    def test_main_invalid_recovery_state_never_requests_google_consent(self):
        for raw, recovery, error in ((client_bytes(client_secret="different-secret"), True, "RECOVERY_CLIENT_MISMATCH"),
                                     (client_bytes(), False, "EXISTING_ITEMS_REQUIRE_OWNER_REVIEW")):
            memory, _ = self.client_only_keychain(raw)
            output = io.StringIO()
            with self.subTest(recovery=recovery), patch.object(setup.DesktopClient, "load", return_value=setup.DesktopClient.parse(client_bytes())), patch.object(
                setup, "select_account_gmail_profile", return_value=("ibkr", self.profile())
            ), patch.object(setup, "CreateOnlyMacOSKeychain", return_value=memory), patch.object(setup, "receive_callback") as receive, patch.object(
                setup, "exchange_and_verify"
            ) as exchange, redirect_stdout(output):
                result = setup.main(self.main_args(recovery=recovery))
            self.assertEqual(result, 2)
            self.assertIn(error, json.loads(output.getvalue())["error"])
            receive.assert_not_called()
            exchange.assert_not_called()
            self.assertFalse(memory.calls)

    def test_main_recovery_consent_interval_races_fail_before_any_writes(self):
        for changed_field in GMAIL_KEYCHAIN_SERVICE_FIELDS:
            memory, client_item = self.client_only_keychain()
            current = attempt()
            current.consume_callback(callback(current))
            def exchange(*args):
                item = KeychainItem(self.profile()[changed_field], self.profile()["credential_account"])
                memory.values[item] = (client_bytes(client_secret="raced-secret") if item == client_item else b"raced-value")
                return "synthetic-refresh"
            output = io.StringIO()
            with self.subTest(changed_field=changed_field), patch.object(setup.DesktopClient, "load", return_value=current.client), patch.object(
                setup, "select_account_gmail_profile", return_value=("ibkr", self.profile())
            ), patch.object(setup, "CreateOnlyMacOSKeychain", return_value=memory), patch.object(
                setup, "receive_callback", return_value=(current, "synthetic-code")
            ), patch.object(setup, "exchange_and_verify", side_effect=exchange), redirect_stdout(output):
                result = setup.main(self.main_args())
            self.assertEqual(result, 2)
            self.assertFalse(memory.calls)
            self.assertIn("GMAIL_SETUP_RECOVERY_", json.loads(output.getvalue().splitlines()[-1])["error"])


if __name__ == "__main__":
    unittest.main()
