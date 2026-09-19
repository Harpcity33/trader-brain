from __future__ import annotations

import ctypes
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from titan_brain.live import local_assembly as assembly_module
from titan_brain.live import provider_clients as clients


ROOT = Path(__file__).resolve().parents[1]
ITEMS = tuple(sorted(clients.IBKR_GMAIL_NATIVE_ITEMS, key=lambda item: item.service))


class NativeFixture:
    """In-memory FFI stand-in; never loads Security.framework or a real item."""

    def __init__(self, raw=b"synthetic-private-value", *, status=0, result_type=7,
                 result_pointer=3003, data_pointer=True, length=None):
        self.buffer = ctypes.create_string_buffer(raw)
        self.symbols = {}
        self.strings = {}
        self.queries = []
        self.released = []
        self.result_pointer = result_pointer

        def make_string(_allocator, value, encoding):
            assert encoding == 0x08000100
            reference = 1001 + len(self.strings)
            self.strings[reference] = value.decode()
            return reference

        def make_dictionary(_allocator, keys, values, count, key_callbacks, value_callbacks):
            self.queries.append({keys[index]: values[index] for index in range(count)})
            return 2002

        def copy(_query, out):
            if out is not None:
                ctypes.cast(out, ctypes.POINTER(ctypes.c_void_p))[0] = result_pointer
            return status

        def release(reference):
            self.released.append(reference.value if isinstance(reference, ctypes.c_void_p) else reference)

        self.security = SimpleNamespace(SecItemCopyMatching=Mock(side_effect=copy))
        self.core = SimpleNamespace(
            CFStringCreateWithCString=Mock(side_effect=make_string),
            CFDictionaryCreate=Mock(side_effect=make_dictionary),
            CFGetTypeID=Mock(return_value=result_type),
            CFDataGetTypeID=Mock(return_value=7),
            CFDataGetLength=Mock(return_value=len(raw) if length is None else length),
            CFDataGetBytePtr=Mock(return_value=ctypes.addressof(self.buffer) if data_pointer else None),
            CFRelease=Mock(side_effect=release),
        )

    def library(self, path):
        if path == "/System/Library/Frameworks/Security.framework/Security":
            return self.security
        if path == "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation":
            return self.core
        raise AssertionError("unexpected native library")

    def symbol(self, _library, name, *, address=False):
        if name not in self.symbols:
            self.symbols[name] = 4000 + len(self.symbols)
        return self.symbols[name]

    def named_query(self):
        names = {value: name for name, value in self.symbols.items()}
        return {names[key]: self.strings.get(value, names.get(value)) for key, value in self.queries[-1].items()}


class GmailNativeRuntimeTests(unittest.TestCase):
    def reader(self):
        return clients.GmailNativeMacOSKeychain(items=ITEMS)

    def invoke(self, fixture, *, item=ITEMS[0], metadata=False, text=False):
        with patch.object(clients.sys, "platform", "darwin"), patch.object(clients.ctypes, "CDLL", side_effect=fixture.library), patch.object(
            clients, "_native_keychain_symbol", side_effect=fixture.symbol
        ), patch.object(clients.subprocess, "run", side_effect=AssertionError("secret subprocess")):
            reader = self.reader()
            if metadata:
                return reader.metadata_status(item)
            return reader.read_text(item) if text else reader.read(item)

    def test_exact_read_only_query_requests_no_ui_and_releases_owned_cf_objects(self):
        fixture = NativeFixture()
        self.assertEqual(self.invoke(fixture), b"synthetic-private-value")
        self.assertEqual(fixture.named_query(), {
            "kSecClass": "kSecClassGenericPassword",
            "kSecAttrService": ITEMS[0].service,
            "kSecAttrAccount": "ibkr-live-ending-3103",
            "kSecMatchLimit": "kSecMatchLimitOne",
            "kSecUseAuthenticationUI": "kSecUseAuthenticationUIFail",
            "kSecUseDataProtectionKeychain": "kCFBooleanFalse",
            "kSecAttrSynchronizable": "kCFBooleanFalse",
            "kSecReturnData": "kCFBooleanTrue",
        })
        self.assertEqual(fixture.released, [3003, 2002, 1002, 1001])
        self.assertEqual(fixture.security.SecItemCopyMatching.argtypes,
                         [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)])
        self.assertEqual(fixture.security.SecItemCopyMatching.restype, ctypes.c_int32)
        self.assertEqual(fixture.core.CFDictionaryCreate.argtypes[3], ctypes.c_long)
        self.assertEqual(fixture.core.CFDataGetLength.restype, ctypes.c_long)
        self.assertEqual(fixture.core.CFDataGetBytePtr.restype, ctypes.c_void_p)
        self.assertEqual(fixture.core.CFDictionaryCreate.call_args.args[4:],
                         (fixture.symbols["kCFTypeDictionaryKeyCallBacks"], fixture.symbols["kCFTypeDictionaryValueCallBacks"]))
        # The API-owned CFData is immutable: it is released, never mutated.
        self.assertEqual(fixture.buffer.value, b"synthetic-private-value")
        self.assertFalse(hasattr(self.reader(), "add"))
        self.assertFalse(hasattr(self.reader(), "delete"))
        self.assertEqual(set(vars(fixture.security)), {"SecItemCopyMatching"})

    def test_only_the_five_exact_ibkr_gmail_items_are_permitted(self):
        for item in ITEMS:
            with self.subTest(item=item):
                fixture = NativeFixture()
                self.assertEqual(self.invoke(fixture, item=item), b"synthetic-private-value")
                self.assertEqual(fixture.named_query()["kSecAttrService"], item.service)
        disallowed = [clients.KeychainItem(ITEMS[0].service),
                      clients.KeychainItem(ITEMS[0].service, "ending-7153"),
                      clients.KeychainItem("titan-full-live-gmail-refresh-token", "ending-7153"),
                      clients.KeychainItem("titan-full-live-ibkr-ending-3103-control-authentication-key", "ibkr-live-ending-3103"),
                      clients.KeychainItem("titan-massive-key", "ibkr-live-ending-3103")]
        with patch.object(clients.ctypes, "CDLL", side_effect=AssertionError("unscoped native access")):
            for item in disallowed:
                with self.subTest(item=item), self.assertRaisesRegex(clients.CredentialUnavailable, "SCOPE_REFUSED"):
                    self.reader().read(item)
                self.assertEqual(self.reader().metadata_status(item), "UNAVAILABLE")
            for items in (ITEMS[:-1], (*ITEMS, ITEMS[0]), (ITEMS[0],) * 5, (*ITEMS[:-1], disallowed[0])):
                with self.assertRaisesRegex(clients.CredentialUnavailable, "SCOPE_REFUSED"):
                    clients.GmailNativeMacOSKeychain(items=items)

    def test_metadata_queries_never_request_or_copy_secret_data(self):
        fixture = NativeFixture()
        self.assertEqual(self.invoke(fixture, metadata=True), "PRESENT")
        self.assertNotIn("kSecReturnData", fixture.named_query())
        self.assertEqual(fixture.named_query()["kSecUseAuthenticationUI"], "kSecUseAuthenticationUIFail")
        self.assertIsNone(fixture.security.SecItemCopyMatching.call_args.args[1])
        fixture.core.CFDataGetBytePtr.assert_not_called()
        self.assertEqual(fixture.released, [2002, 1002, 1001])
        self.assertEqual(self.invoke(NativeFixture(status=-25300), metadata=True), "MISSING")
        self.assertEqual(self.invoke(NativeFixture(status=-25308), metadata=True), "UNAVAILABLE")

    def test_native_failures_and_owner_auth_requirements_are_sanitized_without_retry(self):
        for status, suffix in ((-25300, "ITEM_MISSING"), (-25308, "OWNER_AUTH_REQUIRED"),
                               (-25293, "OWNER_AUTH_REQUIRED"), (-128, "OWNER_AUTH_REQUIRED"),
                               (-50, "READ_FAILED")):
            fixture = NativeFixture(status=status)
            with self.subTest(status=status), self.assertRaisesRegex(clients.CredentialUnavailable, suffix) as caught:
                self.invoke(fixture)
            self.assertNotIn("synthetic-private-value", str(caught.exception))
            fixture.security.SecItemCopyMatching.assert_called_once()
            self.assertEqual(fixture.released, [3003, 2002, 1002, 1001])
        fixture = NativeFixture()
        fixture.security.SecItemCopyMatching.side_effect = OSError("synthetic-private-value")
        with self.assertRaisesRegex(clients.CredentialUnavailable, "READ_FAILED") as caught:
            self.invoke(fixture)
        self.assertIsNone(caught.exception.__cause__)
        self.assertEqual(fixture.released, [2002, 1002, 1001])

    def test_native_invalid_empty_oversized_binary_and_nonutf8_results_fail_closed(self):
        for fixture in (NativeFixture(raw=b""), NativeFixture(length=65537), NativeFixture(length=-1),
                        NativeFixture(raw=b"private\x00data"), NativeFixture(result_type=9),
                        NativeFixture(result_pointer=None), NativeFixture(data_pointer=False)):
            with self.subTest(fixture=fixture), self.assertRaisesRegex(clients.CredentialUnavailable, "ITEM_INVALID"):
                self.invoke(fixture)
            self.assertIn(2002, fixture.released)
        with self.assertRaisesRegex(clients.CredentialUnavailable, "ITEM_NOT_UTF8") as caught:
            self.invoke(NativeFixture(raw=b"\xff"), text=True)
        self.assertIsNone(caught.exception.__cause__)

    def test_native_unavailable_allocations_release_prior_objects_and_never_query(self):
        fixture = NativeFixture()
        fixture.core.CFStringCreateWithCString.side_effect = [1001, None]
        with self.assertRaisesRegex(clients.CredentialUnavailable, "UNAVAILABLE"):
            self.invoke(fixture)
        self.assertEqual(fixture.released, [1001])
        fixture.security.SecItemCopyMatching.assert_not_called()
        fixture = NativeFixture()
        fixture.core.CFDictionaryCreate.side_effect = None
        fixture.core.CFDictionaryCreate.return_value = None
        with self.assertRaisesRegex(clients.CredentialUnavailable, "UNAVAILABLE"):
            self.invoke(fixture)
        self.assertEqual(fixture.released, [1002, 1001])
        fixture.security.SecItemCopyMatching.assert_not_called()
        with patch.object(clients.sys, "platform", "linux"), patch.object(clients.ctypes, "CDLL") as load:
            with self.assertRaisesRegex(clients.CredentialUnavailable, "UNAVAILABLE"):
                self.reader().read(ITEMS[0])
            load.assert_not_called()
        with patch.object(clients.sys, "platform", "darwin"), patch.object(clients.ctypes, "CDLL", side_effect=OSError("synthetic-private-error")):
            with self.assertRaisesRegex(clients.CredentialUnavailable, "READ_FAILED") as caught:
                self.reader().read(ITEMS[0])
        self.assertIsNone(caught.exception.__cause__)


class MemoryKeychain:
    def __init__(self, *, falsey=False):
        self.falsey = falsey
        self.calls = []

    def __bool__(self):
        return not self.falsey

    def read_text(self, item):
        self.calls.append(item)
        if item.service.endswith("desktop-client"):
            return json.dumps({"installed": {
                "client_id": "synthetic.apps.googleusercontent.com", "client_secret": "synthetic-secret",
                "token_uri": "https://oauth2.googleapis.com/token", "redirect_uris": ["http://localhost"],
            }})
        if item.service.endswith("refresh-token"):
            return "synthetic-refresh"
        if item.service.endswith("consent-status"):
            return "production"
        return "owner@example.com"


class GmailNativeAssemblySelectionTests(unittest.TestCase):
    def assembly(self, *, keychain=None, legacy=False):
        assembly = assembly_module.LocalProviderAssembly(
            release_root=ROOT, install_root=ROOT, keychain=keychain,
            full_live_config_name="full_live.json" if legacy else "full_live_ibkr.json",
        )
        profile = "gmail" if legacy else "ibkr_gmail"
        assembly.profile[profile] = {**assembly.profile[profile], "enabled": True}
        return assembly

    def test_default_ibkr_gmail_uses_native_only_for_five_items_and_keeps_other_custodian(self):
        generic = MemoryKeychain()
        native = MemoryKeychain()
        with patch.object(assembly_module, "MacOSKeychain", return_value=generic), patch.object(
            assembly_module, "GmailNativeMacOSKeychain", return_value=native
        ) as factory:
            assembly = self.assembly()
            binding = assembly.gmail_binding()
            self.assertIs(assembly.gmail_binding(), binding)
        self.assertIs(assembly.keychain, generic)
        self.assertEqual(generic.calls, [])
        self.assertEqual(frozenset(native.calls), clients.IBKR_GMAIL_NATIVE_ITEMS)
        self.assertEqual(len(native.calls), 5)
        factory.assert_called_once()
        self.assertEqual(frozenset(factory.call_args.kwargs["items"]), clients.IBKR_GMAIL_NATIVE_ITEMS)

    def test_explicit_injected_keychains_keep_all_five_reads_even_if_falsey(self):
        for falsey in (False, True):
            injected = MemoryKeychain(falsey=falsey)
            with self.subTest(falsey=falsey), patch.object(assembly_module, "MacOSKeychain") as generic, patch.object(
                assembly_module, "GmailNativeMacOSKeychain"
            ) as native:
                assembly = self.assembly(keychain=injected)
                assembly.gmail_binding()
            self.assertIs(assembly.keychain, injected)
            self.assertEqual(frozenset(injected.calls), clients.IBKR_GMAIL_NATIVE_ITEMS)
            generic.assert_not_called()
            native.assert_not_called()

    def test_default_legacy_gmail_stays_on_existing_reader(self):
        generic = MemoryKeychain()
        with patch.object(assembly_module, "MacOSKeychain", return_value=generic), patch.object(
            assembly_module, "GmailNativeMacOSKeychain"
        ) as native:
            assembly = self.assembly(legacy=True)
            assembly.gmail_binding()
        native.assert_not_called()
        self.assertEqual(len(generic.calls), 5)
        self.assertEqual({item.account for item in generic.calls}, {"ending-7153"})
        self.assertFalse(frozenset(generic.calls) & clients.IBKR_GMAIL_NATIVE_ITEMS)

    def test_changed_ibkr_gmail_locator_fails_before_native_or_generic_reads(self):
        generic = MemoryKeychain()
        with patch.object(assembly_module, "MacOSKeychain", return_value=generic), patch.object(clients.ctypes, "CDLL") as load:
            assembly = self.assembly()
            assembly.profile["ibkr_gmail"]["sender_service"] = "titan-full-live-ibkr-ending-3103-control-authentication-key"
            with self.assertRaisesRegex(clients.CredentialUnavailable, "SCOPE_REFUSED"):
                assembly.gmail_binding()
        self.assertEqual(generic.calls, [])
        load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
