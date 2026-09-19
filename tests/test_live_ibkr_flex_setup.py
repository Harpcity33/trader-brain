"""Hermetic setup tests: never access a broker, real Keychain or credentials."""
from datetime import date
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import warnings
from unittest.mock import Mock, patch

from titan_brain.live import ibkr_flex_setup as setup
from titan_brain.live.broker.ibkr_flex import IbkrFlexError


PROFILE = SimpleNamespace(account_key="ibkr-live-ending-3103", account_last4="4567")
ACCOUNT = "U900004567"  # Synthetic; deliberately not a protected account suffix.
VALIDATE_PROFILE = setup._profile
TOKEN = "123456789012345678901234"
QUERY = "123456"
RAW = json.dumps({"schema_version": setup._SCHEMA, "account_key": PROFILE.account_key,
                  "query_id": QUERY, "token": TOKEN}).encode()
DAY = date(2020, 1, 2)


class FlexSetupTests(unittest.TestCase):
    def setUp(self):
        self.profile = patch.object(setup, "_profile", return_value=PROFILE)
        self.profile.start()
        self.addCleanup(self.profile.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "control").mkdir(mode=0o700)

    def test_metadata_is_not_authentication_and_never_reads_secret(self):
        for state in ("PRESENT", "MISSING", "UNAVAILABLE", "unexpected"):
            with self.subTest(state=state), patch.object(setup, "MacOSKeychain") as custody:
                custody.return_value.metadata_status.return_value = state
                result = setup.setup_status(object())
                self.assertEqual(result["ok"], state == "PRESENT")
                self.assertFalse(result["authentication_tested"])
                self.assertFalse(result["daily_starting_equity_ready"])
                custody.return_value.metadata_status.assert_called_once_with(setup.FLEX_ITEM)
                custody.return_value.read.assert_not_called()

    def test_noninteractive_enrollment_never_reads_or_writes_secrets(self):
        with patch.object(setup.sys.stdin, "isatty", return_value=False), patch.object(setup, "CreateOnlyMacOSKeychain") as custody:
            result = setup.enroll(object())
            self.assertEqual(result["code"], "IBKR_FLEX_SETUP_OWNER_TERMINAL_REQUIRED")
            custody.assert_not_called()

    def test_enrollment_is_create_only_and_has_no_full_account_id(self):
        with patch.object(setup, "_require_terminal"), patch.object(setup, "CreateOnlyMacOSKeychain") as custody, patch.object(setup.getpass, "getpass", side_effect=[QUERY, TOKEN]):
            custody.return_value.metadata_status.return_value = "MISSING"
            result = setup.enroll(object())
            self.assertTrue(result["ok"])
            item, raw = custody.return_value.add.call_args.args
            self.assertEqual(item, setup.FLEX_ITEM)
            self.assertEqual(json.loads(raw), json.loads(RAW))
            self.assertNotIn(ACCOUNT, raw.decode())
            self.assertNotIn(TOKEN, json.dumps(result))
            self.assertFalse(result["authentication_tested"])
        for status in ("PRESENT", "UNAVAILABLE"):
            with patch.object(setup, "_require_terminal"), patch.object(setup, "CreateOnlyMacOSKeychain") as custody, patch.object(setup.getpass, "getpass") as prompt:
                custody.return_value.metadata_status.return_value = status
                self.assertFalse(setup.enroll(object())["ok"])
                custody.return_value.add.assert_not_called()
                prompt.assert_not_called()

    def test_duplicate_wrong_scope_extra_and_malformed_credentials_rejected(self):
        for raw in (RAW[:-1] + b',"token":"111111"}', RAW.replace(b"3103", b"9999"),
                    RAW[:-1] + b',"expected_account_id":"U900004567"}', b"[]",
                    b"x" * 4097, RAW.replace(TOKEN.encode(), b"secret-text")):
            with self.subTest(raw_length=len(raw)):
                with self.assertRaisesRegex(setup.FlexSetupError, "CREDENTIAL_INVALID"):
                    setup._credentials(raw, PROFILE)

    def test_invalid_query_stops_before_token_prompt_or_custody_write(self):
        for value in ("", " " + QUERY, QUERY + " ", QUERY + "\n", '"' + QUERY + '"',
                      "Query ID: " + QUERY, "١٢٣٤٥٦", "9" * 33):
            with self.subTest(empty=value == ""), patch.object(setup, "_require_terminal"), \
                    patch.object(setup, "CreateOnlyMacOSKeychain") as custody, \
                    patch.object(setup.getpass, "getpass", return_value=value) as prompt:
                custody.return_value.metadata_status.return_value = "MISSING"
                result = setup.enroll(object())
                suffix = "EMPTY" if value == "" else "FORMAT_INVALID"
                self.assertEqual(result["code"], "IBKR_FLEX_SETUP_QUERY_ID_" + suffix)
                self.assertEqual(prompt.call_count, 1)
                custody.return_value.add.assert_not_called()
                self.assertIn("Query ID", result["action_required"])
                self.assertEqual(set(result), {"code", "ok", "reporting_only",
                    "daily_starting_equity_ready", "live_cash_flow_complete_through",
                    "action_required"})
                self.assertFalse(result["ok"])
                self.assertTrue(result["reporting_only"])
                self.assertFalse(result["daily_starting_equity_ready"])
                self.assertIsNone(result["live_cash_flow_complete_through"])
                self.assertNotIn(QUERY, json.dumps(result))
                if value and value.strip():
                    self.assertNotIn(value, json.dumps(result, ensure_ascii=False))

    def test_invalid_token_reports_only_static_field_guidance_and_never_writes(self):
        for value in ("", " " + TOKEN, TOKEN + "\n", '"' + TOKEN + '"',
                      "Current Token: " + TOKEN, "١٢٣٤٥٦", "9" * 5, "9" * 129):
            with self.subTest(empty=value == ""), patch.object(setup, "_require_terminal"), \
                    patch.object(setup, "CreateOnlyMacOSKeychain") as custody, \
                    patch.object(setup.getpass, "getpass", side_effect=[QUERY, value]) as prompt:
                custody.return_value.metadata_status.return_value = "MISSING"
                result = setup.enroll(object())
                suffix = "EMPTY" if value == "" else "FORMAT_INVALID"
                self.assertEqual(result["code"], "IBKR_FLEX_SETUP_TOKEN_" + suffix)
                self.assertEqual(prompt.call_count, 2)
                custody.return_value.add.assert_not_called()
                self.assertIn("Current Token", result["action_required"])
                self.assertNotIn(TOKEN, json.dumps(result))
                self.assertNotIn(QUERY, json.dumps(result))
                if value:
                    self.assertNotIn(value, json.dumps(result, ensure_ascii=False))

    def test_enrollment_valid_format_boundaries_preserve_exact_input(self):
        for query, token in (("0", "0" * 6), ("9" * 32, "9" * 128)):
            with self.subTest(query_length=len(query)), patch.object(setup, "_require_terminal"), \
                    patch.object(setup, "CreateOnlyMacOSKeychain") as custody, \
                    patch.object(setup.getpass, "getpass", side_effect=[query, token]) as prompt:
                custody.return_value.metadata_status.return_value = "MISSING"
                result = setup.enroll(object())
                self.assertTrue(result["ok"])
                self.assertEqual(prompt.call_count, 2)
                self.assertEqual(custody.return_value.add.call_count, 1)
                stored = json.loads(custody.return_value.add.call_args.args[1])
                self.assertEqual(stored["query_id"], query)
                self.assertEqual(stored["token"], token)
                self.assertFalse(result["authentication_tested"])

    def test_enrollment_private_input_failure_never_retries_or_writes(self):
        def unsafe_prompt(_prompt):
            warnings.warn("cannot control echo", setup.getpass.GetPassWarning)
            self.fail("must fail before fallback input")
        for failure, code in ((unsafe_prompt, "PRIVATE_INPUT_UNAVAILABLE"),
                              (KeyboardInterrupt, "OWNER_CANCELED"),
                              (EOFError, "OWNER_CANCELED")):
            with self.subTest(code=code), patch.object(setup, "_require_terminal"), \
                    patch.object(setup, "CreateOnlyMacOSKeychain") as custody, \
                    patch.object(setup.getpass, "getpass", side_effect=failure) as prompt:
                custody.return_value.metadata_status.return_value = "MISSING"
                result = setup.enroll(object())
                self.assertEqual(result["code"], "IBKR_FLEX_SETUP_" + code)
                self.assertEqual(prompt.call_count, 1)
                custody.return_value.add.assert_not_called()
                self.assertNotIn("action_required", result)

    def test_unknown_custody_errors_are_redacted_not_retried(self):
        with patch.object(setup, "_require_terminal"), patch.object(setup, "CreateOnlyMacOSKeychain") as custody, patch.object(setup.getpass, "getpass", side_effect=[QUERY, TOKEN]):
            custody.return_value.metadata_status.return_value = "MISSING"
            custody.return_value.add.side_effect = RuntimeError(TOKEN + ACCOUNT)
            result = setup.enroll(object())
            self.assertFalse(result["ok"])
            self.assertIn("REVIEW_CUSTODY", result["code"])
            self.assertNotIn(TOKEN, json.dumps(result))
            self.assertEqual(custody.return_value.add.call_count, 1)

    def run_probe(self, outcomes, summary_override=None):
        report = Mock()
        report.diagnostic_summary.return_value = {
            "reporting_only": True, "daily_starting_equity_ready": False,
            "account_last4": "4567", "response_sha256": "private-hash",
            "generation_response_sha256": "private-generation-hash",
            "cash_transactions_count": 1, "response_origin": "flex_web_service_response"}
        if summary_override:
            report.diagnostic_summary.return_value.update(summary_override)
        with patch.object(setup, "_require_terminal"), patch.object(setup, "_account", return_value=ACCOUNT), patch.object(setup, "CreateOnlyMacOSKeychain") as custody, patch.object(setup, "MacOSKeychain") as legacy, patch.object(setup, "IbkrFlexReportReader", autospec=True) as reader, patch.object(setup.time, "sleep") as sleep:
            custody.return_value.read.return_value = RAW
            instance = reader.return_value
            instance.retrieve_report.side_effect = [report if outcome is None else outcome for outcome in outcomes]
            result = setup.probe(object(), report_date=DAY, install_root=self.root)
            custody.return_value.read.assert_called_once_with(setup.FLEX_ITEM)
            custody.assert_called_once_with(maximum_bytes=4096)
            custody.return_value.add.assert_not_called()
            legacy.assert_not_called()
            query = instance.request_report.call_args.args[0]
            self.assertEqual(query.expected_account_id, ACCOUNT)
            self.assertEqual(query.from_date, DAY)
            self.assertEqual(reader.call_args.kwargs["token_reader"](), TOKEN)
            return result, instance, sleep

    def test_native_custody_errors_are_classified_before_network_without_retry(self):
        failures = (
            (setup.KeychainSetupError("GMAIL_SETUP_KEYCHAIN_READ_UNAVAILABLE"), "KEYCHAIN_READ_UNAVAILABLE"),
            (setup.KeychainSetupError("GMAIL_SETUP_KEYCHAIN_READ_FAILED"), "KEYCHAIN_READ_FAILED"),
            (setup.KeychainSetupError("GMAIL_SETUP_KEYCHAIN_ITEM_INVALID"), "KEYCHAIN_ITEM_INVALID"),
            (setup.KeychainSetupError(TOKEN + ACCOUNT), "KEYCHAIN_READ_FAILED"),
            (RuntimeError(TOKEN + ACCOUNT), "KEYCHAIN_READ_FAILED"),
            (KeyboardInterrupt(), "OWNER_CANCELED"),
            (EOFError(), "OWNER_CANCELED"),
        )
        for failure, code in failures:
            with self.subTest(code=code), patch.object(setup, "_require_terminal"), \
                    patch.object(setup, "_account", return_value=ACCOUNT), \
                    patch.object(setup, "CreateOnlyMacOSKeychain") as custody, \
                    patch.object(setup, "MacOSKeychain") as legacy, \
                    patch.object(setup, "IbkrFlexReportReader") as reader, \
                    patch.object(setup.time, "sleep") as sleep:
                custody.return_value.read.side_effect = failure
                result = setup.probe(object(), report_date=DAY, install_root=self.root)
                self.assertEqual(result["code"], "IBKR_FLEX_SETUP_" + code)
                self.assertFalse(result["ok"])
                self.assertTrue(result["reporting_only"])
                self.assertFalse(result["daily_starting_equity_ready"])
                self.assertIsNone(result["live_cash_flow_complete_through"])
                custody.return_value.read.assert_called_once_with(setup.FLEX_ITEM)
                custody.return_value.add.assert_not_called()
                legacy.assert_not_called()
                reader.assert_not_called()
                sleep.assert_not_called()
                for private in (TOKEN, ACCOUNT, QUERY):
                    self.assertNotIn(private, json.dumps(result))
                # Failed/canceled reads must release the serialization lock.
                with setup._probe_lock(self.root):
                    pass

    def test_native_custody_keeps_strict_stored_credential_validation(self):
        with patch.object(setup, "_require_terminal"), \
                patch.object(setup, "_account", return_value=ACCOUNT), \
                patch.object(setup, "CreateOnlyMacOSKeychain") as custody, \
                patch.object(setup, "IbkrFlexReportReader") as reader:
            custody.return_value.read.return_value = RAW.replace(b"3103", b"9999")
            result = setup.probe(object(), report_date=DAY, install_root=self.root)
            self.assertEqual(result["code"], "IBKR_FLEX_SETUP_CREDENTIAL_INVALID")
            custody.return_value.add.assert_not_called()
            reader.assert_not_called()

    def test_probe_still_requires_real_terminal_before_native_custody(self):
        with patch.object(setup.sys.stdin, "isatty", return_value=False), \
                patch.object(setup, "CreateOnlyMacOSKeychain") as custody, \
                patch.object(setup.getpass, "getpass") as prompt, \
                patch.object(setup, "IbkrFlexReportReader") as reader:
            result = setup.probe(object(), report_date=DAY, install_root=self.root)
            self.assertEqual(result["code"], "IBKR_FLEX_SETUP_OWNER_TERMINAL_REQUIRED")
            custody.assert_not_called()
            prompt.assert_not_called()
            reader.assert_not_called()

    def test_probe_reuses_ticket_with_bounded_1019_retry_and_redacted_summary(self):
        result, instance, sleep = self.run_probe([IbkrFlexError("PROVIDER_1019"), None])
        self.assertTrue(result["ok"])
        self.assertFalse(result["daily_starting_equity_ready"])
        instance.request_report.assert_called_once()
        self.assertEqual(instance.retrieve_report.call_count, 2)
        for call in instance.retrieve_report.call_args_list:
            self.assertIs(call.args[0], instance.request_report.return_value)
        self.assertEqual([call.args for call in sleep.call_args_list], [(6,), (6,), (6,)])
        for private in (TOKEN, ACCOUNT, QUERY, "private-hash", "private-generation-hash"):
            self.assertNotIn(private, json.dumps(result))
        self.assertEqual(list((self.root / "control").iterdir()), [self.root / "control/flex-reporting-probe.lock"])
        self.assertEqual((self.root / "control/flex-reporting-probe.lock").read_bytes(), b"")

    def test_1019_limit_and_other_failures_never_regenerate(self):
        for errors, count in (([IbkrFlexError("PROVIDER_1019")] * 3, 3),
                              ([IbkrFlexError("TRANSPORT_FAILED")], 1),
                              ([RuntimeError(TOKEN)], 1)):
            result, instance, _ = self.run_probe(errors)
            self.assertFalse(result["ok"])
            self.assertEqual(instance.retrieve_report.call_count, count)
            instance.request_report.assert_called_once()
            self.assertNotIn(TOKEN, json.dumps(result))

    def test_report_summary_cannot_override_reporting_only_boundary(self):
        result, _, _ = self.run_probe([None], {
            "reporting_only": False, "daily_starting_equity_ready": True,
            "live_cash_flow_complete_through": "forged-watermark"})
        self.assertTrue(result["ok"])
        self.assertTrue(result["reporting_only"])
        self.assertFalse(result["daily_starting_equity_ready"])
        self.assertIsNone(result["live_cash_flow_complete_through"])

    def test_future_date_rejected_before_terminal_custody_or_network(self):
        with patch.object(setup, "_account") as account, patch.object(setup, "CreateOnlyMacOSKeychain") as custody:
            result = setup.probe(object(), report_date=date(9999, 1, 1), install_root=self.root)
            self.assertEqual(result["code"], "IBKR_FLEX_SETUP_HISTORICAL_DATE_REQUIRED")
            account.assert_not_called()
            custody.assert_not_called()

    def test_real_reader_constructor_and_parser_with_injected_transport(self):
        from tests.test_live_ibkr_flex import REPORT, SUCCESS, Response
        from tests.test_live_ibkr_flex import ACCOUNT as FIXTURE_ACCOUNT
        real_reader = setup.IbkrFlexReportReader
        raw = REPORT.replace(FIXTURE_ACCOUNT.encode(), ACCOUNT.encode()).replace(b"20260914", b"20200102")
        clock = [0.0]
        responses = iter([SUCCESS, raw])
        def wait(seconds):
            clock[0] += seconds
        def reader_factory(*, token_reader):
            return real_reader(token_reader=token_reader,
                               opener=lambda *args, **kwargs: Response(next(responses)),
                               monotonic=lambda: clock[0])
        with patch.object(setup, "_require_terminal"), patch.object(setup, "_account", return_value=ACCOUNT), patch.object(setup, "CreateOnlyMacOSKeychain") as custody, patch.object(setup, "IbkrFlexReportReader", side_effect=reader_factory), patch.object(setup.time, "sleep", side_effect=wait):
            custody.return_value.read.return_value = RAW
            result = setup.probe(object(), report_date=DAY, install_root=self.root)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["response_origin"], "injected_transport_unverified_bytes")
        self.assertFalse(result["daily_starting_equity_ready"])
        self.assertEqual(result["cash_transactions_count"], 2)
        self.assertNotIn(ACCOUNT, json.dumps(result))
        self.assertNotIn("response_sha256", result)

    def test_lock_serializes_and_releases_without_authority_writes(self):
        with setup._probe_lock(self.root):
            with self.assertRaisesRegex(setup.FlexSetupError, "PROBE_BUSY"):
                with setup._probe_lock(self.root):
                    self.fail("must not enter")
        with setup._probe_lock(self.root):
            pass
        self.assertEqual(os.stat(self.root / "control/flex-reporting-probe.lock").st_mode & 0o777, 0o600)

    def test_symlink_and_permissive_lock_rejected(self):
        target = self.root / "elsewhere"
        target.write_text("unchanged")
        lock = self.root / "control/flex-reporting-probe.lock"
        lock.symlink_to(target)
        with self.assertRaises(setup.FlexSetupError):
            with setup._probe_lock(self.root):
                pass
        self.assertEqual(target.read_text(), "unchanged")
        lock.unlink()
        lock.touch(mode=0o644)
        with self.assertRaisesRegex(setup.FlexSetupError, "LOCK_UNSAFE"):
            with setup._probe_lock(self.root):
                pass

    def test_full_account_prompt_rejects_wrong_suffix(self):
        with patch.object(setup, "_require_terminal"), patch.object(setup.getpass, "getpass", return_value="U900009999"):
            with self.assertRaisesRegex(setup.FlexSetupError, "ACCOUNT_INVALID"):
                setup._account(PROFILE)

    def test_non_ibkr_profile_never_accesses_fixed_custody(self):
        with patch.object(setup, "_profile", VALIDATE_PROFILE), patch.object(setup.IbkrLocalProviderProfile, "from_config", return_value=None), patch.object(setup, "MacOSKeychain") as custody:
            result = setup.setup_status(SimpleNamespace(config={}))
            self.assertFalse(result["ok"])
            custody.assert_not_called()

    def test_hidden_prompt_cannot_fall_back_to_echoed_input(self):
        def unsafe_prompt(_prompt):
            warnings.warn("cannot control echo", setup.getpass.GetPassWarning)
            self.fail("must fail before fallback input")
        with patch.object(setup.getpass, "getpass", side_effect=unsafe_prompt):
            with self.assertRaisesRegex(setup.FlexSetupError, "PRIVATE_INPUT_UNAVAILABLE"):
                setup._hidden("test")


if __name__ == "__main__":
    unittest.main()
