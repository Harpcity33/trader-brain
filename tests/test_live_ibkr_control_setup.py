"""Hermetic control-key setup tests; never access actual credentials or APIs."""
from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
from pathlib import Path
import runpy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from titan_brain.live import cli
from titan_brain.live import ibkr_control_setup as setup


ROOT = Path(__file__).resolve().parents[1]
ENTROPY = bytes(range(32))
MATERIAL = ENTROPY.hex().encode("ascii")


class IbkrControlSetupTests(unittest.TestCase):
    def setUp(self):
        self.policy = SimpleNamespace(config=json.loads(
            (ROOT / "config/full_live_ibkr.json").read_text(encoding="utf-8")
        ))
        self.bindings = json.loads(
            (ROOT / "config/provider_bindings.json").read_text(encoding="utf-8")
        )
        self.stdin = patch.object(setup.sys.stdin, "isatty", return_value=True)
        self.stderr = patch.object(setup.sys.stderr, "isatty", return_value=True)
        self.stdin.start()
        self.stderr.start()
        self.addCleanup(self.stdin.stop)
        self.addCleanup(self.stderr.stop)

    def assert_setup_boundary(self, result):
        self.assertTrue(result["setup_only"])
        self.assertFalse(result["runtime_reader_access_verified"])
        self.assertFalse(result["write_authority_granted"])
        self.assertFalse(result["activation_performed"])
        self.assertEqual(result["native_readback_verified"], result["ok"])
        self.assertEqual(set(result), {
            "ok", "code", "setup_only", "native_readback_verified",
            "runtime_reader_access_verified", "write_authority_granted",
            "activation_performed",
        })
        self.assertNotIn(MATERIAL.decode(), json.dumps(result))

    def test_missing_key_created_once_with_full_entropy_and_no_authority(self):
        with patch.object(setup, "CreateOnlyMacOSKeychain") as custody, \
                patch.object(setup.secrets, "token_bytes", return_value=ENTROPY) as entropy:
            custody.return_value.metadata_status.return_value = "MISSING"
            result = setup.enroll(self.policy, self.bindings)
        custody.assert_called_once_with(maximum_bytes=4096)
        custody.return_value.metadata_status.assert_called_once_with(setup.CONTROL_ITEM)
        entropy.assert_called_once_with(32)
        custody.return_value.add.assert_called_once_with(setup.CONTROL_ITEM, MATERIAL)
        # add() owns native verification; setup never reads an existing item or
        # calls the separate runtime security-subprocess reader.
        custody.return_value.read.assert_not_called()
        self.assertEqual(result["code"], "IBKR_CONTROL_SETUP_ENROLLED")
        self.assertTrue(result["ok"])
        self.assert_setup_boundary(result)
        self.assertEqual(bytes.fromhex(MATERIAL.decode()), ENTROPY)

    def test_existing_unavailable_and_unknown_custody_never_generate_or_write(self):
        for state in ("PRESENT", "UNAVAILABLE", "unexpected", None):
            with self.subTest(state=state), \
                    patch.object(setup, "CreateOnlyMacOSKeychain") as custody, \
                    patch.object(setup.secrets, "token_bytes") as entropy:
                custody.return_value.metadata_status.return_value = state
                result = setup.enroll(self.policy, self.bindings)
                self.assertEqual(result["code"],
                    "IBKR_CONTROL_SETUP_EXISTING_OR_UNAVAILABLE_CUSTODY")
                entropy.assert_not_called()
                custody.return_value.add.assert_not_called()
                custody.return_value.read.assert_not_called()
                self.assert_setup_boundary(result)

    def test_both_real_terminal_guards_precede_all_custody_and_entropy(self):
        for stdin, stderr in ((False, True), (True, False), (False, False)):
            with self.subTest(stdin=stdin, stderr=stderr), \
                    patch.object(setup.sys.stdin, "isatty", return_value=stdin), \
                    patch.object(setup.sys.stderr, "isatty", return_value=stderr), \
                    patch.object(setup, "CreateOnlyMacOSKeychain") as custody, \
                    patch.object(setup.secrets, "token_bytes") as entropy:
                result = setup.enroll(self.policy, self.bindings)
                self.assertEqual(result["code"], "IBKR_CONTROL_SETUP_OWNER_TERMINAL_REQUIRED")
                custody.assert_not_called()
                entropy.assert_not_called()
                self.assert_setup_boundary(result)

    def test_signed_profile_scope_errors_precede_custody(self):
        variants = [None, {}, {**self.bindings, "schema_version": "other"}]
        for field, value in (
            ("authentication_key_service", "titan-full-live-control-authentication-key"),
            ("credential_account", "ending-7153"),
            ("account_key", "ibkr-live-ending-9999"),
            ("implementation_id", "other"),
            ("credential_backend", "environment"),
            ("authorization_binding_source", "user_supplied"),
            ("extra", "private-value"),
        ):
            changed = copy.deepcopy(self.bindings)
            changed["ibkr_control"][field] = value
            variants.append(changed)
        for field, value in (
            ("authentication_key_service", setup.CONTROL_ITEM.service),
            ("credential_account", setup.CONTROL_ITEM.account),
        ):
            changed = copy.deepcopy(self.bindings)
            changed["control"][field] = value
            variants.append(changed)
        for bindings in variants:
            with self.subTest(bindings=bindings is None), \
                    patch.object(setup, "CreateOnlyMacOSKeychain") as custody, \
                    patch.object(setup.secrets, "token_bytes") as entropy:
                result = setup.enroll(self.policy, bindings)
                self.assertEqual(result["code"], "IBKR_CONTROL_SETUP_PROFILE_INVALID")
                custody.assert_not_called()
                entropy.assert_not_called()
                self.assert_setup_boundary(result)
        for profile in (None, SimpleNamespace(account_key=setup.CONTROL_ITEM.account,
                                            account_last4="3103")):
            with patch.object(setup.IbkrLocalProviderProfile, "from_config", return_value=profile), \
                    patch.object(setup, "CreateOnlyMacOSKeychain") as custody:
                self.assertEqual(setup.enroll(self.policy, self.bindings)["code"],
                    "IBKR_CONTROL_SETUP_PROFILE_INVALID")
                custody.assert_not_called()

    def test_entropy_failures_cannot_store_partial_or_invalid_material(self):
        for value in (b"", b"a" * 31, b"a" * 33, "a" * 32, bytearray(32)):
            with self.subTest(type=type(value).__name__), \
                    patch.object(setup, "CreateOnlyMacOSKeychain") as custody, \
                    patch.object(setup.secrets, "token_bytes", return_value=value):
                custody.return_value.metadata_status.return_value = "MISSING"
                result = setup.enroll(self.policy, self.bindings)
                self.assertEqual(result["code"],
                    "IBKR_CONTROL_SETUP_ENROLLMENT_FAILED_REVIEW_CUSTODY")
                custody.return_value.add.assert_not_called()
                self.assert_setup_boundary(result)

    def test_native_partial_failure_and_cancellation_are_redacted_and_not_retried(self):
        class PrivateFailure(RuntimeError):
            def __str__(self):
                raise AssertionError("private exception must never be stringified")

        for failure, suffix in (
            (PrivateFailure(MATERIAL.decode()), "ENROLLMENT_FAILED_REVIEW_CUSTODY"),
            (KeyboardInterrupt(), "OWNER_CANCELED"),
            (EOFError(), "OWNER_CANCELED"),
        ):
            with self.subTest(code=suffix), \
                    patch.object(setup, "CreateOnlyMacOSKeychain") as custody, \
                    patch.object(setup.secrets, "token_bytes", return_value=ENTROPY) as entropy:
                custody.return_value.metadata_status.return_value = "MISSING"
                custody.return_value.add.side_effect = failure
                result = setup.enroll(self.policy, self.bindings)
                self.assertEqual(result["code"], "IBKR_CONTROL_SETUP_" + suffix)
                custody.return_value.add.assert_called_once_with(setup.CONTROL_ITEM, MATERIAL)
                entropy.assert_called_once_with(32)
                self.assert_setup_boundary(result)

    def test_cli_loads_verified_release_then_config_and_emits_only_fixed_results(self):
        layout = SimpleNamespace(release_root=ROOT, load_release=Mock(
            return_value=({"release_manifest_hash": "a" * 64}, self.policy)))
        parser = cli.build_parser()
        args = parser.parse_args(["ibkr-control-enroll", "--install-root", "/synthetic/install"])
        for suffix in ("ENROLLED", "EXISTING_OR_UNAVAILABLE_CUSTODY"):
            output = io.StringIO()
            with patch.object(cli, "InstallLayout", return_value=layout), \
                    patch.object(setup, "enroll", return_value=setup._result(suffix)) as enroll, \
                    redirect_stdout(output):
                code = args.handler(args)
            self.assertEqual(code, 0 if suffix == "ENROLLED" else 2)
            enroll.assert_called_once_with(self.policy, self.bindings)
            self.assert_setup_boundary(json.loads(output.getvalue()))
        with patch.object(cli, "InstallLayout", return_value=layout), \
                patch.object(layout, "load_release", side_effect=cli.CommandBlocked("bad release")), \
                patch.object(setup, "enroll") as enroll:
            with self.assertRaises(cli.CommandBlocked):
                args.handler(args)
            enroll.assert_not_called()

    def test_cli_refuses_secret_locator_and_override_arguments(self):
        parser = cli.build_parser()
        for option in ("--key", "--secret", "--token", "--keychain-service", "--account", "--force"):
            with self.subTest(option=option), redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit):
                parser.parse_args(["ibkr-control-enroll", "--install-root", "/synthetic/install",
                                   option, "private-value"])

    def test_cli_rejects_additional_private_fields_and_authority_claims(self):
        layout = SimpleNamespace(release_root=ROOT, load_release=Mock(
            return_value=({}, self.policy)))
        args = cli.build_parser().parse_args([
            "ibkr-control-enroll", "--install-root", "/synthetic/install"])
        variants = [
            {**setup._result("ENROLLED"), "key": MATERIAL.decode()},
            {**setup._result("ENROLLED"), "runtime_reader_access_verified": True},
            {**setup._result("ENROLLED"), "write_authority_granted": True},
            {**setup._result("ENROLLED"), "code": MATERIAL.decode()},
            {**setup._result("ENROLLED"), "code": []},
            {**setup._result("ENROLLED"), "ok": False},
        ]
        for report in variants:
            with self.subTest(fields=tuple(report)), \
                    patch.object(cli, "InstallLayout", return_value=layout), \
                    patch.object(setup, "enroll", return_value=report), \
                    patch.object(cli, "_print") as emit:
                with self.assertRaisesRegex(cli.CommandBlocked, "SETUP_REPORT_INVALID"):
                    args.handler(args)
                emit.assert_not_called()

    def test_launcher_enrollment_never_loads_broker_sdk_or_write_authority(self):
        namespace = runpy.run_path(str(ROOT / "scripts/titan-full-live"))
        self.assertFalse(namespace["_requires_installed_broker_sdk"](["ibkr-control-enroll"]))
        self.assertIsNone(namespace["_release_bound_command_inputs"](
            release_root=ROOT,
            install_root=Path("/synthetic/install"),
            full_live_config_name="full_live_ibkr.json",
            release_manifest_hash="a" * 64,
            arguments=["ibkr-control-enroll"],
        ))


if __name__ == "__main__":
    unittest.main()
