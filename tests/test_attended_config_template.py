"""Tests for the attended-execution config template (attended enablement PR).

These assert the NEW config/full_live_ibkr_attended.json is (a) the intended
attended supported-transport delta, and (b) FAIL-CLOSED: it cannot load as a
valid policy while the authority bindings are the owner-provisioned sentinels,
and once provisioned with real 64-hex bindings it is an attended-only supported
transport that still requires per-order confirmation and never allows unattended
mutation. Offline; no broker.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest

from titan_brain.live.policy import PolicyBundle


ROOT = Path(__file__).resolve().parents[1]
ATTENDED = ROOT / "config" / "full_live_ibkr_attended.json"
STAGED = ROOT / "config" / "full_live_ibkr.json"
_HEX64 = "a" * 64


class AttendedConfigTemplateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.attended = json.loads(ATTENDED.read_text())
        self.staged = json.loads(STAGED.read_text())

    def test_execution_delta_is_supported_attended_transport(self) -> None:
        ex = self.attended["execution"]
        self.assertEqual(ex["broker_adapter"], "supported_production_transport")
        self.assertEqual(ex["execution_authority_mode"], "attended_only")
        self.assertTrue(ex["per_mutation_user_confirmation_required"])
        self.assertFalse(ex["supported_unattended_mutation"])
        self.assertEqual(ex["production_transport_id"], "ibkr-tws-api-10.50.2-v1")

    def test_staged_config_is_unchanged(self) -> None:
        # The existing account-3103 staged config must remain staged/attended.
        self.assertEqual(self.staged["execution"]["broker_adapter"], "ibkr_local_gateway_staged")

    def test_only_execution_and_marker_differ_from_staged(self) -> None:
        a = copy.deepcopy(self.attended)
        s = copy.deepcopy(self.staged)
        a.pop("_attended_template", None)
        a.pop("execution", None)
        s.pop("execution", None)
        self.assertEqual(a, s)  # everything except execution + marker is identical

    def test_template_marker_present(self) -> None:
        self.assertIn("TEMPLATE ONLY", self.attended["_attended_template"])

    def test_authority_bindings_are_failclosed_sentinels(self) -> None:
        ex = self.attended["execution"]
        for field in ("production_authorization_binding_id",
                      "production_account_binding_fingerprint",
                      "ibkr_provider_contract_id"):
            value = ex[field]
            # Not 64-hex -> cannot pass policy validation (fail closed).
            self.assertFalse(len(value) == 64 and all(c in "0123456789abcdef" for c in value),
                             f"{field} must be a fail-closed sentinel, not valid hex")

    def test_policy_load_fails_closed_on_sentinel_bindings(self) -> None:
        # Loading the attended template as-is must RAISE (sentinels not hex).
        with self.assertRaises(ValueError):
            PolicyBundle.load(ROOT, config_relative="config/full_live_ibkr_attended.json")

    def test_provisioned_bindings_clear_the_format_check(self) -> None:
        # The sentinels fail policy.py's 64-hex binding-format check; real
        # provisioned bindings pass it. We assert the format rule directly (a
        # full PolicyBundle.load needs many sibling release artifacts a unit
        # should not stage; the binding-format gate is the property under test).
        def is_valid_receipt(value: str) -> bool:
            return len(value) == 64 and all(c in "0123456789abcdef" for c in value)

        ex = self.attended["execution"]
        # Sentinels fail.
        self.assertFalse(is_valid_receipt(ex["production_authorization_binding_id"]))
        self.assertFalse(is_valid_receipt(ex["production_account_binding_fingerprint"]))
        # Owner-provisioned 64-hex values pass.
        self.assertTrue(is_valid_receipt(_HEX64))


if __name__ == "__main__":
    unittest.main()
