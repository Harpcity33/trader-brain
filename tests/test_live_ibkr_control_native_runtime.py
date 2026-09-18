"""Hermetic control custody tests: no framework, Keychain, or broker access."""
from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import patch

from titan_brain.live import ibkr_control_setup, local_assembly as assembly_module
from titan_brain.live import provider_clients as clients
from titan_brain.live.broker.ibkr_transport import IBKR_TRANSPORT_ID
from titan_brain.live.provider_profile import IbkrLocalProviderProfile
from tests.test_live_gmail_native_runtime import NativeFixture


ROOT = Path(__file__).resolve().parents[1]
ITEM = clients.IBKR_CONTROL_NATIVE_ITEM
KEY = b"a" * 64


class ControlNativeRuntimeTests(unittest.TestCase):
    def invoke(self, fixture, *, item=ITEM, metadata=False, text=False):
        with patch.object(clients.sys, "platform", "darwin"), patch.object(
            clients.ctypes, "CDLL", side_effect=fixture.library
        ), patch.object(clients, "_native_keychain_symbol", side_effect=fixture.symbol), patch.object(
            clients.subprocess, "run", side_effect=AssertionError("no subprocess fallback")
        ):
            reader = clients.IbkrControlNativeMacOSKeychain(item=ITEM)
            if metadata:
                return reader.metadata_status(item)
            return reader.read_text(item) if text else reader.read(item)

    def test_exact_native_read_uses_existing_acl_and_fail_on_auth_ui(self):
        fixture = NativeFixture(raw=KEY)
        self.assertEqual(self.invoke(fixture), KEY)
        self.assertEqual(fixture.named_query(), {
            "kSecClass": "kSecClassGenericPassword",
            "kSecAttrService": ITEM.service,
            "kSecAttrAccount": ITEM.account,
            "kSecMatchLimit": "kSecMatchLimitOne",
            "kSecUseAuthenticationUI": "kSecUseAuthenticationUIFail",
            "kSecUseDataProtectionKeychain": "kCFBooleanFalse",
            "kSecAttrSynchronizable": "kCFBooleanFalse",
            "kSecReturnData": "kCFBooleanTrue",
        })
        fixture.security.SecItemCopyMatching.assert_called_once()
        self.assertEqual(set(vars(fixture.security)), {"SecItemCopyMatching"})
        self.assertEqual(fixture.released, [3003, 2002, 1002, 1001])
        self.assertEqual(fixture.buffer.value, KEY)  # Immutable CFData is not modified.
        reader = clients.IbkrControlNativeMacOSKeychain(item=ITEM)
        self.assertEqual(reader.maximum_bytes, 4096)
        for method in ("add", "delete", "sign", "activate"):
            self.assertFalse(hasattr(reader, method))
        self.assertIs(ibkr_control_setup.CONTROL_ITEM, ITEM)

    def test_scope_is_exact_single_control_item_and_gmail_remains_disjoint(self):
        disallowed = [*clients.IBKR_GMAIL_NATIVE_ITEMS,
                      clients.KeychainItem(ITEM.service),
                      clients.KeychainItem(ITEM.service, "ending-7153"),
                      clients.KeychainItem("titan-full-live-control-key", ITEM.account),
                      clients.KeychainItem("titan-massive-key", ITEM.account)]
        with patch.object(clients.ctypes, "CDLL", side_effect=AssertionError("unscoped access")), patch.object(
            clients.subprocess, "run", side_effect=AssertionError("fallback")
        ):
            for item in disallowed:
                with self.subTest(item=item):
                    with self.assertRaisesRegex(clients.CredentialUnavailable, "IBKR_CONTROL_NATIVE_KEYCHAIN_SCOPE_REFUSED"):
                        clients.IbkrControlNativeMacOSKeychain(item=item)
                    reader = clients.IbkrControlNativeMacOSKeychain(item=ITEM)
                    with self.assertRaisesRegex(clients.CredentialUnavailable, "IBKR_CONTROL_NATIVE_KEYCHAIN_SCOPE_REFUSED"):
                        reader.read(item)
                    self.assertEqual(reader.metadata_status(item), "UNAVAILABLE")
            gmail = clients.GmailNativeMacOSKeychain(items=tuple(clients.IBKR_GMAIL_NATIVE_ITEMS))
            with self.assertRaisesRegex(clients.CredentialUnavailable, "GMAIL_NATIVE_KEYCHAIN_SCOPE_REFUSED"):
                gmail.read(ITEM)

    def test_metadata_never_requests_or_copies_secret(self):
        fixture = NativeFixture(raw=KEY)
        self.assertEqual(self.invoke(fixture, metadata=True), "PRESENT")
        self.assertNotIn("kSecReturnData", fixture.named_query())
        self.assertEqual(fixture.named_query()["kSecUseAuthenticationUI"], "kSecUseAuthenticationUIFail")
        self.assertIsNone(fixture.security.SecItemCopyMatching.call_args.args[1])
        fixture.core.CFDataGetBytePtr.assert_not_called()
        self.assertEqual(fixture.released, [2002, 1002, 1001])

    def test_missing_owner_auth_and_other_failures_are_fixed_no_retry(self):
        for status, suffix in ((-25300, "ITEM_MISSING"), (-25308, "OWNER_AUTH_REQUIRED"),
                               (-25293, "OWNER_AUTH_REQUIRED"), (-128, "OWNER_AUTH_REQUIRED"),
                               (-50, "READ_FAILED")):
            fixture = NativeFixture(raw=KEY, status=status)
            with self.subTest(status=status), self.assertRaises(clients.CredentialUnavailable) as caught:
                self.invoke(fixture)
            self.assertEqual(caught.exception.code, "CREDENTIAL_IBKR_CONTROL_NATIVE_KEYCHAIN_" + suffix)
            fixture.security.SecItemCopyMatching.assert_called_once()
            self.assertNotIn(KEY.decode(), str(caught.exception))
            self.assertEqual(fixture.released, [3003, 2002, 1002, 1001])
        self.assertEqual(self.invoke(NativeFixture(status=-25300), metadata=True), "MISSING")
        self.assertEqual(self.invoke(NativeFixture(status=-25308), metadata=True), "UNAVAILABLE")
        fixture = NativeFixture(raw=KEY)
        fixture.security.SecItemCopyMatching.side_effect = OSError("synthetic-private-error")
        with self.assertRaises(clients.CredentialUnavailable) as caught:
            self.invoke(fixture)
        self.assertEqual(caught.exception.code, "CREDENTIAL_IBKR_CONTROL_NATIVE_KEYCHAIN_READ_FAILED")
        self.assertIsNone(caught.exception.__cause__)
        self.assertNotIn("synthetic-private", str(caught.exception))
        fixture.security.SecItemCopyMatching.assert_called_once()
        self.assertEqual(fixture.released, [2002, 1002, 1001])

    def test_invalid_private_data_is_rejected_and_cf_allocations_are_released(self):
        for fixture in (NativeFixture(raw=b""), NativeFixture(length=4097), NativeFixture(length=-1),
                        NativeFixture(raw=b"private\x00data"), NativeFixture(result_type=9),
                        NativeFixture(result_pointer=None), NativeFixture(data_pointer=False)):
            with self.subTest(fixture=fixture), self.assertRaisesRegex(
                clients.CredentialUnavailable, "IBKR_CONTROL_NATIVE_KEYCHAIN_ITEM_INVALID"
            ):
                self.invoke(fixture)
            self.assertIn(2002, fixture.released)
        with self.assertRaisesRegex(clients.CredentialUnavailable, "IBKR_CONTROL_NATIVE_KEYCHAIN_ITEM_NOT_UTF8") as caught:
            self.invoke(NativeFixture(raw=b"\xff"), text=True)
        self.assertIsNone(caught.exception.__cause__)
        fixture = NativeFixture(raw=b"a" * 4096)
        self.assertEqual(self.invoke(fixture), b"a" * 4096)

    def test_unavailable_platform_and_allocations_never_query(self):
        with patch.object(clients.sys, "platform", "linux"), patch.object(clients.ctypes, "CDLL") as load:
            with self.assertRaisesRegex(clients.CredentialUnavailable, "IBKR_CONTROL_NATIVE_KEYCHAIN_UNAVAILABLE"):
                clients.IbkrControlNativeMacOSKeychain(item=ITEM).read(ITEM)
            load.assert_not_called()
        fixture = NativeFixture()
        fixture.core.CFStringCreateWithCString.side_effect = [1001, None]
        with self.assertRaisesRegex(clients.CredentialUnavailable, "IBKR_CONTROL_NATIVE_KEYCHAIN_UNAVAILABLE"):
            self.invoke(fixture)
        self.assertEqual(fixture.released, [1001])
        fixture.security.SecItemCopyMatching.assert_not_called()
        fixture = NativeFixture()
        with patch.object(fixture, "symbol", side_effect=clients.CredentialUnavailable(
            "CREDENTIAL_GMAIL_NATIVE_KEYCHAIN_UNAVAILABLE"
        )), self.assertRaisesRegex(clients.CredentialUnavailable, "IBKR_CONTROL_NATIVE_KEYCHAIN_UNAVAILABLE"):
            self.invoke(fixture)
        self.assertEqual(fixture.released, [1002, 1001])
        fixture.security.SecItemCopyMatching.assert_not_called()


class MemoryKeychain:
    def __init__(self, *, key=KEY, falsey=False):
        self.key = key
        self.falsey = falsey
        self.calls = []

    def __bool__(self):
        return not self.falsey

    def read(self, item):
        self.calls.append(item)
        return self.key


class ControlNativeAssemblySelectionTests(unittest.TestCase):
    def assembly(self, *, keychain=None):
        return assembly_module.LocalProviderAssembly(
            release_root=ROOT, install_root=ROOT, keychain=keychain,
            full_live_config_name="full_live_ibkr.json",
        )

    def read(self, assembly, *, execution=None):
        return assembly._ibkr_managed_control_authority(
            ibkr_profile=IbkrLocalProviderProfile.from_config(assembly.full_live),
            execution=execution if execution is not None else {
                "production_transport_id": IBKR_TRANSPORT_ID,
                "production_authorization_binding_id": "b" * 64,
            },
        )

    def test_default_control_alone_uses_native_and_leaves_other_custody_unchanged(self):
        generic, native = MemoryKeychain(), MemoryKeychain()
        with patch.object(assembly_module, "MacOSKeychain", return_value=generic), patch.object(
            assembly_module, "IbkrControlNativeMacOSKeychain", return_value=native
        ) as factory, patch.object(assembly_module, "GmailNativeMacOSKeychain") as gmail:
            assembly = self.assembly()
            self.assertEqual(self.read(assembly), (KEY, "b" * 64))
        factory.assert_called_once_with(item=ITEM)
        self.assertIs(assembly.keychain, generic)
        self.assertEqual(generic.calls, [])
        self.assertEqual(native.calls, [ITEM])
        gmail.assert_not_called()

    def test_injected_readers_are_preserved_including_falsey(self):
        for falsey in (False, True):
            injected = MemoryKeychain(falsey=falsey)
            with self.subTest(falsey=falsey), patch.object(assembly_module, "MacOSKeychain") as generic, patch.object(
                assembly_module, "IbkrControlNativeMacOSKeychain"
            ) as native:
                assembly = self.assembly(keychain=injected)
                self.assertEqual(self.read(assembly), (KEY, "b" * 64))
                self.assertIs(assembly.keychain, injected)
                self.assertEqual(injected.calls, [ITEM])
                generic.assert_not_called()
                native.assert_not_called()

    def test_changed_default_locator_fails_before_any_native_or_generic_access(self):
        generic = MemoryKeychain()
        with patch.object(assembly_module, "MacOSKeychain", return_value=generic), patch.object(
            clients.ctypes, "CDLL", side_effect=AssertionError("unscoped native access")
        ) as load:
            assembly = self.assembly()
            assembly.profile["ibkr_control"]["authentication_key_service"] = next(iter(clients.IBKR_GMAIL_NATIVE_ITEMS)).service
            with self.assertRaisesRegex(assembly_module.LocalAssemblyError, "MANAGED_CONTROL_KEY_UNAVAILABLE"):
                self.read(assembly)
        self.assertEqual(generic.calls, [])
        load.assert_not_called()

    def test_owner_auth_requirement_stops_without_generic_fallback(self):
        generic = MemoryKeychain()
        with patch.object(assembly_module, "MacOSKeychain", return_value=generic), patch.object(
            assembly_module, "IbkrControlNativeMacOSKeychain"
        ) as factory:
            factory.return_value.read.side_effect = clients.CredentialUnavailable(
                "CREDENTIAL_IBKR_CONTROL_NATIVE_KEYCHAIN_OWNER_AUTH_REQUIRED"
            )
            with self.assertRaisesRegex(assembly_module.LocalAssemblyError, "MANAGED_CONTROL_KEY_UNAVAILABLE") as caught:
                self.read(self.assembly())
        factory.return_value.read.assert_called_once_with(ITEM)
        self.assertEqual(generic.calls, [])
        self.assertIsNone(caught.exception.__cause__)

    def test_existing_key_and_binding_validation_are_not_relaxed(self):
        for key in (b"a" * 31, b"a" * 4097, b"a" * 32 + b"\x00"):
            with self.subTest(key_length=len(key)), self.assertRaisesRegex(
                assembly_module.LocalAssemblyError, "MANAGED_CONTROL_KEY_INVALID"
            ):
                self.read(self.assembly(keychain=MemoryKeychain(key=key)))
        with self.assertRaisesRegex(assembly_module.LocalAssemblyError, "MANAGED_CONTROL_BINDING_INVALID"):
            self.read(self.assembly(keychain=MemoryKeychain()), execution={
                "production_transport_id": IBKR_TRANSPORT_ID,
                "production_authorization_binding_id": "",
            })
        injected = MemoryKeychain()
        with self.assertRaisesRegex(assembly_module.LocalAssemblyError, "MANAGED_CONTROL_PROFILE_INVALID"):
            self.read(self.assembly(keychain=injected), execution={
                "production_transport_id": "not-authorized",
                "production_authorization_binding_id": "b" * 64,
            })
        self.assertEqual(injected.calls, [])


if __name__ == "__main__":
    unittest.main()
