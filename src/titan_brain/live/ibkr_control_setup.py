"""Create-only owner enrollment of the account-scoped managed-control key.

This command stores local secret material only. It never signs a request or
receipt, issues broker authority, changes policy, or activates a service.
Native setup readback is not qualification of the separate runtime reader.
"""
from __future__ import annotations

from collections.abc import Mapping
import secrets
import sys

from .gmail_oauth_setup import CreateOnlyMacOSKeychain
from .provider_clients import IBKR_CONTROL_NATIVE_ITEM, KeychainItem
from .provider_profile import IbkrLocalProviderProfile


CONTROL_ITEM = IBKR_CONTROL_NATIVE_ITEM
_EXPECTED_CONTROL_PROFILE = {
    "implementation_id": "titan.hmac_control.ibkr.keychain.v1",
    "credential_backend": "macos_keychain",
    "authentication_key_service": CONTROL_ITEM.service,
    "credential_account": CONTROL_ITEM.account,
    "account_key": CONTROL_ITEM.account,
    "authorization_binding_source": "signed_execution.production_authorization_binding_id",
}
_CODES = frozenset({
    "ENROLLED", "PROFILE_INVALID", "OWNER_TERMINAL_REQUIRED",
    "EXISTING_OR_UNAVAILABLE_CUSTODY", "OWNER_CANCELED",
    "ENROLLMENT_FAILED_REVIEW_CUSTODY",
})


def _result(code: str) -> dict[str, object]:
    if code not in _CODES:
        code = "ENROLLMENT_FAILED_REVIEW_CUSTODY"
    enrolled = code == "ENROLLED"
    return {
        "ok": enrolled,
        "code": "IBKR_CONTROL_SETUP_" + code,
        "setup_only": True,
        "native_readback_verified": enrolled,
        "runtime_reader_access_verified": False,
        "write_authority_granted": False,
        "activation_performed": False,
    }


def _configured_item(policy, provider_bindings: Mapping[str, object]) -> KeychainItem:
    """Accept only the release-configured locator for this exact account."""
    profile = IbkrLocalProviderProfile.from_config(policy.config)
    if (
        not isinstance(profile, IbkrLocalProviderProfile)
        or profile.account_key != CONTROL_ITEM.account
        or profile.account_last4 != "3103"
        or not isinstance(provider_bindings, Mapping)
        or provider_bindings.get("schema_version")
        != "titan_local_provider_bindings_2026-09-08_v1"
    ):
        raise ValueError()
    configured = provider_bindings.get("ibkr_control")
    if not isinstance(configured, Mapping) or dict(configured) != _EXPECTED_CONTROL_PROFILE:
        raise ValueError()
    legacy = provider_bindings.get("control")
    if isinstance(legacy, Mapping) and (
        legacy.get("authentication_key_service") == CONTROL_ITEM.service
        or legacy.get("credential_account") == CONTROL_ITEM.account
    ):
        raise ValueError()
    return KeychainItem(
        service=configured["authentication_key_service"],
        account=configured["credential_account"],
    )


def enroll(policy, provider_bindings: Mapping[str, object]) -> dict[str, object]:
    """Generate and store one missing key; never read or replace existing keys.

    A real owner terminal is required because native Keychain operations may
    require macOS interaction. No private input is requested. The SDK helper
    creates without overwrite and verifies the new value through native
    readback without changing access controls. A failed or canceled operation
    may have left an item; it must not be automatically retried.
    """
    try:
        item = _configured_item(policy, provider_bindings)
    except Exception:
        return _result("PROFILE_INVALID")
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        return _result("OWNER_TERMINAL_REQUIRED")
    try:
        custody = CreateOnlyMacOSKeychain(maximum_bytes=4096)
        if custody.metadata_status(item) != "MISSING":
            return _result("EXISTING_OR_UNAVAILABLE_CUSTODY")
        entropy = secrets.token_bytes(32)
        if type(entropy) is not bytes or len(entropy) != 32:
            raise ValueError()
        # 64 ASCII bytes preserve all 256 random bits and satisfy the runtime
        # key reader's non-NUL contract. Neither form leaves process memory
        # except for this exact create-only Keychain write.
        material = entropy.hex().encode("ascii")
        custody.add(item, material)
        return _result("ENROLLED")
    except (KeyboardInterrupt, EOFError):
        return _result("OWNER_CANCELED")
    except Exception:
        # Native exceptions can carry private data. Do not stringify, return,
        # retry or log them; creation may have succeeded before readback failed.
        return _result("ENROLLMENT_FAILED_REVIEW_CUSTODY")


__all__ = ["enroll"]
