from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import threading
from typing import Any
from urllib.parse import urlparse


class WebSocketError(RuntimeError):
    pass


class MinimalWebSocket:
    """Dependency-free RFC 6455 client for Massive's JSON stream.

    It requests no extensions and supports text, continuation, ping/pong, and
    close frames. Client frames are masked as required by RFC 6455.
    """

    def __init__(self, url: str, connect_timeout: float = 15.0):
        self.url = url
        self.connect_timeout = connect_timeout
        self.sock: ssl.SSLSocket | socket.socket | None = None
        self._buffer = bytearray()
        self._send_lock = threading.Lock()

    def connect(self) -> None:
        parsed = urlparse(self.url)
        if parsed.scheme != "wss" or not parsed.hostname:
            raise WebSocketError("Only wss:// URLs are permitted")
        port = parsed.port or 443
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        raw = socket.create_connection((parsed.hostname, port), timeout=self.connect_timeout)
        context = ssl.create_default_context()
        sock = context.wrap_socket(raw, server_hostname=parsed.hostname)
        sock.settimeout(self.connect_timeout)

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "User-Agent: titan-momentum-watcher/0.1\r\n\r\n"
        ).encode("ascii")
        sock.sendall(request)
        response = bytearray()
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                sock.close()
                raise WebSocketError("Connection closed during WebSocket handshake")
            response.extend(chunk)
            if len(response) > 65536:
                sock.close()
                raise WebSocketError("Oversized WebSocket handshake")
        header_bytes, leftover = bytes(response).split(b"\r\n\r\n", 1)
        lines = header_bytes.decode("iso-8859-1").split("\r\n")
        if " 101 " not in lines[0]:
            sock.close()
            raise WebSocketError(f"WebSocket upgrade rejected: {lines[0]}")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        if headers.get("sec-websocket-accept") != expected:
            sock.close()
            raise WebSocketError("Invalid Sec-WebSocket-Accept response")
        self.sock = sock
        self._buffer.extend(leftover)

    def close(self) -> None:
        if not self.sock:
            return
        try:
            self._send_frame(0x8, b"")
        except OSError:
            pass
        try:
            self.sock.close()
        finally:
            self.sock = None

    def send_json(self, value: dict[str, Any]) -> None:
        self.send_text(json.dumps(value, separators=(",", ":")))

    def send_text(self, value: str) -> None:
        self._send_frame(0x1, value.encode("utf-8"))

    def ping(self, payload: bytes = b"titan") -> None:
        self._send_frame(0x9, payload[:125])

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if not self.sock:
            raise WebSocketError("WebSocket is not connected")
        first = 0x80 | opcode
        length = len(payload)
        mask_key = os.urandom(4)
        if length < 126:
            header = struct.pack("!BB", first, 0x80 | length)
        elif length <= 0xFFFF:
            header = struct.pack("!BBH", first, 0x80 | 126, length)
        else:
            header = struct.pack("!BBQ", first, 0x80 | 127, length)
        masked = bytes(byte ^ mask_key[index % 4] for index, byte in enumerate(payload))
        with self._send_lock:
            self.sock.sendall(header + mask_key + masked)

    def _recv_exact(self, count: int) -> bytes:
        if not self.sock:
            raise WebSocketError("WebSocket is not connected")
        while len(self._buffer) < count:
            chunk = self.sock.recv(max(4096, count - len(self._buffer)))
            if not chunk:
                raise WebSocketError("WebSocket connection closed")
            self._buffer.extend(chunk)
        result = bytes(self._buffer[:count])
        del self._buffer[:count]
        return result

    def _recv_frame(self) -> tuple[bool, int, bytes]:
        head = self._recv_exact(2)
        first, second = head
        fin = bool(first & 0x80)
        rsv = first & 0x70
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if rsv:
            raise WebSocketError("Unsupported WebSocket extension frame")
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        if length > 64 * 1024 * 1024:
            raise WebSocketError("WebSocket frame exceeds 64 MiB safety limit")
        mask = self._recv_exact(4) if masked else None
        payload = self._recv_exact(length)
        if mask:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return fin, opcode, payload

    def recv_text(self, timeout: float = 30.0) -> str | None:
        if not self.sock:
            raise WebSocketError("WebSocket is not connected")
        self.sock.settimeout(timeout)
        fragments = bytearray()
        text_started = False
        while True:
            try:
                fin, opcode, payload = self._recv_frame()
            except socket.timeout:
                return None
            if opcode == 0x8:
                raise WebSocketError("Server sent a close frame")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x1:
                fragments.extend(payload)
                text_started = True
            elif opcode == 0x0 and text_started:
                fragments.extend(payload)
            elif opcode == 0x2:
                continue
            else:
                raise WebSocketError(f"Unexpected WebSocket opcode {opcode}")
            if fin:
                return fragments.decode("utf-8")

    def recv_json(self, timeout: float = 30.0) -> Any | None:
        text = self.recv_text(timeout=timeout)
        return None if text is None else json.loads(text)

