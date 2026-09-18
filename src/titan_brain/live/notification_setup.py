"""Read-only Gmail setup inventory independent of broker and market providers."""

from __future__ import annotations

import re
from typing import Any, Mapping

from .provider_clients import (
    GMAIL_KEYCHAIN_SERVICE_FIELDS,
    KeychainItem,
    MacOSKeychain,
    select_account_gmail_profile,
)


def notification_setup_status(
    full_live: Mapping[str, Any],
    provider_bindings: Mapping[str, Any],
    *,
    keychain: MacOSKeychain | None = None,
) -> dict[str, Any]:
    """Report public locators and gaps; metadata presence never proves consent."""

    notifications = full_live.get("notifications", {})
    if not isinstance(notifications, Mapping):
        notifications = {}
    missing: list[str] = []
    items: list[dict[str, str]] = []
    profile_key: str | None = None
    credential_account: str | None = None
    try:
        profile_key, selected = select_account_gmail_profile(
            provider_bindings, full_live
        )
    except (TypeError, ValueError) as exc:
        code = str(exc)
        missing.append(
            code if re.fullmatch(r"GMAIL_[A-Z_]+", code)
            else "GMAIL_ACCOUNT_PROFILE_INVALID"
        )
    else:
        credential_account = str(selected["credential_account"])
        if selected.get("enabled") is not True:
            missing.append("GMAIL_LOCAL_PROFILE_DISABLED")
        reader = keychain or MacOSKeychain()
        for field in GMAIL_KEYCHAIN_SERVICE_FIELDS:
            item = KeychainItem(
                service=str(selected[field]), account=credential_account
            )
            status = reader.metadata_status(item)
            if status not in {"PRESENT", "MISSING", "UNAVAILABLE"}:
                status = "UNAVAILABLE"
            items.append({
                "purpose": field.removesuffix("_service"),
                "service": item.service,
                "account": credential_account,
                "metadata_status": status,
            })
            if status != "PRESENT":
                missing.append(f"GMAIL_{field.upper()}_{status}")

    configured_sink = notifications.get("delivery_sink")
    intended_sink = notifications.get("intended_delivery_sink", configured_sink)
    if configured_sink != "gmail_api":
        missing.append("GMAIL_DELIVERY_SINK_NOT_CONFIGURED")
    if notifications.get("destination_bridge_configured") is not True:
        missing.append("GMAIL_DESTINATION_BRIDGE_NOT_CONFIGURED")
    for field in ("destination_fingerprint", "authorization_binding_id"):
        value = notifications.get(field)
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            missing.append(f"GMAIL_{field.upper()}_MISSING_OR_INVALID")
    route_version = notifications.get("route_version")
    if (
        not isinstance(route_version, str) or not route_version
        or len(route_version) > 128 or any(c.isspace() for c in route_version)
    ):
        missing.append("GMAIL_ROUTE_VERSION_MISSING_OR_INVALID")
    if notifications.get("required_assurance") != "OWNER_CONFIRMED":
        missing.append("GMAIL_OWNER_CONFIRMED_DELIVERY_ASSURANCE_REQUIRED")

    return {
        "schema_version": "titan_notification_setup_status_2026-09-14_v1",
        "status": "BLOCKED" if missing else "EVIDENCE_VERIFICATION_REQUIRED",
        "check_kind": "named_keychain_metadata_only",
        "selected_profile": profile_key,
        "credential_account": credential_account,
        "account_namespace_matches": credential_account is not None,
        "configured_delivery_sink": (
            configured_sink
            if isinstance(configured_sink, str)
            and configured_sink in {"gmail_api", "local_jsonl_staging"}
            else "unrecognized"
        ),
        "intended_delivery_sink": (
            intended_sink
            if isinstance(intended_sink, str)
            and intended_sink in {"gmail_api", "local_jsonl_staging"}
            else "unrecognized"
        ),
        "keychain_items": items,
        "missing_prerequisites": missing,
        "unverified_prerequisites": [
            "OWNER_AUTHORIZED_GMAIL_DESTINATION_CONSENT",
            "AUTHENTIC_DURABLE_GMAIL_SEND_AUTHORIZATION",
            "RELEASE_AND_ACCOUNT_BOUND_NOTIFICATION_ROUTE",
            "VISIBLY_RECEIVED_CURRENT_ROUTE_BOUND_TEST",
        ],
        "credential_contents_read": False,
        "broker_checks_performed": False,
        "market_data_checks_performed": False,
        "network_checks_performed": False,
        "delivery_attempted": False,
        "readiness_evidence_issued": False,
        "ready_for_delivery": False,
    }
