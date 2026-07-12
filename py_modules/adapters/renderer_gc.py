"""CDP renderer adapter — ``RendererGcFn`` over the CEF debugger.

One operation on Steam's ``SharedJSContext`` renderer, driven through a minimal
Chrome DevTools Protocol client:

- **Garbage collect** (``HeapProfiler.collectGarbage``): settles the renderer heap
  so the session-budget gate's next RSS reading reflects retained memory rather
  than transient garbage. Steam's natural GC is measured-unreliable (sometimes
  minutes, absent for 12+ min); the explicit collect reclaims deterministically
  (measured: 496 MB in ~5 s on-device 2026-07-11).

Transport: the CEF remote-debugging endpoint on ``localhost:8080`` — a Decky
platform invariant (Decky Loader itself requires CEF debugging enabled), and
NSLGameScanner is production precedent for driving it. ``GET /json`` lists the
debuggable targets; the one titled ``SharedJSContext`` is the plugin-UI renderer.
Its ``webSocketDebuggerUrl`` is opened with a minimal, stdlib-only RFC 6455
client (a single request/response — vendoring a websocket package for one call is
not warranted).

Fail-open contract: every failure path returns ``False`` with a debug log and the
call never raises into the caller, never blocks more than a few seconds total.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
from typing import TYPE_CHECKING
from urllib.request import urlopen

if TYPE_CHECKING:
    import logging

_DEBUGGER_URL = "http://localhost:8080/json"
_TARGET_TITLE = "SharedJSContext"
# RFC 6455 handshake GUID — appended to the client key to derive the expected
# Sec-WebSocket-Accept.
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
# Per-operation timeout. Kept small so the whole attempt (HTTP list + connect
# + handshake + one round-trip) stays well under ~5 s even when every step waits.
_TIMEOUT_SEC = 1.5
# Every CDP command in this module uses request id 1 and awaits the id-1 reply.
_CDP_REQUEST_ID = 1
_GC_MESSAGE = json.dumps({"id": _CDP_REQUEST_ID, "method": "HeapProfiler.collectGarbage"})


class RendererGcAdapter:
    """Real ``RendererGcFn`` that drives ``HeapProfiler.collectGarbage`` over CDP."""

    def __init__(self, *, logger: logging.Logger) -> None:
        self._logger = logger

    def __call__(self) -> bool:
        """Force a renderer GC; return ``True`` on the acked collect, else ``False``.

        Fail-open: any transport, parse, or protocol error returns ``False`` and
        never escapes into the sync path.
        """
        return _run_cdp_command(_GC_MESSAGE, self._logger, "Renderer GC")


def _run_cdp_command(message: str, logger: logging.Logger, label: str) -> bool:
    """Find the SharedJSContext target and send *message*, awaiting its id-1 reply.

    The fail-open entry point the GC adapter uses: any transport, parse, or
    protocol error is caught and returns ``False`` with a debug log under *label*.
    """
    try:
        ws_url = _find_target_ws_url()
        if ws_url is None:
            logger.debug("%s skipped: no %s debug target", label, _TARGET_TITLE)
            return False
        return _send_cdp_command(ws_url, message, logger, label)
    except Exception as e:  # fail-open: never raise into the caller
        logger.debug("%s failed: %s", label, e)
        return False


def _find_target_ws_url() -> str | None:
    """Return the ``SharedJSContext`` target's ``webSocketDebuggerUrl``, or ``None``."""
    with urlopen(_DEBUGGER_URL, timeout=_TIMEOUT_SEC) as resp:  # fixed localhost URL
        targets = json.load(resp)
    for target in targets:
        if isinstance(target, dict) and target.get("title") == _TARGET_TITLE:
            url = target.get("webSocketDebuggerUrl")
            return url if isinstance(url, str) else None
    return None


def _send_cdp_command(ws_url: str, message: str, logger: logging.Logger, label: str) -> bool:
    """Open *ws_url*, send *message*, and await its id-matched reply."""
    host, port, path = _parse_ws_url(ws_url)
    sock = socket.create_connection((host, port), timeout=_TIMEOUT_SEC)
    try:
        sock.settimeout(_TIMEOUT_SEC)
        if not _handshake(sock, host, port, path):
            logger.debug("%s failed: WebSocket handshake rejected", label)
            return False
        sock.sendall(_encode_text_frame(message))
        return _await_cdp_reply(sock)
    finally:
        sock.close()


def _parse_ws_url(ws_url: str) -> tuple[str, int, str]:
    """Split ``ws://host:port/path`` into ``(host, port, path)``; port defaults to 80."""
    rest = ws_url.split("://", 1)[-1]
    authority, _, path = rest.partition("/")
    host, _, port_str = authority.partition(":")
    return host, int(port_str) if port_str else 80, "/" + path


def _handshake(sock: socket.socket, host: str, port: int, path: str) -> bool:
    """Perform the RFC 6455 Upgrade handshake; validate ``Sec-WebSocket-Accept``."""
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(request.encode("ascii"))
    response = _recv_until(sock, b"\r\n\r\n")
    if response is None:
        return False
    # Any bytes ``_recv_until`` read past the header terminator are discarded, and
    # that is safe here: the caller sends the CDP command only AFTER this handshake
    # returns, and we enable no CDP domains, so the server pushes nothing before
    # that request — the reply frame cannot have been buffered into ``response``
    # yet, so nothing is lost.
    # RFC 6455 fixes SHA-1 for Sec-WebSocket-Accept; not a security use.
    accept_digest = hashlib.sha1((key + _WS_GUID).encode("ascii"), usedforsecurity=False).digest()
    expected = base64.b64encode(accept_digest).decode("ascii")
    return expected.lower().encode("ascii") in response.lower()


def _recv_until(sock: socket.socket, terminator: bytes, limit: int = 8192) -> bytes | None:
    """Read from *sock* until *terminator* appears; ``None`` on EOF or overrun."""
    buffer = b""
    while terminator not in buffer:
        chunk = sock.recv(1024)
        if not chunk:
            return None
        buffer += chunk
        if len(buffer) > limit:
            return None
    return buffer


def _encode_text_frame(message: str) -> bytes:
    """Encode *message* as a masked client text frame (FIN=1, opcode=0x1)."""
    payload = message.encode("utf-8")
    length = len(payload)
    header = bytearray([0x81])  # FIN + text opcode
    if length < 126:
        header.append(0x80 | length)  # mask bit + 7-bit length
    else:
        # 16-bit extended length. The fixed CDP command is well under 126
        # bytes so this branch is not hit today, but it is kept so the encoder stays
        # a correct RFC 6455 client if the message ever grows. The 64-bit form is
        # never needed for a request this small.
        header.append(0x80 | 126)
        header.extend(struct.pack(">H", length))
    mask = os.urandom(4)
    header.extend(mask)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return bytes(header) + masked


def _await_cdp_reply(sock: socket.socket) -> bool:
    """Read server frames until the ``id``-matched command reply arrives.

    Returns ``True`` once a text frame carrying ``id == _CDP_REQUEST_ID`` is seen.
    Returns ``False`` on a closed/timed-out connection or any non-text /
    fragmented frame (fragmentation is not expected for this tiny reply — bail
    rather than reassemble).
    """
    for _ in range(8):  # bounded: the reply is the only expected message
        frame = _recv_frame(sock)
        if frame is None:
            return False
        opcode, payload = frame
        if opcode != 0x1:  # not a text frame (close / ping / continuation) — bail
            return False
        try:
            if json.loads(payload).get("id") == _CDP_REQUEST_ID:
                return True
        except (ValueError, TypeError):
            return False
    return False


def _recv_frame(sock: socket.socket) -> tuple[int, bytes] | None:
    """Read one unmasked server frame; return ``(opcode, payload)`` or ``None``.

    Handles the 7-bit and 16-bit length forms. The 64-bit form and masked server
    frames are not expected from CEF for this exchange, so either yields ``None``
    (the caller treats that as failure — fail-open).
    """
    head = _recv_exact(sock, 2)
    if head is None:
        return None
    opcode = head[0] & 0x0F
    masked = bool(head[1] & 0x80)
    length = head[1] & 0x7F
    if length == 126:
        ext = _recv_exact(sock, 2)
        if ext is None:
            return None
        length = struct.unpack(">H", ext)[0]
    elif length == 127:
        return None  # 64-bit length not expected for this reply
    if masked:
        return None  # server frames must not be masked
    payload = _recv_exact(sock, length) if length else b""
    return None if payload is None else (opcode, payload)


def _recv_exact(sock: socket.socket, count: int) -> bytes | None:
    """Read exactly *count* bytes; ``None`` on EOF before *count* are read."""
    buffer = b""
    while len(buffer) < count:
        chunk = sock.recv(count - len(buffer))
        if not chunk:
            return None
        buffer += chunk
    return buffer
