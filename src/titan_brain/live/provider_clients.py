"""Concrete, secret-custodied clients for the local full-live assembly.

Only provider-supported HTTPS/WSS endpoints are accepted.  Secrets are read
from explicitly named macOS Keychain items, retained in process memory, and
never included in exceptions, reports, configuration, command arguments, or
logs.  This module intentionally contains no broker mutation implementation:
the locally verified Robinhood MCP contract still requires attended approval.
"""

from __future__ import annotations

import base64
from collections import deque
import ctypes
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
import sys
import threading
import time
from typing import Any, Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from .massive_adapter import (
    MassiveAuthorizationEvidence,
    MassiveStreamStatus,
    MassiveStoreError,
)
from .notifications import GmailAuthorizationEvidence, NotificationDeliveryError


GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_KEYCHAIN_SERVICE_FIELDS = (
    "desktop_client_service", "refresh_token_service", "consent_status_service",
    "sender_service", "destination_service",
)
_GMAIL_OAUTH_ERROR_RESPONSE_MAX_BYTES = 64 * 1024
_GMAIL_OAUTH_PROVIDER_ERROR_CODES = frozenset(
    {
        "access_denied",
        "admin_policy_enforced",
        "deleted_client",
        "invalid_client",
        "invalid_grant",
        "invalid_request",
        "invalid_scope",
        "org_internal",
        "temporarily_unavailable",
        "unauthorized_client",
        "unsupported_grant_type",
    }
)


def gmail_desktop_loopback_uris_valid(value: object) -> bool:
    """Validate downloaded Desktop client loopback declarations, not prefixes."""
    return (type(value) is list and bool(value)
            and all(type(uri) is str and uri in {
                "http://localhost", "http://127.0.0.1", "http://[::1]"
            } for uri in value))


def _unique_json_object(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _gmail_oauth_http_failure_code(error: HTTPError) -> str:
    """Reduce an OAuth HTTP failure to status plus an allow-listed code.

    The response body, description, headers and URL are never returned or
    retained by the replacement exception. Malformed, duplicate-key and
    oversized bodies keep only the numeric HTTP status.
    """

    status = error.code
    status_text = str(status) if type(status) is int and 100 <= status <= 599 else "UNKNOWN"
    provider_code: str | None = None
    try:
        payload = error.read(_GMAIL_OAUTH_ERROR_RESPONSE_MAX_BYTES + 1)
        if type(payload) is bytes and len(payload) <= _GMAIL_OAUTH_ERROR_RESPONSE_MAX_BYTES:
            decoded = json.loads(payload, object_pairs_hook=_unique_json_object)
            if type(decoded) is dict:
                candidate = decoded.get("error")
                if type(candidate) is str and candidate in _GMAIL_OAUTH_PROVIDER_ERROR_CODES:
                    provider_code = candidate.upper()
    except Exception:
        provider_code = None
    finally:
        try:
            error.close()
        except Exception:
            pass
    suffix = provider_code or "UNCLASSIFIED"
    return f"NOTIFICATION_GMAIL_OAUTH_HTTP_{status_text}_{suffix}"


def _gmail_oauth_transport_failure_code(error: BaseException) -> str:
    """Classify transport shape without stringifying untrusted exceptions."""

    if isinstance(error, (TimeoutError, socket.timeout)):
        return "NOTIFICATION_GMAIL_OAUTH_REFRESH_TIMEOUT"
    if isinstance(error, ssl.SSLError):
        return "NOTIFICATION_GMAIL_OAUTH_REFRESH_TLS_FAILED"
    if isinstance(error, URLError):
        reason = error.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return "NOTIFICATION_GMAIL_OAUTH_REFRESH_TIMEOUT"
        if isinstance(reason, ssl.SSLError):
            return "NOTIFICATION_GMAIL_OAUTH_REFRESH_TLS_FAILED"
        if isinstance(reason, socket.gaierror):
            return "NOTIFICATION_GMAIL_OAUTH_REFRESH_DNS_FAILED"
        return "NOTIFICATION_GMAIL_OAUTH_REFRESH_NETWORK_FAILED"
    return "NOTIFICATION_GMAIL_OAUTH_REFRESH_FAILED"


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

    def metadata_status(self, item: KeychainItem) -> str:
        """Check one exact locator without requesting or retaining its secret."""

        arguments = [self.SECURITY, "find-generic-password", "-s", item.service]
        if item.account:
            arguments.extend(("-a", item.account))
        try:
            result = subprocess.run(
                arguments, check=False, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.SubprocessError):
            return "UNAVAILABLE"
        if result.returncode == 0:
            return "PRESENT"
        # errSecItemNotFound (-25300) is returned as its low eight bits by security.
        return "MISSING" if result.returncode == 44 else "UNAVAILABLE"

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


IBKR_GMAIL_NATIVE_ITEMS = frozenset(
    KeychainItem("titan-full-live-ibkr-ending-3103-gmail-" + suffix, "ibkr-live-ending-3103")
    for suffix in ("desktop-client", "refresh-token", "consent-status", "sender", "destination")
)
IBKR_CONTROL_NATIVE_ITEM = KeychainItem(
    "titan-full-live-ibkr-ending-3103-control-authentication-key",
    "ibkr-live-ending-3103",
)


def _native_keychain_symbol(library: Any, name: str, *, address: bool = False) -> int:
    """Resolve public CF constants and callback structures, without copying them."""
    if address:
        return ctypes.addressof(ctypes.c_byte.in_dll(library, name))
    value = ctypes.c_void_p.in_dll(library, name).value
    if value is None:
        raise CredentialUnavailable("CREDENTIAL_GMAIL_NATIVE_KEYCHAIN_UNAVAILABLE")
    return value


class _ScopedNativeMacOSKeychain(MacOSKeychain):
    """Private read-only FFI shared by separately fixed-scope custodians.

    Each SecItemCopyMatching query requests authentication-UI failure. This
    changes no ACL or process-wide interaction setting. Legacy file-Keychain
    behavior must also pass an attended, bounded production-reader probe before
    a worker is enabled; setup readback alone does not establish runtime access.
    The native call has no Python-level timeout; the inherited subprocess
    timeout does not qualify it as universally nonblocking.
    """

    _native_items: frozenset[KeychainItem] = frozenset()
    _native_error_prefix = "CREDENTIAL_NATIVE_KEYCHAIN"

    def _error(self, suffix: str) -> CredentialUnavailable:
        return CredentialUnavailable(f"{self._native_error_prefix}_{suffix}")

    def _symbol(self, library: Any, name: str, *, address: bool = False) -> int:
        try:
            return _native_keychain_symbol(library, name, address=address)
        except CredentialUnavailable:
            raise self._error("UNAVAILABLE") from None

    def _query(self, item: KeychainItem, *, return_data: bool) -> bytes:
        if item not in self._native_items:
            raise self._error("SCOPE_REFUSED")
        if sys.platform != "darwin":
            raise self._error("UNAVAILABLE")
        try:
            security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
            core = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
            copy = security.SecItemCopyMatching
            copy.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
            copy.restype = ctypes.c_int32
            string = core.CFStringCreateWithCString
            string.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
            string.restype = ctypes.c_void_p
            dictionary = core.CFDictionaryCreate
            dictionary.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
                                   ctypes.POINTER(ctypes.c_void_p), ctypes.c_long,
                                   ctypes.c_void_p, ctypes.c_void_p]
            dictionary.restype = ctypes.c_void_p
            core.CFGetTypeID.argtypes = [ctypes.c_void_p]
            core.CFGetTypeID.restype = ctypes.c_ulong
            core.CFDataGetTypeID.argtypes = []
            core.CFDataGetTypeID.restype = ctypes.c_ulong
            core.CFDataGetLength.argtypes = [ctypes.c_void_p]
            core.CFDataGetLength.restype = ctypes.c_long
            core.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
            core.CFDataGetBytePtr.restype = ctypes.c_void_p
            release = core.CFRelease
            release.argtypes = [ctypes.c_void_p]
            release.restype = None
            owned: list[int] = []
            result = ctypes.c_void_p()
            try:
                service = string(None, item.service.encode("utf-8"), 0x08000100)
                if not service:
                    raise self._error("UNAVAILABLE")
                owned.append(service)
                account = string(None, item.account.encode("utf-8"), 0x08000100)
                if not account:
                    raise self._error("UNAVAILABLE")
                owned.append(account)
                sec = lambda name: self._symbol(security, name)
                cf = lambda name: self._symbol(core, name)
                pairs = [
                    (sec("kSecClass"), sec("kSecClassGenericPassword")),
                    (sec("kSecAttrService"), service),
                    (sec("kSecAttrAccount"), account),
                    (sec("kSecMatchLimit"), sec("kSecMatchLimitOne")),
                    (sec("kSecUseAuthenticationUI"), sec("kSecUseAuthenticationUIFail")),
                    (sec("kSecUseDataProtectionKeychain"), cf("kCFBooleanFalse")),
                    (sec("kSecAttrSynchronizable"), cf("kCFBooleanFalse")),
                ]
                if return_data:
                    pairs.append((sec("kSecReturnData"), cf("kCFBooleanTrue")))
                keys = (ctypes.c_void_p * len(pairs))(*(key for key, _ in pairs))
                values = (ctypes.c_void_p * len(pairs))(*(value for _, value in pairs))
                query = dictionary(None, keys, values, len(pairs),
                                   self._symbol(core, "kCFTypeDictionaryKeyCallBacks", address=True),
                                   self._symbol(core, "kCFTypeDictionaryValueCallBacks", address=True))
                if not query:
                    raise self._error("UNAVAILABLE")
                owned.append(query)
                status = copy(query, ctypes.byref(result) if return_data else None)
                if status == -25300:
                    raise self._error("ITEM_MISSING")
                if status in (-25308, -25293, -128):
                    raise self._error("OWNER_AUTH_REQUIRED")
                if status != 0:
                    raise self._error("READ_FAILED")
                if not return_data:
                    return b""
                if not result.value or core.CFGetTypeID(result) != core.CFDataGetTypeID():
                    raise self._error("ITEM_INVALID")
                length = core.CFDataGetLength(result)
                if not 0 < length <= min(self.maximum_bytes, 65536):
                    raise self._error("ITEM_INVALID")
                data = core.CFDataGetBytePtr(result)
                if not data:
                    raise self._error("ITEM_INVALID")
                value = ctypes.string_at(data, length)
                if b"\x00" in value:
                    raise self._error("ITEM_INVALID")
                return value
            finally:
                # SecItemCopyMatching returns immutable CFData. Release it;
                # writing through CFDataGetBytePtr would violate its API.
                if result.value:
                    release(result)
                for reference in reversed(owned):
                    release(reference)
        except CredentialUnavailable:
            raise
        except Exception:
            raise self._error("READ_FAILED") from None

    def read(self, item: KeychainItem) -> bytes:
        return self._query(item, return_data=True)

    def read_text(self, item: KeychainItem) -> str:
        try:
            return self.read(item).decode("utf-8")
        except UnicodeDecodeError:
            raise self._error("ITEM_NOT_UTF8") from None

    def metadata_status(self, item: KeychainItem) -> str:
        try:
            self._query(item, return_data=False)
            return "PRESENT"
        except CredentialUnavailable as exc:
            return "MISSING" if exc.code == f"{self._native_error_prefix}_ITEM_MISSING" else "UNAVAILABLE"


class GmailNativeMacOSKeychain(_ScopedNativeMacOSKeychain):
    """Read-only custody for exactly the five enrolled IBKR Gmail items."""

    _native_items = IBKR_GMAIL_NATIVE_ITEMS
    _native_error_prefix = "CREDENTIAL_GMAIL_NATIVE_KEYCHAIN"

    def __init__(self, *, items: Sequence[KeychainItem], maximum_bytes: int = 65536):
        super().__init__(maximum_bytes=maximum_bytes)
        if len(items) != 5 or frozenset(items) != IBKR_GMAIL_NATIVE_ITEMS:
            raise self._error("SCOPE_REFUSED")


class IbkrControlNativeMacOSKeychain(_ScopedNativeMacOSKeychain):
    """Read only the exact enrolled IBKR control key using existing OS ACLs.

    No setup/create API, authorization binding, signing, or runtime-readiness
    assertion belongs to this reader. Authentication requirements fail closed.
    """

    _native_items = frozenset({IBKR_CONTROL_NATIVE_ITEM})
    _native_error_prefix = "CREDENTIAL_IBKR_CONTROL_NATIVE_KEYCHAIN"

    def __init__(self, *, item: KeychainItem):
        super().__init__(maximum_bytes=4096)
        if item != IBKR_CONTROL_NATIVE_ITEM:
            raise self._error("SCOPE_REFUSED")


def select_account_gmail_profile(
    profile: Mapping[str, Any], full_live: Mapping[str, Any]
) -> tuple[str, Mapping[str, Any]]:
    """Bind Gmail credential locators to the exact selected trading account."""

    account = full_live.get("account", {})
    if not isinstance(account, Mapping):
        raise ValueError("GMAIL_ACCOUNT_NAMESPACE_INVALID")
    account_key = account.get("account_key", account.get("masked_identifier"))
    supported = {
        "ending-7153": ("gmail", "7153"),
        "ibkr-live-ending-3103": ("ibkr_gmail", "3103"),
    }
    if not isinstance(account_key, str) or account_key not in supported:
        raise ValueError("GMAIL_ACCOUNT_NAMESPACE_INVALID")
    profile_key, last4 = supported[account_key]
    if account.get("required_last4") != last4:
        raise ValueError("GMAIL_ACCOUNT_NAMESPACE_INVALID")
    selected = profile.get(profile_key)
    if not isinstance(selected, Mapping):
        raise ValueError("GMAIL_ACCOUNT_PROFILE_MISSING")
    if (
        selected.get("credential_account") != account_key
        or selected.get("credential_backend") != "macos_keychain"
        or selected.get("implementation_id")
        != "titan.gmail_api.desktop_oauth.keychain_refresh.v1"
        or tuple(selected.get("scopes", ())) != (GMAIL_SEND_SCOPE,)
    ):
        raise ValueError("GMAIL_ACCOUNT_PROFILE_INVALID")
    for field in GMAIL_KEYCHAIN_SERVICE_FIELDS:
        service = selected.get(field)
        if not isinstance(service, str):
            raise ValueError("GMAIL_ACCOUNT_PROFILE_INVALID")
        KeychainItem(service=service, account=account_key)
    if profile_key == "ibkr_gmail":
        legacy = profile.get("gmail", {})
        legacy_services = (
            {legacy.get(field) for field in GMAIL_KEYCHAIN_SERVICE_FIELDS}
            if isinstance(legacy, Mapping) else set()
        )
        if selected.get("account_key") != account_key or any(
            selected.get(field) in legacy_services
            for field in GMAIL_KEYCHAIN_SERVICE_FIELDS
        ):
            raise ValueError("GMAIL_ACCOUNT_PROFILE_NOT_ISOLATED")
    return profile_key, selected


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
                    value = json.loads(fragments.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise WebSocketProtocolError("MASSIVE_STREAM_JSON_INVALID") from None
                if value is None:
                    # None is reserved for no frame/socket timeout, not a
                    # provider's malformed JSON null message.
                    raise WebSocketProtocolError("MASSIVE_STREAM_FRAME_INVALID")
                return value


class MassiveWebSocketStreamTransport:
    """One-consumer Massive WSS transport with bounded subscriptions/reconnects.

    Validate every record in a frame before exposing any of its data. Runtime
    status records must be explicit ``success`` messages; unknown/negative
    statuses invalidate the connection. Handshake frames must contain only
    their expected-stage status, never silently discarded market data. This
    intentionally fails closed on protocol additions requiring review.
    """

    MAX_PENDING_RECORDS = 100_000

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
        # Retain a received JSON frame's tail between bounded drain calls.
        # Read another frame only after this one is exhausted; the underlying
        # WebSocket frame-size limit therefore also bounds this pending queue.
        self._pending_records: deque[tuple[Mapping[str, Any], datetime]] = deque()
        self._stream_generation = 0

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
            self._pending_records.clear()
            self._stream_generation += 1
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
        self._latest_quote_at = None
        self._latest_bar_at = None
        self._subscribed = ()
        self._pending_records.clear()
        self._stream_generation += 1
        self._last_detail = detail
        if socket_client is not None:
            try:
                socket_client.close()
            except Exception:
                # State is already invalid. A close error must not replace a
                # sanitized failure with raw provider/socket exception text.
                pass

    def _frame_records(
        self, value: object, *, expected_status: str | None = None
    ) -> tuple[Mapping[str, Any], ...]:
        if not isinstance(value, list) or not value:
            raise WebSocketProtocolError("MASSIVE_STREAM_FRAME_INVALID")
        if len(value) > self.MAX_PENDING_RECORDS:
            raise WebSocketProtocolError("MASSIVE_STREAM_PENDING_RECORD_OVERFLOW")
        records = []
        for item in value:
            if not isinstance(item, Mapping):
                raise WebSocketProtocolError("MASSIVE_STREAM_FRAME_INVALID")
            event = item.get("ev")
            if not isinstance(event, str) or not event.strip():
                raise WebSocketProtocolError("MASSIVE_STREAM_FRAME_INVALID")
            if event.strip().upper() == "STATUS":
                status = item.get("status")
                if not isinstance(status, str) or not status.strip():
                    raise WebSocketProtocolError("MASSIVE_STREAM_STATUS_INVALID")
                allowed = expected_status if expected_status is not None else "success"
                if status.strip().lower() != allowed:
                    raise WebSocketProtocolError("MASSIVE_STREAM_STATUS_REJECTED")
            elif expected_status is not None:
                # Before subscribe, any data is unexpected and cannot be
                # discarded just because an auth_success precedes it.
                raise WebSocketProtocolError("MASSIVE_STREAM_HANDSHAKE_DATA_UNEXPECTED")
            records.append(dict(item))
        return tuple(records)

    def _receive_status(self, socket_client: _MinimalWebSocket, expected: str) -> None:
        deadline = time.monotonic() + self.connect_timeout_seconds
        while time.monotonic() < deadline:
            value = socket_client.receive_json(
                timeout_seconds=max(0.05, deadline - time.monotonic())
            )
            if value is None:
                continue
            # The entire frame must pass before any positive status counts.
            self._frame_records(value, expected_status=expected)
            return
        raise WebSocketProtocolError("MASSIVE_STREAM_STATUS_TIMEOUT")

    def _connect_locked(self) -> None:
        if self._authenticated and self._socket is not None:
            return
        self._pending_records.clear()
        self._stream_generation += 1
        socket_client = None
        try:
            socket_client = self._websocket_factory(
                self.websocket_url, timeout_seconds=self.connect_timeout_seconds
            )
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
        except Exception:
            if socket_client is not None:
                try:
                    socket_client.close()
                except Exception:
                    pass
            self._disconnect_locked("CONNECTION_FAILED")
            raise MassiveStoreError("MASSIVE_STREAM_CONNECTION_FAILED") from None

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
                generation = self._stream_generation
            assert socket_client is not None
            deadline = time.monotonic() + float(timeout_seconds)
            events: list[Mapping[str, Any]] = []
            while len(events) < int(limit):
                with self._state_lock:
                    if generation != self._stream_generation or socket_client is not self._socket:
                        raise MassiveStoreError("MASSIVE_STREAM_LIFECYCLE_CHANGED")
                    pending = self._pending_records.popleft() if self._pending_records else None
                if pending is None:
                    remaining = max(0.0, deadline - time.monotonic())
                    if timeout_seconds and remaining <= 0:
                        break
                    try:
                        value = socket_client.receive_json(
                            timeout_seconds=(remaining if timeout_seconds else 0.001)
                        )
                    except Exception:
                        with self._state_lock:
                            if generation == self._stream_generation and socket_client is self._socket:
                                self._disconnect_locked("READ_FAILED")
                        raise MassiveStoreError("MASSIVE_STREAM_READ_FAILED") from None
                    if value is None:
                        break
                    with self._state_lock:
                        if generation != self._stream_generation or socket_client is not self._socket:
                            raise MassiveStoreError("MASSIVE_STREAM_LIFECYCLE_CHANGED")
                        try:
                            received_at = _aware(self._clock(), "Massive stream receipt clock")
                            records = self._frame_records(value)
                        except Exception as exc:
                            # Only our fixed protocol codes may leave this
                            # boundary; never retain provider message text.
                            safe_codes = {
                                "MASSIVE_STREAM_FRAME_INVALID",
                                "MASSIVE_STREAM_STATUS_INVALID",
                                "MASSIVE_STREAM_STATUS_REJECTED",
                                "MASSIVE_STREAM_PENDING_RECORD_OVERFLOW",
                            }
                            code = str(exc) if isinstance(exc, WebSocketProtocolError) else ""
                            if code not in safe_codes:
                                code = "MASSIVE_STREAM_FRAME_INVALID"
                            self._disconnect_locked(code.removeprefix("MASSIVE_STREAM_"))
                            raise MassiveStoreError(code) from None
                        self._pending_records.extend((raw, received_at) for raw in records)
                    continue
                raw, received_at = pending
                with self._state_lock:
                    if generation != self._stream_generation or socket_client is not self._socket:
                        raise MassiveStoreError("MASSIVE_STREAM_LIFECYCLE_CHANGED")
                    kind = str(raw.get("ev", "")).upper()
                    if kind == "STATUS":
                        continue
                    if kind not in {"Q", "AM", "A"}:
                        continue
                    symbol = str(raw.get("sym", "")).strip().upper()
                    if symbol not in self._symbols:
                        continue
                    events.append(dict(raw))
                    event_at = self._provider_event_time(raw)
                    if kind == "Q" and event_at is not None:
                        self._latest_quote_at = max(
                            filter(None, (self._latest_quote_at, event_at))
                        )
                    elif kind == "AM":
                        completed_end = self._completed_am_end(
                            raw, received_at=received_at
                        )
                        # Use the frame's original receipt, not the later drain
                        # time, when deciding whether its minute was complete.
                        if completed_end is None:
                            continue
                        self._latest_bar_at = max(
                            filter(
                                None,
                                (self._latest_bar_at, completed_end),
                            )
                        )
                    self._snapshot_resynced = bool(self._subscribed)
            with self._state_lock:
                if generation != self._stream_generation or socket_client is not self._socket:
                    raise MassiveStoreError("MASSIVE_STREAM_LIFECYCLE_CHANGED")
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
            or not gmail_desktop_loopback_uris_valid(redirect_uris)
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
        failure_code: str | None = None
        try:
            with self._opener(request, timeout=self._timeout_seconds) as response:
                payload = response.read(1024 * 1024 + 1)
        except HTTPError as error:
            failure_code = _gmail_oauth_http_failure_code(error)
            payload = b""
        except Exception as error:
            failure_code = _gmail_oauth_transport_failure_code(error)
            payload = b""
        # Raise outside the provider exception handler so the replacement
        # exception retains no URL, response body, headers or provider text in
        # its cause/context chain.
        if failure_code is not None:
            raise NotificationDeliveryError(failure_code)
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
