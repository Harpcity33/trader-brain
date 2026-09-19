"""Durable, provenance-bound notifications with independent delivery workers.

The local JSONL sink remains available as staging evidence. Production
delivery is dependency injected: this module neither discovers credentials
nor performs a send unless an owning process supplies an already authorized
provider client. Durable claims keep service and independent workers from
racing the same outbox row.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.policy import SMTP
from enum import Enum
import json
import os
from pathlib import Path
import re
import threading
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.request import Request, urlopen
from uuid import NAMESPACE_URL, uuid4, uuid5

from .latency import LatencyRecorder
from .models import OutboxMessage
from .policy import sha256_json
from .state import LiveStateStore


CORE_EVENTS = frozenset(
    {
        "READINESS",
        "ENTRY_FILLED",
        "PROTECTION_WORKING",
        "EXIT_FILLED",
        "RISK_PAUSED",
        "UNRESOLVED_SUBMISSION",
        "UNPROTECTED_EXPOSURE",
        "AUTHENTICATION_INCIDENT",
        "CLOSEOUT_INCIDENT",
        "RUNTIME_INCIDENT",
        "RECOVERED",
        "END_OF_DAY",
    }
)
URGENT_EVENTS = frozenset(
    {
        "UNRESOLVED_SUBMISSION",
        "UNPROTECTED_EXPOSURE",
        "AUTHENTICATION_INCIDENT",
        "CLOSEOUT_INCIDENT",
        "RUNTIME_INCIDENT",
    }
)
RECEIPT_SCHEMA = "titan_notification_delivery_receipt_v1"
GMAIL_PROVIDER_COMPOSITION_ID = "titan.gmail_api.rfc2822.oauth_injected.v1"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SAFE_PROVIDER = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")


class NotificationDeliveryError(RuntimeError):
    """Internal machine-coded delivery failure safe for durable persistence."""

    def __init__(self, code: str) -> None:
        normalized = str(code).strip().upper()
        if not re.fullmatch(r"NOTIFICATION_[A-Z0-9_]{1,96}", normalized):
            raise ValueError("notification delivery error code is invalid")
        self.code = normalized
        super().__init__(normalized)


def notification_failure_code(error: BaseException) -> str:
    if isinstance(error, NotificationDeliveryError):
        return error.code
    error_type = re.sub(r"[^A-Z0-9]+", "_", type(error).__name__.upper()).strip("_")
    return f"NOTIFICATION_PROVIDER_{error_type or 'ERROR'}"[:128]


def _aware(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(word in lowered for word in ("secret", "token", "password", "credential")):
                result[str(key)] = "[REDACTED]"
            elif "account" in lowered and isinstance(item, str) and len(item) > 4:
                digits = "".join(character for character in item if character.isdigit())
                result[str(key)] = f"ending-{digits[-4:]}" if len(digits) >= 4 else "[REDACTED]"
            else:
                result[str(key)] = _redact(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        value = re.sub(
            r"\b\d{5,}\b",
            lambda match: "•" * (len(match.group()) - 4) + match.group()[-4:],
            value,
        )
    return value


@dataclass(frozen=True)
class Notification:
    dedupe_key: str
    event_type: str
    severity: str
    subject: str
    body: str
    payload: Mapping[str, Any]


def build_notification(event_type: str, payload: Mapping[str, Any]) -> Notification:
    event = str(event_type).strip().upper()
    if event not in CORE_EVENTS:
        raise ValueError("scan chatter and unknown notification events are not allowed")
    clean = _redact(dict(payload))
    state = str(clean.get("state", "unknown")).upper()
    symbol = str(clean.get("symbol", "ACCOUNT")).upper()
    quantity = clean.get("quantity")
    protection = str(clean.get("protection_state", "not_applicable")).upper()
    reason = str(clean.get("reason", ""))
    summary_parts = [f"state={state}", f"symbol={symbol}"]
    if quantity is not None:
        summary_parts.append(f"quantity={quantity}")
    if event in {"ENTRY_FILLED", "PROTECTION_WORKING", "UNPROTECTED_EXPOSURE"}:
        summary_parts.append(f"protection={protection}")
    if reason:
        summary_parts.append(f"reason={reason}")
    subject = f"Titan {event.replace('_', ' ').title()} — {symbol}"
    body = " | ".join(summary_parts)
    identity = clean.get("event_id") or clean.get("intent_id") or clean.get("session_date")
    if not identity:
        identity = sha256_json(clean)
    return Notification(
        dedupe_key=f"{event}:{identity}",
        event_type=event,
        severity="urgent" if event in URGENT_EVENTS else "info",
        subject=subject,
        body=body,
        payload=clean,
    )


def notification_payload_hash(notification: Notification) -> str:
    """Bind a receipt to the exact redacted notification delivered."""

    return sha256_json(
        {
            "dedupe_key": notification.dedupe_key,
            "event_type": notification.event_type,
            "severity": notification.severity,
            "subject": notification.subject,
            "body": notification.body,
            "payload": notification.payload,
        }
    )


class DeliveryAssurance(str, Enum):
    LOCAL_STAGED = "LOCAL_STAGED"
    PROVIDER_ACCEPTED = "PROVIDER_ACCEPTED"
    OWNER_CONFIRMED = "OWNER_CONFIRMED"


_ASSURANCE_RANK = {
    DeliveryAssurance.LOCAL_STAGED: 0,
    DeliveryAssurance.PROVIDER_ACCEPTED: 1,
    DeliveryAssurance.OWNER_CONFIRMED: 2,
}


def destination_fingerprint(provider: str, destination: str) -> str:
    """Return a stable destination identity without retaining the destination."""

    normalized_provider = str(provider).strip().lower()
    normalized_destination = str(destination).strip()
    if not _SAFE_PROVIDER.fullmatch(normalized_provider):
        raise ValueError("notification provider identifier is invalid")
    if not normalized_destination:
        raise ValueError("notification destination is required")
    return sha256_json(
        {"provider": normalized_provider, "destination": normalized_destination}
    )


@dataclass(frozen=True)
class NotificationRoute:
    """Non-secret identity and assurance contract for one delivery route."""

    provider: str
    destination_fingerprint: str
    route_version: str
    required_assurance: DeliveryAssurance = DeliveryAssurance.PROVIDER_ACCEPTED

    def __post_init__(self) -> None:
        provider = str(self.provider).strip().lower()
        if not _SAFE_PROVIDER.fullmatch(provider):
            raise ValueError("notification provider identifier is invalid")
        if not _SHA256.fullmatch(str(self.destination_fingerprint)):
            raise ValueError("destination fingerprint must be lowercase SHA-256")
        version = str(self.route_version).strip()
        if not version or len(version) > 128 or any(item.isspace() for item in version):
            raise ValueError("notification route version is invalid")
        try:
            assurance = DeliveryAssurance(self.required_assurance)
        except ValueError as exc:
            raise ValueError("notification route assurance is invalid") from exc
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "route_version", version)
        object.__setattr__(self, "required_assurance", assurance)

    @property
    def route_id(self) -> str:
        return sha256_json(
            {
                "provider": self.provider,
                "destination_fingerprint": self.destination_fingerprint,
                "route_version": self.route_version,
            }
        )


@dataclass(frozen=True)
class DeliveryReceipt:
    """Provider/local receipt bound to route, event and payload provenance."""

    route_id: str
    provider: str
    destination_fingerprint: str
    route_version: str
    event_key: str
    payload_hash: str
    provider_receipt_id: str
    accepted_at: datetime
    assurance: DeliveryAssurance
    owner_confirmed_at: datetime | None = None
    schema_version: str = RECEIPT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != RECEIPT_SCHEMA:
            raise ValueError("unsupported notification receipt schema")
        if not _SHA256.fullmatch(str(self.route_id)):
            raise ValueError("receipt route_id must be lowercase SHA-256")
        if not _SHA256.fullmatch(str(self.destination_fingerprint)):
            raise ValueError("receipt destination fingerprint is invalid")
        if not _SHA256.fullmatch(str(self.payload_hash)):
            raise ValueError("receipt payload hash is invalid")
        provider = str(self.provider).strip().lower()
        if not _SAFE_PROVIDER.fullmatch(provider):
            raise ValueError("receipt provider identifier is invalid")
        route_version = str(self.route_version).strip()
        event_key = str(self.event_key).strip()
        provider_receipt = str(self.provider_receipt_id).strip()
        if not route_version or not event_key or not provider_receipt:
            raise ValueError("receipt route, event and provider ID are required")
        if len(provider_receipt) > 512:
            raise ValueError("provider receipt ID is too long")
        assurance = DeliveryAssurance(self.assurance)
        accepted_at = _aware(self.accepted_at, "accepted_at")
        owner_at = self.owner_confirmed_at
        if owner_at is not None:
            owner_at = _aware(owner_at, "owner_confirmed_at")
            if owner_at < accepted_at:
                raise ValueError("owner confirmation predates provider acceptance")
        if assurance is DeliveryAssurance.OWNER_CONFIRMED and owner_at is None:
            raise ValueError("owner-confirmed receipt needs confirmation time")
        if assurance is not DeliveryAssurance.OWNER_CONFIRMED and owner_at is not None:
            raise ValueError("owner confirmation time requires OWNER_CONFIRMED assurance")
        expected_route = NotificationRoute(
            provider=provider,
            destination_fingerprint=str(self.destination_fingerprint),
            route_version=route_version,
            required_assurance=assurance,
        ).route_id
        if expected_route != self.route_id:
            raise ValueError("receipt does not match its route identity")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "route_version", route_version)
        object.__setattr__(self, "event_key", event_key)
        object.__setattr__(self, "provider_receipt_id", provider_receipt)
        object.__setattr__(self, "accepted_at", accepted_at)
        object.__setattr__(self, "owner_confirmed_at", owner_at)
        object.__setattr__(self, "assurance", assurance)

    def to_payload(self) -> Mapping[str, Any]:
        return {
            "schema_version": self.schema_version,
            "route_id": self.route_id,
            "provider": self.provider,
            "destination_fingerprint": self.destination_fingerprint,
            "route_version": self.route_version,
            "event_key": self.event_key,
            "payload_hash": self.payload_hash,
            "provider_receipt_id": self.provider_receipt_id,
            "accepted_at": self.accepted_at.isoformat(),
            "assurance": self.assurance.value,
            "owner_confirmed_at": (
                self.owner_confirmed_at.isoformat()
                if self.owner_confirmed_at is not None
                else None
            ),
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_payload(), sort_keys=True, separators=(",", ":"), allow_nan=False
        )

    @property
    def receipt_hash(self) -> str:
        return sha256_json(self.to_payload())

    @classmethod
    def from_json(cls, raw: str) -> "DeliveryReceipt":
        try:
            value = json.loads(str(raw))
        except json.JSONDecodeError as exc:
            raise ValueError("notification receipt is not valid JSON") from exc
        required = {
            "schema_version",
            "route_id",
            "provider",
            "destination_fingerprint",
            "route_version",
            "event_key",
            "payload_hash",
            "provider_receipt_id",
            "accepted_at",
            "assurance",
            "owner_confirmed_at",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            raise ValueError("notification receipt fields are invalid")
        try:
            accepted_at = datetime.fromisoformat(str(value["accepted_at"]))
            owner_at = (
                datetime.fromisoformat(str(value["owner_confirmed_at"]))
                if value["owner_confirmed_at"] is not None
                else None
            )
        except ValueError as exc:
            raise ValueError("notification receipt timestamps are invalid") from exc
        return cls(
            schema_version=str(value["schema_version"]),
            route_id=str(value["route_id"]),
            provider=str(value["provider"]),
            destination_fingerprint=str(value["destination_fingerprint"]),
            route_version=str(value["route_version"]),
            event_key=str(value["event_key"]),
            payload_hash=str(value["payload_hash"]),
            provider_receipt_id=str(value["provider_receipt_id"]),
            accepted_at=accepted_at,
            assurance=DeliveryAssurance(str(value["assurance"])),
            owner_confirmed_at=owner_at,
        )

    def owner_confirmed(self, *, confirmed_at: datetime) -> "DeliveryReceipt":
        when = _aware(confirmed_at, "confirmed_at")
        if self.assurance is DeliveryAssurance.LOCAL_STAGED:
            raise ValueError("local staging cannot be owner-confirmed as provider delivery")
        return replace(
            self,
            assurance=DeliveryAssurance.OWNER_CONFIRMED,
            owner_confirmed_at=when,
        )


def receipt_satisfies_route(
    receipt: DeliveryReceipt | str,
    route: NotificationRoute,
    *,
    event_key: str | None = None,
    payload_hash: str | None = None,
    minimum_assurance: DeliveryAssurance | None = None,
) -> bool:
    """Strictly validate a receipt against the current configured route."""

    try:
        parsed = receipt if isinstance(receipt, DeliveryReceipt) else DeliveryReceipt.from_json(receipt)
        minimum = DeliveryAssurance(minimum_assurance or route.required_assurance)
    except (TypeError, ValueError):
        return False
    if (
        parsed.route_id != route.route_id
        or parsed.provider != route.provider
        or parsed.destination_fingerprint != route.destination_fingerprint
        or parsed.route_version != route.route_version
        or _ASSURANCE_RANK[parsed.assurance] < _ASSURANCE_RANK[minimum]
    ):
        return False
    if event_key is not None and parsed.event_key != event_key:
        return False
    if payload_hash is not None and parsed.payload_hash != payload_hash:
        return False
    return True


class NotificationSink(Protocol):
    @property
    def route(self) -> NotificationRoute: ...

    def send(self, notification: Notification) -> DeliveryReceipt:
        """Deliver one notification and return a provenance-bound receipt."""


class AuthorizedProviderSender(Protocol):
    """Already-authorized provider client injected by the owning process."""

    def __call__(
        self,
        notification: Notification,
        *,
        idempotency_key: str,
        timeout_seconds: float,
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class GmailAuthorizationEvidence:
    """Redacted identity of an owner-injected Gmail authorization."""

    binding_id: str
    credential_source: str
    scopes: tuple[str, ...]
    authenticated: bool
    provider: str = "gmail"

    def __post_init__(self) -> None:
        if self.provider != "gmail":
            raise ValueError("Gmail authorization provider is invalid")
        if not _SHA256.fullmatch(str(self.binding_id)):
            raise ValueError("Gmail authorization binding must be lowercase SHA-256")
        if not str(self.credential_source).strip():
            raise ValueError("redacted Gmail credential source is required")
        scopes = tuple(str(item).strip() for item in self.scopes)
        if not scopes or any(not item for item in scopes):
            raise ValueError("Gmail authorization scopes must be explicit")
        if not any(item == "gmail.send" or item.endswith("/auth/gmail.send") for item in scopes):
            raise ValueError("Gmail authorization lacks gmail.send scope")
        if self.authenticated is not True:
            raise ValueError("Gmail authorization is not authenticated")
        object.__setattr__(self, "scopes", scopes)


class GmailRequestAuthorizer(Protocol):
    """Injected secret custodian; the sender never discovers credentials."""

    @property
    def evidence(self) -> GmailAuthorizationEvidence: ...

    def authorize(self, headers: Mapping[str, str]) -> Mapping[str, str]: ...


class GmailApiSender:
    """Concrete zero-new-subscription Gmail ``users.messages.send`` client.

    The owner supplies an already-authorized token provider. The MIME message
    uses a deterministic RFC 2822 Message-ID/idempotency header, the request is
    bounded, and only the Gmail response ID is returned as provider evidence.
    Authorization headers and raw addresses are never logged or returned.
    """

    ENDPOINT = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"

    def __init__(
        self,
        *,
        route: NotificationRoute,
        authorizer: GmailRequestAuthorizer,
        destination: str,
        sender_address: str,
        opener: Callable[..., Any] = urlopen,
        clock: Callable[[], datetime] | None = None,
        maximum_response_bytes: int = 1024 * 1024,
    ) -> None:
        if route.provider != "gmail":
            raise ValueError("Gmail sender requires a gmail route")
        if destination_fingerprint("gmail", destination) != route.destination_fingerprint:
            raise ValueError("Gmail destination does not match route fingerprint")
        if not str(sender_address).strip() or "\n" in sender_address or "\r" in sender_address:
            raise ValueError("Gmail sender address is invalid")
        if "\n" in destination or "\r" in destination:
            raise ValueError("Gmail destination is invalid")
        if maximum_response_bytes <= 0:
            raise ValueError("Gmail response size limit must be positive")
        # Access validates the redacted binding and required provider scope;
        # token material remains behind ``authorize``.
        self.authorization = authorizer.evidence
        self.route = route
        self._authorizer = authorizer
        self._destination = str(destination).strip()
        self._sender_address = str(sender_address).strip()
        self._opener = opener
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._maximum_response_bytes = int(maximum_response_bytes)

    @staticmethod
    def _message_id(idempotency_key: str) -> str:
        digest = sha256_json({"event_key": str(idempotency_key)})
        return f"<{digest}@titan-notifications.local>"

    def __call__(
        self,
        notification: Notification,
        *,
        idempotency_key: str,
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        if notification.dedupe_key != str(idempotency_key):
            raise ValueError("Gmail idempotency key must equal the notification event key")
        if not 0 < float(timeout_seconds) <= 30:
            raise ValueError("Gmail request timeout must be in (0, 30]")
        message = EmailMessage(policy=SMTP)
        message["To"] = self._destination
        message["From"] = self._sender_address
        message["Subject"] = notification.subject
        message["Message-ID"] = self._message_id(idempotency_key)
        message["X-Titan-Event-Key"] = str(idempotency_key)
        message.set_content(notification.body)
        raw_message = base64.urlsafe_b64encode(message.as_bytes(policy=SMTP)).decode(
            "ascii"
        )
        body = json.dumps(
            {"raw": raw_message}, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        headers = dict(
            self._authorizer.authorize(
                {
                    "Accept": "application/json",
                    "Content-Type": "application/json; charset=utf-8",
                    "User-Agent": "titan-full-live/2",
                }
            )
        )
        if not headers or any(not str(key).strip() for key in headers):
            raise RuntimeError("injected Gmail authorization returned no headers")
        request = Request(self.ENDPOINT, data=body, method="POST", headers=headers)
        try:
            with self._opener(request, timeout=float(timeout_seconds)) as response:
                payload = response.read(self._maximum_response_bytes + 1)
        except Exception as exc:
            raise RuntimeError(f"Gmail API request failed: {type(exc).__name__}") from exc
        if len(payload) > self._maximum_response_bytes:
            raise RuntimeError("Gmail API response exceeds size limit")
        try:
            decoded = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Gmail API response is not valid JSON") from exc
        if not isinstance(decoded, Mapping) or not str(decoded.get("id", "")).strip():
            raise RuntimeError("Gmail API response has no provider message ID")
        return {
            "provider": self.route.provider,
            "destination_fingerprint": self.route.destination_fingerprint,
            "provider_receipt_id": str(decoded["id"]).strip(),
            "accepted_at": _aware(self._clock(), "Gmail completion clock"),
        }


@dataclass(frozen=True)
class GmailProviderBinding:
    """Runtime-only Gmail values supplied by the authorized owner process."""

    authorizer: GmailRequestAuthorizer
    destination: str
    sender_address: str

    @property
    def implementation_id(self) -> str:
        """Manifest-bound implementation selected by signed configuration."""

        return GMAIL_PROVIDER_COMPOSITION_ID

    @property
    def authorization_binding_id(self) -> str:
        """Non-secret identity of the injected authorization, never its token."""

        return self.authorizer.evidence.binding_id

    def release_components(self) -> tuple[tuple[str, object, tuple[str, ...]], ...]:
        """Expose the secret custodian's executable code for release binding."""

        return (("gmail_authorizer", self.authorizer, ("authorize",)),)


def notification_route_from_config(config: Mapping[str, Any]) -> NotificationRoute:
    """Build the exact non-secret route identity from signed configuration."""

    if str(config.get("delivery_sink", "")) != "gmail_api":
        raise ValueError("signed notification config has no provider route")
    if config.get("destination_bridge_configured") is not True:
        raise ValueError("signed notification destination bridge is disabled")
    return NotificationRoute(
        provider=str(config.get("provider", "")),
        destination_fingerprint=str(config.get("destination_fingerprint", "")),
        route_version=str(config.get("route_version", "")),
        required_assurance=DeliveryAssurance(
            str(config.get("required_assurance", ""))
        ),
    )


def build_notification_sink(
    config: Mapping[str, Any],
    *,
    local_jsonl_path: str | Path,
    gmail: GmailProviderBinding | None = None,
    opener: Callable[..., Any] = urlopen,
    clock: Callable[[], datetime] | None = None,
) -> NotificationSink:
    """Compose a sink from signed config plus runtime-only injected auth.

    The checked-in configuration selects local staging and remains blocked.
    A future owner-signed ``gmail_api`` route cannot start without an injected
    provider-supported authorization and matching in-memory addresses.
    """

    sink = str(config.get("delivery_sink", ""))
    if sink == "local_jsonl_staging":
        return JsonlNotificationSink(local_jsonl_path, clock=clock)
    if sink != "gmail_api":
        raise ValueError("unsupported signed notification sink")
    route = notification_route_from_config(config)
    if gmail is None:
        raise ValueError("Gmail runtime authorization binding was not injected")
    sender = GmailApiSender(
        route=route,
        authorizer=gmail.authorizer,
        destination=gmail.destination,
        sender_address=gmail.sender_address,
        opener=opener,
        clock=clock,
    )
    return InjectedProviderNotificationSink(
        route=route,
        sender=sender,
        timeout_seconds=float(config.get("timeout_seconds", 5)),
    )


class OutboxStore(Protocol):
    def enqueue_notification(self, notification: Notification, created_at: datetime) -> str: ...
    def claim_notifications(
        self,
        now: datetime,
        claim_owner: str,
        claim_expires_at: datetime,
        limit: int,
    ) -> Sequence[Mapping[str, Any]]: ...
    def notification_sent(
        self,
        outbox_id: str,
        receipt: DeliveryReceipt | str,
        delivered_at: datetime,
        claim_owner: str,
    ) -> None: ...
    def notification_failed(
        self,
        outbox_id: str,
        error: str,
        attempted_at: datetime,
        next_attempt_at: datetime,
        claim_owner: str,
    ) -> None: ...


class JsonlNotificationSink:
    """Zero-service-cost local staging; not proof of end-device delivery."""

    def __init__(
        self,
        path: str | Path,
        *,
        route_version: str = "local-jsonl-v1",
        clock: Callable[[], datetime] | None = None,
    ):
        self.path = Path(path)
        self.route = NotificationRoute(
            provider="local_jsonl",
            destination_fingerprint=destination_fingerprint(
                "local_jsonl", str(self.path.expanduser().resolve(strict=False))
            ),
            route_version=route_version,
            required_assurance=DeliveryAssurance.LOCAL_STAGED,
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def send(self, notification: Notification) -> DeliveryReceipt:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload_hash = notification_payload_hash(notification)
        row = {
            "dedupe_key": notification.dedupe_key,
            "event_type": notification.event_type,
            "severity": notification.severity,
            "subject": notification.subject,
            "body": notification.body,
            "payload": notification.payload,
            "delivery_route_id": self.route.route_id,
            "delivery_payload_hash": payload_hash,
        }
        encoded = (
            json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
        ).encode()
        descriptor = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        accepted_at = _aware(self._clock(), "notification sink clock")
        return DeliveryReceipt(
            route_id=self.route.route_id,
            provider=self.route.provider,
            destination_fingerprint=self.route.destination_fingerprint,
            route_version=self.route.route_version,
            event_key=notification.dedupe_key,
            payload_hash=payload_hash,
            provider_receipt_id=sha256_json(row),
            accepted_at=accepted_at,
            assurance=DeliveryAssurance.LOCAL_STAGED,
        )


class InjectedProviderNotificationSink:
    """Bounded production sink over an injected authorized provider client."""

    def __init__(
        self,
        *,
        route: NotificationRoute,
        sender: AuthorizedProviderSender,
        timeout_seconds: float = 5.0,
    ) -> None:
        if route.provider == "local_jsonl":
            raise ValueError("provider sink cannot use the local JSONL route")
        if not 0 < float(timeout_seconds) <= 30:
            raise ValueError("notification provider timeout must be in (0, 30]")
        self.route = route
        self._sender = sender
        self.timeout_seconds = float(timeout_seconds)

    def send(self, notification: Notification) -> DeliveryReceipt:
        payload_hash = notification_payload_hash(notification)
        response = self._sender(
            notification,
            idempotency_key=notification.dedupe_key,
            timeout_seconds=self.timeout_seconds,
        )
        if not isinstance(response, Mapping):
            raise RuntimeError("notification provider response must be an object")
        if response.get("provider") not in (None, self.route.provider):
            raise RuntimeError("notification provider response route mismatch")
        if response.get("destination_fingerprint") not in (
            None,
            self.route.destination_fingerprint,
        ):
            raise RuntimeError("notification provider response destination mismatch")
        receipt_id = str(response.get("provider_receipt_id", "")).strip()
        raw_accepted_at = response.get("accepted_at")
        if isinstance(raw_accepted_at, datetime):
            accepted_at = raw_accepted_at
        elif isinstance(raw_accepted_at, str):
            try:
                accepted_at = datetime.fromisoformat(raw_accepted_at)
            except ValueError as exc:
                raise RuntimeError("notification provider acceptance time is invalid") from exc
        else:
            raise RuntimeError("notification provider acceptance time is missing")
        return DeliveryReceipt(
            route_id=self.route.route_id,
            provider=self.route.provider,
            destination_fingerprint=self.route.destination_fingerprint,
            route_version=self.route.route_version,
            event_key=notification.dedupe_key,
            payload_hash=payload_hash,
            provider_receipt_id=receipt_id,
            accepted_at=accepted_at,
            assurance=DeliveryAssurance.PROVIDER_ACCEPTED,
        )


class LiveStateOutboxAdapter:
    """Adapt :class:`LiveStateStore` without weakening durability."""

    def __init__(self, store: LiveStateStore, account_key: str):
        self.store = store
        self.account_key = str(account_key)

    def enqueue_notification(self, notification: Notification, created_at: datetime) -> str:
        message_id = str(uuid5(NAMESPACE_URL, f"titan-outbox:{notification.dedupe_key}"))
        self.store.enqueue_notification(
            OutboxMessage(
                message_id=message_id,
                event_key=notification.dedupe_key,
                account_key=self.account_key,
                template=notification.event_type,
                payload={
                    "severity": notification.severity,
                    "subject": notification.subject,
                    "body": notification.body,
                    "payload": notification.payload,
                },
                created_at=created_at,
            )
        )
        return message_id

    @staticmethod
    def _normalized_rows(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        result: list[Mapping[str, Any]] = []
        for row in rows:
            try:
                decoded = json.loads(str(row["payload_json"]))
            except json.JSONDecodeError:
                decoded = None
            if (
                isinstance(decoded, Mapping)
                and set(("severity", "subject", "body", "payload")).issubset(decoded)
                and isinstance(decoded["payload"], Mapping)
            ):
                notification = Notification(
                    dedupe_key=str(row["event_key"]),
                    event_type=str(row["template"]),
                    severity=str(decoded["severity"]),
                    subject=str(decoded["subject"]),
                    body=str(decoded["body"]),
                    payload=dict(decoded["payload"]),
                )
            else:
                template = str(row["template"]).upper()
                event_type = (
                    "UNRESOLVED_SUBMISSION"
                    if "SUBMISSION_UNKNOWN" in template
                    else "RUNTIME_INCIDENT"
                )
                raw_payload = (
                    dict(decoded)
                    if isinstance(decoded, Mapping)
                    else {
                        "event_id": str(row["event_key"]),
                        "state": "blocked",
                        "symbol": "ACCOUNT",
                        "reason": "malformed durable notification payload",
                    }
                )
                raw_payload.setdefault("event_id", str(row["event_key"]))
                notification = build_notification(event_type, raw_payload)
            result.append(
                {
                    "outbox_id": row["message_id"],
                    "dedupe_key": notification.dedupe_key,
                    "event_type": notification.event_type,
                    "severity": notification.severity,
                    "subject": notification.subject,
                    "body": notification.body,
                    "payload_json": json.dumps(
                        notification.payload, sort_keys=True, separators=(",", ":")
                    ),
                    "attempts": row["attempt_count"],
                    "created_at": row["created_at"],
                    "claim_owner": row.get("claim_owner"),
                    "claim_expires_at": row.get("claim_expires_at"),
                }
            )
        return result

    def due_notifications(self, now: datetime, limit: int) -> Sequence[Mapping[str, Any]]:
        """Read-only diagnostic compatibility; dispatchers use durable claims."""

        return self._normalized_rows(self.store.due_outbox(now=now, limit=limit))

    def claim_notifications(
        self,
        now: datetime,
        claim_owner: str,
        claim_expires_at: datetime,
        limit: int,
    ) -> Sequence[Mapping[str, Any]]:
        return self._normalized_rows(
            self.store.claim_due_outbox(
                now=now,
                claim_owner=claim_owner,
                claim_expires_at=claim_expires_at,
                limit=limit,
            )
        )

    def notification_sent(
        self,
        outbox_id: str,
        receipt: DeliveryReceipt | str,
        delivered_at: datetime,
        claim_owner: str,
    ) -> None:
        if isinstance(receipt, DeliveryReceipt):
            self.store.mark_notification_attempt(
                outbox_id,
                attempted_at=delivered_at,
                delivered=True,
                delivery_receipt=receipt.to_json(),
                claim_owner=claim_owner,
                delivery_route_id=receipt.route_id,
                delivery_assurance=receipt.assurance.value,
                delivery_receipt_hash=receipt.receipt_hash,
                delivery_payload_hash=receipt.payload_hash,
            )
        else:
            # Unstructured legacy receipts can never pass route readiness.
            self.store.mark_notification_attempt(
                outbox_id,
                attempted_at=delivered_at,
                delivered=True,
                delivery_receipt=str(receipt),
                claim_owner=claim_owner,
            )

    def notification_failed(
        self,
        outbox_id: str,
        error: str,
        attempted_at: datetime,
        next_attempt_at: datetime,
        claim_owner: str,
    ) -> None:
        self.store.mark_notification_attempt(
            outbox_id,
            attempted_at=attempted_at,
            delivered=False,
            error=error,
            next_attempt_at=next_attempt_at,
            claim_owner=claim_owner,
        )


class EnqueueOnlyOutbox:
    """Coordinator-side outbox writer; delivery belongs to another process."""

    independent_delivery = True

    def __init__(self, store: LiveStateOutboxAdapter):
        self.store = store

    def enqueue(
        self, event_type: str, payload: Mapping[str, Any], now: datetime
    ) -> str:
        if now.tzinfo is None:
            raise ValueError("notification time must be timezone-aware")
        return self.store.enqueue_notification(
            build_notification(event_type, payload), now
        )


class OutboxDispatcher:
    def __init__(
        self,
        store: OutboxStore,
        sink: NotificationSink,
        *,
        latency: LatencyRecorder | None = None,
        completion_clock: Callable[[], datetime] | None = None,
        worker_id: str | None = None,
        claim_ttl_seconds: float = 30.0,
    ):
        if not 0 < float(claim_ttl_seconds) <= 300:
            raise ValueError("notification claim TTL must be in (0, 300]")
        provider_timeout = float(getattr(sink, "timeout_seconds", 0.0))
        if provider_timeout and float(claim_ttl_seconds) <= provider_timeout:
            raise ValueError(
                "notification claim TTL must exceed the provider timeout"
            )
        self.store = store
        self.sink = sink
        self.latency = latency
        self.worker_id = worker_id or f"outbox-{os.getpid()}-{uuid4().hex}"
        if len(self.worker_id) > 128 or not re.fullmatch(r"[A-Za-z0-9._:-]+", self.worker_id):
            raise ValueError("notification worker ID is invalid")
        self.claim_ttl_seconds = float(claim_ttl_seconds)
        self._completion_clock = completion_clock or (lambda: datetime.now(timezone.utc))

    def enqueue(self, event_type: str, payload: Mapping[str, Any], now: datetime) -> str:
        if now.tzinfo is None:
            raise ValueError("notification time must be timezone-aware")
        return self.store.enqueue_notification(build_notification(event_type, payload), now)

    def _validate_receipt(
        self, receipt: DeliveryReceipt | str, notification: Notification
    ) -> None:
        route = getattr(self.sink, "route", None)
        if isinstance(receipt, DeliveryReceipt):
            if not isinstance(route, NotificationRoute):
                raise NotificationDeliveryError(
                    "NOTIFICATION_RECEIPT_ROUTE_UNDECLARED"
                )
            minimum = (
                DeliveryAssurance.LOCAL_STAGED
                if route.provider == "local_jsonl"
                else DeliveryAssurance.PROVIDER_ACCEPTED
            )
            if not receipt_satisfies_route(
                receipt,
                route,
                event_key=notification.dedupe_key,
                payload_hash=notification_payload_hash(notification),
                minimum_assurance=minimum,
            ):
                raise NotificationDeliveryError(
                    "NOTIFICATION_RECEIPT_PROVENANCE_MISMATCH"
                )
        elif isinstance(route, NotificationRoute):
            raise NotificationDeliveryError("NOTIFICATION_RECEIPT_UNSTRUCTURED")
        elif not isinstance(receipt, str) or not receipt.strip():
            raise NotificationDeliveryError("NOTIFICATION_RECEIPT_EMPTY")

    def drain(self, now: datetime, *, limit: int = 20) -> tuple[int, int]:
        if now.tzinfo is None:
            raise ValueError("notification time must be timezone-aware")
        claim_expires_at = now + timedelta(seconds=self.claim_ttl_seconds)
        sent = failed = 0
        rows = self.store.claim_notifications(now, self.worker_id, claim_expires_at, limit)
        for row in rows:
            notification = Notification(
                dedupe_key=str(row["dedupe_key"]),
                event_type=str(row["event_type"]),
                severity=str(row["severity"]),
                subject=str(row["subject"]),
                body=str(row["body"]),
                payload=json.loads(str(row["payload_json"])),
            )
            try:
                receipt = self.sink.send(notification)
                self._validate_receipt(receipt, notification)
                self.store.notification_sent(
                    str(row["outbox_id"]), receipt, now, self.worker_id
                )
            except Exception as exc:
                failed += 1
                attempts = int(row.get("attempts", 0)) + 1
                delay = min(300, 2 ** min(attempts, 8))
                try:
                    self.store.notification_failed(
                        str(row["outbox_id"]),
                        notification_failure_code(exc),
                        now,
                        now + timedelta(seconds=delay),
                        self.worker_id,
                    )
                except Exception:
                    # A provider call may outlive a lease that another worker
                    # reclaimed. Never overwrite the new claim owner.
                    pass
            else:
                sent += 1
                try:
                    delivered_at = self._completion_clock()
                    if delivered_at.tzinfo is None:
                        raise ValueError("notification completion time must be aware")
                except Exception:
                    delivered_at = None
                if delivered_at is not None:
                    self._record_delivery_latency(row, notification, delivered_at)
        return sent, failed

    def _record_delivery_latency(
        self,
        row: Mapping[str, Any],
        notification: Notification,
        delivered_at: datetime,
    ) -> None:
        if self.latency is None or row.get("created_at") is None:
            return
        try:
            confirmed_at = datetime.fromisoformat(str(row["created_at"]))
            if confirmed_at.tzinfo is None:
                return
            duration_ms = (delivered_at - confirmed_at).total_seconds() * 1000
            self.latency.record_duration(
                "confirmed_event_to_notification",
                duration_ms,
                observed_at=delivered_at,
                correlation_id=str(row["outbox_id"]),
                metadata={
                    "event_type": notification.event_type,
                    "dedupe_key": notification.dedupe_key,
                },
            )
        except Exception:
            return


class NotificationWorker:
    """Independent bounded outbox worker; it has no broker/order dependency."""

    def __init__(
        self,
        dispatcher: OutboxDispatcher,
        *,
        interval_seconds: float = 1.0,
        batch_limit: int = 20,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not 0.05 <= float(interval_seconds) <= 60:
            raise ValueError("notification worker interval must be in [0.05, 60]")
        if not 1 <= int(batch_limit) <= 100:
            raise ValueError("notification worker batch must be in [1, 100]")
        self.dispatcher = dispatcher
        self.interval_seconds = float(interval_seconds)
        self.batch_limit = int(batch_limit)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def run_once(self, *, now: datetime | None = None) -> tuple[int, int]:
        current = _aware(now or self._clock(), "notification worker clock")
        return self.dispatcher.drain(current, limit=self.batch_limit)

    def run(self, stop_event: threading.Event) -> tuple[int, int]:
        total_sent = total_failed = 0
        while not stop_event.is_set():
            sent, failed = self.run_once()
            total_sent += sent
            total_failed += failed
            stop_event.wait(self.interval_seconds)
        return total_sent, total_failed


__all__ = [
    "AuthorizedProviderSender",
    "CORE_EVENTS",
    "DeliveryAssurance",
    "DeliveryReceipt",
    "EnqueueOnlyOutbox",
    "GMAIL_PROVIDER_COMPOSITION_ID",
    "GmailApiSender",
    "GmailAuthorizationEvidence",
    "GmailProviderBinding",
    "GmailRequestAuthorizer",
    "InjectedProviderNotificationSink",
    "JsonlNotificationSink",
    "LiveStateOutboxAdapter",
    "Notification",
    "NotificationDeliveryError",
    "NotificationRoute",
    "NotificationSink",
    "NotificationWorker",
    "OutboxDispatcher",
    "RECEIPT_SCHEMA",
    "build_notification",
    "build_notification_sink",
    "destination_fingerprint",
    "notification_route_from_config",
    "notification_payload_hash",
    "notification_failure_code",
    "receipt_satisfies_route",
]
