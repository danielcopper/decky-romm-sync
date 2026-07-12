"""Unit tests for ``RendererGcAdapter`` — the hand-rolled CDP GC client.

Covers the pure frame codec / URL parsing, a real handshake + reply round-trip
over a ``socketpair`` (a stand-in for the CEF debugger socket), and every
fail-open path (unreachable debugger, no target, protocol violations) returning
``False`` without raising.
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import threading
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from adapters import renderer_gc
from adapters.renderer_gc import (
    _GC_MESSAGE,
    RendererGcAdapter,
    _await_cdp_reply,
    _encode_text_frame,
    _parse_ws_url,
    _recv_frame,
    _send_cdp_command,
)

if TYPE_CHECKING:
    import pytest

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _server_text_frame(message: str) -> bytes:
    """Build an unmasked server text frame (what CEF sends back)."""
    payload = message.encode("utf-8")
    length = len(payload)
    header = bytearray([0x81])
    if length < 126:
        header.append(length)
    else:
        header.append(126)
        header.extend(struct.pack(">H", length))
    return bytes(header) + payload


# ── _parse_ws_url ────────────────────────────────────────────────


def test_parse_ws_url_with_port() -> None:
    assert _parse_ws_url("ws://localhost:8080/devtools/page/ABC123") == ("localhost", 8080, "/devtools/page/ABC123")


def test_parse_ws_url_without_port_defaults_to_80() -> None:
    assert _parse_ws_url("ws://example/path") == ("example", 80, "/path")


# ── _encode_text_frame ───────────────────────────────────────────


def _decode_client_frame(frame: bytes) -> tuple[int, bytes]:
    """Inverse of ``_encode_text_frame`` — unmask and return (opcode, payload)."""
    opcode = frame[0] & 0x0F
    masked = bool(frame[1] & 0x80)
    length = frame[1] & 0x7F
    offset = 2
    if length == 126:
        length = struct.unpack(">H", frame[2:4])[0]
        offset = 4
    assert masked
    mask = frame[offset : offset + 4]
    body = frame[offset + 4 :]
    return opcode, bytes(b ^ mask[i % 4] for i, b in enumerate(body))


def test_encode_text_frame_short_payload_roundtrips() -> None:
    opcode, payload = _decode_client_frame(_encode_text_frame('{"id":1}'))
    assert opcode == 0x1
    assert payload == b'{"id":1}'


def test_encode_text_frame_extended_length_roundtrips() -> None:
    message = "x" * 200  # >125 forces the 16-bit length form
    frame = _encode_text_frame(message)
    assert frame[1] & 0x7F == 126
    _opcode, payload = _decode_client_frame(frame)
    assert payload == message.encode("utf-8")


# ── _recv_frame / _await_cdp_reply over a socketpair ──────────────


def test_recv_frame_reads_unmasked_server_text_frame() -> None:
    a, b = socket.socketpair()
    try:
        b.sendall(_server_text_frame('{"id":1,"result":{}}'))
        frame = _recv_frame(a)
        assert frame is not None
        opcode, payload = frame
        assert opcode == 0x1
        assert json.loads(payload)["id"] == 1
    finally:
        a.close()
        b.close()


def test_await_cdp_reply_true_on_id_match() -> None:
    a, b = socket.socketpair()
    try:
        b.sendall(_server_text_frame(json.dumps({"id": 1, "result": {}})))
        assert _await_cdp_reply(a) is True
    finally:
        a.close()
        b.close()


def test_await_cdp_reply_skips_other_ids_until_match() -> None:
    a, b = socket.socketpair()
    try:
        b.sendall(_server_text_frame(json.dumps({"id": 99, "result": {}})))
        b.sendall(_server_text_frame(json.dumps({"id": 1, "result": {}})))
        # A reply whose id is not ours is still a valid text frame, so the loop
        # continues to the next frame rather than bailing.
        assert _await_cdp_reply(a) is True
    finally:
        a.close()
        b.close()


def test_await_cdp_reply_false_on_close_frame() -> None:
    a, b = socket.socketpair()
    try:
        b.sendall(bytes([0x88, 0x00]))  # opcode 0x8 = close, empty payload
        assert _await_cdp_reply(a) is False
    finally:
        a.close()
        b.close()


def test_await_cdp_reply_false_on_eof() -> None:
    a, b = socket.socketpair()
    b.close()  # immediate EOF
    try:
        assert _await_cdp_reply(a) is False
    finally:
        a.close()


# ── __call__ fail-open paths ─────────────────────────────────────


def test_call_returns_false_when_debugger_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: object, **_k: object) -> object:
        raise OSError("connection refused")

    monkeypatch.setattr(renderer_gc, "urlopen", _boom)
    logger = MagicMock()
    assert RendererGcAdapter(logger=logger)() is False
    assert logger.debug.called


def test_call_returns_false_when_no_shared_js_context_target(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Resp:
        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *_a: object) -> None:
            return None

        def read(self) -> bytes:
            return b'[{"title": "Steam", "webSocketDebuggerUrl": "ws://x/y"}]'

    monkeypatch.setattr(renderer_gc, "urlopen", lambda *_a, **_k: _Resp())
    assert RendererGcAdapter(logger=MagicMock())() is False


# ── end-to-end handshake + reply over a socketpair ───────────────


def _roundtrip_cdp(monkeypatch: pytest.MonkeyPatch, message: str) -> tuple[bool, bytes]:
    """Drive ``_send_cdp_command`` against an in-thread WS server; return (result, request_frame)."""
    client, server = socket.socketpair()
    captured: dict[str, bytes] = {}

    def _serve() -> None:
        request = server.recv(4096).decode("ascii")
        key = ""
        for line in request.split("\r\n"):
            if line.lower().startswith("sec-websocket-key:"):
                key = line.split(":", 1)[1].strip()
        accept = base64.b64encode(hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()).decode("ascii")
        server.sendall(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode("ascii")
        )
        captured["frame"] = server.recv(4096)  # the masked command frame
        server.sendall(_server_text_frame(json.dumps({"id": 1, "result": {}})))

    thread = threading.Thread(target=_serve)
    thread.start()
    try:
        monkeypatch.setattr(renderer_gc.socket, "create_connection", lambda *_a, **_k: client)
        result = _send_cdp_command("ws://localhost:8080/devtools/page/ABC", message, MagicMock(), "test")
    finally:
        thread.join(timeout=2)
        client.close()
        server.close()
    return result, captured.get("frame", b"")


def test_send_cdp_command_collect_garbage_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    result, frame = _roundtrip_cdp(monkeypatch, _GC_MESSAGE)
    assert result is True
    _opcode, payload = _decode_client_frame(frame)
    assert json.loads(payload)["method"] == "HeapProfiler.collectGarbage"
