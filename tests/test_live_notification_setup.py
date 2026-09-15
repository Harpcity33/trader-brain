from __future__ import annotations

import copy
from contextlib import redirect_stdout
import importlib.machinery
import importlib.util
import io
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from titan_brain.live import cli
from titan_brain.live.local_assembly import LocalAssemblyError, LocalProviderAssembly
from titan_brain.live.notification_setup import notification_setup_status
from titan_brain.live.policy import PolicyBundle
from titan_brain.live.provider_clients import (
    KeychainItem, MacOSKeychain, select_account_gmail_profile,
)


ROOT = Path(__file__).resolve().parents[1]


class NotificationSetupTests(unittest.TestCase):
    def setUp(self):
        self.policy = PolicyBundle.load(ROOT, config_relative="config/full_live_ibkr.json")
        self.bindings = json.loads((ROOT / "config/provider_bindings.json").read_text())

    def test_keychain_metadata_never_requests_or_captures_secret_output(self):
        item = KeychainItem("public-service", "ibkr-live-ending-3103")
        for returncode, expected in ((0, "PRESENT"), (44, "MISSING"), (36, "UNAVAILABLE")):
            with self.subTest(returncode=returncode), patch(
                "titan_brain.live.provider_clients.subprocess.run",
                return_value=SimpleNamespace(returncode=returncode),
            ) as run:
                self.assertEqual(MacOSKeychain().metadata_status(item), expected)
                args, kwargs = run.call_args
                self.assertEqual(args[0], [
                    "/usr/bin/security", "find-generic-password", "-s",
                    "public-service", "-a", "ibkr-live-ending-3103",
                ])
                self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
                self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        with patch(
            "titan_brain.live.provider_clients.subprocess.run",
            side_effect=OSError("secret content must not appear"),
        ):
            self.assertEqual(MacOSKeychain().metadata_status(item), "UNAVAILABLE")

    def test_account_selection_preserves_legacy_and_isolates_ibkr(self):
        legacy = PolicyBundle.load(ROOT)
        name, selected = select_account_gmail_profile(self.bindings, legacy.config)
        self.assertEqual(name, "gmail")
        self.assertEqual(selected["credential_account"], "ending-7153")
        name, selected = select_account_gmail_profile(self.bindings, self.policy.config)
        self.assertEqual(name, "ibkr_gmail")
        self.assertEqual(selected["credential_account"], "ibkr-live-ending-3103")
        for field, value in (
            ("credential_account", "ending-7153"),
            ("account_key", "ending-7153"),
            ("refresh_token_service", self.bindings["gmail"]["refresh_token_service"]),
            ("refresh_token_service", None),
        ):
            changed = copy.deepcopy(self.bindings)
            changed["ibkr_gmail"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                select_account_gmail_profile(changed, self.policy.config)

    def test_invalid_account_profile_cannot_read_legacy_credentials(self):
        reader = Mock()
        assembly = LocalProviderAssembly(
            release_root=ROOT, install_root=ROOT, keychain=reader,
            full_live_config_name="full_live_ibkr.json",
        )
        assembly.profile["ibkr_gmail"]["credential_account"] = "ending-7153"
        assembly.profile["ibkr_gmail"]["enabled"] = True
        with self.assertRaisesRegex(LocalAssemblyError, "GMAIL_ACCOUNT_PROFILE_INVALID"):
            assembly.gmail_binding()
        reader.read.assert_not_called()
        reader.read_text.assert_not_called()
        assembly.close()

    def test_missing_setup_reports_each_named_locator_without_promoting_evidence(self):
        reader = Mock()
        reader.metadata_status.return_value = "MISSING"
        reader.read.side_effect = AssertionError("secret read")
        reader.read_text.side_effect = AssertionError("secret read")
        report = notification_setup_status(
            self.policy.config, self.bindings, keychain=reader
        )
        self.assertEqual(report["selected_profile"], "ibkr_gmail")
        self.assertTrue(report["account_namespace_matches"])
        self.assertEqual(report["configured_delivery_sink"], "local_jsonl_staging")
        self.assertEqual(report["intended_delivery_sink"], "gmail_api")
        self.assertEqual(len(report["keychain_items"]), 5)
        for item in report["keychain_items"]:
            self.assertIn("ibkr-ending-3103-gmail", item["service"])
            self.assertEqual(item["account"], "ibkr-live-ending-3103")
            self.assertEqual(item["metadata_status"], "MISSING")
        self.assertIn("GMAIL_LOCAL_PROFILE_DISABLED", report["missing_prerequisites"])
        self.assertIn("VISIBLY_RECEIVED_CURRENT_ROUTE_BOUND_TEST", report["unverified_prerequisites"])
        self.assertFalse(report["ready_for_delivery"])
        self.assertFalse(report["credential_contents_read"])
        reader.read.assert_not_called()
        reader.read_text.assert_not_called()

    def test_metadata_presence_does_not_attest_oauth_or_owner_consent(self):
        reader = Mock(metadata_status=Mock(return_value="PRESENT"))
        report = notification_setup_status(self.policy.config, self.bindings, keychain=reader)
        self.assertTrue(all(i["metadata_status"] == "PRESENT" for i in report["keychain_items"]))
        self.assertFalse(report["ready_for_delivery"])
        self.assertIn("OWNER_AUTHORIZED_GMAIL_DESTINATION_CONSENT", report["unverified_prerequisites"])
        changed = copy.deepcopy(self.bindings)
        changed["ibkr_gmail"]["credential_account"] = "ending-7153"
        reader.reset_mock()
        report = notification_setup_status(self.policy.config, changed, keychain=reader)
        self.assertFalse(report["account_namespace_matches"])
        reader.metadata_status.assert_not_called()

    def test_malformed_intended_route_is_reported_without_echoing_its_value(self):
        config = copy.deepcopy(self.policy.config)
        config["notifications"]["intended_delivery_sink"] = ["untrusted-content"]
        report = notification_setup_status(
            config, self.bindings,
            keychain=Mock(metadata_status=Mock(return_value="MISSING")),
        )
        self.assertEqual(report["intended_delivery_sink"], "unrecognized")
        self.assertNotIn("untrusted-content", json.dumps(report))

    def test_cli_setup_command_never_resolves_runtime_or_provider_connections(self):
        reader = Mock(metadata_status=Mock(return_value="MISSING"))
        composition = Mock(side_effect=AssertionError("runtime factory invoked"))
        assembly = Mock(keychain=reader)
        layout = SimpleNamespace(
            release_root=ROOT,
            load_release=lambda: ({"release_manifest_hash": "a" * 64}, self.policy),
        )
        output = io.StringIO()
        with patch.object(cli, "InstallLayout", return_value=layout), redirect_stdout(output):
            result = cli.main(
                ["notification-setup-status", "--install-root", "/unused"],
                runtime_composition=composition, provider_assembly=assembly,
            )
        self.assertEqual(result, 2)
        composition.assert_not_called()
        assembly.connection_report.assert_not_called()
        assembly.gmail_binding.assert_not_called()
        assembly.ibkr_read_components.assert_not_called()
        assembly.massive_source.assert_not_called()
        report = json.loads(output.getvalue())
        self.assertFalse(report["broker_checks_performed"])
        self.assertFalse(report["market_data_checks_performed"])
        self.assertFalse(report["delivery_attempted"])

    def test_launcher_classifies_setup_as_notification_only_without_authority(self):
        loader = importlib.machinery.SourceFileLoader("notification_setup_launcher", str(ROOT / "scripts/titan-full-live"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        assert spec is not None
        launcher = importlib.util.module_from_spec(spec)
        loader.exec_module(launcher)
        self.assertIn("notification-setup-status", launcher._NOTIFICATION_ONLY_COMMANDS)
        with patch.object(PolicyBundle, "load", side_effect=AssertionError("authority load")):
            self.assertIsNone(launcher._release_bound_command_inputs(
                release_root=ROOT, install_root=ROOT,
                full_live_config_name="full_live_ibkr.json",
                release_manifest_hash="a" * 64,
                arguments=["notification-setup-status"],
            ))


if __name__ == "__main__":
    unittest.main()
