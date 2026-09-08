"""Concrete, secret-custodied clients for the local full-live assembly.

Only provider-supported HTTPS/WSS endpoints are accepted.  Secrets are read
from explicitly named macOS Keychain items, retained in process memory, and
never included in exceptions, reports, configuration, command arguments, or
logs.  This module intentionally contains no broker mutation implementation:
the locally verified Robinhood MCP contract still requires attended approval.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import re
import socket
import ssl
import struct
import subprocess
import threading
import time
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from .massive_adapter import (
    MassiveAuthorizationEvidence,
    MassiveStreamStatus,
    MassiveStoreError,
)
from .notifications import GmailAuthorizationEvidence, NotificationDeliveryError


GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_KEYCHAIN_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}")


class CredentialUnavailable(RuntimeError):
    """A stable error code for missing or unsafe private credential material."""

    def __init__(self, code: str) -> None:
        normalized = str(code).strip().upper()
        if not re.fullmatch(r"CREDENTIAL_[A-Z0-9_]{1,96}", normalized):
            raise ValueError("credential error code is invalid")
        self.code = normalized
        super().__init__(normalized)


def _aware(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _binding(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class KeychainItem:
    """A public locator for one private macOS Keychain generic-password item."""

    service: str
    account: str | None = None

    def __post_init__(self) -> None:
        for field, value in (("service", self.service), ("account", self.account)):
            if value is None and field == "account":
                continue
            if not _KEYCHAIN_LABEL.fullmatch(str(value)):
                raise ValueError(f"keychain {field} label is invalid")

    @property
    def source_label(self) -> str:
        suffix = f":{self.account}" if self.account else ""
        return f"macos-keychain:{self.service}{suffix}"


class MacOSKeychain:
    """Read-only Keychain loader with bounded output and sanitized failures."""

    SECURITY = "/usr/bin/security"

    def __init__(self, *, timeout_seconds: float = 10.0, maximum_bytes: int = 65536):
        if not 0 < float(timeout_seconds) <= 30:
            raise ValueError("keychain timeout must be in (0, 30]")
        if not 1 <= int(maximum_bytes) <= 1024 * 1024:
            raise ValueError("keychain item size limit is invalid")
        self.timeout_seconds = float(timeout_seconds)
        self.maximum_bytes = int(maximum_bytes)

    def read(self, item: KeychainItem) -> bytes:
        arguments = [self.SECURITY, "find-generic-password", "-s", item.service]
        if item.account:
            arguments.extend(("-a", item.account))
        arguments.append("-w")
        try:
            result = subprocess.run(
                arguments,
                check=False,
                capture_output=True,
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise CredentialUnavailable(
                f"CREDENTIAL_KEYCHAIN_{type(exc).__name__.upper()}"
            ) from exc
        # Never preserve stderr: macOS may include item metadata in it.
        secret = bytes(result.stdout).rstrip(b"\r\n")
        if result.returncode != 0 or not secret:
            raise CredentialUnavailable("CREDENTIAL_KEYCHAIN_ITEM_UNAVAILABLE")
        if len(secret) > self.maximum_bytes or b"\x00" in secret:
            raise CredentialUnavailable("CREDENTIAL_KEYCHAIN_ITEM_INVALID")
        return secret

    def read_text(self, item: KeychainItem) -> str:
        try:
            return self.read(item).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CredentialUnavailable("CREDENTIAL_KEYCHAIN_ITEM_NOT_UTF8") from exc


class KeychainMassiveAuthorizer:
    """Massive REST/WSS authorizer backed by the existing local Keychain item."""

    def __init__(
        self,
        keychain: MacOSKeychain,
        item: KeychainItem,
        *,
        scopes: Sequence[str] = ("stocks:read",),
    ) -> None:
        secret = keychain.read(item)
        normalized_scopes = tuple(str(value).strip() for value in scopes)
        if normalized_scopes != ("stocks:read",):
            raise ValueError("Massive local assembly permits only stocks:read")
        self._secret = secret
        self._evidence = MassiveAuthorizationEvidence(
            binding_id=_binding(
                {
                    "provider": "massive",
                    "credential_source": item.source_label,
                    "credential_fingerprint": hashlib.sha256(secret).hexdigest(),
                    "scopes": normalized_scopes,
                }
            ),
            credential_source=item.source_label,
            scopes=normalized_scopes,
            authenticated=True,
        )

    @property
    def evidence(self) -> MassiveAuthorizationEvidence:
        return self._evidence

    def authorize(self, headers: Mapping[str, str]) -> Mapping[str, str]:
        return {**dict(headers), "Authorization": "Bearer " + self._secret.decode("ascii")}

    def stream_credential(self) -> str:
        """Return the in-memory credential only to the release-bound WSS client."""

        try:
            return self._secret.decode("ascii")
        except UnicodeDecodeError as exc:
            raise CredentialUnavailable("CREDENTIAL_MASSIVE_KEY_NOT_ASCII") from exc


class WebSocketProtocolError(RuntimeError):
    pass


class _MinimalWebSocket:
    """Small RFC 6455 client sufficient for Massive's JSON stock stream."""

    MAX_FRAME_BYTES = 64 * 1024 * 1024

    def __init__(self, url: str, *, timeout_seconds: float) -> None:
        parsed = urlparse(url)
        if (
            parsed.scheme != "wss"
            or parsed.hostname not in {"socket.massive.com", "socket.polygon.io"}
            or parsed.fragment
        ):
            raise ValueError("Massive stream must use an approved WSS endpoint")
        self.url = url
        self.timeout_seconds = float(timeout_seconds)
        self.socket: ssl.SSLSocket | None = None
        self._buffer = bytearray()
        self._send_lock = threading.Lock()

    def connect(self) -> None:
        parsed = urlparse(self.url)
        assert parsed.hostname is not None
        port = parsed.port or 443
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        raw = socket.create_connection(
            (parsed.hostname, port), timeout=self.timeout_seconds
        )
        try:
            context = ssl.create_default_context()
            sock = context.wrap_socket(raw, server_hostname=parsed.hostname)
            sock.settimeout(self.timeout_seconds)
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            request = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {parsed.hostname}:{port}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                "User-Agent: titan-full-live/2\r\n\r\n"
            ).encode("ascii")
            sock.sendall(request)
            response = bytearray()
            while b"\r\n\r\n" not in response:
                chunk = sock.recv(4096)
                if not chunk:
                    raise WebSocketProtocolError("MASSIVE_STREAM_HANDSHAKE_CLOSED")
                response.extend(chunk)
                if len(response) > 65536:
                    raise WebSocketProtocolError("MASSIVE_STREAM_HANDSHAKE_OVERSIZED")
            headers_raw, leftover = bytes(response).split(b"\r\n\r\n", 1)
            lines = headers_raw.decode("iso-8859-1").split("\r\n")
            if " 101 " not in lines[0]:
                raise WebSocketProtocolError("MASSIVE_STREAM_UPGRADE_REJECTED")
            headers: dict[str, str] = {}
            for line in lines[1:]:
                if ":" in line:
                    name, value = line.split(":", 1)
                    headers[name.strip().lower()] = value.strip()
            expected = base64.b64encode(
                hashlib.sha1(
                    (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
                ).digest()
            ).decode("ascii")
            if headers.get("sec-websocket-accept") != expected:
                raise WebSocketProtocolError("MASSIVE_STREAM_HANDSHAKE_INVALID")
            self.socket = sock
            self._buffer.extend(leftover)
        except BaseException:
            raw.close()
            raise

    def close(self) -> None:
        sock = self.socket
        if sock is None:
            return
        try:
            self._send_frame(0x8, b"")
        except OSError:
            pass
        finally:
            sock.close()
            self.socket = None

    def send_json(self, value: Mapping[str, object]) -> None:
        encoded = json.dumps(
            dict(value), separators=(",", ":"), ensure_ascii=True, allow_nan=False
        ).encode("utf-8")
        self._send_frame(0x1, encoded)

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        sock = self.socket
        if sock is None:
            raise WebSocketProtocolError("MASSIVE_STREAM_NOT_CONNECTED")
        length = len(payload)
        mask = os.urandom(4)
        if length < 126:
            header = struct.pack("!BB", 0x80 | opcode, 0x80 | length)
        elif length <= 0xFFFF:
            header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, length)
        else:
            header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, length)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        with self._send_lock:
            sock.sendall(header + mask + masked)

    def _read_exact(self, count: int) -> bytes:
        sock = self.socket
        if sock is None:
            raise WebSocketProtocolError("MASSIVE_STREAM_NOT_CONNECTED")
        while len(self._buffer) < count:
            chunk = sock.recv(max(4096, count - len(self._buffer)))
            if not chunk:
                raise WebSocketProtocolError("MASSIVE_STREAM_CONNECTION_CLOSED")
            self._buffer.extend(chunk)
        result = bytes(self._buffer[:count])
        del self._buffer[:count]
        return result

    def receive_json(self, *, timeout_seconds: float) -> object | None:
        sock = self.socket
        if sock is None:
            raise WebSocketProtocolError("MASSIVE_STREAM_NOT_CONNECTED")
        sock.settimeout(timeout_seconds)
        fragments = bytearray()
        started = False
        while True:
            try:
                first, second = self._read_exact(2)
                fin = bool(first & 0x80)
                opcode = first & 0x0F
                if first & 0x70:
                    raise WebSocketProtocolError("MASSIVE_STREAM_EXTENSION_UNSUPPORTED")
                length = second & 0x7F
                if length == 126:
                    length = struct.unpack("!H", self._read_exact(2))[0]
                elif length == 127:
                    length = struct.unpack("!Q", self._read_exact(8))[0]
                if length > self.MAX_FRAME_BYTES:
                    raise WebSocketProtocolError("MASSIVE_STREAM_FRAME_OVERSIZED")
                mask = self._read_exact(4) if second & 0x80 else None
                payload = self._read_exact(length)
                if mask:
                    payload = bytes(
                        value ^ mask[index % 4]
                        for index, value in enumerate(payload)
                    )
            except socket.timeout:
                return None
            if opcode == 0x8:
                raise WebSocketProtocolError("MASSIVE_STREAM_SERVER_CLOSED")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x1:
                fragments.extend(payload)
                started = True
            elif opcode == 0x0 and started:
                fragments.extend(payload)
            elif opcode == 0x2:
                continue
            else:
                raise WebSocketProtocolError("MASSIVE_STREAM_OPCODE_UNSUPPORTED")
            if fin:
                try:
                    return json.loads(fragments.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise WebSocketProtocolError("MASSIVE_STREAM_JSON_INVALID") from exc


class MassiveWebSocketStreamTransport:
    """One-consumer Massive WSS transport with bounded subscriptions/reconnects."""

    def __init__(
        self,
        authorizer: KeychainMassiveAuthorizer,
        *,
        websocket_url: str = "wss://socket.massive.com/stocks",
        connect_timeout_seconds: float = 5.0,
        maximum_symbols: int = 128,
        clock: Callable[[], datetime] | None = None,
        websocket_factory: Callable[..., _MinimalWebSocket] = _MinimalWebSocket,
    ) -> None:
        parsed = urlparse(websocket_url)
        if (
            parsed.scheme != "wss"
            or parsed.hostname not in {"socket.massive.com", "socket.polygon.io"}
            or parsed.path not in {"/stocks", "/stocks/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Massive stocks WSS URL is not approved")
        if not 0 < float(connect_timeout_seconds) <= 30:
            raise ValueError("Massive stream connect timeout must be in (0, 30]")
        if not 1 <= int(maximum_symbols) <= 512:
            raise ValueError("Massive stream symbol limit is invalid")
        self._authorizer = authorizer
        self._authorization = authorizer.evidence
        self.websocket_url = websocket_url.rstrip("/")
        self.connect_timeout_seconds = float(connect_timeout_seconds)
        self.maximum_symbols = int(maximum_symbols)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._websocket_factory = websocket_factory
        self._socket: _MinimalWebSocket | None = None
        self._state_lock = threading.RLock()
        self._consumer_lock = threading.Lock()
        self._symbols: tuple[str, ...] = ()
        self._subscribed: tuple[str, ...] = ()
        self._connected = False
        self._authenticated = False
        self._snapshot_resynced = False
        self._latest_quote_at: datetime | None = None
        self._latest_bar_at: datetime | None = None
        self._last_detail = "NOT_CONNECTED"

    @property
    def authorization(self) -> MassiveAuthorizationEvidence:
        return self._authorization

    def release_components(self) -> tuple[tuple[str, object, tuple[str, ...]], ...]:
        if self._websocket_factory is not _MinimalWebSocket:
            raise ValueError("custom Massive WSS client is not production-attestable")
        return (
            (
                "massive_stream_authorizer",
                self._authorizer,
                ("stream_credential", "authorize"),
            ),
        )

    @staticmethod
    def _normalize_symbols(symbols: Sequence[str], maximum: int) -> tuple[str, ...]:
        normalized = tuple(
            dict.fromkeys(str(value).strip().upper() for value in symbols)
        )
        if len(normalized) > maximum:
            raise ValueError("Massive stream subscription exceeds active-set limit")
        if any(
            not symbol
            or len(symbol) > 16
            or not symbol.replace(".", "").replace("-", "").isalnum()
            for symbol in normalized
        ):
            raise ValueError("Massive stream symbol is invalid")
        return normalized

    def set_symbols(self, symbols: Sequence[str]) -> None:
        normalized = self._normalize_symbols(symbols, self.maximum_symbols)
        with self._state_lock:
            if normalized == self._symbols:
                return
            previous = set(self._subscribed)
            self._symbols = normalized
            socket_client = self._socket if self._authenticated else None
            if socket_client is None:
                self._snapshot_resynced = False
                return
            requested = set(normalized)
            removed = sorted(previous - requested)
            added = sorted(requested - previous)
            try:
                if removed:
                    socket_client.send_json(
                        {
                            "action": "unsubscribe",
                            "params": ",".join(
                                [*(f"Q.{item}" for item in removed), *(f"AM.{item}" for item in removed)]
                            ),
                        }
                    )
                if added:
                    socket_client.send_json(
                        {
                            "action": "subscribe",
                            "params": ",".join(
                                [*(f"Q.{item}" for item in added), *(f"AM.{item}" for item in added)]
                            ),
                        }
                    )
                self._subscribed = normalized
                self._snapshot_resynced = False
            except Exception:
                self._disconnect_locked("SUBSCRIPTION_UPDATE_FAILED")
                raise MassiveStoreError("MASSIVE_STREAM_SUBSCRIPTION_UPDATE_FAILED")

    def _disconnect_locked(self, detail: str) -> None:
        socket_client = self._socket
        self._socket = None
        self._connected = False
        self._authenticated = False
        self._snapshot_resynced = False
        self._subscribed = ()
        self._last_detail = detail
        if socket_client is not None:
            socket_client.close()

    @staticmethod
    def _status_records(value: object) -> tuple[Mapping[str, Any], ...]:
        if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
            raise WebSocketProtocolError("MASSIVE_STREAM_STATUS_INVALID")
        return tuple(value)

    def _receive_status(self, socket_client: _MinimalWebSocket, expected: str) -> None:
        deadline = time.monotonic() + self.connect_timeout_seconds
        while time.monotonic() < deadline:
            value = socket_client.receive_json(
                timeout_seconds=max(0.05, deadline - time.monotonic())
            )
            if value is None:
                continue
            for item in self._status_records(value):
                if str(item.get("ev", "")).lower() != "status":
                    continue
                status = str(item.get("status", "")).lower()
                if status in {"auth_failed", "failed", "error"}:
                    raise WebSocketProtocolError("MASSIVE_STREAM_AUTH_REJECTED")
                if status == expected:
                    return
        raise WebSocketProtocolError("MASSIVE_STREAM_STATUS_TIMEOUT")

    def _connect_locked(self) -> None:
        if self._authenticated and self._socket is not None:
            return
        socket_client = self._websocket_factory(
            self.websocket_url, timeout_seconds=self.connect_timeout_seconds
        )
        try:
            socket_client.connect()
            self._receive_status(socket_client, "connected")
            socket_client.send_json(
                {"action": "auth", "params": self._authorizer.stream_credential()}
            )
            self._receive_status(socket_client, "auth_success")
            if self._symbols:
                socket_client.send_json(
                    {
                        "action": "subscribe",
                        "params": ",".join(
                            [
                                *(f"Q.{item}" for item in self._symbols),
                                *(f"AM.{item}" for item in self._symbols),
                            ]
                        ),
                    }
                )
            self._socket = socket_client
            self._connected = True
            self._authenticated = True
            self._subscribed = self._symbols
            self._snapshot_resynced = False
            self._last_detail = "AUTHENTICATED"
        except Exception as exc:
            socket_client.close()
            self._disconnect_locked(type(exc).__name__.upper())
            raise MassiveStoreError("MASSIVE_STREAM_CONNECTION_FAILED") from exc

    @staticmethod
    def _provider_timestamp(value: object) -> datetime | None:
        if isinstance(value, bool):
            return None
        try:
            integer = int(value)
            divisor = 1_000_000_000 if integer >= 10**15 else 1_000
            return datetime.fromtimestamp(integer / divisor, timezone.utc)
        except (OSError, OverflowError, TypeError, ValueError):
            return None

    @classmethod
    def _provider_event_time(cls, raw: Mapping[str, Any]) -> datetime | None:
        return cls._provider_timestamp(raw.get("sip_timestamp", raw.get("t")))

    @classmethod
    def _completed_am_end(
        cls, raw: Mapping[str, Any], *, received_at: datetime
    ) -> datetime | None:
        """Return the causal minute end only for a complete, received AM event."""

        if str(raw.get("ev", "")).upper() != "AM":
            return None
        start = cls._provider_timestamp(raw.get("s"))
        provider_end = cls._provider_timestamp(raw.get("e"))
        if (
            start is None
            or provider_end is None
            or start.second != 0
            or start.microsecond != 0
        ):
            return None
        minute_end = start + timedelta(minutes=1)
        if (
            provider_end < minute_end - timedelta(seconds=1)
            or provider_end > minute_end + timedelta(seconds=1)
            or _aware(received_at, "Massive stream receipt") < minute_end
        ):
            return None
        return minute_end

    def drain(
        self, *, limit: int, timeout_seconds: float
    ) -> Sequence[Mapping[str, Any]]:
        if not 1 <= int(limit) <= 10_000:
            raise ValueError("Massive stream drain limit is invalid")
        if not 0 <= float(timeout_seconds) <= 5:
            raise ValueError("Massive stream drain timeout is invalid")
        if not self._consumer_lock.acquire(blocking=False):
            raise MassiveStoreError("MASSIVE_STREAM_MULTIPLE_CONSUMERS")
        try:
            with self._state_lock:
                self._connect_locked()
                socket_client = self._socket
            assert socket_client is not None
            deadline = time.monotonic() + float(timeout_seconds)
            events: list[Mapping[str, Any]] = []
            while len(events) < int(limit):
                remaining = max(0.0, deadline - time.monotonic())
                if timeout_seconds and remaining <= 0:
                    break
                try:
                    value = socket_client.receive_json(
                        timeout_seconds=(remaining if timeout_seconds else 0.001)
                    )
                except Exception as exc:
                    with self._state_lock:
                        self._disconnect_locked(type(exc).__name__.upper())
                    raise MassiveStoreError("MASSIVE_STREAM_READ_FAILED") from exc
                if value is None:
                    break
                received_at = _aware(self._clock(), "Massive stream receipt clock")
                records = self._status_records(value)
                for raw in records:
                    kind = str(raw.get("ev", "")).upper()
                    if kind == "STATUS":
                        continue
                    if kind not in {"Q", "AM", "A"}:
                        continue
                    symbol = str(raw.get("sym", "")).strip().upper()
                    with self._state_lock:
                        if symbol not in self._symbols:
                            continue
                    events.append(dict(raw))
                    event_at = self._provider_event_time(raw)
                    with self._state_lock:
                        if kind == "Q" and event_at is not None:
                            self._latest_quote_at = max(
                                filter(None, (self._latest_quote_at, event_at))
                            )
                        elif kind == "AM":
                            completed_end = self._completed_am_end(
                                raw, received_at=received_at
                            )
                            # Transport health mirrors the stricter cache rule:
                            # a minute is not complete merely because an AM
                            # frame arrived or its start time is old enough.
                            if completed_end is None:
                                continue
                            self._latest_bar_at = max(
                                filter(
                                    None,
                                    (self._latest_bar_at, completed_end),
                                )
                            )
                        self._snapshot_resynced = bool(self._subscribed)
                    if len(events) >= int(limit):
                        break
            return tuple(events)
        finally:
            self._consumer_lock.release()

    def status(self, *, now: datetime) -> MassiveStreamStatus:
        current = _aware(now, "Massive stream status time")
        with self._state_lock:
            return MassiveStreamStatus(
                checked_at=current,
                authorization_binding_id=self._authorization.binding_id,
                connected=self._connected,
                authenticated=self._authenticated,
                snapshot_resynced=self._snapshot_resynced,
                latest_quote_at=self._latest_quote_at,
                latest_completed_bar_at=self._latest_bar_at,
                detail=self._last_detail,
            )

    def probe_authentication(self, *, timeout_seconds: float = 5.0) -> datetime:
        if not 0 < float(timeout_seconds) <= 30:
            raise ValueError("Massive probe timeout is invalid")
        with self._state_lock:
            previous = self.connect_timeout_seconds
            self.connect_timeout_seconds = float(timeout_seconds)
            try:
                self._connect_locked()
                return _aware(self._clock(), "Massive stream probe clock")
            finally:
                self.connect_timeout_seconds = previous

    def close(self) -> None:
        with self._state_lock:
            self._disconnect_locked("CLOSED")


class GmailDesktopOAuthAuthorizer:
    """Refresh-aware Gmail desktop OAuth custodian using only gmail.send."""

    TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

    def __init__(
        self,
        keychain: MacOSKeychain,
        *,
        client_item: KeychainItem,
        refresh_token_item: KeychainItem,
        consent_status_item: KeychainItem,
        timeout_seconds: float = 10.0,
        opener: Callable[..., Any] = urlopen,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not 0 < float(timeout_seconds) <= 30:
            raise ValueError("Gmail OAuth timeout must be in (0, 30]")
        self._client_item = client_item
        self._refresh_item = refresh_token_item
        self._consent_item = consent_status_item
        self._timeout_seconds = float(timeout_seconds)
        self._opener = opener
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        client_raw = keychain.read_text(client_item)
        refresh = keychain.read_text(refresh_token_item).strip()
        consent = keychain.read_text(consent_status_item).strip().lower()
        try:
            parsed = json.loads(client_raw)
        except json.JSONDecodeError as exc:
            raise CredentialUnavailable("CREDENTIAL_GMAIL_CLIENT_JSON_INVALID") from exc
        installed = parsed.get("installed") if isinstance(parsed, Mapping) else None
        if not isinstance(installed, Mapping):
            raise CredentialUnavailable("CREDENTIAL_GMAIL_CLIENT_NOT_DESKTOP")
        self._client_id = str(installed.get("client_id", "")).strip()
        self._client_secret = str(installed.get("client_secret", "")).strip()
        token_uri = str(installed.get("token_uri", "")).strip()
        redirect_uris = installed.get("redirect_uris")
        if (
            not self._client_id
            or not self._client_secret
            or token_uri != self.TOKEN_ENDPOINT
            or not isinstance(redirect_uris, list)
            or not any(str(value).startswith("http://localhost") for value in redirect_uris)
        ):
            raise CredentialUnavailable("CREDENTIAL_GMAIL_DESKTOP_CLIENT_INVALID")
        if not refresh or consent not in {"production", "internal"}:
            raise CredentialUnavailable("CREDENTIAL_GMAIL_DURABLE_CONSENT_UNVERIFIED")
        self._refresh_token = refresh
        self._access_token: str | None = None
        self._expires_at: datetime | None = None
        self._lock = threading.Lock()
        self._evidence = GmailAuthorizationEvidence(
            binding_id=_binding(
                {
                    "provider": "gmail",
                    "client_id_fingerprint": hashlib.sha256(
                        self._client_id.encode("utf-8")
                    ).hexdigest(),
                    "refresh_token_fingerprint": hashlib.sha256(
                        refresh.encode("utf-8")
                    ).hexdigest(),
                    "credential_sources": (
                        client_item.source_label,
                        refresh_token_item.source_label,
                        consent_status_item.source_label,
                    ),
                    "scope": GMAIL_SEND_SCOPE,
                    "consent_status": consent,
                }
            ),
            credential_source="+".join(
                (
                    client_item.source_label,
                    refresh_token_item.source_label,
                    consent_status_item.source_label,
                )
            ),
            scopes=(GMAIL_SEND_SCOPE,),
            authenticated=True,
        )

    @property
    def evidence(self) -> GmailAuthorizationEvidence:
        return self._evidence

    def release_components(self) -> tuple[tuple[str, object, tuple[str, ...]], ...]:
        if self._opener is not urlopen:
            raise ValueError("custom Gmail OAuth opener is not production-attestable")
        return ()

    def _refresh_locked(self) -> None:
        body = urlencode(
            {
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": self._refresh_token,
                "grant_type": "refresh_token",
            }
        ).encode("ascii")
        request = Request(
            self.TOKEN_ENDPOINT,
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "titan-full-live/2",
            },
        )
        try:
            with self._opener(request, timeout=self._timeout_seconds) as response:
                payload = response.read(1024 * 1024 + 1)
        except Exception as exc:
            raise NotificationDeliveryError(
                "NOTIFICATION_GMAIL_OAUTH_REFRESH_FAILED"
            ) from exc
        if len(payload) > 1024 * 1024:
            raise NotificationDeliveryError("NOTIFICATION_GMAIL_OAUTH_RESPONSE_OVERSIZED")
        try:
            decoded = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NotificationDeliveryError(
                "NOTIFICATION_GMAIL_OAUTH_RESPONSE_INVALID"
            ) from exc
        if not isinstance(decoded, Mapping):
            raise NotificationDeliveryError("NOTIFICATION_GMAIL_OAUTH_RESPONSE_INVALID")
        access = str(decoded.get("access_token", "")).strip()
        token_type = str(decoded.get("token_type", "Bearer")).strip().lower()
        try:
            expires_in = int(decoded.get("expires_in", 0))
        except (TypeError, ValueError) as exc:
            raise NotificationDeliveryError(
                "NOTIFICATION_GMAIL_OAUTH_EXPIRY_INVALID"
            ) from exc
        response_scopes = tuple(str(decoded.get("scope", "")).split())
        if (
            not access
            or token_type != "bearer"
            or not 60 <= expires_in <= 86400
            or (response_scopes and set(response_scopes) != {GMAIL_SEND_SCOPE})
        ):
            raise NotificationDeliveryError("NOTIFICATION_GMAIL_OAUTH_GRANT_INVALID")
        now = _aware(self._clock(), "Gmail OAuth clock")
        self._access_token = access
        self._expires_at = now + timedelta(seconds=expires_in)

    def _token(self) -> str:
        with self._lock:
            now = _aware(self._clock(), "Gmail OAuth clock")
            if (
                self._access_token is None
                or self._expires_at is None
                or self._expires_at <= now + timedelta(seconds=60)
            ):
                self._refresh_locked()
            assert self._access_token is not None
            return self._access_token

    def authorize(self, headers: Mapping[str, str]) -> Mapping[str, str]:
        return {**dict(headers), "Authorization": "Bearer " + self._token()}

    def probe_refresh(self) -> datetime:
        # Force a real refresh rather than accepting a cached constructor.
        with self._lock:
            self._access_token = None
            self._expires_at = None
        self._token()
        return _aware(self._clock(), "Gmail OAuth probe clock")


__all__ = [
    "CredentialUnavailable",
    "GMAIL_SEND_SCOPE",
    "GmailDesktopOAuthAuthorizer",
    "KeychainItem",
    "KeychainMassiveAuthorizer",
    "MacOSKeychain",
    "MassiveWebSocketStreamTransport",
    "WebSocketProtocolError",
]
